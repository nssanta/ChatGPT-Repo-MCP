//go:build !windows

package computer

import (
	"os"
	"os/exec"
	"syscall"
)

func configureProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func terminateProcessGroup(process *os.Process) {
	_ = syscall.Kill(-process.Pid, syscall.SIGTERM)
}
