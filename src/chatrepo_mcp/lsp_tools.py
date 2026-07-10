"""One-shot code diagnostics (no LSP protocol).

Runs the native CLI checker for the detected/requested stack (``go vet``,
``pyright``/``ruff``/``compileall`` for Python, ``tsc --noEmit`` for
TypeScript) and normalizes their output into a single diagnostics shape.
Missing external tools degrade gracefully into ``missing_tools`` entries
instead of raising -- this module never fails just because a checker binary
isn't installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import git_tools, parsers, workspace
from .command_tools import _command_env, _redact
from .config import Settings
from .security import SecurityError, display_path, resolve_path_context

#: How "severe" each normalized severity is, for `severity_min` filtering.
_SEVERITY_ORDER = {"error": 3, "warning": 2, "info": 1, "hint": 0}

_DIAGNOSTICS_TIMEOUT = 120


def _severity_rank(severity: str | None) -> int:
    return _SEVERITY_ORDER.get(str(severity or "warning").lower(), 2)


def _normalize_path(cwd: Path, raw_path: str | None, settings: Settings) -> str:
    """Best-effort: make ``raw_path`` (as emitted by a checker) relative to ``project_root``."""
    if not raw_path:
        return raw_path or ""
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return display_path(candidate.resolve(), settings)
    except OSError:
        return str(candidate)


def _missing(tool: str, install_hint: str) -> dict[str, str]:
    return {"tool": tool, "install_hint": install_hint}


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str] | None:
    """Run ``cmd``; returns ``(returncode, stdout, stderr)`` or ``None`` on any execution failure."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_command_env(),
            capture_output=True,
            text=True,
            timeout=_DIAGNOSTICS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, _redact(proc.stdout), _redact(proc.stderr)


# --- Go ----------------------------------------------------------------------


def _go_diagnostics(cwd: Path, settings: Settings, paths: list[str] | None) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    if not shutil.which("go"):
        return [], [], [_missing("go", "install Go from https://go.dev/dl/ (e.g. `apt install golang-go` / `brew install go`)")]

    cmd = ["go", "vet", *(paths if paths else ["./..."])]
    ran = _run(cmd, cwd)
    if ran is None:
        return [], [], []
    _returncode, stdout, stderr = ran
    parsed = parsers.parse_gobuild_output(stdout, stderr)
    diagnostics = []
    for item in parsed.get("diagnostics", []):
        diagnostics.append(
            {
                "path": _normalize_path(cwd, item.get("path"), settings),
                "line": item.get("line"),
                "col": item.get("column"),
                "severity": "error",
                "code": None,
                "message": item.get("message"),
            }
        )
    return diagnostics, [" ".join(cmd)], []


# --- Python --------------------------------------------------------------------


def _pyright_diagnostics(cwd: Path, paths: list[str] | None, settings: Settings) -> list[dict[str, Any]] | None:
    binary = shutil.which("pyright")
    if not binary:
        return None
    cmd = [binary, "--outputjson", *(paths if paths else ["."])]
    ran = _run(cmd, cwd)
    if ran is None:
        return None
    _returncode, stdout, _stderr = ran
    try:
        data = json.loads(stdout)
    except (ValueError, KeyError):
        return None
    diagnostics = []
    for item in data.get("generalDiagnostics", []):
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        severity = str(item.get("severity") or "error").lower()
        if severity == "information":
            severity = "info"
        diagnostics.append(
            {
                "path": _normalize_path(cwd, item.get("file"), settings),
                "line": (start.get("line") + 1) if isinstance(start.get("line"), int) else None,
                "col": (start.get("character") + 1) if isinstance(start.get("character"), int) else None,
                "severity": severity,
                "code": item.get("rule"),
                "message": item.get("message"),
            }
        )
    return diagnostics


_RUFF_ERROR_CODE_PREFIXES = ("E9", "F82")


def _ruff_diagnostics(cwd: Path, paths: list[str] | None, settings: Settings) -> list[dict[str, Any]] | None:
    binary = shutil.which("ruff")
    if not binary:
        return None
    cmd = [binary, "check", "--output-format", "json", *(paths if paths else ["."])]
    ran = _run(cmd, cwd)
    if ran is None:
        return None
    _returncode, stdout, _stderr = ran
    try:
        items = json.loads(stdout)
    except ValueError:
        return None
    diagnostics = []
    for item in items:
        location = item.get("location") or {}
        code = item.get("code")
        severity = "error" if code and str(code).startswith(_RUFF_ERROR_CODE_PREFIXES) else "warning"
        diagnostics.append(
            {
                "path": _normalize_path(cwd, item.get("filename"), settings),
                "line": location.get("row"),
                "col": location.get("column"),
                "severity": severity,
                "code": code,
                "message": item.get("message"),
            }
        )
    return diagnostics


_COMPILEALL_ERROR_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)')


def _compileall_diagnostics(cwd: Path, paths: list[str] | None, settings: Settings) -> list[dict[str, Any]]:
    binary = shutil.which("python3") or shutil.which("python")
    if not binary:
        return []
    cmd = [binary, "-m", "compileall", "-q", *(paths if paths else ["."])]
    ran = _run(cmd, cwd)
    if ran is None:
        return []
    _returncode, stdout, stderr = ran
    text = f"{stdout}\n{stderr}"
    diagnostics = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = _COMPILEALL_ERROR_RE.search(line)
        if not match:
            continue
        message = lines[idx + 1].strip() if idx + 1 < len(lines) else "syntax error"
        diagnostics.append(
            {
                "path": _normalize_path(cwd, match.group("path"), settings),
                "line": int(match.group("line")),
                "col": None,
                "severity": "error",
                "code": None,
                "message": message,
            }
        )
    return diagnostics


def _python_diagnostics(
    cwd: Path, settings: Settings, paths: list[str] | None
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    missing: list[dict[str, str]] = []
    tool_used: list[str] = []

    pyright_result = _pyright_diagnostics(cwd, paths, settings)
    if pyright_result is not None:
        return pyright_result, ["pyright --outputjson"], []
    missing.append(_missing("pyright", "pip install pyright  (or: npm install -g pyright)"))

    ruff_result = _ruff_diagnostics(cwd, paths, settings)
    if ruff_result is not None:
        return ruff_result, ["ruff check --output-format json"], missing
    missing.append(_missing("ruff", "pip install ruff"))

    diagnostics = _compileall_diagnostics(cwd, paths, settings)
    if shutil.which("python3") or shutil.which("python"):
        tool_used.append("python3 -m compileall -q")
    return diagnostics, tool_used, missing


# --- TypeScript ------------------------------------------------------------------


def _find_tsc(cwd: Path) -> str | None:
    local = cwd / "node_modules" / ".bin" / "tsc"
    if local.exists():
        return str(local)
    return shutil.which("tsc")


def _ts_diagnostics(cwd: Path, settings: Settings, paths: list[str] | None) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    tsc_bin = _find_tsc(cwd)
    if not tsc_bin:
        # Deliberately does not fall back to `npx tsc` here: that would
        # trigger an implicit package download/network call for a missing
        # dependency, which can hang or fail in a sandboxed environment.
        return (
            [],
            [],
            [_missing("tsc", "npm install -D typescript  (or: npm install -g typescript)")],
        )
    cmd = [tsc_bin, "--noEmit", "--pretty", "false", *(paths or [])]
    ran = _run(cmd, cwd)
    if ran is None:
        return [], [], []
    _returncode, stdout, stderr = ran
    parsed = parsers.parse_tsc_output(stdout, stderr)
    diagnostics = []
    for item in parsed.get("diagnostics", []):
        diagnostics.append(
            {
                "path": _normalize_path(cwd, item.get("path"), settings),
                "line": item.get("line"),
                "col": item.get("column"),
                "severity": "error",
                "code": item.get("code"),
                "message": item.get("message"),
            }
        )
    return diagnostics, [" ".join(cmd)], []


_LANGUAGE_RUNNERS = {
    "go": _go_diagnostics,
    "python": _python_diagnostics,
    "ts": _ts_diagnostics,
}


def code_diagnostics(
    settings: Settings,
    *,
    repo: str | None = None,
    paths: list[str] | None = None,
    language: str = "auto",
    severity_min: str = "warning",
    limit: int = 200,
) -> dict[str, Any]:
    """Run one-shot diagnostics for a repo/workspace directory.

    ``repo`` is resolved via ``git_tools._resolve_repo_toplevel`` (may raise
    ``GitToolError`` if given but not inside a git repo); when omitted, the
    workspace ``project_root`` is used directly. ``paths`` (if given) are
    passed through to the underlying checker relative to that working
    directory. ``language="auto"`` detects the stack(s) present via
    ``workspace.detect_stack`` and runs every applicable runner (go/python/ts);
    an explicit language runs only that runner. Missing external tools never
    raise -- they are reported in ``missing_tools`` with an ``install_hint``.
    """
    cwd = git_tools._resolve_repo_toplevel(repo, settings) if repo else settings.project_root.resolve()
    validated_paths: list[str] | None = None
    if paths:
        validated_paths = []
        for raw in paths:
            if raw.startswith("-"):
                raise SecurityError(f"diagnostic path must not be an option: {raw}")
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            context = resolve_path_context(
                str(candidate),
                settings,
                allow_hidden=settings.allow_hidden_default,
            )
            validated_paths.append(str(context.target))

    if language == "auto":
        stacks = workspace.detect_stack(cwd)
        languages = []
        if "go" in stacks:
            languages.append("go")
        if "python" in stacks:
            languages.append("python")
        if "ts" in stacks:
            languages.append("ts")
    else:
        languages = [language]

    all_diagnostics: list[dict[str, Any]] = []
    tool_used: list[str] = []
    missing_tools: list[dict[str, str]] = []
    seen_missing: set[str] = set()

    for lang in languages:
        runner = _LANGUAGE_RUNNERS.get(lang)
        if runner is None:
            missing_tools.append(_missing(lang, f"unsupported language: {lang}"))
            continue
        diagnostics, used, missing = runner(cwd, settings, validated_paths)
        all_diagnostics.extend(diagnostics)
        tool_used.extend(used)
        for item in missing:
            if item["tool"] not in seen_missing:
                seen_missing.add(item["tool"])
                missing_tools.append(item)

    filtered = [d for d in all_diagnostics if _severity_rank(d.get("severity")) >= _severity_rank(severity_min)]
    truncated = len(filtered) > limit

    return {
        "ok": True,
        "language": ",".join(languages) if languages else language,
        "tool_used": tool_used,
        "diagnostics": filtered[:limit],
        "missing_tools": missing_tools,
        "truncated": truncated,
    }
