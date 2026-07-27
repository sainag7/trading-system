#!/bin/bash
# Wrapper for scheduled trading-system runs (launchd / cron).
# Runs ONE orchestrator cycle and appends output to logs/scheduled.log.
# All arguments are passed straight through to orchestrator.py, e.g.:
#   run.sh --mode recommend --account individual
#   run.sh --mode live --yes --profile momentum --account agentic
#
# The market-day gate inside the orchestrator makes weekend/holiday firings a
# clean no-op, so it is safe to schedule this on every weekday.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# Prefer a project virtualenv if present, else the system python3.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  PY="$(command -v python3)"
fi

mkdir -p logs
LOG="logs/scheduled.log"
echo "" >> "$LOG"
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') :: orchestrator.py $* =====" >> "$LOG"
exec "$PY" orchestrator.py "$@" >> "$LOG" 2>&1
