package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
)

func (e *Engine) executeGitHubTool(ctx context.Context, name string, args map[string]any) map[string]any {
	if !e.settings.GitHubToolsEnabled {
		return failure("github_tools_disabled", "GITHUB_TOOLS_ENABLED=false")
	}
	if _, err := exec.LookPath("gh"); err != nil {
		return map[string]any{"ok": false, "error_kind": "gh_unavailable", "error": "GitHub CLI is not installed", "install_hint": "https://cli.github.com/"}
	}
	repo := e.settings.ProjectRoot
	if name != "gh_status" {
		var err error
		repo, err = e.resolveRepo(ctx, stringArg(args, "repo", ""))
		if err != nil {
			return withError("github_repo_error", err)
		}
	}
	switch name {
	case "gh_status":
		status := e.runGH(ctx, repo, "auth", "status")
		if status["ok"] == false {
			return status
		}
		rate := e.runGHJSON(ctx, repo, "api", "rate_limit")
		return map[string]any{"ok": true, "authenticated": true, "auth": status["output"], "rate_limit": rate["data"]}
	case "gh_pr_create":
		return e.ghPRCreate(ctx, repo, args)
	case "gh_pr_list":
		return e.runGHJSON(ctx, repo, "pr", "list", "--state", stringArg(args, "state", "open"), "--limit", strconv.Itoa(intArg(args, "limit", 20)), "--json", "number,title,state,isDraft,headRefName,baseRefName,url,author,updatedAt")
	case "gh_pr_view":
		return e.ghPRView(ctx, repo, args)
	case "gh_pr_comment":
		return e.ghPRComment(ctx, repo, args)
	case "gh_pr_merge":
		if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
			return failure("confirmation_required", "merging a pull request requires confirmed=true")
		}
		method := stringArg(args, "method", "squash")
		if method != "merge" && method != "squash" && method != "rebase" {
			return failure("invalid_merge_method", "method must be merge, squash, or rebase")
		}
		return e.runGH(ctx, repo, "pr", "merge", strconv.Itoa(intArg(args, "number", 0)), "--"+method)
	case "gh_checks":
		if number := intArg(args, "pr_number", 0); number > 0 {
			return e.runGHJSON(ctx, repo, "pr", "checks", strconv.Itoa(number), "--json", "name,state,bucket,link,workflow")
		}
		if ref := stringArg(args, "ref", ""); ref != "" {
			return e.runGHJSON(ctx, repo, "run", "list", "--commit", ref, "--json", "databaseId,status,conclusion,workflowName,url,headSha")
		}
		return failure("invalid_checks_request", "one of pr_number or ref is required")
	case "gh_run_view":
		return e.ghRunView(ctx, repo, args)
	case "gh_run_rerun":
		if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
			return failure("confirmation_required", "rerunning a workflow requires confirmed=true")
		}
		arguments := []string{"run", "rerun", stringArg(args, "run_id", "")}
		if boolArg(args, "failed_only", true) {
			arguments = append(arguments, "--failed")
		}
		return e.runGH(ctx, repo, arguments...)
	case "gh_issue_list":
		return e.runGHJSON(ctx, repo, "issue", "list", "--state", stringArg(args, "state", "open"), "--limit", strconv.Itoa(intArg(args, "limit", 20)), "--json", "number,title,state,url,author,labels,updatedAt")
	case "gh_issue_view":
		return e.runGHJSON(ctx, repo, "issue", "view", strconv.Itoa(intArg(args, "number", 0)), "--json", "number,title,body,state,url,author,labels,comments,createdAt,updatedAt")
	default:
		return failure("unknown_github_tool", name)
	}
}

func (e *Engine) runGH(ctx context.Context, repo string, arguments ...string) map[string]any {
	commandContext, cancel := context.WithTimeout(ctx, e.settings.GHTimeout)
	defer cancel()
	command := exec.CommandContext(commandContext, "gh", arguments...)
	command.Dir = repo
	command.Env = append(os.Environ(), "GH_PROMPT_DISABLED=1", "NO_COLOR=1")
	output, err := command.CombinedOutput()
	redacted := redact(string(output))
	redacted, truncated := capText(redacted, e.settings.MaxResponseChars)
	if commandContext.Err() == context.DeadlineExceeded {
		return failure("gh_timeout", "GitHub CLI command timed out")
	}
	if err != nil {
		return map[string]any{"ok": false, "error_kind": "gh_error", "error": strings.TrimSpace(redacted), "exit_error": err.Error()}
	}
	return map[string]any{"ok": true, "output": strings.TrimSpace(redacted), "truncated": truncated}
}

func (e *Engine) runGHJSON(ctx context.Context, repo string, arguments ...string) map[string]any {
	result := e.runGH(ctx, repo, arguments...)
	if result["ok"] == false {
		return result
	}
	output := fmt.Sprint(result["output"])
	var data any
	if output == "" {
		data = []any{}
	} else if err := json.Unmarshal([]byte(output), &data); err != nil {
		return map[string]any{"ok": false, "error_kind": "gh_invalid_json", "error": err.Error(), "output": output}
	}
	return map[string]any{"ok": true, "data": data}
}

func (e *Engine) ghPRCreate(ctx context.Context, repo string, args map[string]any) map[string]any {
	title, body := stringArg(args, "title", ""), stringArg(args, "body", "")
	if strings.TrimSpace(title) == "" {
		return failure("invalid_pr", "title is required")
	}
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if !dryRun && !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "creating a pull request requires confirmed=true")
	}
	preview := map[string]any{"title": title, "body": body, "base": stringArg(args, "base", ""), "head": stringArg(args, "head", ""), "draft": boolArg(args, "draft", false)}
	if dryRun {
		return map[string]any{"ok": true, "dry_run": true, "applied": false, "preview": preview}
	}
	arguments := []string{"pr", "create", "--title", title, "--body", body}
	if base := stringArg(args, "base", ""); base != "" {
		arguments = append(arguments, "--base", base)
	}
	if head := stringArg(args, "head", ""); head != "" {
		arguments = append(arguments, "--head", head)
	}
	if boolArg(args, "draft", false) {
		arguments = append(arguments, "--draft")
	}
	result := e.runGH(ctx, repo, arguments...)
	result["dry_run"], result["applied"] = false, result["ok"]
	return result
}

func (e *Engine) ghPRView(ctx context.Context, repo string, args map[string]any) map[string]any {
	number := strconv.Itoa(intArg(args, "number", 0))
	result := e.runGHJSON(ctx, repo, "pr", "view", number, "--json", "number,title,body,state,isDraft,url,author,headRefName,baseRefName,mergeable,reviewDecision,statusCheckRollup,reviews,comments,commits,files,createdAt,updatedAt")
	if result["ok"] == false {
		return result
	}
	if boolArg(args, "include_diff", false) {
		diff := e.runGH(ctx, repo, "pr", "diff", number)
		result["diff"] = diff["output"]
		result["diff_ok"] = diff["ok"]
	}
	if !boolArg(args, "include_comments", true) {
		if data, ok := result["data"].(map[string]any); ok {
			delete(data, "comments")
		}
	}
	return result
}

func (e *Engine) ghPRComment(ctx context.Context, repo string, args map[string]any) map[string]any {
	if !e.settings.ConfirmationGranted(boolArg(args, "confirmed", false)) {
		return failure("confirmation_required", "posting a pull request comment requires confirmed=true")
	}
	number := strconv.Itoa(intArg(args, "number", 0))
	body := stringArg(args, "body", "")
	if reply := intArg(args, "reply_to", 0); reply > 0 {
		return e.runGH(ctx, repo, "api", "--method", "POST", fmt.Sprintf("repos/{owner}/{repo}/pulls/%s/comments/%d/replies", number, reply), "-f", "body="+body)
	}
	return e.runGH(ctx, repo, "pr", "comment", number, "--body", body)
}

func (e *Engine) ghRunView(ctx context.Context, repo string, args map[string]any) map[string]any {
	runID := stringArg(args, "run_id", "")
	if runID == "" {
		latest := e.runGHJSON(ctx, repo, "run", "list", "--limit", "1", "--json", "databaseId")
		if rows, ok := latest["data"].([]any); ok && len(rows) > 0 {
			if row, ok := rows[0].(map[string]any); ok {
				runID = fmt.Sprint(row["databaseId"])
			}
		}
		if runID == "" {
			return failure("run_not_found", "no workflow run found")
		}
	}
	result := e.runGHJSON(ctx, repo, "run", "view", runID, "--json", "databaseId,status,conclusion,workflowName,url,headBranch,headSha,event,jobs,createdAt,updatedAt")
	if result["ok"] == false || !boolArg(args, "failed_only", true) {
		return result
	}
	logs := e.runGH(ctx, repo, "run", "view", runID, "--log-failed")
	if output, ok := logs["output"].(string); ok {
		result["failed_log"] = tailText(output, intArg(args, "log_tail", 200))
	}
	result["failed_log_ok"] = logs["ok"]
	return result
}
