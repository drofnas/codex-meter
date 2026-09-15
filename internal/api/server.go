package api

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

type Config struct {
	DataDir, Address, Token, Timezone string
	StaleSeconds                      int64
}

type Handler struct {
	config Config
	token  [32]byte
	now    func() time.Time
}

func NewHandler(c Config) (*Handler, error) {
	if !hexToken(c.Token) || c.DataDir == "" || c.StaleSeconds < 30 || c.StaleSeconds > 3600 {
		return nil, errors.New("invalid_configuration")
	}
	if _, err := zone(c.Timezone); err != nil {
		return nil, errors.New("invalid_configuration")
	}
	return &Handler{c, sha256.Sum256([]byte("Bearer " + c.Token)), time.Now}, nil
}
func NewServer(c Config) (*http.Server, error) {
	h, err := NewHandler(c)
	if err != nil {
		return nil, err
	}
	return &http.Server{Addr: c.Address, Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 5 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8 * 1024, ErrorLog: log.New(io.Discard, "", 0)}, nil
}
func respond(w http.ResponseWriter, status int, value any) {
	raw, err := json.Marshal(value)
	if err != nil || len(raw) > MaxBytes {
		status = http.StatusServiceUnavailable
		raw = []byte(`{"version":1,"error":"snapshot_invalid"}`)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = w.Write(raw)
}
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	version := 1
	if r.URL.Path == "/v2/usage" || r.URL.Path == "/v2/history" {
		version = 2
	}
	fail := func(status int, code string) { respond(w, status, document{"version": version, "error": code}) }
	if (r.URL.Path != "/v1/usage" && r.URL.Path != "/v2/usage" && r.URL.Path != "/v2/history" && r.URL.Path != "/healthz") || r.URL.EscapedPath() != r.URL.Path {
		fail(404, "not_found")
		return
	}
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", "GET")
		fail(405, "method_not_allowed")
		return
	}
	if r.URL.Path != "/healthz" {
		values := r.Header.Values("Authorization")
		candidate := sha256.Sum256([]byte(r.Header.Get("Authorization")))
		authorized := subtle.ConstantTimeCompare(candidate[:], h.token[:]) == 1
		if len(values) != 1 || !authorized {
			w.Header().Set("WWW-Authenticate", "Bearer")
			fail(401, "unauthorized")
			return
		}
	}
	if r.URL.RawQuery != "" || r.URL.ForceQuery || r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
		fail(400, "bad_request")
		return
	}
	if r.URL.Path == "/healthz" {
		respond(w, 200, document{"version": 1, "status": "ok"})
		return
	}
	now := h.now().Unix()
	if now < 1 || now > maxEpoch {
		fail(503, "snapshot_invalid")
		return
	}
	if r.URL.Path == "/v2/history" {
		h.serveHistory(w, now)
		return
	}
	raw, err := readSnapshot(filepath.Join(h.config.DataDir, "usage.json"))
	if os.IsNotExist(err) {
		if version == 2 {
			respond(w, 200, unavailableV2(now, h.config.Timezone, h.config.StaleSeconds))
		} else {
			respond(w, 200, unavailable(now, h.config.Timezone, h.config.StaleSeconds))
		}
		return
	}
	if err != nil {
		fail(503, err.Error())
		return
	}
	v, err := decodeVersion(raw, MaxBytes, version)
	if err == nil {
		err = validateVersion(v, version)
	}
	if err != nil {
		fail(503, err.Error())
		return
	}
	project(v, now)
	respond(w, 200, v)
}
func readSnapshot(path string) ([]byte, error) { return readBounded(path, MaxBytes) }
func readBounded(path string, limit int) ([]byte, error) {
	// O_NONBLOCK prevents a raced-in FIFO from hanging a worker; O_NOFOLLOW
	// refuses symlinks to anything outside the usage-only directory.
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if errors.Is(err, syscall.ENOENT) {
			return nil, os.ErrNotExist
		}
		return nil, errors.New("snapshot_unreadable")
	}
	f := os.NewFile(uintptr(fd), "usage.json")
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("snapshot_unreadable")
	}
	if info.Size() > int64(limit) {
		return nil, errors.New("snapshot_oversized")
	}
	raw, err := io.ReadAll(io.LimitReader(f, int64(limit+1)))
	if err != nil {
		return nil, errors.New("snapshot_unreadable")
	}
	if len(raw) > limit {
		return nil, errors.New("snapshot_oversized")
	}
	return raw, nil
}
