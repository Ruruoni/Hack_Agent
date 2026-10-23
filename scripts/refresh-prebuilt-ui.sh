#!/usr/bin/env bash
# Developers: rebuild the UI and refresh the committed prebuilt copy (used when a user's Node.js cannot build it).
set -euo pipefail
cd "$(dirname "$0")/../frontend"
npm run build
rm -rf prebuilt && cp -r dist prebuilt
echo "frontend/prebuilt refreshed - commit it."
