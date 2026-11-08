"""Buddy route: a targeted send through the pair's buddy channel, its transport outcome and its history evidence."""

import argparse
import contextlib
import io

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_buddy import BuddyChannelBase
from peer_tests.test_opencode import OC_B
from peer_tests.test_webdata import EMPTY
from session_peers import channels as sp_channels, claude as sp_claude, journal as sp_journal
from session_peers import messaging as sp_messaging, routes as sp_routes, topics as sp_topics, webdata

FAKE_AUTH = "line-one"


class TargetedBase(BuddyChannelBase):
    def setUp(self):
        super().setUp()
        for member in (self.x, self.y):
            sp_channels.buddy_channel_add(self.lead, member)

    def targeted_args(self, message, **extra):
        values = dict(
            to=None, message=message, message_file=None, json=True, record=False, wake=False,
            never_wake=False, allow_reply_wake=None, in_reply_to=None, from_thread=None,
            from_name=None, from_sid=None, from_socket=None, timeout=None, web_sender=None,
            result_sink=[])
        values.update(extra)
        return argparse.Namespace(**values)

    def send_to(self, recipient, message="hello", **extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sp_channels.send_targeted(self.targeted_args(message, **extra), self.key, self.lead, recipient)
        return rc, out.getvalue(), err.getvalue()

    def sends(self):
        return [e for e in self.history(self.key) if e["kind"] == "channel.send"]


class BuddyRouteTests(TargetedBase):
    def test_a_member_of_the_leads_buddy_channel_routes_to_its_key(self):
        self.assertEqual(sp_channels.buddy_route(self.lead, self.x), (self.key, None))

    def test_a_lead_without_a_buddy_channel_has_no_route(self):
        stranger = "cc:" + new_uuid()
        self.assertEqual(sp_channels.buddy_route(stranger, self.x), (None, "no buddy channel"))

    def test_a_member_outside_the_roster_has_no_route(self):
        outsider = "codex:" + new_uuid()
        self.assertEqual(sp_channels.buddy_route(self.lead, outsider), (None, "member not in #%s" % self.key))

    def test_a_lead_who_left_the_roster_has_no_route(self):
        rec = sp_channels.read_channel(self.key)
        rec["members"].remove(self.lead)
        sp_channels._write_record(rec)
        self.assertEqual(sp_channels.buddy_route(self.lead, self.x), (None, "the lead left #%s" % self.key))

    def test_an_unreadable_store_is_a_reason_not_a_crash(self):
        with mock.patch.object(sp_channels, "find_buddy_channel", side_effect=sp_channels.ChannelError("locked")):
            self.assertEqual(sp_channels.buddy_route(self.lead, self.x), (None, "locked"))
        with mock.patch.object(sp_channels, "find_buddy_channel", side_effect=OSError("io")):
            self.assertEqual(sp_channels.buddy_route(self.lead, self.x), (None, "io"))


class SendTargetedTests(TargetedBase):
    def test_one_recipient_one_history_entry_one_delivery(self):
        rc, out, err = self.send_to(self.y)
        self.assertEqual(rc, 0, err)
        entries = self.sends()
        self.assertEqual(len(entries), 1)
        data = entries[0]["data"]
        self.assertEqual(data["recipients"], [self.y])
        self.assertIs(data["targeted"], True)
        queued = self.queue_calls()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0][2], self.y.partition(":")[2])
        self.assertIn("[channel #%s message %s from %s to %s]" % (self.key, data["message_id"], self.lead, self.y), queued[0][4])
        result = json.loads(out)
        self.assertEqual((result["route"], result["channel"]), ("buddy_channel", self.key))
        self.assertEqual(result["outcomes"], {self.y: "transport accepted"})
        self.assertEqual(result["message_id"], data["message_id"])
        self.assertNotIn("journal", result)
        outcomes = [e for e in self.history(self.key) if e["kind"] == "channel.outcome"]
        self.assertEqual([e["data"]["outcomes"] for e in outcomes], [{self.y: "transport accepted"}])

    def test_an_earlier_buddy_still_on_the_roster_receives_nothing(self):
        self.assertEqual(self.members(), [self.lead, self.x, self.y])
        rc, _out, err = self.send_to(self.y)
        self.assertEqual(rc, 0, err)
        self.assertEqual([c[2] for c in self.queue_calls()], [self.y.partition(":")[2]])

    def test_a_leading_all_is_plain_text_and_not_journaled_without_record(self):
        rc, out, err = self.send_to(self.y, "@all freeze")
        self.assertEqual(rc, 0, err)
        data = self.sends()[0]["data"]
        self.assertEqual((data["recipients"], data["journaled"]), ([self.y], False))
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(len(self.queue_calls()), 1)
        self.assertNotIn("journal", json.loads(out))

    def test_record_journals_that_one_recipient_only(self):
        rc, out, err = self.send_to(self.y, "@all freeze", record=True)
        self.assertEqual(rc, 0, err)
        entries = self.journal_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["to"], [self.y])
        mid = self.sends()[0]["data"]["message_id"]
        self.assertEqual(json.loads(out)["journal"], {"message_id": mid, "recorded": True})
        self.assertTrue(self.sends()[0]["data"]["journaled"])

    def test_human_output_names_the_buddy_route_and_the_outcome(self):
        rc, out, err = self.send_to(self.y, json=False)
        self.assertEqual(rc, 0, err)
        mid = self.sends()[0]["data"]["message_id"]
        self.assertIn("sent to #%s (buddy), message %s" % (self.key, mid), out)
        self.assertIn("%s: transport accepted" % self.y, out)

    def test_a_non_member_recipient_and_a_gone_one_are_refused_before_anything_is_written(self):
        outsider = "codex:" + new_uuid()
        with self.assertRaises(sp_channels.ChannelError):
            self.send_to(outsider)
        with mock.patch.object(sp_channels, "member_liveness", return_value="gone"):
            with self.assertRaises(sp_channels.ChannelError) as ctx:
                self.send_to(self.y)
        self.assertIn("is gone; nothing was recorded or sent", str(ctx.exception))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.queue_calls(), [])

    def test_an_oversize_body_raises_the_distinct_error_naming_the_header(self):
        with self.assertRaises(sp_channels.TargetedTooLarge) as ctx:
            sp_channels.prepare_targeted(self.key, self.lead, self.y, "x" * 2_000_000, new_uuid(), False)
        self.assertRegex(str(ctx.exception), r"channel header .*\bis \d+ bytes")
        self.assertIsInstance(ctx.exception, sp_channels.ChannelError)

    def test_prepare_targeted_writes_nothing(self):
        before = (self.sends(), self.journal_entries())
        plan = sp_channels.prepare_targeted(self.key, self.lead, self.y, "@all hi", new_uuid(), True, wake=True)
        self.assertEqual((plan.snapshot, plan.is_all, plan.journaled, plan.pointer, plan.wake),
                         ([self.y], False, True, True, True))
        self.assertEqual((self.sends(), self.journal_entries()), before)


class UnknownOutcomeTests(TargetedBase):
    def setUp(self):
        super().setUp()
        self.peer, self.peer_typed = self.peer_cc("bravo")
        sp_channels.buddy_channel_add(self.lead, self.peer_typed)

    def test_an_uncertain_frame_is_recorded_unknown_and_never_marks_the_journal_failed(self):
        with mock.patch.object(sp_claude, "send_frame", side_effect=sp_claude.FrameUncertain("broken pipe")), \
                mock.patch.object(sp_journal, "transport_failed") as failed:
            rc, out, err = self.send_to(self.peer_typed, record=True)
        self.assertEqual(rc, 1, err)
        failed.assert_not_called()
        self.assertEqual(json.loads(out)["outcomes"], {self.peer_typed: "unknown"})
        outcome = [e for e in self.history(self.key) if e["kind"] == "channel.outcome"][0]
        self.assertEqual(outcome["data"]["outcomes"], {self.peer_typed: "unknown"})

    def test_a_plain_connect_failure_stays_a_refusal(self):
        with mock.patch.object(sp_claude, "send_frame", side_effect=OSError("refused")), \
                mock.patch.object(sp_journal, "transport_failed", return_value=0):
            rc, out, _err = self.send_to(self.peer_typed)
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["outcomes"][self.peer_typed].startswith("refused: "))

    def test_the_uncertain_error_message_is_printed_for_a_direct_claude_send(self):
        rec = self.read_peer_record()
        args = self.targeted_args("hi", to=self.peer_typed)
        err = io.StringIO()
        with mock.patch.object(sp_claude, "send_frame", side_effect=sp_claude.FrameUncertain("boom")), \
                mock.patch.object(sp_claude, "_resolve_claude_record", return_value=rec), \
                mock.patch.object(sp_routes, "verify_record", return_value=None), \
                contextlib.redirect_stderr(err):
            rc = sp_messaging._send_claude_channel(self.peer_typed, args, self.lead.partition(":")[2])
        self.assertEqual(rc, 1)
        self.assertIn(
            "error: outcome unknown: could not confirm delivery to %s: boom; "
            "check the recipient before resending" % self.peer_typed, err.getvalue())

    def read_peer_record(self):
        return [r for r in sp_claude.read_claude_records()
                if r.get("sessionId") == self.peer_typed.partition(":")[2]][0]


class FakeSocket:
    """A socket whose connect works and whose sendall fails from the ``fail_at``-th call."""

    fail_at = 1
    calls = 0

    def __init__(self, *_args, **_kwargs):
        pass

    def settimeout(self, _timeout):
        pass

    def connect(self, _path):
        pass

    def sendall(self, _data):
        type(self).calls += 1
        if type(self).calls >= type(self).fail_at:
            raise BrokenPipeError("peer went away")

    def close(self):
        pass


class FrameOutcomeTests(Base):
    def setUp(self):
        super().setUp()
        self.listener, self.rec = self.add_listener(name="peer", session_id=new_uuid())
        self.frame = {"type": "user", "text": "hi"}

    def fake_socket(self, fail_at):
        return type("Fake", (FakeSocket,), {"fail_at": fail_at, "calls": 0})

    def test_delivered(self):
        self.assertEqual(sp_claude.deliver_to_record_outcome(self.rec, self.frame), "delivered")
        self.assertTrue(wait_for(lambda: self.listener.of_type("user")))

    def test_a_refused_connect_is_failed(self):
        stale = str(self.socks / "stale.sock")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(stale)
        dead.close()  # the file stays, nothing listens
        rec = dict(self.rec, messagingSocketPath=stale)
        self.assertEqual(sp_claude.deliver_to_record_outcome(rec, self.frame), "failed")
        with self.assertRaises(OSError) as ctx:
            sp_claude.send_frame(stale, self.frame)
        self.assertNotIsInstance(ctx.exception, sp_claude.FrameUncertain)

    def test_a_failure_during_the_frame_write_is_unknown(self):
        with mock.patch.object(sp_claude.socket, "socket", self.fake_socket(1)):
            self.assertEqual(sp_claude.deliver_to_record_outcome(self.rec, self.frame), "unknown")
            with self.assertRaises(sp_claude.FrameUncertain):
                sp_claude.send_frame(self.rec["messagingSocketPath"], self.frame)

    def test_a_failure_writing_the_auth_line_is_failed(self):
        with mock.patch.object(sp_claude.socket, "socket", self.fake_socket(1)), \
                mock.patch.object(sp_claude, "peer_token_for", return_value=FAKE_AUTH):
            self.assertEqual(sp_claude.deliver_to_record_outcome(self.rec, self.frame), "failed")

    def test_a_failure_on_the_frame_after_the_auth_line_is_unknown(self):
        with mock.patch.object(sp_claude.socket, "socket", self.fake_socket(2)), \
                mock.patch.object(sp_claude, "peer_token_for", return_value=FAKE_AUTH):
            self.assertEqual(sp_claude.deliver_to_record_outcome(self.rec, self.frame), "unknown")

    def test_a_non_allowlisted_socket_is_failed_and_deliver_to_record_is_unchanged(self):
        rec = dict(self.rec, messagingSocketPath="/nonexistent-dir/x.sock")
        self.assertEqual(sp_claude.deliver_to_record_outcome(rec, self.frame), "failed")
        self.assertIs(sp_claude.deliver_to_record(rec, self.frame), False)
        with mock.patch.object(sp_claude.socket, "socket", self.fake_socket(1)):
            self.assertIs(sp_claude.deliver_to_record(self.rec, self.frame), False)


class WakePassthroughTests(TargetedBase):
    def captured(self, **kwargs):
        seen = []

        def dispatch(member_args, _sender):
            seen.append(member_args)
            return 0

        with mock.patch.object(sp_messaging, "_dispatch_send", dispatch), \
                mock.patch.object(sp_routes, "check", return_value=None):
            sp_channels._deliver_member(
                self.targeted_args("x"), self.y, self.lead, new_uuid(), "text", None, **kwargs)
        return seen[0]

    def test_defaults_never_wake_and_do_not_force_a_wake(self):
        got = self.captured()
        self.assertEqual((got.never_wake, got.wake), (True, False))
        self.assertFalse(sp_messaging._may_wake_opencode(got, OC_B, self.lead))

    def test_never_wake_false_lets_may_wake_opencode_see_the_binding(self):
        got = self.captured(never_wake=False)
        self.assertEqual((got.never_wake, got.wake), (False, False))
        with mock.patch.object(sp_messaging.sp_buddy, "read_buddy",
                               return_value={"buddy": {"kind": "oc", "uuid": OC_B}}):
            self.assertTrue(sp_messaging._may_wake_opencode(got, OC_B, self.lead))
            self.assertFalse(sp_messaging._may_wake_opencode(got, "other", self.lead))

    def test_wake_is_passed_through(self):
        got = self.captured(never_wake=False, wake=True)
        self.assertTrue(sp_messaging._may_wake_opencode(got, OC_B, self.lead))


class EvidenceTests(TargetedBase):
    def post(self, kind, mid, text_pad=""):
        data = {"message_id": mid}
        if text_pad:
            data["pad"] = text_pad
        return sp_channels.append_history(self.key, kind, self.lead, data)

    def test_history_floor_is_the_last_seq(self):
        base = sp_channels.history_floor(self.key)
        self.assertEqual(base, self.history(self.key)[-1]["seq"])
        self.post("channel.note", new_uuid())
        self.assertEqual(sp_channels.history_floor(self.key), base + 1)

    def test_found_absent_and_the_two_kinds(self):
        floor = sp_channels.history_floor(self.key)
        mid, other = new_uuid(), new_uuid()
        self.assertEqual(sp_channels.find_send(self.key, mid, floor), ("absent", None))
        self.post(sp_channels.EVENT_SEND, mid)
        status, entry = sp_channels.find_send(self.key, mid, floor)
        self.assertEqual((status, entry["data"]["message_id"]), ("found", mid))
        self.assertEqual(sp_channels.find_outcome(self.key, mid, floor), ("absent", None))
        self.post(sp_channels.EVENT_OUTCOME, mid)
        self.assertEqual(sp_channels.find_outcome(self.key, mid, floor)[0], "found")
        self.assertEqual(sp_channels.find_send(self.key, other, floor), ("absent", None))
        # Entries at or before the floor are not searched.
        self.assertEqual(sp_channels.find_send(self.key, mid, floor + 5), ("absent", None))

    def test_pruning_by_count_makes_the_evidence_missing(self):
        floor = sp_channels.history_floor(self.key)
        mid = new_uuid()
        with mock.patch.dict(os.environ, {"SESSION_PEERS_TOPIC_MAX_ENTRIES": "3"}):
            self.post(sp_channels.EVENT_SEND, mid)
            for _ in range(6):
                self.post("channel.note", new_uuid())
            self.assertIsNotNone(sp_channels.read_history(self.key, since=floor)["gap"])
            self.assertEqual(sp_channels.find_send(self.key, mid, floor), ("evidence missing", None))
            self.assertEqual(sp_channels.find_outcome(self.key, mid, floor), ("evidence missing", None))
            self.post(sp_channels.EVENT_OUTCOME, mid)
            self.assertEqual(sp_channels.find_outcome(self.key, mid, floor)[0], "found")

    def test_pruning_by_bytes_makes_the_evidence_missing(self):
        floor = sp_channels.history_floor(self.key)
        mid = new_uuid()
        with mock.patch.dict(os.environ, {"SESSION_PEERS_TOPIC_MAX_BYTES": "2000"}):
            self.post(sp_channels.EVENT_SEND, mid)
            for _ in range(8):
                self.post("channel.note", new_uuid(), "p" * 400)
            self.assertIsNotNone(sp_channels.read_history(self.key, since=floor)["gap"])
            self.assertEqual(sp_channels.find_send(self.key, mid, floor), ("evidence missing", None))

    def test_an_unreadable_history_raises_channel_error(self):
        for exc in (OSError("io"), sp_topics.TopicError("bad topic")):
            with self.subTest(exc=exc), mock.patch.object(sp_channels, "read_history", side_effect=exc):
                with self.assertRaises(sp_channels.ChannelError):
                    sp_channels.find_send(self.key, new_uuid(), 0)
                with self.assertRaises(sp_channels.ChannelError):
                    sp_channels.find_outcome(self.key, new_uuid(), 0)
                with self.assertRaises(sp_channels.ChannelError):
                    sp_channels.history_floor(self.key)


class FinishTargetedTests(TargetedBase):
    def outcomes(self):
        return [e for e in self.history(self.key) if e["kind"] == "channel.outcome"]

    def test_a_second_finish_appends_nothing(self):
        floor = sp_channels.history_floor(self.key)
        mid = new_uuid()
        self.assertIs(sp_channels.finish_targeted(self.key, self.lead, mid, self.y, "unknown", floor), True)
        self.assertIs(sp_channels.finish_targeted(self.key, self.lead, mid, self.y, "transport accepted", floor), False)
        entries = self.outcomes()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["data"], {"message_id": mid, "outcomes": {self.y: "unknown"}})
        self.assertFalse(os.path.exists(sp_channels.send_lock_path(mid)))

    def test_a_published_send_is_finished_with_its_held_lock_released(self):
        floor = sp_channels.history_floor(self.key)
        mid = new_uuid()
        plan = sp_channels.prepare_targeted(self.key, self.lead, self.y, "hi", mid, False)
        lock = sp_channels.publish_targeted(plan)
        self.assertTrue(os.path.exists(sp_channels.send_lock_path(mid)))
        self.assertEqual(sp_channels.find_send(self.key, mid, floor)[0], "found")
        self.assertIs(sp_channels.finish_targeted(self.key, self.lead, mid, self.y, "transport accepted", floor, lock), True)
        self.assertFalse(os.path.exists(sp_channels.send_lock_path(mid)))
        self.assertEqual(self.sends()[-1]["data"]["targeted"], True)

    def test_the_lock_is_released_when_the_append_fails(self):
        mid = new_uuid()
        lock = sp_channels._SendLock(mid)
        with mock.patch.object(sp_channels, "append_history", side_effect=sp_channels.ChannelError("full", 1)):
            with self.assertRaises(sp_channels.ChannelError):
                sp_channels.finish_targeted(self.key, self.lead, mid, self.y, "unknown", 0, lock)
        self.assertFalse(os.path.exists(sp_channels.send_lock_path(mid)))

    def test_release_unfinished_drops_the_lock_without_an_outcome(self):
        mid = new_uuid()
        lock = sp_channels._SendLock(mid)
        sp_channels.release_unfinished(lock)
        self.assertFalse(os.path.exists(sp_channels.send_lock_path(mid)))
        self.assertEqual(self.outcomes(), [])


class WebdataTargetedTests(TargetedBase):
    def test_a_targeted_all_gets_no_tally_and_no_missing_ack_alert(self):
        rc, _out, err = self.send_to(self.y, "@all freeze", record=True)
        self.assertEqual(rc, 0, err)
        page = webdata.channel_page(self.key)
        send = [e for e in page["entries"] if e["kind"] == "send"][0]
        self.assertTrue(send["journaled"])
        self.assertNotIn("tally", send)
        alerts = {row["key"]: row["alert"] for row in webdata.state_view(EMPTY)["channels"]}
        self.assertIsNone(alerts[self.key])
