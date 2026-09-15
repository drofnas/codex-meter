package api

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
)

const HistoryMaxBytes = 65536
const RetentionSeconds = 35 * 86400

func validateHistory(v document) error {
	if !keys(v, "version scope updated_at retention_seconds cycles") || v["version"] != float64(2) || !epoch(v["updated_at"], false) || v["retention_seconds"] != float64(RetentionSeconds) {
		return invalid
	}
	cycles, ok := v["cycles"].([]any)
	if !ok || len(cycles) > 8 {
		return invalid
	}
	if v["scope"] == nil {
		if len(cycles) != 0 {
			return invalid
		}
	} else {
		scope, ok := v["scope"].(string)
		if !ok || !hexToken(scope) {
			return invalid
		}
	}
	previous := float64(0)
	for _, item := range cycles {
		s, ok := item.(document)
		if !ok || validateVersion(s, 2) != nil || s["scope"] != v["scope"] || s["observed_at"] == nil || s["updated_at"].(float64) > v["updated_at"].(float64) {
			return invalid
		}
		end := s["reset_at"].(float64)
		if end <= previous {
			return invalid
		}
		previous = end
	}
	return nil
}
func (h *Handler) serveHistory(w http.ResponseWriter, now int64) {
	fail := func(code string) { respond(w, 503, document{"version": 2, "error": code}) }
	raw, err := readBounded(filepath.Join(h.config.DataDir, "history.json"), HistoryMaxBytes)
	if os.IsNotExist(err) {
		respond(w, 200, document{"version": 2, "scope": nil, "updated_at": nil, "as_of": now, "retention_seconds": RetentionSeconds, "cycles": []any{}})
		return
	}
	if err != nil {
		fail(err.Error())
		return
	}
	v, err := decodeVersion(raw, HistoryMaxBytes, 2)
	if err == nil {
		err = validateHistory(v)
	}
	if err != nil {
		fail(err.Error())
		return
	}
	kept := []any{}
	for _, item := range v["cycles"].([]any) {
		cycle := item.(document)
		if cycle["reset_at"].(float64) <= float64(now-RetentionSeconds) {
			continue
		}
		project(cycle, now)
		kept = append(kept, cycle)
	}
	v["cycles"] = kept
	v["as_of"] = now
	b, err := json.Marshal(v)
	if err != nil || len(b) > HistoryMaxBytes {
		fail("snapshot_oversized")
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(200)
	_, _ = w.Write(b)
}
