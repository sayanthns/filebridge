# File Bridge

Move files between a Mac and an Android phone over your own wifi — or over the
USB cable, when there is no wifi worth trusting. No cloud, no account. A small
Python server on the Mac, a native app on the phone, and a QR code to connect
them.

<p align="center">
  <img src="docs/screenshots/mac-panel-sharing.png" width="330" alt="Mac panel showing a QR code to scan">
  <img src="docs/screenshots/mac-panel-connected.png" width="330" alt="Mac panel after the phone connects">
</p>

Left: waiting to be scanned. Right: once the phone connects, the QR disappears
because it has done its job.

---

## What it does

- **Mac → phone.** Drop files in `~/FileBridge/to-phone`, tap them in the app.
  Or right-click any file in Finder → **Quick Actions → Copy to Phone**
  (there is a **Move to Phone** too, when you do not want a second copy).
- **Phone → Mac.** Pick files in the app, they land in `~/FileBridge/from-phone`.
- **Connect by QR.** Scan in-app, or with the phone's camera (deep link).
- **Or over the cable**, two ways. With **USB debugging** on, press *Pair over
  cable* in the panel: `adb reverse` carries the phone's own loopback to the
  Mac and the app opens already connected — no QR, no typing, and because
  loopback cannot be routed, no VPN can break it. Failing that, turn on **USB
  tethering**, which needs nothing from Developer options; the panel spots the
  `192.168.42.x` link and offers a QR for it. Neither is faster than a good
  5 GHz link — wired is for not depending on the network.
- **Resumable downloads.** Range requests, so a dropped wifi link continues
  instead of restarting a 900 MB file.
- **Remembers what you took**, per file, so a long list stays navigable.
- **Nothing leaves your network.** The phone talks straight to the Mac.

## Requirements

| | |
|---|---|
| Mac | macOS 11+, Python 3.9+ (the system one is fine). `ffprobe` optional, for durations |
| Phone | Android 7.0+ (API 24) |
| Both | The same wifi network — **or** a USB cable, with either USB debugging + `adb` on the Mac, or just USB tethering on the phone |

Nothing to `pip install`. The server is standard library only.

## Install

**Mac app**

```bash
git clone https://github.com/sayanthns/filebridge.git
cd filebridge
./scripts/build-mac-app.sh
```

That assembles `FileBridge.app` into `~/Applications`. Open it, press
**Start sharing**. First launch may need right-click → **Open**, since the
bundle is unsigned.

**Phone app**

Grab `FileBridge-<version>.apk` from
[Releases](https://github.com/sayanthns/filebridge/releases) and install it, or
build from source:

```bash
./scripts/build-android.sh
```

The running server also serves its own APK, which is the easiest way to get it
onto a phone: open `http://<mac-ip>:8001/get?t=<key>` in the phone's browser.
The Mac panel shows that link.

## Using it

1. Open **File Bridge** on the Mac → **Start sharing**. A QR appears.
2. Open **File Bridge** on the phone → **Scan QR from Mac**. It connects itself.
3. Tap a file to download it. **Send** to upload. **Exit** to disconnect.
4. **Stop sharing** on the Mac pauses access; **Start sharing** resumes it.

## Screenshots

| Paused | Phone-facing web view |
|---|---|
| <img src="docs/screenshots/mac-panel-paused.png" width="280"> | <img src="docs/screenshots/web-browse.png" width="220"> |

The web view is a fallback: any browser on the network can use the same server
without installing the app.

> Phone-app screenshots are not in the repo yet — they need a real device, and
> the ones taken during development contained a live access key. Drop them into
> `docs/screenshots/` if you have a device to hand.

## How it fits together

```
   Mac                                            Phone
┌──────────────────────────────┐        ┌───────────────────────────┐
│ FileBridge.app               │        │ File Bridge (Kotlin)      │
│   launcher.sh                │        │   MainActivity            │
│     └─ spawns, then exits ───┼──┐     │     ├─ ZXing scanner      │
│                              │  │     │     ├─ TransferService    │
│ filebridge.py  (HTTP :8001)  │◀─┘     │     └─ multipart upload   │
│   ├─ /connect   panel  local │◀───────┤ /api/status  (localhost)  │
│   ├─ /api/list  browse       │        │ /api/list    (token)      │
│   ├─ /file      Range reads  │◀───────┤ /file        (token)      │
│   └─ /api/upload             │◀───────┤ /api/upload  (token)      │
│                              │        │                           │
│ ~/FileBridge/to-phone   ─────┼───────▶│  Downloads/FileBridge/    │
│ ~/FileBridge/from-phone ◀────┼────────┤  (picked files)           │
└──────────────────────────────┘        └───────────────────────────┘
```

The launcher exits immediately and leaves the server in its own session. That
matters: while the launcher *was* the server, macOS considered the app running,
clicking its icon only activated a windowless process, and quitting it took a
Force Quit.

The Mac UI is HTML served by the server itself, shown in a chromeless Chrome
window. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for why it is not a
native window, and for the rest of the design decisions.

## Security model

Worth understanding before you use it on a network you do not control.

- **A key in the URL** (`?t=…`) gates every phone-facing route. Without it,
  every device on the wifi could read the shared folder.
- **Local-only control.** `/connect`, `/qr.png`, `/api/status`, `/api/stop`,
  `/api/start`, `/api/quit`, `/api/open` and `/api/usb` answer **only** to this
  Mac. They display the key or act on the machine, so a phone must never reach
  them.
- **"This Mac" means the socket, not the address.** Over the cable the phone
  arrives from `127.0.0.1`, so the wired listener is a *separate* socket that is
  never treated as local — it answers `403` to every route above. Getting this
  wrong would hand the phone the panel, and the panel prints the key.
- **USB debugging is a real permission.** The adb cable path needs it on, and it
  lets any authorised computer do far more than move files. Turn it off when you
  are done if you do not otherwise use it. USB tethering needs no such grant,
  but it is plain IP — a VPN on the phone can swallow it exactly as it swallows
  wifi, which loopback cannot be.
- **Path containment.** Served paths are confined to the shared folder. `..` is
  rejected; symlinks you place inside it *are* followed, deliberately, so you
  can link a media folder in.
- **Plain HTTP.** Traffic is unencrypted on your LAN. Fine at home; do not use
  it on café or conference wifi — use the cable there instead, which never puts
  the key on the network at all.
- **The key persists** in `~/.filebridge/key` so the phone stays paired across
  restarts. Delete that file to invalidate every paired device.
- **`~/.filebridge/gui.log` is kept at mode 600** and request lines have the key
  redacted, because the startup banner prints the full link. A log written by a
  version before 1.18.0 has the key in it in the clear — it is re-chmodded on
  the next launch, but delete it if you have ever shared it.

## Development

```
filebridge.py                  server + Mac control panel (stdlib only)
launcher.sh                    what Contents/MacOS/FileBridge runs
tools/qrgen.js                 QR via macOS CoreImage (JXA, no dependency)
tools/qrread.js                decodes a QR — used to verify generated codes
tools/make_qr.sh               reads the link from the log, renders the QR
android/                       Kotlin app, Gradle, no Android Studio needed
scripts/build-mac-app.sh       assembles FileBridge.app
scripts/build-android.sh       builds the debug APK
scripts/make_quick_actions.py  builds the Finder Quick Actions (--uninstall removes them)
VERSIONS.md                    per-component changelog and why each fix exists
docs/ARCHITECTURE.md           design decisions, HTTP API, dead ends
HANDOFF.md                     current state, what is verified vs assumed, open items
```

Run the server directly while working on it:

```bash
python3 filebridge.py ~/FileBridge --port 8001 --token devkey
```

Then `http://127.0.0.1:8001/connect` for the panel, or
`http://127.0.0.1:8001/?t=devkey` for the browse view.

The wired listener comes up alongside it on `127.0.0.1:8002` and can be driven
with `curl` without any phone attached — it should answer `403` to `/connect`
and `200` to `/api/list?t=devkey`. `--no-wired` skips it, and never starts adb.

The tethered path can be exercised without a phone: `--tether-net 10.0.0.`
points the detection at a subnet the Mac is already on and everything downstream
runs for real. Full recipe at the end of
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Three independent versions** — server, Mac app, Android app — because they
talk over a stable HTTP API and rarely need to move together. Bump only what
changed, and always bump Android's `versionCode` or the APK will not install
over the previous one. See [VERSIONS.md](VERSIONS.md).

Picking this up cold? Start with **[HANDOFF.md](HANDOFF.md)** — it separates what
is measured from what is only believed to work, and lists this machine's quirks
(a broken `swiftc`, a Tk that renders nothing, a TCC-protected `~/Documents`).

## Known limits

- **Android only.** No iOS app; iPhones can use the web view instead.
- **Same network required**, unless you use the cable. No relay, no internet
  fallback either way.
- **The cable is not a speed upgrade.** On the hardware here it negotiates USB
  2.0 High Speed (480 Mbit/s) against wifi already running at 600 Mbit/s — and
  wifi moved a real 6.3 MB file at 17.34 MB/s. Wired is for reliability, not
  throughput.
- **Some phones will not expose adb over USB at all.** The Honor tested here
  publishes MTP and a HiSuite CD-ROM and no adb interface, whatever Developer
  options says — hence the tethering fallback. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how to read the descriptors.
- **The APK is debug-signed.** It installs and upgrades fine, but Play Store
  distribution would need a release keystore.
- **No Dock tile of its own.** The Mac app is a launcher that exits, so the
  panel window belongs to Chrome. Drag the app to the Dock for a shortcut.
- **Durations need `ffprobe`.** Without it, files simply show no duration.

## License

MIT — see [LICENSE](LICENSE).
