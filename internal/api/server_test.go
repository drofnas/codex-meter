package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

const fixtures = "../../contracts/v1/fixtures"
const testToken = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func fixture(t testing.TB, name string) []byte {
	t.Helper()
	b, e := os.ReadFile(filepath.Join(fixtures, name))
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func testHandler(t testing.TB) *Handler {
	t.Helper()
	h, e := NewHandler(Config{DataDir: t.TempDir(), Token: testToken, Timezone: "America/Los_Angeles", StaleSeconds: 180})
	if e != nil {
		t.Fatal(e)
	}
	h.now = func() time.Time { return time.Unix(1789000000, 0) }
	return h
}
func publish(t testing.TB, h *Handler, raw []byte) {
	t.Helper()
	p := filepath.Join(h.config.DataDir, "next")
	if e := os.WriteFile(p, raw, 0640); e != nil {
		t.Fatal(e)
	}
	if e := os.Rename(p, filepath.Join(h.config.DataDir, "usage.json")); e != nil {
		t.Fatal(e)
	}
}
func request(h *Handler, method, path string, auth []string, body io.Reader) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, body)
	for _, v := range auth {
		r.Header.Add("Authorization", v)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func usage(h *Handler) *httptest.ResponseRecorder {
	return request(h, "GET", "/v1/usage", []string{"Bearer " + testToken}, nil)
}
func assertResponse(t *testing.T, w *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	if w.Header().Get("Content-Type") != "application/json" || w.Header().Get("Cache-Control") != "no-store" || w.Body.Len() > MaxBytes {
		t.Fatal("response headers/size")
	}
	if code != "" {
		var v document
		if json.Unmarshal(w.Body.Bytes(), &v) != nil || v["error"] != code || len(v) != 2 || w.Body.Len() > 256 {
			t.Fatalf("error envelope %s", w.Body.String())
		}
	}
}

func TestHTTPPrecedence(t *testing.T) {
	h := testHandler(t)
	publish(t, h, []byte("broken"))
	auth := []string{"Bearer " + testToken}
	cases := []struct {
		method, path string
		auth         []string
		body         io.Reader
		status       int
		code         string
	}{
		{"POST", "/unknown?x", nil, nil, 404, "not_found"}, {"GET", "/v1/usage/", auth, nil, 404, "not_found"}, {"GET", "/v1%2fusage", auth, nil, 404, "not_found"},
		{"HEAD", "/v1/usage", nil, nil, 405, "method_not_allowed"}, {"POST", "/healthz", nil, nil, 405, "method_not_allowed"},
		{"GET", "/v1/usage?x", nil, nil, 401, "unauthorized"}, {"GET", "/v1/usage", []string{"Bearer wrong"}, nil, 401, "unauthorized"},
		{"GET", "/v1/usage", []string{auth[0], auth[0]}, nil, 401, "unauthorized"}, {"GET", "/v1/usage", []string{"bearer " + testToken}, nil, 401, "unauthorized"},
		{"GET", "/v1/usage", []string{auth[0] + ", " + auth[0]}, nil, 401, "unauthorized"}, {"GET", "/v1/usage", []string{auth[0] + " "}, nil, 401, "unauthorized"},
		{"GET", "/v1/usage?", auth, nil, 400, "bad_request"}, {"GET", "/healthz?x", nil, nil, 400, "bad_request"},
		{"GET", "/v1/usage", auth, strings.NewReader("x"), 400, "bad_request"}, {"GET", "/healthz", nil, strings.NewReader("x"), 400, "bad_request"},
		{"GET", "/v1/usage", auth, nil, 503, "snapshot_invalid"}, {"GET", "/healthz", nil, nil, 200, ""},
	}
	for i, c := range cases {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			w := request(h, c.method, c.path, c.auth, c.body)
			assertResponse(t, w, c.status, c.code)
			if c.status == 401 && w.Header().Get("WWW-Authenticate") != "Bearer" {
				t.Fatal("challenge")
			}
			if c.status == 405 && w.Header().Get("Allow") != "GET" {
				t.Fatal("allow")
			}
		})
	}
	r := httptest.NewRequest("GET", "/healthz", nil)
	r.TransferEncoding = []string{"chunked"}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	assertResponse(t, w, 400, "bad_request")
}
func TestAllContractFixtures(t *testing.T) {
	var manifest []struct{ File, Kind, Expect string }
	if e := json.Unmarshal(fixture(t, "manifest.json"), &manifest); e != nil {
		t.Fatal(e)
	}
	h := testHandler(t)
	for _, c := range manifest {
		if c.Kind != "usage" {
			continue
		}
		t.Run(c.File, func(t *testing.T) {
			raw := fixture(t, c.File)
			publish(t, h, raw)
			status, code := 503, "snapshot_invalid"
			v, e := decode(raw)
			if c.Expect == "valid" {
				if e != nil {
					t.Fatalf("valid wire fixture rejected by decoder: %v", e)
				}
				if v["updated_at"] != nil && v["as_of"] == v["updated_at"] {
					status, code = 200, ""
				}
			}
			if c.Expect == "version" {
				code = "snapshot_version"
			}
			if c.Expect == "oversized" {
				code = "snapshot_oversized"
			}
			w := usage(h)
			assertResponse(t, w, status, code)
			if status == 200 {
				export(t, c.File, w.Body.Bytes())
			}
		})
	}
}
func export(t testing.TB, name string, raw []byte) {
	t.Helper()
	if dir := os.Getenv("API_TEST_RESPONSES_DIR"); dir != "" {
		if e := os.MkdirAll(dir, 0750); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(filepath.Join(dir, name), raw, 0640); e != nil {
			t.Fatal(e)
		}
	}
}
func TestProjectionAndStorageFailures(t *testing.T) {
	h := testHandler(t)
	w := usage(h)
	assertResponse(t, w, 200, "")
	export(t, "missing.json", w.Body.Bytes())
	raw := fixture(t, "normal.json")
	original, e := decode(raw)
	if e != nil {
		t.Fatal(e)
	}
	updated := original["updated_at"].(float64)
	for _, offset := range []int64{-10, 0, 179, 180, 86400, 604800} {
		t.Run(fmt.Sprint(offset), func(t *testing.T) {
			publish(t, h, raw)
			h.now = func() time.Time { return time.Unix(int64(updated)+offset, 0) }
			w := usage(h)
			assertResponse(t, w, 200, "")
			var got document
			_ = json.Unmarshal(w.Body.Bytes(), &got)
			if got["observed_at"] != original["observed_at"] || got["updated_at"] != updated {
				t.Fatal("timestamp refreshed")
			}
			expected := "ok"
			if offset < -5 || offset >= 180 {
				expected = "stale"
			}
			if got["status"] != expected {
				t.Fatalf("status %v", got["status"])
			}
			export(t, fmt.Sprintf("projection-%d.json", offset), w.Body.Bytes())
		})
	}
	path := filepath.Join(h.config.DataDir, "usage.json")
	_ = os.Remove(path)
	if e := os.Symlink(filepath.Join(h.config.DataDir, "missing"), path); e != nil {
		t.Fatal(e)
	}
	assertResponse(t, usage(h), 503, "snapshot_unreadable")
	_ = os.Remove(path)
	if e := os.Mkdir(path, 0700); e != nil {
		t.Fatal(e)
	}
	assertResponse(t, usage(h), 503, "snapshot_unreadable")
	_ = os.Remove(path)
	if e := syscall.Mkfifo(path, 0600); e != nil {
		t.Fatal(e)
	}
	assertResponse(t, usage(h), 503, "snapshot_unreadable")
	_ = os.Remove(path)
	publish(t, h, raw)
	if e := os.Chmod(path, 0000); e != nil {
		t.Fatal(e)
	}
	if os.Geteuid() != 0 {
		assertResponse(t, usage(h), 503, "snapshot_unreadable")
	}
	_ = os.Chmod(path, 0640)
	publish(t, h, append(raw, bytes.Repeat([]byte(" "), MaxBytes-len(raw))...))
	assertResponse(t, usage(h), 200, "")
	publish(t, h, append(raw, bytes.Repeat([]byte(" "), MaxBytes+1-len(raw))...))
	assertResponse(t, usage(h), 503, "snapshot_oversized")
}
func TestStrictStoredShape(t *testing.T) {
	h := testHandler(t)
	raw := fixture(t, "normal.json")
	for _, key := range []string{"source_error", "remaining_percent", "resets_available", "updated_at", "version"} {
		t.Run("missing-"+key, func(t *testing.T) {
			v, _ := decode(raw)
			delete(v, key)
			b, _ := json.Marshal(v)
			publish(t, h, b)
			assertResponse(t, usage(h), 503, "snapshot_invalid")
		})
	}
	mutations := []func(document){
		func(v document) { v["Version"] = v["version"]; delete(v, "version") }, func(v document) { v["updated_at"] = nil }, func(v document) { v["as_of"] = v["as_of"].(float64) + 1 },
		func(v document) { v["cycle"].(document)["state"] = "unknown" }, func(v document) { v["days"].([]any)[0].(document)["used_delta_pp"] = 99.0 },
		func(v document) { v["remaining_percent"] = "20" }, func(v document) { v["source_error"] = "raw-secret-error" }, func(v document) { v["timezone"] = "Local" },
	}
	for i, mutate := range mutations {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			v, _ := decode(raw)
			mutate(v)
			b, _ := json.Marshal(v)
			publish(t, h, b)
			assertResponse(t, usage(h), 503, "snapshot_invalid")
		})
	}
	for _, b := range [][]byte{append(append([]byte{}, raw...), raw...), []byte(`{"version":true}`), []byte(`{"version":1,"version":1}`), []byte(strings.Repeat("[", 25) + strings.Repeat("]", 25))} {
		publish(t, h, b)
		w := usage(h)
		if w.Code != 503 {
			t.Fatal("invalid input accepted")
		}
	}
	// Integral decimal/exponent spellings are valid protocol integers.
	v, _ := decode(raw)
	b, _ := json.Marshal(v)
	b = bytes.Replace(b, []byte(`"version":1`), []byte(`"version":1.0`), 1)
	publish(t, h, b)
	assertResponse(t, usage(h), 200, "")
}
func TestConcurrentAtomicReplacements(t *testing.T) {
	h := testHandler(t)
	a, b := fixture(t, "normal.json"), fixture(t, "zero-remaining.json")
	publish(t, h, a)
	server := httptest.NewServer(h)
	defer server.Close()
	client := server.Client()
	client.Timeout = 5 * time.Second
	var wg sync.WaitGroup
	errs := make(chan string, 8)
	for n := 0; n < 8; n++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 50; i++ {
				r, _ := http.NewRequest("GET", server.URL+"/v1/usage", nil)
				r.Header.Set("Authorization", "Bearer "+testToken)
				resp, e := client.Do(r)
				if e != nil {
					errs <- "HTTP failure"
					return
				}
				raw, e := io.ReadAll(resp.Body)
				resp.Body.Close()
				var v document
				if e != nil || resp.StatusCode != 200 || json.Unmarshal(raw, &v) != nil || (v["remaining_percent"] != 55.0 && v["remaining_percent"] != 0.0) {
					errs <- fmt.Sprintf("mixed/failed response: %s", raw)
					return
				}
			}
		}()
	}
	for i := 0; i < 100; i++ {
		if i%2 == 0 {
			publish(t, h, b)
		} else {
			publish(t, h, a)
		}
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		t.Error(e)
	}
}
func TestServerLimits(t *testing.T) {
	h := testHandler(t)
	s, e := NewServer(h.config)
	if e != nil {
		t.Fatal(e)
	}
	if s.ReadHeaderTimeout != 5*time.Second || s.ReadTimeout != 5*time.Second || s.WriteTimeout != 10*time.Second || s.IdleTimeout != 30*time.Second || s.MaxHeaderBytes != 8192 {
		t.Fatal("server limits")
	}
}
func BenchmarkUsage(b *testing.B) {
	h := testHandler(b)
	publish(b, h, fixture(b, "normal.json"))
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		w := usage(h)
		if w.Code != 200 {
			b.Fatal(w.Code)
		}
	}
}
