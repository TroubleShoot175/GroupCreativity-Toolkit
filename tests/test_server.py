"""Tests for server.py: auth, group leases, the action API, and hardening.

Starts a real server on an ephemeral loopback port with synthetic session
data in a temp directory. Run from the repo root:
    python -m unittest discover -s tests
"""

import http.client
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
from test_review_core import make_session_dir, read_csv  # noqa: E402

PASSCODE = "ABC234"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Client:
    """Minimal cookie-keeping HTTP client (one 'browser')."""

    def __init__(self, port):
        self.port = port
        self.cookie = None

    def request(self, method, path, body=None, headers=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = content_type
        elif method == "POST":
            payload = b"{}"
            hdrs["Content-Type"] = content_type
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        data = None
        if resp.getheader("Content-Type", "").startswith("application/json"):
            data = json.loads(raw)
        return resp.status, data, resp, raw

    def login(self, passcode=PASSCODE, name=None):
        body = {"passcode": passcode}
        if name is not None:
            body["name"] = name
        status, data, resp, _ = self.request("POST", "/api/login", body)
        set_cookie = resp.getheader("Set-Cookie")
        if status == 200 and set_cookie:
            self.cookie = set_cookie.split(";")[0]
        return status, data, set_cookie

    def post(self, path, body=None):
        status, data, _resp, _raw = self.request("POST", path, body)
        return status, data

    def get(self, path):
        status, data, _resp, _raw = self.request("GET", path)
        return status, data


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.g1 = make_session_dir(root, "G91")
        self.g2 = make_session_dir(root, "G92")
        self.clock = FakeClock()
        self.registry = server.GroupRegistry(
            {"G91": self.g1, "G92": self.g2}, lease_seconds=180, clock=self.clock)
        self.handle = server.ReviewServerHandle(self.registry, port=0, passcode=PASSCODE)
        self.handle.start()
        self.addCleanup(self.handle.stop)
        self.port = self.handle.port

    def client(self, name=None, login=True):
        c = Client(self.port)
        if login:
            status, _data, _cookie = c.login(name=name)
            self.assertEqual(status, 200)
        return c

    def open_group(self, client, group="G91"):
        status, data = client.post(f"/api/groups/{group}/open")
        self.assertEqual(status, 200, data)
        return data["snapshot"]

    def act(self, client, snapshot, action, group="G91", **extra):
        return client.post(f"/api/groups/{group}/action",
                           {"action": action, "revision": snapshot["revision"], **extra})


class StaticAndHeaderTests(ServerTestCase):
    def test_static_files_are_served(self):
        c = Client(self.port)
        for path, ctype in [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")]:
            status, _d, resp, raw = c.request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertTrue(resp.getheader("Content-Type").startswith(ctype))
            self.assertTrue(raw)

    def test_security_headers(self):
        _s, _d, resp, _raw = Client(self.port).request("GET", "/")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        csp = resp.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)

    def test_only_whitelisted_static_paths_are_served(self):
        c = Client(self.port)
        for path in ["/server.py", "/web/index.html", "/../server.py", "/%2e%2e/server.py",
                     "/review_core.py", "/app.js/../server.py"]:
            status, _d = c.get(path)
            self.assertEqual(status, 404, path)

    def test_favicon_request_is_quietly_answered(self):
        status, _d, _resp, raw = Client(self.port).request("GET", "/favicon.ico")
        self.assertEqual((status, raw), (204, b""))


class AuthTests(ServerTestCase):
    def test_api_requires_login(self):
        c = Client(self.port)
        self.assertEqual(c.get("/api/groups")[0], 401)
        self.assertEqual(c.post("/api/groups/G91/open")[0], 401)
        self.assertEqual(c.post("/api/groups/G91/action", {"action": "back", "revision": 0})[0], 401)
        status, data = c.get("/api/me")
        self.assertEqual((status, data["authenticated"]), (200, False))

    def test_bogus_cookie_is_rejected(self):
        c = Client(self.port)
        c.cookie = "gct_session=not-a-real-token"
        self.assertEqual(c.get("/api/groups")[0], 401)
        c.cookie = "\x00garbage;;;"
        self.assertEqual(c.get("/api/groups")[0], 401)

    def test_wrong_passcode_rejected_without_cookie(self):
        status, data, cookie = Client(self.port).login(passcode="ZZZZZZ")
        self.assertEqual(status, 401)
        self.assertIsNone(cookie)

    def test_login_is_case_and_whitespace_tolerant_and_cookie_flags(self):
        status, data, cookie = Client(self.port).login(passcode="  abc234 ")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_passcode_must_be_text(self):
        c = Client(self.port)
        for bad in [123, None, ["ABC234"], {"a": 1}]:
            status, _d, _r, _raw = c.request("POST", "/api/login", {"passcode": bad})
            self.assertEqual(status, 400)

    def test_repeated_failures_are_rate_limited_even_for_correct_code(self):
        c = Client(self.port)
        for _ in range(server.LOGIN_MAX_FAILURES):
            self.assertEqual(c.login(passcode="WRONG1")[0], 401)
        status, data, _cookie = c.login(passcode=PASSCODE)
        self.assertEqual(status, 429)
        self.assertGreaterEqual(data["retry_after"], 1)

    def test_reviewer_labels(self):
        a = self.client(name="  Dana\x07 ")
        b = self.client()
        self.assertEqual(a.get("/api/me")[1]["name"], "Dana")
        self.assertRegex(b.get("/api/me")[1]["name"], r"^Reviewer \d+$")
        long = Client(self.port)
        long.login(name="x" * 200)
        self.assertEqual(len(long.get("/api/me")[1]["name"]), server.MAX_NAME_CHARS)


class GroupLeaseTests(ServerTestCase):
    def test_group_list(self):
        c = self.client()
        status, data = c.get("/api/groups")
        self.assertEqual(status, 200)
        groups = {g["name"]: g for g in data["groups"]}
        self.assertEqual(set(groups), {"G91", "G92"})
        self.assertEqual(groups["G91"]["idea_rows"], 5)
        self.assertEqual(groups["G91"]["decided"], 0)
        self.assertEqual(groups["G91"]["status"], "free")

    def test_second_reviewer_is_blocked_and_sees_holder(self):
        a, b = self.client(name="Alice"), self.client(name="Bob")
        self.open_group(a)
        status, data = b.post("/api/groups/G91/open")
        self.assertEqual((status, data["error"], data["holder"]), (409, "in_use", "Alice"))

        by_name = {g["name"]: g for g in b.get("/api/groups")[1]["groups"]}
        self.assertEqual((by_name["G91"]["status"], by_name["G91"]["holder"]), ("in_use", "Alice"))
        mine = {g["name"]: g for g in a.get("/api/groups")[1]["groups"]}
        self.assertEqual(mine["G91"]["status"], "yours")

    def test_non_holder_cannot_act(self):
        a, b = self.client(), self.client()
        snap = self.open_group(a)
        status, data = self.act(b, snap, "discard")
        self.assertEqual((status, data["error"]), (409, "lost"))
        self.assertEqual(self.act(a, snap, "back")[0], 200)     # holder unaffected

    def test_different_groups_can_be_reviewed_at_once(self):
        a, b = self.client(), self.client()
        sa, sb = self.open_group(a, "G91"), self.open_group(b, "G92")
        self.assertEqual(self.act(a, sa, "save", "G91", text="one")[0], 200)
        self.assertEqual(self.act(b, sb, "save", "G92", text="two")[0], 200)
        self.assertEqual(read_csv(self.g1.with_name("transcript_ideas.csv"))[0]["content"], "one")
        self.assertEqual(read_csv(self.g2.with_name("transcript_ideas.csv"))[0]["content"], "two")

    def test_release_frees_group(self):
        a, b = self.client(), self.client()
        self.open_group(a)
        self.assertEqual(a.post("/api/groups/G91/release")[0], 200)
        self.assertEqual(b.post("/api/groups/G91/open")[0], 200)

    def test_release_by_non_holder_does_nothing(self):
        a, b = self.client(), self.client()
        self.open_group(a)
        b.post("/api/groups/G91/release")
        self.assertEqual(b.post("/api/groups/G91/open")[0], 409)

    def test_lease_expires_and_old_holder_is_told(self):
        a, b = self.client(), self.client()
        snap = self.open_group(a)
        self.clock.advance(181)
        self.open_group(b)                                       # takes over the stale lease
        status, data = self.act(a, snap, "save", text="late")
        self.assertEqual((status, data["error"]), (409, "lost"))
        self.assertEqual(a.post("/api/groups/G91/heartbeat")[0], 409)

    def test_heartbeat_and_actions_keep_lease_alive(self):
        a, b = self.client(), self.client()
        snap = self.open_group(a)
        self.clock.advance(120)
        self.assertEqual(a.post("/api/groups/G91/heartbeat")[0], 200)
        self.clock.advance(120)
        self.assertEqual(b.post("/api/groups/G91/open")[0], 409)   # only 120s since heartbeat
        self.assertEqual(self.act(a, snap, "save", text="x")[0], 200)

    def test_new_holder_resumes_from_disk(self):
        a, b = self.client(), self.client()
        snap = self.open_group(a)
        _s, data = self.act(a, snap, "save", text="first")
        a.post("/api/groups/G91/release")
        snap_b = self.open_group(b)
        self.assertEqual(snap_b["index"], 2)
        self.assertEqual(snap_b["revision"], 0)

    def test_same_reviewer_reopening_keeps_midsplit_state(self):
        a = self.client()
        snap = self.open_group(a)
        _s, data = self.act(a, snap, "split", before="left", after="right")
        again = self.open_group(a)                               # page reload
        self.assertEqual(again["edit_text"], "left")
        self.assertEqual(again["next"]["kind"], "split_half")

    def test_stop_releases_web_locks(self):
        a = self.client()
        self.open_group(a)
        self.assertEqual(self.registry.holders()[0][0], "G91")
        self.handle.stop()
        self.assertEqual(self.registry.holders(), [])

    def test_local_owner_never_goes_stale(self):
        ok, _label = self.registry.acquire("G92", server.LOCAL_OWNER, "Host", sticky=True)
        self.assertTrue(ok)
        self.clock.advance(10_000)
        b = self.client()
        self.assertEqual(b.post("/api/groups/G92/open")[0], 409)
        self.registry.release("G92", server.LOCAL_OWNER)
        self.assertEqual(b.post("/api/groups/G92/open")[0], 200)

    def test_web_owner_blocks_local_owner(self):
        a = self.client(name="Alice")
        self.open_group(a, "G92")
        ok, holder = self.registry.acquire("G92", server.LOCAL_OWNER, "Host", sticky=True)
        self.assertEqual((ok, holder), (False, "Alice"))


class ActionTests(ServerTestCase):
    def test_full_review_flow_writes_simplified_csv(self):
        c = self.client()
        s = self.open_group(c)
        self.assertEqual(s["progress_text"], "Row 1 / 5")
        self.assertFalse(s["can_back"])

        _st, d = self.act(c, s, "save", text="intro idea")
        s = d["snapshot"]
        # split the second row into two ideas, each confirmed in turn
        _st, d = self.act(c, s, "split", before="add more parking", after="cheaper food")
        s = d["snapshot"]
        self.assertEqual((s["edit_text"], s["next"]["kind"]), ("add more parking", "split_half"))
        _st, d = self.act(c, d["snapshot"], "save", text="more parking")
        _st, d = self.act(c, d["snapshot"], "save", text="cheaper food")
        s = d["snapshot"]
        # combine the next two rows and save them as one
        _st, d = self.act(c, s, "combine")
        s = d["snapshot"]
        self.assertEqual(s["original_text"], "Longer library hours so students can study late")
        _st, d = self.act(c, s, "save", text="Late library hours")
        s = d["snapshot"]
        _st, d = self.act(c, s, "discard")
        s = d["snapshot"]
        self.assertTrue(s["done"])

        rows = read_csv(self.g1.with_name("transcript_ideas.csv"))
        self.assertEqual(list(rows[0].keys()), ["time", "speaker", "phase", "content"])
        self.assertEqual([r["content"] for r in rows],
                         ["intro idea", "more parking", "cheaper food", "Late library hours"])
        self.assertEqual(rows[1]["time"], rows[2]["time"])
        self.assertEqual(rows[1]["speaker"], rows[2]["speaker"])

        # and back undoes the discard
        _st, d = self.act(c, s, "back")
        self.assertFalse(d["snapshot"]["done"])
        self.assertEqual(d["snapshot"]["edit_text"], "Free bikes")

    def test_drop_fragment(self):
        c = self.client()
        s = self.open_group(c)
        _st, d = self.act(c, s, "split", before="keep", after="drop")
        _st, d = self.act(c, d["snapshot"], "save", text="keep")
        _st, d = self.act(c, d["snapshot"], "drop")
        rows = read_csv(self.g1.with_name("transcript_ideas.csv"))
        self.assertEqual([r["content"] for r in rows], ["keep"])

    def test_stale_revision_is_rejected_and_changes_nothing(self):
        c = self.client()
        s = self.open_group(c)
        self.assertEqual(self.act(c, s, "save", text="once")[0], 200)
        status, data = self.act(c, s, "save", text="twice")         # double-submit / stale tab
        self.assertEqual((status, data["error"]), (409, "stale"))
        self.assertEqual(data["snapshot"]["index"], 2)
        rows = read_csv(self.g1.with_name("transcript_ideas.csv"))
        self.assertEqual([r["content"] for r in rows], ["once"])

    def test_invalid_actions(self):
        c = self.client()
        s = self.open_group(c)
        rev = s["revision"]
        path = "/api/groups/G91/action"
        self.assertEqual(c.post(path, {"action": "explode", "revision": rev})[0], 400)
        self.assertEqual(c.post(path, {"action": "save", "text": "x"})[0], 400)                    # no revision
        self.assertEqual(c.post(path, {"action": "save", "text": "x", "revision": "0"})[0], 400)
        self.assertEqual(c.post(path, {"action": "save", "text": "x", "revision": True})[0], 400)
        self.assertEqual(c.post(path, {"action": "save", "revision": rev})[0], 400)                # no text
        self.assertEqual(c.post(path, {"action": "save", "text": 5, "revision": rev})[0], 400)
        status, data = c.post(path, {"action": "save", "text": "   ", "revision": rev})
        self.assertEqual((status, data["error"]), (400, "invalid"))
        self.assertIn("snapshot", data)
        status, data = c.post(path, {"action": "split", "before": "a", "after": " ", "revision": rev})
        self.assertEqual(status, 400)
        too_long = "x" * (server.MAX_TEXT_CHARS + 1)
        self.assertEqual(c.post(path, {"action": "save", "text": too_long, "revision": rev})[0], 400)
        # nothing above changed anything
        self.assertEqual(self.act(c, s, "save", text="fine")[0], 200)

    def test_markup_in_text_round_trips_as_data(self):
        c = self.client()
        s = self.open_group(c)
        payload = '<script>alert("x")</script> & <img src=x onerror=1>'
        _st, d = self.act(c, s, "save", text=payload)
        rows = read_csv(self.g1.with_name("transcript_ideas.csv"))
        self.assertEqual(rows[0]["content"], payload)


class HardeningTests(ServerTestCase):
    def test_unknown_and_traversal_group_names(self):
        c = self.client()
        for name in ["NOPE", "..", "%2e%2e", "..%2fG91", "G91%2f..%2fG92", "G91%00", "g91"]:
            status, _d = c.post(f"/api/groups/{name}/open")
            self.assertEqual(status, 404, name)
        self.assertEqual(c.post("/api/groups/G91/../G92/open")[0], 404)

    def test_wrong_content_type_and_cross_origin_refused(self):
        c = self.client()
        status, _d, _r, _raw = c.request("POST", "/api/groups/G91/open", b"{}", content_type="text/plain")
        self.assertEqual(status, 415)
        status, _d, _r, _raw = c.request(
            "POST", "/api/groups/G91/open", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _d, _r, _raw = c.request(
            "POST", "/api/groups/G91/open", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_malformed_bodies(self):
        c = self.client()
        for raw in [b"not json", b"[1,2]", b'"str"', b"\xff\xfe"]:
            status, _d, _r, _raw = c.request("POST", "/api/groups/G91/open", raw)
            self.assertEqual(status, 400, raw)

    def _raw_exchange(self, request_bytes):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(request_bytes)
            chunks = []
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks)

    def test_oversized_body_rejected_before_reading(self):
        request = (
            b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: 70000\r\nConnection: close\r\n\r\n"
        )
        self.assertTrue(self._raw_exchange(request).startswith(b"HTTP/1.0 413"))

    def test_post_without_content_length_rejected(self):
        request = b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
        self.assertTrue(self._raw_exchange(request).startswith(b"HTTP/1.0 411"))

    def test_server_survives_garbage(self):
        self._raw_exchange(b"\x00\x01garbage\r\n\r\n")
        self._raw_exchange(b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual(Client(self.port).get("/")[0], 200)


class PortAndUrlTests(unittest.TestCase):
    def test_busy_port_falls_back_to_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = server.GroupRegistry({"G91": make_session_dir(Path(tmp), "G91")})
            first = server.ReviewServerHandle(registry, port=0)
            first.start()
            self.addCleanup(first.stop)
            second = server.ReviewServerHandle(registry, port=first.port)
            second.start()
            self.addCleanup(second.stop)
            self.assertNotEqual(first.port, second.port)
            self.assertEqual(Client(first.port).get("/")[0], 200)
            self.assertEqual(Client(second.port).get("/")[0], 200)

    def test_passcode_shape_and_urls(self):
        code = server.generate_passcode()
        self.assertEqual(len(code), server.PASSCODE_LENGTH)
        self.assertTrue(set(code) <= set(server.PASSCODE_ALPHABET))
        for ambiguous in "ILO01":
            self.assertNotIn(ambiguous, server.PASSCODE_ALPHABET)
        with tempfile.TemporaryDirectory() as tmp:
            registry = server.GroupRegistry({"G91": make_session_dir(Path(tmp), "G91")})
            handle = server.ReviewServerHandle(registry, port=0)
            self.assertEqual(handle.urls(), [])
            handle.start()
            self.addCleanup(handle.stop)
            self.assertTrue(handle.urls())
            self.assertTrue(all(u.startswith("http://") and u.endswith(f":{handle.port}") for u in handle.urls()))


if __name__ == "__main__":
    unittest.main()
