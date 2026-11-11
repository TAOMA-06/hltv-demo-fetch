#!/usr/bin/env bash
# Convenience wrapper around `python -m hltv_demo_fetch`.
#
#   ./scripts/fetch-hltv.sh --limit 5 --since 2026-10-01
#   ./scripts/fetch-hltv.sh --engine browser --headed --demo-id 113158
#   ./scripts/fetch-hltv.sh --engine cdp --cdp-url http://127.0.0.1:9222
#
# Uses .venv/bin/python when present; override with PYTHON=/path/to/python.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

exec "$PYTHON" -m hltv_demo_fetch "$@"
