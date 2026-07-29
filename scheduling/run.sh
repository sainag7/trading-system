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

# Not `exec`: the run has to be followed by rendering the briefing, so the
# scheduled output is pushed to a file instead of waiting in SQLite to be
# remembered. Capture the status rather than letting `set -e` abort here.
STATUS=0
"$PY" orchestrator.py "$@" >> "$LOG" 2>&1 || STATUS=$?

notify() {  # notify <title> <message>
  # Best-effort only: a headless session has no notification centre, and a
  # failed notification must never change the exit status of the run.
  /usr/bin/osascript -e "display notification \"${2//\"/\\\"}\" with title \"${1//\"/\\\"}\"" \
    >/dev/null 2>&1 || true
}

if [ "$STATUS" -eq 0 ]; then
  # Render the brief for the run that just finished (it is now the latest).
  # A rendering failure is reported but does not fail the run itself — the
  # orchestrator's own output is already safely in the log and the database.
  if BRIEF="$("$PY" -m reporting.brief 2>>"$LOG")"; then
    SUMMARY="$("$PY" -m reporting.brief --summary 2>>"$LOG" || echo "brief ready")"
    echo "----- brief: $BRIEF ($SUMMARY)" >> "$LOG"
    notify "Trading brief ready" "$SUMMARY"
  else
    echo "----- brief: FAILED to render (see above)" >> "$LOG"
    notify "Trading run finished" "Brief failed to render — check logs/scheduled.log"
  fi
else
  echo "----- orchestrator exited $STATUS" >> "$LOG"
  notify "Trading run FAILED" "orchestrator exited $STATUS — check logs/scheduled.log"
fi

exit "$STATUS"
