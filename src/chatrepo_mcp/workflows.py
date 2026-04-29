from __future__ import annotations

import re
import subprocess
from typing import Any

from .command_tools import GitCommitError, git_commit, run_command
from .config import Settings
from .profile import DEFAULT_QUALITY_RULES, load_repo_profile
from .security import is_blocked_relative, normalize_rel_path


RULE_PATTERNS: dict[str, re.Pattern[str]] = {
    "no_new_as_any": re.compile(r"\bas\s+any\b"),
    "no_new_colon_any": re.compile(r":\s*any\b"),
    "no_new_ts_ignore": re.compile(r"@ts-ignore"),
    "no_new_eslint_disable": re.compile(r"eslint-disable"),
    "no_new_console_log": re.compile(r"\bconsole\.log\s*\("),
    "no_secret_like_literals": re.compile(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}"),
}


def _run_git(settings: Settings, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(settings.project_root),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )


def _diff_added_lines(settings: Settings, base_ref: str, paths: list[str] | None) -> list[dict[str, Any]]:
    args = ["diff", "--unified=0", base_ref]
    if paths:
        args.extend(["--", *paths])
    proc = _run_git(settings, args)
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
) -> dict[str, Any]:
    active_rules = rules or load_repo_profile(settings).quality_rules or list(DEFAULT_QUALITY_RULES)
    added_lines = _diff_added_lines(settings, base_ref, paths)
    violations = []
    for item in added_lines:
        for rule in active_rules:
            pattern = RULE_PATTERNS.get(rule)
            if pattern and pattern.search(item["text"]):
                violations.append({"rule": rule, **item})
    return {
        "ok": not violations,
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
) -> dict[str, Any]:
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
            )
            result.update({"id": check_id, "required": required, "kind": "policy_scan"})
        else:
            command, parser = _preset_command(check, settings)
            result = run_command(
                command,
                settings,
                timeout_ms=check.get("timeout_ms"),
                tail_lines=check.get("tail_lines", 200),
                parse_kind=parser,
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
) -> dict[str, Any]:
    gate = run_quality_gate(settings, checks=checks, name=name, stop_on_failure=True)
    if not gate["ok"]:
        return {"ok": False, "committed": False, "gate": gate}
    if not commit.get("enabled", True):
        return {"ok": True, "committed": False, "gate": gate}
    result = git_commit(str(commit["message"]), list(commit["paths"]), settings, dry_run=False)
    if not result.get("ok"):
        return {"ok": False, "committed": False, "gate": gate, "commit_result": result}
    head = _run_git(settings, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    status = _run_git(settings, ["status", "--short"]).stdout.strip()
    clean_ok = not require_clean_after_commit or status == ""
    return {
        "ok": clean_ok,
        "committed": True,
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
) -> dict[str, Any]:
    allowed = set(allowed_dirty_paths or [])
    status = _run_git(settings, ["status", "--short"]).stdout.splitlines()
    dirty_paths = [line[3:] for line in status if len(line) > 3]
    unexpected = [path for path in dirty_paths if path not in allowed]
    branch = _run_git(settings, ["branch", "--show-current"]).stdout.strip()
    git_dir = _run_git(settings, ["rev-parse", "--git-dir"]).stdout.strip()
    rebase_paths = [settings.project_root / git_dir / "rebase-merge", settings.project_root / git_dir / "rebase-apply"]
    rebasing = any(path.exists() for path in rebase_paths)
    ok = not unexpected
    if require_branch and branch != require_branch:
        ok = False
    if require_not_rebasing and rebasing:
        ok = False
    return {
        "ok": ok,
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
