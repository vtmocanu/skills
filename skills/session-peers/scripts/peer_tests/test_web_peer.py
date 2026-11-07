"""The browser peer (`webpeer.BrowserPeer`): record, lock, cleanup and the journaled inbox."""

from __future__ import annotations

import errno
import queue

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_journal_cli import JournalCliBase
from session_peers import journal as sp_journal, messaging as sp_messaging, webpeer as sp_webpeer

EOF = object()


class RawClient:
    """One connection to a peer socket: frames out, NDJSON lines back."""

    def __init__(self, path, frames, half_close=True):
        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buf = b""
        try:
            self.conn.connect(path)
            for frame in frames:
                self.conn.sendall(json.dumps(frame).encode("utf-8") + b"\n")
            if half_close:
                self.conn.shutdown(socket.SHUT_WR)
        except BaseException:
            self.conn.close()
            raise

    def line(self, timeout=5.0):
        """The next parsed line, None on timeout, EOF at end of stream."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.conn.settimeout(left)
            try:
                chunk = self.conn.recv(4096)
            except socket.timeout:
                return None
            except ConnectionResetError:  # the peer closed with our bytes unread
                return EOF
            if not chunk:
                return EOF
            self.buf += chunk
        raw, _sep, self.buf = self.buf.partition(b"\n")
        return json.loads(raw.decode("utf-8"))

    def lines(self, timeout=5.0):
        out = []
        while True:
            item = self.line(timeout)
            if item is None or item is EOF:
                return out
            out.append(item)

    def close(self):
        with contextlib.suppress(OSError):
            self.conn.close()


class WebPeerBase(JournalCliBase):
    def setUp(self):
        super().setUp()
        self.inbound = []
        self.statuses = []
        self._clients_open = []

    def tearDown(self):
        for client in self._clients_open:
            client.close()
        super().tearDown()

    def start_peer(self, **kwargs):
        kwargs.setdefault("on_inbound", self.inbound.append)
        kwargs.setdefault("on_status", lambda frame, sender: self.statuses.append((frame, sender)))
        peer = sp_webpeer.BrowserPeer(**kwargs)
        peer.start()
        self.addCleanup(peer.stop)
        return peer

    def client(self, peer, frames, half_close=True):
        client = RawClient(peer.socket_path, frames, half_close)
        self._clients_open.append(client)
        return client

    def sender(self, name="cc-sender", entrypoint="cli"):
        """A live, verifiable sender: a listener plus a record under the parent's pid."""
        sid = new_uuid()
        listener, rec = self.add_listener(name=name, session_id=sid, pid=os.getppid())
        if entrypoint != "cli":
            rec = self.write_record(os.getppid(), name, sid, listener.path, entrypoint=entrypoint)
        return listener, rec

    @staticmethod
    def frame(body="hello", from_socket=None, msg_id=None):
        return sp_protocol.build_user_frame(body, from_socket, msg_id or new_uuid())

    def send(self, peer, frame):
        client = self.client(peer, [frame])
        return client.lines()

    def peer_records(self):
        return [r for r in sp_claude.read_claude_records() if r.get("entrypoint") == "web"]


class WebPeerRecordTests(WebPeerBase):
    def test_record_fields_mode_and_liveness(self):
        peer = self.start_peer()
        path = self.sessions / ("%d.json" % os.getpid())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.sessions.stat().st_mode), 0o700)
        rec = json.loads(path.read_text())
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["sessionId"], peer.sid)
        self.assertEqual(peer.typed, "web:" + peer.sid)
        self.assertEqual(rec["entrypoint"], "web")
        self.assertEqual(rec["kind"], "interactive")
        self.assertEqual(rec["status"], "idle")
        self.assertEqual(rec["name"], peer.name)
        self.assertEqual(rec["nameSource"], "user")
        self.assertEqual(rec["messagingSocketPath"], peer.socket_path)
        self.assertEqual(peer.socket_path, str(self.socks / ("%d.sock" % os.getpid())))
        self.assertEqual(rec["peerProtocol"], sp_constants.PEER_PROTOCOL)
        self.assertEqual(rec["peerFeatures"], sp_constants.PEER_FEATURES)
        self.assertEqual(rec["procStart"], PS_LSTART)
        self.assertEqual(rec["pidDomain"], sp_process.pid_domain())
        for field in ("cwd", "version", "startedAt", "nameSince", "updatedAt", "statusUpdatedAt"):
            self.assertIn(field, rec)
        for field in ("codexHome",):
            self.assertNotIn(field, rec)
        self.assertEqual(sp_claude.record_liveness(rec), "live")
        self.assertEqual(stat.S_IMODE(os.stat(peer.socket_path).st_mode), 0o600)
        self.assertEqual(sp_runtime.is_uuid(peer.sid), True)
        self.assertEqual(peer.sid, peer.sid.lower())

    def test_name_is_web_dash_os_user_and_sanitised(self):
        with mock.patch.object(sp_webpeer.getpass, "getuser", return_value="dev user@host"):
            self.assertEqual(sp_webpeer.peer_name(), "web-dev-user-host")
        with mock.patch.object(sp_webpeer.getpass, "getuser", side_effect=KeyError("no passwd")):
            self.assertEqual(sp_webpeer.peer_name(), "web-user")
        with mock.patch.object(sp_webpeer.getpass, "getuser", return_value="u" * 200):
            self.assertTrue(sp_constants.PEER_NAME_RE.match(sp_webpeer.peer_name()))

    def test_concurrent_reader_sees_absent_or_complete_record(self):
        peer = self.start_peer()
        stop = threading.Event()
        bad = []
        reads = [0]

        def reader():
            while not stop.is_set():
                try:
                    with open(peer.record_path, encoding="utf-8") as fh:
                        text = fh.read()
                except FileNotFoundError:
                    continue
                reads[0] += 1
                try:
                    if json.loads(text).get("sessionId") != peer.sid:
                        bad.append("wrong record")
                except ValueError:
                    bad.append(text[:60])

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                peer._write_record()
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(bad, [])
        self.assertGreater(reads[0], 0)

    def test_kill_before_the_replace_leaves_no_live_record(self):
        script = (
            "import os, signal, sys\n"
            "sys.path.insert(0, %r)\n"
            "from session_peers import webpeer\n"
            "os.replace = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)\n"
            "webpeer.BrowserPeer().start()\n" % str(HERE)
        )
        proc = subprocess.run([sys.executable, "-c", script], env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(list(self.sessions.glob("*.json")), [])
        self.assertEqual(sp_claude.live_claude_records(), [])
        # SIGKILL runs no cleanup, so only the killed writer's temp file may remain
        # (never a record); a handled failure leaves none (see the cleanup tests).
        self.assertTrue(all(".json.tmp." in p.name for p in self.sessions.iterdir()))
        # The kernel released the lock with the process: a new peer starts.
        self.start_peer()


class WebPeerLockTests(WebPeerBase):
    def lock_dir(self):
        return os.path.expanduser("~/.codex/session-peers")

    def test_second_peer_with_another_codex_home_is_refused(self):
        first = self.start_peer()
        os.environ["CODEX_HOME"] = str(self.root / "other-cx")
        second = sp_webpeer.BrowserPeer()
        errors = []

        def attempt():
            try:
                second.start()
            except BaseException as exc:  # reported below, from the test thread
                errors.append(exc)

        # A second start that blocked on the lock (no LOCK_NB) would hang here.
        thread = threading.Thread(target=attempt, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "the second start blocked instead of failing fast")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], sp_webpeer.WebPeerError)
        self.assertEqual(str(errors[0]), "another peers.py web --chat is running for this user")
        # The loser shares this pid, but must not have removed the winner's files.
        self.assertTrue(os.path.exists(first.record_path))
        self.assertTrue(os.path.exists(first.socket_path))
        with open(first.record_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["sessionId"], first.sid)
        self.assertEqual(self.send(first, self.frame())[0]["action"], "peer_inbox_ack")

    def test_lock_is_released_by_stop(self):
        first = self.start_peer()
        first.stop()
        self.start_peer()

    def test_lock_modes_are_private(self):
        self.start_peer()
        self.assertEqual(stat.S_IMODE(os.stat(self.lock_dir()).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.lock_dir(), "web-chat.lock")).st_mode), 0o600)

    def test_symlinked_lock_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.makedirs(os.path.dirname(self.lock_dir()), exist_ok=True)
        os.symlink(str(elsewhere), self.lock_dir())
        with self.assertRaises(sp_webpeer.WebPeerError):
            sp_webpeer.BrowserPeer().start()
        self.assertEqual(os.listdir(str(elsewhere)), [])
        self.assertEqual(list(self.sessions.glob("*.json")), [])

    def test_symlinked_lock_file_is_refused(self):
        os.makedirs(self.lock_dir(), mode=0o700)
        target = self.root / "target"
        target.write_text("x")
        os.symlink(str(target), os.path.join(self.lock_dir(), "web-chat.lock"))
        with self.assertRaises(sp_webpeer.WebPeerError):
            sp_webpeer.BrowserPeer().start()
        self.assertEqual(target.read_text(), "x")

    def _refused_with_euid_wrong_on_call(self, wrong_call):
        """Start with sp_journal's geteuid wrong only on its Nth call (1: directory, 2: file)."""
        real = os.geteuid
        calls = []

        def fake():
            calls.append(1)
            return real() + 1 if len(calls) == wrong_call else real()

        with mock.patch.object(sp_journal.os, "geteuid", fake):
            with self.assertRaises(sp_webpeer.WebPeerError) as ctx:
                sp_webpeer.BrowserPeer().start()
        self.assertEqual(list(self.sessions.glob("*.json")), [])
        return str(ctx.exception), len(calls)

    def test_lock_directory_with_another_owner_is_refused(self):
        os.makedirs(self.lock_dir(), mode=0o700)
        message, _calls = self._refused_with_euid_wrong_on_call(1)
        self.assertIn("is not a directory owned by this user", message)

    def test_lock_file_with_another_owner_is_refused(self):
        os.makedirs(self.lock_dir(), mode=0o700)
        message, calls = self._refused_with_euid_wrong_on_call(2)
        self.assertEqual(calls, 2)  # the directory check passed; the file check refused
        self.assertIn("web-chat.lock is not owned by this user", message)


class WebPeerCleanupTests(WebPeerBase):
    def test_stop_removes_record_and_socket_and_is_idempotent(self):
        peer = self.start_peer()
        self.assertTrue(os.path.exists(peer.record_path))
        peer.stop()
        peer.stop()
        self.assertFalse(os.path.exists(peer.record_path))
        self.assertFalse(os.path.exists(peer.socket_path))
        self.assertEqual(self.peer_records(), [])
        with self.assertRaises(OSError):
            RawClient(peer.socket_path, [])

    def test_stop_is_safe_from_a_second_thread(self):
        peer = self.start_peer()
        threads = [threading.Thread(target=peer.stop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(os.path.exists(peer.record_path))
        self.start_peer()

    def test_startup_failure_after_the_bind_cleans_everything(self):
        peer = sp_webpeer.BrowserPeer()
        with mock.patch.object(sp_webpeer.sp_runtime, "write_json_atomic", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                peer.start()
        self.assertFalse(os.path.exists(peer.socket_path))
        self.assertFalse(os.path.exists(peer.record_path))
        # The lock was released too.
        self.start_peer()

    def test_a_failed_record_replace_leaves_no_temp_file(self):
        peer = sp_webpeer.BrowserPeer()
        with mock.patch.object(sp_webpeer.sp_runtime.os, "replace", side_effect=OSError(errno.EIO, "boom")):
            with self.assertRaises(OSError):
                peer.start()
        self.assertEqual(list(self.sessions.iterdir()), [])

    def test_stop_before_start_leaks_nothing_and_start_is_refused(self):
        peer = sp_webpeer.BrowserPeer()
        peer.stop()
        with self.assertRaises(sp_webpeer.WebPeerError):
            peer.start()
        peer.stop()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.assertEqual(list(self.socks.glob("*.sock")), [])
        self.start_peer()  # the lock was never kept

    def test_request_stop_before_start_is_refused_too(self):
        peer = sp_webpeer.BrowserPeer()
        peer.request_stop()
        with self.assertRaises(sp_webpeer.WebPeerError):
            peer.start()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.start_peer()

    def test_a_stop_between_the_bind_and_the_record_write_leaves_nothing(self):
        peer = sp_webpeer.BrowserPeer()
        real_bind = peer._bind

        def bind_then_stop():
            real_bind()
            peer.request_stop()

        peer._bind = bind_then_stop
        with self.assertRaises(sp_webpeer.WebPeerError):
            peer.start()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.assertFalse(os.path.exists(peer.socket_path))
        self.start_peer()  # the lock was released

    def test_request_stop_takes_no_lock(self):
        peer = self.start_peer()
        with peer._mutex, peer.stop_event._cond:  # a handler firing inside stop() or wait()
            caller = threading.Thread(target=peer.request_stop, daemon=True)
            caller.start()
            caller.join(5)
            hung = caller.is_alive()
        self.assertFalse(hung, "request_stop blocked on a lock")
        self.assertTrue(peer.stop_requested)
        peer.stop()
        self.assertFalse(os.path.exists(peer.record_path))

    def _stop_during_the_write(self, after_write):
        peer = sp_webpeer.BrowserPeer()
        real = sp_webpeer.sp_runtime.write_json_atomic

        def write(*args, **kwargs):
            def act():
                t = threading.Thread(target=peer.stop)
                t.start()
                t.join(5)

            if not after_write:
                act()
            real(*args, **kwargs)
            if after_write:
                peer.request_stop()

        with mock.patch.object(sp_webpeer.sp_runtime, "write_json_atomic", write):
            with self.assertRaises(sp_webpeer.WebPeerError):
                peer.start()
        peer.stop()
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.assertFalse(os.path.exists(peer.socket_path))

    def test_a_stop_on_another_thread_before_the_record_write_leaves_no_record(self):
        self._stop_during_the_write(after_write=False)

    def test_a_request_stop_during_the_record_write_leaves_no_record(self):
        self._stop_during_the_write(after_write=True)

    def _stop_on_another_thread_during(self, target, attr):
        peer = sp_webpeer.BrowserPeer()
        real = getattr(target, attr)

        def acquire_then_stop(*args, **kwargs):
            out = real(*args, **kwargs)
            t = threading.Thread(target=peer.stop)
            t.start()
            t.join(5)
            return out

        with mock.patch.object(target, attr, acquire_then_stop):
            with self.assertRaises(sp_webpeer.WebPeerError):
                peer.start()
        self.assertFalse(os.path.exists(peer.socket_path))
        self.assertEqual(list(self.sessions.iterdir()), [])
        # The lock was released: a new peer in this process starts.
        self.start_peer()

    def test_a_stop_on_another_thread_while_taking_the_lock_releases_it(self):
        self._stop_on_another_thread_during(sp_webpeer.BrowserPeer, "_take_lock")

    def test_a_stop_that_cleans_up_before_the_lock_is_recorded_still_releases_it(self):
        # The stop's cleanup runs inside _take_lock, before the fd is recorded:
        # only start's forced cleanup can release that lock.
        peer = sp_webpeer.BrowserPeer()
        real = sp_webpeer.fcntl.flock

        def flock_then_stop(fd, op):
            out = real(fd, op)
            t = threading.Thread(target=peer.stop)
            t.start()
            t.join(5)
            return out

        with mock.patch.object(sp_webpeer.fcntl, "flock", flock_then_stop):
            with self.assertRaises(sp_webpeer.WebPeerError):
                peer.start()
        self.assertIsNone(peer._lock_fd)
        self.start_peer()  # the lock was released

    def test_a_stop_on_another_thread_while_binding_releases_socket_and_lock(self):
        self._stop_on_another_thread_during(sp_webpeer.BrowserPeer, "_bind")

    def test_stop_shuts_down_accepted_connections_and_joins_its_threads(self):
        peer = self.start_peer()
        idle = self.client(peer, [], half_close=False)  # blocked in recv, default timeout is long
        time.sleep(0.2)
        peer.stop()
        self.assertIs(idle.line(3), EOF)
        self.assertFalse(peer._accept.is_alive())
        self.assertFalse(peer._notifier.is_alive())

    def test_a_stop_during_the_journal_write_closes_unacknowledged_and_skips_on_inbound(self):
        peer = self.start_peer()
        real = sp_journal.record_message

        def record_then_stop(*args, **kwargs):
            out = real(*args, **kwargs)
            peer.request_stop()
            return out

        with mock.patch.object(sp_journal, "record_message", record_then_stop):
            client = self.client(peer, [self.frame("late")])
            self.assertIs(client.line(5), EOF)
        self.assertEqual(self.inbound, [])

    def test_an_unsafe_socket_directory_is_a_web_peer_error(self):
        elsewhere = self.root / "elsewhere-socks"
        elsewhere.mkdir(mode=0o700)
        link = self.root / "link-socks"
        os.symlink(str(elsewhere), str(link))
        peer = sp_webpeer.BrowserPeer()
        peer.sock_dir = str(link)
        with self.assertRaises(sp_webpeer.WebPeerError) as ctx:
            peer.start()
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(self.sessions.iterdir()), [])
        self.start_peer()

    def test_startup_failure_in_the_bind_releases_the_lock(self):
        peer = sp_webpeer.BrowserPeer()
        with mock.patch.object(sp_webpeer.sp_claude, "socket_path_ok", return_value=False):
            with self.assertRaises(sp_webpeer.WebPeerError):
                peer.start()
        self.start_peer()

    def test_sigterm_in_a_subprocess_cleans_up(self):
        script = (
            "import signal, sys\n"
            "sys.path.insert(0, %r)\n"
            "from session_peers.webpeer import BrowserPeer\n"
            "p = BrowserPeer().start()\n"
            "signal.signal(signal.SIGTERM, lambda s, f: p.request_stop())\n"
            "print(p.sid, flush=True)\n"
            "try:\n"
            "    p.wait_for_stop(60)\n"
            "finally:\n"
            "    p.stop()\n" % str(HERE)
        )
        proc = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._children.append(proc)
        sid = proc.stdout.readline().strip()
        self.assertTrue(sp_runtime.is_uuid(sid))
        self.assertEqual([r["sessionId"] for r in self.peer_records()], [sid])
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertEqual(list(self.sessions.glob("*.json")), [])
        self.assertEqual(list(self.socks.glob("*.sock")), [])
        self.start_peer()


class WebPeerInboxTests(WebPeerBase):
    def test_a_frame_is_journaled_shown_and_acknowledged(self):
        peer = self.start_peer()
        frame = self.frame("hello browser")
        lines = self.send(peer, frame)
        self.assertEqual(lines, [{"type": "control", "action": "peer_inbox_ack", "orig_msg_id": frame["msg_id"]}])
        self.assertEqual(len(self.inbound), 1)
        entry = self.inbound[0]
        self.assertEqual(entry["body"], "hello browser")
        self.assertEqual(entry["orig_msg_id"], frame["msg_id"])
        self.assertNotEqual(entry["message_id"], frame["msg_id"])
        self.assertIsNone(entry["sender"])
        self.assertIsNone(entry["sender_name"])
        self.assertIsInstance(entry["at"], float)
        journaled = self.entry(entry["message_id"])
        self.assertEqual(journaled["to"], [peer.typed])
        self.assertIsNone(journaled["from"])

    def test_accepts_through_the_send_frame_client(self):
        peer = self.start_peer()
        sp_claude.send_frame(peer.socket_path, self.frame("one way"))
        self.assertTrue(wait_for(lambda: self.inbound))
        self.assertEqual(self.inbound[0]["body"], "one way")

    def test_cli_send_exits_0_on_the_ack(self):
        peer = self.start_peer()
        rc, _out, err = self.cli("send", "--to", peer.typed, "--message", "via the cli")
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["body"].endswith("via the cli") for e in self.inbound], [True])

    def test_cli_send_exits_1_with_the_refusal_when_the_journal_fails(self):
        peer = self.start_peer()
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal is full")):
            rc, _out, err = self.cli("send", "--to", peer.typed, "--message", "refused")
        self.assertEqual(rc, 1)
        self.assertIn("journal is full", err)
        self.assertIn("not delivered", err)
        self.assertEqual(self.inbound, [])

    def test_verified_claude_sender_is_typed_cc(self):
        peer = self.start_peer()
        listener, rec = self.sender()
        lines = self.send(peer, self.frame("from cc", listener.path))
        self.assertEqual(lines[0]["action"], "peer_inbox_ack")
        entry = self.inbound[0]
        self.assertEqual(entry["sender"], "cc:" + rec["sessionId"])
        self.assertEqual(entry["sender_name"], "cc-sender")
        self.assertEqual(self.entry(entry["message_id"])["from"], "cc:" + rec["sessionId"])

    def test_verified_codex_shim_sender_is_typed_codex(self):
        peer = self.start_peer()
        listener, rec = self.sender(name="codex-x", entrypoint="codex")
        self.send(peer, self.frame("from codex", listener.path))
        self.assertEqual(self.inbound[0]["sender"], "codex:" + rec["sessionId"])

    def test_unverified_senders_are_journaled_as_none(self):
        peer = self.start_peer()
        real, rec = self.sender()
        spoof = sp_protocol.build_wrapper("spoofed", str(self.socks / "nobody.sock"), new_uuid(), "cc-boss")
        frames = [
            sp_protocol.build_user_frame(spoof, str(self.socks / "nobody.sock"), new_uuid()),
            # A claimed session id and name with no verifiable socket.
            sp_protocol.build_user_frame(
                sp_protocol.build_wrapper("claimed", real.path + ".other", rec["sessionId"], rec["name"]),
                None, new_uuid(),
            ),
            # A reply address outside the allowlisted directories.
            sp_protocol.build_user_frame("outside", "/etc/passwd", new_uuid()),
            self.frame("no from"),
        ]
        for frame in frames:
            self.assertEqual(self.send(peer, frame)[0]["action"], "peer_inbox_ack")
        self.assertEqual([e["sender"] for e in self.inbound], [None] * 4)
        self.assertEqual([e["sender_name"] for e in self.inbound], [None] * 4)
        self.assertEqual([self.entry(e["message_id"])["from"] for e in self.inbound], [None] * 4)

    def test_a_browser_record_is_not_a_verified_sender(self):
        peer = self.start_peer()
        self.send(peer, self.frame("self", peer.socket_path))
        self.assertIsNone(self.inbound[0]["sender"])

    def test_a_non_uuid_msg_id_is_accepted_with_a_fresh_id(self):
        peer = self.start_peer()
        lines = self.send(peer, self.frame("odd id", msg_id="not-a-uuid"))
        self.assertEqual(lines, [{"type": "control", "action": "peer_inbox_ack", "orig_msg_id": "not-a-uuid"}])
        entry = self.inbound[0]
        self.assertEqual(entry["orig_msg_id"], "not-a-uuid")
        self.assertTrue(sp_runtime.is_uuid(entry["message_id"]))
        self.assertIsNotNone(self.entry(entry["message_id"]))

    def test_an_already_journaled_msg_id_gets_a_fresh_record(self):
        peer = self.start_peer()
        taken = new_uuid()
        sp_journal.record_message(None, [peer.typed], "earlier", message_id=taken)
        lines = self.send(peer, self.frame("reused", msg_id=taken))
        self.assertEqual(lines[0]["action"], "peer_inbox_ack")
        self.assertEqual(lines[0]["orig_msg_id"], taken)
        entry = self.inbound[0]
        self.assertEqual(entry["orig_msg_id"], taken)
        self.assertNotEqual(entry["message_id"], taken)
        self.assertEqual(self.entry(taken)["body"], "earlier")
        self.assertEqual(self.entry(entry["message_id"])["body"], "reused")

    def test_a_reused_id_whose_write_fails_is_not_shown(self):
        peer = self.start_peer()
        taken = new_uuid()
        sp_journal.record_message(None, [peer.typed], "earlier", message_id=taken)
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalRefused("no")):
            lines = self.send(peer, self.frame("reused", msg_id=taken))
        self.assertEqual([l["action"] for l in lines], ["peer_inbox_refused"])
        self.assertEqual(self.inbound, [])
        self.assertEqual(self.entry(taken)["body"], "earlier")

    def test_empty_bodies_and_junk_are_ignored(self):
        peer = self.start_peer()
        client = self.client(peer, [self.frame("   ")], half_close=False)
        client.conn.sendall(b"not json\n[1]\n")
        client.conn.shutdown(socket.SHUT_WR)
        self.assertEqual(client.lines(2.0), [])
        self.assertEqual(self.inbound, [])


class WebPeerControlTests(WebPeerBase):
    def test_idle_notice_and_unknown_controls_are_ignored(self):
        peer = self.start_peer()
        frame = self.frame("after controls")
        client = self.client(peer, [
            {"type": "auth", "token": "ignored"},
            {"type": "control", "action": "peer_idle_notice", "orig_msg_id": new_uuid()},
            {"type": "control", "action": "something_new"},
            {"type": "mystery"},
            frame,
        ])
        lines = client.lines()
        self.assertEqual([l["action"] for l in lines], ["peer_inbox_ack"])
        self.assertEqual(self.statuses, [])
        self.assertEqual(len(self.inbound), 1)

    def test_peer_message_status_reaches_on_status_with_the_verified_sender(self):
        peer = self.start_peer()
        listener, rec = self.sender()
        status = {"type": "control", "action": "peer_message_status", "orig_msg_id": new_uuid(),
                  "status": "failed", "detail": "x", "from": "uds:" + listener.path}
        anonymous = dict(status, orig_msg_id=new_uuid(), **{"from": "uds:" + str(self.socks / "nobody.sock")})
        self.client(peer, [status, anonymous])
        self.assertTrue(wait_for(lambda: len(self.statuses) == 2))
        by_id = {f["orig_msg_id"]: sender for f, sender in self.statuses}
        self.assertEqual(by_id[status["orig_msg_id"]], "cc:" + rec["sessionId"])
        self.assertIsNone(by_id[anonymous["orig_msg_id"]])
        self.assertEqual(self.inbound, [])

    def test_status_frames_reach_on_status_sanitised_in_a_new_dict(self):
        peer = self.start_peer()
        raw = {"type": "control", "action": "peer_message_status", "orig_msg_id": "bad\nid",
               "status": "fa\x1b[31mil\ned" + "z" * 100, "detail": "d\x1b[2J\n" + "y" * 1000,
               "extra": "x"}
        self.client(peer, [raw])
        self.assertTrue(wait_for(lambda: self.statuses))
        got = self.statuses[0][0]
        self.assertIsNot(got, raw)
        self.assertNotIn("extra", got)
        self.assertIsNone(got["orig_msg_id"])
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9._-]{1,32}", got["status"]), got["status"])
        self.assertLessEqual(len(got["detail"]), 512)
        self.assertTrue(got["detail"].isprintable())
        odd = dict(raw, orig_msg_id=7, status=["x"], detail={"a": 1})
        self.client(peer, [odd])
        self.assertTrue(wait_for(lambda: len(self.statuses) == 2))
        self.assertEqual(
            [self.statuses[1][0][k] for k in ("orig_msg_id", "status", "detail")], [None, None, None])


class WebPeerOrderingTests(WebPeerBase):
    def test_the_ack_waits_for_the_journal_write(self):
        peer = self.start_peer()
        entered, release = threading.Event(), threading.Event()
        real = sp_journal.record_message
        shown_at_ack = []

        def gated(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return real(*args, **kwargs)

        frame = self.frame("gated")
        with mock.patch.object(sp_journal, "record_message", gated):
            client = self.client(peer, [frame])
            self.assertTrue(entered.wait(5))
            self.assertIsNone(client.line(0.4), "an ack or refusal was written before the journal returned")
            self.assertEqual(self.inbound, [])
            release.set()
            line = client.line(5)
            shown_at_ack.append(len(self.inbound))
        self.assertEqual(line["action"], "peer_inbox_ack")
        self.assertEqual(shown_at_ack, [1])
        self.assertIsNotNone(self.entry(self.inbound[0]["message_id"]))

    def test_an_fsync_failure_window_shows_and_acknowledges_nothing(self):
        peer = self.start_peer()
        listener, rec = self.sender()
        sp_journal.record_message(None, [peer.typed], "seed")  # the journal files exist
        entered, release = threading.Event(), threading.Event()
        real_fsync = os.fsync
        calls = []

        def failing_fsync(fd):
            if not calls:
                calls.append(fd)
                entered.set()
                release.wait(10)
                raise OSError(errno.EIO, "injected fsync failure")
            return real_fsync(fd)

        frame = self.frame("never kept", listener.path)
        with mock.patch.object(sp_journal.os, "fsync", failing_fsync):
            client = self.client(peer, [frame])
            self.assertTrue(entered.wait(5))
            self.assertIsNone(client.line(0.4))
            self.assertEqual(self.inbound, [])
            release.set()
            lines = client.lines()
        self.assertEqual([l["action"] for l in lines], ["peer_inbox_refused"])
        self.assertEqual(lines[0]["orig_msg_id"], frame["msg_id"])
        self.assertEqual(self.inbound, [])
        self.assertNotIn("never kept", [r.get("body") for r in self.records()])
        # The verified sender learns of it: a failed status and a one-time notice.
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertTrue(status)
        self.assertEqual(status[0]["status"], "failed")
        self.assertEqual(status[0]["orig_msg_id"], frame["msg_id"])
        self.assertEqual(status[0]["from"], "uds:" + peer.socket_path)
        notices = wait_for(lambda: listener.of_type("user"))
        self.assertEqual(len(notices), 1)
        text = json.dumps(notices[0])
        self.assertIn("[session-peers] your message to %s was not kept:" % peer.name, text)
        self.assertNotIn("from", notices[0])  # no reply route

    def test_journal_full_is_refused_and_reported_to_a_verified_sender(self):
        peer = self.start_peer()
        listener, rec = self.sender()
        frame = self.frame("too much", listener.path)
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal is full")):
            lines = self.send(peer, frame)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["action"], "peer_inbox_refused")
        self.assertIn("journal is full", lines[0]["detail"])
        self.assertEqual(self.inbound, [])
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertEqual(status[0]["status"], "failed")
        self.assertIn("journal is full", status[0]["detail"])
        self.assertTrue(wait_for(lambda: listener.of_type("user")))

    def test_an_unverified_sender_gets_the_refusal_line_only(self):
        peer = self.start_peer()
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal is full")):
            lines = self.send(peer, self.frame("nobody"))
        self.assertEqual([l["action"] for l in lines], ["peer_inbox_refused"])
        self.assertEqual(self.inbound, [])

    def test_a_sender_that_hung_up_does_not_break_the_inbox(self):
        peer = self.start_peer()
        client = self.client(peer, [self.frame("hang up")], half_close=False)
        client.close()
        self.assertTrue(wait_for(lambda: self.inbound))
        self.assertEqual(self.send(peer, self.frame("still works"))[0]["action"], "peer_inbox_ack")

    def test_on_inbound_failure_closes_without_an_ack_or_refusal(self):
        def boom(_entry):
            raise RuntimeError("feed broke")

        peer = self.start_peer(on_inbound=boom)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            client = self.client(peer, [self.frame(), self.frame("second")])
            self.assertIs(client.line(5), EOF)  # closed, nothing written
        self.assertIn("on_inbound failed", err.getvalue())

    def test_a_foreign_uid_client_is_dropped_before_anything_happens(self):
        peer = self.start_peer()
        sp_journal.record_message(None, [peer.typed], "seed")
        before = len(self.records())
        with mock.patch.object(sp_process, "peer_uid", lambda _conn: os.getuid() + 1):
            lines = self.send(peer, self.frame("intruder"))
        self.assertEqual(lines, [])
        self.assertEqual(self.inbound, [])
        self.assertEqual(len(self.records()), before)
        self.assertNotIn("intruder", [r.get("body") for r in self.records()])
        # The same peer serves a client with our uid afterwards.
        self.assertEqual(self.send(peer, self.frame("ours"))[0]["action"], "peer_inbox_ack")

    def test_an_unsafe_msg_id_is_dropped_from_the_log_frames_and_feed(self):
        peer = self.start_peer()
        evil = "abc\n[session-peers] forged\x1b[31m‮evil\r"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("full\x1b[2J\nx")):
                refused = self.send(peer, self.frame("refused", msg_id=evil))
            accepted = self.send(peer, self.frame("kept", msg_id=evil))
        self.assertEqual(refused[0]["orig_msg_id"], None)
        self.assertNotIn("\x1b", refused[0]["detail"])
        self.assertNotIn("\n", refused[0]["detail"])
        self.assertEqual(accepted, [{"type": "control", "action": "peer_inbox_ack", "orig_msg_id": None}])
        self.assertIsNone(self.inbound[0]["orig_msg_id"])
        logged = err.getvalue()
        self.assertIn("not keeping message", logged)
        for ch in logged:
            self.assertTrue(ch == "\n" or ch.isprintable(), "raw %r in the log" % ch)
        self.assertNotIn("\n[session-peers] forged", logged)
        self.assertNotIn("forged", [line.strip() for line in logged.splitlines()])
        for ok in ("0123-abc", "a" * 128, "id:1.2_x"):
            self.assertEqual(self.send(peer, self.frame("ok", msg_id=ok))[0]["orig_msg_id"], ok)
        self.assertIsNone(self.send(peer, self.frame("long", msg_id="a" * 129))[0]["orig_msg_id"])
        self.assertIsNone(self.send(peer, dict(self.frame("empty"), msg_id=""))[0]["orig_msg_id"])

    def test_the_notice_is_sent_once_per_failure_episode(self):
        peer = self.start_peer()
        listener, _rec = self.sender()
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal is full")):
            for i in range(5):
                lines = self.send(peer, self.frame("m%d" % i, listener.path))
                self.assertEqual(lines[0]["action"], "peer_inbox_refused")
        self.assertTrue(wait_for(lambda: len(listener.of_type("control", "peer_message_status")) == 5))
        self.assertTrue(wait_for(lambda: listener.of_type("user")))
        time.sleep(0.3)
        self.assertEqual(len(listener.of_type("user")), 1)
        # A successful write ends the episode: the next failure notifies again.
        self.assertEqual(self.send(peer, self.frame("fine", listener.path))[0]["action"], "peer_inbox_ack")
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal is full")):
            self.send(peer, self.frame("again", listener.path))
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 2))

    def fake_sender(self, n):
        """A listener and record on its own pid, for calling `_refuse` directly."""
        return self.add_listener(name="sender-%d" % n, session_id=new_uuid(), pid=os.getpid() + n)

    def test_one_senders_success_does_not_reset_another_senders_notice(self):
        peer = self.start_peer()
        a, rec_a = self.fake_sender(1)
        typed_a = "cc:" + rec_a["sessionId"]
        exc = sp_journal.JournalFull("journal is full")
        peer._refuse(mock.Mock(), None, typed_a, rec_a, exc)
        self.assertTrue(wait_for(lambda: len(a.of_type("user")) == 1))
        # Another sender's journaled message must not end A's episode.
        self.assertEqual(self.send(peer, self.frame("b ok"))[0]["action"], "peer_inbox_ack")
        peer._refuse(mock.Mock(), None, typed_a, rec_a, exc)
        time.sleep(0.5)
        self.assertEqual(len(a.of_type("user")), 1)

    def test_a_success_from_the_same_sender_starts_a_new_episode(self):
        peer = self.start_peer()
        listener, rec = self.sender()
        typed = "cc:" + rec["sessionId"]
        peer._refuse(mock.Mock(), None, typed, rec, sp_journal.JournalFull("full"))
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 1))
        self.assertEqual(self.send(peer, self.frame("ok", listener.path))[0]["action"], "peer_inbox_ack")
        self.assertNotIn(typed, peer._notified)

    def test_the_notified_cap_evicts_one_entry_not_all(self):
        peer = self.start_peer()
        with mock.patch.object(sp_webpeer, "_NOTIFIED_MAX", 2):
            for n in (1, 2, 3):
                _listener, rec = self.fake_sender(n)
                peer._refuse(mock.Mock(), None, "cc:x%d" % n, rec, sp_journal.JournalFull("full"))
        self.assertEqual(list(peer._notified), ["cc:x2", "cc:x3"])

    def test_a_dropped_notice_does_not_count_as_notified(self):
        peer = self.start_peer()
        _listener, rec = self.fake_sender(1)
        exc = sp_journal.JournalFull("full")
        full = queue.Queue(maxsize=1)
        full.put_nowait(("filler", {}))
        peer._notices = full
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            peer._refuse(mock.Mock(), None, "cc:x1", rec, exc)
        self.assertIn("queue full", err.getvalue())
        self.assertNotIn("cc:x1", peer._notified)
        full.get_nowait()
        peer._refuse(mock.Mock(), None, "cc:x1", rec, exc)
        self.assertIn("cc:x1", peer._notified)

    def test_a_delivery_failure_log_quotes_the_record_name(self):
        rec = {"name": "evil\nforged line", "pid": 1, "messagingSocketPath": str(self.socks / "dead.sock")}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(sp_claude.deliver_to_record(rec, {"type": "user"}))
        logged = err.getvalue()
        self.assertIn("delivery to", logged)
        self.assertEqual(len([l for l in logged.splitlines() if l.strip()]), 1, logged)
        self.assertIn("'evil\\nforged line'", logged)

    def test_the_client_cap_refuses_extra_connections(self):
        with mock.patch.object(sp_constants, "MAX_CONCURRENT_CLIENTS", 1):
            peer = self.start_peer()
        holder = self.client(peer, [], half_close=False)  # holds the only slot, sends nothing
        time.sleep(0.2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                extra = self.client(peer, [self.frame("over the cap")])
            except OSError:  # the peer may close the refused connection before our write
                pass
            else:
                self.assertIs(extra.line(5), EOF)
            self.assertTrue(wait_for(lambda: "1 already in flight" in err.getvalue(), timeout=5))
        self.assertEqual(self.inbound, [])
        holder.close()

        def acked_after_release():
            try:  # the holder's slot may not be released yet: a refused try is retried
                return self.send(peer, self.frame("after"))[:1] == [
                    {"type": "control", "action": "peer_inbox_ack", "orig_msg_id": mock.ANY}]
            except OSError:
                return False

        self.assertTrue(wait_for(acked_after_release))

    def test_a_silent_connection_times_out_and_frees_its_slot(self):
        with mock.patch.object(sp_constants, "CONN_TIMEOUT", 0.3):
            peer = self.start_peer()
            idle = self.client(peer, [], half_close=False)
            self.assertIs(idle.line(5), EOF)
