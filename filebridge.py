#!/usr/bin/env python3
"""FileBridge — browse, download and upload files between this Mac and a phone.

Stdlib only. One file. No install.

    python3 filebridge.py                       # serves ~/Movies
    python3 filebridge.py ~/Downloads           # serves another folder
    python3 filebridge.py ~/Movies --port 8080

Why this exists rather than `python3 -m http.server`:

* Range requests. http.server answers every request with the whole file, so a
  phone download that drops has to start over, and video cannot be streamed.
  Here a partial request gets a 206 and resumes.
* It shows size and duration, so you can tell a 100 MB / 30 min file from a
  2.8 GB one before tapping.
* It remembers what you already pulled, so a long list stays navigable across
  sessions.
* Uploads, so the phone can send things back.
* A token in the URL, because a plain file server on a shared network hands
  your folder to everyone on it.

State (what you have marked, cached durations) lives in
~/.filebridge/state.json. Deleting that file resets only the bookkeeping —
never your media.
"""

import argparse
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import signal
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = os.path.expanduser("~/.filebridge")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
DEFAULT_ROOT = os.path.expanduser("~/FileBridge")
INBOX_NAME = "from-phone"
OUTBOX_NAME = "to-phone"
APP_VERSION = "1.14.0"
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".mp3", ".m4a"}
CHUNK = 256 * 1024
# Written whenever a phone (i.e. a non-localhost client) actually talks to us.
# The Mac window polls this to know a device connected and hide the QR.
CLIENTS_FILE = "/tmp/filebridge_clients.txt"

# Wired transport. The phone dials its OWN loopback, which `adb reverse` maps
# to a second listener of ours bound to 127.0.0.1. Nothing crosses the network,
# so a full-tunnel VPN on the phone cannot swallow it and no wifi is needed at
# all. Filled in by main(); read by /api/status, which must stay cheap and so
# never shells out to adb itself.
USB = {
    "on": False,        # is the wired listener up
    "adb": "",          # path to adb, "" when we could not find one
    "phone_port": 0,    # port on the phone that adb reverse listens on
    "host_port": 0,     # our loopback listener behind it
    "devices": [],      # serials in the "device" state, ready to use
    "waiting": [],      # [serial, state] for unauthorized / offline ones
    "armed": [],        # serials whose reverse mapping we last set successfully
}

# Stop Sharing pauses instead of exiting. Killing the process meant the panel
# it was serving went dead too, leaving no way to start again without quitting
# and relaunching the app. Paused = the LAN is refused, the Mac panel still
# works, and Start resumes.
PAUSED = {"on": False}

_state_lock = threading.Lock()
_state = {"downloaded": {}, "durations": {}}


# ---------------------------------------------------------------- state


def load_state():
    global _state
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _state = {
                "downloaded": data.get("downloaded") or {},
                "durations": data.get("durations") or {},
            }
    except Exception:
        pass


def save_state():
    with _state_lock:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(_state, handle)
            os.replace(tmp, STATE_FILE)
        except Exception as error:
            print("could not save state:", error)


# ---------------------------------------------------------------- helpers


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return str(int(num)) + " " + unit
            return ("%.1f" % num) + " " + unit
        num /= 1024.0


def human_time(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return str(hours) + "h " + str(minutes) + "m"
    return str(minutes) + "m"


def pretty_name(name):
    """Turn a scraped filename into something readable on a small screen."""
    base = os.path.splitext(name)[0]
    base = base.replace("_", " ").replace(".", " ")
    base = re.sub(r"\s+", " ", base)
    for junk in ("FULL MOVIE", "Full Movie", "FULL EPISODE", "Completed Movie",
                 "ENGLISH SUB", "English Sub", "Eng Sub", "FULL HD", "FULL",
                 "Video Dailymotion"):
        base = base.replace(junk, " ")
    base = re.sub(r"[-–|]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -")
    return base or name


def duration_of(path, size, mtime):
    """ffprobe, cached by path+size+mtime so a rename or edit re-probes."""
    key = path + "|" + str(size) + "|" + str(int(mtime))
    cached = _state["durations"].get(key)
    if cached is not None:
        return cached

    value = 0
    if os.path.splitext(path)[1].lower() in VIDEO_EXT and shutil.which("ffprobe"):
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=20,
            )
            value = int(float(out.stdout.strip() or 0))
        except Exception:
            value = 0

    _state["durations"][key] = value
    return value


# Android's USB tethering always builds this subnet: the phone takes .129 and
# hands the host a lease in the same /24. It is a fixed, documented range, so
# recognising it is how we tell "a phone is on the cable" from "someone plugged
# in a dock" — far more honest than guessing from interface names, which differ
# per Mac (en5, en6, en7...).
TETHER_NET = "192.168.42."


def interface_ips():
    """[(ifname, ipv4)] for every up interface with an address.

    socket alone cannot enumerate interfaces on macOS without ctypes, and this
    is stdlib-only by design, so it reads ifconfig. Cheap, and only called when
    the panel asks.
    """
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True,
                             text=True, timeout=5)
    except Exception:
        return []
    found, name = [], ""
    for line in (out.stdout or "").splitlines():
        if line and not line[0].isspace():
            name = line.split(":")[0]
        elif "inet " in line and name:
            parts = line.split()
            addr = parts[parts.index("inet") + 1]
            if not addr.startswith("127."):
                found.append((name, addr))
    return found


def tether_ip():
    """Our address on the phone's USB-tethered subnet, or "".

    USB tethering is the cable path that needs nothing from Developer options —
    which matters, because MagicOS would not publish an adb interface at all on
    the phone this was built against. It is plain IP, though, so unlike
    `adb reverse` a full-tunnel VPN on the phone can still swallow it.
    """
    for _, addr in interface_ips():
        if addr.startswith(TETHER_NET):
            return addr
    return ""


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------- adb / wired

# Not on PATH on a normal Mac even when the SDK is installed, which is how an
# earlier session concluded there was no way to reach a phone from here.
ADB_PLACES = (
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
)


def find_adb():
    for path in ADB_PLACES:
        if os.access(path, os.X_OK):
            return path
    return shutil.which("adb") or ""


def adb(args, timeout=12):
    """Run adb, or return None. Never raises: a missing cable is normal."""
    if not USB["adb"]:
        return None
    try:
        return subprocess.run([USB["adb"]] + list(args), capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return None


def adb_devices():
    """(ready, waiting). `waiting` is the interesting half: a phone whose owner
    has not tapped Allow yet shows up as `unauthorized`, and saying so beats
    reporting no phone at all."""
    out = adb(["devices"])
    if not out or out.returncode != 0:
        return [], []
    ready, waiting = [], []
    for line in (out.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] == "*":
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            ready.append(serial)
        else:
            waiting.append([serial, state])
    return ready, waiting


def arm_reverse(serial):
    """Point the phone's loopback port at our wired listener."""
    out = adb(["-s", serial, "reverse",
               "tcp:" + str(USB["phone_port"]), "tcp:" + str(USB["host_port"])])
    return bool(out and out.returncode == 0)


def port_busy(port):
    """Is anyone already answering on this loopback port?

    bind() is not a reliable answer. allow_reuse_address (SO_REUSEADDR) lets a
    127.0.0.1 bind succeed *underneath* an existing 0.0.0.0 bind on the same
    port, and the narrower socket then quietly takes every loopback connection
    — including the panel's, which would start getting 403 from the wired
    socket's own security gate. Measured on this machine: a second instance
    bound 127.0.0.1:8801 under a live *:8801 without an error. So ask.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def usb_base():
    return "http://127.0.0.1:" + str(USB["phone_port"])


def usb_deep_link(token):
    return ("filebridge://c?u=" + urllib.parse.quote(usb_base(), safe="") +
            "&t=" + token)


def push_deep_link(serial, link):
    """Open the app on the phone already connected, over the cable.

    This is why wired pairing needs no QR and no typing. The URL is single
    quoted because adb hands the whole command to a shell ON THE DEVICE, and
    the unquoted `&` before the key would be read there as "run in background"
    — the app would launch with a truncated link and no token.
    """
    quoted = "'" + link.replace("'", "'\\''") + "'"
    out = adb(["-s", serial, "shell", "am", "start",
               "-a", "android.intent.action.VIEW", "-d", quoted], timeout=20)
    if not out or out.returncode != 0:
        return False
    # `am` exits 0 even when it refused, so read what it said.
    return "Error" not in ((out.stdout or "") + (out.stderr or ""))


def usb_watch():
    """Keep the reverse mapping armed.

    It is not a one-shot: a reverse mapping belongs to one device connection
    and dies on every unplug, and again whenever the adb server restarts. So
    this re-arms every tick for any ready device rather than trusting a cached
    "already armed" flag — an adb server restart leaves the serial looking
    unchanged while the mapping underneath is gone.

    Costs one `adb devices` per tick with nothing plugged in, two with a phone
    attached, both ~10 ms against a running daemon. The first tick starts the
    adb server if it is not already up; --no-wired is how you avoid that.
    """
    while True:
        ready, waiting = adb_devices()
        USB["devices"], USB["waiting"] = ready, waiting
        USB["armed"] = [s for s in ready if arm_reverse(s)]
        time.sleep(5)


# ---------------------------------------------------------------- server


class Bridge(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, root, token, wired=False):
        self.root = os.path.realpath(root)
        self.token = token
        # True for the loopback socket that sits behind `adb reverse`. Its
        # callers are phones, not this machine — see Handler._local().
        self.wired = wired
        self.pool = ThreadPoolExecutor(max_workers=6)
        super().__init__(addr, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "FileBridge"

    def log_message(self, fmt, *args):
        if "/api/" in self.path or self.path.startswith("/file"):
            sys.stderr.write("  " + (fmt % args)[:110] + "\n")

    def _local(self):
        """Is the caller this Mac, and so allowed the control surface?

        Trust follows the SOCKET, not the address. The wired listener is
        reached only through `adb reverse`, so a phone's requests arrive on it
        from 127.0.0.1 and would otherwise pass this gate — handing the phone
        /connect and /qr.png, which both print the access key, plus
        /api/quit. Answering False here is what keeps the cable a phone
        transport rather than a hole in the local-only routes.
        """
        if self.server.wired:
            return False
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _paused_out(self):
        """True (and answered) when a phone asks while sharing is paused."""
        if PAUSED["on"] and not self._local():
            self._json({"error": "Sharing is paused on the Mac."},
                       HTTPStatus.SERVICE_UNAVAILABLE)
            return True
        return False

    def _authed(self):
        token = self.server.token
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if query.get("t", [None])[0] == token:
            return True
        cookie = self.headers.get("Cookie") or ""
        return ("fb_token=" + token) in cookie

    def _deny(self):
        body = b"<h2>Wrong or missing key</h2><p>Open the full link printed in the terminal.</p>"
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _safe_path(self, rel):
        """Resolve a client path inside the served root, or raise."""
        rel = urllib.parse.unquote(rel or "")
        rel = rel.replace("\\", "/").lstrip("/")
        # normpath (not realpath) so ".." is still blocked but a symlink placed
        # inside the bridge folder deliberately — say, your movies directory —
        # is followed instead of refused.
        target = os.path.normpath(os.path.join(self.server.root, rel))
        root = self.server.root
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("outside root")
        return target

    # ---- routes

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/health":
            return self._json({"ok": True})

        # /connect and /qr.png show the key, so they are localhost-only and
        # deliberately do NOT require it — you are already at the machine.
        if route == "/api/status":
            if not self._local():
                return self._json({"error": "localhost only"}, HTTPStatus.FORBIDDEN)
            client, seen = "", 0
            try:
                parts = open(CLIENTS_FILE).read().split()
                if time.time() - int(parts[1]) < 45:
                    client = parts[0]
                    seen = int(parts[1])
            except Exception:
                pass
            out_dir = os.path.join(self.server.root, OUTBOX_NAME)
            in_dir = os.path.join(self.server.root, INBOX_NAME)
            def count(folder):
                try:
                    return len([f for f in os.listdir(folder)
                                if not f.startswith(".")])
                except OSError:
                    return 0
            return self._json({
                "version": APP_VERSION,
                "sharing": not PAUSED["on"],
                "link": "http://" + lan_ip() + ":" +
                        str(self.server.server_address[1]) + "/?t=" + self.server.token,
                "client": client, "seen": seen,
                "root": self.server.root,
                "to_phone": count(out_dir), "from_phone": count(in_dir),
                "tether": self._tether_block(),
                "usb": {
                    "on": USB["on"],
                    "adb": bool(USB["adb"]),
                    "port": USB["phone_port"],
                    "link": usb_base() + "/?t=" + self.server.token,
                    "devices": USB["devices"],
                    "waiting": USB["waiting"],
                    "armed": USB["armed"],
                },
            })

        if route in ("/connect", "/qr.png"):
            if not self._local():
                return self._json({"error": "localhost only"}, HTTPStatus.FORBIDDEN)
            if route == "/connect":
                return self._connect_page()
            # ?tether=1 encodes the USB-tethered address instead of the wifi
            # one, so pairing over the cable is still a scan when there is no
            # adb to fire a deep link with.
            return self._qr_png(tether_ip() if query.get("tether") else None)

        # Paused: the phone is turned away, the Mac panel keeps working.
        if self._paused_out():
            return

        if not self._authed():
            return self._deny()

        if route == "/manifest.webmanifest":
            return self._manifest()
        if route == "/":
            return self._page(query.get("t", [""])[0])
        if route == "/api/list":
            return self._list(query.get("path", [""])[0])
        if route == "/file":
            return self._send_file(query.get("path", [""])[0], download=True)
        if route == "/get":
            # Short link for installing the phone app: no &path= to mistype.
            return self._send_newest_apk()
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/api/stop", "/api/start", "/api/quit", "/api/open",
                           "/api/usb"):
            # Local control surface for the Mac panel. Localhost only: these
            # act on this machine, so no phone may ever reach them.
            if not self._local():
                return self._json({"error": "localhost only"}, HTTPStatus.FORBIDDEN)
            if parsed.path == "/api/start":
                PAUSED["on"] = False
                return self._json({"sharing": True})

            if parsed.path == "/api/quit":
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return self._json({"quitting": True})

            if parsed.path == "/api/usb":
                return self._pair_usb()

            if parsed.path == "/api/stop":
                PAUSED["on"] = True
                try:
                    os.remove(CLIENTS_FILE)
                except OSError:
                    pass
                return self._json({"sharing": False})
            length = int(self.headers.get("Content-Length") or 0)
            wanted = json.loads(self.rfile.read(length) or b"{}").get("folder", "")
            if wanted not in (OUTBOX_NAME, INBOX_NAME):
                return self._json({"error": "unknown folder"}, HTTPStatus.BAD_REQUEST)
            target = os.path.join(self.server.root, wanted)
            os.makedirs(target, exist_ok=True)
            subprocess.Popen(["open", target])
            return self._json({"opened": wanted})

        if not self._authed():
            return self._deny()

        if parsed.path == "/api/mark":
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            key = data.get("path") or ""
            if data.get("done"):
                _state["downloaded"][key] = int(time.time())
            else:
                _state["downloaded"].pop(key, None)
            save_state()
            return self._json({"ok": True})

        if self._paused_out():
            return

        if parsed.path == "/api/bye":
            # The phone says it is leaving, so the Mac panel can stop claiming
            # a live connection instead of waiting for the staleness window.
            try:
                os.remove(CLIENTS_FILE)
            except OSError:
                pass
            return self._json({"disconnected": True})

        if parsed.path == "/api/upload":
            return self._upload()

        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    # ---- listing

    def _note_client(self):
        """Record a real device connecting, for the Mac window to react to."""
        if self._local():
            return
        # A wired phone reaches us over loopback, so its address says nothing
        # about it. Label it instead — one word, because the panel reads this
        # file back as two whitespace-separated fields.
        ip = "usb" if self.server.wired else self.client_address[0]
        try:
            with open(CLIENTS_FILE, "w") as handle:
                handle.write(ip + " " + str(int(time.time())))
        except OSError:
            pass

    def _list(self, rel):
        self._note_client()
        try:
            base = self._safe_path(rel)
        except ValueError:
            return self._json({"error": "bad path"}, HTTPStatus.FORBIDDEN)

        if not os.path.isdir(base):
            return self._json({"error": "not a folder"}, HTTPStatus.NOT_FOUND)

        dirs, files = [], []
        try:
            entries = sorted(os.listdir(base), key=str.lower)
        except OSError as error:
            return self._json({"error": str(error)}, HTTPStatus.FORBIDDEN)

        for name in entries:
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            relpath = os.path.relpath(full, self.server.root)
            try:
                stat = os.stat(full)
            except OSError:
                continue

            if os.path.isdir(full):
                dirs.append({"name": name, "path": relpath})
            else:
                files.append({
                    "name": name,
                    "pretty": pretty_name(name),
                    "path": relpath,
                    "size": stat.st_size,
                    "size_h": human_size(stat.st_size),
                    "mtime": int(stat.st_mtime),
                    "done": relpath in _state["downloaded"],
                    "video": os.path.splitext(name)[1].lower() in VIDEO_EXT,
                })

        # Probe durations in parallel; first visit to a folder pays it once.
        videos = [f for f in files if f["video"]]
        if videos:
            def probe(item):
                full = os.path.join(self.server.root, item["path"])
                item["duration"] = duration_of(full, item["size"], item["mtime"])
                item["duration_h"] = human_time(item["duration"])
            list(self.server.pool.map(probe, videos))
            save_state()
        for item in files:
            item.setdefault("duration", 0)
            item.setdefault("duration_h", "")

        total = sum(f["size"] for f in files)
        pending = sum(f["size"] for f in files if not f["done"])
        return self._json({
            "cwd": "" if base == self.server.root else os.path.relpath(base, self.server.root),
            "parent": None if base == self.server.root else os.path.relpath(
                os.path.dirname(base), self.server.root),
            "dirs": dirs,
            "files": files,
            "total_h": human_size(total),
            "pending_h": human_size(pending),
            "count": len(files),
            "done_count": sum(1 for f in files if f["done"]),
        })

    # ---- file transfer with Range support

    def _send_file(self, rel, download=False):
        try:
            path = self._safe_path(rel)
        except ValueError:
            return self._json({"error": "bad path"}, HTTPStatus.FORBIDDEN)

        if not os.path.isfile(path):
            return self._json({"error": "missing"}, HTTPStatus.NOT_FOUND)

        info = os.stat(path)
        size = info.st_size
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        partial = False

        # An identity for this exact version of the file. Android's
        # DownloadManager stores the ETag from the first response and replays it
        # as If-Match when it resumes; with no ETag it decides the download
        # *cannot* be resumed and fails outright on the first dropped
        # connection, without ever asking for a range. That is why a big file
        # died at 7.5% and the log showed no second request.
        etag = '"%d-%d"' % (size, info.st_mtime_ns)
        modified = self.date_time_string(info.st_mtime)

        # The file changed under a resuming client: tell it so, rather than
        # splicing bytes from two different files together.
        if_match = self.headers.get("If-Match")
        if if_match and if_match.strip() != "*" and etag not in if_match:
            self.send_response(HTTPStatus.PRECONDITION_FAILED)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Range is what makes an interrupted phone download resume instead of
        # restarting — the whole reason not to use http.server for big files.
        rng = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if if_range and if_range.strip() != etag:
            rng = None  # stale validator: serve the whole file instead
        if rng:
            match = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if match:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    if last:
                        end = min(int(last), size - 1)
                elif last:
                    start = max(0, size - int(last))
                if start <= end < size:
                    partial = True
                else:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", "bytes */" + str(size))
                    self.end_headers()
                    return

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", modified)
        if partial:
            self.send_header("Content-Range",
                             "bytes " + str(start) + "-" + str(end) + "/" + str(size))
        if download:
            name = os.path.basename(path)
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()

        sent = 0
        began = time.time()
        outcome = "complete"
        # A download is one long request with nothing else on the wire, so the
        # connected-client marker would go stale mid-transfer and the panel would
        # claim the phone had left while it was busy receiving a file.
        self._note_client()
        last_note = time.time()
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    block = handle.read(min(CHUNK, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    sent += len(block)
                    remaining -= len(block)
                    if time.time() - last_note > 10:
                        self._note_client()
                        last_note = time.time()
        except (BrokenPipeError, ConnectionResetError):
            outcome = "client-disconnected"
        except Exception as error:
            outcome = "error:" + type(error).__name__
        finally:
            took = max(0.001, time.time() - began)
            sys.stderr.write(
                "  TRANSFER %s %s sent=%d/%d (%.1f%%) in %.1fs %.2fMB/s range=%s\n" % (
                    outcome, os.path.basename(path), sent, length,
                    100.0 * sent / max(1, length), took,
                    sent / took / 1048576, rng or "none"))
            sys.stderr.flush()

    def _send_newest_apk(self):
        """Serve the newest .apk in to-phone/, so the install link stays short."""
        folder = os.path.join(self.server.root, OUTBOX_NAME)
        try:
            apks = [f for f in os.listdir(folder) if f.lower().endswith(".apk")]
        except OSError:
            apks = []
        if not apks:
            return self._json({"error": "no apk in " + OUTBOX_NAME},
                              HTTPStatus.NOT_FOUND)
        newest = max(apks, key=lambda f: os.path.getmtime(os.path.join(folder, f)))
        return self._send_file(os.path.join(OUTBOX_NAME, newest), download=True)

    # ---- upload (phone -> Mac)

    def _upload(self):
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return self._json({"error": "expected multipart"}, HTTPStatus.BAD_REQUEST)

        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()

        # A chunked upload has no Content-Length. Returning 400 here is what
        # made "Send to Mac" fail with a bare 400 from the phone.
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        declared = self.headers.get("Content-Length")
        if chunked:
            remaining = -1                     # read until the terminator
        else:
            remaining = int(declared or 0)
            if remaining <= 0:
                return self._json({"error": "no body"}, HTTPStatus.BAD_REQUEST)

        inbox = os.path.join(self.server.root, INBOX_NAME)
        os.makedirs(inbox, exist_ok=True)

        # Minimal multipart reader: the stdlib cgi module is deprecated and
        # removed in 3.13, and all we need is "one or more file parts".
        delim = b"--" + boundary
        buf = b""
        saved = []
        current = None
        handle = None

        def finish():
            nonlocal handle, current
            if handle:
                handle.close()
                handle = None
                if current:
                    saved.append(os.path.basename(current))
            current = None

        def more():
            """Next slice of body, honouring chunked framing when present."""
            if chunked:
                line = self.rfile.readline(64).strip()
                if not line:
                    return b""
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    return b""
                if size == 0:
                    self.rfile.readline(8)     # trailing CRLF
                    return b""
                data = self.rfile.read(size)
                self.rfile.readline(8)         # CRLF after each chunk
                return data
            return self.rfile.read(min(CHUNK, remaining))

        exhausted = False
        while not exhausted or buf:
            if not exhausted and len(buf) < CHUNK * 2:
                block = more()
                if not block:
                    exhausted = True
                    if not chunked:
                        remaining = 0
                else:
                    if not chunked:
                        remaining -= len(block)
                        if remaining <= 0:
                            exhausted = True
                    buf += block

            idx = buf.find(delim)
            if idx == -1:
                if handle and len(buf) > len(delim) + 4:
                    keep = len(delim) + 4
                    handle.write(buf[:-keep])
                    buf = buf[-keep:]
                if exhausted and not handle:
                    break
                if exhausted and handle:
                    handle.write(buf)
                    buf = b""
                    break
                continue

            if handle:
                trailing = buf[:idx]
                if trailing.endswith(b"\r\n"):
                    trailing = trailing[:-2]
                handle.write(trailing)
                finish()

            buf = buf[idx + len(delim):]
            if buf.startswith(b"--"):
                break

            header_end = buf.find(b"\r\n\r\n")
            while header_end == -1 and not exhausted:
                block = more()
                if not block:
                    exhausted = True
                    break
                buf += block
                header_end = buf.find(b"\r\n\r\n")
            if header_end == -1:
                break

            headers = buf[:header_end].decode("utf-8", "replace")
            buf = buf[header_end + 4:]

            match = re.search(r'filename="([^"]*)"', headers)
            if not match or not match.group(1):
                continue

            name = os.path.basename(match.group(1)).strip() or "upload.bin"
            name = re.sub(r"[^\w \.\-\(\)]+", "_", name)
            target = os.path.join(inbox, name)
            stem, ext = os.path.splitext(target)
            counter = 2
            while os.path.exists(target):
                target = stem + "-" + str(counter) + ext
                counter += 1

            current = target
            handle = open(target, "wb")

        finish()
        return self._json({"ok": True, "saved": saved, "folder": INBOX_NAME})

    # ---- connect page (localhost only)

    def _tether_block(self):
        """What the panel needs to offer a USB-tethered link."""
        addr = tether_ip()
        if not addr:
            return {"on": False}
        port = str(self.server.server_address[1])
        return {"on": True, "ip": addr,
                "link": "http://" + addr + ":" + port + "/?t=" + self.server.token}

    def _pair_usb(self):
        """Arm the reverse mapping, then open the app on the phone connected.

        Pairing over the cable needs no QR and no typing, because adb can fire
        the same deep link the QR encodes straight at the phone. This one does
        shell out to adb — it is a button press, not a poll.
        """
        if not USB["on"]:
            return self._json({"error": "Wired sharing is off (--no-wired)."},
                              HTTPStatus.CONFLICT)
        if not USB["adb"]:
            return self._json({"error": "No adb on this Mac. Install Android "
                                        "platform-tools."}, HTTPStatus.CONFLICT)

        ready, waiting = adb_devices()
        USB["devices"], USB["waiting"] = ready, waiting
        if not ready:
            if any(state == "unauthorized" for _, state in waiting):
                return self._json({"error": "Tap Allow on the phone to trust "
                                            "this Mac, then press this again."},
                                  HTTPStatus.CONFLICT)
            return self._json({"error": "No phone on the cable. Plug it in and "
                                        "turn on USB debugging in Developer "
                                        "options."}, HTTPStatus.CONFLICT)

        link = usb_deep_link(self.server.token)
        armed, opened = [], []
        for serial in ready:
            if not arm_reverse(serial):
                continue
            armed.append(serial)
            if push_deep_link(serial, link):
                opened.append(serial)
        USB["armed"] = armed

        if not armed:
            return self._json({"error": "adb could not set up the tunnel. "
                                        "Replug the cable and try again."},
                              HTTPStatus.BAD_GATEWAY)
        if not opened:
            return self._json({"error": "Tunnel is up but the app would not "
                                        "open. Is File Bridge installed on the "
                                        "phone?"}, HTTPStatus.BAD_GATEWAY)
        return self._json({"paired": opened})

    def _deep_link(self, host=None):
        base = ("http://" + (host or lan_ip()) + ":" +
                str(self.server.server_address[1]))
        return ("filebridge://c?u=" + urllib.parse.quote(base, safe="") +
                "&t=" + self.server.token)

    def _qr_png(self, host=None):
        """QR of the deep link, rendered by macOS CoreImage via JXA."""
        out = "/tmp/filebridge_qr.png"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "qrgen.js")
        try:
            subprocess.run(["osascript", "-l", "JavaScript", script,
                            self._deep_link(host), out, "760"],
                           capture_output=True, timeout=25, check=True)
            with open(out, "rb") as handle:
                blob = handle.read()
        except Exception as error:
            return self._json({"error": "qr failed: " + str(error)},
                              HTTPStatus.INTERNAL_SERVER_ERROR)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _connect_page(self):
        link = "http://" + lan_ip() + ":" + str(self.server.server_address[1]) + \
               "/?t=" + self.server.token
        body = CONNECT_PAGE.replace("__LINK__", link).replace("__VER__", APP_VERSION)
        raw = body.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---- UI

    def _manifest(self):
        """Lets Android/iOS "Add to Home Screen" open it like an app.

        start_url carries the key, otherwise launching from the icon would
        land on the refusal page.
        """
        data = {
            "name": "FileBridge",
            "short_name": "Bridge",
            "start_url": "/?t=" + self.server.token,
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f1115",
            "theme_color": "#0f1115",
            "icons": [{"src": ICON_SVG, "sizes": "any", "type": "image/svg+xml"}],
        }
        body = json.dumps(data).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, token):
        body = PAGE.replace("__TOKEN__", token or self.server.token)
        body = body.replace("__ICON__", ICON_SVG)
        raw = body.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Set-Cookie",
                         "fb_token=" + self.server.token + "; Path=/; Max-Age=604800; SameSite=Lax")
        self.end_headers()
        self.wfile.write(raw)


ICON_SVG = (
    "data:image/svg+xml,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20192%20192'%3E"
    "%3Crect%20width='192'%20height='192'%20rx='38'%20fill='%230f1115'/%3E"
    "%3Cpath%20d='M46%2096h100M110%2072l30%2024-30%2024'%20stroke='%234ade80'"
    "%20stroke-width='13'%20fill='none'%20stroke-linecap='round'%20stroke-linejoin='round'/%3E"
    "%3Ccircle%20cx='52'%20cy='96'%20r='13'%20fill='%234ade80'/%3E%3C/svg%3E"
)

CONNECT_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>File Bridge</title>
<style>
:root{
  --brand:#0572F7; --brand-dim:#0B5ED7; --ink:#101828; --muted:#667085;
  --line:#E4E7EC; --card:#FFFFFF; --bg:#F4F6FA; --ok:#129D5E; --stop:#D92D20;
}
@media(prefers-color-scheme:dark){
  :root{--ink:#F2F4F7; --muted:#98A2B3; --line:#2A2F3A; --card:#171B24; --bg:#0F1218}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 -webkit-user-select:none;user-select:none}
.wrap{max-width:520px;margin:0 auto;padding:26px 22px 34px}
h1{font-size:22px;margin:0;letter-spacing:-.2px}
.sub{color:var(--muted);font-size:13px;margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
 padding:16px 18px;margin-top:18px}
.statusrow{display:flex;align-items:center;gap:9px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--stop);flex:0 0 auto}
.dot.on{background:var(--ok)}
.state{font-weight:650;font-size:15px}
.link{margin-top:10px;font:12.5px/1.5 ui-monospace,Menlo,monospace;color:var(--muted);
 word-break:break-all;-webkit-user-select:text;user-select:text}
.actions{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
button{font:15px/1 inherit;font-weight:650;border:1px solid var(--line);
 background:var(--card);color:var(--ink);border-radius:11px;
 min-height:44px;padding:0 18px;cursor:pointer;transition:transform .08s,background .15s}
button:active{transform:scale(.98)}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button.primary:hover{background:var(--brand-dim)}
button.danger{color:var(--stop);border-color:var(--stop)}
button:disabled{opacity:.45;cursor:default}
.folders{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.folder{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:14px;text-align:left;min-height:auto}
.folder b{display:block;font-size:14px}
.folder span{color:var(--muted);font-size:12px;font-weight:400}
.qrwrap{text-align:center;padding:8px 0 4px}
.qrwrap img{width:236px;height:236px;background:#fff;padding:12px;border-radius:14px}
.qrtitle{font-weight:650;margin-bottom:12px;text-align:center}
.hint{color:var(--muted);font-size:12.5px;margin-top:12px;text-align:center}
.connected{text-align:center;padding:18px 6px}
.connected .ip{font:13px ui-monospace,Menlo,monospace;color:var(--brand);margin-top:6px}
.badge{display:inline-block;background:var(--ok);color:#fff;font-size:12px;
 font-weight:650;padding:5px 11px;border-radius:99px}
.ver{color:var(--muted);font-size:11.5px;text-align:center;margin-top:22px}
</style></head><body>
<div class="wrap">
  <h1>File Bridge</h1>
  <div class="sub">Move files between this Mac and your phone, over wifi or the cable</div>

  <div class="card">
    <div class="statusrow">
      <span class="dot on" id="dot"></span>
      <span class="state" id="state">Sharing</span>
    </div>
    <div class="link" id="link">__LINK__</div>
    <div class="actions">
      <button class="primary" id="start" style="display:none">Start sharing</button>
      <button class="primary" id="copy">Copy link</button>
      <button class="danger" id="stop">Stop sharing</button>
      <button id="quit">Quit</button>
    </div>
  </div>

  <div class="folders">
    <button class="folder" id="openTo"><b>To Phone</b><span id="toCount">-</span></button>
    <button class="folder" id="openFrom"><b>From Phone</b><span id="fromCount">-</span></button>
  </div>

  <div class="card" id="usbcard" style="display:none">
    <div class="statusrow">
      <span class="dot" id="usbdot"></span>
      <span class="state">Cable</span>
    </div>
    <div class="link" id="usbstate">-</div>
    <div class="actions">
      <button class="primary" id="usbpair">Pair over cable</button>
      <button id="usbqr" style="display:none">Show cable QR</button>
    </div>
    <div class="hint" id="usbhint"></div>
  </div>

  <div class="card" id="qrcard"></div>

  <div class="ver">version __VER__</div>
</div>

<script>
const $ = id => document.getElementById(id);
let link = "__LINK__";
let sharing = true, client = "", shownQr = false, dead = false, usb = null,
    tether = null, wantCableQr = false;

function el(tag, cls, text){
  const n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text) n.textContent = text;
  return n;
}

function renderUsb(){
  const card = $("usbcard");
  if(!usb || !usb.on){ card.style.display = "none"; return; }
  card.style.display = "";
  const ready = (usb.devices || []).length > 0;
  const unauth = (usb.waiting || []).some(w => w[1] === "unauthorized");
  const live = client === "usb";
  const tethered = !!(tether && tether.on);
  $("usbqr").style.display = tethered && !live ? "" : "none";
  let state, hint, offer = ready;
  if(tethered && !ready){
    // USB tethering needs nothing from Developer options, which is the whole
    // reason it is offered: MagicOS would not publish an adb interface at all.
    // No adb means no deep link to fire, so pairing is a scan.
    state = tether.link;
    hint = "Tethered over the cable. Scan the cable QR on the phone - a VPN " +
           "can still break this one, unlike adb.";
    offer = false;
  }else if(!usb.adb){
    state = "No adb on this Mac";
    hint = "The cable needs Android platform-tools, or turn on USB tethering " +
           "on the phone instead. Wifi is unaffected.";
    offer = false;
  }else if(live){
    state = usb.link;
    hint = "Connected over the cable. Nothing is crossing the network.";
  }else if(unauth){
    state = "Waiting for the phone to trust this Mac";
    hint = "Tap Allow on the phone, then press Pair over cable.";
    offer = true;
  }else if(!ready){
    state = "No phone on the cable";
    hint = "Plug it in, then turn on USB debugging in Developer options - or " +
           "USB tethering, which needs none of them.";
  }else{
    state = usb.link;
    hint = "Opens the app on the phone already connected - no QR, no typing.";
  }
  $("usbdot").classList.toggle("on",
    live || (usb.armed || []).length > 0 || tethered);
  $("usbstate").textContent = state;
  $("usbhint").textContent = hint;
  $("usbpair").style.display = offer ? "" : "none";
}

function render(){
  if(dead){
    $("dot").classList.remove("on");
    $("state").textContent = "Not running";
    $("link").textContent = "File Bridge has quit";
    ["start","stop","copy","quit"].forEach(id => $(id).style.display = "none");
    $("usbcard").style.display = "none";
    $("qrcard").replaceChildren(
      el("div", "qrtitle", "Sharing ended"),
      el("div", "hint", "Open File Bridge from the Dock or Launchpad and this " +
                        "page will reconnect by itself.")
    );
    return;
  }
  renderUsb();
  $("dot").classList.toggle("on", sharing);
  $("state").textContent = sharing ? (client ? "Sharing - phone connected" : "Sharing")
                                   : "Paused";
  $("link").textContent = sharing ? link : "Not sharing";
  $("start").style.display = sharing ? "none" : "";
  $("stop").style.display  = sharing ? "" : "none";
  $("copy").style.display  = sharing ? "" : "none";

  const card = $("qrcard");
  if(!sharing){
    shownQr = false;
    card.replaceChildren(
      el("div", "qrtitle", "Sharing paused"),
      el("div", "hint", "Your phone cannot reach this Mac. Press Start sharing to resume.")
    );
    return;
  }
  if(client){
    shownQr = false;
    const box = el("div", "connected");
    box.append(el("span", "badge", "Phone connected"),
               el("div", "ip", client === "usb" ? "over the cable" : client),
               el("div", "hint", "Browse and transfer from the phone app."));
    card.replaceChildren(box);
    return;
  }
  // Back to the QR: either nothing has connected yet, or the phone left.
  if(!shownQr){
    shownQr = true;
    const img = el("img");
    img.id = "qr"; img.alt = "QR code to connect";
    img.src = "/qr.png?" + (wantCableQr ? "tether=1&" : "") + Date.now();
    const wrap = el("div", "qrwrap"); wrap.appendChild(img);
    card.replaceChildren(
      el("div", "qrtitle", wantCableQr ? "Scan to connect over the cable"
                                       : "Scan with the File Bridge app"),
      wrap,
      el("div", "hint", "This code disappears once your phone connects.")
    );
  }
}

async function poll(){
  try{
    const r = await fetch("/api/status");
    const s = await r.json();
    if(s.error) return;
    if(dead){ dead = false; shownQr = false; }   // server is back
    link = s.link; sharing = s.sharing; client = s.client || ""; usb = s.usb || null;
    tether = s.tether || null;
    $("toCount").textContent = s.to_phone + (s.to_phone === 1 ? " file" : " files");
    $("fromCount").textContent = s.from_phone + (s.from_phone === 1 ? " file" : " files");
    render();
  }catch(e){
    if(!dead){ dead = true; render(); }
    // Deliberately keep polling: the window is reused when the app is opened
    // again, so this page has to be able to come back to life on its own.
  }
}

async function call(path){ try{ await fetch(path, {method:"POST"}); }catch(e){} }

$("copy").onclick = async () => {
  await navigator.clipboard.writeText(link);
  $("copy").textContent = "Copied";
  setTimeout(() => $("copy").textContent = "Copy link", 1400);
};
$("usbqr").onclick = () => {
  wantCableQr = !wantCableQr;
  $("usbqr").textContent = wantCableQr ? "Show wifi QR" : "Show cable QR";
  shownQr = false;
  render();
};
$("usbpair").onclick = async () => {
  const b = $("usbpair");
  b.disabled = true; b.textContent = "Pairing...";
  try{
    const s = await (await fetch("/api/usb", {method:"POST"})).json();
    if(s.error) $("usbhint").textContent = s.error;
  }catch(e){ $("usbhint").textContent = "Pairing failed - see ~/.filebridge/gui.log"; }
  b.disabled = false; b.textContent = "Pair over cable";
  poll();
};
$("stop").onclick  = async () => { await call("/api/stop");  poll(); };
$("start").onclick = async () => { await call("/api/start"); poll(); };
$("quit").onclick  = async () => {
  $("quit").disabled = true; $("quit").textContent = "Quitting...";
  await call("/api/quit");
  // The process is going away, so settle the UI ourselves rather than waiting
  // for a poll that will simply fail. Polling continues, so reopening the app
  // revives this same window.
  setTimeout(() => { dead = true; render(); $("quit").disabled = false;
                     $("quit").textContent = "Quit"; }, 700);
};
const openFolder = name => fetch("/api/open", {method:"POST",
  headers:{"Content-Type":"application/json"}, body:JSON.stringify({folder:name})});
$("openTo").onclick = () => openFolder("to-phone");
$("openFrom").onclick = () => openFolder("from-phone");

let timer = setInterval(poll, 2500);
poll();
</script></body></html>
"""

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>FileBridge - Connect</title>
<style>
body{margin:0;font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
 background:#0f1115;color:#e9eaec;display:flex;flex-direction:column;
 align-items:center;justify-content:center;min-height:100vh;text-align:center}
h1{font-size:20px;margin:0 0 4px}
.ver{color:#6b7280;font-size:12px;margin-bottom:22px}
img{width:min(74vw,360px);height:auto;background:#fff;padding:14px;border-radius:14px}
p{color:#9aa0a6;font-size:14px;max-width:420px;line-height:1.5;margin:20px 18px 0}
code{display:block;margin-top:14px;color:#4ade80;font-size:12.5px;word-break:break-all;
 background:#161922;padding:10px 12px;border-radius:9px;max-width:90vw}
</style></head><body>
<h1>Scan with your phone</h1>
<div class="ver">FileBridge __VER__</div>
<img src="/qr.png" alt="QR code">
<p>Use the phone's camera or the Scan button in the FileBridge app.
It connects automatically - nothing to type.</p>
<code>__LINK__</code>
</body></html>
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>FileBridge</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f1115">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Bridge">
<link rel="icon" href="__ICON__">
<link rel="apple-touch-icon" href="__ICON__">
<style>
:root{--bg:#fff;--fg:#111;--mut:#666;--line:#e6e6e6;--card:#fafafa;--acc:#0a7d32;--accbg:#e8f5ec}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e9eaec;--mut:#9aa0a6;--line:#23262d;--card:#161922;--acc:#4ade80;--accbg:#14261a}}
*{box-sizing:border-box}
body{margin:0;font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 12px}
h1{font-size:17px;margin:0 0 8px;display:flex;gap:8px;align-items:baseline}
h1 small{font-weight:400;color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=search],select{font:15px inherit;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg)}
input[type=search]{flex:1;min-width:140px}
.pill{font-size:12px;color:var(--mut);background:var(--card);border:1px solid var(--line);border-radius:99px;padding:5px 10px}
.chk{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--mut)}
ul{list-style:none;margin:0;padding:0 0 96px}
li{border-bottom:1px solid var(--line);padding:11px 12px;display:flex;gap:11px;align-items:flex-start}
li.done{opacity:.45}
a.nm{color:var(--fg);text-decoration:none;font-weight:600;font-size:15px;word-break:break-word;display:block}
a.nm:active{opacity:.6}
.meta{color:var(--mut);font-size:12.5px;margin-top:3px}
.tick{flex:0 0 auto;width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--mut);font-size:15px;line-height:28px;text-align:center;cursor:pointer;-webkit-tap-highlight-color:transparent}
li.done .tick{background:var(--accbg);border-color:var(--acc);color:var(--acc)}
.dir a{color:var(--acc);font-weight:600;text-decoration:none}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:9px 12px;display:flex;gap:9px;align-items:center;font-size:13px;color:var(--mut)}
button{font:14px inherit;padding:9px 13px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
#empty{padding:26px 14px;color:var(--mut)}
#up{display:none}
</style></head><body>
<header>
  <h1>FileBridge <small id="crumb"></small></h1>
  <div class="row">
    <input id="q" type="search" placeholder="Search..." autocomplete="off">
    <select id="sort">
      <option value="size">Smallest first</option>
      <option value="-size">Largest first</option>
      <option value="name">Name</option>
      <option value="-mtime">Newest</option>
      <option value="dur">Shortest</option>
    </select>
  </div>
  <div class="row" style="margin-top:8px">
    <label class="chk"><input type="checkbox" id="hide"> Hide taken</label>
    <span class="pill" id="stat"></span>
  </div>
</header>

<ul id="list"></ul>
<div id="empty" hidden>Nothing here.</div>

<footer>
  <button id="upbtn">Send from phone</button>
  <span id="note"></span>
  <input id="up" type="file" multiple>
</footer>

<script>
const T="__TOKEN__";
let cwd="", data={files:[],dirs:[]};

const q=document.getElementById('q'), sortSel=document.getElementById('sort'),
      hide=document.getElementById('hide'), list=document.getElementById('list'),
      stat=document.getElementById('stat'), crumb=document.getElementById('crumb'),
      empty=document.getElementById('empty'), note=document.getElementById('note');

function api(p){return p+(p.includes('?')?'&':'?')+'t='+encodeURIComponent(T)}

async function load(path){
  note.textContent='loading...';
  try{
    const r=await fetch(api('/api/list?path='+encodeURIComponent(path||'')));
    data=await r.json();
  }catch(e){note.textContent='connection lost';return}
  if(data.error){note.textContent=data.error;return}
  cwd=data.cwd||'';
  crumb.textContent=cwd?('/'+cwd):'';
  note.textContent='';
  updateStat();
  render();
}

function updateStat(){
  stat.textContent=data.done_count+' / '+data.count+' taken · '+
                   data.pending_h+' left of '+data.total_h;
}

// Rows are built with DOM nodes and textContent, never innerHTML: every name
// here comes off the filesystem, so it is not content to trust into markup.
function row(cls){
  const li=document.createElement('li');
  if(cls) li.className=cls;
  return li;
}

function render(){
  const term=q.value.trim().toLowerCase();
  let rows=data.files.filter(f=>!(hide.checked&&f.done));
  if(term) rows=rows.filter(f=>(f.pretty+' '+f.name).toLowerCase().includes(term));

  const s=sortSel.value;
  rows.sort((a,b)=>{
    if(s==='size')return a.size-b.size;
    if(s==='-size')return b.size-a.size;
    if(s==='-mtime')return b.mtime-a.mtime;
    if(s==='dur')return (a.duration||1e9)-(b.duration||1e9);
    return a.pretty.localeCompare(b.pretty);
  });

  while(list.firstChild) list.removeChild(list.firstChild);

  if(data.parent!==null&&data.parent!==undefined){
    const li=row('dir'), a=document.createElement('a');
    a.href='#'; a.textContent='⬆ up a folder';
    a.onclick=e=>{e.preventDefault();load(data.parent==='.'?'':data.parent)};
    li.appendChild(a); list.appendChild(li);
  }

  data.dirs.forEach(d=>{
    const li=row('dir'), a=document.createElement('a');
    a.href='#'; a.textContent='\u{1F4C1} '+d.name;
    a.onclick=e=>{e.preventDefault();load(d.path)};
    li.appendChild(a); list.appendChild(li);
  });

  rows.forEach(f=>{
    const li=row(f.done?'done':'');

    const tick=document.createElement('div');
    tick.className='tick';
    tick.textContent=f.done?'✓':'';
    tick.onclick=()=>mark(f,!f.done);

    const wrap=document.createElement('div');
    wrap.style.flex='1';

    const a=document.createElement('a');
    a.className='nm';
    a.href=api('/file?path='+encodeURIComponent(f.path));
    a.textContent=f.pretty;
    // Tapping starts the download; mark it so a long list stays navigable.
    // The tick stays manual so you can correct a mistake.
    a.addEventListener('click',()=>mark(f,true));

    const meta=document.createElement('div');
    meta.className='meta';
    meta.textContent=[f.size_h,f.duration_h].filter(Boolean).join(' · ');

    wrap.appendChild(a); wrap.appendChild(meta);
    li.appendChild(tick); li.appendChild(wrap);
    list.appendChild(li);
  });

  empty.hidden=rows.length>0||data.dirs.length>0;
}

async function mark(f,done){
  f.done=done;
  data.done_count=data.files.filter(x=>x.done).length;
  data.pending_h='';
  updateStat();
  render();
  try{
    await fetch(api('/api/mark'),{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:f.path,done:done})});
  }catch(e){note.textContent='could not save mark'}
}

document.getElementById('upbtn').onclick=()=>document.getElementById('up').click();
document.getElementById('up').onchange=async e=>{
  const files=[...e.target.files]; if(!files.length)return;
  const fd=new FormData(); files.forEach(f=>fd.append('f',f,f.name));
  note.textContent='sending '+files.length+' file(s)...';
  try{
    const r=await fetch(api('/api/upload'),{method:'POST',body:fd});
    const j=await r.json();
    note.textContent=j.ok?('saved to '+j.folder):(j.error||'failed');
    load(cwd);
  }catch(err){note.textContent='upload failed'}
  e.target.value='';
};

q.oninput=render; sortSel.onchange=render; hide.onchange=render;
load('');
</script></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Share a folder with your phone.")
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                        help="folder to serve (default ~/FileBridge)")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--token", default=None, help="reuse a key instead of generating one")
    parser.add_argument("--wired-port", type=int, default=0,
                        help="loopback port behind adb reverse (default: --port + 1)")
    parser.add_argument("--phone-port", type=int, default=0,
                        help="port the phone dials on its own loopback "
                             "(default: same as --port)")
    parser.add_argument("--no-wired", action="store_true",
                        help="skip the cable entirely, and never start adb")
    args = parser.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    if root == DEFAULT_ROOT:
        # First run creates the bridge: drop things in to-phone/ to fetch them
        # from the phone; anything the phone sends arrives in from-phone/.
        for sub in (OUTBOX_NAME, INBOX_NAME):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
    if not os.path.isdir(root):
        sys.exit("not a folder: " + root)

    load_state()
    token = args.token or secrets.token_urlsafe(9)

    # The Finder Quick Actions ("Copy to Phone") need to know where to put
    # things, and the folder is a command-line argument rather than a constant.
    # Recording it here is what lets them work for someone serving a folder
    # other than ~/FileBridge.
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "root"), "w", encoding="utf-8") as handle:
            handle.write(root)
    except OSError:
        pass

    try:
        server = Bridge(("0.0.0.0", args.port), Handler, root, token)
    except OSError as error:
        sys.exit("cannot bind port " + str(args.port) + ": " + str(error) +
                 "\nSomething else is using it - try --port 8010")

    # ---- the cable
    #
    # A SECOND listener, bound to loopback and flagged wired. It has to be a
    # separate socket rather than the same one: `adb reverse` delivers the
    # phone's requests from 127.0.0.1, and the only thing that can then tell a
    # phone from this Mac is which socket it arrived on. See Handler._local().
    wired = None
    if not args.no_wired:
        USB["adb"] = find_adb()
        USB["phone_port"] = args.phone_port or args.port
        USB["host_port"] = args.wired_port or (args.port + 1)
        why = ""
        if USB["host_port"] == args.port:
            why = "--wired-port must differ from --port"
        elif port_busy(USB["host_port"]):
            why = "something already answers on " + str(USB["host_port"])
        if why:
            print("  cable off:", why)
        else:
            try:
                wired = Bridge(("127.0.0.1", USB["host_port"]), Handler, root,
                               token, wired=True)
            except OSError as error:
                print("  cable off: cannot bind", USB["host_port"], "-", error)
            else:
                USB["on"] = True
                threading.Thread(target=wired.serve_forever, daemon=True).start()
                if USB["adb"]:
                    threading.Thread(target=usb_watch, daemon=True).start()

    url = "http://" + lan_ip() + ":" + str(args.port) + "/?t=" + token
    print("")
    print("  FileBridge", APP_VERSION, "serving:", root)
    print("  Phone -> Mac lands:", os.path.join(root, INBOX_NAME))
    print("  Mac -> phone: put files in", os.path.join(root, OUTBOX_NAME))
    print("")
    print("  OPEN THIS ON YOUR PHONE:")
    print("  " + url)
    print("")
    print("  Same Wi-Fi required. The key keeps other devices on the network out.")
    if USB["on"]:
        print("")
        print("  OR OVER THE CABLE (no wifi, and a VPN cannot swallow it):")
        print("  " + usb_base() + "/?t=" + token)
        if USB["adb"]:
            print("  Press \"Pair over cable\" in the panel, or by hand:")
            print("  " + USB["adb"] + " reverse tcp:" + str(USB["phone_port"]) +
                  " tcp:" + str(USB["host_port"]))
        else:
            print("  No adb found, so nothing is tunnelling yet. Install")
            print("  Android platform-tools, or run with --no-wired.")
    print("  Ctrl-C to stop.")
    print("")
    # Under nohup / a pipe, stdout is block-buffered and the URL above would
    # sit unseen in the buffer until the process exits.
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        save_state()


if __name__ == "__main__":
    main()
