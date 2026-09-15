package collector

import (
	"encoding/json"
	"errors"
	"math"
	"time"
)

const retentionSeconds = int64(35 * 86400)
const maxHistoryBytes = 65536
const maxPeriods = 8

type HistorySnapshot struct {
	Version          int        `json:"version"`
	Scope            *string    `json:"scope"`
	UpdatedAt        int64      `json:"updated_at"`
	RetentionSeconds int64      `json:"retention_seconds"`
	Cycles           []Snapshot `json:"cycles"`
}

// Snapshots in Archive are immutable. Copy its slice before any candidate edit.
func (d diskState) clone() diskState { d.Archive = append([]Snapshot{}, d.Archive...); return d }
func (d *diskState) prune(now int64) {
	kept := d.Archive[:0]
	for _, s := range d.Archive {
		if s.ResetAt != nil && *s.ResetAt > now-retentionSeconds {
			kept = append(kept, s)
		}
	}
	d.Archive = kept
}
func archiveValid(s Snapshot, scope string, published int64) bool {
	if s.Version != 2 || s.Scope == nil || *s.Scope != scope || s.ObservedAt == nil || s.ResetAt == nil || s.Remaining == nil || s.AsOf != s.UpdatedAt || s.UpdatedAt > published {
		return false
	}
	loc, err := time.LoadLocation(s.Timezone)
	if err != nil {
		return false
	}
	obs := Observation{Scope: scope, ObservedAt: *s.ObservedAt, ResetAt: *s.ResetAt, UsedPercent: 100 - *s.Remaining, ResetsAvailable: s.ResetsAvailable, ResetsExpireAt: s.ResetsExpireAt}
	var sourceErr error
	if s.SourceError != nil {
		switch *s.SourceError {
		case "auth_missing", "auth_failed", "source_timeout", "source_unavailable", "source_invalid":
			sourceErr = errors.New(*s.SourceError)
		default:
			return false
		}
	}
	expected, err := snapshot(&obs, sourceErr, s.UpdatedAt, Config{Zone: loc, StaleSeconds: s.StaleAfter})
	if err != nil || s.StaleAfter < 30 || s.StaleAfter > 3600 || len(s.Days) != len(expected.Days) {
		return false
	}
	if s.Cycle.State != "observed" && s.Cycle.State != "confirmed" && s.Cycle.State != "ambiguous" {
		return false
	}
	total := 0.0
	for i, d := range s.Days {
		e := expected.Days[i]
		if d.StartAt == nil || d.EndAt == nil || d.Label == nil || *d.StartAt != *e.StartAt || *d.EndAt != *e.EndAt || *d.Label != *e.Label {
			return false
		}
		if *d.StartAt > s.UpdatedAt {
			if d.Coverage != "future" || d.UsedDelta == nil || *d.UsedDelta != 0 {
				return false
			}
		} else {
			if d.Coverage != "unknown" && d.Coverage != "partial" && d.Coverage != "complete" {
				return false
			}
			if (d.Coverage == "unknown") != (d.UsedDelta == nil) || d.Coverage == "complete" && *d.EndAt > s.UpdatedAt || s.Cycle.State == "ambiguous" && d.Coverage != "unknown" {
				return false
			}
		}
		if d.UsedDelta != nil {
			if math.IsNaN(*d.UsedDelta) || math.IsInf(*d.UsedDelta, 0) || *d.UsedDelta < 0 {
				return false
			}
			total += *d.UsedDelta
		}
	}
	expected.Cycle.State = s.Cycle.State
	expected.Days = s.Days
	a, _ := json.Marshal(s)
	b, _ := json.Marshal(expected)
	return total <= 100 && string(a) == string(b)
}
func (d diskState) archiveValid() bool {
	if len(d.Archive) >= maxPeriods {
		return false
	}
	previous := int64(0)
	for _, s := range d.Archive {
		if d.Observation == nil || !archiveValid(s, d.Observation.Scope, d.PublishedAt) || *s.ResetAt <= previous || *s.ResetAt > max(d.History.StableEnd, d.Observation.ResetAt) {
			return false
		}
		previous = *s.ResetAt
	}
	return true
}
