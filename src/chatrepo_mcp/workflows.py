from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import Any

from . import git_tools
from .command_tools import GitCommitError, git_commit, run_command
from .config import Settings
from .profile import DEFAULT_QUALITY_RULES, load_repo_profile
from .security import is_blocked_relative, normalize_rel_path


# Rule id -> regex applied to newly-added diff lines. Kept stack-agnostic at
# the RULE_PATTERNS/RULE_EXTENSIONS level: none of these are enabled by
# default (see profile.DEFAULT_QUALITY_RULES); a repo opts into whichever
# rules match its own stack via `.chatrepo/mcp.yml` `quality_rules`.
RULE_PATTERNS: dict[str, re.Pattern[str]] = {
    # TypeScript / JavaScript
    "no_new_as_any": re.compile(r"\bas\s+any\b"),
    "no_new_colon_any": re.compile(r":\s*any\b"),
    "no_new_ts_ignore": re.compile(r"@ts-ignore"),
    "no_new_eslint_disable": re.compile(r"eslint-disable"),
    "no_new_console_log": re.compile(r"\bconsole\.log\s*\("),
    # Python
    "no_new_print": re.compile(r"(?<!\.)\bprint\s*\("),
    "no_new_pdb": re.compile(r"\bpdb\.set_trace\s*\("),
    "no_new_type_ignore": re.compile(r"#\s*type:\s*ignore"),
    # Go
    "no_new_fmt_println": re.compile(r"\bfmt\.Print(?:ln|f)?\s*\("),
    # Stack-agnostic
    "no_secret_like_literals": re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}"),
}

# File extensions each rule applies to (lowercase, including the leading
# dot). Rules absent from this map (e.g. no_secret_like_literals) apply to
# every file regardless of extension. This prevents cross-stack false
# positives, for example a Python `x: Any` type annotation must never trip
# the TypeScript-only `no_new_colon_any` rule.
RULE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "no_new_as_any": (".ts", ".tsx", ".js", ".jsx"),
    "no_new_colon_any": (".ts", ".tsx"),
    "no_new_ts_ignore": (".ts", ".tsx", ".js", ".jsx"),
    "no_new_eslint_disable": (".ts", ".tsx", ".js", ".jsx"),
    "no_new_console_log": (".ts", ".tsx", ".js", ".jsx"),
    "no_new_print": (".py",),
    "no_new_pdb": (".py",),
    "no_new_type_ignore": (".py",),
    "no_new_fmt_println": (".go",),
}


def _rule_applies_to_path(rule: str, path: str) -> bool:
    """Return whether `rule` is applicable to a file at `path`, by extension.

    Rules with no entry in RULE_EXTENSIONS are stack-agnostic and apply to
    every file.
    """
    allowed_extensions = RULE_EXTENSIONS.get(rule)
    if not allowed_extensions:
        return True
    return PurePosixPath(path).suffix.lower() in allowed_extensions


def _run_git(settings: Settings, args: list[str], *, repo: str | None = None) -> subprocess.CompletedProcess[str]:
    cwd = git_tools._resolve_repo_toplevel(repo, settings)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )


def _diff_added_lines(
    settings: Settings,
    base_ref: str,
    paths: list[str] | None,
    *,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    args = ["diff", "--unified=0", base_ref]
    if paths:
        args.extend(["--", *paths])
    proc = _run_git(settings, args, repo=repo)
    if proc.returncode not in {0, 1}:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git diff failed")
    current_path: str | None = None
    new_line = 0
    added: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,\d+)?", line)
            new_line = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if current_path:
                added.append({"path": current_path, "line": new_line, "text": line[1:]})
            new_line += 1
        elif not line.startswith("-"):
            new_line += 1
    return added


def scan_new_policy_violations(
    settings: Settings,
    *,
    base_ref: str = "HEAD",
    paths: list[str] | None = None,
    rules: list[str] | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    active_rules = rules or load_repo_profile(settings).quality_rules or list(DEFAULT_QUALITY_RULES)
    added_lines = _diff_added_lines(settings, base_ref, paths, repo=repo)
    violations = []
    for item in added_lines:
        for rule in active_rules:
            pattern = RULE_PATTERNS.get(rule)
            if not pattern:
                continue
            if not _rule_applies_to_path(rule, item["path"]):
                continue
            if pattern.search(item["text"]):
                violations.append({"rule": rule, **item})
    return {
        "ok": not violations,
        "repo": git_tools._repo_rel(toplevel, settings),
        "base_ref": base_ref,
        "paths": paths or [],
        "rules": active_rules,
        "violations": violations,
        "count": len(violations),
    }


def _preset_command(check: dict[str, Any], settings: Settings) -> tuple[str, str]:
    profile = load_repo_profile(settings)
    preset_name = check.get("preset")
    if not preset_name:
        return str(check["command"]), str(check.get("parse_kind", "auto"))
    presets = profile.presets
    if preset_name not in presets:
        raise ValueError(f"unknown preset: {preset_name}")
    preset = presets[preset_name]
    return str(preset["command"]), str(check.get("parse_kind") or preset.get("parser", "auto"))


def run_quality_gate(
    settings: Settings,
    *,
    checks: list[dict[str, Any]],
    name: str | None = None,
    stop_on_failure: bool = True,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    results = []
    ok = True
    failed_check: str | None = None
    for index, check in enumerate(checks):
        check_id = str(check.get("id") or check.get("preset") or f"check_{index + 1}")
        required = bool(check.get("required", True))
        if check.get("preset") == "scan_new_policy_violations" or check.get("kind") == "policy_scan":
            result = scan_new_policy_violations(
                settings,
                base_ref=str(check.get("base_ref", "HEAD")),
                paths=check.get("paths"),
                rules=check.get("rules"),
                repo=check.get("repo", repo),
            )
            result.update({"id": check_id, "required": required, "kind": "policy_scan"})
        else:
            command, parser = _preset_command(check, settings)
            # Quality-gate checks are gate-internal, pre-registered presets/commands
            # supplied by the gate definition itself (not raw end-user shell input),
            # so they run trusted -- same as run_test_preset.
            result = run_command(
                command,
                settings,
                cwd=str(toplevel),
                timeout_ms=check.get("timeout_ms"),
                tail_lines=check.get("tail_lines", 200),
                parse_kind=parser,
                policy_exempt=True,
            )
            result.update({"id": check_id, "required": required, "kind": "command"})
        results.append(result)
        if required and not result.get("ok"):
            ok = False
            failed_check = check_id
            if stop_on_failure:
                break
    return {
        "ok": ok,
        "name": name,
        "failed_check": failed_check,
        "checks": results,
        "count": len(results),
    }


def quality_gate_and_commit(
    settings: Settings,
    *,
    checks: list[dict[str, Any]],
    commit: dict[str, Any],
    name: str | None = None,
    require_clean_after_commit: bool = True,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    repo_field = git_tools._repo_rel(toplevel, settings)
    gate = run_quality_gate(settings, checks=checks, name=name, stop_on_failure=True, repo=repo)
    if not gate["ok"]:
        return {"ok": False, "committed": False, "repo": repo_field, "gate": gate}
    if not commit.get("enabled", True):
        return {"ok": True, "committed": False, "repo": repo_field, "gate": gate}
    result = git_commit(
        str(commit["message"]),
        list(commit["paths"]),
        settings,
        dry_run=False,
        repo=repo,
    )
    if not result.get("ok"):
        return {"ok": False, "committed": False, "repo": repo_field, "gate": gate, "commit_result": result}
    head = _run_git(settings, ["rev-parse", "--short", "HEAD"], repo=repo).stdout.strip()
    status = _run_git(settings, ["status", "--short"], repo=repo).stdout.strip()
    clean_ok = not require_clean_after_commit or status == ""
    return {
        "ok": clean_ok,
        "committed": True,
        "repo": repo_field,
        "commit": head,
        "working_tree_clean": status == "",
        "git": {"status_short": status, "head": head},
        "gate": gate,
        "commit_result": result,
    }


def git_worktree_guard(
    settings: Settings,
    *,
    allowed_dirty_paths: list[str] | None = None,
    require_branch: str | None = None,
    require_not_rebasing: bool = True,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    allowed = set(allowed_dirty_paths or [])
    status = _run_git(settings, ["status", "--short"], repo=repo).stdout.splitlines()
    dirty_paths = [line[3:] for line in status if len(line) > 3]
    unexpected = [path for path in dirty_paths if path not in allowed]
    branch = _run_git(settings, ["branch", "--show-current"], repo=repo).stdout.strip()
    git_dir = _run_git(settings, ["rev-parse", "--git-dir"], repo=repo).stdout.strip()
    rebase_paths = [toplevel / git_dir / "rebase-merge", toplevel / git_dir / "rebase-apply"]
    rebasing = any(path.exists() for path in rebase_paths)
    ok = not unexpected
    if require_branch and branch != require_branch:
        ok = False
    if require_not_rebasing and rebasing:
        ok = False
    return {
        "ok": ok,
        "repo": git_tools._repo_rel(toplevel, settings),
        "branch": branch,
        "require_branch": require_branch,
        "rebasing": rebasing,
        "dirty_paths": dirty_paths,
        "dirty_unexpected": unexpected,
    }


def validate_commit_paths(paths: list[str], settings: Settings) -> None:
    for path in paths:
        rel = normalize_rel_path(path)
        if is_blocked_relative(rel, settings):
            raise GitCommitError(f"path is blocked by policy: {rel}")
