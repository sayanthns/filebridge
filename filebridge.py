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
APP_VERSION = "1.11.0"
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".mp3", ".m4a"}
CHUNK = 256 * 1024
# Written whenever a phone (i.e. a non-localhost client) actually talks to us.
# The Mac window polls this to know a device connected and hide the QR.
CLIENTS_FILE = "/tmp/filebridge_clients.txt"

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


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------- server


class Bridge(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, root, token):
        self.root = os.path.realpath(root)
        self.token = token
        self.pool = ThreadPoolExecutor(max_workers=6)
        super().__init__(addr, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "FileBridge"

    def log_message(self, fmt, *args):
        if "/api/" in self.path or self.path.startswith("/file"):
            sys.stderr.write("  " + (fmt % args)[:110] + "\n")

    def _local(self):
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
            if self.client_address[0] not in ("127.0.0.1", "::1"):
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
            })

        if route in ("/connect", "/qr.png"):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "localhost only"}, HTTPStatus.FORBIDDEN)
            return self._connect_page() if route == "/connect" else self._qr_png()

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

        if parsed.path in ("/api/stop", "/api/start", "/api/quit", "/api/open"):
            # Local control surface for the Mac panel. Localhost only: these
            # act on this machine, so no phone may ever reach them.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "localhost only"}, HTTPStatus.FORBIDDEN)
            if parsed.path == "/api/start":
                PAUSED["on"] = False
                return self._json({"sharing": True})

            if parsed.path == "/api/quit":
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return self._json({"quitting": True})

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
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            return
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

    def _deep_link(self):
        base = "http://" + lan_ip() + ":" + str(self.server.server_address[1])
        return ("filebridge://c?u=" + urllib.parse.quote(base, safe="") +
                "&t=" + self.server.token)

    def _qr_png(self):
        """QR of the deep link, rendered by macOS CoreImage via JXA."""
        out = "/tmp/filebridge_qr.png"
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "qrgen.js")
        try:
            subprocess.run(["osascript", "-l", "JavaScript", script,
                            self._deep_link(), out, "760"],
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
  <div class="sub">Move files between this Mac and your phone over wifi</div>

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

  <div class="card" id="qrcard"></div>

  <div class="ver">version __VER__</div>
</div>

<script>
const $ = id => document.getElementById(id);
let link = "__LINK__";
let sharing = true, client = "", shownQr = false, dead = false;

function el(tag, cls, text){
  const n = document.createElement(tag);
  if(cls) n.className = cls;
  if(text) n.textContent = text;
  return n;
}

function render(){
  if(dead){
    $("dot").classList.remove("on");
    $("state").textContent = "Not running";
    $("link").textContent = "File Bridge has quit";
    ["start","stop","copy","quit"].forEach(id => $(id).style.display = "none");
    $("qrcard").replaceChildren(
      el("div", "qrtitle", "Sharing ended"),
      el("div", "hint", "Open File Bridge from the Dock or Launchpad and this " +
                        "page will reconnect by itself.")
    );
    return;
  }
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
               el("div", "ip", client),
               el("div", "hint", "Browse and transfer from the phone app."));
    card.replaceChildren(box);
    return;
  }
  // Back to the QR: either nothing has connected yet, or the phone left.
  if(!shownQr){
    shownQr = true;
    const img = el("img");
    img.id = "qr"; img.alt = "QR code to connect";
    img.src = "/qr.png?" + Date.now();
    const wrap = el("div", "qrwrap"); wrap.appendChild(img);
    card.replaceChildren(
      el("div", "qrtitle", "Scan with the File Bridge app"),
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
    link = s.link; sharing = s.sharing; client = s.client || "";
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

    try:
        server = Bridge(("0.0.0.0", args.port), Handler, root, token)
    except OSError as error:
        sys.exit("cannot bind port " + str(args.port) + ": " + str(error) +
                 "\nSomething else is using it - try --port 8002")

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
