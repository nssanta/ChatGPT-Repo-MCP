from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.security import SecurityError, resolve_repo_path


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_name="test",
        host="127.0.0.1",
        port=8000,
        transport="streamable-http",
        project_root=tmp_path,
        max_file_bytes=1000,
        max_response_chars=1000,
        max_read_files=8,
        max_search_results=50,
        max_tree_entries=100,
        max_diff_bytes=1000,
        max_log_commits=10,
        subprocess_timeout=5,
        blocked_globs=(".env", ".env.*", "**/.git/**"),
        allow_hidden_default=False,
    )


def test_resolve_repo_path_allows_regular_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    p = tmp_path / "src" / "main.py"
    p.parent.mkdir(parents=True)
    p.write_text("print('ok')", encoding="utf-8")
    assert resolve_repo_path("src/main.py", settings) == p.resolve()


def test_resolve_repo_path_rejects_escape(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    try:
        resolve_repo_path("../etc/passwd", settings)
        assert False, "expected SecurityError"
    except SecurityError:
        assert True
