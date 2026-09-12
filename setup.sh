#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/fire-alarm-listener"

mkdir -p "$DEST" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"

for f in \
  fire_alarm_listener.py \
  regression_test.py \
  requirements.txt \
  fskj222_room_16k.wav \
  fskj222_direct_16k.wav \
  local.fire-alarm-listener.plist.template \
  install-service.sh \
  uninstall-service.sh \
  homebridge-smoke.sh \
  homebridge-snippet.json \
  README.txt; do
  cp "$SCRIPT_DIR/$f" "$DEST/$f"
done

if [[ ! -x "$DEST/.venv/bin/python" ]]; then
  python3 -m venv "$DEST/.venv"
fi

"$DEST/.venv/bin/python" -m pip install --upgrade pip
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"
chmod +x "$DEST/fire_alarm_listener.py" "$DEST/regression_test.py" \
  "$DEST/install-service.sh" "$DEST/uninstall-service.sh" "$DEST/homebridge-smoke.sh"

echo
echo "Setup complete: $DEST"
echo
echo "1) Run regression:"
echo "   cd $DEST && .venv/bin/python regression_test.py"
echo
echo "2) Run detector manually once so macOS can grant microphone access:"
echo "   $DEST/.venv/bin/python $DEST/fire_alarm_listener.py --debug --no-homebridge"
echo
echo "3) After Homebridge smoke sensor is configured and tested, install service:"
echo "   $DEST/install-service.sh"
