#!/usr/bin/env bash
# Local development server.
# Runs the FastAPI web server against ./db/history.db with no nginx subpath.
# Usage: bash dev.sh

set -euo pipefail
cd "$(dirname "$0")/.."

DB="$(pwd)/db/history.db"
if [[ ! -f "$DB" ]]; then
    echo "ERROR: $DB not found — copy history.db from the Pi first."
    exit 1
fi

export RSBS_DB_PATH="$DB"
export RSBS_ROOT_PATH=""
export RSBS_WEB_HOST="127.0.0.1"
export RSBS_WEB_PORT="8080"

echo "Starting web server → http://127.0.0.1:8080/"
echo "  DB: $DB"
VENV="$(pwd)/.venv"
if [[ -d "$VENV" ]]; then
    PYTHON="$VENV/bin/python"
    UVICORN="$VENV/bin/uvicorn"
else
    PYTHON="python3"
    UVICORN="uvicorn"
fi

exec "$UVICORN" readsbstats.web:app --host 127.0.0.1 --port 8080 --reload
