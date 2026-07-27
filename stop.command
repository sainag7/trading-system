#!/bin/bash
# Stop the Trading System dashboard (the local server on port 8000).
PORT=8000
pids=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null || true
  echo "Stopped the dashboard (port $PORT)."
else
  echo "The dashboard doesn't appear to be running on port $PORT."
fi
sleep 1
