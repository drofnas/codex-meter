package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

func main() { os.Exit(run()) }

func run() int {
	userDir, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, "home_unavailable")
		return 1
	}
	auth := flag.String("auth-file", filepath.Join(userDir, ".codex", "auth.json"), "native Codex auth file, opened read-only each poll")
	interval := flag.Duration("interval", 60*time.Second, "poll interval (minimum 10s)")
	duration := flag.Duration("duration", 0, "bounded run; zero performs one read")
	flag.Parse()
	if *interval < 10*time.Second || *duration < 0 {
		fmt.Fprintln(os.Stderr, "invalid_arguments")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *duration > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *duration)
		defer cancel()
	}
	client := usageClient()
	defer client.CloseIdleConnections()
	enc := json.NewEncoder(os.Stdout)
	ticker := time.NewTicker(*interval)
	defer ticker.Stop()
	for {
		observed, err := fetch(ctx, client, *auth, time.Now())
		if ctx.Err() != nil {
			return 0
		}
		if err != nil {
			if enc.Encode(map[string]any{"status": "unavailable", "error": err.Error(), "attempted_at": time.Now().Unix()}) != nil {
				return 1
			}
		} else if enc.Encode(observed) != nil {
			return 1
		}
		if *duration == 0 {
			if err != nil {
				return 1
			}
			return 0
		}
		select {
		case <-ctx.Done():
			return 0
		case <-ticker.C:
		}
	}
}
