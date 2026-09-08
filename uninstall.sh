#!/bin/bash
# Stops and removes the MB275 monitor background service.
set -euo pipefail

LABEL="com.tymofii.mb275monitor"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ -f "$PLIST_DST" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_DST" >/dev/null 2>&1 || true
  rm -f "$PLIST_DST"
  echo "Service stopped and removed."
else
  echo "Service was not installed (no plist found)."
fi

echo "Note: this only stops the background service. Your config.json, logs,"
echo "and the project folder itself were left untouched."
