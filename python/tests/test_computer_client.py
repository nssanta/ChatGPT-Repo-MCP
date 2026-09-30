from __future__ import annotations

import base64
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from mcp.types import CallToolResult, ImageContent
import pytest

from chatrepo_mcp import server
from chatrepo_mcp.computer_client import ComputerHostClient, ComputerHostError


def _fake_host(path: Path) -> Path:
    script = path / "fake-computer-host"
    script.write_text(
        """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    req=json.loads(line)
    if req["method"] == "fail":
        reply={"id":req["id"],"error":{"code":"stale_snapshot","message":"refresh"}}
    else:
        reply={"id":req["id"],"result":{"ok":True,"method":req["method"],"params":req.get("params",{})}}
    print(json.dumps(reply), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_python_computer_client_uses_shared_host_protocol(tmp_path: Path, monkeypatch) -> None:
    host = _fake_host(tmp_path)
    monkeypatch.setenv("COMPUTER_HOST_PATH", str(host))
    settings = replace(
        server.settings,
        computer_use_enabled=True,
        computer_control_enabled=False,
        computer_snapshot_ttl_seconds=17,
        computer_action_timeout_ms=1234,
        computer_max_sequence_steps=7,
        computer_capture_max_edge=900,
    )
    client = ComputerHostClient(settings)
    try:
        result = client.call("status", {"probe": True})
        assert result == {"ok": True, "method": "status", "params": {"probe": True}}
        with pytest.raises(ComputerHostError) as caught:
            client.call("fail")
        assert caught.value.code == "stale_snapshot"
    finally:
        client.close()


def test_computer_result_returns_native_mcp_image_without_structured_base64() -> None:
    png = b"\x89PNG\r\n\x1a\n"
    result = server._computer_result(
        {
            "ok": True,
            "snapshot_id": "snap",
            "mime_type": "image/png",
            "image_width": 1,
            "image_height": 1,
            "bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
            "scene": {},
            "image_b64": base64.b64encode(png).decode(),
        }
    )
    assert isinstance(result, CallToolResult)
    assert "image_b64" not in result.structuredContent
    images = [item for item in result.content if isinstance(item, ImageContent)]
    assert len(images) == 1
    assert images[0].data == base64.b64encode(png).decode()

REPO_ROOT = Path(__file__).resolve().parents[2]


def _registered_tools(tmp_path: Path, *, access_mode: str, use: bool, control: bool) -> list[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "PROJECT_ROOT": str(tmp_path),
            "ACCESS_MODE": access_mode,
            "ENABLE_PTY": "false",
            "COMPUTER_USE_ENABLED": "true" if use else "false",
            "COMPUTER_CONTROL_ENABLED": "true" if control else "false",
            "PYTHONPATH": str(REPO_ROOT / "python" / "src"),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from chatrepo_mcp.server import _tool_names; print(json.dumps(_tool_names()))",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_python_registration_gates_computer_eyes_and_hands(tmp_path: Path) -> None:
    baseline = _registered_tools(tmp_path / "base", access_mode="safe", use=False, control=False)
    eyes = _registered_tools(tmp_path / "eyes", access_mode="safe", use=True, control=False)
    hands = _registered_tools(tmp_path / "hands", access_mode="full", use=True, control=True)

    assert len(baseline) == 94
    assert len(eyes) == 101
    assert len(hands) == 111  # PTY explicitly disabled in this registration test.
    assert "computer_observe" not in baseline
    assert "computer_observe" in eyes
    assert "computer_share_snapshot" in eyes
    assert "computer_click" not in eyes
    assert {"computer_click", "computer_move", "computer_type", "computer_sequence"} <= set(hands)


def test_wayland_multi_monitor_layout_and_region_math() -> None:
    path = REPO_ROOT / "go" / "internal" / "computer" / "assets" / "linux" / "wayland_portal.py"
    spec = importlib.util.spec_from_file_location("chatrepo_wayland_portal_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    streams, origin, logical = module.normalize_stream_layout(
        [
            (11, {"logical_size": (1920, 1080), "position": (-1920, 0)}),
            (12, {"logical_size": (2560, 1440), "position": (0, 0)}),
        ]
    )
    assert origin == (-1920, 0)
    assert logical == (4480, 1440)
    assert streams[0]["virtual_position"] == (0, 0)
    assert streams[1]["virtual_position"] == (1920, 0)

    fallback, fallback_origin, fallback_logical = module.normalize_stream_layout(
        [
            (21, {"logical_size": (1280, 720)}),
            (22, {"logical_size": (1920, 1080)}),
        ]
    )
    assert fallback_origin == (0, 0)
    assert fallback_logical == (3200, 1080)
    assert fallback[1]["virtual_position"] == (1280, 0)

    plan = module.region_plan(logical, (4480, 1440), {"x": 1800, "y": 100, "width": 400, "height": 300})
    assert plan["region"] == {"x": 1800, "y": 100, "width": 400, "height": 300}
    assert plan["crop"] == (1800, 100, 400, 300)


def test_computer_share_result_includes_short_lived_resource_link() -> None:
    from mcp.types import ResourceLink

    png = b"\x89PNG\r\n\x1a\n"
    result = server._computer_result(
        {
            "ok": True,
            "snapshot_id": "snap",
            "mime_type": "image/png",
            "name": "computer-snapshot-snap.png",
            "size_bytes": len(png),
            "resource_uri": "chatrepo-screen://local/token123",
            "expires_at": "2030-01-01T00:00:00Z",
            "image_b64": base64.b64encode(png).decode(),
        }
    )
    assert isinstance(result, CallToolResult)
    assert "image_b64" not in result.structuredContent
    links = [item for item in result.content if isinstance(item, ResourceLink)]
    assert len(links) == 1
    assert str(links[0].uri) == "chatrepo-screen://local/token123"
    assert links[0].mimeType == "image/png"
