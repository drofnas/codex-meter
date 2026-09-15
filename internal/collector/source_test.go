package collector

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var testNow = time.Unix(2000000000-3600, 0)

func sourceFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestSourceSelection(t *testing.T) {
	for _, tc := range []struct {
		name  string
		used  float64
		valid bool
	}{
		{"backend-primary", 20, true}, {"weekly-primary", 20, true}, {"weekly-secondary", 35, true},
		{"absent-weekly", 0, false}, {"missing-bucket", 0, false}, {"ambiguous-weekly", 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			o, err := parseObservation(sourceFixture(t, tc.name), testNow)
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, err)
			}
			if err == nil && (o.UsedPercent != tc.used || o.ResetAt != 2000000000 || o.ObservedAt != testNow.Unix()) {
				t.Fatal(o)
			}
		})
	}
	// A newer unrelated bucket cannot replace the named general bucket.
	var root map[string]any
	if err := json.Unmarshal(sourceFixture(t, "weekly-primary"), &root); err != nil {
		t.Fatal(err)
	}
	alternate := root["additional_rate_limits"].([]any)[0].(map[string]any)["rate_limit"].(map[string]any)
	alternate["primary_window"].(map[string]any)["reset_at"] = float64(2000003600)
	b, _ := json.Marshal(root)
	o, err := parseObservation(b, testNow)
	if err != nil || o.UsedPercent != 20 {
		t.Fatal(o, err)
	}
}

func TestMalformedSourceAndClamp(t *testing.T) {
	b := string(sourceFixture(t, "backend-primary"))
	for _, value := range []string{"null", `"20"`, "true", "-1", "1e999", "NaN", "Infinity"} {
		t.Run(value, func(t *testing.T) {
			if _, err := parseObservation([]byte(strings.Replace(b, `"used_percent": 20`, `"used_percent": `+value, 1)), testNow); err == nil {
				t.Fatal("invalid quota accepted")
			}
		})
	}
	for _, used := range []string{"0", "100", "101", "1e100"} {
		o, err := parseObservation([]byte(strings.Replace(b, `"used_percent": 20`, `"used_percent": `+used, 1)), testNow)
		if err != nil {
			t.Fatal(err)
		}
		o.Scope = strings.Repeat("a", 64)
		s, err := snapshot(&o, nil, testNow.Unix(), testConfig(t))
		if err != nil || *s.Remaining < 0 || *s.Remaining > 100 || used != "0" && *s.Remaining != 0 {
			t.Fatal(s, err)
		}
	}
	for _, bad := range [][]byte{
		nil, []byte("null"), []byte("[]"), []byte(b + " {}"), []byte(strings.Repeat(" ", maxSourceBytes+1)),
		[]byte(strings.Replace(b, `"used_percent": 20`, `"used_percent": 20,"used_percent": 21`, 1)),
		[]byte(strings.Replace(b, `"used_percent": 20,`, "", 1)), []byte("{\"bad\":\"\xff\"}"),
		[]byte(strings.Repeat("[", 66) + "0" + strings.Repeat("]", 66)), []byte(b[:len(b)/2]),
	} {
		if _, err := parseObservation(bad, testNow); err == nil {
			t.Fatal("malformed source accepted")
		}
	}
	for _, value := range []string{"0", "-1", "4102444801", "1999996400", "2000604806", "2000000000.5", "null", "true"} {
		if _, err := parseObservation([]byte(strings.ReplaceAll(b, `"reset_at": 2000000000`, `"reset_at": `+value)), testNow); err == nil {
			t.Fatalf("bad reset accepted: %s", value)
		}
	}
	for _, value := range []string{"null", "-1", `"3"`, "1.5", "2147483648", "true", "1e999"} {
		o, err := parseObservation([]byte(strings.Replace(b, `"available_count": 3`, `"available_count": `+value, 1)), testNow)
		if err != nil || o.ResetsAvailable != nil {
			t.Fatal("optional count contaminated quota", err)
		}
	}
	for _, value := range []string{"0", "2147483647", "3.0"} {
		o, err := parseObservation([]byte(strings.Replace(b, `"available_count": 3`, `"available_count": `+value, 1)), testNow)
		if err != nil || o.ResetsAvailable == nil {
			t.Fatal("valid count rejected", err)
		}
	}
}

func TestSourceObservationTime(t *testing.T) {
	b := string(sourceFixture(t, "backend-primary"))
	for _, tc := range []struct {
		raw   string
		valid bool
	}{
		{fmt.Sprint(testNow.Unix() - 60), true}, {fmt.Sprint(testNow.Unix()), true},
		{fmt.Sprint(testNow.Unix() + 1), false}, {"null", false}, {"0", false}, {"true", false}, {"1.5", false},
	} {
		input := strings.Replace(b, "{", `{"observed_at":`+tc.raw+",", 1)
		o, err := parseObservation([]byte(input), testNow)
		if (err == nil) != tc.valid {
			t.Fatal(tc, err)
		}
		if err == nil && fmt.Sprint(o.ObservedAt) != tc.raw {
			t.Fatal("source timestamp replaced")
		}
	}
}

func authFixture(t *testing.T, expiry int64) string {
	t.Helper()
	payload := base64.RawURLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"exp":%d}`, expiry)))
	content := fmt.Sprintf(`{"auth_mode":"chatgpt","tokens":{"access_token":"header.%s.signature","account_id":"synthetic-account","refresh_token":"never-use-or-output"}}`, payload)
	path := filepath.Join(t.TempDir(), "auth.json")
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestFetchPrivacyRecoveryAndCompletionTime(t *testing.T) {
	path := authFixture(t, testNow.Unix()+3600)
	before, _ := os.ReadFile(path)
	clock := testNow
	calls := 0
	client := newClient(time.Second)
	client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.String() == resetCreditsURL {
			if r.Method != "GET" {
				t.Fatal("unsafe reset request")
			}
			return &http.Response{StatusCode: 503, Body: io.NopCloser(strings.NewReader("unavailable")), Header: make(http.Header)}, nil
		}
		calls++
		if r.Method != "GET" || r.URL.String() != usageURL || r.Header.Get("ChatGPT-Account-Id") != "synthetic-account" || strings.Contains(r.Header.Get("Authorization"), "never-use-or-output") {
			t.Fatal("unsafe request")
		}
		clock = clock.Add(2 * time.Second)
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(sourceFixture(t, "backend-primary")))), Header: make(http.Header)}, nil
	})
	now := func() time.Time { return clock }
	key := []byte(strings.Repeat("k", 32))
	o, err := fetch(context.Background(), client, path, key, now)
	if err != nil || o.ObservedAt != testNow.Unix()+2 || calls != 1 {
		t.Fatal(o, err, calls)
	}
	b, _ := json.Marshal(o)
	for _, secret := range []string{"synthetic-account", "signature", "never-use-or-output", "refresh_token", "access_token"} {
		if strings.Contains(string(b), secret) {
			t.Fatal("secret leaked")
		}
	}
	if o.Scope != scopeFor(key, "synthetic-account") || o.Scope == scopeFor(key, "another-account") || o.Scope == scopeFor([]byte(strings.Repeat("x", 32)), "synthetic-account") {
		t.Fatal("scope isolation failed")
	}
	after, _ := os.ReadFile(path)
	if string(after) != string(before) {
		t.Fatal("credentials changed")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err = fetch(context.Background(), client, path, key, now); !errors.Is(err, errAuthMissing) || calls != 1 {
		t.Fatal("missing auth reached network")
	}
	if err := os.WriteFile(path, before, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = fetch(context.Background(), client, path, key, now); err != nil || calls != 2 {
		t.Fatal("recovery failed", err)
	}
	for _, offset := range []int64{-1, 0, 30} {
		if _, err := readCredentials(authFixture(t, testNow.Unix()+offset), testNow); !errors.Is(err, errAuthFailed) {
			t.Fatal(err)
		}
	}
}

func TestHTTPFailuresAndBounds(t *testing.T) {
	path := authFixture(t, testNow.Unix()+3600)
	for _, code := range []int{301, 302, 401, 403, 429, 500} {
		client := newClient(time.Second)
		calls := 0
		client.Transport = transportFunc(func(*http.Request) (*http.Response, error) {
			calls++
			return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader("secret-body")), Header: http.Header{"Location": []string{"https://untrusted.example/"}}}, nil
		})
		_, err := fetch(context.Background(), client, path, make([]byte, 32), func() time.Time { return testNow })
		if err == nil || calls != 1 || strings.Contains(err.Error(), "secret") {
			t.Fatal(code, err, calls)
		}
		if (code == 401 || code == 403) && err != errAuthFailed {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		err  error
		want error
	}{{context.DeadlineExceeded, errSourceTimeout}, {errors.New("private-url-and-token"), errSourceUnavailable}} {
		client := newClient(time.Second)
		client.Transport = transportFunc(func(*http.Request) (*http.Response, error) { return nil, tc.err })
		_, err := fetch(context.Background(), client, path, make([]byte, 32), func() time.Time { return testNow })
		if err != tc.want {
			t.Fatal(err)
		}
	}
	if _, err := boundedRead(strings.NewReader("12345"), 4); err == nil {
		t.Fatal("oversize accepted")
	}
	if _, err := boundedRead(strings.NewReader("1234"), 4); err != nil {
		t.Fatal(err)
	}
	client := newClient(time.Second)
	tr := client.Transport.(*http.Transport)
	if tr.Proxy != nil || !tr.DisableCompression || tr.MaxConnsPerHost != 1 || client.CheckRedirect(nil, nil) != http.ErrUseLastResponse {
		t.Fatal("unsafe HTTP policy")
	}
}
