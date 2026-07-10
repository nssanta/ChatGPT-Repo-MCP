//go:build windows

package tools

import (
	"os/exec"
	"time"
)

func configureProcessGroup(_ *exec.Cmd) {}

func processGroupAlive(_ int) bool                                 { return false }
func terminateProcessGroup(_ int, _ time.Duration) (string, error) { return "unsupported", nil }
