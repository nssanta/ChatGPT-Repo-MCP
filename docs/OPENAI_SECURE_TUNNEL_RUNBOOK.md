# Run ChatRepo MCP from a Local PC with OpenAI Secure MCP Tunnel

This runbook connects a local ChatRepo MCP server to ChatGPT without a public
IP address, DNS name, Caddy, port forwarding, or inbound firewall rule.

```text
ChatGPT -> OpenAI-hosted Secure MCP Tunnel -> tunnel-client on the PC -> http://127.0.0.1:2091/mcp
```

`tunnel-client` opens an outbound HTTPS connection to OpenAI, receives queued
MCP requests, forwards them to the local MCP server, and returns the responses
over the same connection. The MCP server stays bound to `localhost`; it is not a
public web endpoint. The stable identifier is the OpenAI `tunnel_id`, so the
entry point does not change when the home IP changes or the PC reboots.

## Scope and prerequisites

This guide is for Linux hosts using `systemd` and supports either packaged
implementation. The examples use the Go binary; the Python command is shown
alongside it. See [Installation](INSTALL.md) for platform prerequisites. You need:

- A clone of this repository and a local Git repository to expose through it.
- A Platform organization with tunnel permissions:
  - `Tunnels Read + Manage` to create or edit a tunnel.
  - `Tunnels Read + Use` to run the client and select a tunnel in ChatGPT.
- Permission to create a custom MCP app in the intended ChatGPT workspace.
- Outbound HTTPS access from the PC to `api.openai.com:443`.

The Platform tunnel role and ChatGPT developer-mode access are separate. Current
OpenAI policy and product UI can change: the published policy says that Pro can
connect MCPs with read/fetch permissions, while full write/modify MCP support is
beta for Business and Enterprise/Edu. Confirm the capabilities of the specific
ChatGPT workspace before exposing write or command tools.

## 1. Create an OpenAI tunnel

1. Open [Platform Tunnels](https://platform.openai.com/settings/organization/tunnels).
2. Select the Platform organization that will own the tunnel.
3. Select **Create tunnel** and give it an operator-friendly name such as
   `home-pc-chatrepo`.
4. Associate the tunnel with the Platform organization and the ChatGPT workspace
   that will use it.
5. Copy the generated identifier, for example `tunnel_0123456789abcdef...`.

`tunnel_id` is not a URL and is not a secret. It is used in the local client
profile and can be pasted into ChatGPT when the tunnel is not listed.

Useful Platform links:

- [Tunnels](https://platform.openai.com/settings/organization/tunnels)
- [Organization roles](https://platform.openai.com/settings/organization/people/roles)
- [Organization groups](https://platform.openai.com/settings/organization/people/groups)

## 2. Create a dedicated runtime API key

1. Open [Platform API keys](https://platform.openai.com/settings/organization/api-keys).
2. Create a new secret key dedicated to this host, for example
   `chatrepo-tunnel-home-pc`.
3. Save the value immediately: the full secret is shown only once.

This key authenticates `tunnel-client` to the OpenAI tunnel control plane. It is
not a ChatGPT password, and it is not an Admin API key. The key owner needs
`Tunnels Read + Use` in the tunnel's Platform organization.

Never paste the key into chat, source code, `.env` tracked by Git, screenshots,
or shell history. Store it outside the repository with file mode `0600`. If it
is exposed, revoke it in Platform and create a replacement.

## 3. Install `tunnel-client`

Download the supported binary from the **Download tunnel-client** button on the
Platform Tunnels page or from the [latest public release](https://github.com/openai/tunnel-client/releases/latest).
For standard 64-bit Linux choose the archive whose name contains `linux-amd64`.
Verify the release checksum before executing it.

After downloading an archive, install it under the current user's home directory:

```bash
mkdir -p "$HOME/.local/bin"
unzip -p "$HOME/Downloads/tunnel-client-<VERSION>-linux-amd64.zip" \
  > "$HOME/.local/bin/tunnel-client"
chmod 755 "$HOME/.local/bin/tunnel-client"
"$HOME/.local/bin/tunnel-client" --version
```

Replace `<VERSION>` with the exact downloaded archive name. All commands below
use the full binary path, so adding `~/.local/bin` to `PATH` is optional.

## 4. Install and configure ChatRepo MCP

`CHATREPO_DIR` is this project. `PROJECT_ROOT` is the only repository that the
MCP server may access. They should normally be different directories.

```bash
export CHATREPO_DIR="$HOME/src/chatrepo-mcp"
export PROJECT_ROOT="$HOME/src/my-project"

cd "$CHATREPO_DIR"
make build

mkdir -p "$HOME/.config/chatrepo-mcp"
```

For Python instead of Go:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e ./python
```

Create `$HOME/.config/chatrepo-mcp/chatrepo-mcp.env` and substitute your own
`PROJECT_ROOT`:

```env
HOST=127.0.0.1
PORT=2091
TRANSPORT=streamable-http
PROJECT_ROOT=/home/USER/src/my-project
ALLOWED_HOSTS=127.0.0.1:*,localhost:*
MCP_AUTH_MODE=none
```

```bash
chmod 600 "$HOME/.config/chatrepo-mcp/chatrepo-mcp.env"
```

`MCP_AUTH_MODE=none` is appropriate only for this private-tunnel arrangement:
the MCP server listens on loopback and no router or public proxy exposes its
port. Do not change `HOST` to `0.0.0.0`, open port `2091`, or reuse this setting
behind a public reverse proxy. A public endpoint needs independent MCP auth.

## 5. Create the tunnel-client profile

Create `$HOME/.config/tunnel-client/chatrepo-local.yaml`:

```yaml
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "tunnel_REPLACE_WITH_YOUR_ID"
  api_key: "env:CONTROL_PLANE_API_KEY"
health:
  listen_addr: "127.0.0.1:8081"
admin_ui:
  open_browser: false
log:
  level: info
  format: json
mcp:
  server_urls:
    - channel: main
      url: "http://127.0.0.1:2091/mcp"
```

Create the parent directory and protect it:

```bash
mkdir -p "$HOME/.config/tunnel-client"
chmod 700 "$HOME/.config/tunnel-client"
```

Replace the example `tunnel_id`. Then create the secret file
`$HOME/.config/tunnel-client/chatrepo-local.env` outside the repository:

```env
CONTROL_PLANE_API_KEY=sk-REPLACE_WITH_YOUR_RUNTIME_KEY
```

```bash
chmod 600 "$HOME/.config/tunnel-client/chatrepo-local.env"
```

The YAML references the secret through an environment variable, so the key never
appears in the profile file.

## 6. Perform the first manual health check

Start the MCP server in the first terminal:

```bash
cd "$CHATREPO_DIR"
set -a
. "$HOME/.config/chatrepo-mcp/chatrepo-mcp.env"
set +a
./bin/chatrepo-mcp
```

For Python, activate `.venv` and run `python -m chatrepo_mcp` instead.

In a second terminal validate and start the tunnel client:

```bash
set -a
. "$HOME/.config/tunnel-client/chatrepo-local.env"
set +a
"$HOME/.local/bin/tunnel-client" doctor --profile chatrepo-local --explain
"$HOME/.local/bin/tunnel-client" run --profile chatrepo-local
```

From a third terminal, verify both layers:

```bash
curl -fsS http://127.0.0.1:8081/healthz
curl -fsS http://127.0.0.1:8081/readyz
cd "$CHATREPO_DIR"
.venv/bin/python scripts/check_tools.py http://127.0.0.1:2091/mcp
```

Expected output includes `live` from `/healthz`, `ready` from `/readyz`, and the
MCP tool list from `check_tools.py`. The local status UI is available at
`http://127.0.0.1:8081/ui` and must remain loopback-only.

When this MCP server intentionally runs without OAuth, `doctor` can report
missing OAuth metadata. Treat `/readyz` and an actual MCP tool call as the final
health signal in this no-auth private-tunnel setup.

## 7. Create the ChatGPT app using the tunnel

1. Open [ChatGPT Plugins / Apps](https://chatgpt.com/plugins).
2. Select the **+** button to create a developer-mode app. Depending on the
   current UI this may appear under **Settings -> Plugins -> +**.
3. Enter an app name and description, for example `ChatRepo MCP`.
4. Under **Connection**, select **Tunnel**.
5. Select the tunnel from the list, or paste its `tunnel_id`.
6. Review and accept the custom-MCP safety warning, then create the app.
7. In a new chat select the app in the tools menu and start with a low-risk test:
   `Show repo_info and git_status`.

Do not enter `http://127.0.0.1:2091/mcp` into the ChatGPT form. ChatGPT cannot
reach localhost on the PC. Secure MCP Tunnel uses the selected tunnel identity,
not a public URL.

If the tunnel is missing from the selector, confirm that it is associated with
the target ChatGPT workspace, not only the Platform organization, and that the
app creator has `Tunnels Read + Use`.

## 8. Make it survive terminal close and reboot

The manual commands above are only a proof. Closing either terminal or rebooting
the PC stops the MCP server or the tunnel client. Use two independent systemd
user services: one owns the local MCP server and one owns `tunnel-client`.

Create `$HOME/.config/systemd/user/chatrepo-mcp.service` and replace every
`/home/USER/src/chatrepo-mcp` value with the absolute path to your clone:

```ini
[Unit]
Description=ChatRepo MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/USER/src/chatrepo-mcp
EnvironmentFile=%h/.config/chatrepo-mcp/chatrepo-mcp.env
ExecStart=/home/USER/src/chatrepo-mcp/bin/chatrepo-mcp
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

For Python, replace `ExecStart` with
`/home/USER/src/chatrepo-mcp/.venv/bin/python -m chatrepo_mcp`.

Create `$HOME/.config/systemd/user/chatrepo-mcp-tunnel.service`:

```ini
[Unit]
Description=OpenAI Secure MCP Tunnel for ChatRepo
After=chatrepo-mcp.service
Requires=chatrepo-mcp.service

[Service]
Type=simple
EnvironmentFile=%h/.config/tunnel-client/chatrepo-local.env
ExecStart=/home/USER/.local/bin/tunnel-client run --profile chatrepo-local
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

Enable both services and keep the user service manager alive after reboot even
when nobody has logged into the graphical session:

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now chatrepo-mcp.service
systemctl --user enable --now chatrepo-mcp-tunnel.service
systemctl --user status chatrepo-mcp.service chatrepo-mcp-tunnel.service
```

`Restart=always` restarts an exited process. If the internet drops, the client
remains under systemd supervision and can reconnect when connectivity returns.

After a reboot, verify the actual persistent state:

```bash
systemctl --user is-enabled chatrepo-mcp.service chatrepo-mcp-tunnel.service
systemctl --user is-active chatrepo-mcp.service chatrepo-mcp-tunnel.service
curl -fsS http://127.0.0.1:8081/readyz
journalctl --user -u chatrepo-mcp-tunnel.service -n 100 --no-pager
```

Both services must report `enabled` and `active`; `/readyz` must return `ready`.

## Troubleshooting

| Symptom | Verify |
|---|---|
| `401 Unauthorized` | The runtime key is invalid, revoked, or lacks `Tunnels Read + Use`. Create a new runtime key and replace the local secret file. |
| Tunnel is absent in ChatGPT | Tunnel-to-workspace association and `Tunnels Read + Use` for the app creator. |
| `/readyz` is not ready | `journalctl --user -u chatrepo-mcp-tunnel.service -n 100 --no-pager`, then `tunnel-client doctor --profile chatrepo-local --explain`. |
| The client cannot reach MCP | The MCP service is active, port `2091` matches both files, and `scripts/check_tools.py` succeeds locally. |
| Tunnel is offline after reboot | `loginctl show-user "$USER" -p Linger`, both services are enabled, and service logs show no startup error. |
| The app cannot be created | The ChatGPT plan, workspace policy, or developer-mode permission does not allow custom MCP apps. This is distinct from Platform tunnel permissions. |

## Operational boundaries

- Secure MCP Tunnel is for supported OpenAI products. It does not create a
  universal public MCP URL for Claude, curl, or other clients. Use a separate
  solution, such as Cloudflare Named Tunnel with a domain and access policy, for
  that requirement.
- A private tunnel does not make powerful MCP tools harmless. Keep
  `PROJECT_ROOT` narrow and expose only a trusted server.
- Deleting a tunnel in Platform does not remove local configuration or services.
  When retiring this setup, disable both services and revoke the runtime key.

## Official references

- [OpenAI Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
- [openai/tunnel-client releases and documentation](https://github.com/openai/tunnel-client)
- [ChatGPT developer mode and MCP apps](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)
- [Managing projects and API keys in Platform](https://help.openai.com/en/articles/9186755-managing-projects-in-the-api-platform)
- [OpenAI API key safety practices](https://help.openai.com/en/articles/5112595-best-practices-for-api-key-safety)

_Verified against the linked official sources on 2026-07-10. OpenAI UI,
permissions, and availability are subject to change._
