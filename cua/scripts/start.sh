#!/usr/bin/env bash
# Start the CUA service: MCP server at :8765/mcp and the dashboard, which opens in the browser.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=$(sed -n 's/^CUA_MCP_PORT=//p' .env 2>/dev/null || true)
PORT=${PORT:-8765}

# Stop a CUA server left over on this port (Ctrl+C takes a moment, or one runs in another terminal).
for pid in $(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null); do
  if ps -o command= -p "$pid" | grep -q "cua serve"; then
    echo "▶ Stopping the CUA server already running on port $PORT (pid $pid) ..."
    kill "$pid"
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  else
    echo "Port $PORT is used by another program: $(ps -o command= -p "$pid")" >&2
    exit 1
  fi
done
( for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && { open "http://127.0.0.1:$PORT/dashboard"; exit; }
    sleep 1
  done ) &
exec uv run cua serve "$@"
