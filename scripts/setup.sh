#!/usr/bin/env bash
# Linux/macOS convenience equivalent of scripts/setup.ps1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { echo "Python 3.10+ is required (found: $($PY --version 2>&1))"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
pip install -r backend/requirements-dev.txt
pip install -r backend/requirements-optional.txt || echo "(optional packages skipped - NEXUS will use its built-in fallbacks)"
[ -f .env ] || cp .env.example .env
if command -v npm >/dev/null; then (cd frontend && npm install --no-audit --no-fund && npm run build); else echo "Node.js/npm not found: the web UI will not be built (API still works)."; fi
(cd backend && python -m nexus.cli init-db)
echo "Setup finished. Start with: scripts/start.sh"
