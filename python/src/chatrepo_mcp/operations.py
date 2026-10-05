"""Server-wide operation accounting, independent of resource admission limits."""

from __future__ import annotations

import contextvars
import hashlib
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

CURRENT: contextvars.ContextVar[Operation | None] = contextvars.ContextVar(
    "operation", default=None
)
IDENTITY: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "operation_identity", default={}
)
CONTROL_TOOLS = {"list_operations", "get_operation", "cancel_operation"}

# Mutations cannot be interrupted halfway through an edit/commit transaction.
CANCELLABLE_TOOLS = {
    "repo_info",
    "list_dir",
    "tree",
    "read_text_file",
    "read_multiple_files",
    "file_metadata",
    "find_files",
    "search_text",
    "symbol_search",
    "recent_changes",
    "todo_scan",
    "dependency_map",
    "list_repos",
    "context_bootstrap",
    "batch_call",
    "doctor",
    "smoke_all",
    "workspace_symbols",
    "symbol_definition",
    "document_symbols",
    "code_diagnostics",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_branches",
    "git_blame",
    "git_grep",
    "run_command",
    "run_commands",
    "run_test_preset",
    "run_quality_gate",
    "scan_new_policy_violations",
    "gh_status",
    "gh_checks",
    "gh_pr_list",
    "gh_pr_view",
    "gh_issue_list",
    "gh_issue_view",
    "gh_run_view",
    "read_artifact",
    "get_command_log",
    "summarize_command_log",
    "git_worktree_guard",
    "list_test_presets",
}
ACTIVE = {"queued", "running", "cancelling"}


def utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class OperationCancelled(Exception):
    pass


class Operation:
    def __init__(
        self,
        registry: Registry,
        tool: str,
        args: dict[str, Any],
        *,
        kind: str = "tool",
        cancellable: bool | None = None,
        parent: Operation | None = None,
    ) -> None:
        from .output_store import redact_text

        self.registry = registry
        self.id = str(uuid.uuid4())
        self.parent = parent if parent is not None else CURRENT.get()
        identity = self.parent.identity if self.parent else IDENTITY.get()
        self.identity: dict[str, Any] = dict(identity)
        self.event = threading.Event()
        self.callbacks: dict[str, Callable[[], None]] = {}
        self.started = time.monotonic()
        self.progress_updated = self.started
        self.finished: float | None = None
        self.finishing = False
        enabled = tool in CANCELLABLE_TOOLS if cancellable is None else cancellable
        target: dict[str, str] = {
            key: redact_text(str(args[key]))[:512]
            for key in ("path", "paths", "repo", "cwd")
            if key in args
        }
        target["project_root"] = redact_text(str(registry.settings.project_root))[:512]
        if tool in {
            "tree",
            "find_files",
            "recent_changes",
            "todo_scan",
            "search_text",
            "symbol_search",
            "dependency_map",
            "list_dir",
        }:
            target.setdefault("path", ".")
        self.data: dict[str, Any] = {
            "operation_id": self.id,
            "server_instance_id": registry.instance_id,
            "parent_operation_id": self.parent.id if self.parent else None,
            "request_id": self.parent.data["request_id"] if self.parent else self.id,
            "session_id": self.identity.get("session_id"),
            "client": self.identity.get("client"),
            "tool": tool,
            "kind": kind,
            "target": target,
            "started_at": utc(),
            "updated_at": utc(),
            "status": "running",
            "cancellable": enabled,
            "cancel_reason": None
            if enabled
            else "No safe interruption point; already applied changes are not rolled back.",
            "cancel_requested": False,
            "progress": {"phase": "starting", "files": 0, "directories": 0},
        }

    def checkpoint(self, *, phase: str | None = None, files: int = 0, directories: int = 0) -> None:
        if self.event.is_set() or (
            self.data["kind"] == "tool" and self.parent is not None and self.parent.event.is_set()
        ):
            self.event.set()
            raise OperationCancelled("operation cancelled")
        with self.registry.lock:
            progress = self.data["progress"]
            if phase:
                progress["phase"] = phase
            progress["files"] += files
            progress["directories"] += directories
            now = time.monotonic()
            if now - self.progress_updated >= 0.25:
                self.data["updated_at"] = utc()
                self.progress_updated = now

    def add_cancel(self, key: str, callback: Callable[[], None]) -> None:
        with self.registry.lock:
            self.callbacks[key] = callback
            pending = self.event.is_set()
        if pending:
            callback()

    def remove_cancel(self, key: str) -> None:
        with self.registry.lock:
            self.callbacks.pop(key, None)

    def finish(self, status: str) -> None:
        with self.registry.lock:
            if self.finished is not None or self.finishing:
                return
            self.finishing = True
            self.callbacks.clear()
        self.registry.audit("operation_finished", self, status)
        with self.registry.lock:
            self.finished = time.monotonic()
            self.data.update(status=status, finished_at=utc(), updated_at=utc())


class Registry:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.instance_id = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.operations: dict[str, Operation] = {}
        self.sessions: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()

    def audit(self, event: str, operation: Operation, status: str | None = None) -> None:
        from .command_tools import _audit

        with self.lock:
            payload = {
                "timestamp": utc(),
                "event": event,
                **{
                    key: operation.data[key]
                    for key in (
                        "operation_id",
                        "server_instance_id",
                        "parent_operation_id",
                        "request_id",
                        "session_id",
                        "tool",
                        "kind",
                        "status",
                    )
                },
            }
        if status is not None:
            payload["status"] = status
        _audit(self.settings, payload)

    def prune(self) -> None:
        completed = sorted(
            (op for op in self.operations.values() if op.finished is not None),
            key=lambda op: op.finished or 0,
        )
        for index, op in enumerate(completed):
            if index < len(completed) - 1000 or time.monotonic() - (op.finished or 0) > 86400:
                self.operations.pop(op.id, None)

    def start(self, tool: str, args: dict[str, Any], **kwargs: Any) -> Operation:
        operation = Operation(self, tool, args, **kwargs)
        with self.lock:
            self.prune()
            self.operations[operation.id] = operation
        self.audit("operation_started", operation)
        return operation

    def snapshot(self, operation: Operation) -> dict[str, Any]:
        with self.lock:
            snapshot = {
                **operation.data,
                "progress": dict(operation.data["progress"]),
                "age_ms": int(
                    ((operation.finished or time.monotonic()) - operation.started) * 1000
                ),
            }
            if operation.finishing and operation.finished is None:
                snapshot["cancellable"] = False
                snapshot["cancel_reason"] = "Worker has stopped and audit output is being finalized"
                snapshot["progress"]["phase"] = "finalizing"
            return snapshot

    def list(
        self, scope: str = "server", include_finished: bool = False, limit: int = 100
    ) -> dict[str, Any]:
        identity = IDENTITY.get()
        if scope not in {"server", "session"}:
            return error("invalid_scope", "scope must be server or session")
        if scope == "session" and not identity.get("session_id"):
            return error("session_unavailable", "No MCP session is associated with this call")
        with self.lock:
            self.prune()
            selected = sorted(
                (
                    op
                    for op in self.operations.values()
                    if (include_finished or op.finished is None)
                    and (
                        scope == "server"
                        or op.identity.get("session_id") == identity.get("session_id")
                    )
                ),
                key=lambda op: op.started,
                reverse=True,
            )
            limit = min(max(limit, 1), 1000)
            return {
                "ok": True,
                "server_instance_id": self.instance_id,
                "scope": scope,
                "operations": [self.snapshot(op) for op in selected[:limit]],
                "count": len(selected[:limit]),
                "total": len(selected),
                "truncated": len(selected) > limit,
            }

    def get(self, operation_id: str) -> dict[str, Any]:
        with self.lock:
            self.prune()
            op = self.operations.get(operation_id)
            if op is None:
                return error("operation_not_found", "Operation is unknown or its history expired")
            return {"ok": True, "operation": self.snapshot(op)}

    def shutdown(self) -> None:
        with self.lock:
            identifiers = [
                op.id
                for op in self.operations.values()
                if op.finished is None and op.data["cancellable"]
            ]
        for identifier in identifiers:
            self.cancel(identifier)

    def cancel(self, operation_id: str) -> dict[str, Any]:
        with self.lock:
            op = self.operations.get(operation_id)
            if op is None:
                return error("operation_not_found", "Operation is unknown or its history expired")
            if op.finished is not None:
                return {
                    "ok": True,
                    "operation_id": op.id,
                    "cancel_requested": op.event.is_set(),
                    "status": op.data["status"],
                }
            if op.finishing:
                return error(
                    "operation_not_cancellable",
                    "Worker has stopped and audit output is being finalized",
                )
            if not op.data["cancellable"]:
                return error("operation_not_cancellable", str(op.data["cancel_reason"]))
            first = not op.event.is_set()
            op.event.set()
            op.data.update(status="cancelling", cancel_requested=True, updated_at=utc())
            callbacks = list(op.callbacks.values()) if first else []
            children = [
                child.id
                for child in self.operations.values()
                if child.parent is op
                and child.finished is None
                and child.data["kind"] == "tool"
                and child.data["cancellable"]
            ]
        if first:
            self.audit("operation_cancel_requested", op)
        # Cancellation must never block the MCP event loop on TERM/KILL grace periods.
        for callback in callbacks:
            threading.Thread(target=callback, daemon=True, name="operation-cancel").start()
        for child in children:
            self.cancel(child)
        return {"ok": True, "operation_id": op.id, "cancel_requested": True, "status": "cancelling"}


_REGISTRIES: dict[tuple[str, str], Registry] = {}
_REGISTRY_LOCK = threading.Lock()


def note_resource_cancel(settings: Any, resource_id: str) -> None:
    registry = registry_for(settings)
    with registry.lock:
        matched = [
            op
            for op in registry.operations.values()
            if op.data.get("resource_id") == resource_id
            and op.finished is None
            and not op.event.is_set()
        ]
        for op in matched:
            op.event.set()
            op.data.update(status="cancelling", cancel_requested=True, updated_at=utc())
    for op in matched:
        registry.audit("operation_cancel_requested", op)


def registry_for(settings: Any) -> Registry:
    key = (str(settings.project_root), str(settings.command_audit_log_path))
    with _REGISTRY_LOCK:
        if key not in _REGISTRIES:
            _REGISTRIES[key] = Registry(settings)
        return _REGISTRIES[key]


def error(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_kind": kind, "error": message}


def checkpoint(*, phase: str | None = None, files: int = 0, directories: int = 0) -> None:
    operation = CURRENT.get()
    if operation is not None:
        operation.checkpoint(phase=phase, files=files, directories=directories)


@contextmanager
def track(settings: Any, tool: str, args: dict[str, Any], **kwargs: Any) -> Iterator[Operation]:
    operation = registry_for(settings).start(tool, args, **kwargs)
    token = CURRENT.set(operation)
    try:
        operation.checkpoint()
        yield operation
    except OperationCancelled:
        operation.finish("cancelled")
        raise
    except BaseException:
        operation.finish("failed")
        raise
    finally:
        CURRENT.reset(token)
        if operation.finished is None:
            operation.finish("cancelled" if operation.event.is_set() else "completed")


def session_identity(settings: Any, context: Any) -> dict[str, Any]:
    registry = registry_for(settings)
    session = context.session
    request = context.request_context.request
    headers = getattr(request, "headers", {})
    raw = headers.get("mcp-session-id")
    if raw is None:
        with registry.lock:
            raw = registry.sessions.setdefault(session, str(uuid.uuid4()))
    public_id = hashlib.sha256(f"{registry.instance_id}:{raw}".encode()).hexdigest()[:32]
    params = session.client_params
    from .output_store import redact_text

    client = (
        {
            "name": redact_text(params.clientInfo.name)[:128],
            "version": redact_text(params.clientInfo.version)[:128],
        }
        if params and params.clientInfo
        else None
    )
    return {"session_id": public_id, "client": client}
