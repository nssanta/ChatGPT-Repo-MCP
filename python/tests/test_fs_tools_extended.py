from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

from test_command_tools import make_settings

from chatrepo_mcp import fs_tools


def test_symlink_target_outside_root_is_hidden_in_list_dir(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    outside = tmp_path.parent / "outside_path_not_in_root"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")

    link = tmp_path / "linked"
    link.symlink_to(outside)

    result = fs_tools.list_dir(".", settings=settings, include_hidden=True, limit=100)
    names = {item["name"] for item in result["entries"]}

    assert "linked" not in names


def test_read_multiple_files_validates_input_size_and_count(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")

    try:
        fs_tools.read_multiple_files([], settings)
        assert False, "expected ValueError for empty list"
    except ValueError as exc:
        assert "paths must not be empty" in str(exc)

    small = make_settings(tmp_path)
    small = small.__class__(**{**small.__dict__, "max_read_files": 1})
    try:
        fs_tools.read_multiple_files(["a.txt", "b.txt"], small)
        assert False, "expected ValueError for too many paths"
    except ValueError as exc:
        assert "too many paths; max is 1" in str(exc)


def test_read_text_file_rejects_invalid_line_range(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "main.txt"
    target.write_text("one\n", encoding="utf-8")

    try:
        fs_tools.read_text_file("main.txt", settings, start_line=3, end_line=2)
        assert False, "expected ValueError for end_line < start_line"
    except ValueError as exc:
        assert "end_line must be >=" in str(exc)


def test_search_text_raises_for_unsupported_rg_exit_code(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello\n", encoding="utf-8")

    calls: list[list[str]] = []

    class FakeStdout:
        def __iter__(self):
            return iter(())

        def close(self) -> None:
            return None

    class FakeProcess:
        stdout = FakeStdout()

        def wait(self, timeout=None):
            del timeout
            return 2

        def kill(self):
            return None

    def fake_popen(cmd, *args, **kwargs):
        calls.append(list(cmd))
        kwargs["stderr"].write(b"rg failure\n")
        return FakeProcess()

    monkeypatch.setattr(fs_tools.subprocess, "Popen", fake_popen)

    try:
        fs_tools.search_text("hello", settings, path=".")
        assert False, "expected RuntimeError when rg exit code is unsupported"
    except RuntimeError as exc:
        assert "rg failure" in str(exc)

    assert any(item[0] == "rg" for item in calls)


def test_search_and_symbol_paths_are_resolved_and_de_duped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "one.py").write_text("def alpha():\\n    pass\\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("class alpha:\\n    pass\\n", encoding="utf-8")

    result = fs_tools.symbol_search("alpha", settings, limit=10)

    assert result["count"] >= 1
    seen = set()
    for item in result["results"]:
        path_value = item["path"]
        assert path_value not in seen
        seen.add(path_value)


def test_dependency_map_parses_supported_manifests_and_respects_blocked_paths(tmp_path: Path) -> None:
    base_settings = make_settings(tmp_path)
    settings = replace(
        base_settings,
        allow_hidden_default=True,
        blocked_globs=(*base_settings.blocked_globs, "**/.venv/**"),
    )

    (tmp_path / "pyproject.toml").write_text("[project]\\nname = 'x'\\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{\"name\":\"x\"}\\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/x\\n\\ngo 1.22\\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\\nname='x'\\n", encoding="utf-8")

    hidden_dir = tmp_path / ".venv"
    hidden_dir.mkdir()
    (hidden_dir / "requirements.txt").write_text("blocked\\n", encoding="utf-8")

    result = fs_tools.dependency_map(settings)

    keys = set(result["manifests"])
    assert ".chatrepo-mcp/pyproject.toml" not in keys
    assert "pyproject.toml" in keys
    assert "requirements.txt" in keys
    assert "package.json" in keys
    assert "go.mod" in keys
    assert "Cargo.toml" in keys
    assert ".venv/requirements.txt" not in keys


def test_dependency_map_stores_parse_error_for_invalid_manifest(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project\\nname='broken'\\n", encoding="utf-8")

    result = fs_tools.dependency_map(settings)
    entry = result["manifests"].get("pyproject.toml")

    assert isinstance(entry, dict)
    assert "error" in entry


def test_recent_changes_orders_by_mtime_and_todo_scan_finds_tokens(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("TODO old\\n", encoding="utf-8")
    new.write_text("FIXME new\\n", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 120, now - 120))
    os.utime(new, (now, now))

    changed = fs_tools.recent_changes(settings, limit=1)
    todo = fs_tools.todo_scan(settings, limit=10)

    assert changed["count"] == 1
    assert changed["files"][0]["path"].endswith("new.txt")
    assert {item["path"].split("/")[-1] for item in todo["results"]} == {"new.txt", "old.txt"}
