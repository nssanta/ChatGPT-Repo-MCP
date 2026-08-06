from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_command_tools import make_settings
from test_edit_tools import write_allowed_file

from chatrepo_mcp import edit_tools


def test_current_text_sha256_returns_file_sha(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _, expected = write_allowed_file(tmp_path, "missions/CURRENT.md", "hello\n")

    assert edit_tools.current_text_sha256("missions/CURRENT.md", settings) == expected


def test_line_delta_and_unified_diff() -> None:
    added, removed = edit_tools._line_delta("a\n", "a\nb\n")
    assert added == 1
    assert removed == 0
    assert "+++" in edit_tools._unified_diff("f.txt", "a\n", "a\nb\n")


def test_validate_new_text_rejects_non_utf8_encodable(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with pytest.raises(edit_tools.WritePolicyError, match="content is not UTF-8 encodable"):
        edit_tools._validate_new_text("x", "bad\udcff", settings)


def test_check_expected_hash_respects_require_flag(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with pytest.raises(edit_tools.StaleWriteError):
        edit_tools._check_expected_hash("old", None, settings, "x")
    with pytest.raises(edit_tools.StaleWriteError):
        edit_tools._check_expected_hash("old", "", settings, "x")
    with pytest.raises(edit_tools.StaleWriteError):
        edit_tools._check_expected_hash("old", "expected", settings, "x")

    permissive = settings.__class__(**{**settings.__dict__, "require_expected_hash_for_writes": False})
    assert edit_tools._check_expected_hash("old", "", permissive, "x") is None
    assert edit_tools._check_expected_hash(None, None, permissive, "x") is None


def test_structured_error_maps_branches() -> None:
    assert edit_tools.structured_error(edit_tools.StaleWriteError("stale")) == {
        "ok": False,
        "error_kind": "stale_expected_hash",
        "error": "stale",
    }
    assert edit_tools.structured_error(ValueError("anchor not found in block")) == {
        "ok": False,
        "error_kind": "anchor_not_found",
        "error": "anchor not found in block",
    }
    assert edit_tools.structured_error(edit_tools.WritePolicyError("file exceeds MAX_WRITE_FILE_BYTES (20 > 10): a")) == {
        "ok": False,
        "error_kind": "payload_too_large",
        "error": "file exceeds MAX_WRITE_FILE_BYTES (20 > 10): a",
    }
    assert edit_tools.structured_error(FileNotFoundError("missing")) == {
        "ok": False,
        "error_kind": "file_not_found",
        "error": "missing",
    }


def test_resolve_write_path_requires_writable_parent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = replace(settings, writable_globs=("**/*",), dangerously_allow_all_writes=True)
    (tmp_path / "file-parent").write_text("x", encoding="utf-8")

    with pytest.raises(edit_tools.WritePolicyError):
        edit_tools.resolve_write_path("file-parent/nested.txt", settings, create_if_missing=True)


def test_resolve_write_dir_path_rejects_missing_when_not_allowed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = replace(settings, writable_globs=("**/*",), dangerously_allow_all_writes=True)

    with pytest.raises(FileNotFoundError):
        edit_tools.resolve_write_dir_path("new-dir", settings, create_if_missing=False)


def test_append_to_file_adds_newline_when_needed(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)
    target, _ = write_allowed_file(tmp_path, "notes.txt", "first")

    edit_tools.append_to_file("notes.txt", "second", settings, expected_sha256=edit_tools.sha256_text("first"), dry_run=False)
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


def test_insert_at_line_text_validates_position(tmp_path: Path) -> None:
    _ = write_allowed_file(tmp_path, "a.txt", "x\ny\n")

    with pytest.raises(ValueError):
        edit_tools._insert_at_line_text("x\ny\n", 0, "A", after=False)
    assert edit_tools._insert_at_line_text("x\ny\n", 1, "A", after=False) == "A\nx\ny\n"
    assert edit_tools._insert_at_line_text("x\ny\n", 1, "A", after=True) == "x\nA\ny\n"


def test_replace_lines_text_appends_newline_when_missing() -> None:
    assert edit_tools._replace_lines_text("a\n", 1, 1, "b") == "b\n"


def test_move_path_fails_when_destination_exists_and_not_overwrite(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)
    write_allowed_file(tmp_path, "a.txt", "one\n")
    write_allowed_file(tmp_path, "b.txt", "two\n")

    with pytest.raises(FileExistsError):
        edit_tools.move_path(
            "a.txt",
            "b.txt",
            settings,
            expected_sha256=edit_tools.sha256_text("one\n"),
            overwrite=False,
            dry_run=False,
        )


def test_batch_edit_files_rejects_too_many_operations(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "max_batch_operations": 1})

    with pytest.raises(ValueError):
        edit_tools.batch_edit_files(
            [
                {"op": "ensure_directory", "path": "docs"},
                {"op": "ensure_directory", "path": "tmp"},
            ],
            settings,
        )


def test_apply_change_set_invalid_payload_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert edit_tools.apply_change_set([], settings)["ok"] is False
    assert edit_tools.apply_change_set(["bad"], settings)["ok"] is False
    assert edit_tools.apply_change_set([{"bad": True}], settings)["ok"] is False


def test_restore_snapshot_applies_text_and_deletes_missing(tmp_path: Path) -> None:
    target = tmp_path / "memo.txt"
    target.write_text("old", encoding="utf-8")
    gone = tmp_path / "gone.txt"
    gone.write_text("tmp\n", encoding="utf-8")

    snapshot = {
        str(target): "new\n",
        str(gone): None,
    }
    edit_tools._restore_snapshot(snapshot, make_settings(tmp_path))

    assert target.read_text(encoding="utf-8") == "new\n"
    assert not gone.exists()


def test_snapshot_paths_collects_referenced_keys(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)
    write_allowed_file(tmp_path, "a.txt", "one\n")

    operations = [
        {"op": "write", "path": "a.txt"},
        {"op": "move", "source_path": "a.txt", "destination_path": "b.txt"},
        {"op": "write", "path": "missing.txt"},
    ]
    snapshot = edit_tools._snapshot_paths(operations, settings)

    assert any(item.endswith("a.txt") for item in snapshot)


def test_apply_patch_diff_success_and_not_applied_when_dry_run(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    file_path = tmp_path / "main.txt"
    file_path.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=ci@example.com",
            "-c",
            "user.name=ci",
            "commit",
            "-m",
            "seed",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True)
    patch = """diff --git a/main.txt b/main.txt\n--- a/main.txt\n+++ b/main.txt\n@@ -1 +1 @@\n-old\n+new\n"""

    output = edit_tools.apply_patch_diff(patch, settings, dry_run=True, repo=str(tmp_path), expected_base_sha=head.stdout.strip())
    assert output["changed"] is True
    assert output["applied"] is False
    assert file_path.read_text(encoding="utf-8") == "old\n"


def test_apply_patch_diff_rejects_too_large_patch(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings = settings.__class__(**{**settings.__dict__, "max_patch_bytes": 1})

    with pytest.raises(edit_tools.WritePolicyError):
        edit_tools.apply_patch_diff("diff", settings, dry_run=True)


def test_run_operation_unknown_type_raises(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), writable_globs=("**/*",), dangerously_allow_all_writes=True)

    with pytest.raises(ValueError, match="unsupported batch operation"):
        edit_tools._run_operation({"op": "unsupported"}, settings, dry_run=True)
