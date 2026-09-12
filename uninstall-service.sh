#!/bin/zsh
set -euo pipefail
LABEL="local.fire-alarm-listener"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NOW="$(id -u)"
launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Stopped and removed LaunchAgent $LABEL"
echo "Project files remain at $HOME/fire-alarm-listener"
