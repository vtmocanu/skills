"""`peers.py leave` and `peers.py handover`, with the grant receipt that outlives a journal prune."""

import hashlib

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import ChannelSendBase
from peer_tests.test_opencode import OC_B
from session_peers import channels as sp_channels, journal as sp_journal, reply_wake as sp_reply_wake
from session_peers import storage as sp_storage

BUDDY_CHANNEL = "buddy-" + "0123456789ab"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class LifecycleBase(ChannelSendBase):
    def setUp(self):
        super().setUp()
        self.wall = [1_000_000.0]
        patch = mock.patch.object(sp_channels, "_wall", lambda: self.wall[0])
        patch.start()
        self.addCleanup(patch.stop)
        self.sender_cc("lead")
        self.lead = self.me
        self.tid, _rollout = self.one_thread()
        self.x = "codex:" + self.tid

    def as_lead(self):
        return self.as_cc(self.lead.partition(":")[2])

    def members(self, name):
        return sp_channels.read_channel(name)["members"]

    def kinds_of(self, name):
        return [e["kind"] for e in self.history(name)]

    def entries_of(self, name, kind):
        return [e for e in self.history(name) if e["kind"] == kind]


class LeaveTests(LifecycleBase):
    def test_example_3_leave_records_left_and_drops_the_leaver_from_sends_and_general(self):
        self.channel("ops", self.x, "oc:" + OC_B)
        self.channel("second", self.x)
        self.channel("elsewhere", owner="cc:" + new_uuid())
        self.assertIn(self.lead, [m["member"] for m in json.loads(self.ok("members", "general", "--json"))["members"]])
        rc, out, err = self.cli("leave")
        self.assertEqual(rc, 0, err)
        self.assertIn("left #ops", out)
        self.assertIn("left #second", out)
        for name in ("ops", "second"):
            self.assertNotIn(self.lead, self.members(name))
            left = self.entries_of(name, "channel.left")
            self.assertEqual([e["data"] for e in left], [{"member": self.lead}])
            self.assertEqual(sp_channels.read_channel(name)["owner"], self.lead)
            self.assertIn("%s left" % self.lead, self.ok("tail", name))
        self.assertEqual(self.entries_of("elsewhere", "channel.left"), [])
        general = [m["member"] for m in json.loads(self.ok("members", "general", "--json"))["members"]]
        self.assertNotIn(self.lead, general)

        # The next send to the channel does not address L.
        self.as_codex(self.tid)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(list(result["outcomes"]), ["oc:" + OC_B])

        # Idempotent, and `join general` brings the session back.
        self.as_lead()
        before = len(self.history("ops"))
        rc, _out, err = self.cli("leave")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.history("ops")), before)
        self.ok("join", "general")
        general = [m["member"] for m in json.loads(self.ok("members", "general", "--json"))["members"]]
        self.assertIn(self.lead, general)

    def test_each_channel_is_handled_independently_and_a_failure_exits_one(self):
        self.channel("aaa", self.x)
        self.channel("bbb", self.x)
        real = sp_channels.change_membership

        def flaky(key, mutate, actor):
            if key == "aaa":
                raise sp_channels.ChannelError("disk full", 1)
            return real(key, mutate, actor)

        with mock.patch.object(sp_channels, "change_membership", flaky):
            rc, out, err = self.cli("leave")
        self.assertEqual(rc, 1)
        self.assertIn("could not leave #aaa", err)
        self.assertIn(self.lead, self.members("aaa"))
        self.assertNotIn(self.lead, self.members("bbb"))
        self.assertEqual(len(list(pathlib.Path(sp_channels.left_dir()).glob("*.json"))), 1)
        rc, _out, err = self.cli("leave")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(self.lead, self.members("aaa"))

    def corrupt(self, name, mode="json"):
        path = sp_channels._record_path(name)
        if mode == "json":
            pathlib.Path(path).write_text("{not json")
            return
        rec = json.loads(pathlib.Path(path).read_text())
        rec["owner"] = 5
        pathlib.Path(path).write_text(json.dumps(rec))

    def test_an_unreadable_record_fails_the_leave_but_the_rest_is_left(self):
        self.channel("aaa", self.x)
        self.channel("bbb", self.x)
        self.channel("ccc", self.x)
        self.corrupt("aaa")
        self.corrupt("ccc", "owner")
        rc, out, err = self.cli("leave")
        self.assertEqual(rc, 1)
        self.assertIn("could not read #aaa", err)
        self.assertIn("could not read #ccc", err)
        self.assertIn("left #bbb", out)
        self.assertNotIn(self.lead, self.members("bbb"))
        # The #general leave marker is still written.
        self.assertEqual(len(list(pathlib.Path(sp_channels.left_dir()).glob("*.json"))), 1)

    def test_a_store_that_cannot_be_listed_fails_the_leave(self):
        self.channel("aaa", self.x)
        real = os.listdir

        def deny(path="."):
            if path == sp_channels.channels_root():
                raise PermissionError(13, "denied")
            return real(path)

        with mock.patch.object(os, "listdir", deny):
            rc, _out, err = self.cli("leave")
        self.assertEqual(rc, 1)
        self.assertIn("cannot be listed", err)
        self.assertIn(self.lead, self.members("aaa"))
        self.assertEqual(len(list(pathlib.Path(sp_channels.left_dir()).glob("*.json"))), 1)

    def test_leave_needs_an_identifiable_caller(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        rc, _out, err = self.cli("leave")
        self.assertEqual(rc, 2)
        self.assertIn("cannot tell which session is asking", err)

    def test_a_crash_between_membership_and_history_leaves_the_member_out(self):
        self.channel("ops", self.x)

        def hook(name):
            if name == "after_membership_before_history":
                raise RuntimeError("killed at " + name)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            with self.assertRaisesRegex(RuntimeError, "killed at"):
                self.cli("leave")
        self.assertNotIn(self.lead, self.members("ops"))
        self.assertEqual(self.entries_of("ops", "channel.left"), [])


class HandoverTests(LifecycleBase):
    def setUp(self):
        super().setUp()
        self.peer, self.successor = self.peer_cc("successor")
        self.successor_sid = self.successor.partition(":")[2]
        self.channel("ops", self.x)
        sp_channels.create_channel(
            BUDDY_CHANNEL, self.lead, [self.x], kind="buddy", display="buddy · lead", reserved_ok=True)

    def grant(self):
        rc, out, err = self.cli("inbox", "handover", "--to", self.successor)
        self.assertEqual(rc, 0, err)

    def handover(self, *extra):
        return self.cli("handover", "--to", self.successor, *extra)

    def test_example_4_the_successor_becomes_member_and_owner_after_both_commands(self):
        self.grant()
        rc, out, err = self.handover()
        self.assertEqual(rc, 0, err)
        for name in ("ops", BUDDY_CHANNEL):
            rec = sp_channels.read_channel(name)
            self.assertNotIn(self.lead, rec["members"])
            self.assertIn(self.successor, rec["members"])
            self.assertIn(self.x, rec["members"])
            self.assertEqual(rec["owner"], self.successor)
            handed = self.entries_of(name, "channel.handover")
            self.assertEqual([e["data"] for e in handed], [{"member": self.lead, "to": self.successor}])
            self.assertIn("%s handed over to %s" % (self.lead, self.successor), self.ok("tail", name))
        self.assertEqual(sp_channels.read_channel(BUDDY_CHANNEL)["display"], "buddy · successor")
        self.assertEqual(sp_channels.read_channel("ops")["display"], "ops")
        # M can manage the handed-over channels.
        self.as_cc(self.successor_sid)
        self.ok("remove", BUDDY_CHANNEL, self.x)
        self.assertNotIn(self.x, self.members(BUDDY_CHANNEL))
        # The former owner no longer can.
        self.as_lead()
        rc, _out, err = self.ch("remove", "ops", self.x)
        self.assertEqual(rc, 2)
        # Running it again moves nothing.
        count = len(self.history("ops"))
        rc, out, err = self.handover()
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.history("ops")), count)

    def test_an_unreadable_record_fails_the_handover_but_the_rest_moves(self):
        self.channel("zzz", self.x)
        pathlib.Path(sp_channels._record_path("zzz")).write_text("{not json")
        self.grant()
        rc, out, err = self.handover()
        self.assertEqual(rc, 1)
        self.assertIn("could not read #zzz", err)
        for name in ("ops", BUDDY_CHANNEL):
            self.assertEqual(sp_channels.read_channel(name)["owner"], self.successor)

    def test_a_store_that_cannot_be_listed_fails_the_handover(self):
        self.grant()
        real = os.listdir

        def deny(path="."):
            if path == sp_channels.channels_root():
                raise PermissionError(13, "denied")
            return real(path)

        with mock.patch.object(os, "listdir", deny):
            rc, out, err = self.handover()
        self.assertEqual(rc, 1)
        self.assertIn("cannot be listed", err)
        self.assertNotIn("no stored channels", out)
        self.assertEqual(sp_channels.read_channel("ops")["owner"], self.lead)

    def test_handover_stamps_the_record_with_when_ownership_moved(self):
        self.grant()
        self.assertNotIn("handed_over_at", sp_channels.read_channel(BUDDY_CHANNEL))
        self.assertEqual(self.handover()[0], 0)
        self.assertRegex(sp_channels.read_channel(BUDDY_CHANNEL)["handed_over_at"], r"^\d{4}-\d\d-\d\dT")

    def test_a_member_who_is_not_the_owner_keeps_the_channel_display(self):
        owner = "cc:" + new_uuid()
        sp_channels.create_channel(
            "buddy-aaaaaaaaaaaa", owner, [self.lead], kind="buddy", display="buddy · boss", reserved_ok=True)
        self.grant()
        self.assertEqual(self.handover()[0], 0)
        rec = sp_channels.read_channel("buddy-aaaaaaaaaaaa")
        self.assertEqual((rec["owner"], rec["display"]), (owner, "buddy · boss"))
        self.assertEqual(rec["members"], [owner, self.successor])

    def test_a_successor_without_a_name_is_shown_by_its_typed_id_and_escaped(self):
        self.grant()
        with mock.patch.object(sp_channels.sp_identity, "resolve_typed", return_value={"kind": "cc", "uuid": "x", "name": None}):
            self.assertEqual(self.handover()[0], 0)
        self.assertEqual(sp_channels.read_channel(BUDDY_CHANNEL)["display"], "buddy · " + self.successor)

    def test_the_display_name_is_escaped(self):
        self.grant()
        with mock.patch.object(sp_channels.sp_identity, "resolve_typed",
                               return_value={"kind": "cc", "uuid": "x", "name": "evil\x1b[31m\nname"}):
            self.assertEqual(self.handover()[0], 0)
        display = sp_channels.read_channel(BUDDY_CHANNEL)["display"]
        self.assertNotIn("\x1b", display)
        self.assertNotIn("\n", display)

    def test_without_a_grant_or_receipt_it_refuses_and_names_the_command(self):
        rc, _out, err = self.handover()
        self.assertEqual(rc, 2)
        self.assertIn("peers.py inbox handover --to %s" % self.successor, err)
        self.assertIn(self.lead, self.members("ops"))
        self.assertEqual(self.entries_of("ops", "channel.handover"), [])
        # It never creates the grant itself.
        self.assertEqual(sp_journal.read_only_view()["handovers"], [])
        self.assertFalse(pathlib.Path(sp_channels.receipts_dir()).exists())

    def test_a_grant_to_someone_else_does_not_authorise_this_successor(self):
        other_listener, other = self.peer_cc("other")
        rc, _out, err = self.cli("inbox", "handover", "--to", other)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.handover()
        self.assertEqual(rc, 2)
        self.assertIn("peers.py inbox handover --to", err)

    def test_a_name_and_self_are_refused(self):
        for target in ("successor", "cc:successor", self.lead):
            rc, _out, err = self.cli("handover", "--to", target)
            self.assertEqual(rc, 2, target)
        self.assertIn(self.lead, self.members("ops"))

    def test_the_receipt_is_written_with_the_grant_and_survives_a_pruning_ack(self):
        self.grant()
        path = pathlib.Path(sp_channels.receipts_dir()) / ("%s-%s.json" % (digest(self.lead), digest(self.successor)))
        receipt = json.loads(path.read_text())
        self.assertEqual(
            (receipt["from"], receipt["to"], receipt["at"]), (self.lead, self.successor, self.wall[0]))
        self.assertIn("through_seq", receipt)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(sp_journal.read_only_view()["handovers"]), 1)
        # A message recorded for X and acked by X prunes the grant of L, who is no recipient.
        mid = sp_journal.record_message("cc:" + new_uuid(), [self.x], "hold", message_id=new_uuid())
        self.as_codex(self.tid)
        self.assertEqual(self.cli("ack", mid)[0], 0)
        self.assertEqual(sp_journal.read_only_view()["handovers"], [])
        self.as_lead()
        rc, _out, err = self.handover()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sp_channels.read_channel("ops")["owner"], self.successor)

    def test_a_receipt_older_than_fourteen_days_no_longer_authorises(self):
        self.grant()
        self.wall[0] += 14 * 86400 - 60
        with mock.patch.object(sp_journal, "read_only_view", side_effect=sp_journal.JournalUnavailable("no")):
            self.assertEqual(self.handover()[0], 0)
        self.channel("later", self.x)
        self.wall[0] += 120
        with mock.patch.object(sp_journal, "read_only_view", return_value={"handovers": []}):
            rc, _out, err = self.handover()
        self.assertEqual(rc, 2)
        self.assertIn("peers.py inbox handover --to", err)

    def test_a_receipt_write_failure_warns_and_the_grant_still_succeeds(self):
        with mock.patch.object(sp_channels, "write_grant_receipt", side_effect=OSError("disk full")):
            rc, out, err = self.cli("inbox", "handover", "--to", self.successor)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning", err)
        self.assertIn("receipt", err)
        self.assertIn("handover recorded", out)
        self.assertEqual(len(sp_journal.read_only_view()["handovers"]), 1)

    def test_a_crash_between_membership_and_history_still_moved_the_membership(self):
        self.grant()

        def hook(name):
            if name == "after_membership_before_history":
                raise RuntimeError("killed at " + name)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            with self.assertRaisesRegex(RuntimeError, "killed at"):
                self.handover()
        # Channels run in key order: the crash hit the first one, after its membership write.
        rec = sp_channels.read_channel(BUDDY_CHANNEL)
        self.assertEqual((rec["owner"], self.lead in rec["members"], self.successor in rec["members"]),
                         (self.successor, False, True))
        self.assertEqual(self.entries_of(BUDDY_CHANNEL, "channel.handover"), [])

    def test_handover_moves_no_budget_buddy_or_reply_allowance(self):
        owner = {"kind": "cc", "uuid": self.lead.partition(":")[2]}
        seeded = {
            sp_storage.buddy_path(owner): b'{"buddy": {"kind": "codex", "uuid": "%s"}}\n' % self.tid.encode(),
            sp_storage.budget_allow_path(self.tid): b'{"allow": 3, "for": "someone"}\n',
            sp_storage.budget_binding_path(self.tid): b'{"binding": 1}\n',
            sp_reply_wake.path_for(new_uuid()): b'{"grant": 1}\n',
        }
        for path, data in seeded.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        self.grant()

        def snapshot():
            out = {}
            for base in (sp_storage.state_dir(), sp_storage.request_dir()):
                for root, _dirs, files in os.walk(base):
                    for name in files:
                        path = os.path.join(root, name)
                        with open(path, "rb") as fh:
                            out[path] = (fh.read(), os.stat(path).st_mtime_ns)
            return out

        before = snapshot()
        self.assertTrue(set(seeded) <= set(before))
        self.assertEqual(self.handover()[0], 0)
        self.assertEqual(snapshot(), before)

    def test_the_live_journal_grant_authorises_without_a_receipt_and_neither_means_refused(self):
        self.grant()
        shutil.rmtree(sp_channels.receipts_dir())
        with mock.patch.object(sp_journal, "read_only_view", return_value={"handovers": []}):
            rc, _out, err = self.handover()
        self.assertEqual(rc, 2)
        self.assertIn(self.lead, self.members("ops"))
        rc, _out, err = self.handover()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sp_channels.read_channel("ops")["owner"], self.successor)

    def test_receipt_age_tolerates_a_clock_step_back_of_five_minutes_only(self):
        self.grant()
        self.wall[0] -= 300.0
        self.assertTrue(sp_channels.has_receipt(self.lead, self.successor))
        self.wall[0] -= 1.0
        self.assertFalse(sp_channels.has_receipt(self.lead, self.successor))

    def test_a_channel_the_caller_owns_but_left_moves_to_the_successor(self):
        self.assertEqual(self.cli("leave")[0], 0)
        self.assertNotIn(self.lead, self.members("ops"))
        self.assertEqual(sp_channels.read_channel("ops")["owner"], self.lead)
        self.grant()
        rc, out, err = self.handover()
        self.assertEqual(rc, 0, err)
        rec = sp_channels.read_channel("ops")
        self.assertEqual(rec["owner"], self.successor)
        self.assertIn(self.successor, rec["members"])
        self.assertNotIn(self.lead, rec["members"])
        self.assertEqual([e["data"] for e in self.entries_of("ops", "channel.handover")],
                         [{"member": self.lead, "to": self.successor}])

    def test_an_owned_channel_at_the_member_cap_is_not_handed_over_past_it(self):
        self.assertEqual(self.cli("leave")[0], 0)
        full = ["cc:" + new_uuid() for _ in range(sp_constants.CHANNEL_MAX_MEMBERS)]
        path = sp_channels._record_path("ops")
        rec = json.loads(pathlib.Path(path).read_text())
        rec["members"] = full
        pathlib.Path(path).write_text(json.dumps(rec))
        self.grant()
        rc, _out, err = self.handover()
        self.assertEqual(rc, 1)
        self.assertIn("CHANNEL_MAX_MEMBERS", err)
        self.assertEqual(sp_channels.read_channel("ops")["owner"], self.lead)
        self.assertEqual(self.members("ops"), full)

    def test_gc_ignores_and_never_prints_files_that_are_not_receipts(self):
        self.grant()
        grants = pathlib.Path(sp_channels.receipts_dir())
        planted = grants / "evil\x1b[31m\nname.json"
        planted.write_text(json.dumps({"at": 1.0}))
        self.wall[0] += 14 * 86400 + 1
        rc, out, err = self.cli("gc")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("evil", out)
        self.assertTrue(planted.exists())
        self.assertIn("pruned expired handover receipt", out)
        self.assertEqual([p.name for p in grants.glob("*.json")], [planted.name])

    def test_gc_removes_only_expired_receipts(self):
        self.grant()
        self.assertEqual(self.cli("gc")[0], 0)
        self.assertEqual(len(list(pathlib.Path(sp_channels.receipts_dir()).glob("*.json"))), 1)
        self.wall[0] += 14 * 86400 + 1
        rc, out, err = self.cli("gc", "--dry-run")
        self.assertIn("would prune expired handover receipt", out)
        self.assertEqual(len(list(pathlib.Path(sp_channels.receipts_dir()).glob("*.json"))), 1)
        rc, out, err = self.cli("gc")
        self.assertIn("pruned expired handover receipt", out)
        self.assertEqual(list(pathlib.Path(sp_channels.receipts_dir()).glob("*.json")), [])
