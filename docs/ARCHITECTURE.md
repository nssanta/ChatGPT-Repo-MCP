# Architecture

## Goal

Expose a **single repository** on a VPS to ChatGPT through a remote MCP server, with strong path, secret, edit, and command guardrails.

## High-level design

```text
ChatGPT (Developer Mode)
        │
        │ HTTPS /mcp
        ▼
Reverse Proxy (Caddy or Nginx)
        │
        ▼
Python FastMCP server
        │
        ├── Filesystem tools (validated reads)
        ├── Git tools (git subprocess, read-only)
        ├── Safe text edit tools (diff + hash guarded)
        ├── Safe command runner (allowlisted, no shell)
        └── Security / limits / blocked paths
        │
        ▼
One local repository on disk
```

## Core design decisions

### 1) One repository only

`PROJECT_ROOT` points to exactly one repo.  
Every path-based tool resolves relative to that root and rejects traversal outside it.

### 2) Safe edit layer

Write tools are limited to UTF-8 text files inside `PROJECT_ROOT`.
Every write path is checked against:

- repo-root traversal protection
- `BLOCKED_GLOBS`
- `WRITABLE_GLOBS`
- binary/non-UTF-8 detection
- optional `expected_sha256` stale-state guard

Write tools return unified diffs and default to `dry_run=true`.

### 3) Git through subprocess

Git information is obtained through `git` CLI commands executed with:

- working directory = `PROJECT_ROOT`
- explicit timeout
- explicit argument list
- capped output

### 4) Safe command runner

`run_command` is not a bash shell. It parses commands with `shlex`, rejects shell operators, runs with `shell=False`, and only allows known validation commands such as `git diff --check`, selected `npm run test ...`, `npx vitest run ...`, and selected `npx tsx tests/telegram/scenarios/*.test.ts` commands.

### 5) Text/code search through ripgrep

Search-heavy tools rely on `rg`, because it is fast and scales well for large trees.

### 6) Secret-aware file access

Even in read-only mode, not every file should be exposed.  
This server blocks sensitive patterns by default, especially `.env` and private key material.

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

- allowlisted validation commands with exit code, stdout, stderr, duration, and timeout reporting

## Output philosophy

Tool outputs are structured and concise enough for the model to reason over them:

- metadata as JSON-style dictionaries
- textual content capped by bytes/characters
- search results as lists of `{path, line, text}`
- diffs capped to prevent context overload

## Future v2 ideas

- GitHub MCP layer for PRs / issues
- write tools with approval
- safe `run_tests`
- optional UI resource
- language-aware symbol indexing
