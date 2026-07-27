#!/bin/bash
# One-click launcher for the Trading System dashboard (macOS: double-click me).
#
# Ensures the Python venv + deps, builds the web frontend if needed, starts the
# local server bound to 127.0.0.1 (never exposed off-host — it can place real
# orders), and opens your browser. Close this window / press Ctrl+C to stop.
set -e
cd "$(dirname "$0")"

PORT=8000
URL="http://127.0.0.1:$PORT"

# --- Python venv + dependencies -------------------------------------------
if [ ! -x .venv/bin/python ]; then
  echo "Creating Python virtual environment (.venv)..."
  python3 -m venv .venv
fi
PY=.venv/bin/python
if ! "$PY" -c "import fastapi, uvicorn, sse_starlette" >/dev/null 2>&1; then
  echo "Installing Python dependencies (first run)..."
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi

# --- Frontend build (only when missing or stale) --------------------------
NEED_BUILD=0
if [ ! -f web/dist/index.html ]; then
  NEED_BUILD=1
elif [ -n "$(find web/src web/index.html web/package.json -newer web/dist/index.html 2>/dev/null | head -1)" ]; then
  NEED_BUILD=1
fi
if [ "$NEED_BUILD" = "1" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Building the dashboard (this can take a minute the first time)..."
    ( cd web && npm install --no-audit --no-fund --silent && npm run build )
  else
    echo "!! Node.js 18+ not found — cannot build the dashboard frontend."
    echo "   Install Node from https://nodejs.org and re-run this launcher."
    if [ ! -f web/dist/index.html ]; then
      echo "   No existing build to fall back to. Exiting."
      exit 1
    fi
    echo "   Using the previously built dashboard."
  fi
fi

# --- Open the browser once the server responds ----------------------------
(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL/api/status" 2>/dev/null; then open "$URL"; break; fi
    sleep 0.5
  done
) &

echo ""
echo "  Trading System dashboard →  $URL"
echo "  (press Ctrl+C or close this window to stop)"
echo ""
exec "$PY" -m uvicorn server.app:app --host 127.0.0.1 --port "$PORT"
