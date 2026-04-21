# Deploy on Ubuntu 24

## Requirements

Install:

- Python 3.12+
- git
- ripgrep
- Caddy or Nginx
- systemd

## Recommended filesystem layout

```text
/opt/myproject
/opt/chatrepo-mcp
```

## 1. Create a service user

```bash
sudo useradd --system --home /opt/chatrepo-mcp --shell /usr/sbin/nologin chatrepo
```

## 2. Clone your repo and this MCP project

```bash
sudo mkdir -p /opt/chatrepo-mcp
sudo chown -R $USER:$USER /opt/chatrepo-mcp
cd /opt/chatrepo-mcp
git clone <THIS_PROJECT_REPO_URL> .
```

Your target code repo should exist separately, for example:

```bash
sudo mkdir -p /opt/myproject
sudo chown -R $USER:$USER /opt/myproject
git clone <TARGET_REPO_URL> /opt/myproject
```

## 3. Create virtualenv and install

```bash
cd /opt/chatrepo-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## 4. Configure environment

```bash
cp .env.example .env
nano .env
```

At minimum set:

```dotenv
PROJECT_ROOT=/opt/myproject
HOST=127.0.0.1
PORT=8000
TRANSPORT=streamable-http
```

## 5. Test locally

```bash
source .venv/bin/activate
python -m chatrepo_mcp
```

Then in another shell:

```bash
curl -i http://127.0.0.1:8000/mcp
```

A client may GET the endpoint for an SSE stream or POST JSON-RPC to the endpoint. MCP transport docs specify POST JSON-RPC to the MCP endpoint and allow GET for opening an SSE stream.

## 6. Install systemd unit

Copy:

```bash
sudo cp deploy/systemd/chatrepo-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable chatrepo-mcp
sudo systemctl start chatrepo-mcp
sudo systemctl status chatrepo-mcp
```

## 7. Put HTTPS in front

### Caddy

Use `deploy/caddy/Caddyfile.example` and replace the domain.

### Nginx

Use `deploy/nginx/chatrepo-mcp.conf.example` and add a valid TLS cert.

## 8. Important note about public URL

Remote MCP servers for ChatGPT must be reachable on the public internet; the ChatGPT developer mode docs describe creating an app for a **remote MCP server** and list SSE / streaming HTTP as supported protocols.

In practice, use a domain with HTTPS.

If you currently only have IPv4 and no domain yet, finish the code deployment first, then add:

- a domain pointed at the VPS, or
- a tunnel / edge proxy that gives you a stable HTTPS URL

## 9. First production hardening steps

- keep server read-only
- keep blocked patterns enabled
- run as a dedicated system user
- do not store secrets in readable repo files
- monitor logs
- do not expose write tools until you really need them
