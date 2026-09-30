from __future__ import annotations

from typing import Any

from .computer_client import ComputerHostError, computer_call
from .config import Settings


def call_computer(settings: Settings, method: str, **params: Any) -> dict[str, Any]:
    try:
        result = computer_call(settings, method, params)
    except ComputerHostError as exc:
        return {"ok": False, "error_kind": exc.code, "error": exc.message}
    except Exception as exc:  # noqa: BLE001 - transport boundary must return typed MCP errors
        return {"ok": False, "error_kind": "computer_host_failed", "error": str(exc)}
    result.setdefault("ok", True)
    return result
