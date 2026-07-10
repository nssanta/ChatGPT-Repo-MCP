package tools

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestCommandPolicyMatrix(t *testing.T) {
	engine, _ := newTestEngine(t)
	engine.settings.CommandPolicyMode = "allowlist"
	if _, kind, _ := engine.checkCommandPolicy("git status --short", false, false); kind != "" {
		t.Fatal("expected allowed command")
	}
	if _, kind, _ := engine.checkCommandPolicy("git status --short | grep test", false, false); kind != "command_not_allowed" {
		t.Fatalf("expected shell operator rejection, got %q", kind)
	}
	if _, kind, _ := engine.checkCommandPolicy("git push origin main", false, false); kind != "command_not_allowed" {
		t.Fatalf("expected raw git push block, got %q", kind)
	}
	engine.settings.CommandPolicyMode = "full_repo"
	if _, kind, err := engine.checkCommandPolicy("git push origin main", false, false); err != nil || kind != "" {
		t.Fatalf("expected full_repo to allow raw push, kind=%q err=%v", kind, err)
	}
}

func TestRunCommandsAndJobLifecycle(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("command job lifecycle test is bash-specific")
	}
	engine, root := newTestEngine(t)
	engine.settings.CommandPolicyMode = "allowlist"
	engine.settings.CommandJobsDir = filepath.Join(root, "jobs")
	engine.settings.CommandAuditLogPath = filepath.Join(root, ".audit", "commands.log")
	engine.settings.CommandTimeout = 2 * time.Second
	defer os.RemoveAll(root)
	ctx := context.Background()

	started := engine.Execute(ctx, "run_commands", map[string]any{
		"stop_on_failure": true,
		"commands":        []any{"sudo true", "git status --short"},
	})
	if started["ok"].(bool) {
		t.Fatalf("run_commands should fail first command in allowlist mode: %#v", started)
	}
	if got := started["count"].(int); got != 1 {
		t.Fatalf("run_commands stopped unexpectedly: %#v", started)
	}

	started = engine.Execute(ctx, "run_commands", map[string]any{
		"commands":        []any{"sudo true", "git status --short"},
		"stop_on_failure": false,
	})
	if got := started["count"].(int); got != 2 {
		t.Fatalf("run_commands should continue after stop_on_failure=false: %#v", started)
	}

	engine.settings.CommandPolicyMode = "unrestricted"
	job := engine.Execute(ctx, "start_command_job", map[string]any{
		"command":         "sleep 0.6",
		"concurrency_key": "shared-lock",
		"on_conflict":     "fail",
	})
	if job["ok"] != true {
		t.Fatalf("start command job failed: %#v", job)
	}
	jobID := job["job_id"].(string)
	status := engine.getJob(jobID, 10, false)
	if status["status"].(string) != "running" {
		t.Fatalf("job should start running: %#v", status)
	}

	conflict := engine.Execute(ctx, "start_command_job", map[string]any{
		"command":         "sleep 0.1",
		"concurrency_key": "shared-lock",
		"on_conflict":     "fail",
	})
	if conflict["ok"] != false {
		t.Fatalf("expected conflict rejection: %#v", conflict)
	}
	if conflict["error_kind"].(string) != "job_lock_conflict" {
		t.Fatalf("unexpected conflict kind: %#v", conflict)
	}

	attached := engine.Execute(ctx, "start_command_job", map[string]any{
		"command":         "sleep 0.1",
		"concurrency_key": "shared-lock",
		"on_conflict":     "attach",
	})
	if attached["ok"] != true || attached["job_id"].(string) != jobID {
		t.Fatalf("attach should return existing job id: %#v", attached)
	}

	cancel := engine.Execute(ctx, "cancel_command_job", map[string]any{"job_id": jobID})
	if cancel["ok"] != true || cancel["cancelled"] != true {
		t.Fatalf("cancel should succeed: %#v", cancel)
	}
	status = engine.getJob(jobID, 20, true)
	if status["status"].(string) != "cancelled" {
		t.Fatalf("cancelled status expected: %#v", status)
	}
}

func TestCommandLogTools(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("command log tests are bash-specific")
	}
	engine, root := newTestEngine(t)
	engine.settings.CommandPolicyMode = "unrestricted"
	engine.settings.CommandJobsDir = filepath.Join(root, "jobs")
	engine.settings.CommandAuditLogPath = filepath.Join(root, ".audit", "commands.log")
	defer os.RemoveAll(root)
	ctx := context.Background()

	result := engine.Execute(ctx, "run_command", map[string]any{
		"command":          "printf 'alpha\\nbeta\\n'",
		"tail_lines":       20,
		"parse_kind":       "auto",
		"max_output_chars": 1000,
	})
	if result["ok"] != true {
		t.Fatalf("run command failed: %#v", result)
	}
	logID := result["log_id"].(string)
	log := engine.Execute(ctx, "get_command_log", map[string]any{
		"log_id":     logID,
		"stream":     "stdout",
		"start_line": 1,
		"end_line":   1,
		"grep":       "alpha",
	})
	if log["ok"] != true || log["content"].(string) != "alpha" {
		t.Fatalf("log mismatch: %#v", log)
	}
	summary := engine.Execute(ctx, "summarize_command_log", map[string]any{"log_id": logID, "parser": "generic"})
	if summary["ok"] != true {
		t.Fatalf("summary failed: %#v", summary)
	}

	bad := engine.Execute(ctx, "get_command_log", map[string]any{"log_id": logID, "stream": "invalid"})
	if bad["ok"] != false || bad["error_kind"].(string) != "invalid_stream" {
		t.Fatalf("expected invalid stream error: %#v", bad)
	}
}

func TestScanPolicyQualityGateWorkflow(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("git policy test is bash-specific")
	}
	engine, root := newTestEngine(t)
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module example.com/test\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Test")
	runGitTest(t, root, "add", "go.mod")
	runGitTest(t, root, "commit", "-m", "baseline")
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "main.go")
	runGitTest(t, root, "commit", "-m", "main")
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {\n\tpassword = \"secret\"\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	scan := engine.Execute(context.Background(), "scan_new_policy_violations", map[string]any{
		"repo":     "",
		"base_ref": "HEAD",
		"paths":    []any{"main.go"},
		"rules":    []any{"no_secret_like_literals"},
	})
	if scan["ok"] != false {
		t.Fatalf("policy scan should flag secret-like addition: %#v", scan)
	}

	gate := engine.Execute(context.Background(), "run_quality_gate", map[string]any{
		"name": "smoke",
		"checks": []any{
			map[string]any{"id": "cmd", "type": "command", "command": "printf 'ok'", "required": true},
			map[string]any{"id": "policy", "type": "policy", "repo": "", "base_ref": "HEAD", "rules": []any{"no_secret_like_literals"}, "required": true},
		},
		"stop_on_failure": true,
	})
	if gate["ok"] != false {
		t.Fatalf("expected quality gate failure because policy fails: %#v", gate)
	}

	commitDisabled := engine.Execute(context.Background(), "quality_gate_and_commit", map[string]any{
		"checks":                     []any{map[string]any{"type": "command", "command": "printf ok"}},
		"commit":                     map[string]any{"enabled": false},
		"require_clean_after_commit": false,
	})
	if commitDisabled["ok"] != true {
		t.Fatalf("commit skip path should still pass: %#v", commitDisabled)
	}
	if result, ok := commitDisabled["commit"].(map[string]any); !ok || result["skipped"] != true {
		t.Fatalf("commit expected skipped: %#v", commitDisabled)
	}
}
