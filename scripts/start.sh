#!/usr/bin/env bash
# Starts the NEXUS backend (serves the built web UI on http://127.0.0.1:8000). Use --dev for the Vite dev server too.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
[ -d .venv ] || { echo "Run scripts/setup.sh first."; exit 1; }
# shellcheck disable=SC1091
. .venv/bin/activate
mkdir -p data/run data/logs
if [ -f data/run/backend.pid ] && kill -0 "$(cat data/run/backend.pid)" 2>/dev/null; then echo "NEXUS already running (PID $(cat data/run/backend.pid))"; exit 0; fi
[ -d frontend/dist ] || { command -v npm >/dev/null && (cd frontend && npm run build) || true; }
# exec makes the recorded PID the real server process, so stop.sh can end it cleanly
( cd "$ROOT/backend"; exec nohup python -m nexus.cli serve --host 127.0.0.1 --port 8000 > "$ROOT/data/logs/backend.log" 2>&1 ) &
echo $! > data/run/backend.pid
if [ "${1:-}" = "--dev" ]; then
  ( cd "$ROOT/frontend"; exec nohup npm run dev > "$ROOT/data/logs/frontend.log" 2>&1 ) &
  echo $! > data/run/frontend.pid
  echo "UI (dev): http://localhost:5173"
fi
for _ in $(seq 1 40); do curl -fs -m 2 http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break; sleep 0.5; done
echo "NEXUS API/UI: http://127.0.0.1:8000   (logs: data/logs, stop with scripts/stop.sh)"
