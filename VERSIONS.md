# FileBridge — versions

Three pieces, versioned independently. They talk over a small HTTP API, so they
do not have to move together — only bump what you actually changed.

| Piece | Version | Where |
|---|---|---|
| Server | **1.12.0** | `filebridge.py` → `APP_VERSION` |
| Mac app | **1.12.0** | `FileBridge.app` → `CFBundleShortVersionString` |
| Android app | **1.11.1** (code 14) | `android/app/build.gradle` → `versionName` / `versionCode` |

Android needs both: `versionName` is what you read, `versionCode` is what the
installer compares. **A build with an unchanged `versionCode` will not install
over the previous one**, so bump it on every APK you hand to the phone.

---

## Server

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

Then copy the APK into `~/FileBridge/to-phone/` and install it from the phone —
the running server is how you deliver its own updates.

Debug-signed. Installs fine and upgrades in place because the key is stable, but
Play Store distribution would need a release keystore.
