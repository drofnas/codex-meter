package collector

import (
	"encoding/json"
	"math"
	"testing"
	"time"
)

func historyObservation(at int64, used float64) Observation {
	o := observation()
	o.ResetAt -= o.ResetAt % day // UTC midnight for the uniform-day cadence cases.
	o.ObservedAt = o.ResetAt - week + at
	o.UsedPercent = used
	return o
}

func applyHistory(t *testing.T, e *engine, o Observation) Snapshot {
	t.Helper()
	s, err := e.apply(o, nil, o.ObservedAt)
	if err != nil {
		t.Fatal(err)
	}
	if !e.state.History.valid(e.state.Observation) {
		t.Fatalf("invalid private history: %+v", e.state.History)
	}
	return s
}

func assertDay(t *testing.T, s Snapshot, i int, coverage string, amount *float64) {
	t.Helper()
	d := s.Days[i]
	if d.Coverage != coverage || (d.UsedDelta == nil) != (amount == nil) || amount != nil && math.Abs(*d.UsedDelta-*amount) > 1e-9 {
		t.Fatalf("day %d: got %+v; want %s %v", i, d, coverage, amount)
	}
}

func TestHistoryIntervals(t *testing.T) {
	for _, tc := range []struct {
		name     string
		from, to int64
		used     float64
		known    []int
		amount   float64
	}{
		{"same-slot", 1000, 1060, 35, []int{0}, 15},
		{"same-slot-zero", 1000, 1060, 20, []int{0}, 0},
		{"largest-compatible-gap", 1000, 1120, 35, []int{0}, 15},
		{"gap", 1000, 1121, 35, nil, 0},
		{"positive-crossing", day - 30, day + 30, 35, nil, 0},
		{"zero-crossing", day - 30, day + 30, 20, []int{0, 1}, 0},
		{"exact-end", day - 60, day, 35, []int{0}, 15},
		{"skew-positive", -5, 55, 35, nil, 0},
		{"skew-zero", -5, 55, 20, []int{0}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := testEngine(t)
			first := applyHistory(t, e, historyObservation(tc.from, 20))
			for i, d := range first.Days {
				if *d.StartAt > first.AsOf {
					assertDay(t, first, i, "future", ptr(0.0))
				} else {
					assertDay(t, first, i, "unknown", nil)
				}
			}
			o := historyObservation(tc.to, tc.used)
			s := applyHistory(t, e, o)
			for i, d := range s.Days {
				known := false
				for _, k := range tc.known {
					known = known || k == i
				}
				switch {
				case known:
					assertDay(t, s, i, "partial", &tc.amount)
				case *d.StartAt > s.AsOf:
					assertDay(t, s, i, "future", ptr(0.0))
				default:
					assertDay(t, s, i, "unknown", nil)
				}
			}
			before := e.state.History
			applyHistory(t, e, o)
			if e.state.History != before {
				t.Fatal("duplicate added history")
			}
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-"+tc.name, b)
		})
	}
}

func TestHistoryDiscontinuities(t *testing.T) {
	for _, tc := range []struct {
		name          string
		breakInterval func(*engine, Observation) (Snapshot, error)
		want          float64
		state         string
	}{
		{"failed-poll", func(e *engine, o Observation) (Snapshot, error) {
			return e.apply(Observation{}, errSourceTimeout, o.ObservedAt+30)
		}, 5, "observed"},
		{"invalid-sample", func(e *engine, o Observation) (Snapshot, error) {
			o.UsedPercent = -1
			o.ObservedAt += 30
			return e.apply(o, nil, o.ObservedAt)
		}, 5, "observed"},
		{"older-sample", func(e *engine, o Observation) (Snapshot, error) {
			o.ObservedAt--
			o.UsedPercent = 21
			return e.apply(o, nil, o.ObservedAt+31)
		}, 15, "observed"},
		{"equal-time-conflict", func(e *engine, o Observation) (Snapshot, error) {
			o.UsedPercent++
			return e.apply(o, nil, o.ObservedAt+30)
		}, 0, "observed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := testEngine(t)
			applyHistory(t, e, historyObservation(1000, 20))
			o := historyObservation(1060, 25)
			applyHistory(t, e, o)
			if _, err := tc.breakInterval(e, o); err != nil {
				t.Fatal(err)
			}
			// A replay cannot heal a broken interval.
			if _, err := e.apply(o, nil, o.ObservedAt+30); err != nil {
				t.Fatal(err)
			}
			s := applyHistory(t, e, historyObservation(1120, 35))
			if s.Cycle.State != tc.state {
				t.Fatal(s.Cycle)
			}
			if tc.name == "equal-time-conflict" {
				assertDay(t, s, 0, "unknown", nil)
			} else {
				assertDay(t, s, 0, "partial", &tc.want)
			}
			s = applyHistory(t, e, historyObservation(1180, 40))
			assertDay(t, s, 0, "partial", ptr(tc.want+5))
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-"+tc.name, b)
		})
	}
}

func TestHistoryCompleteAndBrokenCoverage(t *testing.T) {
	for _, tc := range []struct {
		name           string
		skip, crossing bool
	}{
		{"complete", false, false}, {"gap", true, false}, {"discarded-boundary", false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			o := historyObservation(0, 0)
			h := newHistory(o, "observed", time.UTC)
			previous := o
			// Exercise real minute cadence for a full cycle without filesystem cost.
			for at := int64(60); at < week; at += 60 {
				if tc.skip && (at == 600 || at == 660) {
					continue
				}
				if tc.crossing && at == day {
					continue
				}
				next := historyObservation(at, float64(at)/float64(week)*98)
				h.accept(&previous, next, 120, time.UTC)
				previous = next
			}
			s, err := snapshot(&previous, nil, previous.ResetAt, testConfig(t))
			if err != nil {
				t.Fatal(err)
			}
			h.project(&s)
			for i := 0; i < 6; i++ {
				want := "complete"
				if tc.skip && i == 0 || tc.crossing && i < 2 {
					want = "partial"
				}
				if s.Days[i].Coverage != want {
					t.Fatalf("slot %d got %s want %s", i, s.Days[i].Coverage, want)
				}
			}
			assertDay(t, s, 6, "partial", ptr(h.Days[6].Used))
			if !h.valid(&previous) {
				t.Fatal("full-cycle state invalid")
			}
			b, err := encodeSnapshot(s)
			if err != nil {
				t.Fatal(err)
			}
			emitSynthetic(t, "history-full-"+tc.name, b)
		})
	}
}

func TestHistoryResetsAndAmbiguity(t *testing.T) {
	for _, name := range []string{"quota-decrease", "early-reset", "unused-drift", "excess-delta"} {
		t.Run(name, func(t *testing.T) {
			e := testEngine(t)
			used := 20.0
			if name == "unused-drift" {
				used = 0
			}
			o := historyObservation(1000, used)
			applyHistory(t, e, o)
			o.ObservedAt += 60
			o.UsedPercent += 5
			if name == "unused-drift" {
				o.UsedPercent = 0
			}
			applyHistory(t, e, o)
			anchor, days := e.state.History.StableEnd, e.state.History.Days
			o.ObservedAt += 60
			switch name {
			case "quota-decrease":
				o.UsedPercent = 5
			case "early-reset":
				o.ResetAt = o.ObservedAt + week
				o.UsedPercent = 0
				o.ResetsAvailable = ptr(int64(2))
			case "unused-drift":
				o.ResetAt += 60
			case "excess-delta":
				o.UsedPercent = 150
			}
			s := applyHistory(t, e, o)
			if s.Status != "ok" || s.Cycle.State != "ambiguous" || *s.ResetAt != o.ResetAt || e.state.History.StableEnd != anchor || e.state.History.Days != days {
				t.Fatal("ambiguity lost authoritative gauge or stable anchor", s.Cycle, e.state.History)
			}
			for i, d := range s.Days {
				if *d.StartAt <= s.AsOf {
					assertDay(t, s, i, "unknown", nil)
				}
			}
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-"+name, b)
			// A stable pair starts a new baseline without counting the suspect gap.
			o.ObservedAt += 60
			s = applyHistory(t, e, o)
			if s.Cycle.State != "observed" || e.state.History.Days != ([9]historyDay{}) {
				t.Fatal("recovery fabricated history or confirmed an early reset")
			}
			o.ObservedAt += 60
			o.UsedPercent += 2
			s = applyHistory(t, e, o)
			assertDay(t, s, 0, "partial", ptr(2.0))
			anchor = e.state.History.StableEnd
			// A subsequent scheduled transition still confirms normally.
			for cycle := int64(1); cycle <= 2; cycle++ {
				o.ResetAt = anchor + cycle*week
				o.ObservedAt = o.ResetAt - week + 60
				o.UsedPercent = 1
				s = applyHistory(t, e, o)
				if s.Cycle.State != "confirmed" || e.state.History.Days != ([9]historyDay{}) {
					t.Fatal("reset did not replace history", s.Cycle)
				}
				o.ObservedAt += 60
				o.UsedPercent = 3
				s = applyHistory(t, e, o)
				assertDay(t, s, 0, "partial", ptr(2.0))
			}
			b, _ = encodeSnapshot(s)
			emitSynthetic(t, "history-confirmed-"+name, b)
		})
	}
}

func TestHistoryScheduledResetRequirements(t *testing.T) {
	for _, tc := range []struct {
		name      string
		at, reset int64
		want      string
	}{
		{"scheduled", week, 2 * week, "confirmed"},
		{"skipped-cycles", 3*week + day, 4 * week, "confirmed"},
		{"too-early", week - 1, 2 * week, "ambiguous"},
		{"overlapping-cycle", week, 2*week - 6, "ambiguous"},
		{"allowed-skew", week, 2*week + 5, "confirmed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := testEngine(t)
			initial := historyObservation(1000, 20)
			applyHistory(t, e, initial)
			o := initial
			o.ObservedAt = initial.ResetAt - week + tc.at
			o.ResetAt = initial.ResetAt - week + tc.reset
			o.UsedPercent = 0
			s := applyHistory(t, e, o)
			if s.Cycle.State != tc.want {
				t.Fatal(s.Cycle)
			}
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-reset-"+tc.name, b)
		})
	}
}

func TestHistoryTimezoneAndRestart(t *testing.T) {
	for _, tc := range []struct {
		name  string
		reset int64
	}{{"spring", 1773214200}, {"fall", 1793602800}} {
		t.Run(tc.name, func(t *testing.T) {
			e := testEngine(t)
			e.config.Zone, _ = time.LoadLocation("America/Los_Angeles")
			o := observation()
			o.ResetAt = tc.reset
			initial, err := snapshot(&Observation{Scope: o.Scope, UsedPercent: o.UsedPercent, ResetAt: o.ResetAt, ObservedAt: o.ResetAt - week}, nil, o.ResetAt-week, e.config)
			if err != nil {
				t.Fatal(err)
			}
			o.ObservedAt = *initial.Days[0].EndAt - 30
			applyHistory(t, e, o)
			o.ObservedAt += 60
			s := applyHistory(t, e, o)
			assertDay(t, s, 0, "partial", ptr(0.0))
			assertDay(t, s, 1, "partial", ptr(0.0))
			before := e.state.History
			e.state = e.store.load()
			e.config.Zone = time.UTC
			s = applyHistory(t, e, o)
			if e.state.History.Timezone != "UTC" || e.state.History == before {
				t.Fatal("timezone change did not repartition calendar dates")
			}
			for i, d := range s.Days {
				if i > 0 && time.Unix(*d.StartAt, 0).UTC().Hour() != 0 {
					t.Fatal("not local midnight")
				}
				if d.UsedDelta != nil && d.Coverage != "future" {
					t.Fatal("nonmatching interval was reassigned")
				}
			}
			b, _ := encodeSnapshot(s)
			emitSynthetic(t, "history-timezone-"+tc.name, b)
		})
	}
}

func TestHistoryPollIntervalAndScope(t *testing.T) {
	e := testEngine(t)
	e.config.Interval = 10 * time.Second
	applyHistory(t, e, historyObservation(1000, 20))
	s := applyHistory(t, e, historyObservation(1030, 30))
	assertDay(t, s, 0, "unknown", nil)
	o := historyObservation(1050, 35)
	s = applyHistory(t, e, o)
	assertDay(t, s, 0, "partial", ptr(5.0))
	o.Scope = observation().Scope[:63] + "b"
	o.ObservedAt += 20
	s = applyHistory(t, e, o)
	assertDay(t, s, 0, "unknown", nil)
	if s.Cycle.State != "observed" {
		t.Fatal(s.Cycle)
	}
}

func BenchmarkHistoryAccept(b *testing.B) {
	initial := historyObservation(0, 0)
	h := newHistory(initial, "observed", time.UTC)
	previous := initial
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		if previous.ObservedAt+60 >= initial.ResetAt {
			h = newHistory(initial, "observed", time.UTC)
			previous = initial
		}
		next := previous
		next.ObservedAt += 60
		next.UsedPercent += 0.001
		h.accept(&previous, next, 120, time.UTC)
		previous = next
	}
}

func TestHistoryPrivateSize(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(0, 0)
	applyHistory(t, e, o)
	h := e.state.History
	for at := int64(60); at < week; at += 60 {
		next := historyObservation(at, float64(at)/float64(week)*100)
		h.accept(&o, next, 120, time.UTC)
		o = next
	}
	e.state.Observation = &o
	e.state.History = h
	e.state.PublishedAt = o.ObservedAt
	b, err := json.Marshal(e.state)
	if err != nil || len(b) > maxHistoryBytes || !h.valid(&o) {
		t.Fatal(len(b), err, h)
	}
	t.Logf("full-cycle private record: %d bytes", len(b))
}

func TestHistoryLateEndpoints(t *testing.T) {
	e := testEngine(t)
	o := historyObservation(0, 20)
	h := newHistory(o, "observed", time.UTC)
	// Save continuous evidence through the last minute of the first slot.
	for at := int64(60); at < day; at += 60 {
		next := historyObservation(at, 20)
		h.accept(&o, next, 120, time.UTC)
		o = next
	}
	e.state.Observation, e.state.History = &o, h
	// Publication is already in the next slot; the endpoint arrives late.
	e.state.PublishedAt = o.ResetAt - week + day + 10
	endpoint := historyObservation(day, 22)
	s, err := e.apply(endpoint, nil, e.state.PublishedAt+10)
	if err != nil {
		t.Fatal(err)
	}
	assertDay(t, s, 0, "complete", ptr(2.0))
	assertDay(t, s, 1, "unknown", nil)
	// A failure after closure cannot erase the completed evidence.
	s, err = e.apply(Observation{}, errSourceTimeout, e.state.PublishedAt+30)
	if err != nil {
		t.Fatal(err)
	}
	assertDay(t, s, 0, "complete", ptr(2.0))
}

func TestHistoryFractionalFullAllowance(t *testing.T) {
	for _, cadence := range []int64{10, 60, 97, 300} {
		previous := historyObservation(0, 0)
		h := newHistory(previous, "observed", time.UTC)
		last := (week - 1) / cadence * cadence
		for at := cadence; at <= last; at += cadence {
			next := historyObservation(at, float64(at)/float64(last)*100)
			h.accept(&previous, next, cadence*2, time.UTC)
			previous = next
		}
		if h.State != "observed" || !h.valid(&previous) {
			t.Fatalf("valid fractional allowance became ambiguous at cadence %d: %+v", cadence, h)
		}
	}
	// Seven valid endpoint differences can sum just above 100 in float64.
	endpoints := []float64{3.0387263868140946, 15.869663424530456, 21.077808265153575, 23.30260718951731, 87.65458421230757, 90.59979707090888, 100}
	previous := historyObservation(0, 0)
	h := newHistory(previous, "observed", time.UTC)
	for i, used := range endpoints {
		// Compatible zero intervals advance through each slot; the final minute
		// supplies its used delta. The seventh endpoint precedes reset strictly.
		end := int64(i+1) * day
		if i == 6 {
			end--
		}
		for previous.ObservedAt < previous.ResetAt-week+end {
			next := previous
			next.ObservedAt = min(previous.ObservedAt+60, previous.ResetAt-week+end)
			if next.ObservedAt == previous.ResetAt-week+end {
				next.UsedPercent = used
			}
			h.accept(&previous, next, 120, time.UTC)
			previous = next
		}
	}
	if h.State != "observed" || !h.valid(&previous) {
		t.Fatalf("fractional sum lost history: %+v", h)
	}
	s, err := snapshot(&previous, nil, previous.ObservedAt, testConfig(t))
	if err != nil {
		t.Fatal(err)
	}
	h.project(&s)
	b, err := encodeSnapshot(s)
	if err != nil {
		t.Fatal(err)
	}
	emitSynthetic(t, "history-fractional-full", b)
}
