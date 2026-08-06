package tools

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
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
	mu                  sync.RWMutex
	ID                  string
	LogID               string
	Command             string
	CWD                 string
	Status              string
	StartedAt           time.Time
	FinishedAt          time.Time
	ExitCode            int
	Stdout              string
	Stderr              string
	StdoutBytes         int64
	StderrBytes         int64
	OutputTruncated     bool
	TimedOut            bool
	ConcurrencyKey      string
	cancel              context.CancelFunc
	pid                 int
	pgid                int
	TerminationReason   string
	CancelRequested     bool
	ProcessGroupCleaned bool
	heavyLease          *heavyOperationLease
	done                chan struct{}
}

type commandRequest struct {
	Command        string
	Timeout        time.Duration
	CWD            string
	Env            map[string]string
	MaxOutput      int
	MaxOutputSet   bool
	TailLines      int
	Confirmed      bool
	ParseKind      string
	PolicyExempt   bool
	ConcurrencyKey string
}

var (
	environmentName   = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	artifactIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	secretPatterns    = []*regexp.Regexp{
		regexp.MustCompile(`(?i)(token|secret|password|api[_-]?key)=([^\s]+)`),
		regexp.MustCompile(`gh[pousr]_[A-Za-z0-9_]{20,}`),
		regexp.MustCompile(`npm_[A-Za-z0-9_]{20,}`),
		regexp.MustCompile(`(?i)Bearer\s+[A-Za-z0-9._-]+`),
		regexp.MustCompile(`https?://[^\s/@]+:[^\s/@]+@[^\s]+`),
		regexp.MustCompile(`git@[^:\s]+:[^\s]+`),
	}
	commandAuditMu      sync.Mutex
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
	case "read_artifact":
		artifactID := stringArg(args, "artifact_id", "")
		if !artifactIDPattern.MatchString(artifactID) || artifactID == "." || artifactID == ".." {
			return failure("invalid_artifact_id", "artifact_id must be an opaque identifier")
		}
		store, err := e.artifactStore()
		if err != nil {
			return outputPersistenceError(err)
		}
		page, err := store.readPage(artifactID, "records", stringArg(args, "cursor", ""), intArg(args, "max_bytes", 64*1024))
		if err != nil {
			return withError("artifact_read_failed", err)
		}
		return page
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
	case "list_command_jobs":
		return e.listJobs(args)
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
	maximum := e.settings.DefaultInlineOutputBytes
	_, maxOutputSet := args["max_output_chars"]
	if maxOutputSet {
		maximum = intArg(args, "max_output_chars", e.settings.DefaultInlineOutputBytes)
	}
	if maximum <= 0 {
		maximum = e.settings.DefaultInlineOutputBytes
	} else if maximum > e.settings.MaxCommandOutputChars {
		maximum = e.settings.MaxCommandOutputChars
	}
	tailLines := -1
	if _, requested := args["tail_lines"]; requested {
		tailLines = intArg(args, "tail_lines", -1)
	}
	return commandRequest{
		Command: stringArg(args, "command", ""), Timeout: timeout, CWD: stringArg(args, "cwd", ""),
		Env: environment, MaxOutput: maximum, MaxOutputSet: maxOutputSet, TailLines: tailLines,
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
	heavyLease, acquired := e.acquireHeavyOperation()
	if !acquired {
		return e.heavyBusyResult()
	}
	defer heavyLease.Release()
	logID := randomID()
	store, err := e.artifactStore()
	if err != nil {
		return outputPersistenceError(err)
	}
	capture, err := newCommandCapture(e.settings.CommandJobsDir, logID, request.MaxOutput, store)
	if err != nil {
		return outputPersistenceError(err)
	}
	e.writeCommandAudit("start", logID, "run_command", normalized, directory, 0, 0, 0, "running")
	result := e.runShell(ctx, normalized, directory, request.Timeout, request.Env, capture)
	if err := capture.Close(); err != nil {
		return outputPersistenceError(err)
	}
	stdoutLimit, stderrLimit := request.MaxOutput, request.MaxOutput
	if capture.stdout.Total() > 0 && capture.stderr.Total() > 0 {
		stdoutLimit = (request.MaxOutput + 1) / 2
		stderrLimit = request.MaxOutput - stdoutLimit
	}
	result.Stdout, result.Stderr = capture.stdout.Preview(stdoutLimit), capture.stderr.Preview(stderrLimit)
	if request.TailLines >= 0 {
		result.Stdout = capture.stdout.TailLines(request.TailLines)
		result.Stderr = capture.stderr.TailLines(request.TailLines)
		result.Stdout, _ = capText(result.Stdout, stdoutLimit)
		result.Stderr, _ = capText(result.Stderr, stderrLimit)
	}
	e.writeCommandMetadata(logID, normalized, directory, result.ExitCode)
	e.writeCommandAudit("finish", logID, "run_command", normalized, directory, time.Since(started), capture.stdout.Total(), capture.stderr.Total(), map[bool]string{true: "completed", false: "failed"}[result.ExitCode == 0 && !result.TimedOut])
	truncated := capture.stdout.Total() > int64(len(result.Stdout)) || capture.stderr.Total() > int64(len(result.Stderr))
	receipt := boundedOutputReceiptWithInlineLimit(
		truncated, capture.stdout.Total()+capture.stderr.Total(),
		int64(len(result.Stdout)+len(result.Stderr)), e.settings.DefaultInlineOutputBytes, request.MaxOutput,
	)
	receipt["returned"] = map[string]any{
		"stdout_bytes": len(result.Stdout), "stderr_bytes": len(result.Stderr),
	}
	receipt["total"] = map[string]any{
		"stdout_bytes": capture.stdout.Total(), "stderr_bytes": capture.stderr.Total(),
	}
	if request.MaxOutputSet {
		receipt["requested"] = map[string]any{"inline_output_bytes": request.MaxOutput}
	}
	response := map[string]any{
		"ok": result.ExitCode == 0 && !result.TimedOut, "command": normalized,
		"cwd": e.perimeter.Display(directory), "exit_code": result.ExitCode,
		"stdout": result.Stdout, "stderr": result.Stderr, "timed_out": result.TimedOut,
		"duration_ms": time.Since(started).Milliseconds(), "log_id": logID,
		"summary":      parseCommandSummary(normalized, capture.stdout.Head(), capture.stderr.Head(), request.ParseKind),
		"stdout_bytes": capture.stdout.Total(), "stderr_bytes": capture.stderr.Total(),
		"output_truncated": truncated,
		"artifact":         map[string]any{"artifact_id": logID, "kind": "command_output", "ordering": "stdout_then_stderr", "continuation_tool": "read_artifact"},
		"receipt":          receipt,
	}
	if truncated {
		response["continuation"] = artifactContinuation(logID)
	}
	return response
}

func artifactContinuation(id string) map[string]any {
	return map[string]any{"tool": "read_artifact", "arguments": map[string]any{"artifact_id": id}}
}

func boundedOutputReceipt(truncated bool, total, returned int64) map[string]any {
	status, reason, completeness := "completed", "none", "complete"
	if truncated {
		status, reason, completeness = "partial", "inline_limit", "partial"
	}
	return map[string]any{"schema_version": 1, "status": status, "completeness": completeness, "reason": reason, "requested": map[string]any{}, "applied": map[string]any{"source_complete": true}, "returned": map[string]any{"bytes": returned}, "total": map[string]any{"bytes": total}, "warnings": []string{}}
}

func boundedOutputReceiptWithInlineLimit(truncated bool, total, returned int64, configured, applied int) map[string]any {
	receipt := boundedOutputReceipt(truncated, total, returned)
	receipt["configured"] = map[string]any{"inline_output_bytes": configured}
	receipt["applied"].(map[string]any)["inline_output_bytes"] = applied
	return receipt
}

func artifactProcessOutput(result processResult, stdoutBytes, stderrBytes int64, configured, applied int) (string, bool, map[string]any) {
	output := result.Stdout
	if result.Stdout != "" && result.Stderr != "" {
		output += "\n"
	}
	output += result.Stderr
	truncated := stdoutBytes > int64(len(result.Stdout)) || stderrBytes > int64(len(result.Stderr))
	receipt := boundedOutputReceiptWithInlineLimit(truncated, stdoutBytes+stderrBytes, int64(len(output)), configured, applied)
	receipt["returned"].(map[string]any)["stdout_bytes"] = len(result.Stdout)
	receipt["returned"].(map[string]any)["stderr_bytes"] = len(result.Stderr)
	receipt["total"].(map[string]any)["stdout_bytes"] = stdoutBytes
	receipt["total"].(map[string]any)["stderr_bytes"] = stderrBytes
	return output, truncated, receipt
}

func (e *Engine) runShell(parent context.Context, command, directory string, timeout time.Duration, overrides map[string]string, capture *commandCapture) processResult {
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
	process := exec.Command(bash, "-lc", commandText)
	process.Dir = directory
	process.Env = e.commandEnvironment(nil)
	for key, value := range overrides {
		if !environmentName.MatchString(key) {
			return processResult{ExitCode: -1, Stderr: fmt.Sprintf("invalid environment variable name: %s", key)}
		}
		process.Env = append(process.Env, key+"="+value)
	}
	process.Stdout = capture.stdout
	process.Stderr = capture.stderr
	configureProcessGroup(process)
	err := process.Start()
	if err == nil {
		done := make(chan error, 1)
		go func() { done <- process.Wait() }()
		select {
		case err = <-done:
		case <-ctx.Done():
			_, _ = terminateProcessGroup(process.Process.Pid, e.settings.KillGrace)
			err = <-done
		}
	}
	exitCode := 0
	if err != nil {
		exitCode = -1
		if exit, ok := err.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
	}
	return processResult{ExitCode: exitCode, TimedOut: ctx.Err() == context.DeadlineExceeded}
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
		directory, _ := e.resolveCommandCWD(cwd)
		fingerprint := sha256.Sum256([]byte(directory + "\x00" + preset + "\x00" + strings.Join(strings.Fields(command), " ")))
		requestArgs["concurrency_key"] = "preset:" + fmt.Sprintf("%x", fingerprint[:])
		requestArgs["on_conflict"] = "attach"
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
	return e.startJobRequest(parent, args, false)
}

func (e *Engine) startInternalJob(parent context.Context, args map[string]any) map[string]any {
	return e.startJobRequest(parent, args, true)
}

func (e *Engine) startJobRequest(parent context.Context, args map[string]any, policyExempt bool) map[string]any {
	request := e.commandRequestFromArgs(args, policyExempt)
	normalized, kind, err := e.checkCommandPolicy(request.Command, request.Confirmed, request.PolicyExempt)
	if err != nil {
		return map[string]any{"ok": false, "error_kind": kind, "error": err.Error()}
	}
	directory, err := e.resolveCommandCWD(request.CWD)
	if err != nil {
		return withError("invalid_cwd", err)
	}
	onConflict := stringArg(args, "on_conflict", "fail")
	if onConflict != "attach" && onConflict != "fail" && onConflict != "wait" {
		return failure("invalid_on_conflict", "on_conflict must be attach, fail, or wait")
	}
	if request.ConcurrencyKey != "" {
		if existing := e.findRunningByKey(request.ConcurrencyKey); existing != nil {
			if onConflict == "attach" {
				return e.jobResult(existing, request.TailLines, false)
			}
			if onConflict == "wait" {
				select {
				case <-existing.done:
				case <-time.After(min(request.Timeout, 30*time.Second)):
					return map[string]any{"ok": false, "error_kind": "job_lock_conflict", "job_id": existing.ID, "concurrency_key": request.ConcurrencyKey}
				}
			}
			if onConflict == "fail" {
				return map[string]any{"ok": false, "error_kind": "job_lock_conflict", "job_id": existing.ID, "concurrency_key": request.ConcurrencyKey}
			}
		}
	}
	heavyLease, acquired := e.acquireHeavyOperation()
	if !acquired {
		return e.heavyBusyResult()
	}
	id := randomID()
	jobContext, cancel := context.WithTimeout(context.Background(), request.Timeout)
	entry := &job{ID: id, LogID: randomID(), Command: normalized, CWD: directory, Status: "running", StartedAt: time.Now().UTC(), ExitCode: -1, ConcurrencyKey: request.ConcurrencyKey, cancel: cancel, heavyLease: heavyLease, done: make(chan struct{})}
	e.jobsMu.Lock()
	e.jobs[id] = entry
	e.jobsMu.Unlock()
	go e.runJob(jobContext, entry, request)
	return e.jobResult(entry, request.TailLines, false)
}

func (e *Engine) runJob(ctx context.Context, entry *job, request commandRequest) {
	defer close(entry.done)
	defer entry.heavyLease.Release()
	store, storeErr := e.artifactStore()
	if storeErr != nil {
		entry.mu.Lock()
		entry.Status = "failed"
		entry.TerminationReason = "output_persistence_failed"
		entry.FinishedAt = time.Now()
		entry.mu.Unlock()
		return
	}
	capture, captureErr := newCommandCapture(e.settings.CommandJobsDir, entry.LogID, request.MaxOutput, store)
	if captureErr != nil {
		entry.mu.Lock()
		entry.Status, entry.Stderr, entry.FinishedAt = "failed", captureErr.Error(), time.Now().UTC()
		entry.TerminationReason = "output_persistence_failed"
		entry.mu.Unlock()
		return
	}
	e.writeCommandAudit("start", entry.LogID, "start_command_job", entry.Command, entry.CWD, 0, 0, 0, "running")
	bash := bashBinary()
	if bash == "" {
		_ = capture.Close()
		entry.mu.Lock()
		entry.Status, entry.Stderr, entry.FinishedAt = "failed", "bash is required", time.Now().UTC()
		entry.mu.Unlock()
		return
	}
	commandText := entry.Command
	if strings.TrimSpace(e.settings.CommandShellPrelude) != "" {
		commandText = e.settings.CommandShellPrelude + "\n" + commandText
	}
	process := exec.Command(bash, "-lc", commandText)
	process.Dir = entry.CWD
	process.Env = e.commandEnvironment(nil)
	for key, value := range request.Env {
		if environmentName.MatchString(key) {
			process.Env = append(process.Env, key+"="+value)
		}
	}
	configureProcessGroup(process)
	process.Stdout, process.Stderr = capture.stdout, capture.stderr
	err := process.Start()
	if err == nil {
		entry.mu.Lock()
		entry.pid = process.Process.Pid
		entry.pgid = process.Process.Pid
		entry.mu.Unlock()
		wait := make(chan error, 1)
		go func() { wait <- process.Wait() }()
		select {
		case err = <-wait:
		case <-ctx.Done():
			entry.mu.Lock()
			entry.Status = "terminating"
			entry.mu.Unlock()
			_, _ = terminateProcessGroup(process.Process.Pid, e.settings.KillGrace)
			err = <-wait
		}
	}
	exitCode := 0
	if err != nil {
		exitCode = -1
		if exit, ok := err.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
	}
	timedOut := ctx.Err() == context.DeadlineExceeded

	// Keep the externally visible state at "running" until durable logs are
	// written. Pollers may clean up their workspace as soon as they observe a
	// terminal state, so publishing completion first races with log creation.
	if closeErr := capture.Close(); closeErr != nil {
		entry.mu.Lock()
		entry.Status, entry.TerminationReason, entry.FinishedAt = "failed", "output_persistence_failed", time.Now().UTC()
		entry.mu.Unlock()
		e.writeCommandAudit("finish", entry.LogID, "start_command_job", entry.Command, entry.CWD, time.Since(entry.StartedAt), capture.stdout.Total(), capture.stderr.Total(), "failed")
		return
	}
	entry.mu.Lock()
	entry.Stdout, entry.Stderr = capture.stdout.TailLines(request.TailLines), capture.stderr.TailLines(request.TailLines)
	entry.StdoutBytes, entry.StderrBytes = capture.stdout.Total(), capture.stderr.Total()
	entry.OutputTruncated = entry.StdoutBytes > int64(len(entry.Stdout)) || entry.StderrBytes > int64(len(entry.Stderr))
	entry.ExitCode, entry.TimedOut = exitCode, timedOut
	entry.ProcessGroupCleaned = entry.pgid == 0 || !processGroupAlive(entry.pgid)
	entry.mu.Unlock()
	e.writeCommandMetadata(entry.LogID, entry.Command, entry.CWD, exitCode)

	entry.mu.Lock()
	entry.FinishedAt = time.Now().UTC()
	entry.Status = "completed"
	if ctx.Err() == context.Canceled {
		entry.Status = "cancelled"
		entry.TerminationReason = "user_cancel"
	} else if timedOut {
		entry.Status = "timed_out"
		entry.TerminationReason = "timeout"
	} else if exitCode != 0 {
		entry.Status = "failed"
		entry.TerminationReason = "nonzero_exit"
	} else {
		entry.TerminationReason = "completed"
	}
	status := entry.Status
	entry.mu.Unlock()
	e.writeCommandAudit("finish", entry.LogID, "start_command_job", entry.Command, entry.CWD, time.Since(entry.StartedAt), entry.StdoutBytes, entry.StderrBytes, status)
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
	sourceActive := entry.Status == "running" || entry.Status == "queued" || entry.Status == "terminating"
	receipt := boundedOutputReceipt(entry.OutputTruncated, entry.StdoutBytes+entry.StderrBytes, int64(len(entry.Stdout)+len(entry.Stderr)))
	if sourceActive {
		receipt["status"] = "partial"
		receipt["completeness"] = "partial"
		receipt["reason"] = "source_active"
		receipt["applied"].(map[string]any)["source_complete"] = false
	}
	result := map[string]any{
		"ok": entry.Status == "running" || entry.Status == "completed", "job_id": entry.ID,
		"log_id": entry.LogID, "status": entry.Status, "command": entry.Command,
		"cwd": e.perimeter.Display(entry.CWD), "exit_code": entry.ExitCode,
		"started_at": entry.StartedAt.Format(time.RFC3339Nano), "timed_out": entry.TimedOut,
		"concurrency_key": entry.ConcurrencyKey,
		"artifact":        map[string]any{"artifact_id": entry.LogID, "kind": "command_output", "ordering": "stdout_then_stderr", "continuation_tool": "read_artifact"},
		"receipt":         receipt,
		"pid":             entry.pid, "pgid": entry.pgid, "command_redacted": entry.Command,
		"termination_reason": entry.TerminationReason, "cancel_requested": entry.CancelRequested,
		"process_group_cleaned": entry.ProcessGroupCleaned,
		"lock_owner_job_id": func() any {
			if entry.ConcurrencyKey != "" {
				return entry.ID
			}
			return nil
		}(),
	}
	result["last_output_at"] = entry.StartedAt.Format(time.RFC3339Nano)
	result["term_signal"] = nil
	result["stdout_bytes"] = entry.StdoutBytes
	if entry.OutputTruncated || sourceActive {
		result["continuation"] = artifactContinuation(entry.LogID)
	}
	result["stderr_bytes"] = entry.StderrBytes
	result["output_truncated"] = entry.OutputTruncated
	if !entry.FinishedAt.IsZero() {
		result["finished_at"] = entry.FinishedAt.Format(time.RFC3339Nano)
	}
	if !concise {
		result["stdout"] = tailText(entry.Stdout, tailLines)
		result["stderr"] = tailText(entry.Stderr, tailLines)
	}
	return result
}

func (e *Engine) listJobs(args map[string]any) map[string]any {
	wanted := map[string]bool{}
	for _, value := range stringSliceArg(args, "status") {
		wanted[value] = true
	}
	cwd := stringArg(args, "cwd", "")
	if cwd != "" {
		if resolved, err := e.resolveCommandCWD(cwd); err == nil {
			cwd = resolved
		}
	}
	includeFinished := boolArg(args, "include_finished", true)
	limit := intArg(args, "limit", 100)
	if limit < 1 {
		limit = 1
	}
	if limit > 1000 {
		limit = 1000
	}
	e.jobsMu.RLock()
	entries := make([]*job, 0, len(e.jobs))
	for _, entry := range e.jobs {
		entries = append(entries, entry)
	}
	e.jobsMu.RUnlock()
	sort.Slice(entries, func(i, j int) bool { return entries[i].StartedAt.After(entries[j].StartedAt) })
	results := make([]map[string]any, 0, len(entries))
	for _, entry := range entries {
		entry.mu.RLock()
		status, directory := entry.Status, entry.CWD
		entry.mu.RUnlock()
		if len(wanted) > 0 && !wanted[status] {
			continue
		}
		if !includeFinished && status != "queued" && status != "running" && status != "terminating" {
			continue
		}
		if cwd != "" && cwd != directory {
			continue
		}
		results = append(results, e.jobResult(entry, 0, true))
		if len(results) == limit {
			break
		}
	}
	return map[string]any{"ok": true, "jobs": results, "count": len(results)}
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
	entry.CancelRequested = true
	if entry.pid > 0 {
		_, _ = terminateProcessGroup(entry.pid, e.settings.KillGrace)
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

func (e *Engine) writeCommandMetadata(id, command, cwd string, exitCode int) {
	metadata, _ := json.Marshal(map[string]any{"log_id": id, "command": redact(command), "cwd": cwd, "exit_code": exitCode})
	if store, err := e.artifactStore(); err == nil {
		_ = store.writeCompanion(id, filepath.Join(e.settings.CommandJobsDir, id+".json"), append(metadata, '\n'))
	}
	payload, _ := json.Marshal(map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "log_id": id, "command": redact(command), "cwd": cwd, "exit_code": exitCode})
	if err := os.MkdirAll(filepath.Dir(e.settings.CommandAuditLogPath), 0o700); err == nil {
		if file, openErr := os.OpenFile(e.settings.CommandAuditLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); openErr == nil {
			_, _ = file.Write(append(payload, '\n'))
			_ = file.Close()
		}
	}
}

func (e *Engine) writeCommandAudit(event, requestID, tool, command, cwd string, duration time.Duration, stdoutBytes, stderrBytes int64, status string) {
	commandAuditMu.Lock()
	defer commandAuditMu.Unlock()
	fingerprint := sha256.Sum256([]byte(redact(command) + "\x00" + cwd))
	payload, _ := json.Marshal(map[string]any{
		"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "event": event,
		"request_id": requestID, "tool": tool, "args_fingerprint": fmt.Sprintf("%x", fingerprint[:]),
		"duration_ms": duration.Milliseconds(), "stdout_bytes": stdoutBytes, "stderr_bytes": stderrBytes,
		"status": status,
	})
	if err := os.MkdirAll(filepath.Dir(e.settings.CommandAuditLogPath), 0o700); err != nil {
		return
	}
	rotateAuditLog(e.settings.CommandAuditLogPath, 10*1024*1024, 5)
	file, err := os.OpenFile(e.settings.CommandAuditLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	_, _ = file.Write(append(payload, '\n'))
	_ = file.Close()
}

func rotateAuditLog(path string, maximum int64, keep int) {
	info, err := os.Stat(path)
	if err != nil || info.Size() < maximum {
		return
	}
	_ = os.Remove(fmt.Sprintf("%s.%d", path, keep))
	for index := keep - 1; index >= 1; index-- {
		_ = os.Rename(fmt.Sprintf("%s.%d", path, index), fmt.Sprintf("%s.%d", path, index+1))
	}
	_ = os.Rename(path, path+".1")
}

func (e *Engine) getCommandLog(args map[string]any) map[string]any {
	id := stringArg(args, "log_id", "")
	stream := stringArg(args, "stream", "stdout")
	if stream != "stdout" && stream != "stderr" && stream != "combined" {
		return failure("invalid_stream", "stream must be stdout, stderr, or combined")
	}
	path := filepath.Join(e.settings.CommandJobsDir, id+"."+stream)
	if stream == "combined" {
		path = filepath.Join(e.settings.CommandJobsDir, "logs", id+".combined")
	}
	start, end := intArg(args, "start_line", 1), intArg(args, "end_line", 0)
	if start < 1 {
		start = 1
	}
	var expression *regexp.Regexp
	if grep := optionalString(args, "grep"); grep != nil {
		compiled, compileErr := regexp.Compile(*grep)
		if compileErr != nil {
			return withError("invalid_regex", compileErr)
		}
		expression = compiled
	}
	content, first, last, total, truncated, err := boundedLogPage(path, start, end, e.settings.MaxResponseChars, expression)
	if err != nil {
		return withError("command_log_error", err)
	}
	next := last + 1
	if last == 0 {
		next = start
	}
	receipt := boundedOutputReceipt(truncated, 0, int64(len(content)))
	receipt["total"] = map[string]any{"lines": total}
	response := map[string]any{"ok": true, "log_id": id, "stream": stream, "content": content, "start_line": first, "end_line": last, "total_lines": total, "truncated": truncated, "next_line": next, "artifact": map[string]any{"artifact_id": id, "continuation_tool": "read_artifact"}, "receipt": receipt}
	if truncated {
		response["continuation"] = artifactContinuation(id)
	}
	return response
}

func (e *Engine) summarizeCommandLog(id, parser string) map[string]any {
	stdout, _, _, _, outTruncated, outErr := boundedLogPage(filepath.Join(e.settings.CommandJobsDir, id+".stdout"), 1, 0, e.settings.MaxResponseChars/2, nil)
	stderr, _, _, _, errTruncated, errErr := boundedLogPage(filepath.Join(e.settings.CommandJobsDir, id+".stderr"), 1, 0, e.settings.MaxResponseChars/2, nil)
	if outErr != nil && errErr != nil {
		return failure("command_log_error", "command log not found")
	}
	incomplete := outTruncated || errTruncated
	response := map[string]any{"ok": true, "log_id": id, "summary": parseCommandSummary("", stdout, stderr, parser), "summary_incomplete": incomplete, "artifact": map[string]any{"artifact_id": id, "continuation_tool": "read_artifact"}, "receipt": boundedOutputReceipt(incomplete, int64(len(stdout)+len(stderr)), int64(len(stdout)+len(stderr)))}
	if incomplete {
		response["continuation"] = artifactContinuation(id)
	}
	return response
}

func (e *Engine) scanPolicy(ctx context.Context, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		return withError("policy_scan_failed", err)
	}
	arguments := []string{"-C", repo, "diff", "--unified=0", stringArg(args, "base_ref", "HEAD"), "--"}
	arguments = append(arguments, stringSliceArg(args, "paths")...)
	process, id, stdoutBytes, stderrBytes, persistErr := e.runArtifactProcess(ctx, "scan_new_policy_violations", repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), e.settings.MaxResponseChars, "git", arguments...)
	artifact := map[string]any{"artifact_id": id, "continuation_tool": "read_artifact"}
	if persistErr != nil {
		if isHeavyBusyError(persistErr) {
			return e.heavyBusyResult()
		}
		return withError("policy_scan_failed", persistErr)
	}
	if process.ExitCode != 0 {
		return map[string]any{"ok": false, "error_kind": "policy_scan_failed", "error": process.Stderr, "artifact": artifact}
	}
	if stdoutBytes+stderrBytes > int64(len(process.Stdout)+len(process.Stderr)) {
		return map[string]any{"ok": false, "error_kind": "policy_scan_incomplete", "error": "policy diff exceeds inline safe parsing limit", "artifact": artifact, "receipt": boundedOutputReceipt(true, stdoutBytes+stderrBytes, int64(len(process.Stdout)+len(process.Stderr))), "continuation": artifactContinuation(id)}
	}
	output := process.Stdout
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
	for _, line := range strings.Split(output, "\n") {
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
	statusProcess := runProcess(ctx, repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), "git", "-C", repo, "status", "--porcelain")
	if statusProcess.ExitCode != 0 {
		return failure("worktree_guard_failed", statusProcess.Stderr)
	}
	status := statusProcess.Stdout
	allowed := stringSliceArg(args, "allowed_dirty_paths")
	var unexpected []string
	for _, line := range strings.Split(strings.TrimSpace(status), "\n") {
		if line == "" {
			continue
		}
		path := strings.TrimSpace(line[3:])
		if !containsString(allowed, path) {
			unexpected = append(unexpected, path)
		}
	}
	branchProcess := runProcess(ctx, repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), "git", "-C", repo, "branch", "--show-current")
	branch := strings.TrimSpace(branchProcess.Stdout)
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
	if added := runProcess(ctx, repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), "git", addArgs...); added.ExitCode != 0 {
		return map[string]any{"ok": false, "error_kind": "git_commit_failed", "error": strings.TrimSpace(added.Stdout + "\n" + added.Stderr)}
	}
	commitArgs := []string{"-C", repo, "commit", "-m", message, "--"}
	commitArgs = append(commitArgs, paths...)
	committed := runProcess(ctx, repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), "git", commitArgs...)
	if committed.ExitCode != 0 {
		return map[string]any{"ok": false, "error_kind": "git_commit_failed", "error": strings.TrimSpace(committed.Stdout + "\n" + committed.Stderr)}
	}
	sha := runProcess(ctx, repo, e.settings.SubprocessTimeout, e.commandEnvironment(nil), "git", "-C", repo, "rev-parse", "HEAD")
	return map[string]any{"ok": true, "dry_run": false, "applied": true, "commit": strings.TrimSpace(sha.Stdout), "output": strings.TrimSpace(committed.Stdout + "\n" + committed.Stderr), "paths": paths}
}

func redact(text string) string {
	result := text
	for _, pattern := range secretPatterns {
		result = pattern.ReplaceAllStringFunc(result, func(match string) string {
			if strings.Contains(strings.ToLower(match), "bearer ") {
				return "Bearer <redacted>"
			}
			if key, _, ok := strings.Cut(match, "="); ok {
				return key + "=<redacted>"
			}
			return "<redacted>"
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
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return fmt.Sprintf("00000000-0000-4000-8000-%012x", time.Now().UnixNano()&0xffffffffffff)
	}
	buffer[6] = (buffer[6] & 0x0f) | 0x40
	buffer[8] = (buffer[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", buffer[0:4], buffer[4:6], buffer[6:8], buffer[8:10], buffer[10:16])
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
