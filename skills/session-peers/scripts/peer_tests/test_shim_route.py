"""A Codex shim's answers ride the pair's buddy channel when the requester leads one."""

from __future__ import annotations

import collections
import contextlib
import io
import os
import signal
import time
from unittest import mock

from .support import ShimBase, append, ev, new_uuid, sp_codex, user_item, wait_for
from session_peers import budgets as sp_budgets, channels as sp_channels, claude as sp_claude
from session_peers import journal as sp_journal
from session_peers import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from session_peers import shim as sp_shim, storage as sp_storage


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

    def journal_entries(self):
        return sp_journal.read_only_view()["entries"]

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
        self.assertEqual(self.journal_entries(), [])

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


    def test_a_fresh_held_answer_goes_direct_once_its_route_no_longer_holds(self):
        # N1: held before publication, then the shim left the channel (`buddy clear`):
        # a retry must not keep failing on a route that can never succeed.
        with self.fail_once():
            self.shim._handle_turn_end(self.turn())
        held = self.shim.reply_budget.held[self.sid]
        self.assertEqual((held["release"], held["phase"]), ("route", "fresh"))
        sp_channels.buddy_channel_remove(self.lead, self.sender)
        self.shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.sends(), [])
        self.assertNotIn(self.sid, self.shim.reply_budget.held)

    def test_an_answer_whose_route_stopped_holding_before_publication_goes_direct(self):
        # N4: buddy_route succeeded, then the roster changed before prepare_targeted.
        real = sp_channels.prepare_targeted

        def leave_then_prepare(*args, **kw):
            sp_channels.buddy_channel_remove(self.lead, self.sender)
            return real(*args, **kw)

        with mock.patch.object(sp_channels, "prepare_targeted", leave_then_prepare):
            self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)

    def test_a_transient_prepare_failure_holds_the_answer_and_a_retry_routes_it_once(self):
        # N4: the route still holds, so the failure is transient: hold, then publish once.
        real = sp_channels.prepare_targeted
        state = {"n": 0}

        def flaky(*args, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise sp_channels.ChannelError("could not read the channel record", 1)
            return real(*args, **kw)

        with mock.patch.object(sp_channels, "prepare_targeted", flaky):
            self.shim._handle_turn_end(self.turn())
            time.sleep(0.2)
            self.assertEqual(self.replies(), [])
            self.assertEqual(self.shim.reply_budget.held[self.sid]["release"], "route")
            self.shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])


    def store_breaks_at(self, trigger):
        """Patch read_channel to fail once ``trigger`` (a patched sp_channels name) has been called."""
        real_read = sp_channels.read_channel
        real_trigger = getattr(sp_channels, trigger)
        state = {"broken": False}

        def read(*args, **kw):
            if state["broken"]:
                raise sp_channels.ChannelError("could not read the channel record", 1)
            return real_read(*args, **kw)

        def trig(*args, **kw):
            state["broken"] = True
            return real_trigger(*args, **kw)

        return state, mock.patch.multiple(sp_channels, read_channel=read, **{trigger: trig})

    def test_an_unreadable_store_at_prepare_and_recheck_holds_the_answer(self):
        # buddy_route verified the route; then the store cannot be read by prepare_targeted
        # nor by the recheck: no proof the route is gone, so hold, never send direct.
        state, patch = self.store_breaks_at("history_floor")
        with patch:
            self.shim._handle_turn_end(self.turn())
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.shim.reply_budget.held[self.sid]["release"], "route")
        self.assertEqual(self.sends(), [])

    def test_a_fresh_held_answer_with_an_unreadable_store_stays_held(self):
        with self.fail_once():
            self.shim._handle_turn_end(self.turn())
        self.assertEqual(self.shim.reply_budget.held[self.sid]["phase"], "fresh")
        state, patch = self.store_breaks_at("prepare_targeted")
        with patch:
            self.shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertIn(self.sid, self.shim.reply_budget.held)
        # Readable again: the retry publishes it once through the channel.
        self.shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)


class TestUnreadableFloor(RouteBase):
    @staticmethod
    def unreadable(key):
        raise sp_channels.ChannelError("history unreadable", 1)

    def test_an_unreadable_history_holds_an_ordinary_answer_and_a_retry_delivers_it_once(self):
        with mock.patch.object(sp_channels, "history_floor", self.unreadable):
            self.shim._handle_turn_end(self.turn())
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.shim.reply_budget.budgets.get(self.sid, 0), 0)
        held = self.shim.reply_budget.held[self.sid]
        self.assertEqual(held["release"], "route")
        self.assertEqual(held["phase"], "fresh")
        self.assertEqual(held["channel"], self.key)
        self.assertTrue(held["channel_mid"])
        self.assertIsNone(held["channel_floor"])
        mid = held["channel_mid"]
        with mock.patch.object(sp_channels, "history_floor", self.unreadable):
            self.shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertIn(self.sid, self.shim.reply_budget.held)
        self.shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)
        self.assertNotIn(self.sid, self.shim.reply_budget.held)

    def test_an_unreadable_history_keeps_a_released_held_answer_held(self):
        budget = self.shim.reply_budget
        budget.budgets[self.sid] = sp_constants.REPLY_BUDGET
        budget.budget_sender_sid = self.sid
        budget.budget_last_at = time.time()
        self.shim._handle_turn_end(self.turn(text="held answer", mid="m-held"))
        self.assertNotIn("channel_mid", budget.held[self.sid])
        with open(sp_storage.budget_reset_path(self.tid), "w") as fh:
            fh.write("{}")
        with mock.patch.object(sp_channels, "history_floor", self.unreadable):
            budget.consume_reset()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(budget.budgets.get(self.sid, 0), 0)
        self.assertEqual(budget.held[self.sid]["release"], "reset")
        budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(budget.budgets[self.sid], 1)
        self.assertEqual(budget.held, {})


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


# -- held replies -----------------------------------------------------------


class HeldBase(RouteBase):
    """Helpers for a held answer that is released through the channel."""

    def budget(self):
        return self.shim.reply_budget

    def cap_hold(self, text="held answer", mid="m-held"):
        """Spend the budget, then let one more answer be held (saved to the state file)."""
        budget = self.budget()
        budget.budgets[self.sid] = sp_constants.REPLY_BUDGET
        budget.budget_sender_sid = self.sid
        budget.budget_last_at = time.time()
        self.shim._handle_turn_end(self.turn(text=text, mid=mid))
        self.assertNotIn("channel_mid", budget.held[self.sid])
        self.shim._save_state()
        return budget.held[self.sid]

    def write_reset(self):
        with open(sp_storage.budget_reset_path(self.tid), "w") as fh:
            fh.write("{}")

    def write_allow(self, total):
        sp_runtime.write_json_atomic(
            sp_storage.budget_allow_path(self.tid),
            {"sid": self.sid, "total": total, "at": sp_runtime.now_iso()}, mode=0o600)

    def uncertain(self):
        """The frame reaches the recipient, then the sender is told the write failed."""
        real = sp_claude.send_frame

        def landed_then_lost(sock_path, frame, auth_token=None, timeout=10.0):
            real(sock_path, frame, auth_token=auth_token, timeout=timeout)
            raise sp_claude.FrameUncertain(32, "Broken pipe")

        return mock.patch.object(sp_claude, "send_frame", landed_then_lost)

    def fail_send_once(self, after_write=False):
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

    def freeze(self, mid="m-held"):
        """A held answer whose release was sent with an uncertain outcome."""
        self.cap_hold(mid=mid)
        self.write_reset()
        with self.uncertain():
            self.budget().consume_reset()
        wait_for(lambda: self.replies())
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["phase"], "unknown")
        return entry

    def frozen_notices(self):
        return [f for f in self.notices() if "may or may not" in self.content(f)]

    def statuses(self):
        return self.listener.of_type("control", "peer_message_status")

    def restart(self, release=True):
        """What run() does after taking ownership, on a fresh process."""
        shim = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        shim.codex_version = "0.153.4"
        shim._reload_state()
        shim._recover_held()
        shim.reply_budget.consume_reset(initial=True)
        shim._save_state()
        self.shim = shim
        if release and shim.reply_budget.release_after_start:
            shim.reply_budget.release_after_start = False
            shim.reply_budget.release_held()
            shim._save_state()
        return shim

    def publish(self, mid, text="held answer"):
        """Write the channel.send of ``mid`` by hand, as an interrupted release would; its floor."""
        floor = sp_channels.history_floor(self.key)
        plan = sp_channels.prepare_targeted(self.key, self.sender, self.lead, text, mid, False)
        sp_channels.release_unfinished(sp_channels.publish_targeted(plan))
        return floor

    def route_entry(self, phase, mid, floor, release=None, text="held answer"):
        entry = {
            "text": text, "mid": "m-held", "turn_id": "t-h", "at": time.time(), "phase": phase,
            "channel": self.key, "channel_mid": mid, "channel_floor": floor,
            "channel_sender": self.sender, "channel_recipient": self.lead,
        }
        if release:
            entry["release"] = release
        self.budget().held[self.sid] = entry
        self.shim._save_state()
        return entry


class TestHeldRelease(HeldBase):
    def test_a_reset_releases_a_held_answer_through_the_channel_once(self):
        self.cap_hold()
        self.write_reset()
        self.budget().consume_reset()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(self.budget().held, {})
        self.assertEqual(self.budget().budgets[self.sid], 1)
        self.assertEqual(self.journal_entries(), [])

    def test_an_allowance_releases_a_held_answer_through_the_channel_once(self):
        self.cap_hold()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        self.budget().consume_allowance()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(self.budget().held, {})
        self.assertEqual(self.budget().budgets[self.sid], sp_constants.REPLY_BUDGET + 1)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_a_binding_releases_a_normal_held_answer_but_not_a_frozen_one(self):
        self.cap_hold()
        bind = {"sid": self.sid, "bind_id": "b-1", "total": 5}
        sp_runtime.write_json_atomic(sp_storage.budget_binding_path(self.tid), bind, mode=0o600)
        self.budget().consume_binding()
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.budget().held, {})
        # A frozen entry is neither released nor discarded by a binding.
        frozen = {"text": "x", "at": time.time(), "phase": "unknown", "channel_mid": "z"}
        self.budget().held[self.sid] = frozen
        self.budget().budgets[self.sid] = 99
        sp_runtime.write_json_atomic(
            sp_storage.budget_binding_path(self.tid), dict(bind, bind_id="b-2", total=500), mode=0o600)
        self.budget().consume_binding()
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertIs(self.budget().held[self.sid], frozen)

    def test_a_failed_write_while_releasing_spends_nothing_and_the_answer_stays_held(self):
        self.cap_hold()
        self.write_reset()
        with self.fail_send_once():
            self.budget().consume_reset()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["phase"], "fresh")
        self.assertEqual(entry["release"], "reset")
        self.assertEqual(self.budget().budgets.get(self.sid, 0), 0)
        self.budget().retry_held()
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.budget().budgets[self.sid], 1)

    def test_a_refused_frame_after_the_history_write_is_retried_with_one_send_and_one_spend(self):
        self.cap_hold()
        self.write_reset()
        with mock.patch.object(sp_claude, "deliver_to_record_outcome", return_value="failed"):
            self.budget().consume_reset()
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["phase"], "published")
        self.assertEqual(entry["release"], "reset")
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(self.budget().budgets.get(self.sid, 0), 0)
        mid = entry["channel_mid"]
        self.budget().retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(self.budget().budgets[self.sid], 1)
        self.assertEqual(self.budget().held, {})
        self.assertEqual(self.journal_entries(), [])

    def test_an_uncertain_release_reaches_the_recipient_once_and_is_never_retried(self):
        self.cap_hold()
        self.write_reset()
        with self.uncertain():
            self.budget().consume_reset()
        wait_for(lambda: self.replies())
        for _ in range(3):
            self.budget().retry_held()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertEqual(self.budget().budgets.get(self.sid, 0), 0)
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["phase"], "unknown")
        self.assertNotIn("release", entry)

    def test_a_cap_blocked_retry_drops_the_release_tag_of_a_route_entry_and_sends_nothing(self):
        self.route_entry("fresh", new_uuid(), None, release="route")
        self.budget().budgets[self.sid] = sp_constants.REPLY_BUDGET
        self.budget().budget_sender_sid = self.sid
        self.budget().budget_last_at = time.time()
        self.budget().retry_held()
        time.sleep(0.2)
        self.assertNotIn("release", self.budget().held[self.sid])
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.sends(), [])

    def test_the_cap_is_honoured_after_other_replies_used_the_budget(self):
        with self.fail_send_once():
            self.shim._handle_turn_end(self.turn(text="first", mid="m-first"))
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["release"], "route")
        for index in range(sp_constants.REPLY_BUDGET):
            self.shim._handle_turn_end(self.turn(text="b%d" % index, mid="mb%d" % index))
        wait_for(lambda: len(self.replies()) == sp_constants.REPLY_BUDGET or None)
        sends = len(self.sends())
        self.budget().retry_held()
        time.sleep(0.2)
        self.assertEqual(len(self.sends()), sends)
        self.assertEqual(len(self.replies()), sp_constants.REPLY_BUDGET)
        self.assertIn(self.sid, self.budget().held)
        self.assertEqual(self.budget().budgets[self.sid], sp_constants.REPLY_BUDGET)


class TestHeldRouteSurvives(HeldBase):
    def hold_published(self):
        self.cap_hold()
        self.write_reset()
        with mock.patch.object(sp_claude, "deliver_to_record_outcome", return_value="failed"):
            self.budget().consume_reset()
        entry = self.budget().held[self.sid]
        self.assertEqual(entry["phase"], "published")
        return entry["channel"], entry["channel_mid"]

    def test_a_buddy_clear_between_the_hold_and_the_release_does_not_reroute(self):
        key, mid = self.hold_published()
        sp_channels.buddy_channel_remove(self.lead, self.sender)
        self.assertEqual(sp_channels.buddy_route(self.lead, self.sender)[0], None)
        self.budget().retry_held()
        wait_for(lambda: self.replies())
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertIn("[channel #%s message %s " % (key, mid), self.content(self.replies()[0]))
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])

    def test_a_handover_between_the_hold_and_the_release_does_not_reroute(self):
        key, mid = self.hold_published()
        successor = "cc:" + new_uuid()

        def hand_over(rec):
            rec["owner"] = successor
            rec["members"] = [successor, self.sender]
            return "channel.handover", {"to": successor}

        sp_channels.change_membership(key, hand_over, self.lead)
        self.assertEqual(sp_channels.buddy_route(self.lead, self.sender)[0], None)
        self.budget().retry_held()
        wait_for(lambda: self.replies())
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertIn("[channel #%s message %s " % (key, mid), self.content(self.replies()[0]))


class TestFreeze(HeldBase):
    def test_a_frozen_sid_gets_no_new_answer_and_is_told_once(self):
        self.freeze()
        for index in range(2):
            self.shim._handle_turn_end(self.turn(text="newer %d" % index, mid="m-new%d" % index))
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.budget().held[self.sid]["text"], "held answer")
        self.assertTrue(self.budget().held[self.sid]["frozen_notified"])
        notices = self.frozen_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("budget reset", self.content(notices[0]))
        self.assertIn("budget allow", self.content(notices[0]))
        # The plain notice is sent once; every blocked request still gets its status.
        self.assertEqual(len([s for s in self.statuses() if "may or may not" in s["detail"]]), 2)

    def test_a_frozen_sid_also_blocks_an_at_name_target(self):
        self.freeze()
        sid2 = new_uuid()
        other, _rec = self.add_listener(name="cc-other", session_id=sid2, pid=os.getppid())
        self.shim.contacts[self.sid] = {
            "name": "cc-main", "socket": self.listener.path, "last_seen": sp_runtime.now_iso()}
        tag = {"from": "cc-other", "sid": sid2, "mid": "m-o", "reply": other.path}
        turn = sp_constants.Turn("t-at", "ping", tag, "complete", "@cc-main look at this")
        self.shim._handle_turn_end(turn)
        wait_for(lambda: self.replies(other))
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)  # only the frozen release itself
        self.assertEqual(len(self.replies(other)), 1)

    def test_the_freeze_survives_expiry_a_peer_change_and_eviction(self):
        entry = self.freeze()
        budget = self.budget()
        budget.expire_held(now=time.time() + 10 * budget.reply_budget_window)
        self.assertIs(budget.held.get(self.sid), entry)
        budget._reset_sequence("the requesting peer changed", time.time())
        self.assertIs(budget.held.get(self.sid), entry)
        for index in range(sp_constants.CONTACT_HISTORY + 1):
            budget.held["s%d" % index] = {"text": "x", "at": time.time()}
        self.shim._save_state()
        saved = sp_runtime.read_json(sp_storage.own_thread_state_path(self.tid))
        self.assertIn(self.sid, saved["held"])
        self.assertEqual(saved["held"][self.sid]["phase"], "unknown")
        self.assertEqual(len(saved["held"]), sp_constants.CONTACT_HISTORY + 1)
        self.assertNotIn("s0", saved["held"])

    def test_the_freeze_survives_a_restart(self):
        self.freeze()
        shim = self.restart()
        self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unknown")
        shim._handle_turn_end(self.turn(text="newer", mid="m-new"))
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])

    def test_a_reset_discards_a_frozen_entry_without_sending_it_again(self):
        self.freeze()
        self.write_reset()
        self.budget().consume_reset()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.budget().held, {})
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.shim._handle_turn_end(self.turn(text="after", mid="m-after"))
        wait_for(lambda: len(self.replies()) == 2 or None)
        self.assertEqual(len(self.sends()), 2)

    def test_an_allowance_discards_a_frozen_entry_without_sending_it_again(self):
        self.freeze()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        self.budget().consume_allowance()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.budget().held, {})
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])

    def test_an_allowance_that_already_covers_still_discards_the_frozen_entry(self):
        self.freeze()
        self.budget().allowance = {
            "sid": self.sid, "total": 10, "at": time.time(), "bound": True}
        self.write_allow(4)
        self.budget().consume_allowance()
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertNotIn(self.sid, self.budget().held)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_an_unwritten_unknown_outcome_is_appended_by_a_later_retry(self):
        def broken(key, sender, mid, recipient, outcome, floor, lock=None):
            if lock is not None:
                sp_channels.release_unfinished(lock)
            raise sp_channels.ChannelError("disk went away", 1)

        self.cap_hold()
        self.write_reset()
        with self.uncertain(), mock.patch.object(sp_channels, "finish_targeted", broken):
            self.budget().consume_reset()
        wait_for(lambda: self.replies())
        entry = self.budget().held[self.sid]
        self.assertTrue(entry["outcome_pending"])
        self.assertEqual(self.outcomes(), [])
        self.budget().retry_held()
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertNotIn("outcome_pending", entry)

    def test_a_pending_outcome_is_appended_by_the_discard(self):
        self.cap_hold()
        self.write_reset()

        def broken(key, sender, mid, recipient, outcome, floor, lock=None):
            if lock is not None:
                sp_channels.release_unfinished(lock)
            raise sp_channels.ChannelError("disk went away", 1)

        with self.uncertain(), mock.patch.object(sp_channels, "finish_targeted", broken):
            self.budget().consume_reset()
        wait_for(lambda: self.replies())
        self.assertEqual(self.outcomes(), [])
        self.write_reset()
        self.budget().consume_reset()
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertEqual(self.budget().held, {})
        self.assertEqual(len(self.replies()), 1)


class TestMarkerCrashPoints(HeldBase):
    def other_frozen(self):
        sid2 = new_uuid()
        other, _rec = self.add_listener(name="cc-other", session_id=sid2, pid=os.getppid())
        self.budget().held[sid2] = {
            "text": "x", "at": time.time(), "phase": "unknown", "channel_mid": "z"}
        return other

    def finish(self):
        wait_for(lambda: self.replies())
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(self.shim.reply_budget.budgets[self.sid], 1)

    def test_a_reset_that_crashed_before_the_save_is_redone_on_restart(self):
        self.cap_hold()
        other = self.other_frozen()
        self.shim._save_state()
        self.write_reset()
        with mock.patch.object(self.shim, "_save_state", side_effect=Crash):
            with self.assertRaises(Crash):
                self.budget().consume_reset()
        self.assertTrue(os.path.exists(sp_storage.budget_reset_path(self.tid)))
        self.restart()
        self.finish()
        self.assertEqual(other.of_type("user"), [])
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(self.tid)))

    def test_a_reset_that_crashed_before_the_unlink_is_idempotent(self):
        self.cap_hold()
        other = self.other_frozen()
        self.shim._save_state()
        self.write_reset()
        with mock.patch.object(sp_budgets.os, "unlink", side_effect=Crash):
            with self.assertRaises(Crash):
                self.budget().consume_reset()
        saved = sp_runtime.read_json(sp_storage.own_thread_state_path(self.tid))
        self.assertEqual(saved["held"][self.sid]["release"], "reset")
        self.assertEqual(list(saved["held"]), [self.sid])
        self.restart()
        self.finish()
        self.assertEqual(other.of_type("user"), [])
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(self.tid)))

    def test_an_undeletable_reset_marker_is_applied_once_not_on_every_poll(self):
        # N5: when the unlink keeps failing, later polls must not zero the budget again
        # or discard a freeze created after the reset.
        self.write_reset()
        with mock.patch.object(sp_budgets.os, "unlink", side_effect=PermissionError("read-only")):
            self.budget().consume_reset()
            self.assertTrue(os.path.exists(sp_storage.budget_reset_path(self.tid)))
            self.budget().budgets[self.sid] = 2
            sid2 = new_uuid()
            self.budget().held[sid2] = {"text": "x", "at": time.time(), "phase": "unknown", "channel_mid": "z"}
            self.budget().consume_reset()
            self.budget().consume_reset()
        self.assertEqual(self.budget().budgets[self.sid], 2)
        self.assertIn(sid2, self.budget().held)
        # Once the unlink works again the marker is removed without re-applying.
        self.budget().consume_reset()
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(self.tid)))
        self.assertEqual(self.budget().budgets[self.sid], 2)
        # A new reset (a new marker) still applies.
        self.write_reset()
        self.budget().consume_reset()
        self.assertEqual(self.budget().budgets, {})

    def arm_after_apply(self):
        armed = {"on": False}
        real_apply = sp_budgets.ReplyBudget._apply_allowance
        real_save = self.shim._save_state

        def apply(budget, grant):
            real_apply(budget, grant)
            armed["on"] = True

        def save():
            if armed["on"]:
                raise Crash()
            real_save()

        return (mock.patch.object(sp_budgets.ReplyBudget, "_apply_allowance", apply),
                mock.patch.object(self.shim, "_save_state", save))

    def test_an_allowance_that_crashed_before_the_save_is_redone_without_a_second_send(self):
        self.cap_hold()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        patch_apply, patch_save = self.arm_after_apply()
        with patch_apply, patch_save:
            with self.assertRaises(Crash):
                self.budget().consume_allowance()
        wait_for(lambda: self.replies())
        self.assertTrue(os.path.exists(sp_storage.budget_allow_path(self.tid)))
        shim = self.restart()
        shim.reply_budget.consume_allowance()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])
        self.assertEqual(shim.reply_budget.budgets[self.sid], sp_constants.REPLY_BUDGET + 1)
        self.assertNotIn(self.sid, shim.reply_budget.held)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_an_allowance_that_crashed_before_the_unlink_is_idempotent(self):
        self.cap_hold()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        with mock.patch.object(sp_budgets.os, "unlink", side_effect=Crash):
            with self.assertRaises(Crash):
                self.budget().consume_allowance()
        wait_for(lambda: self.replies())
        shim = self.restart()
        shim.reply_budget.consume_allowance()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(shim.reply_budget.budgets[self.sid], sp_constants.REPLY_BUDGET + 1)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_a_frozen_entry_is_discarded_by_a_redone_allowance_and_never_resent(self):
        self.freeze()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        patch_apply, patch_save = self.arm_after_apply()
        with patch_apply, patch_save:
            with self.assertRaises(Crash):
                self.budget().consume_allowance()
        shim = self.restart()
        # The discard was saved with the applied grant, before the apply ran.
        self.assertNotIn(self.sid, shim.reply_budget.held)
        shim.reply_budget.consume_allowance()
        shim.reply_budget.retry_held()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertNotIn(self.sid, shim.reply_budget.held)
        shim._handle_turn_end(self.turn(text="after", mid="m-after"))
        wait_for(lambda: len(self.replies()) == 2 or None)

    def test_a_replayed_allowance_keeps_the_freeze_its_own_release_created(self):
        self.cap_hold()
        self.write_allow(sp_constants.REPLY_BUDGET + 2)
        real_unlink = os.unlink
        allow_path = sp_storage.budget_allow_path(self.tid)

        def unlink(path, *args, **kwargs):
            if path == allow_path:
                raise Crash()
            return real_unlink(path, *args, **kwargs)

        with self.uncertain(), mock.patch.object(sp_budgets.os, "unlink", unlink):
            with self.assertRaises(Crash):
                self.budget().consume_allowance()
        wait_for(lambda: self.replies())
        self.assertEqual(self.budget().held[self.sid]["phase"], "unknown")
        self.assertTrue(os.path.exists(sp_storage.budget_allow_path(self.tid)))
        shim = self.restart()
        shim.reply_budget.consume_allowance()
        shim.reply_budget.retry_held()
        time.sleep(0.3)
        self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unknown")
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))
        # A different, later grant is a new decision and discards it as before.
        time.sleep(0.01)
        self.write_allow(sp_constants.REPLY_BUDGET + 3)
        shim.reply_budget.consume_allowance()
        self.assertNotIn(self.sid, shim.reply_budget.held)
        self.assertEqual(len(self.replies()), 1)


class TestHeldRecovery(HeldBase):
    def hold_phase(self, phase, published, release="reset"):
        mid = new_uuid()
        floor = self.publish(mid) if published else sp_channels.history_floor(self.key)
        self.route_entry(phase, mid, floor, release=release)
        return mid, floor

    @staticmethod
    def unreadable(*_args):
        raise sp_channels.ChannelError("unreadable", 1)

    def test_publishing_with_the_send_found_becomes_published_and_is_sent_once(self):
        mid, _floor = self.hold_phase("publishing", True)
        shim = self.restart()
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual(entry["phase"], "published")
        self.assertEqual(entry["release"], "reset")
        self.assertEqual(self.replies(), [])
        shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])

    def test_publishing_with_the_send_absent_becomes_fresh_with_the_same_mid(self):
        mid, _floor = self.hold_phase("publishing", False)
        shim = self.restart()
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual((entry["phase"], entry["channel_mid"]), ("fresh", mid))
        shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])

    def test_an_unreadable_history_blocks_transport_and_spend_until_it_is_readable(self):
        mid, _floor = self.hold_phase("publishing", False)
        with mock.patch.object(sp_channels, "find_send", self.unreadable):
            shim = self.restart()
            self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unresolved")
            for _ in range(2):
                shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.sends(), [])
        self.assertEqual(shim.reply_budget.budgets.get(self.sid, 0), 0)
        self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unresolved")
        shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertEqual(shim.reply_budget.budgets[self.sid], 1)
        self.assertEqual(shim.reply_budget.held, {})

    def test_missing_evidence_is_never_transported(self):
        self.hold_phase("publishing", True)
        missing = mock.Mock(return_value=("evidence missing", None))
        with mock.patch.object(sp_channels, "find_send", missing):
            shim = self.restart()
            for _ in range(3):
                shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unresolved")
        self.assertEqual(shim.reply_budget.budgets.get(self.sid, 0), 0)

    def test_a_published_entry_gets_one_retry(self):
        mid, _floor = self.hold_phase("published", True, release=None)
        shim = self.restart()
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual((entry["phase"], entry["release"]), ("published", "recover"))
        self.assertEqual(self.replies(), [])
        for _ in range(3):
            shim.reply_budget.retry_held()
        wait_for(lambda: self.replies())
        time.sleep(0.2)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        self.assertEqual(self.outcomes(), [{self.lead: "transport accepted"}])

    def test_a_published_entry_still_honours_the_cap(self):
        self.hold_phase("published", True, release=None)
        shim = self.restart()
        shim.reply_budget.budgets[self.sid] = sp_constants.REPLY_BUDGET
        shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])

    def test_sending_with_no_outcome_is_frozen_and_recorded_unknown(self):
        self.hold_phase("sending", True)
        shim = self.restart()
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual(entry["phase"], "unknown")
        self.assertNotIn("release", entry)
        self.assertNotIn("outcome_pending", entry)
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        for _ in range(2):
            shim.reply_budget.retry_held()
        time.sleep(0.2)
        self.assertEqual(self.replies(), [])
        self.assertEqual(shim.reply_budget.budgets.get(self.sid, 0), 0)

    def test_sending_with_an_accepted_outcome_is_spent_and_dropped(self):
        mid, floor = self.hold_phase("sending", True)
        sp_channels.finish_targeted(
            self.key, self.sender, mid, self.lead, sp_channels.OUTCOME_ACCEPTED, floor)
        shim = self.restart()
        self.assertNotIn(self.sid, shim.reply_budget.held)
        self.assertEqual(shim.reply_budget.budgets[self.sid], 1)
        self.assertEqual(self.replies(), [])

    def test_sending_with_any_other_outcome_is_dropped_without_spending(self):
        mid, floor = self.hold_phase("sending", True)
        sp_channels.finish_targeted(
            self.key, self.sender, mid, self.lead, sp_channels.OUTCOME_REFUSED + "x", floor)
        shim = self.restart()
        self.assertNotIn(self.sid, shim.reply_budget.held)
        self.assertEqual(shim.reply_budget.budgets.get(self.sid, 0), 0)

    def test_sending_with_an_unreadable_history_waits_for_the_outcome(self):
        self.hold_phase("sending", True)
        with mock.patch.object(sp_channels, "find_outcome", self.unreadable):
            shim = self.restart()
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual(entry["phase"], "unknown")
        self.assertTrue(entry["outcome_pending"])
        self.assertEqual(self.outcomes(), [])
        shim.reply_budget.retry_held()
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertNotIn("outcome_pending", entry)
        self.assertEqual(self.replies(), [])


class TestHeldDiscardOutcomes(HeldBase):
    def discard_by_expiry(self, phase, published):
        mid = new_uuid()
        floor = self.publish(mid) if published else sp_channels.history_floor(self.key)
        self.route_entry(phase, mid, floor)
        budget = self.budget()
        budget.expire_held(now=time.time() + 10 * budget.reply_budget_window)
        self.assertNotIn(self.sid, budget.held)

    def test_discarding_a_published_entry_records_a_refusal(self):
        self.discard_by_expiry("published", True)
        self.assertEqual(
            self.outcomes(),
            [{self.lead: sp_channels.OUTCOME_REFUSED + "held reply discarded (expired)"}])

    def test_discarding_an_unresolved_entry_whose_send_exists_records_a_refusal(self):
        self.discard_by_expiry("unresolved", True)
        self.assertEqual(
            self.outcomes(),
            [{self.lead: sp_channels.OUTCOME_REFUSED + "held reply discarded (expired)"}])

    def test_discarding_an_unresolved_entry_with_no_send_writes_nothing(self):
        self.discard_by_expiry("unresolved", False)
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(self.sends(), [])

    def test_an_invalid_stored_route_is_discarded_without_channel_io(self):
        mixed = "0A1B2C3D-0000-4000-8000-00000000000F"
        for field, value in (("channel", "../Bad Name"), ("channel", "#" + self.key),
                             ("channel_mid", "NOT-A-UUID"), ("channel_mid", mixed)):
            entry = self.route_entry("published", new_uuid(), 0)
            entry[field] = value
            self.budget().held[self.sid] = entry
            with mock.patch.object(sp_channels, "find_send") as find, \
                    mock.patch.object(sp_channels, "finish_targeted") as finish, \
                    mock.patch.object(sp_channels, "publish_targeted") as publish:
                self.assertEqual(self.shim._route_held(self.rec, "held answer", entry), "discarded")
                self.budget()._discard(self.sid, "test")
            for mocked in (find, finish, publish):
                mocked.assert_not_called()
            self.assertEqual(self.replies(), [])


class TestLedgerPersistPins(HeldBase):
    def crash_after_send(self):
        real = sp_claude.deliver_to_record

        def landed_then_died(rec, frame):
            real(rec, frame)
            raise Crash()

        return mock.patch.object(sp_claude, "deliver_to_record", landed_then_died)

    def assert_not_resent(self, turn):
        with self.crash_after_send():
            with self.assertRaises(Crash):
                self.shim._handle_turn_end(turn)
        wait_for(lambda: self.statuses())
        self.assertEqual(len(self.statuses()), 1)
        restarted = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        restarted.codex_version = "0.153.4"
        restarted._handle_turn_end(turn)
        time.sleep(0.3)
        self.assertEqual(len(self.statuses()), 1)
        self.assertEqual(self.replies(), [])

    def test_the_no_final_message_path_is_not_repeated_after_a_crash(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assert_not_resent(self.turn(text=None))

    def test_the_frozen_path_is_not_repeated_after_a_crash(self):
        self.freeze()
        before = len(self.statuses())
        turn = self.turn(text="newer", mid="m-new")
        with self.crash_after_send():
            with self.assertRaises(Crash):
                self.shim._handle_turn_end(turn)
        wait_for(lambda: len(self.statuses()) > before)
        self.assertEqual(len(self.statuses()), before + 1)
        restarted = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        restarted.codex_version = "0.153.4"
        restarted._handle_turn_end(turn)
        time.sleep(0.3)
        self.assertEqual(len(self.statuses()), before + 1)

    def test_the_budget_cap_path_is_not_repeated_after_a_crash(self):
        budget = self.budget()
        budget.budgets[self.sid] = sp_constants.REPLY_BUDGET
        budget.budget_sender_sid = self.sid
        budget.budget_last_at = time.time()
        self.shim._save_state()
        self.assert_not_resent(self.turn(text="over the cap", mid="m-cap"))


class TestHeldRoutingKeepsItsEntry(HeldBase):
    def test_a_full_held_map_never_evicts_the_entry_being_routed(self):
        mid = new_uuid()
        floor = self.publish(mid)
        entry = self.route_entry("fresh", mid, floor)
        budget = self.budget()
        budget.held.clear()
        budget.held[self.sid] = entry  # inserted first: the oldest, so the first to go
        for index in range(sp_constants.CONTACT_HISTORY + 1):
            budget.held["s%d" % index] = {"text": "x", "at": time.time()}
        seen = []
        real_save = self.shim._save_state

        def save(*args, **kwargs):
            real_save(*args, **kwargs)
            saved = sp_runtime.read_json(sp_storage.own_thread_state_path(self.tid))
            seen.append(self.sid in saved["held"])

        with mock.patch.object(self.shim, "_save_state", save):
            self.shim._route_held(self.rec, "held answer", entry)
        self.assertTrue(seen)
        self.assertTrue(all(seen), seen)
        for outcome in self.outcomes():
            for value in outcome.values():
                self.assertNotIn("evicted", value)


class TestSmallPins(HeldBase):
    def test_a_recovered_entry_with_an_invalid_route_is_dropped_without_channel_io(self):
        mid = new_uuid()
        entry = self.route_entry("publishing", mid, 0, release="reset")
        entry["channel"] = "#" + self.key
        self.shim._save_state()
        with mock.patch.object(sp_channels, "find_send") as find, \
                mock.patch.object(sp_channels, "find_outcome") as outcome:
            shim = self.restart(release=False)
        find.assert_not_called()
        outcome.assert_not_called()
        self.assertNotIn(self.sid, shim.reply_budget.held)
        saved = sp_runtime.read_json(sp_storage.own_thread_state_path(self.tid))
        self.assertNotIn(self.sid, saved["held"])
        self.assertEqual(self.replies(), [])

    def test_a_value_error_recording_the_outcome_is_logged_not_raised(self):
        def bad(key, sender, mid, recipient, outcome, floor, lock=None):
            sp_channels.release_unfinished(lock)
            raise ValueError("bad outcome")

        with mock.patch.object(sp_channels, "finish_targeted", bad), \
                mock.patch.object(sp_runtime, "log") as log:
            self.shim._handle_turn_end(self.turn())
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.replies()), 1)
        lines = [call.args[0] for call in log.call_args_list]
        self.assertTrue([l for l in lines if "could not record the outcome" in l and "bad outcome" in l], lines)

    def test_the_hold_log_line_escapes_a_peer_controlled_name(self):
        rec = dict(self.rec, name="evil\nname")
        turn = self.turn()
        real = sp_channels.append_history

        def flaky(key, kind, actor, data=None):
            if kind == "channel.send":
                raise sp_channels.ChannelError("disk went away", 1)
            return real(key, kind, actor, data)

        with mock.patch.object(sp_channels, "append_history", flaky), \
                mock.patch.object(sp_runtime, "log") as log:
            self.shim._deliver_target(rec, "text", turn.tag, turn)
        lines = [call.args[0] for call in log.call_args_list if "holding the reply" in call.args[0]]
        self.assertEqual(len(lines), 1, log.call_args_list)
        self.assertNotIn("\n", lines[0])
        self.assertIn("evil name", lines[0])

    def test_retry_held_skips_an_entry_discarded_earlier_in_the_loop(self):
        budget = self.budget()
        other = new_uuid()
        budget.held[self.sid] = {"text": "a", "at": time.time(), "release": "reset"}
        budget.held[other] = {"text": "b", "at": time.time(), "release": "reset"}
        released = []

        def release(sid, reason, records):
            released.append(sid)
            budget.held.pop(other, None)  # evicted while the first is released
            return "delivered"

        with mock.patch.object(budget, "_release_one", release):
            budget.retry_held()
        self.assertEqual(released, [self.sid])


class TestPollBatchCrash(RouteBase):
    def setUp(self):
        super().setUp()
        # Not this process's pid: the shim's own record file is named after it.
        self.listener, self.rec = self.add_listener(
            name="cc-main", session_id=self.sid, pid=os.getppid())

    def tagged_turn(self, number):
        turn_id = "batch-%d" % number
        tag = sp_protocol.build_tag("cc-main", self.sid, self.listener.path, "m-batch-%d" % number)
        append(self.shim.rollout_path, ev("task_started", turn_id=turn_id),
               user_item(tag + "\nping"),
               ev("task_complete", turn_id=turn_id, last_agent_message="answer %d" % number))

    def poll_once(self, shim):
        with mock.patch.object(shim.stop, "wait", side_effect=[False, True]), \
                mock.patch.object(shim, "_poll_maintenance", return_value=(0, 0)):
            shim._poll_loop()

    def test_a_crash_while_delivering_one_turn_does_not_lose_the_next_in_the_batch(self):
        self.tagged_turn(1)
        self.tagged_turn(2)

        def die(key, sender, mid, recipient, outcome, floor, lock=None):
            sp_channels.release_unfinished(lock)
            raise Crash()

        with mock.patch.object(sp_channels, "finish_targeted", die):
            with self.assertRaises(Crash):
                self.poll_once(self.shim)
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.replies()), 1)
        restarted = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        restarted.codex_version = "0.153.4"
        self.poll_once(restarted)
        wait_for(lambda: len(self.replies()) >= 2)
        time.sleep(0.3)
        texts = [self.content(f) for f in self.replies()]
        self.assertEqual(len(texts), 2)
        self.assertEqual(len([t for t in texts if "answer 1" in t]), 1)
        self.assertEqual(len([t for t in texts if "answer 2" in t]), 1)
        self.assertEqual(len(self.sends()), 2)


    def test_a_batch_longer_than_the_ledger_bound_replays_no_answer(self):
        turns = 5
        for number in range(1, turns + 1):
            self.tagged_turn(number)
        real = sp_channels.finish_targeted
        calls = {"n": 0}

        def finish(key, sender, mid, recipient, outcome, floor, lock=None):
            calls["n"] += 1
            if calls["n"] == turns:
                sp_channels.release_unfinished(lock)
                raise Crash()
            return real(key, sender, mid, recipient, outcome, floor, lock)

        with mock.patch.object(sp_constants, "PROCESSED_TURN_HISTORY", 3), \
                mock.patch.object(sp_constants, "REPLY_BUDGET", 20):
            # The shim was built before the bound was lowered.
            self.shim.processed_turns = collections.deque(self.shim.processed_turns, maxlen=3)
            with mock.patch.object(sp_channels, "finish_targeted", finish):
                with self.assertRaises(Crash):
                    self.poll_once(self.shim)
            wait_for(lambda: len(self.replies()) >= turns or None)
            restarted = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
            restarted.codex_version = "0.153.4"
            self.poll_once(restarted)
            time.sleep(0.3)
        texts = [self.content(f) for f in self.replies()]
        self.assertEqual(len(texts), turns)
        for number in range(1, turns + 1):
            self.assertEqual(len([t for t in texts if "answer %d" % number in t]), 1, number)
        self.assertEqual(len(self.sends()), turns)

    def crash_on_second_records(self):
        real = sp_claude.live_claude_records
        calls = {"n": 0}

        def records(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Crash()
            return real(*args, **kwargs)

        return mock.patch.object(sp_claude, "live_claude_records", records)

    def assert_each_answer_once(self):
        restarted = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        restarted.codex_version = "0.153.4"
        self.poll_once(restarted)
        wait_for(lambda: len(self.replies()) >= 2)
        time.sleep(0.3)
        texts = [self.content(f) for f in self.replies()]
        self.assertEqual(len([t for t in texts if "answer 1" in t]), 1)
        self.assertEqual(len([t for t in texts if "answer 2" in t]), 1)
        self.assertEqual(len(texts), 2)

    def test_a_crash_before_the_second_turn_persists_does_not_lose_it(self):
        self.tagged_turn(1)
        self.tagged_turn(2)
        with self.crash_on_second_records():
            with self.assertRaises(Crash):
                self.poll_once(self.shim)
        wait_for(lambda: self.replies())
        self.assertEqual(len(self.replies()), 1)
        self.assert_each_answer_once()

    def test_the_cleanup_save_of_a_crashed_batch_does_not_lose_the_turn(self):
        self.tagged_turn(1)
        self.tagged_turn(2)
        calls = {"n": 0}
        real = sp_claude.live_claude_records

        def records(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Crash()
            return real(*args, **kwargs)

        old = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        try:
            with mock.patch.object(sp_codex, "thread_is_held", return_value=(True, os.getpid())), \
                    mock.patch.object(self.shim.stop, "wait", side_effect=[False, True]), \
                    mock.patch.object(self.shim, "_poll_maintenance", return_value=(0, 0)), \
                    mock.patch.object(sp_claude, "live_claude_records", records):
                with self.assertRaises(Crash):
                    self.shim.run()
        finally:
            signal.signal(signal.SIGTERM, old[0])
            signal.signal(signal.SIGINT, old[1])
        self.assertEqual(self.replies(), [])
        self.assert_each_answer_once()


class TestRunRecoversHeld(HeldBase):
    def test_run_freezes_a_sending_entry_with_no_outcome_and_sends_nothing(self):
        mid = new_uuid()
        floor = self.publish(mid)
        self.route_entry("sending", mid, floor, release="reset")
        shim = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        shim.codex_version = "0.153.4"
        TestUnconditionalReload.run_shim(self, shim)
        entry = shim.reply_budget.held[self.sid]
        self.assertEqual(entry["phase"], "unknown")
        self.assertEqual(self.outcomes(), [{self.lead: "unknown"}])
        self.assertEqual(self.replies(), [])
        self.assertEqual([e["data"]["message_id"] for e in self.sends()], [mid])
        saved = sp_runtime.read_json(sp_storage.own_thread_state_path(self.tid))
        self.assertEqual(saved["held"][self.sid]["phase"], "unknown")


class TestUnconditionalReload(RouteBase):
    def run_shim(self, shim):
        seen = {}

        def poll():
            seen["contacts"] = dict(shim.contacts)
            seen["processed"] = list(shim.processed_turns)
            seen["tail"] = shim.tail

        old = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        try:
            with mock.patch.object(sp_codex, "thread_is_held", return_value=(True, os.getpid())), \
                    mock.patch.object(shim, "_poll_loop", poll):
                self.assertEqual(shim.run(), 0)
        finally:
            signal.signal(signal.SIGTERM, old[0])
            signal.signal(signal.SIGINT, old[1])
        return seen

    def test_run_takes_the_state_another_process_saved_with_the_same_updated_at(self):
        self.shim.contacts = {"a": {"name": "a"}}
        self.shim.processed_turns.append("ta")
        self.shim._save_state()
        path = sp_storage.own_thread_state_path(self.tid)
        state_a = sp_runtime.read_json(path)
        shim = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        shim.codex_version = "0.153.4"
        self.assertEqual(list(shim.contacts), ["a"])
        state_b = dict(state_a, contacts={"b": {"name": "b"}}, processed_turns=["tb"])
        self.assertEqual(state_b["updated_at"], state_a["updated_at"])
        sp_runtime.write_json_atomic(path, state_b, mode=0o600)
        seen = self.run_shim(shim)
        self.assertEqual(list(seen["contacts"]), ["b"])
        self.assertEqual(seen["processed"], ["tb"])

    def test_a_fresh_shim_keeps_its_constructor_tail_when_no_state_exists(self):
        self.assertFalse(os.path.exists(sp_storage.own_thread_state_path(self.tid)))
        tail = self.shim.tail
        seen = self.run_shim(self.shim)
        self.assertIs(seen["tail"], tail)

    def test_a_shim_that_fails_ownership_touches_neither_history_nor_state(self):
        path = sp_storage.own_thread_state_path(self.tid)
        before = sp_channels.history_floor(self.key)
        with mock.patch.object(sp_codex, "thread_is_held", return_value=(True, os.getpid())), \
                mock.patch.object(self.shim, "_acquire_ownership", return_value=False), \
                mock.patch.object(self.shim, "_reload_state") as reload_state, \
                mock.patch.object(self.shim, "_recover_held") as recover:
            self.assertEqual(self.shim.run(), 4)
        reload_state.assert_not_called()
        recover.assert_not_called()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(sp_channels.history_floor(self.key), before)
