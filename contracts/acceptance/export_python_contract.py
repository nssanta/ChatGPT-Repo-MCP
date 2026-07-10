#!/usr/bin/env python3
"""Export the Python implementation's public MCP tool contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anyio


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_SRC = REPO_ROOT / "python" / "src"
sys.path.insert(0, str(PYTHON_SRC))
os.environ.setdefault("PROJECT_ROOT", str(REPO_ROOT))

from chatrepo_mcp.server import mcp  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "contracts" / "tool-schemas" / "tools.json",
    )
    return parser.parse_args()


async def export_contract() -> dict[str, object]:
    tools = await mcp.list_tools()
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contractVersion": 1,
        "server": {
            "name": "chatrepo-mcp",
            "version": version,
            "toolCount": len(tools),
        },
        "tools": [
            tool.model_dump(by_alias=True, exclude_none=True)
            for tool in sorted(tools, key=lambda item: item.name)
        ],
    }


def main() -> None:
    args = parse_args()
    payload = anyio.run(export_contract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
