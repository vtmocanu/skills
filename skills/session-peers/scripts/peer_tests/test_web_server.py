"""Web dashboard server: static files, token-guarded JSON API, request hygiene, caps, discovery and read-only behaviour."""

import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import ChannelSendBase
from peer_tests.test_opencode import OC_B
from session_peers import cli as sp_cli, channels as sp_channels, journal as sp_journal, runtime as sp_runtime, storage as sp_storage, web, webdata

REAL_ASSET_DIR = web.ASSET_DIR
ASSET_NAMES = ("index.html", "app.css", "app.js")
JSON_TYPE = "application/json; charset=utf-8"
SECURITY = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}
FIXTURES = {
    "index.html": b"<!doctype html><title>fixture</title>\n",
    "app.css": b"body{margin:0}\n",
    "app.js": b"'use strict';\n",
}


class Resp:
    def __init__(self, raw):
        head, _sep, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status_line = lines[0]
        self.status = int(lines[0].split()[1]) if raw else 0
        self.headers = [tuple(line.split(": ", 1)) for line in lines[1:]]

    def header(self, name):
        found = [v for k, v in self.headers if k.lower() == name.lower()]
        return found[0] if len(found) == 1 else None

    def json(self):
        return json.loads(self.body.decode("ascii"))


def snapshot(*roots):
    """(path, kind, mode, size, mtime_ns, sha256) of everything under the roots, directories included."""
    rows = []
    for root in roots:
        root = str(root)
        if not os.path.exists(root):
            rows.append((root, "missing"))
            continue
        for base, dirs, files in os.walk(root):
            dirs.sort()
            st = os.lstat(base)
            rows.append((base, "dir", stat.S_IMODE(st.st_mode), None, st.st_mtime_ns, None))
            for name in sorted(files):
                path = os.path.join(base, name)
                st = os.lstat(path)
                digest = None
                if stat.S_ISREG(st.st_mode):
                    with open(path, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                rows.append((path, "file", stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, digest))
    return rows


def wait_until(predicate, what, limit=10.0):
    """Poll ``predicate`` until it holds; fail with ``what`` after ``limit`` seconds."""
    end = time.monotonic() + limit
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.01)


class WebBase(ChannelSendBase):
    def setUp(self):
        super().setUp()
        self.sender_cc()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        for name, body in FIXTURES.items():
            (self.assets / name).write_bytes(body)
        for patch in (mock.patch.object(web, "ASSET_DIR", str(self.assets)), mock.patch.object(web, "DISCOVERY_TTL", 0.0)):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = self.make_server()
        self.serving = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.serving.start()
        self.addCleanup(self.stop_server)

    def make_server(self):
        return web.Server(0)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.serving.join(5)

    @property
    def port(self):
        return self.server.port

    # -- raw HTTP ------------------------------------------------------------

    def exchange(self, data, port=None):
        """Send ``data`` (bytes), read to EOF; ``Resp``. A reset after the response counts as EOF."""
        sock = socket.create_connection(("127.0.0.1", port or self.port), timeout=10)
        chunks = []
        try:
            sock.sendall(data)
            while True:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        return Resp(b"".join(chunks))

    def fetch(self, target="/", method="GET", headers=(), token=True, host="default", version="HTTP/1.1", body=b""):
        """One request. ``token`` True sends the right token on /api/*, a string sends that value, None/False none."""
        lines = ["%s %s %s" % (method, target, version)]
        if host == "default":
            lines.append("Host: 127.0.0.1:%d" % self.port)
        elif isinstance(host, (list, tuple)):
            lines.extend("Host: %s" % h for h in host)
        elif host is not None:
            lines.append("Host: %s" % host)
        if token is True:
            lines.append("X-Peers-Token: " + self.server.token)
        elif isinstance(token, str):
            lines.append("X-Peers-Token: " + token)
        lines.extend("%s: %s" % h for h in headers)
        return self.exchange(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)

    def api(self, target, **kw):
        return self.fetch(target, **kw)

    def ok(self, target):
        resp = self.api(target)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()

    def assert_headers(self, resp, ctype):
        got = dict(resp.headers)
        self.assertEqual(len(got), len(resp.headers), "a header was repeated")
        expected = dict(SECURITY, **{"Content-Type": ctype, "Content-Length": str(len(resp.body))})
        if resp.status == 405 and resp.header("Content-Length") == "2" and resp.body == b"":  # HEAD
            expected["Content-Length"] = "2"
        self.assertEqual(got, expected)

    def assert_refused(self, resp, status):
        self.assertEqual(resp.status, status, resp.body)
        self.assert_headers(resp, JSON_TYPE)
        self.assertEqual(resp.body, b"{}")

    # -- fixtures --------------------------------------------------------------

    def ops_channel(self):
        self.tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + self.tid)
        return self.tid

    def start_cursor(self):
        return self.ok("/api/state")["cursor"]

    def changes(self, cursor):
        return self.ok("/api/changes?since=" + urllib.parse.quote(cursor, safe=""))

    def all_routes(self, cursor):
        return [
            "/", "/app.css", "/app.js", "/api/state", "/api/channel/ops", "/api/channel/general",
            "/api/channel/ops?before=2&limit=1", "/api/changes?since=" + urllib.parse.quote(cursor, safe=""),
            "/api/all", "/api/search?q=freeze", "/api/search?q=a&scope=ops",
        ]


class StaticAndApiTests(WebBase):
    def test_static_files_load_without_a_token_and_equal_the_files(self):
        for path, name, ctype in (
            ("/", "index.html", "text/html; charset=utf-8"),
            ("/app.css", "app.css", "text/css; charset=utf-8"),
            ("/app.js", "app.js", "text/javascript; charset=utf-8"),
        ):
            with self.subTest(path=path):
                resp = self.fetch(path)
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.body, FIXTURES[name])
                self.assert_headers(resp, ctype)
        resp = self.fetch("/?x=1")
        self.assertEqual((resp.status, resp.body), (200, FIXTURES["index.html"]))

    @unittest.skipUnless(
        all(os.path.isfile(os.path.join(REAL_ASSET_DIR, n)) for n in ASSET_NAMES),
        "the dashboard asset files are not in session_peers/web/ yet")
    def test_the_real_asset_files_are_served_byte_for_byte(self):
        with mock.patch.object(web, "ASSET_DIR", REAL_ASSET_DIR):
            for path, name in (("/", "index.html"), ("/app.css", "app.css"), ("/app.js", "app.js")):
                with open(os.path.join(REAL_ASSET_DIR, name), "rb") as fh:
                    on_disk = fh.read()
                resp = self.fetch(path)
                self.assertEqual((resp.status, resp.body), (200, on_disk), path)

    def test_only_the_three_files_are_served(self):
        (self.assets / "secret.txt").write_text("nope")
        (self.assets / "sub").mkdir()
        for path in ("/index.html", "/secret.txt", "/sub/", "/sub", "/app.js.map", "/../etc/passwd", "/app.css/",
                     "/static/app.js", "/%61pp.js", "app.js", "/favicon.ico"):
            with self.subTest(path=path):
                self.assert_refused(self.fetch(path), 404)

    def test_a_missing_asset_is_a_404_not_a_listing(self):
        (self.assets / "app.js").unlink()
        self.assert_refused(self.fetch("/app.js"), 404)

    def test_state_has_sessions_channels_and_cursor_and_no_entries(self):
        self.ops_channel()
        self.send("--record")
        view = self.ok("/api/state")
        self.assertTrue({"sessions", "channels", "cursor"} <= set(view))
        self.assertNotIn("entries", json.dumps(view))
        self.assertEqual([c["key"] for c in view["channels"]], ["general", "ops"])
        self.assertEqual(view["discovery"], {"stale": False, "pending": False})
        sessions = {s["member"]: s for s in view["sessions"]}
        self.assertEqual(sessions[self.me]["state"], "idle")
        self.assertEqual(sessions[self.me]["liveness"], "live")
        self.assertEqual(sessions["codex:" + self.tid]["liveness"], "live")
        self.assertEqual(view["sessions"][0]["runtime"] in ("Claude Code", "Codex"), True)

    def test_example_one_two_channels_and_four_sessions_all_appear(self):
        self.ops_channel()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops2", peer, "oc:" + OC_B)
        view = self.ok("/api/state")
        self.assertEqual({c["key"] for c in view["channels"]}, {"general", "ops", "ops2"})
        sessions = {s["member"] for s in view["sessions"]}
        self.assertLessEqual({self.me, "codex:" + self.tid, peer, "oc:" + OC_B}, sessions)
        self.assertGreaterEqual(len(view["sessions"]), 4)

    def test_a_codex_member_with_a_shim_state_file_shows_its_reply_budget(self):
        tid = self.ops_channel()
        sid = new_uuid()
        shim = self.codex_dir / "session-peers"
        shim.mkdir(exist_ok=True)
        (shim / ("%s.json" % tid)).write_text(json.dumps(
            {"budgets": {sid: 2}, "budget_sender_sid": sid, "budget_last_at": sp_runtime.now_iso()}))
        sessions = {s["member"]: s for s in self.ok("/api/state")["sessions"]}
        budget = sessions["codex:" + tid]["reply_budget"]
        self.assertEqual((budget["requester"], budget["used"]), (sid, 2))
        self.assertNotIn("budget_unknown", sessions["codex:" + tid])

    def test_opencode_members_get_activity_and_name_from_opencode(self):
        self.channel("ops", "oc:" + OC_B)
        self.set_oc(active=[OC_B])
        view = self.ok("/api/state")
        sessions = {s["member"]: s for s in view["sessions"]}
        self.assertEqual(sessions["oc:" + OC_B]["state"], "working")
        members = {m["typed"]: m for c in view["channels"] for m in c["members"]}
        self.assertEqual(members["oc:" + OC_B]["name"], "oc-buddy")
        self.set_oc(active=[])
        sessions = {s["member"]: s for s in self.ok("/api/state")["sessions"]}
        self.assertEqual(sessions["oc:" + OC_B]["state"], "idle")

    def test_a_member_that_is_not_live_has_unknown_activity(self):
        sid = new_uuid()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.write_record(dead.pid, "ghost", sid, str(self.socks / "ghost.sock"), status="busy")
        self.channel("ops", "cc:" + sid)
        result = web.run_discovery()
        self.assertNotEqual(result["liveness"]["cc:" + sid], "live")
        self.assertEqual(result["activity"]["cc:" + sid], "unknown")

    def test_double_slash_targets_are_404_whatever_the_python_version(self):
        for target in ("//api/state", "///app.js", "//", "//app.js"):
            with self.subTest(target=target):
                self.assert_refused(self.fetch(target), 404)

    def test_discovery_reads_activity_and_names_from_the_registry(self):
        self.ops_channel()
        self.write_record(os.getpid(), "alpha-renamed", self.me.partition(":")[2], str(self.socks / "x.sock"), status="busy")
        view = self.ok("/api/state")
        sessions = {s["member"]: s for s in view["sessions"]}
        self.assertEqual(sessions[self.me]["state"], "working")
        members = {m["typed"]: m for c in view["channels"] for m in c["members"]}
        self.assertEqual(members[self.me]["name"], "alpha-renamed")

    def test_a_channel_page_is_one_bounded_page(self):
        self.ops_channel()
        self.send("--record")
        self.send("--record", message="second")
        page = self.ok("/api/channel/ops?before=3&limit=200")
        self.assertEqual(page["channel"], "ops")
        self.assertEqual([e["seq"] for e in page["entries"]], [1, 2])
        self.assertLessEqual(len(page["entries"]), 200)
        newest = self.ok("/api/channel/ops?limit=2")
        self.assertEqual(len(newest["entries"]), 2)
        self.assertIsNotNone(newest["next"])
        self.assertEqual(newest["info"]["members"], [self.me, "codex:" + self.tid])

    def test_general_gets_the_discovered_members_for_its_coverage(self):
        self.ops_channel()
        page = self.ok("/api/channel/general")
        self.assertEqual(page["channel"], "general")
        self.assertEqual(page["coverage"], {"native_claude": True})

    def test_all_and_search_answer(self):
        self.ops_channel()
        self.send("--record", message="freeze main héllo")
        everything = self.ok("/api/all")
        self.assertTrue(any(e.get("channel") == "ops" and e["kind"] == "send" for e in everything["entries"]))
        found = self.ok("/api/search?q=FREEZE")
        self.assertEqual([m["channel"] for m in found["matches"] if m["kind"] == "send"], ["ops"])
        accented = self.ok("/api/search?q=" + urllib.parse.quote("héllo") + "&scope=ops")
        self.assertEqual(len(accented["matches"]), 1)

    def test_json_is_compact_ascii_with_an_object_on_top(self):
        self.ops_channel()
        self.send("--record", message="snowman ☃ and \U0001F600")
        resp = self.api("/api/channel/ops")
        raw = resp.body.decode("ascii")
        self.assertIsInstance(json.loads(raw), dict)
        self.assertNotIn(": ", raw.split('"body"')[0])
        self.assertIn("snowman ☃", json.loads(raw)["entries"][1]["body"])
        self.assertIn("\\u2603", raw)
        self.assert_headers(resp, JSON_TYPE)

    def test_two_servers_have_distinct_tokens_of_64_hex(self):
        other = web.Server(0)
        self.addCleanup(other.server_close)
        self.assertRegex(self.server.token, r"^[0-9a-f]{64}$")
        self.assertNotEqual(self.server.token, other.token)


class ChangesOverHttpTests(WebBase):
    def test_example_two_send_then_ack_arrive_on_the_cursor(self):
        tid = self.ops_channel()
        first = self.changes(self.start_cursor())
        self.assertEqual((first["channels"], first["updates"], first["partial"]), ({}, [], False))
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        second = self.changes(first["cursor"])
        entries = second["channels"]["ops"]["entries"]
        self.assertEqual([e["kind"] for e in entries], ["send", "outcome"])
        row = entries[0]["delivery"]["recipients"][0]
        self.assertEqual((row["recipient"], row["state"], row["ack"]), ("codex:" + tid, "transport accepted", "pending"))
        self.assertEqual(second["updates"], [])
        sp_journal.ack(result["message_id"], "codex:" + tid)
        third = self.changes(second["cursor"])
        self.assertEqual(third["channels"], {})
        self.assertEqual(third["updates"], [{
            "message_id": result["message_id"], "recipient": "codex:" + tid, "ack": "acked", "state": "acked"}])


class HygieneTests(WebBase):
    API = ("/api/state", "/api/channel/general", "/api/changes?since=abc", "/api/all", "/api/search?q=a")

    def test_api_without_a_valid_token_is_403_with_an_empty_object(self):
        wrong = "0" * 64
        for target in self.API:
            for label, token in (("missing", None), ("wrong", wrong), ("empty", ""), ("short", self.server.token[:-1]),
                                 ("longer", self.server.token + "0"), ("non-ascii", "é" * 64)):
                with self.subTest(target=target, token=label):
                    self.assert_refused(self.fetch(target, token=token), 403)
            with self.subTest(target=target, token="duplicate"):
                dup = self.fetch(target, token=True, headers=[("X-Peers-Token", self.server.token)])
                self.assert_refused(dup, 403)
        self.assert_refused(self.fetch("/api/nope", token=None), 403)
        self.assertEqual(self.fetch("/api/state", token=self.server.token).status, 200)

    def test_every_method_but_get_is_405(self):
        for method in ("POST", "PROPFIND", "TRACE", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "CONNECT", "get"):
            for target in ("/", "/api/state"):
                with self.subTest(method=method, target=target):
                    resp = self.fetch(target, method=method)
                    self.assertEqual(resp.status, 405)
                    self.assertEqual(dict(resp.headers).keys() >= set(SECURITY), True)
                    for name, value in SECURITY.items():
                        self.assertEqual(resp.header(name), value)
                    self.assertEqual(resp.body, b"" if method == "HEAD" else b"{}")
                    self.assertEqual(resp.header("Content-Length"), "2")
        with self.subTest("method comes before the token and the body"):
            resp = self.fetch("/api/state", method="POST", token=None, headers=[("Content-Length", "5")], body=b"hello")
            self.assertEqual(resp.status, 405)

    def test_host_must_be_exactly_loopback_with_the_port(self):
        port = self.port
        good = ["127.0.0.1:%d" % port, "localhost:%d" % port]
        bad = ["evil.example:%d" % port, "127.0.0.1", "localhost", "127.0.0.1:%d" % (port + 1), "LOCALHOST:%d" % port,
               "127.0.0.1:%d " % port, "[::1]:%d" % port, "0.0.0.0:%d" % port, "", "127.0.0.1:%d.evil.example" % port]
        for target in ("/", "/app.js", "/api/state", "/nope"):
            for host in good:
                with self.subTest(target=target, host=host):
                    self.assertEqual(self.fetch(target, host=host).status, 404 if target == "/nope" else 200)
            for host in bad:
                with self.subTest(target=target, host=host):
                    self.assert_refused(self.fetch(target, host=host), 403)
            with self.subTest(target=target, host="missing"):
                self.assert_refused(self.fetch(target, host=None), 403)
            with self.subTest(target=target, host="duplicate"):
                self.assert_refused(self.fetch(target, host=good + good[:1]), 403)
                self.assert_refused(self.fetch(target, host=[good[0], good[0]]), 403)
        with self.subTest("host comes before the method"):
            self.assert_refused(self.fetch("/", method="POST", host="evil.example"), 403)
        with self.subTest("HTTP/1.0 without a Host"):
            self.assert_refused(self.fetch("/", host=None, version="HTTP/1.0"), 403)

    def test_a_request_body_is_refused(self):
        for label, headers, body in (
            ("length 5", [("Content-Length", "5")], b"hello"),
            ("length non-integer", [("Content-Length", "abc")], b""),
            ("length negative", [("Content-Length", "-1")], b""),
            ("length 00", [("Content-Length", "00")], b""),
            ("two lengths", [("Content-Length", "0"), ("Content-Length", "0")], b""),
            ("chunked", [("Transfer-Encoding", "chunked")], b"0\r\n\r\n"),
            ("identity encoding", [("Transfer-Encoding", "identity")], b""),
        ):
            for target in ("/", "/api/state"):
                with self.subTest(label, target=target):
                    self.assert_refused(self.fetch(target, headers=headers, body=body), 400)
        self.assertEqual(self.fetch("/", headers=[("Content-Length", "0")]).status, 200)

    def test_http_server_level_errors_carry_the_headers_and_an_empty_object(self):
        cases = (
            ("garbage", b"GARBAGE\r\n\r\n", 400),
            ("four words", b"GET / HTTP/1.1 extra\r\nHost: 127.0.0.1:%d\r\n\r\n", 400),
            ("http 0.9", b"GET /\r\n\r\n", 400),
            ("bad version", b"GET / HTTP/9.9\r\n\r\n", 505),
            ("not http", b"GET / FTP/1.1\r\n\r\n", 400),
            ("long uri", b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\nHost: x\r\n\r\n", 414),
            ("long header", b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Long: " + b"a" * 70000 + b"\r\n\r\n", 431),
        )
        for label, raw, status in cases:
            with self.subTest(label):
                if b"%d" in raw:
                    raw = raw % self.port
                self.assert_refused(self.exchange(raw), status)

    def test_route_and_parameter_validation(self):
        self.ops_channel()
        self.send("--record")
        cases = (
            ("/api/nope", 404), ("/api", 404), ("/api/", 404), ("/api/state/", 404), ("/api/state?x=1", 400),
            ("/api/channel/", 400), ("/api/channel/BAD%20KEY", 400), ("/api/channel/a/b", 400),
            ("/api/channel/missing", 404), ("/api/channel/ops?limit=0", 400), ("/api/channel/ops?limit=201", 400),
            ("/api/channel/ops?limit=abc", 400), ("/api/channel/ops?limit=-1", 400), ("/api/channel/ops?limit=", 400),
            ("/api/channel/ops?limit=1&limit=2", 400), ("/api/channel/ops?before=abc", 400),
            ("/api/channel/ops?before=12345678901234567", 400), ("/api/channel/ops?before=%EF%BC%91", 400),
            ("/api/channel/ops?x=1", 400), ("/api/channel/ops?limit=1&", 400), ("/api/channel/ops?limit", 400),
            ("/api/changes", 400), ("/api/changes?since=", 400), ("/api/changes?since=!!", 400),
            ("/api/changes?since=AAAA", 400), ("/api/changes?since=a&since=b", 400),
            ("/api/all?next=!!", 400), ("/api/all?next=AAAA", 400), ("/api/all?x=1", 400),
            ("/api/search", 400), ("/api/search?q=", 400), ("/api/search?q=a&q=b", 400),
            ("/api/search?q=a&scope=BAD%20KEY", 400), ("/api/search?q=a&scope=missing", 404),
            ("/api/search?q=%ff", 400), ("/api/search?q=a&x=1", 400), ("/api/search?q=" + "a" * 300, 400),
            ("http://127.0.0.1:%d/api/state" % self.port, 404), ("api/state", 404),
        )
        for target, status in cases:
            with self.subTest(target=target):
                self.assert_refused(self.api(target), status)

    def test_headers_verbatim_on_every_status(self):
        self.ops_channel()
        seen = {}
        seen[200] = self.api("/api/state")
        self.assert_headers(seen[200], JSON_TYPE)
        seen[400] = self.api("/api/channel/ops?limit=0")
        seen[403] = self.fetch("/api/state", token=None)
        seen[404] = self.fetch("/nope")
        seen[405] = self.fetch("/", method="POST")
        with mock.patch.object(webdata, "state_view", side_effect=RuntimeError("boom-secret")):
            seen[500] = self.api("/api/state")
        for status, resp in seen.items():
            with self.subTest(status=status):
                self.assertEqual(resp.status, status)
                self.assert_headers(resp, JSON_TYPE)
        for status in (400, 403, 404, 405, 500):
            self.assertEqual(seen[status].body, b"{}")
        self.assertEqual([name for name, _v in seen[500].headers][:2], ["Content-Type", "Content-Length"])
        self.assertNotIn(b"boom-secret", seen[500].body)
        static = self.fetch("/app.js")
        self.assert_headers(static, "text/javascript; charset=utf-8")

    def test_a_read_model_error_status_passes_through_and_nothing_is_logged(self):
        err = webdata.WebDataError("conflict", 409)
        with mock.patch.object(webdata, "all_channels", side_effect=err), mock.patch("sys.stderr") as stderr, \
                mock.patch("sys.stdout") as stdout:
            self.assert_refused(self.api("/api/all"), 409)
            self.assert_refused(self.fetch("/", method="POST"), 405)
        self.assertEqual((stderr.write.call_count, stdout.write.call_count), (0, 0))

    def test_every_error_leaves_through_the_one_send_path(self):
        with mock.patch.object(web.Handler, "_send", autospec=True, side_effect=web.Handler._send) as send:
            self.fetch("/api/state", token=None)
            self.fetch("/", method="POST")
            self.exchange(b"GARBAGE\r\n\r\n")
            self.fetch("/app.css")
        self.assertEqual([c.args[1] for c in send.call_args_list], [403, 405, 400, 200])


class ConnectionLimitTests(WebBase):
    def slots(self):
        return self.server._slots._value

    def held(self, count):
        socks = []
        for _ in range(count):
            socks.append(socket.create_connection(("127.0.0.1", self.port), timeout=10))
        self.addCleanup(lambda: [s.close() for s in socks])
        wait_until(lambda: self.slots() == web.MAX_CONNECTIONS - count, "the server to take %d slots" % count)
        return socks

    def test_the_17th_connection_is_closed_unanswered_and_service_resumes(self):
        socks = self.held(16)
        extra = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            extra.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.port)
            try:
                data = extra.recv(65536)
            except ConnectionResetError:
                data = b""
        finally:
            extra.close()
        self.assertEqual(data, b"")
        self.assertEqual(self.slots(), 0)
        for sock in socks:
            sock.close()
        wait_until(lambda: self.slots() == 16, "every slot to come back")
        self.assertEqual(self.fetch("/app.js").status, 200)

    def test_twenty_rounds_of_overflow_never_raise_the_cap(self):
        socks = self.held(16)
        for _ in range(20):
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                try:
                    self.assertEqual(extra.recv(1), b"")
                except ConnectionResetError:
                    pass
            finally:
                extra.close()
            self.assertEqual(self.slots(), 0)
        for sock in socks:
            sock.close()
        wait_until(lambda: self.slots() == 16, "every slot to come back")
        self.assertEqual(self.server._holders, set())
        for _ in range(3):
            self.assertEqual(self.fetch("/app.css").status, 200)
        wait_until(lambda: self.slots() == 16, "every slot to come back")
        with self.assertRaises(ValueError):  # the semaphore is exactly full: one more release would raise
            self.server._slots.release()

    def test_a_thread_that_cannot_start_gives_its_slot_back(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                self.server.process_request(left, ("127.0.0.1", 1))
        self.assertEqual(self.slots(), 16)
        self.assertEqual(left.fileno(), -1)
        self.assertEqual(right.recv(1), b"")

    def test_releasing_a_socket_that_holds_no_slot_does_nothing(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        self.server.shutdown_request(left)
        self.assertEqual(self.slots(), 16)


class DeadlineTests(WebBase):
    def test_a_slow_trickling_client_is_cut_at_the_absolute_deadline(self):
        with mock.patch.object(web, "CONNECTION_DEADLINE", 1.0):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            started = time.monotonic()
            try:
                for byte in b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:1\r\nX-A: " + b"a" * 100:
                    try:
                        sock.send(bytes([byte]))
                    except OSError:
                        break
                    readable, _w, _x = select.select([sock], [], [], 0.3)
                    if readable:
                        break
                    self.assertLess(time.monotonic() - started, 6)
                cut = time.monotonic() - started
                self.assertTrue(select.select([sock], [], [], 2)[0], "the connection was not closed")
                try:
                    self.assertEqual(sock.recv(65536), b"")
                except ConnectionResetError:
                    pass
            finally:
                sock.close()
        self.assertGreaterEqual(cut, 0.9)
        self.assertLess(cut, 2.5)
        wait_until(lambda: self.server._slots._value == 16, "the slot to come back")

    def test_a_client_that_never_finishes_its_headers_is_cut_at_the_header_deadline(self):
        with mock.patch.object(web, "HEADER_DEADLINE", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            started = time.monotonic()
            try:
                sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:1\r\n")
                self.assertTrue(select.select([sock], [], [], 5)[0], "the connection was not closed")
                try:
                    self.assertEqual(sock.recv(65536), b"")
                except ConnectionResetError:
                    pass
                cut = time.monotonic() - started
            finally:
                sock.close()
        self.assertGreaterEqual(cut, 0.4)
        self.assertLess(cut, 2.5)
        wait_until(lambda: self.server._slots._value == 16, "the slot to come back")

    def test_a_header_deadline_never_dispatches_the_truncated_request(self):
        dispatched = []
        with mock.patch.object(web, "HEADER_DEADLINE", 0.5), \
                mock.patch.object(web.Handler, "_dispatch", lambda handler: dispatched.append(handler.path)):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                sock.sendall(b"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % self.port)
                self.assertTrue(select.select([sock], [], [], 5)[0], "the connection was not closed")
                try:
                    data = sock.recv(65536)
                except ConnectionResetError:
                    data = b""
            finally:
                sock.close()
            wait_until(lambda: self.server._slots._value == 16, "the slot to come back")
        self.assertEqual(data, b"")
        self.assertEqual(dispatched, [])

    def test_sixteen_header_stallers_do_not_block_a_real_request_for_long(self):
        with mock.patch.object(web, "HEADER_DEADLINE", 0.5):
            stalled = []
            for _ in range(web.MAX_CONNECTIONS):
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
                sock.sendall(b"GET / HTTP/1.1\r\n")
                stalled.append(sock)
            self.addCleanup(lambda: [s.close() for s in stalled])
            wait_until(lambda: self.server._slots._value == 0, "every slot to be taken")
            started = time.monotonic()
            status = 0
            while status != 200 and time.monotonic() - started < 6:
                status = self.fetch("/app.js").status
                time.sleep(0.05)
            took = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLess(took, 2.5)

    def test_a_request_that_finishes_its_headers_in_time_is_not_cut_by_the_header_deadline(self):
        with mock.patch.object(web, "HEADER_DEADLINE", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                sock.sendall(b"GET /app.js HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.port)
                time.sleep(0.8)  # the timer would have fired by now
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()
        self.assertEqual(Resp(data).status, 200)

    def test_startup_does_no_reverse_dns(self):
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS")), \
                mock.patch.object(socket, "gethostbyaddr", side_effect=AssertionError("reverse DNS")):
            server = web.Server(0)
        try:
            self.assertEqual(server.server_port, server.port)
            self.assertEqual(server.server_name, "127.0.0.1")
        finally:
            server.server_close()

    def test_a_silent_client_is_cut_by_the_recv_timeout(self):
        with mock.patch.object(web, "RECV_TIMEOUT", 0.5):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            started = time.monotonic()
            try:
                self.assertEqual(sock.recv(65536), b"")
            finally:
                sock.close()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertLess(time.monotonic() - started, 5)
        wait_until(lambda: self.server._slots._value == 16, "the slot to come back")


class DiscoveryWorkerTests(WebBase):
    def setUp(self):
        super().setUp()
        self.other = self.home / ".codex"
        self.other.mkdir()
        self.release = threading.Event()
        self.release.set()
        self.addCleanup(self.release.set)

    def gate(self, delay=0.0):
        """Wrap run_discovery: count calls, and block while ``self.release`` is clear."""
        calls = []
        real = web.run_discovery

        def wrapped():
            calls.append(time.monotonic())
            self.release.wait(20)
            time.sleep(delay)
            return real()

        return calls, mock.patch.object(web, "run_discovery", wrapped)

    def home_override_probe(self):
        state = {"active": 0, "max": 0, "calls": 0}
        lock = threading.Lock()
        real = sp_storage.codex_home_override

        @contextlib.contextmanager
        def probe(home):
            with lock:
                state["active"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["active"])
            try:
                with real(home):
                    time.sleep(0.15)
                    yield
            finally:
                with lock:
                    state["active"] -= 1

        return state, mock.patch.object(sp_storage, "codex_home_override", probe)

    def test_concurrent_state_requests_share_one_discovery_run(self):
        self.ops_channel()
        calls, gate = self.gate(delay=0.3)
        with gate, mock.patch.object(web, "DISCOVERY_TTL", 5.0):
            results = []

            def hit():
                results.append(self.api("/api/state"))

            threads = [threading.Thread(target=hit) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.status for r in results], [200] * 4)
        for resp in results:
            self.assertEqual(resp.json()["discovery"], {"stale": False, "pending": False})

    def test_discovery_runs_never_overlap_codex_home_overrides(self):
        self.ops_channel()
        # a second Codex home with its own state DB makes discovery enter codex_home_override
        other_db = self.home / ".codex" / "state_9.sqlite"
        conn = sqlite3.connect(str(other_db))
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, rollout_path TEXT, cwd TEXT, updated_at TEXT)")
        conn.commit()
        conn.close()
        state, probe = self.home_override_probe()
        with probe, mock.patch.object(web, "DISCOVERY_TTL", 0.0):
            results = []
            threads = [threading.Thread(target=lambda: results.append(self.api("/api/state"))) for _ in range(3)]
            for t in threads:
                t.start()
            runners = [threading.Thread(target=web.run_discovery) for _ in range(2)]
            for t in runners:
                t.start()
            for t in threads + runners:
                t.join(60)
        self.assertGreaterEqual(state["calls"], 2)
        self.assertEqual(state["max"], 1)
        self.assertEqual(state["active"], 0)
        self.assertEqual([r.status for r in results], [200] * 3)

    def test_the_cache_is_reused_within_the_ttl_and_refreshed_after(self):
        calls, gate = self.gate()
        with gate:
            with mock.patch.object(web, "DISCOVERY_TTL", 60.0):
                self.api("/api/state")
                self.api("/api/state")
            self.assertEqual(len(calls), 1)
            self.api("/api/state")  # TTL back to 0: stale cache, so a fresh run
            self.assertEqual(len(calls), 2)

    def test_a_slow_discovery_answers_pending_within_the_deadline(self):
        self.ops_channel()
        calls, gate = self.gate()
        self.release.clear()
        with gate, mock.patch.object(web, "DISCOVERY_WAIT", 0.3):
            started = time.monotonic()
            resp = self.api("/api/state")
            took = time.monotonic() - started
            self.assertEqual(resp.status, 200)
            self.assertLess(took, 5)
            self.assertEqual(resp.json()["discovery"], {"stale": False, "pending": True})
            page = self.api("/api/channel/general").json()
            self.assertEqual(page["coverage"], {"native_claude": None})
            self.assertEqual(len(calls), 1)  # still the one run in flight
            self.release.set()
            wait_until(lambda: not self.server.discovery._running, "the worker to finish")
            with mock.patch.object(web, "DISCOVERY_TTL", 60.0):
                done = self.api("/api/state").json()
            self.assertEqual(done["discovery"], {"stale": False, "pending": False})
            self.assertEqual(len(calls), 1)

    def test_the_last_result_is_served_stale_while_a_new_run_is_slow(self):
        self.ops_channel()
        calls, gate = self.gate()
        with gate:
            first = self.api("/api/state").json()
            self.assertEqual(first["discovery"], {"stale": False, "pending": False})
            self.release.clear()
            with mock.patch.object(web, "DISCOVERY_WAIT", 0.3):
                second = self.api("/api/state").json()
            self.assertEqual(second["discovery"], {"stale": True, "pending": False})
            self.assertEqual({s["member"] for s in second["sessions"]}, {s["member"] for s in first["sessions"]})
            self.release.set()
            wait_until(lambda: not self.server.discovery._running, "the worker to finish")

    def test_a_failing_discovery_marks_the_answer_pending_then_stale(self):
        self.ops_channel()
        with mock.patch.object(web, "run_discovery", side_effect=RuntimeError("secret detail")):
            resp = self.api("/api/state")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json()["discovery"], {"stale": False, "pending": True})
        self.assertNotIn(b"secret detail", resp.body)
        self.assertEqual(self.api("/api/state").json()["discovery"], {"stale": False, "pending": False})
        with mock.patch.object(web, "run_discovery", side_effect=RuntimeError("again")):
            self.assertEqual(self.api("/api/state").json()["discovery"], {"stale": True, "pending": False})

    def test_discovery_caps_members(self):
        ids = ["cc:%s" % new_uuid() for _ in range(web.MEMBERS_PER_CHANNEL + 10)]
        seen = []

        def liveness(typed, cache=None):
            seen.append(typed)
            return "unverified"

        with mock.patch.object(sp_channels, "general_members", return_value=(ids, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", liveness):
            result = web.run_discovery()
        self.assertEqual(len(seen), web.MEMBERS_PER_CHANNEL)
        self.assertEqual(set(result["liveness"]), set(ids[:web.MEMBERS_PER_CHANNEL]))
        with mock.patch.object(web, "MEMBERS_TOTAL", 3), \
                mock.patch.object(sp_channels, "general_members", return_value=(ids[:5], {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", liveness):
            self.assertEqual(len(web.run_discovery()["liveness"]), 3)

    # -- bounded fan-out ---------------------------------------------------

    def many_codex_channels(self, channels=9, per=63):
        members = []
        for index in range(channels):
            roster = ["codex:%s" % new_uuid() for _ in range(per)]
            members.extend(roster)
            self.channel("big%d" % index, *roster)
        return members

    def test_a_pass_over_567_codex_members_checks_64_stays_quiet_within_budget_and_caps_at_512(self):
        members = self.many_codex_channels()
        self.assertGreaterEqual(len(members), 512)
        calls = []
        real = sp_channels.member_liveness

        def counting(typed, cache=None):
            calls.append(typed)
            return real(typed, cache)

        stderr = io.StringIO()
        with mock.patch.object(web, "DISCOVERY_PASS_BUDGET", 3.0), contextlib.redirect_stderr(stderr), \
                mock.patch.object(sp_channels, "member_liveness", counting):
            started = time.monotonic()
            result = web.run_discovery()
            took = time.monotonic() - started
        self.assertEqual(stderr.getvalue(), "")
        self.assertLess(took, 3.0)
        # the fixture's own channels may add a few non-codex members, so count every subprocess member
        self.assertEqual(len([m for m in calls if not m.startswith("cc:")]), web.SUBPROCESS_MEMBERS_PER_PASS)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["liveness"]), web.MEMBERS_TOTAL)
        unchecked = {k: v for k, v in result["liveness"].items() if k not in set(calls)}
        self.assertEqual(len(unchecked), web.MEMBERS_TOTAL - len(calls))
        self.assertEqual(set(unchecked.values()), {"unverified"})

    def test_a_pass_shares_one_cache_across_cc_and_oc_members(self):
        self.channel("ops", "cc:" + new_uuid(), "oc:" + OC_B, "oc:ses_zzzzzzzzzzzzZZZZZZZZZZZZZZ")
        caches = []

        def liveness(typed, cache=None):
            caches.append(cache)
            return "unverified"

        with mock.patch.object(sp_channels, "member_liveness", liveness):
            web.run_discovery()
        self.assertGreaterEqual(len(caches), 3)
        self.assertTrue(all(c is caches[0] and c is not None for c in caches))

    def test_thread_home_is_looked_up_only_for_codex_members_that_were_checked(self):
        ids = ["codex:%s" % new_uuid() for _ in range(5)]
        homes = []

        def liveness(typed, cache=None):
            time.sleep(0.2)
            return "unverified"

        def thread_home(ident):
            homes.append(ident)
            return None

        with mock.patch.object(sp_channels, "general_members", return_value=(ids, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", liveness), \
                mock.patch.object(sp_storage, "thread_home", thread_home), \
                mock.patch.object(web, "DISCOVERY_PASS_BUDGET", 0.1):
            result = web.run_discovery()
        self.assertEqual(homes, [ids[0].partition(":")[2]])
        self.assertTrue(result["truncated"])
        with mock.patch.object(sp_channels, "general_members", return_value=(ids, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", return_value="unverified"), \
                mock.patch.object(sp_storage, "thread_home", thread_home), \
                mock.patch.object(web, "SUBPROCESS_MEMBERS_PER_PASS", 2):
            homes.clear()
            web.run_discovery()
        self.assertEqual(homes, [i.partition(":")[2] for i in ids[:2]])

    def test_opencode_is_not_queried_after_the_pass_deadline_or_for_unchecked_members(self):
        oc = ["oc:" + OC_B]

        def slow(typed, cache=None):
            time.sleep(0.2)
            return "live"

        with mock.patch.object(sp_channels, "general_members", return_value=(oc, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", slow), \
                mock.patch.object(web, "_opencode_view", return_value=({}, {})) as view, \
                mock.patch.object(web, "DISCOVERY_PASS_BUDGET", 0.1):
            web.run_discovery()
        view.assert_not_called()
        with mock.patch.object(sp_channels, "general_members", return_value=(oc, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", return_value="live"), \
                mock.patch.object(web, "_opencode_view", return_value=({}, {})) as view, \
                mock.patch.object(web, "SUBPROCESS_MEMBERS_PER_PASS", 0):
            web.run_discovery()
        view.assert_not_called()
        with mock.patch.object(sp_channels, "general_members", return_value=(oc, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", return_value="live"), \
                mock.patch.object(web, "_opencode_view", return_value=({}, {})) as view:
            web.run_discovery()
        view.assert_called_once_with(oc)

    def test_skipping_opencode_for_the_deadline_marks_the_pass_truncated(self):
        oc = ["oc:" + OC_B]

        def slow(typed, cache=None):
            time.sleep(0.2)
            return "live"

        with mock.patch.object(sp_channels, "general_members", return_value=(oc, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", slow), \
                mock.patch.object(web, "_opencode_view", return_value=({}, {})) as view, \
                mock.patch.object(web, "DISCOVERY_PASS_BUDGET", 0.1):
            result = web.run_discovery()
        view.assert_not_called()
        self.assertTrue(result["truncated"])
        with mock.patch.object(sp_channels, "general_members", return_value=(oc, {}, [])), \
                mock.patch.object(sp_channels, "member_liveness", return_value="live"), \
                mock.patch.object(web, "_opencode_view", return_value=({}, {})):
            self.assertFalse(web.run_discovery()["truncated"])

    def test_members_not_reached_before_the_pass_deadline_are_unverified_and_the_answer_partial(self):
        self.ops_channel()
        reached = []

        def slow(typed, cache=None):
            reached.append(typed)
            time.sleep(0.3)
            return "live"

        with mock.patch.object(web, "DISCOVERY_PASS_BUDGET", 0.1), mock.patch.object(sp_channels, "member_liveness", slow):
            result = web.run_discovery()
            view = self.ok("/api/state")
        self.assertEqual(len(reached), 2)  # one per pass: the deadline passed after the first member
        self.assertTrue(result["truncated"])
        self.assertIn("unverified", result["liveness"].values())
        self.assertEqual(view["discovery"], {"stale": False, "pending": False, "truncated": True})
        self.assertFalse(view["partial"])  # top-level partial is only webdata's read budget

    def test_a_complete_pass_is_not_truncated(self):
        self.ops_channel()
        self.assertFalse(web.run_discovery()["truncated"])
        view = self.ok("/api/state")
        self.assertFalse(view["partial"])
        self.assertNotIn("truncated", view["discovery"])


class NoCreateTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="sp", dir="/tmp"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "cx")})
        patch.start()
        self.addCleanup(patch.stop)
        os.makedirs(str(self.root / "cx"))

    def test_the_paths_are_named_but_nothing_is_created_or_changed(self):
        with sp_storage.no_create():
            state = sp_storage.state_dir()
            self.assertEqual(state, str(self.root / "cx" / "session-peers"))
            self.assertEqual(sp_storage.buddies_dir(), os.path.join(state, "buddies"))
            self.assertEqual(sp_storage.request_dir(), os.path.join(state, "requests"))
            self.assertFalse(os.path.exists(state))
        os.makedirs(state, mode=0o755)
        os.chmod(state, 0o755)
        os.utime(state, ns=(10 ** 9, 10 ** 9))
        before = os.stat(state)
        with sp_storage.no_create():
            sp_storage.state_dir()
            sp_storage.buddies_dir()
            sp_storage.request_dir()
        after = os.stat(state)
        self.assertEqual((stat.S_IMODE(after.st_mode), after.st_mtime_ns), (0o755, before.st_mtime_ns))
        self.assertEqual(os.listdir(state), [])

    def test_the_default_still_creates_with_mode_0700(self):
        state = sp_storage.state_dir()
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
        for fn, name in ((sp_storage.buddies_dir, "buddies"), (sp_storage.request_dir, "requests")):
            self.assertEqual(fn(), os.path.join(state, name))
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(state, name)).st_mode), 0o700)

    def test_it_is_thread_local_nests_and_unwinds_on_error(self):
        state = str(self.root / "cx" / "session-peers")
        entered, leave = threading.Event(), threading.Event()
        inside = []

        def worker():
            with sp_storage.no_create():
                inside.append(sp_storage.state_dir())
                entered.set()
                leave.wait(10)

        thread = threading.Thread(target=worker)
        thread.start()
        entered.wait(10)
        try:
            self.assertFalse(os.path.exists(state))
            sp_storage.state_dir()  # this thread is not inside: it creates
            self.assertTrue(os.path.isdir(state))
        finally:
            leave.set()
            thread.join(10)
        shutil.rmtree(state)
        with sp_storage.no_create():
            with sp_storage.no_create():
                sp_storage.state_dir()
            sp_storage.state_dir()
            self.assertFalse(os.path.exists(state))
        with self.assertRaises(KeyError):
            with sp_storage.no_create():
                raise KeyError("x")
        sp_storage.state_dir()
        self.assertTrue(os.path.isdir(state))


class ReadOnlyTests(WebBase):
    def setUp(self):
        super().setUp()
        self.other = self.root / "other-home"
        self.other.mkdir()
        # a Codex shim record naming another CODEX_HOME makes that home a discovery candidate
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self._children.append(proc)
        self.shim_sid = new_uuid()
        self.write_record(proc.pid, "codex-other", self.shim_sid, str(self.socks / "none.sock"),
                          entrypoint="codex", codexHome=str(self.other))
        self.tid = self.ops_channel()
        self.send("--record")
        shutil.rmtree(str(self.codex_dir / "session-peers"), ignore_errors=True)
        self.cursor = self.start_cursor()
        shutil.rmtree(str(self.codex_dir / "session-peers"), ignore_errors=True)

    def hit_all(self):
        statuses = {}
        for target in self.all_routes(self.cursor):
            statuses[target] = self.fetch(target).status
        return statuses

    def test_absent_session_peers_directories_stay_absent(self):
        mine, theirs = self.codex_dir / "session-peers", self.other / "session-peers"
        self.assertFalse(mine.exists() or theirs.exists())
        statuses = self.hit_all()
        self.assertEqual(set(statuses.values()), {200}, statuses)
        self.assertFalse(mine.exists(), "CODEX_HOME/session-peers was created")
        self.assertFalse(theirs.exists(), "another home's session-peers was created")

    def test_present_session_peers_directories_keep_mode_and_mtime(self):
        before = {}
        for home in (self.codex_dir, self.other):
            path = home / "session-peers"
            path.mkdir()
            os.chmod(str(path), 0o755)
            os.utime(str(path), ns=(10 ** 9, 10 ** 9))
            before[path] = os.stat(str(path))
        self.assertEqual(set(self.hit_all().values()), {200})
        for path, old in before.items():
            now = os.stat(str(path))
            self.assertEqual((stat.S_IMODE(now.st_mode), now.st_mtime_ns), (0o755, old.st_mtime_ns), str(path))
            self.assertEqual(os.listdir(str(path)), [])

    def test_no_route_changes_any_store_or_home(self):
        alt = self.root / "alt-sqlite"
        alt.mkdir()
        (self.codex_dir / "config.toml").write_text('sqlite_home = "%s"\n' % alt)
        shutil.copy(str(self.codex_dir / "state_2.sqlite"), str(alt / "state_3.sqlite"))
        (self.other / "state_2.sqlite").write_bytes((self.codex_dir / "state_2.sqlite").read_bytes())
        shim_dir = self.codex_dir / "session-peers"
        shim_dir.mkdir()
        (shim_dir / ("%s.json" % self.tid)).write_text(json.dumps({"reply_budget": {"used": 1, "limit": 3}}))
        (shim_dir / ("%s.pid" % self.tid)).write_text("999999\n")
        roots = (self.home, self.codex_dir, self.other, alt, self.claude_dir)
        for target in self.all_routes(self.cursor):
            with self.subTest(route=target):
                before = snapshot(*roots)
                self.assertEqual(self.fetch(target).status, 200)
                after = snapshot(*roots)
                self.assertEqual(
                    [r for r in after if r not in before][:3], [], "created or changed")
                self.assertEqual([r for r in before if r not in after][:3], [], "removed or changed")

    def make_wal_db(self, keep_open):
        path = self.codex_dir / "state_5.sqlite"
        conn = sqlite3.connect(str(path))
        self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, rollout_path TEXT, cwd TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO threads VALUES (?, 'wal-thread', ?, '/tmp', '2026-09-07T12:00:00Z')",
                     (new_uuid(), str(self.codex_dir / "rollout.jsonl")))
        conn.commit()
        if keep_open:
            self.addCleanup(conn.close)
        else:
            conn.close()
        return path

    def sidecars(self, path):
        return {n for n in os.listdir(str(path.parent)) if n.startswith(path.name + "-")}

    def test_a_wal_database_without_sidecars_gets_no_changes_beyond_its_own_sidecars(self):
        for name in ("state_2.sqlite",):
            (self.codex_dir / name).unlink()
        path = self.make_wal_db(keep_open=False)
        self.assertEqual(self.sidecars(path), set())
        names = set(os.listdir(str(self.codex_dir)))
        main = path.read_bytes()
        self.assertEqual(set(self.hit_all().values()), {200})
        self.assertEqual(path.read_bytes(), main)
        self.assertLessEqual(set(os.listdir(str(self.codex_dir))) - names,
                             {"state_5.sqlite-shm", "state_5.sqlite-wal"})

    def test_a_wal_database_with_sidecars_keeps_its_main_file_and_gains_no_files(self):
        (self.codex_dir / "state_2.sqlite").unlink()
        path = self.make_wal_db(keep_open=True)
        before_sidecars, names = self.sidecars(path), set(os.listdir(str(self.codex_dir)))
        self.assertEqual(before_sidecars, {"state_5.sqlite-shm", "state_5.sqlite-wal"})
        main = path.read_bytes()
        self.assertEqual(set(self.hit_all().values()), {200})
        self.assertEqual(path.read_bytes(), main)
        self.assertEqual(set(os.listdir(str(self.codex_dir))), names)


class CommandTests(WebBase):
    def read_url(self, proc):
        ready, _w, _x = select.select([proc.stdout], [], [], 30)
        self.assertTrue(ready, "peers.py web printed nothing")
        return proc.stdout.readline().strip()

    def run_web(self, *args):
        return self.spawn("web", *args, stderr=subprocess.PIPE)

    def stop(self, proc, sig):
        proc.send_signal(sig)
        self.assertEqual(proc.wait(timeout=20), 0)

    def test_example_one_prints_the_url_and_serves_with_the_token(self):
        proc = self.run_web()
        url = self.read_url(proc)
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)/#token=([0-9a-f]{64})", url)
        self.assertIsNotNone(match, url)
        port, token = int(match.group(1)), match.group(2)
        self.assertGreater(port, 0)
        resp = self.exchange(
            ("GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Peers-Token: %s\r\n\r\n" % (port, token)).encode(), port)
        self.assertEqual(resp.status, 200)
        self.assertTrue({"sessions", "channels", "cursor"} <= set(resp.json()))
        denied = self.exchange(("GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port).encode(), port)
        self.assertEqual(denied.status, 403)
        self.stop(proc, signal.SIGTERM)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()

    def test_sigint_also_stops_cleanly(self):
        proc = self.run_web("--port", "0")
        self.read_url(proc)
        self.stop(proc, signal.SIGINT)

    def test_a_privileged_or_out_of_range_port_exits_2(self):
        for port in ("80", "1023", "65536", "-1", "99999"):
            with self.subTest(port=port):
                rc, out, err = self.cli("web", "--port", port)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("--port", err)

    def test_a_non_numeric_port_exits_2(self):
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stderr(io.StringIO()):
                self.cli("web", "--port", "http")
        self.assertEqual(raised.exception.code, 2)

    def test_a_port_in_use_exits_1_with_a_message(self):
        rc, out, err = self.cli("web", "--port", str(self.port))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("cannot listen", err)

    def test_help_lists_the_command(self):
        text = sp_cli.build_parser().format_help()
        self.assertRegex(text, r"web\s+local dashboard \(read-only; --chat")


if __name__ == "__main__":
    unittest.main()
