#!/bin/bash
# =============================================================
#  VPS STARTUP SCRIPT
#  Runs both the Telegram Bot AND the FastAPI/Uvicorn web server
#  from a single command: bash start.sh
#
#  The entrypoint is main.py which internally calls:
#    uvicorn.run("main:app", host="0.0.0.0", port=$PORT)
#
#  To run manually:
#    python main.py
#
#  To run with Uvicorn directly (alternative):
#    uvicorn main:app --host 0.0.0.0 --port 8080
# =============================================================

set -e

# ── Load env file if present ──────────────────────────────────
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
    echo "✅ Environment loaded from .env"
fi

# ── Default port if not set ───────────────────────────────────
export PORT="${PORT:-8080}"
echo "🚀 Starting on port $PORT ..."

# ── Launch the bot + web server ───────────────────────────────
exec python main.py
