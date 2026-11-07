"""Channel fan-out: `send --to '#NAME'`, its journal, lock, history and delivery order, and reader states."""

import argparse
import types

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channels import ChannelBase
from peer_tests.test_opencode import OC_A, OC_B, OC_CHILD, oc_session
from session_peers import channels as sp_channels, journal as sp_journal, routes as sp_routes

MISSING_OC = "ses_zzzzzzzzzzzzZZZZZZZZZZZZZZ"
HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


class ChannelSendBase(ChannelBase):
    def setUp(self):
        super().setUp()
        self.now = [100.0]
        self.sleeps = []

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds

        for patch in (
            mock.patch.object(sp_channels, "_clock", lambda: self.now[0]),
            mock.patch.object(sp_channels, "_sleep", fake_sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def sender_cc(self, name="alpha"):
        """This process becomes Claude session ``name`` with a live registry record."""
        sid = new_uuid()
        self.me = self.as_cc(sid)
        self.sender_listener, self.sender_rec = self.add_listener(name=name, session_id=sid)
        return self.me

    def peer_cc(self, name):
        """A live Claude session in its own process; (listener, typed id)."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self._children.append(proc)
        sid = new_uuid()
        sock = str(self.socks / ("%d.sock" % proc.pid))
        listener = Listener(sock)
        self._listeners.append(listener)
        self.write_record(proc.pid, name, sid, sock)
        return listener, "cc:" + sid

    def channel(self, name, *members, owner=None):
        return sp_channels.create_channel(name, owner or self.me, list(members))

    def send(self, *extra, name="ops", message="freeze main"):
        rc, out, err = self.cli("send", "--to", "#" + name, "--message", message, "--json", *extra)
        return rc, (json.loads(out) if out.strip() else None), err

    def history(self, name="ops"):
        return sp_channels.read_history(name, limit=sp_constants.TOPIC_TAIL_MAX)["entries"]

    def send_entries(self, name="ops"):
        return [e for e in self.history(name) if e["kind"] == "channel.send"]

    def view_of(self, name="ops", index=-1):
        sends = [e for e in self.tail_json(name, "--limit", "1000")["entries"] if e["kind"] == "channel.send"]
        return sends[index]["view"]

    def journal_entries(self):
        return sp_journal.read_only_view()["entries"]

    def nothing_delivered(self):
        self.assertEqual(self.queue_calls(), [])
        self.assertEqual(self.oc_calls("session.prompt"), [])
        for listener in self._listeners:
            self.assertEqual(listener.of_type("user"), [])


class ChannelSendTests(ChannelSendBase):
    def test_example_1_codex_accepted_and_idle_opencode_not_woken(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.ok("create", "ops", "--member", "codex:" + tid, "--member", "oc:" + OC_B)
        rc, out, err = self.cli("send", "--to", "#ops", "--message", "freeze main")
        self.assertEqual(rc, 0, err)
        self.assertIn("codex:%s: transport accepted" % tid, out)
        self.assertIn("oc:%s: not woken (OpenCode idle)" % OC_B, out)
        queued = self.queue_calls()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2], tid)
        prompts = self.oc_calls("session.prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["data"]["resume"], False)
        self.assertIn("freeze main", prompts[0]["data"]["text"])

        tail = self.tail_json("ops")
        sends = [e for e in tail["entries"] if e["kind"] == "channel.send"]
        self.assertEqual(len(sends), 1)
        data = sends[0]["data"]
        mid = data["message_id"]
        self.assertEqual(data["body"], "freeze main")
        self.assertEqual((data["journaled"], data["delivery"]), (False, "fanout"))
        self.assertEqual(data["recipients"], ["codex:" + tid, "oc:" + OC_B])
        self.assertEqual(data["sender"], self.me)
        self.assertIsNone(data["opencode_coverage"])
        self.assertEqual(
            [e["kind"] for e in tail["entries"]], ["channel.created", "channel.send", "channel.outcome"]
        )
        self.assertEqual(sends[0]["view"]["state"], "complete")
        self.assertEqual(
            [(r["recipient"], r["state"]) for r in sends[0]["view"]["recipients"]],
            [("codex:" + tid, "transport accepted"), ("oc:" + OC_B, "not woken (OpenCode idle)")],
        )
        human = self.ok("tail", "ops")
        self.assertIn("freeze main", human)
        self.assertIn("codex:%s: transport accepted" % tid, human)
        self.assertIn("delivery: complete", human)
        # Plain send keeps its own output and JSON shape.
        rc, out, err = self.cli("send", "--to", "codex:" + tid, "--message", "x", "--json")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("paused", json.loads(out))
        self.assertTrue(mid)

    def test_json_result_and_header_per_recipient(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + OC_B)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["channel"], "ops")
        self.assertNotIn("journal", result)
        self.assertNotIn("opencode_coverage", result)
        mid = result["message_id"]
        codex_text = self.queue_calls()[0][4]
        oc_text = self.oc_calls("session.prompt")[0]["data"]["text"]
        self.assertIn("[channel #ops message %s from %s to codex:%s]" % (mid, self.me, tid), codex_text)
        self.assertIn("[channel #ops message %s from %s to oc:%s]" % (mid, self.me, OC_B), oc_text)
        self.assertNotEqual(codex_text, oc_text)
        self.assertEqual(
            self.oc_calls("session.prompt")[0]["data"]["metadata"]["session_peers"]["message_id"], mid
        )

    def test_unknown_channel_and_non_member_sender_are_refused_before_any_delivery(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        rc, _out, err = self.cli("send", "--to", "#nope", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("no channel named #nope", err)
        self.channel("ops", "codex:" + tid)
        self.as_cc()
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("is not a member of #ops", err)
        rc, _out, err = self.cli("send", "--to", "#Bad Name", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("1-64 characters", err)
        self.nothing_delivered()
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("cannot tell which session is asking", err)

    def test_general_never_gets_a_stored_record(self):
        self.sender_cc()
        self.cli("send", "--to", "#general", "--message", "hello")
        self.assertIsNone(sp_channels.read_channel("general"))
        self.assertFalse(os.path.exists(sp_channels._record_path("general")))

    def test_channel_targets_refuse_wake_reply_wake_and_sender_overrides(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        for extra in (
            ["--wake"], ["--allow-reply-wake"], ["--allow-reply-wake", "30"], ["--in-reply-to", new_uuid()],
            ["--from-thread", new_uuid()], ["--from-name", "spoof"], ["--from-sid", new_uuid()],
            ["--from-socket", "/fake"],
        ):
            with self.subTest(extra=extra):
                rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x", *extra)
                self.assertEqual(rc, 2, err)
                self.assertIn("cannot be used when sending to a channel", err)
        self.nothing_delivered()
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])

    def test_claude_sender_reaches_claude_member_and_a_direct_send_is_still_refused(self):
        self.sender_cc("alpha")
        peer, peer_typed = self.peer_cc("bravo")
        self.channel("ops", peer_typed)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {peer_typed: "transport accepted"})
        frames = wait_for(lambda: peer.of_type("user"))
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual(frame["msg_id"], result["message_id"])
        self.assertEqual(frame["from"], "uds:" + self.sender_rec["messagingSocketPath"])
        content = frame["message"]["content"]
        self.assertIn('<cross-session-message from="uds:%s"' % self.sender_rec["messagingSocketPath"], content)
        self.assertIn('from-name="alpha"', content)
        self.assertIn("[session-peers from Claude Code session alpha (%s)]" % self.me.partition(":")[2], content)
        self.assertIn("[channel #ops message %s from %s to %s]" % (result["message_id"], self.me, peer_typed), content)
        self.assertIn("freeze main", content)
        # The same sender's direct send to that session stays refused.
        rc, _out, err = self.cli("send", "--to", peer_typed, "--message", "direct")
        self.assertEqual(rc, 2)
        self.assertIn("native SendMessage", err)
        time.sleep(0.2)
        self.assertEqual(len(peer.of_type("user")), 1)

    def test_channel_route_exemption_needs_a_validated_snapshot(self):
        self.sender_cc("alpha")
        peer, peer_typed = self.peer_cc("bravo")
        other, other_typed = self.peer_cc("charlie")

        def check(snapshot=None, to=peer_typed):
            args = argparse.Namespace(to=to, message="x")
            if snapshot is not None:
                args.channel_snapshot = snapshot
            return sp_routes.check(args, "channel", "to")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(check(), 2)
            self.assertIn("native SendMessage", err.getvalue())
            # Not a real snapshot: a look-alike, or one that does not list the target.
            self.assertEqual(check(types.SimpleNamespace(members=[peer_typed])), 2)
            self.assertEqual(check(sp_channels._Snapshot("ops", [other_typed])), 2)
            self.assertIsNone(check(sp_channels._Snapshot("ops", [peer_typed])))
            # An unattributable sender stays refused even with a snapshot.
            os.environ["OPENCODE_SESSION_ID"] = OC_A
            self.assertEqual(check(sp_channels._Snapshot("ops", [peer_typed])), 2)
            self.assertIn("sender cannot be attributed", err.getvalue())
        self.assertEqual(peer.of_type("user"), [])
        self.assertEqual(other.of_type("user"), [])

    def test_channel_verb_does_not_exempt_other_verbs(self):
        self.sender_cc("alpha")
        _peer, peer_typed = self.peer_cc("bravo")
        args = argparse.Namespace(to=peer_typed, message="x", channel_snapshot=sp_channels._Snapshot("ops", [peer_typed]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sp_routes.check(args, "send", "to"), 2)

    def test_an_unverified_claude_sender_is_not_labelled_a_claude_session(self):
        # Only an environment id, no live registry record: no Claude label, no reply route.
        sid = new_uuid()
        self.write_record(os.getpid(), "ghost", sid, str(self.socks / "ghost.sock"), procStart="")
        self.me = self.as_cc(sid)
        peer, peer_typed = self.peer_cc("bravo")
        self.channel("ops", peer_typed)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {peer_typed: "transport accepted"})
        content = wait_for(lambda: peer.of_type("user"))[0]["message"]["content"]
        self.assertIn("[session-peers from peer of unverified runtime (%s)]" % sid, content)
        self.assertNotIn("Claude Code session", content)
        self.assertNotIn("ghost.sock", content)
        self.assertNotIn("<cross-session-message", content)

    def test_pacing_spaces_claude_deliveries_and_headers_differ(self):
        self.sender_cc("alpha")
        listeners, typed = [], []
        for name in ("b1", "b2", "b3"):
            listener, member = self.peer_cc(name)
            listeners.append(listener)
            typed.append(member)
        tid, _rollout = self.one_thread()
        self.channel("ops", typed[0], "codex:" + tid, typed[1], typed[2])
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sleeps, [1.0, 1.0])
        contents = []
        for listener, member in zip(listeners, typed):
            frames = wait_for(lambda l=listener: l.of_type("user"))
            self.assertEqual(len(frames), 1)
            content = frames[0]["message"]["content"]
            self.assertIn("to %s]" % member, content)
            contents.append(content)
        self.assertEqual(len(set(contents)), 3)

    def test_pacing_is_measured_from_the_previous_claude_delivery(self):
        self.sender_cc("alpha")
        _l1, first = self.peer_cc("b1")
        _l2, second = self.peer_cc("b2")
        tid, _rollout = self.one_thread()
        self.channel("ops", first, "codex:" + tid, second)
        sp_messaging = sys.modules["session_peers.messaging"]
        real = sp_messaging._dispatch_send

        def slow(args, sid=None):
            # The Codex delivery between the two Claude ones takes 0.4 s.
            if args.to.startswith("codex:"):
                self.now[0] += 0.4
            return real(args, sid)

        with mock.patch.object(sp_messaging, "_dispatch_send", slow):
            rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 0.6)

    def test_pacing_does_not_slow_codex_or_opencode(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + OC_B, "oc:" + OC_A)
        rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.sleeps, [])

    def test_paused_codex_thread_reports_queued(self):
        self.sender_cc()
        tid, rollout = self.one_thread()
        append(rollout, ev("task_started", turn_id="t9"), ev("turn_aborted", turn_id="t9"))
        self.channel("ops", "codex:" + tid)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"codex:" + tid: "queued (thread paused)"})
        self.assertEqual(len(self.queue_calls()), 1)

    def test_codex_and_opencode_senders_reach_a_claude_member_through_the_normal_sender(self):
        peer, peer_typed = self.peer_cc("bravo")
        me = self.as_codex()
        self.make_state_db([{"id": me.partition(":")[2], "name": "me", "rollout_path": str(self.make_rollout("me.jsonl"))}])
        self.channel("ops", peer_typed, owner=me)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        content = wait_for(lambda: peer.of_type("user"))[0]["message"]["content"]
        self.assertIn("[session-peers from Codex thread", content)
        self.assertIn("freeze main", content)
        os.environ.pop("CODEX_THREAD_ID")
        self.as_opencode(OC_A)
        self.channel("octo", peer_typed, owner="oc:" + OC_A)
        rc, result, err = self.send(name="octo", message="from oc")
        self.assertEqual(rc, 0, err)
        content = wait_for(lambda: len(peer.of_type("user")) == 2 and peer.of_type("user")[1])["message"]["content"]
        self.assertIn("[session-peers from OpenCode session", content)

    def test_one_member_failing_does_not_stop_the_rest(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        ghost = new_uuid()
        self.channel("ops", "codex:" + ghost, "oc:" + OC_B, "codex:" + tid)
        rc, result, err = self.send()
        self.assertEqual(rc, 1)
        self.assertEqual(result["status"], "partial")
        outcomes = result["outcomes"]
        # A thread the store does not know cannot be proven gone: its route refuses.
        self.assertTrue(outcomes["codex:" + ghost].startswith("refused: "), outcomes)
        self.assertEqual(outcomes["oc:" + OC_B], "not woken (OpenCode idle)")
        self.assertEqual(outcomes["codex:" + tid], "transport accepted")
        self.assertEqual(len(self.queue_calls()), 1)

    def test_empty_channel_records_history_only(self):
        self.sender_cc()
        self.channel("solo")
        rc, result, err = self.send(name="solo")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {})
        entry = self.send_entries("solo")[0]
        self.assertEqual((entry["data"]["delivery"], entry["data"]["recipients"]), ("none", []))
        self.assertEqual(self.view_of("solo")["state"], "recorded")
        self.assertFalse(pathlib.Path(sp_channels.sends_dir()).exists())
        rc, _out, err = self.cli("send", "--to", "#solo", "--message", "x", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("no recipient of this message can be recorded", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(len(self.send_entries("solo")), 1)

    def test_size_precheck_refuses_before_journaling_or_sending(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        body = "x" * (sp_constants.MAX_TEXT_CHARS - 10)
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", body, "--record")
        self.assertEqual(rc, 1)
        self.assertIn("peer cap", err)
        self.assertIn("nothing was journaled or sent", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])
        self.nothing_delivered()
        rc, _result, err = self.send(message="y" * 1000)
        self.assertEqual(rc, 0, err)

    def test_claude_sender_with_a_quote_heavy_name_is_never_journaled_then_refused(self):
        # A double quote escapes to six bytes in the wrapper's from-name; a long
        # registry name must be cut and measured so the preflight stays an upper bound.
        self.sender_cc('"' * 5000)
        peer, peer_typed = self.peer_cc("bravo")
        self.channel("ops", peer_typed)
        body = "x" * (sp_constants.MAX_TEXT_CHARS - 10000)
        rc, result, err = self.send("--record", message=body)
        if rc == 0:
            frames = wait_for(lambda: peer.of_type("user"))
            self.assertEqual(len(frames), 1)
            content = frames[0]["message"]["content"]
            self.assertLessEqual(len(content.encode("utf-8")), sp_constants.MAX_TEXT_CHARS)
            self.assertIn('from-name="%s"' % ("&quot;" * sp_constants.MAX_TAG_FIELD_CHARS), content)
        else:
            self.assertEqual(rc, 1)
            self.assertIn("nothing was journaled or sent", err)
            self.assertEqual(self.journal_entries(), [])
            self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])
            self.assertEqual(peer.of_type("user"), [])

    def test_body_over_the_message_cap_is_refused_like_send(self):
        self.sender_cc()
        self.channel("ops")
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x" * (sp_constants.MAX_TEXT_CHARS + 1))
        self.assertEqual(rc, 1)
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])

    def test_untrusted_text_is_escaped_in_human_output(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        os.environ["FAKE_CODEX_RC"] = "1"
        os.environ["FAKE_CODEX_STDERR"] = "boom\x1b[31m red\x07"
        rc, out, err = self.cli("send", "--to", "#ops", "--message", "line1\nline2\x1b[31m red\x07")
        self.assertEqual(rc, 1)
        for text in (out, err, self.ok("tail", "ops")):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
        self.assertIn("refused: ", out)
        outcomes = self.view_of()["recipients"][0]["state"]
        self.assertNotIn("\x1b", outcomes)
        entry = [l for l in self.ok("tail", "ops").splitlines() if "[channel.send]" in l]
        self.assertEqual(len(entry), 1)
        self.assertIn("line1 line2", entry[0])


class ChannelLivenessSendTests(ChannelSendBase):
    def test_opencode_gone_needs_a_complete_walk_and_not_found(self):
        self.sender_cc()
        self.channel("ops", "oc:" + MISSING_OC, "oc:" + OC_B)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"]["oc:" + MISSING_OC], "skipped: gone")
        self.assertEqual(result["outcomes"]["oc:" + OC_B], "not woken (OpenCode idle)")
        sessions = [c["params"]["sessionID"] for c in self.oc_calls("session.prompt")]
        self.assertEqual(sessions, [OC_B])

    def test_opencode_omitted_but_retrievable_is_delivered_without_waking(self):
        self.sender_cc()
        self.channel("ops", "oc:" + OC_CHILD)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"oc:" + OC_CHILD: "not woken (OpenCode idle)"})
        prompt = self.oc_calls("session.prompt")[0]
        self.assertEqual(prompt["params"]["sessionID"], OC_CHILD)
        self.assertEqual(prompt["data"]["resume"], False)

    def test_opencode_incomplete_walk_is_unverified_not_gone(self):
        self.sender_cc()
        self.channel("ops", "oc:" + MISSING_OC)
        self.set_oc(page_size=1, repeat_cursor=True)
        rc, result, err = self.send()
        self.assertEqual(rc, 1)
        outcome = result["outcomes"]["oc:" + MISSING_OC]
        self.assertTrue(outcome.startswith("refused: "), outcome)
        self.assertNotEqual(outcome, "skipped: gone")

    def test_opencode_bound_buddy_is_not_woken_by_a_channel_either(self):
        self.sender_cc()
        self.channel("ops", "oc:" + OC_B)
        with mock.patch.object(sp_buddy, "read_buddy", return_value={"buddy": {"kind": "oc", "uuid": OC_B}}):
            rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"oc:" + OC_B: "not woken (OpenCode idle)"})
        self.assertEqual(self.oc_calls("session.prompt")[0]["data"]["resume"], False)

    def test_opencode_running_session_reports_accepted(self):
        self.sender_cc()
        self.channel("ops", "oc:" + OC_B)
        self.set_oc(active=[OC_B])
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"oc:" + OC_B: "transport accepted"})

    def test_claude_gone_when_dead_or_omitted_by_a_complete_read(self):
        self.sender_cc()
        dead_sid = new_uuid()
        self.write_record(2 ** 22 + 12345, "ghost", dead_sid, str(self.socks / "x.sock"))
        unknown = "cc:" + new_uuid()
        self.channel("ops", "cc:" + dead_sid, unknown)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"cc:" + dead_sid: "skipped: gone", unknown: "skipped: gone"})
        self.nothing_delivered()

    def test_claude_incomplete_registry_read_is_unverified(self):
        self.sender_cc()
        unknown = "cc:" + new_uuid()
        self.channel("ops", unknown)
        (self.sessions / "broken.json").write_text("{not json")
        rc, result, err = self.send()
        self.assertEqual(rc, 1)
        self.assertTrue(result["outcomes"][unknown].startswith("refused: "), result)

    def test_codex_gone_only_when_the_thread_is_proven_not_held(self):
        self.sender_cc()
        tid, rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        self.clear_holders()
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"codex:" + tid: "skipped: gone"})
        self.assertEqual(self.queue_calls(), [])
        self.set_holder(rollout)
        os.environ["FAKE_LSOF_RC"] = "1"
        rc, result, err = self.send()
        self.assertEqual(rc, 1)
        outcome = result["outcomes"]["codex:" + tid]
        self.assertTrue(outcome.startswith("refused: "), outcome)
        self.assertIn("liveness is unavailable", outcome)
        self.assertEqual(self.queue_calls(), [])

    def test_codex_unknown_schema_is_unverified(self):
        self.sender_cc()
        tid = new_uuid()
        self.make_state_db([], filename="state_1.sqlite", good=False)
        self.channel("ops", "codex:" + tid)
        rc, result, err = self.send()
        # Delivery goes through the route, which queues a UUID in degraded mode.
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {"codex:" + tid: "transport accepted"})


class ChannelJournalSendTests(ChannelSendBase):
    def acks(self, mid):
        rc, out, err = self.cli("inbox", "acks", mid, "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_record_journals_one_entry_naming_every_resolvable_member(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + OC_B)
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        mid = result["message_id"]
        self.assertEqual(result["journal"], {"message_id": mid, "recorded": True})
        entries = self.journal_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["message_id"], entries[0]["from"]), (mid, self.me))
        self.assertEqual(entries[0]["to"], ["codex:" + tid, "oc:" + OC_B])
        self.assertEqual(entries[0]["body"], "freeze main")
        text = self.queue_calls()[0][4]
        self.assertIn(sp_journal.pointer_text(mid), text)
        data = self.send_entries()[0]["data"]
        self.assertEqual((data["journaled"], data["message_id"], data["excluded"]), (True, mid, []))
        acks = self.acks(mid)
        self.assertEqual([r["slot"] for r in acks["recipients"]], ["codex:" + tid, "oc:" + OC_B])
        self.assertEqual({r["state"] for r in acks["recipients"]}, {"pending"})

    def test_journal_is_written_before_history_and_history_before_delivery(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        seen = {}

        def hook(name):
            if name in ("send_after_journal", "send_after_lock", "send_after_history"):
                seen[name] = (
                    len(self.journal_entries()), len(self.send_entries()), len(self.queue_calls()),
                )

        with mock.patch.object(sp_channels, "_failpoint", hook):
            rc, _result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen["send_after_journal"], (1, 0, 0))
        self.assertEqual(seen["send_after_lock"], (1, 0, 0))
        self.assertEqual(seen["send_after_history"], (1, 1, 0))
        self.assertEqual(len(self.queue_calls()), 1)

    def test_d1_unresolvable_member_is_excluded_from_the_ack_denominator(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + OC_B)
        # OC_B is listed (so it is not gone) but cannot be fetched: the journal cannot address it.
        self.set_oc(get_error_tag="Boom")
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 1, err)
        mid = result["message_id"]
        self.assertEqual(result["outcomes"]["codex:" + tid], "transport accepted")
        self.assertTrue(result["outcomes"]["oc:" + OC_B].startswith("refused: "), result)
        self.assertEqual(result["notes"], {"oc:" + OC_B: "ACK not requested (unverified)"})
        self.assertEqual(self.journal_entries()[0]["to"], ["codex:" + tid])
        self.assertEqual([r["slot"] for r in self.acks(mid)["recipients"]], ["codex:" + tid])
        self.assertEqual(self.oc_calls("session.prompt"), [])
        outcome = [e for e in self.history() if e["kind"] == "channel.outcome"][0]["data"]
        self.assertEqual(outcome["notes"], result["notes"])
        self.assertEqual(outcome["outcomes"], result["outcomes"])
        view = self.view_of()
        rows = {r["recipient"]: r for r in view["recipients"]}
        self.assertEqual(rows["oc:" + OC_B]["ack"], "not requested (unverified)")
        self.assertTrue(rows["oc:" + OC_B]["state"].startswith("refused: "))
        self.assertEqual((rows["codex:" + tid]["state"], rows["codex:" + tid]["ack"]), ("transport accepted", "pending"))
        rc, out, err = self.cli("send", "--to", "#ops", "--message", "again", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("ACK not requested (unverified)", out)

    def test_d1_zero_resolvable_refuses_before_recording_or_delivering(self):
        self.sender_cc()
        self.channel("ops", "oc:" + OC_B)
        self.set_oc(get_error_tag="Boom")
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("no recipient of this message can be recorded for ACK", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.oc_calls("session.prompt"), [])
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])

    def test_d1_gone_members_are_not_journaled(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + MISSING_OC)
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.journal_entries()[0]["to"], ["codex:" + tid])
        self.assertEqual(result["outcomes"]["oc:" + MISSING_OC], "skipped: gone")
        rows = {r["recipient"]: r for r in self.view_of()["recipients"]}
        self.assertEqual(rows["oc:" + MISSING_OC]["ack"], "not requested")

    def test_member_unreachable_after_journaling_stays_pending_with_its_refusal(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        os.environ["FAKE_CODEX_RC"] = "1"
        os.environ["FAKE_CODEX_STDERR"] = "queue broke"
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 1)
        self.assertTrue(result["outcomes"]["codex:" + tid].startswith("refused: "), result)
        recipient = self.acks(result["message_id"])["recipients"][0]
        self.assertEqual((recipient["slot"], recipient["state"]), ("codex:" + tid, "pending"))
        self.assertIn("codex queue failed", recipient["transport_error"]["detail"])

    def test_transport_error_is_appended_only_for_journaled_slots(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        os.environ["FAKE_CODEX_RC"] = "1"
        with mock.patch.object(sp_journal, "transport_error") as appended:
            rc, _result, err = self.send()
            self.assertEqual(rc, 1)
            appended.assert_not_called()
            rc, result, err = self.send("--record")
            self.assertEqual(rc, 1)
            appended.assert_called_once()
            self.assertEqual(appended.call_args[0][:2], (result["message_id"], "codex:" + tid))

    def test_failed_transport_error_update_exits_76(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        os.environ["FAKE_CODEX_RC"] = "1"
        with mock.patch.object(sp_journal, "transport_error", side_effect=sp_journal.JournalUpdateFailed("disk full")):
            rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x", "--record")
        self.assertEqual(rc, 76)
        self.assertIn("journal update failed: disk full", err)
        # The outcome entry is still appended and the fan-out finished.
        self.assertEqual([e["kind"] for e in self.history()][-1], "channel.outcome")

    def test_journal_failure_sends_nothing_and_leaves_no_history(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        for exc, code in ((sp_journal.JournalFull("full"), 75), (sp_journal.JournalUnavailable("down"), 1)):
            with self.subTest(code=code):
                with mock.patch.object(sp_journal, "record_message", side_effect=exc):
                    rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x", "--record")
                self.assertEqual(rc, code)
                self.assertIn(str(exc), err)
        self.nothing_delivered()
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])
        self.assertFalse(list(pathlib.Path(sp_channels.sends_dir()).glob("*.lock")))

    def test_history_append_failure_after_journaling_reports_journaled_not_delivered(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        real = sp_channels.append_history

        def fail_send(key, kind, actor, data=None):
            if kind == "channel.send":
                raise sp_channels.ChannelError("could not append to the history of #ops: disk gone", 1)
            return real(key, kind, actor, data)

        with mock.patch.object(sp_channels, "append_history", fail_send):
            rc, _out, err = self.cli("send", "--to", "#ops", "--message", "x", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("journaled, not delivered", err)
        self.assertEqual(len(self.journal_entries()), 1)
        self.nothing_delivered()
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])
        self.assertFalse(list(pathlib.Path(sp_channels.sends_dir()).glob("*.lock")))

    def test_crash_during_the_send_append_delivers_nothing(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        real = sp_channels.append_history

        def crash(key, kind, actor, data=None):
            if kind == "channel.send":
                raise RuntimeError("process died")
            return real(key, kind, actor, data)

        with mock.patch.object(sp_channels, "append_history", crash):
            with self.assertRaises(RuntimeError):
                self.cli("send", "--to", "#ops", "--message", "x", "--record")
        self.nothing_delivered()
        entry = self.journal_entries()[0]
        self.assertEqual([r["state"] for r in entry["recipients"]], ["pending"])
        self.assertEqual([e["kind"] for e in self.history()], ["channel.created"])

    def test_history_keeps_the_body_after_the_journal_prunes_it(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        rc, result, err = self.send("--record", message="freeze main until tagged")
        self.assertEqual(rc, 0, err)
        mid = result["message_id"]
        sp_journal.ack(mid, "codex:" + tid)
        # A later write under a tight cap prunes the finished entry to make room.
        other = "cc:" + new_uuid()
        for _ in range(3):
            rid = new_uuid()
            sp_journal.record_request(
                rid, other, ["codex:" + tid], "r" * 2000, time.time() + 600, "/nonexistent/spj/%s.json" % rid)
        size = os.path.getsize(os.path.join(sp_journal.journal_dir(), sp_journal.JOURNAL_NAME))
        os.environ["SESSION_PEERS_JOURNAL_MAX_BYTES"] = str(size + 20)
        # The write below does not fit until the acknowledged entry is pruned.
        sp_journal.record_message(self.me, ["codex:" + tid], "freeze main until tagged")
        self.assertNotIn(mid, [e["message_id"] for e in self.journal_entries()])
        send = [e for e in self.tail_json("ops")["entries"] if e["kind"] == "channel.send"][0]
        self.assertEqual(send["data"]["body"], "freeze main until tagged")
        self.assertIn("freeze main until tagged", self.ok("tail", "ops"))
        rows = send["view"]["recipients"]
        self.assertEqual(rows[0]["ack"], "unverified")

    def test_acked_state_is_read_from_the_journal(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        sp_journal.ack(result["message_id"], "codex:" + tid)
        row = self.view_of()["recipients"][0]
        self.assertEqual((row["state"], row["ack"]), ("acked", "acked"))
        self.assertIn("codex:%s: acked (ACK acked)" % tid, self.ok("tail", "ops"))

    def test_unreadable_journal_reads_unverified(self):
        self.sender_cc()
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        rc, _result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        with mock.patch.object(sp_journal, "read_only_view", side_effect=sp_journal.JournalUnavailable("denied")):
            row = self.view_of()["recipients"][0]
        self.assertEqual((row["state"], row["ack"]), ("transport accepted", "unverified"))


class ChannelInterruptedSendTests(ChannelSendBase):
    def setUp(self):
        super().setUp()
        self.sender_cc()
        self.tid, self.rollout = self.one_thread()
        self.channel("ops", "codex:" + self.tid)

    def crash_at(self, point):
        """Run one send that dies at ``point`` (an in-process stand-in for a killed sender)."""
        def hook(name):
            if name == point:
                raise RuntimeError("killed at " + name)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            with self.assertRaisesRegex(RuntimeError, "killed at"):
                self.cli("send", "--to", "#ops", "--message", "freeze main", "--record")
        return self.send_entries()[0]["data"]["message_id"]

    def lock_file(self, mid):
        return pathlib.Path(sp_channels.send_lock_path(mid))

    def test_a_dead_sender_with_no_outcome_reads_unknown_and_is_never_redelivered(self):
        mid = self.crash_at("send_before_outcome")
        self.assertTrue(self.lock_file(mid).exists())
        # A pid in the file, live or not, plays no part.
        self.lock_file(mid).write_text("%d\n" % os.getpid())
        self.assertEqual(len(self.queue_calls()), 1)
        for _ in range(2):
            view = self.view_of()
            self.assertEqual(view["state"], "unknown")
            self.assertEqual([r["state"] for r in view["recipients"]], ["unknown"])
        self.assertIn("codex:%s: unknown" % self.tid, self.ok("tail", "ops"))
        self.assertEqual(len(self.queue_calls()), 1)

    def test_a_lock_held_by_an_unrelated_process_reads_in_progress_despite_pid_reuse(self):
        mid = self.crash_at("send_after_history")
        helper = subprocess.Popen([sys.executable, "-c", "pass"])
        helper.wait()
        self.lock_file(mid).write_text("%d\n" % helper.pid)
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.lock_file(mid))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self._children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        with mock.patch.object(sp_process, "pid_alive", side_effect=AssertionError("a pid was probed")):
            view = self.view_of()
        self.assertEqual(view["state"], "in progress")
        self.assertEqual([r["state"] for r in view["recipients"]], ["in progress"])
        holder.stdin.close()
        holder.wait(timeout=10)
        self.assertEqual(self.view_of()["state"], "unknown")

    def test_a_lock_that_cannot_be_opened_reads_unverified(self):
        mid = self.crash_at("send_after_history")
        real_open = os.open
        target = str(self.lock_file(mid))

        def deny(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", deny):
            view = self.view_of()
        self.assertEqual(view["state"], "unverified")
        self.assertEqual([r["state"] for r in view["recipients"]], ["unverified"])

    def test_an_outcome_that_lands_between_the_lock_probe_and_the_re_search_reads_complete(self):
        mid = self.crash_at("send_before_outcome")
        real_probe = sp_channels._probe_send_lock

        def land_then_free(message_id):
            real_probe(message_id)
            sp_channels.append_history("ops", "channel.outcome", self.me, {
                "message_id": message_id, "outcomes": {"codex:" + self.tid: "transport accepted"}})
            return "free"

        with mock.patch.object(sp_channels, "_probe_send_lock", land_then_free):
            view = self.view_of()
        self.assertEqual(view["state"], "complete")
        self.assertEqual([r["state"] for r in view["recipients"]], ["transport accepted"])
        self.assertTrue(mid)

    def test_a_missing_lock_without_an_outcome_reads_unknown(self):
        mid = self.crash_at("send_after_history")
        self.lock_file(mid).unlink()
        self.assertEqual(self.view_of()["state"], "unknown")

    def test_a_reader_between_the_history_entry_and_delivery_reads_in_progress(self):
        seen = []

        def hook(name):
            if name == "send_after_history":
                self.assertEqual(self.queue_calls(), [])
                seen.append(self.view_of())

        with mock.patch.object(sp_channels, "_failpoint", hook):
            rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["state"], "in progress")
        self.assertEqual(self.view_of()["state"], "complete")

    def test_an_outcome_past_the_first_tail_page_is_found(self):
        rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        for i in range(25):
            sp_channels.append_history("ops", "channel.note", self.me, {"body": "n%d" % i})
        send_seq = self.send_entries()[0]["seq"]
        page = self.tail_json("ops", "--since", str(send_seq - 1), "--limit", "1")
        self.assertEqual([e["kind"] for e in page["entries"]], ["channel.send"])
        self.assertEqual(page["entries"][0]["view"]["state"], "complete")
        self.assertEqual(page["entries"][0]["view"]["recipients"][0]["state"], "transport accepted")

    def test_the_outcome_search_pages_through_every_retained_entry(self):
        def hook(name):
            if name == "send_before_outcome":
                for i in range(5):
                    sp_channels.append_history("ops", "channel.note", self.me, {"body": "n%d" % i})

        with mock.patch.object(sp_channels, "_failpoint", hook):
            rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        send = self.send_entries()[0]
        outcome = [e for e in self.history() if e["kind"] == "channel.outcome"][0]
        self.assertEqual(outcome["seq"], send["seq"] + 6)
        with mock.patch.object(sp_constants, "TOPIC_TAIL_MAX", 2):
            found = sp_channels._find_outcome("ops", send["data"]["message_id"], send["seq"])
        self.assertEqual(found["seq"], outcome["seq"])
        self.assertIsNone(sp_channels._find_outcome("ops", new_uuid(), send["seq"]))

    def test_a_torn_outcome_append_reads_unknown_and_a_later_send_is_readable(self):
        real = sp_channels.append_history

        def torn(key, kind, actor, data=None):
            if kind == "channel.outcome":
                log, _meta, _lock = sp_topics._topic_paths(sp_channels.topic_name(key), sp_channels.history_dir())
                with open(log, "ab") as fh:
                    fh.write(b'{"seq": 3, "ts": "2026-01-01T00:00:00.000Z", "kind": "channel.outcome", "da')
                raise RuntimeError("killed mid-append")
            return real(key, kind, actor, data)

        with mock.patch.object(sp_channels, "append_history", torn):
            with self.assertRaises(RuntimeError):
                self.cli("send", "--to", "#ops", "--message", "first")
        self.assertEqual(self.view_of()["state"], "unknown")
        self.assertEqual(len(self.queue_calls()), 1)
        rc, second, err = self.send(message="second")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.queue_calls()), 2)
        sends = [e for e in self.tail_json("ops")["entries"] if e["kind"] == "channel.send"]
        self.assertEqual([e["data"]["body"] for e in sends], ["first", "second"])
        self.assertEqual([e["view"]["state"] for e in sends], ["unknown", "complete"])
        self.assertEqual(len(self.queue_calls()), 2)

    def test_gc_removes_only_send_locks_nobody_holds(self):
        mid = self.crash_at("send_after_history")
        held = new_uuid()
        held_path = pathlib.Path(sp_channels.send_lock_path(held))
        held_path.write_text("")
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(held_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self._children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        (pathlib.Path(sp_channels.sends_dir()) / "not-a-uuid.lock").write_text("")
        self.assertEqual(self.cli("gc")[0], 0)
        self.assertFalse(self.lock_file(mid).exists())
        self.assertTrue(held_path.exists())
        self.assertTrue((pathlib.Path(sp_channels.sends_dir()) / "not-a-uuid.lock").exists())
        self.assertEqual(self.view_of()["state"], "unknown")
        holder.stdin.close()
        holder.wait(timeout=10)
        self.assertEqual(self.cli("gc")[0], 0)
        self.assertFalse(held_path.exists())

    def test_send_lock_names_must_be_uuids(self):
        for bad in ("../x", "abc", "", None):
            with self.assertRaises(ValueError):
                sp_channels.send_lock_path(bad)
        self.assertTrue(sp_channels.send_lock_path(new_uuid()).endswith(".lock"))

    def test_send_lock_is_private(self):
        def hook(name):
            if name == "send_after_lock":
                root = pathlib.Path(sp_channels.sends_dir())
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                (lock,) = root.glob("*.lock")
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertFalse(list(pathlib.Path(sp_channels.sends_dir()).glob("*.lock")))


class ChannelRecordTrustTests(ChannelSendBase):
    """A membership file is peer-writable and is not trusted on read."""

    def setUp(self):
        super().setUp()
        self.sender_cc()

    def rewrite(self, mutate, name="ops"):
        path = sp_channels._record_path(name)
        with open(path) as fh:
            rec = json.load(fh)
        mutate(rec)
        with open(path, "w") as fh:
            json.dump(rec, fh)

    def test_a_roster_over_the_member_cap_is_refused_not_truncated(self):
        self.channel("ops")
        extra = ["cc:" + new_uuid() for _ in range(sp_constants.CHANNEL_MAX_MEMBERS + 1)]
        self.rewrite(lambda rec: rec["members"].extend(extra))
        rc, _result, err = self.send()
        self.assertEqual(rc, 2)
        self.assertIn("CHANNEL_MAX_MEMBERS", err)
        self.assertEqual(self.history(), [e for e in self.history() if e["kind"] == "channel.created"])
        self.nothing_delivered()

    def test_members_refuses_a_roster_over_the_cap(self):
        self.channel("ops")
        extra = ["cc:" + new_uuid() for _ in range(sp_constants.CHANNEL_MAX_MEMBERS + 1)]
        self.rewrite(lambda rec: rec["members"].extend(extra))
        rc, out, err = self.cli("channel", "members", "ops")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("lists %d members" % (len(extra) + 1), err)
        self.assertIn("CHANNEL_MAX_MEMBERS", err)

    def test_a_member_listed_many_times_is_delivered_once(self):
        listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        self.rewrite(lambda rec: rec["members"].extend([peer] * 20 + [peer.upper().replace("CC:", "cc:")]))
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(result["outcomes"]), [peer])
        self.assertTrue(wait_for(lambda: listener.of_type("user")))
        time.sleep(0.5)  # a duplicate delivery would arrive by now
        self.assertEqual(len(listener.of_type("user")), 1)

    def test_malformed_members_are_dropped(self):
        self.channel("ops")
        self.rewrite(lambda rec: rec["members"].extend([7, None, {"a": 1}, "nonsense", "cc:short", ["cc:x"]]))
        self.assertEqual(sp_channels.read_channel("ops")["members"], [self.me])
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {})

    def test_a_record_with_a_non_string_field_reads_as_absent(self):
        self.channel("ops")
        for field in ("display", "kind", "owner"):
            self.rewrite(lambda rec: rec.__setitem__(field, 5))
            self.assertIsNone(sp_channels.read_channel("ops"), field)
            self.rewrite(lambda rec: rec.__setitem__(field, "ops"))
        self.assertIsNotNone(sp_channels.read_channel("ops"))

    def test_an_oversized_record_file_is_refused_unread(self):
        self.channel("ops")
        self.rewrite(lambda rec: rec.__setitem__("pad", "x" * sp_channels.RECORD_MAX_BYTES))
        self.assertIsNone(sp_channels.read_channel("ops"))
        rc, _result, err = self.send()
        self.assertEqual(rc, 2)
        self.assertIn("no channel named #ops", err)
        self.nothing_delivered()

    def test_a_body_that_only_the_codex_tag_and_origin_push_over_budget_journals_nothing(self):
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        header = sp_channels._delivery_text("ops", new_uuid(), self.me, "codex:" + tid, "", True)
        # The bare delivery text fits the budget; the transport's tag and origin lines do not.
        body = "x" * (sp_runtime.argv_text_budget() - sp_runtime.utf8_len(header) - 1)
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", body, "--record")
        self.assertEqual(rc, 1, err)
        self.assertIn("nothing was journaled or sent", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries(), [])
        self.nothing_delivered()

    def test_multibyte_sender_metadata_counts_by_bytes_in_the_codex_framing(self):
        # A 256-emoji session name is 1 KiB per use in the tag and again in the origin line.
        self.sender_rec["name"] = "\U0001F600" * sp_constants.MAX_TAG_FIELD_CHARS
        self.write_record(
            os.getpid(), self.sender_rec["name"], self.me.partition(":")[2], self.sender_rec["messagingSocketPath"])
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        header = sp_channels._delivery_text("ops", new_uuid(), self.me, "codex:" + tid, "", True)
        # Fits an all-ASCII worst case (about 1.5 KB of framing), not the real emoji framing (over 2 KB).
        body = "x" * (sp_runtime.argv_text_budget() - sp_runtime.utf8_len(header) - 1700)
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", body, "--record")
        self.assertEqual(rc, 1, err)
        self.assertIn("nothing was journaled or sent", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries(), [])
        self.nothing_delivered()

    def test_a_body_over_the_codex_argv_budget_with_record_journals_nothing(self):
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid)
        body = "x" * (sp_runtime.argv_text_budget() + 10)
        self.assertLess(len(body), sp_constants.MAX_TEXT_CHARS)
        rc, _out, err = self.cli("send", "--to", "#ops", "--message", body, "--record")
        self.assertEqual(rc, 1)
        self.assertIn("codex queue", err)
        self.assertIn("nothing was journaled or sent", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries(), [])
        self.nothing_delivered()
