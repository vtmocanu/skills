"""Quiet channels resolve notification intent before any durable send write."""

from peer_tests.test_channel_send import ChannelSendBase
from peer_tests.support import *  # noqa: F401,F403
from session_peers import channels as channels


class QuietChannelTests(ChannelSendBase):
    def channel(self, name, *members, owner=None):
        return channels.create_channel(name, owner or self.me, list(members))

    def test_old_and_new_default_quiet_buddy_always_all(self):
        rec = self.channel("ops")
        self.assertEqual(channels.delivery_mode(rec), "mentions")
        def legacy(row):
            row.pop("deliver")
            return None, None
        channels.change_membership("ops", legacy, self.me)
        self.assertEqual(channels.delivery_mode(channels.read_channel("ops")), "mentions")
        self.assertNotIn("deliver", channels.read_channel("ops"), "reading old channels does not rewrite them")
        rec.pop("deliver")
        self.assertEqual(channels.delivery_mode(rec), "mentions")
        rec.update(kind="buddy", deliver="mentions")
        self.assertEqual(channels.delivery_mode(rec), "all")

    def test_buddy_mode_is_fixed_and_join_only_warns(self):
        other = "cc:" + new_uuid()
        rec = channels.create_channel("buddy-test", other, [self.me], kind="buddy", reserved_ok=True)
        self.assertEqual(rec["deliver"], "all")
        def bind(row):
            row["current_buddy"] = self.me
            return None, None
        channels.change_membership("buddy-test", bind, other)
        self.channel("ops", owner=other)
        result = self.ok("join", "ops")
        self.assertIn("buddies normally stay out", result)
        self.assertIn(self.me, channels.read_channel("ops")["members"])
        self.as_cc(other.partition(":")[2])
        rc, _out, err = self.ch("set", "buddy-test", "--deliver", "all")
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot be changed", err)

    def test_plain_post_records_without_notifications_or_journal(self):
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        rc, result, err = self.send()
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][peer], "recorded (not mentioned)")
        self.assertEqual(self.send_entries()[0]["data"]["delivery"], "none")
        self.assertEqual(self.journal_entries(), [])
        self.nothing_delivered()

    def test_explicit_name_delivers_only_to_canonical_member(self):
        self.sender_cc()
        self.one_thread()  # make every resolver's discovery source readable
        listener, peer = self.peer_cc("beta")
        _other, ignored = self.peer_cc("gamma")
        self.channel("ops", peer, ignored)
        rc, result, err = self.send(message="@beta please review")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][peer], channels.OUTCOME_ACCEPTED)
        self.assertEqual(result["outcomes"][ignored], "recorded (not mentioned)")
        self.assertEqual(self.send_entries()[0]["data"]["mentioned"], [peer])
        self.assertEqual(len(listener.of_type("user")), 1)

    def test_unknown_and_nonmember_refuse_before_send_writes(self):
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops")
        for token in ("nobody", peer):
            rc, _result, _err = self.send(message="@" + token)
            self.assertNotEqual(rc, 0)
            self.assertEqual(self.send_entries(), [])
            self.assertEqual(self.journal_entries(), [])
        self.nothing_delivered()

    def test_ambiguous_name_refuses_before_any_send_or_journal(self):
        self.sender_cc()
        self.one_thread()
        _a, first = self.peer_cc("duplicate")
        _b, second = self.peer_cc("duplicate")
        self.channel("ops", first, second)
        rc, _result, err = self.send(message="@duplicate review")
        self.assertNotEqual(rc, 0)
        self.assertIn("resolve", err)
        self.assertEqual(self.send_entries(), [])
        self.assertEqual(self.journal_entries(), [])
        self.nothing_delivered()

    def test_middle_text_all_uses_shared_allowance_across_stored_channels(self):
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        self.channel("second", peer)
        rc, result, err = self.send(message="please @all review")
        self.assertEqual(rc, 0, err)
        self.assertTrue(result["journal"]["recorded"])
        rc, _result, err = self.send(name="second", message="please @all review again")
        self.assertEqual(rc, 2)
        self.assertIn("60", err)
        self.assertEqual(self.send_entries("second"), [])

    def test_unread_is_not_a_delivery_ack(self):
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        rc, _result, err = self.send(message="@all review")
        self.assertEqual(rc, 0, err)
        before = self.journal_entries()
        self.as_cc(peer.partition(":")[2])
        self.tail_json("ops", "--unread")
        self.assertEqual(self.journal_entries(), before)

    def test_here_requires_positive_live_evidence(self):
        self.sender_cc()
        _a, live = self.peer_cc("beta")
        _b, unknown = self.peer_cc("gamma")
        self.channel("ops", live, unknown)
        with mock.patch.object(channels, "member_liveness", side_effect=lambda m, c: "live" if m == live else "unverified"):
            rc, result, err = self.send(message="check @here")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][unknown], "recorded (@here requires live evidence)")
        self.assertEqual(result["outcomes"][live], channels.OUTCOME_ACCEPTED)

    def test_mode_change_requires_owner_and_cannot_change_buddy(self):
        self.channel("ops")
        self.ok("set", "ops", "--deliver", "all")
        self.assertEqual(channels.delivery_mode(channels.read_channel("ops")), "all")
        self.as_cc()
        rc, _out, _err = self.ch("set", "ops", "--deliver", "mentions")
        self.assertNotEqual(rc, 0)

    def test_unread_pages_advance_only_the_reader_and_leave_json_shape_unchanged(self):
        from session_peers import channel_reads
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        self.send(message="first")
        self.send(message="second")
        self.as_cc(peer.partition(":")[2])
        before = channel_reads.cursor("ops", peer)
        result = self.tail_json("ops", "--unread", "--limit", "1")
        self.assertEqual(before, 0)
        self.assertEqual([e["seq"] for e in result["entries"]], [1])
        self.assertEqual(channel_reads.cursor("ops", peer), 1)
        result = self.tail_json("ops", "--unread", "--limit", "1")
        self.assertEqual([e["seq"] for e in result["entries"]], [2])
        self.assertEqual(channel_reads.cursor("ops", self.me), 0)
        self.assertEqual(set(result), {"topic", "entries", "next", "gap", "channel"})
        self.assertEqual(len(channel_reads.summaries(peer)), 1)
        self.assertIn("1 unread (0 mentioning you)", channel_reads.summaries(peer)[0])
        self.tail_json("ops", "--unread")
        self.assertEqual(channel_reads.summaries(peer), [])

    def test_unread_counts_exclude_own_posts_without_advancing_cursor(self):
        from session_peers import channel_reads
        self.sender_cc()
        _listener, peer = self.peer_cc("beta")
        self.channel("ops", peer)
        self.send(message="own note")
        self.assertEqual(channel_reads.summaries(self.me), [])
        self.assertEqual(channel_reads.cursor("ops", self.me), 0)
        self.assertIn("1 unread (0 mentioning you)", channel_reads.summaries(peer)[0])

    def test_unread_rejects_since_and_nonmember_without_cursor_write(self):
        from session_peers import channel_reads
        self.channel("ops")
        rc, _out, _err = self.ch("tail", "ops", "--unread", "--since", "0")
        self.assertNotEqual(rc, 0)
        stranger = self.as_cc()
        rc, _out, _err = self.ch("tail", "ops", "--unread")
        self.assertNotEqual(rc, 0)
        self.assertEqual(channel_reads.cursor("ops", stranger), 0)

    def test_output_failure_and_plain_tail_never_advance_and_json_has_no_reminder(self):
        from session_peers import channel_reads
        self.channel("ops")
        self.tail_json("ops")
        self.assertEqual(channel_reads.cursor("ops", self.me), 0)
        with mock.patch.object(channel_reads, "print_summary", side_effect=AssertionError("JSON polluted")):
            self.assertEqual(len(json.loads(self.ok("list", "--json"))), 1)
        args = argparse.Namespace(name="ops", unread=True, since=None, limit=1, json=True)
        with mock.patch("sys.stdout", new=io.StringIO()) as output:
            output.flush = mock.Mock(side_effect=BrokenPipeError("closed reader"))
            with self.assertRaises(BrokenPipeError):
                channels._cmd_tail(args, self.me)
        self.assertEqual(channel_reads.cursor("ops", self.me), 0)

    def test_read_progress_resets_when_channel_is_recreated_and_does_not_become_ack(self):
        from session_peers import channel_reads
        self.channel("ops")
        self.tail_json("ops", "--unread")
        self.assertEqual(channel_reads.cursor("ops", self.me), 1)
        def recreate(rec):
            rec["created_at"] = "2099-01-01T00:00:00Z"
            return None, None
        channels.change_membership("ops", recreate, self.me)
        self.assertEqual(channel_reads.cursor("ops", self.me), 0)
        self.assertEqual(self.journal_entries(), [])
