# Architecture

## Goal

Expose a **single repository** on a VPS to ChatGPT through a remote MCP server, with a strong read-only boundary.

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
        ├── Filesystem tools (validated, read-only)
        ├── Git tools (git subprocess, read-only)
        └── Security / limits / blocked paths
        │
        ▼
One local repository on disk
```

## Core design decisions

### 1) One repository only

`PROJECT_ROOT` points to exactly one repo.  
Every path-based tool resolves relative to that root and rejects traversal outside it.

### 2) Read-only v1

No tools that mutate the filesystem or Git history:

- no write
- no patch
- no shell exec
- no commit
- no push

### 3) Git through subprocess

Git information is obtained through `git` CLI commands executed with:

- working directory = `PROJECT_ROOT`
- explicit timeout
- explicit argument list
- capped output

### 4) Text/code search through ripgrep

Search-heavy tools rely on `rg`, because it is fast and scales well for large trees.

### 5) Secret-aware file access

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
