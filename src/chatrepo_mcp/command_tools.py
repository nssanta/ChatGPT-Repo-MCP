from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .parsers import parse_command_output
from .profile import load_repo_profile
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
DENIED_WORDS = {"curl", "wget", "ssh", "scp", "rsync", "printenv", "sudo", "su"}
DESTRUCTIVE_WORDS = {"rm", "rmdir", "unlink", "mv", "chmod", "chown", "git push"}
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


TEST_PRESETS = {
    "agent_message_processor": "npx vitest run packages/agent/src/features/processing/message-processor.test.ts",
    "gateway_adapter": "npm run test -w packages/gateway -- --run",
    "tg_d59": "npx tsx tests/telegram/scenarios/d59-trust-mode-approval.test.ts",
    "tg_d62": "npx tsx tests/telegram/scenarios/d62-tool-confirmation-trust-modes.test.ts",
    "tg_d64": "npx tsx tests/telegram/scenarios/d64-approval-tts-no-record-voice.test.ts",
    "tg_d67": "npx tsx tests/telegram/scenarios/d67-tool-trace-visibility-modes.test.ts",
    "full_agent_tests": "npm run test -w packages/agent -- --run",
    "full_gateway_tests": "npm run test -w packages/gateway -- --run",
}

JOB_PROCS: dict[str, subprocess.Popen[str]] = {}


def _split_command(command: str, *, allow_shell_operators: bool = False) -> list[str]:
    if not allow_shell_operators and any(token in command for token in SHELL_TOKENS):
        raise CommandPolicyError("shell operators are not allowed")
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise CommandPolicyError(f"invalid command syntax: {exc}") from exc
    if not parts:
        raise CommandPolicyError("command must not be empty")
    if any(part in DENIED_WORDS for part in parts):
        raise CommandPolicyError("command contains a forbidden executable or token")
    if any(is_blocked_relative(normalize_rel_path(part), _settings_for_block_check) for part in parts if "/" in part or part.startswith(".")):
        raise CommandPolicyError("command references a blocked path")
    return parts


_settings_for_block_check: Settings


def _check_command_policy(command: str, settings: Settings, *, confirmed: bool = False) -> str:
    full_repo = settings.command_policy_mode == "full_repo"
    globals()["_settings_for_block_check"] = settings
    parts = _split_command(command, allow_shell_operators=full_repo)
    normalized = " ".join(parts)
    if full_repo:
        return command
    for prefix in CONFIRMATION_COMMANDS:
        if normalized == prefix or normalized.startswith(prefix + " "):
            if not confirmed:
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


def _resolved_binaries(cwd: Path, env: dict[str, str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for binary in ("node", "npm", "npx"):
        proc = subprocess.run(
            ["/bin/bash", "-lc", _bash_command(f"command -v {binary}")],
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        result[binary] = proc.stdout.strip() or None
    return result


def _bash_command(command: str) -> str:
    return "\n".join(
        [
            "export NVM_DIR=${NVM_DIR:-/root/.nvm}",
            "[ -s /etc/profile ] && . /etc/profile",
            "[ -s /root/.profile ] && . /root/.profile",
            "[ -s /root/.bashrc ] && . /root/.bashrc",
            "[ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\"",
            "command -v npm >/dev/null 2>&1 || export PATH=/root/.nvm/versions/node/v22.22.0/bin:$PATH",
            command,
        ]
    )


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
) -> dict[str, Any]:
    normalized = _check_command_policy(command, settings, confirmed=confirmed)
    effective_timeout_ms = min(timeout_ms or settings.command_timeout_ms, settings.command_timeout_ms)
    output_limit = min(max_output_chars or settings.max_command_output_chars, settings.max_command_output_chars)
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env)
    if run_env.get("EVA_LIVE_TESTS") == "1" and settings.command_policy_mode != "full_repo":
        raise ConfirmationRequiredError("Live E2E commands require owner confirmation")
    started = time.monotonic()
    resolved = _resolved_binaries(run_cwd, run_env)
    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", _bash_command(normalized)],
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


def run_test_preset(
    preset: str,
    settings: Settings,
    *,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
    background: bool = False,
) -> dict[str, Any]:
    profile = load_repo_profile(settings)
    presets = {**{key: {"command": value, "parser": "auto"} for key, value in TEST_PRESETS.items()}, **profile.presets}
    if preset not in presets:
        raise CommandPolicyError(f"unknown test preset: {preset}")
    preset_config = presets[preset]
    command = str(preset_config["command"])
    effective_timeout = timeout_ms or (300_000 if preset.startswith("full_") else settings.command_timeout_ms)
    if background:
        return start_command_job(command, settings, timeout_ms=effective_timeout, tail_lines=tail_lines)
    return run_command(
        command,
        settings,
        timeout_ms=effective_timeout,
        tail_lines=tail_lines,
        parse_kind=str(preset_config.get("parser", "auto")),
    )


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
    normalized = _check_command_policy(command, settings, confirmed=confirmed)
    run_cwd = _resolve_cwd(cwd, settings)
    run_env = _command_env(env)
    if run_env.get("EVA_LIVE_TESTS") == "1" and settings.command_policy_mode != "full_repo" and not confirmed:
        raise ConfirmationRequiredError("Live E2E commands require owner confirmation")
    job_id = uuid.uuid4().hex
    meta_path, out_path, err_path = _job_paths(settings, job_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    out_handle = out_path.open("w", encoding="utf-8")
    err_handle = err_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", _bash_command(normalized)],
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
    }
    _write_job_meta(settings, job_id, meta)
    if concurrency_key:
        _write_lock(settings, concurrency_key, job_id)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "lock_status": "acquired" if concurrency_key else "none",
        "pid": proc.pid,
        "command": _redact(normalized),
        "concurrency_key": concurrency_key,
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
    timed_out = running and duration_ms > int(meta.get("timeout_ms", settings.command_timeout_ms))
    if timed_out:
        kill_status = _terminate_process_group(pid)
        running = False
        meta["status"] = "timed_out"
        meta["kill_status"] = kill_status
    else:
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
    alternatives = []
    if "&&" in command:
        alternatives = [part.strip() for part in command.split("&&") if part.strip()]
    elif ";" in command:
        alternatives = [part.strip() for part in command.split(";") if part.strip()]
    elif "|" in command:
        alternatives = [part.strip() for part in command.split("|") if part.strip()]
    try:
        normalized = _check_command_policy(command, settings, confirmed=confirmed)
        result = {"ok": True, "allowed": True, "command": _redact(normalized)}
        if alternatives:
            result["safe_split"] = alternatives
            result["safe_alternative"] = "Prefer run_commands with these split commands when possible."
        return result
    except ConfirmationRequiredError as exc:
        return {
            "ok": False,
            "allowed": False,
            "error_kind": "confirmation_required",
            "reason": str(exc),
            "safe_alternative": "Use confirmed=true only after owner confirmation, or use a safer preset.",
        }
    except CommandPolicyError as exc:
        return {
            "ok": False,
            "allowed": False,
            "error_kind": "command_not_allowed",
            "reason": str(exc),
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
