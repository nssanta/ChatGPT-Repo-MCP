from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import workspace
from .config import Settings


class GitToolError(RuntimeError):
    """Raised when a git command fails."""


def _resolve_repo_toplevel(repo: str | None, settings: Settings) -> Path:
    """Resolve the git toplevel directory for an optional repo-relative path.

    When ``repo`` is given, it is resolved against ``project_root`` (or used
    as-is when absolute) and must land inside a git repository within the
    allowed workspace roots, otherwise a ``GitToolError`` is raised. When
    omitted, the nearest git toplevel from ``project_root`` is used; if
    ``project_root`` itself is not inside a git repository (a polyrepo
    workspace root), it is returned as-is so callers can surface a clear
    "not a git repository" error or handle the polyrepo case explicitly.
    """
    roots = workspace.resolve_roots(settings)
    if repo:
        candidate = Path(repo).expanduser()
        target = candidate if candidate.is_absolute() else settings.project_root / candidate
        toplevel = workspace.find_git_toplevel(target, roots)
        if toplevel is None:
            raise GitToolError(f"path is not inside a git repository: {repo}")
        return toplevel
    toplevel = workspace.find_git_toplevel(settings.project_root, roots)
    return toplevel if toplevel is not None else settings.project_root.resolve()


def _repo_rel(toplevel: Path, settings: Settings) -> str:
    """Return ``toplevel``'s path relative to ``project_root``; "" for the root itself."""
    try:
        rel = toplevel.resolve().relative_to(settings.project_root.resolve()).as_posix()
    except ValueError:
        return str(toplevel)
    return "" if rel == "." else rel


def _run_git(
    args: list[str],
    settings: Settings,
    *,
    max_bytes: int | None = None,
    cwd: Path | None = None,
    network: bool = False,
) -> str:
    resolved_cwd = cwd if cwd is not None else _resolve_repo_toplevel(None, settings)
    cmd = ["git", *args]
    env = None
    if network:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        cmd,
        cwd=str(resolved_cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.git_network_timeout if network else settings.subprocess_timeout,
        env=env,
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


def repo_git_info(settings: Settings, repo: str | None = None) -> dict[str, Any]:
    if repo is None:
        roots = workspace.resolve_roots(settings)
        toplevel = workspace.find_git_toplevel(settings.project_root, roots)
        if toplevel is None:
            # The workspace root itself is not a git repository: this is a
            # polyrepo layout, surface discovery instead of failing.
            return {"polyrepo": True, "repos": workspace.list_workspace_repos(settings)}
    else:
        toplevel = _resolve_repo_toplevel(repo, settings)

    current_branch = _run_git(["branch", "--show-current"], settings, cwd=toplevel).strip()
    remotes = _run_git(["remote", "-v"], settings, cwd=toplevel).strip().splitlines()
    top_level = _run_git(["rev-parse", "--show-toplevel"], settings, cwd=toplevel).strip()
    return {
        "repo": _repo_rel(toplevel, settings),
        "branch": current_branch,
        "top_level": top_level,
        "remotes": remotes,
        "git_dir": _run_git(["rev-parse", "--git-dir"], settings, cwd=toplevel).strip(),
    }


def git_status(settings: Settings, short: bool = True, repo: str | None = None) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    args = ["status", "--short", "--branch"] if short else ["status"]
    return {
        "repo": _repo_rel(toplevel, settings),
        "status": _run_git(args, settings, cwd=toplevel, max_bytes=settings.max_response_chars).strip(),
    }


def git_diff(
    settings: Settings,
    staged: bool = False,
    pathspec: str | None = None,
    context_lines: int = 3,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    args = ["diff", f"-U{context_lines}"]
    if staged:
        args.insert(1, "--staged")
    if pathspec:
        args.extend(["--", pathspec])
    return {
        "repo": _repo_rel(toplevel, settings),
        "diff": _run_git(args, settings, cwd=toplevel, max_bytes=settings.max_diff_bytes),
    }


def git_log(
    settings: Settings,
    limit: int = 20,
    pathspec: str | None = None,
    since: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    limit = min(limit, settings.max_log_commits)
    pretty = "%H%x09%h%x09%an%x09%ad%x09%s"
    args = ["log", f"--max-count={limit}", "--date=iso", f"--pretty=format:{pretty}"]
    if since:
        args.append(f"--since={since}")
    if pathspec:
        args.extend(["--", pathspec])

    lines = _run_git(args, settings, cwd=toplevel, max_bytes=settings.max_response_chars).splitlines()
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
    return {"repo": _repo_rel(toplevel, settings), "commits": commits, "count": len(commits)}


def git_show(
    settings: Settings,
    revision: str,
    path: str | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    spec = revision if not path else f"{revision}:{path}"
    return {
        "repo": _repo_rel(toplevel, settings),
        "revision": spec,
        "content": _run_git(["show", spec], settings, cwd=toplevel, max_bytes=settings.max_response_chars),
    }


def git_branches(settings: Settings, all_branches: bool = True, repo: str | None = None) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    args = ["branch", "-vv"]
    if all_branches:
        args.insert(1, "-a")
    return {
        "repo": _repo_rel(toplevel, settings),
        "branches": _run_git(args, settings, cwd=toplevel, max_bytes=settings.max_response_chars).splitlines(),
    }


def git_blame(
    settings: Settings,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    repo: str | None = None,
) -> dict[str, Any]:
    toplevel = _resolve_repo_toplevel(repo, settings)
    range_spec = f"-L{start_line},{end_line}" if end_line is not None else f"-L{start_line},+200"
    output = _run_git(
        ["blame", "-w", range_spec, "--", path],
        settings,
        cwd=toplevel,
        max_bytes=settings.max_response_chars,
    )
    return {"repo": _repo_rel(toplevel, settings), "path": path, "blame": output}


def _git_grep_args(
    query: str,
    revision: str | None,
    pathspec: str | None,
    paths: list[str] | None,
    case_sensitive: bool,
) -> list[str]:
    args = ["grep", "-nI"]
    if not case_sensitive:
        args.append("-i")
    args.append(query)
    if revision:
        args.append(revision)
    if paths:
        args.extend(["--", *paths])
    elif pathspec:
        args.extend(["--", pathspec])
    return args


def _run_git_grep(args: list[str], settings: Settings, cwd: Path) -> str:
    """Run ``git grep``. Exit code 1 ("no matches") is not treated as an error."""
    cmd = ["git", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.subprocess_timeout,
    )
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitToolError(stderr or f"git command failed: {' '.join(cmd)}")
    output = proc.stdout
    max_bytes = settings.max_response_chars
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        output = encoded[:max_bytes].decode("utf-8", errors="replace") + "\n...[truncated]"
    return output


def _parse_grep_output(output: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            path, line_no, text = parts
            try:
                line_no_int = int(line_no)
            except ValueError:
                continue
            results.append({"path": path, "line": line_no_int, "text": text})
    return results


def _git_grep_fanout(
    settings: Settings,
    args: list[str],
    query: str,
    repos: list[dict[str, Any]],
) -> dict[str, Any]:
    root = settings.project_root.resolve()
    limit = settings.max_search_results
    aggregated: list[dict[str, Any]] = []
    searched: list[str] = []
    for entry in repos:
        if len(aggregated) >= limit:
            break
        rel = entry["path"]
        directory = root / rel if rel else root
        searched.append(rel)
        try:
            output = _run_git_grep(args, settings, directory)
        except (GitToolError, subprocess.SubprocessError, OSError):
            continue
        for hit in _parse_grep_output(output):
            hit["repo"] = rel
            aggregated.append(hit)
            if len(aggregated) >= limit:
                break
    return {
        "polyrepo": True,
        "repos_searched": searched,
        "query": query,
        "results": aggregated,
        "count": len(aggregated),
    }


def git_grep(
    settings: Settings,
    query: str,
    revision: str | None = None,
    pathspec: str | None = None,
    paths: list[str] | None = None,
    case_sensitive: bool = False,
    repo: str | None = None,
) -> dict[str, Any]:
    args = _git_grep_args(query, revision, pathspec, paths, case_sensitive)

    if repo is None:
        roots = workspace.resolve_roots(settings)
        root_is_git = workspace.find_git_toplevel(settings.project_root, roots) is not None
        if not root_is_git:
            git_repos = [entry for entry in workspace.list_workspace_repos(settings) if entry.get("is_git")]
            if git_repos:
                return _git_grep_fanout(settings, args, query, git_repos)

    toplevel = _resolve_repo_toplevel(repo, settings)
    output = _run_git_grep(args, settings, toplevel)
    results = _parse_grep_output(output)
    return {
        "repo": _repo_rel(toplevel, settings),
        "query": query,
        "results": results,
        "count": len(results),
    }


def list_repos(settings: Settings) -> dict[str, Any]:
    """Discover workspace repos (polyrepo-aware) and annotate each with its current branch."""
    repos = workspace.list_workspace_repos(settings)
    root = settings.project_root.resolve()
    short_timeout = min(settings.subprocess_timeout, 10)
    for entry in repos:
        if not entry.get("is_git"):
            entry.setdefault("branch", None)
            entry.setdefault("dirty", None)
            continue
        directory = root / entry["path"] if entry["path"] else root
        try:
            proc = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=str(directory),
                check=False,
                capture_output=True,
                text=True,
                timeout=short_timeout,
            )
            entry["branch"] = proc.stdout.strip() if proc.returncode == 0 else None
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(directory),
                check=False,
                capture_output=True,
                text=True,
                timeout=short_timeout,
            )
            entry["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
        except (subprocess.SubprocessError, OSError):
            entry["branch"] = None
            entry["dirty"] = None
    return {"repos": repos}
