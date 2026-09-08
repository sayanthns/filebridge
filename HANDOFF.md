# Handoff

For whoever picks this up next. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
alongside this — it holds the HTTP API and the dead ends. This file is state and
honesty: what works, what is merely *believed* to work, and what is left.

Last updated: 2026-09-08.

## Where things stand

| Piece | Version | State |
|---|---|---|
| Server (`filebridge.py`) | 1.13.0 | Working. Browse, download (Range + ETag), upload, pause/resume, wired listener |
| Mac app | 1.13.0 | Working. Installed at `~/Applications/FileBridge.app`, Dock shortcut added |
| Android app | 1.12.0 (code 15) | Working over wifi, both directions, screen off. Cable side compile-verified only |

**Large downloads to the phone (the long-running bug).** Two causes, one after
the other:

1. The server sent no `ETag`, so a resume was impossible even in principle.
   Fixed in server 1.11.0 (`ETag`, `Last-Modified`, `If-Range`, `If-Match`),
   verified by curl including a byte-identical resumed tail.
2. DownloadManager still would not resume. It holds no wifi lock, so the radio
   slept with the screen: three attempts at a 707 MB file died at **15.8 s,
   15.9 s and 16.2 s** — three different byte counts, one clock, matching a 15 s
   display timeout — and it never issued a single ranged request afterwards.
   Android 1.10.0 drops it for `TransferService`, which holds a WifiLock and a
   WakeLock and runs its own `Range:` resume loop. **Uploads had the identical
   hole until 1.11.0** — they ran on the Activity's pool with no lock and no
   progress display, so a big send died on screen-off and said nothing.

Both landed and a large file now completes on the phone with the screen off.
The shape of a healthy transfer in `~/.filebridge/gui.log` is several `TRANSFER`
lines for one file, the later ones carrying `range=bytes=N-`, ending in
`complete`. A single `range=none` line with no follow-up is the old failure.

**The cable works, but has never met a phone.** 1.13.0 adds a second listener
bound to `127.0.0.1:8002` and flagged `wired`; `adb reverse tcp:8001 tcp:8002`
is what carries the phone's `127.0.0.1:8001` to it. Everything on the Mac side
is measured — see below — because a loopback socket can be driven with `curl`
and needs no device. **What is not measured is the two adb calls**: arming the
reverse mapping against a real phone, and `adb shell am start` opening the app.
USB debugging is off on the Honor here, so `adb devices` is empty and neither
has ever run against hardware. Turning it on is the whole remaining test.

The security consequence of the cable is the part to read twice. `_local()` used
to compare `client_address[0]` against `127.0.0.1`, which is right with one
listener and wrong with two, because `adb reverse` delivers the phone's requests
*from* `127.0.0.1`. With the guard removed, the cable socket served `/connect`
with the access key rendered into the page, handed the key over again in
`/api/status`, and killed the server with one `POST /api/quit`. That is measured
too, deliberately, so nobody "simplifies" the guard away later.

**The cable is not faster, and saying otherwise will disappoint someone.** The
phone negotiates USB 2.0 High Speed here — 480 Mbit/s, `Device Speed = 2` from
`ioreg -p IOUSB -l` — against wifi that was already an 802.11ax 80 MHz link at
−55 dBm with a 600 Mbit/s transmit rate. Either the C-to-C cable or the Honor's
port is USB 2.0 only. What the cable actually buys: no wifi needed at all, no
VPN can swallow loopback, and there is no radio to fall asleep.

**A VPN on the phone breaks everything, and looks like the wrong wifi.** A
full-tunnel VPN (Proton, in the case that cost a session) routes `192.168.x.x`
through the exit server, so the app cannot open a socket to the Mac at all and
every symptom points at the network. Before debugging a "could not reach the Mac"
report, check for a VPN badge. The fix is the VPN app's own "Allow LAN
connections". App 1.11.1 detects it and says so.

**`adb` exists on this machine** at `~/Library/Android/sdk/platform-tools/adb`
— it is simply not on `PATH`, which is why earlier sessions concluded there was
no way to reach a phone. With USB debugging on, `adb install -r` and
`adb logcat` are available and would end the guesswork.

Released as [v1.13.1](https://github.com/sayanthns/filebridge/releases), with the
1.11.1 APK attached.

## Verified vs assumed

Be careful with this distinction — nearly every bug in this project's history got
through because something was "tested" in a way that did not exercise the real
path.

**Measured, with output:**
- Range requests: `206`, exact byte counts for `0-1023` and mid-file `5000-5099`
- Path containment: `../../etc/passwd` and its URL-encoded form refused
- Local-only routes: `403` from the LAN, `200` from `127.0.0.1`
- Pause: LAN `503` while the panel still answers `200`, resume restores access
- Uploads: byte-identical round trip, both chunked and fixed-length framing
- `/api/bye`: clears the connected client immediately
- Quit → relaunch: server `0` → `1`, no lingering launcher process
- APK: manifest, permissions, bundled zxing classes, portrait scanner
  (`screenOrientation=0x1`), valid signature, `TransferService` declared
  `foregroundServiceType=dataSync`, `FileProvider` authority
  `com.enfono.filebridge.files`
- ETag and resume: `412` on a stale `If-Match`, full `200` on a stale
  `If-Range`, and a resumed tail whose sha1 matches the source byte for byte
- **A 707 MB file completing on the phone with the screen off** (1.10.0 app,
  1.11.0 server) — the bug that took four attempts to pin down. A 1 GB file
  followed, in 292 s
- **Uploads through the service** (1.11.0 app): progress notification, and a
  large send finishing with the screen off. Confirmed by use, not by curl
- **Tapping a finished download opens it** in a player rather than reopening the
  app. Confirmed by use
- **The wired listener refuses the control surface**: `403` from
  `127.0.0.1:8002` for `/connect`, `/qr.png`, `/api/status`, `/api/quit`,
  `/api/stop`, `/api/open` and `/api/usb`, and the access key appears **zero**
  times in the `/connect` body. `200` for `/api/list?t=<key>`, `403` without the
  key and with a wrong one — so it is a phone transport, not a hole
- **That guard is load-bearing, not decorative.** With the two lines deleted the
  same socket returned `200` for `/connect` **with the key in the page**, `200`
  for `/api/status` **with the key in the JSON**, and `200` for `/api/quit`
  after which the server was gone
- **The cable carries the real API**: `206` with exact byte counts for `0-1023`
  and `5000-5099`, `ETag` + `Last-Modified` + `Content-Range` present, a whole
  file sha1-identical to the source, an upload sha1-identical round trip, and
  `../../etc/passwd` refused in both plain and URL-encoded form
- **Pause covers the cable**, because it runs through the same `_local()`: `503`
  on the wired socket while the panel still answers `200`, and `200` again after
  Start
- **The wired bind refuses to shadow anything**: `--wired-port` equal to
  `--port` is refused, and a port something already answers on is refused — with
  the wifi listener still coming up in both cases. Worth knowing *why*:
  `allow_reuse_address` let a `127.0.0.1:8801` bind succeed under a live `*:8801`
  with no error at all
- **The panel's Cable card in all five states** (no adb / unauthorized / ready /
  connected / wired off), driven through the page's own `renderUsb()` in a real
  browser, plus a screenshot of the rendered card and a clean console
- **APK 1.12.0**: `versionCode 15`, `filebridge` scheme filter intact, valid
  debug signature, and the new code present — in `classes3.dex`, because the app
  is multidex and zxing fills the first one
- **Finder Quick Actions**: both workflows run via `automator -i` against a
  throwaway root — copy leaves the original, move does not, a name collision
  becomes `name-2.ext` rather than an overwrite, and a file already inside the
  served folder is skipped. Then the installed copy was run against the real
  folder

**Not verified — treat as unknown:**
- **`adb reverse` and `adb shell am start` against a real device.** The two
  calls the cable rests on. USB debugging is off on the phone here, so
  `adb devices` is empty and the whole adb half is reasoned from the code plus
  the fact that `adb reverse` with no device fails in 13 ms rather than hanging.
  The command strings are right — the device-side shell quoting was checked by
  round-tripping a token containing a `'` through `sh -c` — but nothing has
  spoken to a phone
- **The transport fallback on the phone** (`url_wifi` / `url_usb`).
  Compile-verified, and the new strings are confirmed inside the APK, but no
  cable has been unplugged mid-session to watch it swap
- **Phone-side visuals.** No Android device has ever been attached to this Mac
  for a build; layouts, the scanner UI and the back-button behaviour are
  compile-verified only. Downloading is the exception — that one is confirmed by
  use. Note `adb` **does** exist here (see above), so this is fixable.
- **The panel reviving itself after a Quit** (1.9.0). The server restart is
  measured; the page waking back up is reasoned from the code, because driving a
  live browser session across a server restart was not possible here.
- **The VPN warning** (1.11.1). `TRANSPORT_VPN` is the documented way to ask and
  the string is compiled into the APK, but neither the Settings row nor the
  reworded connect error has been seen on a screen with a VPN up.
- **Any second machine.** Only ever run on one Mac, one phone, one network.

## Open items

1. **Turn on USB debugging and run the cable once.** Everything else about it
   is measured; this is the one gap, and it is a toggle plus a tap. Developer
   options → USB debugging, then "Allow" when the Mac's key prompt appears, then
   "Pair over cable" in the panel. Expect the app to open already connected and
   the panel to show `usb`. `adb reverse --list` should print
   `tcp:8001 tcp:8002`.
2. **Uploads cannot resume.** The server has one multipart `POST /api/upload`
   with no offset, so an interrupted send starts over. An endpoint taking a byte
   offset and appending to a temp file would close the last asymmetry between the
   two directions — downloads already resume.
3. **No tests.** Everything above was `curl` by hand. The commands are listed at
   the end of ARCHITECTURE.md and would convert directly into a shell test
   script — that is the highest-value next task.
4. **Debug-signed APK.** Installs and upgrades fine, but is not distributable.
   Needs a release keystore, which is a credential the owner must create.
5. **No iOS app.** The cable does not help here either — `adb` is Android
   only. iPhones can use the browse view at `/?t=<key>` instead.
6. **The Mac app has no Dock tile of its own** while running. It is a launcher
   that exits by design (see ARCHITECTURE.md — keeping it alive is what caused
   the Force Quit bug). The panel window belongs to Chrome. A real tile needs a
   GUI process, and Tk cannot provide one on this machine.
7. **Chrome reuses its `--app` window** for the same URL, and remembers its last
   size — including full screen. If the panel comes up full screen, that is
   Chrome's memory, not the launcher, which asks for 560×880.
8. **Plain HTTP.** Fine on a home LAN, wrong for shared wifi. TLS would mean a
   self-signed cert and a trust prompt on the phone — or use the cable on
   shared wifi, which never puts the key on the network at all.
9. **The adb watchdog spawns `adb devices` every 5 s** for as long as the server
   runs, and its first tick starts the adb server if it is not already up. Both
   are deliberate — a reverse mapping dies on every unplug, so re-arming has to
   be a loop — but a watcher that only wakes on device attach would be cheaper.
   `--no-wired` turns the whole thing off.

## This machine's quirks

These cost hours. They are properties of the environment, not the code.

| Thing | Reality |
|---|---|
| `swiftc` | Broken: *redefinition of module 'SwiftBridging'* from a bad CommandLineTools module map. No native Swift UI possible here |
| Tkinter | Only Apple's system **Tk 8.5.9**, which draws blank windows on modern macOS. Widgets build, nothing renders |
| `~/Documents` | TCC-protected. A Finder-launched app reading code there gets `Operation not permitted`. Terminal *does* have access, which hides the bug |
| Gradle | Not on `PATH`. `scripts/build-android.sh` finds a cached distribution under `~/.gradle/wrapper/dists`. **It must be a 8.x one** — a cached 9.7.1 appeared and newest-wins picked it, and AGP 8.2.1 dies on Gradle 9 with *Could not isolate value … BuildFlowService$Parameters* (it wants `org/gradle/api/internal/HasConvention`, removed in 9). The script now asks for 8.2.1 by name |
| JDK | `JAVA_HOME=/opt/homebrew/opt/openjdk@17` — AGP 8.2.1 needs 17, not 21 |
| `screencapture` | Needs Screen Recording permission; unavailable, so UI could not be visually checked. Headless Chrome was used for the panel screenshots |
| `apksigner` | Needs `JAVA_HOME=/opt/homebrew/opt/openjdk@17` in the environment or it reports *Unable to locate a Java Runtime*. `build-android.sh` sets it; a bare shell does not |
| QR generation | macOS CoreImage via JXA (`tools/qrgen.js`). No pip install. `tools/qrread.js` decodes, and every generated QR should be decoded to confirm it scans |

## Machine-local state (not in the repo)

| Path | What | Notes |
|---|---|---|
| `~/FileBridge/to-phone` | Mac → phone | |
| `~/FileBridge/from-phone` | phone → Mac | |
| `~/.filebridge/root` | which folder is being served | Written at startup; the Quick Actions read it |
| `~/.filebridge/quick-actions` | version of the installed Quick Actions | Launcher reinstalls when it differs from the bundle's |
| `~/Library/Services/*.workflow` | Copy / Move to Phone | Installed by the launcher. `make_quick_actions.py --uninstall` removes them |
| `~/.filebridge/key` | access key | **Secret.** Persisted so pairing survives restarts. Delete to unpair every device |
| `~/.filebridge/state.json` | taken flags, cached durations | Safe to delete |
| `~/.filebridge/gui.log` | server + launcher output | First place to look when the app "does nothing" |
| `/tmp/filebridge_clients.txt` | last phone seen | 45 s freshness window. Reads back as two whitespace-separated fields, which is why a wired phone is recorded as `usb` and not a label with a space in it |

## Getting going

```bash
git clone https://github.com/sayanthns/filebridge.git && cd filebridge
python3 filebridge.py ~/FileBridge --port 8001 --token devkey
```

Panel: `http://127.0.0.1:8001/connect` · Browse view:
`http://127.0.0.1:8001/?t=devkey` · Wired listener: `127.0.0.1:8002`, which
should answer `403` to `/connect` and `200` to `/api/list?t=devkey`.

Build the app bundle with `./scripts/build-mac-app.sh`, the APK with
`./scripts/build-android.sh`.

**When taking screenshots for docs, use a throwaway instance** on a spare port
with a fake token and fake filenames. The first set committed here captured a
live access key inside the QR and had to be redone.

## Conventions worth keeping

- **Three independent versions.** Bump only what changed. Android needs
  `versionCode` bumped too, or the APK silently will not install over the old one.
- **`VERSIONS.md` says why**, not just what. The reasons are the useful part.
- **Local control routes go before the auth gate**, phone routes after. Putting
  `/api/stop` after it made Stop Sharing silently `403` and do nothing.
- **Assert that a patch changed something.** A string replace matching nothing
  fails silently — a pause guard "added" that way was simply absent, and only a
  `200` where `503` was expected revealed it.
- **Test as the identity that performs the step.** Bugs landed in three
  different users (a web worker, `root`-created directories, `www-data`) while
  testing happened as the developer in Terminal.
