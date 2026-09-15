package collector

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestStoreIsolationAndRestart(t *testing.T) {
	c := testConfig(t)
	s, err := OpenStore(c)
	if err != nil {
		t.Fatal(err)
	}
	key := append([]byte(nil), s.key...)
	if _, err := OpenStore(c); err != errLocked {
		t.Fatal("second writer accepted", err)
	}
	other := c
	other.StateDir = filepath.Join(t.TempDir(), "state")
	if _, err := OpenStore(other); err != errLocked {
		t.Fatal("data lock bypassed", err)
	}
	s.Close()
	s, err = OpenStore(c)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if !bytes.Equal(key, s.key) {
		t.Fatal("installation scope changed")
	}
	for _, path := range []string{c.StateDir, filepath.Join(c.StateDir, "installation.key")} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm()&0077 != 0 {
			t.Fatal("private permissions", path, err)
		}
	}
	files, _ := os.ReadDir(c.DataDir)
	for _, f := range files {
		if f.Name() != ".collector.lock" {
			t.Fatal("private data in mount", f.Name())
		}
	}
}

func TestCrashAtRename(t *testing.T) {
	if root := os.Getenv("COLLECTOR_CRASH_DIR"); root != "" {
		c := Config{DataDir: filepath.Join(root, "data"), StateDir: filepath.Join(root, "state"), Zone: time.UTC, StaleSeconds: 180}
		s, err := OpenStore(c)
		if err != nil {
			t.Fatal(err)
		}
		s.rename = func(_, _ string) error { os.Exit(23); return nil }
		_ = s.atomicWrite(filepath.Join(c.DataDir, "usage.json"), []byte(`{"new":"complete-but-unpublished"}`), 0640)
		t.Fatal("crash hook did not run")
	}
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(e.config.DataDir, "usage.json")
	before, _ := os.ReadFile(path)
	e.store.Close()
	child := exec.Command(os.Args[0], "-test.run=^TestCrashAtRename$")
	child.Env = append(os.Environ(), "COLLECTOR_CRASH_DIR="+filepath.Dir(e.config.DataDir))
	if err := child.Run(); err == nil || child.ProcessState.ExitCode() != 23 {
		t.Fatal("unexpected child exit", err)
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("crash replaced previous valid snapshot")
	}
	store, err := OpenStore(e.config)
	if err != nil {
		t.Fatal("crash did not release writer locks", err)
	}
	defer store.Close()
	restarted := &engine{config: e.config, state: store.load(), store: store}
	obs.ObservedAt += 60
	obs.UsedPercent = 25
	if _, err := restarted.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(e.config.DataDir, ".usage.json.tmp")); !os.IsNotExist(err) {
		t.Fatal("crash debris retained", err)
	}
}

func TestCorruptStateAndKeyRotation(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(e.config.StateDir, "observation.json")
	valid, _ := os.ReadFile(path)
	var fields map[string]any
	_ = json.Unmarshal(valid, &fields)
	fields["observation"].(map[string]any)["used_percent"] = nil
	badNull, _ := json.Marshal(fields)
	delete(fields["observation"].(map[string]any), "used_percent")
	badMissing, _ := json.Marshal(fields)
	for _, bad := range [][]byte{[]byte("{"), []byte("null"), badNull, badMissing, bytes.Repeat([]byte(" "), 4097)} {
		if err := os.WriteFile(path, bad, 0600); err != nil {
			t.Fatal(err)
		}
		if got := e.store.load(); got.Observation != nil || got.PublishedAt != 0 {
			t.Fatal("corrupt baseline trusted")
		}
	}
	if err := os.WriteFile(path, valid, 0600); err != nil {
		t.Fatal(err)
	}
	e.store.key = bytes.Repeat([]byte{42}, 32)
	if e.store.load().Observation != nil {
		t.Fatal("rotated key reused old account scope")
	}
}

func TestTemporarySymlinksAndDebris(t *testing.T) {
	e := testEngine(t)
	outside := filepath.Join(t.TempDir(), "untouched")
	if err := os.WriteFile(outside, []byte("untouched"), 0600); err != nil {
		t.Fatal(err)
	}
	temp := filepath.Join(e.config.DataDir, ".usage.json.tmp")
	if err := os.Symlink(outside, temp); err != nil {
		t.Fatal(err)
	}
	if err := e.store.atomicWrite(filepath.Join(e.config.DataDir, "usage.json"), []byte("{}"), 0640); err != errStorage {
		t.Fatal("followed temp symlink", err)
	}
	b, _ := os.ReadFile(outside)
	if string(b) != "untouched" {
		t.Fatal("wrote through symlink")
	}
	if err := os.Remove(temp); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		// Simulate debris left when the process was killed mid-write.
		if err := os.WriteFile(temp, []byte("truncated"), 0600); err != nil {
			t.Fatal(err)
		}
		if err := e.store.atomicWrite(filepath.Join(e.config.DataDir, "usage.json"), []byte(`{"version":1}`), 0640); err != nil {
			t.Fatal(err)
		}
	}
	files, _ := os.ReadDir(e.config.DataDir)
	if len(files) != 2 {
		t.Fatal(files)
	}
}

func TestConcurrentReadersSeeWholeSnapshots(t *testing.T) {
	e := testEngine(t)
	obs := observation()
	if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(e.config.DataDir, "usage.json")
	stop := make(chan struct{})
	fail := make(chan error, 1)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
			}
			b, err := os.ReadFile(path)
			if err == nil {
				var s Snapshot
				err = json.Unmarshal(b, &s)
				if err == nil && (s.Version != 2 || s.ObservedAt == nil) {
					err = errSourceInvalid
				}
			}
			if err != nil {
				select {
				case fail <- err:
				default:
				}
				return
			}
		}
	}()
	for i := 0; i < 100; i++ {
		obs.ObservedAt++
		obs.UsedPercent = float64(i)
		if _, err := e.apply(obs, nil, obs.ObservedAt); err != nil {
			t.Error(err)
			break
		}
	}
	close(stop)
	wg.Wait()
	select {
	case err := <-fail:
		t.Fatal("reader saw partial document", err)
	default:
	}
}
