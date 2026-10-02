"""Regression coverage for budgets."""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import io
import json
import os
import pathlib
import stat
import sys
import threading
import time
from datetime import timezone
from .support import (
    BuddyBase,
    ShimBase,
    new_uuid,
    sp_buddy,
    sp_claude,
    sp_codex,
    sp_constants,
    sp_lifecycle,
    sp_protocol,
    sp_runtime,
    sp_shim,
    sp_storage,
    wait_for,
)


class TestBudgetResetPersistence(ShimBase):
    """N4: a reset the shim never wrote down comes back on restart."""

    def test_the_reset_is_written_to_the_state_file(self):
        shim, tid, _rollout = self.make_shim()
        shim.reply_budget.budgets["s1"] = sp_constants.REPLY_BUDGET
        shim.reply_budget.budget_sender_sid = "s1"
        shim.reply_budget.budget_last_at = time.time()
        shim.reply_budget.budget_notified = {"s1"}
        shim._save_state()
        self.cli("budget", "reset", tid)
        with contextlib.redirect_stderr(io.StringIO()):
            shim.reply_budget.consume_reset()
        self.assertEqual(shim.reply_budget.budgets, {})
        self.assertIsNone(shim.reply_budget.budget_sender_sid)
        self.assertIsNone(shim.reply_budget.budget_last_at)
        self.assertEqual(shim.reply_budget.budget_notified, set())
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid))
        self.assertEqual(state["budgets"], {})
        self.assertIsNone(state["budget_sender_sid"])
        self.assertIsNone(state["budget_last_at"])
        self.assertEqual(state["budget_notified"], [])


class TestBudgetAllow(BuddyBase):
    def setUp(self):
        super().setUp()
        self.shim, self.tid, _rollout = self.make_shim()
        self.sid_a, self.sid_b = new_uuid(), new_uuid()
        self.listener_a, _ = self.add_listener(name="cc-a", session_id=self.sid_a)
        self.listener_b, _ = self.add_listener(
            name="cc-b", session_id=self.sid_b, pid=os.getppid()
        )

    def turn(self, sid, listener, turn_id, text="answer"):
        tag = {"from": "cc", "sid": sid, "mid": "m-" + turn_id, "reply": listener.path}
        return sp_constants.Turn(turn_id, "ping", tag, "complete", text)

    @staticmethod
    def replies(listener):
        return [f for f in listener.of_type("user") if f.get("from")]

    def allow(self, replies, sid=None):
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", str(replies),
            "--for-session", sid or self.sid_a,
        )
        self.assertEqual(rc, 0, err)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_allowance()

    def exhaust(self, sid, listener, count=sp_constants.REPLY_BUDGET):
        self.shim.reply_budget.budgets[sid] = count
        self.shim.reply_budget.budget_sender_sid = sid
        self.shim.reply_budget.budget_last_at = time.time()

    def test_the_cap_is_raised_for_that_session_only(self):
        self.allow(5)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 5)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_b), sp_constants.REPLY_BUDGET)
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(6):
                self.shim._handle_turn_end(self.turn(self.sid_a, self.listener_a, "t%d" % i))
        wait_for(lambda: len(self.replies(self.listener_a)) >= 5)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 5)
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])
        state = sp_runtime.read_json(sp_storage.thread_state_path(self.tid))
        self.assertEqual(state["allowance"]["total"], 5)

    def test_a_repeated_or_lower_grant_does_not_replenish(self):
        self.allow(5)
        self.exhaust(self.sid_a, self.listener_a, 5)
        self.allow(5)
        self.allow(4)
        self.assertEqual(self.shim.reply_budget.allowance["total"], 5)
        self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], 5)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(self.sid_a, self.listener_a, "t-held"))
        time.sleep(0.2)
        self.assertEqual(self.replies(self.listener_a), [])
        self.allow(8)  # higher: raises the total, usage untouched
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 8)

    def test_the_allowance_is_dropped_when_the_sequence_resets(self):
        # Granted during A's running sequence, the grant ends with it.
        for breaker in ({"sid": self.sid_b}, {}):
            self.exhaust(self.sid_a, self.listener_a)
            self.allow(5)
            self.assertTrue(self.shim.reply_budget.allowance["bound"])
            with contextlib.redirect_stderr(io.StringIO()):
                self.shim.reply_budget.advance_sequence(breaker)
            self.assertIsNone(self.shim.reply_budget.allowance, breaker)
        self.exhaust(self.sid_a, self.listener_a)
        self.allow(5)
        self.shim.reply_budget.budget_last_at = time.time() - self.shim.reply_budget.reply_budget_window - 1
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
        self.assertIsNone(self.shim.reply_budget.allowance)
        self.allow(5)
        self.cli("budget", "reset", self.tid)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_reset()
        self.assertIsNone(self.shim.reply_budget.allowance)

    def test_a_grant_made_before_the_sequence_survives_its_first_reply(self):
        self.allow(4)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 4)

    def test_a_raising_grant_releases_the_held_reply_once(self):
        self.exhaust(self.sid_a, self.listener_a)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(
                self.turn(self.sid_a, self.listener_a, "t-held", text="held answer")
            )
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])
        self.allow(5)
        frames = wait_for(lambda: self.replies(self.listener_a))
        body, _attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(body, "[held reply, in reply to message m-t-held]\nheld answer")
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], sp_constants.REPLY_BUDGET + 1)
        self.allow(6)  # raising again: nothing left to release
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 1)

    def test_a_non_raising_grant_releases_nothing(self):
        entry = {"text": "held", "mid": "m", "turn_id": "t", "at": time.time()}
        self.allow(5)
        self.exhaust(self.sid_a, self.listener_a, 5)
        self.shim.reply_budget.held = {self.sid_a: dict(entry)}
        self.allow(5)
        self.allow(sp_constants.REPLY_BUDGET, sid=self.sid_b)  # at the default: no raise
        time.sleep(0.2)
        self.assertEqual(self.replies(self.listener_a), [])
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])

    def test_a_grant_never_releases_to_a_gone_session_or_an_expired_entry(self):
        gone = new_uuid()
        self.exhaust(gone, self.listener_a)
        self.shim.reply_budget.held = {
            gone: {"text": "held", "mid": "m", "turn_id": "t", "at": time.time()}
        }
        self.allow(5, sid=gone)
        self.assertEqual(self.shim.reply_budget.held, {})
        self.exhaust(self.sid_a, self.listener_a)
        self.shim.reply_budget.held = {
            self.sid_a: {
                "text": "stale", "mid": "m", "turn_id": "t",
                "at": time.time() - self.shim.reply_budget.reply_budget_window - 1,
            }
        }
        self.allow(5)
        time.sleep(0.2)
        self.assertEqual(self.replies(self.listener_a), [])
        self.assertEqual(self.shim.reply_budget.held, {})

    def test_the_exhausted_notice_names_allow_and_reset(self):
        self.exhaust(self.sid_a, self.listener_a)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(self.sid_a, self.listener_a, "t-x"))
        notice = wait_for(
            lambda: [f for f in self.listener_a.of_type("user") if not f.get("from")]
        )
        text = notice[0]["message"]["content"]
        self.assertIn("peers.py budget allow %s --replies N" % self.tid, text)
        self.assertIn("peers.py budget reset %s" % self.tid, text)
        status = wait_for(
            lambda: self.listener_a.of_type("control", "peer_message_status")
        )
        self.assertIn("budget allow", status[0]["detail"])

    def write_marker(self, grant):
        path = sp_storage.budget_allow_path(self.tid)
        if isinstance(grant, str):
            pathlib.Path(path).write_text(grant)
        else:
            sp_runtime.write_json_atomic(path, grant, mode=0o600)
        return path

    @staticmethod
    def iso_ago(seconds):
        return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()

    def test_a_grant_consumed_by_a_late_shim_is_judged_by_its_grant_time(self):
        # The marker was written a day before any shim consumed it.
        path = self.write_marker(
            {"sid": self.sid_a, "total": sp_constants.BUDGET_ALLOW_MAX, "at": self.iso_ago(86400)}
        )
        late = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        late.codex_version = "0.153.4"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            late.reply_budget.consume_allowance()
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(late.reply_budget.allowance)
        self.assertEqual(late.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)
        self.assertIn("stale reply allowance", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(sp_constants.REPLY_BUDGET + 1):
                late._handle_turn_end(self.turn(self.sid_a, self.listener_a, "t%d" % i))
        wait_for(lambda: len(self.replies(self.listener_a)) >= sp_constants.REPLY_BUDGET)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), sp_constants.REPLY_BUDGET)

    def test_a_fresh_grant_keeps_its_original_time(self):
        granted = time.time() - 60
        self.write_marker({"sid": self.sid_a, "total": 5, "at": self.iso_ago(60)})
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_allowance()
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 5)
        self.assertAlmostEqual(self.shim.reply_budget.allowance["at"], granted, delta=2)

    def test_a_pre_sequence_grant_expires_from_its_grant_time(self):
        # Interpretation A: an up-front grant is honoured by its own first
        # reply only inside the window measured from when it was granted.
        window = self.shim.reply_budget.reply_budget_window
        self.write_marker({"sid": self.sid_a, "total": 5, "at": self.iso_ago(window - 30)})
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_allowance()
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 5)
        self.shim.reply_budget.allowance["at"] = time.time() - window - 1  # time passes
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
        self.assertIsNone(self.shim.reply_budget.allowance)

    def test_a_malformed_grant_is_discarded(self):
        for grant in (
            "not json",
            {"sid": self.sid_a, "total": 5},
            {"sid": self.sid_a, "total": 5, "at": "yesterday-ish"},
            {"sid": self.sid_a, "total": 21, "at": self.iso_ago(1)},
            {"sid": self.sid_a, "total": True, "at": self.iso_ago(1)},
            {"total": 5, "at": self.iso_ago(1)},
            {"sid": self.sid_a, "total": 5, "at": self.iso_ago(-3600)},  # future
        ):
            path = self.write_marker(grant)
            with contextlib.redirect_stderr(io.StringIO()):
                self.shim.reply_budget.consume_allowance()
            self.assertFalse(os.path.exists(path), grant)
            self.assertIsNone(self.shim.reply_budget.allowance, grant)

    def test_a_pending_stale_grant_is_not_refreshed_by_a_new_one(self):
        self.write_marker({"sid": self.sid_a, "total": 20, "at": self.iso_ago(86400)})
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "4", "--for-session", self.sid_a
        )
        self.assertEqual(rc, 0, err)
        marker = sp_runtime.read_json(sp_storage.budget_allow_path(self.tid))
        self.assertEqual(marker["total"], 4)
        self.assertLess(time.time() - sp_runtime.parse_time(marker["at"]), 60)
        # A fresh pending higher grant keeps its own time, not a refreshed one.
        self.write_marker({"sid": self.sid_a, "total": 9, "at": self.iso_ago(120)})
        self.cli("budget", "allow", self.tid, "--replies", "4", "--for-session", self.sid_a)
        marker = sp_runtime.read_json(sp_storage.budget_allow_path(self.tid))
        self.assertEqual(marker["total"], 9)
        self.assertGreater(time.time() - sp_runtime.parse_time(marker["at"]), 100)

    def test_a_grant_made_while_another_peer_holds_the_sequence_waits_for_its_own(self):
        # B has been using the shim; A is granted 5, then A's sequence starts.
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(self.sid_b, self.listener_b, "b1"))
        self.allow(5)
        self.assertFalse(self.shim.reply_budget.allowance["bound"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(self.sid_b, self.listener_b, "b2"))
            for i in range(6):
                self.shim._handle_turn_end(self.turn(self.sid_a, self.listener_a, "a%d" % i))
        wait_for(lambda: len(self.replies(self.listener_a)) >= 5)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 5)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 5)
        self.assertTrue(self.shim.reply_budget.allowance["bound"])
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])

    def run_a(self, count, prefix):
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(count):
                self.shim._handle_turn_end(
                    self.turn(self.sid_a, self.listener_a, "%s%d" % (prefix, i))
                )

    def age_sequence(self):
        """Push A's current sequence past the idle window without a turn."""
        self.shim.reply_budget.budget_last_at = time.time() - self.shim.reply_budget.reply_budget_window - 1

    def test_a_grant_after_an_idle_expiry_binds_to_the_next_sequence(self):
        self.run_a(sp_constants.REPLY_BUDGET, "old")
        wait_for(lambda: len(self.replies(self.listener_a)) >= sp_constants.REPLY_BUDGET)
        self.age_sequence()
        self.allow(5)
        self.assertFalse(self.shim.reply_budget.allowance["bound"])
        self.run_a(6, "new")
        wait_for(lambda: len(self.replies(self.listener_a)) >= sp_constants.REPLY_BUDGET + 5)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), sp_constants.REPLY_BUDGET + 5)
        self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], 5)
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])

    def test_a_fresh_grant_replaces_one_bound_to_an_expired_sequence(self):
        # A's old sequence ran under a raised allowance of 5 and went idle.
        self.run_a(1, "old")
        self.allow(5)
        self.assertTrue(self.shim.reply_budget.allowance["bound"])
        self.run_a(4, "more")
        wait_for(lambda: len(self.replies(self.listener_a)) >= 5)
        self.age_sequence()
        self.allow(5)
        self.assertFalse(self.shim.reply_budget.allowance["bound"])
        self.assertEqual(self.shim.reply_budget.budgets, {})
        self.run_a(6, "new")
        wait_for(lambda: len(self.replies(self.listener_a)) >= 10)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 10)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 5)
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])

    def test_an_expired_sequence_does_not_revive_its_own_grant(self):
        # Without a new grant, the old bound one ends with its idle sequence.
        self.run_a(1, "old")
        self.allow(5)
        self.age_sequence()
        self.run_a(1, "new")
        self.assertIsNone(self.shim.reply_budget.allowance)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)

    def test_a_spent_or_stale_waiting_grant_is_not_revived(self):
        # Spent: A's sequence ran under the grant, then ended; A comes back.
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(self.turn(self.sid_b, self.listener_b, "b1"))
        self.allow(5)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
            self.shim.reply_budget.advance_sequence({"sid": self.sid_b})
            self.assertIsNone(self.shim.reply_budget.allowance)
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)
        # Stale: granted while B held the sequence, A starts after the window.
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_b})
        self.allow(5)
        self.assertFalse(self.shim.reply_budget.allowance["bound"])
        self.shim.reply_budget.allowance["at"] = time.time() - self.shim.reply_budget.reply_budget_window - 1
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.advance_sequence({"sid": self.sid_a})
        self.assertIsNone(self.shim.reply_budget.allowance)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)

    def test_a_held_reply_survives_a_failed_release_and_is_delivered_once(self):
        self.exhaust(self.sid_a, self.listener_a)
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim._handle_turn_end(
                self.turn(self.sid_a, self.listener_a, "t-held", text="held answer")
            )
        self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])
        saved = sp_claude.deliver_to_record
        sp_claude.deliver_to_record = lambda _rec, _frame: False  # the route is down
        try:
            self.allow(5)
            self.assertEqual(sorted(self.shim.reply_budget.held), [self.sid_a])
            self.assertEqual(self.shim.reply_budget.held[self.sid_a]["release"], "allow")
            self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], sp_constants.REPLY_BUDGET)
            with contextlib.redirect_stderr(io.StringIO()):
                self.shim.reply_budget.retry_held()  # still down: kept, uncounted
            self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], sp_constants.REPLY_BUDGET)
            state = sp_runtime.read_json(sp_storage.thread_state_path(self.tid))
            self.assertEqual(state["held"][self.sid_a]["release"], "allow")
        finally:
            sp_claude.deliver_to_record = saved
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.retry_held()  # the route recovered
            self.shim.reply_budget.retry_held()
        frames = wait_for(lambda: self.replies(self.listener_a))
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 1)
        body, _attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(body, "[held reply, in reply to message m-t-held]\nheld answer")
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], sp_constants.REPLY_BUDGET + 1)

    def test_a_failed_reset_release_keeps_the_held_reply_for_a_retry(self):
        self.exhaust(self.sid_a, self.listener_a)
        self.shim.reply_budget.held = {
            self.sid_a: {"text": "held", "mid": "m", "turn_id": "t", "at": time.time()}
        }
        saved = sp_claude.deliver_to_record
        sp_claude.deliver_to_record = lambda _rec, _frame: False
        try:
            self.cli("budget", "reset", self.tid)
            with contextlib.redirect_stderr(io.StringIO()):
                self.shim.reply_budget.consume_reset()
        finally:
            sp_claude.deliver_to_record = saved
        self.assertEqual(self.shim.reply_budget.held[self.sid_a]["release"], "reset")
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.retry_held()
        self.assertTrue(wait_for(lambda: self.replies(self.listener_a)))
        self.assertEqual(self.shim.reply_budget.held, {})
        self.assertEqual(self.shim.reply_budget.budgets[self.sid_a], 1)

    def assert_allow_refused(self, state):
        # A shim keeps the code it started with. One started before `budget
        # allow` existed never reads the marker, so the grant silently did
        # nothing and the requester was held after 3 replies (field report).
        pid = self.hold_pidfile(self.tid)
        path = sp_storage.thread_state_path(self.tid)
        if state is None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        else:
            sp_runtime.write_json_atomic(path, dict(state, thread_id=self.tid))
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "20", "--for-session", self.sid_a
        )
        self.assertEqual(rc, 1)
        self.assertIn("cannot verify the running shim for %s (pid %d)" % (self.tid, pid), err)
        self.assertIn("peers.py restart %s" % self.tid, err)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_a_shim_without_the_feature_refuses_the_grant(self):
        self.assert_allow_refused({"shim_pid": os.getppid(), "budgets": {self.sid_a: 1}})

    def test_a_running_shim_with_no_state_refuses_the_grant(self):
        self.assert_allow_refused(None)

    def test_state_from_another_shim_refuses_the_grant(self):
        self.assert_allow_refused(
            {"shim_pid": os.getppid() + 1, "shim_features": ["budget_allow"]}
        )

    def test_a_current_shim_advertises_budget_allow_and_takes_the_grant(self):
        self.shim._save_state()
        state = sp_runtime.read_json(sp_storage.thread_state_path(self.tid))
        self.assertIn("budget_allow", state["shim_features"])
        pid = self.hold_pidfile(self.tid, pid=state["shim_pid"])
        rc, out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "20", "--for-session", self.sid_a
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("no shim is running", out)
        self.assertEqual(sp_runtime.read_json(sp_storage.budget_allow_path(self.tid))["total"], 20)
        self.assertEqual(pid, os.getpid())

    def test_out_of_range_replies_are_rejected(self):
        for n in ("0", "21"):
            rc, _out, err = self.cli(
                "budget", "allow", self.tid, "--replies", n, "--for-session", self.sid_a
            )
            self.assertEqual(rc, 2, n)
            self.assertIn("between 1 and %d" % sp_constants.BUDGET_ALLOW_MAX, err)
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))

    def test_the_requester_defaults_to_the_calling_claude_session(self):
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "4", "--as", "cc:%s" % self.sid_b
        )
        self.assertEqual(rc, 0, err)
        marker = sp_runtime.read_json(sp_storage.budget_allow_path(self.tid))
        self.assertEqual(marker["sid"], self.sid_b)
        self.assertEqual(stat.S_IMODE(os.stat(sp_storage.budget_allow_path(self.tid)).st_mode), 0o600)
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "4", "--as", "codex:%s" % new_uuid()
        )
        self.assertEqual(rc, 2)
        self.assertIn("--for-session", err)

    def test_buddy_set_and_clear_leave_budgets_alone(self):
        self.allow(5)
        self.exhaust(self.sid_a, self.listener_a, 5)
        self.shim.reply_budget.held = {self.sid_a: {"text": "h", "mid": "m", "turn_id": "t", "at": time.time()}}
        self.shim._save_state()
        before = pathlib.Path(sp_storage.thread_state_path(self.tid)).read_bytes()
        owner = "cc:%s" % self.sid_a
        self.assertEqual(self.buddy(owner, "set", self.tid)[0], 0)
        self.assertEqual(self.buddy(owner, "clear")[0], 0)
        self.assertEqual(pathlib.Path(sp_storage.thread_state_path(self.tid)).read_bytes(), before)
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(self.tid)))
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(self.tid)))


class TestRestartAndBuddyReplies(BuddyBase):
    """`restart` keeps the sequence; `buddy set --replies` is a finite total."""

    def setUp(self):
        super().setUp()
        self.tid, self.tid2 = self.two_threads("codex-one", "codex-two")
        self.shim = sp_shim.Shim(sp_codex.resolve_thread(self.tid))
        self.shim.codex_version = "0.153.4"
        self.sid_a, self.sid_b = new_uuid(), new_uuid()
        self.listener_a, _ = self.add_listener(name="cc-a", session_id=self.sid_a)
        self.listener_b, _ = self.add_listener(
            name="cc-b", session_id=self.sid_b, pid=os.getppid()
        )
        self.owner = "cc:%s" % self.sid_a

    def new_shim(self, tid=None):
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid or self.tid))
        shim.codex_version = "0.153.4"
        return shim

    def turn(self, sid, listener, turn_id):
        tag = {"from": "cc", "sid": sid, "mid": "m-" + turn_id, "reply": listener.path}
        return sp_constants.Turn(turn_id, "ping", tag, "complete", "answer")

    @staticmethod
    def replies(listener):
        return [f for f in listener.of_type("user") if f.get("from")]

    def deliver(self, shim, sid, listener, count, prefix="t"):
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(count):
                shim._handle_turn_end(self.turn(sid, listener, "%s%d" % (prefix, i)))

    def consume(self, shim):
        with contextlib.redirect_stderr(io.StringIO()):
            shim.reply_budget.consume_allowance()
            shim.reply_budget.consume_binding()

    def grant(self, replies):
        rc, _out, err = self.cli(
            "budget", "allow", self.tid, "--replies", str(replies),
            "--for-session", self.sid_a,
        )
        self.assertEqual(rc, 0, err)
        self.consume(self.shim)

    def prove(self):
        """Stand in for a running shim that provably reads binding allowances."""
        if not getattr(self, "_proved", False):
            self.hold_pidfile(self.tid, pid=os.getpid())
            self._proved = True
        self.shim._save_state()

    def bind(self, *extra, target=None, owner=None, prove=True):
        if prove:
            self.prove()
        return self.buddy(owner or self.owner, "set", target or "codex:%s" % self.tid, *extra)

    # -- A2: restart ----------------------------------------------------

    def test_restart_does_not_write_a_reset_or_unregister(self):
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(self.tid), {"thread_id": self.tid, "budgets": {"s": 2}}
        )
        before = pathlib.Path(sp_storage.thread_state_path(self.tid)).read_bytes()
        calls = []
        saved = (sp_lifecycle.stop_shim, sp_lifecycle.attach_thread)
        sp_lifecycle.stop_shim = lambda tid: calls.append(("stop", tid)) or True
        sp_lifecycle.attach_thread = lambda tid, verbose=True: calls.append(("start", tid)) or 4242
        try:
            rc, out, err = self.cli("restart", self.tid)
        finally:
            sp_lifecycle.stop_shim, sp_lifecycle.attach_thread = saved
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [("stop", self.tid), ("start", self.tid)])
        self.assertIn("kept", out)
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(self.tid)))
        self.assertEqual(pathlib.Path(sp_storage.thread_state_path(self.tid)).read_bytes(), before)

    def test_a_restarted_shim_keeps_a_valid_grant_and_the_replies_spent(self):
        self.grant(5)
        self.deliver(self.shim, self.sid_a, self.listener_a, 2)
        wait_for(lambda: len(self.replies(self.listener_a)) >= 2)
        restarted = self.new_shim()
        self.assertEqual(restarted.reply_budget.allowance["total"], 5)
        self.assertEqual(restarted.reply_budget.budgets[self.sid_a], 2)
        self.assertEqual(restarted.reply_budget.cap_for(self.sid_a), 5)
        self.deliver(restarted, self.sid_a, self.listener_a, 4, prefix="r")
        wait_for(lambda: len(self.replies(self.listener_a)) >= 5)
        time.sleep(0.2)
        # 2 before the restart + 3 after: the total was never replenished.
        self.assertEqual(len(self.replies(self.listener_a)), 5)
        self.assertEqual(sorted(restarted.reply_budget.held), [self.sid_a])

    def test_a_restart_does_not_resurrect_an_expired_grant(self):
        window = self.shim.reply_budget.reply_budget_window
        self.shim.reply_budget.budgets[self.sid_a] = 2
        self.shim.reply_budget.budget_sender_sid = self.sid_a
        self.shim.reply_budget.budget_last_at = time.time()
        self.grant(5)
        self.shim.reply_budget.budget_last_at = time.time() - window - 5
        self.shim._save_state()
        self.assertEqual(self.shim.reply_budget.allowance["bound"], True)
        restarted = self.new_shim()
        self.assertIsNone(restarted.reply_budget.allowance)
        self.assertEqual(restarted.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)
        # A grant that never saw its sequence expires from its grant time.
        self.shim.reply_budget.allowance = {
            "sid": self.sid_a, "total": 5, "bound": False,
            "at": time.time() - window - 5,
        }
        self.shim.reply_budget.budget_last_at = None
        self.shim._save_state()
        self.assertIsNone(self.new_shim().reply_budget.allowance)

    def test_an_explicit_reset_or_up_still_clears_the_grant(self):
        self.grant(5)
        self.shim.reply_budget.budgets[self.sid_a] = 2
        self.shim.reply_budget.budget_sender_sid = self.sid_a
        self.shim.reply_budget.budget_last_at = time.time()
        self.shim._save_state()
        restarted = self.new_shim()
        self.assertEqual(restarted.reply_budget.allowance["total"], 5)
        self.assertEqual(self.cli("budget", "reset", self.tid)[0], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            restarted.reply_budget.consume_reset()
        self.assertIsNone(restarted.reply_budget.allowance)
        self.assertEqual(restarted.reply_budget.budgets, {})
        # `up` writes the same marker, so a shim that starts after it drops it.
        self.grant(5)
        self.shim._save_state()
        saved = sp_lifecycle.reconcile
        sp_lifecycle.reconcile = lambda verbose=True: 0
        try:
            self.assertEqual(self.cli("up", self.tid)[0], 0)
        finally:
            sp_lifecycle.reconcile = saved
        self.assertTrue(os.path.exists(sp_storage.budget_reset_path(self.tid)))
        with contextlib.redirect_stderr(io.StringIO()):
            self.new_shim().reply_budget.consume_reset(initial=True)

    # -- A5: buddy set --replies ----------------------------------------

    def test_binding_without_replies_grants_the_default_total(self):
        rc, out, err = self.bind()
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")
        default = sp_constants.BUDDY_REPLIES_DEFAULT
        self.assertIn("replies left: %d of %d" % (default, default), out)
        self.assertEqual(
            json.loads(self.record_path(self.owner).read_text())["replies"], default
        )
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), default)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_b), sp_constants.REPLY_BUDGET)

    def test_the_default_total_never_attaches_a_shim_itself(self):
        calls = []
        saved = sp_lifecycle.attach_thread
        sp_lifecycle.attach_thread = lambda tid, verbose=True: calls.append(tid) or None
        try:
            rc, out, err = self.bind(prove=False)
        finally:
            sp_lifecycle.attach_thread = saved
        self.assertEqual(rc, 0, err)
        self.assertNotIn("default reply total", err)
        self.assertNotIn("replies left", out)
        # Only the status line's attach, as before; the default grant adds none.
        self.assertEqual(calls, [self.tid])
        self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))
        self.assertNotIn("replies", json.loads(self.record_path(self.owner).read_text()))

    def test_an_older_shim_takes_a_small_explicit_total_but_not_the_default(self):
        pid = os.getppid()
        self.hold_pidfile(self.tid, pid=pid)
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(self.tid),
            {"thread_id": self.tid, "shim_pid": pid,
             "shim_features": ["budget_allow", "binding_allowance"]},
        )
        rc, _out, err = self.bind("--replies", "50", prove=False)
        self.assertEqual(rc, 1)
        self.assertIn("at most %d" % sp_constants.BUDGET_ALLOW_MAX, err)
        self.assertFalse(self.record_path(self.owner).exists())
        rc, out, err = self.bind(prove=False)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: bound without the default reply total", err)
        self.assertNotIn("replies left", out)
        self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))
        rc, out, err = self.bind("--replies", str(sp_constants.BUDGET_ALLOW_MAX), prove=False)
        self.assertEqual(rc, 0, err)
        self.assertIn("replies left: %d of %d" % ((sp_constants.BUDGET_ALLOW_MAX,) * 2), out)

    def test_the_maximum_total_binds_and_a_shim_honours_it(self):
        top = sp_constants.BUDDY_REPLIES_MAX
        rc, out, err = self.bind("--replies", str(top))
        self.assertEqual(rc, 0, err)
        self.assertIn("replies left: %d of %d" % (top, top), out)
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.binding["total"], top)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), top)
        self.assertEqual(self.new_shim().reply_budget.binding["total"], top)

    def test_a_default_bind_on_a_buddy_another_owner_holds_warns_and_binds(self):
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        other = "cc:%s" % self.sid_b
        rc, out, err = self.bind(owner=other)
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: bound without the default reply total", err)
        self.assertIn("already holds a reply total", err)
        self.assertNotIn("replies", json.loads(self.record_path(other).read_text()))
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.binding["sid"], self.sid_a)
        self.assertEqual(self.shim.reply_budget.binding["total"], 6)

    def test_a_bound_total_survives_sequences_and_a_restart_without_replenishing(self):
        rc, out, err = self.bind("--replies", "6")
        self.assertEqual(rc, 0, err)
        self.assertIn("replies left: 6 of 6", out)
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), 6)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_b), sp_constants.REPLY_BUDGET)
        self.deliver(self.shim, self.sid_a, self.listener_a, 2, prefix="a")
        self.deliver(self.shim, self.sid_b, self.listener_b, 1, prefix="b")  # new sequence
        self.assertEqual(self.shim.reply_budget.binding["spent"], 2)  # B's reply spent nothing
        restarted = self.new_shim()
        self.assertEqual(restarted.reply_budget.binding["spent"], 2)
        self.deliver(restarted, self.sid_a, self.listener_a, 6, prefix="c")
        wait_for(lambda: len(self.replies(self.listener_a)) >= 6)
        time.sleep(0.2)
        self.assertEqual(len(self.replies(self.listener_a)), 6)
        self.assertEqual(restarted.reply_budget.binding["spent"], 6)
        self.assertEqual(sorted(restarted.reply_budget.held), [self.sid_a])
        # A later sequence gets only the default cap: nothing is replenished.
        restarted.reply_budget.advance_sequence({"sid": self.sid_b})
        self.assertEqual(restarted.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)
        # Re-binding the same buddy keeps the spent count and the higher total.
        for again in ("6", "3"):
            self.assertEqual(self.bind("--replies", again)[0], 0)
            self.consume(restarted)
        self.assertEqual((restarted.reply_budget.binding["total"], restarted.reply_budget.binding["spent"]), (6, 6))
        # An explicit reset and `up` leave the bound total alone.
        self.assertEqual(self.cli("budget", "reset", self.tid)[0], 0)
        with contextlib.redirect_stderr(io.StringIO()):
            restarted.reply_budget.consume_reset()
        self.assertEqual(restarted.reply_budget.binding["spent"], 6)
        rc, out, _err = self.buddy(self.owner, "show")
        self.assertIn("replies left: 0 of 6", out)

    def test_clear_revokes_the_total_even_before_the_shim_read_it(self):
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.assertEqual(self.buddy(self.owner, "clear")[0], 0)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid_a), sp_constants.REPLY_BUDGET)
        self.assertIsNone(self.new_shim().reply_budget.binding)
        # Cleared before the shim ever polled: the revoke replaces the grant.
        self.assertEqual(self.bind("--replies", "4")[0], 0)
        self.assertEqual(self.buddy(self.owner, "clear")[0], 0)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)

    def test_binding_another_buddy_revokes_and_a_new_binding_starts_fresh(self):
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.deliver(self.shim, self.sid_a, self.listener_a, 1)
        self.assertEqual(self.bind(target="codex:%s" % self.tid2)[0], 0)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)
        other = self.new_shim(self.tid2)
        self.consume(other)
        self.assertIsNone(other.reply_budget.binding)
        # Binding the first buddy again is a new binding: nothing carried over.
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.assertEqual((self.shim.reply_budget.binding["total"], self.shim.reply_budget.binding["spent"]), (6, 0))

    def test_it_is_refused_when_the_running_shim_cannot_read_it(self):
        self.hold_pidfile(self.tid)
        rc, _out, err = self.bind("--replies", "6", prove=False)
        self.assertEqual(rc, 1)
        self.assertIn("peers.py restart %s" % self.tid, err)
        self.assertFalse(self.record_path(self.owner).exists())
        self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))
        pid = os.getppid()
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(self.tid),
            {"thread_id": self.tid, "shim_pid": pid, "shim_features": ["budget_allow"]},
        )
        self.assertEqual(self.bind("--replies", "6", prove=False)[0], 1)
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(self.tid),
            {"thread_id": self.tid, "shim_pid": pid,
             "shim_features": list(sp_constants.SHIM_FEATURES)},
        )
        self.assertEqual(self.bind("--replies", "6", prove=False)[0], 0)

    def test_it_is_refused_when_no_shim_is_running_or_attachable(self):
        rc, _out, err = self.bind("--replies", "6", prove=False)
        self.assertEqual(rc, 1)
        self.assertIn("no running shim", err)
        self.assertFalse(self.record_path(self.owner).exists())
        self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))

    def test_a_second_owner_cannot_overwrite_a_shared_buddys_binding(self):
        other = "cc:%s" % self.sid_b
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.deliver(self.shim, self.sid_a, self.listener_a, 4)
        self.assertEqual(self.shim.reply_budget.binding["spent"], 4)
        marker_before = sp_storage.budget_binding_path(self.tid)
        rc, _out, err = self.bind("--replies", "5", owner=other)
        self.assertEqual(rc, 1)
        self.assertIn("already holds a reply total", err)
        self.assertFalse(os.path.exists(marker_before))
        self.assertFalse(self.record_path(other).exists())
        # The first owner repeating its binding keeps its spent count.
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.assertEqual((self.shim.reply_budget.binding["total"], self.shim.reply_budget.binding["spent"]), (6, 4))
        # Its pending revoke cannot be overwritten by the other owner either.
        self.assertEqual(self.buddy(self.owner, "clear")[0], 0)
        self.assertEqual(self.bind("--replies", "5", owner=other)[0], 1)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)
        self.assertEqual(self.bind("--replies", "5", owner=other)[0], 0)

    def run_buddy_set(self, owner, results):
        args = argparse.Namespace(
            buddy_cmd="set", as_identity=owner, target="codex:%s" % self.tid,
            uses=None, replies=5, json=False,
        )
        results[owner] = sp_buddy.cmd_buddy(args)

    def test_two_owners_binding_at_once_yield_exactly_one_binding(self):
        import threading

        self.prove()
        other = "cc:%s" % self.sid_b
        checked, go = threading.Event(), threading.Event()
        calls = []
        real = sp_buddy._binding_conflict

        def paused(owner, buddy):
            result = real(owner, buddy)
            calls.append(owner["uuid"])
            if len(calls) == 1:
                checked.set()  # the first binder has checked, not yet published
                go.wait(10)
            return result

        sp_buddy._binding_conflict = paused
        results = {}
        out, err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = io.StringIO()
        try:
            first = threading.Thread(target=self.run_buddy_set, args=(self.owner, results))
            first.start()
            self.assertTrue(checked.wait(10))
            second = threading.Thread(target=self.run_buddy_set, args=(other, results))
            second.start()
            time.sleep(0.5)  # the second binder runs its check now, or waits on the lock
            go.set()
            first.join(30)
            second.join(30)
        finally:
            sp_buddy._binding_conflict = real
            sys.stdout, sys.stderr = out, err
        self.assertEqual(sorted(results.values()), [0, 1], results)
        self.assertEqual(results[self.owner], 0)  # the paused binder published first
        marker = sp_runtime.read_json(sp_storage.budget_binding_path(self.tid))
        self.assertEqual(marker["sid"], self.sid_a)
        self.assertFalse(self.record_path(other).exists())

    def test_a_clear_paused_after_its_record_read_cannot_orphan_a_rebind(self):
        self.prove()
        shim2 = self.new_shim(self.tid2)
        self.hold_pidfile(self.tid2, pid=os.getpid())
        shim2._save_state()
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        checked, go = threading.Event(), threading.Event()
        me = []
        real = sp_buddy.read_buddy

        def paused(owner):
            result = real(owner)
            if not me:
                me.append(threading.get_ident())
                checked.set()  # clear has read the binding to thread A
                go.wait(10)
            return result

        sp_buddy.read_buddy = paused
        results = {}
        out, err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = io.StringIO()
        try:
            def clear():
                results["clear"] = sp_buddy.cmd_buddy(argparse.Namespace(
                    buddy_cmd="clear", as_identity=self.owner, json=False))

            def rebind():
                results["rebind"] = sp_buddy.cmd_buddy(argparse.Namespace(
                    buddy_cmd="set", as_identity=self.owner, json=False, uses=None,
                    target="codex:%s" % self.tid2, replies=5))

            first = threading.Thread(target=clear)
            first.start()
            self.assertTrue(checked.wait(10))
            second = threading.Thread(target=rebind)
            second.start()
            time.sleep(0.5)  # the rebind runs now, or waits on the owner lock
            go.set()
            first.join(30)
            second.join(30)
        finally:
            sp_buddy.read_buddy = real
            sys.stdout, sys.stderr = out, err
        self.assertEqual(results["clear"], 0, results)
        # Never an allowance on thread B without the owner record that can clear it.
        marker = sp_runtime.read_json(sp_storage.budget_binding_path(self.tid2))
        if self.record_path(self.owner).exists():
            record = json.loads(self.record_path(self.owner).read_text())
            self.assertEqual(record["buddy"]["uuid"], self.tid2)
            self.assertEqual(marker["bind_id"], record["bind_id"])
        else:
            self.assertIsNone(marker)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)  # A was revoked

    def fail_writes(self, suffix):
        real = sp_runtime.write_json_atomic

        def failing(path, *args, **kwargs):
            if str(path).endswith(suffix):
                raise OSError("disk full")
            return real(path, *args, **kwargs)

        sp_runtime.write_json_atomic = failing
        self.addCleanup(setattr, sp_runtime, "write_json_atomic", real)
        return lambda: setattr(sp_runtime, "write_json_atomic", real)

    def assert_revocable(self):
        """A grant, if any, has an owner record that `buddy clear` can revoke."""
        marker = sp_runtime.read_json(sp_storage.budget_binding_path(self.tid))
        if marker is not None:
            record = json.loads(self.record_path(self.owner).read_text())
            self.assertEqual(record["bind_id"], marker["bind_id"])

    def test_a_failed_record_write_leaves_no_grant_and_a_retry_keeps_spent(self):
        restore = self.fail_writes("cc-%s.json" % self.sid_a)
        rc, _out, err = self.bind("--replies", "6")
        self.assertEqual(rc, 1)
        self.assertIn("retry", err)
        self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))
        self.assertFalse(self.record_path(self.owner).exists())
        restore()
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        self.deliver(self.shim, self.sid_a, self.listener_a, 3)
        self.assertEqual(self.shim.reply_budget.binding["spent"], 3)
        # The same failure on a re-bind: nothing published, spent untouched.
        restore = self.fail_writes("cc-%s.json" % self.sid_a)
        self.assertEqual(self.bind("--replies", "6")[0], 1)
        restore()
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.binding["spent"], 3)

    def test_a_failed_grant_write_leaves_a_record_that_clear_and_a_retry_use(self):
        restore = self.fail_writes(".budget-binding")
        rc, _out, err = self.bind("--replies", "6")
        self.assertEqual(rc, 1)
        self.assertIn("retry", err)
        self.assert_revocable()
        self.assertTrue(self.record_path(self.owner).exists())
        restore()
        # A retry reuses the record's bind_id and publishes the grant.
        before = json.loads(self.record_path(self.owner).read_text())["bind_id"]
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.assertEqual(
            json.loads(self.record_path(self.owner).read_text())["bind_id"], before
        )
        self.consume(self.shim)
        self.deliver(self.shim, self.sid_a, self.listener_a, 2)
        self.assertEqual(self.shim.reply_budget.binding["spent"], 2)
        # Failing again keeps the live grant and its spent count revocable.
        restore = self.fail_writes(".budget-binding")
        self.assertEqual(self.bind("--replies", "6")[0], 1)
        restore()
        self.assert_revocable()
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.binding["spent"], 2)
        self.assertEqual(self.buddy(self.owner, "clear")[0], 0)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)

    def test_a_busy_binding_lock_fails_visibly_and_changes_nothing(self):
        self.prove()
        saved = sp_constants.BINDING_LOCK_TIMEOUT
        sp_constants.BINDING_LOCK_TIMEOUT = 0.2
        try:
            with sp_storage.binding_lock(self.tid):
                rc, _out, err = self.bind("--replies", "5")
                self.assertEqual(rc, 1)
                self.assertIn("lock", err)
                self.assertFalse(self.record_path(self.owner).exists())
                self.assertFalse(os.path.exists(sp_storage.budget_binding_path(self.tid)))
                # The shim leaves a marker it cannot lock for its next poll.
                sp_runtime.write_json_atomic(
                    sp_storage.budget_binding_path(self.tid),
                    {"sid": self.sid_a, "bind_id": "b", "total": 3, "at": sp_runtime.now_iso()},
                )
                with contextlib.redirect_stderr(io.StringIO()):
                    self.shim.reply_budget.consume_binding()
                self.assertIsNone(self.shim.reply_budget.binding)
                self.assertTrue(os.path.exists(sp_storage.budget_binding_path(self.tid)))
        finally:
            sp_constants.BINDING_LOCK_TIMEOUT = saved
        self.consume(self.shim)
        self.assertEqual(self.shim.reply_budget.binding["total"], 3)

    def test_a_failed_revoke_fails_the_command_and_keeps_the_record(self):
        self.assertEqual(self.bind("--replies", "6")[0], 0)
        self.consume(self.shim)
        real = sp_runtime.write_json_atomic

        def failing(path, *args, **kwargs):
            if str(path).endswith(".budget-binding"):
                raise OSError("disk full")
            return real(path, *args, **kwargs)

        sp_runtime.write_json_atomic = failing
        try:
            rc, out, err = self.buddy(self.owner, "clear")
            self.assertEqual(rc, 1)
            self.assertIn("still bound", err)
            self.assertNotIn("buddy cleared", out)
            self.assertTrue(self.record_path(self.owner).exists())
            rc, _out, err = self.bind(target="codex:%s" % self.tid2)
            self.assertEqual(rc, 1)
            self.assertIn("still bound", err)
            record = json.loads(self.record_path(self.owner).read_text())
            self.assertEqual(record["buddy"]["uuid"], self.tid)
        finally:
            sp_runtime.write_json_atomic = real
        self.assertIsNotNone(self.shim.reply_budget.binding)
        self.assertEqual(self.buddy(self.owner, "clear")[0], 0)
        self.consume(self.shim)
        self.assertIsNone(self.shim.reply_budget.binding)

    def test_bad_replies_are_rejected(self):
        for value in ("0", str(sp_constants.BUDDY_REPLIES_MAX + 1)):
            rc, _out, err = self.bind("--replies", value)
            self.assertEqual(rc, 2, err)
        rc, _out, err = self.bind("--replies", "5", target="cc:%s" % self.sid_b)
        self.assertEqual(rc, 2)
        self.assertIn("Codex buddy", err)
        self.assertFalse(self.record_path(self.owner).exists())
        rc, _out, err = self.buddy("codex:%s" % new_uuid(), "set", "codex:%s" % self.tid,
                                   "--replies", "5")
        self.assertEqual(rc, 2)
