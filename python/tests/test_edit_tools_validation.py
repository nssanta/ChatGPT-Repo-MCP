from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from chatrepo_mcp import edit_tools
from test_command_tools import make_settings
from test_edit_tools import write_allowed_file
import pytest


def test_read_existing_text_rejects_oversize_and_binary_content(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_write_file_bytes=3)

    oversized = tmp_path / "big.txt"
    oversized.write_text("1234", encoding="utf-8")
    with pytest.raises(edit_tools.WritePolicyError, match="file exceeds MAX_WRITE_FILE_BYTES"):
        edit_tools._read_existing_text(oversized, settings)

    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"x\x00y")
    with pytest.raises(edit_tools.WritePolicyError, match="binary files are not writable"):
        edit_tools._read_existing_text(binary, make_settings(tmp_path))


def test_validate_new_text_rejects_binary_and_too_large(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_write_file_bytes=3)

    with pytest.raises(edit_tools.WritePolicyError, match="binary content is not writable"):
        edit_tools._validate_new_text("a.txt", "x\x00y", settings)
    with pytest.raises(edit_tools.WritePolicyError, match="content exceeds MAX_WRITE_FILE_BYTES"):
        edit_tools._validate_new_text("a.txt", "1234", settings)


def test_is_writable_relative_blocks_secret_and_allows_secret_with_mode(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert edit_tools._is_writable_relative(".env", settings) is False



def test_resolve_write_path_and_dir_specific_permission_edges(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(FileNotFoundError, match="file does not exist"):
        edit_tools.resolve_write_path("missions/new.txt", settings)

    settings = replace(settings, writable_globs=("tmp/*.txt",), dangerously_allow_all_writes=False)
    with pytest.raises(edit_tools.WritePolicyError, match="parent path is not writable by policy"):
        edit_tools.resolve_write_path("tmp/new.txt", settings, create_if_missing=True)

    (tmp_path / "file-parent").write_text("x", encoding="utf-8")
    with pytest.raises(edit_tools.WritePolicyError, match="parent path is not a directory"):
        edit_tools.resolve_write_path("file-parent/nested.txt", settings=replace(settings, writable_globs=("**/*",), dangerously_allow_all_writes=True), create_if_missing=True)


def test_resolve_write_dir_path_rejects_root_and_file_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(edit_tools.WritePolicyError, match="directory path must not be repository root"):
        edit_tools.resolve_write_dir_path(".", settings)

    target = tmp_path / "f.txt"
    target.write_text("x\n", encoding="utf-8")
    with pytest.raises(edit_tools.WritePolicyError, match="not a directory"):
        edit_tools.resolve_write_dir_path("f.txt", settings)


def test_structured_error_branches_for_paths_and_patch() -> None:
    assert edit_tools.structured_error(edit_tools.WritePolicyError("path is not writable by policy: f")) == {
        "ok": False,
        "error_kind": "path_not_writable",
        "error": "path is not writable by policy: f",
    }
    assert edit_tools.structured_error(edit_tools.PatchApplyError("reject")) == {
        "ok": False,
        "error_kind": "patch_rejected",
        "error": "reject",
    }


def test_create_text_file_rejects_existing_without_overwrite(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)
    write_allowed_file(tmp_path, "dup.txt", "old\n")

    with pytest.raises(FileExistsError):
        edit_tools.create_text_file("dup.txt", "new\n", settings, overwrite=False, dry_run=False)


def test_replace_insert_delete_invalid_inputs_and_rollback(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)

    path, old_hash = write_allowed_file(tmp_path, "base.txt", "one\n")

    with pytest.raises(ValueError, match="find must not be empty"):
        edit_tools.replace_text_in_file("base.txt", "", "x", settings, expected_sha256=old_hash, dry_run=False)
    with pytest.raises(ValueError, match="position must be 'before' or 'after'"):
        edit_tools.insert_text_in_file("base.txt", "x", "middle", "y", settings, expected_sha256=old_hash, dry_run=False)
    with pytest.raises(ValueError, match="provide either find or start_line/end_line"):
        edit_tools.delete_text_in_file("base.txt", settings, expected_sha256=old_hash, dry_run=False)

    result = edit_tools.batch_edit_files(
        [
            {
                "op": "write",
                "path": "base.txt",
                "content": "two\n",
                "expected_sha256": old_hash,
            },
            {
                "op": "replace",
                "path": "missing.txt",
                "find": "x",
                "replace": "y",
                "expected_sha256": "abc",
            },
        ],
        settings,
        atomic=True,
        dry_run=False,
    )

    assert result["rollback_performed"] is True
    assert path.read_text(encoding="utf-8") == "one\n"


def test_update_current_mission_validation_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, _ = write_allowed_file(tmp_path, "missions/CURRENT.md", "## Goal\n\nstart\n")

    with pytest.raises(ValueError, match="position must be 'before_goal'"):
        edit_tools.update_current_mission("x", "y", settings, position="after")
    with pytest.raises(ValueError, match="unsupported mission preset"):
        edit_tools.update_current_mission("x", "y", settings, preset="unsupported")
    with pytest.raises(ValueError, match="provide preset, chunks, or section_title/content"):
        edit_tools.update_current_mission("", "", settings)
