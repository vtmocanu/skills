"""A Codex shim's answers ride the pair's buddy channel when the requester leads one."""

from __future__ import annotations

import contextlib
import io
import os
import time
from unittest import mock

from .support import ShimBase, new_uuid, sp_codex, wait_for
from session_peers import channels as sp_channels, claude as sp_claude, constants as sp_constants
from session_peers import protocol as sp_protocol, shim as sp_shim, storage as sp_storage


class Crash(BaseException):
    """Stands in for a process killed between two steps."""


class RouteBase(ShimBase):
    """A shim whose requester (a Claude listener) leads a buddy channel the thread belongs to."""

    def setUp(self):
        super().setUp()
        self.shim, self.tid, _rollout = self.make_shim()
        self.sid = new_uuid()
        self.lead = "cc:" + self.sid
        self.listener, self.rec = self.add_listener(name="cc-main", session_id=self.sid)
        self.sender = "codex:" + self.tid
        self.key = sp_channels.buddy_channel_add(self.lead, self.sender)
        self.turns = 0

    def turn(self, text="the answer", mid="m-1", turn_id=None, reply=None):
        self.turns += 1
        tag = {"from": "cc-main", "sid": self.sid, "mid": mid, "reply": reply or self.listener.path}
        return sp_constants.Turn(turn_id or "t%d" % self.turns, "ping", tag, "complete", text)

    def history(self):
        return sp_channels.read_history(self.key, limit=sp_constants.TOPIC_TAIL_MAX)["entries"]

    def sends(self):
        return [e for e in self.history() if e["kind"] == "channel.send"]

    def outcomes(self):
        return [e["data"]["outcomes"] for e in self.history() if e["kind"] == "channel.outcome"]

    def replies(self, listener=None):
        return [f for f in (listener or self.listener).of_type("user") if f.get("from")]

    def notices(self):
        return [f for f in self.listener.of_type("user") if not f.get("from")]

    def content(self, frame):
        return frame["message"]["content"]


class TestRoutedAnswer(RouteBase):
    def test_the_answer_is_one_targeted_send_and_one_frame_with_the_channel_header(self):
        self.shim._handle_turn_end(self.turn())
        frames = wait_for(lambda: self.replies())
        time.sleep(0.2)
        frames = self.replies()
        self.assertEqual(len(frames), 1)
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        data = sends[0]["data"]
        self.assertEqual(data["recipients"], [self.lead])
        self.assertIs(data["targeted"], True)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        body = self.content(frames[0])
        self.assertIn("[channel #%s message %s from %s to %s]" % (self.key, data["message_id"], self.sender, self.lead), body)
        self.assertIn("[in reply to message m-1]", body)
        self.assertEqual(frames[0]["msg_id"], data["message_id"])
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)

    def test_a_codex_lead_is_routed_as_codex(self):
        lead_tid = new_uuid()
        listener, _rec = self.add_listener(name="codex-lead", session_id=lead_tid, pid=os.getppid())
        self.write_record(os.getppid(), "codex-lead", lead_tid, listener.path, entrypoint="codex")
        lead = "codex:" + lead_tid
        key = sp_channels.buddy_channel_add(lead, self.sender)
        self.sid = lead_tid
        with mock.patch.object(sp_channels, "member_liveness", return_value="live"):
            self.shim._handle_turn_end(self.turn(reply=listener.path))
        wait_for(lambda: self.replies(listener))
        time.sleep(0.2)
        self.assertEqual(len(self.replies(listener)), 1)
        entries = [e for e in sp_channels.read_history(key, limit=50)["entries"] if e["kind"] == "channel.send"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["data"]["recipients"], [lead])
        self.assertIn("to %s]" % lead, self.content(self.replies(listener)[0]))

    def test_a_shim_under_another_codex_home_still_routes_through_the_channel(self):
        other = self.root / "orca"
        other.mkdir()
        os.environ["CODEX_HOME"] = str(other)
        self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn("[channel #%s " % self.key, self.content(self.replies()[0]))

    def test_without_a_buddy_channel_the_delivery_is_direct(self):
        sp_channels.buddy_channel_remove(self.lead, self.sender)
        self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        frames = self.replies()
        self.assertEqual(len(frames), 1)
        self.assertNotIn("[channel #", self.content(frames[0]))
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)

    def test_a_routed_answer_leaves_no_no_final_message_notice(self):
        self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(self.notices(), [])

    def test_a_manual_channel_post_does_not_hide_an_empty_final_answer(self):
        sp_channels.append_history(
            self.key, "channel.send", self.sender,
            {"sender": self.sender, "body": "by hand", "message_id": new_uuid(), "recipients": [self.lead],
             "targeted": True})
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(text=None))
        wait_for(lambda: self.notices())
        notices = self.notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("no final message", self.content(notices[0]))


class TestRoutedAtMostOnce(RouteBase):
    def test_a_crash_after_the_frame_lands_sends_no_second_frame_and_leaves_the_send_unfinished(self):
        turn = self.turn()

        def die(key, sender, mid, recipient, outcome, floor, lock=None):
            sp_channels.release_unfinished(lock)  # the dead process frees its lock
            raise Crash()

        with mock.patch.object(sp_channels, "finish_targeted", die):
            with self.assertRaises(Crash):
                self.shim._handle_turn_end(turn)
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.replies()), 1)
        thread = sp_codex.resolve_thread(self.tid)
        restarted = sp_shim.Shim(thread)
        restarted.codex_version = "0.153.4"
        restarted._handle_turn_end(turn)
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [])
        mid = self.sends()[0]["data"]["message_id"]
        lock = sp_channels._SendLock(mid)  # free: nothing holds it
        lock.release()


class TestRouteFailures(RouteBase):
    def fail_once(self, after_write=False):
        real = sp_channels.append_history
        state = {"n": 0}

        def flaky(key, kind, actor, data=None):
            if kind == "channel.send" and state["n"] == 0:
                state["n"] += 1
                if after_write:
                    real(key, kind, actor, data)
                raise sp_channels.ChannelError("disk went away", 1)
            return real(key, kind, actor, data)

        return mock.patch.object(sp_channels, "append_history", flaky)

    def test_a_failed_channel_write_holds_the_answer_and_a_retry_delivers_it_once(self):
        with self.fail_once():
            self.shim._handle_turn_end(self.turn())
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)
        held = self.shim.reply_budget.held[self.sid]
        self.assertEqual(held["release"], "route")
        self.assertEqual(held["channel"], self.key)
        mid = held["channel_mid"]
        self.assertTrue(mid)
        self.shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        sends = self.sends()
        self.assertEqual([e["data"]["message_id"] for e in sends], [mid])
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertNotIn(self.sid, self.shim.reply_budget.held)
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)
        self.shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)

    def test_a_publish_that_raised_after_the_entry_was_written_is_delivered_once(self):
        with self.fail_once(after_write=True):
            self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])

    def test_an_unconfirmed_frame_is_recorded_unknown_spends_nothing_and_is_not_retried(self):
        real = sp_claude.deliver_to_record

        def uncertain(rec, frame):
            real(rec, frame)
            return "unknown"

        with mock.patch.object(sp_claude, "deliver_to_record_outcome", uncertain):
            self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)
        self.assertEqual(self.shim.reply_budget.held, {})
        self.shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)

    def test_a_refused_frame_is_recorded_refused_and_spends_nothing(self):
        with mock.patch.object(sp_claude, "deliver_to_record_outcome", return_value="failed"):
            self.shim._handle_turn_end(self.turn())
        self.assertEqual(self.outcomes(), [{self.lead: "refused: delivery failed"}])
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)

    def test_an_unreadable_history_drops_the_answer_without_delivering(self):
        def unreadable(key, mid, floor):
            raise sp_channels.ChannelError("unreadable", 1)

        with self.fail_once(), mock.patch.object(sp_channels, "find_send", unreadable):
            self.shim._handle_turn_end(self.turn())
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)


class TestRoutedBudget(RouteBase):
    def test_a_spent_budget_holds_and_the_reset_releases_through_the_channel_once(self):
        for index in range(sp_constants.REPLY_BUDGET):
            self.shim._handle_turn_end(self.turn(text="a%d" % index, mid="m%d" % index))
        wait_for(lambda: len(self.replies()) == sp_constants.REPLY_BUDGET or None)
        sends_before = len(self.sends())
        self.assertEqual(sends_before, sp_constants.REPLY_BUDGET)
        self.shim._handle_turn_end(self.turn(text="held answer", mid="m-held"))
        held = self.shim.reply_budget.held[self.sid]
        self.assertNotIn("channel_mid", held)
        self.assertEqual(len(self.sends()), sends_before)
        with open(sp_storage.budget_reset_path(self.tid), "w") as fh:
            fh.write("{}")
        self.shim.reply_budget.consume_reset()
        wait_for(lambda: len(self.replies()) == sp_constants.REPLY_BUDGET + 1 or None)
        sends = self.sends()
        self.assertEqual(len(sends), sends_before + 1)
        self.assertIn("held answer", sends[-1]["data"]["body"])
        self.assertIn("held reply, in reply to message m-held", self.content(self.replies()[-1]))
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)

    def test_a_frozen_held_reply_keeps_new_answers_unsent(self):
        self.shim.reply_budget.held[self.sid] = {
            "text": "x", "at": time.time(), "phase": "unknown"}
        self.shim.reply_budget.budget_sender_sid = self.sid  # the sequence is running
        self.shim.reply_budget.budget_last_at = time.time()
        self.shim._handle_turn_end(self.turn())
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)
