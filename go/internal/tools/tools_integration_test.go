package tools

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestRunTestPresetAndCommandRequestBounds(t *testing.T) {
	if runtime.GOOS == "windows" {
		return
	}
	engine, root := newTestEngine(t)
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module fixture\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	request := engine.commandRequestFromArgs(map[string]any{
		"command":          "printf test",
		"timeout_ms":       -1,
		"max_output_chars": 0,
	}, false)
	if request.Timeout != engine.settings.CommandTimeout {
		t.Fatalf("expected timeout default, got %v", request.Timeout)
	}
	if request.MaxOutput != engine.settings.MaxCommandOutputChars {
		t.Fatalf("expected max output clamp, got %v", request.MaxOutput)
	}

	list := engine.Execute(context.Background(), "list_test_presets", map[string]any{})
	if list["ok"] != true {
		t.Fatalf("list presets failed: %#v", list)
	}
	presets, ok := list["presets"].(map[string]any)
	if !ok || len(presets) == 0 {
		t.Fatalf("expected presets map: %#v", list)
	}
	if _, exists := presets["test"]; !exists {
		t.Fatalf("test preset should exist: %#v", presets)
	}

	unknown := engine.Execute(context.Background(), "run_test_preset", map[string]any{"preset": "missing"})
	if unknown["ok"] != false || unknown["error_kind"] != "unknown_preset" {
		t.Fatalf("expected unknown_preset, got %#v", unknown)
	}

	engine.settings.CommandPolicyMode = "unrestricted"
	foreground := engine.Execute(context.Background(), "run_test_preset", map[string]any{"preset": "test", "cwd": "."})
	if foreground["ok"] != true {
		t.Fatalf("run_test_preset should execute: %#v", foreground)
	}

	background := engine.Execute(context.Background(), "run_test_preset", map[string]any{
		"preset":          "test",
		"cwd":             ".",
		"background":      true,
		"on_conflict":     "fail",
		"concurrency_key": "fixture-test",
	})
	if background["ok"] != true {
		t.Fatalf("background preset should start: %#v", background)
	}
	jobID := background["job_id"].(string)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		job := engine.getJob(jobID, 20, true)
		status, _ := job["status"].(string)
		if status == "completed" || status == "failed" || status == "timed_out" {
			break
		}
		if status != "running" {
			t.Fatalf("unexpected job status: %#v", job)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestMoveDeleteInsertAndApplyPatch(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false
	engine.settings.AllowMoveDeleteOperations = true

	if err := os.WriteFile(filepath.Join(root, "source.txt"), []byte("alpha\nalpha\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "target.txt"), []byte("target\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	dryMove := engine.Execute(context.Background(), "move_path", map[string]any{
		"source_path":      "source.txt",
		"destination_path": "moved.txt",
		"dry_run":          true,
	})
	if dryMove["ok"] != true || dryMove["applied"].(bool) != false {
		t.Fatalf("dry move should be preview only: %#v", dryMove)
	}
	if _, err := os.Stat(filepath.Join(root, "source.txt")); err != nil {
		t.Fatalf("source should remain in dry run: %v", err)
	}

	exists := engine.Execute(context.Background(), "move_path", map[string]any{
		"source_path":      "moved.txt",
		"destination_path": "target.txt",
		"overwrite":        false,
	})
	if exists["error_kind"] != "already_exists" {
		t.Fatalf("expected already_exists on existing destination: %#v", exists)
	}

	moved := engine.Execute(context.Background(), "move_path", map[string]any{
		"source_path":      "moved.txt",
		"destination_path": "target.txt",
		"overwrite":        true,
	})
	if moved["ok"] != true {
		t.Fatalf("overwrite move failed: %#v", moved)
	}
	if fileExists(filepath.Join(root, "moved.txt")) {
		t.Fatalf("moved source should be renamed")
	}

	dryDelete := engine.Execute(context.Background(), "delete_path", map[string]any{
		"path":    "target.txt",
		"dry_run": true,
	})
	if dryDelete["ok"] != true || dryDelete["applied"].(bool) != false {
		t.Fatalf("dry delete should not remove file: %#v", dryDelete)
	}
	if _, err := os.Stat(filepath.Join(root, "target.txt")); err != nil {
		t.Fatalf("dry delete should keep file: %v", err)
	}
	deleted := engine.Execute(context.Background(), "delete_path", map[string]any{"path": "target.txt"})
	if deleted["ok"] != true || deleted["changed"].(bool) != true {
		t.Fatalf("delete should remove file: %#v", deleted)
	}

	if err := os.WriteFile(filepath.Join(root, "insert.txt"), []byte("first\nanchor\nthird\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	insert := engine.Execute(context.Background(), "insert_text_in_file", map[string]any{
		"path":     "insert.txt",
		"anchor":   "anchor",
		"position": "after",
		"content":  "inserted\n",
	})
	if insert["ok"] != true {
		t.Fatalf("insert should succeed: %#v", insert)
	}
	invalid := engine.Execute(context.Background(), "insert_text_in_file", map[string]any{
		"path":     "insert.txt",
		"anchor":   "anchor",
		"position": "mid",
		"content":  "x",
	})
	if invalid["error_kind"] != "invalid_position" {
		t.Fatalf("invalid position should fail: %#v", invalid)
	}
	if err := os.WriteFile(filepath.Join(root, "headings.txt"), []byte("## Goal\n\n## Goal\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	heading := engine.Execute(context.Background(), "insert_after_heading", map[string]any{
		"path":    "headings.txt",
		"heading": "## Goal",
		"content": "x\n",
	})
	if heading["error_kind"] != "heading_not_unique" {
		t.Fatalf("duplicate heading should be rejected: %#v", heading)
	}
	missing := engine.Execute(context.Background(), "insert_before_heading", map[string]any{
		"path":    "headings.txt",
		"heading": "## Missing",
		"content": "x\n",
	})
	if missing["error_kind"] != "heading_not_found" {
		t.Fatalf("missing heading should fail: %#v", missing)
	}

	if err := os.WriteFile(filepath.Join(root, "git.txt"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")
	runGitTest(t, root, "add", "git.txt")
	runGitTest(t, root, "commit", "-m", "init")
	headOutput, err := exec.Command("git", "-C", root, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatal(err)
	}
	head := strings.TrimSpace(string(headOutput))

	engine.settings.AccessMode = "full"
	stale := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":              ".",
		"expected_base_sha": "deadbeef",
		"patch": `diff --git a/git.txt b/git.txt
index 0000000..1111111 100644
--- a/git.txt
+++ b/git.txt
@@ -1 +1 @@
-one
+two
`,
	})
	if stale["error_kind"] != "stale_base" {
		t.Fatalf("expected stale_base: %#v", stale)
	}

	dry := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":              ".",
		"expected_base_sha": head,
		"dry_run":           true,
		"patch": `diff --git a/git.txt b/git.txt
index 0000000..1111111 100644
--- a/git.txt
+++ b/git.txt
@@ -1 +1 @@
-one
+two
`,
	})
	if dry["ok"] != true || dry["applied"].(bool) != false {
		t.Fatalf("dry patch should not apply: %#v", dry)
	}

	applied := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":              ".",
		"expected_base_sha": head,
		"patch": `diff --git a/git.txt b/git.txt
index 0000000..1111111 100644
--- a/git.txt
+++ b/git.txt
@@ -1 +1 @@
-one
+two
`,
	})
	if applied["ok"] != true || applied["applied"].(bool) != true {
		t.Fatalf("patch should apply: %#v", applied)
	}
	if patched, err := os.ReadFile(filepath.Join(root, "git.txt")); err != nil || string(patched) != "two\n" {
		t.Fatalf("unexpected patched content: %q, err=%v", patched, err)
	}
	if empty := engine.Execute(context.Background(), "apply_patch", map[string]any{"repo": ".", "patch": ""}); empty["error_kind"] != "invalid_patch" {
		t.Fatalf("empty patch must be rejected: %#v", empty)
	}
}

func TestGitHelpersCoverage(t *testing.T) {
	engine, root := newTestEngine(t)

	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")
	if err := os.WriteFile(filepath.Join(root, "base.go"), []byte("package main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "base.go")
	runGitTest(t, root, "commit", "-m", "base")

	result := engine.Execute(context.Background(), "git_grep", map[string]any{"query": "missing-string", "pathspec": "base.go"})
	if result["ok"] != true || result["count"].(int) != 0 {
		t.Fatalf("git_grep should return no matches: %#v", result)
	}

	if err := os.WriteFile(filepath.Join(root, "base.go"), []byte("package main\nvar x = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	switched := engine.Execute(context.Background(), "git_switch_branch", map[string]any{"branch": "feature"})
	if switched["error_kind"] != "dirty_worktree" {
		t.Fatalf("expected dirty_worktree: %#v", switched)
	}

	invalid := engine.Execute(context.Background(), "git_create_branch", map[string]any{"branch": "bad branch"})
	if invalid["error_kind"] != "invalid_branch" {
		t.Fatalf("expected invalid_branch: %#v", invalid)
	}
}

func TestFilesystemDiagnosticsAndSearch(t *testing.T) {
	engine, root := newTestEngine(t)
	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("hello\nHello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module fixture\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "package.json"), []byte(`{"dependencies":{"left-pad":"1.3.0"}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "requirements.txt"), []byte("requests==2.31.0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "AGENTS.md"), []byte("# agents\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	fallback := engine.searchFallback("Hello", []string{root}, false, true, 10)
	if fallback["engine"] != "go-fallback" {
		t.Fatalf("searchFallback should be selected with direct helper: %#v", fallback)
	}
	if fallback["count"].(int) == 0 {
		t.Fatalf("fallback should find line: %#v", fallback)
	}
	deps := engine.dependencyMap(".")
	if deps["ok"] != true {
		t.Fatalf("dependency map failed: %#v", deps)
	}
	stackDependencies, ok := deps["dependencies"].(map[string]any)
	if !ok || len(stackDependencies) == 0 {
		t.Fatalf("dependencies map empty: %#v", deps)
	}

	bootstrap := engine.contextBootstrap(context.Background())
	if bootstrap["ok"] != true {
		t.Fatalf("context bootstrap failed: %#v", bootstrap)
	}
	if _, ok := bootstrap["context_file"].(map[string]any); !ok {
		t.Fatalf("expected context file metadata: %#v", bootstrap)
	}

	calls := []map[string]any{
		{"tool": "read_text_file", "args": map[string]any{"path": "notes.txt", "with_line_numbers": false}},
		{"tool": "file_metadata", "args": map[string]any{"path": "notes.txt", "include_stat": true}},
	}
	batch := engine.batchCall(context.Background(), calls)
	if batch["ok"] != true || batch["count"].(int) != 2 {
		t.Fatalf("batch call should succeed: %#v", batch)
	}
	batchResult := batch["results"].([]map[string]any)
	if len(batchResult) != 2 || batchResult[0]["result"].(map[string]any)["ok"] != true {
		t.Fatalf("batch first result should succeed: %#v", batch)
	}
	if batchResult[1]["result"].(map[string]any)["ok"] != true {
		t.Fatalf("batch second result should succeed: %#v", batch)
	}

	recent := engine.recentChanges(context.Background(), ".", nil, 3)
	if recent["ok"] != true {
		t.Fatalf("recent changes call failed: %#v", recent)
	}

	originalPath := os.Getenv("PATH")
	privatePath := t.TempDir()
	t.Setenv("PATH", privatePath)
	diagPy := engine.codeDiagnostics(context.Background(), map[string]any{"language": "python"})
	t.Setenv("PATH", originalPath)
	if diagPy["ok"] != true {
		t.Fatalf("python diagnostics should still be ok: %#v", diagPy)
	}
	missingTools, _ := diagPy["missing_tools"].([]map[string]any)
	if len(missingTools) == 0 {
		t.Fatalf("expected forced missing tools for python: %#v", diagPy)
	}

	if _, err := exec.LookPath("go"); err == nil {
		if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		diagGo := engine.codeDiagnostics(context.Background(), map[string]any{"language": "go", "repo": "."})
		if diagGo["ok"] != true {
			t.Fatalf("go diagnostics should be ok: %#v", diagGo)
		}
		checks, ok := diagGo["checks"].([]map[string]any)
		if !ok || len(checks) == 0 {
			t.Fatalf("go diagnostics should include checks: %#v", diagGo)
		}
	}
}

func TestGitHubToolModesAndFakeBinary(t *testing.T) {
	if runtime.GOOS == "windows" {
		return
	}
	engine, root := newTestEngine(t)
	ctx := context.Background()
	runGitTest(t, root, "init", "-b", "main")
	if output, err := exec.Command("git", "-C", root, "rev-parse", "--show-toplevel").CombinedOutput(); err != nil {
		t.Fatalf("git repository check failed: %v, out=%s", err, string(output))
	}
	if repoTop, err := engine.resolveRepo(ctx, ""); err != nil {
		t.Fatalf("resolveRepo failed before gh tools: %v", err)
	} else if repoTop == "" {
		t.Fatalf("resolveRepo returned empty toplevel")
	}

	disabled := engine.Execute(ctx, "gh_status", map[string]any{})
	if disabled["ok"] != false || disabled["error_kind"] != "github_tools_disabled" {
		t.Fatalf("github should be disabled in default settings: %#v", disabled)
	}

	originalPath := os.Getenv("PATH")
	engine.settings.GitHubToolsEnabled = true
	t.Setenv("PATH", t.TempDir())
	unavailable := engine.Execute(ctx, "gh_status", map[string]any{})
	if unavailable["error_kind"] != "gh_unavailable" {
		t.Fatalf("expected gh_unavailable, got %#v", unavailable)
	}

	scriptDir := t.TempDir()
	installFakeGitHubCLI(t, scriptDir)
	t.Setenv("PATH", scriptDir+string(os.PathListSeparator)+originalPath)

	status := engine.Execute(ctx, "gh_status", map[string]any{})
	if status["ok"] != true || status["authenticated"].(bool) != true {
		t.Fatalf("gh_status should be ok: %#v", status)
	}
	payload, _ := json.Marshal(status)
	if !strings.Contains(string(payload), "authenticated") {
		t.Fatalf("unexpected gh_status payload: %#v", status)
	}
	if rateData, ok := status["rate_limit"].(map[string]any); !ok || rateData["resources"] == nil {
		t.Fatalf("rate limit payload should be parsed JSON: %#v", status)
	}

	list := engine.Execute(ctx, "gh_pr_list", map[string]any{})
	if list["ok"] != true {
		t.Fatalf("gh_pr_list should parse json: %#v", list)
	}
	rows, _ := list["data"].([]any)
	if len(rows) != 1 {
		t.Fatalf("expected one PR row: %#v", list)
	}

	invalidJSON := engine.Execute(ctx, "gh_pr_view", map[string]any{"number": 1})
	if invalidJSON["ok"] != false || invalidJSON["error_kind"] != "gh_invalid_json" {
		t.Fatalf("invalid json should fail in runGHJSON: %#v", invalidJSON)
	}
}

func installFakeGitHubCLI(t *testing.T, directory string) {
	t.Helper()
	script := filepath.Join(directory, "gh")
	payload := `#!/bin/sh
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  echo "Logged in to github.com"
  exit 0
fi
if [ "$1" = "api" ] && [ "$2" = "rate_limit" ]; then
  echo '{"resources":{"core":{"remaining":4999}}}'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then
  echo '[{"number":1,"title":"demo","state":"open","isDraft":false,"headRefName":"feature","baseRefName":"main","url":"http://example","author":"test","updatedAt":"now"}]'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo 'not json'
  exit 0
fi
exit 1
`
	if err := os.WriteFile(script, []byte(payload), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(script, 0o755); err != nil {
		t.Fatal(err)
	}
}
