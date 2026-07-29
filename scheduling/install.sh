#!/bin/bash
# Install once-per-trading-day launchd jobs on macOS.
#
#   ./scheduling/install.sh                 # advice job only (individual, read-only)
#   ./scheduling/install.sh --enable-live   # + the AUTONOMOUS live-trading job (agentic)
#
# Two jobs, both fire on weekdays; the orchestrator's market-day gate no-ops on
# NYSE holidays:
#   * individual advice  — 10:00 ET — recommend mode, READ-ONLY, never trades.
#   * agentic autonomous — 10:05 ET — live mode on the $100 account (--enable-live).
#
# Schedule times are expressed in MARKET TIME (US/Eastern) and converted to this
# Mac's local timezone at install, because launchd fires on local time. Getting
# this wrong is silent: on a Central-time Mac a naive "10:00" fires at 11:00 ET,
# 90 minutes after the open instead of the intended 30.
#
# Re-run this installer if the Mac's timezone changes — the conversion is
# resolved once, here, not at fire time.
#
# Safety: the autonomous live job is NOT installed unless you pass --enable-live
# AND type the confirmation — do the commissioning (one supervised order) first.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SH="$REPO_DIR/scheduling/run.sh"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_ADVICE="com.trading-system.individual-advice"
LABEL_LIVE="com.trading-system.agentic-live"

# Target times in MARKET TIME (ET). 10:00 ET is 30 minutes after the open, which
# lets the opening auction settle before the daily cycle reads prices.
ET_HOUR_ADVICE=10; ET_MIN_ADVICE=0
ET_HOUR_LIVE=10;   ET_MIN_LIVE=5

# Convert an ET wall-clock time to this Mac's local wall clock. Echoes "H M".
et_to_local () {
  python3 - "$1" "$2" <<'PY'
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

hour, minute = int(sys.argv[1]), int(sys.argv[2])
et = datetime.now(ZoneInfo("America/New_York")).replace(
    hour=hour, minute=minute, second=0, microsecond=0)
local = et.astimezone()
print(local.hour, local.minute)
PY
}

read -r HOUR_ADVICE MIN_ADVICE <<<"$(et_to_local $ET_HOUR_ADVICE $ET_MIN_ADVICE)"
read -r HOUR_LIVE   MIN_LIVE   <<<"$(et_to_local $ET_HOUR_LIVE   $ET_MIN_LIVE)"

LOCAL_TZ="$(date +%Z)"
printf 'Market time %02d:%02d ET  ->  %02d:%02d %s (this Mac)\n' \
  "$ET_HOUR_ADVICE" "$ET_MIN_ADVICE" "$HOUR_ADVICE" "$MIN_ADVICE" "$LOCAL_TZ"

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

printf "Installing individual advice job (read-only, %02d:%02d %s, weekdays)...\n" \
  "$HOUR_ADVICE" "$MIN_ADVICE" "$LOCAL_TZ"
write_plist "$LABEL_ADVICE" "$HOUR_ADVICE" "$MIN_ADVICE" \
  --mode recommend --account individual

if [ "$ENABLE_LIVE" = "1" ]; then
  echo ""
  echo "!!! You are about to schedule AUTONOMOUS REAL-MONEY trading on the"
  echo "!!! agentic account (live mode, no per-order confirmation)."
  echo "!!! Only do this AFTER the supervised commissioning order (Phase 5)."
  read -r -p "Type 'ENABLE LIVE' to confirm: " ans
  if [ "$ans" = "ENABLE LIVE" ]; then
    printf "Installing agentic autonomous job (live, %02d:%02d %s, weekdays)...\n" \
      "$HOUR_LIVE" "$MIN_LIVE" "$LOCAL_TZ"
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
