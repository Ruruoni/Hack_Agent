#!/usr/bin/env bash
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
for n in backend frontend; do
  f="$ROOT/data/run/$n.pid"
  if [ -f "$f" ]; then
    pid=$(cat "$f")
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null && echo "stopped $n ($pid)" || echo "$n was not running"
    rm -f "$f"
  fi
done
