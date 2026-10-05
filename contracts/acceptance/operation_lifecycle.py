"""Real MCP clients verify session attribution, unmetered walks and cancellation."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.client.streamable_http import streamablehttp_client


async def lifecycle_acceptance(url: str, fixture: Path) -> None:
    scan = fixture / "operation-fixture"
    scan.mkdir(exist_ok=True)
    for directory in range(32):
        target = scan / str(directory)
        target.mkdir(exist_ok=True)
        for file in range(128):
            (target / f"{file}.txt").write_text("needle\n", encoding="utf-8")

    async with AsyncExitStack() as stack:
        async def connect() -> ClientSession:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(url))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return session

        first, second = await connect(), await connect()

        async def call(session: ClientSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
            result = await asyncio.wait_for(session.call_tool(name, args), 10)
            assert result.content and hasattr(result.content[0], "text"), result
            return json.loads(result.content[0].text)

        async def wait_operation(tool: str, *, kind: str = "tool") -> dict[str, Any]:
            for _ in range(200):
                listing = await call(second, "list_operations", {})
                for op in listing["operations"]:
                    if op["tool"] == tool and op["kind"] == kind:
                        return op
                await asyncio.sleep(0.01)
            raise AssertionError(f"{tool} was never observable")

        # Repeated paths make the test deterministic without a large real data tree.
        work = asyncio.create_task(call(first, "recent_changes", {"paths": [str(scan)] * 500, "limit": 5}))
        op = await wait_operation("recent_changes")
        assert op["session_id"] and op["server_instance_id"]
        assert (await call(second, "list_heavy_operations", {}))["used"] == 0
        mine = await call(first, "list_operations", {"scope": "session"})
        theirs = await call(second, "list_operations", {"scope": "session"})
        assert op["operation_id"] in {row["operation_id"] for row in mine["operations"]}
        assert op["operation_id"] not in {row["operation_id"] for row in theirs["operations"]}
        await call(second, "cancel_operation", {"operation_id": op["operation_id"]})
        await call(second, "cancel_operation", {"operation_id": op["operation_id"]})
        result = await work
        assert result["error_kind"] == "operation_cancelled", result
        final = await call(second, "get_operation", {"operation_id": op["operation_id"]})
        assert final["operation"]["status"] == "cancelled", final
        assert final["operation"]["progress"]["directories"] > 0

        calls = [{"tool": "recent_changes", "args": {"paths": [str(scan)] * 500, "limit": 5}} for _ in range(4)]
        batch = asyncio.create_task(call(first, "batch_call", {"calls": calls, "max_concurrency": 2}))
        parent = await wait_operation("batch_call")
        await wait_operation("recent_changes")
        listing = await call(second, "list_operations", {})
        assert any(child["parent_operation_id"] == parent["operation_id"] for child in listing["operations"])
        await call(second, "cancel_operation", {"operation_id": parent["operation_id"]})
        assert (await batch)["error_kind"] == "operation_cancelled"

        # Detaching jobs must survive the launcher's completion and keep ownership.
        job = await call(first, "start_command_job", {"command": "sleep 30", "cwd": str(scan)})
        assert job["ok"], job
        child = await wait_operation("start_command_job", kind="job")
        assert child["session_id"] == op["session_id"] and child["resource_id"] == job["job_id"]
        await call(second, "cancel_operation", {"operation_id": child["operation_id"]})
        for _ in range(200):
            state = (await call(second, "get_operation", {"operation_id": child["operation_id"]}))["operation"]
            if state["status"] not in {"running", "cancelling", "queued"}:
                break
            await asyncio.sleep(0.01)
        assert state["status"] == "cancelled", state

        tool_names = {tool.name for tool in (await first.list_tools()).tools}
        if "start_terminal_session" in tool_names:
            terminal = await call(first, "start_terminal_session", {"command": "sleep 30", "cwd": str(scan)})
            assert terminal["ok"], terminal
            child = await wait_operation("start_terminal_session", kind="terminal")
            assert child["resource_id"] == terminal["session_id"]
            await call(second, "cancel_operation", {"operation_id": child["operation_id"]})
            for _ in range(300):
                state = (await call(second, "get_operation", {"operation_id": child["operation_id"]}))["operation"]
                if state["status"] not in {"running", "cancelling", "queued"}:
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == "cancelled", state

        if os.name == "posix":
            # Target a real JSON-RPC request, independently of operation-ID cancellation.
            request_id = first._request_id
            work = asyncio.create_task(call(first, "recent_changes", {"paths": [str(scan)] * 500, "limit": 5}))
            target = await wait_operation("recent_changes")
            await first.send_notification(types.ClientNotification(types.CancelledNotification(
                params=types.CancelledNotificationParams(requestId=request_id, reason="acceptance cancellation"),
            )))
            try:
                result = await work
                assert result["error_kind"] == "operation_cancelled", result
            except McpError:
                pass  # Python SDK returns its request-cancelled protocol error.
            for _ in range(200):
                state = (await call(second, "get_operation", {"operation_id": target["operation_id"]}))["operation"]
                if state["status"] not in {"running", "cancelling", "queued"}:
                    break
                await asyncio.sleep(0.01)
            assert state["status"] == "cancelled" and state["cancel_requested"], state

        # Empty lists after cancellation are evidence of completion, not just request acknowledgement.
        assert not (await call(second, "list_operations", {}))["operations"]
        assert (await call(second, "list_heavy_operations", {}))["used"] == 0
        assert (await call(second, "list_command_jobs", {"include_finished": False}))["count"] == 0
        if "list_terminal_sessions" in tool_names:
            assert (await call(second, "list_terminal_sessions", {"include_finished": False}))["count"] == 0


async def shutdown_acceptance(url: str, fixture: Path, process: Any) -> None:
    """SIGTERM must cancel a long walk before HTTP waits for request completion."""
    import contextlib
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        headers = {"Accept": "application/json, text/event-stream"}
        response = await client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "shutdown-acceptance", "version": "1"},
            },
        })
        response.raise_for_status()
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        await client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        scan = str(fixture / "operation-fixture")
        request = asyncio.create_task(client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "recent_changes", "arguments": {"paths": [scan] * 500, "limit": 5},
            },
        }))
        for _ in range(200):
            response = await client.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "list_operations", "arguments": {},
                },
            })
            payload = response.json()["result"]
            listing = json.loads(payload["content"][0]["text"])
            if any(op["tool"] == "recent_changes" for op in listing["operations"]):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("shutdown fixture never started")
        process.terminate()
        await asyncio.to_thread(process.wait, 10)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
            await request
