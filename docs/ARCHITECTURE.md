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

`filebridge.py` also shells out to `adb` — found by path, since it is not on
`PATH` on a normal Mac even with the SDK installed.

## Two transports

The same API, reached two ways. Nothing above the socket knows which.

| | Wifi | Cable, via adb | Cable, via tethering |
|---|---|---|---|
| Listener | `0.0.0.0:8001` | `127.0.0.1:8002`, flagged `wired` | `0.0.0.0:8001` |
| Phone dials | `http://<lan-ip>:8001` | `http://127.0.0.1:8001` on the phone | `http://192.168.42.x:8001` |
| Carried by | the network | `adb reverse tcp:8001 tcp:8002` | a CDC ECM/NCM link on the cable |
| Needs | same wifi | USB debugging + one "Allow" tap | **nothing** in Developer options |
| Pairs by | QR | deep link fired over adb | QR (`/qr.png?tether=1`) |
| A VPN can break it | **yes** | no — nothing is routed | **yes** — it is plain IP |
| Radio can sleep | yes | no | no |

Prefer adb when the phone allows it: loopback cannot be routed, so it is the
only one of the three a VPN cannot touch. Tethering exists because a phone may
simply refuse to publish an adb interface — but on macOS it only works if the
phone tethers over CDC ECM or NCM. **Android tethers over RNDIS, which macOS
cannot drive at all**, so on the hardware here neither cable path works. Both
dead ends are written up below; the panel reports which one you are hitting
rather than leaving you to guess.

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

Tethering needs no second listener: `192.168.42.x` is a real address on a real
interface, so the existing `0.0.0.0` socket already answers there and the phone
is correctly treated as a phone. Only the *advertised* address had to change,
which is what `tether_ip()` and `/qr.png?tether=1` are for — `lan_ip()` asks the
routing table for one address and would keep printing the wifi one.

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

`/api/usb` reports `armed` alongside any `error`, because the two halves fail
independently: arming the tunnel is the hard part, and `adb shell am start`
opening the app is only a shortcut. When arming worked and the launch did not,
the phone can still be paired by scanning `/qr.png?usb=1` — the loopback link,
which reaches the Mac through the tunnel that is already up.

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

**Assuming USB tethering gives macOS a network interface.** It does not, for
Android. Android's tethering gadget is **RNDIS** — Microsoft's protocol,
control interface class 239 / subclass 4 / protocol 1 — and macOS has never
shipped a driver for it. It ships `AppleUSBECM.kext` and `AppleUSBNCM.kext`,
for CDC ECM and CDC NCM, and nothing for RNDIS.

Measured on the Honor here. Turning tethering on *did* rebuild the USB function
set — `idProduct` went 4221 → 4234, unlike the adb attempt — and the phone
published:

```
RNDIS Communications Control@0   239/4/1
RNDIS Ethernet Data@1             10/0/0
```

Both matched only the generic `IOUSBHostInterface`. Zero network driver nodes
bound, no `enX`, no address, so `tether_ip()` correctly finds nothing. The
phone believes it is tethering and the Mac cannot see it.

This is why `rndis_on_cable()` exists: "a phone is tethering and this Mac
cannot use it" is a completely different report from "nobody turned tethering
on", and without it someone will go hunting a driver that does not exist.
HoRNDIS was the third-party answer; it is an unsigned kext, unmaintained, and
not viable on Apple Silicon. Detection matches the node *name* rather than the
descriptor because the authoritative query costs 350 ms and 5 MB while the
names cost 25 ms — and the names come from the Linux kernel's `f_rndis` gadget,
not from a vendor, so they are safe to trust.

**Assuming "USB debugging is on" means adb can see the phone.** On the Honor
this was built against, MagicOS never added the adb function to the USB
composition. With the toggle on, "Transfer files" selected and the cable
replugged, `ioreg` showed only:

```
MTP@0           255/255/0   vendor MTP
Mass Storage@1  8/6/80      "Linux File-CD Gadget" — HiSuite's autorun disc
```

adb's interface is class 255 / subclass 66 / protocol 1, and `idProduct` was
identical before and after the replug — so the function set was never rebuilt,
and no amount of `adb kill-server` / `adb usb` / `adb reconnect` helps. Read the
interface descriptors before debugging the host: an absent interface and a
refused authorisation look the same from `adb devices` (both print nothing
useful) but have nothing in common. `unauthorized` means the interface is there.

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

**Logging a subprocess's output without reading what is in it.** `am start`
echoes the intent it launched, URI and all — and that URI carries the access
key. Capturing its output for diagnosis therefore writes the key into
`~/.filebridge/gui.log`, the file every troubleshooting note points at first.
Redact before printing, and mind the boundary: `t=` also occurs inside `act=`
and `dat=`, so `t=[^&\s}]+` blanks the whole line and destroys the diagnostic
it was added to capture. `(?<![A-Za-z0-9_])t=` is the pattern that works.

**Setting a UI message and then re-rendering.** The panel's Pair button wrote
the server's error into the hint and then called `poll()`, whose `render()`
overwrote it with the button's own optimistic description. A real
`POST /api/usb 502` — *"Tunnel is up, but the app would not open"* — was
therefore never once seen on screen, while the card claimed the opposite. Any
message that outlives the event that produced it needs to live in state the
renderer reads, not in the DOM the renderer rewrites.

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

# --- tethering. To exercise the detection without a phone, point TETHER_NET at
# --- a subnet this Mac is already on; the code path is otherwise identical.
sed 's|^TETHER_NET = "192.168.42."|TETHER_NET = "10.0.0."|' filebridge.py > /tmp/sim.py
cp -R tools /tmp/tools          # _qr_png resolves tools/ next to the script
python3 /tmp/sim.py ~/FileBridge --port 8881 --token devkey &
curl -s http://127.0.0.1:8881/api/status | python3 -m json.tool | grep -A3 tether
curl -so /tmp/q.png "http://127.0.0.1:8881/qr.png?tether=1"
osascript -l JavaScript /tmp/tools/qrread.js /tmp/q.png    # must be the tether address
```

Decode every QR you generate — that is the only way to know it scans, and the
`?tether=1` path has its own `_deep_link(host)` argument to get wrong.

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
