from __future__ import annotations

import json
from pathlib import Path

import anyio

from chatrepo_mcp import __version__
from chatrepo_mcp.server import mcp

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
    expected = [tool for tool in contract["tools"] if tool["name"] not in pty_names]
    assert actual == expected
    assert len(actual) == contract["server"]["toolCount"] - len(pty_names)


def test_python_version_matches_shared_release_version() -> None:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert __version__ == version
