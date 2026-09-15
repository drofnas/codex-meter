package main

import (
	"strings"
	"testing"
)

func TestConfig(t *testing.T) {
	env := map[string]string{"METER_API_TOKEN": strings.Repeat("a", 64), "METER_TIMEZONE": "UTC"}
	get := func(k string) (string, bool) { v, ok := env[k]; return v, ok }
	c, e := config(nil, get)
	if e != nil || c.Address != "127.0.0.1:8080" || c.DataDir != "/data" {
		t.Fatal("defaults", e)
	}
	for _, args := range [][]string{{"--token", ""}, {"--timezone", ""}, {"--timezone", "Local"}, {"--port", "80"}, {"--port", "65536"}, {"--bind-address", "localhost"}, {"--stale-seconds", "29"}, {"--stale-seconds", "3601"}, {"--data-dir", ""}, {"extra"}} {
		if _, e := config(args, get); e == nil {
			t.Fatalf("accepted %q", args)
		}
	}
	env["METER_API_PORT"] = ""
	if _, e := config(nil, get); e == nil {
		t.Fatal("empty environment")
	}
	c, e = config([]string{"--port", "9000"}, get)
	if e != nil || c.Address != "127.0.0.1:9000" {
		t.Fatal("CLI precedence", e)
	}
}
