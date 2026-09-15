package collector

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestResetJitterPreservesDaysAndArchive(t *testing.T) {
	e := testEngine(t)
	e.config.Zone, _ = time.LoadLocation("America/Los_Angeles")
	// Synthetic Saturday reset and Monday observation keep the calendar shape
	// without retaining observations from a real account.
	end := time.Date(2030, time.June, 15, 5, 0, 0, 0, e.config.Zone).Unix()
	o := Observation{Scope: observation().Scope, ResetAt: end - week, ObservedAt: end - week - 60, UsedPercent: 20}
	applyHistory(t, e, o)
	o.ResetAt, o.ObservedAt, o.UsedPercent = end, end-week+1000, 10
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent = 11
	applyHistory(t, e, o)
	// Monday, following an unobserved gap, then a measured one-point interval.
	o.ObservedAt, o.UsedPercent = end-week+2*day+3600, 35
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent++
	applyHistory(t, e, o)
	for i, shift := range []int64{-2, -1, 1, 2, 0} {
		o.ObservedAt += 60
		o.UsedPercent++
		o.ResetAt = end + shift
		s := applyHistory(t, e, o)
		if s.Cycle.State != "confirmed" || *s.ResetAt != o.ResetAt || e.state.History.StableEnd != end {
			t.Fatal("jitter changed accounting anchor or source reset", s.Cycle)
		}
		assertDay(t, s, 2, "partial", ptr(float64(i+2)))
		assertDay(t, s, 0, "partial", ptr(1.0))
		b, _ := encodeSnapshot(s)
		emitSynthetic(t, fmt.Sprintf("history-reset-jitter-%d", shift), b)
		e.state = e.store.load()
		if e.state.Observation == nil || len(e.state.Archive) != 2 || *e.state.Archive[0].ResetAt != end-week || *e.state.Archive[1].ResetAt != o.ResetAt {
			t.Fatal("restart lost ledger or duplicated active period")
		}
	}
	// Comparison is with the fixed anchor: successive one-second moves cannot
	// ratchet the tolerance, and changing resets cannot establish recovery.
	for _, shift := range []int64{301, 302} {
		o.ObservedAt += 60
		o.ResetAt = end + shift
		s := applyHistory(t, e, o)
		if s.Cycle.State != "ambiguous" || e.state.History.StableEnd != end {
			t.Fatal("drifting reset accepted as stable")
		}
	}
}

func TestResetJitterAcrossMidnight(t *testing.T) {
	for _, shift := range []int64{-1, 1} {
		e := testEngine(t)
		o := historyObservation(day+1000, 20)
		end := o.ResetAt
		applyHistory(t, e, o)
		o.ObservedAt += 60
		o.UsedPercent++
		applyHistory(t, e, o)
		o.ObservedAt += 60
		o.ResetAt += shift
		o.UsedPercent++
		s := applyHistory(t, e, o)
		if len(s.Days) != 8 || e.state.History.Count != 7 {
			t.Fatal("test did not cross midnight slot count")
		}
		found := false
		for i, d := range s.Days {
			if *d.StartAt == end-week+day {
				assertDay(t, s, i, "partial", ptr(2.0))
				found = true
			}
		}
		if !found {
			t.Fatal("lost unchanged calendar date")
		}
		b, _ := encodeSnapshot(s)
		emitSynthetic(t, fmt.Sprintf("history-reset-midnight-%d", shift), b)
		e.state = e.store.load()
		if e.state.Observation == nil {
			t.Fatal("jitter state rejected on restart")
		}
	}
}

func TestResetJitterClipsOnlyZeroAtCycleStart(t *testing.T) {
	for _, delta := range []float64{0, 1} {
		e := testEngine(t)
		o := historyObservation(-1, 20)
		applyHistory(t, e, o)
		o.ObservedAt += 4
		o.ResetAt += 2
		o.UsedPercent += delta
		applyHistory(t, e, o)
		d := e.state.History.Days[0]
		if d.Known != (delta == 0) || d.Used != 0 || d.CoveredUntil != 0 {
			t.Fatal("reset-edge interval fabricated usage or complete coverage", d)
		}
	}
}

func TestPersistedAmbiguityRecoversForward(t *testing.T) {
	for _, version := range []int{2, 4} {
		t.Run(fmt.Sprint(version), func(t *testing.T) {
			e := testEngine(t)
			e.config.Zone, _ = time.LoadLocation("America/Los_Angeles")
			end := time.Date(2030, time.June, 15, 5, 0, 0, 0, e.config.Zone).Unix()
			o := Observation{Scope: observation().Scope, ResetAt: end, ObservedAt: end - week + 3600, UsedPercent: 10}
			applyHistory(t, e, o)
			o.ObservedAt += 61
			o.ResetAt++
			// Recreate a persisted ambiguity flag after a one-second jump.
			applyHistory(t, e, o)
			e.state.History.State, e.state.History.BaselineUsable = "ambiguous", false
			o.ObservedAt, o.ResetAt, o.UsedPercent = end-week+2*day+3600, end+3600, 35
			applyHistory(t, e, o)
			if version == 2 {
				raw, _ := json.Marshal(map[string]any{"version": 2, "key_id": e.state.KeyID, "observation": o, "published_at": o.ObservedAt, "history": e.state.History})
				if err := os.WriteFile(filepath.Join(e.config.StateDir, "observation.json"), raw, 0600); err != nil {
					t.Fatal(err)
				}
			}
			e.state = e.store.load()
			if e.state.History.State != "ambiguous" {
				t.Fatal("test did not load saved ambiguity")
			}
			// A gap cannot recover; the following matching reset establishes a
			// baseline and discards all consumption preceding that baseline.
			o.ObservedAt += 121
			s := applyHistory(t, e, o)
			if s.Cycle.State != "ambiguous" {
				t.Fatal("long gap recovered history")
			}
			o.ObservedAt += 60
			o.UsedPercent++
			s = applyHistory(t, e, o)
			if s.Cycle.State != "observed" || e.state.History.StableEnd != o.ResetAt {
				t.Fatal("did not rebaseline")
			}
			assertDay(t, s, 2, "unknown", nil)
			e.state = e.store.load()
			o.ObservedAt += 60
			o.UsedPercent += 2
			// Crash after private commit, before public history publication.
			e.store.rename = func(from, to string) error {
				if filepath.Base(to) == "history.json" {
					return errStorage
				}
				return os.Rename(from, to)
			}
			if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
				t.Fatal("missing injected failure", err)
			}
			e.store.rename = os.Rename
			s = applyHistory(t, e, o)
			assertDay(t, s, 0, "unknown", nil)
			assertDay(t, s, 1, "unknown", nil)
			assertDay(t, s, 2, "partial", ptr(2.0))
			if len(e.state.Archive) != 1 {
				t.Fatal("recovery duplicated current period")
			}
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-recovered", b)
		})
	}
}
