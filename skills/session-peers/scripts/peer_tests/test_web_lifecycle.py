"""Stable URLs are opt-in; reuse and signals require the exact owned server identity."""

import json
import http.client
import os
import signal
import socket
import stat
import subprocess
import select
import time
import errno
import types
import unittest
import sys
from unittest import mock

from peer_tests.support import Base, new_uuid, sp_process
from session_peers import web, web_lifecycle as lifecycle
from peer_tests.test_web_server import WebBase


class StableWebTests(Base):
    def setUp(self):
        super().setUp()
        self.owned = None

    def tearDown(self):
        # Stop only the daemon this fixture successfully launched, with its original identity.
        try:
            candidates = [self.owned] if self.owned is not None else []
            if (lifecycle.root() / "server.json").exists():
                current = lifecycle.read(lifecycle.root() / "server.json")
                cfg = lifecycle.read(lifecycle.root() / "config.json")
                if current and cfg:
                    candidates.insert(0, (cfg, current))
            seen = set()
            for cfg, record in candidates:
                if record["instance"] in seen:
                    continue
                seen.add(record["instance"])
                lifecycle.stop(record, cfg)
        finally:
            super().tearDown()

    def call(self, *args):
        child = self.spawn("web", *args, stderr=subprocess.PIPE)
        out, err = child.communicate(timeout=20)
        return child.returncode, out.strip(), err

    def configured(self):
        with lifecycle.locked():
            _path, cfg = lifecycle.config()
        return cfg

    def launch(self):
        rc, address, err = self.call("--ensure")
        self.assertEqual(rc, 0, err)
        cfg = self.configured()
        record = lifecycle.read(lifecycle.root() / "server.json")
        self.owned = (dict(cfg), dict(record))
        self.assertEqual(address, lifecycle.url(cfg))
        return cfg, record

    def test_parent_exits_server_survives_and_ensure_reuses_the_same_url(self):
        cfg, record = self.launch()
        self.assertEqual(os.getsid(record["pid"]), record["pid"], "daemon has its own session")
        self.assertEqual(lifecycle.health(cfg)["instance"], record["instance"])
        self.assertTrue(lifecycle.verified(record, cfg))
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "another-home")}):
            rc, address, err = self.call("--ensure")
        self.assertEqual(rc, 0, err)
        self.assertEqual(address, lifecycle.url(cfg))
        self.assertEqual(lifecycle.read(lifecycle.root() / "server.json"), record)
        for file in ("config.json", "server.json", "server.log"):
            self.assertEqual(stat.S_IMODE((lifecycle.root() / file).stat().st_mode), 0o600)
        self.assertEqual(self.call("--stop")[0], 0)
        self.assertIsNone(lifecycle.health(cfg))
        self.assertIsNone(lifecycle.read(lifecycle.root() / "server.json"))
        self.owned = None

    def test_managed_log_never_contains_the_token_and_is_truncated_on_start(self):
        cfg = self.configured()
        log = lifecycle.root() / "server.log"
        log.write_text("old log content that must not accumulate\n")
        self.launch()
        text = log.read_text()
        self.assertNotIn(cfg["token"], text)
        self.assertNotIn("old log content", text)

    def test_foreign_wildcard_listener_is_never_cobound(self):
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign.bind(("0.0.0.0", 0))
            foreign.listen()
            port = foreign.getsockname()[1]
            rc, _out, err = self.call("--ensure", "--port", str(port))
            self.assertEqual(rc, 1, err)
            self.assertEqual(foreign.getsockname()[1], port)

    def test_bound_wildcard_reservation_semantics_are_platform_explicit(self):
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign.bind(("0.0.0.0", 0))
            port = foreign.getsockname()[1]
            rc, _out, err = self.call("--ensure", "--port", str(port))
            if sys.platform.startswith("linux"):
                self.assertEqual(rc, 0, err)
                with self.assertRaises(OSError) as refused:
                    foreign.listen()
                self.assertEqual(refused.exception.errno, errno.EADDRINUSE)
            else:
                self.assertEqual(rc, 1, err)
            self.assertEqual(foreign.getsockname(), ("0.0.0.0", port))

    def test_immediate_restart_after_stop_keeps_url_within_start_budget(self):
        cfg, record = self.launch()
        self.assertEqual(self.call("--stop")[0], 0)
        self.owned = None
        started = time.monotonic()
        restarted, current = self.launch()
        self.assertLess(time.monotonic() - started, lifecycle.WAIT)
        self.assertEqual(restarted, cfg)
        self.assertNotEqual(current["instance"], record["instance"])

    def test_live_and_unknown_port_probes_refuse_before_server_construction(self):
        class NeverServer:
            def __init__(self, *_args, **_kw):
                raise AssertionError("must not bind before a refused probe")
        for code in (0, errno.ETIMEDOUT, errno.EACCES):
            with mock.patch.object(lifecycle, "port_probe", return_value=code), \
                    self.assertRaisesRegex(lifecycle.LifecycleError, "holder"):
                lifecycle.stable_server(types.SimpleNamespace(Server=NeverServer), 8765)

    def test_wrong_bound_address_is_closed_and_refused(self):
        created = []
        class WrongServer:
            def __init__(self, *_args, **_kw):
                self.server_address = ("0.0.0.0", 8765)
                self.server_close = mock.Mock()
                created.append(self)
        with mock.patch.object(lifecycle, "port_probe", side_effect=[errno.ECONNREFUSED, 0]), \
                self.assertRaisesRegex(lifecycle.LifecycleError, "exact loopback"):
            lifecycle.stable_server(types.SimpleNamespace(Server=WrongServer), 8765)
        created[0].server_close.assert_called_once()

    def test_failed_start_kills_and_reaps_the_child_and_releases_inherited_lock(self):
        cfg = self.configured()
        real_popen = subprocess.Popen
        children = []
        def delayed(_args, **kw):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(25)"], **kw)
            children.append(child)
            self._children.append(child)
            return child
        with lifecycle.locked() as fd, mock.patch.object(lifecycle, "WAIT", 0.15), \
                mock.patch.object(lifecycle.subprocess, "Popen", side_effect=delayed):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.ensure(cfg, fd)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll(), "timed-out child must not keep the inherited lock")
        self.assertEqual(self.call("--url")[0], 0)

    def test_lock_wait_allows_more_than_the_start_budget(self):
        wait = lifecycle.WAIT
        with mock.patch.object(lifecycle.time, "monotonic", side_effect=[0, 0.75 * wait, 1.25 * wait]), \
                mock.patch.object(lifecycle.time, "sleep"), \
                mock.patch.object(lifecycle.fcntl, "flock", side_effect=[BlockingIOError(), BlockingIOError(), None]) as lock:
            with lifecycle.locked():
                pass
        self.assertEqual(lock.call_count, 3)

    def test_unverified_identity_errors_explain_manual_recovery(self):
        self.launch()
        with mock.patch.object(lifecycle, "process_state", return_value="unverified"):
            for args in (("--ensure",), ("--stop",), ("--rotate-token",)):
                rc, _out, err = self.cli("web", *args)
                self.assertEqual(rc, 1)
                self.assertIn("server.json", err)
                self.assertIn("remove", err)
                self.assertIn("inspect", err.lower())

    def test_concurrent_ensure_launchers_return_one_server(self):
        launchers = [self.spawn("web", "--ensure", stderr=subprocess.PIPE) for _ in range(2)]
        results = [(p, p.communicate(timeout=20)) for p in launchers]
        cfg = self.configured()
        record = lifecycle.read(lifecycle.root() / "server.json")
        self.owned = (cfg, record)
        for p, (out, err) in results:
            self.assertEqual(p.returncode, 0, err)
            self.assertEqual(out.strip(), lifecycle.url(cfg))
        self.assertTrue(lifecycle.verified(record, cfg))

    def test_url_creates_private_configuration_but_no_server(self):
        rc, address, err = self.call("--url")
        self.assertEqual(rc, 0, err)
        cfg = self.configured()
        self.assertEqual(address, lifecycle.url(cfg))
        self.assertIsNone(lifecycle.read(lifecycle.root() / "server.json"))
        self.assertIsNone(lifecycle.health(cfg))
        self.assertEqual(stat.S_IMODE(lifecycle.root().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((lifecycle.root() / "config.json").stat().st_mode), 0o600)

    def test_url_does_not_probe_process_or_http_health(self):
        cfg = self.configured()
        lifecycle.write(lifecycle.root() / "server.json", {"pid": 99999999, "pid_domain": sp_process.pid_domain(), "instance": "old"})
        with mock.patch.object(lifecycle, "process_state", side_effect=AssertionError("process probe")), \
                mock.patch.object(lifecycle, "health", side_effect=AssertionError("HTTP probe")):
            rc, address, err = self.cli("web", "--url")
        self.assertEqual(rc, 0, err)
        self.assertEqual(address.strip(), lifecycle.url(cfg))

    def test_fifo_log_is_refused_without_waiting_for_a_reader(self):
        self.configured()
        os.mkfifo(lifecycle.root() / "server.log", 0o600)
        rc, _out, err = self.call("--ensure")
        self.assertEqual(rc, 1)
        self.assertTrue(err)
        self.assertIsNone(lifecycle.read(lifecycle.root() / "server.json"))

    def test_first_use_falls_forward_once_but_a_foreign_holder_never_moves_saved_port(self):
        with socket.socket() as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            port = foreign.getsockname()[1]
            with mock.patch.object(lifecycle, "first_port", return_value=port):
                cfg = self.configured()
            rc, _out, err = self.call("--ensure")
            self.assertEqual(rc, 1)
            self.assertIn("port", err)
            self.assertEqual(self.configured(), cfg)
        with mock.patch.object(socket.socket, "bind", side_effect=[OSError("busy"), None]):
            self.assertEqual(lifecycle.first_port(8765), 8766)

    def test_stale_pidfile_starts_a_new_verified_instance(self):
        self.configured()
        lifecycle.write(lifecycle.root() / "server.json", {"pid": 99999999, "pid_domain": sp_process.pid_domain(),
                                                          "proc_start": "old", "instance": "old"})
        _cfg, record = self.launch()
        self.assertNotEqual(record["instance"], "old")

    def test_rotation_and_port_change_require_stopping_and_never_start_a_server(self):
        cfg, _record = self.launch()
        for args in (("--rotate-token",), ("--ensure", "--port", str(cfg["port"] + 1))):
            rc, _out, err = self.call(*args)
            self.assertEqual(rc, 1)
            self.assertIn("--stop", err)
            self.assertEqual(self.configured(), cfg)
        self.assertEqual(self.call("--stop")[0], 0)
        self.owned = None
        rc, address, err = self.call("--rotate-token")
        self.assertEqual(rc, 0, err)
        changed = self.configured()
        self.assertNotEqual(cfg["token"], changed["token"])
        self.assertEqual(cfg["port"], changed["port"])
        self.assertEqual(address, lifecycle.url(changed))
        self.assertIsNone(lifecycle.health(changed))
        fresh, _record = self.launch()
        self.assertIsNone(lifecycle.health(dict(fresh, token=cfg["token"])))
        self.assertEqual(self.call("--stop")[0], 0)
        self.owned = None
        with socket.socket() as pick:
            pick.bind(("127.0.0.1", 0))
            port = pick.getsockname()[1]
        self.assertEqual(self.call("--url", "--port", str(port))[0], 0)
        self.assertEqual(self.configured()["port"], port)

    def test_health_never_discloses_the_saved_token_to_a_foreign_listener(self):
        cfg = self.configured()
        with mock.patch.object(lifecycle.http.client, "HTTPConnection") as client:
            response = client.return_value.getresponse.return_value
            response.status = 200
            for value in ({"proof": "bad"}, {"proof": "é"}, {"proof": "b" * 64, "service": "foreign"}):
                response.read.return_value = json.dumps(value).encode()
                self.assertIsNone(lifecycle.health(cfg))
            args, kw = client.return_value.request.call_args
            self.assertEqual(args[:2], ("GET", "/api/instance"))
            self.assertNotIn("X-Peers-Token", kw["headers"])
            self.assertNotIn(cfg["token"], json.dumps(kw["headers"]))

    def test_health_proofs_do_not_authorize_data_routes(self):
        cfg, _record = self.launch()
        nonce = "a" * 32
        conn = http.client.HTTPConnection("127.0.0.1", cfg["port"], timeout=2)
        try:
            conn.request("GET", "/api/state", headers={"X-Peers-Probe": nonce,
                                                       "X-Peers-Proof": lifecycle.probe_proof(cfg["token"], nonce)})
            self.assertEqual(conn.getresponse().status, 403)
        finally:
            conn.close()

    def test_stop_checks_process_start_and_authenticated_instance_before_signalling(self):
        cfg, record = self.launch()
        for tamper in (dict(record, proc_start="wrong start"), dict(record, instance="wrong instance")):
            with mock.patch.object(lifecycle.os, "kill") as kill:
                if tamper["proc_start"] != record["proc_start"]:
                    lifecycle.stop(tamper, cfg)  # a reused PID is stale, never a target
                else:
                    with self.assertRaisesRegex(lifecycle.LifecycleError, "refusing to signal"):
                        lifecycle.stop(tamper, cfg)
                self.assertFalse(any(c.args[1] == signal.SIGTERM for c in kill.call_args_list))
        with mock.patch.object(lifecycle.process, "proc_start_checked", return_value=(None, "denied")), \
                mock.patch.object(lifecycle.os, "kill") as kill:
            with self.assertRaisesRegex(lifecycle.LifecycleError, "refusing to signal"):
                lifecycle.stop(record, cfg)
            # pid_alive uses signal 0, but SIGTERM must not be sent.
            self.assertFalse(any(c.args[1] == signal.SIGTERM for c in kill.call_args_list))

    def test_older_code_is_reported_and_restart_keeps_url_but_replaces_instance(self):
        cfg, record = self.launch()
        # Simulate an installed-code upgrade without changing files or the running server.
        with mock.patch.object(lifecycle, "digest", return_value="new-code"):
            rc, _out, err = self.cli("web", "--ensure")
        self.assertEqual(rc, 1)
        self.assertIn("--ensure --restart", err)
        rc, address, err = self.call("--ensure", "--restart")
        self.assertEqual(rc, 0, err)
        updated = lifecycle.read(lifecycle.root() / "server.json")
        self.owned = (cfg, updated)
        self.assertEqual(address, lifecycle.url(cfg))
        self.assertNotEqual(updated["instance"], record["instance"])
        self.assertEqual(updated["code_digest"], lifecycle.digest())

    def test_chat_configuration_never_uses_the_read_only_token(self):
        cfg = self.configured()
        with lifecycle.locked():
            _path, chat_cfg = lifecycle.config(chat=True)
        self.assertNotEqual(chat_cfg["token"], cfg["token"])
        self.assertNotEqual(chat_cfg["port"], cfg["port"])
        self.assertEqual(stat.S_IMODE((lifecycle.root() / "chat.json").stat().st_mode), 0o600)
        for args in (("--chat", "--ensure"), ("--chat", "--url"), ("--chat", "--stop"),
                     ("--chat", "--rotate-token"), ("--persist-token",), ("--restart",), ("--ensure", "--port", "0")):
            self.assertEqual(self.call(*args)[0], 2, args)

    def test_explicit_ports_cannot_collide_across_persistent_modes(self):
        readonly = self.configured()
        with lifecycle.locked():
            _path, chat = lifecycle.config(chat=True)
        self.assertEqual(self.call("--chat", "--persist-token", "--port", str(readonly["port"]))[0], 1)
        self.assertEqual(self.call("--url", "--port", str(chat["port"]))[0], 1)

    def test_persistent_chat_uses_its_own_url_and_refuses_the_read_only_token(self):
        readonly = self.configured()
        addresses = []
        for _ in range(2):
            child = self.spawn("web", "--chat", "--persist-token", stderr=subprocess.PIPE)
            readable, _w, _x = select.select([child.stdout], [], [], 15)
            self.assertTrue(readable)
            address = child.stdout.readline().strip()
            with lifecycle.locked():
                _path, cfg = lifecycle.config(chat=True)
            self.assertEqual(address, lifecycle.url(cfg))
            self.assertFalse(lifecycle.health(cfg)["read_only"])
            self.assertIsNone(lifecycle.health(dict(cfg, token=readonly["token"])))
            addresses.append(address)
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=15), 0)
        self.assertEqual(addresses[0], addresses[1])

    def test_invalid_config_and_symlink_are_refused_instead_of_replaced(self):
        self.configured()
        path = lifecycle.root() / "config.json"
        path.write_bytes(b" " * 4097)
        self.assertEqual(self.call("--url")[0], 1)
        path.write_text(json.dumps({"version": 1, "port": 80, "token": "bad"}))
        self.assertEqual(self.call("--url")[0], 1)
        path.unlink()
        target = self.root / "elsewhere"
        target.write_text("preserve me")
        path.symlink_to(target)
        self.assertEqual(self.call("--url")[0], 1)
        self.assertEqual(target.read_text(), "preserve me")


class PlainWebLifecycleCompatibilityTests(WebBase):
    def test_plain_server_does_not_compute_a_digest_or_expose_instance_health(self):
        with mock.patch.object(lifecycle, "digest", side_effect=lifecycle.LifecycleError("unreadable code")) as digest:
            plain = web.Server(0)
            plain.server_close()
            digest.assert_not_called()
        self.assertEqual(self.api("/api/instance").status, 404)
