# chatrepo-mcp

Read-only MCP server for ChatGPT developer mode that gives the model deep visibility into **one Git repository** on your VPS.

## What this project is

This server is designed as a **safe v1** for codebase analysis in chat:

- repository overview
- file tree browsing
- reading files
- searching code and text
- recent file changes
- TODO / FIXME scanning
- dependency manifest extraction
- Git status / diff / log / show / blame / branches / grep

It is intentionally **read-only**. There are **no write tools**, no shell execution tool, no commit/push, and no patch application in this version.

## Why this shape

The current OpenAI Apps / ChatGPT setup requires an MCP server to expose capabilities to ChatGPT, while UI is optional. ChatGPT developer mode supports **SSE** and **streaming HTTP** transports, and supports **OAuth**, **No Authentication**, and **Mixed Authentication** when creating an app. The official Apps SDK docs also recommend using the official SDKs and marking read-only tools with `readOnlyHint=true`. The official Python SDK supports FastMCP and Streamable HTTP.

## Tool surface

### Filesystem / repo analysis

- `repo_info`
- `list_dir`
- `tree`
- `read_text_file`
- `read_multiple_files`
- `file_metadata`
- `find_files`
- `search_text`
- `symbol_search`
- `recent_changes`
- `todo_scan`
- `dependency_map`

### Git

- `git_status`
- `git_diff`
- `git_log`
- `git_show`
- `git_branches`
- `git_blame`
- `git_grep`

## Security model

This project is opinionated:

- one repository root only
- path validation on every file operation
- blocked secret patterns by default
- no arbitrary command execution tool
- `.git` is blocked from file reads, while Git commands still run inside the repo
- file size and output limits prevent accidental giant payloads

Default blocked patterns include:

- `.env`
- `.env.*`
- `*.pem`
- `*.key`
- `*.p12`
- `*.pfx`
- `**/.git/**`
- `**/.venv/**`
- `**/node_modules/**`

You can change the list in `.env`.

## Recommended v1 deployment choice

For the first real deployment, use:

- **Python**
- **FastMCP**
- **streamable-http**
- **Ubuntu 24**
- **one repo**
- **read-only**
- **ChatGPT app auth = No Authentication**
- secrets blocked at the server level

That keeps the initial version small and practical, while leaving room for OAuth and write tools later.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

cp .env.example .env
# edit PROJECT_ROOT and optional limits

python -m chatrepo_mcp
```

The MCP endpoint will be served at:

```text
http://127.0.0.1:8000/mcp
```

## Ubuntu VPS install path suggestion

```text
/opt/myproject        # target repository to inspect
/opt/chatrepo-mcp     # this MCP project
```

## ChatGPT app settings

In ChatGPT developer mode:

- **Name:** Repo Reader
- **Description:** Read-only repository and git analysis for one project
- **URL:** `https://YOUR_DOMAIN/mcp`
- **Authentication:** `Без авторизации` for v1

See `docs/CONNECT_CHATGPT.md`.

## Project layout

```text
chatrepo-mcp/
├── .env.example
├── pyproject.toml
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEPLOY_VPS.md
│   └── CONNECT_CHATGPT.md
├── scripts/
│   ├── install_ubuntu.sh
│   └── smoke_test.sh
├── deploy/
│   ├── systemd/chatrepo-mcp.service
│   ├── nginx/chatrepo-mcp.conf.example
│   └── caddy/Caddyfile.example
└── src/chatrepo_mcp/
    ├── __init__.py
    ├── __main__.py
    ├── config.py
    ├── fs_tools.py
    ├── git_tools.py
    ├── server.py
    └── security.py
```

## Notes

This repository is a **strong starter**. You will still need to fill in real deployment values:

- domain
- actual repository path
- public HTTPS
- service user and permissions

If later you want parity closer to Codex / Claude Code, the next phase is:

- optional GitHub layer for PRs/issues
- optional write tools with explicit approval
- optional UI for file tree / diff viewer
