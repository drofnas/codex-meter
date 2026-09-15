package collector

import (
	"bytes"
	"encoding/json"
	"io"
	"math"
	"os"
	"unicode/utf8"
)

const maxSourceBytes = 256 * 1024
const maxAuthBytes = 64 * 1024
const maxSnapshotBytes = 4096
const maxEpoch int64 = 4102444800
const week int64 = 604800
const day int64 = 86400

func boundedRead(r io.Reader, limit int64) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errSourceInvalid
	}
	return b, nil
}

func readFile(path string, limit int64) ([]byte, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, errSourceInvalid
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return boundedRead(f, limit)
}

// encoding/json alone accepts duplicate keys and repairs invalid UTF-8. Check
// tokens before decoding, with bounded nesting. Field readers check numeric
// ranges; an overflowing optional count must not invalidate a valid quota.
func strictJSON(b []byte, target any, noExtra bool) error {
	if !utf8.Valid(b) {
		return errSourceInvalid
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	var value func(int) error
	value = func(depth int) error {
		if depth > 64 {
			return errSourceInvalid
		}
		tok, err := d.Token()
		if err != nil {
			return errSourceInvalid
		}
		switch v := tok.(type) {
		case json.Delim:
			switch v {
			case '{':
				seen := map[string]bool{}
				for d.More() {
					key, err := d.Token()
					name, ok := key.(string)
					if err != nil || !ok || seen[name] {
						return errSourceInvalid
					}
					seen[name] = true
					if err := value(depth + 1); err != nil {
						return err
					}
				}
			case '[':
				for d.More() {
					if err := value(depth + 1); err != nil {
						return err
					}
				}
			default:
				return errSourceInvalid
			}
			if _, err := d.Token(); err != nil {
				return errSourceInvalid
			}
		}
		return nil
	}
	if value(0) != nil {
		return errSourceInvalid
	}
	if _, err := d.Token(); err != io.EOF {
		return errSourceInvalid
	}
	d = json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	if noExtra {
		d.DisallowUnknownFields()
	}
	if d.Decode(target) != nil {
		return errSourceInvalid
	}
	return nil
}

func object(v any) map[string]any { m, _ := v.(map[string]any); return m }
func number(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	return f, err == nil && !math.IsNaN(f) && !math.IsInf(f, 0)
}
func integer(v any) (int64, bool) {
	f, ok := number(v)
	// Every integer consumed by this protocol fits exactly in IEEE-754.
	if !ok || math.Trunc(f) != f || math.Abs(f) > 1<<53 {
		return 0, false
	}
	return int64(f), true
}
func validEpoch(n int64) bool { return n >= 1 && n <= maxEpoch }
