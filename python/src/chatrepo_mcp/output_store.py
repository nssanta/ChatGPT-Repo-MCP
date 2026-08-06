from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)(token|secret|password|api[_-]?key)=([^\s]+)"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"npm_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._-]+"),
    re.compile(r"https?://[^\s/@]+:[^\s/@]+@[^\s]+"),
    re.compile(r"git@[^:\s]+:[^\s]+"),
)
_PRIVATE_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def redact_text(text: str) -> str:
    text = re.sub(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.DOTALL,
    )
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_replacement, text)
    return text


def _replacement(match: re.Match[str]) -> str:
    value = match.group(0)
    if value.lower().startswith("bearer "):
        return "Bearer <redacted>"
    if "=" in value:
        return value.split("=", 1)[0] + "=<redacted>"
    return "<redacted>"


class StreamingRedactor:
    """Редактирует поток до записи, удерживая только ограниченный хвост."""

    def __init__(self, carry_chars: int = 8192) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._carry_chars = carry_chars
        self._private = False
        self._discard_secret_token = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        self._pending += self._decoder.decode(data, final=final)
        output: list[str] = []
        while True:
            if self._discard_secret_token:
                boundary = re.search(r"\s", self._pending)
                if boundary is None:
                    self._pending = self._pending[-1:]
                    break
                self._pending = self._pending[boundary.start():]
                self._discard_secret_token = False
                continue
            if self._private:
                end = _PRIVATE_END.search(self._pending)
                if end is None:
                    self._pending = self._pending[-128:]
                    break
                self._pending = self._pending[end.end():]
                output.append("[REDACTED PRIVATE KEY]")
                self._private = False
                continue
            begin = _PRIVATE_BEGIN.search(self._pending)
            if begin is not None:
                output.append(redact_text(self._pending[:begin.start()]))
                self._pending = self._pending[begin.end():]
                self._private = True
                continue
            if final:
                output.append(redact_text(self._pending))
                self._pending = ""
                break
            if len(self._pending) <= self._carry_chars:
                break
            sensitive = re.search(
                r"(?i)(?:token|secret|password|api[_-]?key)=|Bearer\s+|gh[pousr]_|npm_|https?://[^\s/@]+:|git@",
                self._pending,
            )
            if sensitive is not None:
                output.append(redact_text(self._pending[:sensitive.start()]))
                credential = self._pending[sensitive.start():]
                prefix_length = len(sensitive.group(0))
                boundary = re.search(r"\s", credential[prefix_length:])
                if boundary is not None:
                    boundary_index = prefix_length + boundary.start()
                    output.append(redact_text(credential[:boundary_index]))
                    self._pending = credential[boundary_index:]
                    continue
                output.append(_stream_secret_marker(sensitive.group(0)))
                self._pending = ""
                self._discard_secret_token = True
                break
            cut = len(self._pending) - self._carry_chars
            whitespace = max(self._pending.rfind(" ", 0, cut), self._pending.rfind("\n", 0, cut), self._pending.rfind("\t", 0, cut))
            if whitespace >= 0:
                cut = whitespace + 1
            output.append(redact_text(self._pending[:cut]))
            self._pending = self._pending[cut:]
        return "".join(output).encode("utf-8")


def _stream_secret_marker(prefix: str) -> str:
    lowered = prefix.lower()
    if lowered.startswith("bearer"):
        return "Bearer <redacted>"
    if "=" in prefix:
        return prefix.split("=", 1)[0] + "=<redacted>"
    return "<redacted>"


class BoundedTail:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(maximum, 0)
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, data: bytes) -> None:
        if self.maximum == 0 or not data:
            return
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self.maximum and self._chunks:
            overflow = self._size - self.maximum
            first = self._chunks[0]
            if len(first) <= overflow:
                self._chunks.popleft()
                self._size -= len(first)
            else:
                self._chunks[0] = first[overflow:]
                self._size -= overflow

    def text(self) -> str:
        data = b"".join(self._chunks)
        for start in range(min(4, len(data) + 1)):
            try:
                return data[start:].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
        return ""


class BoundedHead:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(maximum, 0)
        self._data = bytearray()

    def append(self, data: bytes) -> None:
        remaining = self.maximum - len(self._data)
        if remaining > 0:
            self._data.extend(data[:remaining])

    def text(self) -> str:
        data = bytes(self._data)
        while data:
            try:
                return data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                if exc.start < len(data) - 4:
                    raise ArtifactPersistenceError("artifact preview contains invalid UTF-8") from exc
                data = data[:exc.start]
        return ""


_INLINE_TRUNCATION_MARKER = "\n...[truncated; full output is available in the artifact]...\n"


def _utf8_prefix_text(text: str, maximum: int) -> str:
    """Return at most ``maximum`` UTF-8 bytes without splitting a codepoint."""
    data = text.encode("utf-8")[:max(maximum, 0)]
    while data:
        try:
            return data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            data = data[:exc.start]
    return ""


def _utf8_suffix_text(text: str, maximum: int) -> str:
    """Return a UTF-8-safe suffix bounded by bytes rather than characters."""
    data = text.encode("utf-8")[-max(maximum, 0):] if maximum > 0 else b""
    for start in range(min(4, len(data) + 1)):
        try:
            return data[start:].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
    return ""


def inline_head_tail(head: str, tail: str, *, total_bytes: int, maximum: int) -> str:
    """Render an artifact preview within one inline budget, preserving both ends."""
    maximum = max(maximum, 0)
    if total_bytes <= maximum:
        return _utf8_prefix_text(head, maximum)
    marker = _utf8_prefix_text(_INLINE_TRUNCATION_MARKER, maximum)
    payload_bytes = max(maximum - len(marker.encode("utf-8")), 0)
    head_bytes = (payload_bytes + 1) // 2
    tail_bytes = payload_bytes - head_bytes
    return (
        _utf8_prefix_text(head, head_bytes)
        + marker
        + _utf8_suffix_text(tail, tail_bytes)
    )


class ArtifactQuotaError(OSError):
    pass


class ArtifactPersistenceError(OSError):
    pass


class OutputArtifact:
    def __init__(self, store: OutputStore, log_id: str, stream: str, path: Path, preview_bytes: int) -> None:
        self.store = store
        self.log_id = log_id
        self.stream = stream
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb", buffering=0)
        self._redactor = StreamingRedactor()
        self._tail = BoundedTail(preview_bytes)
        self._head = BoundedHead(preview_bytes)
        self._hash = hashlib.sha256()
        self.bytes_written = 0
        self._closed = False
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._closed:
                raise ValueError("artifact is closed")
            redacted = self._redactor.feed(data)
            self._persist(redacted)
            return len(data)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            failure: OSError | None = None
            try:
                self._persist(self._redactor.feed(b"", final=True))
                self._handle.flush()
                os.fsync(self._handle.fileno())
            except OSError as exc:
                failure = exc
            finally:
                try:
                    self._handle.close()
                except OSError as exc:
                    failure = failure or exc
                self._closed = True
            if failure is not None:
                self.store.abort_artifact(self.log_id)
                if isinstance(failure, (ArtifactPersistenceError, ArtifactQuotaError)):
                    raise failure
                raise ArtifactPersistenceError(f"artifact close failed: {failure}") from failure
            try:
                self.store.finish_stream(self.log_id, self.stream, self.bytes_written, self.sha256)
            except OSError as exc:
                self.store.abort_artifact(self.log_id)
                if isinstance(exc, ArtifactQuotaError):
                    raise
                raise ArtifactPersistenceError(f"artifact metadata close failed: {exc}") from exc

    def abort(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._handle.close()
                finally:
                    self._closed = True
                    self.store.abort_stream(self.log_id)

    def _persist(self, data: bytes) -> None:
        if not data:
            return
        self.store.claim(len(data), self.log_id, self.bytes_written)
        try:
            written = self._handle.write(data)
        except OSError as exc:
            self.store.rollback_claim(len(data), self.log_id)
            raise ArtifactPersistenceError(f"artifact write failed: {exc}") from exc
        if written != len(data):
            self.store.rollback_claim(len(data) - max(written, 0), self.log_id)
            raise ArtifactPersistenceError(f"short artifact write: {written}/{len(data)}")
        self.bytes_written += written
        self._hash.update(data)
        self._tail.append(data)
        self._head.append(data)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    @property
    def preview(self) -> str:
        return self._tail.text()

    @property
    def head(self) -> str:
        return self._head.text()


class OutputStore:
    def __init__(self, root: Path, *, quota_bytes: int, max_artifact_bytes: int, reserve_bytes: int, ttl_seconds: int) -> None:
        self.root = root / "artifacts"
        self.logs = self.root
        self.quota_bytes = max(quota_bytes, 1)
        self.max_artifact_bytes = max(max_artifact_bytes, 1)
        self.reserve_bytes = max(reserve_bytes, 0)
        self.ttl_seconds = max(ttl_seconds, 0)
        self._lock = threading.RLock()
        self._aux_lock = threading.Lock()
        self._usage: int | None = None
        self._logical_bytes: dict[str, int] = {}
        self._active: dict[str, int] = {}
        self._lifecycle_leases: dict[str, int] = {}

    def acquire_lifecycle(self, log_id: str) -> None:
        """Protect runtime-owned metadata until its terminal state is durable."""
        with self._lock:
            self._lifecycle_leases[log_id] = self._lifecycle_leases.get(log_id, 0) + 1

    def release_lifecycle(self, log_id: str) -> None:
        with self._lock:
            remaining = self._lifecycle_leases.get(log_id, 0) - 1
            if remaining > 0:
                self._lifecycle_leases[log_id] = remaining
            else:
                self._lifecycle_leases.pop(log_id, None)

    def _is_runtime_owned(self, log_id: str) -> bool:
        with self._lock:
            return log_id in self._active or log_id in self._lifecycle_leases

    def prepare(self) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        self.reconcile_stale_active()
        self.cleanup()
        usage = self.usage_bytes(refresh=self._usage is None)
        if usage >= self.quota_bytes:
            self._evict_lru(1, exclude="")
            usage = self.usage_bytes()
            if usage >= self.quota_bytes:
                raise ArtifactQuotaError("artifact quota is exhausted")
        disk = shutil.disk_usage(self.root)
        free = disk.free
        required_free = max(self.reserve_bytes, disk.total // 10)
        if free < required_free:
            raise ArtifactQuotaError("filesystem free space is below artifact reserve")

    def open(self, log_id: str, stream: str, path: Path, preview_bytes: int) -> OutputArtifact:
        self.prepare()
        suffix = {"stdout": "out", "stderr": "err", "combined": "combined"}.get(stream)
        if suffix is None:
            raise ValueError(f"unsupported artifact stream: {stream}")
        artifact_path = self.root / f"{log_id}.{suffix}"
        # Не позволяем вызывающему коду вынести учитываемые байты за пределы store.
        if path.resolve() != artifact_path.resolve():
            path = artifact_path
        with self._lock:
            if log_id not in self._logical_bytes:
                self._logical_bytes[log_id] = sum(
                    candidate.stat().st_size
                    for candidate in self.root.glob(f"{log_id}.*")
                    if candidate.suffix in {".out", ".err", ".combined"}
                )
            previous = artifact_path.stat().st_size if artifact_path.exists() else 0
            if previous:
                self._usage = max(0, self.usage_bytes() - previous)
                self._logical_bytes[log_id] = max(0, self._logical_bytes[log_id] - previous)
            self._active[log_id] = self._active.get(log_id, 0) + 1
        try:
            return OutputArtifact(self, log_id, stream, path, preview_bytes)
        except OSError:
            self.abort_stream(log_id)
            self._usage = None
            raise

    def claim(self, amount: int, log_id: str, artifact_bytes: int) -> None:
        with self._lock:
            logical = self._logical_bytes.get(log_id, 0)
            if logical + amount > self.max_artifact_bytes:
                raise ArtifactQuotaError(f"per-artifact quota exceeded for {log_id}")
            usage = self.usage_bytes()
            if usage + amount > self.quota_bytes:
                self._evict_lru(amount, exclude=log_id)
                usage = self.usage_bytes()
                if usage + amount > self.quota_bytes:
                    raise ArtifactQuotaError(f"artifact quota exceeded for {log_id}")
            disk = shutil.disk_usage(self.root)
            if disk.free - amount < max(self.reserve_bytes, disk.total // 10):
                raise ArtifactQuotaError(f"filesystem reserve would be violated for {log_id}")
            self._usage = usage + amount
            self._logical_bytes[log_id] = logical + amount

    def rollback_claim(self, amount: int, log_id: str) -> None:
        with self._lock:
            self._usage = max(0, self.usage_bytes() - amount)
            self._logical_bytes[log_id] = max(0, self._logical_bytes.get(log_id, 0) - amount)

    def claim_aux(self, amount: int, log_id: str) -> None:
        with self._lock:
            usage = self.usage_bytes()
            if usage + amount > self.quota_bytes:
                self._evict_lru(amount, exclude=log_id)
                usage = self.usage_bytes()
            disk = shutil.disk_usage(self.root)
            if usage + amount > self.quota_bytes:
                raise ArtifactQuotaError(f"artifact metadata quota exceeded for {log_id}")
            if disk.free - amount < max(self.reserve_bytes, disk.total // 10):
                raise ArtifactQuotaError(f"filesystem reserve would be violated for {log_id}")
            self._usage = usage + amount

    def write_aux(self, log_id: str, target: Path, data: bytes) -> None:
        with self._aux_lock:
            self._write_aux_locked(log_id, target, data)

    def _write_aux_locked(self, log_id: str, target: Path, data: bytes) -> None:
        old_size = target.stat().st_size if target.exists() else 0
        temporary = target.with_suffix(target.suffix + ".tmp")
        self.claim_aux(len(data), log_id)
        try:
            with temporary.open("wb") as handle:
                written = handle.write(data)
                if written != len(data):
                    raise ArtifactPersistenceError(f"short artifact metadata write: {written}/{len(data)}")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            self._usage = None
            if isinstance(exc, (ArtifactPersistenceError, ArtifactQuotaError)):
                raise
            raise ArtifactPersistenceError(f"artifact metadata write failed: {exc}") from exc
        with self._lock:
            self._usage = max(0, self.usage_bytes() - old_size)

    def finish_stream(self, log_id: str, stream: str, size: int, digest: str) -> None:
        metadata = self.root / f"{log_id}.{stream}.meta.json"
        self.write_aux(
            log_id,
            metadata,
            json.dumps({"size_bytes": size, "sha256": digest, "complete": True}, sort_keys=True).encode(),
        )
        with self._lock:
            self._usage = None
        with self._lock:
            active = self._active.get(log_id, 0) - 1
            if active > 0:
                self._active[log_id] = active
            else:
                self._active.pop(log_id, None)
        self._persist_logical_digest(log_id)

    def abort_stream(self, log_id: str) -> None:
        with self._lock:
            active = self._active.get(log_id, 0) - 1
            if active > 0:
                self._active[log_id] = active
            else:
                self._active.pop(log_id, None)

    def abort_artifact(self, log_id: str, *, preserve_manifest: bool = False) -> None:
        with self._lock:
            self._active.pop(log_id, None)
            self._logical_bytes.pop(log_id, None)
            for candidate in self.root.glob(f"{log_id}.*"):
                if preserve_manifest and candidate == self.root / f"{log_id}.json":
                    continue
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    continue
            self._usage = None

    def _persist_logical_digest(self, log_id: str) -> None:
        combined = self.root / f"{log_id}.combined"
        if combined.exists() and (self.root / f"{log_id}.combined.meta.json").exists():
            digest = json.loads((self.root / f"{log_id}.combined.meta.json").read_text())["sha256"]
        else:
            streams = [self.root / f"{log_id}.out", self.root / f"{log_id}.err"]
            if not all(path.exists() for path in streams) or not all(
                (self.root / f"{log_id}.{name}.meta.json").exists()
                for name in ("stdout", "stderr")
            ):
                return
            hasher = hashlib.sha256()
            for path in streams:
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        hasher.update(chunk)
            digest = hasher.hexdigest()
        target = self.root / f"{log_id}.digest.json"
        self.write_aux(log_id, target, json.dumps({"sha256": digest}, sort_keys=True).encode())
        with self._lock:
            self._usage = None

    def _evict_lru(self, required: int, *, exclude: str) -> None:
        manifests: list[tuple[float, Path, str]] = []
        known: set[str] = set()
        for meta in self.root.glob("*.json"):
            if meta.name.endswith((".meta.json", ".digest.json")):
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                log_id = str(payload.get("log_id", meta.stem))
                known.add(log_id)
                if log_id == exclude or self._is_runtime_owned(log_id) or payload.get("pinned"):
                    continue
                if payload.get("status") in {"running", "starting", "closing"}:
                    continue
                manifests.append((self._artifact_recency(log_id, meta), meta, log_id))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        orphan_groups = self._orphan_groups(known)
        for log_id, paths in orphan_groups.items():
            if log_id == exclude or self._is_runtime_owned(log_id):
                continue
            manifests.append((max(path.stat().st_mtime for path in paths), paths[0], log_id))
        for _, _, log_id in sorted(manifests):
            if self.usage_bytes() + required <= self.quota_bytes:
                break
            removed = 0
            for candidate in self.root.glob(f"{log_id}.*"):
                try:
                    removed += candidate.stat().st_size
                    candidate.unlink()
                except OSError:
                    continue
            self._usage = max(0, self.usage_bytes() - removed)
            self._logical_bytes.pop(log_id, None)

    def usage_bytes(self, *, refresh: bool = False) -> int:
        if self._usage is not None and not refresh:
            return self._usage
        total = 0
        if self.root.exists():
            for path in self.root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    continue
        self._usage = total
        return total

    def cleanup(self, now: float | None = None) -> None:
        if self.ttl_seconds <= 0 or not self.root.exists():
            return
        cutoff = (time.time() if now is None else now) - self.ttl_seconds
        known: set[str] = set()
        for meta in self.logs.glob("*.json"):
            if meta.name.endswith((".meta.json", ".digest.json")):
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
                log_id = str(payload.get("log_id", meta.stem))
                known.add(log_id)
                if payload.get("pinned") or payload.get("status") in {"running", "starting", "closing"}:
                    continue
                if self._artifact_recency(log_id, meta) >= cutoff:
                    continue
                for candidate in self.logs.glob(f"{log_id}.*"):
                    candidate.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        for log_id, paths in self._orphan_groups(known).items():
            if self._is_runtime_owned(log_id) or any(path.stat().st_mtime >= cutoff for path in paths):
                continue
            for path in paths:
                path.unlink(missing_ok=True)
        self._usage = None

    def reconcile_stale_active(self) -> None:
        if not self.root.exists():
            return
        for meta in self.root.glob("*.json"):
            if meta.name.endswith((".meta.json", ".digest.json")):
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            log_id = str(payload.get("log_id", meta.stem))
            if payload.get("status") not in {"running", "starting", "closing", "terminating"}:
                continue
            if self._is_runtime_owned(log_id):
                continue
            payload["status"] = "failed"
            payload["complete"] = False
            payload["termination_reason"] = "runtime_restart"
            payload["finished_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self.write_aux(
                log_id, meta,
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
            )

    def _artifact_recency(self, log_id: str, manifest: Path) -> float:
        recency = manifest.stat().st_mtime
        for suffix in ("out", "err", "combined"):
            path = self.root / f"{log_id}.{suffix}"
            if path.exists():
                recency = max(recency, path.stat().st_mtime)
        return recency

    def _orphan_groups(self, known: set[str]) -> dict[str, list[Path]]:
        groups: dict[str, list[Path]] = {}
        for path in self.root.iterdir():
            if not path.is_file():
                continue
            log_id = _artifact_file_id(path.name)
            if log_id and log_id not in known:
                groups.setdefault(log_id, []).append(path)
        return groups


def _artifact_file_id(name: str) -> str | None:
    for suffix in (
        ".stdout.meta.json.tmp", ".stderr.meta.json.tmp", ".combined.meta.json.tmp",
        ".digest.json.tmp", ".json.tmp",
        ".stdout.meta.json", ".stderr.meta.json", ".combined.meta.json",
        ".digest.json", ".out", ".err", ".combined",
        ".json",
    ):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return None


_STORE_CACHE: dict[tuple[str, int, int, int, int], OutputStore] = {}
_STORE_CACHE_LOCK = threading.Lock()


def store_for(settings: Any) -> OutputStore:
    key = (
        str(settings.command_jobs_dir.resolve()),
        getattr(settings, "artifact_total_bytes", 10_737_418_240),
        getattr(settings, "artifact_max_bytes", 5_368_709_120),
        getattr(settings, "artifact_disk_reserve_bytes", 2_147_483_648),
        getattr(settings, "artifact_ttl_seconds", 604_800),
    )
    with _STORE_CACHE_LOCK:
        cached = _STORE_CACHE.get(key)
        if cached is not None:
            return cached
        cached = OutputStore(
        settings.command_jobs_dir,
        quota_bytes=key[1], max_artifact_bytes=key[2], reserve_bytes=key[3], ttl_seconds=key[4],
        )
        _STORE_CACHE[key] = cached
        return cached


def artifact_reference(artifact_id: str, *, complete: bool, reason: str = "none") -> dict[str, Any]:
    inline_complete = reason not in {"inline_limit", "source_active"}
    continuation = None if inline_complete else {
        "tool": "read_artifact",
        "arguments": {"artifact_id": artifact_id},
    }
    return {
        "artifact_id": artifact_id,
        "has_more": not inline_complete,
        "eof": complete and inline_complete,
        "next_cursor": None,
        "continuation": continuation,
        "receipt": {
            "schema_version": 1,
            "status": "completed" if inline_complete else "partial",
            "completeness": "complete" if inline_complete else "partial",
            "reason": reason,
            "configured": {"page_bytes": 65_536, "max_page_bytes": 262_144},
            "applied": {"source_complete": complete},
            "returned": {},
            "total": None,
            "warnings": [],
        },
    }


def read_artifact(
    artifact_id: str,
    settings: Any,
    *,
    cursor: str | None = None,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    if not artifact_id or "/" in artifact_id or "\\" in artifact_id or artifact_id in {".", ".."}:
        raise ValueError("invalid artifact_id")
    maximum = min(max(max_bytes, 1), 262_144)
    root = settings.command_jobs_dir / "artifacts"
    meta_path = root / f"{artifact_id}.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"artifact not found: {artifact_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    streams = _artifact_streams(root, artifact_id)
    if not streams:
        raise FileNotFoundError(f"artifact payload not found: {artifact_id}")
    offset = _decode_cursor(cursor, artifact_id, settings) if cursor else 0
    total = sum(path.stat().st_size for _, path in streams)
    if offset > total:
        raise ValueError("artifact cursor is beyond current payload")
    kind = str(meta.get("kind") or ("pty" if streams[0][0] == "combined" else "command_output"))
    payload_type = "base64" if kind == "binary" else ("records" if len(streams) > 1 else "text")
    remaining = maximum
    logical = 0
    pieces: list[tuple[str, bytes]] = []
    for stream, path in streams:
        size = path.stat().st_size
        if offset >= logical + size:
            logical += size
            continue
        local = max(offset - logical, 0)
        with path.open("rb") as handle:
            handle.seek(local)
            # Один дополнительный байт позволяет отличить полный UTF-8 codepoint
            # от оборванного на границе страницы без неограниченного buffering.
            data = handle.read(remaining + (4 if payload_type != "base64" else 0))
            if payload_type != "base64" and len(data) > remaining:
                data = _utf8_page_prefix(data, remaining)
            else:
                data = data[:remaining]
        if data:
            pieces.append((stream, data))
            remaining -= len(data)
        elif payload_type != "base64" and local < size and remaining > 0:
            raise ValueError("max_bytes is too small for the next UTF-8 codepoint")
        logical += size
        if remaining == 0:
            break
    page = b"".join(data for _, data in pieces)
    end = offset + len(page)
    complete = bool(meta.get("complete", meta.get("status") not in {"running", "starting", "closing"}))
    has_more = end < total or not complete
    more_bytes = end < total
    page_complete = not has_more
    if payload_type == "records":
        payload_value: dict[str, Any] = {"type": "records", "records": [
            {"stream": stream, "data": data.decode("utf-8", errors="strict")}
            for stream, data in pieces
        ]}
    elif payload_type == "base64":
        payload_value = {"type": "base64", "base64": base64.b64encode(page).decode("ascii")}
    else:
        payload_value = {"type": "text", "text": page.decode("utf-8", errors="strict")}
    digest, digest_warning = _stored_digest(root, artifact_id, streams, complete)
    access_warning: str | None = None
    accessed_at = time.time()
    try:
        for _, path in streams:
            os.utime(path, (accessed_at, accessed_at))
        os.utime(meta_path, (accessed_at, accessed_at))
    except OSError as exc:
        access_warning = f"artifact access recency update failed: {exc}"
    created_raw = meta.get("created_at") or meta.get("started_at") or time.time()
    if isinstance(created_raw, (int, float)):
        created_at = datetime.fromtimestamp(created_raw, UTC).isoformat().replace("+00:00", "Z")
        expires_at = datetime.fromtimestamp(
            accessed_at + getattr(settings, "artifact_ttl_seconds", 604_800), UTC,
        ).isoformat().replace("+00:00", "Z")
    else:
        created_at = str(created_raw)
        expires_at = None
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "payload": payload_value,
        "byte_range": {"start": offset, "end": end},
        "has_more": has_more,
        "eof": complete and end >= total,
        "next_cursor": _encode_cursor(artifact_id, end, settings) if has_more else None,
        "metadata": {
            "kind": kind,
            "mime_type": "application/x-ndjson" if payload_type == "records" else "text/plain; charset=utf-8",
            "size_bytes": total,
            "sha256": digest,
            "created_at": created_at,
            "expires_at": expires_at,
            "ordering": (
                "stdout_then_stderr" if len(streams) > 1
                else ("capture_order" if streams[0][0] == "combined" else "source_order")
            ),
            "complete": complete,
        },
        "receipt": {
            "schema_version": 1,
            "status": "completed" if page_complete else "partial",
            "completeness": "complete" if page_complete else "partial",
            "reason": "none" if page_complete else ("inline_limit" if more_bytes else "unknown"),
            "requested": {"max_bytes": max_bytes},
            "applied": {
                "max_bytes": maximum,
                "payload": payload_type,
                "source_complete": complete,
            },
            "returned": {"bytes": len(page), "records": len(pieces)},
            "total": {"bytes": total},
            "warnings": [warning for warning in (digest_warning, access_warning) if warning],
        },
    }


def _artifact_streams(root: Path, artifact_id: str) -> list[tuple[str, Path]]:
    combined = root / f"{artifact_id}.combined"
    if combined.exists():
        return [("combined", combined)]
    return [
        (stream, path)
        for stream, path in (("stdout", root / f"{artifact_id}.out"), ("stderr", root / f"{artifact_id}.err"))
        if path.exists()
    ]


def _utf8_page_prefix(data: bytes, maximum: int) -> bytes:
    end = min(maximum, len(data))
    while end > 0:
        try:
            data[:end].decode("utf-8", errors="strict")
            return data[:end]
        except UnicodeDecodeError as exc:
            if exc.start < end - 4:
                raise ArtifactPersistenceError("artifact contains invalid UTF-8") from exc
            end = exc.start
    return b""


def _stored_digest(
    root: Path, artifact_id: str, streams: list[tuple[str, Path]], complete: bool,
) -> tuple[str | None, str | None]:
    if not complete:
        return None, "sha256 unavailable until artifact is complete"
    logical = root / f"{artifact_id}.digest.json"
    try:
        return str(json.loads(logical.read_text(encoding="utf-8"))["sha256"]), None
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    digests: list[str] = []
    for stream, _ in streams:
        sidecar = root / f"{artifact_id}.{stream}.meta.json"
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            digests.append(str(payload["sha256"]))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            legacy_key = f"{stream}_sha256"
            # Старые command artifacts хранят digest в общей metadata.
            try:
                common = json.loads((root / f"{artifact_id}.json").read_text(encoding="utf-8"))
                digests.append(str(common[legacy_key]))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                return None, "sha256 unavailable for legacy artifact"
    if len(digests) == 1:
        return digests[0], None
    return None, "logical sha256 unavailable for legacy multi-stream artifact"


def _encode_cursor(artifact_id: str, offset: int, settings: Any) -> str:
    raw = json.dumps({"v": 1, "a": artifact_id, "o": offset}, separators=(",", ":")).encode()
    signature = hmac.digest(_cursor_key(settings), raw, "sha256")
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _decode_cursor(cursor: str, artifact_id: str, settings: Any) -> int:
    try:
        signed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if len(signed) <= 32:
            raise ValueError
        raw, signature = signed[:-32], signed[-32:]
        if not hmac.compare_digest(signature, hmac.digest(_cursor_key(settings), raw, "sha256")):
            raise ValueError
        value = json.loads(raw)
        if value != {"v": 1, "a": artifact_id, "o": value.get("o")}:
            raise ValueError
        offset = int(value["o"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid artifact cursor") from exc


def _cursor_key(settings: Any) -> bytes:
    root = settings.command_jobs_dir
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".artifact-cursor-key"
    try:
        key = path.read_bytes()
        if len(key) == 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        return key
    except FileExistsError:
        existing = path.read_bytes()
        if len(existing) != 32:
            raise ArtifactPersistenceError("invalid artifact cursor key")
        return existing
