#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/chatrepo-mcp}"
TARGET_REPO="${TARGET_REPO:-/opt/myproject}"
CHATREPO_IMPLEMENTATION="${CHATREPO_IMPLEMENTATION:-go}"

sudo apt-get update
sudo apt-get install -y git ripgrep caddy

if ! id chatrepo >/dev/null 2>&1; then
  sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin chatrepo
fi

mkdir -p "$APP_DIR"
mkdir -p "$TARGET_REPO"

cd "$APP_DIR"
case "$CHATREPO_IMPLEMENTATION" in
  python)
    sudo apt-get install -y python3 python3-venv python3-pip
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -U pip
    pip install -e ./python
    ;;
  go)
    if ! command -v go >/dev/null 2>&1; then
      echo "Go 1.25+ is required to build from source; alternatively install a release binary." >&2
      exit 1
    fi
    make build
    ;;
  *)
    echo "CHATREPO_IMPLEMENTATION must be 'python' or 'go'" >&2
    exit 2
    ;;
esac

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env — edit PROJECT_ROOT before starting."
fi

echo "Done. Next:"
echo "  1) edit $APP_DIR/.env"
if [[ "$CHATREPO_IMPLEMENTATION" == "python" ]]; then
  echo "  2) run: source $APP_DIR/.venv/bin/activate && python -m chatrepo_mcp"
else
  echo "  2) run: $APP_DIR/bin/chatrepo-mcp"
fi
