# Architecture

## Runtime managers (v0.3)

Both implementations derive one effective executable PATH from `MCP_EXTRA_PATH`, inherited PATH, the server process's active virtualenv, and existing standard toolchain directories. Doctor, diagnostics, GitHub tools, and every command/PTY child use that same environment. The server never searches a target repository's `.venv` implicitly.

Command jobs and POSIX terminal sessions are UUID-owned resources with shared logs. Each process receives its own process group; cancel, timeout, idle timeout, and orderly shutdown terminate the group with TERM followed by KILL. Preset locks prevent duplicate tests. PTY defaults on but remains gated by full access; `ENABLE_PTY=false` explicitly removes its tools.

The canonical contract describes the full catalog. Runtime registration can expose the smaller non-PTY subset; readiness and doctor report the active capabilities.

## Goal

Expose a workspace, polyrepo, or trusted machine to ChatGPT through remote MCP, with an explicit safe/full operating mode.

## High-level design

```text
ChatGPT (Developer Mode)
        │
        │ HTTPS /mcp
        ▼
Reverse Proxy (Caddy or Nginx)
        │
        ▼
Shared MCP catalog (96 tools; 90 default)
        │
        ├── Python FastMCP package
        └── Go MCP binary
        │
        ├── Filesystem tools (validated reads)
        ├── Git tools + git-workflow (branch/stash/fetch/pull/push/merge/worktree)
        ├── GitHub tools (PR/CI via `gh`) + diagnostics/symbol index
        ├── Safe text edit tools (diff + hash guarded)
        ├── Policy-gated command runner (allowlist / guarded / unrestricted)
        └── Security / limits / blocked paths
        │
        ▼
One workspace folder on disk (single repo or polyrepo)
```

Both servers expose the same names, input schemas, additive output schemas,
annotations, `.env` configuration, and structured behavior.
`contracts/tool-schemas/tools.json` is the checked-in contract-v3 public API;
language-specific acceptance tests reject schema or runtime-registration drift.
Operational details for streaming, receipts, artifact paging, quotas, and
resource profiles live in [BOUNDED_OUTPUT.md](BOUNDED_OUTPUT.md).

## Core design decisions

### 1) One workspace root, optionally many repos

`PROJECT_ROOT` points to the workspace folder — one git repo, or a parent folder containing several independent repos (polyrepo; see `list_repos`/`workspace.py`).
Every path-based tool resolves relative to that root (or an extra `WORKSPACE_ROOTS` folder) and rejects traversal outside it, unless `FILESYSTEM_UNRESTRICTED=true` removes the perimeter on purpose.

### 2) Safe edit layer

Structured write tools operate on UTF-8 text files inside the configured roots (or anywhere in full mode).
Every write path is checked against:

- repo-root traversal protection
- `BLOCKED_GLOBS`
- `WRITABLE_GLOBS`
- binary/non-UTF-8 detection
- optional `expected_sha256` stale-state guard

Write tools return unified diffs. If `dry_run` is omitted, safe mode previews and full mode applies; an explicit value always wins.

### 3) Git through subprocess

Git information is obtained through `git` CLI commands executed with:

- working directory = the selected repository (`PROJECT_ROOT` by default)
- explicit timeout
- explicit argument list
- capped output

### 4) Safe/full command runner

`ACCESS_MODE=safe` uses scoped paths, allowlisted commands by default, preview writes, hashes, and confirmation gates. `ACCESS_MODE=full` forces unrestricted bash/filesystem, applies writes by default, enables move/delete, and treats structural confirmations as granted. Safe mode blocks raw `git push`; full mode intentionally permits it because it is real shell access. Separate structural interlocks remain for secret tools, force push, and hard reset.

Command, job, terminal, Git/GitHub, and exhaustive-search output is redacted before persistence or bounded in-memory retention. Direct command, Git, and GitHub responses use a configurable 64 KiB head/tail preview by default, independently of their hard capture ceilings. Full redacted output is stored as a quota-managed artifact; bounded inline receipts state whether the inline view is complete and provide an opaque `read_artifact` continuation when it is not. Heavy operations write start/finish audit records without raw arguments or secrets.

### 5) Text/code search through ripgrep

Search-heavy tools rely on `rg`, because it is fast and scales well for large trees. Quick search streams matches and terminates at its global result cap. Exhaustive search reuses the durable background-job lifecycle instead of buffering the repository-wide result in the server process. `recent_changes` walks file metadata and retains only a bounded top-N heap.

### 6) Secret-aware file access

Even in read-only mode, not every file should be exposed.  
This server blocks sensitive patterns by default. Structured access can be enabled only with `ACCESS_MODE=full` plus `ALLOW_SECRET_ACCESS=true`; raw full-mode shell follows OS permissions.

### 7) Two implementations, one release

The Python package and Go binary are equal public implementations. Python
derives exact success/error output unions from its result models; Go embeds and
publishes the generated shared contract so release binaries need no runtime
schema files. A single `VERSION`, release tag workflow, and documentation set
cover both packages.

## Tool groups

### Repo / files

- repo info
- directory listing
- textual tree
- single file read with line ranges
- multi-file read
- metadata
- filename search
- text search
- symbol search
- recent changes
- todo scan
- dependency manifests

### Git

- status
- diff
- log
- show
- branches
- blame
- grep

### Edits

- full-file write/create
- exact replace/insert/delete
- line-based replace/insert
- markdown heading insert
- append
- unified diff patch
- atomic batch edits

### Commands

- policy-gated validation commands with exit code, stdout, stderr, duration, and timeout reporting
- multi-command validation batches, stack-autodetected test/lint/build presets
- background jobs for long E2E commands with polling and cancellation
- controlled `git_commit` for explicitly listed paths (no push)

### Git workflow

- branch/stash/restore/fetch/pull/merge/revert/reset, plus worktree add/list/remove
- safe mode routes push through `git_push`; full mode also exposes raw shell
- structured force push and hard reset require `ALLOW_FORCE_PUSH` / `ALLOW_HARD_RESET`

### GitHub (via `gh` CLI)

- PR create/list/view/comment/merge, CI check/run inspection and rerun, issue list/view
- graceful `gh_unavailable`/`no_github_remote` errors when `gh` isn't installed/authenticated or there's no GitHub remote

### Diagnostics and symbols

- one-shot `code_diagnostics` (`go vet` / `pyright` or `ruff` / `tsc --noEmit`, autodetected per stack)
- `symbol_definition` / `document_symbols` / `workspace_symbols` via `ctags` when installed, else a regex heuristic

## Output philosophy

Tool outputs are structured and concise enough for the model to reason over them:

- metadata as JSON-style dictionaries
- textual content capped by bytes/characters, with truthful completeness receipts
- search results as lists of `{path, line, text}`
- diffs capped to prevent context overload
- durable redacted artifacts paged through the single `read_artifact` tool

## Future ideas

- a full LSP client (find-references/rename via live `gopls`/`pyright`) beyond the current one-shot diagnostics + ctags index
- optional UI resource for tree/diff views
