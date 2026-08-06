"""Run a black-box acceptance smoke against both packaged implementations."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CONTRACT = json.loads(
    (ROOT / "contracts" / "tool-schemas" / "tools.json").read_text(encoding="utf-8")
)
PTY_TOOLS = {
    "start_terminal_session", "read_terminal_session", "write_terminal_session",
    "resize_terminal_session", "close_terminal_session", "list_terminal_sessions",
}
OUTPUT_VALIDATORS = {
    tool["name"]: Draft202012Validator(tool["outputSchema"])
    for tool in CANONICAL_CONTRACT["tools"]
}


def normalize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = json.loads(json.dumps(tools))
    for tool in normalized:
        annotations = tool.setdefault("annotations", {})
        annotations.setdefault("readOnlyHint", False)
        annotations.setdefault("destructiveHint", True)
        annotations.setdefault("idempotentHint", False)
        annotations.setdefault("openWorldHint", True)
    return normalized


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
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, arguments)
        if not result.content or not hasattr(result.content[0], "text"):
            raise AssertionError(f"{name} returned no JSON text content")
        payload = json.loads(result.content[0].text)
        errors = sorted(OUTPUT_VALIDATORS[name].iter_errors(payload), key=lambda item: list(item.path))
        if errors:
            details = "; ".join(
                f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
                for error in errors[:5]
            )
            raise AssertionError(f"{name} returned output outside its canonical schema: {details}")
        return payload


async def inspect(url: str) -> dict[str, Any]:
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tools = [
            tool.model_dump(by_alias=True, exclude_none=True)
            for tool in sorted(listed.tools, key=lambda item: item.name)
        ]
        return {"tools": normalize_tools(tools)}


async def walk_artifact(
    url: str, artifact_id: str, *, expected_payload_type: str = "records",
) -> dict[str, Any]:
    cursor: str | None = None
    records: list[dict[str, Any]] = []
    text_parts: list[str] = []
    pages: list[dict[str, Any]] = []
    expected_start = 0
    for _ in range(32):
        page = await call(url, "read_artifact", {
            "artifact_id": artifact_id,
            "cursor": cursor,
            "max_bytes": 65_536,
        })
        assert page["ok"] is True
        assert page["artifact_id"] == artifact_id
        if not isinstance(page.get("payload"), dict):
            raise TypeError(f"read_artifact returned untyped payload: {page!r}")
        payload_type = page["payload"].get("type")
        if expected_payload_type and payload_type != expected_payload_type:
            raise AssertionError(
                "artifact payload type mismatch: "
                f"type={payload_type!r} expected={expected_payload_type!r} "
                f"metadata={page.get('metadata')!r}"
            )
        pages.append(page)
        ordering = page["metadata"]["ordering"]
        assert ordering in {"stdout_then_stderr", "capture_order"}
        assert page["byte_range"]["start"] == expected_start
        assert page["byte_range"]["end"] >= expected_start
        expected_start = page["byte_range"]["end"]
        if payload_type == "records":
            records.extend(page["payload"]["records"])
        elif payload_type == "text":
            text_parts.append(page["payload"]["text"])
        else:
            raise AssertionError(f"unsupported acceptance payload type: {payload_type!r}")
        has_more, eof, next_cursor = page["has_more"], page["eof"], page["next_cursor"]
        assert has_more is (not eof)
        receipt = page["receipt"]
        assert receipt["status"] == ("partial" if has_more else "completed")
        assert receipt["completeness"] == ("partial" if has_more else "complete")
        assert receipt["reason"] == ("inline_limit" if has_more else "none")
        if not has_more:
            assert next_cursor is None
            return {"records": records, "text": "".join(text_parts), "pages": pages}
        assert isinstance(next_cursor, str) and next_cursor and not next_cursor.isdigit()
        assert next_cursor != cursor
        cursor = next_cursor
    raise AssertionError("read_artifact did not reach eof within 32 bounded pages")


def assert_declared_record_order(records: list[dict[str, Any]], ordering: str) -> None:
    if ordering == "stdout_then_stderr":
        ranks = [{"stdout": 0, "stderr": 1}[record["stream"]] for record in records]
        assert ranks == sorted(ranks)
    elif ordering != "capture_order":
        raise AssertionError(f"unexpected command artifact ordering: {ordering!r}")


async def verify_mixed_stream_artifact(url: str, runtime: str) -> None:
    lines = 3_000
    source = (
        "import sys;"
        f"[(sys.stdout.write(f'mix-out-{{i:04d}}\\n'),sys.stderr.write(f'mix-err-{{i:04d}}\\n')) for i in range({lines})]"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    result = await call(url, "run_command", {"command": command, "max_output_chars": 1_024})
    assert result["ok"] is True
    assert result.get("full_output_truncated", result.get("output_truncated")) is True
    walk = await walk_artifact(url, result["artifact"]["artifact_id"])
    records = walk["records"]
    streams = {record["stream"] for record in records}
    assert streams == {"stdout", "stderr"}
    stdout = "".join(record["data"] for record in records if record["stream"] == "stdout")
    stderr = "".join(record["data"] for record in records if record["stream"] == "stderr")
    expected_stdout = "".join(f"mix-out-{index:04d}\n" for index in range(lines))
    expected_stderr = "".join(f"mix-err-{index:04d}\n" for index in range(lines))
    if stdout != expected_stdout or stderr != expected_stderr:
        raise AssertionError(
            f"{runtime} mixed stream reconstruction mismatch: "
            f"stdout={len(stdout)}/{len(expected_stdout)} stderr={len(stderr)}/{len(expected_stderr)}"
        )
    ordering = walk["pages"][-1]["metadata"]["ordering"]
    assert_declared_record_order(records, ordering)


async def verify_saturated_mixed_batch(url: str, runtime: str, capacity: int) -> None:
    jobs: list[str] = []
    try:
        for _ in range(capacity):
            started = await call(url, "start_command_job", {"command": "sleep 10"})
            if started.get("ok") is not True:
                raise AssertionError(f"{runtime} could not saturate heavy capacity: {started!r}")
            jobs.append(started["job_id"])
        result = await call(url, "batch_call", {
            "calls": [
                {"tool": "file_metadata", "args": {"path": "hello.txt"}},
                {"tool": "git_diff", "args": {}},
            ],
            "max_concurrency": 6,
        })
        items = result["results"]
        assert result["ok"] is False
        assert [item["index"] for item in items] == [0, 1]
        assert items[0]["ok"] is True
        assert items[1]["ok"] is False
        busy = items[1]["result"]
        assert busy["ok"] is False
        assert busy["error_kind"] == "resource_busy"
        assert busy["capacity"] == capacity
        assert "applied_capacity" not in busy
    finally:
        await asyncio.gather(*(
            call(url, "cancel_command_job", {"job_id": job_id})
            for job_id in jobs
        ))


async def verify_pty_lifecycle(url: str, runtime: str, tool_names: set[str]) -> None:
    terminal_tools = {"start_terminal_session", "read_terminal_session", "close_terminal_session"}
    if not terminal_tools.issubset(tool_names):
        return
    lines = 3_000
    source = (
        "import sys,time;"
        f"marker='pty-'+'durable-line\\n';sys.stdout.write(marker*{lines});"
        "sys.stdout.flush();time.sleep(30)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    started = await call(url, "start_terminal_session", {"command": command, "idle_timeout_ms": 60_000})
    assert started["ok"] is True
    session_id = started["session_id"]
    artifact = started["artifact"]
    artifact_id = artifact["artifact_id"]
    assert artifact["has_more"] is True
    assert artifact["eof"] is False
    assert artifact["continuation"] == {
        "tool": "read_artifact", "arguments": {"artifact_id": artifact_id},
    }
    receipt = artifact["receipt"]
    assert receipt["status"] == "partial"
    assert receipt["completeness"] == "partial"
    assert receipt["reason"] == "source_active"
    assert receipt["applied"]["source_complete"] is False
    cursor = 0
    live_parts: list[str] = []
    try:
        for _ in range(64):
            page = await call(url, "read_terminal_session", {
                "session_id": session_id, "cursor": cursor, "max_bytes": 4_096, "wait_ms": 500,
            })
            assert page["ok"] is True
            data = page["data"]
            assert len(data.encode()) <= 4_096
            live_parts.append(data)
            next_cursor = page["next_cursor"]
            assert next_cursor >= cursor
            cursor = next_cursor
            if "".join(live_parts).count("pty-durable-line") >= lines:
                break
        else:
            observed = "".join(live_parts)
            raise AssertionError(
                f"{runtime} PTY output was not fully readable before close: "
                f"markers={observed.count('pty-durable-line')}/{lines} bytes={len(observed.encode())}"
            )
    finally:
        closed = await call(url, "close_terminal_session", {
            "session_id": session_id, "grace_ms": 500, "force": True,
        })
        assert closed["ok"] is True

    final_page: dict[str, Any] | None = None
    for _ in range(50):
        candidate = await call(url, "read_artifact", {"artifact_id": artifact_id, "max_bytes": 1})
        if candidate.get("ok") and candidate["metadata"].get("sha256") is not None:
            final_page = candidate
            break
        await asyncio.sleep(0.1)
    if final_page is None:
        raise AssertionError(f"{runtime} PTY artifact did not finalize after close")
    walk = await walk_artifact(url, artifact_id, expected_payload_type="")
    records = walk["records"]
    if records:
        assert {record["stream"] for record in records} == {"combined"}
        durable = "".join(record["data"] for record in records)
    else:
        durable = walk["text"]
    assert durable.count("pty-durable-line") == lines
    assert walk["pages"][-1]["metadata"]["ordering"] == "capture_order"


async def verify(python_url: str, go_url: str, fixture: Path) -> None:
    python_meta, go_meta = await asyncio.gather(inspect(python_url), inspect(go_url))
    if python_meta["tools"] != go_meta["tools"]:
        python_schema = json.dumps(python_meta["tools"], indent=2, sort_keys=True).splitlines()
        go_schema = json.dumps(go_meta["tools"], indent=2, sort_keys=True).splitlines()
        difference = "\n".join(difflib.unified_diff(python_schema, go_schema, "python", "go", n=3))
        raise AssertionError(f"Python/Go live tool schemas differ:\n{difference}")
    assert len(python_meta["tools"]) == 90

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

    batch_arguments = {
        "calls": [{"tool": "repo_info", "args": {}} for _ in range(6)],
        "max_concurrency": 6,
    }
    python_batch, go_batch = await asyncio.gather(
        call(python_url, "batch_call", batch_arguments),
        call(go_url, "batch_call", batch_arguments),
    )
    for result in (python_batch, go_batch):
        assert result["ok"] is True
        assert result["requested_max_concurrency"] == 6
        assert result["applied_max_concurrency"] == 6
        assert result["heavy_capacity"] >= 1
        assert result["resource_profile"] in {"small", "medium", "large", "custom"}
        assert [item["index"] for item in result["results"]] == list(range(6))

    await asyncio.gather(
        verify_saturated_mixed_batch(python_url, "python", python_batch["heavy_capacity"]),
        verify_saturated_mixed_batch(go_url, "go", go_batch["heavy_capacity"]),
    )

    inline_command = (
        f"{shlex.quote(sys.executable)} -c \"import sys;"
        "sys.stdout.write('A'*40000+'Z'*40000);"
        "sys.stderr.write('B'*40000+'Y'*40000)\""
    )
    python_inline, go_inline = await asyncio.gather(
        call(python_url, "run_command", {"command": inline_command, "parse_kind": "none"}),
        call(go_url, "run_command", {"command": inline_command, "parse_kind": "none"}),
    )
    for result in (python_inline, go_inline):
        assert result["ok"] is True
        returned = len(result["stdout"].encode()) + len(result["stderr"].encode())
        assert returned <= 65_536
        assert result["stdout"].startswith("A" * 100)
        assert result["stdout"].endswith("Z" * 100)
        assert result["stderr"].startswith("B" * 100)
        assert result["stderr"].endswith("Y" * 100)
        assert result["receipt"]["configured"]["inline_output_bytes"] == 65_536
        assert result["receipt"]["applied"]["inline_output_bytes"] == 65_536
        returned_receipt = result["receipt"]["returned"]
        assert returned_receipt["stdout_bytes"] + returned_receipt["stderr_bytes"] == returned

    python_command, go_command = await asyncio.gather(
        call(python_url, "run_command", {"command": "git diff", "max_output_chars": 1_024}),
        call(go_url, "run_command", {"command": "git diff", "max_output_chars": 1_024}),
    )
    for result in (python_command, go_command):
        assert result["ok"] is True
        assert result.get("full_output_truncated", result.get("output_truncated")) is True
        assert result["artifact"]["artifact_id"] == result["log_id"]
        expected_continuation = {
            "tool": "read_artifact",
            "arguments": {"artifact_id": result["log_id"]},
        }
        if result.get("continuation") != expected_continuation:
            raise AssertionError(f"run_command continuation mismatch: {result!r}")

    python_walk, go_walk = await asyncio.gather(
        walk_artifact(python_url, python_command["artifact"]["artifact_id"]),
        walk_artifact(go_url, go_command["artifact"]["artifact_id"]),
    )
    expected_process = await asyncio.to_thread(
        subprocess.run,
        ["git", "diff"],
        cwd=fixture,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_stdout = expected_process.stdout.replace(
        "token=dual-acceptance-secret", "token=<redacted>",
    )
    for runtime, walk in (("python", python_walk), ("go", go_walk)):
        records = walk["records"]
        assert records
        assert all(record["stream"] in {"stdout", "stderr"} for record in records)
        stdout = "".join(record["data"] for record in records if record["stream"] == "stdout")
        if stdout != expected_stdout:
            mismatch = next(
                (index for index, pair in enumerate(zip(stdout, expected_stdout)) if pair[0] != pair[1]),
                min(len(stdout), len(expected_stdout)),
            )
            raise AssertionError(
                f"{runtime} reconstructed stdout differs at {mismatch}; "
                f"actual_len={len(stdout)} expected_len={len(expected_stdout)} "
                f"actual_sha={hashlib.sha256(stdout.encode()).hexdigest()} "
                f"expected_sha={hashlib.sha256(expected_stdout.encode()).hexdigest()} "
                f"actual_context={stdout[max(0, mismatch-40):mismatch+80]!r} "
                f"expected_context={expected_stdout[max(0, mismatch-40):mismatch+80]!r}"
            )
        logical = "".join(record["data"] for record in records).encode()
        metadata = walk["pages"][-1]["metadata"]
        assert metadata["size_bytes"] == len(logical)
        assert metadata["sha256"] == hashlib.sha256(logical).hexdigest()
        assert "dual-acceptance-secret" not in logical.decode()
        created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(metadata["expires_at"].replace("Z", "+00:00"))
        assert expires > created

    await asyncio.gather(
        verify_mixed_stream_artifact(python_url, "python"),
        verify_mixed_stream_artifact(go_url, "go"),
    )

    active_command = "printf 'active-artifact\\n'; sleep 3"
    python_job, go_job = await asyncio.gather(
        call(python_url, "start_command_job", {"command": active_command}),
        call(go_url, "start_command_job", {"command": active_command}),
    )
    for runtime, url, job in (("python", python_url, python_job), ("go", go_url, go_job)):
        assert job["ok"] is True
        artifact_id = job["artifact"]["artifact_id"]
        active_page: dict[str, Any] | None = None
        candidate: dict[str, Any] | None = None
        for _ in range(30):
            candidate = await call(url, "read_artifact", {"artifact_id": artifact_id})
            if candidate.get("ok"):
                active_page = candidate
                break
            await asyncio.sleep(0.05)
        if active_page is None:
            raise AssertionError(f"{runtime} active artifact unavailable: {candidate!r}")
        if active_page["metadata"]["sha256"] is not None:
            raise AssertionError(
                f"{runtime} active artifact exposed a final digest: "
                f"metadata={active_page['metadata']!r} receipt={active_page['receipt']!r}"
            )
        assert active_page["receipt"]["status"] == "partial"
        assert active_page["receipt"]["completeness"] == "partial"
        assert active_page["receipt"]["reason"] == "unknown"
        assert any("sha256" in warning and "complete" in warning for warning in active_page["receipt"]["warnings"])


async def verify_full_contract(python_url: str, go_url: str) -> None:
    python_meta, go_meta = await asyncio.gather(inspect(python_url), inspect(go_url))
    expected = normalize_tools(CANONICAL_CONTRACT["tools"])
    expected_count = CANONICAL_CONTRACT["server"]["toolCount"]
    python_names = {tool["name"] for tool in python_meta["tools"]}
    go_names = {tool["name"] for tool in go_meta["tools"]}
    python_has_pty = PTY_TOOLS.issubset(python_names)
    go_has_pty = PTY_TOOLS.issubset(go_names)
    if python_has_pty is not go_has_pty:
        raise AssertionError("Python/Go full-mode PTY registration differs")
    live_expected = expected if python_has_pty else [
        tool for tool in expected if tool["name"] not in PTY_TOOLS
    ]
    if python_meta["tools"] != live_expected:
        raise AssertionError("Python full-mode live schema differs from the canonical contract")
    if go_meta["tools"] != live_expected:
        raise AssertionError("Go full-mode live schema differs from the canonical contract")
    if len(expected) != expected_count:
        raise AssertionError(
            f"canonical toolCount={expected_count} but tools contains {len(expected)} entries"
        )
    if expected_count <= 90:
        raise AssertionError(
            "canonical full-mode contract does not include the expanded tool surface"
        )
    await asyncio.gather(
        verify_pty_lifecycle(python_url, "python", python_names),
        verify_pty_lifecycle(go_url, "go", go_names),
    )


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
        with (fixture / "hello.txt").open("a", encoding="utf-8") as handle:
            handle.write("artifact-page-token\n" * 30_000)
            handle.write("token=dual-acceptance-secret redaction-proof\n")

        python_port, go_port = free_port(), free_port()
        base_env = {
            **os.environ,
            "PROJECT_ROOT": str(fixture),
            "HOST": "127.0.0.1",
            "ACCESS_MODE": "safe",
            "MCP_AUTH_MODE": "none",
            "ALLOWED_HOSTS": "127.0.0.1:*,localhost:*",
            "DANGEROUSLY_ALLOW_ALL_WRITES": "true",
            "COMMAND_POLICY_MODE": "full_repo",
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

        full_python_port, full_go_port = free_port(), free_port()
        full_base_env = {
            **base_env,
            "ACCESS_MODE": "full",
            "ENABLE_PTY": "true",
        }
        full_python_env = {
            **full_base_env,
            "PORT": str(full_python_port),
            "PYTHONPATH": str(ROOT / "python" / "src"),
        }
        full_go_env = {**full_base_env, "PORT": str(full_go_port)}
        with ExitStack() as stack:
            python_process = subprocess.Popen(
                [sys.executable, "-m", "chatrepo_mcp"], cwd=ROOT, env=full_python_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            go_process = subprocess.Popen(
                [str(go_binary)], cwd=ROOT, env=full_go_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            stack.callback(lambda: python_process.kill() if python_process.poll() is None else None)
            stack.callback(lambda: go_process.kill() if go_process.poll() is None else None)
            wait_for_port(full_python_port, python_process)
            wait_for_port(full_go_port, go_process)
            asyncio.run(
                verify_full_contract(
                    f"http://127.0.0.1:{full_python_port}/mcp",
                    f"http://127.0.0.1:{full_go_port}/mcp",
                )
            )
    print("dual-server acceptance ok: 90 safe tools, full canonical tools, and core behavior")


if __name__ == "__main__":
    main()
