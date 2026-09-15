package collector

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"math"
	"net"
	"net/http"
	"strings"
	"time"
)

const usageURL = "https://chatgpt.com/backend-api/wham/usage"
const resetCreditsURL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"

// Never return provider bodies, paths, identifiers, tokens or transport details.
var (
	errAuthMissing       = errors.New("auth_missing")
	errAuthFailed        = errors.New("auth_failed")
	errSourceTimeout     = errors.New("source_timeout")
	errSourceUnavailable = errors.New("source_unavailable")
	errSourceInvalid     = errors.New("source_invalid")
)

// Observation is the bounded private restart baseline, not the wire envelope.
// Raw identity and credentials never enter this type.
type Observation struct {
	Scope           string  `json:"scope"`
	UsedPercent     float64 `json:"used_percent"`
	ResetAt         int64   `json:"reset_at"`
	ObservedAt      int64   `json:"observed_at"`
	ResetsAvailable *int64  `json:"resets_available"`
	ResetsExpireAt  *int64  `json:"resets_expire_at,omitempty"`
}

type credentials struct{ access, account string }

func readCredentials(path string, now time.Time) (credentials, error) {
	b, err := readFile(path, maxAuthBytes)
	if err != nil {
		return credentials{}, errAuthMissing
	}
	var auth struct {
		Mode   string `json:"auth_mode"`
		Tokens struct {
			Access  string `json:"access_token"`
			Account string `json:"account_id"`
		} `json:"tokens"`
	}
	if strictJSON(b, &auth, false) != nil || auth.Mode != "chatgpt" || !safeHeader(auth.Tokens.Access) || !safeHeader(auth.Tokens.Account) {
		return credentials{}, errAuthMissing
	}
	parts := strings.Split(auth.Tokens.Access, ".")
	if len(parts) != 3 {
		return credentials{}, errAuthMissing
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	var claims struct {
		Expires int64 `json:"exp"`
	}
	if err != nil || strictJSON(payload, &claims, false) != nil || claims.Expires <= 0 {
		return credentials{}, errAuthMissing
	}
	// Expiry guard only; the provider verifies the JWT. Never refresh credentials.
	if claims.Expires <= now.Add(30*time.Second).Unix() {
		return credentials{}, errAuthFailed
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

func scopeFor(key []byte, account string) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(account))
	return hex.EncodeToString(m.Sum(nil))
}

func newClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout:       timeout,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		Transport: &http.Transport{
			Proxy: nil, DisableCompression: true,
			TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
			TLSHandshakeTimeout: timeout, ResponseHeaderTimeout: timeout,
			DialContext:     (&net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}).DialContext,
			MaxConnsPerHost: 1, MaxIdleConns: 1, IdleConnTimeout: 90 * time.Second,
		},
	}
}

func sourceFailure(err error) error {
	var ne net.Error
	if errors.Is(err, context.DeadlineExceeded) || errors.As(err, &ne) && ne.Timeout() {
		return errSourceTimeout
	}
	return errSourceUnavailable
}

func fetch(ctx context.Context, client *http.Client, authPath string, key []byte, now func() time.Time) (Observation, error) {
	creds, err := readCredentials(authPath, now())
	if err != nil {
		return Observation{}, err
	}
	// Fixed destination, no proxy or redirect: no configurable credential sink.
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, usageURL, nil)
	if err != nil {
		return Observation{}, errSourceUnavailable
	}
	req.Header.Set("Authorization", "Bearer "+creds.access)
	req.Header.Set("ChatGPT-Account-Id", creds.account)
	req.Header.Set("User-Agent", "codex-usage-meter/0.1")
	req.Header.Set("Accept", "application/json")
	res, err := client.Do(req)
	if err != nil {
		return Observation{}, sourceFailure(err)
	}
	defer res.Body.Close()
	if res.StatusCode == 401 || res.StatusCode == 403 {
		return Observation{}, errAuthFailed
	}
	if res.StatusCode != 200 {
		return Observation{}, errSourceUnavailable
	}
	if res.Header.Get("Content-Encoding") != "" {
		return Observation{}, errSourceInvalid
	}
	b, err := boundedRead(res.Body, maxSourceBytes)
	if err != nil {
		if ctx.Err() != nil {
			return Observation{}, sourceFailure(ctx.Err())
		}
		if sourceFailure(err) == errSourceTimeout {
			return Observation{}, errSourceTimeout
		}
		return Observation{}, errSourceInvalid
	}
	// Stamp only after the complete response body has arrived.
	obs, err := parseObservation(b, now())
	if err != nil {
		return Observation{}, err
	}
	obs.Scope = scopeFor(key, creds.account)
	// Optional details share the transport but get only two seconds. A failure
	// must never discard the successful quota/count observation or reuse old expiry.
	if obs.ResetsAvailable != nil && *obs.ResetsAvailable > 0 {
		detailCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		fetchResetDetails(detailCtx, client, creds, &obs)
	}
	return obs, nil
}

func parseObservation(b []byte, completed time.Time) (Observation, error) {
	if len(b) > maxSourceBytes {
		return Observation{}, errSourceInvalid
	}
	var root map[string]any
	if strictJSON(b, &root, false) != nil || root == nil {
		return Observation{}, errSourceInvalid
	}
	// The HTTP endpoint identifies general Codex as rate_limit;
	// additional_rate_limits are deliberately excluded regardless of recency.
	bucket := object(root["rate_limit"])
	if bucket == nil {
		return Observation{}, errSourceInvalid
	}
	var selected map[string]any
	for _, raw := range []any{bucket["primary_window"], bucket["secondary_window"]} {
		w := object(raw)
		if duration, ok := integer(w["limit_window_seconds"]); ok && duration == week {
			if selected != nil {
				return Observation{}, errSourceInvalid
			}
			selected = w
		}
	}
	if selected == nil {
		return Observation{}, errSourceInvalid
	}
	used, validUsed := number(selected["used_percent"])
	reset, validReset := integer(selected["reset_at"])
	observed := completed.Unix()
	if raw, present := root["observed_at"]; present {
		var ok bool
		observed, ok = integer(raw)
		if !ok {
			return Observation{}, errSourceInvalid
		}
	}
	if !validUsed || !validReset || !validEpoch(completed.Unix()) || observed > completed.Unix() {
		return Observation{}, errSourceInvalid
	}
	obs := Observation{UsedPercent: used, ResetAt: reset, ObservedAt: observed}
	if !obs.valid(false) {
		return Observation{}, errSourceInvalid
	}
	summary := object(root["rate_limit_reset_credits"])
	if n, ok := integer(summary["available_count"]); ok && n >= 0 && n <= math.MaxInt32 {
		obs.ResetsAvailable = &n
		obs.ResetsExpireAt = earliestResetExpiry(summary["credits"], n, observed)
	}
	return obs, nil
}

func (o Observation) valid(withScope bool) bool {
	if withScope {
		if len(o.Scope) != 64 || strings.Trim(o.Scope, "0123456789abcdef") != "" {
			return false
		}
	}
	return !math.IsNaN(o.UsedPercent) && !math.IsInf(o.UsedPercent, 0) && o.UsedPercent >= 0 &&
		validEpoch(o.ObservedAt) && validEpoch(o.ResetAt) && validEpoch(o.ResetAt-week) &&
		o.ObservedAt >= o.ResetAt-week-5 && o.ObservedAt < o.ResetAt &&
		(o.ResetsAvailable == nil || *o.ResetsAvailable >= 0 && *o.ResetsAvailable <= math.MaxInt32) &&
		(o.ResetsExpireAt == nil || o.ResetsAvailable != nil && *o.ResetsAvailable > 0 && validEpoch(*o.ResetsExpireAt) && *o.ResetsExpireAt > o.ObservedAt)
}

func fetchResetDetails(ctx context.Context, client *http.Client, creds credentials, obs *Observation) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, resetCreditsURL, nil)
	if err != nil {
		return
	}
	req.Header.Set("Authorization", "Bearer "+creds.access)
	req.Header.Set("ChatGPT-Account-Id", creds.account)
	req.Header.Set("User-Agent", "codex-usage-meter/0.1")
	req.Header.Set("Accept", "application/json")
	res, err := client.Do(req)
	if err != nil {
		return
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK || res.Header.Get("Content-Encoding") != "" {
		return
	}
	b, err := boundedRead(res.Body, maxSourceBytes)
	var summary map[string]any
	if err != nil || strictJSON(b, &summary, false) != nil {
		return
	}
	n, ok := integer(summary["available_count"])
	if !ok || n < 0 || n > math.MaxInt32 {
		return
	}
	// The later details response supplies an authoritative count too. In
	// particular, redemption between the two reads must not leave an old count.
	obs.ResetsAvailable = &n
	obs.ResetsExpireAt = earliestResetExpiry(summary["credits"], n, obs.ObservedAt)
}

func earliestResetExpiry(raw any, count, observed int64) *int64 {
	rows, ok := raw.([]any)
	if !ok || count <= 0 {
		return nil
	}
	seen := map[string]bool{}
	var earliest *int64
	for _, rawRow := range rows {
		row := object(rawRow)
		if row == nil {
			return nil
		}
		if row["status"] != "available" {
			continue
		}
		id, ok := row["id"].(string)
		if !ok || id == "" || seen[id] || row["reset_type"] != "codex_rate_limits" {
			return nil
		}
		if supported, present := row["is_supported_by_plan"]; present && supported != true {
			return nil
		}
		seen[id] = true
		rawExpiry, present := row["expires_at"]
		if !present {
			return nil
		}
		if rawExpiry == nil {
			continue
		} // Explicitly non-expiring.
		epoch, valid := integer(rawExpiry)
		if s, ok := rawExpiry.(string); ok {
			t, err := time.Parse(time.RFC3339Nano, s)
			epoch, valid = t.Unix(), err == nil
		}
		if !valid || !validEpoch(epoch) || epoch <= observed {
			return nil
		}
		if earliest == nil || epoch < *earliest {
			earliest = ptr(epoch)
		}
	}
	// A capped or inconsistent list cannot establish the earliest overall expiry.
	if int64(len(seen)) != count {
		return nil
	}
	return earliest
}
