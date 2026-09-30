package computer

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

type DriverError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *DriverError) Error() string {
	if e.Code == "" {
		return e.Message
	}
	return e.Code + ": " + e.Message
}

type protocolRequest struct {
	ID     int            `json:"id"`
	Method string         `json:"method"`
	Params map[string]any `json:"params"`
}

type protocolReply struct {
	ID     int             `json:"id"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  any             `json:"error,omitempty"`
	Fatal  bool            `json:"fatal,omitempty"`
}

type lineProcess struct {
	mu      sync.Mutex
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	stdout  *bufio.Reader
	seq     int
	label   string
	timeout time.Duration
}

func newDriverProcess(assets RuntimeAssets, timeout time.Duration) (*lineProcess, error) {
	command, args, err := driverCommand(assets)
	if err != nil {
		return nil, err
	}
	return startLineProcess(command, args, "computer driver", timeout)
}

func newPortalProcess(assets RuntimeAssets, timeout time.Duration) (*lineProcess, error) {
	if assets.Portal == "" {
		return nil, errors.New("Wayland portal helper is not bundled")
	}
	token := filepath.Join(assets.Directory, "wayland-portal.token")
	return startLineProcess("/usr/bin/python3", []string{"-u", assets.Portal, "--token-file", token}, "Wayland portal", timeout)
}

func startLineProcess(command string, args []string, label string, timeout time.Duration) (*lineProcess, error) {
	cmd := exec.Command(command, args...)
	cmd.Env = os.Environ()
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = io.Discard
	configureProcessGroup(cmd)
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start %s: %w", label, err)
	}
	return &lineProcess{cmd: cmd, stdin: stdin, stdout: bufio.NewReaderSize(stdout, 4*1024*1024), label: label, timeout: timeout}, nil
}

func driverCommand(assets RuntimeAssets) (string, []string, error) {
	switch runtime.GOOS {
	case "linux":
		if _, err := os.Stat("/usr/bin/python3"); err != nil {
			return "", nil, fmt.Errorf("computer use on Linux requires /usr/bin/python3: %w", err)
		}
		return "/usr/bin/python3", []string{"-u", assets.Driver}, nil
	case "windows":
		literal := strings.ReplaceAll(assets.Driver, "'", "''")
		script := "& ([ScriptBlock]::Create([IO.File]::ReadAllText('" + literal + "', [Text.Encoding]::UTF8)))"
		return "powershell.exe", []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script}, nil
	case "darwin":
		if executable, err := os.Executable(); err == nil {
			prebuilt := filepath.Join(filepath.Dir(executable), "chatrepo-computer-driver")
			if info, statErr := os.Stat(prebuilt); statErr == nil && info.Mode().IsRegular() {
				return prebuilt, nil, nil
			}
		}
		binary := filepath.Join(assets.Directory, "computer_driver")
		if info, err := os.Stat(binary); err == nil && info.Mode().IsRegular() {
			return binary, nil, nil
		}
		if err := compileSwiftDriver(assets.Driver, binary); err != nil {
			return "", nil, err
		}
		return binary, nil, nil
	default:
		return "", nil, fmt.Errorf("unsupported_os: %s", runtime.GOOS)
	}
}

func compileSwiftDriver(source, target string) error {
	swiftc, err := exec.LookPath("swiftc")
	if err != nil {
		return fmt.Errorf("macOS computer helper is not prebuilt and swiftc is unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, swiftc, source, "-O", "-o", target)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("compile macOS computer helper: %w: %s", err, strings.TrimSpace(string(output)))
	}
	if err := os.Chmod(target, 0o700); err != nil {
		return err
	}
	return nil
}

func (p *lineProcess) close() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.terminateLocked()
}

func (p *lineProcess) running() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.cmd != nil && p.cmd.Process != nil
}

func (p *lineProcess) terminateLocked() {
	if p.cmd == nil {
		return
	}
	cmd := p.cmd
	p.cmd = nil
	_ = p.stdin.Close()
	if cmd.Process != nil {
		terminateProcessGroup(cmd.Process)
	}
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(1500 * time.Millisecond):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	}
}

func (p *lineProcess) call(ctx context.Context, method string, params map[string]any, out any) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.cmd == nil || p.cmd.Process == nil {
		return fmt.Errorf("%s is not running", p.label)
	}
	p.seq++
	request := protocolRequest{ID: p.seq, Method: method, Params: params}
	line, err := json.Marshal(request)
	if err != nil {
		return err
	}
	if _, err := p.stdin.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("%s write: %w", p.label, err)
	}
	timeout := p.timeout
	if deadline, ok := ctx.Deadline(); ok {
		if remaining := time.Until(deadline); remaining > 0 && (timeout <= 0 || remaining < timeout) {
			timeout = remaining
		}
	}
	type readResult struct {
		line []byte
		err  error
	}
	readCh := make(chan readResult, 1)
	go func() {
		replyLine, readErr := p.stdout.ReadBytes('\n')
		readCh <- readResult{line: replyLine, err: readErr}
	}()
	var rr readResult
	if timeout <= 0 {
		select {
		case <-ctx.Done():
			p.terminateLocked()
			return ctx.Err()
		case rr = <-readCh:
		}
	} else {
		timer := time.NewTimer(timeout)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			p.terminateLocked()
			return ctx.Err()
		case <-timer.C:
			p.terminateLocked()
			return &DriverError{Code: "timeout", Message: p.label + " timed out"}
		case rr = <-readCh:
		}
	}
	if rr.err != nil {
		p.terminateLocked()
		return fmt.Errorf("%s read: %w", p.label, rr.err)
	}
	var reply protocolReply
	if err := json.Unmarshal(rr.line, &reply); err != nil {
		p.terminateLocked()
		return fmt.Errorf("%s returned invalid JSON: %w", p.label, err)
	}
	if reply.ID != request.ID {
		p.terminateLocked()
		return fmt.Errorf("%s returned reply id %d, expected %d", p.label, reply.ID, request.ID)
	}
	if reply.Error != nil {
		code, message := decodeProtocolError(reply.Error)
		if reply.Fatal {
			p.terminateLocked()
		}
		return &DriverError{Code: code, Message: message}
	}
	if out == nil || len(reply.Result) == 0 {
		return nil
	}
	if err := json.Unmarshal(reply.Result, out); err != nil {
		return fmt.Errorf("decode %s result for %s: %w", p.label, method, err)
	}
	return nil
}

func decodeProtocolError(value any) (string, string) {
	switch typed := value.(type) {
	case string:
		return "failed", typed
	case map[string]any:
		code := fmt.Sprint(typed["code"])
		message := fmt.Sprint(typed["message"])
		if code == "" || code == "<nil>" {
			code = "failed"
		}
		if message == "" || message == "<nil>" {
			message = "computer driver request failed"
		}
		return code, message
	default:
		data, _ := json.Marshal(value)
		return "failed", strings.TrimSpace(string(data))
	}
}

func isWayland() bool {
	return strings.EqualFold(os.Getenv("XDG_SESSION_TYPE"), "wayland") || os.Getenv("WAYLAND_DISPLAY") != ""
}

func intValue(value any, fallback int) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		if parsed, err := strconv.Atoi(typed.String()); err == nil {
			return parsed
		}
	}
	return fallback
}
