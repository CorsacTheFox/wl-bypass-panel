#!/usr/bin/env bash
# Convenience launcher. Creates a venv, installs deps, and starts uvicorn.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "Starting server on ${WB_HOST:-127.0.0.1}:${WB_PORT:-8000}"
exec python -m uvicorn main:app --host "${WB_HOST:-127.0.0.1}" --port "${WB_PORT:-8000}"
