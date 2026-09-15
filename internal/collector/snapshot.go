package collector

import (
	"codex-usage-meter/internal/calendar"
	"encoding/json"
	"math"
	"time"
)

type Day struct {
	EndAt     *int64   `json:"end_at"`
	StartAt   *int64   `json:"start_at"`
	Label     *string  `json:"label"`
	UsedDelta *float64 `json:"used_delta_pp"`
	Coverage  string   `json:"coverage"`
}
type Cycle struct {
	StartAt *int64 `json:"start_at"`
	EndAt   *int64 `json:"end_at"`
	State   string `json:"state"`
}
type Snapshot struct {
	ResetsExpireAt  *int64   `json:"resets_expire_at,omitempty"`
	Version         int      `json:"version"`
	Scope           *string  `json:"scope"`
	Bucket          string   `json:"bucket"`
	WindowMinutes   int      `json:"window_minutes"`
	ObservedAt      *int64   `json:"observed_at"`
	UpdatedAt       int64    `json:"updated_at"`
	AsOf            int64    `json:"as_of"`
	AgeSeconds      *int64   `json:"age_seconds"`
	StaleAfter      int64    `json:"stale_after_seconds"`
	Status          string   `json:"status"`
	Reason          *string  `json:"reason"`
	SourceError     *string  `json:"source_error"`
	Remaining       *float64 `json:"remaining_percent"`
	ResetAt         *int64   `json:"reset_at"`
	ResetLocal      *string  `json:"reset_local"`
	Timezone        string   `json:"timezone"`
	Cycle           Cycle    `json:"cycle"`
	Days            []Day    `json:"days"`
	ResetsAvailable *int64   `json:"resets_available"`
}

func ptr[T any](v T) *T { return &v }

func snapshot(obs *Observation, sourceErr error, now int64, c Config) (Snapshot, error) {
	if !validEpoch(now) {
		return Snapshot{}, errSourceInvalid
	}
	s := Snapshot{Version: 2, Days: []Day{}, Bucket: "codex", WindowMinutes: 10080,
		UpdatedAt: now, AsOf: now, StaleAfter: c.StaleSeconds, Timezone: c.Zone.String(),
		Status: "unavailable", Reason: ptr("no_observation"), Cycle: Cycle{State: "unknown"}}
	if sourceErr != nil {
		s.SourceError = ptr(sourceErr.Error())
	}
	if obs == nil {
		if sourceErr == nil {
			return Snapshot{}, errSourceInvalid
		}
		return s, nil
	}
	if !obs.valid(true) || now < obs.ObservedAt {
		return Snapshot{}, errSourceInvalid
	}
	s.Scope, s.ObservedAt, s.ResetAt, s.ResetsAvailable = ptr(obs.Scope), ptr(obs.ObservedAt), ptr(obs.ResetAt), obs.ResetsAvailable
	s.ResetsExpireAt = obs.ResetsExpireAt
	s.AgeSeconds = ptr(now - obs.ObservedAt)
	s.Remaining = ptr(math.Max(0, math.Min(100, 100-obs.UsedPercent)))
	s.ResetLocal = ptr(time.Unix(obs.ResetAt, 0).In(c.Zone).Format("2006-01-02 15:04 -07:00"))
	s.Cycle = Cycle{StartAt: ptr(obs.ResetAt - week), EndAt: ptr(obs.ResetAt), State: "observed"}
	labels := []string{"Su", "M", "T", "W", "Th", "F", "Sa"}
	bounds := calendar.Bounds(obs.ResetAt, c.Zone)
	if bounds == nil {
		return Snapshot{}, errSourceInvalid
	}
	for _, interval := range bounds {
		d := Day{StartAt: ptr(interval.Start), EndAt: ptr(interval.End), Label: ptr(labels[time.Unix(interval.Start, 0).In(c.Zone).Weekday()]), Coverage: "unknown"}
		if interval.Start > now {
			d.Coverage, d.UsedDelta = "future", ptr(0.0)
		}
		s.Days = append(s.Days, d)
	}
	s.Status, s.Reason = "ok", nil
	switch {
	case sourceErr != nil:
		s.Status, s.Reason = "stale", ptr("source_error")
	case now >= obs.ResetAt:
		s.Status, s.Reason = "stale", ptr("reset_due")
	case now-obs.ObservedAt >= c.StaleSeconds:
		s.Status, s.Reason = "stale", ptr("too_old")
	}
	return s, nil
}

func encodeSnapshot(s Snapshot) ([]byte, error) {
	b, err := json.Marshal(s)
	if err != nil || len(b) > maxSnapshotBytes {
		return nil, errSourceInvalid
	}
	return b, nil
}
