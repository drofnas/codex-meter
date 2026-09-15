package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestThirtyFiveDayRetentionAndRestart(t *testing.T) {
	e := testEngine(t)
	first := historyObservation(3600, 0)
	start := first.ResetAt - week
	for d := int64(0); d < 60; d++ {
		o := first
		o.ResetAt = first.ResetAt + (d/7)*week
		o.ObservedAt = start + d*day + 3600
		o.UsedPercent = float64(d%7) * 2
		applyHistory(t, e, o)
		o.ObservedAt += 60
		o.UsedPercent++
		applyHistory(t, e, o)
		e.state = e.store.load()
		if e.state.Observation == nil || len(e.state.Archive) == 0 {
			t.Fatal("lost archive on restart", d)
		}
	}
	raw, err := os.ReadFile(filepath.Join(e.config.DataDir, "history.json"))
	if err != nil {
		t.Fatal(err)
	}
	var h HistorySnapshot
	if json.Unmarshal(raw, &h) != nil || h.RetentionSeconds != retentionSeconds || len(h.Cycles) > maxPeriods {
		t.Fatal("invalid history")
	}
	now := e.state.PublishedAt
	found := 0
	for _, s := range h.Cycles {
		if *s.ResetAt <= now-retentionSeconds {
			t.Fatal("expired cycle retained")
		}
		for _, d := range s.Days {
			if *d.StartAt >= now-30*day && *d.StartAt <= now {
				if d.UsedDelta == nil || *d.UsedDelta != 1 || d.Coverage != "partial" {
					t.Fatal("retained daily total changed", d)
				}
				found++
			}
		}
	}
	if found < 30 {
		t.Fatal("less than 30 days retained", found)
	}
	if folder := os.Getenv("COLLECTOR_TEST_SNAPSHOTS_DIR"); folder != "" {
		if err := os.MkdirAll(filepath.Join(folder, "history"), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(folder, "history", "retained.json"), raw, 0600); err != nil {
			t.Fatal(err)
		}
	}
	before := string(raw)
	// An interrupted public-history publication must not duplicate the committed delta.
	e.store.rename = func(from, to string) error {
		if filepath.Base(to) == "history.json" {
			return errStorage
		}
		return os.Rename(from, to)
	}
	o := *e.state.Observation
	o.ObservedAt += 60
	o.UsedPercent++
	if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
		t.Fatal(err)
	}
	raw, _ = os.ReadFile(filepath.Join(e.config.DataDir, "history.json"))
	if string(raw) != before {
		t.Fatal("partial archive exposed")
	}
	e.store.rename = os.Rename
	applyHistory(t, e, o)
	e.state = e.store.load()
	s := applyHistory(t, e, o)
	current := int((o.ObservedAt - (o.ResetAt - week)) / day)
	assertDay(t, s, current, "partial", ptr(2.0))
	// Switching accounts must not combine their retained totals.
	o.Scope = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	o.ObservedAt++
	applyHistory(t, e, o)
	if len(e.state.Archive) != 1 || *e.state.Archive[0].Scope != o.Scope {
		t.Fatal("mixed account histories")
	}
}

func TestCalendarWeekStaysAnchored(t *testing.T) {
	e := testEngine(t)
	e.config.Zone, _ = time.LoadLocation("America/Los_Angeles")
	end := time.Date(2026, 9, 19, 5, 0, 0, 0, e.config.Zone).Unix()
	o := Observation{Scope: observation().Scope, ResetAt: end, ObservedAt: end - week + 60, UsedPercent: 1}
	initial := applyHistory(t, e, o)
	if len(initial.Days) != 8 || *initial.Days[0].Label != "Sa" || *initial.Days[7].Label != "Sa" {
		t.Fatal(initial.Days)
	}
	if *initial.Days[7].EndAt-*initial.Days[7].StartAt != 5*3600 {
		t.Fatal("missing final five hours")
	}
	for i := 1; i < 8; i++ {
		now := *initial.Days[i].StartAt
		s, err := e.apply(Observation{}, errSourceTimeout, now)
		if err != nil {
			t.Fatal(err)
		}
		for j, d := range s.Days {
			if *d.StartAt != *initial.Days[j].StartAt || *d.EndAt != *initial.Days[j].EndAt || *d.Label != *initial.Days[j].Label {
				t.Fatal("midnight shifted the week")
			}
		}
	}
}

func TestLegacyV2IsForwardOnly(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(1000, 20)
	legacy := map[string]any{"version": 2, "key_id": e.store.keyID(), "observation": o, "published_at": o.ObservedAt, "history": map[string]any{"state": "observed", "stable_end": o.ResetAt, "baseline_usable": true, "days": []any{}}}
	raw, _ := json.Marshal(legacy)
	path := filepath.Join(e.config.StateDir, "observation.json")
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	e.state = e.store.load()
	if e.state.Version != 3 || e.state.Observation == nil || e.state.History.BaselineUsable || len(e.state.Archive) != 0 {
		t.Fatal("bad migration")
	}
	backup, err := os.ReadFile(filepath.Join(e.config.StateDir, "observation.pre-v3.json"))
	if err != nil || string(backup) != string(raw) {
		t.Fatal("backup lost")
	}
	o.ObservedAt += 60
	o.UsedPercent = 30
	s := applyHistory(t, e, o)
	assertDay(t, s, 0, "unknown", nil)
	o.ObservedAt += 60
	o.UsedPercent = 32
	s = applyHistory(t, e, o)
	assertDay(t, s, 0, "partial", ptr(2.0))
}

func TestMigrationBackupFailurePreservesOriginal(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(1000, 20)
	raw, _ := json.Marshal(map[string]any{"version": 1, "key_id": e.store.keyID(), "observation": o, "published_at": o.ObservedAt})
	path := filepath.Join(e.config.StateDir, "observation.json")
	os.WriteFile(path, raw, 0600)
	e.store.rename = func(from, to string) error {
		if filepath.Base(to) == "observation.pre-v3.json" {
			return errStorage
		}
		return os.Rename(from, to)
	}
	e.state = e.store.load()
	if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
		t.Fatal("migration ignored failed backup", err)
	}
	after, _ := os.ReadFile(path)
	if string(after) != string(raw) {
		t.Fatal("original lost before backup")
	}
	e.store.rename = os.Rename
	e.state = e.store.load()
	applyHistory(t, e, o)
}

func TestTimezoneChangeWhileSourceUnavailable(t *testing.T) {
	e := testEngine(t)
	e.config.Zone, _ = time.LoadLocation("America/Los_Angeles")
	end := time.Date(2026, 3, 15, 0, 30, 0, 0, e.config.Zone).Unix()
	o := Observation{Scope: observation().Scope, ResetAt: end, ObservedAt: end - week + 3600, UsedPercent: 1}
	first := applyHistory(t, e, o)
	if len(first.Days) != 9 {
		t.Fatal("expected nine-day DST case")
	}
	e.config.Zone = time.UTC
	e.state = e.store.load()
	s, err := e.apply(Observation{}, errSourceTimeout, o.ObservedAt+60)
	if err != nil || len(s.Days) != 8 || s.Timezone != "UTC" || *s.Remaining != 99 {
		t.Fatal("failed-source timezone change", s, err)
	}
	if !e.store.load().History.valid(&o) {
		t.Fatal("repartition did not persist")
	}
}
