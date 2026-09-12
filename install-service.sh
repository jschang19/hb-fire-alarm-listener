#!/bin/zsh
set -euo pipefail

DEST="$HOME/fire-alarm-listener"
PLIST="$HOME/Library/LaunchAgents/local.fire-alarm-listener.plist"
LABEL="local.fire-alarm-listener"
UID_NOW="$(id -u)"

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
  echo "Run $DEST/setup.sh (or the package ./setup.sh) first." >&2
  exit 1
fi

sed "s|__HOME__|$HOME|g" "$DEST/local.fire-alarm-listener.plist.template" > "$PLIST"
plutil -lint "$PLIST"

launchctl bootout "gui/$UID_NOW/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NOW" "$PLIST"
launchctl enable "gui/$UID_NOW/$LABEL"
launchctl kickstart -k "gui/$UID_NOW/$LABEL"

echo "Installed and started $LABEL"
echo "Status: launchctl print gui/$UID_NOW/$LABEL"
echo "Log:    $HOME/Library/Logs/fire-alarm-listener.log"
echo "Errors: $HOME/Library/Logs/fire-alarm-listener.err.log"
