from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

from test_command_tools import make_settings

from chatrepo_mcp import fs_tools, index_tools, lsp_tools


def test_repo_info_is_complete(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    info = fs_tools.repo_info(settings)

    assert info["project_root"] == str(tmp_path.resolve())
    assert info["exists"] is True
    assert info["is_dir"] is True
    assert info["config"]["transport"] == settings.transport
    assert "binary_globs" not in info["config"]


def test_tree_respects_max_tree_entries_and_counts_entries(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c\n", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "deep.txt").write_text("d\n", encoding="utf-8")

    settings = replace(settings := make_settings(tmp_path), max_tree_entries=3)
    result = fs_tools.tree(".", settings, depth=1)

    assert result["path"] == "."
    assert result["max_entries"] == 3
    assert result["entries"] == 3
    assert "a.txt" in result["tree"]
    assert "subdir" in result["tree"]


def test_list_dir_respects_limit_and_truncation(tmp_path: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")

    settings = replace(settings := make_settings(tmp_path), max_tree_entries=2)
    result = fs_tools.list_dir(".", settings, limit=10)

    assert len(result["entries"]) == 2
    assert result["truncated"] is False


def test_iter_files_works_for_direct_file_input(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "direct.txt"
    target.write_text("x", encoding="utf-8")

    files = list(fs_tools._iter_files(tmp_path, target, settings, allow_hidden=True))
    assert files == [target]


def test_safe_rel_and_entry_allowed_handle_path_escape_and_hidden(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    outside_root = tmp_path.parent / "outside_target"
    outside_root.mkdir()
    if outside_root.exists() and outside_root.is_symlink():
        outside_root.unlink()

    assert fs_tools._safe_rel(tmp_path, outside_root) is None

    settings_no_hidden = replace(settings, allow_hidden_default=False)
    ok, rel = fs_tools._entry_allowed(tmp_path, tmp_path / ".hidden", settings_no_hidden, allow_hidden=False)
    assert ok is False
    assert rel == ".hidden"


def test_read_text_file_truncates_output_when_limit_small_and_without_line_numbers(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_response_chars=12)
    source = tmp_path / "long.txt"
    source.write_text("one two three four five\n", encoding="utf-8")

    result = fs_tools.read_text_file("long.txt", settings, start_line=1, end_line=5, with_line_numbers=False)

    assert result["start_line"] == 1
    assert result["end_line"] == 1
    assert result["content"].endswith("\n...[truncated]")


def test_read_text_file_binary_guard_can_be_forced_via_monkeypatch(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "file.bin"
    source.write_text("binary", encoding="utf-8")
    monkeypatch.setattr(fs_tools, "_is_probably_text", lambda _: False)

    try:
        fs_tools.read_text_file("file.bin", settings)
        assert False, "expected unsupported binary error"
    except ValueError as exc:
        assert "unsupported or binary file" in str(exc)


def test_read_multiple_files_collects_success_and_error_rows(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")

    result = fs_tools.read_multiple_files(["ok.txt", "missing.txt"], settings)

    assert len(result["files"]) == 2
    assert result["files"][0]["path"] == "ok.txt"
    assert result["files"][0]["start_line"] == 1
    assert result["files"][0]["end_line"] == 1
    assert result["files"][1]["path"] == "missing.txt"
    assert "not a file: missing.txt" in result["files"][1]["error"]


def test_file_metadata_respects_include_stat_false(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "meta.txt"
    target.write_text("meta", encoding="utf-8")

    result = fs_tools.file_metadata("meta.txt", settings, include_stat=False)

    assert result["path"] == "meta.txt"
    assert result["type"] == "file"
    assert "size" not in result


def test_search_text_blocks_paths_filtered_out_and_returns_empty_when_group_empty(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "blocked_target.txt"
    target.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        fs_tools,
        "_entry_allowed",
        lambda root, path, settings, allow_hidden: (False, None),
    )

    result = fs_tools.search_text("x", settings, paths=["blocked_target.txt"])

    assert result["results"] == []
    assert result["count"] == 0


def test_search_text_ignores_unparseable_ripgrep_lines(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")

    class _Process:
        returncode = 0
        stdout = io.StringIO("not-a-rg-line\nanother:bad:line")

        def wait(self, timeout=None):
            del timeout
            return self.returncode

        def kill(self):
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

    monkeypatch.setattr(fs_tools.subprocess, "Popen", lambda *args, **kwargs: _Process())

    result = fs_tools.search_text("needle", settings, path="a.py")

    assert result["results"] == []
    assert result["count"] == 0


def test_dependency_map_file_target_and_parse_errors(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    direct = fs_tools.dependency_map(settings, path="requirements.txt")
    assert direct["count"] == 1
    assert direct["manifests"]["requirements.txt"] == ["requests"]

    (tmp_path / "package.json").write_text("{", encoding="utf-8")
    package = fs_tools.dependency_map(settings, path=tmp_path)
    assert isinstance(package["manifests"]["package.json"], dict)
    assert "error" in package["manifests"]["package.json"]


def test_dependency_map_includes_blocked_check(tmp_path: Path) -> None:
    base = make_settings(tmp_path)
    settings = replace(base, blocked_globs=(".secret/**",))
    (tmp_path / ".secret").mkdir()
    (tmp_path / ".secret" / "requirements.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("y\n", encoding="utf-8")

    result = fs_tools.dependency_map(settings)

    assert ".secret/requirements.txt" not in result["manifests"]
    assert result["manifests"]["requirements.txt"] == ["y"]


def test_rg_exclude_globs_skips_secret_patterns_when_secret_access_enabled(tmp_path: Path) -> None:
    settings = replace(
        make_settings(tmp_path),
        allow_secret_access=True,
        blocked_globs=("*.pem",),
        secret_globs=("*.pem",),
    )
    globs = fs_tools._rg_exclude_globs(settings)

    assert globs == []


def test_ctags_available_uses_ctags_binary(tmp_path: Path, monkeypatch) -> None:
    index_tools._ctags_binary.cache_clear()
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: "/usr/bin/ctags")
    assert index_tools._ctags_available() is True

    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: None)
    assert index_tools._ctags_available() is False


def test_index_cache_path_and_load_cache_variants(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    cache = index_tools._cache_path(settings, "svc/pkg")
    assert "svc__pkg" in str(cache)

    missing = index_tools._load_cache(tmp_path / "missing.json")
    assert missing is None

    broken = tmp_path / ".cache"
    broken.mkdir()
    cache_file = broken / "broken.json"
    cache_file.write_text("...", encoding="utf-8")
    assert index_tools._load_cache(cache_file) is None


def test_parse_ctags_json_lines_filters_bad_rows() -> None:
    lines = (
        "not-json\n"
        '{"_type":"other","name":"x"}\n'
        '{"_type":"tag","name":"good"}\n'
    )
    result = index_tools._parse_ctags_json_lines(lines)

    assert result == [{"_type": "tag", "name": "good"}]


def test_ctags_file_and_recursive_missing_binary_return_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: None)

    assert index_tools._run_ctags_file(tmp_path) == []
    assert index_tools._run_ctags_recursive(tmp_path) == []


def test_run_ctags_file_ignores_subprocess_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: "/usr/bin/ctags")

    def _run(cmd, *args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(index_tools, "run_bounded", _run)

    assert index_tools._run_ctags_file(tmp_path / "x.py") == []
    assert index_tools._run_ctags_recursive(tmp_path) == []


def test_normalize_tags_skips_invalid_and_relative_entries(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    tmp_root = settings.project_root

    called = {"count": 0}

    class _Context:
        display = "ok.py"

    def fake_resolve(path: str, settings, *, allow_hidden: bool):
        called["count"] += 1
        if path.endswith("bad.py"):
            raise index_tools.SecurityError("blocked")
        return _Context()

    monkeypatch.setattr(index_tools, "resolve_path_context", fake_resolve)
    result = index_tools._normalize_tags(
        [
            {"name": "ok", "path": "ok.py"},
            {"name": "bad", "path": "bad.py"},
            {"name": "nopath"},
        ],
        tmp_root,
        settings,
    )

    assert called["count"] == 2
    assert len(result) == 1
    assert result[0]["path"] == "ok.py"


def test_document_symbols_returns_none_and_not_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    result = index_tools.document_symbols(settings, "missing.py")

    assert result["ok"] is False
    assert result["path"] == "missing.py"
    assert result["error"] == "not a file"


def test_pyright_diagnostics_binary_missing_and_run_failure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: None)
    assert lsp_tools._pyright_diagnostics(tmp_path, None, settings) is None

    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/usr/bin/pyright")
    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: None)
    assert lsp_tools._pyright_diagnostics(tmp_path, None, settings) is None


def test_normalize_path_and_ruff_output_parse_paths(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    assert lsp_tools._normalize_path(tmp_path, None, settings) == ""

    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: (0, "[", ""))
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/usr/bin/ruff")
    assert lsp_tools._ruff_diagnostics(tmp_path, None, settings) is None


def test_ruff_diagnostics_marks_critical_error_by_prefix(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    payload = '[{"filename":"bad.py","location":{"row":2,"column":3},"code":"E902","message":"error"}]'
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/usr/bin/ruff")
    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: (0, payload, ""))

    diagnostics = lsp_tools._ruff_diagnostics(tmp_path, None, settings)

    assert diagnostics == [
        {"path": "bad.py", "line": 2, "col": 3, "severity": "error", "code": "E902", "message": "error"},
    ]


def test_compileall_and_tsc_handles_missing_tools_and_subprocess_failures(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: None)
    assert lsp_tools._compileall_diagnostics(tmp_path, None, settings) == []

    monkeypatch.setattr(lsp_tools.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: None)
    assert lsp_tools._compileall_diagnostics(tmp_path, None, settings) == []

    monkeypatch.setattr(lsp_tools, "_find_tsc", lambda cwd: None)
    assert lsp_tools._ts_diagnostics(tmp_path, settings, None) == (
        [],
        [],
        [{"tool": "tsc", "install_hint": "npm install -D typescript  (or: npm install -g typescript)"}],
    )


def test_tsc_find_local_binary_and_command_failure(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    local_bin = tmp_path / "node_modules" / ".bin"
    (local_bin).mkdir(parents=True)
    (local_bin / "tsc").write_text("#!/bin/sh\necho tsc\n", encoding="utf-8")

    assert lsp_tools._find_tsc(tmp_path) == str(local_bin / "tsc")

    monkeypatch.setattr(lsp_tools, "_run", lambda cmd, cwd: None)
    assert lsp_tools._ts_diagnostics(tmp_path, settings, None) == ([], [], [])


def test_code_diagnostics_auto_runs_ts_and_dedupes_missing_tools(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(lsp_tools.workspace, "detect_stack", lambda cwd: {"ts"})
    monkeypatch.setitem(lsp_tools._LANGUAGE_RUNNERS, "ts", lambda cwd, settings, paths: ([], [], []))
    monkeypatch.setattr(lsp_tools, "_ts_diagnostics", lambda cwd, settings, paths: ([], [], []))
    ts_result = lsp_tools.code_diagnostics(settings, language="auto")
    assert ts_result["language"] == "ts"
    assert ts_result["tool_used"] == []

    monkeypatch.setattr(lsp_tools.workspace, "detect_stack", lambda cwd: {"go", "python"})
    monkeypatch.setitem(lsp_tools._LANGUAGE_RUNNERS, "go", lambda cwd, settings, paths: ([], ["go vet ."], [{"tool": "python", "install_hint": "install python checker"}]))
    monkeypatch.setattr(
        lsp_tools,
        "_go_diagnostics",
        lambda cwd, settings, paths: ([], ["go vet ."], [{"tool": "python", "install_hint": "install python checker"}]),
    )
    monkeypatch.setitem(lsp_tools._LANGUAGE_RUNNERS, "python", lambda cwd, settings, paths: ([], ["ruff"], [{"tool": "python", "install_hint": "different"}]))
    monkeypatch.setattr(
        lsp_tools,
        "_python_diagnostics",
        lambda cwd, settings, paths: ([], ["ruff"], [{"tool": "python", "install_hint": "different"}]),
    )

    deduped = lsp_tools.code_diagnostics(settings, language="auto")
    assert deduped["missing_tools"] == [{"tool": "python", "install_hint": "install python checker"}]
