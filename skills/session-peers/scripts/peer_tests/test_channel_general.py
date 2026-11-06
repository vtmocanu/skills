"""Derived #general, `@all` with its limits and ACK tally, the D1/D2 statements and leave markers."""

import fcntl
import hashlib

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import HOLDER, MISSING_OC, ChannelSendBase
from peer_tests.test_opencode import OC_A, OC_B, oc_session
from session_peers import channels as sp_channels, journal as sp_journal

BROADCAST = "@all freeze main until the release is tagged"
COVERAGE_JSON = {"scope": "top-level sessions updated within the window", "hours": 6}


class GeneralBase(ChannelSendBase):
    def setUp(self):
        super().setUp()
        os.environ["SESSION_PEERS_OPENCODE_RECENT_HOURS"] = "6"
        self.wall = [5000.0]
        patch = mock.patch.object(sp_channels, "_wall", lambda: self.wall[0])
        patch.start()
        self.addCleanup(patch.stop)
        self.set_oc(sessions=[oc_session(OC_B, "oc-idle")])
        self.sender_cc()
        self.tid, self.rollout = self.one_thread()

    def post(self, message=BROADCAST, *extra):
        return self.send(*extra, name="general", message=message)

    def as_sender(self):
        return self.as_cc(self.me.partition(":")[2])

    def stamp_path(self, sender=None):
        digest = hashlib.sha256((sender or self.me).encode()).hexdigest()
        return pathlib.Path(sp_channels.channels_root()) / "allcast" / (digest + ".json")

    def acks(self, mid):
        rc, out, err = self.cli("inbox", "acks", mid, "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def general_members(self):
        rc, out, err = self.ch("members", "general", "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)


class GeneralBroadcastTests(GeneralBase):
    def test_example_2_at_all_to_general_with_ack_tally_and_rate_limit(self):
        rc, result, err = self.post()
        self.assertEqual(rc, 0, err)
        mid = result["message_id"]
        self.assertEqual(result["outcomes"], {
            "codex:" + self.tid: "transport accepted", "oc:" + OC_B: "not woken (OpenCode idle)"})
        self.assertEqual(result["opencode_coverage"], COVERAGE_JSON)
        entries = self.journal_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["to"], ["codex:" + self.tid, "oc:" + OC_B])
        self.assertEqual(entries[0]["from"], self.me)
        self.assertEqual(len(self.queue_calls()), 1)
        data = self.send_entries("general")[0]["data"]
        self.assertEqual((data["journaled"], data["delivery"]), (True, "fanout"))
        self.assertEqual(data["opencode_coverage"], COVERAGE_JSON)

        self.as_codex(self.tid)
        rc, out, err = self.cli("ack", mid)
        self.assertEqual(rc, 0, err)
        self.as_sender()
        states = {r["slot"]: r["state"] for r in self.acks(mid)["recipients"]}
        self.assertEqual(states, {"codex:" + self.tid: "acked", "oc:" + OC_B: "pending"})
        self.assertNotIn(self.me, states)

        # A second @all inside 60 seconds is refused, and nothing more is delivered.
        self.wall[0] += 59
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2)
        self.assertIn("60 seconds", err)
        self.assertIn("ALL_INTERVAL", err)
        self.assertEqual(len(self.journal_entries()), 1)
        self.assertEqual(len(self.queue_calls()), 1)
        self.wall[0] += 2
        rc, _result, err = self.post("@all and again")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.journal_entries()), 2)

    def test_the_coverage_statement_names_the_window_and_is_recorded(self):
        rc, out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "OpenCode coverage: top-level sessions updated within the last 6 h (as peers.py list)", out)
        self.assertNotIn("all OpenCode sessions", out)
        os.environ["SESSION_PEERS_OPENCODE_RECENT_HOURS"] = "1.5"
        rc, out, err = self.cli("send", "--to", "#general", "--message", "plain post")
        self.assertEqual(rc, 0, err)
        self.assertIn("within the last 1.5 h (as peers.py list)", out)
        rc, result, err = self.post("another plain post")
        self.assertEqual(result["opencode_coverage"]["hours"], 1.5)

    def test_opencode_discovery_failure_is_stated_and_recorded(self):
        self.set_oc(page_size=1, repeat_cursor=True)
        rc, out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 0, err)
        self.assertIn("note: OpenCode was not enumerated: ", out)
        self.assertNotIn("OpenCode coverage:", out)
        data = self.send_entries("general")[0]["data"]
        self.assertIs(data["opencode_coverage"]["enumerated"], False)
        self.assertTrue(data["opencode_coverage"]["error"])
        self.assertEqual(self.journal_entries()[0]["to"], ["codex:" + self.tid])
        self.wall[0] += 61
        rc, result, err = self.post("@all json")
        self.assertIs(result["opencode_coverage"]["enumerated"], False)

    def test_plain_post_is_recorded_only(self):
        rc, result, err = self.post("hello everyone")
        self.assertEqual(rc, 0, err)
        self.assertEqual((result["status"], result["outcomes"]), ("recorded", {}))
        self.assertEqual(result["opencode_coverage"], COVERAGE_JSON)
        self.assertNotIn("journal", result)
        self.nothing_delivered()
        self.assertEqual(self.journal_entries(), [])
        data = self.send_entries("general")[0]["data"]
        self.assertEqual((data["delivery"], data["journaled"], data["recipients"]), ("none", False, []))
        view = self.view_of("general")
        self.assertEqual(view["state"], "recorded")
        tail = self.ok("tail", "general")
        self.assertIn("hello everyone", tail)
        self.assertIn("delivery: recorded", tail)
        self.assertFalse(self.stamp_path().exists())
        # A plain post is not rate limited and "@allies" is not the broadcast.
        for _ in range(2):
            self.assertEqual(self.post("@allies assemble")[0], 0)
        self.assertEqual(self.journal_entries(), [])

    def test_record_on_a_plain_post_journals_without_delivering(self):
        rc, result, err = self.post("note for the record", "--record")
        self.assertEqual(rc, 0, err)
        mid = result["message_id"]
        self.assertEqual(result["outcomes"], {"codex:" + self.tid: "recorded", "oc:" + OC_B: "recorded"})
        self.nothing_delivered()
        entries = self.journal_entries()
        self.assertEqual([(e["message_id"], e["to"]) for e in entries],
                         [(mid, ["codex:" + self.tid, "oc:" + OC_B])])
        self.assertEqual(self.view_of("general")["state"], "recorded")
        self.assertFalse(self.stamp_path().exists())
        self.assertEqual({r["state"] for r in self.acks(mid)["recipients"]}, {"pending"})

    def test_record_on_a_plain_post_with_nobody_resolvable_is_refused(self):
        self.clear_holders()
        self.set_oc(get_error_tag="Boom")
        rc, _out, err = self.cli("send", "--to", "#general", "--message", "x", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("no recipient of this message can be recorded", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries("general"), [])

    def test_d1_unresolvable_member_is_refused_and_outside_the_denominator(self):
        self.set_oc(get_error_tag="Boom")
        rc, result, err = self.post()
        self.assertEqual(rc, 1, err)
        mid = result["message_id"]
        self.assertEqual(result["outcomes"]["codex:" + self.tid], "transport accepted")
        self.assertTrue(result["outcomes"]["oc:" + OC_B].startswith("refused: "), result)
        self.assertEqual(result["notes"], {"oc:" + OC_B: "ACK not requested (unverified)"})
        self.assertEqual([r["slot"] for r in self.acks(mid)["recipients"]], ["codex:" + self.tid])
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_d1_zero_resolvable_refuses_before_anything_and_keeps_the_allowance(self):
        self.clear_holders()
        self.set_oc(get_error_tag="Boom")
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 1)
        self.assertIn("no recipient of this message can be recorded for ACK", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries("general"), [])
        self.assertFalse(self.stamp_path().exists())
        self.nothing_delivered()
        self.set_oc(get_error_tag=None)
        rc, _result, err = self.post()
        self.assertEqual(rc, 0, err)

    def test_over_the_recipient_cap_is_refused_before_anything_is_journaled(self):
        ids = ["ses_%024dzz" % i for i in range(sp_channels.ALL_MAX_RECIPIENTS + 1)]
        self.set_oc(sessions=[oc_session(sid, "s%d" % i) for i, sid in enumerate(ids)])
        self.clear_holders()
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2)
        self.assertIn("at most 64", err)
        self.assertIn("ALL_MAX_RECIPIENTS", err)
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries("general"), [])
        self.assertFalse(self.stamp_path().exists())
        rc, _out, err = self.cli("send", "--to", "#general", "--message", "x", "--record")
        self.assertEqual(rc, 2)
        self.assertIn("at most 64", err)
        # Membership itself has no cap, and a plain post is not limited.
        self.assertEqual(len(self.general_members()["members"]), 65 + 1)
        self.assertEqual(self.post("fine")[0], 0)
        # The sender is not counted: 64 other members fit.
        self.set_oc(sessions=[oc_session(sid, "s") for sid in ids[:64]])
        self.assertEqual(self.post()[0], 0)
        self.assertEqual(len(self.journal_entries()[0]["to"]), 64)

    def test_stored_channel_at_all_is_journaled_without_record_and_limited(self):
        self.channel("ops", "codex:" + self.tid, "oc:" + OC_B)
        rc, result, err = self.send(name="ops", message=BROADCAST)
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["journal"], {"message_id": result["message_id"], "recorded": True})
        self.assertEqual(self.journal_entries()[0]["to"], ["codex:" + self.tid, "oc:" + OC_B])
        self.assertTrue(self.stamp_path().exists())
        self.assertNotIn("opencode_coverage", result)
        # The allowance is per sender across channels: #general is refused now too.
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2)
        self.assertIn("60 seconds", err)
        # A plain stored-channel post is neither journaled nor limited.
        rc, result, err = self.send(name="ops", message="plain")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("journal", result)

    def test_at_all_excludes_gone_members_from_the_delivery_set(self):
        self.channel("ops", "codex:" + self.tid, "oc:" + MISSING_OC)
        rc, result, err = self.send(name="ops", message=BROADCAST)
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"]["oc:" + MISSING_OC], "skipped: gone")
        self.assertEqual(self.journal_entries()[0]["to"], ["codex:" + self.tid])

    def test_a_member_unreachable_after_journaling_stays_pending(self):
        os.environ["FAKE_CODEX_RC"] = "1"
        rc, result, err = self.post()
        self.assertEqual(rc, 1)
        mid = result["message_id"]
        self.assertTrue(result["outcomes"]["codex:" + self.tid].startswith("refused: "))
        states = {r["slot"]: r["state"] for r in self.acks(mid)["recipients"]}
        self.assertEqual(states["codex:" + self.tid], "pending")
        self.assertTrue(self.stamp_path().exists())  # the message exists, so the allowance stays spent


class AllowanceTests(GeneralBase):
    def test_a_refused_journal_restores_the_allowance(self):
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal full")):
            rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.stamp_path().exists())
        self.nothing_delivered()
        rc, _result, err = self.post()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.queue_calls()), 1)

    def test_a_refused_journal_puts_back_the_previous_stamp(self):
        self.assertEqual(self.post()[0], 0)
        previous = self.stamp_path().read_text()
        self.wall[0] += 61
        with mock.patch.object(sp_journal, "record_message", side_effect=sp_journal.JournalFull("journal full")):
            rc, _out, _err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.stamp_path().read_text(), previous)

    def test_a_crash_after_the_reservation_keeps_the_stamp(self):
        def hook(name):
            if name == "allcast_after_reserve":
                raise RuntimeError("killed at " + name)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            with self.assertRaisesRegex(RuntimeError, "killed at"):
                self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertTrue(self.stamp_path().exists())
        self.assertEqual(self.journal_entries(), [])
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2)
        self.assertIn("60 seconds", err)

    def test_the_stamp_is_reserved_before_the_journal_and_the_lock_file_is_never_replaced(self):
        seen = []
        real = sp_journal.record_message
        lock = self.stamp_path().with_suffix(".lock")

        def spy(*args, **kwargs):
            fd = os.open(str(lock), os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = False
                except BlockingIOError:
                    held = True
            finally:
                os.close(fd)  # closing also drops a lock this probe took
            seen.append((self.stamp_path().exists(), os.stat(str(lock)).st_ino))
            self.assertTrue(held, "the allowance lock must be held while the message is journaled")
            return real(*args, **kwargs)

        with mock.patch.object(sp_journal, "record_message", spy):
            self.assertEqual(self.post()[0], 0)
        self.assertEqual(seen[0][0], True)
        inode = os.stat(str(lock)).st_ino
        self.assertEqual(seen[0][1], inode)
        self.wall[0] += 61
        self.assertEqual(self.post("@all again")[0], 0)
        self.assertEqual(os.stat(str(lock)).st_ino, inode)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.stamp_path().stat().st_mode), 0o600)

    def test_two_processes_get_one_broadcast_between_them(self):
        argv = ("send", "--to", "#general", "--message", BROADCAST, "--json")
        procs = [self.spawn(*argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            results.append((proc.returncode, out, err))
        codes = sorted(rc for rc, _o, _e in results)
        self.assertEqual(codes[1], 2, results)
        self.assertEqual(codes[0], 0, results)
        refused = [e for rc, _o, e in results if rc == 2][0]
        self.assertIn("60 seconds", refused)
        self.assertEqual(len(self.journal_entries()), 1)
        self.assertEqual(len(self.queue_calls()), 1)

    def test_a_sender_waits_on_the_held_allowance_lock_then_is_refused(self):
        self.wall[0] = time.time()  # the child process reads the real clock
        self.assertEqual(self.post()[0], 0)
        lock = self.stamp_path().with_suffix(".lock")
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self._children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        proc = self.spawn("send", "--to", "#general", "--message", BROADCAST,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaises(subprocess.TimeoutExpired):
            proc.wait(timeout=5)  # still blocked on the lock
        holder.stdin.close()
        holder.wait(timeout=10)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 2, err)
        self.assertIn("60 seconds", err)
        self.assertEqual(len(self.journal_entries()), 1)


class GeneralSenderTests(GeneralBase):
    def test_a_made_up_session_id_cannot_post_to_general(self):
        ghost = self.as_cc(new_uuid())  # UUID-shaped, but no such session
        for extra in ((), ("--record",)):
            for message in (BROADCAST, "plain note"):
                rc, _out, err = self.cli("send", "--to", "#general", "--message", message, *extra)
                self.assertEqual(rc, 2, (message, extra))
                self.assertIn("not a session peers.py list discovers", err)
        self.assert_nothing_recorded(ghost)

    def assert_nothing_recorded(self, sender):
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.queue_calls(), [])
        self.assertEqual(self.send_entries("general"), [])
        self.assertFalse(self.stamp_path(sender).exists())
        self.assertFalse((pathlib.Path(sp_channels.channels_root()) / "allcast").exists())

    def test_an_unverified_claude_sender_can_post(self):
        sid = new_uuid()
        self.write_record(os.getpid(), "noproc", sid, str(self.socks / "u.sock"), procStart="")
        self.me = self.as_cc(sid)
        rc, _out, err = self.cli("send", "--to", "#general", "--message", "plain note")
        self.assertEqual(rc, 0, err)

    def test_a_random_codex_thread_without_a_state_db_cannot_post(self):
        empty = self.root / "empty-codex"
        empty.mkdir()
        os.environ["CODEX_SQLITE_HOME"] = str(empty)
        self.addCleanup(os.environ.pop, "CODEX_SQLITE_HOME", None)
        tid = new_uuid()
        ghost = self.as_codex(tid)
        rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2, err)
        self.assertIn("not a session peers.py list discovers", err)
        self.assert_nothing_recorded(ghost)

    def test_a_dead_recorded_codex_thread_cannot_post(self):
        tid = new_uuid()
        ghost = self.as_codex(tid)
        threads = [{"id": tid, "name": "dead", "live": False}]
        with mock.patch.object(sp_channels.sp_codex, "codex_threads", return_value=(threads, True)):
            rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
        self.assertEqual(rc, 2, err)
        self.assertIn("not a session peers.py list discovers", err)
        self.assert_nothing_recorded(ghost)


class AllowanceClockTests(GeneralBase):
    def test_a_future_dated_stamp_is_refused_like_a_fresh_one(self):
        self.assertEqual(self.post()[0], 0)
        for ahead in (30, 100000):
            self.stamp_path().write_text(json.dumps({"sender": self.me, "at": self.wall[0] + ahead}))
            rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
            self.assertEqual(rc, 2, ahead)
            self.assertIn("60 seconds", err)
        self.assertEqual(len(self.journal_entries()), 1)

    def test_non_finite_and_non_numeric_stamps_are_refused_without_a_traceback(self):
        self.assertEqual(self.post()[0], 0)
        for raw in ('{"at": Infinity}', '{"at": -Infinity}', '{"at": NaN}', '{"at": "soon"}'):
            self.stamp_path().write_text(raw)
            rc, _out, err = self.cli("send", "--to", "#general", "--message", BROADCAST)
            self.assertEqual(rc, 2, raw)
            self.assertIn("60 seconds", err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(len(self.journal_entries()), 1)


class GeneralMembershipTests(GeneralBase):
    def test_an_unverified_claude_session_is_a_member(self):
        sid = new_uuid()
        self.write_record(os.getpid(), "noproc", sid, str(self.socks / "u.sock"), procStart="")
        members = [m["member"] for m in self.general_members()["members"]]
        self.assertIn("cc:" + sid, members)

    def test_a_codex_thread_with_unknown_liveness_is_a_member(self):
        tid = new_uuid()
        real = sp_channels.sp_codex.codex_threads
        threads = real()[0] + [{"id": tid, "name": "maybe", "live": None}]
        with mock.patch.object(sp_channels.sp_codex, "codex_threads", return_value=(threads, True)):
            members = [m["member"] for m in self.general_members()["members"]]
        self.assertIn("codex:" + tid, members)

    def test_members_are_what_list_shows_minus_left_sessions(self):
        peer, peer_typed = self.peer_cc("bravo")
        # A Codex shim record counts as codex:, and a dead Claude record is not a member.
        shim_sid = new_uuid()
        shim_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self._children.append(shim_proc)
        self.write_record(shim_proc.pid, "shimmy", shim_sid, str(self.socks / "shim.sock"), entrypoint="codex")
        dead_sid = new_uuid()
        self.write_record(2 ** 22 + 12345, "ghost", dead_sid, str(self.socks / "x.sock"))
        members = [m["member"] for m in self.general_members()["members"]]
        self.assertEqual(set(members[:3]), {self.me, peer_typed, "codex:" + shim_sid})
        self.assertIn("codex:" + self.tid, members)
        self.assertEqual(members[-1], "oc:" + OC_B)
        self.assertNotIn("cc:" + dead_sid, members)
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(self.general_members()["opencode_coverage"], COVERAGE_JSON)
        human = self.ok("members", "general")
        self.assertIn("OpenCode coverage: top-level sessions updated within the last 6 h", human)

    def test_opencode_membership_is_the_recent_top_level_window_only(self):
        self.set_oc(sessions=[
            oc_session(OC_A, "recent"), oc_session(OC_B, "child", parent=OC_A),
            oc_session("ses_oldoldoldold", "old", updated_ms=1000)])
        members = [m["member"] for m in self.general_members()["members"]]
        self.assertIn("oc:" + OC_A, members)
        self.assertNotIn("oc:" + OC_B, members)
        self.assertNotIn("oc:ses_oldoldoldold", members)

    def test_join_and_leave_general_move_the_marker_only(self):
        self.assertIn(self.me, [m["member"] for m in self.general_members()["members"]])
        self.assertEqual(self.ok("leave", "general").strip(), "left #general")
        self.assertNotIn(self.me, [m["member"] for m in self.general_members()["members"]])
        marker = list(pathlib.Path(sp_channels.left_dir()).glob("*.json"))
        self.assertEqual([p.name for p in marker],
                         [hashlib.sha256(self.me.encode()).hexdigest() + ".json"])
        self.assertEqual(stat.S_IMODE(marker[0].stat().st_mode), 0o600)
        self.assertEqual(self.ok("leave", "general").strip(), "left #general")
        self.assertEqual(self.ok("join", "general").strip(), "joined #general")
        self.assertIn(self.me, [m["member"] for m in self.general_members()["members"]])
        self.assertEqual(self.ok("join", "general").strip(), "already in #general")
        self.assertEqual(list(pathlib.Path(sp_channels.left_dir()).glob("*.json")), [])

    def test_a_left_session_is_not_addressed_by_at_all(self):
        self.as_codex(self.tid)
        self.ok("leave", "general")
        self.as_sender()
        rc, result, err = self.post()
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(result["outcomes"]), ["oc:" + OC_B])
        self.assertEqual(self.queue_calls(), [])

    def test_a_marker_is_kept_for_a_live_session_and_removed_once_it_is_verified_gone(self):
        dead_sid = new_uuid()
        self.write_record(2 ** 22 + 12345, "ghost", dead_sid, str(self.socks / "x.sock"))
        sp_channels.set_left(self.me)
        sp_channels.set_left("cc:" + dead_sid)
        # A session missing from a registry read that failed is not proven gone.
        (self.sessions / "broken.json").write_text("{not json")
        rc, out, err = self.cli("gc")
        self.assertEqual(rc, 0, err)
        names = sorted(p.name for p in pathlib.Path(sp_channels.left_dir()).glob("*.json"))
        self.assertEqual(len(names), 2, out)
        (self.sessions / "broken.json").unlink()
        rc, out, err = self.cli("gc", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("would prune leave marker", out)
        self.assertEqual(len(list(pathlib.Path(sp_channels.left_dir()).glob("*.json"))), 2)
        rc, out, err = self.cli("gc")
        self.assertEqual(rc, 0, err)
        self.assertIn("pruned leave marker", out)
        left = [p.name for p in pathlib.Path(sp_channels.left_dir()).glob("*.json")]
        self.assertEqual(left, [hashlib.sha256(self.me.encode()).hexdigest() + ".json"])
        # Age alone never removes a marker.
        old = pathlib.Path(sp_channels.left_dir()) / left[0]
        os.utime(str(old), (1, 1))
        self.assertEqual(self.cli("gc", "--days", "0")[0], 0)
        self.assertTrue(old.exists())
