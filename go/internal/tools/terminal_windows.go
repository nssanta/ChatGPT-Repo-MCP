//go:build windows

package tools

import (
	"context"
	"time"
)

type terminalSession struct{}

func (e *Engine) executeTerminalTool(_ context.Context, name string, _ map[string]any) map[string]any {
	return failure("pty_unavailable", "POSIX PTY is unavailable on Windows: "+name)
}

func (e *Engine) closeTerminal(_ string, _ string, _ time.Duration, _ bool) map[string]any {
	return failure("pty_unavailable", "POSIX PTY is unavailable on Windows")
}
