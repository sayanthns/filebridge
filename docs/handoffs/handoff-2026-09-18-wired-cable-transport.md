# Handoff — settle why `adb shell am start` refused

**Generated:** 2026-09-18 · **Closed out:** 2026-09-18
**Next focus:** close the one remaining code unknown

---

## Goal of next session

**Find out why `adb shell am start` refused.** It is the last unknown in the
cable work; everything else about both wired paths is measured.

Nothing else is outstanding. Everything from the 2026-09-08 session shipped as
[v1.19.0](https://github.com/sayanthns/filebridge/releases/tag/v1.19.0), tree is
clean and `HEAD == origin/main`.

Do **not** re-run the adb/tethering exercise hoping for a different answer. Both
cable paths and exactly what would unblock each are in
[HANDOFF.md](../../HANDOFF.md) — read that first, it is the state-of-truth file
for this project and is current as of 1.19.0.

## State of play

**Done** — all pushed, tree clean, `HEAD == origin/main`:
- Wired transport (second loopback listener behind `adb reverse`) and the
  `_local()` socket-vs-address security fix — `58eaace`
- USB tethering path + RNDIS detection — `44a4ef5`, `f99c2fc`
- Access key no longer written to `gui.log` (was there 762× at mode 644) — `80c12c5`
- Autopair: the cable pairs itself the instant a phone appears — `adaca0e`
- Per-release rationale is in [VERSIONS.md](../../VERSIONS.md); dead ends and the
  descriptor evidence are in [docs/ARCHITECTURE.md](../ARCHITECTURE.md)

**Also done since this handoff was first written:**
- `v1.19.0` tagged and released (autopair + `--adb`), tag at `HEAD`
- `agentsync` enabled here — pre-push hook installed and verified firing, graph
  built AST-only at 269 nodes. Generated state gitignored; `.graphifyignore`
  committed. See `e9dfc02` for why the LLM backend is not used

**In progress:** nothing mid-flight.

**Blocking — both are phone-side, neither is a code problem:**
- adb never publishes its interface (`255/66/1` absent). The likely cause is
  recorded in HANDOFF.md: the phone re-enables USB tethering by itself on
  connect, and that RNDIS composition carries no adb interface.
- Wireless debugging was requested and agreed but never turned on — mDNS
  advertised nothing across a 10-minute watch.

## Open decisions

1. **Whether to keep the Cable card visible when no cable path can work.**
   It currently sits there reading "No phone on the cable" forever on this
   hardware. *Lean: keep it.* Silently hiding a broken transport is how someone
   ends up hunting a driver that does not exist — but the user has seen it three
   times now and may disagree.
2. **Whether to pursue the cable at all.** Wifi measured 17.34 MB/s against a
   USB 2.0 cable that would not have beaten it. *Lean: only if the user still
   wants it* — the value was always VPN-immunity and no radio sleep, never speed.

## How to settle the `am start` question

Needs adb to see the phone by any transport. Wireless debugging is the cheapest:
Developer options → Wireless debugging → Pair device with pairing code, then
`adb pair <ip>:<port> <code>`. `adb mdns services` discovers the endpoint while
that dialog is open. Then just wait — autopair arms the tunnel and fires the
deep link within 5s, and server ≥1.17.0 logs the result as an `AM start:` line
in `~/.filebridge/gui.log` with the key redacted.

This is **not** a wired connection — the adb transport rides wifi. It is only
for capturing the failure reason, which then applies to the USB path.

## Skills to use

- `superpowers:systematic-debugging` — for the `am start` refusal, if it needs real
  digging rather than one log line
- `superpowers:verification-before-completion` — this project's history is bugs that
  got through because something was "tested" without exercising the real path
- `frappe-erpnext-expert` — **not** applicable here; noted only so it is not
  invoked reflexively

## Artifacts

- Repo: `/Users/sayanthns/Documents/Claude Code Main/R&D Products/filebridge`
- State of truth: [HANDOFF.md](../../HANDOFF.md) — verified vs assumed, open
  items, this machine's quirks
- Design + dead ends: [docs/ARCHITECTURE.md](../ARCHITECTURE.md)
- Changelog with reasons: [VERSIONS.md](../../VERSIONS.md)
- Releases: https://github.com/sayanthns/filebridge/releases (latest tag
  `v1.19.0`, at `HEAD`; nothing untagged)
- APK for the phone: `dist/FileBridge-1.13.0.apk` (versionCode 16).
  `~/FileBridge/to-phone/` is currently empty — re-stage it if the phone needs it.
- Runtime log: `~/.filebridge/gui.log` — mode 600, request lines redacted
- `agentsync` is enabled: `.agentsync/` (gitignored) holds `git-state.md`,
  `memory.md` and `graph/`. Refresh with `agentsync sync`; it also runs itself
  on every push. Note `.agentsync/refresh.log` is only written by `init`, so it
  looks stale even when syncs work — check `git-state.md` mtime instead
- Known environment fault, unfixed on purpose: the global npm
  `@anthropic-ai/claude-code` never downloaded its native binary, so no `claude`
  is on `$PATH`. Only affects graphify's LLM backend; the AST path is unaffected
