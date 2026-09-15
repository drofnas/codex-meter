package collector

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"time"
)

type pollFunc func(context.Context) (Observation, error)
type waitFunc func(context.Context, time.Duration) bool

type engine struct {
	config Config
	state  diskState
	store  *Store
}

func equalObservation(a, b Observation) bool {
	return a.Scope == b.Scope && a.UsedPercent == b.UsedPercent && a.ResetAt == b.ResetAt && a.ObservedAt == b.ObservedAt &&
		(a.ResetsExpireAt == nil && b.ResetsExpireAt == nil || a.ResetsExpireAt != nil && b.ResetsExpireAt != nil && *a.ResetsExpireAt == *b.ResetsExpireAt) &&
		(a.ResetsAvailable == nil && b.ResetsAvailable == nil || a.ResetsAvailable != nil && b.ResetsAvailable != nil && *a.ResetsAvailable == *b.ResetsAvailable)
}

func (e *engine) apply(obs Observation, sourceErr error, now int64) (Snapshot, error) {
	candidate := e.state.clone()
	candidate.History.rezone(e.config.Zone)
	if sourceErr == nil {
		if !obs.valid(true) || obs.ObservedAt > now {
			sourceErr = errSourceInvalid
			candidate.History.BaselineUsable = false
		} else if prev := candidate.Observation; prev != nil && prev.Scope == obs.Scope && obs.ObservedAt < prev.ObservedAt {
			sourceErr = errSourceInvalid
		} else if prev := candidate.Observation; prev != nil && prev.Scope == obs.Scope && obs.ObservedAt == prev.ObservedAt && !equalObservation(*prev, obs) {
			sourceErr = errSourceInvalid
			candidate.History.State, candidate.History.BaselineUsable = "ambiguous", false
		} else {
			previous := candidate.Observation
			candidate.History.accept(previous, obs, int64(e.config.Interval/time.Second)*2, e.config.Zone)
			if previous != nil && previous.Scope != obs.Scope {
				candidate.Archive = []Snapshot{}
			}
			candidate.Observation = &obs
		}
	} else {
		candidate.History.BaselineUsable = false
	}
	// v1 forbids publication before observation. Preserve the on-disk timestamp
	// across wall-clock rollback; the API's projection will mark clock_error.
	if now < e.state.PublishedAt || candidate.Observation != nil && now < candidate.Observation.ObservedAt {
		return Snapshot{}, errors.New("clock_error")
	}
	s, err := snapshot(candidate.Observation, sourceErr, now, e.config)
	if err != nil {
		return Snapshot{}, err
	}
	candidate.History.project(&s)
	candidate.PublishedAt = now
	if s.ObservedAt != nil {
		// Keep completed periods and one current snapshot. Reset variations and
		// recovery must not accumulate overlapping copies of the active week.
		kept := candidate.Archive[:0]
		for _, old := range candidate.Archive {
			if *old.ResetAt <= *s.ObservedAt {
				kept = append(kept, old)
			}
		}
		candidate.Archive = append(kept, s)
	}
	candidate.prune(now)
	if len(candidate.Archive) >= maxPeriods {
		return Snapshot{}, errStorage
	}
	if err := e.store.publish(candidate, s); err != nil {
		// Private commit may have succeeded before public rename failed. Reload
		// the atomic record so another attempt cannot overwrite a committed delta
		// using an older in-memory baseline.
		e.state = e.store.load()
		return Snapshot{}, err
	}
	e.state = candidate
	return s, nil
}

func Run(ctx context.Context, c Config, log io.Writer) error {
	store, err := OpenStore(c)
	if err != nil {
		return err
	}
	defer store.Close()
	client := newClient(c.Timeout)
	defer client.CloseIdleConnections()
	e := &engine{config: c, state: store.load(), store: store}
	poll := func(ctx context.Context) (Observation, error) {
		return fetch(ctx, client, c.AuthFile, store.key, time.Now)
	}
	return loop(ctx, e, poll, time.Now, wait, log)
}

func wait(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func retryDelay(interval time.Duration, failures int) time.Duration {
	d := interval
	for i := 1; i < failures && d < 300*time.Second; i++ {
		d *= 2
	}
	if d > 300*time.Second {
		return 300 * time.Second
	}
	return d
}

func loop(ctx context.Context, e *engine, poll pollFunc, now func() time.Time, sleep waitFunc, log io.Writer) error {
	failures := 0
	lastEvent := ""
	for ctx.Err() == nil {
		started := now()
		attempt, cancel := context.WithTimeout(ctx, e.config.Timeout)
		obs, sourceErr := poll(attempt)
		cancel()
		if ctx.Err() != nil {
			return nil
		}
		s, err := e.apply(obs, sourceErr, now().Unix())
		event := s.Status
		if err != nil {
			event = err.Error()
		} else if s.SourceError != nil {
			event = *s.SourceError
		}
		// Transitions only, with no usage values or raw error text. No log files.
		if event != lastEvent {
			if json.NewEncoder(log).Encode(map[string]string{"event": event}) != nil {
				return errors.New("log_unavailable")
			}
			lastEvent = event
		}
		failed := err != nil || s.SourceError != nil || s.Status != "ok"
		if e.config.Once {
			if err != nil {
				return err
			}
			if failed {
				return errSourceUnavailable
			}
			return nil
		}
		delay := e.config.Interval
		if failed {
			if failures < 10 {
				failures++
			}
			delay = retryDelay(e.config.Interval, failures)
		} else {
			failures = 0
			// Healthy starts remain approximately one interval apart. Failure
			// waits are at least a full interval after completion.
			if elapsed := now().Sub(started); elapsed > 0 && elapsed < delay {
				delay -= elapsed
			}
		}
		if !sleep(ctx, delay) {
			return nil
		}
	}
	return nil
}
