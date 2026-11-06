"""Buddy channels: `buddy set` and `buddy clear` keep the lead's channel in step with the binding."""

import contextlib
import hashlib

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import ChannelSendBase
from session_peers import channels as sp_channels, journal as sp_journal
from session_peers import lifecycle as sp_lifecycle, storage as sp_storage


class BuddyChannelBase(ChannelSendBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(sp_lifecycle, "attach_thread", lambda thread_id, verbose=True: None)
        patch.start()
        self.addCleanup(patch.stop)
        self.sender_cc("lead")
        self.lead = self.me
        self.x, self.y = ["codex:" + t for t in self.threads("codex-x", "codex-y")]
        self.key = "buddy-" + hashlib.sha256(self.lead.encode()).hexdigest()[:12]

    def threads(self, *names):
        tids, rows = [], []
        for name in names:
            tid = new_uuid()
            rollout = self.make_rollout("%s.jsonl" % tid)
            rows.append({"id": tid, "name": name, "rollout_path": str(rollout)})
            self.set_holder(rollout)
            tids.append(tid)
        self.make_state_db(rows)
        return tids

    def bset(self, typed, *extra):
        return self.cli("buddy", "set", typed, *extra)

    def members(self, key=None):
        return sp_channels.read_channel(key or self.key)["members"]

    def store(self):
        root = pathlib.Path(sp_channels.channels_root())
        if not root.exists():
            return {}
        return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.suffix == ".json"}

    def kinds_of(self, key=None):
        return [e["kind"] for e in self.history(key or self.key)]


class BuddyChannelTests(BuddyChannelBase):
    def test_example_5_two_buddies_share_one_channel_and_clear_removes_the_current_one(self):
        rc, _out, err = self.bset(self.x)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.bset(self.y)
        self.assertEqual(rc, 0, err)
        rows = json.loads(self.ok("list", "--json"))
        self.assertEqual(rows, [{
            "name": self.key, "display": "buddy · lead", "kind": "buddy",
            "owner": self.lead, "members": 3, "you": "owner",
        }])
        self.assertEqual(self.members(), [self.lead, self.x, self.y])

        self.ok("remove", self.key, self.x)
        self.assertEqual(self.members(), [self.lead, self.y])
        # The Codex buddy that is still bound keeps its place until clear.
        rc, out, err = self.cli("buddy", "clear")
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy cleared", out)
        self.assertEqual(self.members(), [self.lead])
        removed = [e["data"] for e in self.history(self.key) if e["kind"] == "channel.removed"]
        self.assertEqual(removed, [{"member": self.x}, {"member": self.y}])
        self.assertFalse(self.record_path_of(self.lead).exists())

    def record_path_of(self, owner):
        kind, _sep, ident = owner.partition(":")
        return pathlib.Path(sp_storage.buddies_dir()) / ("%s-%s.json" % (kind, ident))

    def test_rebinding_the_same_buddy_is_a_no_op_for_the_channel(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        before = self.history(self.key)
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.members(), [self.lead, self.x])
        self.assertEqual(self.history(self.key), before)
        self.assertEqual(self.kinds_of(), ["channel.created", "channel.joined"])

    def test_clear_with_a_buddy_already_removed_from_the_channel_changes_nothing(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.ok("remove", self.key, self.x)
        count = len(self.history(self.key))
        rc, _out, err = self.cli("buddy", "clear")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.history(self.key)), count)

    def test_clear_with_no_buddy_creates_no_channel(self):
        rc, out, _err = self.cli("buddy", "clear")
        self.assertEqual(rc, 0)
        self.assertIn("no buddy was set", out)
        self.assertEqual(self.store(), {})

    def test_join_stays_refused_on_a_buddy_channel(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.as_cc()
        rc, _out, err = self.ch("join", self.key)
        self.assertNotEqual(rc, 0)
        self.assertIn("buddy channels are joined through buddy set", err)

    def test_only_the_owner_can_remove_from_a_buddy_channel(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.as_cc()
        rc, _out, err = self.ch("remove", self.key, self.x)
        self.assertEqual(rc, 2)
        self.assertIn("only the owner", err)
        self.assertIn(self.x, self.members())


class BuddyChannelGuardTests(BuddyChannelBase):
    def test_set_for_another_session_warns_and_leaves_the_channel_store_untouched(self):
        other = "cc:" + new_uuid()
        before = self.store()
        rc, out, err = self.bset(self.x, "--as", other)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: buddy channel not updated", err)
        self.assertEqual(self.store(), before)
        self.assertTrue(self.record_path_of(other).exists())

    def test_clear_for_another_session_warns_and_leaves_the_channel_untouched(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        other = "cc:" + new_uuid()
        self.assertEqual(self.bset(self.y, "--as", other)[0], 0)
        before = self.store()
        rc, out, err = self.cli("buddy", "clear", "--as", other)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: buddy channel not updated", err)
        self.assertEqual(self.store(), before)
        self.assertIn("buddy cleared", out)

    def test_an_unidentifiable_caller_warns_and_the_binding_still_happens(self):
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        rc, _out, err = self.bset(self.x, "--as", self.lead)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: buddy channel not updated", err)
        self.assertEqual(self.store(), {})
        self.assertTrue(self.record_path_of(self.lead).exists())

    def record_path_of(self, owner):
        kind, _sep, ident = owner.partition(":")
        return pathlib.Path(sp_storage.buddies_dir()) / ("%s-%s.json" % (kind, ident))


class BuddyChannelFailureTests(BuddyChannelBase):
    def test_a_create_failure_warns_and_the_binding_is_written(self):
        with mock.patch.object(sp_channels, "create_channel", side_effect=RuntimeError("disk full")):
            rc, out, err = self.bset(self.x)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: buddy channel not updated: disk full", err)
        self.assertEqual(self.store(), {})
        kind, _sep, ident = self.lead.partition(":")
        self.assertTrue((pathlib.Path(sp_storage.buddies_dir()) / ("%s-%s.json" % (kind, ident))).exists())

    def test_a_membership_failure_on_clear_warns_and_the_binding_is_cleared(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        with mock.patch.object(sp_channels, "change_membership",
                               side_effect=sp_channels.ChannelError("locked", 1)):
            rc, out, err = self.cli("buddy", "clear")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: buddy channel not updated: locked", err)
        self.assertIn("buddy cleared", out)
        self.assertIn(self.x, self.members())

    def test_the_failure_reason_is_escaped(self):
        with mock.patch.object(sp_channels, "create_channel", side_effect=RuntimeError("bad\x1b[31m\nline")):
            rc, _out, err = self.bset(self.x)
        self.assertEqual(rc, 0)
        self.assertNotIn("\x1b", err)
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("warning: buddy channel")]), 1)


class BuddyChannelLockOrderTests(BuddyChannelBase):
    def test_no_channel_lock_is_taken_while_a_buddy_lock_is_held(self):
        held = []
        violations = []
        real_owner, real_bind, real_flock = sp_storage.owner_lock, sp_storage._binding_locks, sp_channels._flock

        def tracking(real, label):
            @contextlib.contextmanager
            def wrapper(*args, **kwargs):
                held.append(label)
                try:
                    with real(*args, **kwargs) as value:
                        yield value
                finally:
                    held.remove(label)
            return wrapper

        @contextlib.contextmanager
        def flock(path):
            if held:
                violations.append((list(held), path))
            with real_flock(path):
                yield

        with mock.patch.object(sp_storage, "owner_lock", tracking(real_owner, "owner_lock")), \
                mock.patch.object(sp_storage, "_binding_locks", tracking(real_bind, "binding_locks")), \
                mock.patch.object(sp_channels, "_flock", flock):
            self.assertEqual(self.bset(self.x)[0], 0)
            self.assertEqual(self.bset(self.y)[0], 0)
            self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertEqual(violations, [])
        self.assertEqual(self.kinds_of(), ["channel.created", "channel.joined", "channel.joined", "channel.removed"])


class BuddyChannelDisplayTests(BuddyChannelBase):
    @contextlib.contextmanager
    def named_owner(self, name):
        """The channel code sees the lead under ``name``; every other lookup is real."""
        real = sp_channels.sp_identity.resolve_typed

        def resolve(target, *args, **kwargs):
            if target == self.lead:
                return {"kind": "cc", "uuid": self.lead.partition(":")[2], "name": name}
            return real(target, *args, **kwargs)

        with mock.patch.object(sp_channels.sp_identity, "resolve_typed", resolve):
            yield

    def test_a_hostile_owner_name_is_escaped_in_the_display_and_the_listing(self):
        with self.named_owner("evil\x1b]0;pwned\x07\x1b[31m\nname"):
            self.assertEqual(self.bset(self.x)[0], 0)
        display = sp_channels.read_channel(self.key)["display"]
        self.assertTrue(display.startswith("buddy · evil"))
        for text in (display, self.ok("list"), self.ok("list", "--json"), self.ok("members", self.key)):
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
        self.assertNotIn("\n", display)

    def test_an_owner_without_a_name_is_shown_by_its_typed_id(self):
        with self.named_owner(None):
            self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(sp_channels.read_channel(self.key)["display"], "buddy · " + self.lead)


class BuddyChannelHandoverTests(BuddyChannelBase):
    def setUp(self):
        super().setUp()
        _peer, self.successor = self.peer_cc("successor")
        self.successor_sid = self.successor.partition(":")[2]

    def hand_over(self):
        rc, _out, err = self.cli("inbox", "handover", "--to", self.successor)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("handover", "--to", self.successor)
        self.assertEqual(rc, 0, err)

    def test_example_4_tail_the_successor_owns_and_can_manage_the_buddy_channel(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.hand_over()
        rec = sp_channels.read_channel(self.key)
        self.assertEqual(rec["owner"], self.successor)
        self.assertIn(self.successor, rec["members"])
        self.assertNotIn(self.lead, rec["members"])
        self.assertEqual(rec["display"], "buddy · successor")
        self.as_cc(self.successor_sid)
        self.ok("remove", self.key, self.x)
        self.assertNotIn(self.x, self.members())

    def test_the_new_lead_binds_into_the_handed_over_channel_not_a_new_one(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.hand_over()
        self.as_cc(self.successor_sid)
        self.assertEqual(self.bset(self.y, "--as", self.successor)[0], 0)
        keys = [r["name"] for r in json.loads(self.ok("list", "--json"))]
        self.assertEqual(keys, [self.key])
        self.assertEqual(self.members(), [self.x, self.successor, self.y])

    def test_the_most_recent_handover_wins_among_several_handed_over_channels(self):
        older = "buddy-" + "aaaaaaaaaaaa"
        newer = "buddy-" + "bbbbbbbbbbbb"
        for key in (older, newer):
            sp_channels.create_channel(key, "cc:" + new_uuid(), [], kind="buddy", display="buddy", reserved_ok=True)
        ticks = iter(range(0, 50))
        base = time.time()
        # Recent stamps, one second apart, so history retention keeps the entries.
        stamp = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base + next(ticks)))  # noqa: E731

        def hand_to_lead(rec):
            rec["owner"] = self.lead
            rec["members"].append(self.lead)
            return "channel.handover", {"member": "cc:x", "to": self.lead}

        # Hand each to the lead; the later handover is the one buddy set reuses.
        with mock.patch.object(sp_runtime, "now_iso", stamp):
            for key in (newer, older):
                sp_channels.change_membership(key, hand_to_lead, self.lead)
        self.assertEqual(sp_channels.find_buddy_channel(self.lead), older)
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertIn(self.x, self.members(older))
        self.assertNotIn(self.x, self.members(newer))

    def test_a_channel_created_for_a_lead_that_never_got_a_handover_is_not_reused_by_another(self):
        sp_channels.create_channel(
            "buddy-cccccccccccc", "cc:" + new_uuid(), [], kind="buddy", display="buddy", reserved_ok=True)
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(sorted(r["name"] for r in json.loads(self.ok("list", "--json"))),
                         sorted([self.key, "buddy-cccccccccccc"]))
