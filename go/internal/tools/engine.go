// Package tools implements the language-independent ChatRepo MCP behavior.
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/security"
)

// Engine dispatches contract tools to focused implementations.
type Engine struct {
	settings     config.Settings
	perimeter    *security.Perimeter
	toolNames    []string
	jobsMu       sync.RWMutex
	jobs         map[string]*job
	terminalsMu  sync.RWMutex
	terminals    map[string]*terminalSession
	artifactOnce sync.Once
	artifacts    *artifactStore
	artifactErr  error
	heavySlots   chan struct{}
}

type heavyOperationLease struct {
	slots chan struct{}
	once  sync.Once
}

type heavyOperationBusyError struct{ capacity int }

func (err heavyOperationBusyError) Error() string {
	return fmt.Sprintf("resource_busy: maximum %d heavy operations are already running", err.capacity)
}

func (e *Engine) acquireHeavyOperation() (*heavyOperationLease, bool) {
	if e.heavySlots == nil {
		return &heavyOperationLease{}, true
	}
	select {
	case e.heavySlots <- struct{}{}:
		return &heavyOperationLease{slots: e.heavySlots}, true
	default:
		return nil, false
	}
}

func (lease *heavyOperationLease) Release() {
	if lease == nil || lease.slots == nil {
		return
	}
	lease.once.Do(func() { <-lease.slots })
}

func (e *Engine) heavyBusyResult() map[string]any {
	return map[string]any{"ok": false, "error_kind": "resource_busy", "error": fmt.Sprintf("maximum %d heavy operations are already running", e.settings.MaxHeavyOperations), "capacity": e.settings.MaxHeavyOperations, "retry_hint": "wait for a running operation to finish"}
}

func (e *Engine) heavyBusyError() error {
	return heavyOperationBusyError{capacity: e.settings.MaxHeavyOperations}
}

func isHeavyBusyError(err error) bool {
	_, ok := err.(heavyOperationBusyError)
	return ok
}

func (e *Engine) artifactStore() (*artifactStore, error) {
	e.artifactOnce.Do(func() {
		e.artifacts, e.artifactErr = newArtifactStore(e.settings.CommandJobsDir, e.settings.ArtifactTotalBytes, e.settings.ArtifactMaxBytes, e.settings.ArtifactTTL, e.settings.ArtifactDiskReserveBytes)
	})
	return e.artifacts, e.artifactErr
}

// New creates a tool engine. toolNames is used by doctor and smoke_all.
func New(settings config.Settings, toolNames []string) *Engine {
	names := append([]string(nil), toolNames...)
	sort.Strings(names)
	engine := &Engine{
		settings:  settings,
		perimeter: security.New(settings),
		toolNames: names,
		jobs:      make(map[string]*job),
		terminals: make(map[string]*terminalSession),
	}
	if settings.MaxHeavyOperations > 0 {
		engine.heavySlots = make(chan struct{}, settings.MaxHeavyOperations)
	}
	return engine
}

// ToolNames returns the registered runtime tool catalog for readiness output.
func (e *Engine) ToolNames() []string { return append([]string(nil), e.toolNames...) }

// Shutdown terminates every process group owned by this engine.
func (e *Engine) Shutdown() {
	e.jobsMu.RLock()
	jobIDs := make([]string, 0, len(e.jobs))
	for id := range e.jobs {
		jobIDs = append(jobIDs, id)
	}
	e.jobsMu.RUnlock()
	for _, id := range jobIDs {
		e.jobsMu.RLock()
		entry := e.jobs[id]
		e.jobsMu.RUnlock()
		if entry == nil {
			continue
		}
		entry.mu.RLock()
		running := entry.Status == "running" || entry.Status == "terminating" || entry.Status == "queued"
		entry.mu.RUnlock()
		if running {
			_ = e.cancelJob(id)
		}
	}
	e.terminalsMu.RLock()
	terminalIDs := make([]string, 0, len(e.terminals))
	for id := range e.terminals {
		terminalIDs = append(terminalIDs, id)
	}
	e.terminalsMu.RUnlock()
	for _, id := range terminalIDs {
		_ = e.closeTerminal(id, "SIGTERM", e.settings.KillGrace, false)
	}
}

// Execute invokes one public tool and returns structured JSON content.
func (e *Engine) Execute(ctx context.Context, name string, args map[string]any) map[string]any {
	var result map[string]any
	switch name {
	case "repo_info", "list_dir", "tree", "read_text_file", "read_multiple_files",
		"file_metadata", "find_files", "search_text", "symbol_search", "recent_changes",
		"todo_scan", "dependency_map", "list_repos", "doctor", "smoke_all",
		"context_bootstrap", "batch_call", "symbol_definition", "document_symbols",
		"workspace_symbols", "code_diagnostics":
		result = e.executeReadTool(ctx, name, args)
	case "write_text_file", "replace_text_in_file", "insert_text_in_file",
		"delete_text_in_file", "create_text_file", "move_path", "delete_path",
		"ensure_directory", "batch_edit_files", "apply_change_set", "replace_lines",
		"insert_before_line", "insert_after_line", "insert_before_heading",
		"insert_after_heading", "append_to_file", "apply_patch", "update_current_mission":
		result = e.executeEditTool(ctx, name, args)
	case "run_command", "run_commands", "run_test_preset", "list_test_presets",
		"read_artifact",
		"run_quality_gate", "quality_gate_and_commit", "scan_new_policy_violations",
		"command_policy_check", "get_command_log", "summarize_command_log",
		"git_worktree_guard", "start_command_job", "get_command_job", "get_job_status",
		"list_command_jobs", "cancel_command_job", "git_commit":
		result = e.executeCommandTool(ctx, name, args)
	case "git_status", "git_diff", "git_log", "git_show", "git_branches", "git_blame",
		"git_grep", "git_switch_branch", "git_create_branch", "git_add", "git_restore",
		"git_stash", "git_fetch", "git_pull", "git_push", "git_merge", "git_revert",
		"git_reset", "git_worktree_add", "git_worktree_list", "git_worktree_remove":
		result = e.executeGitTool(ctx, name, args)
	case "prepare_task_worktree":
		result = e.executeGitTool(ctx, name, args)
	case "gh_status", "gh_pr_create", "gh_pr_list", "gh_pr_view", "gh_pr_comment",
		"gh_pr_merge", "gh_checks", "gh_run_view", "gh_run_rerun", "gh_issue_list",
		"gh_issue_view":
		result = e.executeGitHubTool(ctx, name, args)
	case "start_terminal_session", "read_terminal_session", "write_terminal_session", "resize_terminal_session", "close_terminal_session", "list_terminal_sessions":
		result = e.executeTerminalTool(ctx, name, args)
	default:
		result = failure("unknown_tool", fmt.Sprintf("unknown tool: %s", name))
	}
	if result == nil {
		return failure("internal_error", "tool returned no structured result")
	}
	return result
}

func failure(kind, message string) map[string]any {
	return map[string]any{"ok": false, "error_kind": kind, "error": message}
}

func withError(kind string, err error) map[string]any {
	if err == nil {
		return failure(kind, "unknown error")
	}
	return failure(kind, err.Error())
}

func stringArg(args map[string]any, key, fallback string) string {
	value, ok := args[key]
	if !ok || value == nil {
		return fallback
	}
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func optionalString(args map[string]any, key string) *string {
	value, ok := args[key]
	if !ok || value == nil {
		return nil
	}
	text := fmt.Sprint(value)
	return &text
}

func boolArg(args map[string]any, key string, fallback bool) bool {
	value, ok := args[key]
	if !ok || value == nil {
		return fallback
	}
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		parsed, err := strconv.ParseBool(typed)
		return err == nil && parsed
	default:
		return fallback
	}
}

func optionalBool(args map[string]any, key string) *bool {
	value, ok := args[key]
	if !ok || value == nil {
		return nil
	}
	parsed := boolArg(args, key, false)
	return &parsed
}

func intArg(args map[string]any, key string, fallback int) int {
	value, ok := args[key]
	if !ok || value == nil {
		return fallback
	}
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case json.Number:
		parsed, err := typed.Int64()
		if err == nil {
			return int(parsed)
		}
	case string:
		parsed, err := strconv.Atoi(typed)
		if err == nil {
			return parsed
		}
	}
	return fallback
}

func stringSliceArg(args map[string]any, key string) []string {
	value, ok := args[key]
	if !ok || value == nil {
		return nil
	}
	switch typed := value.(type) {
	case []string:
		return append([]string(nil), typed...)
	case []any:
		result := make([]string, 0, len(typed))
		for _, item := range typed {
			if item != nil {
				result = append(result, fmt.Sprint(item))
			}
		}
		return result
	default:
		return []string{fmt.Sprint(typed)}
	}
}

func mapsArg(args map[string]any, key string) []map[string]any {
	value, ok := args[key]
	if !ok || value == nil {
		return nil
	}
	if mapped, ok := value.([]map[string]any); ok {
		return mapped
	}
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	result := make([]map[string]any, 0, len(items))
	for _, item := range items {
		if mapped, ok := item.(map[string]any); ok {
			result = append(result, mapped)
		}
	}
	return result
}

func mapArg(args map[string]any, key string) map[string]any {
	if value, ok := args[key].(map[string]any); ok {
		return value
	}
	return nil
}

func capText(text string, limit int) (string, bool) {
	if limit <= 0 || len(text) <= limit {
		return text, false
	}
	return headTailText(text, limit), true
}

const inlineOmissionMarker = "\n… [output omitted; read artifact] …\n"

// headTailText preserves useful context from both ends while ensuring the
// inline response never exceeds its caller's byte budget.
func headTailText(text string, limit int) string {
	if limit <= 0 || len(text) <= limit {
		return text
	}
	if limit <= len(inlineOmissionMarker) {
		return utf8Prefix(text, limit)
	}
	remaining := limit - len(inlineOmissionMarker)
	head := utf8Prefix(text, remaining/2)
	tail := utf8Suffix(text, remaining-len(head))
	return head + inlineOmissionMarker + tail
}

func utf8Prefix(text string, limit int) string {
	if limit <= 0 {
		return ""
	}
	if len(text) <= limit {
		return text
	}
	end := limit
	for end > 0 && !utf8.RuneStart(text[end]) {
		end--
	}
	return text[:end]
}

func utf8Suffix(text string, limit int) string {
	if limit <= 0 {
		return ""
	}
	if len(text) <= limit {
		return text
	}
	start := len(text) - limit
	for start < len(text) && !utf8.RuneStart(text[start]) {
		start++
	}
	return text[start:]
}

func binaryStatus(name string) map[string]any {
	path, err := exec.LookPath(name)
	if err != nil {
		return map[string]any{"available": false}
	}
	return map[string]any{"available": true, "path": path}
}

func bashBinary() string {
	if runtime.GOOS != "windows" {
		if path, err := exec.LookPath("bash"); err == nil {
			return path
		}
		return "/bin/bash"
	}
	for _, name := range []string{"bash.exe", "bash"} {
		if path, err := exec.LookPath(name); err == nil {
			return path
		}
	}
	return ""
}

func containsString(values []string, wanted string) bool {
	for _, value := range values {
		if value == wanted {
			return true
		}
	}
	return false
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
