package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"codex-usage-meter/internal/api"
)

func config(args []string, env func(string) (string, bool)) (api.Config, error) {
	get := func(k, def string) string {
		if v, ok := env(k); ok {
			return v
		}
		return def
	}
	f := flag.NewFlagSet("meter-api", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	data := f.String("data-dir", get("METER_DATA_DIR", "/data"), "")
	bind := f.String("bind-address", get("METER_API_BIND_ADDRESS", "127.0.0.1"), "")
	port := f.String("port", get("METER_API_PORT", "8080"), "")
	token := f.String("token", get("METER_API_TOKEN", ""), "")
	timezone := f.String("timezone", get("METER_TIMEZONE", ""), "")
	stale := f.String("stale-seconds", get("METER_STALE_SECONDS", "180"), "")
	bad := errors.New("invalid_configuration")
	if f.Parse(args) != nil || f.NArg() != 0 || net.ParseIP(*bind) == nil {
		return api.Config{}, bad
	}
	p, err := strconv.Atoi(*port)
	if err != nil || p < 1024 || p > 65535 {
		return api.Config{}, bad
	}
	s, err := strconv.ParseInt(*stale, 10, 64)
	if err != nil {
		return api.Config{}, bad
	}
	c := api.Config{DataDir: *data, Address: net.JoinHostPort(*bind, *port), Token: *token, Timezone: *timezone, StaleSeconds: s}
	if _, err := api.NewHandler(c); err != nil {
		return api.Config{}, bad
	}
	return c, nil
}
func run() int {
	c, err := config(os.Args[1:], os.LookupEnv)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid_configuration")
		return 2
	}
	server, err := api.NewServer(c)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid_configuration")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	done := make(chan error, 1)
	go func() { done <- server.ListenAndServe() }()
	select {
	case err := <-done:
		if !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, "server_unavailable")
			return 1
		}
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if server.Shutdown(shutdown) != nil {
			_ = server.Close()
		}
	}
	return 0
}
func main() { os.Exit(run()) }
