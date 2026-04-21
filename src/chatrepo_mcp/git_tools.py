from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import Settings


class GitToolError(RuntimeError):
    """Raised when a git command fails."""


def _run_git(args: list[str], settings: Settings, *, max_bytes: int | None = None) -> str:
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(settings.project_root),
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.subprocess_timeout,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitToolError(stderr or f"git command failed: {' '.join(cmd)}")
    output = proc.stdout
    if max_bytes is not None:
        encoded = output.encode("utf-8", errors="replace")
        if len(encoded) > max_bytes:
            output = encoded[:max_bytes].decode("utf-8", errors="replace") + "\n...[truncated]"
    return output


def repo_git_info(settings: Settings) -> dict[str, Any]:
    current_branch = _run_git(["branch", "--show-current"], settings).strip()
    remotes = _run_git(["remote", "-v"], settings).strip().splitlines()
    top_level = _run_git(["rev-parse", "--show-toplevel"], settings).strip()
    return {
        "branch": current_branch,
        "top_level": top_level,
        "remotes": remotes,
        "git_dir": _run_git(["rev-parse", "--git-dir"], settings).strip(),
    }


def git_status(settings: Settings, short: bool = True) -> dict[str, Any]:
    args = ["status", "--short", "--branch"] if short else ["status"]
    return {"status": _run_git(args, settings, max_bytes=settings.max_response_chars).strip()}


def git_diff(
    settings: Settings,
    staged: bool = False,
    pathspec: str | None = None,
    context_lines: int = 3,
) -> dict[str, Any]:
    args = ["diff", f"-U{context_lines}"]
    if staged:
        args.insert(1, "--staged")
    if pathspec:
        args.extend(["--", pathspec])
    return {"diff": _run_git(args, settings, max_bytes=settings.max_diff_bytes)}


def git_log(
    settings: Settings,
    limit: int = 20,
    pathspec: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    limit = min(limit, settings.max_log_commits)
    pretty = "%H%x09%h%x09%an%x09%ad%x09%s"
    args = ["log", f"--max-count={limit}", "--date=iso", f"--pretty=format:{pretty}"]
    if since:
        args.append(f"--since={since}")
    if pathspec:
        args.extend(["--", pathspec])

    lines = _run_git(args, settings, max_bytes=settings.max_response_chars).splitlines()
    commits = []
    for line in lines:
        parts = line.split("\t", 4)
        if len(parts) == 5:
            full_sha, short_sha, author, date, subject = parts
            commits.append(
                {
                    "sha": full_sha,
                    "short_sha": short_sha,
                    "author": author,
                    "date": date,
                    "subject": subject,
                }
            )
    return {"commits": commits, "count": len(commits)}


def git_show(
    settings: Settings,
    revision: str,
    path: str | None = None,
) -> dict[str, Any]:
    spec = revision if not path else f"{revision}:{path}"
    return {"revision": spec, "content": _run_git(["show", spec], settings, max_bytes=settings.max_response_chars)}


def git_branches(settings: Settings, all_branches: bool = True) -> dict[str, Any]:
    args = ["branch", "-vv"]
    if all_branches:
        args.insert(1, "-a")
    return {"branches": _run_git(args, settings, max_bytes=settings.max_response_chars).splitlines()}


def git_blame(
    settings: Settings,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    range_spec = f"-L{start_line},{end_line}" if end_line is not None else f"-L{start_line},+200"
    output = _run_git(["blame", "-w", range_spec, "--", path], settings, max_bytes=settings.max_response_chars)
    return {"path": path, "blame": output}


def git_grep(
    settings: Settings,
    query: str,
    revision: str | None = None,
    pathspec: str | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    args = ["grep", "-nI"]
    if not case_sensitive:
        args.append("-i")
    args.append(query)
    if revision:
        args.append(revision)
    if pathspec:
        args.extend(["--", pathspec])
    output = _run_git(args, settings, max_bytes=settings.max_response_chars)
    results = []
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            path, line_no, text = parts
            results.append({"path": path, "line": int(line_no), "text": text})
    return {"query": query, "results": results, "count": len(results)}
