"""Git workflow tools: branch switching, staging, stash, fetch/pull/push, merge/revert/reset, worktrees.

This is Phase 4 of the plan (see `snuggly-spinning-toast.md`, "Фаза 4 — Git-workflow + push"):
structural, aduited tools that go beyond the read-only tools in `git_tools.py` and the single
`git_commit` in `command_tools.py`. Every function is a plain, side-effecting function that takes
`settings` first, resolves the target git toplevel via `git_tools._resolve_repo_toplevel` (never
re-implemented here), runs git through `git_tools._run_git`, and returns a JSON-able
`{"ok": ..., "repo": ..., ...}` dict.

Two error-signalling conventions are used, matching the rest of the codebase:
- Truly exceptional conditions (invalid git command, bad ref, disabled feature) raise
  `git_tools.GitToolError`.
- Operations that need explicit owner confirmation before doing something destructive/network
  raise `command_tools.ConfirmationRequiredError` (the server layer turns this into
  `{"ok": False, "error_kind": "confirmation_required", "reason": ...}`).
- Recoverable *outcomes* that are not policy violations but still need the caller's attention
  (dirty working tree, merge/pull/revert conflicts, a rejected push) are returned as
  `{"ok": False, "error_kind": "...", ...}` dicts rather than raised, so the agent can inspect
  and decide what to do next without losing repo state. Conflicts are left unresolved for the
  caller to fix (merge/revert), except `git_pull` which auto-aborts on conflict since a pull is
  expected to be a no-op-or-fast-forward from the caller's point of view.

Registration into `server.py` (tool names, MCP annotations, `RepoScope` typing) is intentionally
left to a separate integration pass; this module only exports plain callables.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import security, workspace
from .command_tools import ConfirmationRequiredError, _audit, _redact
from .config import Settings
from .git_tools import GitToolError, _repo_rel, _resolve_repo_toplevel, _run_git

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

#: Two-letter `git status --porcelain` codes that indicate an unresolved merge conflict.
_CONFLICT_CODES = {"UU", "AA", "DD", "AU", "UA", "UD", "DU"}

#: Tokens that would make `git_add` behave like a blanket `git add .`/`git add -A`; explicit
#: paths are required instead so the agent (and the audit trail) always knows exactly what was
#: staged.
_FORBIDDEN_ADD_TOKENS = {".", "-a", "-A", "--all"}

_STASH_ACTIONS = {"push", "pop", "apply", "list", "show", "drop"}
_STASH_CONFIRM_ACTIONS = {"pop", "drop"}


def _conflicted_paths(status_porcelain: str) -> list[str]:
    """Extract paths with an unresolved-conflict status code from `git status --porcelain`."""
    paths: list[str] = []
    for line in status_porcelain.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        if code in _CONFLICT_CODES:
            paths.append(line[3:].strip())
    return paths


def _current_branch(settings: Settings, toplevel: Path) -> str:
    return _run_git(["branch", "--show-current"], settings, cwd=toplevel).strip()


def _head_sha(settings: Settings, toplevel: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], settings, cwd=toplevel).strip()


def _remote_ref_snapshot(settings: Settings, toplevel: Path, remote: str) -> dict[str, str]:
    """Map `refs/remotes/<remote>/*` -> object sha, best-effort (empty if remote unknown)."""
    try:
        output = _run_git(
            ["for-each-ref", "--format=%(refname) %(objectname)", f"refs/remotes/{remote}"],
            settings,
            cwd=toplevel,
        )
    except GitToolError:
        return {}
    refs: dict[str, str] = {}
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


# --------------------------------------------------------------------------
# Branches
# --------------------------------------------------------------------------


def git_switch_branch(
    settings: Settings,
    branch: str,
    *,
    repo: str | None = None,
    create: bool = False,
    start_point: str | None = None,
    stash_first: bool = False,
) -> dict[str, Any]:
    """Switch to `branch` (optionally creating it), refusing to run over a dirty tree.

    If the working tree is dirty and `stash_first` is false, returns
    `{"ok": False, "error_kind": "git_dirty", ...}` instead of switching. If `stash_first` is
    true, auto-stashes (including untracked files) before switching, and best-effort restores
    the stash if the switch itself fails.
    """
    if not branch.strip():
        raise GitToolError("branch must not be empty")
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    previous_branch = _current_branch(settings, toplevel)
    porcelain = _run_git(["status", "--porcelain"], settings, cwd=toplevel).strip()
    dirty = bool(porcelain)
    stashed = False

    if dirty and not stash_first:
        return {
            "ok": False,
            "error_kind": "git_dirty",
            "repo": repo_rel,
            "branch": previous_branch,
            "status": porcelain,
            "hint": (
                "Working tree has uncommitted changes. Commit them, run "
                "git_stash(action='push'), or retry git_switch_branch with stash_first=true."
            ),
        }

    if not create and start_point:
        raise GitToolError("start_point is only valid together with create=true")

    if dirty and stash_first:
        stash_message = f"chatrepo-mcp: autostash before switching to {branch}"
        _run_git(["stash", "push", "-u", "-m", stash_message], settings, cwd=toplevel)
        stashed = True

    args = ["switch"]
    if create:
        args.append("-c")
    args.append(branch)
    if create and start_point:
        args.append(start_point)

    try:
        _run_git(args, settings, cwd=toplevel)
    except GitToolError:
        if stashed:
            # Best-effort: don't leave the tree dirty *and* stashed *and* unswitched.
            try:
                _run_git(["stash", "pop"], settings, cwd=toplevel)
            except GitToolError:
                pass
        raise

    return {
        "ok": True,
        "repo": repo_rel,
        "previous_branch": previous_branch,
        "branch": branch,
        "stashed": stashed,
    }


def git_create_branch(
    settings: Settings,
    branch: str,
    *,
    repo: str | None = None,
    start_point: str = "HEAD",
    checkout: bool = True,
) -> dict[str, Any]:
    """Create `branch` from `start_point`, validating the name via `git check-ref-format`."""
    if not branch.strip():
        raise GitToolError("branch must not be empty")
    toplevel = _resolve_repo_toplevel(repo, settings)
    _run_git(["check-ref-format", "--branch", branch], settings, cwd=toplevel)
    base_sha = _run_git(["rev-parse", start_point], settings, cwd=toplevel).strip()
    if checkout:
        _run_git(["switch", "-c", branch, start_point], settings, cwd=toplevel)
    else:
        _run_git(["branch", branch, start_point], settings, cwd=toplevel)
    return {
        "ok": True,
        "repo": _repo_rel(toplevel, settings),
        "branch": branch,
        "base_sha": base_sha,
        "checked_out": checkout,
    }


# --------------------------------------------------------------------------
# Staging / restoring
# --------------------------------------------------------------------------


def git_add(
    settings: Settings,
    paths: list[str],
    *,
    repo: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Stage explicit `paths` only; blanket `git add .`/`-A`/`--all` is refused.

    Each path is checked against `security.is_secret_relative`/`is_blocked_relative`; matching
    paths are skipped (reported in `skipped_blocked`) rather than staged.
    """
    if not paths:
        raise GitToolError("paths must not be empty; git_add only accepts explicit paths")
    for raw in paths:
        if raw.strip() in _FORBIDDEN_ADD_TOKENS:
            raise GitToolError(
                f"'{raw}' is not allowed: git_add requires explicit paths (no '.'/'-A'/'--all')"
            )

    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)

    candidates: list[str] = []
    skipped_blocked: list[str] = []
    for raw in paths:
        rel = security.normalize_rel_path(raw)
        if security.is_secret_relative(rel, settings) or security.is_blocked_relative(rel, settings):
            skipped_blocked.append(rel)
            continue
        candidates.append(rel)

    if not candidates:
        return {
            "ok": True,
            "repo": repo_rel,
            "staged": [],
            "skipped_blocked": skipped_blocked,
            "dry_run": dry_run,
        }

    if dry_run:
        output = _run_git(["add", "--dry-run", "--", *candidates], settings, cwd=toplevel)
        would_stage: list[str] = []
        for line in output.strip().splitlines():
            text = line.strip()
            if "'" in text:
                would_stage.append(text.split("'", 2)[1])
            elif text:
                would_stage.append(text)
        return {
            "ok": True,
            "repo": repo_rel,
            "staged": would_stage,
            "skipped_blocked": skipped_blocked,
            "dry_run": True,
        }

    _run_git(["add", "--", *candidates], settings, cwd=toplevel)
    return {
        "ok": True,
        "repo": repo_rel,
        "staged": candidates,
        "skipped_blocked": skipped_blocked,
        "dry_run": False,
    }


def git_restore(
    settings: Settings,
    paths: list[str],
    *,
    repo: str | None = None,
    staged: bool = False,
    source: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Unstage (`staged=True`, always safe) or discard (`staged=False`, needs `confirmed`) changes.

    Discarding requires `confirmed=true`; without it, raises `ConfirmationRequiredError` whose
    message includes a preview of the diff that would be lost.
    """
    if not paths:
        raise GitToolError("paths must not be empty")
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    rel_paths = [security.normalize_rel_path(p) for p in paths]

    if staged:
        _run_git(["restore", "--staged", "--", *rel_paths], settings, cwd=toplevel)
        return {"ok": True, "repo": repo_rel, "restored": rel_paths, "staged": True}

    if not settings.confirmation_granted(confirmed):
        preview = _run_git(
            ["diff", "--", *rel_paths],
            settings,
            cwd=toplevel,
            max_bytes=min(settings.max_diff_bytes, 4000),
        )
        raise ConfirmationRequiredError(
            "git_restore would permanently discard uncommitted working-tree changes for "
            f"{rel_paths}; pass confirmed=true after reviewing this preview diff:\n{preview}"
        )

    args = ["restore"]
    if source:
        args += ["--source", source]
    args += ["--", *rel_paths]
    _run_git(args, settings, cwd=toplevel)
    return {"ok": True, "repo": repo_rel, "restored": rel_paths, "staged": False}


def git_stash(
    settings: Settings,
    *,
    repo: str | None = None,
    action: str = "push",
    message: str | None = None,
    include_untracked: bool = True,
    stash_ref: str | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Run `git stash <action>`. `pop`/`drop` require `confirmed` (loss/conflict risk)."""
    if action not in _STASH_ACTIONS:
        raise GitToolError(f"action must be one of {sorted(_STASH_ACTIONS)}")
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)

    if action in _STASH_CONFIRM_ACTIONS and not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            f"git_stash(action='{action}') can lose stashed work or conflict with the working "
            "tree; pass confirmed=true after explicit owner confirmation."
        )

    if action == "push":
        args = ["stash", "push"]
        if include_untracked:
            args.append("-u")
        if message:
            args += ["-m", message]
        output = _run_git(args, settings, cwd=toplevel).strip()
        return {"ok": True, "repo": repo_rel, "action": "push", "output": output}

    if action == "list":
        output = _run_git(["stash", "list"], settings, cwd=toplevel).strip()
        stashes = [line for line in output.splitlines() if line.strip()]
        return {"ok": True, "repo": repo_rel, "action": "list", "stashes": stashes}

    if action == "show":
        ref = stash_ref or "stash@{0}"
        output = _run_git(["stash", "show", "-p", ref], settings, cwd=toplevel, max_bytes=settings.max_diff_bytes)
        return {"ok": True, "repo": repo_rel, "action": "show", "stash_ref": ref, "diff": output}

    # apply / pop / drop
    args = ["stash", action]
    if stash_ref:
        args.append(stash_ref)
    output = _run_git(args, settings, cwd=toplevel).strip()
    return {
        "ok": True,
        "repo": repo_rel,
        "action": action,
        "stash_ref": stash_ref or "stash@{0}",
        "output": output,
    }


# --------------------------------------------------------------------------
# Network: fetch / pull / push
# --------------------------------------------------------------------------


def git_fetch(
    settings: Settings,
    *,
    repo: str | None = None,
    remote: str = "origin",
    prune: bool = False,
    all_repos: bool = False,
) -> dict[str, Any]:
    """Fetch `remote`. With `all_repos=True`, fans out across every git repo in the workspace."""
    if all_repos:
        entries = [entry for entry in workspace.list_workspace_repos(settings) if entry.get("is_git")]
        results = []
        for entry in entries:
            entry_repo = entry["path"] or None
            try:
                result = git_fetch(settings, repo=entry_repo, remote=remote, prune=prune, all_repos=False)
            except GitToolError as exc:
                result = {"ok": False, "repo": entry["path"], "error": str(exc)}
            results.append(result)
        return {
            "ok": all(r.get("ok") for r in results),
            "all_repos": True,
            "remote": remote,
            "results": results,
            "count": len(results),
        }

    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    before = _remote_ref_snapshot(settings, toplevel, remote)

    args = ["fetch", remote]
    if prune:
        args.append("--prune")
    _run_git(args, settings, cwd=toplevel, network=True, max_bytes=settings.max_response_chars)

    after = _remote_ref_snapshot(settings, toplevel, remote)
    updated_refs: list[dict[str, Any]] = []
    for ref, sha in after.items():
        if before.get(ref) != sha:
            updated_refs.append({"ref": ref, "before": before.get(ref), "after": sha})
    for ref, sha in before.items():
        if ref not in after:
            updated_refs.append({"ref": ref, "before": sha, "after": None})

    return {"ok": True, "repo": repo_rel, "remote": remote, "updated_refs": updated_refs}


def git_pull(
    settings: Settings,
    *,
    repo: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    ff_only: bool = True,
    rebase: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Pull `remote`/`branch`. Fast-forward-only by default; non-ff/rebase need `confirmed`.

    On conflict, aborts the in-progress merge/rebase automatically and returns
    `{"ok": False, "error_kind": "pull_conflict", "conflicts": [...]}` so the repo is left clean.
    """
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    current_branch = _current_branch(settings, toplevel)
    target_branch = branch or current_branch

    needs_confirmation = rebase or not ff_only
    if needs_confirmation and not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            "git_pull with rebase=true or ff_only=false can rewrite local history or create a "
            "merge commit; pass confirmed=true after explicit owner confirmation."
        )

    before_sha = _head_sha(settings, toplevel)

    args = ["pull"]
    if rebase:
        args.append("--rebase")
    elif ff_only:
        args.append("--ff-only")
    else:
        args.append("--no-ff")
    args.append(remote)
    if target_branch:
        args.append(target_branch)

    try:
        _run_git(args, settings, cwd=toplevel, network=True, max_bytes=settings.max_response_chars)
    except GitToolError as exc:
        status = _run_git(["status", "--porcelain"], settings, cwd=toplevel)
        conflicts = _conflicted_paths(status)
        if conflicts or "conflict" in str(exc).lower():
            abort_args = ["rebase", "--abort"] if rebase else ["merge", "--abort"]
            try:
                _run_git(abort_args, settings, cwd=toplevel)
            except GitToolError:
                pass
            return {
                "ok": False,
                "error_kind": "pull_conflict",
                "repo": repo_rel,
                "conflicts": conflicts,
                "error": str(exc),
            }
        raise

    after_sha = _head_sha(settings, toplevel)
    files_changed: list[str] = []
    if before_sha != after_sha:
        diff_names = _run_git(["diff", "--name-only", before_sha, after_sha], settings, cwd=toplevel)
        files_changed = [line for line in diff_names.strip().splitlines() if line.strip()]

    return {
        "ok": True,
        "repo": repo_rel,
        "before_sha": before_sha,
        "after_sha": after_sha,
        "files_changed": files_changed,
    }


def git_push(
    settings: Settings,
    *,
    repo: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    set_upstream: bool = False,
    force_with_lease: bool = False,
    dry_run: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Push `branch` to `remote`, guarded by policy (see module docstring / plan Phase 4):

    1. `dry_run=True` by default: runs a real `git push --dry-run` and returns the preview.
    2. Pushing to a `settings.protected_branches` branch always requires `confirmed=true`, even
       for a dry run.
    3. `force_with_lease` is only available when `settings.allow_force_push` is true *and*
       `confirmed=true`; plain `--force` is not supported at all (no parameter for it).
    4. Any real push (`dry_run=false`) requires `confirmed=true`, protected branch or not.
    5. Every real (non-dry-run) push is written to the audit log.
    6. The network call sets `GIT_TERMINAL_PROMPT=0` and
       `GIT_SSH_COMMAND="ssh -oBatchMode=yes"` so a missing credential fails fast as a structured
       error instead of hanging on a prompt. `git_tools._run_git(network=True)` only sets
       `GIT_TERMINAL_PROMPT`, so this runs its own `subprocess.run` (still via
       `_resolve_repo_toplevel` for the cwd) rather than extending the shared helper.
    """
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    current_branch = _current_branch(settings, toplevel)
    target_branch = branch or current_branch
    if not target_branch:
        raise GitToolError("cannot push from a detached HEAD; pass branch explicitly")

    protected = target_branch in settings.protected_branches

    if force_with_lease and not settings.allow_force_push:
        raise GitToolError(
            "force_with_lease is disabled: set ALLOW_FORCE_PUSH=true in settings to enable it"
        )

    reasons: list[str] = []
    if protected:
        reasons.append(
            f"target branch '{target_branch}' is protected ({', '.join(settings.protected_branches)})"
        )
    if not dry_run:
        reasons.append("this is a real push (dry_run=false)")
    if force_with_lease:
        reasons.append("force_with_lease was requested")
    if reasons and not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError("git_push requires confirmed=true: " + "; ".join(reasons))

    args = ["push"]
    if dry_run:
        args.append("--dry-run")
    if force_with_lease:
        args.append("--force-with-lease")
    if set_upstream:
        args.append("--set-upstream")
    args.append(remote)
    args.append(target_branch)

    remote_ref = f"refs/remotes/{remote}/{target_branch}"
    try:
        remote_before_sha = _run_git(["rev-parse", remote_ref], settings, cwd=toplevel).strip()
    except GitToolError:
        remote_before_sha = None

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(toplevel),
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.git_network_timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error_kind": "push_timeout",
            "repo": repo_rel,
            "remote": remote,
            "branch": target_branch,
            "dry_run": dry_run,
            "error": str(exc),
        }

    stdout = _redact(proc.stdout)
    stderr = _redact(proc.stderr)
    ok = proc.returncode == 0

    if not dry_run:
        _audit(
            settings,
            {
                "timestamp": int(time.time()),
                "tool": "git_push",
                "repo": repo_rel,
                "remote": remote,
                "branch": target_branch,
                "protected": protected,
                "force_with_lease": force_with_lease,
                "set_upstream": set_upstream,
                "ok": ok,
                "exit_code": proc.returncode,
            },
        )

    if not ok:
        return {
            "ok": False,
            "error_kind": "push_rejected",
            "repo": repo_rel,
            "remote": remote,
            "branch": target_branch,
            "dry_run": dry_run,
            "stdout": stdout,
            "stderr": stderr,
        }

    remote_after_sha = remote_before_sha
    if not dry_run:
        try:
            remote_after_sha = _run_git(["rev-parse", remote_ref], settings, cwd=toplevel).strip()
        except GitToolError:
            remote_after_sha = None

    return {
        "ok": True,
        "repo": repo_rel,
        "pushed_ref": f"{remote}/{target_branch}",
        "dry_run": dry_run,
        "protected": protected,
        "force_with_lease": force_with_lease,
        "remote_before_sha": remote_before_sha,
        "remote_after_sha": remote_after_sha,
        "stdout": stdout,
        "stderr": stderr,
    }


# --------------------------------------------------------------------------
# Merge / revert / reset
# --------------------------------------------------------------------------


def git_merge(
    settings: Settings,
    branch: str,
    *,
    repo: str | None = None,
    no_ff: bool = False,
    message: str | None = None,
    abort: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Merge `branch` into HEAD (needs `confirmed`), or `abort=True` to cancel an in-progress merge.

    On conflict, the merge is left unresolved (no auto-abort) so the caller can inspect and fix
    it, or call `git_merge(abort=True)` explicitly.
    """
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)

    if abort:
        _run_git(["merge", "--abort"], settings, cwd=toplevel)
        return {"ok": True, "repo": repo_rel, "aborted": True}

    if not branch.strip():
        raise GitToolError("branch must not be empty")
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            f"git_merge('{branch}') modifies the working tree and history; pass confirmed=true "
            "after explicit owner confirmation."
        )

    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    if message:
        args += ["-m", message]
    args.append(branch)

    try:
        _run_git(args, settings, cwd=toplevel)
    except GitToolError as exc:
        status = _run_git(["status", "--porcelain"], settings, cwd=toplevel)
        conflicts = _conflicted_paths(status)
        if conflicts or "conflict" in str(exc).lower():
            return {
                "ok": False,
                "error_kind": "merge_conflict",
                "repo": repo_rel,
                "conflicts": conflicts,
                "error": str(exc),
                "hint": "Resolve conflicts and commit, or call git_merge(abort=true) to cancel.",
            }
        raise

    merged_sha = _head_sha(settings, toplevel)
    return {"ok": True, "repo": repo_rel, "merged_sha": merged_sha, "conflicts": []}


def git_revert(
    settings: Settings,
    revision: str,
    *,
    repo: str | None = None,
    no_commit: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Revert `revision` (needs `confirmed`). On conflict, auto-aborts and reports conflicts."""
    if not revision.strip():
        raise GitToolError("revision must not be empty")
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            f"git_revert('{revision}') rewrites the working tree/history; pass confirmed=true "
            "after explicit owner confirmation."
        )
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)

    args = ["revert", "--no-edit"]
    if no_commit:
        args.append("--no-commit")
    args.append(revision)

    try:
        _run_git(args, settings, cwd=toplevel)
    except GitToolError as exc:
        status = _run_git(["status", "--porcelain"], settings, cwd=toplevel)
        conflicts = _conflicted_paths(status)
        if conflicts or "conflict" in str(exc).lower():
            try:
                _run_git(["revert", "--abort"], settings, cwd=toplevel)
            except GitToolError:
                pass
            return {
                "ok": False,
                "error_kind": "revert_conflict",
                "repo": repo_rel,
                "conflicts": conflicts,
                "error": str(exc),
            }
        raise

    reverted_sha = None if no_commit else _head_sha(settings, toplevel)
    return {
        "ok": True,
        "repo": repo_rel,
        "reverted": revision,
        "reverted_sha": reverted_sha,
        "no_commit": no_commit,
    }


def git_reset(
    settings: Settings,
    *,
    repo: str | None = None,
    mode: str = "mixed",
    target: str = "HEAD~1",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Reset HEAD to `target`; hard mode additionally requires ALLOW_HARD_RESET=true."""
    if mode not in {"soft", "mixed", "hard"}:
        raise GitToolError("git_reset mode must be one of: soft, mixed, hard")
    if mode == "hard" and not settings.allow_hard_reset:
        raise GitToolError("hard reset is disabled: set ALLOW_HARD_RESET=true to enable it")
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            f"git_reset(mode='{mode}', target='{target}') moves HEAD and can strip commits from "
            "the current branch; pass confirmed=true after explicit owner confirmation."
        )
    toplevel = _resolve_repo_toplevel(repo, settings)
    _run_git(["reset", f"--{mode}", target], settings, cwd=toplevel)
    head_sha = _head_sha(settings, toplevel)
    return {
        "ok": True,
        "repo": _repo_rel(toplevel, settings),
        "mode": mode,
        "target": target,
        "head_sha": head_sha,
    }


# --------------------------------------------------------------------------
# Worktrees
# --------------------------------------------------------------------------


def git_worktree_add(
    settings: Settings,
    branch: str,
    *,
    repo: str | None = None,
    base: str = "HEAD",
    create_branch: bool = True,
) -> dict[str, Any]:
    """Add a worktree under `project_root/.chatrepo-worktrees/<repo>-<branch>/`."""
    if not branch.strip():
        raise GitToolError("branch must not be empty")
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    repo_label = repo_rel.replace("/", "-") or toplevel.name
    safe_branch = re.sub(r"[^A-Za-z0-9_.-]+", "-", branch).strip("-") or "branch"

    worktrees_root = settings.project_root / ".chatrepo-worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktrees_root / f"{repo_label}-{safe_branch}"
    if worktree_path.exists():
        raise GitToolError(f"worktree path already exists: {worktree_path}")

    args = ["worktree", "add"]
    if create_branch:
        args += ["-b", branch, str(worktree_path), base]
    else:
        args += [str(worktree_path), branch]

    _run_git(args, settings, cwd=toplevel)
    return {
        "ok": True,
        "repo": repo_rel,
        "worktree_path": str(worktree_path),
        "branch": branch,
    }


def prepare_task_worktree(
    settings: Settings,
    *,
    branch: str,
    task_name: str,
    repo: str | None = None,
    base: str = "HEAD",
    dry_run: bool = True,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Resolve an exact base and prepare an isolated task branch/worktree."""
    if not branch.strip() or not task_name.strip():
        raise GitToolError("branch and task_name are required")
    if not settings.confirmation_granted(confirmed) and not dry_run:
        raise ConfirmationRequiredError("prepare_task_worktree creates a branch and worktree; pass confirmed=true")
    toplevel = _resolve_repo_toplevel(repo, settings)
    repo_rel = _repo_rel(toplevel, settings)
    _run_git(["check-ref-format", "--branch", branch], settings, cwd=toplevel)
    base_sha = _run_git(["rev-parse", "--verify", f"{base}^{{commit}}"], settings, cwd=toplevel).strip()
    status = _run_git(["status", "--porcelain"], settings, cwd=toplevel)
    parent_dirty = bool(status.strip())
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_name).strip("-._")
    if not safe_task:
        raise GitToolError("task_name has no usable characters")
    worktree_path = (settings.project_root / ".chatrepo-worktrees" / safe_task).resolve()
    if worktree_path.exists():
        raise GitToolError(f"worktree path already exists: {worktree_path}")
    branch_exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(toplevel), timeout=settings.subprocess_timeout, check=False,
    ).returncode == 0
    if branch_exists:
        raise GitToolError(f"branch already exists: {branch}")
    warnings = ["parent worktree is dirty; uncommitted changes are not copied"] if parent_dirty else []
    result = {
        "ok": True, "dry_run": dry_run, "applied": not dry_run, "repo": repo_rel,
        "branch": branch, "base": base, "base_sha": base_sha,
        "worktree_path": str(worktree_path), "parent_dirty": parent_dirty, "warnings": warnings,
    }
    if dry_run:
        return result
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(["worktree", "add", "-b", branch, str(worktree_path), base_sha], settings, cwd=toplevel)
    except Exception:
        subprocess.run(["git", "branch", "-D", branch], cwd=str(toplevel), timeout=settings.subprocess_timeout, check=False, capture_output=True)
        raise
    return result


def git_worktree_list(settings: Settings, *, repo: str | None = None) -> dict[str, Any]:
    """List worktrees registered against the repo, parsed from `git worktree list --porcelain`."""
    toplevel = _resolve_repo_toplevel(repo, settings)
    output = _run_git(["worktree", "list", "--porcelain"], settings, cwd=toplevel)

    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[len("worktree ") :].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].strip()
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
        elif line.startswith("locked"):
            current["locked"] = True
            reason = line[len("locked") :].strip()
            if reason:
                current["lock_reason"] = reason
        elif line == "prunable":
            current["prunable"] = True
    if current:
        worktrees.append(current)

    return {"ok": True, "repo": _repo_rel(toplevel, settings), "worktrees": worktrees}


def git_worktree_remove(
    settings: Settings,
    worktree_path: str,
    *,
    repo: str | None = None,
    force: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Remove a worktree (needs `confirmed`). Refuses paths outside the allowed workspace roots."""
    if not settings.confirmation_granted(confirmed):
        raise ConfirmationRequiredError(
            f"git_worktree_remove('{worktree_path}') deletes a worktree checkout; pass "
            "confirmed=true after explicit owner confirmation."
        )
    toplevel = _resolve_repo_toplevel(repo, settings)

    candidate = Path(worktree_path).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (toplevel / worktree_path).resolve()
    if not workspace.is_within_roots(target, workspace.resolve_roots(settings)):
        raise GitToolError(f"worktree_path escapes allowed workspace roots: {worktree_path}")

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    _run_git(args, settings, cwd=toplevel)
    return {"ok": True, "repo": _repo_rel(toplevel, settings), "removed": str(target)}
