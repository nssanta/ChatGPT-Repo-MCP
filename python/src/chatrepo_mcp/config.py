from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .resource_profile import resolve_resource_limits

# `python -m chatrepo_mcp` is the documented local entrypoint.  Load the
# adjacent .env before Settings.from_env() is evaluated by server.py.
load_dotenv()


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
    max_patch_bytes: int
    max_command_output_chars: int
    command_timeout_ms: int
    command_audit_log_path: Path
    mcp_auth_mode: str
    mcp_bearer_token: str | None
    command_policy_mode: str
    command_jobs_dir: Path
    workspace_roots: tuple[str, ...]
    filesystem_unrestricted: bool
    workspace_scan_depth: int
    denied_words: tuple[str, ...]
    destructive_words: tuple[str, ...]
    command_shell_prelude: str
    git_network_timeout: int
    protected_branches: tuple[str, ...]
    allow_force_push: bool
    gh_timeout: int
    github_tools_enabled: bool
    secret_globs: tuple[str, ...]
    binary_globs: tuple[str, ...]
    access_mode: str = "safe"
    allow_secret_access: bool = False
    allow_hard_reset: bool = False
    mcp_extra_path: tuple[str, ...] = ()
    kill_grace_ms: int = 5_000
    enable_pty: bool = True
    max_terminal_sessions: int = 4
    artifact_total_bytes: int = 10_737_418_240
    artifact_max_bytes: int = 5_368_709_120
    artifact_disk_reserve_bytes: int = 2_147_483_648
    artifact_ttl_seconds: int = 604_800
    resource_profile: str = "auto"
    resource_profile_applied: str = "small"
    resource_detected_memory_bytes: int | None = None
    resource_buffer_bytes: int = 16 * 1024**2
    max_heavy_operations: int = 2
    persist_full_output: bool = True
    # Ограничиваем обычный inline-ответ, но не объём сохраняемого артефакта.
    default_inline_output_bytes: int = 65_536

    @property
    def full_access(self) -> bool:
        return self.access_mode == "full"

    @property
    def default_dry_run(self) -> bool:
        return not self.full_access

    def effective_dry_run(self, requested: bool | None) -> bool:
        return self.default_dry_run if requested is None else requested

    def confirmation_granted(self, confirmed: bool | None) -> bool:
        return self.full_access or confirmed is True

    @staticmethod
    def from_env() -> Settings:
        raw_project_root = os.getenv("PROJECT_ROOT", "").strip()
        if not raw_project_root:
            raise RuntimeError("PROJECT_ROOT is required")
        project_root = Path(raw_project_root).expanduser().resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise RuntimeError(f"PROJECT_ROOT must be an existing directory: {project_root}")

        access_mode = os.getenv("ACCESS_MODE", "safe").strip().lower()
        if access_mode not in {"safe", "full"}:
            raise RuntimeError("ACCESS_MODE must be one of: safe, full")
        full_access = access_mode == "full"
        allow_secret_access = full_access and _env_bool("ALLOW_SECRET_ACCESS", False)
        secret_globs = _env_csv(
            "SECRET_GLOBS",
            ".env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**",
        )
        if not secret_globs and not allow_secret_access:
            raise RuntimeError(
                "SECRET_GLOBS must not be empty unless ACCESS_MODE=full and ALLOW_SECRET_ACCESS=true"
            )

        auth_mode = os.getenv("MCP_AUTH_MODE", "none").strip().lower()
        if auth_mode not in {"none", "bearer"}:
            raise RuntimeError("MCP_AUTH_MODE must be one of: none, bearer")
        bearer_token = os.getenv("MCP_BEARER_TOKEN")
        if auth_mode == "bearer" and not (bearer_token and bearer_token.strip()):
            raise RuntimeError("MCP_BEARER_TOKEN is required when MCP_AUTH_MODE=bearer")

        command_policy_mode = (
            "unrestricted"
            if full_access
            else os.getenv("COMMAND_POLICY_MODE", "allowlist").strip().lower()
        )
        if command_policy_mode not in {"allowlist", "guarded", "unrestricted", "full_repo"}:
            raise RuntimeError(
                "COMMAND_POLICY_MODE must be one of: allowlist, guarded, unrestricted, full_repo"
            )
        resource_profile = os.getenv("RESOURCE_PROFILE", "auto").strip().lower()
        custom_buffer_raw = os.getenv("RESOURCE_BUFFER_BYTES")
        custom_heavy_raw = os.getenv("MAX_HEAVY_OPERATIONS")
        try:
            resource_limits = resolve_resource_limits(
                resource_profile,
                custom_buffer_bytes=int(custom_buffer_raw) if custom_buffer_raw else None,
                custom_heavy_operations=int(custom_heavy_raw) if custom_heavy_raw else None,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        persist_full_output = _env_bool("PERSIST_FULL_OUTPUT", True)
        if not persist_full_output:
            raise RuntimeError(
                "PERSIST_FULL_OUTPUT=false is unsupported: bounded durable output is mandatory"
            )
        settings = Settings(
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
            allowed_hosts=_env_csv("ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"),
            enable_dns_rebinding_protection=_env_bool("ENABLE_DNS_REBINDING_PROTECTION", True),
            canonical_namespace=os.getenv("CANONICAL_NAMESPACE") or f"/{project_root.name}",
            ephemeral_handles_supported=_env_bool("EPHEMERAL_HANDLES_SUPPORTED", False),
            writable_globs=_env_csv(
                "WRITABLE_GLOBS",
                "**/*",
            ),
            max_write_file_bytes=_env_int("MAX_WRITE_FILE_BYTES", 1_000_000),
            dangerously_allow_all_writes=(
                full_access or _env_bool("DANGEROUSLY_ALLOW_ALL_WRITES", False)
            ),
            require_expected_hash_for_writes=(
                False
                if full_access
                else _env_bool("REQUIRE_EXPECTED_HASH_FOR_WRITES", True)
            ),
            max_batch_operations=_env_int("MAX_BATCH_OPERATIONS", 50),
            max_combined_diff_chars=_env_int("MAX_COMBINED_DIFF_CHARS", 300_000),
            allow_move_delete_operations=(
                full_access or _env_bool("ALLOW_MOVE_DELETE_OPERATIONS", False)
            ),
            max_patch_bytes=_env_int("MAX_PATCH_BYTES", 500_000),
            max_command_output_chars=_env_int("MAX_COMMAND_OUTPUT_CHARS", 200_000),
            command_timeout_ms=_env_int("COMMAND_TIMEOUT_MS", 300_000),
            command_audit_log_path=Path(
                os.getenv("COMMAND_AUDIT_LOG_PATH", "~/.local/state/chatrepo-mcp/commands.log")
            ).expanduser(),
            mcp_auth_mode=auth_mode,
            mcp_bearer_token=bearer_token,
            command_policy_mode=command_policy_mode,
            command_jobs_dir=Path(os.getenv("COMMAND_JOBS_DIR", "/tmp/chatrepo-mcp-jobs")).expanduser(),
            workspace_roots=_env_csv("WORKSPACE_ROOTS", ""),
            filesystem_unrestricted=(
                full_access or _env_bool("FILESYSTEM_UNRESTRICTED", False)
            ),
            workspace_scan_depth=_env_int("WORKSPACE_SCAN_DEPTH", 2),
            denied_words=_env_csv("DENIED_WORDS", "sudo,su"),
            destructive_words=_env_csv(
                "DESTRUCTIVE_WORDS",
                "rm -rf,rmdir,git push --force,git reset --hard,git clean,"
                "docker system prune,chmod -R,chown -R,mkfs,dd",
            ),
            command_shell_prelude=os.getenv("COMMAND_SHELL_PRELUDE", ""),
            mcp_extra_path=tuple(
                part.strip()
                for part in os.getenv("MCP_EXTRA_PATH", "").split(os.pathsep)
                if part.strip()
            ),
            kill_grace_ms=_env_int("KILL_GRACE_MS", 5_000),
            enable_pty=os.name == "posix" and _env_bool("ENABLE_PTY", True),
            max_terminal_sessions=_env_int("MAX_TERMINAL_SESSIONS", 4),
            artifact_total_bytes=_env_int("ARTIFACT_TOTAL_BYTES", 10_737_418_240),
            artifact_max_bytes=_env_int("ARTIFACT_MAX_BYTES", 5_368_709_120),
            artifact_disk_reserve_bytes=_env_int("ARTIFACT_DISK_RESERVE_BYTES", 2_147_483_648),
            artifact_ttl_seconds=_env_int("ARTIFACT_TTL_SECONDS", 604_800),
            resource_profile=resource_profile,
            resource_profile_applied=resource_limits.profile,
            resource_detected_memory_bytes=resource_limits.detected_memory_bytes,
            resource_buffer_bytes=resource_limits.buffer_bytes,
            max_heavy_operations=resource_limits.heavy_operations,
            persist_full_output=persist_full_output,
            default_inline_output_bytes=_env_int("DEFAULT_INLINE_OUTPUT_BYTES", 65_536),
            git_network_timeout=_env_int("GIT_NETWORK_TIMEOUT", 60),
            protected_branches=_env_csv("PROTECTED_BRANCHES", "main,master"),
            allow_force_push=_env_bool("ALLOW_FORCE_PUSH", False),
            gh_timeout=_env_int("GH_TIMEOUT", 60),
            github_tools_enabled=_env_bool("GITHUB_TOOLS_ENABLED", True),
            secret_globs=secret_globs,
            binary_globs=_env_csv(
                "BINARY_GLOBS",
                "**/.venv/**,**/node_modules/**,**/*.db,**/*.sqlite,**/*.sqlite3,**/*.bin,"
                "**/*.png,**/*.jpg,**/*.jpeg,**/*.webp,**/*.pdf,**/*.zip,**/*.tar,**/*.gz",
            ),
            access_mode=access_mode,
            allow_secret_access=allow_secret_access,
            allow_hard_reset=_env_bool("ALLOW_HARD_RESET", False),
        )
        inline_hard_limit = min(
            settings.max_response_chars,
            settings.max_diff_bytes,
            settings.max_command_output_chars,
        )
        if not 0 < settings.default_inline_output_bytes <= inline_hard_limit:
            raise RuntimeError(
                "DEFAULT_INLINE_OUTPUT_BYTES must be positive and no greater than "
                f"the smallest configured output ceiling ({inline_hard_limit})"
            )
        return settings
