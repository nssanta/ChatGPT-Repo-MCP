from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.fs_tools import find_files, list_dir, read_text_file, search_text, symbol_search
from chatrepo_mcp.security import SecurityError, is_blocked_relative, resolve_repo_path


def make_settings(tmp_path: Path, allow_hidden_default: bool = False) -> Settings:
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
        blocked_globs=(".env", ".env.*", "*.pem", "*.key", "**/.git/**", "**/.venv/**", "**/node_modules/**"),
        allow_hidden_default=allow_hidden_default,
        allowed_hosts=("127.0.0.1", "localhost"),
        enable_dns_rebinding_protection=True,
        canonical_namespace="/Eva_Ai",
        ephemeral_handles_supported=False,
        writable_globs=(
            ".claude/**",
            "missions/**",
            "docs/**",
            "reports/**",
        ),
        max_write_file_bytes=1000,
        dangerously_allow_all_writes=False,
        require_expected_hash_for_writes=True,
        max_batch_operations=50,
        max_combined_diff_chars=300000,
        allow_move_delete_operations=True,
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


def test_read_text_file_allows_hidden_when_configured(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    p = tmp_path / ".claude" / "MEMORY.md"
    p.parent.mkdir(parents=True)
    p.write_text("memory\n", encoding="utf-8")

    result = read_text_file(".claude/MEMORY.md", settings)

    assert result["path"] == ".claude/MEMORY.md"
    assert result["line_count"] == 1
    assert result["sha256"]


def test_read_text_file_still_blocks_secret_globs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    p = tmp_path / ".env"
    p.write_text("SECRET=value\n", encoding="utf-8")

    try:
        read_text_file(".env", settings)
        assert False, "expected SecurityError"
    except SecurityError:
        assert True


def test_blocked_globs_match_root_nested_and_directory_entries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)

    assert is_blocked_relative(".env", settings)
    assert is_blocked_relative("tests/telegram/.env", settings)
    assert is_blocked_relative("secrets/private.key", settings)
    assert is_blocked_relative(".git", settings)
    assert is_blocked_relative(".git/config", settings)
    assert is_blocked_relative("node_modules/pkg/index.js", settings)


def test_list_dir_hides_blocked_entries(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "MEMORY.md").write_text("memory\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    result = list_dir(".", settings, include_hidden=True)
    names = {entry["name"] for entry in result["entries"]}

    assert ".env" not in names
    assert ".git" not in names
    assert "node_modules" not in names
    assert ".claude" in names
    assert "src" in names


def test_find_and_search_skip_blocked_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    (tmp_path / ".env").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("const needle = true\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("needle\n", encoding="utf-8")

    found = find_files("*.py", settings)
    searched = search_text("needle", settings)

    assert found["matches"] == ["src/main.py"]
    assert [item["path"] for item in searched["results"]] == ["src/main.py"]


def test_symbol_search_falls_back_without_scanning_blocked_paths(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("status_type: 'tts_synthesizing'\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.ts").write_text("const tts_synthesizing = true\n", encoding="utf-8")

    result = symbol_search("tts_synthesizing", settings, limit=5)

    assert result["count"] == 1
    assert result["results"][0]["path"] == "src/main.ts"


def test_symlink_to_blocked_path_is_hidden(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_hidden_default=True)
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / ".env").symlink_to(tmp_path / ".env")

    result = list_dir("tests", settings, include_hidden=True)

    assert result["entries"] == []


def test_settings_defaults_to_canonical_namespace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("CANONICAL_NAMESPACE", raising=False)
    monkeypatch.delenv("EPHEMERAL_HANDLES_SUPPORTED", raising=False)

    settings = Settings.from_env()

    assert settings.canonical_namespace == "/Eva_Ai"
    assert settings.ephemeral_handles_supported is False
