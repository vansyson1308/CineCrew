#!/usr/bin/env bash
# CineCrew Studio — one-command local start (offline fixture by default; no paid calls).
#   scripts/start_studio.sh [--port 8765]
# Needs: python3 >= 3.10, ffmpeg/ffprobe, node >= 18 (only to build the UI once).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f studio-ui/dist/index.html ]; then (cd studio-ui && npm ci && npm run build); fi
exec python -m src.studio serve "$@"
