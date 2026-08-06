from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_command_tools import make_settings

from chatrepo_mcp import command_tools
from chatrepo_mcp.command_tools import (
    get_command_job,
    get_command_log,
    run_command,
    start_command_job,
)
from chatrepo_mcp.output_store import (
    ArtifactPersistenceError,
    ArtifactQuotaError,
    BoundedHead,
    BoundedTail,
    OutputArtifact,
    OutputStore,
    StreamingRedactor,
    read_artifact,
    store_for,
)
from chatrepo_mcp.terminal_tools import read_terminal_session, start_terminal_session


def test_streaming_redactor_hides_chunk_boundary_secrets_and_private_keys() -> None:
    source = (
        b"before token=abcdefghijklmnopqrstuvwxyz012345 after\n"
        b"-----BEGIN PRIVATE KEY-----\nPRIVATE-MATERIAL\n-----END PRIVATE KEY-----\n"
        b"Bearer abc.def.ghi end\n"
    )
    redactor = StreamingRedactor(carry_chars=32)
    output = b"".join(redactor.feed(bytes([byte])) for byte in source)
    output += redactor.feed(b"", final=True)

    assert b"abcdefghijklmnopqrstuvwxyz" not in output
    assert b"PRIVATE-MATERIAL" not in output
    assert b"abc.def.ghi" not in output
    assert output.count(b"<redacted>") >= 2
    assert b"[REDACTED PRIVATE KEY]" in output
    assert b"before " in output and b" end" in output


@pytest.mark.parametrize("split", range(1, 96))
def test_streaming_redactor_is_fail_closed_at_every_secret_split(split: int) -> None:
    secret = b"token=" + b"s" * 20_000 + b" end"
    private = b"-----BEGIN PRIVATE KEY-----\n" + b"k" * 20_000 + b"\n-----END PRIVATE KEY-----\n"
    source = secret + private
    redactor = StreamingRedactor(carry_chars=32)
    output = redactor.feed(source[:split]) + redactor.feed(source[split:]) + redactor.feed(b"", final=True)
    assert b"s" * 100 not in output
    assert b"k" * 100 not in output
    assert b"<redacted>" in output
    assert b"[REDACTED PRIVATE KEY]" in output


@pytest.mark.parametrize("chunk_size", [1, 7, 8_191, 8_192, 8_193, 20_000])
def test_streaming_redactor_preserves_safe_suffix_after_large_record_secret(
    chunk_size: int,
) -> None:
    source = b"x" * 8_300 + b"+token=never-leak redaction-proof\n"
    redactor = StreamingRedactor(carry_chars=8_192)
    output = b"".join(
        redactor.feed(source[offset:offset + chunk_size])
        for offset in range(0, len(source), chunk_size)
    )
    output += redactor.feed(b"", final=True)
    assert b"never-leak" not in output
    assert output.endswith(b"+token=<redacted> redaction-proof\n")


@pytest.mark.parametrize(
    ("secret", "marker"),
    [
        (b"token=hidden", b"token=<redacted>"),
        (b"api_key=hidden", b"api_key=<redacted>"),
        (b"Bearer hidden", b"Bearer <redacted>"),
    ],
)
def test_streaming_redactor_final_and_carry_boundaries_keep_marker_and_suffix(
    secret: bytes, marker: bytes,
) -> None:
    source = b"p" * 40 + b"+" + secret + b" safe-tail"
    for split in range(len(source) + 1):
        redactor = StreamingRedactor(carry_chars=32)
        output = redactor.feed(source[:split]) + redactor.feed(source[split:])
        output += redactor.feed(b"", final=True)
        assert b"hidden" not in output
        assert b"+" + marker + b" safe-tail" in output


@pytest.mark.parametrize("limit", range(1, 13))
def test_utf8_head_and_tail_are_valid_at_every_byte_boundary(limit: int) -> None:
    source = "😀яéz"
    encoded = source.encode()
    head = BoundedHead(limit)
    tail = BoundedTail(limit)
    for byte in encoded:
        head.append(bytes([byte]))
        tail.append(bytes([byte]))
    head_text = head.text()
    tail_text = tail.text()
    assert "�" not in head_text + tail_text
    assert source.startswith(head_text)
    assert source.endswith(tail_text)
    assert len(head_text.encode()) <= limit
    assert len(tail_text.encode()) <= limit


def test_run_command_streams_full_redacted_output_to_disk_with_bounded_preview(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", max_command_output_chars=1024,
        artifact_disk_reserve_bytes=0, artifact_total_bytes=128 * 1024 * 1024,
    )
    payload_bytes = 64 * 1024 * 1024
    command = f"{sys.executable} -c \"import sys;sys.stdout.write('x'*{payload_bytes})\""

    tracemalloc.start()
    result = run_command(command, settings, tail_lines=5, parse_kind="none")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result["ok"] is True
    assert result["full_output_truncated"] is True
    assert len(result["stdout"]) <= 1024
    assert result["stdout_bytes"] == payload_bytes
    assert peak < 32 * 1024 * 1024
    log_path = settings.command_jobs_dir / "artifacts" / f"{result['log_id']}.out"
    assert log_path.stat().st_size == payload_bytes
    digest = hashlib.sha256()
    with log_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    assert digest.hexdigest() == result["stdout_sha256"]
    assert result["continuation"] == {
        "tool": "read_artifact", "arguments": {"artifact_id": result["log_id"]},
    }
    assert result["receipt"]["status"] == "partial"
    assert result["receipt"]["completeness"] == "partial"
    assert result["receipt"]["applied"]["source_complete"] is True
    page = get_command_log(result["log_id"], settings, start_line=1, end_line=1)
    assert len(page["content"]) <= settings.max_response_chars


def test_run_command_default_inline_preview_is_head_tail_and_artifact_is_complete(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", max_command_output_chars=200_000,
        default_inline_output_bytes=65_536, artifact_disk_reserve_bytes=0,
    )
    payload = "A" * 50_000 + "ё" * 10_000 + "Z" * 50_000
    command = f"{sys.executable} -c \"import sys;sys.stdout.write({payload!r})\""

    result = run_command(command, settings, parse_kind="none")

    assert result["ok"] is True
    assert len(result["stdout"].encode("utf-8")) <= settings.default_inline_output_bytes
    assert result["stdout"].startswith("A" * 100)
    assert result["stdout"].endswith("Z" * 100)
    assert "full output is available in the artifact" in result["stdout"]
    assert result["receipt"]["applied"]["inline_output_bytes"] == 65_536
    log_path = settings.command_jobs_dir / "artifacts" / f"{result['log_id']}.out"
    digest = hashlib.sha256(log_path.read_bytes()).hexdigest()
    assert digest == result["stdout_sha256"]


def test_run_command_shares_inline_budget_between_stdout_and_stderr(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo",
        default_inline_output_bytes=65_536, artifact_disk_reserve_bytes=0,
    )
    command = (
        f"{sys.executable} -c \"import sys;"
        "sys.stdout.write('A'*40000+'Z'*40000);"
        "sys.stderr.write('B'*40000+'Y'*40000)\""
    )

    result = run_command(command, settings, parse_kind="none")

    assert result["ok"] is True
    returned = len(result["stdout"].encode("utf-8")) + len(result["stderr"].encode("utf-8"))
    assert returned <= settings.default_inline_output_bytes
    assert result["stdout"].startswith("A" * 100) and result["stdout"].endswith("Z" * 100)
    assert result["stderr"].startswith("B" * 100) and result["stderr"].endswith("Y" * 100)
    assert result["receipt"]["returned"]["stdout_bytes"] + result["receipt"]["returned"]["stderr_bytes"] == returned


def test_run_command_direct_zero_output_limit_normalizes_to_default(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0)

    result = run_command("printf xyz", settings, max_output_chars=0, parse_kind="none")

    assert result["ok"] is True
    assert result["stdout"] == "xyz"
    assert result["receipt"]["requested"] == {"inline_output_bytes": 0}
    assert result["receipt"]["applied"]["inline_output_bytes"] == settings.default_inline_output_bytes


def test_four_concurrent_commands_remain_within_global_memory_budget(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", max_command_output_chars=1024,
        artifact_disk_reserve_bytes=0, artifact_total_bytes=256 * 1024 * 1024,
        artifact_max_bytes=64 * 1024 * 1024, max_heavy_operations=4,
    )
    payload_bytes = 16 * 1024 * 1024
    command = f"{sys.executable} -c \"import sys;sys.stdout.write('y'*{payload_bytes})\""

    tracemalloc.start()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_command(command, settings, parse_kind="none"), range(4)))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert all(result["ok"] is True for result in results)
    assert all(result["stdout_bytes"] == payload_bytes for result in results)
    assert peak < 64 * 1024 * 1024


def test_background_job_polling_keeps_full_disk_output_and_bounded_tail(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", max_command_output_chars=2048,
        artifact_disk_reserve_bytes=0, artifact_total_bytes=8 * 1024 * 1024,
    )
    payload_bytes = 2 * 1024 * 1024
    command = f"{sys.executable} -c \"import sys;sys.stdout.write('z'*{payload_bytes})\""
    started = start_command_job(command, settings, timeout_ms=10_000)
    assert started["receipt"]["completeness"] == "partial"
    assert started["receipt"]["applied"]["source_complete"] is False
    assert started["continuation"]["tool"] == "read_artifact"

    deadline = time.time() + 10
    status = get_command_job(started["job_id"], settings)
    while status["status"] == "running" and time.time() < deadline:
        assert len(status["stdout_tail"]) <= settings.max_command_output_chars
        time.sleep(0.02)
        status = get_command_job(started["job_id"], settings)

    assert status["status"] == "completed"
    assert status["stdout_bytes"] == payload_bytes
    assert len(status["stdout_tail"]) <= settings.max_command_output_chars
    assert (settings.command_jobs_dir / "artifacts" / f"{started['job_id']}.out").stat().st_size == payload_bytes


def test_unpolled_background_job_durably_reaches_terminal_metadata(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
    )
    started = start_command_job("printf unpolled", settings, timeout_ms=10_000)
    meta_path = settings.command_jobs_dir / "artifacts" / f"{started['job_id']}.json"
    deadline = time.time() + 5
    while time.time() < deadline:
        meta = json.loads(meta_path.read_text())
        if meta["status"] != "running":
            break
        time.sleep(0.02)
    assert meta["status"] == "completed"
    assert meta["exit_code"] == 0
    assert meta["finished_at"]
    assert meta["termination_reason"] == "completed"


def test_sync_command_cleans_exited_parent_inherited_pipe_descendant(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
        kill_grace_ms=100,
    )
    pid_file = tmp_path / "sync-grandchild.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}"
    started = time.monotonic()
    result = run_command(command, settings, timeout_ms=10_000, parse_kind="none")
    assert time.monotonic() - started < 5
    descendant_pid = int(pid_file.read_text())
    assert result["ok"] is True
    assert result["process_group_cleaned"] is True
    assert result["drain_cleanup"] != "not_needed"
    assert not os.path.exists(f"/proc/{descendant_pid}")
    manifest = json.loads(
        (settings.command_jobs_dir / "artifacts" / f"{result['log_id']}.json").read_text()
    )
    assert manifest["complete"] is True
    assert manifest["process_group_cleaned"] is True


def test_unpolled_background_job_cleans_inherited_pipe_descendant(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
        kill_grace_ms=100,
    )
    pid_file = tmp_path / "background-grandchild.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}"
    started = start_command_job(command, settings, timeout_ms=10_000)
    meta_path = settings.command_jobs_dir / "artifacts" / f"{started['job_id']}.json"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        meta = json.loads(meta_path.read_text())
        if meta["status"] != "running":
            break
        time.sleep(0.02)
    descendant_pid = int(pid_file.read_text())
    assert meta["status"] == "completed"
    assert meta["complete"] is True
    assert meta["process_group_cleaned"] is True
    assert meta["drain_cleanup"] != "not_needed"
    assert not os.path.exists(f"/proc/{descendant_pid}")
    assert not store_for(settings)._is_runtime_owned(started["job_id"])


def test_polling_inherited_pipe_job_never_publishes_transient_completion(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
        kill_grace_ms=100,
    )
    pid_file = tmp_path / "polled-grandchild.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}"
    started = start_command_job(command, settings, timeout_ms=10_000)
    observed = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = get_command_job(started["job_id"], settings)
        observed.append(status["status"])
        if status["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            break
        time.sleep(0.005)
    descendant_pid = int(pid_file.read_text())
    assert status["status"] == "completed"
    assert observed[-1] == "completed"
    assert all(item == "running" for item in observed[:-1])
    assert status["process_group_cleaned"] is True
    assert not os.path.exists(f"/proc/{descendant_pid}")


def test_prepare_cannot_reconcile_same_runtime_finishing_job(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), artifact_disk_reserve_bytes=0,
    )
    store = store_for(settings)
    job_id = "same-runtime-finishing"
    store.acquire_lifecycle(job_id)
    output = store.open(job_id, "combined", tmp_path / "ignored", 64)
    output.write(b"done")
    meta_path = store.root / f"{job_id}.json"
    store.write_aux(job_id, meta_path, json.dumps({
        "log_id": job_id, "status": "running", "complete": False,
    }).encode())
    output.close()
    assert store._active == {}

    # Новый artifact вызывает prepare между закрытием stream writers и terminal metadata.
    probe = store.open("concurrent-probe", "combined", tmp_path / "ignored-probe", 8)
    probe.close()
    assert json.loads(meta_path.read_text())["status"] == "running"

    terminal = {"log_id": job_id, "status": "completed", "complete": True}
    store.write_aux(job_id, meta_path, json.dumps(terminal).encode())
    store.release_lifecycle(job_id)
    assert not store._is_runtime_owned(job_id)


def test_store_enforces_reserve_quota_and_ttl_without_deleting_pinned(tmp_path) -> None:
    root = tmp_path / "store"
    logs = root / "artifacts"
    logs.mkdir(parents=True)
    (logs / "old.out").write_bytes(b"old")
    (logs / "old.json").write_text(json.dumps({"log_id": "old", "created_at": 1, "status": "completed"}))
    (logs / "pin.out").write_bytes(b"pin")
    (logs / "pin.json").write_text(json.dumps({"log_id": "pin", "created_at": 1, "status": "completed", "pinned": True}))
    for path in logs.iterdir():
        os.utime(path, (1, 1))

    store = OutputStore(root, quota_bytes=1024, max_artifact_bytes=1024, reserve_bytes=0, ttl_seconds=10)
    store.cleanup(now=100)
    assert not (logs / "old.out").exists()
    assert (logs / "pin.out").exists()

    blocked = OutputStore(root, quota_bytes=4, max_artifact_bytes=4, reserve_bytes=2, ttl_seconds=0)
    with pytest.raises(ArtifactQuotaError):
        blocked.prepare()


def test_cleanup_expires_orphans_but_preserves_active_orphan(tmp_path) -> None:
    root = tmp_path / "store"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    orphan = artifacts / "orphan.out"
    orphan.write_bytes(b"old")
    os.utime(orphan, (1, 1))
    store = OutputStore(root, quota_bytes=100_000, max_artifact_bytes=100_000, reserve_bytes=0, ttl_seconds=10)
    store.cleanup(now=100)
    assert not orphan.exists()

    active = store.open("active-orphan", "combined", tmp_path / "ignored", 8)
    active.write(b"active")
    os.utime(active.path, (1, 1))
    store.cleanup(now=100)
    assert active.path.exists()
    active.abort()
    store.abort_artifact("active-orphan")


def test_restart_reconciles_unowned_running_manifest_and_allows_eviction(tmp_path) -> None:
    root = tmp_path / "store"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "stale.out").write_bytes(b"stale")
    (artifacts / "stale.json").write_text(json.dumps({
        # Живой PID не доказывает ownership после restart и может уже принадлежать
        # совершенно другому процессу.
        "log_id": "stale", "status": "running", "pid": os.getpid(),
        "created_at": 1,
    }))
    restarted = OutputStore(
        root, quota_bytes=10_000, max_artifact_bytes=10_000, reserve_bytes=0, ttl_seconds=0,
    )
    restarted.prepare()
    reconciled = json.loads((artifacts / "stale.json").read_text())
    assert reconciled["status"] == "failed"
    assert reconciled["termination_reason"] == "runtime_restart"
    assert restarted._active == {}
    restarted.quota_bytes = restarted.usage_bytes()
    restarted._evict_lru(1, exclude="")
    assert not (artifacts / "stale.out").exists()
    assert not (artifacts / "stale.json").exists()


def test_read_refreshes_lru_and_evicts_unread_artifact_first(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), artifact_total_bytes=10_000, artifact_max_bytes=10_000,
        artifact_disk_reserve_bytes=0, artifact_ttl_seconds=3600,
    )
    root = settings.command_jobs_dir / "artifacts"
    root.mkdir(parents=True)
    for artifact_id, modified in (("read-me", 1), ("unread", 2)):
        payload = root / f"{artifact_id}.out"
        manifest = root / f"{artifact_id}.json"
        payload.write_bytes(artifact_id.encode())
        manifest.write_text(json.dumps({
            "log_id": artifact_id, "status": "completed", "complete": True,
            "created_at": modified,
        }))
        os.utime(payload, (modified, modified))
        os.utime(manifest, (modified, modified))
    read_artifact("read-me", settings, max_bytes=64)
    store = store_for(settings)
    store.usage_bytes(refresh=True)
    store.quota_bytes = store.usage_bytes()
    store._evict_lru(1, exclude="")
    assert (root / "read-me.out").exists()
    assert not (root / "unread.out").exists()


def test_metadata_bytes_obey_store_quota_and_fail_closed(tmp_path) -> None:
    store = OutputStore(
        tmp_path / "store", quota_bytes=32, max_artifact_bytes=32,
        reserve_bytes=0, ttl_seconds=0,
    )
    artifact = store.open("metadata-quota", "combined", tmp_path / "ignored", 8)
    artifact.write(b"x")
    with pytest.raises(ArtifactQuotaError, match="metadata quota"):
        artifact.close()
    assert store._active == {}
    assert not list(store.root.glob("metadata-quota.*"))


def test_store_is_shared_and_combined_streams_share_one_logical_limit(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), artifact_total_bytes=4096, artifact_max_bytes=10,
        artifact_disk_reserve_bytes=0, artifact_ttl_seconds=0,
    )
    first = store_for(settings)
    assert first is store_for(settings)
    out = first.open("same", "stdout", tmp_path / "outside.out", 16)
    out.write(b"123456")
    out.close()
    assert not (tmp_path / "outside.out").exists()
    err = first.open("same", "stderr", tmp_path / "outside.err", 16)
    err.write(b"12345")
    with pytest.raises(ArtifactQuotaError):
        err.close()


def test_store_evicts_completed_lru_but_never_active_artifact(tmp_path) -> None:
    root = tmp_path / "store"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "old.out").write_bytes(b"o" * 9_000)
    (artifacts / "old.json").write_text(json.dumps({"log_id": "old", "status": "completed"}))
    store = OutputStore(root, quota_bytes=15_000, max_artifact_bytes=15_000, reserve_bytes=0, ttl_seconds=0)
    active = store.open("active", "combined", tmp_path / "ignored", 8)
    active.write(b"a" * 9_000)
    (artifacts / "active.json").write_text(json.dumps({"log_id": "active", "status": "completed"}))
    active.write(b"b" * 1_000)
    active.close()
    assert not (artifacts / "old.out").exists()
    assert (artifacts / "active.combined").exists()


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_artifact_write_fault_is_typed_and_rolls_back_accounting(
    tmp_path, error_number: int,
) -> None:
    store = OutputStore(
        tmp_path / "store", quota_bytes=100_000, max_artifact_bytes=100_000,
        reserve_bytes=0, ttl_seconds=0,
    )
    artifact = store.open("fault", "combined", tmp_path / "ignored", 8)
    usage_before = store.usage_bytes()

    class FaultyHandle:
        def write(self, data: bytes) -> int:
            raise OSError(error_number, "injected")

        def close(self) -> None:
            return None

    artifact._handle.close()
    artifact._handle = FaultyHandle()  # type: ignore[assignment]
    with pytest.raises(ArtifactPersistenceError, match="artifact write failed"):
        artifact.write(b"x" * 9_000)
    assert store.usage_bytes() == usage_before
    assert artifact.bytes_written == 0


def test_short_write_is_typed_and_accounts_only_persisted_bytes(tmp_path) -> None:
    store = OutputStore(
        tmp_path / "store", quota_bytes=100_000, max_artifact_bytes=100_000,
        reserve_bytes=0, ttl_seconds=0,
    )
    artifact = store.open("short", "combined", tmp_path / "ignored", 8)
    real_handle = artifact._handle

    class ShortHandle:
        def write(self, data: bytes) -> int:
            return real_handle.write(data[:-1])

        def close(self) -> None:
            real_handle.close()

    artifact._handle = ShortHandle()  # type: ignore[assignment]
    with pytest.raises(ArtifactPersistenceError, match="short artifact write"):
        artifact.write(b"x" * 9_000)
    persisted = artifact.path.stat().st_size
    assert persisted > 0
    assert store.usage_bytes() == persisted
    assert artifact.bytes_written == 0


def test_artifact_close_metadata_failure_cleans_active_and_all_streams(
    tmp_path, monkeypatch,
) -> None:
    store = OutputStore(
        tmp_path / "store", quota_bytes=100_000, max_artifact_bytes=100_000,
        reserve_bytes=0, ttl_seconds=0,
    )
    out = store.open("close-fault", "stdout", tmp_path / "ignored-out", 8)
    err = store.open("close-fault", "stderr", tmp_path / "ignored-err", 8)
    out.write(b"stdout")
    err.write(b"stderr")
    original = store.finish_stream

    def fail_metadata(log_id: str, stream: str, size: int, digest: str) -> None:
        if stream == "stderr":
            raise OSError(errno.ENOSPC, "injected metadata failure")
        original(log_id, stream, size, digest)

    monkeypatch.setattr(store, "finish_stream", fail_metadata)
    out.close()
    with pytest.raises(ArtifactPersistenceError, match="metadata close failed"):
        err.close()
    assert store._active == {}
    assert not list(store.root.glob("close-fault.*"))


def test_command_and_background_capture_faults_terminate_without_ram_fallback(
    tmp_path, monkeypatch,
) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
    )

    def fail_persist(self, data: bytes) -> None:
        if data:
            raise ArtifactPersistenceError("injected ENOSPC")

    monkeypatch.setattr(OutputArtifact, "_persist", fail_persist)
    sync = run_command("printf '%100000s' x", settings, parse_kind="none")
    assert sync["ok"] is False
    assert sync["error_kind"] == "artifact_capture_failed"
    assert len(sync["stdout"]) == 0
    sync_store = store_for(settings)
    assert sync_store._active == {}
    assert not list(sync_store.root.glob(f"{sync['log_id']}.*"))

    started = start_command_job("printf '%100000s' x; sleep 5", settings)
    deadline = time.time() + 5
    status = get_command_job(started["job_id"], settings)
    while status["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
        status = get_command_job(started["job_id"], settings)
    assert status["status"] == "failed"
    assert status.get("error_kind") == "artifact_capture_failed"
    assert status["stdout_bytes"] == 0
    failed_meta = json.loads(
        (settings.command_jobs_dir / "artifacts" / f"{started['job_id']}.json").read_text()
    )
    assert failed_meta["complete"] is False
    assert "continuation" not in failed_meta
    assert not list((settings.command_jobs_dir / "artifacts").glob(f"{started['job_id']}.*.meta.json"))
    assert not list((settings.command_jobs_dir / "artifacts").glob(f"{started['job_id']}.digest.json"))


def test_sync_final_manifest_failure_aborts_completed_artifacts(tmp_path, monkeypatch) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
    )
    store = store_for(settings)
    original = store.write_aux

    def fail_final_manifest(log_id, target, data):
        if target.name == f"{log_id}.json":
            raise ArtifactPersistenceError("injected final manifest ENOSPC")
        original(log_id, target, data)

    monkeypatch.setattr(store, "write_aux", fail_final_manifest)
    result = run_command("printf complete", settings, parse_kind="none")
    assert result["ok"] is False
    assert result["error_kind"] == "artifact_capture_failed"
    assert store._active == {}
    assert not list(store.root.iterdir())


def test_background_drain_finalization_failure_is_incomplete_and_fail_closed(
    tmp_path, monkeypatch,
) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
    )

    def fail_finalize(*_args, **_kwargs):
        raise OSError("injected drain finalization failure")

    monkeypatch.setattr(command_tools, "_finalize_process_drains", fail_finalize)
    started = start_command_job("printf captured", settings, timeout_ms=10_000)
    meta_path = settings.command_jobs_dir / "artifacts" / f"{started['job_id']}.json"
    deadline = time.time() + 5
    while time.time() < deadline:
        meta = json.loads(meta_path.read_text())
        if meta["status"] != "running":
            break
        time.sleep(0.02)
    assert meta["status"] == "failed"
    assert meta["complete"] is False
    assert meta["error_kind"] == "artifact_capture_failed"
    assert "stdout_sha256" not in meta
    assert "stderr_sha256" not in meta
    store = store_for(settings)
    assert store._active == {}
    assert sorted(path.name for path in store.root.glob(f"{started['job_id']}.*")) == [
        f"{started['job_id']}.json",
    ]


def test_pipe_transport_close_cannot_double_close_reused_descriptor(tmp_path) -> None:
    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(read_fd, "rb")
    command_tools._close_pipe_transport(pipe)
    reused_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        assert reused_fd == read_fd
        pipe.close()
        os.fstat(reused_fd)
    finally:
        os.close(reused_fd)
        os.close(write_fd)


def test_forced_drain_cleanup_closes_transport_and_wrapper(monkeypatch) -> None:
    class FakeThread:
        def __init__(self, states: list[bool]) -> None:
            self.states = iter(states)
            self.alive = False

        def join(self, timeout=None) -> None:
            self.alive = next(self.states, False)

        def is_alive(self) -> bool:
            return self.alive

    class FakeRaw:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakePipe:
        def __init__(self) -> None:
            self.raw = FakeRaw()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    stdout, stderr = FakePipe(), FakePipe()
    process = type("FakeProcess", (), {"pid": 4242, "stdout": stdout, "stderr": stderr})()
    first = FakeThread([True, True, False])
    second = FakeThread([False, False, False])
    monkeypatch.setattr(command_tools, "_terminate_process_group", lambda *args, **kwargs: "killed")
    monkeypatch.setattr(command_tools, "_wait_process_group_cleaned", lambda *args, **kwargs: True)

    cleanup, cleaned, forced = command_tools._finalize_process_drains(
        process, (first, second), grace_seconds=0,
    )

    assert (cleanup, cleaned, forced) == ("killed", True, True)
    assert stdout.raw.closed and stderr.raw.closed
    assert stdout.closed and stderr.closed


def test_initial_job_manifest_failure_terminates_child_and_cleans_store(
    tmp_path, monkeypatch,
) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
    )
    store = store_for(settings)
    original_write_aux = store.write_aux

    def fail_manifest(log_id, target, data):
        if target.name == f"{log_id}.json":
            raise ArtifactQuotaError("injected manifest quota")
        original_write_aux(log_id, target, data)

    monkeypatch.setattr(store, "write_aux", fail_manifest)
    created = []
    original_popen = command_tools.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(command_tools.subprocess, "Popen", recording_popen)
    with pytest.raises(ArtifactQuotaError, match="manifest quota"):
        start_command_job("sleep 30", settings)
    assert len(created) == 1
    assert created[0].poll() is not None
    assert store._active == {}
    assert not list(store.root.glob("*.out"))
    assert not list(store.root.glob("*.err"))
    assert not list(store.root.glob("*.json"))
    audit = [json.loads(line) for line in settings.command_audit_log_path.read_text().splitlines()]
    assert audit[-1]["status"] == "failed"
    assert audit[-1]["error_kind"] == "artifact_metadata_failed"


def test_read_artifact_pages_utf8_crlf_and_huge_line_without_rescanning(tmp_path, monkeypatch) -> None:
    settings = replace(
        make_settings(tmp_path), artifact_total_bytes=1024 * 1024, artifact_max_bytes=1024 * 1024,
        artifact_disk_reserve_bytes=0,
    )
    artifact = store_for(settings).open("page", "combined", tmp_path / "ignored", 32)
    expected = ("😀\r\n" + "я" * 200_000).encode()
    artifact.write(expected)
    artifact.close()
    root = settings.command_jobs_dir / "artifacts"
    (root / "page.json").write_text(json.dumps({"log_id": "page", "kind": "pty", "complete": True}))

    original_open = type(root / "page.combined").open
    payload_opens = 0

    def counted_open(path, *args, **kwargs):
        nonlocal payload_opens
        if path.name == "page.combined":
            payload_opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(root / "page.combined"), "open", counted_open)
    cursor = None
    rebuilt = bytearray()
    while True:
        page = read_artifact("page", settings, cursor=cursor, max_bytes=257)
        assert page["payload"]["type"] == "text"
        if page["has_more"]:
            assert page["receipt"]["status"] == "partial"
            assert page["receipt"]["completeness"] == "partial"
            assert page["receipt"]["reason"] == "inline_limit"
            assert page["receipt"]["applied"]["source_complete"] is True
        rebuilt.extend(page["payload"]["text"].encode())
        if cursor is None and page["next_cursor"] is not None:
            signed = page["next_cursor"]
            position = len(signed) // 2
            tampered = signed[:position] + ("A" if signed[position] != "A" else "B") + signed[position + 1:]
            with pytest.raises(ValueError, match="invalid artifact cursor"):
                read_artifact("page", settings, cursor=tampered, max_bytes=257)
        cursor = page["next_cursor"]
        if page["eof"]:
            assert page["receipt"]["status"] == "completed"
            assert page["receipt"]["completeness"] == "complete"
            assert page["receipt"]["reason"] == "none"
            break
    assert bytes(rebuilt) == expected
    assert payload_opens < len(expected) // 200
    assert page["metadata"]["sha256"] == hashlib.sha256(expected).hexdigest()


def test_command_audit_records_start_and_failure_receipts(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_total_bytes=1,
        artifact_max_bytes=1, artifact_disk_reserve_bytes=0,
    )
    result = run_command("printf abc", settings, parse_kind="none")
    records = [json.loads(line) for line in settings.command_audit_log_path.read_text().splitlines()]
    assert result["ok"] is False
    assert [record["event"] for record in records] == ["command_started", "command_finished"]
    assert records[0]["request_id"] == records[1]["request_id"]
    assert records[1]["status"] == "failed"
    assert "args_fingerprint" in records[0] and "duration_ms" in records[1]


def test_command_audit_rotates_at_ten_mib_and_keeps_five_generations(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.command_audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.command_audit_log_path.open("wb") as handle:
        handle.truncate(10 * 1024 * 1024)
    for generation in range(1, 7):
        settings.command_audit_log_path.with_name(
            f"{settings.command_audit_log_path.name}.{generation}"
        ).write_text(str(generation))
    command_tools._audit(settings, {"event": "probe", "request_id": "r"})
    assert json.loads(settings.command_audit_log_path.read_text())["event"] == "probe"
    assert settings.command_audit_log_path.with_name(f"{settings.command_audit_log_path.name}.1").stat().st_size == 10 * 1024 * 1024
    assert settings.command_audit_log_path.with_name(f"{settings.command_audit_log_path.name}.5").exists()
    assert not settings.command_audit_log_path.with_name(f"{settings.command_audit_log_path.name}.6").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is POSIX-only")
def test_pty_persists_redacted_secret_across_writes(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path), command_policy_mode="full_repo", artifact_disk_reserve_bytes=0,
        artifact_total_bytes=8 * 1024 * 1024,
    )
    script = "import os,time;os.write(1,b'token=abcdefghijk');time.sleep(.05);os.write(1,b'lmnopqrstuvwxyz\\n')"
    started = start_terminal_session(settings, command=f"{sys.executable} -c \"{script}\"", idle_timeout_ms=5_000)
    cursor = 0
    combined = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        page = read_terminal_session(started["session_id"], settings, cursor=cursor, wait_ms=100)
        combined += page["data"]
        cursor = page["next_cursor"]
        if page["eof"]:
            break
    assert "abcdefghijklmnopqrstuvwxyz" not in combined
    assert "token=<redacted>" in combined
    artifact = read_artifact(started["log_id"], settings)
    assert artifact["metadata"]["kind"] == "pty"
    assert artifact["metadata"]["ordering"] == "capture_order"
