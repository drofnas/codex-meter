// Package api serves the prepared public snapshot without importing the collector.
package api

import (
	"bytes"
	"codex-usage-meter/internal/calendar"
	"encoding/json"
	"errors"
	"io"
	"math"
	"slices"
	"strings"
	"time"
	_ "time/tzdata"
	"unicode/utf8"
)

const MaxBytes = 4096
const maxEpoch = 4102444800
const daySeconds = 86400
const weekSeconds = 7 * daySeconds

var invalid = errors.New("snapshot_invalid")

type document = map[string]any

// Decode tokens ourselves: encoding/json otherwise accepts duplicate keys and
// invalid UTF-8. Keep numbers as numbers until every field has been checked.
func decode(raw []byte) (document, error) {
	return decodeVersion(raw, MaxBytes, 1)
}
func decodeVersion(raw []byte, limit int, version int) (document, error) {
	if len(raw) > limit {
		return nil, errors.New("snapshot_oversized")
	}
	if !utf8.Valid(raw) {
		return nil, invalid
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var value func(int) (any, error)
	value = func(depth int) (any, error) {
		if depth > 24 {
			return nil, invalid
		}
		tok, err := d.Token()
		if err != nil {
			return nil, invalid
		}
		switch t := tok.(type) {
		case json.Delim:
			switch t {
			case '{':
				m := document{}
				for d.More() {
					key, err := d.Token()
					k, ok := key.(string)
					if err != nil || !ok {
						return nil, invalid
					}
					if _, exists := m[k]; exists {
						return nil, invalid
					}
					v, err := value(depth + 1)
					if err != nil {
						return nil, err
					}
					m[k] = v
				}
				end, err := d.Token()
				if err != nil || end != json.Delim('}') {
					return nil, invalid
				}
				return m, nil
			case '[':
				a := []any{}
				for d.More() {
					v, err := value(depth + 1)
					if err != nil {
						return nil, err
					}
					a = append(a, v)
				}
				end, err := d.Token()
				if err != nil || end != json.Delim(']') {
					return nil, invalid
				}
				return a, nil
			default:
				return nil, invalid
			}
		case json.Number:
			n, err := t.Float64()
			if err != nil || math.IsNaN(n) || math.IsInf(n, 0) {
				return nil, invalid
			}
			return n, nil
		default:
			return tok, nil
		}
	}
	v, err := value(0)
	if err != nil {
		return nil, err
	}
	if _, err = d.Token(); err != io.EOF {
		return nil, invalid
	}
	m, ok := v.(document)
	if !ok {
		return nil, invalid
	}
	if v, exists := m["version"]; exists && v != float64(version) {
		return nil, errors.New("snapshot_version")
	}
	return m, nil
}
func keys(m document, names string) bool {
	required := strings.Fields(names)
	if len(m) != len(required) {
		return false
	}
	for _, k := range required {
		if _, ok := m[k]; !ok {
			return false
		}
	}
	return true
}
func number(v any, low, high float64, integral, nullable bool) bool {
	if v == nil {
		return nullable
	}
	n, ok := v.(float64)
	return ok && n >= low && n <= high && (!integral || n == math.Trunc(n))
}
func epoch(v any, nullable bool) bool { return number(v, 1, maxEpoch, true, nullable) }
func oneOf(v any, nullable bool, values ...string) bool {
	if v == nil {
		return nullable
	}
	s, ok := v.(string)
	return ok && slices.Contains(values, s)
}
func hexToken(s string) bool { return len(s) == 64 && strings.Trim(s, "0123456789abcdef") == "" }
func zone(name string) (*time.Location, error) {
	if name == "" || len(name) > 64 || name == "Local" || strings.ContainsAny(name, "\\\x00") {
		return nil, invalid
	}
	return time.LoadLocation(name)
}
func freshness(v document) (string, any, any) {
	if v["observed_at"] == nil {
		return "unavailable", "no_observation", nil
	}
	now, obs, updated := v["as_of"].(float64), v["observed_at"].(float64), v["updated_at"].(float64)
	age := math.Max(0, now-obs)
	switch {
	case now+5 < math.Max(obs, updated):
		return "stale", "clock_error", age
	case v["source_error"] != nil:
		return "stale", "source_error", age
	case now >= v["reset_at"].(float64):
		return "stale", "reset_due", age
	case age >= v["stale_after_seconds"].(float64):
		return "stale", "too_old", age
	default:
		return "ok", nil, age
	}
}

// validateVersion implements the structural schema plus validate_snapshot semantics.
// It validates the original publication before any response-time projection.
func validateVersion(v document, version int) error {
	names := "version scope bucket window_minutes observed_at updated_at as_of age_seconds stale_after_seconds status reason source_error remaining_percent reset_at reset_local timezone cycle days resets_available"
	if expiry, present := v["resets_expire_at"]; present {
		names += " resets_expire_at"
		if version != 2 || !epoch(expiry, true) {
			return invalid
		}
		if expiry != nil {
			count, cOK := v["resets_available"].(float64)
			observed, oOK := v["observed_at"].(float64)
			if !cOK || !oOK || count <= 0 || expiry.(float64) <= observed {
				return invalid
			}
		}
	}
	if !keys(v, names) || v["version"] != float64(version) || v["bucket"] != "codex" || v["window_minutes"] != float64(10080) {
		return invalid
	}
	for _, k := range []string{"observed_at", "updated_at", "reset_at"} {
		if !epoch(v[k], true) {
			return invalid
		}
	}
	if !epoch(v["as_of"], false) || !number(v["age_seconds"], 0, maxEpoch, true, true) || !number(v["stale_after_seconds"], 30, 3600, true, false) || !number(v["remaining_percent"], 0, 100, false, true) || !number(v["resets_available"], 0, 2147483647, true, true) {
		return invalid
	}
	if !oneOf(v["status"], false, "ok", "stale", "unavailable") || !oneOf(v["reason"], true, "no_observation", "source_error", "clock_error", "reset_due", "too_old") || !oneOf(v["source_error"], true, "auth_missing", "auth_failed", "source_timeout", "source_unavailable", "source_invalid") {
		return invalid
	}
	if v["scope"] != nil {
		s, ok := v["scope"].(string)
		if !ok || !hexToken(s) {
			return invalid
		}
	}
	name, ok := v["timezone"].(string)
	if !ok {
		return invalid
	}
	loc, err := zone(name)
	if err != nil {
		return invalid
	}
	c, ok := v["cycle"].(document)
	if !ok || !keys(c, "start_at end_at state") || !epoch(c["start_at"], true) || !epoch(c["end_at"], true) || !oneOf(c["state"], false, "unknown", "observed", "confirmed", "ambiguous") {
		return invalid
	}
	days, ok := v["days"].([]any)
	if !ok || version == 1 && len(days) != 7 || version == 2 && !(len(days) == 0 || len(days) >= 7 && len(days) <= 9) {
		return invalid
	}
	for _, entry := range days {
		d, ok := entry.(document)
		names := "start_at label used_delta_pp coverage"
		if version == 2 {
			names += " end_at"
		}
		if !ok || !keys(d, names) || version == 2 && !epoch(d["end_at"], false) || !epoch(d["start_at"], true) || !oneOf(d["label"], true, "M", "T", "W", "Th", "F", "Sa", "Su") || !number(d["used_delta_pp"], 0, 100, false, true) || !oneOf(d["coverage"], false, "unknown", "partial", "complete", "future") {
			return invalid
		}
	}
	// A synthesized missing-file response must never be accepted from disk.
	if v["updated_at"] == nil || v["as_of"] != v["updated_at"] {
		return invalid
	}
	if v["observed_at"] == nil {
		for _, k := range []string{"scope", "remaining_percent", "reset_at", "reset_local", "resets_available"} {
			if v[k] != nil {
				return invalid
			}
		}
		if version == 2 && len(days) != 0 {
			return invalid
		}
		if c["start_at"] != nil || c["end_at"] != nil || c["state"] != "unknown" || v["source_error"] == nil {
			return invalid
		}
		for _, entry := range days {
			d := entry.(document)
			if d["start_at"] != nil || d["label"] != nil || d["used_delta_pp"] != nil || d["coverage"] != "unknown" {
				return invalid
			}
		}
	} else {
		for _, k := range []string{"scope", "remaining_percent", "reset_at", "reset_local"} {
			if v[k] == nil {
				return invalid
			}
		}
		if c["start_at"] == nil || c["end_at"] == nil || c["state"] == "unknown" || c["end_at"] != v["reset_at"] {
			return invalid
		}
		start, end, obs, updated := c["start_at"].(float64), c["end_at"].(float64), v["observed_at"].(float64), v["updated_at"].(float64)
		if end-start != weekSeconds || obs < start-5 || obs >= end || updated < obs {
			return invalid
		}
		if v["reset_local"] != time.Unix(int64(end), 0).In(loc).Format("2006-01-02 15:04 -07:00") {
			return invalid
		}
		bounds := calendar.Bounds(int64(end), loc)
		if version == 2 && (bounds == nil || len(bounds) != len(days)) {
			return invalid
		}
		labels := []string{"Su", "M", "T", "W", "Th", "F", "Sa"}
		total := 0.0
		for i, entry := range days {
			d := entry.(document)
			ds := start + float64(i*daySeconds)
			de := ds + daySeconds
			if version == 2 {
				ds, de = float64(bounds[i].Start), float64(bounds[i].End)
				if d["end_at"] != de {
					return invalid
				}
			}
			if d["start_at"] != ds || d["label"] != labels[time.Unix(int64(ds), 0).In(loc).Weekday()] {
				return invalid
			}
			coverage, delta := d["coverage"], d["used_delta_pp"]
			if ds > updated {
				if coverage != "future" || delta != float64(0) {
					return invalid
				}
			} else if c["state"] == "ambiguous" {
				if coverage != "unknown" || delta != nil {
					return invalid
				}
			} else if coverage == "future" || (coverage == "unknown") != (delta == nil) || (coverage == "complete" && de > updated) {
				return invalid
			}
			if delta != nil {
				total += delta.(float64)
			}
		}
		if total > 100 {
			return invalid
		}
	}
	status, reason, age := freshness(v)
	if v["status"] != status || v["reason"] != reason || v["age_seconds"] != age {
		return invalid
	}
	return nil
}
func project(v document, now int64) {
	v["as_of"] = float64(now)
	effective := float64(now)
	for _, k := range []string{"observed_at", "updated_at"} {
		if n, ok := v[k].(float64); ok {
			effective = math.Max(effective, n)
		}
	}
	for _, entry := range v["days"].([]any) {
		d := entry.(document)
		if d["coverage"] == "future" && d["start_at"].(float64) <= effective {
			d["coverage"], d["used_delta_pp"] = "unknown", nil
		}
	}
	v["status"], v["reason"], v["age_seconds"] = freshness(v)
}
func unavailable(now int64, name string, stale int64) document {
	days := []any{}
	for i := 0; i < 7; i++ {
		days = append(days, document{"start_at": nil, "label": nil, "used_delta_pp": nil, "coverage": "unknown"})
	}
	return document{"version": 1, "scope": nil, "bucket": "codex", "window_minutes": 10080, "observed_at": nil, "updated_at": nil, "as_of": now, "age_seconds": nil, "stale_after_seconds": stale, "status": "unavailable", "reason": "no_observation", "source_error": nil, "remaining_percent": nil, "reset_at": nil, "reset_local": nil, "timezone": name, "cycle": document{"start_at": nil, "end_at": nil, "state": "unknown"}, "days": days, "resets_available": nil}
}

func unavailableV2(now int64, name string, stale int64) document {
	v := unavailable(now, name, stale)
	v["version"] = 2
	v["days"] = []any{}
	return v
}
