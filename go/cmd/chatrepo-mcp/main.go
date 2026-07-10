package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/app"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
)

func main() {
	settings, err := config.Load()
	if err != nil {
		slog.Error("invalid configuration", "error", err)
		os.Exit(2)
	}
	application, err := app.New(settings)
	if err != nil {
		slog.Error("initialize server", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := application.Run(ctx); err != nil {
		_, _ = fmt.Fprintf(os.Stderr, "chatrepo-mcp: %v\n", err)
		os.Exit(1)
	}
}
