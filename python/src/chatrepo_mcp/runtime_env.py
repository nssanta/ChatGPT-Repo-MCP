from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Settings


_VERSION_ARGS = {
    "go": ("version",),
    "node": ("--version",),
    "npm": ("--version",),
    "python3": ("--version",),
    "git": ("--version",),
    "rg": ("--version",),
    "gh": ("--version",),
    "ctags": ("--version",),
    "ruff": ("--version",),
    "mypy": ("--version",),
    "pyright": ("--version",),
}


def _existing_dirs(values: list[tuple[str, str]]) -> tuple[list[str], dict[str, str]]:
    paths: list[str] = []
    sources: dict[str, str] = {}
    seen: set[str] = set()
    for raw, source in values:
        if not raw:
            continue
        path = str(Path(raw).expanduser().resolve())
        if path in seen or not Path(path).is_dir():
            continue
        seen.add(path)
        paths.append(path)
        sources[path] = source
    return paths, sources


def effective_path(settings: Settings) -> tuple[list[str], dict[str, str], list[str]]:
    home = Path.home()
    candidates: list[tuple[str, str]] = [
        *((value, "explicit_extra") for value in settings.mcp_extra_path),
        *((value, "inherited_path") for value in os.environ.get("PATH", "").split(os.pathsep)),
    ]
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        candidates.append((str(Path(virtual_env) / "bin"), "virtualenv"))
    executable_bin = str(Path(sys.executable).resolve().parent)
    candidates.append((executable_bin, "virtualenv"))
    for variable, suffix in (
        ("GOROOT", "bin"),
        ("GOPATH", "bin"),
        ("CARGO_HOME", "bin"),
        ("PYENV_ROOT", "shims"),
    ):
        if value := os.environ.get(variable):
            candidates.append((str(Path(value) / suffix), "standard_fallback"))
    if value := os.environ.get("NVM_BIN"):
        candidates.append((value, "standard_fallback"))
    candidates.extend(
        (value, "standard_fallback")
        for value in (
            "/usr/local/go/bin",
            "/usr/local/bin",
            "/usr/bin",
            str(home / "go/bin"),
            str(home / ".local/bin"),
            str(home / ".cargo/bin"),
        )
    )
    paths, sources = _existing_dirs(candidates)
    warnings = [] if paths else ["effective PATH contains no existing directories"]
    return paths, sources, warnings


def command_environment(settings: Settings, overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    paths, _, _ = effective_path(settings)
    env["PATH"] = os.pathsep.join(paths)
    for key, value in (overrides or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid env key: {key}")
        env[key] = value
    return env


def resolve_binary(name: str, settings: Settings) -> tuple[str | None, str]:
    paths, sources, _ = effective_path(settings)
    for directory in paths:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), sources[directory]
    return None, "not_found"


def tool_status(name: str, settings: Settings) -> dict[str, Any]:
    path, source = resolve_binary(name, settings)
    result: dict[str, Any] = {
        "name": name,
        "available": path is not None,
        "path": path,
        "source": source,
        "version": None,
        "version_error": None,
    }
    if path is None:
        return result
    try:
        proc = subprocess.run(
            [path, *_VERSION_ARGS.get(name, ("--version",))],
            env=command_environment(settings),
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        output = (proc.stdout or proc.stderr).strip().splitlines()
        if proc.returncode == 0 and output:
            result["version"] = output[0][:300]
        else:
            result["version_error"] = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300]
    except (OSError, subprocess.SubprocessError) as exc:
        result["version_error"] = str(exc)
    return result
