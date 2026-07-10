#!/usr/bin/env python3
"""Run a black-box acceptance smoke against both packaged implementations."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode}")
        with socket.socket() as client:
            client.settimeout(0.2)
            if client.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"server did not listen on port {port}")


async def call(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if not result.content or not hasattr(result.content[0], "text"):
                raise AssertionError(f"{name} returned no JSON text content")
            return json.loads(result.content[0].text)


async def inspect(url: str) -> dict[str, Any]:
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            return {"tools": sorted(tool.name for tool in listed.tools)}


async def verify(python_url: str, go_url: str, fixture: Path) -> None:
    python_meta, go_meta = await asyncio.gather(inspect(python_url), inspect(go_url))
    assert python_meta["tools"] == go_meta["tools"]
    assert len(python_meta["tools"]) == 89

    for name, arguments in (
        ("list_dir", {"path": ".", "include_hidden": False}),
        ("read_text_file", {"path": "hello.txt", "with_line_numbers": False}),
        ("search_text", {"query": "shared-token", "path": ".", "regex": False}),
        ("command_policy_check", {"command": "git status --short"}),
        ("git_status", {"short": True}),
        ("create_text_file", {"path": "preview.txt", "content": "preview", "dry_run": True}),
    ):
        python_result, go_result = await asyncio.gather(
            call(python_url, name, arguments), call(go_url, name, arguments)
        )
        if python_result.get("ok") is False or go_result.get("ok") is False:
            raise AssertionError(
                f"{name} failed: python={python_result!r} go={go_result!r}"
            )
        if name == "list_dir":
            python_names = sorted(item["name"] for item in python_result["entries"])
            go_names = sorted(item["name"] for item in go_result["entries"])
            assert python_names == go_names
        elif name == "read_text_file":
            assert python_result["content"] == go_result["content"]
            assert python_result["sha256"] == go_result["sha256"]
        elif name == "search_text":
            assert python_result["count"] == go_result["count"] == 1
        elif name == "command_policy_check":
            assert python_result["allowed"] is go_result["allowed"] is True
        elif name == "create_text_file":
            assert python_result["dry_run"] is go_result["dry_run"] is True
            assert go_result["applied"] is False
            assert not (fixture / "preview.txt").exists()


def main() -> None:
    go_binary = ROOT / "bin" / ("chatrepo-mcp.exe" if os.name == "nt" else "chatrepo-mcp")
    if not go_binary.exists():
        raise SystemExit("Go binary is missing; run `make build` first")
    with tempfile.TemporaryDirectory(prefix="chatrepo-acceptance-") as temporary:
        fixture = Path(temporary)
        (fixture / "hello.txt").write_text("shared-token\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main", str(fixture)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(fixture), "config", "user.email", "acceptance@example.com"], check=True)
        subprocess.run(["git", "-C", str(fixture), "config", "user.name", "Acceptance"], check=True)
        subprocess.run(["git", "-C", str(fixture), "add", "hello.txt"], check=True)
        subprocess.run(["git", "-C", str(fixture), "commit", "-m", "fixture"], check=True, capture_output=True)

        python_port, go_port = free_port(), free_port()
        base_env = {
            **os.environ,
            "PROJECT_ROOT": str(fixture),
            "HOST": "127.0.0.1",
            "ACCESS_MODE": "safe",
            "MCP_AUTH_MODE": "none",
            "ALLOWED_HOSTS": "127.0.0.1:*,localhost:*",
            "DANGEROUSLY_ALLOW_ALL_WRITES": "true",
        }
        python_env = {**base_env, "PORT": str(python_port), "PYTHONPATH": str(ROOT / "python" / "src")}
        go_env = {**base_env, "PORT": str(go_port)}
        with ExitStack() as stack:
            python_process = subprocess.Popen(
                [sys.executable, "-m", "chatrepo_mcp"], cwd=ROOT, env=python_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            go_process = subprocess.Popen(
                [str(go_binary)], cwd=ROOT, env=go_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            stack.callback(lambda: python_process.kill() if python_process.poll() is None else None)
            stack.callback(lambda: go_process.kill() if go_process.poll() is None else None)
            wait_for_port(python_port, python_process)
            wait_for_port(go_port, go_process)
            asyncio.run(
                verify(
                    f"http://127.0.0.1:{python_port}/mcp",
                    f"http://127.0.0.1:{go_port}/mcp",
                    fixture,
                )
            )
    print("dual-server acceptance ok: 89 default tools and core behavior")


if __name__ == "__main__":
    main()
