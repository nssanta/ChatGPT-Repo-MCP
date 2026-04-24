from __future__ import annotations

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .command_tools import (
    CommandPolicyError,
    ConfirmationRequiredError,
    GitCommitError,
    cancel_command_job,
    get_command_job,
    git_commit,
    run_command,
    run_commands,
    run_test_preset,
    start_command_job,
)
from .config import Settings
from .edit_tools import (
    append_to_file,
    apply_patch_diff,
    batch_edit_files,
    create_text_file,
    current_text_sha256,
    delete_text_in_file,
    delete_path,
    ensure_directory,
    insert_after_heading,
    insert_after_line,
    insert_before_heading,
    insert_before_line,
    insert_text_in_file,
    move_path,
    replace_text_in_file,
    replace_lines,
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
    git_blame,
    git_branches,
    git_diff,
    git_grep,
    git_log,
    git_show,
    git_status,
    repo_git_info,
)

settings = Settings.from_env()


class StaticBearerVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if settings.mcp_auth_mode != "bearer":
            return AccessToken(token=token, client_id="no-auth", scopes=["repo"], expires_at=None)
        if settings.mcp_bearer_token and token == settings.mcp_bearer_token:
            return AccessToken(token=token, client_id="chatgpt", scopes=["repo"], expires_at=None)
        return None


auth_settings = (
    AuthSettings(
        issuer_url="https://localhost",
        resource_server_url="https://localhost",
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


@mcp.tool(
    name="repo_info",
    annotations={**READ_ONLY, "title": "Repository Info"},
)
def repo_info_tool() -> dict:
    """Return the MCP server configuration relevant to the inspected repository."""
    return _repo_info_with_git()


def _repo_info_with_git() -> dict:
    result = repo_info(settings)
    try:
        result["git"] = repo_git_info(settings)
    except Exception as exc:  # noqa: BLE001
        result["git_error"] = str(exc)
    return result


@mcp.tool(
    name="list_dir",
    annotations={**READ_ONLY, "title": "List Directory"},
)
def list_dir_tool(path: str = ".", include_hidden: bool = True, limit: int = 200) -> dict:
    """List files and directories under a repo-relative path."""
    return list_dir(path=path, settings=settings, include_hidden=include_hidden, limit=limit)


@mcp.tool(
    name="tree",
    annotations={**READ_ONLY, "title": "Tree"},
)
def tree_tool(path: str = ".", depth: int = 4, include_hidden: bool = True) -> dict:
    """Return a textual directory tree for a repo-relative path."""
    return tree(path=path, settings=settings, depth=depth, include_hidden=include_hidden)


@mcp.tool(
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


@mcp.tool(
    name="read_multiple_files",
    annotations={**READ_ONLY, "title": "Read Multiple Files"},
)
def read_multiple_files_tool(paths: list[str]) -> dict:
    """Read several repo files at once. Useful when comparing modules or gathering context."""
    return read_multiple_files(paths=paths, settings=settings)


@mcp.tool(
    name="file_metadata",
    annotations={**READ_ONLY, "title": "File Metadata"},
)
def file_metadata_tool(path: str, include_stat: bool = True) -> dict:
    """Return basic metadata for a repo-relative file or directory."""
    return file_metadata(path=path, settings=settings, include_stat=include_stat)


@mcp.tool(
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


@mcp.tool(
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
) -> dict:
    """Search text across the repository or within a specific file/directory."""
    return search_text(
        query=query,
        settings=settings,
        path=path,
        paths=paths,
        regex=regex,
        case_sensitive=case_sensitive,
        limit=limit,
    )


@mcp.tool(
    name="symbol_search",
    annotations={**READ_ONLY, "title": "Symbol Search"},
)
def symbol_search_tool(symbol: str, path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Heuristically search for declarations or references of a symbol name."""
    return symbol_search(symbol=symbol, settings=settings, path=path, paths=paths, limit=limit)


@mcp.tool(
    name="recent_changes",
    annotations={**READ_ONLY, "title": "Recent Changes"},
)
def recent_changes_tool(path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Return files sorted by recent filesystem modification time."""
    return recent_changes(settings=settings, path=path, paths=paths, limit=limit)


@mcp.tool(
    name="todo_scan",
    annotations={**READ_ONLY, "title": "Todo Scan"},
)
def todo_scan_tool(path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict:
    """Find TODO, FIXME, XXX, and HACK markers across the repository."""
    return todo_scan(settings=settings, path=path, paths=paths, limit=limit)


@mcp.tool(
    name="dependency_map",
    annotations={**READ_ONLY, "title": "Dependency Map"},
)
def dependency_map_tool(path: str = ".") -> dict:
    """Parse common dependency manifest files such as pyproject.toml, package.json, go.mod, and Cargo.toml."""
    return dependency_map(settings=settings, path=path)


@mcp.tool(
    name="git_status",
    annotations={**READ_ONLY, "title": "Git Status"},
)
def git_status_tool(short: bool = True) -> dict:
    """Return the current git status for the repository."""
    return git_status(settings=settings, short=short)


@mcp.tool(
    name="git_diff",
    annotations={**READ_ONLY, "title": "Git Diff"},
)
def git_diff_tool(
    staged: bool = False,
    pathspec: str | None = None,
    context_lines: int = 3,
) -> dict:
    """Return git diff output for the working tree or staged changes."""
    return git_diff(settings=settings, staged=staged, pathspec=pathspec, context_lines=context_lines)


@mcp.tool(
    name="git_log",
    annotations={**READ_ONLY, "title": "Git Log"},
)
def git_log_tool(limit: int = 20, pathspec: str | None = None, since: str | None = None) -> dict:
    """Return recent commit history, optionally filtered by path or since-date."""
    return git_log(settings=settings, limit=limit, pathspec=pathspec, since=since)


@mcp.tool(
    name="git_show",
    annotations={**READ_ONLY, "title": "Git Show"},
)
def git_show_tool(revision: str, path: str | None = None) -> dict:
    """Show a commit object or a file at a given revision."""
    return git_show(settings=settings, revision=revision, path=path)


@mcp.tool(
    name="git_branches",
    annotations={**READ_ONLY, "title": "Git Branches"},
)
def git_branches_tool(all_branches: bool = True) -> dict:
    """List local or all branches with tracking information."""
    return git_branches(settings=settings, all_branches=all_branches)


@mcp.tool(
    name="git_blame",
    annotations={**READ_ONLY, "title": "Git Blame"},
)
def git_blame_tool(path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """Blame a file line range to see who changed it and in which commit."""
    return git_blame(settings=settings, path=path, start_line=start_line, end_line=end_line)


@mcp.tool(
    name="git_grep",
    annotations={**READ_ONLY, "title": "Git Grep"},
)
def git_grep_tool(
    query: str,
    revision: str | None = None,
    pathspec: str | None = None,
    paths: list[str] | None = None,
    case_sensitive: bool = False,
) -> dict:
    """Search tracked content through git grep, optionally at a revision."""
    return git_grep(
        settings=settings,
        query=query,
        revision=revision,
        pathspec=pathspec,
        paths=paths,
        case_sensitive=case_sensitive,
    )


TOOL_NAMES = [
    "dependency_map",
    "file_metadata",
    "find_files",
    "git_blame",
    "git_branches",
    "git_diff",
    "git_grep",
    "git_log",
    "git_show",
    "git_status",
    "list_dir",
    "read_multiple_files",
    "read_text_file",
    "recent_changes",
    "repo_info",
    "search_text",
    "symbol_search",
    "todo_scan",
    "tree",
    "doctor",
    "context_bootstrap",
    "batch_call",
    "smoke_all",
    "write_text_file",
    "replace_text_in_file",
    "insert_text_in_file",
    "delete_text_in_file",
    "create_text_file",
    "move_path",
    "delete_path",
    "ensure_directory",
    "batch_edit_files",
    "replace_lines",
    "insert_before_line",
    "insert_after_line",
    "insert_before_heading",
    "insert_after_heading",
    "append_to_file",
    "apply_patch",
    "update_current_mission",
    "run_command",
    "run_commands",
    "run_test_preset",
    "start_command_job",
    "get_command_job",
    "cancel_command_job",
    "git_commit",
]


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
    }


def _write_result(func, *args, **kwargs) -> dict:
    try:
        return func(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return structured_error(exc)


def _command_result(
    command: str,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
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
        )
    except ConfirmationRequiredError as exc:
        return {"ok": False, "error_kind": "confirmation_required", "reason": str(exc), "command": command}
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "command": command}


@mcp.tool(
    name="doctor",
    annotations={**READ_ONLY, "title": "Doctor"},
)
def doctor_tool() -> dict:
    """Run a compact health check for repository, git, policy, search, and symbol tools."""
    checks = {}
    try:
        checks["repo_info"] = {"ok": True, "result": _repo_info_with_git()}
    except Exception as exc:  # noqa: BLE001
        checks["repo_info"] = {"ok": False, "error": str(exc)}
    for name, args in [
        ("git_status", {"short": True}),
        ("read_text_file", {"path": ".claude/MEMORY.md", "start_line": 1, "end_line": 1}),
        ("read_text_file", {"path": ".env", "start_line": 1, "end_line": 1}),
        ("search_text", {"query": "tts_synthesizing", "path": ".", "limit": 1}),
        ("symbol_search", {"symbol": "tts_synthesizing", "path": ".", "limit": 1}),
    ]:
        key = "blocked_policy" if name == "read_text_file" and args["path"] == ".env" else name
        try:
            result = _batch_dispatch(name, args)
            checks[key] = {"ok": key != "blocked_policy", "result": result}
        except Exception as exc:  # noqa: BLE001
            checks[key] = {"ok": key == "blocked_policy", "error": str(exc)}
    return {
        "project_root": str(settings.project_root),
        **_namespace_info(),
        **_write_config_info(),
        "tools": TOOL_NAMES,
        "tool_count": len(TOOL_NAMES),
        "checks": checks,
    }


@mcp.tool(
    name="smoke_all",
    annotations={**READ_ONLY, "title": "Smoke All"},
)
def smoke_all_tool() -> dict:
    """Run the standard Eva_Ai MCP smoke test in one call."""
    checks = []
    for name, args in [
        ("repo_info", {}),
        ("read_text_file", {"path": ".claude/MEMORY.md", "start_line": 1, "end_line": 1}),
        ("list_dir", {"path": ".", "limit": 300}),
        ("search_text", {"query": "tts_synthesizing", "path": ".", "limit": 3}),
        ("symbol_search", {"symbol": "tts_synthesizing", "path": ".", "limit": 3}),
        ("git_status", {"short": True}),
        ("git_log", {"limit": 3}),
        ("git_show", {"revision": "HEAD"}),
        ("read_text_file", {"path": ".env", "start_line": 1, "end_line": 1}),
        ("replace_text_in_file", {"path": ".claude/MEMORY.md", "find": "\n", "replace": "\n", "dry_run": True}),
        ("insert_before_heading", {"path": "missions/CURRENT.md", "heading": "## Goal", "content": "\n", "dry_run": True}),
        ("batch_edit_files", {"operations": [{"op": "ensure_directory", "path": "reports/mcp-smoke"}], "dry_run": True}),
        ("run_command", {"command": "git diff --check"}),
        ("run_command", {"command": "npm --version"}),
    ]:
        key = "blocked_policy" if name == "read_text_file" and args["path"] == ".env" else name
        if name == "replace_text_in_file":
            key = "write_dry_run"
            try:
                args["expected_sha256"] = current_text_sha256(".claude/MEMORY.md", settings)
            except Exception:
                pass
        if name == "batch_edit_files":
            key = "batch_write_dry_run"
        if name == "insert_before_heading":
            key = "heading_write_dry_run"
            try:
                args["expected_sha256"] = current_text_sha256("missions/CURRENT.md", settings)
            except Exception:
                pass
        if name == "run_command":
            key = "run_command_npm_visibility" if args["command"] == "npm --version" else "run_command_git_diff_check"
        try:
            result = _command_result(**args) if name == "run_command" else _batch_dispatch(name, args)
            ok = key != "blocked_policy"
            item = {"name": key, "tool": name, "ok": ok}
            if name == "list_dir":
                names = [entry["name"] for entry in result.get("entries", [])]
                item["blocked_visible"] = [
                    value for value in [".env", ".git", "node_modules", ".venv"] if value in names
                ]
                item["ok"] = not item["blocked_visible"]
            elif name in {"search_text", "symbol_search", "git_log"}:
                item["count"] = result.get("count")
            elif name == "repo_info":
                item["project_root"] = result.get("project_root")
                item["git_error"] = result.get("git_error")
                item["ok"] = not result.get("git_error")
            checks.append(item)
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": key, "tool": name, "ok": key == "blocked_policy", "error": str(exc)})
    return {
        **_namespace_info(),
        **_write_config_info(),
        "project_root": str(settings.project_root),
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
    }


@mcp.tool(
    name="context_bootstrap",
    annotations={**READ_ONLY, "title": "Context Bootstrap"},
)
def context_bootstrap_tool() -> dict:
    """Read the repository's standard context files in one call."""
    paths = [".claude/MEMORY.md", "missions/CURRENT.md", "AGENTS.md", "missions/BACKLOG.md"]
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
    return {"files": files, "count": len(files)}


@mcp.tool(
    name="batch_call",
    annotations={**READ_ONLY, "title": "Batch Call"},
)
def batch_call_tool(calls: list[dict]) -> dict:
    """Run up to 10 read-only tool calls in one request."""
    if len(calls) > 10:
        raise ValueError("too many calls; max is 10")
    results = []
    for call in calls:
        tool = call.get("tool")
        args = call.get("args") or {}
        if not isinstance(tool, str) or not isinstance(args, dict):
            results.append({"tool": tool, "ok": False, "error": "call must contain string tool and object args"})
            continue
        try:
            results.append({"tool": tool, "ok": True, "result": _batch_dispatch(tool, args)})
        except Exception as exc:  # noqa: BLE001
            results.append({"tool": tool, "ok": False, "error": str(exc)})
    return {"results": results, "count": len(results)}


@mcp.tool(
    name="write_text_file",
    annotations={**WRITE_ACTION, "title": "Write Text File"},
)
def write_text_file_tool(
    path: str,
    content: str,
    create_if_missing: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="replace_text_in_file",
    annotations={**WRITE_ACTION, "title": "Replace Text In File"},
)
def replace_text_in_file_tool(
    path: str,
    find: str,
    replace: str,
    replace_all: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="insert_text_in_file",
    annotations={**WRITE_ACTION, "title": "Insert Text In File"},
)
def insert_text_in_file_tool(
    path: str,
    anchor: str,
    position: str,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="delete_text_in_file",
    annotations={**WRITE_ACTION, "title": "Delete Text In File"},
)
def delete_text_in_file_tool(
    path: str,
    find: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="create_text_file",
    annotations={**WRITE_ACTION, "title": "Create Text File"},
)
def create_text_file_tool(
    path: str,
    content: str,
    overwrite: bool = False,
    dry_run: bool = True,
) -> dict:
    """Use this when you need to create a new UTF-8 text file in the repository."""
    return _write_result(create_text_file, path=path, content=content, settings=settings, overwrite=overwrite, dry_run=dry_run)


@mcp.tool(
    name="move_path",
    annotations={**WRITE_ACTION, "title": "Move Path"},
)
def move_path_tool(
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="delete_path",
    annotations={**WRITE_ACTION, "title": "Delete Path"},
)
def delete_path_tool(
    path: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Use this when you need to delete an allowed UTF-8 repo file."""
    return _write_result(delete_path, path=path, settings=settings, expected_sha256=expected_sha256, dry_run=dry_run)


@mcp.tool(
    name="ensure_directory",
    annotations={**WRITE_ACTION, "title": "Ensure Directory"},
)
def ensure_directory_tool(path: str, dry_run: bool = True) -> dict:
    """Use this when you need to create a directory for docs, reports, packets, or source files."""
    return _write_result(ensure_directory, path=path, settings=settings, dry_run=dry_run)


@mcp.tool(
    name="batch_edit_files",
    annotations={**WRITE_ACTION, "title": "Batch Edit Files"},
)
def batch_edit_files_tool(
    operations: list[dict],
    atomic: bool = True,
    dry_run: bool = True,
) -> dict:
    """Use this when several related repo edits must be previewed or applied together."""
    return _write_result(batch_edit_files, operations=operations, settings=settings, atomic=atomic, dry_run=dry_run)


@mcp.tool(
    name="replace_lines",
    annotations={**WRITE_ACTION, "title": "Replace Lines"},
)
def replace_lines_tool(
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="insert_before_line",
    annotations={**WRITE_ACTION, "title": "Insert Before Line"},
)
def insert_before_line_tool(
    path: str,
    line: int,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="insert_after_line",
    annotations={**WRITE_ACTION, "title": "Insert After Line"},
)
def insert_after_line_tool(
    path: str,
    line: int,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="insert_before_heading",
    annotations={**WRITE_ACTION, "title": "Insert Before Heading"},
)
def insert_before_heading_tool(
    path: str,
    heading: str,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="insert_after_heading",
    annotations={**WRITE_ACTION, "title": "Insert After Heading"},
)
def insert_after_heading_tool(
    path: str,
    heading: str,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="append_to_file",
    annotations={**WRITE_ACTION, "title": "Append To File"},
)
def append_to_file_tool(
    path: str,
    content: str,
    expected_sha256: str | None = None,
    dry_run: bool = True,
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


@mcp.tool(
    name="apply_patch",
    annotations={**WRITE_ACTION, "title": "Apply Patch"},
)
def apply_patch_tool(
    patch: str,
    dry_run: bool = True,
    expected_base_sha: str | None = None,
) -> dict:
    """Use this when you need to apply a unified diff patch across one or more allowed repo files."""
    return _write_result(apply_patch_diff, patch=patch, settings=settings, dry_run=dry_run, expected_base_sha=expected_base_sha)


@mcp.tool(
    name="update_current_mission",
    annotations={**WRITE_ACTION, "title": "Update Current Mission"},
)
def update_current_mission_tool(
    section_title: str | None = None,
    content: str | None = None,
    position: str = "before_goal",
    preset: str | None = None,
    chunks: list[str] | None = None,
    dry_run: bool = True,
) -> dict:
    """Use this when you need to add a mission section to missions/CURRENT.md before ## Goal."""
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


@mcp.tool(
    name="run_command",
    annotations={**WRITE_ACTION, "title": "Run Command"},
)
def run_command_tool(
    command: str,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    max_output_chars: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
) -> dict:
    """Use this when you need to run an allowlisted validation command and report exit code."""
    return _command_result(
        command=command,
        timeout_ms=timeout_ms,
        cwd=cwd,
        env=env,
        max_output_chars=max_output_chars,
        tail_lines=tail_lines,
        confirmed=confirmed,
    )


@mcp.tool(
    name="run_commands",
    annotations={**WRITE_ACTION, "title": "Run Commands"},
)
def run_commands_tool(
    commands: list[str],
    stop_on_failure: bool = False,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
) -> dict:
    """Use this when you need to run several allowlisted validation commands and compare exit codes."""
    return run_commands(
        commands=commands,
        settings=settings,
        stop_on_failure=stop_on_failure,
        timeout_ms=timeout_ms,
        tail_lines=tail_lines,
        confirmed=confirmed,
    )


@mcp.tool(
    name="run_test_preset",
    annotations={**WRITE_ACTION, "title": "Run Test Preset"},
)
def run_test_preset_tool(
    preset: str,
    timeout_ms: int | None = None,
    tail_lines: int | None = 200,
    background: bool = False,
) -> dict:
    """Use this when you need to run a named test preset without sending a long command string."""
    try:
        return run_test_preset(
            preset=preset,
            settings=settings,
            timeout_ms=timeout_ms,
            tail_lines=tail_lines,
            background=background,
        )
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "preset": preset}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "preset": preset}


@mcp.tool(
    name="start_command_job",
    annotations={**WRITE_ACTION, "title": "Start Command Job"},
)
def start_command_job_tool(
    command: str,
    timeout_ms: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    tail_lines: int | None = 200,
    confirmed: bool = False,
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
        )
    except ConfirmationRequiredError as exc:
        return {"ok": False, "error_kind": "confirmation_required", "reason": str(exc), "command": command}
    except CommandPolicyError as exc:
        return {"ok": False, "error_kind": "command_not_allowed", "error": str(exc), "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "command_failed", "error": str(exc), "command": command}


@mcp.tool(
    name="get_command_job",
    annotations={**READ_ONLY, "title": "Get Command Job"},
)
def get_command_job_tool(job_id: str, tail_lines: int | None = 200) -> dict:
    """Use this to poll a background command job and read output tails."""
    try:
        return get_command_job(job_id=job_id, settings=settings, tail_lines=tail_lines)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc), "job_id": job_id}


@mcp.tool(
    name="cancel_command_job",
    annotations={**WRITE_ACTION, "title": "Cancel Command Job"},
)
def cancel_command_job_tool(job_id: str) -> dict:
    """Use this to cancel a background command job."""
    try:
        return cancel_command_job(job_id=job_id, settings=settings)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "job_error", "error": str(exc), "job_id": job_id}


@mcp.tool(
    name="git_commit",
    annotations={**WRITE_ACTION, "title": "Git Commit"},
)
def git_commit_tool(message: str, paths: list[str], dry_run: bool = True) -> dict:
    """Use this when you need to commit exactly listed files without pushing."""
    try:
        return git_commit(message=message, paths=paths, settings=settings, dry_run=dry_run)
    except GitCommitError as exc:
        return {"ok": False, "error_kind": "git_commit_rejected", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error_kind": "git_commit_failed", "error": str(exc)}
