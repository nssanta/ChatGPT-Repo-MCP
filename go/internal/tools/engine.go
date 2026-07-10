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

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/security"
)

// Engine dispatches contract tools to focused implementations.
type Engine struct {
	settings  config.Settings
	perimeter *security.Perimeter
	toolNames []string
	jobsMu    sync.RWMutex
	jobs      map[string]*job
}

// New creates a tool engine. toolNames is used by doctor and smoke_all.
func New(settings config.Settings, toolNames []string) *Engine {
	names := append([]string(nil), toolNames...)
	sort.Strings(names)
	return &Engine{
		settings:  settings,
		perimeter: security.New(settings),
		toolNames: names,
		jobs:      make(map[string]*job),
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
		"run_quality_gate", "quality_gate_and_commit", "scan_new_policy_violations",
		"command_policy_check", "get_command_log", "summarize_command_log",
		"git_worktree_guard", "start_command_job", "get_command_job", "get_job_status",
		"cancel_command_job", "git_commit":
		result = e.executeCommandTool(ctx, name, args)
	case "git_status", "git_diff", "git_log", "git_show", "git_branches", "git_blame",
		"git_grep", "git_switch_branch", "git_create_branch", "git_add", "git_restore",
		"git_stash", "git_fetch", "git_pull", "git_push", "git_merge", "git_revert",
		"git_reset", "git_worktree_add", "git_worktree_list", "git_worktree_remove":
		result = e.executeGitTool(ctx, name, args)
	case "gh_status", "gh_pr_create", "gh_pr_list", "gh_pr_view", "gh_pr_comment",
		"gh_pr_merge", "gh_checks", "gh_run_view", "gh_run_rerun", "gh_issue_list",
		"gh_issue_view":
		result = e.executeGitHubTool(ctx, name, args)
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
	return text[:limit] + "\n...[truncated]", true
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
