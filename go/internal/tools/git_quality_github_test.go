package tools

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestGitToolScenarios(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("git worktree operations are unstable on windows")
	}

	engine, root := newTestEngine(t)
	ctx := context.Background()
	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")

	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "main.go")
	runGitTest(t, root, "commit", "-m", "base")

	addEmpty := engine.Execute(ctx, "git_add", map[string]any{})
	if addEmpty["error_kind"] != "git_add_rejected" {
		t.Fatalf("expected git_add_rejected: %#v", addEmpty)
	}
	addBlank := engine.Execute(ctx, "git_add", map[string]any{"paths": []any{"."}})
	if addBlank["error_kind"] != "git_add_rejected" {
		t.Fatalf("expected blanket path rejection: %#v", addBlank)
	}

	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	restoreDryRun := engine.Execute(ctx, "git_restore", map[string]any{"paths": []any{"main.go"}})
	if restoreDryRun["error_kind"] != "confirmation_required" {
		t.Fatalf("expected confirmation required for restore: %#v", restoreDryRun)
	}
	restore := engine.Execute(ctx, "git_restore", map[string]any{"paths": []any{"main.go"}, "confirmed": true, "source": "HEAD"})
	if restore["ok"] != true {
		t.Fatalf("restore should apply: %#v", restore)
	}

	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	addDry := engine.Execute(ctx, "git_add", map[string]any{"paths": []any{"notes.txt"}, "dry_run": true})
	if addDry["ok"] != true || addDry["applied"].(bool) != false {
		t.Fatalf("dry run add should be preview: %#v", addDry)
	}
	addReal := engine.Execute(ctx, "git_add", map[string]any{"paths": []any{"notes.txt"}, "dry_run": false})
	if addReal["ok"] != true || addReal["applied"].(bool) != true {
		t.Fatalf("add should apply: %#v", addReal)
	}

	popStash := engine.Execute(ctx, "git_stash", map[string]any{"action": "pop"})
	if popStash["error_kind"] != "confirmation_required" {
		t.Fatalf("stash pop should require confirmation: %#v", popStash)
	}
	listStash := engine.Execute(ctx, "git_stash", map[string]any{"action": "list"})
	if listStash["ok"] != true {
		t.Fatalf("stash list failed: %#v", listStash)
	}

	remote := filepath.Join(root, "origin.git")
	runGitTest(t, root, "init", "--bare", "origin.git")
	runGitTest(t, root, "remote", "add", "origin", remote)
	runGitTest(t, root, "push", "-u", "origin", "main")

	fetch := engine.Execute(ctx, "git_fetch", map[string]any{})
	if fetch["ok"] != true {
		t.Fatalf("fetch should succeed: %#v", fetch)
	}
	fetchAll := engine.Execute(ctx, "git_fetch", map[string]any{"all_repos": true})
	if fetchAll["ok"] != true {
		t.Fatalf("fetch all_repos should succeed: %#v", fetchAll)
	}

	pull := engine.Execute(ctx, "git_pull", map[string]any{"ff_only": false})
	if pull["error_kind"] != "confirmation_required" {
		t.Fatalf("pull with rebase options should require confirmation: %#v", pull)
	}

	if err := os.WriteFile(filepath.Join(root, "main2.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "main2.go")
	runGitTest(t, root, "commit", "-m", "feature-file")
	runGitTest(t, root, "switch", "-c", "feature")

	push := engine.Execute(ctx, "git_push", map[string]any{"branch": "feature"})
	if push["ok"] != true {
		t.Fatalf("push should succeed: %#v", push)
	}
	forcePush := engine.Execute(ctx, "git_push", map[string]any{"branch": "feature", "force_with_lease": true})
	if forcePush["error_kind"] != "force_push_disabled" {
		t.Fatalf("force push should be disabled: %#v", forcePush)
	}

	mergeAbort := engine.Execute(ctx, "git_merge", map[string]any{"abort": true})
	if mergeAbort["ok"] != false {
		t.Fatalf("merge abort without state should fail with ok=false: %#v", mergeAbort)
	}
	mergeConfirm := engine.Execute(ctx, "git_merge", map[string]any{"branch": "main"})
	if mergeConfirm["error_kind"] != "confirmation_required" {
		t.Fatalf("merge should require confirmation: %#v", mergeConfirm)
	}

	revert := engine.Execute(ctx, "git_revert", map[string]any{"revision": "non-existent", "confirmed": true})
	if revert["error_kind"] != "revert_conflict" {
		t.Fatalf("bad revision should produce revert_conflict: %#v", revert)
	}
	if err := engine.Execute(ctx, "git_revert", map[string]any{"revision": "", "confirmed": true}); err["error_kind"] != "revert_conflict" {
		t.Fatalf("empty revision should surface revert conflict: %#v", err)
	}
	invalidReset := engine.Execute(ctx, "git_reset", map[string]any{"mode": "bad"})
	if invalidReset["error_kind"] != "invalid_reset_mode" {
		t.Fatalf("invalid reset mode should fail: %#v", invalidReset)
	}
	hardReset := engine.Execute(ctx, "git_reset", map[string]any{"mode": "hard", "confirmed": true})
	if hardReset["error_kind"] != "hard_reset_disabled" {
		t.Fatalf("hard reset should be disabled by default: %#v", hardReset)
	}
	softReset := engine.Execute(ctx, "git_reset", map[string]any{"mode": "soft", "confirmed": true, "target": "HEAD"})
	if softReset["ok"] != true {
		t.Fatalf("soft reset should pass in this repo: %#v", softReset)
	}

	invalidWorktreeAdd := engine.Execute(ctx, "git_worktree_add", map[string]any{"branch": ""})
	if invalidWorktreeAdd["error_kind"] != "invalid_branch" {
		t.Fatalf("empty branch should be rejected: %#v", invalidWorktreeAdd)
	}
	worktreeList := engine.Execute(ctx, "git_worktree_list", map[string]any{})
	if worktreeList["ok"] != true || worktreeList["count"].(int) < 0 {
		t.Fatalf("worktree list should succeed: %#v", worktreeList)
	}
	worktreeAdd := engine.Execute(ctx, "git_worktree_add", map[string]any{"branch": "feature-cli-worktree", "create_branch": true})
	if worktreeAdd["ok"] != true {
		t.Fatalf("worktree add should apply: %#v", worktreeAdd)
	}
	worktreePath := worktreeAdd["path"].(string)
	removed := engine.Execute(ctx, "git_worktree_remove", map[string]any{"worktree_path": worktreePath, "confirmed": true})
	if removed["ok"] != true {
		t.Fatalf("worktree remove should succeed: %#v", removed)
	}
	invalidRemove := engine.Execute(ctx, "git_worktree_remove", map[string]any{"worktree_path": "unknown-path", "confirmed": true})
	if invalidRemove["ok"] != false {
		t.Fatalf("missing worktree removal should return handled failure: %#v", invalidRemove)
	}

	if !allOK([]map[string]any{{"ok": true}, {"ok": true}}) {
		t.Fatalf("allOK should handle all-success")
	}
	if allOK([]map[string]any{{"ok": true}, {"ok": false}}) {
		t.Fatalf("allOK should fail on any false")
	}
	if sanitizeBranch("feat/one\\two..path") != "feat-one-two-path" {
		t.Fatalf("sanitizeBranch should normalize path separators and traversal")
	}
	if len(engine.conflicts(ctx, root)) != 0 {
		t.Fatalf("fresh repo should have no conflicts")
	}
}

func TestQualityGateAndCommitScenarios(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("command tools require bash")
	}
	engine, root := newTestEngine(t)
	engine.settings.AccessMode = "full"
	engine.settings.CommandPolicyMode = "unrestricted"

	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "main.go")
	runGitTest(t, root, "commit", "-m", "base")
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n\nfunc main() {\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := engine.Execute(context.Background(), "quality_gate_and_commit", map[string]any{
		"name": "scenario",
		"checks": []any{
			map[string]any{"id": "cmd", "type": "command", "command": "printf ok", "required": true},
		},
		"commit": map[string]any{
			"message": "scenario commit",
			"paths":   []any{"main.go"},
		},
		"require_clean_after_commit": false,
	})
	if result["ok"] != true {
		t.Fatalf("quality gate commit should pass: %#v", result)
	}
	commit := result["commit"].(map[string]any)
	if commit["ok"] != true || commit["applied"] != true {
		t.Fatalf("commit should apply in full mode: %#v", commit)
	}

	if err := os.WriteFile(filepath.Join(root, "dirty.txt"), []byte("dirty\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	guard := engine.Execute(context.Background(), "worktree_guard", map[string]any{"required_branch": "main"})
	if guard["ok"] != false {
		t.Fatalf("dirty working tree should fail default guard: %#v", guard)
	}
	allowed := engine.Execute(context.Background(), "git_worktree_guard", map[string]any{
		"required_branch":     "main",
		"allowed_dirty_paths": []any{"dirty.txt", ".audit/", ".jobs/"},
	})
	if allowed["ok"] != true {
		t.Fatalf("allowed_dirty_paths should pass when explicitly permitted: %#v", allowed)
	}
}

func TestGitHubToolScenarios(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell-based test binary scripts are platform-specific")
	}
	engine, root := newTestEngine(t)
	ctx := context.Background()
	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")

	originalPath := os.Getenv("PATH")
	scriptDir := t.TempDir()
	installFakeGitHubCLIv3(t, scriptDir)
	engine.settings.GitHubToolsEnabled = true
	t.Setenv("PATH", scriptDir+string(os.PathListSeparator)+originalPath)

	status := engine.Execute(ctx, "gh_status", map[string]any{})
	if status["ok"] != true || status["authenticated"].(bool) != true {
		t.Fatalf("gh_status should be authenticated: %#v", status)
	}

	createDry := engine.Execute(ctx, "gh_pr_create", map[string]any{"title": "Feat", "body": "desc", "dry_run": true})
	if createDry["ok"] != true || createDry["dry_run"].(bool) != true {
		t.Fatalf("dry run PR create should preview: %#v", createDry)
	}
	create := engine.Execute(ctx, "gh_pr_create", map[string]any{"title": "Feat", "body": "desc", "confirmed": true})
	if create["ok"] != true {
		t.Fatalf("gh_pr_create should execute: %#v", create)
	}

	comment := engine.Execute(ctx, "gh_pr_comment", map[string]any{"number": 1, "body": "review", "confirmed": true})
	if comment["ok"] != true {
		t.Fatalf("gh_pr_comment should apply: %#v", comment)
	}
	reply := engine.Execute(ctx, "gh_pr_comment", map[string]any{"number": 1, "body": "reply", "reply_to": 2, "confirmed": true})
	if reply["ok"] != true {
		t.Fatalf("gh_pr_comment reply path should apply: %#v", reply)
	}
	if commentReq := engine.Execute(ctx, "gh_pr_comment", map[string]any{"number": 1, "body": "nope"}); commentReq["error_kind"] != "confirmation_required" {
		t.Fatalf("gh_pr_comment should require confirmation: %#v", commentReq)
	}

	merge := engine.Execute(ctx, "gh_pr_merge", map[string]any{"number": 1, "confirmed": true, "method": "merge"})
	if merge["ok"] != true {
		t.Fatalf("gh_pr_merge should execute: %#v", merge)
	}
	badMethod := engine.Execute(ctx, "gh_pr_merge", map[string]any{"number": 1, "confirmed": true, "method": "bad"})
	if badMethod["error_kind"] != "invalid_merge_method" {
		t.Fatalf("invalid merge method should be rejected: %#v", badMethod)
	}

	list := engine.Execute(ctx, "gh_pr_list", map[string]any{"state": "open", "limit": 5})
	if list["ok"] != true || len(list["data"].([]any)) != 1 {
		t.Fatalf("gh_pr_list should parse: %#v", list)
	}
	view := engine.Execute(ctx, "gh_pr_view", map[string]any{"number": 1, "include_comments": false, "include_diff": true})
	if _, ok := view["ok"].(bool); !ok || view["ok"] != true {
		t.Fatalf("gh_pr_view should parse: %#v", view)
	}
	viewRaw := engine.Execute(ctx, "gh_pr_view", map[string]any{"number": 2})
	if viewRaw["ok"] != true {
		t.Fatalf("gh_pr_view fallback parser should pass: %#v", viewRaw)
	}
	data := viewRaw["data"]
	if serialized, _ := json.Marshal(data); len(serialized) == 0 {
		t.Fatalf("gh_pr_view should return data payload: %#v", viewRaw)
	}

	checks := engine.Execute(ctx, "gh_checks", map[string]any{"pr_number": 1})
	if checks["ok"] != true {
		t.Fatalf("gh_checks should parse by pr number: %#v", checks)
	}
	runView := engine.Execute(ctx, "gh_run_view", map[string]any{"run_id": "123", "failed_only": true, "log_tail": 1})
	if runView["ok"] != true {
		t.Fatalf("gh_run_view should parse: %#v", runView)
	}
	if runView["failed_log"].(string) == "" {
		t.Fatalf("run view should include failed log: %#v", runView)
	}

	issues := engine.Execute(ctx, "gh_issue_list", map[string]any{"state": "open", "limit": 3})
	if issues["ok"] != true || len(issues["data"].([]any)) == 0 {
		t.Fatalf("gh_issue_list should parse: %#v", issues)
	}
	issueView := engine.Execute(ctx, "gh_issue_view", map[string]any{"number": 1})
	if issueView["ok"] != true {
		t.Fatalf("gh_issue_view should parse: %#v", issueView)
	}
	runChecks := engine.Execute(ctx, "gh_checks", map[string]any{})
	if runChecks["error_kind"] != "invalid_checks_request" {
		t.Fatalf("gh_checks should require request args: %#v", runChecks)
	}
}

func installFakeGitHubCLIv3(t *testing.T, directory string) {
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
if [ "$1" = "api" ] && [ "$2" = "--method" ]; then
  echo '{"ok":true}'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then
  echo '[{"number":1,"title":"feat","state":"open","isDraft":false,"headRefName":"feature","baseRefName":"main","url":"http://example","author":"test","updatedAt":"now"}]'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "Created pull request #1"
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  echo '{"number":1,"title":"feat","body":"body","state":"open","isDraft":false,"url":"http://example","author":"test","headRefName":"feature","baseRefName":"main","mergeable":"MERGEABLE","reviewDecision":"APPROVED","statusCheckRollup":[], "comments":[{"body":"x"}], "commits":[],"files":[],"createdAt":"now","updatedAt":"now"}'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then
  echo '{"ok":true}'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "merge" ]; then
  echo '{"merged":true}'
  exit 0
fi
if [ "$1" = "pr" ] && [ "$2" = "checks" ]; then
  echo '[{"name":"ci","state":"success","bucket":"success","link":"", "workflow":"ci"}]'
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
  echo '[{"number":1,"title":"Bug","state":"open","url":"http://example/1","author":"test","labels":[],"updatedAt":"now"}]'
  exit 0
fi
if [ "$1" = "issue" ] && [ "$2" = "view" ]; then
  echo '{"number":1,"title":"Bug","body":"body","state":"open","url":"http://example/1","author":"test","labels":[],"comments":[],"createdAt":"now","updatedAt":"now"}'
  exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "list" ]; then
  echo '[{"databaseId":"123","status":"completed","conclusion":"success","workflowName":"ci","url":"http://example","headSha":"dead","headBranch":"main"}]'
  exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "view" ] && [ "$4" = "--json" ]; then
  echo '{"databaseId":"123","status":"completed","conclusion":"success","workflowName":"ci","url":"http://example","headBranch":"main","headSha":"dead","event":"push","jobs":[],"createdAt":"now","updatedAt":"now"}'
  exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "view" ] && [ "$3" = "123" ] && [ "$4" = "--log-failed" ]; then
  echo "failed log"
  exit 0
fi
echo 'not json'
exit 1
`
	if err := os.WriteFile(script, []byte(payload), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(script, 0o755); err != nil {
		t.Fatal(err)
	}
}
