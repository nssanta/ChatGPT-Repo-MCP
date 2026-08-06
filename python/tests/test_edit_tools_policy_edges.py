from __future__ import annotations

from pathlib import Path

import pytest
from test_edit_tools import make_settings

from chatrepo_mcp.edit_tools import (
    PatchApplyError,
    StaleWriteError,
    WritePolicyError,
    _combined_diff,
    _path_result,
    _run_operation,
    apply_patch_diff,
    resolve_write_dir_path,
    resolve_write_path,
    sha256_text,
    structured_error,
    write_text_file,
)
from chatrepo_mcp.security import SecurityError


def test_validation_paths_raise_expected_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    (tmp_path / "bin.bin").write_bytes(b"\x00\xff")

    try:
        write_text_file("bin.bin", "ok", settings, create_if_missing=True, dry_run=True)
        raise AssertionError("expected binary read error")
    except WritePolicyError as exc:
        assert "binary files are not writable" in str(exc)

    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff")
    try:
        write_text_file("bad.txt", "x", settings, expected_sha256=sha256_text(""), dry_run=True)
        raise AssertionError("expected non-utf8 read error")
    except WritePolicyError as exc:
        assert "not valid UTF-8" in str(exc)

    tiny = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    tiny = tiny.__class__(**{**tiny.__dict__, "max_write_file_bytes": 3})
    (tmp_path / "small.txt").write_text("old", encoding="utf-8")
    try:
        write_text_file("small.txt", "toolong", tiny, expected_sha256=sha256_text("old"), dry_run=True)
        raise AssertionError("expected content size error")
    except WritePolicyError as exc:
        assert "content exceeds MAX_WRITE_FILE_BYTES" in str(exc)


def test_resolve_path_rejects_root_non_file_and_symlink_rules(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "target.txt").write_text("ok\n", encoding="utf-8")

    with pytest.raises(WritePolicyError, match="repo file"):
        resolve_write_path(".", settings)

    with pytest.raises(WritePolicyError, match="not a file"):
        resolve_write_path("docs", settings)

    outside = tmp_path.parent / "outside_target.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "docs" / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(SecurityError, match="path escapes allowed roots"):
        resolve_write_path("docs/link.txt", settings)

    with pytest.raises(WritePolicyError, match="must not be repository root"):
        resolve_write_dir_path(".", settings)


def test_structured_error_classifies_more_error_kinds() -> None:
    assert structured_error(StaleWriteError("bad hash"))["error_kind"] == "stale_expected_hash"
    assert structured_error(ValueError("anchor not found"))["error_kind"] == "anchor_not_found"
    assert structured_error(WritePolicyError("payload exceeds MAX"))["error_kind"] == "payload_too_large"
    assert structured_error(PatchApplyError("git-style file paths"))["error_kind"] == "invalid_patch_format"


def test_combined_diff_truncation_and_unsupported_operation() -> None:
    settings = make_settings(Path("/tmp"), writable_globs=("**/*",), dangerous=True)
    settings = settings.__class__(**{**settings.__dict__, "max_combined_diff_chars": 16})
    result = _path_result(
        path="a.txt",
        changed=True,
        dry_run=True,
        diff_unified=("a\n" * 20),
        old_sha256="a",
        new_sha256="b",
        lines_added=1,
        lines_removed=1,
    )
    combined = _combined_diff([result], settings)

    assert combined.endswith("[truncated]")
    assert len(combined) > 16

    try:
        _run_operation({"op": "unsupported"}, settings, dry_run=True)
        raise AssertionError("expected unsupported op error")
    except ValueError as exc:
        assert "unsupported batch operation" in str(exc)


def test_apply_patch_rejects_stale_base_and_invalid_patch_format(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, writable_globs=("**/*",), dangerous=True, )
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    patch = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new
"""

    try:
        apply_patch_diff(patch, settings, dry_run=True, expected_base_sha="deadbeef")
        raise AssertionError("expected stale base error")
    except StaleWriteError:
        pass

    try:
        apply_patch_diff("not a patch", settings, dry_run=True)
        raise AssertionError("expected patch format error")
    except PatchApplyError as exc:
        assert "invalid unified diff format" in str(exc)
