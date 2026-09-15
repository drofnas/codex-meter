package collector

import (
	"codex-usage-meter/internal/calendar"
	"math"
	"time"
)

// Calendar intervals belong to StableEnd, even while the public cycle is ambiguous.
// Value fields make candidate copies independent until persistence succeeds.
type history struct {
	State          string               `json:"state"`
	StableEnd      int64                `json:"stable_end"`
	BaselineUsable bool                 `json:"baseline_usable"`
	HighUsed       float64              `json:"high_used"`
	Timezone       string               `json:"timezone"`
	Count          int                  `json:"count"`
	Bounds         [9]calendar.Interval `json:"bounds"`
	Days           [9]historyDay        `json:"days"`
}

type historyDay struct {
	Used         float64 `json:"used"`
	Known        bool    `json:"known"`
	CoveredUntil int64   `json:"covered_until"`
}

// Compare against the fixed ledger anchor, never a moving previous timestamp.
// Daily estimates tolerate clock jitter without letting the anchor drift.
func compatibleReset(a, b int64) bool { return a >= b-300 && a <= b+300 }

func newHistory(obs Observation, state string, loc *time.Location) history {
	h := history{State: state, StableEnd: obs.ResetAt, BaselineUsable: true, HighUsed: obs.UsedPercent, Timezone: loc.String()}
	bounds := calendar.Bounds(obs.ResetAt, loc)
	h.Count = len(bounds)
	copy(h.Bounds[:], bounds)
	return h
}
func (h *history) rezone(loc *time.Location) {
	if h.StableEnd == 0 || h.Timezone == loc.String() {
		return
	}
	fresh := newHistory(Observation{ResetAt: h.StableEnd}, h.State, loc)
	fresh.BaselineUsable = false
	fresh.HighUsed = h.HighUsed
	for i := 0; i < fresh.Count; i++ {
		for j := 0; j < h.Count; j++ {
			if fresh.Bounds[i] == h.Bounds[j] {
				fresh.Days[i] = h.Days[j]
			}
		}
	}
	*h = fresh
}
func (h *history) accept(previous *Observation, obs Observation, maxGap int64, loc *time.Location) {
	h.rezone(loc)
	if previous == nil || previous.Scope != obs.Scope {
		*h = newHistory(obs, "observed", loc)
		return
	}
	if obs.ObservedAt <= previous.ObservedAt {
		return // Ordering/conflict decisions belong to the collector.
	}
	start := obs.ResetAt - week
	if obs.ObservedAt >= h.StableEnd && obs.ResetAt > h.StableEnd && start >= h.StableEnd-5 && start <= obs.ObservedAt+5 {
		*h = newHistory(obs, "confirmed", loc)
		return
	}
	if h.State == "ambiguous" {
		if obs.ObservedAt-previous.ObservedAt <= maxGap && obs.ResetAt == previous.ResetAt && obs.UsedPercent >= previous.UsedPercent {
			// Restart sampling without throwing away earlier calendar evidence.
			fresh := newHistory(obs, "observed", loc)
			if compatibleReset(obs.ResetAt, h.StableEnd) {
				fresh.HighUsed = max(h.HighUsed, obs.UsedPercent)
			}
			for i, b := range fresh.Bounds[:fresh.Count] {
				for j, old := range h.Bounds[:h.Count] {
					if b == old {
						fresh.Days[i] = h.Days[j]
					} else if compatibleReset(obs.ResetAt, h.StableEnd) && b.Start < obs.ObservedAt && max(b.Start, old.Start) < min(b.End, old.End) {
						fresh.Days[i] = historyDay{Used: h.Days[j].Used, Known: h.Days[j].Known}
					}
				}
			}
			*h = fresh
		}
		return
	}
	if !compatibleReset(obs.ResetAt, h.StableEnd) {
		h.State, h.BaselineUsable = "ambiguous", false
		return
	}
	if h.BaselineUsable && obs.ObservedAt-previous.ObservedAt <= maxGap && obs.UsedPercent >= h.HighUsed {
		from := max(previous.ObservedAt, max(h.StableEnd, previous.ResetAt, obs.ResetAt)-week)
		to := min(obs.ObservedAt, h.StableEnd, previous.ResetAt, obs.ResetAt)
		delta := obs.UsedPercent - h.HighUsed
		// Only measured zero can be clipped across an uncertain reset edge.
		if delta == 0 || from == previous.ObservedAt && to == obs.ObservedAt {
			h.interval(from, to, delta)
		}
	}
	h.HighUsed = max(h.HighUsed, obs.UsedPercent)
	h.BaselineUsable = h.State != "ambiguous"
}

func (h *history) interval(from, to int64, delta float64) {
	start := h.StableEnd - week
	// A skewed observation before the nominal start cannot attribute consumption
	// to this cycle. Zero is safe to clip to the start.
	if from < start {
		if delta > 0 {
			return
		}
		from = start
	}
	if to <= from {
		return
	}
	first, last := -1, -1
	for i := 0; i < h.Count; i++ {
		b := h.Bounds[i]
		if from >= b.Start && from < b.End {
			first = i
		}
		if to-1 >= b.Start && to-1 < b.End {
			last = i
		}
	}
	if first < 0 || last < 0 || delta > 0 && first != last {
		return
	}
	proposed := h.Days
	proposed[first].Used += delta
	total := 0.0
	for _, d := range proposed {
		total += d.Used
	}
	if total > 100+1e-9 {
		// Keep the existing estimates when an inconsistent increment cannot fit
		// the wire bound. The uncovered interval leaves this day partial.
		return
	}
	if total > 100 {
		// Summing fractional per-slot differences can overshoot 100 by a few
		// float ULPs. Remove only that roundoff, then stay inside the wire bound.
		proposed[first].Used = max(0, math.Nextafter(proposed[first].Used-(total-100), 0))
	}
	h.Days = proposed
	for i := first; i <= last; i++ {
		lo, hi := max(from, h.Bounds[i].Start), min(to, h.Bounds[i].End)
		d := &h.Days[i]
		d.Known = true
		if lo == h.Bounds[i].Start || d.CoveredUntil == lo {
			d.CoveredUntil = hi
		}
	}
}

func (h history) project(s *Snapshot) {
	if s.ObservedAt == nil {
		return
	}
	s.Cycle.State = h.State
	if h.State == "ambiguous" || s.Timezone != h.Timezone {
		return // snapshot() already supplies unknown/future slots at new bounds.
	}
	for i := range s.Days {
		if s.Days[i].Coverage == "future" {
			continue
		}
		for j, b := range h.Bounds[:h.Count] {
			d := h.Days[j]
			if max(*s.Days[i].StartAt, b.Start) >= min(*s.Days[i].EndAt, b.End) || !d.Known {
				continue
			}
			s.Days[i].UsedDelta, s.Days[i].Coverage = ptr(d.Used), "partial"
			if *s.Days[i].StartAt == b.Start && *s.Days[i].EndAt == b.End && d.CoveredUntil == b.End && s.AsOf >= b.End {
				s.Days[i].Coverage = "complete"
			}
			break
		}
	}
}

func (h history) valid(obs *Observation) bool {
	if obs == nil {
		return h == (history{})
	}
	if h.State != "observed" && h.State != "confirmed" && h.State != "ambiguous" || !validEpoch(h.StableEnd) || !validEpoch(h.StableEnd-week) {
		return false
	}
	if math.IsNaN(h.HighUsed) || math.IsInf(h.HighUsed, 0) || h.HighUsed < 0 || h.State != "ambiguous" && h.HighUsed < obs.UsedPercent {
		return false
	}
	if h.State != "ambiguous" && !compatibleReset(h.StableEnd, obs.ResetAt) || h.State == "ambiguous" && h.BaselineUsable {
		return false
	}
	loc, err := time.LoadLocation(h.Timezone)
	if err != nil {
		return false
	}
	bounds := calendar.Bounds(h.StableEnd, loc)
	if len(bounds) != h.Count || h.Count < 7 || h.Count > 9 {
		return false
	}
	total := 0.0
	for i, d := range h.Days {
		if i >= h.Count {
			if d != (historyDay{}) || h.Bounds[i] != (calendar.Interval{}) {
				return false
			}
			continue
		}
		if h.Bounds[i] != bounds[i] {
			return false
		}
		start, end := bounds[i].Start, bounds[i].End
		if math.IsNaN(d.Used) || math.IsInf(d.Used, 0) || d.Used < 0 || !d.Known && (d.Used != 0 || d.CoveredUntil != 0) {
			return false
		}
		if d.Known && start >= obs.ObservedAt || d.CoveredUntil != 0 && (d.CoveredUntil <= start || d.CoveredUntil > end || d.CoveredUntil > obs.ObservedAt) {
			return false
		}
		total += d.Used
	}
	return total <= 100
}
