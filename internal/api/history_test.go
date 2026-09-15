package api

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func v2Fixture(t *testing.T, name string) document {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("../../contracts/v2/fixtures", name+".json"))
	if err != nil {
		t.Fatal(err)
	}
	v, err := decodeVersion(raw, MaxBytes, 2)
	if err != nil || validateVersion(v, 2) != nil {
		t.Fatal("invalid fixture", err)
	}
	return v
}
func TestV2CalendarAndHistoryRoutes(t *testing.T) {
	h, err := NewHandler(Config{DataDir: t.TempDir(), Token: testToken, Timezone: "America/Los_Angeles", StaleSeconds: 180})
	if err != nil {
		t.Fatal(err)
	}
	v := v2Fixture(t, "saturday-five-am")
	now := int64(v["updated_at"].(float64))
	h.now = func() time.Time { return time.Unix(now, 0) }
	raw, _ := json.Marshal(v)
	if err = os.WriteFile(filepath.Join(h.config.DataDir, "usage.json"), raw, 0640); err != nil {
		t.Fatal(err)
	}
	history := document{"version": 2, "scope": v["scope"], "updated_at": v["updated_at"], "retention_seconds": RetentionSeconds, "cycles": []any{v}}
	raw, _ = json.Marshal(history)
	if err = os.WriteFile(filepath.Join(h.config.DataDir, "history.json"), raw, 0640); err != nil {
		t.Fatal(err)
	}
	request := func(path string, auth bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest("GET", path, nil)
		if auth {
			r.Header.Set("Authorization", "Bearer "+testToken)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	for _, path := range []string{"/v2/usage", "/v2/history"} {
		if request(path, false).Code != 401 || request(path, true).Code != 200 {
			t.Fatal("route/auth failure", path)
		}
	}
	if request("/v1/usage", true).Code != 503 {
		t.Fatal("v1 accepted v2")
	}
	if request("/v2/history?scope=other", true).Code != 400 {
		t.Fatal("unsupported query accepted")
	}
	now += 2 * 86400
	w := request("/v2/usage", true)
	got, err := decodeVersion(w.Body.Bytes(), MaxBytes, 2)
	if err != nil {
		t.Fatal(err)
	}
	days := got["days"].([]any)
	if len(days) != 8 || days[0].(document)["label"] != "Sa" || days[7].(document)["label"] != "Sa" {
		t.Fatal("week moved")
	}
	old := days[0].(document)["start_at"]
	if old != v["days"].([]any)[0].(document)["start_at"] {
		t.Fatal("lost first Saturday")
	}
	// Archive response has its own larger bound and rejects mixed scopes/corruption.
	cycles := history["cycles"].([]any)
	cycles[0].(document)["scope"] = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	raw, _ = json.Marshal(history)
	os.WriteFile(filepath.Join(h.config.DataDir, "history.json"), raw, 0640)
	if request("/v2/history", true).Code != 503 {
		t.Fatal("mixed scope accepted")
	}
}
func TestV2FixtureContract(t *testing.T) {
	raw, err := os.ReadFile("../../contracts/v2/fixtures/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct{ File, Expect string }
	if json.Unmarshal(raw, &cases) != nil {
		t.Fatal("manifest")
	}
	for _, tc := range cases {
		t.Run(tc.File, func(t *testing.T) {
			raw, err := os.ReadFile("../../contracts/v2/fixtures/" + tc.File)
			if err != nil {
				t.Fatal(err)
			}
			v, err := decodeVersion(raw, MaxBytes, 2)
			if err == nil {
				err = validateVersion(v, 2)
			}
			// The missing-file fixture is a response, never a persisted publication.
			var fixture document
			json.Unmarshal(raw, &fixture)
			valid := tc.Expect == "valid" && fixture["updated_at"] != nil && fixture["as_of"] == fixture["updated_at"]
			if (err == nil) != valid {
				t.Fatal("contract disagreement", tc.Expect, err)
			}
		})
	}
}

func TestResetFactsSurviveAPIProjectionAndReplacement(t *testing.T) {
	h, err := NewHandler(Config{DataDir: t.TempDir(), Token: testToken, Timezone: "America/Los_Angeles", StaleSeconds: 180})
	if err != nil {
		t.Fatal(err)
	}
	v := v2Fixture(t, "resets-red")
	now := int64(v["updated_at"].(float64)) + 60
	h.now = func() time.Time { return time.Unix(now, 0) }
	for _, name := range []string{"resets-red", "resets-blue", "resets-zero", "resets-unknown", "normal"} {
		v = v2Fixture(t, name)
		raw, _ := json.Marshal(v)
		if err := os.WriteFile(filepath.Join(h.config.DataDir, "usage.json"), raw, 0640); err != nil {
			t.Fatal(err)
		}
		r := httptest.NewRequest("GET", "/v2/usage", nil)
		r.Header.Set("Authorization", "Bearer "+testToken)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		got, err := decodeVersion(w.Body.Bytes(), MaxBytes, 2)
		if w.Code != 200 || err != nil || got["resets_available"] != v["resets_available"] || got["resets_expire_at"] != v["resets_expire_at"] || got["as_of"] != float64(now) {
			t.Fatal(name, "reset facts changed", err)
		}
	}
}
