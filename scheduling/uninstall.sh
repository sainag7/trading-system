#!/bin/bash
# Remove the trading-system launchd jobs (stops all scheduled runs).
set -euo pipefail

AGENTS_DIR="$HOME/Library/LaunchAgents"
for label in com.trading-system.individual-advice com.trading-system.agentic-live; do
  plist="$AGENTS_DIR/$label.plist"
  if [ -f "$plist" ]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "removed: $plist"
  fi
done
echo "Done. No scheduled trading-system runs remain."
echo "(For an instant emergency stop without uninstalling, run: python orchestrator.py --kill)"
