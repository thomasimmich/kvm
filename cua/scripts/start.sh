#!/usr/bin/env bash
# Start the CUA service: MCP server at :8765/mcp and the dashboard, which opens in the browser.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=$(sed -n 's/^CUA_MCP_PORT=//p' .env 2>/dev/null || true)
PORT=${PORT:-8765}
( for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && { open "http://127.0.0.1:$PORT/dashboard"; exit; }
    sleep 1
  done ) &
exec uv run cua serve "$@"
