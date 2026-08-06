"""Symbol index (definitions / document outline / workspace search).

Backed by universal-ctags when available (cached per project/repo under
``~/.cache/chatrepo-mcp/``), falling back to the existing regex-based
``fs_tools.symbol_search``/``search_text`` heuristics when ctags is missing.
Every result carries an ``engine`` field (``"ctags"`` or ``"heuristic"``) so
callers know which one produced it. Never raises just because ctags isn't
installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import fs_tools, git_tools
from .bounded_subprocess import run_bounded
from .config import Settings
from .security import SecurityError, display_path, resolve_path_context, resolve_repo_path

#: Directory names never fed to ctags (ctags itself also skips binaries/VCS
#: internals, but these are excluded up front to keep indexing fast).
_EXCLUDE_DIRS = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "dist",
    "build",
)

_CTAGS_TIMEOUT = 60
_CACHE_TTL_SECONDS = 300


class CtagsIncompleteError(RuntimeError):
    """Raised when ctags succeeded but its bounded output was incomplete."""


_HEURISTIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s*def\s+(\w+)"), "function"),
    (re.compile(r"^\s*class\s+(\w+)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?async\s+function\s+(\w+)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?function\s+(\w+)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*="), "const"),
    (re.compile(r"^\s*(?:export\s+)?let\s+(\w+)\s*="), "let"),
    (re.compile(r"^\s*(?:export\s+)?var\s+(\w+)\s*="), "var"),
    (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface"),
    (re.compile(r"^\s*type\s+(\w+)\s+struct\b"), "struct"),
    (re.compile(r"^\s*(?:export\s+)?type\s+(\w+)"), "type"),
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"), "function"),
)


@lru_cache(maxsize=1)
def _ctags_binary() -> str | None:
    """Path to a *universal-ctags* binary, or ``None`` if absent/not universal-ctags."""
    binary = shutil.which("ctags")
    if not binary:
        return None
    try:
        proc = run_bounded(
            [binary, "--version"], timeout=5, max_stdout_bytes=4_096, max_stderr_bytes=4_096,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if "Universal Ctags" not in (proc.stdout or ""):
        return None
    return binary


def _ctags_available() -> bool:
    return _ctags_binary() is not None


# --- caching -------------------------------------------------------------------


def _cache_dir(settings: Settings) -> Path:
    digest = sha256(str(settings.project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".cache" / "chatrepo-mcp" / digest


def _cache_path(settings: Settings, repo_rel: str) -> Path:
    safe_repo = repo_rel.replace("/", "__") or "_root"
    return _cache_dir(settings) / safe_repo / "symbols.json"


def _load_cache(
    path: Path, ttl_seconds: int = _CACHE_TTL_SECONDS, max_bytes: int = 1_000_000,
) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    generated_at = data.get("generated_at", 0)
    if time.time() - generated_at > ttl_seconds:
        return None
    symbols = data.get("symbols")
    return symbols if isinstance(symbols, list) else None


def _save_cache(path: Path, symbols: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"generated_at": time.time(), "symbols": symbols}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


# --- ctags invocation ------------------------------------------------------------


def _parse_ctags_json_lines(text: str) -> list[dict[str, Any]]:
    tags: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("_type") != "tag":
            continue
        tags.append(obj)
    return tags


def _run_ctags_recursive(target: Path, max_bytes: int = 1_000_000) -> list[dict[str, Any]]:
    """Run universal-ctags recursively over ``target``; empty list if unavailable/failed."""
    binary = _ctags_binary()
    if not binary:
        return []
    exclude_args = [f"--exclude={name}" for name in _EXCLUDE_DIRS]
    incomplete = False
    for fields_arg in ("--fields=+n", None):
        cmd = [binary, "--output-format=json", "-R", *exclude_args]
        if fields_arg:
            cmd.append(fields_arg)
        cmd.append(str(target))
        try:
            proc = run_bounded(
                cmd, cwd=str(target), timeout=_CTAGS_TIMEOUT,
                max_stdout_bytes=max_bytes, max_stderr_bytes=4_096,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            if bool(getattr(proc, "stdout_truncated", False)):
                incomplete = True
                continue
            return _parse_ctags_json_lines(proc.stdout)
    if incomplete:
        raise CtagsIncompleteError("ctags recursive output exceeded its bounded capture")
    return []


def _run_ctags_file(target: Path, max_bytes: int = 1_000_000) -> list[dict[str, Any]]:
    """Run universal-ctags over a single file; empty list if unavailable/failed."""
    binary = _ctags_binary()
    if not binary:
        return []
    incomplete = False
    for fields_arg in ("--fields=+n", None):
        cmd = [binary, "--output-format=json"]
        if fields_arg:
            cmd.append(fields_arg)
        cmd.append(str(target))
        try:
            proc = run_bounded(
                cmd, cwd=str(target.parent), timeout=_CTAGS_TIMEOUT,
                max_stdout_bytes=max_bytes, max_stderr_bytes=4_096,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            if bool(getattr(proc, "stdout_truncated", False)):
                incomplete = True
                continue
            return _parse_ctags_json_lines(proc.stdout)
    if incomplete:
        raise CtagsIncompleteError("ctags file output exceeded its bounded capture")
    return []


def _normalize_tags(raw_tags: list[dict[str, Any]], base_dir: Path, settings: Settings) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tag in raw_tags:
        path_field = tag.get("path")
        if not path_field:
            continue
        candidate = Path(path_field)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            context = resolve_path_context(
                str(candidate.resolve()),
                settings,
                allow_hidden=settings.allow_hidden_default,
            )
        except (SecurityError, OSError):
            continue
        out.append(
            {
                "name": tag.get("name"),
                "path": context.display,
                "line": tag.get("line"),
                "kind": tag.get("kind"),
                "signature": tag.get("signature"),
                "scope": tag.get("scope"),
            }
        )
    return out


def _resolve_index_scope(settings: Settings, repo: str | None) -> tuple[Path, str]:
    """Return ``(directory_to_index, repo_rel_key)`` for ``repo`` (or the workspace root)."""
    if not repo:
        toplevel = settings.project_root.resolve()
        return toplevel, git_tools._repo_rel(toplevel, settings)
    try:
        toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    except git_tools.GitToolError:
        toplevel = resolve_repo_path(repo, settings, allow_hidden=True)
    return toplevel, git_tools._repo_rel(toplevel, settings)


def _get_or_build_index(settings: Settings, toplevel: Path, repo_rel: str) -> list[dict[str, Any]]:
    cache_path = _cache_path(settings, repo_rel)
    cached = _load_cache(cache_path, max_bytes=settings.max_diff_bytes)
    if cached is not None:
        return cached
    raw_tags = _run_ctags_recursive(toplevel, settings.max_diff_bytes)
    symbols = _normalize_tags(raw_tags, toplevel, settings)
    _save_cache(cache_path, symbols)
    return symbols


# --- heuristic fallback ------------------------------------------------------------


def _infer_kind_from_text(text: str) -> str | None:
    for pattern, kind in _HEURISTIC_PATTERNS:
        if pattern.match(text):
            return kind
    return None


def _heuristic_document_symbols(text: str) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in _HEURISTIC_PATTERNS:
            match = pattern.match(line)
            if match:
                symbols.append(
                    {
                        "name": match.group(1),
                        "kind": kind,
                        "line": line_no,
                        "signature": line.strip()[:200],
                        "scope": None,
                    }
                )
                break
    return symbols


# --- public API --------------------------------------------------------------------


def symbol_definition(
    settings: Settings,
    symbol: str,
    *,
    repo: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find definitions of ``symbol`` (ctags index when available, else regex heuristic)."""
    limit = min(limit, settings.max_search_results)

    if _ctags_available():
        try:
            toplevel, repo_rel = _resolve_index_scope(settings, repo)
            symbols = _get_or_build_index(settings, toplevel, repo_rel)
            matches = [item for item in symbols if item.get("name") == symbol]
            if kind:
                matches = [item for item in matches if item.get("kind") == kind]
            matches = matches[:limit]
            return {"ok": True, "symbol": symbol, "definitions": matches, "count": len(matches), "engine": "ctags"}
        except Exception:  # noqa: BLE001, S110 - never let index issues break the tool, fall back
            pass

    result = fs_tools.symbol_search(symbol, settings, path=repo or ".", limit=limit)
    definitions: list[dict[str, Any]] = []
    for item in result["results"]:
        inferred_kind = _infer_kind_from_text(item["text"])
        if kind and inferred_kind != kind:
            continue
        definitions.append(
            {
                "path": item["path"],
                "line": item["line"],
                "kind": inferred_kind,
                "signature": item["text"].strip()[:200],
                "scope": None,
            }
        )
        if len(definitions) >= limit:
            break
    return {"ok": True, "symbol": symbol, "definitions": definitions, "count": len(definitions), "engine": "heuristic"}


def document_symbols(settings: Settings, path: str, *, repo: str | None = None) -> dict[str, Any]:
    """Outline (name/kind/line/signature/scope) of a single file."""
    candidate = Path(path)
    if repo and not candidate.is_absolute():
        repo_root, _repo_rel = _resolve_index_scope(settings, repo)
        candidate = repo_root / candidate
    target = resolve_repo_path(str(candidate), settings, allow_hidden=settings.allow_hidden_default)
    shown_path = display_path(target, settings)
    if not target.is_file():
        return {"ok": False, "path": path, "symbols": [], "engine": "none", "error": "not a file"}

    if _ctags_available():
        try:
            raw_tags = _run_ctags_file(target, settings.max_response_chars)
            if raw_tags:
                symbols = [
                    {
                        "name": tag.get("name"),
                        "kind": tag.get("kind"),
                        "line": tag.get("line"),
                        "signature": tag.get("signature"),
                        "scope": tag.get("scope"),
                    }
                    for tag in raw_tags
                ]
                return {"ok": True, "path": shown_path, "symbols": symbols, "engine": "ctags"}
        except Exception:  # noqa: BLE001, S110 - optional ctags failure falls back to heuristics
            pass

    with target.open("rb") as handle:
        raw = handle.read(settings.max_file_bytes + 1)
    if len(raw) > settings.max_file_bytes:
        return {
            "ok": False, "path": shown_path, "symbols": [], "engine": "none",
            "error": f"file exceeds MAX_FILE_BYTES ({settings.max_file_bytes})",
        }
    text = raw.decode("utf-8", errors="replace")
    symbols = _heuristic_document_symbols(text)
    return {"ok": True, "path": shown_path, "symbols": symbols, "engine": "heuristic"}


def workspace_symbols(settings: Settings, query: str, *, repo: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Fuzzy/substring search across the symbol index (ctags) or a text-search fallback."""
    limit = min(limit, settings.max_search_results)

    if _ctags_available():
        try:
            toplevel, repo_rel = _resolve_index_scope(settings, repo)
            symbols = _get_or_build_index(settings, toplevel, repo_rel)
            needle = query.lower()
            matches = [item for item in symbols if item.get("name") and needle in str(item["name"]).lower()]
            matches = matches[:limit]
            return {"ok": True, "query": query, "symbols": matches, "count": len(matches), "engine": "ctags"}
        except Exception:  # noqa: BLE001, S110 - optional ctags failure falls back to heuristics
            pass

    result = fs_tools.search_text(query, settings, path=repo or ".", regex=False, case_sensitive=False, limit=limit)
    symbols = [
        {
            "path": item["path"],
            "line": item["line"],
            "name": query,
            "kind": _infer_kind_from_text(item["text"]),
            "signature": item["text"].strip()[:200],
            "scope": None,
        }
        for item in result["results"]
    ]
    return {"ok": True, "query": query, "symbols": symbols, "count": len(symbols), "engine": "heuristic"}
