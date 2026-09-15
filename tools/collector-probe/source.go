package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"os"
	"strings"
	"time"
)

const usageURL = "https://chatgpt.com/backend-api/wham/usage"
const maxSourceBytes = 256 * 1024
const maxAuthBytes = 64 * 1024

// Codes are deliberately independent of response bodies, paths, and credentials.
var (
	errAuthUnavailable = errors.New("auth_unavailable")
	errAuthExpired     = errors.New("auth_expired")
	errUnauthorized    = errors.New("upstream_unauthorized")
	errUpstream        = errors.New("upstream_unavailable")
	errSource          = errors.New("invalid_source")
	errWeekly          = errors.New("weekly_unavailable")
)

// Observation is spike output, not the future firmware/API wire contract.
type Observation struct {
	Source          string  `json:"source"`
	Bucket          string  `json:"bucket"`
	WindowMinutes   int     `json:"window_minutes"`
	UsedPercent     float64 `json:"used_percent"`
	Remaining       float64 `json:"remaining_percent"`
	ResetsAt        int64   `json:"resets_at"`
	ObservedAt      int64   `json:"observed_at"`
	AvailableResets *int64  `json:"available_resets"`
}

type credentials struct{ access, account string }

func boundedRead(r io.Reader, limit int64) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, errSource
	}
	return b, nil
}

func readCredentials(path string, now time.Time) (credentials, error) {
	f, err := os.Open(path)
	if err != nil {
		return credentials{}, errAuthUnavailable
	}
	defer f.Close()
	b, err := boundedRead(f, maxAuthBytes)
	if err != nil {
		return credentials{}, errAuthUnavailable
	}
	var auth struct {
		Mode   string `json:"auth_mode"`
		Tokens struct {
			Access  string `json:"access_token"`
			Account string `json:"account_id"`
		} `json:"tokens"`
	}
	if json.Unmarshal(b, &auth) != nil || auth.Mode != "chatgpt" || auth.Tokens.Account == "" {
		return credentials{}, errAuthUnavailable
	}
	parts := strings.Split(auth.Tokens.Access, ".")
	if len(parts) != 3 || !safeHeader(auth.Tokens.Access) || !safeHeader(auth.Tokens.Account) {
		return credentials{}, errAuthUnavailable
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	var claims struct {
		Expires int64 `json:"exp"`
	}
	if err != nil || json.Unmarshal(payload, &claims) != nil || claims.Expires <= 0 {
		return credentials{}, errAuthUnavailable
	}
	// This is an expiry guard, not JWT signature verification; the service authenticates it.
	if claims.Expires <= now.Add(30*time.Second).Unix() {
		return credentials{}, errAuthExpired
	}
	return credentials{auth.Tokens.Access, auth.Tokens.Account}, nil
}

func safeHeader(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < 33 || c > 126 {
			return false
		}
	}
	return true
}

func usageClient() *http.Client {
	return &http.Client{
		Timeout:       10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		Transport: &http.Transport{
			Proxy:                 nil,
			TLSClientConfig:       &tls.Config{MinVersion: tls.VersionTLS12},
			TLSHandshakeTimeout:   5 * time.Second,
			ResponseHeaderTimeout: 8 * time.Second,
			MaxConnsPerHost:       1,
			MaxIdleConns:          1,
			IdleConnTimeout:       90 * time.Second,
		},
	}
}

func fetch(ctx context.Context, client *http.Client, authPath string, now time.Time) (Observation, error) {
	creds, err := readCredentials(authPath, now)
	if err != nil {
		return Observation{}, err
	}
	// Endpoint is fixed: credentials cannot be redirected to a configured host.
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, usageURL, nil)
	if err != nil {
		return Observation{}, errUpstream
	}
	req.Header.Set("Authorization", "Bearer "+creds.access)
	req.Header.Set("ChatGPT-Account-Id", creds.account)
	req.Header.Set("User-Agent", "codex-usage-meter-poc/0.1")
	req.Header.Set("Accept", "application/json")
	res, err := client.Do(req)
	if err != nil {
		return Observation{}, errUpstream
	}
	defer res.Body.Close()
	if res.StatusCode == http.StatusUnauthorized || res.StatusCode == http.StatusForbidden {
		return Observation{}, errUnauthorized
	}
	if res.StatusCode != http.StatusOK {
		return Observation{}, errUpstream
	}
	b, err := boundedRead(res.Body, maxSourceBytes)
	if err != nil {
		return Observation{}, err
	}
	return parseObservation(b, now)
}

func object(v any) map[string]any { m, _ := v.(map[string]any); return m }

func number(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	return f, err == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
}

func integer(v any) (int64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := n.Int64()
	return i, err == nil
}

func parseObservation(b []byte, now time.Time) (Observation, error) {
	if len(b) > maxSourceBytes {
		return Observation{}, errSource
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var root map[string]any
	if dec.Decode(&root) != nil || root == nil {
		return Observation{}, errSource
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return Observation{}, errSource
	}
	bucket := object(root["rate_limit"])
	if bucket == nil {
		return Observation{}, errWeekly
	}
	var selected map[string]any
	for _, raw := range []any{bucket["primary_window"], bucket["secondary_window"]} {
		w := object(raw)
		duration, valid := integer(w["limit_window_seconds"])
		if valid && duration == 604800 {
			if selected != nil {
				return Observation{}, errWeekly
			}
			selected = w
		}
	}
	if selected == nil {
		return Observation{}, errWeekly
	}
	used, valid := number(selected["used_percent"])
	reset, validReset := integer(selected["reset_at"])
	if !valid || used < 0 || used > 100 || !validReset || reset <= 0 {
		return Observation{}, errSource
	}
	obs := Observation{Source: "codex-internal-usage", Bucket: "codex", WindowMinutes: 10080, UsedPercent: used,
		Remaining: math.Max(0, math.Min(100, 100-used)), ResetsAt: reset, ObservedAt: now.Unix()}
	if n, valid := integer(object(root["rate_limit_reset_credits"])["available_count"]); valid && n >= 0 {
		obs.AvailableResets = &n
	}
	return obs, nil
}
