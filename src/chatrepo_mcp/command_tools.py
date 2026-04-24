from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings


class CommandPolicyError(ValueError):
    """Raised when a command is not allowed by the MCP command policy."""


@dataclass(frozen=True)
class CommandRule:
    prefix: tuple[str, ...]
    allow_extra_args: bool = False


ALLOWED_COMMANDS = (
    CommandRule(("git", "status", "--short")),
    CommandRule(("git", "status", "--short", "--branch")),
    CommandRule(("git", "diff", "--check")),
    CommandRule(("git", "diff", "--name-only")),
    CommandRule(("npm", "run", "test", "-w", "packages/agent", "--", "--run")),
    CommandRule(("npm", "run", "test", "-w", "packages/gateway", "--", "--run")),
    CommandRule(("npm", "run", "test:fast", "-w", "packages/integration")),
    CommandRule(("npx", "vitest", "run"), allow_extra_args=True),
    CommandRule(("npx", "tsx", "tests/telegram/scenarios/"), allow_extra_args=True),
)

SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "$(", "`"}
DENIED_EXECUTABLES = {"curl", "wget", "ssh", "scp", "rsync", "rm", "cat"}


def _split_command(command: str) -> list[str]:
    if any(token in command for token in SHELL_TOKENS):
        raise CommandPolicyError("shell operators are not allowed")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise CommandPolicyError(f"invalid command syntax: {exc}") from exc
    if not parts:
        raise CommandPolicyError("command must not be empty")
    if parts[0] in DENIED_EXECUTABLES:
        raise CommandPolicyError(f"command executable is not allowed: {parts[0]}")
    return parts


def _matches_rule(parts: list[str], rule: CommandRule) -> bool:
    prefix = list(rule.prefix)
    if rule.prefix == ("npx", "tsx", "tests/telegram/scenarios/"):
        return (
            len(parts) >= 3
            and parts[0] == "npx"
            and parts[1] == "tsx"
            and parts[2].startswith("tests/telegram/scenarios/")
            and parts[2].endswith(".test.ts")
        )
    if rule.allow_extra_args:
        return parts[: len(prefix)] == prefix
    return parts == prefix


def _ensure_allowed(parts: list[str]) -> None:
    if any(_matches_rule(parts, rule) for rule in ALLOWED_COMMANDS):
        return
    raise CommandPolicyError("command is not allowlisted")


def run_command(command: str, settings: Settings, *, timeout_ms: int | None = None) -> dict[str, Any]:
    parts = _split_command(command)
    _ensure_allowed(parts)
    effective_timeout_ms = min(timeout_ms or settings.command_timeout_ms, settings.command_timeout_ms)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            parts,
            cwd=str(settings.project_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=effective_timeout_ms / 1000,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": proc.returncode == 0,
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[: settings.max_command_output_chars],
            "stderr": proc.stderr[: settings.max_command_output_chars],
            "duration_ms": duration_ms,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "command": command,
            "exit_code": None,
            "stdout": (exc.stdout or "")[: settings.max_command_output_chars],
            "stderr": (exc.stderr or "")[: settings.max_command_output_chars],
            "duration_ms": duration_ms,
            "timed_out": True,
        }
