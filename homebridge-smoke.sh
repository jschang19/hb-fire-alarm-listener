#!/bin/zsh
set -euo pipefail
BASE="http://127.0.0.1:51828/"
ID="livingRoomFireAlarm"
ACTION="${1:-status}"
case "$ACTION" in
  on|true)
    curl -fsS -G "$BASE" --data-urlencode "accessoryId=$ID" --data-urlencode "state=true"; echo ;;
  off|false)
    curl -fsS -G "$BASE" --data-urlencode "accessoryId=$ID" --data-urlencode "state=false"; echo ;;
  status)
    curl -fsS -G "$BASE" --data-urlencode "accessoryId=$ID"; echo ;;
  *)
    echo "Usage: $0 {on|off|status}" >&2
    exit 2 ;;
esac
