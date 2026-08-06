from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import workspace
from .bounded_subprocess import run_bounded
from .config import Settings
from .resource_profile import acquire_heavy_operation


class GitToolError(RuntimeError):
    """Raised when a git command fails."""

    def __init__(self, message: str, *, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


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
    output, _, _, _ = _run_git_capture(
        args, settings, max_bytes=max_bytes, cwd=cwd, network=network,
    )
    return output


def _run_git_capture(
    args: list[str],
    settings: Settings,
    *,
    max_bytes: int | None = None,
    cwd: Path | None = None,
    network: bool = False,
    persist_artifact: bool = False,
) -> tuple[str, str, bool, dict[str, Any] | None]:
    resolved_cwd = cwd if cwd is not None else _resolve_repo_toplevel(None, settings)
    cmd = ["git", *args]
    env = None
    if network:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
    hard_limit = max_bytes if max_bytes is not None else settings.max_response_chars
    limit = min(hard_limit, settings.default_inline_output_bytes) if persist_artifact else hard_limit
    stderr_limit = limit if persist_artifact else settings.max_response_chars
    lease = acquire_heavy_operation(settings) if persist_artifact else None
    try:
        proc = run_bounded(
            cmd,
            cwd=str(resolved_cwd),
            timeout=settings.git_network_timeout if network else settings.subprocess_timeout,
            env=env,
            max_stdout_bytes=limit,
            max_stderr_bytes=stderr_limit,
            max_combined_bytes=limit if persist_artifact else None,
            artifact_settings=settings if persist_artifact else None,
        )
    except subprocess.TimeoutExpired as exc:
        partial = getattr(exc, "result", None)
        artifact = getattr(partial, "artifact", None)
        result: dict[str, Any] = {
            "ok": False,
            "error_kind": "git_timeout",
            "message": f"git command timed out: {' '.join(cmd)}",
            "stdout": getattr(partial, "stdout", ""),
            "stderr": getattr(partial, "stderr", ""),
            "truncated": True,
        }
        if partial is not None and artifact is not None:
            receipt = artifact["receipt"]
            receipt["configured"]["inline_output_bytes"] = settings.default_inline_output_bytes
            receipt["applied"]["inline_output_bytes"] = limit
            receipt["returned"] = {
                "stdout_bytes": len(partial.stdout.encode("utf-8")),
                "stderr_bytes": len(partial.stderr.encode("utf-8")),
            }
            receipt["total"] = {
                "stdout_bytes": partial.stdout_bytes,
                "stderr_bytes": partial.stderr_bytes,
            }
            result["artifact"] = artifact
            result["receipt"] = receipt
            result["continuation"] = artifact["continuation"]
        raise GitToolError(result["message"], result=result) from exc
    finally:
        if lease is not None:
            lease.release()
    artifact = getattr(proc, "artifact", None)
    output = proc.stdout
    stderr = proc.stderr
    truncated = proc.stdout_truncated or proc.stderr_truncated
    if truncated and artifact is None:
        output += "\n...[truncated]"
    if artifact is not None:
        receipt = artifact["receipt"]
        receipt["configured"]["inline_output_bytes"] = settings.default_inline_output_bytes
        receipt["applied"]["inline_output_bytes"] = limit
        receipt["returned"] = {
            "stdout_bytes": len(proc.stdout.encode("utf-8")),
            "stderr_bytes": len(proc.stderr.encode("utf-8")),
        }
        receipt["total"] = {
            "stdout_bytes": proc.stdout_bytes,
            "stderr_bytes": proc.stderr_bytes,
        }
    if proc.returncode != 0:
        message = stderr.strip() or output.strip() or f"git command failed: {' '.join(cmd)}"
        if artifact is not None:
            error_result: dict[str, Any] = {
                "ok": False,
                "error_kind": "git_error",
                "message": message,
                "stdout": output,
                "stderr": stderr,
                "truncated": truncated,
                "artifact": artifact,
                "receipt": artifact["receipt"],
            }
            if truncated:
                error_result["continuation"] = artifact["continuation"]
            raise GitToolError(message, result=error_result)
        raise GitToolError(message)
    return output, stderr, truncated, artifact


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
    status, stderr, truncated, artifact = _run_git_capture(
        args, settings, cwd=toplevel, max_bytes=settings.max_response_chars, persist_artifact=True,
    )
    result = {
        "repo": _repo_rel(toplevel, settings),
        "status": status.strip(),
        "stderr": stderr,
        "truncated": truncated,
    }
    if artifact is not None:
        result["artifact"] = artifact
        result["receipt"] = artifact["receipt"]
    if truncated and artifact is not None:
        result["continuation"] = {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
    return result


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
    diff, stderr, truncated, artifact = _run_git_capture(
        args, settings, cwd=toplevel, max_bytes=settings.max_diff_bytes, persist_artifact=True,
    )
    result = {
        "repo": _repo_rel(toplevel, settings),
        "diff": diff,
        "stderr": stderr,
        "truncated": truncated,
    }
    if artifact is not None:
        result["artifact"] = artifact
        result["receipt"] = artifact["receipt"]
    if truncated and artifact is not None:
        result["continuation"] = {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
    return result


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
    content, stderr, truncated, artifact = _run_git_capture(
        ["show", spec], settings, cwd=toplevel, max_bytes=settings.max_response_chars, persist_artifact=True,
    )
    result = {
        "repo": _repo_rel(toplevel, settings),
        "revision": spec,
        "content": content,
        "stderr": stderr,
        "truncated": truncated,
    }
    if artifact is not None:
        result["artifact"] = artifact
        result["receipt"] = artifact["receipt"]
    if truncated and artifact is not None:
        result["continuation"] = {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
    return result


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
    output, stderr, truncated, artifact = _run_git_capture(
        ["blame", "-w", range_spec, "--", path],
        settings,
        cwd=toplevel,
        max_bytes=settings.max_response_chars,
        persist_artifact=True,
    )
    result = {
        "repo": _repo_rel(toplevel, settings), "path": path, "blame": output,
        "stderr": stderr, "truncated": truncated,
    }
    if artifact is not None:
        result["artifact"] = artifact
        result["receipt"] = artifact["receipt"]
    if truncated and artifact is not None:
        result["continuation"] = {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
    return result


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


def _run_git_grep(
    args: list[str], settings: Settings, cwd: Path, *, persist_artifact: bool = False,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Run ``git grep``. Exit code 1 ("no matches") is not treated as an error."""
    cmd = ["git", *args]
    proc = run_bounded(
        cmd,
        cwd=str(cwd),
        timeout=settings.subprocess_timeout,
        max_stdout_bytes=settings.max_response_chars,
        max_stderr_bytes=settings.max_response_chars,
        artifact_settings=settings if persist_artifact else None,
    )
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitToolError(stderr or f"git command failed: {' '.join(cmd)}")
    output = proc.stdout
    if proc.stdout_truncated:
        output += "\n...[truncated]"
    return output, proc.stdout_truncated, getattr(proc, "artifact", None)


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
    output_truncated = False
    artifacts: list[dict[str, Any]] = []
    for entry in repos:
        if len(aggregated) >= limit:
            break
        rel = entry["path"]
        directory = root / rel if rel else root
        searched.append(rel)
        try:
            output, truncated, artifact = _run_git_grep(args, settings, directory, persist_artifact=True)
            output_truncated = output_truncated or truncated
            if artifact is not None:
                artifacts.append(artifact)
        except (GitToolError, subprocess.SubprocessError, OSError):
            continue
        for hit in _parse_grep_output(output):
            hit["repo"] = rel
            aggregated.append(hit)
            if len(aggregated) >= limit:
                break
    result = {
        "polyrepo": True,
        "repos_searched": searched,
        "query": query,
        "results": aggregated,
        "count": len(aggregated),
        "truncated": output_truncated or len(aggregated) >= limit,
    }
    if artifacts:
        result["artifacts"] = artifacts
    if output_truncated:
        result["continuations"] = [
            {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
            for artifact in artifacts
        ]
    return result


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
    output, output_truncated, artifact = _run_git_grep(args, settings, toplevel, persist_artifact=True)
    results = _parse_grep_output(output)
    result = {
        "repo": _repo_rel(toplevel, settings),
        "query": query,
        "results": results,
        "count": len(results),
        "truncated": output_truncated,
    }
    if artifact is not None:
        result["artifact"] = artifact
    if output_truncated and artifact is not None:
        result["continuation"] = {"tool": "read_artifact", "arguments": {"artifact_id": artifact["artifact_id"]}}
    return result


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
            proc = run_bounded(
                ["git", "branch", "--show-current"],
                cwd=str(directory),
                timeout=short_timeout,
                max_stdout_bytes=4_096,
                max_stderr_bytes=4_096,
            )
            entry["branch"] = proc.stdout.strip() if proc.returncode == 0 else None
            status = run_bounded(
                ["git", "status", "--porcelain"],
                cwd=str(directory),
                timeout=short_timeout,
                max_stdout_bytes=1,
                max_stderr_bytes=4_096,
            )
            entry["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
        except (subprocess.SubprocessError, OSError):
            entry["branch"] = None
            entry["dirty"] = None
    return {"repos": repos}
