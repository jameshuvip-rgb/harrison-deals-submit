#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -r requirements.txt
export HD_SUBMIT_HOST="${HD_SUBMIT_HOST:-0.0.0.0}"
export HD_SUBMIT_PORT="${HD_SUBMIT_PORT:-8788}"
exec python app.py
