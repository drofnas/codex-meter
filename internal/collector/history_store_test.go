package collector

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestHistoryCrashRecovery(t *testing.T) {
	if root := os.Getenv("HISTORY_CRASH_DIR"); root != "" {
		phase := os.Getenv("HISTORY_CRASH_PHASE")
		c := Config{DataDir: filepath.Join(root, "data"), StateDir: filepath.Join(root, "state"), Zone: time.UTC, StaleSeconds: 180, Interval: time.Minute}
		s, err := OpenStore(c)
		if err != nil {
			t.Fatal(err)
		}
		s.rename = func(from, to string) error {
			if phase == "before-private" && filepath.Base(to) == "observation.json" || phase == "before-public" && filepath.Base(to) == "usage.json" {
				os.Exit(23)
			}
			err := os.Rename(from, to)
			if err == nil && phase == "after-public" && filepath.Base(to) == "usage.json" {
				os.Exit(23)
			}
			return err
		}
		e := &engine{config: c, store: s, state: s.load()}
		o := historyObservation(1120, 31)
		_, _ = e.apply(o, nil, o.ObservedAt)
		t.Fatal("crash hook did not run")
	}
	for _, phase := range []string{"before-private", "before-public", "after-public"} {
		t.Run(phase, func(t *testing.T) {
			e := testEngine(t)
			applyHistory(t, e, historyObservation(1000, 20))
			applyHistory(t, e, historyObservation(1060, 25))
			before, _ := os.ReadFile(filepath.Join(e.config.DataDir, "usage.json"))
			e.store.Close()
			child := exec.Command(os.Args[0], "-test.run=^TestHistoryCrashRecovery$")
			child.Env = append(os.Environ(), "HISTORY_CRASH_DIR="+filepath.Dir(e.config.DataDir), "HISTORY_CRASH_PHASE="+phase)
			if err := child.Run(); err == nil || child.ProcessState.ExitCode() != 23 {
				t.Fatal("unexpected crash result", err)
			}
			after, _ := os.ReadFile(filepath.Join(e.config.DataDir, "usage.json"))
			if phase != "after-public" && !bytes.Equal(before, after) {
				t.Fatal("uncommitted public file changed")
			}
			var published Snapshot
			if json.Unmarshal(after, &published) != nil {
				t.Fatal("partial public document")
			}
			s, err := OpenStore(e.config)
			if err != nil {
				t.Fatal(err)
			}
			defer s.Close()
			restarted := &engine{config: e.config, store: s, state: s.load()}
			want := 5.0
			if phase != "before-private" {
				want = 11
			}
			if restarted.state.History.Days[0].Used != want {
				t.Fatal("atomic ledger/baseline lost", restarted.state)
			}
			result := applyHistory(t, restarted, historyObservation(1120, 31))
			assertDay(t, result, 0, "partial", ptr(11.0))
			result = applyHistory(t, restarted, historyObservation(1180, 35))
			assertDay(t, result, 0, "partial", ptr(15.0))
			for _, path := range []string{filepath.Join(e.config.DataDir, ".usage.json.tmp"), filepath.Join(e.config.StateDir, ".observation.json.tmp")} {
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatal("crash debris retained", err)
				}
			}
		})
	}
}

func TestHistoryPublicationFailureRetainsCommit(t *testing.T) {
	e := testEngine(t)
	applyHistory(t, e, historyObservation(1000, 20))
	applyHistory(t, e, historyObservation(1060, 25))
	before, _ := os.ReadFile(filepath.Join(e.config.DataDir, "usage.json"))
	e.store.rename = func(from, to string) error {
		if filepath.Base(to) == "usage.json" {
			return errStorage
		}
		return os.Rename(from, to)
	}
	o := historyObservation(1120, 31)
	if _, err := e.apply(o, nil, o.ObservedAt); err != errStorage {
		t.Fatal(err)
	}
	if e.state.History.Days[0].Used != 11 || e.state.Observation.ObservedAt != o.ObservedAt {
		t.Fatal("in-memory baseline behind private commit")
	}
	after, _ := os.ReadFile(filepath.Join(e.config.DataDir, "usage.json"))
	if !bytes.Equal(before, after) {
		t.Fatal("public file replaced after failure")
	}
	e.store.rename = os.Rename
	s := applyHistory(t, e, historyObservation(1181, 35))
	assertDay(t, s, 0, "partial", ptr(15.0))
}

func TestHistoryStateValidationAndMigration(t *testing.T) {
	e := testEngine(t)
	applyHistory(t, e, historyObservation(1000, 20))
	applyHistory(t, e, historyObservation(1060, 25))
	path := filepath.Join(e.config.StateDir, "observation.json")
	valid, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"missing-history", func(v map[string]any) { delete(v, "history") }},
		{"null-history", func(v map[string]any) { v["history"] = nil }},
		{"missing-high-used", func(v map[string]any) { delete(v["history"].(map[string]any), "high_used") }},
		{"null-high-used", func(v map[string]any) { v["history"].(map[string]any)["high_used"] = nil }},
		{"negative-high-used", func(v map[string]any) { v["history"].(map[string]any)["high_used"] = -1 }},
		{"low-high-used", func(v map[string]any) { v["history"].(map[string]any)["high_used"] = 24 }},
		{"missing-known", func(v map[string]any) {
			delete(v["history"].(map[string]any)["days"].([]any)[0].(map[string]any), "known")
		}},
		{"null-known", func(v map[string]any) {
			v["history"].(map[string]any)["days"].([]any)[0].(map[string]any)["known"] = nil
		}},
		{"negative-used", func(v map[string]any) { v["history"].(map[string]any)["days"].([]any)[0].(map[string]any)["used"] = -1 }},
		{"excess-used", func(v map[string]any) {
			v["history"].(map[string]any)["days"].([]any)[0].(map[string]any)["used"] = 101
		}},
		{"short-ledger", func(v map[string]any) { h := v["history"].(map[string]any); h["days"] = h["days"].([]any)[:6] }},
		{"long-ledger", func(v map[string]any) {
			h := v["history"].(map[string]any)
			h["days"] = append(h["days"].([]any), h["days"].([]any)[6])
		}},
		{"wrong-anchor", func(v map[string]any) { v["history"].(map[string]any)["stable_end"] = 2000000001 }},
		{"future-coverage", func(v map[string]any) {
			v["history"].(map[string]any)["days"].([]any)[6].(map[string]any)["known"] = true
		}},
		{"false-complete", func(v map[string]any) {
			v["history"].(map[string]any)["days"].([]any)[0].(map[string]any)["covered_until"] = 2000000000 - week + day
		}},
		{"ambiguous-baseline", func(v map[string]any) { v["history"].(map[string]any)["state"] = "ambiguous" }},
		{"bad-version", func(v map[string]any) { v["version"] = 5 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var v map[string]any
			_ = json.Unmarshal(valid, &v)
			tc.mutate(v)
			b, _ := json.Marshal(v)
			if err := os.WriteFile(path, b, 0600); err != nil {
				t.Fatal(err)
			}
			e.state = e.store.load()
			if e.state.Observation != nil {
				t.Fatal("corrupt state trusted")
			}
			s := applyHistory(t, e, historyObservation(1120, 35))
			assertDay(t, s, 0, "unknown", nil)
			s = applyHistory(t, e, historyObservation(1180, 40))
			assertDay(t, s, 0, "partial", ptr(5.0))
		})
	}
	var legacy map[string]any
	_ = json.Unmarshal(valid, &legacy)
	delete(legacy, "history")
	delete(legacy, "archive")
	legacy["version"] = 1
	b, _ := json.Marshal(legacy)
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
	e.state = e.store.load()
	if e.state.Version != 4 || e.state.Observation == nil || e.state.History.BaselineUsable {
		t.Fatal("unsafe migration", e.state)
	}
	s := applyHistory(t, e, historyObservation(1120, 35))
	assertDay(t, s, 0, "unknown", nil)
	s = applyHistory(t, e, historyObservation(1180, 40))
	assertDay(t, s, 0, "partial", ptr(5.0))
	if e.store.load().History != e.state.History {
		t.Fatal("migrated state did not persist")
	}
}

func TestHistoryAmbiguityAndFailurePersist(t *testing.T) {
	for _, ambiguous := range []bool{false, true} {
		e := testEngine(t)
		applyHistory(t, e, historyObservation(1000, 20))
		o := historyObservation(1060, 25)
		applyHistory(t, e, o)
		if ambiguous {
			o.UsedPercent = 5
			o.ObservedAt += 60
			applyHistory(t, e, o)
		} else {
			if _, err := e.apply(Observation{}, errSourceInvalid, o.ObservedAt+30); err != nil {
				t.Fatal(err)
			}
		}
		saved := e.state.History
		e.state = e.store.load()
		if saved != e.state.History {
			t.Fatal("restart healed missing interval or ambiguity")
		}
		o.ObservedAt += 120
		o.UsedPercent += 5
		s := applyHistory(t, e, o)
		assertDay(t, s, 0, "partial", ptr(5.0))
	}
}

func BenchmarkHistoryPublication(b *testing.B) {
	root := b.TempDir()
	c := Config{DataDir: filepath.Join(root, "data"), StateDir: filepath.Join(root, "state"), Zone: time.UTC, StaleSeconds: 180, Interval: time.Minute}
	store, err := OpenStore(c)
	if err != nil {
		b.Fatal(err)
	}
	defer store.Close()
	e := &engine{config: c, store: store, state: store.load()}
	o := historyObservation(0, 0)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if o.ObservedAt+60 >= o.ResetAt {
			o.ResetAt += week
			o.UsedPercent = 0
		}
		o.ObservedAt += 60
		o.UsedPercent += 0.001
		if _, err := e.apply(o, nil, o.ObservedAt); err != nil {
			b.Fatal(err)
		}
	}
}
