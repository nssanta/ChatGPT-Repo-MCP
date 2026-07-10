//go:build windows

package tools

import "os/exec"

func configureProcessGroup(_ *exec.Cmd) {}

func terminateProcessGroup(_ int) error { return nil }
