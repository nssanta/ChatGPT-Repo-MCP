package tools

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func (e *Engine) executeGitTool(ctx context.Context, name string, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		return withError("git_error", err)
	}
	switch name {
	case "git_status":
		arguments := []string{"status"}
		if boolArg(args, "short", true) {
			arguments = append(arguments, "--short", "--branch")
		} else {
			arguments = append(arguments, "--porcelain=v2", "--branch")
		}
		return e.gitOutput(ctx, repo, name, arguments...)
	case "git_diff":
		arguments := []string{"diff", fmt.Sprintf("--unified=%d", intArg(args, "context_lines", 3))}
		if boolArg(args, "staged", false) {
			arguments = append(arguments, "--staged")
		}
		if pathspec := optionalString(args, "pathspec"); pathspec != nil {
			arguments = append(arguments, "--", *pathspec)
		}
		return e.gitOutputCapped(ctx, repo, name, e.settings.MaxDiffBytes, arguments...)
	case "git_log":
		limit := min(max(intArg(args, "limit", 20), 1), e.settings.MaxLogCommits)
		arguments := []string{"log", "--date=iso-strict", "--pretty=format:%H%x09%h%x09%an%x09%ad%x09%s", "-n", strconv.Itoa(limit)}
		if since := optionalString(args, "since"); since != nil {
			arguments = append(arguments, "--since", *since)
		}
		if pathspec := optionalString(args, "pathspec"); pathspec != nil {
			arguments = append(arguments, "--", *pathspec)
		}
		result := e.gitOutput(ctx, repo, name, arguments...)
		if output, ok := result["output"].(string); ok {
			var commits []map[string]any
			for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
				parts := strings.SplitN(line, "\t", 5)
				if len(parts) == 5 {
					commits = append(commits, map[string]any{"sha": parts[0], "short_sha": parts[1], "author": parts[2], "date": parts[3], "subject": parts[4]})
				}
			}
			result["commits"] = commits
		}
		return result
	case "git_show":
		arguments := []string{"show", "--stat", "--patch", stringArg(args, "revision", "HEAD")}
		if path := optionalString(args, "path"); path != nil {
			arguments = append(arguments, "--", *path)
		}
		return e.gitOutputCapped(ctx, repo, name, e.settings.MaxDiffBytes, arguments...)
	case "git_branches":
		arguments := []string{"branch", "--format=%(refname:short)%09%(objectname:short)%09%(upstream:short)%09%(HEAD)"}
		if boolArg(args, "all_branches", true) {
			arguments = append(arguments, "--all")
		}
		return e.gitOutput(ctx, repo, name, arguments...)
	case "git_blame":
		arguments := []string{"blame", "--line-porcelain"}
		start := intArg(args, "start_line", 1)
		end := intArg(args, "end_line", 0)
		if end > 0 {
			arguments = append(arguments, "-L", fmt.Sprintf("%d,%d", start, end))
		} else if start > 1 {
			arguments = append(arguments, "-L", fmt.Sprintf("%d,+100", start))
		}
		arguments = append(arguments, "--", stringArg(args, "path", ""))
		return e.gitOutputCapped(ctx, repo, name, e.settings.MaxResponseChars, arguments...)
	case "git_grep":
		return e.gitGrep(ctx, repo, args)
	case "git_switch_branch":
		return e.gitSwitch(ctx, repo, args)
	case "git_create_branch":
		return e.gitCreateBranch(ctx, repo, args)
	case "git_add":
		return e.gitAdd(ctx, repo, args)
	case "git_restore":
		return e.gitRestore(ctx, repo, args)
	case "git_stash":
		return e.gitStash(ctx, repo, args)
	case "git_fetch":
		return e.gitFetch(ctx, repo, args)
	case "git_pull":
		return e.gitPull(ctx, repo, args)
	case "git_push":
		return e.gitPush(ctx, repo, args)
	case "git_merge":
		return e.gitMerge(ctx, repo, args)
	case "git_revert":
		return e.gitRevert(ctx, repo, args)
	case "git_reset":
		return e.gitReset(ctx, repo, args)
	case "git_worktree_add":
		return e.gitWorktreeAdd(ctx, repo, args)
	case "git_worktree_list":
		return e.gitWorktreeList(ctx, repo)
	case "git_worktree_remove":
		return e.gitWorktreeRemove(ctx, repo, args)
	case "prepare_task_worktree":
		return e.prepareTaskWorktree(ctx, repo, args)
	default:
		return failure("unknown_git_tool", name)
	}
}

func (e *Engine) gitInfo(ctx context.Context, repo string) map[string]any {
	branch, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "branch", "--show-current")
	head, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "rev-parse", "HEAD")
	status, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "status", "--porcelain")
	remote, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "remote", "get-url", "origin")
	return map[string]any{
		"toplevel": e.perimeter.Display(repo), "branch": strings.TrimSpace(branch),
		"head": strings.TrimSpace(head), "dirty": strings.TrimSpace(status) != "",
		"origin": redact(strings.TrimSpace(remote)),
	}
}

func (e *Engine) runGit(ctx context.Context, repo string, timeout time.Duration, arguments ...string) (string, error) {
	commandContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	command := exec.CommandContext(commandContext, "git", append([]string{"-C", repo}, arguments...)...)
	output, err := command.CombinedOutput()
	if commandContext.Err() == context.DeadlineExceeded {
		return string(output), fmt.Errorf("git command timed out")
	}
	if err != nil {
		return string(output), fmt.Errorf("git %s failed: %s", arguments[0], strings.TrimSpace(string(output)))
	}
	return string(output), nil
}

func (e *Engine) gitOutput(ctx context.Context, repo, tool string, arguments ...string) map[string]any {
	return e.gitOutputCapped(ctx, repo, tool, e.settings.MaxResponseChars, arguments...)
}

func (e *Engine) gitOutputCapped(ctx context.Context, repo, tool string, limit int, arguments ...string) map[string]any {
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return map[string]any{"ok": false, "error_kind": "git_error", "error": err.Error(), "tool": tool}
	}
	output, truncated := capText(output, limit)
	return map[string]any{"ok": true, "repo": e.perimeter.Display(repo), "output": output, "truncated": truncated}
}

func (e *Engine) gitGrep(ctx context.Context, repo string, args map[string]any) map[string]any {
	query := stringArg(args, "query", "")
	if query == "" {
		return failure("invalid_query", "query must not be empty")
	}
	arguments := []string{"grep", "-n", "--full-name"}
	if !boolArg(args, "case_sensitive", true) {
		arguments = append(arguments, "-i")
	}
	arguments = append(arguments, "-e", query)
	if revision := optionalString(args, "revision"); revision != nil {
		arguments = append(arguments, *revision)
	}
	paths := stringSliceArg(args, "paths")
	if pathspec := optionalString(args, "pathspec"); pathspec != nil {
		paths = append(paths, *pathspec)
	}
	if len(paths) > 0 {
		arguments = append(arguments, "--")
		arguments = append(arguments, paths...)
	}
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil && !strings.Contains(err.Error(), "exit status 1") {
		// git grep reports no matches as exit 1; the wrapped message is tolerated below.
		if !strings.Contains(err.Error(), "failed:") || strings.TrimSpace(output) != "" {
			return withError("git_error", err)
		}
	}
	var matches []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(output), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) == 3 {
			lineNumber, _ := strconv.Atoi(parts[1])
			matches = append(matches, map[string]any{"path": parts[0], "line": lineNumber, "text": parts[2]})
		}
	}
	return map[string]any{"ok": true, "query": query, "matches": matches, "count": len(matches)}
}

func (e *Engine) gitSwitch(ctx context.Context, repo string, args map[string]any) map[string]any {
	branch := stringArg(args, "branch", "")
	if branch == "" {
		return failure("invalid_branch", "branch must not be empty")
	}
	status, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "status", "--porcelain")
	stashed := false
	if strings.TrimSpace(status) != "" {
		if !boolArg(args, "stash_first", false) {
			return failure("dirty_worktree", "working tree is dirty; set stash_first=true")
		}
		if _, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "stash", "push", "--include-untracked", "-m", "chatrepo-mcp auto-stash"); err != nil {
			return withError("git_switch_failed", err)
		}
		stashed = true
	}
	arguments := []string{"switch"}
	if boolArg(args, "create", false) {
		arguments = append(arguments, "-c", branch)
		if start := optionalString(args, "start_point"); start != nil {
			arguments = append(arguments, *start)
		}
	} else {
		arguments = append(arguments, branch)
	}
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("git_switch_failed", err)
	}
	return map[string]any{"ok": true, "branch": branch, "stashed": stashed, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitCreateBranch(ctx context.Context, repo string, args map[string]any) map[string]any {
	branch := stringArg(args, "branch", "")
	if _, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "check-ref-format", "--branch", branch); err != nil {
		return withError("invalid_branch", err)
	}
	start := stringArg(args, "start_point", "HEAD")
	arguments := []string{"branch", branch, start}
	if boolArg(args, "checkout", true) {
		arguments = []string{"switch", "-c", branch, start}
	}
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("git_create_branch_failed", err)
	}
	return map[string]any{"ok": true, "branch": branch, "start_point": start, "checked_out": boolArg(args, "checkout", true), "output": strings.TrimSpace(output)}
}

func (e *Engine) validateGitPaths(repo string, paths []string) error {
	if len(paths) == 0 {
		return fmt.Errorf("explicit paths are required")
	}
	for _, path := range paths {
		if path == "." || path == "-A" || path == "--all" {
			return fmt.Errorf("blanket path %q is not allowed", path)
		}
		if _, err := e.perimeter.Resolve(filepath.Join(repo, path), true, true); err != nil {
			return err
		}
	}
	return nil
}

func (e *Engine) gitAdd(ctx context.Context, repo string, args map[string]any) map[string]any {
	paths := stringSliceArg(args, "paths")
	if err := e.validateGitPaths(repo, paths); err != nil {
		return withError("git_add_rejected", err)
	}
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if dryRun {
		return map[string]any{"ok": true, "dry_run": true, "applied": false, "paths": paths}
	}
	arguments := append([]string{"add", "--"}, paths...)
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("git_add_failed", err)
	}
	return map[string]any{"ok": true, "dry_run": false, "applied": true, "paths": paths, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitRestore(ctx context.Context, repo string, args map[string]any) map[string]any {
	paths := stringSliceArg(args, "paths")
	if err := e.validateGitPaths(repo, paths); err != nil {
		return withError("git_restore_rejected", err)
	}
	staged := boolArg(args, "staged", false)
	if !staged && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "discarding working-tree changes requires confirmed=true")
	}
	arguments := []string{"restore"}
	if staged {
		arguments = append(arguments, "--staged")
	}
	if source := optionalString(args, "source"); source != nil {
		arguments = append(arguments, "--source", *source)
	}
	arguments = append(arguments, "--")
	arguments = append(arguments, paths...)
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("git_restore_failed", err)
	}
	return map[string]any{"ok": true, "paths": paths, "staged": staged, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitStash(ctx context.Context, repo string, args map[string]any) map[string]any {
	action := stringArg(args, "action", "push")
	if (action == "pop" || action == "drop") && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "stash pop/drop requires confirmed=true")
	}
	arguments := []string{"stash", action}
	switch action {
	case "push":
		if boolArg(args, "include_untracked", true) {
			arguments = append(arguments, "--include-untracked")
		}
		if message := optionalString(args, "message"); message != nil {
			arguments = append(arguments, "-m", *message)
		}
	case "pop", "apply", "show", "drop":
		if ref := optionalString(args, "stash_ref"); ref != nil {
			arguments = append(arguments, *ref)
		}
	case "list":
	default:
		return failure("invalid_stash_action", "unsupported stash action")
	}
	return e.gitOutput(ctx, repo, "git_stash", arguments...)
}

func (e *Engine) gitFetch(ctx context.Context, repo string, args map[string]any) map[string]any {
	if boolArg(args, "all_repos", false) {
		var results []map[string]any
		for _, entry := range e.workspaceEntries(ctx) {
			if entry["is_git"] != true {
				continue
			}
			path := fmt.Sprint(entry["path"])
			target, err := e.resolveRepo(ctx, path)
			if err != nil {
				results = append(results, withError("git_fetch_failed", err))
				continue
			}
			clone := cloneMap(args)
			clone["all_repos"] = false
			results = append(results, e.gitFetch(ctx, target, clone))
		}
		return map[string]any{"ok": allOK(results), "results": results}
	}
	arguments := []string{"fetch", stringArg(args, "remote", "origin")}
	if boolArg(args, "prune", false) {
		arguments = append(arguments, "--prune")
	}
	output, err := e.runGit(ctx, repo, e.settings.GitNetworkTimeout, arguments...)
	if err != nil {
		return withError("git_fetch_failed", err)
	}
	return map[string]any{"ok": true, "remote": stringArg(args, "remote", "origin"), "output": strings.TrimSpace(output)}
}

func (e *Engine) gitPull(ctx context.Context, repo string, args map[string]any) map[string]any {
	ffOnly := boolArg(args, "ff_only", true)
	rebase := boolArg(args, "rebase", false)
	if (!ffOnly || rebase) && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "rebase or non-fast-forward pull requires confirmed=true")
	}
	branch := stringArg(args, "branch", "")
	if branch == "" {
		output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "branch", "--show-current")
		if err != nil || strings.TrimSpace(output) == "" {
			return failure("detached_head", "branch is required on detached HEAD")
		}
		branch = strings.TrimSpace(output)
	}
	arguments := []string{"pull"}
	if ffOnly {
		arguments = append(arguments, "--ff-only")
	}
	if rebase {
		arguments = append(arguments, "--rebase")
	}
	arguments = append(arguments, stringArg(args, "remote", "origin"), branch)
	output, err := e.runGit(ctx, repo, e.settings.GitNetworkTimeout, arguments...)
	if err != nil {
		_, _ = e.runGit(ctx, repo, e.settings.SubprocessTimeout, "merge", "--abort")
		_, _ = e.runGit(ctx, repo, e.settings.SubprocessTimeout, "rebase", "--abort")
		return withError("pull_conflict", err)
	}
	return map[string]any{"ok": true, "branch": branch, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitPush(ctx context.Context, repo string, args map[string]any) map[string]any {
	branch := stringArg(args, "branch", "")
	if branch == "" {
		output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "branch", "--show-current")
		if err != nil || strings.TrimSpace(output) == "" {
			return failure("detached_head", "branch is required on detached HEAD")
		}
		branch = strings.TrimSpace(output)
	}
	force := boolArg(args, "force_with_lease", false)
	if force && !e.settings.AllowForcePush {
		return failure("force_push_disabled", "ALLOW_FORCE_PUSH=true is required")
	}
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	requiresConfirmation := !dryRun || force || containsString(e.settings.ProtectedBranches, branch)
	if requiresConfirmation && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "this push requires confirmed=true")
	}
	arguments := []string{"push", "--porcelain"}
	if dryRun {
		arguments = append(arguments, "--dry-run")
	}
	if boolArg(args, "set_upstream", false) {
		arguments = append(arguments, "--set-upstream")
	}
	if force {
		arguments = append(arguments, "--force-with-lease")
	}
	arguments = append(arguments, stringArg(args, "remote", "origin"), branch)
	output, err := e.runGit(ctx, repo, e.settings.GitNetworkTimeout, arguments...)
	if err != nil {
		return withError("git_push_failed", err)
	}
	return map[string]any{"ok": true, "branch": branch, "dry_run": dryRun, "applied": !dryRun, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitMerge(ctx context.Context, repo string, args map[string]any) map[string]any {
	if boolArg(args, "abort", false) {
		return e.gitOutput(ctx, repo, "git_merge", "merge", "--abort")
	}
	if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "merging requires confirmed=true")
	}
	branch := stringArg(args, "branch", "")
	if branch == "" {
		return failure("invalid_branch", "branch is required")
	}
	arguments := []string{"merge"}
	if boolArg(args, "no_ff", false) {
		arguments = append(arguments, "--no-ff")
	}
	if message := optionalString(args, "message"); message != nil {
		arguments = append(arguments, "-m", *message)
	}
	arguments = append(arguments, branch)
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return map[string]any{"ok": false, "error_kind": "merge_conflict", "error": err.Error(), "conflicts": e.conflicts(ctx, repo)}
	}
	return map[string]any{"ok": true, "branch": branch, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitRevert(ctx context.Context, repo string, args map[string]any) map[string]any {
	if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "revert requires confirmed=true")
	}
	arguments := []string{"revert"}
	if boolArg(args, "no_commit", false) {
		arguments = append(arguments, "--no-commit")
	}
	arguments = append(arguments, stringArg(args, "revision", ""))
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		_, _ = e.runGit(ctx, repo, e.settings.SubprocessTimeout, "revert", "--abort")
		return map[string]any{"ok": false, "error_kind": "revert_conflict", "error": err.Error()}
	}
	return map[string]any{"ok": true, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitReset(ctx context.Context, repo string, args map[string]any) map[string]any {
	mode := stringArg(args, "mode", "mixed")
	if mode != "soft" && mode != "mixed" && mode != "hard" {
		return failure("invalid_reset_mode", "mode must be soft, mixed, or hard")
	}
	if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "reset requires confirmed=true")
	}
	if mode == "hard" && !e.settings.AllowHardReset {
		return failure("hard_reset_disabled", "ALLOW_HARD_RESET=true is required")
	}
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "reset", "--"+mode, stringArg(args, "target", "HEAD~1"))
	if err != nil {
		return withError("git_reset_failed", err)
	}
	return map[string]any{"ok": true, "mode": mode, "output": strings.TrimSpace(output)}
}

func (e *Engine) gitWorktreeAdd(ctx context.Context, repo string, args map[string]any) map[string]any {
	branch := stringArg(args, "branch", "")
	if branch == "" {
		return failure("invalid_branch", "branch is required")
	}
	directory := filepath.Join(repo, ".chatrepo-worktrees", sanitizeBranch(branch))
	if _, err := e.perimeter.Resolve(directory, true, true); err != nil {
		return withError("worktree_rejected", err)
	}
	arguments := []string{"worktree", "add"}
	if boolArg(args, "create_branch", true) {
		arguments = append(arguments, "-b", branch, directory, stringArg(args, "base", "HEAD"))
	} else {
		arguments = append(arguments, directory, branch)
	}
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("worktree_add_failed", err)
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(directory), "branch": branch, "output": strings.TrimSpace(output)}
}

func (e *Engine) prepareTaskWorktree(ctx context.Context, repo string, args map[string]any) map[string]any {
	branch, taskName := stringArg(args, "branch", ""), stringArg(args, "task_name", "")
	if branch == "" || taskName == "" {
		return failure("worktree_rejected", "branch and task_name are required")
	}
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if !dryRun && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "prepare_task_worktree requires confirmed=true")
	}
	if output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "check-ref-format", "--branch", branch); err != nil {
		return map[string]any{"ok": false, "error_kind": "invalid_branch", "error": strings.TrimSpace(output)}
	}
	base := stringArg(args, "base", "HEAD")
	baseSHAOutput, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "rev-parse", "--verify", base+"^{commit}")
	if err != nil {
		return withError("invalid_base", err)
	}
	baseSHA := strings.TrimSpace(baseSHAOutput)
	status, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "status", "--porcelain")
	parentDirty := strings.TrimSpace(status) != ""
	safeTask := sanitizeBranch(taskName)
	if safeTask == "" {
		return failure("invalid_task_name", "task_name has no usable characters")
	}
	directory := filepath.Join(e.settings.ProjectRoot, ".chatrepo-worktrees", safeTask)
	if _, err := os.Stat(directory); err == nil {
		return failure("worktree_exists", "worktree path already exists")
	}
	if _, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "show-ref", "--verify", "--quiet", "refs/heads/"+branch); err == nil {
		return failure("branch_exists", "branch already exists")
	}
	warnings := []string{}
	if parentDirty {
		warnings = append(warnings, "parent worktree is dirty; uncommitted changes are not copied")
	}
	result := map[string]any{"ok": true, "dry_run": dryRun, "applied": !dryRun, "repo": e.perimeter.Display(repo), "branch": branch, "base": base, "base_sha": baseSHA, "worktree_path": e.perimeter.Display(directory), "parent_dirty": parentDirty, "warnings": warnings}
	if dryRun {
		return result
	}
	if err := os.MkdirAll(filepath.Dir(directory), 0755); err != nil {
		return withError("worktree_create_failed", err)
	}
	if _, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "worktree", "add", "-b", branch, directory, baseSHA); err != nil {
		_, _ = e.runGit(ctx, repo, e.settings.SubprocessTimeout, "branch", "-D", branch)
		return withError("worktree_create_failed", err)
	}
	return result
}

func (e *Engine) gitWorktreeList(ctx context.Context, repo string) map[string]any {
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "worktree", "list", "--porcelain")
	if err != nil {
		return withError("worktree_list_failed", err)
	}
	var worktrees []map[string]any
	current := make(map[string]any)
	for _, line := range strings.Split(output, "\n") {
		if line == "" {
			if len(current) > 0 {
				worktrees = append(worktrees, current)
				current = make(map[string]any)
			}
			continue
		}
		key, value, _ := strings.Cut(line, " ")
		current[key] = value
		if key == "worktree" {
			current[key] = e.perimeter.Display(value)
		}
	}
	if len(current) > 0 {
		worktrees = append(worktrees, current)
	}
	return map[string]any{"ok": true, "worktrees": worktrees, "count": len(worktrees)}
}

func (e *Engine) gitWorktreeRemove(ctx context.Context, repo string, args map[string]any) map[string]any {
	if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "worktree removal requires confirmed=true")
	}
	resolved, err := e.perimeter.Resolve(stringArg(args, "worktree_path", ""), true, true)
	if err != nil {
		return withError("worktree_remove_rejected", err)
	}
	arguments := []string{"worktree", "remove"}
	if boolArg(args, "force", false) {
		arguments = append(arguments, "--force")
	}
	arguments = append(arguments, resolved.Absolute)
	output, err := e.runGit(ctx, repo, e.settings.SubprocessTimeout, arguments...)
	if err != nil {
		return withError("worktree_remove_failed", err)
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(resolved.Absolute), "output": strings.TrimSpace(output)}
}

func (e *Engine) conflicts(ctx context.Context, repo string) []string {
	output, _ := e.runGit(ctx, repo, e.settings.SubprocessTimeout, "diff", "--name-only", "--diff-filter=U")
	var result []string
	for _, path := range strings.Split(strings.TrimSpace(output), "\n") {
		if path != "" {
			result = append(result, path)
		}
	}
	return result
}

func sanitizeBranch(branch string) string {
	replacer := strings.NewReplacer("/", "-", "\\", "-", "..", "-")
	return replacer.Replace(branch)
}

func allOK(results []map[string]any) bool {
	for _, result := range results {
		if result["ok"] == false {
			return false
		}
	}
	return true
}
