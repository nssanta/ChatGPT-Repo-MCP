from __future__ import annotations

import hashlib
import io
from pathlib import Path

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ResourceLink

import chatrepo_mcp.file_transfer as file_transfer
import chatrepo_mcp.server as server_module
from chatrepo_mcp.config import Settings
from chatrepo_mcp.server import mcp


def transfer_settings(tmp_path: Path, monkeypatch, *, import_limit: int = 1024, export_limit: int = 1024) -> Settings:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("COMPUTER_USE_ENABLED", raising=False)
    monkeypatch.delenv("COMPUTER_CONTROL_ENABLED", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("ACCESS_MODE", "safe")
    monkeypatch.setenv("WRITABLE_GLOBS", "**/*")
    monkeypatch.setenv("DANGEROUSLY_ALLOW_ALL_WRITES", "true")
    monkeypatch.setenv("COMMAND_JOBS_DIR", str(tmp_path / ".jobs"))
    monkeypatch.setenv("COMMAND_AUDIT_LOG_PATH", str(tmp_path / ".audit" / "commands.log"))
    monkeypatch.setenv("FILE_TRANSFER_IMPORT_MAX_BYTES", str(import_limit))
    monkeypatch.setenv("FILE_TRANSFER_EXPORT_MAX_BYTES", str(export_limit))
    return Settings.from_env()


def test_export_binary_file_and_read_resource(tmp_path: Path, monkeypatch) -> None:
    settings = transfer_settings(tmp_path, monkeypatch)
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
    path = tmp_path / "image.png"
    path.write_bytes(payload)

    result = file_transfer.export_file_to_chat(path="image.png", settings=settings)

    assert result["ok"] is True
    assert result["size_bytes"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["mime_type"] == "image/png"
    token = str(result["resource_uri"]).removeprefix("chatrepo-file://local/")
    assert file_transfer.read_export_resource(token=token, settings=settings) == payload


def test_export_respects_secret_and_size_limits(tmp_path: Path, monkeypatch) -> None:
    settings = transfer_settings(tmp_path, monkeypatch, export_limit=3)
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"1234")

    secret = file_transfer.export_file_to_chat(path=".env", settings=settings)
    large = file_transfer.export_file_to_chat(path="blob.bin", settings=settings)

    assert secret["ok"] is False
    assert secret["error_kind"] == "path_traversal_or_blocked"
    assert large["ok"] is False
    assert large["error_kind"] == "payload_too_large"


def test_receive_chat_file_dry_run_does_not_download(tmp_path: Path, monkeypatch) -> None:
    settings = transfer_settings(tmp_path, monkeypatch)
    result = file_transfer.receive_chat_file(
        file={
            "download_url": "https://files.example.invalid/file",
            "file_id": "file_123",
            "mime_type": "application/octet-stream",
            "file_name": "blob.bin",
        },
        destination_path="incoming/blob.bin",
        settings=settings,
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["size_bytes"] is None
    assert not (tmp_path / "incoming" / "blob.bin").exists()


def test_receive_chat_file_streams_binary_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    settings = transfer_settings(tmp_path, monkeypatch)
    payload = b"binary\x00payload\xff"

    class FakeResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Length": str(len(payload))}
            self._stream = io.BytesIO(payload)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def geturl(self) -> str:
            return "https://files.example.test/file"

        def read(self, size: int = -1) -> bytes:
            return self._stream.read(size)

    class FakeOpener:
        def open(self, request, timeout: int):
            assert request.full_url == "https://files.example.test/file"
            assert timeout == 60
            return FakeResponse()

    monkeypatch.setattr(file_transfer, "_validate_public_https_url", lambda _url: None)
    monkeypatch.setattr(file_transfer.urllib.request, "build_opener", lambda *_handlers: FakeOpener())

    args = {
        "file": {
            "download_url": "https://files.example.test/file",
            "file_id": "file_456",
            "file_name": "payload.bin",
            "mime_type": "application/octet-stream",
        },
        "destination_path": "incoming/payload.bin",
        "settings": settings,
        "dry_run": False,
    }
    result = file_transfer.receive_chat_file(**args)

    target = tmp_path / "incoming" / "payload.bin"
    assert result["ok"] is True
    assert result["size_bytes"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert target.read_bytes() == payload

    second = file_transfer.receive_chat_file(**args)
    assert second["ok"] is False
    assert second["error_kind"] == "destination_exists"
    assert target.read_bytes() == payload


def test_download_url_rejects_private_address() -> None:
    try:
        file_transfer._validate_public_https_url("https://127.0.0.1/file")
    except file_transfer.FileTransferError as exc:
        assert exc.kind == "download_host_not_public"
    else:
        raise AssertionError("private download address must be rejected")


def test_chat_file_schema_and_metadata_are_openai_compatible() -> None:
    async def inspect() -> None:
        tools = await mcp.list_tools()
        receive = next(tool for tool in tools if tool.name == "receive_chat_file")
        assert receive.meta == {"openai/fileParams": ["file"]}

        schema = receive.inputSchema
        file_schema = schema["$defs"]["ChatFileParam"]
        assert file_schema["required"] == ["download_url", "file_id"]
        assert set(file_schema["properties"]) == {"download_url", "file_id", "mime_type", "file_name"}
        for prop in file_schema["properties"].values():
            assert prop["type"] == "string"

        export = next(tool for tool in tools if tool.name == "export_file_to_chat")
        assert export.annotations.readOnlyHint is True

    anyio.run(inspect)


def test_python_mcp_resource_link_round_trip(tmp_path: Path, monkeypatch) -> None:
    settings = transfer_settings(tmp_path, monkeypatch, export_limit=4096)
    payload = b"mcp-binary\x00payload\xff"
    (tmp_path / "result.bin").write_bytes(payload)
    monkeypatch.setattr(server_module, "settings", settings)

    async def exercise() -> None:
        async with create_connected_server_and_client_session(server_module.mcp) as session:
            result = await session.call_tool("export_file_to_chat", {"path": "result.bin"})
            assert result.isError is False
            link = next(item for item in result.content if isinstance(item, ResourceLink))
            resource = await session.read_resource(link.uri)
            assert len(resource.contents) == 1
            assert resource.contents[0].blob is not None
            import base64

            assert base64.b64decode(resource.contents[0].blob) == payload

    anyio.run(exercise)


def test_computer_snapshot_materializes_in_private_cache_and_uses_file_resource(
    tmp_path: Path, monkeypatch
) -> None:
    import base64
    import os

    settings = transfer_settings(tmp_path / "workspace", monkeypatch, export_limit=4096)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    payload = b"\x89PNG\r\n\x1a\ncomputer-snapshot"

    result = file_transfer.materialize_computer_snapshot(
        result={
            "ok": True,
            "snapshot_id": "abcdef1234567890",
            "mime_type": "image/png",
            "image_b64": base64.b64encode(payload).decode(),
        },
        settings=settings,
    )

    target = Path(result["path"])
    assert result["ok"] is True
    assert target.parent == tmp_path / "cache" / "chatrepo-mcp" / "shared-screens"
    assert target.name.startswith("computer-snapshot-abcdef12-")
    assert target.suffix == ".png"
    assert target.read_bytes() == payload
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600

    token = str(result["resource_uri"]).removeprefix("chatrepo-file://local/")
    assert file_transfer.read_export_resource(token=token, settings=settings) == payload

    removed = file_transfer.cleanup_computer_shares(now=target.stat().st_mtime + 301)
    assert removed >= 1
    assert not target.exists()
