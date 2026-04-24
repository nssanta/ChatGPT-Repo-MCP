# Connect this MCP server to ChatGPT

## Prerequisites

You need:

- ChatGPT developer mode enabled
- a public HTTPS URL to your MCP endpoint
- the server already running

Official docs say developer mode is enabled via **Settings → Apps → Advanced settings → Developer mode** and that app creation supports **SSE** / **streaming HTTP** with **OAuth**, **No Authentication**, or **Mixed Authentication**.

## Recommended full-agent settings

Use:

- **Name:** Repo Reader
- **Description:** Read-only repository and git analysis for one project
- **URL:** `https://YOUR_DOMAIN/mcp`
- **Authentication:** `Bearer token`

For full-agent tools, use Bearer auth. The server reads:

```text
MCP_AUTH_MODE=bearer
MCP_BEARER_TOKEN=<secret>
```

No-auth is only acceptable for temporary read-only experiments. OAuth/HMAC can be added later, but Bearer is the pragmatic default for a private single-owner VPS connector.

## Steps

1. Open ChatGPT.
2. Go to **Settings → Apps**.
3. Enable **Developer mode**.
4. Click **Create app**.
5. Fill in:
   - Name
   - Description
   - MCP URL
   - Authentication type
6. Confirm the warning.
7. Create the app.

## First prompts to test

Use prompts like:

- `Изучи архитектуру репозитория`
- `Покажи все модули, связанные с auth`
- `Найди где используется JWT`
- `Покажи последние изменения по backend`
- `Проверь TODO и FIXME по проекту`
- `Объясни разницу между ветками и текущим diff`

## If connection fails

Check:

- the server is reachable from the public internet
- HTTPS is valid
- reverse proxy forwards `/mcp`
- systemd service is healthy
- your domain resolves correctly
