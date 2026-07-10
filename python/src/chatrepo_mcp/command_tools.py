from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import profile as _profile_module
from .config import Settings
from .git_tools import _repo_rel, _resolve_repo_toplevel
from .parsers import parse_command_output
from .profile import DEFAULT_PRESETS, load_repo_profile, resolve_presets_for_dir
from .security import SecurityError, is_blocked_relative, normalize_rel_path
from .workspace import detect_stack, is_within_roots, resolve_roots


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


# Generic, stack-agnostic allowlist for `command_policy_mode="allowlist"`.
# Deliberately free of any project-specific paths/scripts: extend it per-repo
# via the `allowed_commands` section of `.chatrepo/mcp.yml` (see
# `_profile_command_overrides`).
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
    CommandRule("npx vitest run", allow_suffix=True),
)

# Generic destructive-but-common services that require explicit confirmation
# in `command_policy_mode="allowlist"`, in addition to whatever
# `_is_destructive` (settings.destructive_words) flags. Extend via the
# `confirmation_commands` section of `.chatrepo/mcp.yml`.
CONFIRMATION_COMMANDS = (
    "docker compose",
    "systemctl",
)

SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "$(", "`"}
_SHELL_SPLIT_RE = re.compile(r"\|\||&&|[;|]")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_WRAPPER_COMMANDS = {"sudo", "env"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|password|api[_-]?key)=([^\s]+)"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"npm_[A-Za-z0-9_]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"https?://[^\s/@]+:[^\s/@]+@[^\s]+"),
    re.compile(r"git@[^:\s]+:[^\s]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def _canonical(command: str) -> str:
    return " ".join(shlex.split(command))


# Back-compat module-level symbol: some call sites still `import TEST_PRESETS`
# for display purposes (e.g. listing built-in preset names). The previous
# project-specific hardcoded table has been removed; real preset resolution goes through
# `load_repo_profile(settings).presets` (and, from Phase 3 onward,
# `workspace.resolve_presets_for`). This mirrors the generic, stack-agnostic
# defaults from `profile.DEFAULT_PRESETS` so the symbol stays non-empty and
# import-safe without reintroducing project-specific commands.
TEST_PRESETS: dict[str, str] = {name: str(cfg["command"]) for name, cfg in DEFAULT_PRESETS.items()}

JOB_PROCS: dict[str, subprocess.Popen[str]] = {}

_UNRESTRICTED_MODES = {"unrestricted", "full_repo"}


def _split_segments(command: str) -> list[str]:
    """Split a shell command string into segments on ``; | && ||``."""
    return [segment.strip() for segment in _SHELL_SPLIT_RE.split(command) if segment.strip()]


def _segment_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _first_exec_token(segment: str) -> str | None:
    """First real executable token of a segment, skipping leading ``VAR=val`` assignments."""
    tokens = _segment_tokens(segment)
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return None
    return tokens[idx]


def _effective_tokens(segment: str) -> list[str]:
    """Tokens of a segment with leading ``VAR=val`` and ``sudo``/``env`` wrappers stripped.

    Used for destructive-pattern matching so ``sudo rm -rf /`` is recognized
    as the ``rm -rf`` pattern rather than being hidden behind the wrapper.
    """
    tokens = _segment_tokens(segment)
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    while idx < len(tokens) and tokens[idx] in _WRAPPER_COMMANDS:
        idx += 1
        while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
            idx += 1
        while idx < len(tokens) and tokens[idx].startswith("-"):
            idx += 1
    return tokens[idx:]


#: First two effective tokens of a raw ``git push`` invocation. Matched after unwrapping
#: ``sudo``/``env`` wrappers, same as ``_is_destructive``.
_GIT_PUSH_TOKENS = ("git", "push")


def _reject_raw_git_push(command: str) -> None:
    """Block a raw ``git push`` segment when a safe-mode caller invokes this check.

    Safe modes route pushing through ``git_push`` for audit and structural
    checks. Full mode skips this check deliberately because it promises an
    unrestricted shell.
    """
    for segment in _split_segments(command):
        tokens = _effective_tokens(segment)
        if len(tokens) >= 2 and tuple(tokens[:2]) == _GIT_PUSH_TOKENS:
            raise CommandPolicyError(
                "raw 'git push' is blocked by policy; use the git_push tool instead of run_command"
            )


def _deny_check(command: str, settings: Settings) -> None:
    """Raise ``CommandPolicyError`` if any segment's first token is denied.

    Matches only the first executable token of each ``;``/``|``/``&&``/``||``
    segment (after skipping leading ``VAR=val`` assignments) -- not any word
    anywhere in the command. This avoids false positives like
    ``git log --grep curl`` being blocked just because "curl" appears as an
    argument. An empty ``settings.denied_words`` denies nothing.
    """
    denied = {word.strip() for word in settings.denied_words if word.strip()}
    if not denied:
        return
    for segment in _split_segments(command):
        token = _first_exec_token(segment)
        if token and token in denied:
            raise CommandPolicyError(f"command uses a denied executable or token: {token}")


def _is_destructive(command: str, settings: Settings) -> bool:
    """True if any segment starts with one of ``settings.destructive_words``.

    Patterns may be single words (``rmdir``, ``dd``, ``mkfs``) or multi-word
    prefixes (``git push --force``, ``docker system prune``); they are
    matched as an exact token-prefix of the segment (after unwrapping
    ``sudo``/``env`` wrappers), not a substring search.
    """
    patterns = [_segment_tokens(pattern) for pattern in settings.destructive_words if pattern.strip()]
    patterns = [tokens for tokens in patterns if tokens]
    if not patterns:
        return False
    for segment in _split_segments(command):
        tokens = _effective_tokens(segment)
        if not tokens:
            continue
        for pattern_tokens in patterns:
            if len(tokens) >= len(pattern_tokens) and tokens[: len(pattern_tokens)] == pattern_tokens:
                return True
    return False


def _profile_command_overrides(settings: Settings) -> tuple[tuple[CommandRule, ...], tuple[str, ...]]:
    """Read ``allowed_commands``/``confirmation_commands`` overrides from ``.chatrepo/mcp.yml``.

    TODO(Phase 3): move this parsing into `profile.RepoProfile` once it grows
    first-class support for these sections; for now this reads the repo
    config file directly (via the shared simple-YAML parser) to avoid
    touching `profile.py` in this pass. Any parse failure degrades to "no
    overrides" rather than breaking command execution.
    """
    path = settings.project_root / ".chatrepo" / "mcp.yml"
    if not path.exists():
        return (), ()
    try:
        data = _profile_module._parse_simple_yaml(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return (), ()
    allowed: list[CommandRule] = []
    allowed_raw = data.get("allowed_commands")
    if isinstance(allowed_raw, list):
        for item in allowed_raw:
            if isinstance(item, str) and item.strip():
                allowed.append(CommandRule(item))
            elif isinstance(item, dict) and item.get("command"):
                allowed.append(CommandRule(str(item["command"]), allow_suffix=bool(item.get("allow_suffix", False))))
    confirmation_raw = data.get("confirmation_commands")
    confirmation = tuple(str(item) for item in confirmation_raw if str(item).strip()) if isinstance(confirmation_raw, list) else ()
    return tuple(allowed), confirmation


def _split_command(command: str, settings: Settings, *, allow_shell_operators: bool = False) -> list[str]:
    if not allow_shell_operators and any(token in command for token in SHELL_TOKENS):
        raise CommandPolicyError("shell operators are not allowed")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise CommandPolicyError(f"invalid command syntax: {exc}") from exc
    if not parts:
        raise CommandPolicyError("command must not be empty")
    if any(is_blocked_relative(normalize_rel_path(part), settings) for part in parts if "/" in part or part.startswith(".")):
        raise CommandPolicyError("command references a blocked path")
    return parts


def _check_command_policy(
    command: str,
    settings: Settings,
    *,
    confirmed: bool = False,
    policy_exempt: bool = False,
) -> str:
    """Apply ``settings.command_policy_mode`` to ``command`` and return the normalized command.

    Safe access checks `_reject_raw_git_push` before mode-specific handling;
    full access skips it and exposes the shell as requested.

    Three modes:
    - ``unrestricted`` (alias ``full_repo``): god-mode, returns ``command`` verbatim with
      no further policy checks. The `cwd` perimeter (`_resolve_cwd`) still applies upstream.
    - ``guarded``: shell operators allowed; `_deny_check` always applies;
      `_is_destructive` commands require `confirmed=True` or raise `ConfirmationRequiredError`.
    - ``allowlist``: shell operators forbidden; command must match `CONFIRMATION_COMMANDS`
      (confirmed) or `ALLOWED_COMMANDS`, both extendable via `.chatrepo/mcp.yml`, or it is
      rejected. `policy_exempt=True` (set by trusted internal callers such as
      `run_test_preset`) skips the allowlist membership check but still runs `_deny_check`
      and the destructive-confirmation gate.
    """
    if not settings.full_access:
        _reject_raw_git_push(command)

    mode = settings.command_policy_mode
    if mode in _UNRESTRICTED_MODES:
        return command

    if mode == "guarded":
        parts = _split_command(command, settings, allow_shell_operators=True)
        normalized = " ".join(parts)
        _deny_check(command, settings)
        if _is_destructive(command, settings) and not confirmed:
            raise ConfirmationRequiredError(
                "This command matches a destructive pattern (settings.destructive_words) "
                "and requires confirmed=true after explicit owner confirmation."
            )
        return normalized

    if mode == "allowlist":
        parts = _split_command(command, settings, allow_shell_operators=False)
        normalized = " ".join(parts)
        _deny_check(command, settings)
        if policy_exempt:
            if _is_destructive(command, settings) and not confirmed:
                raise ConfirmationRequiredError(
                    "This preset command matches a destructive pattern and requires "
                    "confirmed=true after explicit owner confirmation."
                )
            return normalized
        allowed_overrides, confirmation_overrides = _profile_command_overrides(settings)
        for prefix in (*CONFIRMATION_COMMANDS, *confirmation_overrides):
            if normalized == prefix or normalized.startswith(prefix + " "):
                if not confirmed:
                    raise ConfirmationRequiredError("This command requires owner confirmation")
        for rule in (*ALLOWED_COMMANDS, *allowed_overrides):
            allowed = _canonical(rule.command)
            if normalized == allowed or (rule.allow_suffix and normalized.startswith(allowed + " ")):
                return normalized
        raise CommandPolicyError("command is not allowlisted")

    raise CommandPolicyError(f"unknown command_policy_mode: {mode}")


def _resolve_cwd(cwd: str | None, settings: Settings) -> Path:
    roots = resolve_roots(settings)
    if cwd:
        cwd_path = Path(cwd)
        target = cwd_path.resolve() if cwd_path.is_absolute() else (settings.project_root.resolve() / cwd).resolve()
    else:
        target = settings.project_root.resolve()
    if not is_within_roots(target, roots):
        raise SecurityError(f"cwd escapes repository root: {cwd}")
    if not target.exists() or not target.is_dir():
        raise CommandPolicyError(f"cwd is not a directory: {cwd}")
    return target


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redact(text: str | bytes | None) -> str:
    text = _as_text(text)
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", redacted)
    return redacted


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


def _tail(text: str, tail_lines: int | None) -> str:
    if not tail_lines:
        return ""
    return "\n".join(text.splitlines()[-tail_lines:])


# cwd (str) -> resolved binary map. Detection runs at most once per cwd per
# process instead of spawning a subprocess per binary on every run_command call.
_BINARY_RESOLUTION_CACHE: dict[str, dict[str, str | None]] = {}

# Only probe for binaries that are actually relevant to the detected stack(s)
# of the target cwd -- no more hardcoded node/npm/npx checks on every command.
_STACK_BINARIES: dict[str, tuple[str, ...]] = {
    "go": ("go",),
    "python": ("python3", "pip"),
    "node": ("node", "npm", "npx"),
    "rust": ("cargo",),
}


def _resolved_binaries(cwd: Path, env: dict[str, str], settings: Settings) -> dict[str, str | None]:
    cache_key = str(cwd)
    cached = _BINARY_RESOLUTION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    binaries: set[str] = set()
    for stack in detect_stack(cwd):
        binaries.update(_STACK_BINARIES.get(stack, ()))
    result: dict[str, str | None] = {}
    for binary in sorted(binaries):
        proc = subprocess.run(
            ["/bin/bash", "-lc", _bash_command(f"command -v {binary}", settings)],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        result[binary] = proc.stdout.strip() or None
    _BINARY_RESOLUTION_CACHE[cache_key] = result
    return result


def _bash_command(command: str, settings: Settings) -> str:
    """Wrap ``command`` for ``bash -lc``, optionally prefixed with a user-configured prelude.

    `bash -lc` already sources the invoking user's own shell profile, so no
    project-specific toolchain path (e.g. a hardcoded nvm install) is
    injected here. Repos that need extra environment setup (nvm, pyenv,
    virtualenv activation, ...) configure it explicitly via
    ``settings.command_shell_prelude`` (env `COMMAND_SHELL_PRELUDE`).
    """
    prelude = settings.command_shell_prelude.strip()
    if not prelude:
        return command
    return f"{prelude}\n{command}"


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


def _command_log_paths(settings: Settings, log_id: str) -> tuple[Path, Path, Path]:
    root = settings.command_jobs_dir / "logs"
    return root / f"{log_id}.json", root / f"{log_id}.out", root / f"{log_id}.err"


def _lock_path(settings: Settings, concurrency_key: str) -> Path:
    return settings.command_jobs_dir / "locks" / f"{_safe_key(concurrency_key)}.json"


def _write_command_log(
    settings: Settings,
    *,
    command: str,
    cwd: str,
    stdout: str,
    stderr: str,
    result: dict[str, Any],
) -> str | None:
    log_id = uuid.uuid4().hex
    meta_path, out_path, err_path = _command_log_paths(settings, log_id)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(stdout, encoding="utf-8")
        err_path.write_text(stderr, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "log_id": log_id,
                    "command": _redact(command),
                    "cwd": cwd,
                    "exit_code": result.get("exit_code"),
                    "duration_ms": result.get("duration_ms"),
                    "timed_out": result.get("timed_out"),
                    "created_at": time.time(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return log_id
    except OSError:
        return None


def _attach_parse_and_log(
    result: dict[str, Any],
    settings: Settings,
    *,
    command: str,
    cwd: str,
    stdout: str,
    stderr: str,
    parse_kind: str | None,
) -> dict[str, Any]:
    parsed = parse_command_output(command, stdout, stderr, parse_kind=parse_kind)
    if parsed:
        result["parsed"] = parsed
        result["summary"] = parsed.get("summary")
    elif result.get("ok"):
        result["summary"] = "exit 0"
    else:
        result["summary"] = f"exit {result.get('exit_code')}" if result.get("exit_code") is not None else "failed"
    log_id = _write_command_log(settings, command=command, cwd=cwd, stdout=stdout, stderr=stderr, result=result)
    if log_id:
        result["log_id"] = log_id
    return result


def run_command(
    command: str,
    settings: Settings,
    *,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
    parse_kind: str | None = "auto",
    policy_exempt: bool = False,
) -> dict[str, Any]:
    """Run ``command`` through ``bash -lc`` under the repo's command policy.

    ``policy_exempt=True`` is for trusted internal callers only (e.g.
    `run_test_preset` resolving a preset from the repo profile / autodetect):
    in `allowlist` mode it skips the allowlist-membership check while still
    applying `_deny_check` and the destructive-confirmation gate. It has no
    effect in `guarded`/`unrestricted` modes. Direct end-user calls should
    always use the default `policy_exempt=False`.
    """
    normalized = _check_command_policy(command, settings, confirmed=confirmed, policy_exempt=policy_exempt)
    effective_timeout_ms = min(timeout_ms or settings.command_timeout_ms, settings.command_timeout_ms)
    output_limit = min(max_output_chars or settings.max_command_output_chars, settings.max_command_output_chars)
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env)
    started = time.monotonic()
    resolved = _resolved_binaries(run_cwd, run_env, settings)
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", _bash_command(normalized, settings)],
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
            "command": _redact(normalized),
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
        result = _attach_parse_and_log(
            result,
            settings,
            command=normalized,
            cwd=str(run_cwd),
            stdout=stdout,
            stderr=stderr,
            parse_kind=parse_kind,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _redact(exc.stdout)
        stderr = _redact(exc.stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": False,
            "error_kind": "command_timeout",
            "command": _redact(normalized),
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
        result = _attach_parse_and_log(
            result,
            settings,
            command=normalized,
            cwd=str(run_cwd),
            stdout=stdout,
            stderr=stderr,
            parse_kind=parse_kind,
        )
    _audit(
        settings,
        {
            "timestamp": int(time.time()),
            "command": _redact(normalized),
            "cwd": str(run_cwd),
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "stdout_chars": len(str(result.get("stdout", ""))),
            "stderr_chars": len(str(result.get("stderr", ""))),
            "policy_source": "preset" if policy_exempt else "direct",
        },
    )
    result["policy_source"] = "preset" if policy_exempt else "direct"
    return result


def run_commands(
    commands: list[str],
    settings: Settings,
    *,
    stop_on_failure: bool = False,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
    parse_kind: str | None = "auto",
) -> dict[str, Any]:
    results = []
    for command in commands:
        try:
            result = run_command(
                command,
                settings,
                timeout_ms=timeout_ms,
                tail_lines=tail_lines,
                confirmed=confirmed,
                parse_kind=parse_kind,
            )
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


def _resolve_test_preset(preset: str, settings: Settings) -> tuple[str, dict[str, Any], str]:
    """Resolve ``preset`` to ``(action, preset_config, resolved_cwd)``.

    Two supported forms:
    - A named preset key from the repo profile (built-in defaults or
      ``.chatrepo/mcp.yml`` ``presets:`` section), used verbatim -- kept for
      backward compatibility with pre-autodetect named presets (e.g.
      ``git_diff_check``).
    - ``"<action>"`` (``test``/``lint``/``typecheck``/``format``/``build``),
      resolved for the workspace root, or ``"<service_path>:<action>"``,
      resolved for a workspace sub-directory. Resolution merges
      ``workspace.resolve_presets_for`` (stack autodetect / Makefile targets)
      with any profile preset whose own ``cwd`` targets the same directory
      (profile entries win on a name collision -- they are explicit
      overrides).
    """
    profile = load_repo_profile(settings)
    if preset in profile.presets:
        cfg = profile.presets[preset]
        return preset, cfg, str(cfg.get("cwd") or "").strip("/")

    if ":" in preset:
        service_dir, _, action = preset.partition(":")
    else:
        service_dir, action = "", preset
    service_dir = service_dir.strip("/")
    action = action.strip()

    resolved = resolve_presets_for_dir(service_dir, settings)
    if not action or action not in resolved:
        available = ", ".join(sorted(resolved)) or "none detected"
        location = service_dir or "workspace root"
        raise CommandPolicyError(
            f"unknown test preset '{preset}': no action '{action}' for {location}. Available actions: {available}"
        )
    return action, resolved[action], service_dir


def run_test_preset(
    preset: str,
    settings: Settings,
    *,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
    background: bool = False,
) -> dict[str, Any]:
    """Run a preset resolved from stack autodetection, Makefile targets, and the repo profile.

    ``preset`` accepts either a bare action name (``test``/``lint``/
    ``typecheck``/``format``/``build``), resolved at the workspace root; a
    composite ``"<service_path>:<action>"`` (e.g. ``"api-gateway:test"``),
    resolved for that workspace sub-directory in a polyrepo; or a named
    preset key from the repo profile / ``.chatrepo/mcp.yml`` (backward
    compatible with the pre-autodetect named-preset behavior). See
    `_resolve_test_preset` for the full resolution order. Presets resolved
    here are treated as trusted (`policy_exempt=True`) regardless of
    `command_policy_mode`.
    """
    action, preset_config, resolved_cwd = _resolve_test_preset(preset, settings)
    command = str(preset_config["command"])
    preset_cwd = preset_config.get("cwd")
    if preset_cwd is None:
        preset_cwd = resolved_cwd
    effective_timeout = timeout_ms or preset_config.get("timeout_ms") or settings.command_timeout_ms
    parser = str(preset_config.get("parser", "auto"))
    if background:
        result = start_command_job(
            command,
            settings,
            timeout_ms=effective_timeout,
            cwd=preset_cwd or None,
            tail_lines=tail_lines,
            policy_exempt=True,
        )
    else:
        result = run_command(
            command,
            settings,
            timeout_ms=effective_timeout,
            cwd=preset_cwd or None,
            tail_lines=tail_lines,
            parse_kind=parser,
            policy_exempt=True,
        )
    result["preset"] = preset
    result["resolved_action"] = action
    result["resolved_cwd"] = preset_cwd or ""
    return result


def _job_paths(settings: Settings, job_id: str) -> tuple[Path, Path, Path]:
    root = settings.command_jobs_dir
    return root / f"{job_id}.json", root / f"{job_id}.out", root / f"{job_id}.err"


def _read_job_meta(settings: Settings, job_id: str) -> dict[str, Any]:
    meta_path, _, _ = _job_paths(settings, job_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"job not found: {job_id}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _write_job_meta(settings: Settings, job_id: str, meta: dict[str, Any]) -> None:
    meta_path, _, _ = _job_paths(settings, job_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def start_command_job(
    command: str,
    settings: Settings,
    *,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
    concurrency_key: str | None = None,
    on_conflict: str = "fail",
    policy_exempt: bool = False,
) -> dict[str, Any]:
    if on_conflict not in {"fail", "attach", "wait"}:
        raise CommandPolicyError("on_conflict must be one of: fail, attach, wait")
    if concurrency_key:
        existing = _active_lock_job(settings, concurrency_key)
        if existing:
            if on_conflict == "attach":
                return {"ok": True, "status": "attached", "lock_status": "attached", **existing}
            if on_conflict == "wait":
                deadline = time.time() + min((timeout_ms or settings.command_timeout_ms) / 1000, 30)
                while time.time() < deadline:
                    time.sleep(0.2)
                    existing = _active_lock_job(settings, concurrency_key)
                    if not existing:
                        break
                if existing:
                    return {"ok": False, "error_kind": "job_lock_conflict", "lock_status": "busy", **existing}
            else:
                return {"ok": False, "error_kind": "job_lock_conflict", "lock_status": "busy", **existing}
    normalized = _check_command_policy(command, settings, confirmed=confirmed, policy_exempt=policy_exempt)
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env)
    job_id = uuid.uuid4().hex
    meta_path, out_path, err_path = _job_paths(settings, job_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    out_handle = out_path.open("w", encoding="utf-8")
    err_handle = err_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", _bash_command(normalized, settings)],
        cwd=str(run_cwd),
        env=run_env,
        text=True,
        stdout=out_handle,
        stderr=err_handle,
        start_new_session=True,
    )
    out_handle.close()
    err_handle.close()
    JOB_PROCS[job_id] = proc
    meta = {
        "job_id": job_id,
        "command": _redact(normalized),
        "cwd": str(run_cwd),
        "pid": proc.pid,
        "started_at": time.time(),
        "timeout_ms": timeout_ms or settings.command_timeout_ms,
        "tail_lines": tail_lines,
        "status": "running",
        "concurrency_key": concurrency_key,
        "policy_source": "preset" if policy_exempt else "direct",
    }
    _write_job_meta(settings, job_id, meta)
    if concurrency_key:
        _write_lock(settings, concurrency_key, job_id)
    watcher = threading.Thread(
        target=_watch_job_timeout,
        args=(job_id, settings, int(str(meta["timeout_ms"]))),
        daemon=True,
        name=f"chatrepo-job-timeout-{job_id[:8]}",
    )
    watcher.start()
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "lock_status": "acquired" if concurrency_key else "none",
        "pid": proc.pid,
        "command": _redact(normalized),
        "concurrency_key": concurrency_key,
        "policy_source": "preset" if policy_exempt else "direct",
    }


def _is_pid_running(pid: int) -> bool:
    if not Path(f"/proc/{pid}").exists():
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        parts = stat_path.read_text(encoding="utf-8", errors="replace").split()
        if len(parts) > 2 and parts[2] == "Z":
            return False
    return True


def _terminate_process_group(pid: int, *, grace_seconds: float = 1.0) -> str:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "not_running"
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _is_pid_running(pid):
            return "terminated"
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    return "killed"


def _watch_job_timeout(job_id: str, settings: Settings, timeout_ms: int) -> None:
    """Enforce a background-job timeout even if no client polls the job."""
    time.sleep(max(timeout_ms, 1) / 1000)
    try:
        meta = _read_job_meta(settings, job_id)
        pid = int(meta["pid"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return
    if not _is_pid_running(pid):
        return
    kill_status = _terminate_process_group(pid)
    meta["status"] = "timed_out"
    meta["kill_status"] = kill_status
    _write_job_meta(settings, job_id, meta)
    _clear_lock(settings, meta.get("concurrency_key"), job_id)
    JOB_PROCS.pop(job_id, None)


def _write_lock(settings: Settings, concurrency_key: str, job_id: str) -> None:
    path = _lock_path(settings, concurrency_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"concurrency_key": concurrency_key, "job_id": job_id}, sort_keys=True), encoding="utf-8")


def _clear_lock(settings: Settings, concurrency_key: str | None, job_id: str) -> None:
    if not concurrency_key:
        return
    path = _lock_path(settings, concurrency_key)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return
    if data.get("job_id") == job_id:
        path.unlink(missing_ok=True)


def _active_lock_job(settings: Settings, concurrency_key: str) -> dict[str, Any] | None:
    path = _lock_path(settings, concurrency_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        job_id = str(data["job_id"])
        meta = _read_job_meta(settings, job_id)
        pid = int(meta["pid"])
    except Exception:
        path.unlink(missing_ok=True)
        return None
    if _is_pid_running(pid):
        return {
            "job_id": job_id,
            "attached_to_job_id": job_id,
            "pid": pid,
            "concurrency_key": concurrency_key,
            "command": _redact(str(meta.get("command", ""))),
            "status": meta.get("status", "running"),
        }
    _clear_lock(settings, concurrency_key, job_id)
    return None


def get_command_job(job_id: str, settings: Settings, *, tail_lines: int | None = 200) -> dict[str, Any]:
    meta = _read_job_meta(settings, job_id)
    pid = int(meta["pid"])
    proc = JOB_PROCS.get(job_id)
    return_code = proc.poll() if proc is not None else None
    if proc is not None and return_code is not None:
        JOB_PROCS.pop(job_id, None)
    running = return_code is None and _is_pid_running(pid)
    _, out_path, err_path = _job_paths(settings, job_id)
    raw_stdout = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    raw_stderr = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
    stdout = _redact(raw_stdout)
    stderr = _redact(raw_stderr)
    if stdout != raw_stdout and out_path.exists():
        out_path.write_text(stdout, encoding="utf-8")
    if stderr != raw_stderr and err_path.exists():
        err_path.write_text(stderr, encoding="utf-8")
    duration_ms = int((time.time() - float(meta["started_at"])) * 1000)
    already_timed_out = meta.get("status") == "timed_out"
    timed_out = already_timed_out or (
        running and duration_ms > int(meta.get("timeout_ms", settings.command_timeout_ms))
    )
    if timed_out and running:
        kill_status = _terminate_process_group(pid)
        running = False
        meta["status"] = "timed_out"
        meta["kill_status"] = kill_status
    elif not timed_out:
        meta["status"] = "running" if running else "completed"
    if not running:
        _clear_lock(settings, meta.get("concurrency_key"), job_id)
    _write_job_meta(settings, job_id, meta)
    return {
        "ok": not timed_out and not running,
        "job_id": job_id,
        "status": meta["status"],
        "running": running,
        "exit_code": return_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "command": _redact(meta["command"]),
        "pid": pid,
        "kill_status": meta.get("kill_status"),
        "concurrency_key": meta.get("concurrency_key"),
        "process_alive": _is_pid_running(pid),
        "stdout_tail": _tail(stdout, tail_lines),
        "stderr_tail": _tail(stderr, tail_lines),
    }


def get_job_status(job_id: str, settings: Settings) -> dict[str, Any]:
    return get_command_job(job_id, settings, tail_lines=0)


def get_command_log(
    log_id: str,
    settings: Settings,
    *,
    stream: str = "stdout",
    start_line: int | None = None,
    end_line: int | None = None,
    grep: str | None = None,
) -> dict[str, Any]:
    meta_path, out_path, err_path = _command_log_paths(settings, log_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"log not found: {log_id}")
    path = err_path if stream == "stderr" else out_path
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    lines = text.splitlines()
    selected = list(enumerate(lines, start=1))
    if grep:
        pattern = re.compile(grep)
        selected = [(line_no, line) for line_no, line in selected if pattern.search(line)]
    if start_line is not None:
        selected = [(line_no, line) for line_no, line in selected if line_no >= start_line]
    if end_line is not None:
        selected = [(line_no, line) for line_no, line in selected if line_no <= end_line]
    content = "\n".join(f"{line_no}: {line}" for line_no, line in selected)
    return {
        "ok": True,
        "log_id": log_id,
        "stream": stream,
        "line_count": len(lines),
        "content": content,
        "meta": json.loads(meta_path.read_text(encoding="utf-8")),
    }


def summarize_command_log(log_id: str, settings: Settings, *, parser: str = "auto") -> dict[str, Any]:
    meta_path, out_path, err_path = _command_log_paths(settings, log_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"log not found: {log_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stdout = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    stderr = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
    parsed = parse_command_output(str(meta.get("command", "")), stdout, stderr, parse_kind=parser)
    return {
        "ok": True,
        "log_id": log_id,
        "command": _redact(str(meta.get("command"))),
        "parsed": parsed,
        "summary": parsed.get("summary") if parsed else "no parser summary",
    }


def command_policy_check(command: str, settings: Settings, *, confirmed: bool = False) -> dict[str, Any]:
    """Dry-run `_check_command_policy` and explain the outcome (mode, allowed/denied/needs-confirmation)."""
    mode = settings.command_policy_mode
    alternatives = []
    if "&&" in command:
        alternatives = [part.strip() for part in command.split("&&") if part.strip()]
    elif ";" in command:
        alternatives = [part.strip() for part in command.split(";") if part.strip()]
    elif "|" in command:
        alternatives = [part.strip() for part in command.split("|") if part.strip()]
    try:
        normalized = _check_command_policy(command, settings, confirmed=confirmed)
        result = {
            "ok": True,
            "allowed": True,
            "mode": mode,
            "command": _redact(normalized),
            "explanation": f"allowed under command_policy_mode={mode}",
        }
        if alternatives:
            result["safe_split"] = alternatives
            result["safe_alternative"] = "Prefer run_commands with these split commands when possible."
        return result
    except ConfirmationRequiredError as exc:
        return {
            "ok": False,
            "allowed": False,
            "mode": mode,
            "error_kind": "confirmation_required",
            "reason": str(exc),
            "explanation": f"needs_confirmation under command_policy_mode={mode}: {exc}",
            "safe_alternative": "Use confirmed=true only after owner confirmation, or use a safer preset.",
        }
    except CommandPolicyError as exc:
        return {
            "ok": False,
            "allowed": False,
            "mode": mode,
            "error_kind": "command_not_allowed",
            "reason": str(exc),
            "explanation": f"denied under command_policy_mode={mode}: {exc}",
            "safe_split": alternatives,
        }


def cancel_command_job(job_id: str, settings: Settings) -> dict[str, Any]:
    meta = _read_job_meta(settings, job_id)
    pid = int(meta["pid"])
    kill_status = _terminate_process_group(pid)
    status = "cancelled" if kill_status in {"terminated", "killed"} else "completed"
    meta["status"] = status
    meta["kill_status"] = kill_status
    _clear_lock(settings, meta.get("concurrency_key"), job_id)
    _write_job_meta(settings, job_id, meta)
    return {"ok": True, "job_id": job_id, "status": status, "kill_status": kill_status, "process_alive": _is_pid_running(pid)}


def git_commit(
    message: str,
    paths: list[str],
    settings: Settings,
    *,
    dry_run: bool = True,
    repo: str | None = None,
) -> dict[str, Any]:
    if not message.strip():
        raise GitCommitError("commit message must not be empty")
    if not paths:
        raise GitCommitError("paths must not be empty")
    root = _resolve_repo_toplevel(repo, settings)
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
        return {
            "ok": True,
            "repo": _repo_rel(root, settings),
            "dry_run": True,
            "paths": rel_paths,
            "staged_diff": diff.stdout,
        }
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
        "repo": _repo_rel(root, settings),
        "dry_run": False,
        "paths": rel_paths,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "staged_diff": diff.stdout,
    }
