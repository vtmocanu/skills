"""Browser peer addressing and routing: `web:<uuid>` addresses, provenance, registry readers, `send`."""

from __future__ import annotations

import argparse

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_journal_cli import JournalOpenCodeBase
from peer_tests.test_opencode import OC_A, OC_B
from session_peers import channels as sp_channels, cli as sp_cli, identity as sp_identity
from session_peers import inbox as sp_inbox, journal as sp_journal, messaging as sp_messaging
from session_peers import reply_wake as sp_reply_wake, routes as sp_routes, watches as sp_watches

WEB = "5b0d3f1e-1111-4222-8333-444455556666"
WEB_OTHER = "6c1e4a2f-2222-4333-8444-555566667777"
UPPER_WEB = WEB.upper()


class FakeInbox:
    """A stand-in browser-peer inbox speaking the F1(c) acknowledgement protocol.

    It reads until the client half-closes, then answers by ``mode``: ``ack``,
    ``refuse``, ``other`` (an ack for another message, then close), ``eof``
    (close with no answer) or ``silent`` (hold the connection open).
    """

    def __init__(self, path, mode="ack"):
        self.path = path
        self.mode = mode
        self.frames = []
        self.auth = []
        self._conns = []
        self._lock = threading.Lock()
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        os.chmod(path, 0o600)
        self.srv.listen(8)
        self.srv.settimeout(0.2)
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        conn.settimeout(5)
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except (socket.timeout, OSError):
            return
        frame = None
        for raw in buf.splitlines():
            try:
                item = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if item.get("type") == "auth":
                self.auth.append(item)
            else:
                frame = item
        if frame is None:
            return
        with self._lock:
            self.frames.append(frame)
        mid = frame.get("msg_id")
        answer = None
        if self.mode == "ack":
            answer = {"type": "control", "action": "peer_inbox_ack", "orig_msg_id": mid}
        elif self.mode == "refuse":
            answer = {"type": "control", "action": "peer_inbox_refused", "orig_msg_id": mid,
                      "detail": "journal full"}
        elif self.mode == "other":
            answer = {"type": "control", "action": "peer_inbox_ack", "orig_msg_id": new_uuid()}
        if self.mode == "silent":
            return
        with contextlib.suppress(OSError):
            if answer is not None:
                conn.sendall(json.dumps(answer).encode("utf-8") + b"\n")
            conn.close()

    def close(self):
        self._stop.set()
        with contextlib.suppress(OSError):
            self.srv.close()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.close()


class WebRoutingBase(JournalOpenCodeBase):
    def add_web(self, mode="ack", pid=None, name="browser-peer", sid=WEB):
        """A live browser-peer registry record backed by a fake inbox."""
        pid = os.getpid() if pid is None else pid
        path = str(self.socks / ("%d.sock" % pid))
        inbox = FakeInbox(path, mode)
        self._listeners.append(inbox)
        rec = self.write_record(pid, name, sid, path, entrypoint="web")
        return inbox, rec

    def web_cli(self, argv, web=None):
        """Run a CLI command the way the chat server does: ``args.web_sender`` set."""
        args = sp_cli.build_parser().parse_args(argv)
        if web is not None:
            args.web_sender = web
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = args.func(args)
        return rc, out.getvalue(), err.getvalue()


class WebAddressTests(unittest.TestCase):
    def test_parse_address_accepts_each_kind(self):
        cc_id, codex_id = new_uuid(), new_uuid()
        for typed, kind, ident in (
            ("cc:" + cc_id, "cc", cc_id),
            ("codex:" + codex_id, "codex", codex_id),
            ("oc:" + OC_A, "oc", OC_A),
            ("web:" + WEB, "web", WEB),
        ):
            self.assertEqual(sp_identity.parse_address(typed), {"kind": kind, "uuid": ident})
        self.assertEqual(sp_identity.ADDRESS_KINDS, ("cc", "codex", "oc", "web"))

    def test_parse_address_refuses_malformed_addresses(self):
        for value in (
            None, "", "web", "web:", "web:abc", "web:" + UPPER_WEB,
            "web:" + WEB + "\n", "web:" + WEB + " ", " web:" + WEB, "WEB:" + WEB,
            "web:" + WEB[:-1], "web:" + WEB.replace("-", ""), "web:oc_" + WEB,
            "oc:notasession", "oc:ses_short", "cc:abc", "codex:abc", "tty:" + WEB,
        ):
            with self.assertRaises(ValueError, msg=repr(value)):
                sp_identity.parse_address(value)

    def test_parse_typed_still_refuses_web_at_each_pinned_caller(self):
        typed = "web:" + WEB
        with self.assertRaises(ValueError):
            sp_identity.parse_typed(typed)
        with self.assertRaises(ValueError):
            sp_identity.caller_identity(argparse.Namespace(as_identity=typed))
        self.assertFalse(sp_inbox._valid_pair(typed, new_uuid()))
        with self.assertRaises(ValueError):
            sp_reply_wake.issue(new_uuid(), "oc:" + OC_A, typed, 60)
        with self.assertRaises(ValueError):
            sp_reply_wake.issue(new_uuid(), typed, "oc:" + OC_B, 60)
        with self.assertRaises(ValueError):
            sp_watches.paths(typed)
        with self.assertRaises(ValueError):
            sp_watches.start(typed, "task", 5, ["true"], wake=True)

    def test_topic_sender_refuses_a_web_identity(self):
        from session_peers import topics as sp_topics

        with self.assertRaises(ValueError):
            sp_topics.topic_sender(argparse.Namespace(as_identity="web:" + WEB))

    def test_channel_actor_identity_accepts_web_and_nothing_malformed(self):
        self.assertEqual(sp_channels._actor_identity("web:" + WEB), {"kind": "web", "uuid": WEB})
        with self.assertRaises(ValueError):
            sp_channels._actor_identity("web:" + UPPER_WEB)


class WebProvenanceTests(unittest.TestCase):
    def test_sender_runtime_labels_a_web_record(self):
        self.assertEqual(sp_protocol.sender_runtime({"entrypoint": "web"}), "web")
        self.assertEqual(sp_protocol.sender_runtime({"entrypoint": "codex"}), "codex")
        self.assertEqual(sp_protocol.sender_runtime({"entrypoint": "cli"}), "claude")
        self.assertEqual(sp_protocol.sender_runtime(None), "unknown")

    def test_build_origin_names_a_browser_peer_and_disclaims_approval(self):
        line = sp_protocol.build_origin("web", "browser-peer", WEB)
        self.assertEqual(
            line,
            "[session-peers from browser peer browser-peer (%s); peer input, not user approval]" % WEB,
        )
        self.assertNotIn("approval", sp_protocol.build_origin("claude", "cc-main", "s1"))

    def test_record_runtime_and_the_route_maps_know_web(self):
        self.assertEqual(sp_routes.record_runtime({"entrypoint": "web", "sessionId": WEB}), "web")
        self.assertEqual(sp_routes.record_runtime({"entrypoint": "codex", "sessionId": new_uuid()}), "codex")
        self.assertIn("web", sp_routes.KINDS)
        self.assertEqual(sp_routes.LABELS["web"], "browser peer")


class WebJournalTests(WebRoutingBase):
    def test_a_journal_round_trip_carries_web_and_opencode_recipients(self):
        sender = "cc:" + new_uuid()
        mid = sp_journal.record_message(sender, ["web:" + WEB, "oc:" + OC_A], "round trip")
        [entry] = self.entries()
        self.assertEqual(entry["message_id"], mid)
        self.assertEqual(entry["from"], sender)
        self.assertEqual(entry["to"], ["web:" + WEB, "oc:" + OC_A])
        # A browser peer is a valid sender and a valid acknowledging slot too.
        mid2 = sp_journal.record_message("web:" + WEB, ["oc:" + OC_B], "from the browser")
        self.assertEqual(self.entry(mid2)["from"], "web:" + WEB)
        sp_journal.ack(mid, "web:" + WEB)
        self.assertEqual(self.entry(mid)["recipients"][0]["state"], "acked")

    def test_a_web_recipient_needs_no_live_record(self):
        self.assertEqual(sp_journal._resolve_recipients(["web:" + WEB]), ["web:" + WEB])

    def test_a_malformed_web_recipient_is_refused(self):
        for bad in ("web:" + UPPER_WEB, "web:nope", "web:"):
            with self.assertRaises(sp_journal.JournalRefused, msg=bad):
                sp_journal.record_message("cc:" + new_uuid(), [bad], "x")
        with self.assertRaises(sp_journal.JournalRefused):
            sp_journal._norm_caller("web:nope")
        self.assertEqual(sp_journal._norm_caller("web:" + WEB), "web:" + WEB)

    def test_an_unknown_cc_recipient_is_still_refused(self):
        with self.assertRaises(sp_journal.JournalRefused):
            sp_journal.record_message("cc:" + new_uuid(), ["cc:" + new_uuid()], "x")


class WebRegistryReaderTests(WebRoutingBase):
    """One test per registry reader: include or exclude the browser peer's record on purpose."""

    def test_by_target_and_by_name_exclude_web_unless_asked(self):  # claude.py by_name/by_target
        _inbox, rec = self.add_web()
        self.assertEqual(sp_claude.claude_record_by_target(WEB), [])
        self.assertEqual(sp_claude.claude_record_by_target("browser-peer"), [])
        self.assertEqual(sp_claude.claude_record_by_name("browser-peer"), [])
        self.assertEqual(
            [r["sessionId"] for r in sp_claude.claude_record_by_target(WEB, include_web=True)], [WEB]
        )
        self.assertEqual(
            [r["sessionId"] for r in sp_claude.claude_record_by_name("browser-peer", include_web=True)], [WEB]
        )

    def test_a_cc_target_never_resolves_to_the_browser_peer(self):  # claude._resolve_claude_record
        self.add_web()
        for target in (WEB, "browser-peer"):
            with self.assertRaises(sp_codex.ResolveNotFound):
                sp_claude._resolve_claude_record(target)
        rc, _out, err = self.cli("send", "--to", "cc:" + WEB, "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no live Claude session", err)

    def test_by_socket_finds_the_web_record(self):  # claude_record_by_socket
        inbox, _rec = self.add_web()
        self.assertEqual(sp_claude.claude_record_by_socket(inbox.path)["sessionId"], WEB)
        self.assertEqual([r["sessionId"] for r in sp_claude.live_claude_records()], [WEB])

    def test_current_claude_session_id_ignores_a_web_socket(self):  # claude._current_claude_session_id
        inbox, _rec = self.add_web()
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = inbox.path
        self.assertIsNone(sp_claude._current_claude_session_id())

    def test_default_socket_dir_follows_a_live_web_record(self):  # claude.default_socket_dir
        other = self.root / "elsewhere"
        other.mkdir()
        os.environ.pop("SESSION_PEERS_SOCKET_DIR", None)
        path = str(other / "web.sock")
        self.write_record(os.getpid(), "browser-peer", WEB, path, entrypoint="web")
        self.assertEqual(sp_claude.default_socket_dir(), str(other))

    def test_general_discovery_skips_the_web_record(self):  # channels._discover_general
        sid = new_uuid()
        self.add_listener(name="cc-main", session_id=sid, pid=os.getppid())
        self.add_web()
        members, _coverage, _gaps = sp_channels._discover_general()
        self.assertIn("cc:" + sid, members)
        self.assertNotIn("cc:" + WEB, members)
        self.assertNotIn("web:" + WEB, members)

    def test_cc_liveness_ignores_the_web_record(self):  # channels._claude_liveness
        self.add_web()
        self.assertEqual(sp_channels.member_liveness("cc:" + WEB), "gone")

    def test_identity_candidates_and_resolution_skip_the_web_record(self):  # identity.py:95,225,229
        self.add_web()
        self.assertEqual(sp_identity.describe_candidates("browser-peer"), [])
        with self.assertRaises(sp_codex.ResolveNotFound):
            sp_identity.resolve_typed("cc:" + WEB)

    def test_buddy_status_does_not_count_the_web_record_as_a_claude_session(self):  # buddy._claude_status
        self.add_web()
        status = sp_buddy._claude_status(WEB)
        self.assertIs(status["live"], False)
        self.assertIn("no live Claude session", status["route"])

    def test_buddy_binding_conflict_ignores_a_web_record_holding_the_sid(self):  # buddy._binding_conflict
        self.add_web()
        tid = new_uuid()
        for path in (sp_storage.budget_binding_path(tid), sp_storage.thread_state_path(tid)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        pathlib.Path(sp_storage.budget_binding_path(tid)).write_text(json.dumps({"sid": WEB}))
        os.makedirs(sp_storage.buddies_dir(), exist_ok=True)
        refusal, reclaimable = sp_buddy._binding_conflict(
            {"kind": "cc", "uuid": new_uuid()}, {"kind": "codex", "uuid": tid}
        )
        self.assertIsNone(refusal)
        self.assertEqual(list(reclaimable), [WEB])

    def test_owner_gone_check_ignores_the_web_record(self):  # maintenance._owner_verified_gone
        self.add_web()
        self.assertTrue(sp_maintenance._owner_verified_gone({"kind": "cc", "uuid": WEB}))

    def test_topic_sender_does_not_adopt_a_web_socket(self):  # topics.topic_sender
        inbox, _rec = self.add_web()
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = inbox.path
        self.assertIsNone(sp_topics.topic_sender(argparse.Namespace()))

    def test_the_web_record_occupies_its_name(self):  # codex.peer_name_for_thread, routes._verified_name
        self.add_web(name="shared-name", pid=os.getppid())
        tid = new_uuid()
        self.assertEqual(sp_codex.peer_name_for_thread("shared-name", tid), "codex-%s" % tid[:8])
        _listener, rec = self.add_listener(name="shared-name", session_id=new_uuid(), pid=os.getpid())
        self.assertEqual(sp_routes._verified_name(rec), (None, "duplicate"))

    def test_list_labels_the_record_web(self):  # diagnostics.cmd_list
        self.add_web()
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        [view] = json.loads(out)["claude"]
        self.assertEqual(view["runtime"], "web")
        self.assertEqual(view["sessionId"], WEB)
        rc, out, err = self.cli("list")
        self.assertIn("browser-peer [browser peer]", out)

    def test_the_dashboard_registry_view_does_not_match_web_as_cc(self):  # web._registry_view
        from session_peers import web as sp_web

        by_sid = {WEB: [{"sessionId": WEB, "entrypoint": "web", "name": "browser-peer", "status": "idle"}]}
        activity, name = sp_web._registry_view("cc:" + WEB, "cc", WEB, True, by_sid)
        self.assertEqual((activity, name), ("unknown", None))


class WebShimReaderTests(ShimBase):
    """shim.py includes the browser peer's record: it is a reply address like any peer's."""

    def test_shim_reader_decisions(self):  # shim._inbound_sender, _verified_requester, _reply_targets
        pid = os.getppid()
        path = str(self.socks / ("%d.sock" % pid))
        inbox = FakeInbox(path)
        self._listeners.append(inbox)
        self.write_record(pid, "browser-peer", WEB, path, entrypoint="web")
        shim, _tid, _rollout = self.make_shim()
        sender, name, sid, sock = shim._inbound_sender({"from": "uds:" + path, "msg_id": "m"}, {})
        self.assertEqual((sender["sessionId"], name, sid, sock), (WEB, "browser-peer", WEB, path))
        records = sp_claude.live_claude_records()
        turn = argparse.Namespace(turn_id="t")
        requester, _sock = shim._verified_requester(turn, {"reply": path, "sid": WEB}, records)
        self.assertEqual(requester["sessionId"], WEB)
        requester, _sock = shim._verified_requester(turn, {"reply": path, "sid": new_uuid()}, records)
        self.assertIsNone(requester)
        # An @-addressed reply reaches the browser peer by name, once it has been in contact.
        shim.contacts.pop(WEB, None)
        self.assertEqual(shim._reply_targets("@browser-peer hello", None, records), [])
        shim.contacts[WEB] = {"name": "browser-peer", "socket": path}
        targets = shim._reply_targets("@browser-peer hello", None, records)
        self.assertEqual([t["sessionId"] for t in targets], [WEB])


class WebRouteCheckTests(WebRoutingBase):
    def test_a_live_web_target_passes_the_route_check(self):
        self.add_web()
        args = argparse.Namespace(to="web:" + WEB, record=False)
        self.assertIsNone(sp_routes.check(args, "send", "to"))
        self.assertEqual(args.route_checked["runtime"], "web")
        self.assertEqual(args.route_checked["id"], WEB)

    def test_an_offline_web_target_is_refused_without_record(self):
        args = argparse.Namespace(to="web:" + WEB, record=False)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = sp_routes.check(args, "send", "to")
        self.assertEqual(rc, 1)
        self.assertIn("browser peer web:%s is not running" % WEB, err.getvalue())

    def test_an_offline_web_target_passes_with_record(self):
        args = argparse.Namespace(to="web:" + WEB, record=True)
        self.assertIsNone(sp_routes.check(args, "send", "to"))
        self.assertEqual(args.route_checked["runtime"], "web")

    def test_a_malformed_web_target_is_refused_even_with_record(self):
        args = argparse.Namespace(to="web:" + UPPER_WEB, record=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = sp_routes.check(args, "send", "to")
        self.assertEqual(rc, 2)
        self.assertIn("web:<uuid>", err.getvalue())

    def test_a_correlated_verb_cannot_reach_a_browser_peer(self):
        self.add_web()
        for verb in ("ask", "dispatch"):
            rc, _out, err = self.cli(verb, "--to", "web:" + WEB, "--message", "x", "--timeout", "1")
            self.assertEqual(rc, 2, verb)
            self.assertIn("cannot reach the browser peer", err)

    def test_sender_kind_prefers_web_sender_over_the_environment(self):  # D3
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = str(self.socks / "x.sock")
        self.assertEqual(sp_routes.sender_kind(argparse.Namespace()).kind, "cc")
        args = argparse.Namespace(web_sender={"sid": WEB, "name": "browser-peer", "socket": None})
        self.assertEqual(sp_routes.sender_kind(args), sp_routes.Sender("web", WEB))
        self.assertFalse(sp_routes._claudeish(sp_routes.sender_kind(args)))

    def test_a_malformed_web_sender_is_refused_not_attributed_to_the_environment(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        args = argparse.Namespace(
            web_sender={"sid": "not-a-uuid", "name": "x", "socket": None}, to="oc:" + OC_B, message="hi",
            record=False, from_thread=None, from_name=None, from_sid=None, from_socket=None,
            allow_reply_wake=None, in_reply_to=None, json=False, wake=False, never_wake=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = sp_routes.check(args, "send", "to")
        self.assertEqual(rc, 2)
        self.assertIn("malformed browser sender", err.getvalue())


class WebSendTests(WebRoutingBase):
    def sender(self):
        _listener, rec = self.peer("cc-sender", pid=os.getppid())
        self.as_claude(rec)
        return rec

    def test_a_live_web_peer_acknowledges_and_the_send_succeeds(self):
        inbox, _rec = self.add_web()
        rec = self.sender()
        rc, out, err = self.cli("send", "--to", "web:" + WEB, "--message", "hello", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "sent")
        self.assertEqual(payload["target"], "web")
        self.assertTrue(payload["acknowledged"])
        [frame] = inbox.frames
        self.assertEqual(frame["msg_id"], payload["message_id"])
        self.assertEqual(frame["from"], "uds:" + rec["messagingSocketPath"])
        lines = frame["message"]["content"].splitlines()
        self.assertEqual(
            lines[0], "[session-peers from Claude Code session cc-sender (%s)]" % rec["sessionId"]
        )
        self.assertEqual(lines[-1], "hello")

    def test_a_refusal_is_not_delivered_and_exits_1(self):
        inbox, _rec = self.add_web(mode="refuse")
        self.sender()
        rc, out, err = self.cli("send", "--to", "web:" + WEB, "--message", "hello", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("refused the message: journal full", err)
        self.assertIn("not delivered", err)
        self.assertEqual(len(inbox.frames), 1)
        [entry] = self.entries()
        self.assertIn("refused: journal full", entry["recipients"][0]["transport_error"]["detail"])

    def test_no_answer_within_the_timeout_is_an_unknown_outcome(self):
        inbox, _rec = self.add_web(mode="silent")
        self.sender()
        with mock.patch.object(sp_messaging, "WEB_ACK_TIMEOUT", 0.3):
            started = time.monotonic()
            rc, _out, err = self.cli("send", "--to", "web:" + WEB, "--message", "hello")
        self.assertEqual(rc, 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("outcome unknown", err)
        self.assertEqual(len(inbox.frames), 1)

    def test_eof_without_an_answer_is_an_unknown_outcome(self):
        self.add_web(mode="eof")
        self.sender()
        rc, _out, err = self.cli("send", "--to", "web:" + WEB, "--message", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("outcome unknown", err)
        self.assertIn("closed without an answer", err)

    def test_an_ack_for_another_message_does_not_count(self):
        self.add_web(mode="other")
        self.sender()
        rc, _out, err = self.cli("send", "--to", "web:" + WEB, "--message", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("outcome unknown", err)

    def test_the_full_cli_path_with_no_live_web_record_and_record(self):
        self.sender()
        rc, out, err = self.cli("send", "--to", "web:" + WEB, "--message", "later", "--record", "--json")
        self.assertEqual(rc, 1)
        self.assertIn("is not running", err)
        [entry] = self.entries()
        self.assertEqual(entry["to"], ["web:" + WEB])
        self.assertEqual(entry["body"], "later")
        recipient = entry["recipients"][0]
        self.assertEqual(recipient["slot"], "web:" + WEB)
        self.assertIn("transport_error", recipient)
        self.assertIn("not running", recipient["transport_error"]["detail"])

    def test_without_record_an_offline_send_writes_nothing(self):
        self.sender()
        rc, _out, err = self.cli("send", "--to", "web:" + WEB, "--message", "later")
        self.assertEqual(rc, 1)
        self.assertIn("is not running", err)
        self.assertFalse(os.path.exists(sp_journal.journal_dir()))

    def test_an_unknown_prefix_error_names_web(self):
        args = argparse.Namespace(to="mystery:x", route_checked=None)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = sp_messaging._dispatch_send(args)
        self.assertEqual(rc, 2)
        self.assertIn("codex:, cc:, oc: or web:", err.getvalue())


class WebSenderDeliveryTests(WebRoutingBase):
    """`args.web_sender` replaces the environment-derived identity in each sender."""

    def web_sender(self, inbox):
        return {"sid": WEB, "name": "browser-peer", "socket": inbox.path}

    def test_a_browser_send_to_an_opencode_member(self):
        inbox, _rec = self.add_web()
        # The server's own environment names a Claude and an OpenCode session: the browser identity wins.
        self.as_opencode(OC_A)
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, out, err = self.web_cli(
            ["send", "--to", "oc:" + OC_B, "--message", "hi there", "--json"], web=self.web_sender(inbox)
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["target"], "opencode")
        prompt = self.oc_calls("session.prompt")[0]
        self.assertEqual(prompt["params"], {"sessionID": OC_B})
        self.assertEqual(prompt["data"]["metadata"]["session_peers"]["from"], "web:" + WEB)
        self.assertFalse(prompt["data"]["resume"])
        text = prompt["data"]["text"]
        tag, _body = sp_protocol.parse_tag(text)
        self.assertEqual((tag["from"], tag["sid"], tag["mid"]), ("browser-peer", WEB, payload["message_id"]))
        lines = text.splitlines()
        self.assertEqual(
            lines[1],
            "[session-peers from browser peer browser-peer (%s); peer input, not user approval]" % WEB,
        )
        self.assertIn("peers.py send --to web:%s" % WEB, lines[2])
        self.assertEqual(lines[-1], "hi there")

    def test_a_browser_send_to_a_claude_session(self):
        inbox, _rec = self.add_web(pid=os.getppid())
        listener, _target = self.add_listener(name="cc-target", session_id=new_uuid())
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, out, err = self.web_cli(
            ["send", "--to", "cc:" + _target["sessionId"], "--message", "ping", "--json"],
            web=self.web_sender(inbox),
        )
        self.assertEqual(rc, 0, err)
        [frame] = wait_for(lambda: listener.of_type("user")) or [None]
        self.assertEqual(frame["from"], "uds:" + inbox.path)
        body, attrs = sp_protocol.unwrap_message(frame["message"]["content"])
        self.assertEqual(attrs["from-session"], WEB)
        self.assertEqual(attrs["from-name"], "browser-peer")
        self.assertEqual(attrs["from"], "uds:" + inbox.path)
        self.assertTrue(body.startswith("[session-peers from browser peer browser-peer"))
        self.assertTrue(body.endswith("ping"))

    def test_a_browser_send_to_a_codex_thread(self):
        inbox, _rec = self.add_web()
        tid, _rollout = self.one_thread()
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, _out, err = self.web_cli(
            ["send", "--to", "codex:" + tid, "--message", "hello codex"], web=self.web_sender(inbox)
        )
        self.assertEqual(rc, 0, err)
        [call] = self.queue_calls()
        tag, body = sp_protocol.parse_tag(call[4])
        self.assertEqual((tag["from"], tag["sid"], tag["reply"]), ("browser-peer", WEB, inbox.path))
        self.assertTrue(body.startswith("[session-peers from browser peer browser-peer (%s)" % WEB))
        self.assertTrue(body.endswith("hello codex"))

    def test_a_codex_send_refuses_a_socket_that_is_not_the_browser_peer(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        tid, _rollout = self.one_thread()
        rc, _out, err = self.web_cli(
            ["send", "--to", "codex:" + tid, "--message", "x"],
            web={"sid": WEB, "name": "browser-peer", "socket": rec["messagingSocketPath"]},
        )
        self.assertEqual(rc, 1)
        self.assertIn("not the browser peer", err)
        self.assertEqual(self.queue_calls(), [])

    def test_a_malformed_browser_sender_is_an_error_not_an_env_fallback(self):
        with self.assertRaises(ValueError):
            sp_messaging._web_sender(argparse.Namespace(web_sender={"sid": "nope"}))
        self.assertIsNone(sp_messaging._web_sender(argparse.Namespace()))

    def test_sender_kind_refuses_a_malformed_web_sender(self):  # routes.sender_kind
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()  # never the fallback for a browser send
        for bad in ({"sid": "nope"}, {"sid": UPPER_WEB}, {"sid": WEB + "\n"}, {"sid": 7}, {"sid": ""}, {}, "x"):
            args = argparse.Namespace(web_sender=bad)
            with self.assertRaises(sp_routes._Refusal, msg=repr(bad)) as caught:
                sp_routes.sender_kind(args)
            self.assertEqual(caught.exception.code, 2)

    def test_the_environment_thread_id_is_ignored_for_a_browser_sender(self):  # identity._thread_from_args
        os.environ["CODEX_THREAD_ID"] = new_uuid()
        self.assertTrue(sp_identity._thread_from_args(argparse.Namespace()))
        web = {"sid": WEB, "name": "browser-peer", "socket": None}
        self.assertIsNone(sp_identity._thread_from_args(argparse.Namespace(web_sender=web)))

    def test_a_codex_shim_target_is_routed_by_the_browser_sender_not_the_server_env(self):  # messaging._codex_shim_thread
        shim_sid = new_uuid()
        _listener, rec = self.add_listener(name="shim-alias", session_id=shim_sid, pid=os.getpid())
        self.write_record(os.getpid(), "shim-alias", shim_sid, rec["messagingSocketPath"], entrypoint="codex")
        os.environ["CODEX_THREAD_ID"] = new_uuid()
        # A Codex sender in the environment keeps the Claude-facing path.
        self.assertIsNone(sp_messaging._codex_shim_thread(shim_sid, argparse.Namespace()))
        # The server's environment is not the browser peer: the shim's thread is the route.
        web = {"sid": WEB, "name": "browser-peer", "socket": None}
        self.assertEqual(sp_messaging._codex_shim_thread(shim_sid, argparse.Namespace(web_sender=web)), shim_sid)


class WebVerbOrderTests(WebRoutingBase):
    """A verb a browser peer cannot take is refused before any offline advice."""

    def test_ask_to_an_offline_web_target_names_send_not_record(self):
        rc, _out, err = self.cli("ask", "--to", "web:" + WEB, "--message", "x", "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertIn("cannot reach the browser peer", err)
        self.assertIn("Use send.", err)
        self.assertNotIn("--record", err)

    def test_dispatch_to_an_offline_web_target_names_send_not_record(self):
        rc, _out, err = self.cli("dispatch", "--to", "web:" + WEB, "--message", "x", "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertIn("cannot reach the browser peer", err)
        self.assertNotIn("--record", err)

    def test_wait_for_a_web_target_is_refused_live_or_offline(self):
        for live in (False, True):
            if live:
                self.add_web()
            rc, _out, err = self.cli("wait", "--for", "web:" + WEB, "--state", "idle", "--timeout", "1")
            self.assertEqual(rc, 2, live)
            self.assertIn("cannot reach the browser peer", err)
            self.assertNotIn("--record", err)

    def test_send_to_an_offline_web_target_still_advises_record(self):
        rc, _out, err = self.cli("send", "--to", "web:" + WEB, "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("Use --record", err)


class WebSendProvenanceTests(WebRoutingBase):
    """`_send_web`: the origin line and the frame's `from` name one sender."""

    def web_on_a_child_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self._children.append(child)
        return self.add_web(pid=child.pid)

    def test_from_socket_names_both_the_origin_and_the_frame_from(self):
        inbox, _rec = self.web_on_a_child_pid()
        _la, rec_a = self.peer("cc-a", pid=os.getpid())
        _lb, rec_b = self.peer("cc-b", pid=os.getppid())
        self.as_cc(rec_a)
        rc, _out, err = self.cli(
            "send", "--to", "web:" + WEB, "--message", "hello",
            "--from-socket", rec_b["messagingSocketPath"],
        )
        self.assertEqual(rc, 0, err)
        [frame] = inbox.frames
        self.assertEqual(frame["from"], "uds:" + rec_b["messagingSocketPath"])
        content = frame["message"]["content"]
        self.assertEqual(
            content.splitlines()[0],
            "[session-peers from Claude Code session cc-b (%s)]" % rec_b["sessionId"],
        )
        self.assertNotIn("cc-a", content)
        self.assertNotIn(rec_a["sessionId"], content)

    def test_a_from_socket_nobody_listens_on_is_refused_before_anything_is_sent(self):
        inbox, _rec = self.web_on_a_child_pid()
        _la, rec_a = self.peer("cc-a", pid=os.getpid())
        self.as_cc(rec_a)
        rc, _out, err = self.cli(
            "send", "--to", "web:" + WEB, "--message", "hello",
            "--from-socket", str(self.socks / "nobody.sock"),
        )
        self.assertEqual(rc, 1)
        self.assertIn("no live Claude session listens", err)
        self.assertEqual(inbox.frames, [])


class WebCallerViewTests(WebRoutingBase):
    """`messaging._caller_view` reads the browser peer's record as a browser peer."""

    def test_a_session_id_naming_the_web_record_is_a_browser_peer(self):  # include_web=True
        self.add_web()
        os.environ["CLAUDE_CODE_SESSION_ID"] = WEB
        runtime, name, address = sp_messaging._caller_view(argparse.Namespace())
        self.assertEqual((runtime, name, address), ("web", "browser-peer", "web:" + WEB))
        self.assertIn(
            "browser peer browser-peer", sp_protocol.build_origin(runtime, name, address.partition(":")[2])
        )

    def test_a_messaging_socket_naming_the_web_record_is_a_browser_peer(self):  # by_socket path
        inbox, _rec = self.add_web()
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = inbox.path
        runtime, name, address = sp_messaging._caller_view(argparse.Namespace())
        self.assertEqual((runtime, name, address), ("web", "browser-peer", "web:" + WEB))


class WebExcludedReaderTests(WebRoutingBase):
    """The readers that must never take the browser peer's record for a cc session or a Codex shim."""

    def test_wait_for_cc_ignores_a_web_record_sharing_the_session_id(self):  # messaging.cmd_wait_peer
        sid = new_uuid()
        _listener, rec = self.add_listener(name="cc-busy", session_id=sid, pid=os.getpid())
        self.write_record(os.getpid(), "cc-busy", sid, rec["messagingSocketPath"], status="busy")
        self.add_web(pid=os.getppid(), sid=sid)  # idle, same sessionId
        rc, _out, err = self.cli("wait", "--for", "cc:" + sid, "--state", "idle", "--timeout", "1")
        self.assertEqual(rc, 124, err)

    def test_the_opencode_request_text_does_not_take_a_web_record_as_the_cc_requester(self):
        self.add_web()
        text = sp_messaging._opencode_request_text(
            {"_runtime": "opencode"}, {"kind": "cc", "uuid": WEB}, None, new_uuid(), "question", 30
        )
        self.assertNotIn("browser", text)
        self.assertIn("Claude Code session", text.splitlines()[0])

    def test_a_web_record_is_not_a_running_codex_shim(self):  # routes._shim_running
        self.add_web()
        self.assertFalse(sp_routes._shim_running(WEB))

    def test_a_shim_target_is_never_a_web_record(self):  # messaging._codex_shim_thread
        self.add_web()
        self.assertIsNone(sp_messaging._codex_shim_thread(WEB, argparse.Namespace()))

    def test_a_web_record_at_a_shim_pid_is_not_a_shim_record(self):  # lifecycle.shim_pid
        tid = new_uuid()
        pid = self.hold_pidfile(tid, pid=os.getppid())
        self.add_web(pid=pid)  # a web record, sessionId differing from the thread
        self.assertEqual(sp_lifecycle.shim_pid(tid), pid)
        # The same record as a shim of another thread is the mismatch shim_pid refuses.
        self.write_record(pid, "shim", WEB, str(self.socks / "x.sock"), entrypoint="codex")
        self.assertIsNone(sp_lifecycle.shim_pid(tid))

    def test_a_web_record_contributes_no_codex_home(self):  # storage._recorded_shim_homes
        self.write_record(os.getpid(), "browser-peer", WEB, str(self.socks / "w.sock"),
                          entrypoint="web", codexHome="/somewhere/else")
        self.assertEqual(sp_storage._recorded_shim_homes(), [])
        self.write_record(os.getpid(), "shim", WEB, str(self.socks / "w.sock"),
                          entrypoint="codex", codexHome="/somewhere/else")
        self.assertEqual(sp_storage._recorded_shim_homes(), ["/somewhere/else"])


class WebShimInclusionTests(ShimBase):
    """shim.py addresses the browser peer's record on purpose; the U2 inbox ignores such notices."""

    def web_inbox(self):
        pid = os.getppid()
        path = str(self.socks / ("%d.sock" % pid))
        inbox = FakeInbox(path)
        self._listeners.append(inbox)
        self.write_record(pid, "browser-peer", WEB, path, entrypoint="web")
        return inbox

    def test_the_reply_budget_reads_the_web_record(self):  # shim.py ReplyBudget records
        self.web_inbox()
        shim, _tid, _rollout = self.make_shim()
        self.assertIn(WEB, [r.get("sessionId") for r in shim.reply_budget._records()])

    def test_an_idle_notice_can_target_a_web_socket(self):  # shim._fire_idle
        inbox = self.web_inbox()
        shim, _tid, _rollout = self.make_shim()
        mid = new_uuid()
        shim._fire_idle((mid, inbox.path), "the Codex thread is idle")
        frames = wait_for(lambda: inbox.frames)
        self.assertIsNotNone(frames, "no frame reached the browser peer")
        self.assertEqual(
            (frames[0]["type"], frames[0]["action"], frames[0]["orig_msg_id"]),
            ("control", "peer_idle_notice", mid),
        )
