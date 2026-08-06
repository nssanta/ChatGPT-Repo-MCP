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

### Recommended full-agent settings

Use:

- **Name:** Repo Agent (or any name you like)
- **Description:** Coding agent access to your workspace — files, git, tests/builds, git workflow, GitHub PR/CI, diagnostics
- **Connection:** `Tunnel`
- **Tunnel:** select the tunnel created in OpenAI Platform, or paste its `tunnel_id`
- **Authentication:** `No Authentication` when this MCP server is loopback-only behind the tunnel

Open <https://chatgpt.com/plugins>, press the **+** button shown beside search, and fill in the **New app** dialog. Select **Tunnel**, not **Server URL**, for a private local machine. The `CONTROL_PLANE_API_KEY` used by `tunnel-client` is transport authentication and must not be pasted into this dialog or used as `MCP_BEARER_TOKEN`.

Recommended local server settings behind Secure MCP Tunnel:

```text
HOST=127.0.0.1
MCP_AUTH_MODE=none
ACCESS_MODE=full
```

`ACCESS_MODE=full` removes this server's internal dry-run and confirmation defaults. It does not control ChatGPT's own four-level permission selector.

This no-auth recommendation applies only when the MCP listener remains on loopback/private networking and the OpenAI tunnel is the only route. For a public URL, use a real OAuth-capable gateway or another authentication method the ChatGPT app dialog supports. The server's static bearer mode is useful for MCP clients that can send an `Authorization` header directly; it is not OAuth.

## Option B: public HTTPS endpoint

Choose **Server URL**, enter the public `/mcp` HTTPS endpoint, and use OAuth or another authentication method supported by the ChatGPT app dialog. Do not expose a full-access agent anonymously.

## Steps

1. Open <https://chatgpt.com/plugins> (or ChatGPT **Settings → Apps**).
2. Enable **Developer mode** if ChatGPT asks for it.
3. Press the **+** button beside the plugin search field.
4. In **New app**, enter a name and description, switch **Connection** to **Tunnel**, and select your tunnel.
5. Choose the app authentication type (`No Authentication` for the private loopback setup above).
6. Accept the custom-MCP risk warning, click **Scan Tools**, and wait for discovery.
7. Create the app.
8. Open its permissions and select **Allow all actions** if you want ChatGPT not to ask before read/write/network tool calls. This is the fourth/highest mode shown in the web UI; keep a stricter level if the endpoint is shared.

Keep the MCP annotations truthful: write, destructive, and network tools remain labelled as such even in full mode. The web permission setting decides whether ChatGPT asks; `ACCESS_MODE=full` decides whether the server itself previews/asks internally.

Full tunnel setup, binary installation, API-key handling, `doctor`, and systemd are documented in [`EXPOSE_LOCAL_PC.md`](EXPOSE_LOCAL_PC.md).

After deploying a build that changes the tool catalog or schemas, verify the
new count directly with `scripts/check_tools.py`, reconnect or refresh the app
connection, and start a **new chat**. ChatGPT may retain the MCP tool catalog in
an existing conversation; a browser-page refresh alone is not proof that the
new schema was loaded.

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
