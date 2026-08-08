from __future__ import annotations

import hashlib
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
from datetime import UTC
from pathlib import Path
from typing import Any

from . import profile as _profile_module
from .bounded_subprocess import run_bounded
from .config import Settings
from .git_tools import _repo_rel, _resolve_repo_toplevel
from .output_store import (
    ArtifactQuotaError,
    OutputArtifact,
    StreamingRedactor,
    artifact_reference,
    inline_head_tail,
    redact_text,
    store_for,
)
from .parsers import parse_command_output
from .profile import DEFAULT_PRESETS, load_repo_profile, resolve_presets_for_dir
from .resource_profile import ResourceBusyError, acquire_heavy_operation
from .runtime_env import command_environment
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

JOB_PROCS: dict[str, subprocess.Popen[bytes]] = {}
JOB_DRAIN_THREADS: dict[str, tuple[threading.Thread, threading.Thread]] = {}
JOB_CAPTURE_ERRORS: dict[str, list[str]] = {}
JOB_LOCK = threading.RLock()
AUDIT_LOCK = threading.RLock()

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
            if (normalized == prefix or normalized.startswith(prefix + " ")) and not confirmed:
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
    return redact_text(_as_text(text))


def _safe_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


def _utc_now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        proc = run_bounded(
            ["/bin/bash", "-lc", _bash_command(f"command -v {binary}", settings)],
            cwd=str(cwd),
            env=env,
            timeout=5,
            max_stdout_bytes=4_096,
            max_stderr_bytes=4_096,
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


def _command_env(extra_env: dict[str, str] | None = None, settings: Settings | None = None) -> dict[str, str]:
    try:
        if settings is None:
            env = os.environ.copy()
            for key, value in (extra_env or {}).items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError(f"invalid env key: {key}")
                env[key] = value
            return env
        return command_environment(settings, extra_env)
    except ValueError as exc:
        raise CommandPolicyError(str(exc)) from exc


def _audit(settings: Settings, payload: dict[str, Any]) -> None:
    path = settings.command_audit_log_path
    try:
        with AUDIT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= 10 * 1024 * 1024:
                for stale in path.parent.glob(f"{path.name}.*"):
                    suffix = stale.name.removeprefix(f"{path.name}.")
                    if suffix.isdigit() and int(suffix) > 5:
                        stale.unlink(missing_ok=True)
                for index in range(5, 0, -1):
                    source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
                    target = path.with_name(f"{path.name}.{index}")
                    if source.exists():
                        if index == 5:
                            target.unlink(missing_ok=True)
                        os.replace(source, target)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _command_log_paths(settings: Settings, log_id: str) -> tuple[Path, Path, Path]:
    root = settings.command_jobs_dir / "artifacts"
    return root / f"{log_id}.json", root / f"{log_id}.out", root / f"{log_id}.err"


def _lock_path(settings: Settings, concurrency_key: str) -> Path:
    return settings.command_jobs_dir / "locks" / f"{_safe_key(concurrency_key)}.json"


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
    requested_output_limit = max_output_chars
    output_limit = min(
        settings.default_inline_output_bytes
        if requested_output_limit is not None and requested_output_limit <= 0
        else requested_output_limit
        if requested_output_limit is not None
        else settings.default_inline_output_bytes,
        settings.max_command_output_chars,
    )
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env, settings)
    started = time.monotonic()
    resolved = _resolved_binaries(run_cwd, run_env, settings)
    log_id = str(uuid.uuid4())
    meta_path, out_path, err_path = _command_log_paths(settings, log_id)
    store = store_for(settings)
    stdout_artifact: OutputArtifact | None = None
    stderr_artifact: OutputArtifact | None = None
    _audit(settings, {
        "timestamp": int(time.time()), "event": "command_started", "request_id": log_id,
        "tool": "run_command", "args_fingerprint": hashlib.sha256(
            f"{normalized}\0{run_cwd}".encode()
        ).hexdigest(),
    })
    try:
        heavy_lease = acquire_heavy_operation(
            settings, tool="run_command", cwd=str(run_cwd), request_id=log_id,
        )
    except ResourceBusyError:
        _audit(settings, {
            "timestamp": int(time.time()), "event": "command_finished", "request_id": log_id,
            "tool": "run_command", "args_fingerprint": hashlib.sha256(
                f"{normalized}\0{run_cwd}".encode()
            ).hexdigest(),
            "duration_ms": 0, "stdout_bytes": 0, "stderr_bytes": 0,
            "status": "failed", "error_kind": "resource_busy",
        })
        raise
    try:
        stdout_artifact = store.open(log_id, "stdout", out_path, output_limit)
        stderr_artifact = store.open(log_id, "stderr", err_path, output_limit)
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", _bash_command(normalized, settings)],
            cwd=str(run_cwd),
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        def cancel_process() -> None:
            _terminate_process_group(proc.pid, grace_seconds=settings.kill_grace_ms / 1000)

        heavy_lease.set_cancel(cancel_process)
        drain_errors: list[BaseException] = []

        def drain(pipe: Any, artifact: OutputArtifact) -> None:
            try:
                while chunk := pipe.read(65536):
                    artifact.write(chunk)
            except (OSError, ValueError) as exc:
                drain_errors.append(exc)
                _terminate_process_group(proc.pid, grace_seconds=settings.kill_grace_ms / 1000)

        stdout_thread = threading.Thread(target=drain, args=(proc.stdout, stdout_artifact), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(proc.stderr, stderr_artifact), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            proc.wait(timeout=effective_timeout_ms / 1000)
            timed_out = False
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc.pid, grace_seconds=settings.kill_grace_ms / 1000)
            proc.wait()
            timed_out = True
        drain_cleanup, process_group_cleaned, forced_pipe_close = _finalize_process_drains(
            proc, (stdout_thread, stderr_thread),
            grace_seconds=settings.kill_grace_ms / 1000,
        )
        stdout_artifact.close()
        stderr_artifact.close()
        if drain_errors:
            raise OSError(f"artifact capture failed: {drain_errors[0]}")
        stdout_limit = output_limit
        stderr_limit = output_limit
        if stdout_artifact.bytes_written and stderr_artifact.bytes_written:
            stdout_limit = (output_limit + 1) // 2
            stderr_limit = output_limit - stdout_limit
        stdout = inline_head_tail(
            stdout_artifact.head, stdout_artifact.preview,
            total_bytes=stdout_artifact.bytes_written, maximum=stdout_limit,
        )
        stderr = inline_head_tail(
            stderr_artifact.head, stderr_artifact.preview,
            total_bytes=stderr_artifact.bytes_written, maximum=stderr_limit,
        )
        stdout_tail = _tail(stdout_artifact.preview, tail_lines)
        stderr_tail = _tail(stderr_artifact.preview, tail_lines)
        duration_ms = int((time.monotonic() - started) * 1000)
        result: dict[str, Any] = {
            "ok": proc.returncode == 0 and not timed_out,
            "command": _redact(normalized),
            "exit_code": None if timed_out else proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "full_output_truncated": stdout_artifact.bytes_written > len(stdout.encode("utf-8")) or stderr_artifact.bytes_written > len(stderr.encode("utf-8")),
            "duration_ms": duration_ms,
            "timed_out": timed_out,
            "cwd": str(run_cwd),
            "resolved_binaries": resolved,
            "log_id": log_id,
            "stdout_bytes": stdout_artifact.bytes_written,
            "stderr_bytes": stderr_artifact.bytes_written,
            "stdout_sha256": stdout_artifact.sha256,
            "stderr_sha256": stderr_artifact.sha256,
            "drain_cleanup": drain_cleanup,
            "forced_pipe_close": forced_pipe_close,
            "process_group_cleaned": process_group_cleaned,
        }
        if timed_out:
            result["error_kind"] = "command_timeout"
        parsed = parse_command_output(normalized, stdout_artifact.preview, stderr_artifact.preview, parse_kind=parse_kind)
        if parsed:
            result["parsed"] = parsed
            result["summary"] = parsed.get("summary")
        else:
            result["summary"] = "exit 0" if result["ok"] else f"exit {result['exit_code']}"
        store.write_aux(
            log_id,
            meta_path,
            json.dumps({
                "log_id": log_id, "command": _redact(normalized), "cwd": str(run_cwd),
                "exit_code": result["exit_code"], "duration_ms": duration_ms, "timed_out": timed_out,
                "created_at": time.time(), "complete": True,
                "stdout_bytes": stdout_artifact.bytes_written, "stderr_bytes": stderr_artifact.bytes_written,
                "stdout_sha256": stdout_artifact.sha256, "stderr_sha256": stderr_artifact.sha256,
                "drain_cleanup": drain_cleanup, "forced_pipe_close": forced_pipe_close,
                "process_group_cleaned": process_group_cleaned,
            }, ensure_ascii=False, sort_keys=True).encode(),
        )
        reference = artifact_reference(
            log_id,
            complete=True,
            reason="inline_limit" if result["full_output_truncated"] else "none",
        )
        result["artifact"] = reference
        result["continuation"] = reference["continuation"]
        result["receipt"] = reference["receipt"]
        result["receipt"]["configured"]["inline_output_bytes"] = settings.default_inline_output_bytes
        result["receipt"]["applied"]["inline_output_bytes"] = output_limit
        result["receipt"]["requested"] = (
            {"inline_output_bytes": requested_output_limit}
            if requested_output_limit is not None else {}
        )
        result["receipt"]["returned"] = {
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        }
        result["receipt"]["total"] = {
            "stdout_bytes": stdout_artifact.bytes_written,
            "stderr_bytes": stderr_artifact.bytes_written,
        }
    except (OSError, ArtifactQuotaError) as exc:
        for artifact in (stdout_artifact, stderr_artifact):
            if artifact is not None:
                try:
                    artifact.close()
                except OSError:
                    pass
        if stdout_artifact is not None or stderr_artifact is not None:
            store.abort_artifact(log_id)
        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            "ok": False,
            "error_kind": "artifact_capture_failed" if isinstance(exc, ArtifactQuotaError) or stdout_artifact is not None else "command_spawn_error",
            "command": _redact(normalized),
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "duration_ms": duration_ms,
            "timed_out": False,
            "cwd": str(run_cwd),
            "log_id": log_id,
        }
    _audit(
        settings,
        {
            "timestamp": int(time.time()),
            "event": "command_finished", "request_id": log_id, "tool": "run_command",
            "args_fingerprint": hashlib.sha256(f"{normalized}\0{run_cwd}".encode()).hexdigest(),
            "cwd": str(run_cwd),
            "exit_code": result["exit_code"],
            "duration_ms": result["duration_ms"],
            "timed_out": result["timed_out"],
            "stdout_chars": len(str(result.get("stdout", ""))),
            "stderr_chars": len(str(result.get("stderr", ""))),
            "stdout_bytes": result.get("stdout_bytes", 0),
            "stderr_bytes": result.get("stderr_bytes", 0),
            "status": "completed" if result.get("ok") else "failed",
            "policy_source": "preset" if policy_exempt else "direct",
        },
    )
    heavy_lease.release()
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
        fingerprint = hashlib.sha256(
            "\0".join((str(Path(settings.project_root, str(preset_cwd or "")).resolve()), preset, _canonical(command))).encode()
        ).hexdigest()
        result = start_command_job(
            command,
            settings,
            timeout_ms=effective_timeout,
            cwd=preset_cwd or None,
            tail_lines=tail_lines,
            policy_exempt=True,
            concurrency_key=f"preset:{fingerprint}",
            on_conflict="attach",
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
    root = settings.command_jobs_dir / "artifacts"
    return root / f"{job_id}.json", root / f"{job_id}.out", root / f"{job_id}.err"


def _read_job_meta(settings: Settings, job_id: str) -> dict[str, Any]:
    meta_path, _, _ = _job_paths(settings, job_id)
    with JOB_LOCK:
        if not meta_path.exists():
            raise FileNotFoundError(f"job not found: {job_id}")
        return json.loads(meta_path.read_text(encoding="utf-8"))


def _write_job_meta(settings: Settings, job_id: str, meta: dict[str, Any]) -> None:
    meta_path, _, _ = _job_paths(settings, job_id)
    data = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode()
    with JOB_LOCK:
        store_for(settings).write_aux(job_id, meta_path, data)


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
    audit_tool: str = "start_command_job",
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
    run_env = _command_env(env, settings)
    job_id = str(uuid.uuid4())
    started_monotonic = time.monotonic()
    meta_path, out_path, err_path = _job_paths(settings, job_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    store = store_for(settings)
    fingerprint = hashlib.sha256(f"{normalized}\0{run_cwd}".encode()).hexdigest()
    _audit(settings, {
        "timestamp": int(time.time()), "event": "heavy_started", "request_id": job_id,
        "tool": audit_tool, "args_fingerprint": fingerprint,
    })
    try:
        heavy_lease = acquire_heavy_operation(
            settings,
            tool=audit_tool,
            cwd=str(run_cwd),
            request_id=job_id,
            cancel_tool="cancel_command_job",
            cancel_id=job_id,
        )
    except ResourceBusyError:
        _audit(settings, {
            "timestamp": int(time.time()), "event": "heavy_finished", "request_id": job_id,
            "tool": audit_tool, "args_fingerprint": fingerprint,
            "duration_ms": 0, "bytes": 0, "status": "failed",
            "error_kind": "resource_busy",
        })
        raise
    store.acquire_lifecycle(job_id)
    out_artifact: OutputArtifact | None = None
    err_artifact: OutputArtifact | None = None
    proc: subprocess.Popen[bytes] | None = None

    def fail_start(exc: OSError | ValueError, error_kind: str) -> None:
        if proc is not None:
            _terminate_process_group(proc.pid, grace_seconds=0)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc.pid, grace_seconds=0)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        for output_artifact in (out_artifact, err_artifact):
            if output_artifact is not None:
                output_artifact.abort()
        store.abort_artifact(job_id)
        store.release_lifecycle(job_id)
        heavy_lease.release()
        _audit(settings, {
            "timestamp": int(time.time()), "event": "heavy_finished", "request_id": job_id,
            "tool": audit_tool, "args_fingerprint": fingerprint,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            "bytes": 0, "status": "failed", "error_kind": error_kind,
            "error": str(exc),
        })

    try:
        out_artifact = store.open(job_id, "stdout", out_path, settings.max_command_output_chars)
        err_artifact = store.open(job_id, "stderr", err_path, settings.max_command_output_chars)
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", _bash_command(normalized, settings)],
            cwd=str(run_cwd),
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        fail_start(exc, "artifact_capture_failed" if out_artifact is not None else "command_spawn_error")
        raise

    assert proc is not None and out_artifact is not None and err_artifact is not None

    def drain(pipe: Any, artifact: OutputArtifact) -> None:
        try:
            while chunk := pipe.read(65536):
                artifact.write(chunk)
            artifact.close()
        except (OSError, ValueError) as exc:
            with JOB_LOCK:
                JOB_CAPTURE_ERRORS.setdefault(job_id, []).append(str(exc))
            _terminate_process_group(proc.pid, grace_seconds=0)

    out_thread = threading.Thread(target=drain, args=(proc.stdout, out_artifact), daemon=True, name=f"chatrepo-job-out-{job_id[:8]}")
    err_thread = threading.Thread(target=drain, args=(proc.stderr, err_artifact), daemon=True, name=f"chatrepo-job-err-{job_id[:8]}")
    def finish_heavy_audit() -> None:
        try:
            proc.wait()
            try:
                drain_cleanup, process_group_cleaned, forced_pipe_close = _finalize_process_drains(
                    proc, (out_thread, err_thread),
                    grace_seconds=settings.kill_grace_ms / 1000,
                )
            except OSError as exc:
                with JOB_LOCK:
                    JOB_CAPTURE_ERRORS.setdefault(job_id, []).append(str(exc))
                drain_cleanup, process_group_cleaned, forced_pipe_close = "failed", False, True
            with JOB_LOCK:
                terminal = {"completed", "failed", "cancelled", "timed_out"}
                finished_meta = _read_job_meta(settings, job_id)
                capture_errors = JOB_CAPTURE_ERRORS.pop(job_id, [])
                status = str(finished_meta.get("status", "running"))
                if status not in terminal:
                    if finished_meta.get("cancel_requested"):
                        status = "cancelled"
                        finished_meta["termination_reason"] = "user_cancel"
                    elif finished_meta.get("timed_out") or finished_meta.get("termination_reason") == "timeout":
                        status = "timed_out"
                        finished_meta["termination_reason"] = "timeout"
                    elif capture_errors or proc.returncode not in {0, None}:
                        status = "failed"
                        finished_meta["termination_reason"] = (
                            "artifact_capture_failed" if capture_errors else "nonzero_exit"
                        )
                    else:
                        status = "completed"
                        finished_meta["termination_reason"] = "completed"
                if capture_errors:
                    status = "failed"
                    finished_meta["error_kind"] = "artifact_capture_failed"
                    finished_meta["capture_error"] = capture_errors[0]
                    out_artifact.abort()
                    err_artifact.abort()
                    store.abort_artifact(job_id, preserve_manifest=True)
                    finished_meta.pop("stdout_sha256", None)
                    finished_meta.pop("stderr_sha256", None)
                finished_meta.update({
                    "status": status,
                    "complete": not capture_errors,
                    "exit_code": proc.returncode,
                    "finished_at": finished_meta.get("finished_at") or _utc_now(),
                    "stdout_bytes": 0 if capture_errors else out_artifact.bytes_written,
                    "stderr_bytes": 0 if capture_errors else err_artifact.bytes_written,
                    "output_truncated": (
                        out_artifact.bytes_written > len(out_artifact.head.encode("utf-8"))
                        or err_artifact.bytes_written > len(err_artifact.head.encode("utf-8"))
                    ),
                    "drain_cleanup": drain_cleanup,
                    "forced_pipe_close": forced_pipe_close,
                    "process_group_cleaned": process_group_cleaned,
                })
                if not capture_errors:
                    finished_meta["stdout_sha256"] = out_artifact.sha256
                    finished_meta["stderr_sha256"] = err_artifact.sha256
                _write_job_meta(settings, job_id, finished_meta)
                JOB_PROCS.pop(job_id, None)
                JOB_DRAIN_THREADS.pop(job_id, None)
            _clear_lock(settings, finished_meta.get("concurrency_key"), job_id)
            _audit(settings, {
                "timestamp": int(time.time()), "event": "heavy_finished", "request_id": job_id,
                "tool": audit_tool, "args_fingerprint": fingerprint,
                "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                "bytes": out_artifact.bytes_written + err_artifact.bytes_written,
                "status": str(finished_meta["status"]),
            })
        finally:
            heavy_lease.release()
            store.release_lifecycle(job_id)

    now = _utc_now()
    meta = {
        "job_id": job_id,
        "command": _redact(normalized),
        "cwd": str(run_cwd),
        "pid": proc.pid,
        "pgid": proc.pid,
        "started_at": time.time(),
        "started_at_rfc3339": now,
        "finished_at": None,
        "last_output_at": now,
        "timeout_ms": timeout_ms or settings.command_timeout_ms,
        "tail_lines": tail_lines,
        "status": "running",
        "complete": False,
        "exit_code": None,
        "term_signal": None,
        "termination_reason": None,
        "timed_out": False,
        "cancel_requested": False,
        "process_group_cleaned": False,
        "lock_owner_job_id": job_id if concurrency_key else None,
        "log_id": job_id,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "output_truncated": False,
        "concurrency_key": concurrency_key,
        "policy_source": "preset" if policy_exempt else "direct",
    }
    try:
        _write_job_meta(settings, job_id, meta)
    except (OSError, ValueError) as exc:
        fail_start(exc, "artifact_metadata_failed")
        raise
    out_thread.start()
    err_thread.start()
    with JOB_LOCK:
        JOB_PROCS[job_id] = proc
        JOB_DRAIN_THREADS[job_id] = (out_thread, err_thread)
    threading.Thread(
        target=finish_heavy_audit, daemon=True, name=f"chatrepo-heavy-{job_id[:8]}",
    ).start()
    if concurrency_key:
        _write_lock(settings, concurrency_key, job_id)
    watcher = threading.Thread(
        target=_watch_job_timeout,
        args=(job_id, settings, int(str(meta["timeout_ms"]))),
        daemon=True,
        name=f"chatrepo-job-timeout-{job_id[:8]}",
    )
    watcher.start()
    artifact = artifact_reference(job_id, complete=False, reason="inline_limit")
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "lock_status": "acquired" if concurrency_key else "none",
        "pid": proc.pid,
        "pgid": proc.pid,
        "log_id": job_id,
        "command": _redact(normalized),
        "concurrency_key": concurrency_key,
        "policy_source": "preset" if policy_exempt else "direct",
        "artifact": artifact,
        "continuation": artifact["continuation"],
        "receipt": artifact["receipt"],
    }


def _is_pid_running(pid: int) -> bool:
    try:
        if not Path(f"/proc/{pid}").exists():
            return False
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            parts = stat_path.read_text(encoding="utf-8", errors="replace").split()
            if len(parts) > 2 and parts[2] == "Z":
                return False
    except OSError:
        # Процесс может исчезнуть между проверкой каталога и чтением stat.
        return False
    return True


def _is_process_group_running(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return _is_pid_running(pgid)
    except PermissionError:
        return True


def _terminate_process_group(pid: int, *, grace_seconds: float = 1.0) -> str:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "not_running"
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _is_process_group_running(pid):
            return "terminated"
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not _is_process_group_running(pid):
            return "killed"
        time.sleep(0.05)
    return "kill_sent"


def _wait_process_group_cleaned(pgid: int, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_process_group_running(pgid):
            return True
        time.sleep(0.025)
    return not _is_process_group_running(pgid)


def _finalize_process_drains(
    proc: subprocess.Popen[bytes],
    threads: tuple[threading.Thread, threading.Thread],
    *,
    grace_seconds: float,
) -> tuple[str, bool, bool]:
    """Bound drain completion after the direct process has exited."""
    deadline = time.monotonic() + 1
    for thread in threads:
        thread.join(timeout=max(deadline - time.monotonic(), 0))
    cleanup = "not_needed"
    forced_pipe_close = False
    if any(thread.is_alive() for thread in threads):
        # Лидер уже мог выйти, но его process group продолжает жить и держать pipe.
        cleanup = _terminate_process_group(proc.pid, grace_seconds=grace_seconds)
        deadline = time.monotonic() + 1
        for thread in threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0))
    if any(thread.is_alive() for thread in threads):
        forced_pipe_close = True
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                _close_pipe_transport(pipe)
        for thread in threads:
            thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        raise OSError("subprocess output drains did not terminate")
    if forced_pipe_close:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
    return cleanup, _wait_process_group_cleaned(proc.pid), forced_pipe_close


def _close_pipe_transport(pipe: Any) -> None:
    """Interrupt a buffered reader without leaving it owning a stale descriptor."""
    raw = getattr(pipe, "raw", None)
    if raw is not None:
        raw.close()
    else:
        pipe.close()


def _watch_job_timeout(job_id: str, settings: Settings, timeout_ms: int) -> None:
    """Enforce a background-job timeout even if no client polls the job."""
    time.sleep(max(timeout_ms, 1) / 1000)
    try:
        meta = _read_job_meta(settings, job_id)
        pid = int(meta["pid"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return
    if not _is_process_group_running(pid):
        return
    meta["status"] = "terminating"
    meta["timed_out"] = True
    meta["termination_reason"] = "timeout"
    _write_job_meta(settings, job_id, meta)
    kill_status = _terminate_process_group(pid, grace_seconds=settings.kill_grace_ms / 1000)
    with JOB_LOCK:
        proc = JOB_PROCS.pop(job_id, None)
    if proc is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    meta["status"] = "timed_out"
    meta["kill_status"] = kill_status
    meta["timed_out"] = True
    meta["termination_reason"] = "timeout"
    meta["finished_at"] = _utc_now()
    meta["process_group_cleaned"] = _wait_process_group_cleaned(pid)
    _write_job_meta(settings, job_id, meta)
    _clear_lock(settings, meta.get("concurrency_key"), job_id)


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
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
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
    with JOB_LOCK:
        proc = JOB_PROCS.get(job_id)
    return_code = proc.poll() if proc is not None else None
    terminal_statuses = {"completed", "failed", "cancelled", "timed_out"}
    finishing = (
        proc is not None
        and return_code is not None
        and meta.get("status") not in terminal_statuses
    )
    running = finishing or (proc is not None and return_code is None) or (
        proc is None and _is_process_group_running(int(meta.get("pgid", pid)))
    )
    _, out_path, err_path = _job_paths(settings, job_id)
    if proc is None:
        _sanitize_legacy_artifact(out_path)
        _sanitize_legacy_artifact(err_path)
    stdout_bytes = out_path.stat().st_size if out_path.exists() else 0
    stderr_bytes = err_path.stat().st_size if err_path.exists() else 0
    stdout = _read_file_tail(out_path, settings.max_command_output_chars)
    stderr = _read_file_tail(err_path, settings.max_command_output_chars)
    meta["stdout_bytes"] = stdout_bytes
    meta["stderr_bytes"] = stderr_bytes
    if stdout or stderr:
        meta["last_output_at"] = _utc_now()
    duration_ms = int((time.time() - float(meta["started_at"])) * 1000)
    already_timed_out = meta.get("status") == "timed_out"
    timed_out = already_timed_out or (
        meta.get("status") in {"running", "terminating"}
        and duration_ms > int(meta.get("timeout_ms", settings.command_timeout_ms))
    )
    if timed_out and running:
        try:
            kill_status = _terminate_process_group(pid, grace_seconds=settings.kill_grace_ms / 1000)
        except TypeError:  # compatibility for injected/simple test doubles
            kill_status = _terminate_process_group(pid)
        running = False
        meta["status"] = "timed_out"
        meta["kill_status"] = kill_status
    elif meta.get("status") == "failed":
        running = False
    elif not timed_out:
        meta["status"] = "running" if running else "completed"
        if not running:
            meta["exit_code"] = return_code
            meta["finished_at"] = meta.get("finished_at") or _utc_now()
            meta["termination_reason"] = "completed" if return_code in {0, None} else "nonzero_exit"
            if return_code not in {0, None}:
                meta["status"] = "failed"
            with JOB_LOCK:
                capture_errors = JOB_CAPTURE_ERRORS.pop(job_id, [])
            if capture_errors:
                meta["status"] = "failed"
                meta["error_kind"] = "artifact_capture_failed"
                meta["capture_error"] = capture_errors[0]
            meta["process_group_cleaned"] = not _is_process_group_running(int(meta.get("pgid", pid)))
    if not running:
        _clear_lock(settings, meta.get("concurrency_key"), job_id)
    with JOB_LOCK:
        latest_meta = _read_job_meta(settings, job_id)
        if latest_meta.get("status") in terminal_statuses and meta.get("status") not in terminal_statuses:
            meta = latest_meta
            running = False
            timed_out = meta.get("status") == "timed_out"
        else:
            _write_job_meta(settings, job_id, meta)
    return {
        "ok": meta["status"] in {"running", "completed"},
        "job_id": job_id,
        "status": meta["status"],
        "running": running,
        "exit_code": meta.get("exit_code", return_code),
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "command": _redact(meta["command"]),
        "pid": pid,
        "pgid": int(meta.get("pgid", pid)),
        "kill_status": meta.get("kill_status"),
        "concurrency_key": meta.get("concurrency_key"),
        "process_alive": _is_pid_running(pid),
        "command_redacted": _redact(meta["command"]),
        "started_at": meta.get("started_at_rfc3339"),
        "finished_at": meta.get("finished_at"),
        "last_output_at": meta.get("last_output_at"),
        "term_signal": meta.get("term_signal"),
        "termination_reason": meta.get("termination_reason"),
        "cancel_requested": meta.get("cancel_requested", False),
        "process_group_cleaned": meta.get("process_group_cleaned", False),
        "lock_owner_job_id": meta.get("lock_owner_job_id"),
        "log_id": meta.get("log_id", job_id),
        "stdout_bytes": meta.get("stdout_bytes", 0),
        "stderr_bytes": meta.get("stderr_bytes", 0),
        "output_truncated": meta.get("output_truncated", False),
        "error_kind": meta.get("error_kind"),
        "capture_error": meta.get("capture_error"),
        "stdout_tail": _tail(stdout, tail_lines),
        "stderr_tail": _tail(stderr, tail_lines),
    }


def _read_file_tail(path: Path, maximum: int) -> str:
    if not path.exists() or maximum <= 0:
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(size - maximum, 0))
        return handle.read(maximum).decode("utf-8", errors="replace")


def _sanitize_legacy_artifact(path: Path) -> None:
    """Однократно обезвреживаем старые файлы, созданные до redact-before-persist."""
    artifact_id = path.name.rsplit(".", 1)[0]
    stream = "stdout" if path.suffix == ".out" else "stderr"
    if not path.exists() or (path.parent / f"{artifact_id}.{stream}.meta.json").exists():
        return
    temporary = path.with_suffix(path.suffix + ".redacting")
    redactor = StreamingRedactor()
    with path.open("rb") as source, temporary.open("wb") as target:
        while chunk := source.read(65_536):
            target.write(redactor.feed(chunk))
        target.write(redactor.feed(b"", final=True))
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


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
        meta_path, out_path, err_path = _job_paths(settings, log_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"log not found: {log_id}")
    if stream == "combined":
        path = settings.command_jobs_dir / "artifacts" / f"{log_id}.combined"
    else:
        path = err_path if stream == "stderr" else out_path
    pattern = re.compile(grep) if grep else None
    selected: list[str] = []
    selected_chars = 0
    line_count = 0
    truncated = False
    maximum = settings.max_response_chars
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_count, raw_line in enumerate(handle, start=1):
                line = raw_line.rstrip("\r\n")
                if start_line is not None and line_count < start_line:
                    continue
                if end_line is not None and line_count > end_line:
                    continue
                if pattern and not pattern.search(line):
                    continue
                rendered = f"{line_count}: {line}"
                if selected_chars + len(rendered) + (1 if selected else 0) > maximum:
                    truncated = True
                    continue
                selected.append(rendered)
                selected_chars += len(rendered) + (1 if selected_chars else 0)
    content = "\n".join(selected)
    return {
        "ok": True,
        "log_id": log_id,
        "stream": stream,
        "line_count": line_count,
        "content": content,
        "truncated": truncated,
        "meta": json.loads(meta_path.read_text(encoding="utf-8")),
    }


def summarize_command_log(log_id: str, settings: Settings, *, parser: str = "auto") -> dict[str, Any]:
    meta_path, out_path, err_path = _command_log_paths(settings, log_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"log not found: {log_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stdout = _read_file_head_tail(out_path, settings.max_command_output_chars)
    stderr = _read_file_head_tail(err_path, settings.max_command_output_chars)
    parsed = parse_command_output(str(meta.get("command", "")), stdout, stderr, parse_kind=parser)
    return {
        "ok": True,
        "log_id": log_id,
        "command": _redact(str(meta.get("command"))),
        "parsed": parsed,
        "summary": parsed.get("summary") if parsed else "no parser summary",
    }


def _read_file_head_tail(path: Path, maximum: int) -> str:
    if not path.exists() or maximum <= 0:
        return ""
    size = path.stat().st_size
    half = max(maximum // 2, 1)
    with path.open("rb") as handle:
        head = handle.read(half)
        if size <= half:
            return head.decode("utf-8", errors="replace")
        handle.seek(max(size - half, half))
        tail = handle.read(half)
    return (head + b"\n...<bounded summary sample>...\n" + tail).decode("utf-8", errors="replace")


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
    if meta.get("status") in {"completed", "failed", "cancelled", "timed_out"}:
        return {"ok": True, "job_id": job_id, "status": meta["status"], "cancelled": False}
    meta["cancel_requested"] = True
    meta["status"] = "terminating"
    _write_job_meta(settings, job_id, meta)
    kill_status = _terminate_process_group(pid, grace_seconds=settings.kill_grace_ms / 1000)
    status = "cancelled"
    meta["status"] = status
    meta["kill_status"] = kill_status
    meta["termination_reason"] = "user_cancel"
    meta["finished_at"] = _utc_now()
    with JOB_LOCK:
        proc = JOB_PROCS.pop(job_id, None)
    if proc is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    meta["process_group_cleaned"] = _wait_process_group_cleaned(int(meta.get("pgid", pid)))
    _clear_lock(settings, meta.get("concurrency_key"), job_id)
    _write_job_meta(settings, job_id, meta)
    return {"ok": True, "job_id": job_id, "status": status, "cancelled": True, "kill_status": kill_status, "process_alive": _is_pid_running(pid), "process_group_cleaned": meta["process_group_cleaned"]}


def list_command_jobs(
    settings: Settings,
    *,
    status: list[str] | None = None,
    cwd: str | None = None,
    limit: int = 100,
    include_finished: bool = True,
) -> dict[str, Any]:
    wanted = set(status or [])
    terminal = {"completed", "failed", "cancelled", "timed_out"}
    jobs: list[dict[str, Any]] = []
    settings.command_jobs_dir.mkdir(parents=True, exist_ok=True)
    roots = (settings.command_jobs_dir, settings.command_jobs_dir / "artifacts")
    for path in (candidate for root in roots for candidate in root.glob("*.json")):
        if path.name.endswith((".meta.json", ".digest.json")):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            job_id = str(meta["job_id"])
        except (OSError, KeyError, json.JSONDecodeError):
            continue
        if wanted and meta.get("status") not in wanted:
            continue
        if not include_finished and meta.get("status") in terminal:
            continue
        if cwd and str(meta.get("cwd")) != str(_resolve_cwd(cwd, settings)):
            continue
        jobs.append(get_job_status(job_id, settings))
    jobs.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    jobs = jobs[: max(1, min(limit, 1000))]
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def shutdown_command_jobs(settings: Settings) -> None:
    with JOB_LOCK:
        job_ids = list(JOB_PROCS)
    for job_id in job_ids:
        try:
            meta = _read_job_meta(settings, job_id)
            if meta.get("status") in {"running", "terminating", "queued"}:
                pid = int(meta["pid"])
                _terminate_process_group(pid, grace_seconds=settings.kill_grace_ms / 1000)
                meta.update(status="cancelled", termination_reason="server_shutdown", finished_at=_utc_now(), process_group_cleaned=not _is_process_group_running(pid))
                _clear_lock(settings, meta.get("concurrency_key"), job_id)
                _write_job_meta(settings, job_id, meta)
        except Exception:  # noqa: BLE001, S112 - shutdown remains best-effort for every job
            continue


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
    status = run_bounded(
        ["git", "diff", "--cached", "--name-only"],
        cwd=str(root),
        timeout=settings.subprocess_timeout,
        max_stdout_bytes=settings.max_response_chars,
        max_stderr_bytes=8_192,
        artifact_settings=settings,
    )
    if status.truncated:
        raise GitCommitError("staged path inventory exceeds bounded response; narrow the commit scope")
    staged = [line for line in status.stdout.splitlines() if line.strip()]
    unrelated = [path for path in staged if path not in rel_paths]
    if unrelated:
        raise GitCommitError(f"unrelated staged changes exist: {', '.join(unrelated)}")
    diff_args = ["git", "diff", "--", *rel_paths] if dry_run else ["git", "diff", "--cached", "--", *rel_paths]
    if not dry_run:
        add_result = run_bounded(
            ["git", "add", "--", *rel_paths], cwd=str(root),
            timeout=settings.subprocess_timeout, max_stdout_bytes=8_192, max_stderr_bytes=8_192,
        )
        if add_result.returncode != 0:
            raise GitCommitError(add_result.stderr or "git add failed")
    diff = run_bounded(
        diff_args,
        cwd=str(root),
        timeout=settings.subprocess_timeout,
        max_stdout_bytes=settings.max_response_chars,
        max_stderr_bytes=8_192,
        artifact_settings=settings,
    )
    if dry_run:
        return {
            "ok": True,
            "repo": _repo_rel(root, settings),
            "dry_run": True,
            "paths": rel_paths,
            "staged_diff": diff.stdout,
            "output_truncated": diff.stdout_truncated,
            "artifact": diff.artifact,
            "continuation": diff.artifact.get("continuation") if diff.artifact else None,
        }
    proc = run_bounded(
        ["git", "commit", "-m", message, "--", *rel_paths],
        cwd=str(root),
        timeout=settings.subprocess_timeout,
        max_stdout_bytes=settings.max_response_chars,
        max_stderr_bytes=settings.max_response_chars,
        artifact_settings=settings,
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
        "output_truncated": proc.truncated or diff.stdout_truncated,
        "artifact": diff.artifact if diff.stdout_truncated else proc.artifact,
        "continuation": (
            (diff.artifact or {}).get("continuation")
            if diff.stdout_truncated else (proc.artifact or {}).get("continuation")
        ),
    }
