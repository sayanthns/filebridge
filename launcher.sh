#!/bin/bash
# Short-lived launcher: ensure the server is up, show the panel, then exit.
#
# It must exit. An earlier version exec'd the server, so the app process WAS
# the server — macOS then considered FileBridge already running, and clicking
# the icon only "activated" a windowless process: nothing appeared and it
# needed a Force Quit.
#
# The server is spawned in its own session (setsid via Python) so that this
# script exiting does not let LaunchServices reap it — which is what killed it
# back when it was merely backgrounded.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/.filebridge/gui.log"
mkdir -p "$HOME/.filebridge"
PORT=8001
URL="http://127.0.0.1:$PORT/connect"

[ -s "$HOME/.filebridge/key" ] || \
  /usr/bin/python3 -c 'import secrets;print(secrets.token_urlsafe(9))' > "$HOME/.filebridge/key"
chmod 600 "$HOME/.filebridge/key"
KEY="$(cat "$HOME/.filebridge/key")"
mkdir -p "$HOME/FileBridge/to-phone" "$HOME/FileBridge/from-phone"

# Finder right-click -> Quick Actions -> Copy / Move to Phone. Installed from
# inside the bundle, and only rewritten when the shipped version changes, so
# this costs nothing on a normal launch. Remove them with:
#   python3 scripts/make_quick_actions.py --uninstall
QA_SRC="$RES/QuickActions"
QA_STAMP="$HOME/.filebridge/quick-actions"
if [ -d "$QA_SRC" ]; then
  QA_VERSION="$(cat "$QA_SRC/VERSION" 2>/dev/null)"
  if [ "$(cat "$QA_STAMP" 2>/dev/null)" != "$QA_VERSION" ]; then
    mkdir -p "$HOME/Library/Services"
    for wf in "$QA_SRC"/*.workflow; do
      [ -d "$wf" ] || continue
      name="$(basename "$wf")"
      rm -rf "$HOME/Library/Services/$name"
      cp -R "$wf" "$HOME/Library/Services/$name"
    done
    echo "$QA_VERSION" > "$QA_STAMP"
    # Ask the services registry to notice them without a logout.
    /System/Library/CoreServices/pbs -update >/dev/null 2>&1
  fi
fi

show_panel() {
  if [ -d "/Applications/Google Chrome.app" ]; then
    open -na "Google Chrome" --args --app="$URL" --window-size=560,880
  else
    open "$URL"
  fi
}

# Port answers => already sharing. Just re-show the panel.
if /usr/bin/curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health"; then
  show_panel
  exit 0
fi

echo "--- launch $(date) ---" >> "$LOG"
/usr/bin/python3 - "$RES/filebridge.py" "$HOME/FileBridge" "$PORT" "$KEY" "$LOG" <<'PYEOF'
import subprocess, sys
server, root, port, key, log = sys.argv[1:6]
with open(log, "a") as out:
    # start_new_session=True == setsid: its own session and process group, so
    # it outlives this launcher instead of being reaped with it.
    subprocess.Popen(["/usr/bin/python3", "-u", server, root,
                      "--port", port, "--token", key],
                     stdout=out, stderr=subprocess.STDOUT,
                     start_new_session=True)
PYEOF

for i in $(seq 1 60); do
  /usr/bin/curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$PORT/health" && break
  sleep 0.25
done
show_panel
exit 0
