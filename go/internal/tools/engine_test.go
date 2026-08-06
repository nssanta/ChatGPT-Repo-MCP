package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/contracts"
)

func testSettings(root string) config.Settings {
	return config.Settings{
		AppName: "chatrepo-mcp", Host: "127.0.0.1", Port: 8000, Transport: "streamable-http",
		ProjectRoot: root, WorkspaceScanDepth: 2, AccessMode: "safe",
		BlockedGlobs: []string{".env", "**/.git/**", "**/*.bin"}, SecretGlobs: []string{".env", "**/.git/**"},
		BinaryGlobs: []string{"**/*.bin"}, WritableGlobs: []string{"**/*"}, DangerouslyAllowAllWrites: true,
		MaxFileBytes: 1_000_000, MaxResponseChars: 100_000, MaxReadFiles: 25, MaxSearchResults: 100,
		MaxTreeEntries: 1000, MaxDiffBytes: 100_000, MaxLogCommits: 100, MaxWriteFileBytes: 1_000_000,
		MaxBatchOperations: 50, MaxCombinedDiffChars: 100_000, MaxPatchBytes: 100_000,
		DefaultInlineOutputBytes: 64 * 1024,
		MaxCommandOutputChars:    100_000, CommandTimeout: 5 * time.Second, SubprocessTimeout: 5 * time.Second,
		GitNetworkTimeout: 5 * time.Second, GHTimeout: 5 * time.Second,
		CommandAuditLogPath: filepath.Join(root, ".audit", "commands.log"), CommandJobsDir: filepath.Join(root, ".jobs"),
		CommandPolicyMode: "allowlist", DeniedWords: []string{"sudo", "su"},
		DestructiveWords: []string{"rm -rf", "git reset --hard"}, ProtectedBranches: []string{"main", "master"},
		GitHubToolsEnabled: false, AllowedHosts: []string{"127.0.0.1", "localhost"}, CanonicalNamespace: "/test",
	}
}

func newTestEngine(t *testing.T) (*Engine, string) {
	t.Helper()
	root := t.TempDir()
	document, err := contracts.Load()
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(document.Tools))
	for _, tool := range document.Tools {
		names = append(names, tool.Name)
	}
	return New(testSettings(root), names), root
}

func TestFilesystemAndEditRoundTrip(t *testing.T) {
	engine, root := newTestEngine(t)
	path := filepath.Join(root, "docs", "note.md")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("# Note\nold\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	read := engine.Execute(context.Background(), "read_text_file", map[string]any{"path": "docs/note.md", "with_line_numbers": false})
	if read["ok"] != true || !strings.Contains(read["content"].(string), "old") {
		t.Fatalf("read result: %#v", read)
	}
	hash := read["sha256"].(string)
	preview := engine.Execute(context.Background(), "replace_text_in_file", map[string]any{"path": "docs/note.md", "find": "old", "replace": "new", "expected_sha256": hash})
	if preview["ok"] != true || preview["applied"] != false {
		t.Fatalf("preview: %#v", preview)
	}
	applied := engine.Execute(context.Background(), "replace_text_in_file", map[string]any{"path": "docs/note.md", "find": "old", "replace": "new", "expected_sha256": hash, "dry_run": false})
	if applied["ok"] != true || applied["applied"] != true {
		t.Fatalf("apply: %#v", applied)
	}
	content, _ := os.ReadFile(path)
	if string(content) != "# Note\nnew\n" {
		t.Fatalf("content = %q", content)
	}
	stale := engine.Execute(context.Background(), "append_to_file", map[string]any{"path": "docs/note.md", "content": "x", "expected_sha256": hash, "dry_run": false})
	if stale["error_kind"] != "stale_write" {
		t.Fatalf("stale result: %#v", stale)
	}
}

func TestLineHeadingBatchAndStructuralEdits(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false
	engine.settings.AllowMoveDeleteOperations = true
	if err := os.WriteFile(filepath.Join(root, "a.md"), []byte("# A\none\ntwo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, call := range []struct {
		name string
		args map[string]any
	}{
		{"insert_after_heading", map[string]any{"path": "a.md", "heading": "# A", "content": "intro\n", "dry_run": false}},
		{"replace_lines", map[string]any{"path": "a.md", "start_line": 3, "end_line": 3, "replacement": "ONE", "dry_run": false}},
		{"append_to_file", map[string]any{"path": "a.md", "content": "tail\n", "dry_run": false}},
	} {
		if result := engine.Execute(context.Background(), call.name, call.args); result["ok"] != true {
			t.Fatalf("%s: %#v", call.name, result)
		}
	}
	batch := engine.Execute(context.Background(), "batch_edit_files", map[string]any{
		"dry_run": false, "atomic": true,
		"operations": []any{
			map[string]any{"operation": "create", "path": "b.txt", "content": "b"},
			map[string]any{"operation": "replace", "path": "missing", "find": "x", "replace": "y"},
		},
	})
	if batch["ok"] != false || batch["rolled_back"] != true || fileExists(filepath.Join(root, "b.txt")) {
		t.Fatalf("atomic batch: %#v", batch)
	}
	move := engine.Execute(context.Background(), "move_path", map[string]any{"source_path": "a.md", "destination_path": "moved.md", "dry_run": false})
	if move["ok"] != true {
		t.Fatalf("move: %#v", move)
	}
	deleted := engine.Execute(context.Background(), "delete_path", map[string]any{"path": "moved.md", "dry_run": false})
	if deleted["ok"] != true || fileExists(filepath.Join(root, "moved.md")) {
		t.Fatalf("delete: %#v", deleted)
	}
}

func TestCommandPolicyRunnerAndJobs(t *testing.T) {
	engine, _ := newTestEngine(t)
	allowed := engine.Execute(context.Background(), "command_policy_check", map[string]any{"command": "git status --short"})
	if allowed["allowed"] != true {
		t.Fatalf("policy: %#v", allowed)
	}
	denied := engine.Execute(context.Background(), "run_command", map[string]any{"command": "sudo true"})
	if denied["ok"] != false {
		t.Fatalf("denied: %#v", denied)
	}
	engine.settings.CommandPolicyMode = "unrestricted"
	run := engine.Execute(context.Background(), "run_command", map[string]any{"command": "printf '2 passed'", "tail_lines": 10})
	if run["ok"] != true || !strings.Contains(run["stdout"].(string), "2 passed") {
		t.Fatalf("run: %#v", run)
	}
	started := engine.Execute(context.Background(), "start_command_job", map[string]any{"command": "printf done", "tail_lines": 10})
	if started["ok"] != true {
		t.Fatalf("start: %#v", started)
	}
	id := started["job_id"].(string)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		status := engine.Execute(context.Background(), "get_command_job", map[string]any{"job_id": id})
		if status["status"] != "running" {
			if status["status"] != "completed" {
				t.Fatalf("job: %#v", status)
			}
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("job did not complete")
}

func TestGitReadAndWorkflow(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false
	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Test")
	if err := os.WriteFile(filepath.Join(root, "file.txt"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "file.txt")
	runGitTest(t, root, "commit", "-m", "initial")
	for _, name := range []string{"git_status", "git_log", "git_branches", "git_show", "repo_info"} {
		result := engine.Execute(context.Background(), name, map[string]any{})
		if result["ok"] != true {
			t.Fatalf("%s: %#v", name, result)
		}
	}
	branch := engine.Execute(context.Background(), "git_create_branch", map[string]any{"branch": "feature", "checkout": true})
	if branch["ok"] != true {
		t.Fatalf("create branch: %#v", branch)
	}
	if err := os.WriteFile(filepath.Join(root, "file.txt"), []byte("two\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	commit := engine.Execute(context.Background(), "git_commit", map[string]any{"message": "change", "paths": []any{"file.txt"}, "dry_run": false})
	if commit["ok"] != true {
		t.Fatalf("commit: %#v", commit)
	}
}

func TestAllContractToolsHaveDispatcherRoute(t *testing.T) {
	engine, _ := newTestEngine(t)
	document, _ := contracts.Load()
	for _, tool := range document.Tools {
		result := engine.Execute(context.Background(), tool.Name, map[string]any{})
		if result["error_kind"] == "unknown_tool" || result["error_kind"] == "unknown_read_tool" || result["error_kind"] == "unknown_edit_tool" || result["error_kind"] == "unknown_command_tool" || result["error_kind"] == "unknown_git_tool" || result["error_kind"] == "unknown_github_tool" {
			t.Errorf("%s has no dispatcher route: %#v", tool.Name, result)
		}
	}
}

func TestResourceBufferDTOIsExplicitlyDiagnosticOnly(t *testing.T) {
	engine, _ := newTestEngine(t)
	engine.settings.ResourceBufferBytes = 32 * 1024 * 1024
	repo := engine.Execute(context.Background(), "repo_info", map[string]any{})
	configResult := repo["config"].(map[string]any)
	if configResult["resource_buffer_enforced"] != false || configResult["resource_buffer_semantics"] != "diagnostic_estimate_only" {
		t.Fatalf("repo_info buffer truth: %#v", configResult)
	}
	doctor := engine.Execute(context.Background(), "doctor", map[string]any{})
	limits := doctor["resource_limits"].(map[string]any)
	if limits["buffer_enforced"] != false || limits["buffer_semantics"] != "diagnostic_estimate_only" {
		t.Fatalf("doctor buffer truth: %#v", limits)
	}
}

func TestSearchMetadataDependencyAndSymbols(t *testing.T) {
	engine, root := newTestEngine(t)
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module example.com/test\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc Hello() {} // TODO\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, call := range []struct {
		name string
		args map[string]any
	}{
		{"list_dir", map[string]any{}}, {"tree", map[string]any{"depth": 2}},
		{"find_files", map[string]any{"pattern": "*.go"}}, {"search_text", map[string]any{"query": "Hello"}},
		{"todo_scan", map[string]any{}}, {"dependency_map", map[string]any{}},
		{"document_symbols", map[string]any{"path": "main.go"}}, {"workspace_symbols", map[string]any{"query": "Hello"}},
	} {
		result := engine.Execute(context.Background(), call.name, call.args)
		if result["ok"] != true {
			t.Fatalf("%s: %#v", call.name, result)
		}
	}
	data, _ := os.ReadFile(filepath.Join(root, "main.go"))
	digest := sha256.Sum256(data)
	if metadata := engine.Execute(context.Background(), "file_metadata", map[string]any{"path": "main.go"}); metadata["sha256"] != hex.EncodeToString(digest[:]) {
		t.Fatalf("metadata: %#v", metadata)
	}
}

func runGitTest(t *testing.T, directory string, arguments ...string) {
	t.Helper()
	command := exec.Command("git", append([]string{"-C", directory}, arguments...)...)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", arguments, err, output)
	}
}
