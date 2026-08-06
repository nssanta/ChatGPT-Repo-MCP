"""GitHub PR/CI tools built on top of the ``gh`` CLI (Phase 5).

``gh`` is an external binary, not a pip dependency: every public function
here degrades gracefully to a structured ``{"ok": False, "error_kind": ...}``
dict when ``gh`` is missing, unauthenticated, or the target repo has no
GitHub remote -- it never raises for those cases. The only exception that is
allowed to propagate is ``ConfirmationRequiredError`` (from
``command_tools``), raised by destructive/write operations that were called
without ``confirmed=True`` -- exactly like the rest of the MCP tool surface.

``cwd`` for every ``gh`` invocation is the git toplevel resolved via
``git_tools._resolve_repo_toplevel`` so ``gh`` can infer owner/repo from the
remote itself; this is what makes the tools polyrepo-friendly without any
extra plumbing. All subprocess output is passed through
``command_tools._redact`` before being returned or logged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from . import git_tools
from .bounded_subprocess import run_bounded
from .command_tools import ConfirmationRequiredError, _audit, _redact
from .config import Settings
from .output_store import inline_head_tail
from .resource_profile import acquire_heavy_operation
from .runtime_env import command_environment, resolve_binary

#: Shown whenever ``gh`` is missing so the caller (human or agent) knows how
#: to unblock itself.
GH_INSTALL_HINT = "sudo apt install gh && gh auth login"

#: Short, fixed timeout for the availability probe itself (``gh --version`` /
#: ``gh auth status``). Intentionally independent of ``settings.gh_timeout``,
#: which governs the actual data-fetching/mutating commands.
_PROBE_TIMEOUT = 10

#: Substrings ``gh`` prints (to stdout/stderr) when it cannot resolve a
#: GitHub repository from the current directory's git remotes. Matched
#: case-insensitively against the combined, redacted output of a failed
#: ``gh`` invocation.
_NO_REMOTE_MARKERS = (
    "no git remotes found",
    "unable to determine",
    "could not determine",
    "not a git repository",
    "failed to determine",
)

#: Substrings indicating the current `gh auth` session is missing/invalid.
_NOT_AUTHENTICATED_MARKERS = (
    "gh auth login",
    "authentication required",
    "not logged in",
    "http 401",
)


def _disabled() -> dict[str, Any]:
    return {"ok": False, "error_kind": "github_tools_disabled"}


def _guard(settings: Settings) -> dict[str, Any] | None:
    """Return the standard disabled-error dict, or ``None`` if github tools are enabled."""
    if not settings.github_tools_enabled:
        return _disabled()
    return None


def _require_gh_ready(settings: Settings | None = None) -> dict[str, Any] | None:
    """Return a structural error dict if ``gh`` is missing/unauthenticated, else ``None``.

    Used by write/destructive operations to check availability *before*
    demanding ``confirmed=True``: there is no point asking an agent to
    confirm a destructive action that cannot possibly run yet because ``gh``
    isn't installed or authenticated.
    """
    availability = _gh_available(settings)
    if not availability["installed"]:
        return {"ok": False, "error_kind": "gh_unavailable", "install_hint": GH_INSTALL_HINT}
    if not availability["authenticated"]:
        return {"ok": False, "error_kind": "gh_not_authenticated", "install_hint": "run `gh auth login` to authenticate"}
    return None


def _gh_available(settings: Settings | None = None) -> dict[str, Any]:
    """Probe whether ``gh`` is installed and authenticated.

    Returns ``{"installed": bool, "authenticated": bool, "version": str|None,
    "hint": str}``. Never raises -- any subprocess error is treated as "not
    installed"/"not authenticated".
    """
    path = shutil.which("gh") if settings is None else resolve_binary("gh", settings)[0]
    if not path:
        return {
            "installed": False,
            "authenticated": False,
            "version": None,
            "hint": GH_INSTALL_HINT,
            "path": None,
        }

    version: str | None = None
    command_binary = "gh" if settings is None else path
    try:
        proc = run_bounded(
            [command_binary, "--version"],
            env=_gh_env(settings) if settings is not None else None,
            timeout=_PROBE_TIMEOUT,
            max_stdout_bytes=4_096,
            max_stderr_bytes=4_096,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            version = proc.stdout.strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError):
        version = None

    authenticated = False
    try:
        auth_proc = run_bounded(
            [command_binary, "auth", "status"],
            env=_gh_env(settings) if settings is not None else None,
            timeout=_PROBE_TIMEOUT,
            max_stdout_bytes=4_096,
            max_stderr_bytes=4_096,
        )
        authenticated = auth_proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        authenticated = False

    hint = "" if authenticated else "run `gh auth login` to authenticate"
    return {
        "installed": True,
        "authenticated": authenticated,
        "version": version,
        "hint": hint,
        "path": path,
    }


def _gh_env(settings: Settings) -> dict[str, str]:
    if not hasattr(settings, "mcp_extra_path"):
        env = os.environ.copy()
        env.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})
        return env
    return command_environment(settings, {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})


def _run_gh(
    args: list[str],
    settings: Settings,
    *,
    repo: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run ``gh <args>`` with ``cwd`` set to the resolved git toplevel.

    Always returns a structured dict, never raises:
    - ``gh`` missing            -> ``{"ok": False, "error_kind": "gh_unavailable", "install_hint": ...}``
    - path not inside a repo    -> ``{"ok": False, "error_kind": "no_github_remote", ...}``
    - ``gh`` cannot find remote -> ``{"ok": False, "error_kind": "no_github_remote", ...}``
    - not authenticated         -> ``{"ok": False, "error_kind": "gh_not_authenticated", ...}``
    - timeout                   -> ``{"ok": False, "error_kind": "gh_timeout", ...}``
    - other non-zero exit       -> ``{"ok": False, "error_kind": "gh_command_failed", ...}``
    - success                   -> ``{"ok": True, "stdout": ..., "stderr": ..., "exit_code": 0}``
    """
    availability = _gh_available(settings)
    if not availability["installed"]:
        return {"ok": False, "error_kind": "gh_unavailable", "install_hint": GH_INSTALL_HINT}

    try:
        toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    except git_tools.GitToolError as exc:
        return {"ok": False, "error_kind": "no_github_remote", "error": _redact(str(exc))}

    binary = availability.get("path") or (
        resolve_binary("gh", settings)[0] if hasattr(settings, "mcp_extra_path") else "gh"
    )
    if binary is None:
        return {"ok": False, "error_kind": "gh_unavailable", "install_hint": GH_INSTALL_HINT}
    inline_limit = min(getattr(settings, "default_inline_output_bytes", 65_536), settings.max_diff_bytes)
    cmd = [binary, *args]
    try:
        lease = acquire_heavy_operation(settings)
        try:
            proc = run_bounded(
                cmd,
                cwd=str(toplevel),
                env=_gh_env(settings),
                timeout=timeout or settings.gh_timeout,
                max_stdout_bytes=inline_limit,
                max_stderr_bytes=inline_limit,
                max_combined_bytes=inline_limit,
                artifact_settings=settings,
            )
        finally:
            lease.release()
    except subprocess.TimeoutExpired as exc:
        partial = getattr(exc, "result", None)
        timeout_result: dict[str, Any] = {
            "ok": False,
            "error_kind": "gh_timeout",
            "timeout": timeout or settings.gh_timeout,
        }
        if partial is not None:
            artifact = getattr(partial, "artifact", None)
            timeout_result.update({
                "stdout": _redact(partial.stdout),
                "stderr": _redact(partial.stderr),
                "exit_code": None,
                "output_truncated": True,
            })
            if artifact is not None:
                receipt = artifact["receipt"]
                receipt["configured"]["inline_output_bytes"] = getattr(settings, "default_inline_output_bytes", 65_536)
                receipt["applied"]["inline_output_bytes"] = inline_limit
                receipt["returned"] = {
                    "stdout_bytes": len(partial.stdout.encode("utf-8")),
                    "stderr_bytes": len(partial.stderr.encode("utf-8")),
                }
                receipt["total"] = {
                    "stdout_bytes": partial.stdout_bytes,
                    "stderr_bytes": partial.stderr_bytes,
                }
                timeout_result.update({
                    "artifact": artifact,
                    "continuation": artifact["continuation"],
                    "receipt": receipt,
                })
        return timeout_result
    except OSError as exc:
        return {"ok": False, "error_kind": "gh_unavailable", "install_hint": GH_INSTALL_HINT, "error": _redact(str(exc))}

    stdout_total = getattr(proc, "stdout_bytes", len(proc.stdout.encode("utf-8")))
    stderr_total = getattr(proc, "stderr_bytes", len(proc.stderr.encode("utf-8")))
    stdout = _redact(proc.stdout)
    stderr = _redact(proc.stderr)
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > inline_limit:
        stdout_limit = inline_limit
        stderr_limit = inline_limit
        if stdout_total and stderr_total:
            stdout_limit = (inline_limit + 1) // 2
            stderr_limit = inline_limit - stdout_limit
        stdout = inline_head_tail(stdout, stdout, total_bytes=stdout_total, maximum=stdout_limit)
        stderr = inline_head_tail(stderr, stderr, total_bytes=stderr_total, maximum=stderr_limit)
    artifact = getattr(proc, "artifact", None)
    output_truncated = (
        bool(getattr(proc, "truncated", False))
        or stdout_total > len(stdout.encode("utf-8"))
        or stderr_total > len(stderr.encode("utf-8"))
    )
    response: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "output_truncated": output_truncated,
    }
    if artifact is not None:
        receipt = artifact["receipt"]
        if output_truncated:
            artifact.update({
                "has_more": True,
                "eof": False,
                "continuation": {
                    "tool": "read_artifact",
                    "arguments": {"artifact_id": artifact["artifact_id"]},
                },
            })
            receipt.update({
                "status": "partial", "completeness": "partial", "reason": "inline_limit",
            })
        receipt["configured"]["inline_output_bytes"] = getattr(settings, "default_inline_output_bytes", 65_536)
        receipt["applied"]["inline_output_bytes"] = inline_limit
        receipt["returned"] = {
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        }
        receipt["total"] = {
            "stdout_bytes": stdout_total,
            "stderr_bytes": stderr_total,
        }
        response.update({"artifact": artifact, "continuation": artifact["continuation"], "receipt": receipt})
    if proc.returncode != 0:
        combined = f"{stderr}\n{stdout}".lower()
        if any(marker in combined for marker in _NO_REMOTE_MARKERS):
            return {"ok": False, "error_kind": "no_github_remote", **response}
        if any(marker in combined for marker in _NOT_AUTHENTICATED_MARKERS):
            return {
                "ok": False,
                "error_kind": "gh_not_authenticated",
                "install_hint": "run `gh auth login` to authenticate",
                **response,
            }
        return {"ok": False, "error_kind": "gh_command_failed", **response}

    return {"ok": True, **response}


def _json_or_error(gh_result: dict[str, Any], *, default: Any) -> tuple[Any, dict[str, Any] | None]:
    """Parse ``gh_result["stdout"]`` as JSON, or return a structural error dict."""
    if gh_result.get("output_truncated"):
        evidence: dict[str, Any] = {
            key: gh_result[key]
            for key in ("artifact", "continuation", "receipt")
            if key in gh_result
        }
        return default, {
            "ok": False,
            "error_kind": "gh_output_truncated",
            "output_truncated": True,
            "stdout": gh_result.get("stdout"),
            **evidence,
        }
    try:
        return json.loads(gh_result.get("stdout") or json.dumps(default)), None
    except json.JSONDecodeError:
        return default, {"ok": False, "error_kind": "gh_bad_output", "stdout": gh_result.get("stdout")}


def _audit_gh(settings: Settings, tool: str, *, repo: str | None, ok: bool, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "timestamp": int(time.time()),
        "tool": f"github:{tool}",
        "repo": repo or "",
        "ok": ok,
    }
    for key, value in extra.items():
        payload[key] = _redact(value) if isinstance(value, str) else value
    _audit(settings, payload)


def _repo_owner_name(settings: Settings, repo: str | None) -> tuple[str, str] | None:
    """Resolve ``(owner, name)`` for ``repo`` via ``gh repo view``, or ``None`` on failure."""
    gh_result = _run_gh(["repo", "view", "--json", "owner,name"], settings, repo=repo)
    if not gh_result.get("ok"):
        return None
    data, _err = _json_or_error(gh_result, default={})
    owner = (data or {}).get("owner", {}).get("login") if isinstance(data, dict) else None
    name = (data or {}).get("name") if isinstance(data, dict) else None
    if owner and name:
        return str(owner), str(name)
    return None


def _branch_push_status(
    settings: Settings,
    repo: str | None,
    branch: str | None = None,
) -> dict[str, Any]:
    """Check whether the selected branch has an upstream and is not ahead of it."""
    toplevel = git_tools._resolve_repo_toplevel(repo, settings)
    branch = branch or git_tools._run_git(["branch", "--show-current"], settings, cwd=toplevel).strip()
    branch_ref = branch or "HEAD"
    try:
        upstream = git_tools._run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch_ref}@{{u}}"],
            settings,
            cwd=toplevel,
        ).strip()
    except git_tools.GitToolError:
        return {"pushed": False, "branch": branch, "upstream": None, "ahead": None, "reason": "no upstream configured for current branch"}

    try:
        counts = git_tools._run_git(
            ["rev-list", "--left-right", "--count", f"{upstream}...{branch_ref}"],
            settings,
            cwd=toplevel,
        ).strip()
        _behind_str, ahead_str = counts.split()
        ahead = int(ahead_str)
    except (git_tools.GitToolError, ValueError):
        return {"pushed": False, "branch": branch, "upstream": upstream, "ahead": None, "reason": "could not compare branch with upstream"}

    return {
        "pushed": ahead == 0,
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "reason": None if ahead == 0 else f"{ahead} commit(s) not pushed to {upstream}",
    }


def gh_status(settings: Settings) -> dict[str, Any]:
    """Report ``gh`` install/auth status plus a brief API rate-limit snapshot when authenticated."""
    guard = _guard(settings)
    if guard:
        return guard

    availability = _gh_available(settings)
    result: dict[str, Any] = {"ok": availability["installed"] and availability["authenticated"], **availability}
    if not availability["installed"]:
        result["error_kind"] = "gh_unavailable"
        result["install_hint"] = GH_INSTALL_HINT
        return result
    if not availability["authenticated"]:
        result["error_kind"] = "gh_not_authenticated"
        result["install_hint"] = "run `gh auth login` to authenticate"
        return result

    rate_result = _run_gh(["api", "rate_limit"], settings, timeout=min(settings.gh_timeout, 15))
    if rate_result.get("ok"):
        data, _err = _json_or_error(rate_result, default={})
        core = (data or {}).get("resources", {}).get("core", {}) if isinstance(data, dict) else {}
        if core:
            result["rate_limit"] = {
                "limit": core.get("limit"),
                "remaining": core.get("remaining"),
                "reset": core.get("reset"),
            }
    return result


def gh_pr_create(
    settings: Settings,
    title: str,
    body: str,
    *,
    repo: str | None = None,
    base: str | None = None,
    head: str | None = None,
    draft: bool = False,
    dry_run: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Create a GitHub pull request for the current (or given) repo/branch.

    Pre-check: the current branch must have an upstream and be fully pushed
    (``ahead == 0``); otherwise returns ``error_kind: "branch_not_pushed"``
    hinting to call ``git_push`` first. ``dry_run=True`` (default) only
    previews the ``gh pr create`` invocation; the real, network-mutating
    call additionally requires ``confirmed=True`` (raises
    ``ConfirmationRequiredError`` otherwise).
    """
    guard = _guard(settings)
    if guard:
        return guard

    not_ready = _require_gh_ready(settings)
    if not_ready:
        return not_ready

    try:
        push_status = _branch_push_status(settings, repo, head)
    except git_tools.GitToolError as exc:
        return {"ok": False, "error_kind": "no_github_remote", "error": _redact(str(exc))}
    if not push_status["pushed"]:
        return {"ok": False, "error_kind": "branch_not_pushed", "hint": "call git_push first", **push_status}

    args = ["pr", "create", "--title", title, "--body", body]
    if base:
        args += ["--base", base]
    if head:
        args += ["--head", head]
    if draft:
        args.append("--draft")

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_run": " ".join(["gh", *args]),
            "title": title,
            "body": body,
            "base": base,
            "head": head or push_status["branch"],
            "draft": draft,
        }

    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            "gh_pr_create with dry_run=false opens a real pull request on GitHub and requires "
            "confirmed=true after explicit owner confirmation."
        )

    gh_result = _run_gh(args, settings, repo=repo)
    ok = bool(gh_result.get("ok"))
    _audit_gh(settings, "gh_pr_create", repo=repo, ok=ok, title=title, base=base, head=head or push_status["branch"])
    if not ok:
        return gh_result

    output = (gh_result.get("stdout") or "").strip()
    url = output.splitlines()[-1] if output else ""
    number: int | None = None
    if url:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            number = int(tail)
    return {"ok": True, "url": url, "number": number}


def gh_pr_list(
    settings: Settings,
    *,
    repo: str | None = None,
    state: str = "open",
    limit: int = 20,
) -> dict[str, Any]:
    """List pull requests via ``gh pr list --json ...``."""
    guard = _guard(settings)
    if guard:
        return guard

    args = [
        "pr",
        "list",
        "--json",
        "number,title,headRefName,author,statusCheckRollup,url",
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    gh_result = _run_gh(args, settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    prs, err = _json_or_error(gh_result, default=[])
    if err:
        return err
    return {"ok": True, "prs": prs, "count": len(prs)}


def gh_pr_view(
    settings: Settings,
    number: int,
    *,
    repo: str | None = None,
    include_diff: bool = False,
    include_comments: bool = True,
) -> dict[str, Any]:
    """View PR metadata (and optionally its diff / review comment threads)."""
    guard = _guard(settings)
    if guard:
        return guard

    fields = "number,title,body,state,url,headRefName,baseRefName,author,mergeable,statusCheckRollup,reviews"
    if include_comments:
        fields += ",comments"
    gh_result = _run_gh(["pr", "view", str(number), "--json", fields], settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    data, err = _json_or_error(gh_result, default={})
    if err:
        return err

    result: dict[str, Any] = {"ok": True, "pr": data}
    if include_diff:
        diff_result = _run_gh(["pr", "diff", str(number)], settings, repo=repo)
        if diff_result.get("ok"):
            result["diff"] = diff_result.get("stdout") or ""
            if diff_result.get("artifact") is not None:
                result["diff_artifact"] = diff_result["artifact"]
                result["diff_receipt"] = diff_result["receipt"]
                if diff_result.get("output_truncated"):
                    result["diff_continuation"] = diff_result.get("continuation")
        else:
            result["diff_error"] = diff_result
    return result


def gh_pr_comment(
    settings: Settings,
    number: int,
    body: str,
    *,
    repo: str | None = None,
    reply_to: int | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Post a PR-level comment, or reply to a specific review comment.

    Always requires ``confirmed=True`` (raises ``ConfirmationRequiredError``
    otherwise): this posts real, visible content to GitHub. Without
    ``reply_to`` this is a plain ``gh pr comment``; with ``reply_to`` it goes
    through ``gh api .../pulls/{number}/comments/{reply_to}/replies`` since
    the ``gh`` CLI has no dedicated "reply to review comment" subcommand.
    """
    guard = _guard(settings)
    if guard:
        return guard
    not_ready = _require_gh_ready(settings)
    if not_ready:
        return not_ready
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            "gh_pr_comment posts a real comment to GitHub and requires confirmed=true "
            "after explicit owner confirmation."
        )

    if reply_to is None:
        gh_result = _run_gh(["pr", "comment", str(number), "--body", body], settings, repo=repo)
        ok = bool(gh_result.get("ok"))
        _audit_gh(settings, "gh_pr_comment", repo=repo, ok=ok, number=number, reply_to=None)
        if not ok:
            return gh_result
        output = (gh_result.get("stdout") or "").strip()
        url = output.splitlines()[-1] if output else None
        return {"ok": True, "url": url}

    owner_name = _repo_owner_name(settings, repo)
    if owner_name is None:
        return {"ok": False, "error_kind": "no_github_remote"}
    owner, name = owner_name
    api_path = f"repos/{owner}/{name}/pulls/{number}/comments/{reply_to}/replies"
    gh_result = _run_gh(["api", api_path, "-f", f"body={body}"], settings, repo=repo)
    ok = bool(gh_result.get("ok"))
    _audit_gh(settings, "gh_pr_comment", repo=repo, ok=ok, number=number, reply_to=reply_to)
    if not ok:
        return gh_result
    data, err = _json_or_error(gh_result, default={})
    if err:
        return {"ok": True, "url": None}
    return {"ok": True, "url": (data or {}).get("html_url")}


def gh_pr_merge(
    settings: Settings,
    number: int,
    *,
    repo: str | None = None,
    method: str = "squash",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Merge a pull request. Always requires ``confirmed=True``."""
    guard = _guard(settings)
    if guard:
        return guard
    if method not in {"merge", "squash", "rebase"}:
        return {"ok": False, "error_kind": "invalid_method", "error": f"method must be one of merge, squash, rebase (got {method!r})"}
    not_ready = _require_gh_ready(settings)
    if not_ready:
        return not_ready
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            "gh_pr_merge merges a real pull request on GitHub and requires confirmed=true "
            "after explicit owner confirmation."
        )

    gh_result = _run_gh(["pr", "merge", str(number), f"--{method}"], settings, repo=repo)
    ok = bool(gh_result.get("ok"))
    _audit_gh(settings, "gh_pr_merge", repo=repo, ok=ok, number=number, method=method)
    if not ok:
        return gh_result
    return {"ok": True, "merged": True, "stdout": gh_result.get("stdout")}


def gh_checks(
    settings: Settings,
    *,
    repo: str | None = None,
    pr_number: int | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """List CI check runs for a PR (``pr_number``) or an arbitrary commit ``ref``."""
    guard = _guard(settings)
    if guard:
        return guard
    if pr_number is None and ref is None:
        return {"ok": False, "error_kind": "missing_argument", "error": "either pr_number or ref must be given"}

    if pr_number is not None:
        gh_result = _run_gh(["pr", "checks", str(pr_number), "--json", "name,state,bucket,link"], settings, repo=repo)
        if not gh_result.get("ok"):
            # Older `gh` versions do not support `--json` on `pr checks`.
            fallback = _run_gh(["pr", "checks", str(pr_number)], settings, repo=repo)
            if not fallback.get("ok"):
                return fallback
            return {"ok": True, "checks": [], "raw": fallback.get("stdout")}
        data, err = _json_or_error(gh_result, default=[])
        if err:
            return {"ok": True, "checks": [], "raw": gh_result.get("stdout")}
        checks = [
            {
                "name": item.get("name"),
                "status": item.get("state"),
                "conclusion": item.get("bucket"),
                "url": item.get("link"),
            }
            for item in (data or [])
        ]
        return {"ok": True, "checks": checks}

    owner_name = _repo_owner_name(settings, repo)
    if owner_name is None:
        return {"ok": False, "error_kind": "no_github_remote"}
    owner, name = owner_name
    gh_result = _run_gh(["api", f"repos/{owner}/{name}/commits/{ref}/check-runs"], settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    data, err = _json_or_error(gh_result, default={})
    if err:
        return err
    checks = [
        {
            "name": item.get("name"),
            "status": item.get("status"),
            "conclusion": item.get("conclusion"),
            "url": item.get("html_url"),
        }
        for item in (data or {}).get("check_runs", [])
    ]
    return {"ok": True, "checks": checks}


def gh_run_view(
    settings: Settings,
    *,
    repo: str | None = None,
    run_id: str | None = None,
    failed_only: bool = True,
    log_tail: int = 200,
) -> dict[str, Any]:
    """View a workflow run (latest on the current branch when ``run_id`` is omitted).

    When ``failed_only`` (default), fetches the failed-jobs-only log via
    ``gh run view --log-failed`` and returns just its last ``log_tail`` lines
    as ``failed_logs``.
    """
    guard = _guard(settings)
    if guard:
        return guard

    if not run_id:
        toplevel = git_tools._resolve_repo_toplevel(repo, settings)
        branch = git_tools._run_git(["branch", "--show-current"], settings, cwd=toplevel).strip()
        list_args = ["run", "list", "--limit", "1", "--json", "databaseId"]
        if branch:
            list_args += ["--branch", branch]
        latest = _run_gh(list_args, settings, repo=repo)
        if not latest.get("ok"):
            return latest
        runs, err = _json_or_error(latest, default=[])
        if err:
            return err
        if not runs:
            return {"ok": False, "error_kind": "run_not_found", "error": "no workflow runs found"}
        run_id = str(runs[0]["databaseId"])

    args = ["run", "view", str(run_id)]
    args += ["--json", "databaseId,name,status,conclusion,headBranch,event,url,jobs"]
    gh_result = _run_gh(args, settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    run_data, err = _json_or_error(gh_result, default={})
    if err:
        return err

    result: dict[str, Any] = {"ok": True, "run": run_data}
    if failed_only:
        log_args = ["run", "view", str(run_id)]
        log_args.append("--log-failed")
        log_result = _run_gh(log_args, settings, repo=repo)
        if log_result.get("ok"):
            stdout = log_result.get("stdout") or ""
            lines = stdout.splitlines()
            result["failed_logs"] = "\n".join(lines[-log_tail:]) if log_tail else stdout
        else:
            result["failed_logs_error"] = log_result
    return result


def gh_run_rerun(
    settings: Settings,
    run_id: str,
    *,
    repo: str | None = None,
    failed_only: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Re-trigger a workflow run. Always requires ``confirmed=True``."""
    guard = _guard(settings)
    if guard:
        return guard
    not_ready = _require_gh_ready(settings)
    if not_ready:
        return not_ready
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            "gh_run_rerun re-triggers a real CI workflow run and requires confirmed=true "
            "after explicit owner confirmation."
        )

    args = ["run", "rerun", str(run_id)]
    if failed_only:
        args.append("--failed")
    gh_result = _run_gh(args, settings, repo=repo)
    ok = bool(gh_result.get("ok"))
    _audit_gh(settings, "gh_run_rerun", repo=repo, ok=ok, run_id=run_id, failed_only=failed_only)
    if not ok:
        return gh_result
    return {"ok": True, "rerun": True, "run_id": run_id, "stdout": gh_result.get("stdout")}


def gh_issue_list(
    settings: Settings,
    *,
    repo: str | None = None,
    state: str = "open",
    limit: int = 20,
) -> dict[str, Any]:
    """List issues via ``gh issue list --json ...`` (read-only)."""
    guard = _guard(settings)
    if guard:
        return guard

    args = [
        "issue",
        "list",
        "--json",
        "number,title,state,author,labels,url",
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    gh_result = _run_gh(args, settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    issues, err = _json_or_error(gh_result, default=[])
    if err:
        return err
    return {"ok": True, "issues": issues, "count": len(issues)}


def gh_issue_view(settings: Settings, number: int, *, repo: str | None = None) -> dict[str, Any]:
    """View a single issue via ``gh issue view --json ...`` (read-only)."""
    guard = _guard(settings)
    if guard:
        return guard

    fields = "number,title,body,state,author,labels,comments,url"
    gh_result = _run_gh(["issue", "view", str(number), "--json", fields], settings, repo=repo)
    if not gh_result.get("ok"):
        return gh_result
    data, err = _json_or_error(gh_result, default={})
    if err:
        return err
    return {"ok": True, "issue": data}
