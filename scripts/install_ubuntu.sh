#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/chatrepo-mcp}"
TARGET_REPO="${TARGET_REPO:-/opt/myproject}"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git ripgrep caddy

if ! id chatrepo >/dev/null 2>&1; then
  sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin chatrepo
fi

mkdir -p "$APP_DIR"
mkdir -p "$TARGET_REPO"

cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env — edit PROJECT_ROOT before starting."
fi

echo "Done. Next:"
echo "  1) edit $APP_DIR/.env"
echo "  2) run: source $APP_DIR/.venv/bin/activate && python -m chatrepo_mcp"
