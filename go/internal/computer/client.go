package computer

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

type HostClient struct {
	mu     sync.Mutex
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader
	seq    int
}

type HostOptions struct {
	SnapshotTTL      time.Duration
	ActionTimeout    time.Duration
	IdleTimeout      time.Duration
	MaxSequenceSteps int
	CaptureMaxEdge   int
	ControlEnabled   bool
}

func NewHostClient(options HostOptions) (*HostClient, error) {
	path, err := locateHostBinary()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(path)
	cmd.Env = append(os.Environ(),
		fmt.Sprintf("COMPUTER_SNAPSHOT_TTL_SECONDS=%d", maxInt64(1, int64(options.SnapshotTTL/time.Second))),
		fmt.Sprintf("COMPUTER_ACTION_TIMEOUT_MS=%d", maxInt64(1, options.ActionTimeout.Milliseconds())),
		fmt.Sprintf("COMPUTER_IDLE_TIMEOUT_SECONDS=%d", maxInt64(30, int64(options.IdleTimeout/time.Second))),
		fmt.Sprintf("COMPUTER_MAX_SEQUENCE_STEPS=%d", maxInt(1, options.MaxSequenceSteps)),
		fmt.Sprintf("COMPUTER_CAPTURE_MAX_EDGE=%d", maxInt(256, options.CaptureMaxEdge)),
		fmt.Sprintf("COMPUTER_CONTROL_ENABLED=%t", options.ControlEnabled),
	)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start computer host: %w", err)
	}
	return &HostClient{cmd: cmd, stdin: stdin, stdout: bufio.NewReaderSize(stdout, 16*1024*1024)}, nil
}

func locateHostBinary() (string, error) {
	if explicit := strings.TrimSpace(os.Getenv("COMPUTER_HOST_PATH")); explicit != "" {
		if info, err := os.Stat(explicit); err == nil && info.Mode().IsRegular() {
			return explicit, nil
		}
		return "", fmt.Errorf("COMPUTER_HOST_PATH does not point to a regular file: %s", explicit)
	}
	name := "chatrepo-computer-host"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	exe, _ := os.Executable()
	candidates := []string{}
	if exe != "" {
		candidates = append(candidates, filepath.Join(filepath.Dir(exe), name))
	}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates,
			filepath.Join(cwd, "bin", name),
			filepath.Join(cwd, name),
			filepath.Join(cwd, "go", name),
		)
	}
	if path, err := exec.LookPath(name); err == nil {
		candidates = append(candidates, path)
	}
	for _, candidate := range candidates {
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return candidate, nil
		}
	}
	return "", fmt.Errorf("computer host binary %q was not found; run make build or set COMPUTER_HOST_PATH", name)
}

func (c *HostClient) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.dropLocked()
}

func (c *HostClient) dropLocked() {
	if c.cmd == nil {
		return
	}
	cmd := c.cmd
	c.cmd = nil
	_ = c.stdin.Close()
	if cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
	_ = cmd.Wait()
}

func (c *HostClient) Call(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cmd == nil || c.cmd.Process == nil {
		return nil, fmt.Errorf("computer host is not running")
	}
	c.seq++
	payload, err := json.Marshal(map[string]any{"id": c.seq, "method": method, "params": params})
	if err != nil {
		return nil, err
	}
	if _, err := c.stdin.Write(append(payload, '\n')); err != nil {
		return nil, fmt.Errorf("computer host write: %w", err)
	}
	type readResult struct {
		data []byte
		err  error
	}
	ch := make(chan readResult, 1)
	go func() {
		line, readErr := c.stdout.ReadBytes('\n')
		ch <- readResult{data: line, err: readErr}
	}()
	var rr readResult
	select {
	case <-ctx.Done():
		c.dropLocked()
		return nil, ctx.Err()
	case <-time.After(3 * time.Minute):
		c.dropLocked()
		return nil, fmt.Errorf("computer host request timed out")
	case rr = <-ch:
	}
	if rr.err != nil {
		return nil, fmt.Errorf("computer host read: %w", rr.err)
	}
	var reply struct {
		ID     int            `json:"id"`
		Result map[string]any `json:"result"`
		Error  *DriverError   `json:"error"`
	}
	if err := json.Unmarshal(rr.data, &reply); err != nil {
		return nil, fmt.Errorf("computer host returned invalid JSON: %w", err)
	}
	if reply.ID != c.seq {
		return nil, fmt.Errorf("computer host reply id %d does not match request %d", reply.ID, c.seq)
	}
	if reply.Error != nil {
		return nil, reply.Error
	}
	if reply.Result == nil {
		return map[string]any{}, nil
	}
	return reply.Result, nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
