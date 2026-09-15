package collector

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testEngine(t *testing.T) *engine {
	t.Helper()
	c := testConfig(t)
	s, err := OpenStore(c)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return &engine{config: c, state: s.load(), store: s}
}
func observation() Observation {
	return Observation{Scope: strings.Repeat("a", 64), UsedPercent: 20, ObservedAt: testNow.Unix(), ResetAt: 2000000000, ResetsAvailable: ptr(int64(3))}
}

func TestFailuresPreserveAndRecover(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	first, err := e.apply(obs, nil, obs.ObservedAt)
	if err != nil || first.Status != "ok" {
		t.Fatal(first, err)
	}
	for i, code := range []error{errAuthMissing, errAuthFailed, errSourceTimeout, errSourceInvalid, errSourceUnavailable} {
		s, err := e.apply(Observation{}, code, obs.ObservedAt+int64(i+1)*60)
		if err != nil || s.Status != "stale" || *s.SourceError != code.Error() || *s.ObservedAt != obs.ObservedAt || *s.Remaining != 80 || *s.ResetsAvailable != 3 || *s.AgeSeconds != int64(i+1)*60 {
			t.Fatal(s, err)
		}
	}
	obs.ObservedAt += 360
	obs.UsedPercent = 25
	obs.ResetsAvailable = nil
	s, err := e.apply(obs, nil, obs.ObservedAt)
	if err != nil || s.Status != "ok" || s.SourceError != nil || *s.Remaining != 75 || s.ResetsAvailable != nil {
		t.Fatal(s, err)
	}
	// Restart while signed out retains the bounded observation and scope.
	e.store.Close()
	store, err := OpenStore(e.config)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	restarted := &engine{config: e.config, state: store.load(), store: store}
	s, err = restarted.apply(Observation{}, errAuthFailed, obs.ObservedAt+60)
	if err != nil || *s.ObservedAt != obs.ObservedAt || *s.Scope != obs.Scope || *s.Remaining != 75 {
		t.Fatal(s, err)
	}
}

func TestOrderingAndChangedAccount(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	dup, err := e.apply(obs, nil, obs.ObservedAt+60)
	if err != nil || *dup.ObservedAt != obs.ObservedAt || *dup.AgeSeconds != 60 {
		t.Fatal(dup, err)
	}
	for _, change := range []func(*Observation){func(o *Observation) { o.ObservedAt-- }, func(o *Observation) { o.UsedPercent++ }, func(o *Observation) { o.ResetsAvailable = ptr(int64(4)) }, func(o *Observation) { o.ResetAt++ }} {
		bad := obs
		change(&bad)
		s, err := e.apply(bad, nil, obs.ObservedAt+120)
		if err != nil || s.SourceError == nil || *s.SourceError != "source_invalid" || *s.Remaining != 80 || *s.ObservedAt != obs.ObservedAt {
			t.Fatal(s, err)
		}
	}
	other := obs
	other.Scope = strings.Repeat("b", 64)
	other.ObservedAt--
	other.UsedPercent = 0
	s, err := e.apply(other, nil, obs.ObservedAt+180)
	if err != nil || *s.Scope != other.Scope || *s.Remaining != 100 {
		t.Fatal(s, err)
	}
	for _, d := range s.Days {
		if d.Coverage != "unknown" && d.Coverage != "future" {
			t.Fatal("fabricated history")
		}
	}
}

func TestClockRollbackAndResetPassage(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt+120); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(e.config.DataDir, "usage.json")
	before, _ := os.ReadFile(path)
	if _, err := e.apply(obs, nil, obs.ObservedAt+60); err == nil || err.Error() != "clock_error" {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("clock rollback replaced snapshot")
	}
	// Replayed observations cannot fabricate a fresh reset after wall-clock passage.
	s, err := e.apply(obs, nil, obs.ResetAt)
	if err != nil || s.Status != "stale" || *s.Reason != "reset_due" || *s.ResetAt != obs.ResetAt {
		t.Fatal(s, err)
	}
}

func TestLoopTimeoutBackoffCancellationAndLogs(t *testing.T) {
	e := testEngine(t)
	clock := testNow
	calls := 0
	var sleeps []time.Duration
	var log bytes.Buffer
	obs := observation()
	poll := func(ctx context.Context) (Observation, error) {
		if _, ok := ctx.Deadline(); !ok {
			t.Fatal("poll has no deadline")
		}
		calls++
		clock = clock.Add(2 * time.Second)
		if calls <= 5 {
			return Observation{}, errSourceTimeout
		}
		obs.ObservedAt = clock.Unix()
		return obs, nil
	}
	sleep := func(ctx context.Context, d time.Duration) bool {
		sleeps = append(sleeps, d)
		clock = clock.Add(d)
		return len(sleeps) < 7
	}
	if err := loop(context.Background(), e, poll, func() time.Time { return clock }, sleep, &log); err != nil {
		t.Fatal(err)
	}
	want := []time.Duration{60, 120, 240, 300, 300, 58, 58}
	for i, d := range sleeps {
		if d != want[i]*time.Second {
			t.Fatal(sleeps)
		}
	}
	if calls != 7 || strings.Count(log.String(), "\n") != 2 || strings.Contains(log.String(), obs.Scope) || strings.Contains(log.String(), "used_percent") {
		t.Fatal(calls, log.String())
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := loop(ctx, e, func(context.Context) (Observation, error) {
		t.Fatal("poll after cancellation")
		return Observation{}, nil
	}, time.Now, wait, &log); err != nil {
		t.Fatal(err)
	}
	if wait(ctx, time.Hour) {
		t.Fatal("shutdown failed")
	}
	// The loop cannot start a second request while its current one is blocked.
	e.config.Timeout = time.Millisecond
	blockedCalls := 0
	if err := loop(context.Background(), e, func(ctx context.Context) (Observation, error) {
		blockedCalls++
		<-ctx.Done()
		return Observation{}, errSourceTimeout
	}, time.Now, func(context.Context, time.Duration) bool { return false }, &log); err != nil || blockedCalls != 1 {
		t.Fatal(err, blockedCalls)
	}
}

func TestPublicationFailureKeepsPreviousSnapshot(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(e.config.DataDir, "usage.json")
	before, _ := os.ReadFile(path)
	e.store.rename = func(from, to string) error {
		if to == path {
			return errors.New("interrupted publication")
		}
		return os.Rename(from, to)
	}
	obs.ObservedAt += 60
	obs.UsedPercent = 30
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != errStorage {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("failed write replaced old snapshot")
	}
	e.store.rename = os.Rename
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	files, _ := os.ReadDir(e.config.DataDir)
	if len(files) != 3 {
		t.Fatal("unbounded publication debris", files)
	}
}

// Export synthetic output when requested, then validate it using the independent
// Python contract oracle in the repository's collector validation command.
func TestGeneratedSnapshots(t *testing.T) {
	c := testConfig(t)
	obs := observation()
	types := []struct {
		name string
		o    *Observation
		err  error
		at   int64
	}{
		{"normal", &obs, nil, obs.ObservedAt}, {"duplicate-stale", &obs, nil, obs.ObservedAt + 180},
		{"reset-due", &obs, nil, obs.ResetAt}, {"unavailable", nil, errAuthMissing, obs.ObservedAt},
		{"auth-failed", &obs, errAuthFailed, obs.ObservedAt + 60}, {"timeout", &obs, errSourceTimeout, obs.ObservedAt + 120},
		{"truncated", &obs, errSourceInvalid, obs.ObservedAt + 240},
	}
	for _, tc := range types {
		s, err := snapshot(tc.o, tc.err, tc.at, c)
		if err != nil {
			t.Fatal(err)
		}
		b, err := encodeSnapshot(s)
		if err != nil {
			t.Fatal(err)
		}
		emitSynthetic(t, tc.name, b)
	}
	for _, tc := range []struct {
		name, zone string
		reset      int64
	}{
		{"dst-spring", "America/Los_Angeles", 1773214200},
		{"dst-fall", "America/Los_Angeles", 1793602800},
		{"half-hour", "Asia/Kolkata", 2000000000},
	} {
		c.Zone, _ = time.LoadLocation(tc.zone)
		o := obs
		o.ResetAt = tc.reset
		o.ObservedAt = tc.reset - week + day
		s, err := snapshot(&o, nil, o.ObservedAt, c)
		if err != nil {
			t.Fatal(err)
		}
		for i, d := range s.Days {
			if i == 0 && *d.StartAt != o.ResetAt-week || i > 0 && (*s.Days[i-1].EndAt != *d.StartAt || time.Unix(*d.StartAt, 0).In(c.Zone).Hour() != 0) {
				t.Fatal("invalid calendar boundary")
			}
			if i == len(s.Days)-1 && *d.EndAt != o.ResetAt {
				t.Fatal("missing final partial date")
			}
			if *d.StartAt > s.AsOf && (d.Coverage != "future" || *d.UsedDelta != 0) {
				t.Fatal("bad future")
			}
		}
		b, err := encodeSnapshot(s)
		if err != nil {
			t.Fatal(err)
		}
		emitSynthetic(t, tc.name, b)
	}
	obs.UsedPercent = 140
	obs.ResetsAvailable = nil
	s, err := snapshot(&obs, nil, obs.ObservedAt, c)
	if err != nil || *s.Remaining != 0 {
		t.Fatal(s, err)
	}
	b, _ := encodeSnapshot(s)
	emitSynthetic(t, "over-quota", b)
}

func emitSynthetic(t *testing.T, name string, b []byte) {
	t.Helper()
	var parsed map[string]any
	if json.Unmarshal(b, &parsed) != nil || len(b) > 4096 {
		t.Fatal("invalid output")
	}
	if dir := os.Getenv("COLLECTOR_TEST_SNAPSHOTS_DIR"); dir != "" {
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name+".json"), b, 0600); err != nil {
			t.Fatal(err)
		}
	}
}
