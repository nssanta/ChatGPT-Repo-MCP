from __future__ import annotations

from typing import Any

from .computer_client import ComputerHostError, computer_call
from .config import Settings
from .file_transfer import FileTransferError, materialize_computer_snapshot


def call_computer(settings: Settings, method: str, **params: Any) -> dict[str, Any]:
    try:
        result = computer_call(settings, method, params)
    except ComputerHostError as exc:
        return {"ok": False, "error_kind": exc.code, "error": exc.message}
    except Exception as exc:  # noqa: BLE001 - transport boundary must return typed MCP errors
        return {"ok": False, "error_kind": "computer_host_failed", "error": str(exc)}
    result.setdefault("ok", True)
    return result


def share_computer_snapshot(settings: Settings, snapshot_id: str) -> dict[str, Any]:
    result = call_computer(settings, "share_snapshot", snapshot_id=snapshot_id)
    if result.get("ok") is not True:
        return result
    try:
        return materialize_computer_snapshot(result=result, settings=settings)
    except FileTransferError as exc:
        return {"ok": False, "error_kind": exc.kind, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - cache/file-transfer boundary
        return {"ok": False, "error_kind": "share_cache_failed", "error": str(exc)}
