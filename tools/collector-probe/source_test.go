package main

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

var testNow = time.Unix(1900000000, 0)

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestQuotaFixtures(t *testing.T) {
	for _, tc := range []struct {
		name    string
		used    float64
		resets  *int64
		wantErr error
	}{
		{"backend-primary", 20, intPtr(3), nil}, {"weekly-primary", 20, intPtr(0), nil},
		{"weekly-secondary", 35, nil, nil},
		{"absent-weekly", 0, nil, errWeekly}, {"missing-bucket", 0, nil, errWeekly},
		{"ambiguous-weekly", 0, nil, errWeekly},
	} {
		t.Run(tc.name, func(t *testing.T) {
			obs, err := parseObservation(fixture(t, tc.name), testNow)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("got %v want %v", err, tc.wantErr)
			}
			if err != nil {
				return
			}
			if obs.UsedPercent != tc.used || obs.Remaining != 100-tc.used || obs.WindowMinutes != 10080 || obs.Bucket != "codex" || obs.ResetsAt != 2000000000 || obs.ObservedAt != testNow.Unix() {
				t.Fatalf("unexpected observation: %+v", obs)
			}
			if (obs.AvailableResets == nil) != (tc.resets == nil) || tc.resets != nil && *obs.AvailableResets != *tc.resets {
				t.Fatal("incorrect reset count")
			}
		})
	}
}

func intPtr(n int64) *int64 { return &n }

func TestMalformedQuota(t *testing.T) {
	b := string(fixture(t, "backend-primary"))
	for _, replacement := range []string{"null", `"20"`, "true", "-1", "101", "1e999"} {
		t.Run(replacement, func(t *testing.T) {
			modified := strings.Replace(b, `"used_percent": 20`, `"used_percent": `+replacement, 1)
			if _, err := parseObservation([]byte(modified), testNow); err == nil {
				t.Fatal("accepted invalid quota")
			}
		})
	}
	for _, bad := range [][]byte{nil, []byte("null"), []byte("[]"), append([]byte(b), []byte(" {}")...), []byte(strings.Repeat(" ", maxSourceBytes+1))} {
		if _, err := parseObservation(bad, testNow); err == nil {
			t.Fatal("accepted invalid source")
		}
	}
	for _, value := range []string{"0", "100"} {
		obs, err := parseObservation([]byte(strings.Replace(b, `"used_percent": 20`, `"used_percent": `+value, 1)), testNow)
		if err != nil || obs.Remaining < 0 || obs.Remaining > 100 {
			t.Fatal("valid boundary rejected")
		}
	}
}

func TestOptionalCountCannotInvalidateQuota(t *testing.T) {
	b := string(fixture(t, "backend-primary"))
	for _, value := range []string{"null", "-1", `"3"`, "1.5"} {
		obs, err := parseObservation([]byte(strings.Replace(b, `"available_count": 3`, `"available_count": `+value, 1)), testNow)
		if err != nil || obs.AvailableResets != nil {
			t.Fatal("optional invalid field contaminated quota")
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

func TestCredentialExpiryAndReload(t *testing.T) {
	if _, err := readCredentials(filepath.Join(t.TempDir(), "missing"), testNow); !errors.Is(err, errAuthUnavailable) {
		t.Fatal(err)
	}
	for _, offset := range []int64{-1, 0, 30} {
		p := authFixture(t, testNow.Unix()+offset)
		if _, err := readCredentials(p, testNow); !errors.Is(err, errAuthExpired) {
			t.Fatalf("offset %d: %v", offset, err)
		}
	}
	p := authFixture(t, testNow.Unix()+3600)
	before, _ := os.ReadFile(p)
	if _, err := readCredentials(p, testNow); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(p)
	if string(before) != string(after) {
		t.Fatal("auth file changed")
	}
	if err := os.WriteFile(p, []byte(`{"auth_mode":"chatgpt","tokens":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readCredentials(p, testNow); !errors.Is(err, errAuthUnavailable) {
		t.Fatal("credential removal was not reloaded")
	}
	if err := os.WriteFile(p, before, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readCredentials(p, testNow); err != nil {
		t.Fatal("credential restoration was not reloaded")
	}
}

func TestFetchOnlyAllowedRead(t *testing.T) {
	p := authFixture(t, testNow.Unix()+3600)
	before, _ := os.ReadFile(p)
	calls := 0
	client := &http.Client{Transport: transportFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != "GET" || r.URL.String() != usageURL || r.Header.Get("ChatGPT-Account-Id") != "synthetic-account" || !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer header.") {
			t.Fatal("unexpected request")
		}
		if strings.Contains(r.Header.Get("Authorization"), "never-use-or-output") {
			t.Fatal("refresh token used")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(fixture(t, "backend-primary")))), Header: make(http.Header)}, nil
	})}
	obs, err := fetch(context.Background(), client, p, testNow)
	if err != nil || calls != 1 {
		t.Fatalf("fetch: %v calls %d", err, calls)
	}
	out, _ := json.Marshal(obs)
	for _, secret := range []string{"synthetic-account", "signature", "refresh_token", "never-use-or-output"} {
		if strings.Contains(string(out), secret) {
			t.Fatal("output leaked credential")
		}
	}
	after, _ := os.ReadFile(p)
	if string(after) != string(before) {
		t.Fatal("fetch changed auth file")
	}
}

func TestHTTPFailuresNeverRefreshOrLeak(t *testing.T) {
	p := authFixture(t, testNow.Unix()+3600)
	for _, code := range []int{301, 302, 401, 403, 429, 500} {
		calls := 0
		client := usageClient()
		client.Transport = transportFunc(func(r *http.Request) (*http.Response, error) {
			calls++
			return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader("secret-response-body")), Header: http.Header{"Location": []string{"https://untrusted.example/"}}}, nil
		})
		_, err := fetch(context.Background(), client, p, testNow)
		if err == nil || calls != 1 || strings.Contains(err.Error(), "secret") {
			t.Fatalf("status %d: %v calls %d", code, err, calls)
		}
	}
	client := usageClient()
	calls := 0
	client.Transport = transportFunc(func(*http.Request) (*http.Response, error) {
		calls++
		return nil, errors.New("sensitive transport details")
	})
	_, err := fetch(context.Background(), client, p, testNow)
	if !errors.Is(err, errUpstream) || strings.Contains(err.Error(), "sensitive") {
		t.Fatal("unsafe network error")
	}
	_, err = fetch(context.Background(), client, authFixture(t, testNow.Unix()-1), testNow)
	if !errors.Is(err, errAuthExpired) || calls != 1 {
		t.Fatal("expired credential reached network")
	}
}

func TestBoundedRead(t *testing.T) {
	if _, err := boundedRead(strings.NewReader(strings.Repeat("x", maxSourceBytes+1)), maxSourceBytes); err == nil {
		t.Fatal("oversized body accepted")
	}
	if _, err := boundedRead(strings.NewReader("1234"), 4); err != nil {
		t.Fatal(err)
	}
}
