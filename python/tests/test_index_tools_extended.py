from __future__ import annotations

from chatrepo_mcp import index_tools
from chatrepo_mcp import git_tools
from test_command_tools import make_settings


def test_ctags_symbol_definition_falls_back_when_lookup_fails(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (tmp_path, "service"))
    monkeypatch.setattr(
        index_tools,
        "_get_or_build_index",
        lambda settings, toplevel, repo_rel: (_ for _ in ()).throw(RuntimeError("ctags failed")),
    )

    def fake_symbol_search(symbol: str, settings, *, path: str = ".", regex: bool = False, limit: int = 10) -> dict:
        return {
            "ok": True,
            "query": symbol,
            "count": 1,
            "results": [{"path": "service/main.py", "line": 3, "text": "def hello_world():", "source": "regex"}],
        }

    monkeypatch.setattr(index_tools.fs_tools, "symbol_search", fake_symbol_search)

    result = index_tools.symbol_definition(settings, "hello_world", repo="service", limit=5)

    assert result["engine"] == "heuristic"
    assert result["count"] == 1
    assert result["definitions"][0]["kind"] == "function"


def test_document_symbols_prefers_ctags_when_available(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "service" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("def x():\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(
        index_tools,
        "_run_ctags_file",
        lambda target_path: [{"name": "x", "kind": "function", "line": 1, "signature": "x()", "scope": None}],
    )

    result = index_tools.document_symbols(settings, str(target))

    assert result["engine"] == "ctags"
    assert len(result["symbols"]) == 1
    assert result["symbols"][0]["name"] == "x"


def test_document_symbols_falls_back_to_heuristics_when_ctags_empty(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "service" / "util.py"
    target.parent.mkdir(parents=True)
    target.write_text("class X:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_run_ctags_file", lambda target_path: [])

    result = index_tools.document_symbols(settings, str(target), repo=None)

    assert result["engine"] == "heuristic"
    assert result["symbols"][0]["kind"] == "class"


def test_workspace_symbols_prefers_ctags_and_filters_query(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    cached = [
        {"name": "alpha", "path": "a.py", "kind": "function", "line": 2, "scope": "mod"},
        {"name": "beta", "path": "b.py", "kind": "class", "line": 1, "scope": "mod"},
    ]

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (tmp_path, ""))
    monkeypatch.setattr(index_tools, "_get_or_build_index", lambda settings, toplevel, repo_rel: cached)

    result = index_tools.workspace_symbols(settings, "be", limit=10)

    assert result["engine"] == "ctags"
    assert result["count"] == 1
    assert result["symbols"][0]["name"] == "beta"


def test_workspace_symbols_falls_back_to_fs_search_on_ctags_error(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(index_tools, "_ctags_available", lambda: True)
    monkeypatch.setattr(index_tools, "_resolve_index_scope", lambda settings, repo: (tmp_path, ""))
    monkeypatch.setattr(
        index_tools,
        "_get_or_build_index",
        lambda settings, toplevel, repo_rel: (_ for _ in ()).throw(RuntimeError("cache parse")),
    )

    def fake_search(query: str, settings, *, path: str = ".", regex: bool = False, case_sensitive: bool = True, limit: int = 10):
        return {
            "ok": True,
            "count": 1,
            "query": query,
            "results": [{"path": "main.py", "line": 9, "text": "def search_me():"}],
        }

    monkeypatch.setattr(index_tools.fs_tools, "search_text", fake_search)

    result = index_tools.workspace_symbols(settings, "search_me", limit=10)

    assert result["engine"] == "heuristic"
    assert result["symbols"][0]["name"] == "search_me"
    assert result["symbols"][0]["kind"] == "function"


def test_resolve_index_scope_falls_back_to_repo_root_for_non_git_path(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    service = tmp_path / "service"
    service.mkdir()
    original = git_tools._resolve_repo_toplevel
    monkeypatch.setattr(git_tools, "_resolve_repo_toplevel", lambda repo, settings: (_ for _ in ()).throw(git_tools.GitToolError("not git")))

    toplevel, rel = index_tools._resolve_index_scope(settings, str(service))

    assert toplevel == service
    assert rel == "service"

    monkeypatch.setattr(git_tools, "_resolve_repo_toplevel", original)


def test_cache_path_and_load_cache_miss_happily_loads_stale_json(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    cache_path = index_tools._cache_path(settings, "svc")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text('{"generated_at": 1, "symbols": [{"name": "old"}]}', encoding="utf-8")
    assert index_tools._load_cache(cache_path, ttl_seconds=0) is None

    cache_path.write_text('{"generated_at": 999999999999, "symbols": [{"name": "new"}]}', encoding="utf-8")
    data = index_tools._load_cache(cache_path, ttl_seconds=3600)
    assert data == [{"name": "new"}]

    cache_path.write_text("{not json}", encoding="utf-8")
    assert index_tools._load_cache(cache_path, ttl_seconds=3600) is None
