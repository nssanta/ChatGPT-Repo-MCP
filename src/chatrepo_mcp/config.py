from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.getenv(name, default).split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str
    host: str
    port: int
    transport: str
    project_root: Path
    max_file_bytes: int
    max_response_chars: int
    max_read_files: int
    max_search_results: int
    max_tree_entries: int
    max_diff_bytes: int
    max_log_commits: int
    subprocess_timeout: int
    blocked_globs: tuple[str, ...]
    allow_hidden_default: bool
    allowed_hosts: tuple[str, ...]
    enable_dns_rebinding_protection: bool
    canonical_namespace: str
    ephemeral_handles_supported: bool
    writable_globs: tuple[str, ...]
    max_write_file_bytes: int
    dangerously_allow_all_writes: bool
    require_expected_hash_for_writes: bool
    max_batch_operations: int
    max_combined_diff_chars: int
    allow_move_delete_operations: bool

    @staticmethod
    def from_env() -> "Settings":
        project_root = Path(os.getenv("PROJECT_ROOT", "")).expanduser().resolve()
        if not str(project_root):
            raise RuntimeError("PROJECT_ROOT is required")
        return Settings(
            app_name=os.getenv("APP_NAME", "chatrepo-mcp"),
            host=os.getenv("HOST", "127.0.0.1"),
            port=_env_int("PORT", 8000),
            transport=os.getenv("TRANSPORT", "streamable-http"),
            project_root=project_root,
            max_file_bytes=_env_int("MAX_FILE_BYTES", 5_000_000),
            max_response_chars=_env_int("MAX_RESPONSE_CHARS", 1_000_000),
            max_read_files=_env_int("MAX_READ_FILES", 25),
            max_search_results=_env_int("MAX_SEARCH_RESULTS", 500),
            max_tree_entries=_env_int("MAX_TREE_ENTRIES", 5_000),
            max_diff_bytes=_env_int("MAX_DIFF_BYTES", 1_000_000),
            max_log_commits=_env_int("MAX_LOG_COMMITS", 100),
            subprocess_timeout=_env_int("SUBPROCESS_TIMEOUT", 15),
            blocked_globs=_env_csv(
                "BLOCKED_GLOBS",
                ".env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**,**/.venv/**,**/node_modules/**,"
                "**/*.db,**/*.sqlite,**/*.sqlite3,**/*.bin,**/*.png,**/*.jpg,**/*.jpeg,"
                "**/*.webp,**/*.pdf,**/*.zip,**/*.tar,**/*.gz",
            ),
            allow_hidden_default=_env_bool("ALLOW_HIDDEN_DEFAULT", True),
            allowed_hosts=_env_csv("ALLOWED_HOSTS", "127.0.0.1,localhost"),
            enable_dns_rebinding_protection=_env_bool("ENABLE_DNS_REBINDING_PROTECTION", True),
            canonical_namespace=os.getenv("CANONICAL_NAMESPACE", "/Eva_Ai"),
            ephemeral_handles_supported=_env_bool("EPHEMERAL_HANDLES_SUPPORTED", False),
            writable_globs=_env_csv(
                "WRITABLE_GLOBS",
                "**/*",
            ),
            max_write_file_bytes=_env_int("MAX_WRITE_FILE_BYTES", 1_000_000),
            dangerously_allow_all_writes=_env_bool("DANGEROUSLY_ALLOW_ALL_WRITES", True),
            require_expected_hash_for_writes=_env_bool("REQUIRE_EXPECTED_HASH_FOR_WRITES", True),
            max_batch_operations=_env_int("MAX_BATCH_OPERATIONS", 50),
            max_combined_diff_chars=_env_int("MAX_COMBINED_DIFF_CHARS", 300_000),
            allow_move_delete_operations=_env_bool("ALLOW_MOVE_DELETE_OPERATIONS", True),
        )
