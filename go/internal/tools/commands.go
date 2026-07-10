package tools

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

type job struct {
	mu             sync.RWMutex
	ID             string
	LogID          string
	Command        string
	CWD            string
	Status         string
	StartedAt      time.Time
	FinishedAt     time.Time
	ExitCode       int
	Stdout         string
	Stderr         string
	TimedOut       bool
	ConcurrencyKey string
	cancel         context.CancelFunc
	pid            int
	done           chan struct{}
}

type commandRequest struct {
	Command        string
	Timeout        time.Duration
	CWD            string
	Env            map[string]string
	MaxOutput      int
	TailLines      int
	Confirmed      bool
	ParseKind      string
	PolicyExempt   bool
	ConcurrencyKey string
}

var (
	environmentName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	secretPatterns  = []*regexp.Regexp{
		regexp.MustCompile(`(?i)(token|secret|password|api[_-]?key)=([^\s]+)`),
		regexp.MustCompile(`gh[pousr]_[A-Za-z0-9_]{20,}`),
		regexp.MustCompile(`npm_[A-Za-z0-9_]{20,}`),
		regexp.MustCompile(`(?i)Bearer\s+[A-Za-z0-9._-]+`),
		regexp.MustCompile(`https?://[^\s/@]+:[^\s/@]+@[^\s]+`),
		regexp.MustCompile(`git@[^:\s]+:[^\s]+`),
	}
	allowlistedCommands = []struct {
		command     string
		allowSuffix bool
	}{
		{"git status --short", false}, {"git status --short --branch", false},
		{"git diff --check", false}, {"git diff", false}, {"git diff --name-only", false},
		{"git log --oneline -n 20", false}, {"npm --version", false}, {"node --version", false},
		{"npx --version", false}, {"npx vitest run", true},
	}
)

func (e *Engine) executeCommandTool(ctx context.Context, name string, args map[string]any) map[string]any {
	switch name {
	case "run_command":
		return e.runCommand(ctx, e.commandRequestFromArgs(args, false))
	case "run_commands":
		return e.runCommands(ctx, args)
	case "run_test_preset":
		return e.runTestPreset(ctx, args)
	case "list_test_presets":
		path := stringArg(args, "path", "")
		if path != "" {
			return map[string]any{"ok": true, "path": path, "presets": e.presetsFor(path)}
		}
		repos := e.workspaceEntries(ctx)
		resolved := make(map[string]any)
		for _, repo := range repos {
			repoPath := fmt.Sprint(repo["path"])
			resolved[repoPath] = e.presetsFor(firstNonEmpty(repoPath, "."))
		}
		return map[string]any{"ok": true, "presets": e.presetsFor("."), "repos": resolved}
	case "command_policy_check":
		normalized, kind, err := e.checkCommandPolicy(stringArg(args, "command", ""), false, false)
		if err != nil {
			return map[string]any{"ok": false, "allowed": false, "error_kind": kind, "reason": err.Error(), "policy_mode": e.settings.CommandPolicyMode}
		}
		return map[string]any{"ok": true, "allowed": true, "normalized": normalized, "policy_mode": e.settings.CommandPolicyMode}
	case "start_command_job":
		return e.startJob(ctx, args)
	case "get_command_job":
		return e.getJob(stringArg(args, "job_id", ""), intArg(args, "tail_lines", 200), false)
	case "get_job_status":
		return e.getJob(stringArg(args, "job_id", ""), 0, true)
	case "cancel_command_job":
		return e.cancelJob(stringArg(args, "job_id", ""))
	case "get_command_log":
		return e.getCommandLog(args)
	case "summarize_command_log":
		return e.summarizeCommandLog(stringArg(args, "log_id", ""), stringArg(args, "parser", "auto"))
	case "scan_new_policy_violations":
		return e.scanPolicy(ctx, args)
	case "run_quality_gate":
		return e.runQualityGate(ctx, args)
	case "quality_gate_and_commit":
		return e.qualityGateAndCommit(ctx, args)
	case "git_worktree_guard":
		return e.worktreeGuard(ctx, args)
	case "git_commit":
		return e.gitCommit(ctx, args)
	default:
		return failure("unknown_command_tool", name)
	}
}

func (e *Engine) commandRequestFromArgs(args map[string]any, exempt bool) commandRequest {
	timeout := time.Duration(intArg(args, "timeout_ms", int(e.settings.CommandTimeout/time.Millisecond))) * time.Millisecond
	if timeout <= 0 || timeout > e.settings.CommandTimeout {
		timeout = e.settings.CommandTimeout
	}
	environment := make(map[string]string)
	if raw, ok := args["env"].(map[string]any); ok {
		for key, value := range raw {
			environment[key] = fmt.Sprint(value)
		}
	}
	maximum := intArg(args, "max_output_chars", e.settings.MaxCommandOutputChars)
	if maximum <= 0 || maximum > e.settings.MaxCommandOutputChars {
		maximum = e.settings.MaxCommandOutputChars
	}
	return commandRequest{
		Command: stringArg(args, "command", ""), Timeout: timeout, CWD: stringArg(args, "cwd", ""),
		Env: environment, MaxOutput: maximum, TailLines: intArg(args, "tail_lines", 200),
		Confirmed: e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)),
		ParseKind: stringArg(args, "parse_kind", "auto"), PolicyExempt: exempt,
		ConcurrencyKey: stringArg(args, "concurrency_key", ""),
	}
}

func (e *Engine) runCommand(ctx context.Context, request commandRequest) map[string]any {
	started := time.Now()
	normalized, kind, err := e.checkCommandPolicy(request.Command, request.Confirmed, request.PolicyExempt)
	if err != nil {
		key := "command_not_allowed"
		if kind == "confirmation_required" {
			key = kind
		}
		return map[string]any{"ok": false, "error_kind": key, "error": err.Error(), "command": request.Command}
	}
	directory, err := e.resolveCommandCWD(request.CWD)
	if err != nil {
		return withError("invalid_cwd", err)
	}
	result := e.runShell(ctx, normalized, directory, request.Timeout, request.Env)
	result.Stdout = redact(result.Stdout)
	result.Stderr = redact(result.Stderr)
	fullStdout, fullStderr := result.Stdout, result.Stderr
	result.Stdout, _ = capText(result.Stdout, request.MaxOutput)
	result.Stderr, _ = capText(result.Stderr, request.MaxOutput)
	if request.TailLines >= 0 {
		result.Stdout = tailText(result.Stdout, request.TailLines)
		result.Stderr = tailText(result.Stderr, request.TailLines)
	}
	logID := randomID()
	e.writeCommandLogs(logID, normalized, directory, fullStdout, fullStderr, result.ExitCode)
	return map[string]any{
		"ok": result.ExitCode == 0 && !result.TimedOut, "command": normalized,
		"cwd": e.perimeter.Display(directory), "exit_code": result.ExitCode,
		"stdout": result.Stdout, "stderr": result.Stderr, "timed_out": result.TimedOut,
		"duration_ms": time.Since(started).Milliseconds(), "log_id": logID,
		"summary": parseCommandSummary(normalized, fullStdout, fullStderr, request.ParseKind),
	}
}

func (e *Engine) runShell(parent context.Context, command, directory string, timeout time.Duration, overrides map[string]string) processResult {
	bash := bashBinary()
	if bash == "" {
		return processResult{ExitCode: -1, Stderr: "bash is required; install Git Bash on Windows"}
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	commandText := command
	if strings.TrimSpace(e.settings.CommandShellPrelude) != "" {
		commandText = e.settings.CommandShellPrelude + "\n" + command
	}
	process := exec.CommandContext(ctx, bash, "-lc", commandText)
	process.Dir = directory
	process.Env = os.Environ()
	for key, value := range overrides {
		if !environmentName.MatchString(key) {
			return processResult{ExitCode: -1, Stderr: fmt.Sprintf("invalid environment variable name: %s", key)}
		}
		process.Env = append(process.Env, key+"="+value)
	}
	var stdout strings.Builder
	var stderr strings.Builder
	process.Stdout = &stdout
	process.Stderr = &stderr
	err := process.Run()
	exitCode := 0
	if err != nil {
		exitCode = -1
		if exit, ok := err.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
	}
	return processResult{ExitCode: exitCode, Stdout: stdout.String(), Stderr: stderr.String(), TimedOut: ctx.Err() == context.DeadlineExceeded}
}

func (e *Engine) resolveCommandCWD(cwd string) (string, error) {
	if cwd == "" {
		return e.settings.ProjectRoot, nil
	}
	return e.resolveDirectory(cwd, true)
}

func (e *Engine) checkCommandPolicy(command string, confirmed, exempt bool) (string, string, error) {
	command = strings.TrimSpace(command)
	if command == "" {
		return "", "command_not_allowed", fmt.Errorf("command must not be empty")
	}
	mode := e.settings.CommandPolicyMode
	if mode == "unrestricted" || mode == "full_repo" {
		return command, "", nil
	}
	segments := splitShellSegments(command)
	for _, segment := range segments {
		tokens := strings.Fields(segment)
		if len(tokens) == 0 {
			continue
		}
		first := tokens[0]
		for strings.Contains(first, "=") && len(tokens) > 1 {
			tokens = tokens[1:]
			first = tokens[0]
		}
		if containsString(e.settings.DeniedWords, first) {
			return "", "command_not_allowed", fmt.Errorf("command uses a denied executable or token: %s", first)
		}
		if !e.settings.FullAccess() && len(tokens) >= 2 && tokens[0] == "git" && tokens[1] == "push" {
			return "", "command_not_allowed", fmt.Errorf("raw 'git push' is blocked; use the git_push tool")
		}
	}
	destructive := false
	for _, prefix := range e.settings.DestructiveWords {
		for _, segment := range segments {
			if segment == prefix || strings.HasPrefix(segment, prefix+" ") {
				destructive = true
			}
		}
	}
	if destructive && !confirmed {
		return "", "confirmation_required", fmt.Errorf("command matches a destructive pattern and requires confirmed=true")
	}
	if mode == "guarded" || exempt {
		return command, "", nil
	}
	if strings.ContainsAny(command, "|;&><`") || strings.Contains(command, "$(") {
		return "", "command_not_allowed", fmt.Errorf("shell operators are not allowed in allowlist mode")
	}
	normalized := strings.Join(strings.Fields(command), " ")
	for _, prefix := range []string{"docker compose", "systemctl"} {
		if normalized == prefix || strings.HasPrefix(normalized, prefix+" ") {
			if !confirmed {
				return "", "confirmation_required", fmt.Errorf("this command requires owner confirmation")
			}
		}
	}
	for _, rule := range allowlistedCommands {
		if normalized == rule.command || (rule.allowSuffix && strings.HasPrefix(normalized, rule.command+" ")) {
			return normalized, "", nil
		}
	}
	return "", "command_not_allowed", fmt.Errorf("command is not allowlisted")
}

func splitShellSegments(command string) []string {
	replacer := strings.NewReplacer("&&", "\n", "||", "\n", ";", "\n", "|", "\n")
	var result []string
	for _, segment := range strings.Split(replacer.Replace(command), "\n") {
		if segment = strings.TrimSpace(segment); segment != "" {
			result = append(result, segment)
		}
	}
	return result
}

func (e *Engine) runCommands(ctx context.Context, args map[string]any) map[string]any {
	commands := stringSliceArg(args, "commands")
	if len(commands) == 0 {
		return failure("invalid_commands", "commands must not be empty")
	}
	results := make([]map[string]any, 0, len(commands))
	ok := true
	for _, command := range commands {
		requestArgs := cloneMap(args)
		requestArgs["command"] = command
		result := e.runCommand(ctx, e.commandRequestFromArgs(requestArgs, false))
		results = append(results, result)
		if result["ok"] == false {
			ok = false
			if boolArg(args, "stop_on_failure", false) {
				break
			}
		}
	}
	return map[string]any{"ok": ok, "results": results, "count": len(results)}
}

func (e *Engine) runTestPreset(ctx context.Context, args map[string]any) map[string]any {
	preset := stringArg(args, "preset", "")
	cwd := stringArg(args, "cwd", "")
	action := preset
	if service, named, ok := strings.Cut(preset, ":"); ok {
		cwd, action = service, named
	}
	if cwd == "" {
		cwd = "."
	}
	presets := e.presetsFor(cwd)
	configuration, ok := presets[action].(map[string]any)
	if !ok {
		return map[string]any{"ok": false, "error_kind": "unknown_preset", "error": fmt.Sprintf("unknown preset %q", preset), "available": sortedKeys(presets)}
	}
	command := fmt.Sprint(configuration["command"])
	requestArgs := cloneMap(args)
	requestArgs["command"] = command
	requestArgs["cwd"] = cwd
	if boolArg(args, "background", false) {
		return e.startJob(ctx, requestArgs)
	}
	request := e.commandRequestFromArgs(requestArgs, true)
	result := e.runCommand(ctx, request)
	result["preset"] = preset
	return result
}

func sortedKeys(values map[string]any) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (e *Engine) startJob(parent context.Context, args map[string]any) map[string]any {
	request := e.commandRequestFromArgs(args, boolArg(args, "background", false))
	normalized, kind, err := e.checkCommandPolicy(request.Command, request.Confirmed, request.PolicyExempt)
	if err != nil {
		return map[string]any{"ok": false, "error_kind": kind, "error": err.Error()}
	}
	directory, err := e.resolveCommandCWD(request.CWD)
	if err != nil {
		return withError("invalid_cwd", err)
	}
	onConflict := stringArg(args, "on_conflict", "fail")
	if request.ConcurrencyKey != "" {
		if existing := e.findRunningByKey(request.ConcurrencyKey); existing != nil {
			if onConflict == "attach" || onConflict == "wait" {
				return e.jobResult(existing, request.TailLines, false)
			}
			return map[string]any{"ok": false, "error_kind": "job_conflict", "job_id": existing.ID, "concurrency_key": request.ConcurrencyKey}
		}
	}
	id := randomID()
	jobContext, cancel := context.WithTimeout(context.Background(), request.Timeout)
	entry := &job{ID: id, LogID: id, Command: normalized, CWD: directory, Status: "running", StartedAt: time.Now().UTC(), ExitCode: -1, ConcurrencyKey: request.ConcurrencyKey, cancel: cancel, done: make(chan struct{})}
	e.jobsMu.Lock()
	e.jobs[id] = entry
	e.jobsMu.Unlock()
	go e.runJob(jobContext, entry, request)
	return e.jobResult(entry, request.TailLines, false)
}

func (e *Engine) runJob(ctx context.Context, entry *job, request commandRequest) {
	defer close(entry.done)
	bash := bashBinary()
	if bash == "" {
		entry.mu.Lock()
		entry.Status, entry.Stderr, entry.FinishedAt = "failed", "bash is required", time.Now().UTC()
		entry.mu.Unlock()
		return
	}
	commandText := entry.Command
	if strings.TrimSpace(e.settings.CommandShellPrelude) != "" {
		commandText = e.settings.CommandShellPrelude + "\n" + commandText
	}
	process := exec.CommandContext(ctx, bash, "-lc", commandText)
	process.Dir = entry.CWD
	process.Env = os.Environ()
	for key, value := range request.Env {
		if environmentName.MatchString(key) {
			process.Env = append(process.Env, key+"="+value)
		}
	}
	configureProcessGroup(process)
	var stdout strings.Builder
	var stderr strings.Builder
	process.Stdout, process.Stderr = &stdout, &stderr
	err := process.Start()
	if err == nil {
		entry.mu.Lock()
		entry.pid = process.Process.Pid
		entry.mu.Unlock()
		err = process.Wait()
	}
	exitCode := 0
	if err != nil {
		exitCode = -1
		if exit, ok := err.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
	}
	stdoutText := redact(stdout.String())
	stderrText := redact(stderr.String())
	timedOut := ctx.Err() == context.DeadlineExceeded

	// Keep the externally visible state at "running" until durable logs are
	// written. Pollers may clean up their workspace as soon as they observe a
	// terminal state, so publishing completion first races with log creation.
	entry.mu.Lock()
	entry.Stdout, entry.Stderr = stdoutText, stderrText
	entry.ExitCode, entry.TimedOut = exitCode, timedOut
	entry.mu.Unlock()
	e.writeCommandLogs(entry.LogID, entry.Command, entry.CWD, stdoutText, stderrText, exitCode)

	entry.mu.Lock()
	entry.FinishedAt = time.Now().UTC()
	entry.Status = "completed"
	if ctx.Err() == context.Canceled {
		entry.Status = "cancelled"
	} else if timedOut {
		entry.Status = "timed_out"
	} else if exitCode != 0 {
		entry.Status = "failed"
	}
	entry.mu.Unlock()
}

func (e *Engine) findRunningByKey(key string) *job {
	e.jobsMu.RLock()
	defer e.jobsMu.RUnlock()
	for _, entry := range e.jobs {
		entry.mu.RLock()
		running := entry.ConcurrencyKey == key && entry.Status == "running"
		entry.mu.RUnlock()
		if running {
			return entry
		}
	}
	return nil
}

func (e *Engine) getJob(id string, tailLines int, concise bool) map[string]any {
	e.jobsMu.RLock()
	entry := e.jobs[id]
	e.jobsMu.RUnlock()
	if entry == nil {
		return failure("job_not_found", fmt.Sprintf("unknown job id: %s", id))
	}
	return e.jobResult(entry, tailLines, concise)
}

func (e *Engine) jobResult(entry *job, tailLines int, concise bool) map[string]any {
	entry.mu.RLock()
	defer entry.mu.RUnlock()
	result := map[string]any{
		"ok": entry.Status == "running" || entry.Status == "completed", "job_id": entry.ID,
		"log_id": entry.LogID, "status": entry.Status, "command": entry.Command,
		"cwd": e.perimeter.Display(entry.CWD), "exit_code": entry.ExitCode,
		"started_at": entry.StartedAt.Format(time.RFC3339Nano), "timed_out": entry.TimedOut,
		"concurrency_key": entry.ConcurrencyKey,
	}
	if !entry.FinishedAt.IsZero() {
		result["finished_at"] = entry.FinishedAt.Format(time.RFC3339Nano)
	}
	if !concise {
		result["stdout"] = tailText(entry.Stdout, tailLines)
		result["stderr"] = tailText(entry.Stderr, tailLines)
	}
	return result
}

func (e *Engine) cancelJob(id string) map[string]any {
	e.jobsMu.RLock()
	entry := e.jobs[id]
	e.jobsMu.RUnlock()
	if entry == nil {
		return failure("job_not_found", fmt.Sprintf("unknown job id: %s", id))
	}
	entry.mu.Lock()
	if entry.Status != "running" {
		status := entry.Status
		entry.mu.Unlock()
		return map[string]any{"ok": true, "job_id": id, "status": status, "cancelled": false}
	}
	entry.cancel()
	if entry.pid > 0 {
		_ = terminateProcessGroup(entry.pid)
	}
	entry.mu.Unlock()
	select {
	case <-entry.done:
	case <-time.After(2 * time.Second):
		return map[string]any{"ok": false, "error_kind": "job_cancel_timeout", "job_id": id, "status": "running"}
	}
	entry.mu.RLock()
	status := entry.Status
	entry.mu.RUnlock()
	return map[string]any{"ok": status == "cancelled", "job_id": id, "status": status, "cancelled": status == "cancelled"}
}

func (e *Engine) writeCommandLogs(id, command, cwd, stdout, stderr string, exitCode int) {
	if err := os.MkdirAll(e.settings.CommandJobsDir, 0o700); err == nil {
		_ = os.WriteFile(filepath.Join(e.settings.CommandJobsDir, id+".stdout"), []byte(stdout), 0o600)
		_ = os.WriteFile(filepath.Join(e.settings.CommandJobsDir, id+".stderr"), []byte(stderr), 0o600)
		metadata, _ := json.Marshal(map[string]any{"log_id": id, "command": redact(command), "cwd": cwd, "exit_code": exitCode})
		_ = os.WriteFile(filepath.Join(e.settings.CommandJobsDir, id+".json"), append(metadata, '\n'), 0o600)
	}
	payload, _ := json.Marshal(map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "log_id": id, "command": redact(command), "cwd": cwd, "exit_code": exitCode})
	if err := os.MkdirAll(filepath.Dir(e.settings.CommandAuditLogPath), 0o700); err == nil {
		if file, openErr := os.OpenFile(e.settings.CommandAuditLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); openErr == nil {
			_, _ = file.Write(append(payload, '\n'))
			_ = file.Close()
		}
	}
}

func (e *Engine) getCommandLog(args map[string]any) map[string]any {
	id := stringArg(args, "log_id", "")
	stream := stringArg(args, "stream", "stdout")
	if stream != "stdout" && stream != "stderr" {
		return failure("invalid_stream", "stream must be stdout or stderr")
	}
	data, err := os.ReadFile(filepath.Join(e.settings.CommandJobsDir, id+"."+stream))
	if err != nil {
		return withError("command_log_error", err)
	}
	lines := strings.Split(strings.TrimSuffix(string(data), "\n"), "\n")
	start, end := intArg(args, "start_line", 1), intArg(args, "end_line", len(lines))
	if start < 1 {
		start = 1
	}
	if end <= 0 || end > len(lines) {
		end = len(lines)
	}
	if grep := optionalString(args, "grep"); grep != nil {
		expression, compileErr := regexp.Compile(*grep)
		if compileErr != nil {
			return withError("invalid_regex", compileErr)
		}
		filtered := lines[:0]
		for _, line := range lines {
			if expression.MatchString(line) {
				filtered = append(filtered, line)
			}
		}
		lines = filtered
		start, end = 1, len(lines)
	}
	content := ""
	if start <= end && start <= len(lines) {
		content = strings.Join(lines[start-1:end], "\n")
	}
	return map[string]any{"ok": true, "log_id": id, "stream": stream, "content": content, "start_line": start, "end_line": end, "total_lines": len(lines)}
}

func (e *Engine) summarizeCommandLog(id, parser string) map[string]any {
	stdout, outErr := os.ReadFile(filepath.Join(e.settings.CommandJobsDir, id+".stdout"))
	stderr, errErr := os.ReadFile(filepath.Join(e.settings.CommandJobsDir, id+".stderr"))
	if outErr != nil && errErr != nil {
		return failure("command_log_error", "command log not found")
	}
	return map[string]any{"ok": true, "log_id": id, "summary": parseCommandSummary("", string(stdout), string(stderr), parser)}
}

func (e *Engine) scanPolicy(ctx context.Context, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		return withError("policy_scan_failed", err)
	}
	arguments := []string{"-C", repo, "diff", "--unified=0", stringArg(args, "base_ref", "HEAD"), "--"}
	arguments = append(arguments, stringSliceArg(args, "paths")...)
	output, err := exec.CommandContext(ctx, "git", arguments...).Output()
	if err != nil {
		return withError("policy_scan_failed", err)
	}
	rules := stringSliceArg(args, "rules")
	if len(rules) == 0 {
		rules = []string{"no_secret_like_literals", "no_new_console_log", "no_new_any", "no_new_print"}
	}
	patterns := map[string]*regexp.Regexp{
		"no_secret_like_literals": regexp.MustCompile(`(?i)(password|secret|api[_-]?key)\s*[:=]\s*["'][^"']+["']`),
		"no_new_console_log":      regexp.MustCompile(`\bconsole\.log\s*\(`),
		"no_new_any":              regexp.MustCompile(`(?::\s*any\b|\bas\s+any\b)`),
		"no_new_print":            regexp.MustCompile(`\bprint\s*\(`),
	}
	var violations []map[string]any
	currentPath := ""
	for _, line := range strings.Split(string(output), "\n") {
		if strings.HasPrefix(line, "+++ b/") {
			currentPath = strings.TrimPrefix(line, "+++ b/")
			continue
		}
		if !strings.HasPrefix(line, "+") || strings.HasPrefix(line, "+++") {
			continue
		}
		text := strings.TrimPrefix(line, "+")
		for _, rule := range rules {
			if pattern := patterns[rule]; pattern != nil && pattern.MatchString(text) {
				violations = append(violations, map[string]any{"rule": rule, "path": currentPath, "text": text})
			}
		}
	}
	return map[string]any{"ok": len(violations) == 0, "violations": violations, "count": len(violations), "rules": rules}
}

func (e *Engine) runQualityGate(ctx context.Context, args map[string]any) map[string]any {
	checks := mapsArg(args, "checks")
	results := make([]map[string]any, 0, len(checks))
	ok := true
	for index, check := range checks {
		var result map[string]any
		if stringArg(check, "type", "") == "policy" || stringArg(check, "policy", "") != "" {
			result = e.scanPolicy(ctx, check)
		} else if preset := stringArg(check, "preset", ""); preset != "" {
			presetArgs := cloneMap(check)
			presetArgs["preset"] = preset
			result = e.runTestPreset(ctx, presetArgs)
		} else {
			result = e.runCommand(ctx, e.commandRequestFromArgs(check, true))
		}
		required := boolArg(check, "required", true)
		result["id"] = firstNonEmpty(stringArg(check, "id", ""), fmt.Sprintf("check-%d", index+1))
		result["required"] = required
		results = append(results, result)
		if required && result["ok"] == false {
			ok = false
			if boolArg(args, "stop_on_failure", true) {
				break
			}
		}
	}
	return map[string]any{"ok": ok, "name": stringArg(args, "name", ""), "checks": results}
}

func (e *Engine) qualityGateAndCommit(ctx context.Context, args map[string]any) map[string]any {
	gate := e.runQualityGate(ctx, args)
	result := map[string]any{"ok": gate["ok"], "gate": gate}
	if gate["ok"] == false {
		result["commit"] = map[string]any{"ok": false, "skipped": true, "reason": "quality gate failed"}
		return result
	}
	commit := mapArg(args, "commit")
	if commit == nil || !boolArg(commit, "enabled", true) {
		result["commit"] = map[string]any{"ok": true, "skipped": true}
		return result
	}
	commit["repo"] = stringArg(args, "repo", "")
	commit["dry_run"] = false
	committed := e.gitCommit(ctx, commit)
	result["commit"] = committed
	result["ok"] = committed["ok"]
	if boolArg(args, "require_clean_after_commit", true) && committed["ok"] == true {
		guard := e.worktreeGuard(ctx, map[string]any{"repo": stringArg(args, "repo", "")})
		result["final_guard"] = guard
		result["ok"] = guard["ok"]
	}
	return result
}

func (e *Engine) worktreeGuard(ctx context.Context, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		return withError("worktree_guard_failed", err)
	}
	status, err := exec.CommandContext(ctx, "git", "-C", repo, "status", "--porcelain").Output()
	if err != nil {
		return withError("worktree_guard_failed", err)
	}
	allowed := stringSliceArg(args, "allowed_dirty_paths")
	var unexpected []string
	for _, line := range strings.Split(strings.TrimSpace(string(status)), "\n") {
		if line == "" {
			continue
		}
		path := strings.TrimSpace(line[3:])
		if !containsString(allowed, path) {
			unexpected = append(unexpected, path)
		}
	}
	branchOutput, _ := exec.CommandContext(ctx, "git", "-C", repo, "branch", "--show-current").Output()
	branch := strings.TrimSpace(string(branchOutput))
	required := stringArg(args, "require_branch", "")
	rebasing := fileExists(filepath.Join(repo, ".git", "rebase-merge")) || fileExists(filepath.Join(repo, ".git", "rebase-apply"))
	ok := len(unexpected) == 0 && (required == "" || required == branch) && (!boolArg(args, "require_not_rebasing", true) || !rebasing)
	return map[string]any{"ok": ok, "branch": branch, "required_branch": required, "unexpected_dirty_paths": unexpected, "rebasing": rebasing}
}

func (e *Engine) gitCommit(ctx context.Context, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		return withError("git_commit_failed", err)
	}
	message := strings.TrimSpace(stringArg(args, "message", ""))
	paths := stringSliceArg(args, "paths")
	if message == "" || len(paths) == 0 {
		return failure("git_commit_rejected", "message and explicit paths are required")
	}
	for _, path := range paths {
		if path == "." || path == "-A" || path == "--all" {
			return failure("git_commit_rejected", "blanket staging is not allowed")
		}
		if _, resolveErr := e.perimeter.Resolve(filepath.Join(repo, path), true, true); resolveErr != nil {
			return withError("git_commit_rejected", resolveErr)
		}
	}
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if dryRun {
		return map[string]any{"ok": true, "dry_run": true, "applied": false, "message": message, "paths": paths}
	}
	addArgs := append([]string{"-C", repo, "add", "--"}, paths...)
	if output, runErr := exec.CommandContext(ctx, "git", addArgs...).CombinedOutput(); runErr != nil {
		return map[string]any{"ok": false, "error_kind": "git_commit_failed", "error": strings.TrimSpace(string(output))}
	}
	commitArgs := []string{"-C", repo, "commit", "-m", message, "--"}
	commitArgs = append(commitArgs, paths...)
	output, runErr := exec.CommandContext(ctx, "git", commitArgs...).CombinedOutput()
	if runErr != nil {
		return map[string]any{"ok": false, "error_kind": "git_commit_failed", "error": strings.TrimSpace(string(output))}
	}
	sha, _ := exec.CommandContext(ctx, "git", "-C", repo, "rev-parse", "HEAD").Output()
	return map[string]any{"ok": true, "dry_run": false, "applied": true, "commit": strings.TrimSpace(string(sha)), "output": strings.TrimSpace(string(output)), "paths": paths}
}

func redact(text string) string {
	result := text
	for _, pattern := range secretPatterns {
		result = pattern.ReplaceAllStringFunc(result, func(match string) string {
			if strings.Contains(strings.ToLower(match), "bearer ") {
				return "Bearer [REDACTED]"
			}
			if key, _, ok := strings.Cut(match, "="); ok {
				return key + "=[REDACTED]"
			}
			return "[REDACTED]"
		})
	}
	return result
}

func tailText(text string, lines int) string {
	if lines < 0 {
		return text
	}
	if lines == 0 {
		return ""
	}
	parts := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	if len(parts) > lines {
		parts = parts[len(parts)-lines:]
	}
	return strings.Join(parts, "\n")
}

func randomID() string {
	buffer := make([]byte, 12)
	if _, err := rand.Read(buffer); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buffer)
}

func parseCommandSummary(command, stdout, stderr, kind string) map[string]any {
	combined := stdout + "\n" + stderr
	if kind == "none" {
		return nil
	}
	if kind == "auto" {
		switch {
		case strings.Contains(command, "pytest") || strings.Contains(combined, " passed"):
			kind = "pytest"
		case strings.Contains(command, "go test") || strings.Contains(combined, "?\t") || strings.Contains(combined, "ok\t"):
			kind = "gotest"
		case strings.Contains(command, "ruff"):
			kind = "ruff"
		default:
			kind = "generic"
		}
	}
	counts := make(map[string]int)
	countPattern := regexp.MustCompile(`(?i)(\d+)\s+(passed|failed|skipped)`)
	for _, match := range countPattern.FindAllStringSubmatch(combined, -1) {
		var value int
		_, _ = fmt.Sscan(match[1], &value)
		counts[strings.ToLower(match[2])] += value
	}
	return map[string]any{"parser": kind, "counts": counts}
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
