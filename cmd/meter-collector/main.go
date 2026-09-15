package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"codex-usage-meter/internal/collector"
)

func main() { os.Exit(run()) }

func run() int {
	c, err := collector.LoadConfig(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid_configuration")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := collector.Run(ctx, c, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}
