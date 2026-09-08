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

## Two transports

The same API, reached two ways. Nothing above the socket knows which.

| | Wifi | Cable |
|---|---|---|
| Listener | `0.0.0.0:8001` | `127.0.0.1:8002`, flagged `wired` |
| Phone dials | `http://<lan-ip>:8001` | `http://127.0.0.1:8001` on the phone |
| Carried by | the network | `adb reverse tcp:8001 tcp:8002` |
| Needs | same wifi | USB debugging + one "Allow" tap |
| A VPN can break it | **yes** | no — nothing is routed |
| Radio can sleep | yes | no |

```
Mac                                                        Phone
 filebridge.py                                        File Bridge app
   ├── 0.0.0.0:8001   ──────── wifi ───────────────────▶  http://<lan-ip>:8001
   └── 127.0.0.1:8002 ◀── adb reverse ──  127.0.0.1:8001 ◀── same app, same key
```

**The cable is not faster.** Measured on this machine: the phone negotiates USB
2.0 High Speed (480 Mbit/s, `Device Speed = 2` in `ioreg -p IOUSB`) while wifi
was an 802.11ax 80 MHz link at −55 dBm with a 600 Mbit/s transmit rate. Wired
buys independence from the network, not throughput — and the two worst bugs in
this project's history were both network-layer (a VPN eating `192.168.x.x`, and
the radio sleeping with the screen).

**Two listeners, not one, and that is the security model.** `adb reverse`
delivers the phone's requests from `127.0.0.1`, so an address check cannot tell
the phone from this Mac. Which socket it arrived on can. `Handler._local()`
returns `False` for the wired one unconditionally, which is what keeps
`/connect` and `/qr.png` — both of which print the access key — away from the
phone. See the dead ends.

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
| `/api/usb` | POST | Arm `adb reverse` and open the app on the phone, connected |

`/api/status` carries a `usb` object — `on`, `adb`, `port`, `link`, `devices`,
`waiting`, `armed` — which the panel renders as the Cable card. It is read
straight out of the module-level `USB` dict and **never shells out to adb**: the
panel polls this every 2.5 s. `/api/usb` does shell out, because it is a button.

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

Over the cable there is no QR at all — the Mac fires the *same* deep link at the
phone itself:

```
Mac                                                    Phone
 │  adb reverse tcp:8001 tcp:8002                        │
 │──────────────────────────────────────────────────────▶ │  loopback now
 │                                                        │  reaches the Mac
 │  adb shell am start -a VIEW -d 'filebridge://c?u=…'    │
 │──────────────────────────────────────────────────────▶ │  app opens, already
 │                                                        │  connected
 │◀───── GET /api/list?t=<key> on 127.0.0.1:8002 ─────────│  panel shows "usb"
```

The app keeps one saved link per transport (`url_wifi`, `url_usb`) and falls
back to the other when the live one fails, so unplugging the cable does not
strand it on a `127.0.0.1` that nothing answers.

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

**An address check for "is this the Mac?", once a cable existed.** `_local()`
compared `client_address[0]` against `127.0.0.1`. That is correct with one
listener and catastrophic with two: `adb reverse` hands the phone's requests to
us *from* `127.0.0.1`. Measured with the guard removed — the phone side got
`/connect` with the access key rendered in it, read the key again out of
`/api/status`, and stopped the server with one `POST /api/quit`. Trust has to
follow the socket. Bind the wired listener separately, flag it, and let
`_local()` answer `False` for it.

**Trusting `bind()` to fail on a taken port.** `allow_reuse_address` is set, and
on macOS that lets a `127.0.0.1:8801` bind succeed *underneath* a live `*:8801`
— confirmed here, no error raised. The narrower socket then takes every loopback
connection, so the panel would have started getting `403` from the wired
socket's own security gate: the feature silently eating the UI that configures
it. `port_busy()` connects first and asks.

**`adb shell am start -d <url>` unquoted.** adb joins the argv and hands it to a
shell **on the device**, where the `&` before the key is "run in background".
The app launches with the token cut off and refuses its own pairing link. Single
quote the URL.

**Assuming new Kotlin lands in `classes.dex`.** The app is multidex — bundled
zxing fills the first dex, so app code sits in `classes3.dex`. Grepping only
`classes.dex` for a new string returns zero and looks exactly like a build that
did not pick up the change.

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

# --- the cable. 8002 is the wired listener; run these against it directly,
# --- no phone needed, because it is just a loopback socket.

# does the wired socket refuse the control surface? every one of these is 403,
# and the key must appear zero times in that first body
for r in /connect /qr.png /api/status; do
  curl -o /dev/null -w "$r %{http_code}\n" "http://127.0.0.1:8002$r"
done
curl -s http://127.0.0.1:8002/connect | grep -c "$KEY"                        # 0
for r in /api/quit /api/stop /api/open /api/usb; do
  curl -X POST -o /dev/null -w "$r %{http_code}\n" "http://127.0.0.1:8002$r"
done

# is it still a phone transport? key required, key accepted
curl -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8002/api/list"           # 403
curl -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8002/api/list?t=$KEY"    # 200

# and the wifi socket still trusts loopback
curl -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/connect             # 200
```

To prove the guard is load-bearing rather than vacuous, delete these two lines
from `_local()` and re-run the block above — the panel, the key and `/api/quit`
all come back:

```python
        if self.server.wired:
            return False
```

Two habits that would have saved most of the debugging above:

1. **Test as the identity that actually performs the step.** Bugs landed in
   three different users — gunicorn's worker, `root`-created directories, and
   `www-data` — while testing happened in Terminal as the developer.
2. **Assert that a patch changed something.** A string-replace that matches
   nothing fails silently; a pause guard "added" this way was simply absent, and
   only a `200` where `503` was expected revealed it.
