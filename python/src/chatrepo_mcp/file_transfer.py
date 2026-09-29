"""Bidirectional file transfer between ChatGPT and the connected machine."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import mimetypes
import os
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import Settings
from .security import (
    SecurityError,
    display_path,
    find_containing_root,
    is_hidden_relative,
    is_secret_relative,
    is_transfer_blocked_relative,
    matches_any_glob,
    normalize_rel_path,
    rel_posix,
)
from .workspace import is_within_roots, resolve_roots


_RESOURCE_PREFIX = "chatrepo-file://local/"
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class FileTransferError(ValueError):
    """A typed, user-recoverable file-transfer failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, FileTransferError):
        return {"ok": False, "error_kind": exc.kind, "error": str(exc)}
    if isinstance(exc, SecurityError):
        return {"ok": False, "error_kind": "path_traversal_or_blocked", "error": str(exc)}
    return {"ok": False, "error_kind": "file_transfer_error", "error": str(exc)}


def _transfer_writable(relative: str, settings: Settings) -> bool:
    relative = normalize_rel_path(relative)
    if is_secret_relative(relative, settings) and not settings.allow_secret_access:
        return False
    if is_transfer_blocked_relative(relative, settings):
        return False
    catch_all = any(pattern.strip() in {"*", "**", "**/*"} for pattern in settings.writable_globs)
    if catch_all and settings.dangerously_allow_all_writes:
        return True
    return matches_any_glob(relative, settings.writable_globs)


def _resolve_transfer_path(path: str, settings: Settings, *, for_write: bool) -> tuple[Path, str]:
    if not path.strip():
        raise FileTransferError("invalid_path", "path must not be empty")

    raw = Path(path)
    lexical = raw.absolute() if raw.is_absolute() else (settings.project_root.resolve() / raw).absolute()

    # Reject a direct symlink endpoint. Existing parent symlinks are resolved
    # before the allowed-root check, so they cannot be used to escape the perimeter.
    if lexical.exists() and lexical.is_symlink():
        raise FileTransferError("symlink_not_allowed", f"symlink endpoints are not allowed: {path}")

    target = lexical.resolve(strict=False)
    roots = resolve_roots(settings)
    if not is_within_roots(target, roots):
        raise SecurityError(f"path escapes allowed roots: {path}")
    root = find_containing_root(target, roots)
    relative = rel_posix(root, target)

    if is_secret_relative(relative, settings) and not settings.allow_secret_access:
        raise SecurityError(f"path is blocked by secret policy: {display_path(target, settings)}")
    if is_transfer_blocked_relative(relative, settings):
        raise SecurityError(f"path is blocked by security policy: {display_path(target, settings)}")
    if not settings.allow_hidden_default and is_hidden_relative(relative):
        raise SecurityError(f"hidden paths are not allowed: {display_path(target, settings)}")
    if for_write and not _transfer_writable(relative, settings):
        raise FileTransferError("path_not_writable", f"path is not writable by policy: {display_path(target, settings)}")

    return target, display_path(target, settings)


def _validate_public_https_url(raw_url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError as exc:
        raise FileTransferError("invalid_download_url", "file download URL is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise FileTransferError("invalid_download_url", "file download URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise FileTransferError("invalid_download_url", "file download URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FileTransferError("invalid_download_url", "file download URL has an invalid port") from exc
    if port not in (None, 443):
        raise FileTransferError("invalid_download_url", "file download URL must use the default HTTPS port")

    try:
        addresses = {
            str(item[4][0]).split("%", 1)[0]
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise FileTransferError("download_host_unresolved", f"cannot resolve file download host: {parsed.hostname}") from exc
    if not addresses:
        raise FileTransferError("download_host_unresolved", f"cannot resolve file download host: {parsed.hostname}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise FileTransferError("download_host_unresolved", f"invalid resolved address for {parsed.hostname}") from exc
        if not ip.is_global:
            raise FileTransferError(
                "download_host_not_public",
                f"file download host resolves to a non-public address: {parsed.hostname}",
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        _validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_to_temp(
    *,
    download_url: str,
    directory: Path,
    max_bytes: int,
) -> tuple[Path, int, str]:
    _validate_public_https_url(download_url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        download_url,
        method="GET",
        headers={"User-Agent": "chatrepo-mcp/file-transfer"},
    )

    temp_path: Path | None = None
    completed = False
    try:
        with opener.open(request, timeout=60) as response:
            final_url = response.geturl()
            _validate_public_https_url(final_url)
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    content_length = -1
                if content_length > max_bytes:
                    raise FileTransferError(
                        "payload_too_large",
                        f"file exceeds FILE_TRANSFER_IMPORT_MAX_BYTES ({content_length} > {max_bytes})",
                    )

            handle = tempfile.NamedTemporaryFile(
                prefix=".chatrepo-upload-",
                dir=directory,
                delete=False,
            )
            temp_path = Path(handle.name)
            digest = hashlib.sha256()
            total = 0
            with handle:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise FileTransferError(
                            "payload_too_large",
                            f"file exceeds FILE_TRANSFER_IMPORT_MAX_BYTES ({total} > {max_bytes})",
                        )
                    handle.write(chunk)
                    digest.update(chunk)
            completed = True
            return temp_path, total, digest.hexdigest()
    except urllib.error.HTTPError as exc:
        raise FileTransferError("download_failed", f"file download returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FileTransferError("download_failed", f"file download failed: {exc.reason}") from exc
    finally:
        if not completed and temp_path is not None:
            temp_path.unlink(missing_ok=True)


def receive_chat_file(
    *,
    file: dict[str, Any],
    destination_path: str,
    settings: Settings,
    dry_run: bool,
) -> dict[str, Any]:
    """Download one ChatGPT host file parameter to a new machine file."""
    try:
        target, display = _resolve_transfer_path(destination_path, settings, for_write=True)
        if target.exists():
            raise FileTransferError("destination_exists", f"destination already exists: {display}")

        file_id = str(file.get("file_id") or "").strip()
        download_url = str(file.get("download_url") or "").strip()
        file_name = str(file.get("file_name") or target.name)
        mime_type = str(file.get("mime_type") or mimetypes.guess_type(file_name)[0] or "application/octet-stream")
        if not file_id or not download_url:
            raise FileTransferError("invalid_file_param", "ChatGPT file parameter is missing file_id or download_url")

        if dry_run:
            return {
                "ok": True,
                "path": display,
                "file_id": file_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "size_bytes": None,
                "sha256": None,
                "dry_run": True,
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path, total, digest = _download_to_temp(
            download_url=download_url,
            directory=target.parent,
            max_bytes=settings.file_transfer_import_max_bytes,
        )
        try:
            # os.link is atomic and fails if the destination appeared after the
            # preflight check, so an existing file is never overwritten.
            os.link(temp_path, target)
        except FileExistsError as exc:
            raise FileTransferError("destination_exists", f"destination already exists: {display}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

        return {
            "ok": True,
            "path": display,
            "file_id": file_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "size_bytes": total,
            "sha256": digest,
            "dry_run": False,
        }
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _sha256_file(path: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FileTransferError(
                    "payload_too_large",
                    f"file exceeds FILE_TRANSFER_EXPORT_MAX_BYTES ({total} > {max_bytes})",
                )
            digest.update(chunk)
    return total, digest.hexdigest()


def _encode_resource_path(path: Path) -> str:
    return base64.urlsafe_b64encode(str(path).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_resource_path(token: str) -> str:
    if not token or len(token) > 16_384:
        raise FileTransferError("invalid_resource_uri", "invalid exported-file resource token")
    padding = "=" * (-len(token) % 4)
    try:
        value = base64.b64decode(token + padding, altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FileTransferError("invalid_resource_uri", "invalid exported-file resource token") from exc
    if "\x00" in value:
        raise FileTransferError("invalid_resource_uri", "invalid exported-file resource path")
    return value


def export_file_to_chat(*, path: str, settings: Settings) -> dict[str, Any]:
    """Create a stable MCP ResourceLink URI for a local regular file."""
    try:
        target, display = _resolve_transfer_path(path, settings, for_write=False)
        if not target.exists():
            raise FileTransferError("file_not_found", f"file does not exist: {display}")
        if not target.is_file():
            raise FileTransferError("not_a_file", f"path is not a regular file: {display}")
        size, digest = _sha256_file(target, settings.file_transfer_export_max_bytes)
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        resource_uri = f"{_RESOURCE_PREFIX}{_encode_resource_path(target)}"
        return {
            "ok": True,
            "path": display,
            "name": target.name,
            "mime_type": mime_type,
            "size_bytes": size,
            "sha256": digest,
            "resource_uri": resource_uri,
        }
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def read_export_resource(*, token: str, settings: Settings) -> bytes:
    """Resolve and read an exported-file resource, rechecking all policy limits."""
    path = _decode_resource_path(token)
    target, _display = _resolve_transfer_path(path, settings, for_write=False)
    if not target.exists() or not target.is_file():
        raise ValueError("exported file is no longer available")
    with target.open("rb") as handle:
        data = handle.read(settings.file_transfer_export_max_bytes + 1)
    if len(data) > settings.file_transfer_export_max_bytes:
        raise ValueError(
            "exported file now exceeds FILE_TRANSFER_EXPORT_MAX_BYTES "
            f"({len(data)} > {settings.file_transfer_export_max_bytes})"
        )
    return data
