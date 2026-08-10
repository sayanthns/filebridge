# FileBridge — versions

Three pieces, versioned independently. They talk over a small HTTP API, so they
do not have to move together — only bump what you actually changed.

| Piece | Version | Where |
|---|---|---|
| Server | **1.9.0** | `filebridge.py` → `APP_VERSION` |
| Mac app | **1.9.0** | `FileBridge.app` → `CFBundleShortVersionString` |
| Android app | **1.5.0** (code 6) | `android/app/build.gradle` → `versionName` / `versionCode` |

Android needs both: `versionName` is what you read, `versionCode` is what the
installer compares. **A build with an unchanged `versionCode` will not install
over the previous one**, so bump it on every APK you hand to the phone.

---

## Server

### 1.9.0
- **The panel recovers by itself after a Quit.** Chrome reuses an existing
  `--app` window for the same URL, so reopening File Bridge fronts the *same*
  page rather than making a new one. Since that page had stopped polling, it sat
  on "Sharing ended" forever and never showed the new QR. It now keeps polling
  while dead and revives when the server answers again.

### 1.8.0
- **The "server gone" state hides its buttons.** Pressing Quit left Copy link /
  Stop sharing / "Quitting…" on screen, so a finished quit read as a frozen
  panel with controls for a process that no longer existed. `render()` now owns
  that state like every other.

### 1.7.0
- **Stop Sharing pauses instead of exiting.** Killing the process also killed the
  panel it was serving, so there was no way to start again without relaunching.
  Paused now means the LAN gets `503` while the Mac panel stays live; `/api/start`
  resumes and `/api/quit` exits.
- The panel is driven by one `render()`, so the **QR comes back** when the phone
  disconnects. Previously it was replaced once and never restored, which is why
  the Mac kept claiming a phone was connected.

### 1.6.0
- **Uploads accept chunked bodies.** The Android client streamed chunked, so
  there was no `Content-Length`, and the server rejected it as empty — every
  "Send" failed with a bare `400`.
- `/api/bye` lets the phone announce it is leaving; connected-client freshness
  cut from 90 s to 45 s.

### 1.5.0
- **Stop Sharing actually stops.** `/api/stop` sat behind the token check, so
  the panel (a plain page with no token) got a 403 and the server kept running
  with the phone still connected. Local control routes now sit ahead of the
  auth gate — localhost is the gate that matters for them. Stop also clears the
  connected-client marker and exits via `os._exit`, since softer signals left
  the process alive with its sockets open.
- Control panel: status, Copy link, Stop, folder tiles with live counts, and a
  QR that disappears once a phone connects.
- `/get` serves the newest APK, so the install link has nothing to mistype.

### 1.1.0
- `/connect` page and `/qr.png`, both **localhost-only** — they display the key,
  so serving them to the wifi would defeat the key.
- `APP_VERSION` shown in the startup banner.

### 1.0.0
- Browse, download, upload. Range requests (resumable downloads), ffprobe
  durations cached, per-file "taken" tracking, token auth, PWA manifest.

---

## Mac app

### 1.5.0
- **Reopens after you close the window.** The launcher used to `exec` the
  server, so the app process *was* the server: macOS saw FileBridge as running,
  clicking the icon merely activated a windowless process, and it took a Force
  Quit. The launcher is now short-lived — it starts the server in its own
  session (`start_new_session`, i.e. setsid) and exits, so every click either
  starts sharing or re-shows the panel.
- UI is HTML in a chromeless Chrome window. **Tkinter was abandoned**: this
  machine only has Apple's system Tk 8.5.9, which draws blank windows on modern
  macOS — the widgets were built fine, Tk just never rendered them.
- App icon applied; installed at `~/Applications` (not `~/Documents`, which
  macOS protects — a Finder-launched app got "Operation not permitted" there).

### 1.1.0
- Shows the QR by opening the `/connect` page in the **browser**.
  1.0.0 opened it in Preview, where it was invisible: an AppleScript
  `display dialog` stays frontmost and covered it.
- Real version metadata and a bundle id (`com.enfono.filebridge.mac`); ad-hoc
  signed so Gatekeeper complains once rather than every launch.

### 1.0.0
- Start/stop only, as intended. **Fixed on first run:** the script assigned to a
  variable named `running`, which is a reserved property of an AppleScript
  applet — it failed with `-10006` before doing anything.

---

## Android app

### 1.5.0 (versionCode 6)
- **Scanner opens in portrait.** ZXing's bundled `CaptureActivity` is declared
  landscape; overridden with `tools:replace`.
- **Back button behaves normally**: up a folder, then out to the connect screen,
  then out of the app — via `OnBackPressedDispatcher`.

### 1.4.0 (versionCode 5)
- "Send to Mac" was clipping its own label; now "Send", single line.
- Breadcrumb said "To Phone" at the root, which holds *both* folders; now
  "File Bridge".
- A paused Mac reports "Paused on the Mac" and keeps you on the list instead of
  reading as a network failure.

### 1.3.0 (versionCode 4)
- **Exit button** to disconnect: tells the Mac via `/api/bye`, forgets the link.
- Uploads declare a fixed length instead of streaming chunked, which is what the
  server's `400` was about. Upload errors now show the server's message.

### 1.1.0 (versionCode 2)
- **In-app QR scanner** (`zxing-android-embedded`) — chosen over ML Kit because
  it needs no Play Services and brings its own camera Activity.
- Scanned text and `filebridge://` deep links share one code path
  (`connectFromPayload`), so both routes behave identically.
- `CAMERA` permission; camera declared not-required so the app still installs on
  a device without one.

### 1.0.0 (versionCode 1)
- Native list, DownloadManager downloads (background, resumable, notification),
  chunked uploads, folder browsing, remembered connection,
  `filebridge://` deep link.

---

## Releasing

```bash
cd android && JAVA_HOME=/opt/homebrew/opt/openjdk@17 \
  ~/.gradle/wrapper/dists/gradle-8.2.1-all/*/gradle-8.2.1/bin/gradle assembleDebug
```

Then copy the APK into `~/FileBridge/to-phone/` and install it from the phone —
the running server is how you deliver its own updates.

Debug-signed. Installs fine and upgrades in place because the key is stable, but
Play Store distribution would need a release keystore.
