//go:build !windows

package tools

import (
	"errors"
	"os/exec"
	"syscall"
	"time"
)

func configureProcessGroup(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func processGroupAlive(pid int) bool {
	err := syscall.Kill(-pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

func terminateProcessGroup(pid int, grace time.Duration) (string, error) {
	if err := syscall.Kill(-pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
		return "term_failed", err
	}
	deadline := time.Now().Add(grace)
	for time.Now().Before(deadline) {
		if !processGroupAlive(pid) {
			return "terminated", nil
		}
		time.Sleep(25 * time.Millisecond)
	}
	if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return "kill_failed", err
	}
	deadline = time.Now().Add(grace)
	for time.Now().Before(deadline) {
		if !processGroupAlive(pid) {
			return "killed", nil
		}
		time.Sleep(25 * time.Millisecond)
	}
	return "kill_sent", nil
}
