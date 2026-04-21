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
            max_file_bytes=_env_int("MAX_FILE_BYTES", 262_144),
            max_response_chars=_env_int("MAX_RESPONSE_CHARS", 40_000),
            max_read_files=_env_int("MAX_READ_FILES", 8),
            max_search_results=_env_int("MAX_SEARCH_RESULTS", 100),
            max_tree_entries=_env_int("MAX_TREE_ENTRIES", 500),
            max_diff_bytes=_env_int("MAX_DIFF_BYTES", 131_072),
            max_log_commits=_env_int("MAX_LOG_COMMITS", 50),
            subprocess_timeout=_env_int("SUBPROCESS_TIMEOUT", 15),
            blocked_globs=tuple(
                p.strip()
                for p in os.getenv(
                    "BLOCKED_GLOBS",
                    ".env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**,**/.venv/**,**/node_modules/**",
                ).split(",")
                if p.strip()
            ),
            allow_hidden_default=_env_bool("ALLOW_HIDDEN_DEFAULT", False),
        )
