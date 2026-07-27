#!/bin/bash
# Install once-per-trading-day launchd jobs on macOS.
#
#   ./scheduling/install.sh                 # advice job only (individual, read-only)
#   ./scheduling/install.sh --enable-live   # + the AUTONOMOUS live-trading job (agentic)
#
# Two jobs, both fire on weekdays; the orchestrator's market-day gate no-ops on
# NYSE holidays:
#   * individual advice  — 10:00 local — recommend mode, READ-ONLY, never trades.
#   * agentic autonomous — 10:05 local — live mode on the $100 account (--enable-live).
#
# TIMES ARE IN THE MAC'S LOCAL TIMEZONE. Defaults assume US/Eastern (market
# time). If this Mac is not on Eastern time, edit HOUR_* below.
#
# Safety: the autonomous live job is NOT installed unless you pass --enable-live
# AND type the confirmation — do the Phase 5 commissioning (one supervised
# order) first.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SH="$REPO_DIR/scheduling/run.sh"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_ADVICE="com.trading-system.individual-advice"
LABEL_LIVE="com.trading-system.agentic-live"

HOUR_ADVICE=10; MIN_ADVICE=0
HOUR_LIVE=10;   MIN_LIVE=5

ENABLE_LIVE=0
[ "${1:-}" = "--enable-live" ] && ENABLE_LIVE=1

chmod +x "$RUN_SH"
mkdir -p "$AGENTS_DIR" "$REPO_DIR/logs"

# Emit a launchd plist that fires Mon-Fri at HOUR:MIN. Args after the first 3
# positionals are the orchestrator.py arguments.
write_plist () {
  local label="$1" hour="$2" minute="$3"; shift 3
  local plist="$AGENTS_DIR/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    echo "    <string>$RUN_SH</string>"
    for arg in "$@"; do echo "    <string>$arg</string>"; done
    echo '  </array>'
    echo '  <key>StartCalendarInterval</key><array>'
    for wd in 1 2 3 4 5; do
      echo "    <dict><key>Weekday</key><integer>$wd</integer>"
      echo "         <key>Hour</key><integer>$hour</integer>"
      echo "         <key>Minute</key><integer>$minute</integer></dict>"
    done
    echo '  </array>'
    echo "  <key>StandardOutPath</key><string>$REPO_DIR/logs/launchd.out.log</string>"
    echo "  <key>StandardErrorPath</key><string>$REPO_DIR/logs/launchd.err.log</string>"
    echo "  <key>WorkingDirectory</key><string>$REPO_DIR</string>"
    echo '</dict></plist>'
  } > "$plist"
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "  loaded: $plist"
}

echo "Installing individual advice job (read-only, 10:00 local, weekdays)..."
write_plist "$LABEL_ADVICE" "$HOUR_ADVICE" "$MIN_ADVICE" \
  --mode recommend --account individual

if [ "$ENABLE_LIVE" = "1" ]; then
  echo ""
  echo "!!! You are about to schedule AUTONOMOUS REAL-MONEY trading on the"
  echo "!!! agentic account (live mode, no per-order confirmation)."
  echo "!!! Only do this AFTER the supervised commissioning order (Phase 5)."
  read -r -p "Type 'ENABLE LIVE' to confirm: " ans
  if [ "$ans" = "ENABLE LIVE" ]; then
    echo "Installing agentic autonomous job (live, 10:05 local, weekdays)..."
    write_plist "$LABEL_LIVE" "$HOUR_LIVE" "$MIN_LIVE" \
      --mode live --yes --profile momentum --account agentic
  else
    echo "Confirmation not given — live job NOT installed."
  fi
else
  echo "(Autonomous live job NOT installed — re-run with --enable-live after commissioning.)"
fi

echo ""
echo "Done. Inspect:  launchctl list | grep trading-system"
echo "Logs:           $REPO_DIR/logs/scheduled.log"
echo "Disable all:    ./scheduling/uninstall.sh   (or engage the kill switch)"
