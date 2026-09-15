package collector

import (
	"bufio"
	"errors"
	"flag"
	"io"
	"maps"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	_ "time/tzdata" // The native publisher owns IANA rendering, even without host zone files.
)

var errConfig = errors.New("invalid_configuration")

type Config struct {
	AuthFile, DataDir, StateDir string
	Zone                        *time.Location
	Interval, Timeout           time.Duration
	StaleSeconds                int64
	Once                        bool
}

var defaults = map[string]string{
	"METER_AUTH_FILE": "~/.codex/auth.json", "METER_DATA_DIR": ".local/data",
	"METER_STATE_DIR": ".local/state", "METER_TIMEZONE": "",
	"METER_COLLECTION_SECONDS": "60", "METER_STALE_SECONDS": "180",
	"METER_SOURCE_TIMEOUT_SECONDS": "10",
	// Known shared settings are accepted but never consumed by the collector.
	"METER_API_BIND_ADDRESS": "127.0.0.1", "METER_API_PORT": "8080", "METER_API_TOKEN": "",
}

func LoadConfig(args []string) (Config, error) {
	cwd, err := os.Getwd()
	if err != nil {
		return Config{}, errConfig
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return Config{}, errConfig
	}
	return parseConfig(args, os.Environ(), cwd, home, macTimezone)
}

func parseConfig(args, env []string, cwd, home string, discover func() (string, error)) (Config, error) {
	f := flag.NewFlagSet("meter-collector", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	envFile := f.String("env-file", "", "literal settings file (opt-in)")
	once := f.Bool("once", false, "one collection attempt")
	options := map[string]*string{}
	for _, pair := range [][2]string{
		{"auth-file", "METER_AUTH_FILE"}, {"data-dir", "METER_DATA_DIR"},
		{"state-dir", "METER_STATE_DIR"}, {"timezone", "METER_TIMEZONE"},
		{"collection-seconds", "METER_COLLECTION_SECONDS"}, {"stale-seconds", "METER_STALE_SECONDS"},
		{"source-timeout-seconds", "METER_SOURCE_TIMEOUT_SECONDS"},
	} {
		options[pair[0]] = f.String(pair[0], "", pair[1])
	}
	if f.Parse(args) != nil || f.NArg() != 0 {
		return Config{}, errConfig
	}
	values := maps.Clone(defaults)
	base := cwd
	if *envFile != "" {
		path, err := absolutePath(*envFile, cwd, home)
		if err != nil {
			return Config{}, errConfig
		}
		b, err := readFile(path, maxAuthBytes)
		if err != nil {
			return Config{}, errConfig
		}
		seen := map[string]bool{}
		scanner := bufio.NewScanner(strings.NewReader(string(b)))
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			key, value, ok := strings.Cut(line, "=")
			if !ok || key == "" || strings.TrimSpace(key) != key || seen[key] {
				return Config{}, errConfig
			}
			seen[key] = true
			if _, known := defaults[key]; strings.HasPrefix(key, "METER_") && !known {
				return Config{}, errConfig
			}
			values[key] = value
		}
		if scanner.Err() != nil {
			return Config{}, errConfig
		}
		base = filepath.Dir(path)
	}
	for _, item := range env {
		key, value, _ := strings.Cut(item, "=")
		if !strings.HasPrefix(key, "METER_") {
			continue
		}
		if _, known := defaults[key]; !known {
			return Config{}, errConfig
		}
		values[key] = value
	}
	f.Visit(func(o *flag.Flag) {
		if p, ok := options[o.Name]; ok {
			values[o.Usage] = *p
		}
	})
	if *envFile == "" {
		explicitEmpty := false
		f.Visit(func(o *flag.Flag) {
			if o.Name == "env-file" {
				explicitEmpty = true
			}
		})
		if explicitEmpty {
			return Config{}, errConfig
		}
	}
	var c Config
	var err error
	c.AuthFile, err = absolutePath(values["METER_AUTH_FILE"], base, home)
	if err != nil {
		return Config{}, errConfig
	}
	c.DataDir, err = absolutePath(values["METER_DATA_DIR"], base, home)
	if err != nil {
		return Config{}, errConfig
	}
	c.StateDir, err = absolutePath(values["METER_STATE_DIR"], base, home)
	if err != nil {
		return Config{}, errConfig
	}
	if overlaps(c.DataDir, c.StateDir) || overlaps(c.DataDir, filepath.Dir(c.AuthFile)) || overlaps(c.StateDir, filepath.Dir(c.AuthFile)) {
		return Config{}, errConfig
	}
	seconds, err := boundedInt(values["METER_COLLECTION_SECONDS"], 10, 300)
	if err != nil {
		return Config{}, errConfig
	}
	stale, err := boundedInt(values["METER_STALE_SECONDS"], 30, 3600)
	if err != nil || stale < 2*seconds {
		return Config{}, errConfig
	}
	timeout, err := boundedInt(values["METER_SOURCE_TIMEOUT_SECONDS"], 1, 30)
	if err != nil || timeout > seconds {
		return Config{}, errConfig
	}
	zone := values["METER_TIMEZONE"]
	if zone == "" {
		zone, err = discover()
		if err != nil {
			return Config{}, errConfig
		}
	}
	if zone == "" || len(zone) > 64 || zone == "Local" || strings.ContainsAny(zone, "\\\x00") {
		return Config{}, errConfig
	}
	c.Zone, err = time.LoadLocation(zone)
	if err != nil {
		return Config{}, errConfig
	}
	c.Interval, c.Timeout, c.StaleSeconds, c.Once = time.Duration(seconds)*time.Second, time.Duration(timeout)*time.Second, stale, *once
	return c, nil
}

func boundedInt(s string, low, high int64) (int64, error) {
	if s == "" || strings.Trim(s, "0123456789") != "" {
		return 0, errConfig
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil || n < low || n > high {
		return 0, errConfig
	}
	return n, nil
}

// Resolve existing ancestors too, so absent leaf directories cannot hide overlap.
func absolutePath(path, base, home string) (string, error) {
	if path == "" || strings.ContainsRune(path, '\x00') {
		return "", errConfig
	}
	if strings.HasPrefix(path, "~/") {
		path = filepath.Join(home, path[2:])
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(base, path)
	}
	path = filepath.Clean(path)
	resolved, err := filepath.EvalSymlinks(path)
	if err == nil {
		return resolved, nil
	}
	if !os.IsNotExist(err) {
		return "", errConfig
	}
	// A dangling symlink is not an absent ordinary path.
	if info, statErr := os.Lstat(path); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return "", errConfig
	}
	parent, err := absolutePath(filepath.Dir(path), base, home)
	if err != nil {
		return "", err
	}
	return filepath.Join(parent, filepath.Base(path)), nil
}

func overlaps(a, b string) bool {
	return a == b || strings.HasPrefix(a, b+string(os.PathSeparator)) || strings.HasPrefix(b, a+string(os.PathSeparator)) || a == "/" || b == "/"
}

func macTimezone() (string, error) {
	path, err := filepath.EvalSymlinks("/etc/localtime")
	if err != nil {
		return "", errConfig
	}
	_, zone, ok := strings.Cut(path, "/zoneinfo/")
	if !ok || zone == "" {
		return "", errConfig
	}
	return zone, nil
}
