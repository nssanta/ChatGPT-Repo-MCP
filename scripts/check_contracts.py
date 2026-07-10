#!/usr/bin/env python3
"""Verify shared contract copies and release metadata without modifying files."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anyio


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

from chatrepo_mcp import __version__  # noqa: E402
from chatrepo_mcp.server import mcp  # noqa: E402


async def main() -> None:
    canonical_path = ROOT / "contracts" / "tool-schemas" / "tools.json"
    go_path = ROOT / "go" / "internal" / "contracts" / "tools.json"
    canonical_bytes = canonical_path.read_bytes()
    if canonical_bytes != go_path.read_bytes():
        raise SystemExit("Go embedded contract is stale; run `make contracts`")

    contract = json.loads(canonical_bytes)
    tools = await mcp.list_tools()
    live = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    if live != contract["tools"]:
        raise SystemExit("Python live tool schema differs from the canonical contract")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = (ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    if not match or match.group(1) != version or __version__ != version:
        raise SystemExit("VERSION, Python package version, and __version__ must match")
    if contract["server"]["version"] != version:
        raise SystemExit("contract server version differs from VERSION")
    if contract["server"]["toolCount"] != len(live):
        raise SystemExit("contract toolCount differs from live tool count")
    print(f"contract ok: version={version} tools={len(live)}")


if __name__ == "__main__":
    anyio.run(main)
