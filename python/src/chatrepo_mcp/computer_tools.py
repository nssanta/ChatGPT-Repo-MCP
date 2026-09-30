from __future__ import annotations

import base64
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


def read_computer_share(settings: Settings, token: str) -> tuple[bytes, str]:
    result = computer_call(settings, "read_share", {"token": token})
    image_b64 = result.get("image_b64")
    if not isinstance(image_b64, str) or not image_b64:
        raise ComputerHostError("share_invalid", "computer share returned no image")
    return base64.b64decode(image_b64), str(result.get("mime_type") or "image/png")
