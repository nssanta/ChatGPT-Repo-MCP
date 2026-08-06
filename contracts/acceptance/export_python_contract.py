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

from bounded_output_contract import (
    BOUNDED_OUTPUT_RESULT_SCHEMAS,
)
from chatrepo_mcp.server import mcp


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
    live_tools = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "contractVersion": 3,
        "server": {
            "name": "chatrepo-mcp",
            "version": version,
            "toolCount": len(live_tools),
        },
        "resultSchemas": BOUNDED_OUTPUT_RESULT_SCHEMAS,
        "tools": live_tools,
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
