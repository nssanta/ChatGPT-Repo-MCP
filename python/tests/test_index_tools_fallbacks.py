from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from test_command_tools import make_settings

from chatrepo_mcp import index_tools


def test_ctags_binary_returns_none_when_not_present(monkeypatch) -> None:
    index_tools._ctags_binary.cache_clear()
    monkeypatch.setattr(index_tools.shutil, "which", lambda name: None)
    assert index_tools._ctags_binary() is None


def test_ctags_binary_rejects_non_universal_version(monkeypatch) -> None:
    index_tools._ctags_binary.cache_clear()
    monkeypatch.setattr(index_tools.shutil, "which", lambda name: "/usr/bin/ctags")
    monkeypatch.setattr(index_tools, "run_bounded", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Exuberant Ctags"))

    assert index_tools._ctags_binary() is None


def test_ctags_binary_rejects_subprocess_error(monkeypatch) -> None:
    index_tools._ctags_binary.cache_clear()
    monkeypatch.setattr(index_tools.shutil, "which", lambda name: "/usr/bin/ctags")
    monkeypatch.setattr(index_tools, "run_bounded", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))

    assert index_tools._ctags_binary() is None


def test_run_ctags_recursive_retries_without_fields_arg(monkeypatch) -> None:
    monkeypatch.setattr(index_tools, "_ctags_binary", lambda: "/usr/bin/ctags")

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(
            returncode=0,
            stdout='{"_type":"tag","name":"foo","path":"main.py","line":1}\n{"foo":"bar"}\n',
        )

    monkeypatch.setattr(index_tools, "run_bounded", fake_run)
    result = index_tools._run_ctags_recursive(Path("/tmp"))

    assert calls[0][2] == "-R"
    assert len(result) == 1
    assert result[0]["name"] == "foo"


def test_get_or_build_index_uses_cache_and_skips_rebuild(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    cache_path = index_tools._cache_path(settings, "service")
    index_tools._save_cache(cache_path, [{"name": "cached"}])

    called = {"rebuild": False}

    def fake_run_ctags_recursive(path: Path) -> list[dict]:
        called["rebuild"] = True
        return []

    monkeypatch.setattr(index_tools, "_run_ctags_recursive", fake_run_ctags_recursive)

    symbols = index_tools._get_or_build_index(settings, tmp_path, "service")

    assert called["rebuild"] is False
    assert symbols == [{"name": "cached"}]


def test_incomplete_ctags_index_is_not_cached_and_public_search_falls_back(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    saved: list[list[dict]] = []
    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (tmp_path, ""))
    monkeypatch.setattr(index_tools, "_load_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        index_tools, "_run_ctags_recursive",
        lambda *args, **kwargs: (_ for _ in ()).throw(index_tools.CtagsIncompleteError("capped")),
    )
    monkeypatch.setattr(index_tools, "_save_cache", lambda path, symbols: saved.append(symbols))
    monkeypatch.setattr(
        index_tools.fs_tools, "search_text",
        lambda *args, **kwargs: {"results": [], "count": 0},
    )

    result = index_tools.workspace_symbols(settings, "missing")

    assert result["engine"] == "heuristic"
    assert saved == []


def test_normalize_tags_skips_security_restricted_symbols(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)

    class _Context:
        display = "allowed.py"

    calls = {"count": 0}

    def fake_resolve_path_context(path: str, settings, allow_hidden: bool):
        calls["count"] += 1
        if path.endswith("forbidden.py"):
            raise index_tools.SecurityError("blocked")
        return _Context()

    monkeypatch.setattr(index_tools, "resolve_path_context", fake_resolve_path_context)
    monkeypatch.setattr(index_tools, "resolve_repo_path", lambda *args, **kwargs: Path("/tmp"))

    raw_tags = [
        {"name": "ok", "path": "allowed.py", "line": 1, "kind": "function", "signature": "", "scope": None},
        {"name": "bad", "path": "forbidden.py", "line": 2, "kind": "class", "signature": "", "scope": None},
    ]

    normalized = index_tools._normalize_tags(raw_tags, tmp_path, settings)

    assert calls["count"] == 2
    assert len(normalized) == 1
    assert normalized[0]["name"] == "ok"
