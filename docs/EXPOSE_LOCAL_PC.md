# Expose a Local PC with a Stable Address

How to run this MCP server on your own machine (behind NAT / dynamic IP) so it
always has **one stable public entry point** that survives reboots.

The real problem is **not Caddy**. A home PC behind NAT/CGNAT cannot rely on an
inbound reverse proxy, because that needs a public IP plus open ports 80/443,
which most home connections don't have (CGNAT, dynamic IP, ISP terms). The
correct primitive is an **outbound tunnel** with a fixed address, not an inbound
proxy. Caddy only makes sense on a VPS with a public IP.

## Check the plan gate first, not the tunnel

OpenAI's current policy says full write/modify MCP support is beta for
**Business / Enterprise / Edu**; Pro can create apps and connect MCPs with
read/fetch permissions. Product UI and entitlements are rolling out, so confirm
what your own ChatGPT workspace allows before exposing write or command tools.
A tunnel solves *delivery*, not this *permission*.

## Complete OpenAI Tunnel walkthrough

For the exact Linux setup used with this project - Platform tunnel creation,
runtime key, `tunnel-client`, ChatGPT app, `systemd --user`, reboot verification,
and troubleshooting - see the [OpenAI Secure MCP Tunnel runbook](OPENAI_SECURE_TUNNEL_RUNBOOK.md).

## Comparison

Legend: ✅ good / yes · 🟡 with caveat · ❌ minus / no. "Auto-start" assumes a
configured systemd unit.

| Option | Stable address | No domain | Behind CGNAT | Privacy | Cost | Auto-start | Not only ChatGPT |
|---|---|---|---|---|---|---|---|
| **OpenAI Secure MCP Tunnel** | ✅ address = `tunnel_id`, never changes | ✅ | ✅ outbound-only | ✅ endpoint not public | 🟡 client free; needs API key | 🟡 systemd on you | ❌ OpenAI only |
| **Cloudflare Named Tunnel** | ✅ permanent subdomain | ❌ needs domain on CF | ✅ | 🟡 public (lock with CF Access) | ✅ free + domain ~$1–10/yr | ✅ installs systemd itself | ✅ any client |
| **Tailscale Funnel** | ✅ fixed `*.ts.net` | ✅ | ✅ | 🟡 public when Funnel is on | ✅ free | ✅ service out of the box | ✅ any client |
| **ngrok** | 🟡 one dev domain | ✅ | ✅ | 🟡 public | 🟡 always-on → paid | 🟡 systemd on you | ✅ any client |
| **Caddy + public IP / ports** | 🟡 needs static IP or DDNS | ❌ needs domain | ❌ breaks under CGNAT | ❌ ports open to the world | ✅ domain only | ✅ systemd | ✅ any client |
| **Self-hosted relay (frp / rathole)** | ✅ own domain/IP | ❌ usually a domain | ✅ via VPS | 🟡 under your control | 🟡 VPS ~$3–5/mo | ✅ systemd ×2 | ✅ any client |

## Recommendation (ranked)

1. **OpenAI Secure MCP Tunnel** — if the target is ChatGPT specifically and your
   Platform org has the rights. Most native and most private: the address is a
   `tunnel_id` that physically cannot change, the endpoint is never public, and
   no domain or ports are needed. In ChatGPT you pick **Connection → Tunnel**
   instead of pasting a URL.
2. **Cloudflare Named Tunnel** — best generic choice: a permanent subdomain,
   free, `cloudflared` registers its own systemd service, works with any client
   (ChatGPT, Claude, curl). Requires a domain on Cloudflare.
3. **Tailscale Funnel** — if you want no domain and no Platform org, and a public
   URL is fine. Fixed `*.ts.net` hostname, free, runs as a service.
4. **ngrok** — quickest start for a test; "always on" is not guaranteed on the
   free plan (needs a paid plan).
5. **Caddy / self-hosted relay** — only if you already have a public IP + open
   ports or a cheap VPS. Not a primary path for a home PC behind NAT.

## The three front-runners in detail

### OpenAI Secure MCP Tunnel — native, most private

The Go client `tunnel-client` opens an outbound HTTPS connection to OpenAI,
long-polls for queued work, and forwards each JSON-RPC request to your local
server. In ChatGPT you select **Connection → Tunnel** and your tunnel, so the
address can never drift.

- Needs Platform-org rights (`Tunnels Read + Manage` to create, `Read + Use` to
  run the client), a `tunnel_id`, and a runtime API key.
- The client is open source (Apache-2.0, `openai/tunnel-client`).
- Reboot survival is on you (systemd).

### Cloudflare Named Tunnel — universal, any client

A permanent subdomain on your own domain, TLS terminated by Cloudflare, zero open
ports. `cloudflared service install` registers a systemd unit that auto-starts on
boot and reconnects on network changes.

- Only requirement: a domain added to Cloudflare (the plan itself is free).
- Address example: `https://mcp.yourdomain.com/mcp`.
- The same URL also works for Claude, curl, and other MCP clients.

### Tailscale Funnel — no domain, no Platform org

A fixed hostname tied to the machine, free, no domain purchase. Enabled with one
command and runs as a system service.

- Address example: `https://your-pc.tailnet.ts.net/mcp`.
- Trade-off: the hostname is less pretty and Funnel must be enabled once in the
  tailnet admin.

## Required regardless of the tunnel

These do not depend on which tunnel you pick.

1. **Two separate systemd services** — the server (`chatrepo-mcp`) and the tunnel
   client as distinct units with `Restart=always` and `enable`. Then a server
   redeploy never changes the address and everything comes back after a reboot.
2. **Bearer token on the public edge** — the default is `MCP_AUTH_MODE=none`, but
   write/command tools are exposed. Set `MCP_AUTH_MODE=bearer` plus a long
   `MCP_BEARER_TOKEN`. Less critical for the OpenAI tunnel (traffic only comes
   from OpenAI) but still worth it.
3. **Verify the ChatGPT gate before anything else** — confirm your plan/workspace
   can create a write connector at all. If it is read/fetch only, do not expose
   the V5 write layer.

## Sources

- OpenAI Secure MCP Tunnel — <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>
- `openai/tunnel-client` — <https://github.com/openai/tunnel-client>
- ChatGPT Developer Mode / MCP apps — <https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt>
- Cloudflare Tunnel — <https://developers.cloudflare.com/tunnel/>
- ngrok free-plan limits — <https://ngrok.com/docs/pricing-limits/free-plan-limits>

_Accurate as of July 2026._
