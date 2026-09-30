//go:build windows

package computer

import (
	"os"
	"os/exec"
)

func configureProcessGroup(_ *exec.Cmd) {}

func terminateProcessGroup(process *os.Process) {
	_ = process.Kill()
}
