# Handoff

For whoever picks this up next. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
alongside this — it holds the HTTP API and the dead ends. This file is state and
honesty: what works, what is merely *believed* to work, and what is left.

Last updated: 2026-08-10.

## Where things stand

| Piece | Version | State |
|---|---|---|
| Server (`filebridge.py`) | 1.11.0 | Working. Browse, download (Range + ETag), upload, pause/resume |
| Mac app | 1.11.0 | Working. Installed at `~/Applications/FileBridge.app`, Dock shortcut added |
| Android app | 1.10.0 (code 11) | Working. Confirmed on the author's phone, screen off |

**Large downloads to the phone (the long-running bug).** Two causes, one after
the other:

1. The server sent no `ETag`, so a resume was impossible even in principle.
   Fixed in server 1.11.0 (`ETag`, `Last-Modified`, `If-Range`, `If-Match`),
   verified by curl including a byte-identical resumed tail.
2. DownloadManager still would not resume. It holds no wifi lock, so the radio
   slept with the screen: three attempts at a 707 MB file died at **15.8 s,
   15.9 s and 16.2 s** — three different byte counts, one clock, matching a 15 s
   display timeout — and it never issued a single ranged request afterwards.
   Android 1.10.0 drops it for `DownloadService`, which holds a WifiLock and a
   WakeLock and runs its own `Range:` resume loop.

Both landed and a large file now completes on the phone with the screen off.
The shape of a healthy transfer in `~/.filebridge/gui.log` is several `TRANSFER`
lines for one file, the later ones carrying `range=bytes=N-`, ending in
`complete`. A single `range=none` line with no follow-up is the old failure.

**`adb` exists on this machine** at `~/Library/Android/sdk/platform-tools/adb`
— it is simply not on `PATH`, which is why earlier sessions concluded there was
no way to reach a phone. With USB debugging on, `adb install -r` and
`adb logcat` are available and would end the guesswork.

Released as [v1.11.0](https://github.com/sayanthns/filebridge/releases), with the
1.10.0 APK attached.

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
  (`screenOrientation=0x1`), valid signature, `DownloadService` present in the
  dex and declared `foregroundServiceType=dataSync`
- ETag and resume: `412` on a stale `If-Match`, full `200` on a stale
  `If-Range`, and a resumed tail whose sha1 matches the source byte for byte
- **A 707 MB file completing on the phone with the screen off** (1.10.0 app,
  1.11.0 server) — the bug that took four attempts to pin down

**Not verified — treat as unknown:**
- **Phone-side visuals.** No Android device has ever been attached to this Mac
  for a build; layouts, the scanner UI and the back-button behaviour are
  compile-verified only. Downloading is the exception — that one is confirmed by
  use. Note `adb` **does** exist here (see above), so this is fixable.
- **The panel reviving itself after a Quit** (1.9.0). The server restart is
  measured; the page waking back up is reasoned from the code, because driving a
  live browser session across a server restart was not possible here.
- **Any second machine.** Only ever run on one Mac, one phone, one network.

## Open items

1. **No tests.** Everything above was `curl` by hand. The commands are listed at
   the end of ARCHITECTURE.md and would convert directly into a shell test
   script — that is the highest-value next task.
2. **Debug-signed APK.** Installs and upgrades fine, but is not distributable.
   Needs a release keystore, which is a credential the owner must create.
3. **No iOS app.** iPhones can use the browse view at `/?t=<key>` instead.
4. **The Mac app has no Dock tile of its own** while running. It is a launcher
   that exits by design (see ARCHITECTURE.md — keeping it alive is what caused
   the Force Quit bug). The panel window belongs to Chrome. A real tile needs a
   GUI process, and Tk cannot provide one on this machine.
5. **Chrome reuses its `--app` window** for the same URL, and remembers its last
   size — including full screen. If the panel comes up full screen, that is
   Chrome's memory, not the launcher, which asks for 560×880.
6. **Plain HTTP.** Fine on a home LAN, wrong for shared wifi. TLS would mean a
   self-signed cert and a trust prompt on the phone.

## This machine's quirks

These cost hours. They are properties of the environment, not the code.

| Thing | Reality |
|---|---|
| `swiftc` | Broken: *redefinition of module 'SwiftBridging'* from a bad CommandLineTools module map. No native Swift UI possible here |
| Tkinter | Only Apple's system **Tk 8.5.9**, which draws blank windows on modern macOS. Widgets build, nothing renders |
| `~/Documents` | TCC-protected. A Finder-launched app reading code there gets `Operation not permitted`. Terminal *does* have access, which hides the bug |
| Gradle | Not on `PATH`. `scripts/build-android.sh` finds a cached distribution under `~/.gradle/wrapper/dists` |
| JDK | `JAVA_HOME=/opt/homebrew/opt/openjdk@17` — AGP 8.2.1 needs 17, not 21 |
| `screencapture` | Needs Screen Recording permission; unavailable, so UI could not be visually checked. Headless Chrome was used for the panel screenshots |
| QR generation | macOS CoreImage via JXA (`tools/qrgen.js`). No pip install. `tools/qrread.js` decodes, and every generated QR should be decoded to confirm it scans |

## Machine-local state (not in the repo)

| Path | What | Notes |
|---|---|---|
| `~/FileBridge/to-phone` | Mac → phone | |
| `~/FileBridge/from-phone` | phone → Mac | |
| `~/.filebridge/key` | access key | **Secret.** Persisted so pairing survives restarts. Delete to unpair every device |
| `~/.filebridge/state.json` | taken flags, cached durations | Safe to delete |
| `~/.filebridge/gui.log` | server + launcher output | First place to look when the app "does nothing" |
| `/tmp/filebridge_clients.txt` | last phone seen | 45 s freshness window |

## Getting going

```bash
git clone https://github.com/sayanthns/filebridge.git && cd filebridge
python3 filebridge.py ~/FileBridge --port 8001 --token devkey
```

Panel: `http://127.0.0.1:8001/connect` · Browse view:
`http://127.0.0.1:8001/?t=devkey`

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
