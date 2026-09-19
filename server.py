#!/usr/bin/env python3
"""
server.py — LAN web server for the idea-review step.

Serves a small browser UI (web/) plus a JSON API so several reviewers on the
same network can review different groups at once. Standard library only.

Design notes:
  * Access is gated by a shared passcode generated at server start.
  * A group can be open by one reviewer at a time (GroupRegistry leases);
    a reviewer's heartbeat keeps the lease alive, and it expires if they
    vanish, so a closed laptop can't lock a group forever.
  * Group names from the URL are only ever used as dictionary keys into the
    registry — never to build a file path.
  * Plain HTTP: the passcode and transcripts are unencrypted on the LAN.
    Intended for a trusted private network.
"""

import hmac
import json
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from review_core import ReviewSession, ReviewState, load_idea_rows

DEFAULT_PORT = 8765
LEASE_SECONDS = 180
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 20_000
MAX_NAME_CHARS = 40
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
COOKIE_NAME = "gct_session"
# No I/L/O/0/1 — easy to read aloud and type.
PASSCODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PASSCODE_LENGTH = 6

LOCAL_OWNER = "local"


def resource_path(relative: str) -> Path:
    """Locate bundled resources both from source and from a PyInstaller build."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def generate_passcode() -> str:
    return "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(PASSCODE_LENGTH))


def lan_addresses() -> list[str]:
    """Best-effort list of this machine's private IPv4 addresses."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        # A UDP "connect" sends nothing; it just asks the OS which interface
        # would be used to reach an outside address.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith(("127.", "169.254.", "0.")))


# --------------------------------------------------------------------------
# Group leases
# --------------------------------------------------------------------------

class LockLost(Exception):
    """The caller no longer holds the group (someone else took it over)."""


class _Group:
    def __init__(self, name: str, csv_path: Path, total: int):
        self.name = name
        self.csv_path = csv_path
        self.total = total
        self.lock = threading.RLock()   # serializes ReviewSession use
        self.session = None
        self.owner = None
        self.label = None
        self.sticky = False             # local desktop owner never goes stale
        self.last_seen = 0.0


class GroupRegistry:
    """Which groups exist, who is reviewing each, and their sessions.

    Shared by the web server and the host's desktop window so the two can't
    open the same group at once.
    """

    def __init__(self, groups: dict, lease_seconds: float = LEASE_SECONDS, clock=time.monotonic):
        self._lease = lease_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._groups = {}
        for name, csv_path in groups.items():
            csv_path = Path(csv_path)
            self._groups[name] = _Group(name, csv_path, len(load_idea_rows(csv_path)))

    # -- lookup -------------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._groups

    def names(self) -> list[str]:
        return list(self._groups)

    # -- leases -------------------------------------------------------------

    def _stale(self, g: _Group) -> bool:
        return g.owner is not None and not g.sticky and self._clock() - g.last_seen > self._lease

    def acquire(self, name: str, owner: str, label: str, sticky: bool = False):
        """Try to take a group. Returns (True, label) on success or
        (False, holder_label) if someone else has it."""
        with self._lock:
            g = self._groups[name]
            if g.owner is not None and g.owner != owner and not self._stale(g):
                return False, g.label
            if g.owner != owner:
                g.session = None            # new holder starts from what's on disk
            g.owner, g.label, g.sticky = owner, label, sticky
            g.last_seen = self._clock()
            return True, label

    def touch(self, name: str, owner: str) -> bool:
        """Refresh the lease. False if the caller isn't the holder."""
        with self._lock:
            g = self._groups[name]
            if g.owner != owner:
                return False
            g.last_seen = self._clock()
            return True

    def release(self, name: str, owner: str):
        with self._lock:
            g = self._groups[name]
            if g.owner == owner:
                g.owner = g.label = g.session = None
                g.sticky = False

    def release_matching(self, predicate):
        """Release every group whose owner satisfies predicate(owner)."""
        with self._lock:
            for g in self._groups.values():
                if g.owner is not None and predicate(g.owner):
                    g.owner = g.label = g.session = None
                    g.sticky = False

    def status(self, name: str, owner) -> str:
        with self._lock:
            g = self._groups[name]
            if g.owner is None:
                return "free"
            if g.owner == owner:
                return "yours"
            return "free" if self._stale(g) else "in_use"

    def holders(self) -> list[tuple[str, str]]:
        """[(group name, holder label)] for groups someone currently holds."""
        with self._lock:
            return [(g.name, g.label) for g in self._groups.values()
                    if g.owner is not None and not self._stale(g)]

    # -- sessions -----------------------------------------------------------

    def with_session(self, name: str, owner: str, fn):
        """Run fn(session) while holding the group, refreshing the lease.
        Raises LockLost if `owner` isn't the holder."""
        if not self.touch(name, owner):
            raise LockLost(name)
        g = self._groups[name]
        with g.lock:
            if g.session is None:
                g.session = ReviewSession(g.csv_path, simplified=True)
            return fn(g.session)

    def describe(self, owner) -> list[dict]:
        out = []
        for name, g in self._groups.items():
            try:
                state = ReviewState(g.csv_path.with_name(g.csv_path.stem + "_review_state.json"))
                decided = sum(1 for i in range(g.total) if state.is_decided(i))
            except (OSError, ValueError):
                decided = 0
            status = self.status(name, owner)
            holder = None
            if status == "in_use":
                with self._lock:
                    holder = g.label
            out.append({
                "name": name,
                "idea_rows": g.total,
                "decided": decided,
                "status": status,
                "holder": holder,
            })
        return out


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
_GROUP_ROUTE = re.compile(r"^/api/groups/([^/]+)/(open|action|heartbeat|release)$")
_ACTIONS = {"save", "drop", "discard", "combine", "split", "back"}


class _HTTPError(Exception):
    def __init__(self, status: int, message: str, extra: dict = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def _clean_label(raw) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:MAX_NAME_CHARS]


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # http.server enables SO_REUSEADDR, which on Windows lets a second server
    # silently share a busy port; we want "port in use" to fail so we can
    # move to the next one.
    allow_reuse_address = False

    def __init__(self, address, registry: GroupRegistry, passcode: str, clock=time.monotonic):
        super().__init__(address, _Handler)
        self.registry = registry
        self.passcode = passcode
        self.clock = clock
        self._auth_lock = threading.Lock()
        self._reviewers = {}        # token -> label
        self._reviewer_count = 0
        self._failures = {}         # client ip -> [monotonic timestamps]
        self.last_error = None

    # -- reviewers / login throttling ---------------------------------------

    def new_reviewer(self, requested_name: str) -> tuple[str, str]:
        with self._auth_lock:
            self._reviewer_count += 1
            label = requested_name or f"Reviewer {self._reviewer_count}"
            token = secrets.token_urlsafe(32)
            self._reviewers[token] = label
            return token, label

    def reviewer_label(self, token):
        with self._auth_lock:
            return self._reviewers.get(token)

    def reviewer_labels(self) -> list[str]:
        with self._auth_lock:
            return list(self._reviewers.values())

    def login_retry_after(self, ip: str) -> int:
        """Seconds until `ip` may try again, or 0 if it may try now."""
        now = self.clock()
        with self._auth_lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
            self._failures[ip] = recent
            if len(recent) >= LOGIN_MAX_FAILURES:
                return max(1, int(LOGIN_WINDOW_SECONDS - (now - recent[0])) + 1)
            return 0

    def record_login_failure(self, ip: str):
        with self._auth_lock:
            self._failures.setdefault(ip, []).append(self.clock())

    def server_bind(self):
        # http.server's version calls socket.getfqdn(), a reverse-DNS lookup
        # that takes over a second on some Windows networks and would freeze
        # the host window when the server starts. We don't need the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        # The packaged app is windowed (no stderr); remember the error instead.
        self.last_error = sys.exc_info()[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "GroupCreativityReview"
    sys_version = ""

    def log_message(self, fmt, *args):   # windowed exe has no stderr to write to
        pass

    # -- plumbing -----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, headers: dict = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: dict, headers: dict = None):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _require_auth(self) -> tuple[str, str]:
        token = self._token()
        label = self.server.reviewer_label(token) if token else None
        if label is None:
            raise _HTTPError(401, "auth")
        return token, label

    def _read_json(self) -> dict:
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            raise _HTTPError(403, "cross-origin request refused")
        if self.headers.get_content_type() != "application/json":
            raise _HTTPError(415, "expected application/json")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _HTTPError(411, "Content-Length required")
        if length < 0 or length > MAX_BODY_BYTES:
            raise _HTTPError(413, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise _HTTPError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise _HTTPError(400, "expected a JSON object")
        return body

    def _dispatch(self, fn):
        try:
            fn()
        except _HTTPError as exc:
            self._send_json(exc.status, {"error": exc.message, **exc.extra})
        except LockLost:
            self._send_json(409, {"error": "lost", "message": "Another reviewer now has this group."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.server.last_error = sys.exc_info()[1]
            try:
                self._send_json(500, {"error": "internal"})
            except OSError:
                pass

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def _get(self):
        path = urlparse(self.path).path
        if path in _STATIC:
            filename, ctype = _STATIC[path]
            try:
                body = (resource_path("web") / filename).read_bytes()
            except OSError:
                raise _HTTPError(404, "not found")
            self._send(200, body, ctype)
        elif path == "/favicon.ico":       # browsers ask; answering avoids a console error
            self._send(204, b"", "image/x-icon")
        elif path == "/api/me":
            token = self._token()
            label = self.server.reviewer_label(token) if token else None
            self._send_json(200, {"authenticated": label is not None, "name": label})
        elif path == "/api/groups":
            token, _label = self._require_auth()
            self._send_json(200, {"groups": self.server.registry.describe("web:" + token)})
        else:
            raise _HTTPError(404, "not found")

    def _post(self):
        path = urlparse(self.path).path
        # Always consume the body first: replying to a POST with unread data
        # can reset the connection on Windows before the client sees the reply.
        body = self._read_json()
        if path == "/api/login":
            return self._login(body)
        match = _GROUP_ROUTE.match(path)
        if not match:
            raise _HTTPError(404, "not found")
        token, label = self._require_auth()
        name, verb = unquote(match.group(1)), match.group(2)
        registry = self.server.registry
        if not registry.has(name):           # whitelist: never touches the filesystem
            raise _HTTPError(404, "unknown group")
        owner = "web:" + token

        if verb == "open":
            ok, holder = registry.acquire(name, owner, label)
            if not ok:
                raise _HTTPError(409, "in_use", {"holder": holder})
            snapshot = registry.with_session(name, owner, lambda s: s.snapshot())
            self._send_json(200, {"snapshot": snapshot, "group": name})
        elif verb == "heartbeat":
            if not registry.touch(name, owner):
                raise LockLost(name)
            self._send_json(200, {"ok": True})
        elif verb == "release":
            registry.release(name, owner)
            self._send_json(200, {"ok": True})
        else:
            self._action(name, owner, body)

    def _login(self, body: dict):
        ip = self.client_address[0]
        wait = self.server.login_retry_after(ip)
        if wait:
            raise _HTTPError(429, "too_many_attempts", {"retry_after": wait})
        supplied = body.get("passcode")
        if not isinstance(supplied, str):
            raise _HTTPError(400, "passcode required")
        expected = self.server.passcode.upper().encode("utf-8")
        if not hmac.compare_digest(supplied.strip().upper().encode("utf-8"), expected):
            self.server.record_login_failure(ip)
            raise _HTTPError(401, "wrong_passcode")
        token, label = self.server.new_reviewer(_clean_label(body.get("name")))
        cookie = f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Strict; Path=/"
        self._send_json(200, {"ok": True, "name": label}, {"Set-Cookie": cookie})

    def _action(self, name: str, owner: str, body: dict):
        action = body.get("action")
        if action not in _ACTIONS:
            raise _HTTPError(400, "unknown action")
        revision = body.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise _HTTPError(400, "revision required")

        def text(key):
            value = body.get(key)
            if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
                raise _HTTPError(400, f"{key} must be text of at most {MAX_TEXT_CHARS} characters")
            return value

        params = {}
        if action == "save":
            params["text"] = text("text")
        elif action == "split":
            params["before"], params["after"] = text("before"), text("after")

        def run(session):
            if session.revision != revision:
                return "stale", session.snapshot()
            try:
                if action == "save":
                    session.save_next(params["text"])
                elif action == "drop":
                    session.drop_fragment()
                elif action == "discard":
                    session.discard()
                elif action == "combine":
                    session.combine_next()
                elif action == "split":
                    session.split(params["before"], params["after"])
                elif action == "back":
                    session.back()
            except ValueError as exc:
                return "invalid", {"message": str(exc), "snapshot": session.snapshot()}
            return "ok", session.snapshot()

        outcome, payload = self.server.registry.with_session(name, owner, run)
        if outcome == "stale":
            raise _HTTPError(409, "stale", {"snapshot": payload})
        if outcome == "invalid":
            raise _HTTPError(400, "invalid", payload)
        self._send_json(200, {"snapshot": payload})


# --------------------------------------------------------------------------
# Start/stop wrapper used by the host window
# --------------------------------------------------------------------------

class ReviewServerHandle:
    def __init__(self, registry: GroupRegistry, port: int = DEFAULT_PORT, passcode: str = None):
        self.registry = registry
        self.requested_port = port
        self.passcode = passcode or generate_passcode()
        self.httpd = None
        self.thread = None

    @property
    def running(self) -> bool:
        return self.httpd is not None

    @property
    def port(self):
        return self.httpd.server_address[1] if self.httpd else None

    def start(self):
        if self.httpd:
            return
        last_error = None
        candidates = [0] if self.requested_port == 0 else range(self.requested_port, self.requested_port + 20)
        for candidate in candidates:
            try:
                self.httpd = ReviewHTTPServer(("0.0.0.0", candidate), self.registry, self.passcode)
                break
            except OSError as exc:
                last_error = exc
        else:
            raise last_error
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        )
        self.thread.start()

    def stop(self):
        if not self.httpd:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        self.registry.release_matching(lambda owner: owner.startswith("web:"))
        self.httpd = None
        self.thread = None

    def urls(self) -> list[str]:
        if not self.httpd:
            return []
        addresses = lan_addresses() or ["localhost"]
        return [f"http://{addr}:{self.port}" for addr in addresses]

    def reviewer_labels(self) -> list[str]:
        return self.httpd.reviewer_labels() if self.httpd else []
