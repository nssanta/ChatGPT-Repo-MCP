from __future__ import annotations

import atexit
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Any, Literal

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, Field

from .command_tools import (
    TEST_PRESETS,
    CommandPolicyError,
    ConfirmationRequiredError,
    GitCommitError,
    cancel_command_job,
    command_policy_check,
    get_command_job,
    get_command_log,
    get_job_status,
    git_commit,
    list_command_jobs,
    run_command,
    run_commands,
    run_test_preset,
    shutdown_command_jobs,
    start_command_job,
    summarize_command_log,
)
from .config import Settings
from .edit_tools import (
    append_to_file,
    apply_change_set,
    apply_patch_diff,
    batch_edit_files,
    create_text_file,
    current_text_sha256,
    delete_path,
    delete_text_in_file,
    ensure_directory,
    insert_after_heading,
    insert_after_line,
    insert_before_heading,
    insert_before_line,
    insert_text_in_file,
    move_path,
    replace_lines,
    replace_text_in_file,
    structured_error,
    update_current_mission,
    write_text_file,
)
from .fs_tools import (
    dependency_map,
    file_metadata,
    find_files,
    list_dir,
    read_multiple_files,
    read_text_file,
    recent_changes,
    repo_info,
    search_text,
    symbol_search,
    todo_scan,
    tree,
)
from .git_tools import (
    GitToolError,
    git_blame,
    git_branches,
    git_diff,
    git_grep,
    git_log,
    git_show,
    git_status,
    list_repos,
    repo_git_info,
)
from .git_workflow_tools import (
    git_add,
    git_create_branch,
    git_fetch,
    git_merge,
    git_pull,
    git_push,
    git_reset,
    git_restore,
    git_revert,
    git_stash,
    git_switch_branch,
    git_worktree_add,
    git_worktree_list,
    git_worktree_remove,
    prepare_task_worktree,
)
from .github_tools import (
    gh_checks,
    gh_issue_list,
    gh_issue_view,
    gh_pr_comment,
    gh_pr_create,
    gh_pr_list,
    gh_pr_merge,
    gh_pr_view,
    gh_run_rerun,
    gh_run_view,
    gh_status,
)
from .index_tools import document_symbols, symbol_definition, workspace_symbols
from .lsp_tools import code_diagnostics
from .output_store import read_artifact
from .profile import list_test_presets, load_repo_profile
from .resource_profile import (
    ResourceBusyError,
    cancel_heavy_operation,
    list_heavy_operations,
)
from .result_models import result_model_for_tool
from .runtime_env import effective_path, tool_status
from .terminal_tools import (
    close_terminal_session,
    list_terminal_sessions,
    read_terminal_session,
    resize_terminal_session,
    shutdown_terminal_sessions,
    start_terminal_session,
    write_terminal_session,
)
from .workflows import (
    git_worktree_guard,
    quality_gate_and_commit,
    run_quality_gate,
    scan_new_policy_violations,
)
from .workspace import list_workspace_repos

settings = Settings.from_env()
atexit.register(shutdown_terminal_sessions, settings)
atexit.register(shutdown_command_jobs, settings)


class StaticBearerVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if settings.mcp_auth_mode != "bearer":
            return AccessToken(token=token, client_id="no-auth", scopes=["repo"], expires_at=None)
        if settings.mcp_bearer_token and token == settings.mcp_bearer_token:
            return AccessToken(token=token, client_id="chatgpt", scopes=["repo"], expires_at=None)
        return None


auth_settings = (
    AuthSettings(
        issuer_url=AnyHttpUrl("https://localhost"),
        resource_server_url=AnyHttpUrl("https://localhost"),
        required_scopes=["repo"],
    )
    if settings.mcp_auth_mode == "bearer"
    else None
)

mcp = FastMCP(
    settings.app_name,
    host=settings.host,
    port=settings.port,
    streamable_http_path="/mcp",
    json_response=True,
    token_verifier=StaticBearerVerifier() if settings.mcp_auth_mode == "bearer" else None,
    auth=auth_settings,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=[],
    ),
)

_TOOL_REGISTRY: list[str] = []


def _tool(*args: Any, **kwargs: Any):
    """Wrap ``mcp.tool`` while recording the registered name in ``_TOOL_REGISTRY``.

    This keeps a source-of-truth tool-name list in sync automatically (built at
    decoration time) instead of a hand-maintained literal list that can drift.
    """

    def decorator(fn):
        name = kwargs.get("name") or fn.__name__
        # FastMCP serializes the existing dict to legacy text before it
        # validates the same result into structuredContent.
        fn.__annotations__["return"] = result_model_for_tool(name)
        if "structured_output" in kwargs:
            raise RuntimeError("tool wrappers own structured_output registration")
        kwargs["structured_output"] = True
        _TOOL_REGISTRY.append(name)
        return mcp.tool(*args, **kwargs)(fn)

    return decorator


def _tool_names() -> list[str]:
    """Return the names of all tools registered on this server.

    Prefers the FastMCP tool manager's own registry (authoritative at call
    time); falls back to the decorator-time ``_TOOL_REGISTRY`` if that
    private API is unavailable in a future ``mcp`` package version.
    """
    try:
        names = [tool.name for tool in mcp._tool_manager.list_tools()]
        if names:
            return sorted(names)
    except Exception:  # noqa: BLE001, S110 - private FastMCP API has registry fallback
        pass
    return sorted(_TOOL_REGISTRY)


READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}

WRITE_ACTION = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": False,
}

SAFE_EDIT_ACTION = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": False,
}

COMMAND_ACTION = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "openWorldHint": True,
}

NETWORK_READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
}

NETWORK_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": True,
}


RepoPath = Annotated[
    str,
    Field(description="Project-relative path, or an absolute path allowed by WORKSPACE_ROOTS/full access."),
]
OptionalRepoPath = Annotated[
    str | None,
    Field(description="Optional repository-relative working directory. Defaults to the repository root."),
]
RepoScope = Annotated[
    str | None,
    Field(
        description=(
            "Optional path to a sub-repository in a polyrepo workspace (as returned by list_repos). "
            "Defaults to the workspace root's own git repository, if any."
        )
    ),
]
ExpectedSha = Annotated[
    str | None,
    Field(description="Expected current file SHA-256. Use the sha256 returned by read tools to avoid stale writes."),
]
DryRun = Annotated[
    bool | None,
    Field(description="Preview when true; apply when false; omit to use the ACCESS_MODE default."),
]
Confirmed = Annotated[
    bool,
    Field(description="Set true only after the owner explicitly confirms this action; otherwise a confirmation_required error is returned."),
]
SmallText = Annotated[str, Field(description="UTF-8 text payload. Prefer compact chunks for ChatGPT tool reliability.")]
LineNumber = Annotated[int, Field(description="1-based line number.")]
TailLines = Annotated[
    int | None,
    Field(description="Number of trailing stdout/stderr lines to include in command results. Use null to omit tails."),
]
TimeoutMs = Annotated[
    int | None,
    Field(description="Optional timeout in milliseconds, capped by server configuration."),
]
ParseKind = Literal[
    "auto",
    "none",
    "vitest",
    "tsc",
    "git_diff_check",
    "pytest",
    "gotest",
    "gobuild",
    "ruff",
    "mypy",
    "cargo_test",
    "cargo_build",
    "eslint",
]
MissionPreset = Literal["mandatory_system_tool_log"]
MissionPosition = Literal["before_goal"]
InsertPosition = Literal["before", "after"]


@_tool(
    name="repo_info",
    annotations={**READ_ONLY, "title": "Repository Info"},
)
def repo_info_tool(repo: RepoScope = None) -> dict:
    """Return the MCP server configuration relevant to the inspected repository.

    ``repo`` optionally selects a sub-repository in a polyrepo workspace; when
    the workspace root itself is not a git repository and ``repo`` is omitted,
    the git section reports ``polyrepo: true`` with the discovered repos.
    """
    return _repo_info_with_git(repo=repo)


def _repo_info_with_git(repo: str | None = None) -> dict:
    result = repo_info(settings)
    try:
        result["git"] = repo_git_info(settings, repo=repo)
    except Exception as exc:  # noqa: BLE001
        result["git_error"] = str(exc)
    return result


@_tool(
    name="list_dir",
    annotations={**READ_ONLY, "title": "List Directory"},
)
def list_dir_tool(path: str = ".", include_hidden: bool = True, limit: int = 200) -> dict:
    """List files and directories under a repo-relative path."""
    return list_dir(path=path, settings=settings, include_hidden=include_hidden, limit=limit)


@_tool(
    name="tree",
    annotations={**READ_ONLY, "title": "Tree"},
)
def tree_tool(path: str = ".", depth: int = 4, include_hidden: bool = True) -> dict:
    """Return a textual directory tree for a repo-relative path."""
    return tree(path=path, settings=settings, depth=depth, include_hidden=include_hidden)


@_tool(
    name="read_text_file",
    annotations={**READ_ONLY, "title": "Read Text File"},
)
def read_text_file_tool(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    with_line_numbers: bool = True,
) -> dict:
    """Read a text file from the repository, optionally limiting the line range."""
    return read_text_file(
        path=path,
        settings=settings,
        start_line=start_line,
        end_line=end_line,
        with_line_numbers=with_line_numbers,
    )


@_tool(
    name="read_multiple_files",
    annotations={**READ_ONLY, "title": "Read Multiple Files"},
)
def read_multiple_files_tool(paths: list[str]) -> dict:
    """Read several repo files at once. Useful when comparing modules or gathering context."""
    return read_multiple_files(paths=paths, settings=settings)


@_tool(
    name="file_metadata",
    annotations={**READ_ONLY, "title": "File Metadata"},
)
def file_metadata_tool(path: str, include_stat: bool = True) -> dict:
    """Return basic metadata for a repo-relative file or directory."""
    return file_metadata(path=path, settings=settings, include_stat=include_stat)


@_tool(
    name="find_files",
    annotations={**READ_ONLY, "title": "Find Files"},
)
def find_files_tool(
    pattern: str,
    path: str = ".",
    include_hidden: bool = True,
    limit: int = 200,
) -> dict:
    """Find files by glob pattern below a repo-relative path."""
    return find_files(
        pattern=pattern,
        settings=settings,
        path=path,
        include_hidden=include_hidden,
        limit=limit,
    )


@_tool(
    name="search_text",
    annotations={**READ_ONLY, "title": "Search Text"},
)
def search_text_tool(
    query: str,
    path: str = ".",
    paths: list[str] | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 100,
    mode: Annotated[
        Literal["quick", "exhaustive"],
        Field(description="quick returns a bounded inline result; exhaustive starts a durable background search job."),
    ] = "quick",
) -> dict:
    """Search text with a bounded quick path or a durable exhaustive background job."""
    try:
        return search_text(
            query=query, settings=settings, path=path, paths=paths, regex=regex,
            case_sensitive=case_sensitive, limit=limit, mode=mode,
        )
    except ResourceBusyError as exc:
        return _resource_busy_result(exc)


@_tool(
    name="symbol_search",
    annotations={**READ_ONLY, "title": "Symbol Search"},
)
def symbol_search_tool(symbol: str, path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Heuristically search for declarations or references of a symbol name."""
    return symbol_search(symbol=symbol, settings=settings, path=path, paths=paths, limit=limit)


@_tool(
    name="recent_changes",
    annotations={**READ_ONLY, "title": "Recent Changes"},
)
def recent_changes_tool(path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Return files sorted by recent filesystem modification time."""
    return recent_changes(settings=settings, path=path, paths=paths, limit=limit)


@_tool(
    name="todo_scan",
    annotations={**READ_ONLY, "title": "Todo Scan"},
)
def todo_scan_tool(path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Find TODO, FIXME, XXX, and HACK markers across the repository."""
    return todo_scan(settings=settings, path=path, paths=paths, limit=limit)


@_tool(
    name="dependency_map",
    annotations={**READ_ONLY, "title": "Dependency Map"},
)
def dependency_map_tool(path: str = ".") -> dict:
    """Parse common dependency manifest files such as pyproject.toml, package.json, go.mod, and Cargo.toml."""
    return dependency_map(settings=settings, path=path)


@_tool(
    name="git_status",
    annotations={**READ_ONLY, "title": "Git Status"},
)
def git_status_tool(short: bool = True, repo: RepoScope = None) -> dict:
    """Return the current git status for the repository (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(git_status, settings=settings, short=short, repo=repo)


@_tool(
    name="git_diff",
    annotations={**READ_ONLY, "title": "Git Diff"},
)
def git_diff_tool(
    staged: bool = False,
    pathspec: str | None = None,
    context_lines: int = 3,
    repo: RepoScope = None,
) -> dict:
    """Return git diff output for the working tree or staged changes (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(
        git_diff, settings=settings, staged=staged, pathspec=pathspec,
        context_lines=context_lines, repo=repo,
    )


@_tool(
    name="git_log",
    annotations={**READ_ONLY, "title": "Git Log"},
)
def git_log_tool(
    limit: int = 20,
    pathspec: str | None = None,
    since: str | None = None,
    repo: RepoScope = None,
) -> dict:
    """Return recent commit history, optionally filtered by path or since-date (or a polyrepo sub-repo via `repo`)."""
    return git_log(settings=settings, limit=limit, pathspec=pathspec, since=since, repo=repo)


@_tool(
    name="git_show",
    annotations={**READ_ONLY, "title": "Git Show"},
)
def git_show_tool(revision: str, path: str | None = None, repo: RepoScope = None) -> dict:
    """Show a commit object or a file at a given revision (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(git_show, settings=settings, revision=revision, path=path, repo=repo)


@_tool(
    name="git_branches",
    annotations={**READ_ONLY, "title": "Git Branches"},
)
def git_branches_tool(all_branches: bool = True, repo: RepoScope = None) -> dict:
    """List local or all branches with tracking information (or a polyrepo sub-repo via `repo`)."""
    return git_branches(settings=settings, all_branches=all_branches, repo=repo)


@_tool(
    name="git_blame",
    annotations={**READ_ONLY, "title": "Git Blame"},
)
def git_blame_tool(path: str, start_line: int = 1, end_line: int | None = None, repo: RepoScope = None) -> dict:
    """Blame a file line range to see who changed it and in which commit (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(
        git_blame, settings=settings, path=path, start_line=start_line,
        end_line=end_line, repo=repo,
    )


@_tool(
    name="git_grep",
    annotations={**READ_ONLY, "title": "Git Grep"},
)
def git_grep_tool(
    query: str,
    revision: str | None = None,
    pathspec: str | None = None,
    paths: list[str] | None = None,
    case_sensitive: bool = False,
    repo: RepoScope = None,
) -> dict:
    """Search tracked content through git grep, optionally at a revision.

    If `repo` is omitted and the workspace root is a polyrepo (not itself a
    git repository), the search fans out across all discovered sub-repos.
    """
    return git_grep(
        settings=settings,
        query=query,
        revision=revision,
        pathspec=pathspec,
        paths=paths,
        case_sensitive=case_sensitive,
        repo=repo,
    )


@_tool(
    name="list_repos",
    annotations={**READ_ONLY, "title": "List Repos"},
)
def list_repos_tool() -> dict:
    """List all git repositories in the workspace (polyrepo discovery): path, detected stack, branch, makefile targets."""
    return list_repos(settings)


def _namespace_info() -> dict:
    return {
        "tool_invocation_model": "chatgpt_connector_tools",
        "canonical_namespace_configured": settings.canonical_namespace,
        "canonical_tool_prefix_configured": f"{settings.canonical_namespace}/",
        "ephemeral_handles_supported": settings.ephemeral_handles_supported,
        "chatgpt_visible_namespace_note": "Use the tool names or handles shown by ChatGPT. Session link handles can be ephemeral.",
        "backend_restart_preserves_tunnel_url": True,
    }


def _batch_dispatch(tool: str, args: dict | None = None) -> dict:
    args = args or {}
    handlers = {
        "repo_info": lambda: _repo_info_with_git(),
        "list_dir": lambda: list_dir(settings=settings, **args),
        "tree": lambda: tree(settings=settings, **args),
        "read_text_file": lambda: read_text_file(settings=settings, **args),
        "read_multiple_files": lambda: read_multiple_files(settings=settings, **args),
        "file_metadata": lambda: file_metadata(settings=settings, **args),
        "find_files": lambda: find_files(settings=settings, **args),
        "search_text": lambda: search_text(settings=settings, **args),
        "symbol_search": lambda: symbol_search(settings=settings, **args),
        "recent_changes": lambda: recent_changes(settings=settings, **args),
        "todo_scan": lambda: todo_scan(settings=settings, **args),
        "dependency_map": lambda: dependency_map(settings=settings, **args),
        "git_status": lambda: git_status(settings=settings, **args),
        "git_diff": lambda: git_diff(settings=settings, **args),
        "git_log": lambda: git_log(settings=settings, **args),
        "git_show": lambda: git_show(settings=settings, **args),
        "git_branches": lambda: git_branches(settings=settings, **args),
        "git_blame": lambda: git_blame(settings=settings, **args),
        "git_grep": lambda: git_grep(settings=settings, **args),
        "replace_text_in_file": lambda: replace_text_in_file(settings=settings, **args)
        if args.get("dry_run") is True
        else (_ for _ in ()).throw(ValueError("batch_call only allows write tools when dry_run=true")),
        "replace_lines": lambda: replace_lines(settings=settings, **args)
        if args.get("dry_run") is True
        else (_ for _ in ()).throw(ValueError("batch_call only allows write tools when dry_run=true")),
        "insert_before_heading": lambda: insert_before_heading(settings=settings, **args)
        if args.get("dry_run") is True
        else (_ for _ in ()).throw(ValueError("batch_call only allows write tools when dry_run=true")),
        "batch_edit_files": lambda: batch_edit_files(settings=settings, **args)
        if args.get("dry_run") is True
        else (_ for _ in ()).throw(ValueError("batch_call only allows batch_edit_files when dry_run=true")),
    }
    if tool not in handlers:
        raise ValueError(f"tool is not allowed for batch_call: {tool}")
    return handlers[tool]()


def _write_config_info() -> dict:
    return {
        "access_mode": settings.access_mode,
        "full_access": settings.full_access,
        "default_dry_run": settings.default_dry_run,
        "filesystem_unrestricted": settings.filesystem_unrestricted,
        "allow_secret_access": settings.allow_secret_access,
        "allow_force_push": settings.allow_force_push,
        "allow_hard_reset": settings.allow_hard_reset,
        "write_tools_enabled": True,
        "writable_globs": list(settings.writable_globs),
        "max_write_file_bytes": settings.max_write_file_bytes,
        "max_batch_operations": settings.max_batch_operations,
        "max_combined_diff_chars": settings.max_combined_diff_chars,
        "dangerously_allow_all_writes": settings.dangerously_allow_all_writes,
        "require_expected_hash_for_writes": settings.require_expected_hash_for_writes,
        "allow_move_delete_operations": settings.allow_move_delete_operations,
        "max_patch_bytes": settings.max_patch_bytes,
        "max_command_output_chars": settings.max_command_output_chars,
        "command_timeout_ms": settings.command_timeout_ms,
        "command_audit_log_path": str(settings.command_audit_log_path),
        "mcp_auth_mode": settings.mcp_auth_mode,
        "resource_profile": settings.resource_profile,
        "resource_profile_applied": settings.resource_profile_applied,
        "resource_detected_memory_bytes": settings.resource_detected_memory_bytes,
        "resource_buffer_bytes": settings.resource_buffer_bytes,
        "resource_buffer_enforced": False,
        "resource_buffer_semantics": "diagnostic_estimate_only",
        "max_heavy_operations": settings.max_heavy_operations,
        "persist_full_output": settings.persist_full_output,
    }


def _write_result(func, *args, **kwargs) -> dict:
    if "dry_run" in kwargs and kwargs["dry_run"] is None:
        kwargs["dry_run"] = settings.default_dry_run
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return structured_error(exc)


def _resource_busy_result(exc: ResourceBusyError) -> dict[str, Any]:
    return {
        "ok": False,
        "error_kind": "resource_busy",
        "error": str(exc),
        "capacity": exc.capacity,
        "operations": exc.operations,
        "retry_hint": "Inspect list_heavy_operations, cancel a safe operation, or Retry after one finishes.",
    }


def _structural_result(func, *args, **kwargs) -> dict:
    """Call a git-workflow/github/lsp/index tool function with uniform error handling.

    Mirrors the confirmation-required handling already used by the command
    tools (`_command_result`), so every structural tool behaves the same way
    when it needs explicit owner confirmation. Also converts `GitToolError`
    (raised for truly exceptional git failures, e.g. an invalid revision or a
    disabled feature) into a structured `{"ok": False, "error_kind": "git_error", ...}`
    result instead of letting it propagate as an unhandled exception.
    """
    if "dry_run" in kwargs and kwargs["dry_run"] is None:
        kwargs["dry_run"] = settings.default_dry_run
    if "confirmed" in kwargs:
        kwargs["confirmed"] = settings.confirmation_granted(kwargs["confirmed"])
    try:
        return func(*args, **kwargs)
    except ConfirmationRequiredError as exc:
        return {"ok": False, "error_kind": "confirmation_required", "message": str(exc)}
    except GitToolError as exc:
        if exc.result is not None:
            return exc.result
        return {"ok": False, "error_kind": "git_error", "message": str(exc)}
    except ResourceBusyError as exc:
        return _resource_busy_result(exc)


def _command_result(
    command: str,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
    parse_kind: str | None = "auto",
) -> dict:
    try:
        return run_command(
            command=command,
            settings=settings,
            timeout_ms=timeout_ms,
            cwd=cwd,
            env=env,
            max_output_chars=max_output_chars,
            tail_lines=tail_lines,
            confirmed=confirmed,
            parse_kind=parse_kind,
        )
    except ConfirmationRequiredError as exc:
        return {"ok": False, "error_kind": "confirmation_required", "reason": str(exc), "command": command}
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "command": command}
    except ResourceBusyError as exc:
        return {**_resource_busy_result(exc), "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "command": command}


def _first_existing_repo_file(candidates: list[str]) -> str | None:
    """Return the first candidate repo-relative path that exists as a file."""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if (settings.project_root / candidate).is_file():
                return candidate
        except Exception:  # noqa: BLE001, S112 - optional candidate probing is best-effort
            continue
    return None


def _mission_context_candidates() -> list[str]:
    """Candidate context files for this repo: profile-declared mission files, then README."""
    profile = load_repo_profile(settings)
    candidates = [
        profile.mission.get("current"),
        profile.mission.get("memory"),
        "README.md",
        "README_RU.md",
    ]
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _first_root_markdown_file() -> str | None:
    """Return the first Markdown file found at the repository root, if any."""
    try:
        listing = list_dir(path=".", settings=settings, include_hidden=False, limit=200)
    except Exception:  # noqa: BLE001 - optional Markdown probe is best-effort
        return None
    for entry in listing.get("entries", []):
        if entry.get("type") == "file" and str(entry.get("name", "")).lower().endswith(".md"):
            return entry.get("path") or entry.get("name")
    return None


def _search_probe_token() -> str:
    """Pick a neutral token to sanity-check search_text/symbol_search.

    Uses the name of the first file found at the repository root so the
    check never depends on any project-specific symbol; falls back to a
    generic keyword when the root has no files.
    """
    try:
        listing = list_dir(path=".", settings=settings, include_hidden=False, limit=50)
        for entry in listing.get("entries", []):
            if entry.get("type") == "file" and entry.get("name"):
                return entry["name"]
    except Exception:  # noqa: BLE001, S110 - optional probe falls back to a neutral token
        pass
    return "def"


def _capability_matrix() -> dict[str, dict[str, Any]]:
    """Report external binaries through the server's effective PATH."""
    binaries = ["git", "rg", "gh", "ctags", "go", "python3", "node", "docker"]
    return {name: tool_status(name, settings) for name in binaries}


@_tool(
    name="list_heavy_operations",
    annotations={**READ_ONLY, "title": "List Heavy Operations"},
)
def list_heavy_operations_tool() -> dict:
    """List every operation currently occupying the shared heavy-operation pool."""
    return list_heavy_operations(settings)


@_tool(
    name="cancel_heavy_operation",
    annotations={**WRITE_ACTION, "title": "Cancel Heavy Operation"},
)
def cancel_heavy_operation_tool(operation_id: str) -> dict:
    """Cancel one synchronous heavy operation by its observable operation id."""
    return cancel_heavy_operation(settings, operation_id)


@_tool(
    name="doctor",
    annotations={**READ_ONLY, "title": "Doctor"},
)
def doctor_tool() -> dict:
    """Run a compact health check for repository, git, policy, search, and symbol tools."""
    checks: dict[str, Any] = {}
    try:
        checks["repo_info"] = {"ok": True, "result": _repo_info_with_git()}
    except Exception as exc:  # noqa: BLE001
        checks["repo_info"] = {"ok": False, "error": str(exc)}

    try:
        checks["git_status"] = {"ok": True, "result": _batch_dispatch("git_status", {"short": True})}
    except Exception as exc:  # noqa: BLE001
        checks["git_status"] = {"ok": False, "error": str(exc)}

    mission_path = _first_existing_repo_file(_mission_context_candidates())
    if mission_path:
        try:
            result = _batch_dispatch("read_text_file", {"path": mission_path, "start_line": 1, "end_line": 1})
            checks["mission_context"] = {"ok": True, "path": mission_path, "result": result}
        except Exception as exc:  # noqa: BLE001
            checks["mission_context"] = {"ok": False, "path": mission_path, "error": str(exc)}
    else:
        checks["mission_context"] = {
            "ok": True,
            "status": "skipped",
            "reason": "no mission or README context file found",
        }

    try:
        _batch_dispatch("read_text_file", {"path": ".env", "start_line": 1, "end_line": 1})
        checks["blocked_policy"] = {"ok": False, "error": "expected .env to be blocked by policy"}
    except Exception as exc:  # noqa: BLE001
        checks["blocked_policy"] = {"ok": True, "error": str(exc)}

    probe_token = _search_probe_token()
    for tool_name in ("search_text", "symbol_search"):
        args = (
            {"query": probe_token, "path": ".", "limit": 1}
            if tool_name == "search_text"
            else {"symbol": probe_token, "path": ".", "limit": 1}
        )
        try:
            checks[tool_name] = {"ok": True, "probe_token": probe_token, "result": _batch_dispatch(tool_name, args)}
        except Exception as exc:  # noqa: BLE001
            checks[tool_name] = {"ok": False, "probe_token": probe_token, "error": str(exc)}

    try:
        checks["workspace"] = {"ok": True, "result": list_workspace_repos(settings)}
    except Exception as exc:  # noqa: BLE001
        checks["workspace"] = {"ok": False, "error": str(exc)}

    checks["capabilities"] = {"ok": True, "result": _capability_matrix()}

    tools = _tool_names()
    path_entries, _, path_warnings = effective_path(settings)
    matrix = _capability_matrix()
    return {
        "project_root": str(settings.project_root),
        **_namespace_info(),
        **_write_config_info(),
        "tools": tools,
        "tool_count": len(tools),
        "heavy_operations": list_heavy_operations(settings),
        "effective_path": path_entries,
        "path_warnings": path_warnings,
        "toolchains": [matrix.get(name, {"name": name, "available": False, "source": "not_found"}) for name in ("go", "python3", "node")],
        "capabilities": {
            **matrix,
            "pty": {
                "available": os.name == "posix",
                "enabled": bool(settings.full_access and settings.enable_pty and os.name == "posix"),
                "reason": None if os.name == "posix" else "POSIX PTY is unavailable on this platform",
            },
            "subagents": {
                "available": False,
                "enabled": False,
                "reason": "No executor configured in V1",
            },
        },
        "checks": checks,
    }


@_tool(
    name="smoke_all",
    annotations={**READ_ONLY, "title": "Smoke All"},
)
def smoke_all_tool() -> dict:
    """Run a self-check smoke test of core MCP capabilities in one call."""
    probe_token = _search_probe_token()
    mission_path = _first_existing_repo_file(_mission_context_candidates())
    md_path = _first_root_markdown_file()

    plan: list[tuple[str, str, dict[str, Any]]] = [
        ("repo_info", "repo_info", {}),
        ("list_dir", "list_dir", {"path": ".", "limit": 300}),
        ("search_text", "search_text", {"query": probe_token, "path": ".", "limit": 3}),
        ("symbol_search", "symbol_search", {"symbol": probe_token, "path": ".", "limit": 3}),
        ("git_status", "git_status", {"short": True}),
        ("git_log", "git_log", {"limit": 3}),
        ("git_show", "git_show", {"revision": "HEAD"}),
        ("blocked_policy", "read_text_file", {"path": ".env", "start_line": 1, "end_line": 1}),
        ("batch_write_dry_run", "batch_edit_files", {"operations": [{"op": "ensure_directory", "path": "reports/mcp-smoke"}], "dry_run": True}),
        ("run_command_git_diff_check", "run_command", {"command": "git diff --check"}),
        ("run_command_git_version", "run_command", {"command": "git --version"}),
        ("list_test_presets", "list_test_presets", {}),
        ("policy_scan", "scan_new_policy_violations", {"base_ref": "HEAD", "paths": []}),
    ]
    if mission_path:
        plan.insert(1, ("mission_context", "read_text_file", {"path": mission_path, "start_line": 1, "end_line": 1}))
    if md_path:
        try:
            expected_sha = current_text_sha256(md_path, settings)
        except Exception:  # noqa: BLE001
            expected_sha = None
        plan.append(
            (
                "write_dry_run",
                "replace_text_in_file",
                {"path": md_path, "find": "\n", "replace": "\n", "dry_run": True, "expected_sha256": expected_sha},
            )
        )

    checks = []
    for key, tool, args in plan:
        try:
            if tool == "run_command":
                result = _command_result(**args)
            elif tool == "list_test_presets":
                result = list_test_presets(settings)
            elif tool == "scan_new_policy_violations":
                result = scan_new_policy_violations(settings, **args)
            else:
                result = _batch_dispatch(tool, args)
            ok = key != "blocked_policy"
            item = {"name": key, "tool": tool, "ok": ok}
            if tool == "list_dir":
                names = [entry["name"] for entry in result.get("entries", [])]
                item["blocked_visible"] = [
                    value for value in [".env", ".git", "node_modules", ".venv"] if value in names
                ]
                item["ok"] = not item["blocked_visible"]
            elif tool in {"search_text", "symbol_search", "git_log"}:
                item["count"] = result.get("count")
            elif tool == "repo_info":
                item["project_root"] = result.get("project_root")
                item["git_error"] = result.get("git_error")
                item["ok"] = not result.get("git_error")
            checks.append(item)
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": key, "tool": tool, "ok": key == "blocked_policy", "error": str(exc)})

    if not mission_path:
        checks.append(
            {
                "name": "mission_context",
                "tool": "read_text_file",
                "ok": True,
                "status": "skipped",
                "reason": "no mission or README context file found",
            }
        )
    if not md_path:
        checks.append(
            {
                "name": "write_dry_run",
                "tool": "replace_text_in_file",
                "ok": True,
                "status": "skipped",
                "reason": "no markdown file found at repository root",
            }
        )

    return {
        **_namespace_info(),
        **_write_config_info(),
        "project_root": str(settings.project_root),
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
    }


@_tool(
    name="context_bootstrap",
    annotations={**READ_ONLY, "title": "Context Bootstrap"},
)
def context_bootstrap_tool() -> dict:
    """Read the repository's standard context files and polyrepo workspace info in one call.

    Reads a neutral, stack-agnostic set of optional context files (README,
    AGENTS/CLAUDE guides, the repo's ``.chatrepo/mcp.yml`` profile, plus any
    mission files declared in that profile) and reports discovered
    sub-repositories for polyrepo workspaces. Missing files are reported as
    ``missing``, never as an error.
    """
    profile = load_repo_profile(settings)
    paths = list(
        dict.fromkeys(
            [
                "AGENTS.md",
                "CLAUDE.md",
                "README.md",
                "README_RU.md",
                ".chatrepo/mcp.yml",
                *profile.mission.values(),
            ]
        )
    )
    files = []
    for path in paths:
        try:
            files.append(read_text_file(path=path, settings=settings))
        except FileNotFoundError:
            files.append({"path": path, "missing": True})
        except ValueError as exc:
            if "not a file" in str(exc):
                files.append({"path": path, "missing": True})
            else:
                files.append({"path": path, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            files.append({"path": path, "error": str(exc)})
    try:
        workspace_repos = list_workspace_repos(settings)
    except Exception:  # noqa: BLE001
        workspace_repos = []
    return {"files": files, "count": len(files), "workspace": workspace_repos}


@_tool(
    name="batch_call",
    annotations={**READ_ONLY, "title": "Batch Call"},
)
def batch_call_tool(
    calls: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Up to 10 calls shaped as {'tool': '<tool_name>', 'args': {...}}. "
                "Only read-only tools are allowed, except selected write tools with dry_run=true."
            ),
            max_length=10,
        ),
    ],
    execution: Literal["parallel", "sequential"] = "parallel",
    max_concurrency: Annotated[int, Field(ge=1, le=10)] = 4,
) -> dict:
    """Run up to 10 safe inspection/preview calls, in parallel by default."""
    if len(calls) > 10:
        raise ValueError("too many calls; max is 10")
    def invoke(index: int, call: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        tool = call.get("tool")
        args = call.get("args") or {}
        if not isinstance(tool, str) or not isinstance(args, dict):
            return index, {"index": index, "tool": tool, "ok": False, "error": "call must contain string tool and object args"}
        try:
            return index, {"index": index, "tool": tool, "ok": True, "result": _batch_dispatch(tool, args)}
        except ResourceBusyError as exc:
            return index, {
                "index": index,
                "tool": tool,
                "ok": False,
                "result": _resource_busy_result(exc),
            }
        except Exception as exc:  # noqa: BLE001
            return index, {"index": index, "tool": tool, "ok": False, "error": str(exc)}

    ordered: list[dict[str, Any] | None] = [None] * len(calls)
    applied_concurrency = 1 if execution == "sequential" else min(max_concurrency, max(len(calls), 1))
    if execution == "sequential":
        for index, call in enumerate(calls):
            _, ordered[index] = invoke(index, call)
    else:
        with ThreadPoolExecutor(max_workers=min(applied_concurrency, max(len(calls), 1))) as pool:
            futures = [pool.submit(invoke, index, call) for index, call in enumerate(calls)]
            for future in as_completed(futures):
                index, result = future.result()
                ordered[index] = result
    results = [item for item in ordered if item is not None]
    return {
        "ok": all(item["ok"] for item in results),
        "execution": execution,
        "max_concurrency": max_concurrency,
        "requested_max_concurrency": max_concurrency,
        "applied_max_concurrency": applied_concurrency,
        "heavy_capacity": settings.max_heavy_operations,
        "resource_profile": settings.resource_profile_applied,
        "results": results,
        "count": len(results),
    }


@_tool(
    name="write_text_file",
    annotations={**WRITE_ACTION, "title": "Write Text File"},
)
def write_text_file_tool(
    path: RepoPath,
    content: SmallText,
    create_if_missing: Annotated[bool, Field(description="When true, create the file if it does not exist.")] = False,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to replace the entire contents of an allowed UTF-8 repo text file."""
    return _write_result(
        write_text_file,
        path=path,
        content=content,
        settings=settings,
        create_if_missing=create_if_missing,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="replace_text_in_file",
    annotations={**SAFE_EDIT_ACTION, "title": "Replace Text In File"},
)
def replace_text_in_file_tool(
    path: RepoPath,
    find: Annotated[str, Field(description="Exact UTF-8 text fragment to find.")],
    replace: Annotated[str, Field(description="Replacement UTF-8 text.")],
    replace_all: Annotated[bool, Field(description="When false, replace exactly one occurrence.")] = False,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to replace an exact text fragment in an allowed UTF-8 repo file."""
    return _write_result(
        replace_text_in_file,
        path=path,
        find=find,
        replace=replace,
        settings=settings,
        replace_all=replace_all,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="insert_text_in_file",
    annotations={**SAFE_EDIT_ACTION, "title": "Insert Text In File"},
)
def insert_text_in_file_tool(
    path: RepoPath,
    anchor: Annotated[str, Field(description="Exact text anchor already present in the target file.")],
    position: Annotated[InsertPosition, Field(description="Insert content before or after the exact anchor.")],
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to insert text before or after an exact anchor in an allowed repo file."""
    return _write_result(
        insert_text_in_file,
        path=path,
        anchor=anchor,
        position=position,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="delete_text_in_file",
    annotations={**WRITE_ACTION, "title": "Delete Text In File"},
)
def delete_text_in_file_tool(
    path: RepoPath,
    find: Annotated[str | None, Field(description="Exact text to delete. Use either this or start_line/end_line.")] = None,
    start_line: Annotated[int | None, Field(description="1-based first line to delete when deleting by line range.")] = None,
    end_line: Annotated[int | None, Field(description="1-based last line to delete when deleting by line range.")] = None,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to delete exact text or a line range from an allowed repo file."""
    return _write_result(
        delete_text_in_file,
        path=path,
        settings=settings,
        find=find,
        start_line=start_line,
        end_line=end_line,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="create_text_file",
    annotations={**SAFE_EDIT_ACTION, "title": "Create Text File"},
)
def create_text_file_tool(
    path: RepoPath,
    content: SmallText,
    overwrite: Annotated[bool, Field(description="When true, replace an existing text file.")] = False,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to create a new UTF-8 text file in the repository."""
    return _write_result(create_text_file, path=path, content=content, settings=settings, overwrite=overwrite, dry_run=dry_run)


@_tool(
    name="move_path",
    annotations={**WRITE_ACTION, "title": "Move Path"},
)
def move_path_tool(
    source_path: RepoPath,
    destination_path: RepoPath,
    overwrite: Annotated[bool, Field(description="When true, overwrite the destination path if it exists.")] = False,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to rename or move an allowed UTF-8 repo file."""
    return _write_result(
        move_path,
        source_path=source_path,
        destination_path=destination_path,
        settings=settings,
        overwrite=overwrite,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="delete_path",
    annotations={**WRITE_ACTION, "title": "Delete Path"},
)
def delete_path_tool(
    path: RepoPath,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to delete an allowed UTF-8 repo file."""
    return _write_result(delete_path, path=path, settings=settings, expected_sha256=expected_sha256, dry_run=dry_run)


@_tool(
    name="ensure_directory",
    annotations={**SAFE_EDIT_ACTION, "title": "Ensure Directory"},
)
def ensure_directory_tool(path: RepoPath, dry_run: DryRun = None) -> dict:
    """Use this when you need to create a directory for docs, reports, packets, or source files."""
    return _write_result(ensure_directory, path=path, settings=settings, dry_run=dry_run)


@_tool(
    name="batch_edit_files",
    annotations={**WRITE_ACTION, "title": "Batch Edit Files"},
)
def batch_edit_files_tool(
    operations: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Ordered edit operations. Each item must include op plus operation-specific fields. "
                "Supported op values: write, replace, insert, delete_text, create_file, move, delete_file, ensure_directory."
            )
        ),
    ],
    atomic: Annotated[bool, Field(description="When true, rollback all earlier operations if any operation fails.")] = True,
    dry_run: DryRun = None,
) -> dict:
    """Use this when several related repo edits must be previewed or applied together with one combined diff."""
    return _write_result(batch_edit_files, operations=operations, settings=settings, atomic=atomic, dry_run=dry_run)


@_tool(
    name="apply_change_set",
    annotations={**WRITE_ACTION, "title": "Apply Change Set"},
)
def apply_change_set_tool(
    operations: Annotated[
        list[dict[str, Any]],
        Field(description="Non-empty list of exact edit operations. Each file operation may include expected_sha256."),
    ],
    atomic: Annotated[bool, Field(description="When true, rollback earlier applied operations if any operation fails.")] = True,
    dry_run: DryRun = None,
    name: Annotated[str | None, Field(description="Optional human-readable change-set name.")] = None,
) -> dict:
    """Use this for multi-file exact repo edits with dry-run diff preview, rollback, and structured errors."""
    return _write_result(apply_change_set, operations=operations, settings=settings, atomic=atomic, dry_run=dry_run, name=name)


@_tool(
    name="replace_lines",
    annotations={**SAFE_EDIT_ACTION, "title": "Replace Lines"},
)
def replace_lines_tool(
    path: RepoPath,
    start_line: LineNumber,
    end_line: LineNumber,
    replacement: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to replace a small line range in an allowed UTF-8 repo file."""
    return _write_result(
        replace_lines,
        path=path,
        start_line=start_line,
        end_line=end_line,
        replacement=replacement,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="insert_before_line",
    annotations={**SAFE_EDIT_ACTION, "title": "Insert Before Line"},
)
def insert_before_line_tool(
    path: RepoPath,
    line: LineNumber,
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to insert compact text before a specific line number."""
    return _write_result(
        insert_before_line,
        path=path,
        line=line,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="insert_after_line",
    annotations={**SAFE_EDIT_ACTION, "title": "Insert After Line"},
)
def insert_after_line_tool(
    path: RepoPath,
    line: LineNumber,
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to insert compact text after a specific line number."""
    return _write_result(
        insert_after_line,
        path=path,
        line=line,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="insert_before_heading",
    annotations={**SAFE_EDIT_ACTION, "title": "Insert Before Heading"},
)
def insert_before_heading_tool(
    path: RepoPath,
    heading: Annotated[str, Field(description="Exact Markdown heading text, for example '## Goal'.")],
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to insert markdown before a heading with a small payload."""
    return _write_result(
        insert_before_heading,
        path=path,
        heading=heading,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="insert_after_heading",
    annotations={**SAFE_EDIT_ACTION, "title": "Insert After Heading"},
)
def insert_after_heading_tool(
    path: RepoPath,
    heading: Annotated[str, Field(description="Exact Markdown heading text, for example '## Goal'.")],
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to insert markdown after a heading with a small payload."""
    return _write_result(
        insert_after_heading,
        path=path,
        heading=heading,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="append_to_file",
    annotations={**SAFE_EDIT_ACTION, "title": "Append To File"},
)
def append_to_file_tool(
    path: RepoPath,
    content: SmallText,
    expected_sha256: ExpectedSha = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to append a small text block to an allowed UTF-8 repo file."""
    return _write_result(
        append_to_file,
        path=path,
        content=content,
        settings=settings,
        expected_sha256=expected_sha256,
        dry_run=dry_run,
    )


@_tool(
    name="apply_patch",
    annotations={**WRITE_ACTION, "title": "Apply Patch"},
)
def apply_patch_tool(
    patch: Annotated[str, Field(description="Unified diff patch text using diff --git file headers.")],
    dry_run: DryRun = None,
    expected_base_sha: Annotated[
        str | None,
        Field(description="Optional expected git HEAD/base SHA before applying the patch."),
    ] = None,
    repo: RepoScope = None,
) -> dict:
    """Use this when you need to apply a unified diff patch across one or more allowed repo files."""
    return _write_result(
        apply_patch_diff,
        patch=patch,
        settings=settings,
        dry_run=dry_run,
        expected_base_sha=expected_base_sha,
        repo=repo,
    )


@_tool(
    name="update_current_mission",
    annotations={**SAFE_EDIT_ACTION, "title": "Update Current Mission"},
)
def update_current_mission_tool(
    section_title: Annotated[
        str | None,
        Field(description="Markdown section heading/title to insert when not using a preset."),
    ] = None,
    content: Annotated[
        str | None,
        Field(description="Markdown content for the new section. Prefer chunks for long content."),
    ] = None,
    position: Annotated[MissionPosition, Field(description="Where to insert the mission section.")] = "before_goal",
    preset: Annotated[
        MissionPreset | None,
        Field(description="Optional server-side mission template. Use this to avoid large ChatGPT payloads."),
    ] = None,
    chunks: Annotated[
        list[str] | None,
        Field(description="Optional ordered content chunks joined server-side for safer long mission updates."),
    ] = None,
    dry_run: DryRun = None,
) -> dict:
    """Use this when you need to add a mission section to the repo's configured mission file (see profile.mission) before ## Goal."""
    return _write_result(
        update_current_mission,
        section_title=section_title,
        content=content,
        settings=settings,
        position=position,
        preset=preset,
        chunks=chunks,
        dry_run=dry_run,
    )


@_tool(
    name="run_command",
    annotations={**COMMAND_ACTION, "title": "Run Command"},
)
def run_command_tool(
    command: Annotated[
        str,
        Field(
            description=(
                "Repo-local command to run through /bin/bash -lc. Prefer run_test_preset for known tests. "
                "Server policy blocks secrets paths and forbidden executables."
            )
        ),
    ],
    timeout_ms: TimeoutMs = None,
    cwd: OptionalRepoPath = None,
    env: Annotated[
        dict[str, str] | None,
        Field(description="Optional environment overrides. Keys must be shell-safe names; secret values are redacted from output."),
    ] = None,
    max_output_chars: Annotated[
        int | None,
        Field(ge=1, description="Optional stdout/stderr character cap, capped by server configuration."),
    ] = None,
    tail_lines: TailLines = 200,
    confirmed: Annotated[
        bool,
        Field(description="Set true only after the owner explicitly confirms a command that server policy marks risky."),
    ] = False,
    parse_kind: Annotated[
        ParseKind,
        Field(description="Output parser to attach structured summary. Use auto for command-based inference."),
    ] = "auto",
) -> dict:
    """Use this when you need to run an allowlisted validation command and report exit code."""
    return _command_result(
        command=command,
        timeout_ms=timeout_ms,
        cwd=cwd,
        env=env,
        max_output_chars=max_output_chars,
        tail_lines=tail_lines,
        confirmed=settings.confirmation_granted(confirmed),
        parse_kind=parse_kind,
    )


@_tool(
    name="run_commands",
    annotations={**COMMAND_ACTION, "title": "Run Commands"},
)
def run_commands_tool(
    commands: Annotated[
        list[str],
        Field(description="Ordered repo-local commands to run. Use this instead of shell operators like &&.", min_length=1),
    ],
    stop_on_failure: Annotated[bool, Field(description="When true, stop after the first non-zero exit code.")] = False,
    timeout_ms: TimeoutMs = None,
    tail_lines: TailLines = 200,
    confirmed: Annotated[
        bool,
        Field(description="Set true only after the owner explicitly confirms commands that server policy marks risky."),
    ] = False,
    parse_kind: Annotated[
        ParseKind,
        Field(description="Output parser for every command result. Use auto for command-based inference."),
    ] = "auto",
) -> dict:
    """Use this when you need to run several allowlisted validation commands and compare exit codes."""
    return run_commands(
        commands=commands,
        settings=settings,
        stop_on_failure=stop_on_failure,
        timeout_ms=timeout_ms,
        tail_lines=tail_lines,
        confirmed=confirmed,
        parse_kind=parse_kind,
    )


@_tool(
    name="run_test_preset",
    annotations={**COMMAND_ACTION, "title": "Run Test Preset"},
)
def run_test_preset_tool(
    preset: Annotated[
        str,
        Field(
            description=(
                "Preset to run: an action name (test, lint, typecheck, format, build) resolved for the "
                "workspace root or the given cwd; a composite 'service/path:action' to resolve it for a "
                "polyrepo sub-directory (autodetected via go.mod/pyproject.toml/package.json/Cargo.toml/"
                "Makefile, see list_repos/list_test_presets); or a named preset key from .chatrepo/mcp.yml "
                f"or the generic built-ins: {', '.join(TEST_PRESETS)}."
            )
        ),
    ],
    timeout_ms: TimeoutMs = None,
    tail_lines: TailLines = 200,
    background: Annotated[bool, Field(description="When true, start the preset as a background job and poll it later.")] = False,
    cwd: OptionalRepoPath = None,
) -> dict:
    """Use this when you need to run a stack-autodetected or named test/lint/build preset without sending a long command string.

    `cwd` is a convenience for the bare action-name form: passing
    `preset="test", cwd="api-gateway"` is equivalent to `preset="api-gateway:test"`.
    It is ignored when `preset` already uses the composite `service:action` syntax
    or names an explicit profile preset.
    """
    effective_preset = preset
    if cwd and ":" not in preset:
        effective_preset = f"{cwd.strip('/')}:{preset}"
    try:
        return run_test_preset(
            preset=effective_preset,
            settings=settings,
            timeout_ms=timeout_ms,
            tail_lines=tail_lines,
            background=background,
        )
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "preset": preset}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "preset": preset}


@_tool(
    name="list_test_presets",
    annotations={**READ_ONLY, "title": "List Test Presets"},
)
def list_test_presets_tool(
    path: Annotated[
        str | None,
        Field(
            description=(
                "Optional workspace sub-directory to resolve presets for (as returned by list_repos). "
                "When omitted, returns the repo-profile presets plus a per-repo summary of autodetected "
                "actions across the whole workspace."
            )
        ),
    ] = None,
) -> dict:
    """Use this to list built-in/autodetected and repo-local command/test presets from .chatrepo/mcp.yml."""
    return list_test_presets(settings, path=path)


@_tool(
    name="run_quality_gate",
    annotations={**COMMAND_ACTION, "title": "Run Quality Gate"},
)
def run_quality_gate_tool(
    checks: Annotated[list[dict[str, Any]], Field(description="Ordered checks. Each item uses preset or command, plus optional id, required, timeout_ms, parse_kind, repo.")],
    name: Annotated[str | None, Field(description="Optional human-readable gate name.")] = None,
    stop_on_failure: Annotated[bool, Field(description="When true, stop after the first failed required check.")] = True,
    repo: RepoScope = None,
) -> dict:
    """Use this to run a structured quality gate from presets, commands, and policy scans.

    `repo` sets the default sub-repository for policy-scan checks in a
    polyrepo workspace; an individual check can override it with its own `repo` key.
    """
    try:
        return run_quality_gate(settings, checks=checks, name=name, stop_on_failure=stop_on_failure, repo=repo)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "quality_gate_failed", "error": str(exc)}


@_tool(
    name="quality_gate_and_commit",
    annotations={**WRITE_ACTION, "title": "Quality Gate And Commit"},
)
def quality_gate_and_commit_tool(
    checks: Annotated[list[dict[str, Any]], Field(description="Required/optional checks to run before committing.")],
    commit: Annotated[dict[str, Any], Field(description="Commit config with message, paths, and optional enabled boolean.")],
    name: Annotated[str | None, Field(description="Optional human-readable gate name.")] = None,
    require_clean_after_commit: Annotated[bool, Field(description="When true, final status must be clean after commit.")] = True,
    repo: RepoScope = None,
) -> dict:
    """Use this to run quality gates and commit exactly listed files only if all required gates pass."""
    try:
        return quality_gate_and_commit(
            settings,
            checks=checks,
            commit=commit,
            name=name,
            require_clean_after_commit=require_clean_after_commit,
            repo=repo,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "quality_gate_commit_failed", "error": str(exc)}


@_tool(
    name="scan_new_policy_violations",
    annotations={**READ_ONLY, "title": "Scan New Policy Violations"},
)
def scan_new_policy_violations_tool(
    base_ref: Annotated[str, Field(description="Git base revision to diff against, for example HEAD or HEAD~1.")] = "HEAD",
    paths: Annotated[list[str] | None, Field(description="Optional repo-relative paths to limit the diff scan.")] = None,
    rules: Annotated[list[str] | None, Field(description="Optional rule ids. Defaults come from .chatrepo/mcp.yml or built-ins.")] = None,
    repo: RepoScope = None,
) -> dict:
    """Use this to scan only newly added diff lines for policy violations such as new any casts."""
    try:
        return scan_new_policy_violations(settings, base_ref=base_ref, paths=paths, rules=rules, repo=repo)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "policy_scan_failed", "error": str(exc)}


@_tool(
    name="command_policy_check",
    annotations={**READ_ONLY, "title": "Command Policy Check"},
)
def command_policy_check_tool(command: Annotated[str, Field(description="Command to validate without executing it.")]) -> dict:
    """Use this to explain whether a command is allowed and suggest safe alternatives."""
    return command_policy_check(command, settings)


@_tool(
    name="read_artifact",
    annotations={**READ_ONLY, "title": "Read Artifact"},
)
def read_artifact_tool(
    artifact_id: Annotated[str, Field(min_length=1, description="Opaque artifact id returned by another tool receipt.")],
    cursor: Annotated[str | None, Field(description="Opaque continuation cursor from the previous page.")] = None,
    max_bytes: Annotated[int, Field(ge=1, le=262_144, description="Maximum artifact bytes to return in this page.")] = 65_536,
) -> dict:
    """Read a bounded page from a durable tool-output artifact."""
    try:
        return read_artifact(artifact_id, settings, cursor=cursor, max_bytes=max_bytes)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "artifact_read_error", "error": str(exc), "artifact_id": artifact_id}


@_tool(
    name="get_command_log",
    annotations={**READ_ONLY, "title": "Get Command Log"},
)
def get_command_log_tool(
    log_id: Annotated[str, Field(description="Log id returned by run_command or run_commands.")],
    stream: Annotated[Literal["stdout", "stderr", "combined"], Field(description="Which stream to read; PTY logs use combined.")] = "stdout",
    start_line: Annotated[int | None, Field(description="Optional first 1-based line to return.")] = None,
    end_line: Annotated[int | None, Field(description="Optional last 1-based line to return.")] = None,
    grep: Annotated[str | None, Field(description="Optional regular expression to filter lines.")] = None,
) -> dict:
    """Use this to read saved command logs by line range or grep."""
    try:
        return get_command_log(log_id, settings, stream=stream, start_line=start_line, end_line=end_line, grep=grep)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_log_error", "error": str(exc), "log_id": log_id}


@_tool(
    name="summarize_command_log",
    annotations={**READ_ONLY, "title": "Summarize Command Log"},
)
def summarize_command_log_tool(
    log_id: Annotated[str, Field(description="Log id returned by run_command or run_commands.")],
    parser: Annotated[ParseKind, Field(description="Parser to apply to the saved log.")] = "auto",
) -> dict:
    """Use this to parse and summarize a saved command log."""
    try:
        return summarize_command_log(log_id, settings, parser=parser)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_log_error", "error": str(exc), "log_id": log_id}


@_tool(
    name="git_worktree_guard",
    annotations={**READ_ONLY, "title": "Git Worktree Guard"},
)
def git_worktree_guard_tool(
    allowed_dirty_paths: Annotated[list[str] | None, Field(description="Dirty paths allowed to exist before work starts.")] = None,
    require_branch: Annotated[str | None, Field(description="Optional required current branch name.")] = None,
    require_not_rebasing: Annotated[bool, Field(description="When true, fail if git rebase state exists.")] = True,
    repo: RepoScope = None,
) -> dict:
    """Use this before edits/commits to verify branch, rebase state, and unexpected dirty files (or a polyrepo sub-repo via `repo`)."""
    try:
        return git_worktree_guard(
            settings,
            allowed_dirty_paths=allowed_dirty_paths,
            require_branch=require_branch,
            require_not_rebasing=require_not_rebasing,
            repo=repo,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "worktree_guard_failed", "error": str(exc)}


@_tool(
    name="start_command_job",
    annotations={**COMMAND_ACTION, "title": "Start Command Job"},
)
def start_command_job_tool(
    command: Annotated[
        str,
        Field(description="Long-running repo-local command to start in the background. Poll with get_command_job."),
    ],
    timeout_ms: TimeoutMs = None,
    cwd: OptionalRepoPath = None,
    env: Annotated[
        dict[str, str] | None,
        Field(description="Optional environment overrides. Secret-like output is redacted."),
    ] = None,
    tail_lines: TailLines = 200,
    confirmed: Annotated[
        bool,
        Field(description="Set true only after owner confirmation for policy-gated commands."),
    ] = False,
    concurrency_key: Annotated[
        str | None,
        Field(description="Optional lock key. Jobs with the same key cannot start in parallel silently."),
    ] = None,
    on_conflict: Annotated[
        Literal["fail", "attach", "wait"],
        Field(description="How to behave if concurrency_key is already locked by a running job."),
    ] = "fail",
) -> dict:
    """Use this for long-running allowlisted repo commands that should be polled later."""
    try:
        return start_command_job(
            command=command,
            settings=settings,
            timeout_ms=timeout_ms,
            cwd=cwd,
            env=env,
            tail_lines=tail_lines,
            confirmed=confirmed,
            concurrency_key=concurrency_key,
            on_conflict=on_conflict,
        )
    except ConfirmationRequiredError as exc:
        return {"ok": False, "error_kind": "confirmation_required", "reason": str(exc), "command": command}
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "command": command}
    except ResourceBusyError as exc:
        return {**_resource_busy_result(exc), "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "command": command}


@_tool(
    name="get_command_job",
    annotations={**READ_ONLY, "title": "Get Command Job"},
)
def get_command_job_tool(job_id: str, tail_lines: int | None = 200) -> dict:
    """Use this to poll a background command job and read output tails."""
    try:
        return get_command_job(job_id=job_id, settings=settings, tail_lines=tail_lines)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc), "job_id": job_id}


@_tool(
    name="get_job_status",
    annotations={**READ_ONLY, "title": "Get Job Status"},
)
def get_job_status_tool(job_id: Annotated[str, Field(description="Background command job id.")]) -> dict:
    """Use this to read concise lifecycle status for a background command job."""
    try:
        return get_job_status(job_id=job_id, settings=settings)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc), "job_id": job_id}


@_tool(
    name="list_command_jobs",
    annotations={**READ_ONLY, "title": "List Command Jobs"},
)
def list_command_jobs_tool(
    status: list[Literal["queued", "running", "terminating", "completed", "failed", "cancelled", "timed_out"]] | None = None,
    cwd: OptionalRepoPath = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    include_finished: bool = True,
) -> dict:
    """List background command jobs with optional lifecycle and cwd filters."""
    try:
        return list_command_jobs(settings, status=list(status) if status else None, cwd=cwd, limit=limit, include_finished=include_finished)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc)}


@_tool(
    name="cancel_command_job",
    annotations={**WRITE_ACTION, "title": "Cancel Command Job"},
)
def cancel_command_job_tool(job_id: str) -> dict:
    """Use this to cancel a background command job."""
    try:
        return cancel_command_job(job_id=job_id, settings=settings)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc), "job_id": job_id}


if settings.full_access and settings.enable_pty and os.name == "posix":

    @_tool(name="start_terminal_session", annotations={**COMMAND_ACTION, "title": "Start Terminal Session"})
    def start_terminal_session_tool(
        cwd: OptionalRepoPath = None,
        shell: str | None = None,
        command: str | None = None,
        cols: Annotated[int, Field(ge=1, le=1000)] = 120,
        rows: Annotated[int, Field(ge=1, le=1000)] = 40,
        env: dict[str, str] | None = None,
        idle_timeout_ms: Annotated[int, Field(ge=1000, le=86_400_000)] = 1_800_000,
    ) -> dict:
        """Start a persistent interactive POSIX terminal session."""
        try:
            return start_terminal_session(
                settings, cwd=cwd, shell=shell, command=command, cols=cols, rows=rows,
                env=env, idle_timeout_ms=idle_timeout_ms,
            )
        except ResourceBusyError as exc:
            return _resource_busy_result(exc)

    @_tool(name="read_terminal_session", annotations={**READ_ONLY, "title": "Read Terminal Session"})
    def read_terminal_session_tool(
        session_id: str,
        cursor: Annotated[int, Field(ge=0)] = 0,
        max_bytes: Annotated[int, Field(ge=1, le=65536)] = 65536,
        wait_ms: Annotated[int, Field(ge=0, le=30000)] = 1000,
    ) -> dict:
        """Read new terminal output from a byte cursor, optionally waiting for data."""
        return read_terminal_session(session_id, settings, cursor=cursor, max_bytes=max_bytes, wait_ms=wait_ms)

    @_tool(name="write_terminal_session", annotations={**COMMAND_ACTION, "title": "Write Terminal Session"})
    def write_terminal_session_tool(session_id: str, data: str, encoding: Literal["utf8", "base64"] = "utf8") -> dict:
        """Write UTF-8 or base64-decoded control bytes to a terminal session."""
        return write_terminal_session(session_id, data=data, encoding=encoding)

    @_tool(name="resize_terminal_session", annotations={**COMMAND_ACTION, "title": "Resize Terminal Session"})
    def resize_terminal_session_tool(
        session_id: str,
        cols: Annotated[int, Field(ge=1, le=1000)],
        rows: Annotated[int, Field(ge=1, le=1000)],
    ) -> dict:
        """Resize a running terminal session."""
        return resize_terminal_session(session_id, cols=cols, rows=rows)

    @_tool(name="close_terminal_session", annotations={**WRITE_ACTION, "title": "Close Terminal Session"})
    def close_terminal_session_tool(
        session_id: str,
        signal: Literal["SIGTERM", "SIGINT", "SIGHUP", "SIGKILL"] = "SIGTERM",
        grace_ms: Annotated[int, Field(ge=0, le=30000)] = 5000,
        force: bool = False,
    ) -> dict:
        """Close a terminal and its process group, escalating to SIGKILL when needed."""
        return close_terminal_session(session_id, settings, signal_name=signal, grace_ms=grace_ms, force=force)

    @_tool(name="list_terminal_sessions", annotations={**READ_ONLY, "title": "List Terminal Sessions"})
    def list_terminal_sessions_tool(include_finished: bool = True) -> dict:
        """List persistent terminal sessions owned by this server process."""
        return list_terminal_sessions(include_finished=include_finished)


@_tool(
    name="git_commit",
    annotations={**WRITE_ACTION, "title": "Git Commit"},
)
def git_commit_tool(message: str, paths: list[str], dry_run: DryRun = None, repo: RepoScope = None) -> dict:
    """Use this when you need to commit exactly listed files without pushing."""
    try:
        return git_commit(
            message=message,
            paths=paths,
            settings=settings,
            dry_run=settings.effective_dry_run(dry_run),
            repo=repo,
        )
    except GitCommitError as exc:
        return {"ok": False, "error_kind": "git_commit_rejected", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "git_commit_failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Git workflow tools (branch/stage/stash/fetch/pull/push/merge/revert/reset/worktree)
# ---------------------------------------------------------------------------


@_tool(
    name="git_switch_branch",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Switch Branch"},
)
def git_switch_branch_tool(
    branch: Annotated[str, Field(description="Branch name to switch to.")],
    create: Annotated[bool, Field(description="When true, create the branch (like `git switch -c`).")] = False,
    start_point: Annotated[str | None, Field(description="Starting point for a newly created branch. Only valid with create=true.")] = None,
    stash_first: Annotated[bool, Field(description="When true, auto-stash a dirty working tree before switching.")] = False,
    repo: RepoScope = None,
) -> dict:
    """Switch to a branch (or a polyrepo sub-repo via `repo`), refusing to run over a dirty tree unless stash_first=true."""
    return _structural_result(
        git_switch_branch,
        settings,
        branch,
        repo=repo,
        create=create,
        start_point=start_point,
        stash_first=stash_first,
    )


@_tool(
    name="git_create_branch",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Create Branch"},
)
def git_create_branch_tool(
    branch: Annotated[str, Field(description="New branch name; validated via `git check-ref-format`.")],
    start_point: Annotated[str, Field(description="Revision the new branch starts from.")] = "HEAD",
    checkout: Annotated[bool, Field(description="When true, check out the new branch immediately.")] = True,
    repo: RepoScope = None,
) -> dict:
    """Create a new branch (or a polyrepo sub-repo via `repo`) from start_point."""
    return _structural_result(
        git_create_branch,
        settings,
        branch,
        repo=repo,
        start_point=start_point,
        checkout=checkout,
    )


@_tool(
    name="git_add",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Add"},
)
def git_add_tool(
    paths: Annotated[list[str], Field(description="Explicit repo-relative paths to stage. Blanket '.'/'-A'/'--all' is rejected.")],
    dry_run: DryRun = None,
    repo: RepoScope = None,
) -> dict:
    """Stage explicit paths (or a polyrepo sub-repo via `repo`); paths blocked by policy are reported, not staged."""
    return _structural_result(git_add, settings, paths, repo=repo, dry_run=dry_run)


@_tool(
    name="git_restore",
    annotations={**WRITE_ACTION, "title": "Git Restore"},
)
def git_restore_tool(
    paths: Annotated[list[str], Field(description="Repo-relative paths to restore.")],
    staged: Annotated[bool, Field(description="When true, unstage only (always safe). When false, discard working-tree changes (needs confirmed).")] = False,
    source: Annotated[str | None, Field(description="Optional revision to restore file contents from.")] = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Unstage or discard changes for paths (or a polyrepo sub-repo via `repo`); discarding needs confirmed=true."""
    return _structural_result(
        git_restore,
        settings,
        paths,
        repo=repo,
        staged=staged,
        source=source,
        confirmed=confirmed,
    )


@_tool(
    name="git_stash",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Stash"},
)
def git_stash_tool(
    action: Annotated[
        Literal["push", "pop", "apply", "list", "show", "drop"],
        Field(description="Stash subcommand to run."),
    ] = "push",
    message: Annotated[str | None, Field(description="Optional message for action='push'.")] = None,
    include_untracked: Annotated[bool, Field(description="When true, include untracked files in action='push'.")] = True,
    stash_ref: Annotated[str | None, Field(description="Stash reference for apply/pop/drop/show, defaults to stash@{0}.")] = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Run git stash push/pop/apply/list/show/drop (or a polyrepo sub-repo via `repo`); pop/drop need confirmed=true."""
    return _structural_result(
        git_stash,
        settings,
        repo=repo,
        action=action,
        message=message,
        include_untracked=include_untracked,
        stash_ref=stash_ref,
        confirmed=confirmed,
    )


@_tool(
    name="git_fetch",
    annotations={**NETWORK_READ, "title": "Git Fetch"},
)
def git_fetch_tool(
    remote: Annotated[str, Field(description="Remote name to fetch from.")] = "origin",
    prune: Annotated[bool, Field(description="When true, prune remote-tracking refs deleted upstream.")] = False,
    all_repos: Annotated[bool, Field(description="When true, fetch every git repo discovered in the polyrepo workspace.")] = False,
    repo: RepoScope = None,
) -> dict:
    """Fetch a remote (or a polyrepo sub-repo via `repo`), reporting which remote-tracking refs moved."""
    return _structural_result(git_fetch, settings, repo=repo, remote=remote, prune=prune, all_repos=all_repos)


@_tool(
    name="git_pull",
    annotations={**NETWORK_WRITE, "title": "Git Pull"},
)
def git_pull_tool(
    remote: Annotated[str, Field(description="Remote name to pull from.")] = "origin",
    branch: Annotated[str | None, Field(description="Remote branch to pull. Defaults to the current branch's name.")] = None,
    ff_only: Annotated[bool, Field(description="When true (default), only allow a fast-forward pull.")] = True,
    rebase: Annotated[bool, Field(description="When true, rebase local commits onto the pulled branch instead of merging.")] = False,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Pull a remote branch (or a polyrepo sub-repo via `repo`); rebase or non-fast-forward pulls need confirmed=true.

    On conflict, the in-progress merge/rebase is auto-aborted and reported as `error_kind: "pull_conflict"`.
    """
    return _structural_result(
        git_pull,
        settings,
        repo=repo,
        remote=remote,
        branch=branch,
        ff_only=ff_only,
        rebase=rebase,
        confirmed=confirmed,
    )


@_tool(
    name="git_push",
    annotations={**NETWORK_WRITE, "title": "Git Push"},
)
def git_push_tool(
    remote: Annotated[str, Field(description="Remote name to push to.")] = "origin",
    branch: Annotated[str | None, Field(description="Branch to push. Defaults to the current branch; required on a detached HEAD.")] = None,
    set_upstream: Annotated[bool, Field(description="When true, set the pushed branch's upstream tracking.")] = False,
    force_with_lease: Annotated[
        bool,
        Field(description="When true, push with --force-with-lease. Only available when the server enables ALLOW_FORCE_PUSH, and always needs confirmed=true."),
    ] = False,
    dry_run: DryRun = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Push a branch through the audited structural workflow (safe mode routes pushes here).

    Pushing a `settings.protected_branches` branch, a real (non-dry-run) push, or force_with_lease all require
    confirmed=true in safe mode. Full mode also permits raw shell push.
    """
    return _structural_result(
        git_push,
        settings,
        repo=repo,
        remote=remote,
        branch=branch,
        set_upstream=set_upstream,
        force_with_lease=force_with_lease,
        dry_run=dry_run,
        confirmed=confirmed,
    )


@_tool(
    name="git_merge",
    annotations={**WRITE_ACTION, "title": "Git Merge"},
)
def git_merge_tool(
    branch: Annotated[str, Field(description="Branch to merge into HEAD. Ignored when abort=true.")] = "",
    no_ff: Annotated[bool, Field(description="When true, always create a merge commit (no fast-forward).")] = False,
    message: Annotated[str | None, Field(description="Optional custom merge commit message.")] = None,
    abort: Annotated[bool, Field(description="When true, abort an in-progress merge instead of starting one.")] = False,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Merge a branch into HEAD, or abort an in-progress merge (or a polyrepo sub-repo via `repo`); merging needs confirmed=true.

    On conflict, the merge is left unresolved for inspection/fixing, or call again with abort=true.
    """
    return _structural_result(
        git_merge,
        settings,
        branch,
        repo=repo,
        no_ff=no_ff,
        message=message,
        abort=abort,
        confirmed=confirmed,
    )


@_tool(
    name="git_revert",
    annotations={**WRITE_ACTION, "title": "Git Revert"},
)
def git_revert_tool(
    revision: Annotated[str, Field(description="Revision to revert.")],
    no_commit: Annotated[bool, Field(description="When true, apply the revert to the working tree/index without committing.")] = False,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Revert a revision (or a polyrepo sub-repo via `repo`); needs confirmed=true. On conflict, auto-aborts and reports conflicts."""
    return _structural_result(
        git_revert,
        settings,
        revision,
        repo=repo,
        no_commit=no_commit,
        confirmed=confirmed,
    )


@_tool(
    name="git_reset",
    annotations={**WRITE_ACTION, "title": "Git Reset"},
)
def git_reset_tool(
    mode: Annotated[
        Literal["soft", "mixed", "hard"],
        Field(description="Reset mode. Hard additionally requires ALLOW_HARD_RESET=true."),
    ] = "mixed",
    target: Annotated[str, Field(description="Revision to reset HEAD to.")] = "HEAD~1",
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Reset HEAD to a target revision; full mode skips internal confirmation, while hard remains separately gated."""
    return _structural_result(git_reset, settings, repo=repo, mode=mode, target=target, confirmed=confirmed)


@_tool(
    name="git_worktree_add",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Worktree Add"},
)
def git_worktree_add_tool(
    branch: Annotated[str, Field(description="Branch to check out into the new worktree.")],
    base: Annotated[str, Field(description="Revision the new branch is created from, when create_branch=true.")] = "HEAD",
    create_branch: Annotated[bool, Field(description="When true, create `branch` from `base`; when false, `branch` must already exist.")] = True,
    repo: RepoScope = None,
) -> dict:
    """Add a worktree under `.chatrepo-worktrees/` for a branch (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(git_worktree_add, settings, branch, repo=repo, base=base, create_branch=create_branch)


@_tool(
    name="prepare_task_worktree",
    annotations={**SAFE_EDIT_ACTION, "title": "Prepare Task Worktree"},
)
def prepare_task_worktree_tool(
    branch: Annotated[str, Field(description="New task branch to create.")],
    task_name: Annotated[str, Field(description="Stable task name used for the worktree directory.")],
    base: Annotated[str, Field(description="Base revision resolved to an exact commit before creation.")] = "HEAD",
    dry_run: DryRun = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Prepare an isolated branch/worktree from an exact committed base without copying parent changes."""
    return _structural_result(
        prepare_task_worktree, settings, branch=branch, task_name=task_name, base=base,
        dry_run=settings.effective_dry_run(dry_run), confirmed=confirmed, repo=repo,
    )


@_tool(
    name="git_worktree_list",
    annotations={**SAFE_EDIT_ACTION, "title": "Git Worktree List"},
)
def git_worktree_list_tool(repo: RepoScope = None) -> dict:
    """List worktrees registered against the repository (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(git_worktree_list, settings, repo=repo)


@_tool(
    name="git_worktree_remove",
    annotations={**WRITE_ACTION, "title": "Git Worktree Remove"},
)
def git_worktree_remove_tool(
    worktree_path: Annotated[str, Field(description="Path of the worktree to remove (must stay within allowed workspace roots).")],
    force: Annotated[bool, Field(description="When true, remove even if the worktree has local modifications.")] = False,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Remove a worktree (or a polyrepo sub-repo via `repo`); needs confirmed=true."""
    return _structural_result(
        git_worktree_remove,
        settings,
        worktree_path,
        repo=repo,
        force=force,
        confirmed=confirmed,
    )


# ---------------------------------------------------------------------------
# GitHub tools (PRs, checks, runs, issues) via the `gh` CLI
# ---------------------------------------------------------------------------


@_tool(
    name="gh_status",
    annotations={**NETWORK_READ, "title": "GitHub Status"},
)
def gh_status_tool() -> dict:
    """Report whether the `gh` CLI is installed/authenticated, plus a brief API rate-limit snapshot."""
    return _structural_result(gh_status, settings)


@_tool(
    name="gh_pr_create",
    annotations={**NETWORK_WRITE, "title": "GitHub PR Create"},
)
def gh_pr_create_tool(
    title: Annotated[str, Field(description="Pull request title.")],
    body: Annotated[str, Field(description="Pull request body (Markdown).")],
    base: Annotated[str | None, Field(description="Base branch to merge into. Defaults to the repo's default branch.")] = None,
    head: Annotated[str | None, Field(description="Head branch. Defaults to the current branch.")] = None,
    draft: Annotated[bool, Field(description="When true, create the PR as a draft.")] = False,
    dry_run: DryRun = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Create a GitHub pull request for the current branch (or a polyrepo sub-repo via `repo`).

    Requires the current branch to already be pushed (call git_push first). A real (non-dry-run)
    PR creation needs confirmed=true.
    """
    return _structural_result(
        gh_pr_create,
        settings,
        title,
        body,
        repo=repo,
        base=base,
        head=head,
        draft=draft,
        dry_run=dry_run,
        confirmed=confirmed,
    )


@_tool(
    name="gh_pr_list",
    annotations={**NETWORK_READ, "title": "GitHub PR List"},
)
def gh_pr_list_tool(
    state: Annotated[Literal["open", "closed", "merged", "all"], Field(description="PR state filter.")] = "open",
    limit: Annotated[int, Field(description="Maximum number of pull requests to return.")] = 20,
    repo: RepoScope = None,
) -> dict:
    """List pull requests (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(gh_pr_list, settings, repo=repo, state=state, limit=limit)


@_tool(
    name="gh_pr_view",
    annotations={**NETWORK_READ, "title": "GitHub PR View"},
)
def gh_pr_view_tool(
    number: Annotated[int, Field(description="Pull request number.")],
    include_diff: Annotated[bool, Field(description="When true, also fetch the PR's diff.")] = False,
    include_comments: Annotated[bool, Field(description="When true, include PR-level comments.")] = True,
    repo: RepoScope = None,
) -> dict:
    """View pull request metadata, reviews, and optionally its diff (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(
        gh_pr_view,
        settings,
        number,
        repo=repo,
        include_diff=include_diff,
        include_comments=include_comments,
    )


@_tool(
    name="gh_pr_comment",
    annotations={**NETWORK_WRITE, "title": "GitHub PR Comment"},
)
def gh_pr_comment_tool(
    number: Annotated[int, Field(description="Pull request number to comment on.")],
    body: Annotated[str, Field(description="Comment body (Markdown).")],
    reply_to: Annotated[int | None, Field(description="Optional review comment id to reply to instead of posting a top-level PR comment.")] = None,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Post a real, visible comment on a pull request (or a polyrepo sub-repo via `repo`); always needs confirmed=true."""
    return _structural_result(
        gh_pr_comment,
        settings,
        number,
        body,
        repo=repo,
        reply_to=reply_to,
        confirmed=confirmed,
    )


@_tool(
    name="gh_pr_merge",
    annotations={**NETWORK_WRITE, "title": "GitHub PR Merge"},
)
def gh_pr_merge_tool(
    number: Annotated[int, Field(description="Pull request number to merge.")],
    method: Annotated[Literal["merge", "squash", "rebase"], Field(description="Merge strategy.")] = "squash",
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Merge a pull request on GitHub (or a polyrepo sub-repo via `repo`); always needs confirmed=true."""
    return _structural_result(gh_pr_merge, settings, number, repo=repo, method=method, confirmed=confirmed)


@_tool(
    name="gh_checks",
    annotations={**NETWORK_READ, "title": "GitHub Checks"},
)
def gh_checks_tool(
    pr_number: Annotated[int | None, Field(description="Pull request number to list CI checks for.")] = None,
    ref: Annotated[str | None, Field(description="Commit ref to list CI checks for, instead of a PR number.")] = None,
    repo: RepoScope = None,
) -> dict:
    """List CI check runs for a pull request or a commit ref (or a polyrepo sub-repo via `repo`); one of pr_number/ref is required."""
    return _structural_result(gh_checks, settings, repo=repo, pr_number=pr_number, ref=ref)


@_tool(
    name="gh_run_view",
    annotations={**NETWORK_READ, "title": "GitHub Run View"},
)
def gh_run_view_tool(
    run_id: Annotated[str | None, Field(description="Workflow run id. Defaults to the latest run on the current branch.")] = None,
    failed_only: Annotated[bool, Field(description="When true, also fetch the failed-jobs-only log tail.")] = True,
    log_tail: Annotated[int, Field(description="Number of trailing failed-log lines to include.")] = 200,
    repo: RepoScope = None,
) -> dict:
    """View a GitHub Actions workflow run and optionally its failed-job log tail (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(
        gh_run_view,
        settings,
        repo=repo,
        run_id=run_id,
        failed_only=failed_only,
        log_tail=log_tail,
    )


@_tool(
    name="gh_run_rerun",
    annotations={**NETWORK_WRITE, "title": "GitHub Run Rerun"},
)
def gh_run_rerun_tool(
    run_id: Annotated[str, Field(description="Workflow run id to re-trigger.")],
    failed_only: Annotated[bool, Field(description="When true, only rerun failed jobs.")] = True,
    confirmed: Confirmed = False,
    repo: RepoScope = None,
) -> dict:
    """Re-trigger a GitHub Actions workflow run (or a polyrepo sub-repo via `repo`); always needs confirmed=true."""
    return _structural_result(
        gh_run_rerun,
        settings,
        run_id,
        repo=repo,
        failed_only=failed_only,
        confirmed=confirmed,
    )


@_tool(
    name="gh_issue_list",
    annotations={**NETWORK_READ, "title": "GitHub Issue List"},
)
def gh_issue_list_tool(
    state: Annotated[Literal["open", "closed", "all"], Field(description="Issue state filter.")] = "open",
    limit: Annotated[int, Field(description="Maximum number of issues to return.")] = 20,
    repo: RepoScope = None,
) -> dict:
    """List issues (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(gh_issue_list, settings, repo=repo, state=state, limit=limit)


@_tool(
    name="gh_issue_view",
    annotations={**NETWORK_READ, "title": "GitHub Issue View"},
)
def gh_issue_view_tool(
    number: Annotated[int, Field(description="Issue number.")],
    repo: RepoScope = None,
) -> dict:
    """View a single issue's metadata and comments (or a polyrepo sub-repo via `repo`)."""
    return _structural_result(gh_issue_view, settings, number, repo=repo)


# ---------------------------------------------------------------------------
# One-shot diagnostics and symbol index tools
# ---------------------------------------------------------------------------


@_tool(
    name="code_diagnostics",
    annotations={**READ_ONLY, "title": "Code Diagnostics"},
)
def code_diagnostics_tool(
    paths: Annotated[list[str] | None, Field(description="Optional paths to limit the check to, passed through to the underlying checker.")] = None,
    language: Annotated[
        Literal["auto", "go", "python", "ts"],
        Field(description="Language checker to run. 'auto' detects the stack(s) present and runs every applicable runner."),
    ] = "auto",
    severity_min: Annotated[Literal["hint", "info", "warning", "error"], Field(description="Minimum severity to include.")] = "warning",
    limit: Annotated[int, Field(description="Maximum number of diagnostics to return.")] = 200,
    repo: RepoScope = None,
) -> dict:
    """Run one-shot diagnostics (go vet / pyright or ruff / tsc --noEmit) for the workspace or a polyrepo sub-repo via `repo`.

    Missing external checker binaries never fail the call; they are reported in `missing_tools`.
    """
    return _structural_result(
        code_diagnostics,
        settings,
        repo=repo,
        paths=paths,
        language=language,
        severity_min=severity_min,
        limit=limit,
    )


@_tool(
    name="symbol_definition",
    annotations={**READ_ONLY, "title": "Symbol Definition"},
)
def symbol_definition_tool(
    symbol: Annotated[str, Field(description="Exact symbol name to find definitions for.")],
    kind: Annotated[str | None, Field(description="Optional ctags kind filter, e.g. 'function' or 'class'.")] = None,
    limit: Annotated[int, Field(description="Maximum number of definitions to return.")] = 20,
    repo: RepoScope = None,
) -> dict:
    """Find definitions of a symbol (ctags index when available, else a regex heuristic) in the repo or a polyrepo sub-repo via `repo`."""
    return _structural_result(symbol_definition, settings, symbol, repo=repo, kind=kind, limit=limit)


@_tool(
    name="document_symbols",
    annotations={**READ_ONLY, "title": "Document Symbols"},
)
def document_symbols_tool(path: RepoPath, repo: RepoScope = None) -> dict:
    """Return a file's outline (name/kind/line/signature/scope), from ctags when available or a regex heuristic."""
    return _structural_result(document_symbols, settings, path, repo=repo)


@_tool(
    name="workspace_symbols",
    annotations={**READ_ONLY, "title": "Workspace Symbols"},
)
def workspace_symbols_tool(
    query: Annotated[str, Field(description="Substring to search for across indexed symbol names.")],
    limit: Annotated[int, Field(description="Maximum number of matching symbols to return.")] = 50,
    repo: RepoScope = None,
) -> dict:
    """Fuzzy/substring-search symbol names across the repo or a polyrepo sub-repo via `repo` (ctags index or text-search fallback)."""
    return _structural_result(workspace_symbols, settings, query, repo=repo, limit=limit)
