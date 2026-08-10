#!/bin/bash
# Reads the running server's link out of the log and renders a scannable QR.
# The QR holds a filebridge:// deep link, so the phone's ordinary camera opens
# the app already connected — no in-app scanner, no camera permission.
LOG="${1:-/tmp/filebridge.log}"
OUT="${2:-/tmp/filebridge_qr.png}"
HERE="$(cd "$(dirname "$0")" && pwd)"

LINK=$(grep -o 'http://[0-9.]*:[0-9]*/?t=[A-Za-z0-9_-]*' "$LOG" | head -1)
[ -z "$LINK" ] && { echo "no link in $LOG" >&2; exit 1; }

BASE="${LINK%%/?t=*}"
TOKEN="${LINK##*?t=}"
PAYLOAD="filebridge://c?u=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$BASE")&t=$TOKEN"

osascript -l JavaScript "$HERE/qrgen.js" "$PAYLOAD" "$OUT" 700 >/dev/null
echo "$OUT"
