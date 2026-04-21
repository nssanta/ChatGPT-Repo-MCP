#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000/mcp}"

echo "GET $BASE_URL"
curl -i "$BASE_URL" || true
echo

echo "POST tools/list"
curl -sS -X POST "$BASE_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}' | sed 's/\\n/\n/g'
echo
