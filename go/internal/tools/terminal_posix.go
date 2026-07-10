//go:build !windows

package tools

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
)

type terminalSession struct {
	mu                            sync.RWMutex
	ID, LogID, CWD, Shell, Status string
	PID, PGID, Cols, Rows         int
	CreatedAt, LastActivity       time.Time
	ExitCode                      *int
	TermSignal                    any
	IdleTimeout                   time.Duration
	process                       *exec.Cmd
	pty                           *os.File
	output                        []byte
	done                          chan struct{}
}

func (e *Engine) executeTerminalTool(ctx context.Context, name string, args map[string]any) map[string]any {
	switch name {
	case "start_terminal_session":
		return e.startTerminal(args)
	case "read_terminal_session":
		return e.readTerminal(stringArg(args, "session_id", ""), intArg(args, "cursor", 0), intArg(args, "max_bytes", 65536), intArg(args, "wait_ms", 1000))
	case "write_terminal_session":
		return e.writeTerminal(stringArg(args, "session_id", ""), stringArg(args, "data", ""), stringArg(args, "encoding", "utf8"))
	case "resize_terminal_session":
		return e.resizeTerminal(stringArg(args, "session_id", ""), intArg(args, "cols", 120), intArg(args, "rows", 40))
	case "close_terminal_session":
		return e.closeTerminal(stringArg(args, "session_id", ""), stringArg(args, "signal", "SIGTERM"), time.Duration(intArg(args, "grace_ms", 5000))*time.Millisecond, boolArg(args, "force", false))
	case "list_terminal_sessions":
		return e.listTerminals(boolArg(args, "include_finished", true))
	default:
		return failure("unknown_terminal_tool", name)
	}
}

func (e *Engine) terminalResult(session *terminalSession) map[string]any {
	session.mu.RLock()
	defer session.mu.RUnlock()
	var exit any
	if session.ExitCode != nil {
		exit = *session.ExitCode
	}
	return map[string]any{"ok": true, "session_id": session.ID, "status": session.Status, "pid": session.PID, "pgid": session.PGID, "cwd": e.perimeter.Display(session.CWD), "shell": session.Shell, "cols": session.Cols, "rows": session.Rows, "created_at": session.CreatedAt.UTC().Format(time.RFC3339Nano), "last_activity_at": session.LastActivity.UTC().Format(time.RFC3339Nano), "exit_code": exit, "term_signal": session.TermSignal, "log_id": session.LogID, "next_cursor": len(session.output)}
}

func (e *Engine) startTerminal(args map[string]any) map[string]any {
	e.terminalsMu.RLock()
	active := 0
	for _, item := range e.terminals {
		item.mu.RLock()
		if item.Status == "starting" || item.Status == "running" || item.Status == "closing" {
			active++
		}
		item.mu.RUnlock()
	}
	e.terminalsMu.RUnlock()
	if active >= e.settings.MaxTerminalSessions {
		return failure("terminal_limit", "maximum terminal sessions reached")
	}
	directory, err := e.resolveCommandCWD(stringArg(args, "cwd", ""))
	if err != nil {
		return withError("invalid_cwd", err)
	}
	shell := stringArg(args, "shell", "")
	if shell == "" {
		shell = bashBinary()
	}
	if info, err := os.Stat(shell); err != nil || info.IsDir() || info.Mode()&0o111 == 0 {
		return failure("invalid_shell", "shell is not executable")
	}
	cols, rows := intArg(args, "cols", 120), intArg(args, "rows", 40)
	argv := []string{"-i"}
	if command := optionalString(args, "command"); command != nil && *command != "" {
		argv = []string{"-lc", *command}
	}
	command := exec.Command(shell, argv...)
	command.Dir = directory
	command.Env = e.commandEnvironment(nil)
	ptmx, err := pty.StartWithSize(command, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if err != nil {
		return withError("terminal_spawn_error", err)
	}
	now := time.Now().UTC()
	session := &terminalSession{ID: randomID(), LogID: randomID(), CWD: directory, Shell: shell, Status: "running", PID: command.Process.Pid, PGID: command.Process.Pid, Cols: cols, Rows: rows, CreatedAt: now, LastActivity: now, IdleTimeout: time.Duration(intArg(args, "idle_timeout_ms", 1800000)) * time.Millisecond, process: command, pty: ptmx, done: make(chan struct{})}
	e.terminalsMu.Lock()
	e.terminals[session.ID] = session
	e.terminalsMu.Unlock()
	e.persistTerminal(session)
	go e.readTerminalOutput(session)
	go e.watchTerminalIdle(session)
	result := e.terminalResult(session)
	result["next_cursor"] = 0
	return result
}

func (e *Engine) persistTerminal(session *terminalSession) {
	_ = os.MkdirAll(filepath.Join(e.settings.CommandJobsDir, "logs"), 0700)
	metadata, _ := json.Marshal(e.terminalResult(session))
	_ = os.WriteFile(filepath.Join(e.settings.CommandJobsDir, "logs", session.LogID+".json"), append(metadata, '\n'), 0600)
}

func (e *Engine) readTerminalOutput(session *terminalSession) {
	defer close(session.done)
	path := filepath.Join(e.settings.CommandJobsDir, "logs", session.LogID+".combined")
	_ = os.MkdirAll(filepath.Dir(path), 0700)
	file, _ := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if file != nil {
		defer file.Close()
	}
	buffer := make([]byte, 65536)
	for {
		count, err := session.pty.Read(buffer)
		if count > 0 {
			text := []byte(redact(string(buffer[:count])))
			session.mu.Lock()
			session.output = append(session.output, text...)
			session.LastActivity = time.Now().UTC()
			session.mu.Unlock()
			if file != nil {
				_, _ = file.Write(text)
			}
		}
		if err != nil {
			break
		}
	}
	err := session.process.Wait()
	code := 0
	if err != nil {
		code = -1
		if exit, ok := err.(*exec.ExitError); ok {
			code = exit.ExitCode()
		}
	}
	session.mu.Lock()
	session.ExitCode = &code
	if session.Status != "closed" {
		if code == 0 {
			session.Status = "exited"
		} else {
			session.Status = "failed"
		}
	}
	session.LastActivity = time.Now().UTC()
	session.mu.Unlock()
	e.persistTerminal(session)
	_ = session.pty.Close()
}

func (e *Engine) terminal(id string) (*terminalSession, error) {
	e.terminalsMu.RLock()
	item := e.terminals[id]
	e.terminalsMu.RUnlock()
	if item == nil {
		return nil, fmt.Errorf("terminal session not found: %s", id)
	}
	return item, nil
}

func (e *Engine) readTerminal(id string, cursor, maximum, waitMS int) map[string]any {
	session, err := e.terminal(id)
	if err != nil {
		return withError("terminal_not_found", err)
	}
	if cursor < 0 {
		cursor = 0
	}
	if maximum < 1 {
		maximum = 1
	}
	if maximum > 65536 {
		maximum = 65536
	}
	deadline := time.Now().Add(time.Duration(min(max(waitMS, 0), 30000)) * time.Millisecond)
	for {
		session.mu.RLock()
		size, status := len(session.output), session.Status
		session.mu.RUnlock()
		if size > cursor || status != "running" || time.Now().After(deadline) {
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	session.mu.RLock()
	defer session.mu.RUnlock()
	end := min(cursor+maximum, len(session.output))
	if cursor > len(session.output) {
		cursor = len(session.output)
		end = cursor
	}
	data := string(session.output[cursor:end])
	eof := session.Status != "running" && end == len(session.output)
	return map[string]any{"ok": true, "session_id": id, "data": data, "next_cursor": end, "eof": eof, "truncated": end < len(session.output)}
}

func (e *Engine) writeTerminal(id, data, encoding string) map[string]any {
	session, err := e.terminal(id)
	if err != nil {
		return withError("terminal_not_found", err)
	}
	payload := []byte(data)
	if encoding == "base64" {
		payload, err = base64.StdEncoding.DecodeString(data)
	} else if encoding != "utf8" {
		return failure("invalid_encoding", "encoding must be utf8 or base64")
	}
	if err != nil {
		return withError("invalid_base64", err)
	}
	if len(payload) > 65536 {
		return failure("terminal_write_too_large", "terminal write exceeds 65536 bytes")
	}
	count, err := session.pty.Write(payload)
	if err != nil {
		return withError("terminal_write_failed", err)
	}
	session.mu.Lock()
	session.LastActivity = time.Now().UTC()
	session.mu.Unlock()
	return map[string]any{"ok": true, "session_id": id, "bytes_written": count}
}

func (e *Engine) resizeTerminal(id string, cols, rows int) map[string]any {
	session, err := e.terminal(id)
	if err != nil {
		return withError("terminal_not_found", err)
	}
	if cols < 1 || rows < 1 || cols > 1000 || rows > 1000 {
		return failure("invalid_terminal_size", "cols and rows must be between 1 and 1000")
	}
	if err = pty.Setsize(session.pty, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)}); err != nil {
		return withError("terminal_resize_failed", err)
	}
	session.mu.Lock()
	session.Cols, session.Rows, session.LastActivity = cols, rows, time.Now().UTC()
	session.mu.Unlock()
	return e.terminalResult(session)
}

func (e *Engine) closeTerminal(id, signalName string, grace time.Duration, force bool) map[string]any {
	session, err := e.terminal(id)
	if err != nil {
		return withError("terminal_not_found", err)
	}
	session.mu.Lock()
	if session.Status == "closed" || session.Status == "exited" || session.Status == "failed" {
		session.mu.Unlock()
		result := e.terminalResult(session)
		result["closed"] = false
		return result
	}
	session.Status = "closing"
	session.TermSignal = signalName
	session.mu.Unlock()
	if force {
		_ = syscall.Kill(-session.PGID, syscall.SIGKILL)
	} else if signalName == "SIGTERM" {
		_, _ = terminateProcessGroup(session.PGID, grace)
	} else {
		signals := map[string]syscall.Signal{"SIGINT": syscall.SIGINT, "SIGHUP": syscall.SIGHUP, "SIGKILL": syscall.SIGKILL}
		value, ok := signals[signalName]
		if !ok {
			return failure("invalid_signal", "unsupported terminal signal")
		}
		_ = syscall.Kill(-session.PGID, value)
	}
	select {
	case <-session.done:
	case <-time.After(grace):
		_, _ = terminateProcessGroup(session.PGID, e.settings.KillGrace)
	}
	session.mu.Lock()
	session.Status = "closed"
	session.LastActivity = time.Now().UTC()
	session.mu.Unlock()
	e.persistTerminal(session)
	result := e.terminalResult(session)
	result["closed"] = true
	return result
}

func (e *Engine) watchTerminalIdle(session *terminalSession) {
	ticker := time.NewTicker(min(session.IdleTimeout/4, 5*time.Second))
	defer ticker.Stop()
	for range ticker.C {
		session.mu.RLock()
		expired := session.Status == "running" && time.Since(session.LastActivity) >= session.IdleTimeout
		finished := session.Status != "running"
		session.mu.RUnlock()
		if finished {
			return
		}
		if expired {
			e.closeTerminal(session.ID, "SIGTERM", e.settings.KillGrace, false)
			return
		}
	}
}

func (e *Engine) listTerminals(includeFinished bool) map[string]any {
	e.terminalsMu.RLock()
	items := make([]*terminalSession, 0, len(e.terminals))
	for _, item := range e.terminals {
		items = append(items, item)
	}
	e.terminalsMu.RUnlock()
	sort.Slice(items, func(i, j int) bool { return items[i].CreatedAt.After(items[j].CreatedAt) })
	results := []map[string]any{}
	for _, item := range items {
		item.mu.RLock()
		active := item.Status == "starting" || item.Status == "running" || item.Status == "closing"
		item.mu.RUnlock()
		if includeFinished || active {
			results = append(results, e.terminalResult(item))
		}
	}
	return map[string]any{"ok": true, "sessions": results, "count": len(results)}
}
