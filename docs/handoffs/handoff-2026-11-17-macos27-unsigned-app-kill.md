# Handoff — server killed after launch on macOS 27 (fixed), phone side untested

**Generated:** 2026-11-17
**Next focus:** confirm the fix from the phone, then settle why `am start` refused

---

## Goal of next session

1. **Prove the fix end to end with the phone.** The Mac side is verified (server
   survives a cold launch and a Quit → relaunch). Nothing has been tried from the
   phone since. Open the panel, load the wifi link on the phone, move a file each
   way.
2. **Find out why the phone cannot resolve `filebridge://`.** `gui.log` has the
   answer to *what* (below); *why* needs adb to see the phone.

Read [HANDOFF.md](../../HANDOFF.md) first — state-of-truth, updated today.

## State of play

**Done — committed locally, not pushed:**
- `3fe4866` — `scripts/build-mac-app.sh` ad-hoc signs the bundle. Installed copy at
  `~/Applications/FileBridge.app` was rebuilt with it and is the running one.

**Uncommitted:** `HANDOFF.md` (today's findings) and this file.

**What broke.** The panel read "Not running — File Bridge has quit" and the phone
could not connect. Nobody had quit it: `gui.log` showed a fresh `--- launch` banner
every 10–30 s, each followed by one `GET /api/status` and then nothing — no
traceback, and the last `/api/quit` was weeks old. That pattern means an external
kill.

**Root cause** (from `/usr/bin/log show` on `loginwindow`, 08:10:50): the bundle is
unsigned, so Background Task Management cannot build an identifier for it
(`failed to construct identifier`) and answers `Error -98`. When the launcher
exits, macOS then "schedules termination of the app's subordinate processes" —
the server, ~0.4 s later.

**Ruled out, in order** (do not retry these):

| Tried | Result |
|---|---|
| `setsid` / `start_new_session` (what shipped) | Reaped — process responsibility, not session |
| Server as a `launchctl bootstrap` job | Reaped ~1 s after launcher exit |
| Ad-hoc `codesign --force --deep -s -` on the bundle | **Works**, alone, with the original launcher |

**In progress:** nothing mid-flight.

**Blocking:** nothing code-side. Phone-side items below need the human.

## The `am start` question — half answered

Today's `gui.log` gave the failure text, 25+ times, always `rc=1`:

```
Error: Activity not started, unable to resolve Intent
{ act=android.intent.action.VIEW dat=filebridge://c/... flg=0x10000000 xflg=0x4 }
```

So the phone had **no activity matching the `filebridge` scheme**. The repo manifest
declares it (`android/app/src/main/AndroidManifest.xml:52`, VIEW + BROWSABLE +
DEFAULT), so the likeliest cause is that the **APK on the phone predates that
filter** or is not installed. That is a hypothesis, not a finding — nothing has
looked at the phone. Check with adb once it sees the phone:

```bash
adb shell pm list packages | grep filebridge
adb shell dumpsys package com.enfono.filebridge | grep -i versionCode
```

Compare against versionCode 16 (`dist/FileBridge-1.13.0.apk`). If lower, `adb install -r`
it. Then autopair should open the app on its own within 5 s of the phone appearing.

## Open decisions

1. **Whether to keep the Cable card when no cable path can work.** Unchanged: *lean
   keep*. Still reads "No phone on the cable" on this hardware.
2. **Whether to pursue the cable at all.** Unchanged: only if the user still wants
   VPN-immunity / no radio sleep. Wifi measured 17.34 MB/s.
3. **Whether to update the stale comment in `launcher.sh`.** It still says `setsid`
   is what stops the server being reaped. That is no longer the whole story on
   macOS 27 (signing is). Left alone to keep the fix to one file.

## Needs the human

| Action | Why |
|---|---|
| Open `http://10.178.225.232:8001/` link from the panel on the phone, send a file each way | Only a phone can prove the wifi path after the fix |
| Decide whether to push `3fe4866` | Nothing has been pushed; no release is cut for this |

## Skills to use

- `superpowers:systematic-debugging` — if the server dies again; the lesson here was
  that two plausible fixes were wrong and the log, not the code, held the answer
- `superpowers:verification-before-completion` — "alive at 20 s" and "no `-98`" were
  the checks; a phone transfer is the one still missing
- `frappe-erpnext-expert` — not applicable; noted so it is not invoked reflexively

## Artifacts

- Repo: `/Users/sayanthns/Documents/Claude Code Main/R&D Products/filebridge`
- State of truth: [HANDOFF.md](../../HANDOFF.md) — new quirks-table row for the
  macOS 27 kill, and the `am start` entry rewritten
- Runtime log: `~/.filebridge/gui.log` (mode 600, key redacted in request lines)
- Diagnose a vanishing server: `/usr/bin/log show --last 5m --predicate
  'eventMessage CONTAINS[c] "filebridge"'` and look for `-98`. Use the full path —
  zsh's builtin `log` swallows the arguments
- If the app is ever rebuilt by hand or copied: re-sign with
  `codesign --force --deep -s - ~/Applications/FileBridge.app`
- `agentsync` ran at the start of this wrap-up (`graph/` 269 nodes, AST only)
