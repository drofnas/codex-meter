package collector

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func seedDailyEstimates(t *testing.T, e *engine) Observation {
	t.Helper()
	o := historyObservation(day+1000, 10)
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent = 16
	applyHistory(t, e, o)
	o.ObservedAt += day
	o.UsedPercent = 25
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent = 30
	s := applyHistory(t, e, o)
	assertDay(t, s, 1, "partial", ptr(6.0))
	assertDay(t, s, 2, "partial", ptr(5.0))
	return o
}

func TestCorrectionsPreserveDailyEstimates(t *testing.T) {
	e := testEngine(t)
	o := seedDailyEstimates(t, e)
	for _, step := range []struct{ used, today float64 }{
		{29, 5}, {29, 5}, {30, 5}, {28, 5}, {30, 5}, {31, 6}, {30, 6}, {32, 7},
	} {
		o.ObservedAt += 60
		o.UsedPercent = step.used
		s := applyHistory(t, e, o)
		if s.Cycle.State != "observed" || *s.Remaining != 100-step.used {
			t.Fatal("correction changed history state or authoritative quota")
		}
		assertDay(t, s, 1, "partial", ptr(6.0))
		assertDay(t, s, 2, "partial", ptr(step.today))
		e.state = e.store.load()
		if e.state.Observation == nil || e.state.History.HighUsed < step.used {
			t.Fatal("restart lost correction baseline")
		}
		assertDay(t, e.state.Archive[0], 1, "partial", ptr(6.0))
		assertDay(t, e.state.Archive[0], 2, "partial", ptr(step.today))
	}
	// A private commit followed by failed publication must not add the increment twice.
	o.ObservedAt += 60
	o.UsedPercent = 33
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
	s := applyHistory(t, e, o)
	assertDay(t, s, 2, "partial", ptr(8.0))
	b, _ := encodeSnapshot(s)
	emitSynthetic(t, "history-correction-estimate", b)
}

func TestMinuteResetJitterPreservesCalendarEstimates(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(1000, 10)
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent = 16
	applyHistory(t, e, o)
	anchor := o.ResetAt
	for _, shift := range []int64{-300, -60, 60, 300, 0} {
		o.ObservedAt += 60
		o.ResetAt = anchor + shift
		s := applyHistory(t, e, o)
		found := false
		for i, d := range s.Days {
			if *d.StartAt <= o.ObservedAt && o.ObservedAt < *d.EndAt {
				assertDay(t, s, i, "partial", ptr(6.0))
				found = true
			}
		}
		if !found || s.Cycle.State != "observed" || e.state.History.StableEnd != anchor {
			t.Fatal("jitter erased current date or moved the anchor")
		}
		e.state = e.store.load()
		if e.state.Observation == nil {
			t.Fatal("jitter state failed to reload")
		}
	}
}

func TestRecoveryPreservesMatchingDaysAndBacksUpPreviousLedger(t *testing.T) {
	e := testEngine(t)
	o := seedDailyEstimates(t, e)
	anchor := o.ResetAt
	o.ObservedAt += 60
	o.ResetAt += 3600
	applyHistory(t, e, o)
	before, _ := os.ReadFile(filepath.Join(e.config.StateDir, "observation.json"))
	o.ObservedAt += 60
	e.store.rename = func(from, to string) error {
		if filepath.Base(to) == "observation.previous.json" {
			return errStorage
		}
		return os.Rename(from, to)
	}
	if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
		t.Fatal("ledger replaced without its backup", err)
	}
	after, _ := os.ReadFile(filepath.Join(e.config.StateDir, "observation.json"))
	if !bytes.Equal(before, after) {
		t.Fatal("backup failure changed the original")
	}
	e.store.rename = os.Rename
	s := applyHistory(t, e, o)
	assertDay(t, s, 1, "partial", ptr(6.0))
	assertDay(t, s, 2, "partial", ptr(5.0))
	backup, err := os.ReadFile(filepath.Join(e.config.StateDir, "observation.previous.json"))
	if err != nil || !bytes.Equal(before, backup) {
		t.Fatal("previous ledger backup was not exact", err)
	}
	var saved diskState
	if json.Unmarshal(backup, &saved) != nil || saved.History.StableEnd != anchor {
		t.Fatal("backup did not retain the earlier anchor")
	}
	o.ObservedAt += 60
	o.UsedPercent++
	s = applyHistory(t, e, o)
	assertDay(t, s, 2, "partial", ptr(6.0))
}

func writeV3Fixture(t *testing.T, path string) []byte {
	t.Helper()
	raw, _ := os.ReadFile(path)
	var legacy map[string]any
	if json.Unmarshal(raw, &legacy) != nil {
		t.Fatal("invalid initial state")
	}
	legacy["version"] = 3
	delete(legacy["history"].(map[string]any), "high_used")
	raw, _ = json.Marshal(legacy)
	if err := os.WriteFile(path, raw, 0600); err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestV3UpgradePreservesDailyEvidence(t *testing.T) {
	for _, failBackup := range []bool{false, true} {
		e := testEngine(t)
		o := seedDailyEstimates(t, e)
		path := filepath.Join(e.config.StateDir, "observation.json")
		raw := writeV3Fixture(t, path)
		if failBackup {
			e.store.rename = func(from, to string) error {
				if filepath.Base(to) == "observation.pre-v4.json" {
					return errStorage
				}
				return os.Rename(from, to)
			}
			e.state = e.store.load()
			if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
				t.Fatal("upgrade proceeded without a backup", err)
			}
			after, _ := os.ReadFile(path)
			if !bytes.Equal(after, raw) {
				t.Fatal("failed upgrade changed v3 state")
			}
			e.store.rename = os.Rename
		}
		e.state = e.store.load()
		if e.state.Version != 4 || e.state.Observation == nil || e.state.History.HighUsed != o.UsedPercent {
			t.Fatal("v3 upgrade lost observation or correction baseline")
		}
		backupPath := filepath.Join(e.config.StateDir, "observation.pre-v4.json")
		backup, err := os.ReadFile(backupPath)
		if err != nil || !bytes.Equal(backup, raw) {
			t.Fatal("v3 backup was not exact", err)
		}
		info, _ := os.Stat(backupPath)
		if info.Mode().Perm() != 0600 {
			t.Fatal("backup is not owner-only")
		}
		o.ObservedAt += 60
		o.UsedPercent--
		s := applyHistory(t, e, o)
		assertDay(t, s, 1, "partial", ptr(6.0))
		assertDay(t, s, 2, "partial", ptr(5.0))
		e.state = e.store.load()
		if e.state.Observation == nil || e.state.History.HighUsed != o.UsedPercent+1 {
			t.Fatal("v4 reload lost correction baseline")
		}
	}
}

func TestV3UpgradeWithoutObservation(t *testing.T) {
	e := testEngine(t)
	if _, err := e.apply(Observation{}, errAuthMissing, testNow.Unix()); err != nil {
		t.Fatal(err)
	}
	writeV3Fixture(t, filepath.Join(e.config.StateDir, "observation.json"))
	d := e.store.load()
	if d.Version != 4 || d.PublishedAt != testNow.Unix() || d.Observation != nil || d.History != (history{}) {
		t.Fatal("empty v3 record did not migrate")
	}
}

func TestLegacyAmbiguityKeepsJitteredEdgeEstimate(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(1000, 10)
	applyHistory(t, e, o)
	o.ObservedAt += 60
	o.UsedPercent = 16
	applyHistory(t, e, o)
	e.state.History.State, e.state.History.BaselineUsable = "ambiguous", false
	o.ObservedAt += 60
	o.ResetAt++
	applyHistory(t, e, o)
	writeV3Fixture(t, filepath.Join(e.config.StateDir, "observation.json"))
	e.state = e.store.load()
	if e.state.Observation == nil || e.state.History.State != "ambiguous" {
		t.Fatal("legacy ambiguity failed to load")
	}
	o.ObservedAt += 60
	s := applyHistory(t, e, o)
	assertDay(t, s, 0, "partial", ptr(6.0))
	e.state = e.store.load()
	if e.state.Observation == nil {
		t.Fatal("recovered edge estimate failed to reload")
	}
}
