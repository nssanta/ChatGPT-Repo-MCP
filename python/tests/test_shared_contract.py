from __future__ import annotations

import json
from pathlib import Path

import anyio

from chatrepo_mcp import __version__
from chatrepo_mcp.server import mcp, settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "contracts" / "tool-schemas" / "tools.json"


def test_python_server_matches_shared_tool_contract() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    tools = anyio.run(mcp.list_tools)
    actual = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    pty_names = {
        "start_terminal_session", "read_terminal_session", "write_terminal_session",
        "resize_terminal_session", "close_terminal_session", "list_terminal_sessions",
    }
    excluded: set[str] = set()
    if not (settings.full_access and settings.enable_pty):
        excluded.update(pty_names)
    computer_read = {
        "computer_status", "computer_observe", "computer_zoom",
        "computer_windows", "computer_elements", "computer_wait",
    }
    computer_control = {
        "computer_element", "computer_click", "computer_move", "computer_type", "computer_key",
        "computer_scroll", "computer_drag", "computer_window", "computer_launch", "computer_sequence",
    }
    if not settings.computer_use_enabled:
        excluded.update(computer_read)
        excluded.update(computer_control)
    elif not settings.computer_control_enabled:
        excluded.update(computer_control)
    expected = [tool for tool in contract["tools"] if tool["name"] not in excluded]
    assert actual == expected
    assert len(actual) == contract["server"]["toolCount"] - len(excluded)


def test_python_version_matches_shared_release_version() -> None:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert __version__ == version
