package collector

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testConfig(t *testing.T) Config {
	t.Helper()
	root := t.TempDir()
	return Config{AuthFile: filepath.Join(root, "auth", "auth.json"), DataDir: filepath.Join(root, "data"), StateDir: filepath.Join(root, "state"), Zone: time.UTC, Interval: 60 * time.Second, Timeout: 10 * time.Second, StaleSeconds: 180}
}

func TestConfigurationPrecedence(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home")
	file := filepath.Join(root, "settings.env")
	if err := os.WriteFile(file, []byte("# literal file\nMETER_COLLECTION_SECONDS=80\nMETER_STALE_SECONDS=240\nMETER_DATA_DIR=shared\nMETER_API_TOKEN=\nOTHER=$(never-execute)\n"), 0600); err != nil {
		t.Fatal(err)
	}
	c, err := parseConfig([]string{"--env-file", file, "--collection-seconds", "70"}, []string{"METER_COLLECTION_SECONDS=90"}, root, home, func() (string, error) { return "America/Los_Angeles", nil })
	if err != nil || c.Interval != 70*time.Second || c.DataDir != filepath.Join(root, "shared") || c.AuthFile != filepath.Join(home, ".codex/auth.json") || c.Zone.String() != "America/Los_Angeles" {
		t.Fatal(c, err)
	}
	c, err = parseConfig([]string{"--env-file", file}, []string{"METER_COLLECTION_SECONDS=90"}, root, home, func() (string, error) { return "UTC", nil })
	if err != nil || c.Interval != 90*time.Second {
		t.Fatal(c, err)
	}
	c, err = parseConfig([]string{"--env-file", file}, nil, root, home, func() (string, error) { return "UTC", nil })
	if err != nil || c.Interval != 80*time.Second {
		t.Fatal(c, err)
	}
	_, err = parseConfig([]string{"--env-file", file}, []string{"METER_DATA_DIR="}, root, home, func() (string, error) { return "UTC", nil })
	if err == nil {
		t.Fatal("empty inherited required value fell back")
	}
}

func TestConfigurationRejections(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home")
	for _, args := range [][]string{
		{"--collection-seconds", "9"}, {"--collection-seconds", "301"}, {"--collection-seconds", "60.0"},
		{"--collection-seconds", "100"}, {"--stale-seconds", "29"}, {"--stale-seconds", "3601"},
		{"--source-timeout-seconds", "0"}, {"--source-timeout-seconds", "31"}, {"--source-timeout-seconds", "20", "--collection-seconds", "10"},
		{"--timezone", "Local"}, {"--timezone", "No/Such_Zone"}, {"--data-dir", ""}, {"--env-file", ""}, {"--unknown"}, {"extra"},
		{"--data-dir", "same", "--state-dir", "same/child"}, {"--data-dir", "same/child", "--state-dir", "same"},
		{"--data-dir", home}, {"--state-dir", filepath.Join(home, ".codex/nested")},
	} {
		if _, err := parseConfig(args, nil, root, home, func() (string, error) { return "UTC", nil }); err == nil {
			t.Fatal("accepted", args)
		}
	}
	for _, env := range [][]string{{"METER_UNKNOWN=value"}, {"METER_STALE_SECONDS="}, {"METER_AUTH_FILE="}} {
		if _, err := parseConfig(nil, env, root, home, func() (string, error) { return "UTC", nil }); err == nil {
			t.Fatal("accepted", env)
		}
	}
	if _, err := parseConfig(nil, nil, root, home, func() (string, error) { return "", errors.New("no host zone") }); err == nil {
		t.Fatal("silently fell back to UTC")
	}
	for _, body := range []string{"METER_DATA_DIR=a\nMETER_DATA_DIR=b\n", "METER_UNRECOGNIZED=a\n", "METER_DATA_DIR\n", "METER_DATA_DIR =a\n"} {
		file := filepath.Join(root, "bad.env")
		if err := os.WriteFile(file, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := parseConfig([]string{"--env-file", file}, nil, root, home, func() (string, error) { return "UTC", nil }); err == nil {
			t.Fatal("accepted", body)
		}
	}
}

func TestSymlinkBoundaries(t *testing.T) {
	root := t.TempDir()
	home := filepath.Join(root, "home")
	auth := filepath.Join(home, ".codex")
	if err := os.MkdirAll(auth, 0700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "alias")
	if err := os.Symlink(auth, link); err != nil {
		t.Fatal(err)
	}
	if _, err := parseConfig([]string{"--data-dir", filepath.Join(link, "new")}, nil, root, home, func() (string, error) { return "UTC", nil }); err == nil {
		t.Fatal("symlink bypassed isolation")
	}
	dangling := filepath.Join(root, "dangling")
	if err := os.Symlink(filepath.Join(root, "missing"), dangling); err != nil {
		t.Fatal(err)
	}
	if _, err := absolutePath(dangling, root, home); err == nil {
		t.Fatal("dangling symlink accepted")
	}
}
