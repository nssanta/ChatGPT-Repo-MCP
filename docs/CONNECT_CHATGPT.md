# Connect this MCP server to ChatGPT

## Prerequisites

You need ChatGPT developer-mode access and a running MCP server. You can connect
it either through a public HTTPS URL or through an OpenAI Secure MCP Tunnel.

Official docs say developer mode is enabled via **Settings → Apps → Advanced settings → Developer mode** and that app creation supports **SSE** / **streaming HTTP** with **OAuth**, **No Authentication**, or **Mixed Authentication**.

## Option A: OpenAI Secure MCP Tunnel (recommended for a local PC)

If the MCP server runs on a private PC, do not expose it publicly. In the app
creation form choose **Connection -> Tunnel**, select the tunnel or paste its
`tunnel_id`, and keep `tunnel-client` running locally. Full setup, systemd
autostart, and troubleshooting: [OpenAI Secure MCP Tunnel runbook](OPENAI_SECURE_TUNNEL_RUNBOOK.md).

## Option B: public HTTPS endpoint

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

1. Open [ChatGPT Plugins / Apps](https://chatgpt.com/plugins).
2. Enable developer mode if your workspace exposes the toggle.
3. Click the **+** button to create an app.
4. Fill in:
   - Name
   - Description
   - MCP URL, or select **Tunnel** under Connection
   - Authentication type
5. Confirm the warning.
6. Create the app.

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
