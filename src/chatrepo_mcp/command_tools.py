from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .security import SecurityError, is_blocked_relative, normalize_rel_path


class CommandPolicyError(ValueError):
    """Raised when a command is not allowed by the MCP command policy."""


class ConfirmationRequiredError(ValueError):
    """Raised when a command is recognized but requires explicit owner confirmation."""


class GitCommitError(ValueError):
    """Raised when a controlled git commit cannot be completed safely."""


@dataclass(frozen=True)
class CommandRule:
    command: str
    allow_suffix: bool = False


ALLOWED_COMMANDS = (
    CommandRule("git status --short"),
    CommandRule("git status --short --branch"),
    CommandRule("git diff --check"),
    CommandRule("git diff"),
    CommandRule("git diff --name-only"),
    CommandRule("git log --oneline -n 20"),
    CommandRule("npm --version"),
    CommandRule("node --version"),
    CommandRule("npx --version"),
    CommandRule("npm run build -w packages/agent"),
    CommandRule("npm run build -w packages/gateway"),
    CommandRule("npm run typecheck -w packages/agent"),
    CommandRule("npm run typecheck -w packages/gateway"),
    CommandRule("npm run test -w packages/agent -- --run"),
    CommandRule("npm run test -w packages/gateway -- --run"),
    CommandRule("npm run test:fast -w packages/integration"),
    CommandRule("npx vitest run", allow_suffix=True),
    CommandRule("npx tsx tests/telegram/scenarios/d59-trust-mode-approval.test.ts"),
    CommandRule("npx tsx tests/telegram/scenarios/d62-tool-confirmation-trust-modes.test.ts"),
    CommandRule("npx tsx tests/telegram/scenarios/d64-approval-tts-no-record-voice.test.ts"),
    CommandRule("npx tsx tests/telegram/scenarios/d67-tool-trace-visibility-modes.test.ts"),
)

CONFIRMATION_COMMANDS = (
    "bash scripts/start_local.sh",
    "bash scripts/live-gate.sh --no-start",
    "docker compose",
    "systemctl",
    "EVA_LIVE_TESTS=1 npx vitest run --config vitest.e2e.live.config.ts",
    "npx vitest run --config vitest.e2e.live.config.ts",
)

SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "$(", "`"}
DENIED_WORDS = {"curl", "wget", "ssh", "scp", "rsync", "rm", "cat", "printenv", "sudo"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|password|api[_-]?key)=([^\s]+)"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"npm_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def _canonical(command: str) -> str:
    return " ".join(shlex.split(command))


def _split_command(command: str) -> list[str]:
    if any(token in command for token in SHELL_TOKENS):
        raise CommandPolicyError("shell operators are not allowed")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise CommandPolicyError(f"invalid command syntax: {exc}") from exc
    if not parts:
        raise CommandPolicyError("command must not be empty")
    if any(part in DENIED_WORDS for part in parts):
        raise CommandPolicyError("command contains a forbidden executable or token")
    return parts


def _check_command_policy(command: str) -> str:
    parts = _split_command(command)
    normalized = " ".join(parts)
    for prefix in CONFIRMATION_COMMANDS:
        if normalized == prefix or normalized.startswith(prefix + " "):
            raise ConfirmationRequiredError("This command requires owner confirmation")
    for rule in ALLOWED_COMMANDS:
        allowed = _canonical(rule.command)
        if normalized == allowed or (rule.allow_suffix and normalized.startswith(allowed + " ")):
            return normalized
    raise CommandPolicyError("command is not allowlisted")


def _resolve_cwd(cwd: str | None, settings: Settings) -> Path:
    root = settings.project_root.resolve()
    target = (root / cwd).resolve() if cwd else root
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"cwd escapes repository root: {cwd}") from exc
    if not target.exists() or not target.is_dir():
        raise CommandPolicyError(f"cwd is not a directory: {cwd}")
    return target


def _redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", redacted)
    return redacted


def _tail(text: str, tail_lines: int | None) -> str:
    if not tail_lines:
        return ""
    return "\n".join(text.splitlines()[-tail_lines:])


def _resolved_binaries(cwd: Path, env: dict[str, str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for binary in ("node", "npm", "npx"):
        proc = subprocess.run(
            ["/bin/bash", "-lc", f"command -v {binary}"],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        result[binary] = proc.stdout.strip() or None
    return result


def _command_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra_env:
        for key, value in extra_env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise CommandPolicyError(f"invalid env key: {key}")
            env[key] = value
    return env


def _audit(settings: Settings, payload: dict[str, Any]) -> None:
    path = settings.command_audit_log_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def run_command(
    command: str,
    settings: Settings,
    *,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int | None = None,
    tail_lines: int | None = 200,
) -> dict[str, Any]:
    normalized = _check_command_policy(command)
    effective_timeout_ms = min(timeout_ms or settings.command_timeout_ms, settings.command_timeout_ms)
    output_limit = min(max_output_chars or settings.max_command_output_chars, settings.max_command_output_chars)
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env)
    if run_env.get("EVA_LIVE_TESTS") == "1":
        raise ConfirmationRequiredError("Live E2E commands require owner confirmation")
    started = time.monotonic()
    resolved = _resolved_binaries(run_cwd, run_env)
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", normalized],
            cwd=str(run_cwd),
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=effective_timeout_ms / 1000,
        )
        stdout = _redact(proc.stdout)
        stderr = _redact(proc.stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": proc.returncode == 0,
            "command": normalized,
            "exit_code": proc.returncode,
            "stdout": stdout[:output_limit],
            "stderr": stderr[:output_limit],
            "stdout_tail": _tail(stdout, tail_lines),
            "stderr_tail": _tail(stderr, tail_lines),
            "full_output_truncated": len(stdout) > output_limit or len(stderr) > output_limit,
            "duration_ms": duration_ms,
            "timed_out": False,
            "cwd": str(run_cwd),
            "resolved_binaries": resolved,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _redact(exc.stdout or "")
        stderr = _redact(exc.stderr or "")
        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": False,
            "command": normalized,
            "exit_code": None,
            "stdout": stdout[:output_limit],
            "stderr": stderr[:output_limit],
            "stdout_tail": _tail(stdout, tail_lines),
            "stderr_tail": _tail(stderr, tail_lines),
            "full_output_truncated": len(stdout) > output_limit or len(stderr) > output_limit,
            "duration_ms": duration_ms,
            "timed_out": True,
            "cwd": str(run_cwd),
            "resolved_binaries": resolved,
        }
    _audit(
        settings,
        {
            "timestamp": int(time.time()),
            "command": normalized,
            "cwd": str(run_cwd),
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "stdout_chars": len(result["stdout"]),
            "stderr_chars": len(result["stderr"]),
        },
    )
    return result


def run_commands(
    commands: list[str],
    settings: Settings,
    *,
    stop_on_failure: bool = False,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
) -> dict[str, Any]:
    results = []
    for command in commands:
        try:
            result = run_command(command, settings, timeout_ms=timeout_ms, tail_lines=tail_lines)
        except ConfirmationRequiredError as exc:
            result = {"ok": False, "error_kind": "confirmation_required", "command": command, "reason": str(exc)}
        except CommandPolicyError as exc:
            result = {"ok": False, "error_kind": "command_not_allowed", "command": command, "error": str(exc)}
        results.append(result)
        if stop_on_failure and not result.get("ok"):
            break
    return {
        "ok": all(item.get("ok") for item in results),
        "stop_on_failure": stop_on_failure,
        "results": results,
        "count": len(results),
    }


def git_commit(
    message: str,
    paths: list[str],
    settings: Settings,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    if not message.strip():
        raise GitCommitError("commit message must not be empty")
    if not paths:
        raise GitCommitError("paths must not be empty")
    root = settings.project_root.resolve()
    rel_paths = []
    for path in paths:
        rel = normalize_rel_path(path)
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise SecurityError(f"path escapes repository root: {path}") from exc
        if is_blocked_relative(rel, settings):
            raise GitCommitError(f"path is blocked by policy: {rel}")
        rel_paths.append(rel)
    status = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )
    staged = [line for line in status.stdout.splitlines() if line.strip()]
    unrelated = [path for path in staged if path not in rel_paths]
    if unrelated:
        raise GitCommitError(f"unrelated staged changes exist: {', '.join(unrelated)}")
    diff_args = ["git", "diff", "--", *rel_paths] if dry_run else ["git", "diff", "--cached", "--", *rel_paths]
    if not dry_run:
        subprocess.run(["git", "add", "--", *rel_paths], cwd=str(root), check=False, timeout=settings.subprocess_timeout)
    diff = subprocess.run(
        diff_args,
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "paths": rel_paths, "staged_diff": diff.stdout}
    proc = subprocess.run(
        ["git", "commit", "-m", message, "--", *rel_paths],
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )
    return {
        "ok": proc.returncode == 0,
        "dry_run": False,
        "paths": rel_paths,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "staged_diff": diff.stdout,
    }
