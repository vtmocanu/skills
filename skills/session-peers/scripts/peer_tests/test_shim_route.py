"""A Codex shim's answers ride the pair's buddy channel when the requester leads one."""

from __future__ import annotations

import contextlib
import io
import os
import signal
import time
from unittest import mock

from .support import ShimBase, new_uuid, sp_codex, wait_for
from session_peers import budgets as sp_budgets, channels as sp_channels, claude as sp_claude
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
        self.assertEqual(len([s for s in self.statuses() if "may or may not" in s["detail"]]), 1)

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
        self.assertEqual(shim.reply_budget.held[self.sid]["phase"], "unknown")
        shim.reply_budget.consume_allowance()
        shim.reply_budget.retry_held()
        time.sleep(0.3)
        self.assertEqual(len(self.replies()), 1)
        self.assertNotIn(self.sid, shim.reply_budget.held)
        shim._handle_turn_end(self.turn(text="after", mid="m-after"))
        wait_for(lambda: len(self.replies()) == 2 or None)


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
