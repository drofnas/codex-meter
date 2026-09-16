package collector

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"syscall"
)

var errStorage = errors.New("storage_unavailable")
var errLocked = errors.New("collector_already_running")

type diskState struct {
	Version     int          `json:"version"`
	KeyID       string       `json:"key_id"`
	Observation *Observation `json:"observation"`
	PublishedAt int64        `json:"published_at"`
	Archive     []Snapshot   `json:"archive"`
	History     history      `json:"history"`
}

type Store struct {
	config  Config
	key     []byte
	loadErr error
	locks   []*os.File
	// Injection seam for interrupted-write tests; production uses os.Rename.
	rename func(string, string) error
}

func OpenStore(c Config) (*Store, error) {
	s := &Store{config: c, rename: os.Rename}
	for _, dir := range []struct {
		path string
		mode os.FileMode
	}{{c.StateDir, 0700}, {c.DataDir, 0750}} {
		if err := os.MkdirAll(dir.path, dir.mode); err != nil {
			s.Close()
			return nil, errStorage
		}
		info, err := os.Stat(dir.path)
		if err != nil || !info.IsDir() || info.Mode().Perm() & ^dir.mode != 0 {
			s.Close()
			return nil, errStorage
		}
		f, err := openNoFollow(filepath.Join(dir.path, ".collector.lock"), os.O_CREATE|os.O_RDWR, 0600)
		if err != nil {
			s.Close()
			return nil, errStorage
		}
		if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
			f.Close()
			s.Close()
			return nil, errLocked
		}
		s.locks = append(s.locks, f)
	}
	keyPath := filepath.Join(c.StateDir, "installation.key")
	key, err := readFile(keyPath, 32)
	if err != nil && !os.IsNotExist(err) {
		s.Close()
		return nil, errStorage
	}
	if os.IsNotExist(err) {
		key = make([]byte, 32)
		if _, err = rand.Read(key); err != nil {
			s.Close()
			return nil, errStorage
		}
		if err = s.atomicWrite(keyPath, key, 0600); err != nil {
			s.Close()
			return nil, err
		}
	} else {
		info, statErr := os.Lstat(keyPath)
		if statErr != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || len(key) != 32 {
			s.Close()
			return nil, errStorage
		}
	}
	s.key = key
	return s, nil
}

func (s *Store) Close() {
	for _, f := range s.locks {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}
	s.locks = nil
}

func openNoFollow(path string, flags int, mode os.FileMode) (*os.File, error) {
	fd, err := syscall.Open(path, flags|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, uint32(mode))
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() {
		f.Close()
		return nil, errStorage
	}
	return f, nil
}

// Fixed, per-target temporary names plus exclusive directory locks bound debris
// even across SIGKILL. The destination is untouched until a complete fsynced file
// is renamed on the same filesystem. Never follow a leftover symlink.
func (s *Store) atomicWrite(path string, b []byte, mode os.FileMode) error {
	temp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+".tmp")
	f, err := openNoFollow(temp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return errStorage
	}
	defer os.Remove(temp)
	if err = f.Chmod(mode); err == nil {
		_, err = f.Write(b)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil || closeErr != nil {
		return errStorage
	}
	if err = s.rename(temp, path); err != nil {
		return errStorage
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return errStorage
	}
	defer dir.Close()
	if err = dir.Sync(); err != nil {
		return errStorage
	}
	return nil
}

func (s *Store) keyID() string { hash := sha256.Sum256(s.key); return hex.EncodeToString(hash[:]) }

func (s *Store) load() diskState {
	s.loadErr = nil
	empty := diskState{Version: 4, KeyID: s.keyID(), Archive: []Snapshot{}}
	b, err := readFile(filepath.Join(s.config.StateDir, "observation.json"), maxHistoryBytes)
	if err != nil {
		return empty
	}
	var fields map[string]any
	if strictJSON(b, &fields, false) != nil {
		return empty
	}
	version, _ := integer(fields["version"])
	legacy := version == 1 || version == 2
	legacyHistory := object(fields["history"])
	if legacy {
		// Forward-only migration: retain the authoritative observation, not totals
		// that cannot be split at local midnight. Back up exact bytes once.
		want := 4
		if version == 2 {
			want = 5
		}
		if len(fields) != want {
			return empty
		}
		if _, ok := fields["history"]; ok != (want == 5) {
			return empty
		}
		delete(fields, "history")
		fields["version"] = json.Number("4")
		fields["history"] = history{}
		fields["archive"] = []Snapshot{}
	}
	if version == 3 {
		// v3 already has daily evidence. Only the correction baseline is new.
		h := object(fields["history"])
		if h == nil {
			return empty
		}
		if _, exists := h["high_used"]; exists {
			return empty
		}
		h["high_used"] = json.Number("0")
		if o := object(fields["observation"]); o != nil {
			h["high_used"] = o["used_percent"]
		}
		fields["version"] = json.Number("4")
	}
	converted, _ := json.Marshal(fields)
	var state diskState
	if strictJSON(converted, &state, true) != nil || state.KeyID != s.keyID() || !validEpoch(state.PublishedAt) {
		return empty
	}
	if state.Observation != nil {
		o := object(fields["observation"])
		for _, key := range []string{"scope", "used_percent", "reset_at", "observed_at", "resets_available"} {
			if _, ok := o[key]; !ok {
				return empty
			}
		}
		if o["used_percent"] == nil || !state.Observation.valid(true) || state.PublishedAt < state.Observation.ObservedAt {
			return empty
		}
	}
	if legacy {
		if s.backupUpgrade(b) != nil {
			s.loadErr = errStorage
			return empty
		}
		if state.Observation != nil {
			state.History = newHistory(*state.Observation, "observed", s.config.Zone)
			state.History.BaselineUsable = false
			if legacyHistory["state"] == "ambiguous" {
				anchor, ok := integer(legacyHistory["stable_end"])
				if !ok || !validEpoch(anchor-week) {
					return empty
				}
				stable := *state.Observation
				stable.ResetAt = anchor
				state.History = newHistory(stable, "ambiguous", s.config.Zone)
				state.History.BaselineUsable = false
			}
		}
		return state
	}
	// Round-trip shape equality rejects missing/null scalar fields and truncated
	// arrays that encoding/json otherwise silently fills with zero values.
	encoded, err := json.Marshal(state)
	var canonical map[string]any
	if err != nil || strictJSON(encoded, &canonical, false) != nil || !reflect.DeepEqual(fields, canonical) || state.Version != 4 || !state.History.valid(state.Observation) || !state.archiveValid() {
		return empty
	}
	if version == 3 && s.backupUpgrade(b) != nil {
		s.loadErr = errStorage
		return empty
	}
	return state
}

func (s *Store) backupUpgrade(b []byte) error {
	path := filepath.Join(s.config.StateDir, "observation.pre-v4.json")
	if info, err := os.Lstat(path); os.IsNotExist(err) {
		return s.atomicWrite(path, b, 0600)
	} else if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return errStorage
	}
	return nil
}

func (s *Store) backup(name string, state diskState) error {
	b, err := json.Marshal(state)
	if err != nil || len(b) > maxHistoryBytes {
		return errStorage
	}
	return s.atomicWrite(filepath.Join(s.config.StateDir, name), b, 0600)
}

func (s *Store) publish(state diskState, current Snapshot) error {
	if s.loadErr != nil {
		return s.loadErr
	}
	b, err := encodeSnapshot(current)
	if err != nil {
		return err
	}
	private, err := json.Marshal(state)
	if err != nil || len(private) > maxHistoryBytes {
		return errStorage
	}
	h := HistorySnapshot{2, current.Scope, current.UpdatedAt, retentionSeconds, state.Archive}
	archive, err := json.Marshal(h)
	if err != nil || len(archive) > maxHistoryBytes || len(state.Archive) > maxPeriods {
		return errStorage
	}
	// A crash between writes leaves a valid old public file and a newer private
	// observation. Restart can safely publish that baseline without reading logs.
	if err := s.atomicWrite(filepath.Join(s.config.StateDir, "observation.json"), private, 0600); err != nil {
		return err
	}
	if err = s.atomicWrite(filepath.Join(s.config.DataDir, "history.json"), archive, 0640); err != nil {
		return err
	}
	return s.atomicWrite(filepath.Join(s.config.DataDir, "usage.json"), b, 0640)
}
