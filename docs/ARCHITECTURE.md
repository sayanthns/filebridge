# Architecture and decisions

Written for whoever picks this up next, human or agent. The HTTP API is the
contract; everything else is replaceable. The dead ends at the end are the part
worth reading first — each one cost real debugging time.

## Pieces

| Piece | Language | Role |
|---|---|---|
| `filebridge.py` | Python 3, stdlib only | HTTP server, Mac control panel, phone-facing web view |
| `launcher.sh` | bash | `Contents/MacOS/FileBridge`: starts the server detached, opens the panel, exits |
| `tools/qrgen.js` | JXA | QR PNG via macOS CoreImage — no dependency to install |
| `android/` | Kotlin | Native phone app: browse, download, upload, scan |

## HTTP API

Two classes of route, and the split is the security model.

### Phone-facing — requires `?t=<key>`

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Browse view (HTML) for any browser on the network |
| `/api/list?path=` | GET | JSON listing: name, pretty name, size, duration, taken flag |
| `/file?path=` | GET | File download. **Honours `Range`**, replies `206` |
| `/get` | GET | Newest APK in `to-phone/` — short install link |
| `/api/upload` | POST | multipart upload into `from-phone/`. Accepts chunked *and* fixed-length |
| `/api/mark` | POST | `{path, done}` — records a file as taken |
| `/api/bye` | POST | Phone announces it is leaving; clears the connected marker |
| `/manifest.webmanifest` | GET | Lets the browse view install to a home screen |
| `/health` | GET | No key needed. Used by the launcher to wait for readiness |

### Mac-only — `127.0.0.1` and no key

These display the key or act on the machine, so they must never answer the LAN.
They deliberately do **not** require the key: you are already at the machine, and
requiring it to view the page that reveals it is circular.

| Route | Method | Purpose |
|---|---|---|
| `/connect` | GET | The control panel |
| `/qr.png` | GET | QR of the `filebridge://` deep link |
| `/api/status` | GET | sharing/paused, link, connected client, folder counts |
| `/api/stop` | POST | **Pause** sharing (does not exit) |
| `/api/start` | POST | Resume sharing |
| `/api/quit` | POST | Exit the process |
| `/api/open` | POST | `{folder}` — reveal `to-phone`/`from-phone` in Finder |

While paused, phone requests get `503` with a readable message; the panel keeps
working. That is why Stop pauses rather than exits — see the dead ends.

## Connection handshake

```
Mac                                        Phone
 │  QR encodes:                              │
 │  filebridge://c?u=<url-enc base>&t=<key>   │
 │──────────────────────────────────────────▶ │  in-app scanner (ZXing)
 │                                            │  or the camera app, which
 │                                            │  opens the app by deep link
 │                                            │
 │◀───────── GET /api/list?t=<key> ───────────│  first call after connecting
 │  writes /tmp/filebridge_clients.txt        │
 │  panel sees a client, hides the QR         │
```

Scanned text and deep-link intents both funnel into `connectFromPayload()`, so
the two routes cannot drift apart. The link is saved, so later launches skip the
scan entirely.

## State on disk

| Path | What |
|---|---|
| `~/FileBridge/to-phone` | Mac → phone |
| `~/FileBridge/from-phone` | phone → Mac |
| `~/.filebridge/key` | access key, persisted so pairing survives restarts |
| `~/.filebridge/state.json` | taken flags + cached ffprobe durations |
| `/tmp/filebridge_clients.txt` | last non-localhost client + timestamp (45 s freshness) |
| `~/.filebridge/gui.log` | server + launcher output. First place to look |

Deleting `state.json` clears bookkeeping only, never media.

## Dead ends — do not repeat these

**Tkinter for the Mac UI.** This machine has only Apple's system **Tk 8.5.9**,
which draws blank windows on modern macOS. The widget tree built fine (6
children, no traceback) and nothing rendered. If you want a native window you
need Tk 8.6 (`brew install python-tk`) or a real toolkit. `swiftc` was tried
first and fails outright here: *redefinition of module 'SwiftBridging'* from a
broken CommandLineTools module map.

**Sources outside the bundle.** With the code in `~/Documents`, a
Finder-launched app dies with `[Errno 1] Operation not permitted` — macOS
protects that folder (TCC). Terminal has that permission, so testing from a
shell hides the bug entirely. The bundle is self-contained for this reason.

**Launcher that stays alive.** `exec`ing the server made the app process *be*
the server. macOS then saw FileBridge as running, clicking the icon only
activated a windowless process, and it needed a Force Quit. The launcher must
exit and leave the server in its own session (`start_new_session`, i.e. setsid).
Merely backgrounding it is not enough — LaunchServices reaps the process group.

**`pgrep -f` as an "already running?" check.** The pattern matched an unrelated
process, so every launch took the already-running branch and exited without
starting anything. Ask the port (`/health`) instead.

**Stop that exits the process.** The panel is served *by* the server, so killing
it left a dead page with no way to start again short of relaunching. Stop pauses.

**Local control routes behind the key gate.** `/api/status` and `/api/stop` were
placed after the auth check, so the panel — a plain page with no key — got `403`
and Stop silently did nothing. Local routes go *before* the gate.

**Chunked uploads rejected.** The server required `Content-Length`; the Android
client used chunked streaming, so every upload failed with a bare `400`. Both
sides now agree: Android sends a fixed length, and the server also decodes
chunked bodies.

**`Response.call_on_close` for "delete after download".** It fires on *aborted*
transfers too, so a cancelled download deleted its own file. Deletion is an
explicit action plus a scheduled sweep.

**`0700` permissions on served files.** nginx-style handoff aside, anything that
another user's process must read cannot be `0700`. Relevant if you add an
X-Accel-style path later.

**Preview for showing the QR.** An AppleScript `display dialog` stays frontmost
and hid it. Anything modal will.

## Testing notes

There is no test suite; verification has been manual and mostly `curl`. What
actually catches regressions:

```bash
# is the pause gate real?
curl -o /dev/null -w '%{http_code}\n' "http://<lan-ip>:8001/api/list?t=$KEY"   # 200
curl -X POST http://127.0.0.1:8001/api/stop
curl -o /dev/null -w '%{http_code}\n' "http://<lan-ip>:8001/api/list?t=$KEY"   # 503
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/connect            # 200

# do local routes stay local?
curl -o /dev/null -w '%{http_code}\n' "http://<lan-ip>:8001/api/status"        # 403

# does Range actually work?
curl -s -r 0-1023 "http://<lan-ip>:8001/file?t=$KEY&path=to-phone/x" | wc -c   # 1024

# is path containment intact?
curl -o /dev/null -w '%{http_code}\n' "http://<lan-ip>:8001/file?t=$KEY&path=../../etc/passwd"
```

Two habits that would have saved most of the debugging above:

1. **Test as the identity that actually performs the step.** Bugs landed in
   three different users — gunicorn's worker, `root`-created directories, and
   `www-data` — while testing happened in Terminal as the developer.
2. **Assert that a patch changed something.** A string-replace that matches
   nothing fails silently; a pause guard "added" this way was simply absent, and
   only a `200` where `503` was expected revealed it.
