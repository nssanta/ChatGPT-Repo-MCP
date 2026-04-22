from pathlib import Path

from chatrepo_mcp.config import Settings
from chatrepo_mcp.edit_tools import (
    StaleWriteError,
    WritePolicyError,
    delete_text_in_file,
    insert_text_in_file,
    replace_text_in_file,
    sha256_text,
    write_text_file,
)
from chatrepo_mcp.security import SecurityError


def make_settings(tmp_path: Path, *, writable_globs: tuple[str, ...] | None = None, dangerous: bool = False) -> Settings:
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
        allow_hidden_default=True,
        allowed_hosts=("127.0.0.1", "localhost"),
        enable_dns_rebinding_protection=True,
        canonical_namespace="/Eva_Ai",
        ephemeral_handles_supported=False,
        writable_globs=writable_globs
        or (
            ".claude/**",
            "missions/**",
            "docs/**",
            "reports/**",
        ),
        max_write_file_bytes=1000,
        dangerously_allow_all_writes=dangerous,
        require_expected_hash_for_writes=True,
    )


def write_allowed_file(tmp_path: Path, rel: str, text: str) -> tuple[Path, str]:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, sha256_text(text)


def test_replace_success_and_not_found(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/CURRENT.md", "hello world\n")

    result = replace_text_in_file(
        "missions/CURRENT.md",
        "world",
        "team",
        settings,
        expected_sha256=old_hash,
        dry_run=False,
    )

    assert result["changed"] is True
    assert "team" in path.read_text(encoding="utf-8")
    try:
        replace_text_in_file("missions/CURRENT.md", "missing", "x", settings, expected_sha256=result["new_sha256"])
        assert False, "expected not found"
    except ValueError:
        assert True


def test_insert_before_and_after(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/BACKLOG.md", "A\nB\n")

    result = insert_text_in_file("missions/BACKLOG.md", "B", "before", "before-", settings, expected_sha256=old_hash, dry_run=False)
    result = insert_text_in_file(
        "missions/BACKLOG.md",
        "before-B",
        "after",
        "-after",
        settings,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )

    assert path.read_text(encoding="utf-8") == "A\nbefore-B-after\n"


def test_delete_by_text_and_line_range(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    path, old_hash = write_allowed_file(tmp_path, "missions/CURRENT_REPORT.md", "one\ntwo\nthree\n")

    result = delete_text_in_file("missions/CURRENT_REPORT.md", settings, find="two\n", expected_sha256=old_hash, dry_run=False)
    result = delete_text_in_file(
        "missions/CURRENT_REPORT.md",
        settings,
        start_line=2,
        end_line=2,
        expected_sha256=result["new_sha256"],
        dry_run=False,
    )

    assert path.read_text(encoding="utf-8") == "one\n"


def test_write_create_if_missing_and_dry_run_no_mutation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = tmp_path / "missions" / "packets" / "new.md"

    preview = write_text_file("missions/packets/new.md", "draft\n", settings, create_if_missing=True, dry_run=True)
    assert preview["changed"] is True
    assert not target.exists()

    result = write_text_file("missions/packets/new.md", "draft\n", settings, create_if_missing=True, dry_run=False)
    assert target.read_text(encoding="utf-8") == "draft\n"
    assert result["new_sha256"] == sha256_text("draft\n")


def test_stale_hash_and_missing_hash_reject(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_allowed_file(tmp_path, "missions/USER_BUGS.md", "bug\n")

    try:
        replace_text_in_file("missions/USER_BUGS.md", "bug", "fix", settings, expected_sha256="bad")
        assert False, "expected stale hash"
    except StaleWriteError:
        assert True

    try:
        replace_text_in_file("missions/USER_BUGS.md", "bug", "fix", settings)
        assert False, "expected missing hash"
    except StaleWriteError:
        assert True


def test_blocked_writable_and_traversal_reject(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('x')\n", encoding="utf-8")

    try:
        write_text_file(".env", "x", settings, expected_sha256=sha256_text("SECRET=value\n"))
        assert False, "expected blocked path"
    except WritePolicyError:
        assert True

    try:
        write_text_file("src/main.py", "x", settings, expected_sha256=sha256_text("print('x')\n"))
        assert False, "expected writable policy reject"
    except WritePolicyError:
        assert True

    try:
        write_text_file("../outside.md", "x", settings)
        assert False, "expected traversal reject"
    except SecurityError:
        assert True


def test_star_requires_dangerous_flag(tmp_path: Path) -> None:
    strict = make_settings(tmp_path, writable_globs=("*",), dangerous=False)
    dangerous = make_settings(tmp_path, writable_globs=("*",), dangerous=True)
    path, old_hash = write_allowed_file(tmp_path, "src/main.py", "old\n")

    try:
        write_text_file("src/main.py", "new\n", strict, expected_sha256=old_hash)
        assert False, "expected dangerous flag reject"
    except WritePolicyError:
        assert True

    result = write_text_file("src/main.py", "new\n", dangerous, expected_sha256=old_hash, dry_run=False)
    assert result["changed"] is True
    assert path.read_text(encoding="utf-8") == "new\n"
