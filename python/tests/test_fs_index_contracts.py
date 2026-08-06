from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_command_tools import make_settings

from chatrepo_mcp import fs_tools, index_tools


def test_fs_is_probably_text_recognizes_special_filenames() -> None:
    assert fs_tools._is_probably_text(Path("Dockerfile")) is True
    assert fs_tools._is_probably_text(Path("requirements.txt")) is True


def test_fs_read_text_rejects_file_when_exceeds_max_bytes(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_file_bytes=4)
    (tmp_path / "big.txt").write_text("12345", encoding="utf-8")

    with pytest.raises(ValueError, match="file exceeds MAX_FILE_BYTES"):
        fs_tools.read_text_file("big.txt", settings)


def test_entry_allowed_blocks_symlink_escaping_project_root(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    outside = tmp_path.parent / "outside_escape_target.txt"
    outside.write_text("x", encoding="utf-8")
    link = tmp_path / "escape.link"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)

    ok, rel = fs_tools._entry_allowed(tmp_path, link, settings, allow_hidden=True)
    assert ok is False
    assert rel == "escape.link"


def test_list_dir_and_tree_reject_file_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory: file.txt"):
        fs_tools.list_dir("file.txt", settings)
    with pytest.raises(ValueError, match="not a directory: file.txt"):
        fs_tools.tree("file.txt", settings)


def test_read_text_file_clamps_start_and_end_ranges(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "sample.txt").write_text("a\nb\nc", encoding="utf-8")

    result = fs_tools.read_text_file("sample.txt", settings, start_line=0, end_line=999)
    assert result["start_line"] == 1
    assert result["end_line"] == 3
    assert result["line_count"] == 3


def test_find_files_rejects_non_dir_target(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    file_path = tmp_path / "single.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory: single.txt"):
        fs_tools.find_files("*.txt", settings, path="single.txt")


def test_find_files_limits_matches_to_requested_cap(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_tree_entries=3)
    for idx in range(4):
        (tmp_path / f"a{idx}.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ignore.md").write_text("x", encoding="utf-8")

    result = fs_tools.find_files("a*.txt", settings, path=".", limit=1)
    assert result["count"] == 1
    assert len(result["matches"]) == 1


def test_search_text_filters_out_blocked_relative_path_results(tmp_path: Path, monkeypatch) -> None:
    settings = replace(make_settings(tmp_path), blocked_globs=("skip.py",))
    (tmp_path / "skip.py").write_text("needle", encoding="utf-8")

    class _Result:
        returncode = 0
        stdout = "skip.py:1:needle\n"
        stderr = ""

    monkeypatch.setattr(fs_tools.subprocess, "run", lambda *args, **kwargs: _Result())

    result = fs_tools.search_text("needle", settings, path=".")
    assert result["results"] == []
    assert result["count"] == 0


def test_search_text_stops_after_limit_is_reached(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "a.txt").write_text("x\nx", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")

    class _Result:
        returncode = 0
        stdout = "a.txt:1:one\nb.txt:1:two\n"
        stderr = ""

    monkeypatch.setattr(fs_tools.subprocess, "run", lambda *args, **kwargs: _Result())

    result = fs_tools.search_text("x", settings, paths=["a.txt", "b.txt"], limit=1)
    assert result["count"] == 1
    assert len(result["results"]) == 1


def test_symbol_search_deduplicates_pattern_matches(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def fake_search_text(query, settings: object, *, path=".", paths=None, regex=False, case_sensitive=False, limit=100):
        del query, path, paths, regex, case_sensitive  # interface stability only
        return {
            "results": [
                {"path": "x.py", "line": 1, "text": "def foo():", "source": "regex"},
            ],
            "count": 1,
        }

    monkeypatch.setattr(fs_tools, "search_text", fake_search_text)
    result = fs_tools.symbol_search("foo", settings, paths=["x.py"])
    assert len(result["results"]) == 1


def test_dependency_map_parses_pyproject_package_json_and_go_mod(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["requests>=2.0"]

[project.optional-dependencies]
dev = ["pytest"]

[tool.poetry.dependencies]
python = "^3.11"
click = "8"

[tool.poetry.group]
dev-dependencies = ["mypy"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.0.0",
                "dependencies": {"left-pad": "1.3"},
                "devDependencies": {"ruff": "0.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text(
        "module demo\n\ngo 1.22\n\nrequire (\n\tgithub.com/a v1.0.0\n\tgithub.com/b v2.0.0 // comment\n)\nrequire github.com/c v3.0.0\n",
        encoding="utf-8",
    )

    result = fs_tools.dependency_map(settings)
    manifests = result["manifests"]

    assert result["count"] >= 4
    assert manifests["requirements.txt"] == ["requests"]
    assert manifests["pyproject.toml"]["project.dependencies"] == ["requests>=2.0"]
    assert manifests["package.json"]["dependencies"]["left-pad"] == "1.3"
    assert "github.com/a v1.0.0" in manifests["go.mod"]


def test_dependency_map_skips_blocked_manifests(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), blocked_globs=("go.mod",))
    (tmp_path / "go.mod").write_text("module demo\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    result = fs_tools.dependency_map(settings)
    assert "go.mod" not in result["manifests"]
    assert "requirements.txt" in result["manifests"]


def test_index_ctags_binary_detects_universal_variant(monkeypatch) -> None:
    index_tools._ctags_binary.cache_clear()
    monkeypatch.setattr(index_tools.shutil, "which", lambda name: "/usr/bin/ctags")
    monkeypatch.setattr(
        index_tools,
        "run_bounded",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Universal Ctags"),
    )
    assert index_tools._ctags_binary() == "/usr/bin/ctags"


def test_index_save_cache_ignores_filesystem_error(tmp_path: Path, monkeypatch) -> None:
    cache_path = tmp_path / "symbols" / "symbols.json"
    original_mkdir = index_tools.Path.mkdir

    def broken_mkdir(self, *args, **kwargs):
        if self == cache_path.parent:
            raise OSError("no fs")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(index_tools.Path, "mkdir", broken_mkdir)
    index_tools._save_cache(cache_path, [])


def test_index_load_cache_rejects_non_list_payload(tmp_path: Path) -> None:
    cache_path = tmp_path / "symbols.json"
    cache_path.write_text(
        json.dumps({"generated_at": time.time(), "symbols": {"bad": "payload"}}),
        encoding="utf-8",
    )
    assert index_tools._load_cache(cache_path) is None


def test_run_ctags_file_returns_empty_when_command_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: "/usr/bin/ctags")
    calls = {"count": 0}

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(index_tools, "run_bounded", fake_run)
    assert index_tools._run_ctags_file(tmp_path / "x.py") == []
    assert calls["count"] == 2


def test_index_normalize_tags_with_absolute_paths(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "abs.py"
    source.write_text("x", encoding="utf-8")

    class Context:
        display = "abs.py"

    monkeypatch.setattr(index_tools, "resolve_path_context", lambda path, settings, allow_hidden: Context())
    result = index_tools._normalize_tags([{"name": "abs", "path": str(source.resolve())}], tmp_path, settings)
    assert result == [{"name": "abs", "path": "abs.py", "line": None, "kind": None, "signature": None, "scope": None}]


def test_index_resolve_index_scope_defaults_to_workspace_root(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    toplevel, repo_rel = index_tools._resolve_index_scope(settings, None)

    assert toplevel == tmp_path.resolve()
    assert repo_rel == index_tools.git_tools._repo_rel(toplevel, settings)


def test_get_or_build_index_builds_and_saves_cache_when_missing(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    cache_path = tmp_path / "cache" / "symbols.json"

    monkeypatch.setattr(index_tools, "_cache_path", lambda settings, repo_rel: cache_path)
    monkeypatch.setattr(
        index_tools, "_run_ctags_recursive",
        lambda target, max_bytes=1_000_000: [{"name": "foo", "path": "main.py"}],
    )
    monkeypatch.setattr(index_tools, "_normalize_tags", lambda raw_tags, base_dir, settings: [{"name": "foo", "path": "main.py", "line": 1}])

    symbols = index_tools._get_or_build_index(settings, tmp_path, "repo")
    assert symbols == [{"name": "foo", "path": "main.py", "line": 1}]
    assert cache_path.exists()


def test_symbol_definition_with_ctags_kind_filter_keeps_only_requested_kind(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (tmp_path, "svc"))
    monkeypatch.setattr(
        index_tools,
        "_get_or_build_index",
        lambda settings, toplevel, repo_rel: [
            {"name": "Foo", "kind": "function", "path": "a.py"},
            {"name": "Foo", "kind": "class", "path": "b.py"},
        ],
    )

    result = index_tools.symbol_definition(settings, "Foo", kind="class")
    assert result["engine"] == "ctags"
    assert result["count"] == 1
    assert result["definitions"][0]["path"] == "b.py"


def test_symbol_definition_heuristic_path_respects_limit(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(index_tools, "_ctags_available", lambda: False)

    def fake_symbol_search(symbol: str, settings: object, *, path: str = ".", limit: int = 10, **kwargs):
        del symbol, path, kwargs
        return {
            "results": [
                {"path": "x.py", "line": 1, "text": "def one():"},
                {"path": "x.py", "line": 2, "text": "class C:"},
            ],
            "count": 2,
        }

    monkeypatch.setattr(index_tools.fs_tools, "symbol_search", fake_symbol_search)
    result = index_tools.symbol_definition(settings, "x", limit=1)

    assert result["engine"] == "heuristic"
    assert result["count"] == 1


def test_document_symbols_repo_relative_path_uses_repo_root_and_ctags_fallback(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target = repo_root / "main.py"
    target.write_text("class A:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (repo_root, "repo"))
    monkeypatch.setattr(index_tools, "_run_ctags_file", lambda target: (_ for _ in ()).throw(RuntimeError("boom")))

    result = index_tools.document_symbols(settings, "main.py", repo="repo")
    assert result["engine"] == "heuristic"
    assert result["symbols"][0]["kind"] == "class"
