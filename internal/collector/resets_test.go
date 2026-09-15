package collector

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func resetRows() []any {
	return []any{
		map[string]any{"id": "later", "status": "available", "reset_type": "codex_rate_limits", "expires_at": testNow.Add(10 * 24 * time.Hour).Format(time.RFC3339Nano)},
		map[string]any{"id": "first", "status": "available", "reset_type": "codex_rate_limits", "expires_at": testNow.Add(2 * 24 * time.Hour).Format(time.RFC3339Nano)},
	}
}

func TestResetExpiryDetails(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func([]any) ([]any, int64)
		want   int64
	}{
		{"unsorted", func(r []any) ([]any, int64) { return r, 2 }, testNow.Unix() + 2*86400},
		{"redeemed-earliest", func(r []any) ([]any, int64) { r[1].(map[string]any)["status"] = "redeemed"; return r, 1 }, testNow.Unix() + 10*86400},
		{"zero", func(r []any) ([]any, int64) { return r, 0 }, 0},
		{"capped", func(r []any) ([]any, int64) { return r, 3 }, 0},
		{"missing", func(r []any) ([]any, int64) { return nil, 2 }, 0},
		{"duplicate", func(r []any) ([]any, int64) { return []any{r[0], r[0]}, 2 }, 0},
		{"unknown-expiry", func(r []any) ([]any, int64) { delete(r[1].(map[string]any), "expires_at"); return r, 2 }, 0},
		{"malformed-expiry", func(r []any) ([]any, int64) { r[1].(map[string]any)["expires_at"] = "tomorrow"; return r, 2 }, 0},
		{"expired", func(r []any) ([]any, int64) {
			r[1].(map[string]any)["expires_at"] = testNow.Format(time.RFC3339)
			return r, 2
		}, 0},
		{"other-type", func(r []any) ([]any, int64) { r[1].(map[string]any)["reset_type"] = "unknown"; return r, 2 }, 0},
		{"unsupported", func(r []any) ([]any, int64) { r[1].(map[string]any)["is_supported_by_plan"] = false; return r, 2 }, 0},
		{"nonexpiring-and-finite", func(r []any) ([]any, int64) { r[1].(map[string]any)["expires_at"] = nil; return r, 2 }, testNow.Unix() + 10*86400},
		{"all-nonexpiring", func(r []any) ([]any, int64) {
			for _, row := range r {
				row.(map[string]any)["expires_at"] = nil
			}
			return r, 2
		}, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rows, n := tc.change(resetRows())
			got := earliestResetExpiry(rows, n, testNow.Unix())
			if tc.want == 0 {
				if got != nil {
					t.Fatal("invented expiry")
				}
			} else if got == nil || *got != tc.want {
				t.Fatal("wrong earliest expiry", got)
			}
		})
	}
	rows := []any{map[string]any{"id": "numeric", "status": "available", "reset_type": "codex_rate_limits", "expires_at": json.Number("2000100000")}}
	if got := earliestResetExpiry(rows, 1, testNow.Unix()); got == nil || *got != 2000100000 {
		t.Fatal("numeric expiry")
	}
}

func TestResetFetchIsolationAndReplacement(t *testing.T) {
	path := authFixture(t, testNow.Unix()+3600)
	for _, scenario := range []string{"success", "zero", "capped", "malformed", "duplicate", "timeout", "oversized", "compressed", "unauthorized", "unavailable"} {
		t.Run(scenario, func(t *testing.T) {
			calls := 0
			client := newClient(time.Second)
			client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.Method != "GET" || r.Header.Get("ChatGPT-Account-Id") != "synthetic-account" || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer header.") {
					t.Fatal("unsafe request")
				}
				body := string(sourceFixture(t, "backend-primary"))
				code := 200
				header := make(http.Header)
				if calls == 1 {
					if r.URL.String() != usageURL {
						t.Fatal("usage destination")
					}
				} else {
					if calls != 2 || r.URL.String() != resetCreditsURL {
						t.Fatal("unexpected request")
					}
					deadline, ok := r.Context().Deadline()
					if !ok || time.Until(deadline) > 2*time.Second {
						t.Fatal("unbounded optional request")
					}
					n := 2
					rows := resetRows()
					switch scenario {
					case "zero":
						n = 0
						rows = nil
					case "capped":
						n = 5
					case "timeout":
						return nil, context.DeadlineExceeded
					case "compressed":
						header.Set("Content-Encoding", "gzip")
					case "unauthorized":
						code = 401
					case "unavailable":
						code = 503
					}
					b, _ := json.Marshal(map[string]any{"available_count": n, "credits": rows})
					body = string(b)
					if scenario == "malformed" {
						body = "{"
					}
					if scenario == "duplicate" {
						body = `{"available_count":2,"available_count":0}`
					}
					if scenario == "oversized" {
						body = strings.Repeat(" ", maxSourceBytes+1)
					}
				}
				return &http.Response{StatusCode: code, Header: header, Body: io.NopCloser(strings.NewReader(body))}, nil
			})
			obs, err := fetch(context.Background(), client, path, make([]byte, 32), func() time.Time { return testNow })
			if err != nil || calls != 2 || obs.UsedPercent != 20 || obs.ResetsAvailable == nil {
				t.Fatal("optional failure damaged quota", err)
			}
			want := int64(3)
			switch scenario {
			case "success":
				want = 2
			case "zero":
				want = 0
			case "capped":
				want = 5
			}
			if *obs.ResetsAvailable != want {
				t.Fatal("count inferred or stale", *obs.ResetsAvailable, want)
			}
			if (obs.ResetsExpireAt != nil) != (scenario == "success") {
				t.Fatal("expiry availability")
			}
			b, _ := json.Marshal(obs)
			for _, s := range []string{"later", "first", "signature", "synthetic-account", "description"} {
				if strings.Contains(string(b), s) {
					t.Fatal("details leaked")
				}
			}
		})
	}
}

func TestNoResetDetailsRequestWithoutPositiveCount(t *testing.T) {
	for _, count := range []string{"0", "null", "-1", "1.5"} {
		client := newClient(time.Second)
		calls := 0
		client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
			calls++
			if r.URL.String() != usageURL {
				t.Fatal("unnecessary detail request")
			}
			body := strings.Replace(string(sourceFixture(t, "backend-primary")), `"available_count": 3`, `"available_count": `+count, 1)
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
		})
		if _, err := fetch(context.Background(), client, authFixture(t, testNow.Unix()+3600), make([]byte, 32), func() time.Time { return testNow }); err != nil || calls != 1 {
			t.Fatal(count, err, calls)
		}
	}
}

func TestResetExpiryPersistsReplacesAndClears(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	obs.ResetsAvailable = ptr(int64(2))
	obs.ResetsExpireAt = ptr(obs.ObservedAt + 2*86400)
	first, err := e.apply(obs, nil, obs.ObservedAt)
	if err != nil || first.ResetsExpireAt == nil || *first.ResetsExpireAt != *obs.ResetsExpireAt {
		t.Fatal("snapshot lost expiry", err)
	}
	// Reload the exact private commit, including archived snapshots and expiry.
	e.state = e.store.load()
	if e.state.Observation == nil || !equalObservation(*e.state.Observation, obs) {
		t.Fatal("restart lost reset facts")
	}
	stale, err := e.apply(Observation{}, errSourceTimeout, obs.ObservedAt+60)
	if err != nil || stale.Status != "stale" || stale.ResetsExpireAt == nil || *stale.ResetsAvailable != 2 {
		t.Fatal("stale reset facts", err)
	}
	// Simulate redemption elsewhere: no mutating provider request is made.
	obs.ObservedAt += 120
	obs.ResetsAvailable = ptr(int64(1))
	obs.ResetsExpireAt = ptr(obs.ObservedAt + 10*86400)
	next, err := e.apply(obs, nil, obs.ObservedAt)
	if err != nil || next.Status != "ok" || *next.ResetsAvailable != 1 || *next.ResetsExpireAt != *obs.ResetsExpireAt {
		t.Fatal("replacement failed", err)
	}
	obs.ObservedAt += 60
	obs.ResetsExpireAt = nil // Fresh count but a failed optional details read.
	next, err = e.apply(obs, nil, obs.ObservedAt)
	if err != nil || next.Status != "ok" || next.ResetsExpireAt != nil {
		t.Fatal("old expiry survived", err)
	}
	obs.ObservedAt += 60
	obs.ResetsAvailable = ptr(int64(0))
	next, err = e.apply(obs, nil, obs.ObservedAt)
	if err != nil || *next.ResetsAvailable != 0 || next.ResetsExpireAt != nil {
		t.Fatal("zero failed", err)
	}
}
