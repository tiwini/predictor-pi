#!/usr/bin/env bash
# Regenera tutorial_btc.{html,pdf} y los screenshots de tutorial_btc_assets/.
# Requiere: server :8001 corriendo + playwright instalado (./venv/bin/pip install
# playwright markdown && ./venv/bin/playwright install chromium).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! curl -sf -o /dev/null http://127.0.0.1:8001/; then
  echo "ERROR: predictor_web no responde en :8001. Arráncalo primero."
  exit 1
fi

./venv/bin/python3 scripts/take_screenshots.py
./venv/bin/python3 scripts/build_tutorial_btc.py
echo "OK: tutorial_btc.{html,pdf} regenerados."
