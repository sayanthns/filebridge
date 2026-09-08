# FileBridge — versions

Three pieces, versioned independently. They talk over a small HTTP API, so they
do not have to move together — only bump what you actually changed.

| Piece | Version | Where |
|---|---|---|
| Server | **1.16.0** | `filebridge.py` → `APP_VERSION` |
| Mac app | **1.16.0** | `FileBridge.app` → `CFBundleShortVersionString` |
| Android app | **1.13.0** (code 16) | `android/app/build.gradle` → `versionName` / `versionCode` |

Android needs both: `versionName` is what you read, `versionCode` is what the
installer compares. **A build with an unchanged `versionCode` will not install
over the previous one**, so bump it on every APK you hand to the phone.

---

## Server

### 1.16.0
- **The panel stops overwriting the reason pairing failed.** The button set the
  hint and then called `poll()`, so `render()` immediately replaced the real
  failure with the button's own optimistic description. This was not
  theoretical: a press logged `POST /api/usb 502` while the card went on
  reading *"Opens the app on the phone already connected"* — and the 502 was
  the message that mattered. `pairError` is now sticky, shown in place of the
  computed hint, and cleared only when a phone connects or the cable empties.
- **A failed `am start` is no longer the end of it.** `/api/usb` now reports
  `armed` separately from the pairing, because arming the tunnel is the hard
  part and it succeeded — only the shortcut failed. Once `adb reverse` is up
  the phone can reach us on its own `127.0.0.1`, so a scan gets there just as
  well as a deep link: `/qr.png?usb=1` encodes the loopback link, the panel
  flips to it by itself on that failure, and "Show cable QR" now appears for an
  armed tunnel as well as for tethering.
- The QR button's label moved into `renderUsb()`. It was set only in the click
  handler, so the pairing handler flipping `wantCableQr` left it stale.
- Observed while fixing this: **adb did briefly see the phone and the reverse
  tunnel did come up.** 502 is only reachable past the no-devices check, and the
  green dot needs a non-empty `armed`. So the adb path is closer to working on
  this hardware than the earlier sessions concluded — what failed was
  `adb shell am start`, not the tunnel.

### 1.15.0
- **Says so when the phone is tethering in a form macOS cannot use.** Turning
  USB tethering on *did* work — the Honor rebuilt its USB function set
  (`idProduct` 4221 → 4234, unlike the adb attempt) and published
  `RNDIS Communications Control` (239/4/1) and `RNDIS Ethernet Data` (10/0/0).
  But Android tethers over **RNDIS**, and macOS has never shipped a driver for
  it: it has `AppleUSBECM.kext` and `AppleUSBNCM.kext` and nothing else. Both
  interfaces matched only the generic `IOUSBHostInterface`, no network node
  bound, no `enX` appeared, and `tether_ip()` correctly found nothing.
- Without this, the panel would have said "No phone on the cable" — which is
  true and useless, and would send someone hunting a driver that does not
  exist. `rndis_on_cable()` separates "a phone is tethering and this Mac cannot
  use it" from "nobody turned tethering on", and the Cable card now names RNDIS
  and points at USB debugging or wifi instead.
- Detected by node name, not descriptor. The authoritative query
  (`ioreg -l -c IOUSBHostInterface`) costs **350 ms and 5 MB**; the names cost
  **25 ms**. Safe, because those strings come from the Linux kernel's `f_rndis`
  gadget rather than from a vendor. It runs in the watch thread, never in a
  request — `/api/status` is polled every 2.5 s.
- The watch thread now starts whether or not adb is present, since a tethering
  phone is worth watching for either way.

### 1.14.0
- **USB tethering as a second wired path**, because the first one did not
  survive contact with the phone. MagicOS never published an adb interface: with
  USB debugging on, "Transfer files" selected and the cable replugged, the Honor
  offered only `MTP` (255/255/0) and a mass-storage CD-ROM (8/6/80, a "Linux
  File-CD Gadget" holding Honor's HiSuite installer). adb's interface is
  255/66/1 and `idProduct` never changed across the replug, so the USB function
  set was never rebuilt. `adb devices` stayed empty for twelve minutes of
  polling. Tethering needs nothing from Developer options at all.
- `tether_ip()` recognises **192.168.42.x**, the fixed subnet Android's USB
  tethering always builds (phone at `.129`). That is a documented constant, so
  matching on it is honest; matching on interface names is not, because they
  differ per Mac. `interface_ips()` reads `ifconfig` rather than reaching for
  ctypes, since this file is stdlib-only on purpose.
- **The QR can encode the tethered address**: `/qr.png?tether=1`. Needed because
  the adb path pairs by firing a deep link at the phone, and with no adb there
  is nothing to fire it with — so tethered pairing is a scan again. With no
  tether up, `?tether=1` falls back to the wifi address rather than emitting a
  broken link.
- **It is the weaker of the two wired paths and the panel says so.** Tethering
  is plain IP, so a full-tunnel VPN on the phone can still swallow it, exactly
  as it swallows wifi. `adb reverse` is loopback and cannot be routed at all.
  Prefer the cable-with-adb when the phone will allow it.
- `/api/status` gains a `tether` block. It costs one `ifconfig` per poll, which
  is why it is a small block and not a rescan of anything else.

### 1.13.0
- **A wired transport, over the USB cable.** The phone dials its own
  `127.0.0.1:8001`, which `adb reverse` maps to a second listener of ours bound
  to loopback on 8002. No wifi is involved at any point, which removes the two
  failure classes that have cost the most time here: a full-tunnel VPN cannot
  swallow loopback, and there is no radio to fall asleep. It is **not faster** —
  the cable on this Mac negotiates USB 2.0 High Speed (480 Mbit/s) against an
  802.11ax link already running at 600 Mbit/s. Reliability is the whole point.
- **`_local()` now follows the socket, not the address.** This is the part to
  understand before touching any of it. `adb reverse` delivers the phone's
  requests from `127.0.0.1`, so the old address check would have handed the
  phone `/connect` and `/qr.png` — **both of which print the access key** — plus
  `/api/quit` and `/api/stop`. Measured, with the guard removed: the cable got
  the panel with the key in it, read the key again out of `/api/status`, and
  killed the server with one POST. The wired listener answers `403` to all of
  them and the key appears zero times.
- **"Pair over cable" needs no QR and no typing.** `POST /api/usb` (localhost
  only) arms the reverse mapping and then fires the *same* deep link the QR
  encodes straight at the phone with `adb shell am start`, so the app comes up
  already connected. The URL is single-quoted on the way through: adb hands the
  command to a shell **on the device**, where the unquoted `&` before the key
  would have been read as "run in background" and truncated the token off.
- **The wired bind is probed, not attempted.** `allow_reuse_address` means a
  `127.0.0.1:8801` bind succeeds *underneath* a live `*:8801` — measured here —
  and the narrower socket then quietly takes every loopback connection,
  including the panel's, which would start getting 403 from the wired socket's
  own gate. `port_busy()` asks first, and `--wired-port` equal to `--port` is
  refused outright.
- Pause covers the cable too, because it runs through the same `_local()`. A
  wired phone shows in the panel as `usb` rather than an address that says
  nothing. `--no-wired` skips the whole thing, including ever starting adb.

### 1.12.0
- **Records the served folder** in `~/.filebridge/root`. The folder is a
  command-line argument rather than a constant, so the Finder Quick Actions had
  no way to know where to put things for anyone not using the default
  `~/FileBridge`. Now they read it.

### 1.11.1
- **The panel stops claiming the phone left while it is downloading.** The
  connected-client marker was only written by `/api/list`, and a download is one
  long request with nothing else on the wire — so after the 45 s freshness
  window the panel decided no phone was there and put the QR back, mid-transfer.
  `/file` now marks the client when it starts and every 10 s while bytes move.

### 1.11.0
- **Big downloads to the phone survive a dropped connection.** `/file` now sends
  an `ETag` (`"<size>-<mtime_ns>"`) and `Last-Modified`. Android's
  DownloadManager keeps the ETag from the first response and replays it as
  `If-Match` with `Range: bytes=N-` when it resumes; with no ETag it decides the
  download *cannot* be resumed and fails on the first broken connection without
  ever asking for a range. The 1.10.0 transfer log proved exactly that: a 707 MB
  file died at 7.5% after 16 s, and the whole log contained no ranged request.
  `If-Range` with a stale validator now falls back to a full `200`, and a stale
  `If-Match` answers `412` rather than splicing two different files together.

### 1.10.0
- **Every transfer logs how it ended** — `complete` / `client-disconnected` /
  `error:<type>`, with bytes sent, duration, rate and the requested range. Added
  because three plausible theories all fitted the one thing Android shows for a
  failure ("Unable to download"), and none of them could be told apart without
  knowing what the Mac saw. It answered it in one attempt.

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

### 1.16.0
- Ships server 1.16.0: the Cable card keeps the real pairing error, and can
  hand you a QR for an armed tunnel.

### 1.15.0
- Ships server 1.15.0, so the Cable card can tell you RNDIS is the problem.

### 1.14.0
- Ships server 1.14.0, so the Cable card also covers USB tethering and can show
  a QR for it.

### 1.13.0
- Ships server 1.13.0, so the panel gains the Cable card and "Pair over cable".
  Nothing in the launcher changed: the server binds the wired port and keeps the
  reverse mapping armed itself, because it is the process that outlives the
  launcher.

### 1.12.0
- **Finder right-click: Copy to Phone / Move to Phone.** Two Quick Actions ship
  inside the bundle and are installed to `~/Library/Services` on launch, then
  left alone until the shipped version changes. Sending a file stops requiring
  the app at all — right-click it where it already is.
- Copy and move are deliberately **separate items** rather than one that guesses.
  Moving a file into `to-phone` and letting the phone collect it removes the only
  copy from where it was; that should be a thing you chose, not a default.
- A name already in `to-phone` gets a numbered sibling instead of being
  overwritten, and anything already inside the served folder is skipped — moving
  a file onto itself is how you lose it.
- Remove them with `python3 scripts/make_quick_actions.py --uninstall`.

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

### 1.13.0 (versionCode 16)
- **`isCable()` counts a tethered address too.** It matched only loopback, so a
  `192.168.42.x` link would have been filed in the wifi slot and overwritten the
  real one — the exact bug the two slots were added to prevent. Both wired
  shapes now classify as the cable, so the fallback still has somewhere to fall
  back to.
- The cable error offers tethering as well as the panel's Pair button, since
  either can be the thing that is off.

### 1.12.0 (versionCode 15)
- **One saved link per transport, and a fallback between them.** Pairing over
  the cable overwrote the only saved link, so unplugging left the app pointed at
  a `127.0.0.1` that nothing answers and the Mac had to be scanned again. It now
  keeps `url_wifi` and `url_usb` separately and, when the live one fails, tries
  the other once before throwing the user back to the scanner. Cheap in the
  common direction: with nothing behind `adb reverse`, loopback refuses
  immediately instead of burning the 8 s connect timeout.
- **A VPN is no longer blamed for the cable.** The VPN warning is right about
  wifi and meaningless about loopback, where no VPN can reach — naming it would
  send someone off to debug the wrong thing. Over the cable the message asks for
  the cable and the panel's Pair button instead.
- Nothing was needed for pairing itself: the Mac fires a `filebridge://` deep
  link over adb, and that already funnels into `connectFromPayload()`.

### 1.11.1 (versionCode 14)
- **A VPN is named as the cause when one is on.** "Could not reach the Mac" listed
  the wifi and Start sharing, but not the one condition that looks identical to
  both: a full-tunnel VPN routes even `192.168.x.x` to the exit server, so a Mac
  two metres away is unreachable. Detected via `TRANSPORT_VPN` rather than
  guessed, so the message only appears when it is true.
- Settings shows the same warning while a VPN holds the default route. It is a
  live condition, not a permission, so there is nothing to grant or remember.

### 1.11.0 (versionCode 13)
- **Sending shows progress, and survives the screen going off.** Uploads ran on
  the Activity's own thread pool with no wifi lock and no notification — the same
  fault that killed downloads before 1.10.0, except uploads still had it and
  nobody had noticed, because the only thing on screen was "sending 1 file(s)…"
  whether it worked or not. They now go through the service alongside downloads,
  with a progress notification and the radio held awake.
- **A queued job could be dropped silently.** The worker polled the queue, saw
  it empty, and exited — while a job arriving in that window found the thread
  still alive, added itself, and was never claimed. The tap looked accepted and
  nothing happened. The queue is now only declared empty while holding the lock a
  new job must also take.
- **The notification says how many are waiting**, and a second tap no longer
  relabels the notification of the file already in flight. Transfers run one at a
  time on purpose: two large ones over a single wifi link finish later than the
  same two back to back, and one progress bar cannot honestly describe both.
- **Tapping a finished file opens it.** The completion notification launches a
  player (`ACTION_VIEW`) rather than reopening this app, and tapping an
  already-downloaded row in the list offers Open / Download again instead of
  quietly fetching it a second time. Android 9 and older reach the file through
  a `FileProvider`; from 10 the MediaStore uri is already openable.
- **Uploads still cannot resume** — the server takes one multipart POST with no
  offset endpoint, so an interrupted send restarts. The failure now says so, and
  says how far it got. That endpoint is the next thing worth building.

### 1.10.1 (versionCode 12)
- The Camera permission row showed the *files* icon — the only icon set had no
  camera in it, so a placeholder was left in and never replaced.
- The unrestricted-background explanation no longer quotes DownloadManager's
  "Unable to download", which this app can no longer produce.

### 1.10.0 (versionCode 11)
- **The app downloads files itself. DownloadManager is gone.** It runs transfers
  as JobScheduler work and holds no wifi lock, so the radio slept with the
  screen and the transfer was dropped as "network lost" rather than retried.
  Measured on the Mac across three attempts at a 707 MB file: dead at 15.8 s,
  15.9 s and 16.2 s — three different byte counts, one clock, i.e. the phone's
  15 s display timeout. It never once asked for a byte range afterwards, even
  with the server sending an ETag.
- `DownloadService` (renamed `TransferService` in 1.11.0) is a foreground
  service holding a **WifiLock**
  (`FULL_LOW_LATENCY`, or `FULL_HIGH_PERF` below Android 10) and a partial
  WakeLock while bytes are moving, with its own resume loop: `Range: bytes=N-`
  plus `If-Match` after every drop, backoff between tries, and a give-up only
  after 8 attempts that move **no** bytes at all. Any progress resets the count,
  so a flaky link finishes rather than failing.
- **A partial file survives a give-up.** The target and its ETag are recorded
  per URL, so downloading the same file again continues from where it stopped
  instead of starting over.
- Written through MediaStore on Android 10+ (append mode `wa`, `IS_PENDING`
  until complete), plain files below that. Notification shows progress, rate and
  a Cancel action.
- Failures now say what actually happened in a sentence, instead of mapping
  Android's error enum to a guess.

### 1.9.0 (versionCode 10)
- **The failure reason now actually reaches you.** 1.8.0 asked Android *why* a
  download failed, but only while the app was on screen — the receiver lived
  between `onStart` and `onStop`, and a long download fails precisely when the
  phone is asleep with the app long gone. Pending downloads are recorded in
  prefs and swept on every resume, so the reason survives the process dying.
  First failure of a sweep gets a dialog, the rest are toasts.
- The "could not resume" message names the real cause: a Mac older than 1.11.0
  sends no ETag, and without one Android refuses to resume at all.

### 1.8.0 (versionCode 9)
- **Failures say why.** Android reports one generic "Unable to download" for
  every cause, which is why three different theories fitted the same symptom.
  `COLUMN_REASON` is read and explained in plain words.
- Free space is checked before enqueuing, and a name collision in
  `Downloads/FileBridge` picks the next free name instead of failing.

### 1.6.0 (versionCode 7)
- **Settings tab with in-app permission management.** Bottom nav (Files /
  Settings), reachable *before* connecting so permissions can be fixed first —
  a blocked camera is exactly why Scan would seem to do nothing.
- **Unrestricted-background request.** The likely cause of "Unable to download"
  on large files: Android stopping the transfer once the app is backgrounded or
  the screen sleeps. Android requires its own system dialog for this; no app can
  grant it silently.
- Camera is requested at the moment Scan is tapped, not cold on first launch.
  Notification permission is only real on Android 13+, so older versions are
  told "not needed" rather than shown a dead button.
- Every permission row shows live status as **text** (`Allowed` / `Grant` /
  `Fix`), never colour alone, with a plain-language reason.
- Settings also carries connection info (which Mac, where downloads land,
  Disconnect) and About (version, versionCode, package, source, licence).
- **Real vector icons** replace the unicode glyphs (`▸ ✓ ⬇`), which were
  font-dependent and rendered inconsistently across devices.
- All UI text moved into `strings.xml`; shared row styles keep the 8dp rhythm
  and 64dp touch height from drifting as rows are added.

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

or just `./scripts/build-android.sh`, which finds a Gradle 8 and sets `JAVA_HOME`
itself. It writes two copies: `FileBridge-<ver>.apk` at the root, and
`dist/FileBridge-<ver>.apk`.

Then copy the APK into `~/FileBridge/to-phone/` and install it from the phone —
the running server is how you deliver its own updates. **Copy, do not move.** A
loose APK at the repo root is not a durable home for one: a 1.12.0 build went
missing between the build and the install, the Trash was empty, and nothing in
this project deletes served files, so there was nothing to recover. The `dist/`
copy is the one that is committed (`*.apk` is ignored, `!dist/*.apk` is not) and
attached to the GitHub release:

```bash
git add dist/FileBridge-<ver>.apk && git commit -m "Release <ver>"
git tag -a v<repo-ver> -m "..." && git push origin main --tags
gh release create v<repo-ver> dist/FileBridge-<ver>.apk --title "..." --notes "..."
```

The repo tag is its own sequence and does **not** track any one component —
`v1.13.1` shipped with server 1.12.0.

Debug-signed. Installs fine and upgrades in place because the key is stable, but
Play Store distribution would need a release keystore.
