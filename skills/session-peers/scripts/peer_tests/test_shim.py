"""Regression coverage for shim."""

from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
from unittest import mock
import os
import pathlib
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from datetime import timezone
import uuid as uuidlib
from .support import (
    strip_origin,
    Base,
    PEERS,
    PS_LSTART,
    ShimBase,
    append,
    assistant_item,
    ev,
    sp_claude,
    sp_codex,
    sp_constants,
    sp_lifecycle,
    sp_process,
    sp_protocol,
    sp_rollout,
    sp_runtime,
    sp_shim,
    sp_storage,
    user_item,
    wait_for,
)


class TestShimInbound(ShimBase):
    def test_a_user_frame_is_queued_with_the_senders_tag(self):
        shim, tid, _rollout = self.make_shim()
        listener, rec = self.add_listener(name="cc-main", session_id="s1")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(json.dumps(self.inbound_frame("do the thing", listener.path)))
        calls = self.queue_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], tid)
        tag, body = sp_protocol.parse_tag(calls[0][4])
        self.assertEqual(tag["from"], "cc-main")
        self.assertEqual(tag["sid"], "s1")
        self.assertEqual(tag["mid"], "m-1")
        self.assertEqual(tag["reply"], listener.path)
        self.assertEqual(strip_origin(body), "do the thing")
        self.assertIn("s1", shim.contacts)
        self.assertIn("queued inbound message m-1", err.getvalue())

    def test_a_failed_queue_names_the_message_in_the_diagnostic_log(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        os.environ["FAKE_CODEX_RC"] = "1"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(json.dumps(
                self.inbound_frame("hello", listener.path, msg_id="review-123")
            ))
        self.assertIn("queue failed for message review-123", err.getvalue())

    def test_a_failed_queue_sends_the_sender_one_visible_notice(self):
        # A plain SendMessage does not track the correlated `failed` status, so
        # without this notice a queue failure is silent on the sender's side.
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        os.environ["FAKE_CODEX_RC"] = "1"
        os.environ["FAKE_CODEX_STDERR"] = "boom"
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(json.dumps(self.inbound_frame("do it", listener.path)))
        notices = wait_for(
            lambda: [f for f in listener.of_type("user") if not f.get("from")]
        )
        self.assertIsNotNone(notices)
        self.assertEqual(len(notices), 1)
        self.assertIn("was not queued", notices[0]["message"]["content"])
        self.assertIn("boom", notices[0]["message"]["content"])

    def test_an_auth_line_before_the_user_frame_is_accepted(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        shim._handle_line(json.dumps({"type": "auth", "token": "tok"}))
        shim._handle_line(json.dumps(self.inbound_frame("hi", listener.path)))
        self.assertEqual(len(self.queue_calls()), 1)

    def test_the_measured_inbound_wrapper_has_no_from_session(self):
        # M0: Claude's wrapper carried only from, from-name and from-mode. The
        # session id therefore comes from the record at the `from` socket, not
        # from the wrapper, and the reply check still has something to verify.
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        content = (
            '<cross-session-message from="uds:%s" from-name="cc-main" '
            'from-mode="prompting">\ndo the thing\n</cross-session-message>'
            % listener.path
        )
        frame = sp_protocol.build_user_frame(content, listener.path)
        shim._handle_line(json.dumps(frame))
        tag, body = sp_protocol.parse_tag(self.queue_calls()[0][4])
        self.assertEqual(strip_origin(body), "do the thing")
        self.assertEqual(tag["from"], "cc-main")
        self.assertEqual(tag["sid"], "s1")
        self.assertEqual(tag["reply"], listener.path)

    def test_a_bare_unwrapped_body_is_still_queued(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        frame = sp_protocol.build_user_frame("plain body", listener.path)
        shim._handle_line(json.dumps(frame))
        _tag, body = sp_protocol.parse_tag(self.queue_calls()[0][4])
        self.assertEqual(strip_origin(body), "plain body")

    def test_a_body_over_the_cap_is_truncated_not_dropped(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        big = "x" * (sp_constants.MAX_TEXT_CHARS + 10)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(json.dumps(self.inbound_frame(big, listener.path)))
        queued = self.queue_calls()[0][4]
        self.assertLessEqual(len(queued), sp_runtime.argv_text_budget())
        _tag, body = sp_protocol.parse_tag(queued)
        body = strip_origin(body)
        self.assertTrue(body.startswith("x"))
        self.assertLess(len(body), len(big))
        self.assertIn("truncating", err.getvalue())

    def test_an_empty_body_is_ignored(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(json.dumps(self.inbound_frame("   ", listener.path)))
        self.assertEqual(self.queue_calls(), [])

    def test_a_non_json_line_is_ignored(self):
        shim, _tid, _rollout = self.make_shim()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line("not json at all")
        self.assertEqual(self.queue_calls(), [])

    def test_a_reply_address_outside_the_allowlist_is_dropped_from_the_tag(self):
        shim, _tid, _rollout = self.make_shim()
        outside = str(self.root / "evil.sock")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(json.dumps(self.inbound_frame("hi", outside)))
        self.assertIn("outside the allowlisted", err.getvalue())
        tag, _body = sp_protocol.parse_tag(self.queue_calls()[0][4])
        self.assertIsNone(tag["reply"])

    def test_a_paused_thread_queues_and_tells_the_sender_it_is_held(self):
        shim, _tid, rollout = self.make_shim()
        listener, _rec = self.add_listener()
        # The interrupt arrives while the shim runs, so the tail learns it on
        # its next poll; that cached answer is what the inbound path reads (N6).
        append(rollout, ev("task_started", turn_id="t9"), ev("turn_aborted", turn_id="t9"))
        self.assertEqual(shim.tail.last_boundary, "complete")
        shim.tail.poll()
        self.assertEqual(shim.tail.last_boundary, "aborted")
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(json.dumps(self.inbound_frame("hi", listener.path)))
        self.assertEqual(len(self.queue_calls()), 1)
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertIsNotNone(status, "no peer_message_status arrived")
        self.assertEqual(status[0]["status"], "held")
        self.assertEqual(status[0]["orig_msg_id"], "m-1")
        self.assertIn("interrupt", status[0]["detail"])

    def test_a_dead_thread_refuses_to_queue_and_stops_the_shim(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        self.clear_holders()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(json.dumps(self.inbound_frame("hi", listener.path)))
        self.assertEqual(self.queue_calls(), [])
        self.assertTrue(shim.stop.is_set())
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertEqual(status[0]["status"], "failed")

    def test_an_unverified_thread_refuses_to_queue_but_keeps_the_shim(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        os.environ["FAKE_LSOF_RC"] = "126"
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(
                json.dumps(self.inbound_frame("hi", listener.path))
            )
        self.assertEqual(self.queue_calls(), [])
        self.assertFalse(shim.stop.is_set())
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertEqual(status[0]["status"], "failed")
        self.assertIn("liveness probe", status[0]["detail"])

    def test_a_client_with_a_foreign_uid_is_refused(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        original = sp_process.peer_uid
        sp_process.peer_uid = lambda _conn: os.getuid() + 1
        try:
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            b.sendall(
                json.dumps(self.inbound_frame("hi", listener.path)).encode() + b"\n"
            )
            b.close()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                shim._handle_connection(a)
        finally:
            sp_process.peer_uid = original
        self.assertIn("refusing a client", err.getvalue())
        self.assertEqual(self.queue_calls(), [])

    def test_a_client_with_our_uid_is_served(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        original = sp_process.peer_uid
        sp_process.peer_uid = lambda _conn: os.getuid()
        try:
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            b.sendall(
                json.dumps(self.inbound_frame("hi", listener.path)).encode() + b"\n"
            )
            b.close()
            shim._handle_connection(a)
        finally:
            sp_process.peer_uid = original
        self.assertEqual(len(self.queue_calls()), 1)

    def test_an_unknown_control_action_is_ignored(self):
        shim, _tid, _rollout = self.make_shim()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(json.dumps({"type": "control", "action": "rename",
                                          "name": "hijack"}))
        self.assertIn("ignoring control action", err.getvalue())
        self.assertEqual(shim.name, "codex-uzi")


class TestShimReplies(ShimBase):
    def _turn(self, tid_socket, text="the answer", sid="s1", outcome="complete",
              turn_id="t1", msg_id="m-1"):
        tag = {
            "from": "cc-main",
            "sid": sid,
            "mid": msg_id,
            "reply": tid_socket,
        }
        return sp_constants.Turn(turn_id, "ping", tag, outcome,
                          text if outcome == "complete" else None)

    @staticmethod
    def _reply_frames(listener):
        # A delivered reply carries a `from` reply route; the budget-drop notice
        # (build_user_frame(body, None)) deliberately has none. Counting frames
        # with `from` isolates real replies from the notice.
        return [f for f in listener.of_type("user") if f.get("from")]

    @staticmethod
    def _notice_frames(listener):
        return [f for f in listener.of_type("user") if not f.get("from")]

    def test_a_bridged_turn_is_answered_back_to_its_sender(self):
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim._handle_turn_end(self._turn(listener.path))
        frames = wait_for(lambda: listener.of_type("user"))
        self.assertEqual(len(frames), 1)
        body, attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(strip_origin(body), "[in reply to message m-1]\nthe answer")
        self.assertEqual(attrs["from-name"], "codex-uzi")
        self.assertEqual(attrs["from-session"], tid)
        self.assertEqual(frames[0]["from"], "uds:%s" % shim.sock_path)

    def test_the_same_turn_is_never_delivered_twice(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s1")
        turn = self._turn(listener.path)
        shim._handle_turn_end(turn)
        wait_for(lambda: listener.of_type("user"))
        shim._handle_turn_end(turn)
        time.sleep(0.2)
        self.assertEqual(len(listener.of_type("user")), 1)

    def test_an_aborted_turn_delivers_nothing(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s1")
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(self._turn(listener.path, outcome="aborted"))
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])

    def test_a_null_completion_sends_the_requester_one_nonreplyable_notice(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s1")
        turn = sp_constants.Turn("t1", "ping",
                          {"from": "cc-main", "sid": "s1", "mid": "m-9",
                           "reply": listener.path},
                          "complete", None)
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(turn)
        wait_for(lambda: self._notice_frames(listener))
        time.sleep(0.2)
        self.assertEqual(self._reply_frames(listener), [])
        notices = self._notice_frames(listener)
        self.assertEqual(len(notices), 1)
        body, _attrs = sp_protocol.unwrap_message(notices[0]["message"]["content"])
        self.assertIn("no final message", body)
        self.assertIn("m-9", body)
        statuses = [f for f in listener.of_type("control")
                    if f.get("action") == "peer_message_status"]
        self.assertEqual([(s["orig_msg_id"], s["status"]) for s in statuses],
                         [("m-9", "failed")])

    def test_a_null_completion_from_an_unverified_tag_stays_silent(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s-new")
        turn = sp_constants.Turn("t1", "ping",
                          {"from": "cc-main", "sid": "s-old", "reply": listener.path},
                          "complete", None)
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(turn)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])

    def test_a_verified_requester_becomes_a_contact(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        self.assertNotIn("s1", shim.contacts)
        shim._handle_turn_end(self._turn(listener.path))
        wait_for(lambda: listener.of_type("user"))
        self.assertIn("s1", shim.contacts)
        # A later untagged turn (a direct Codex turn) may now address it.
        shim._handle_turn_end(
            sp_constants.Turn("t2", "ping", None, "complete", "@cc-main follow-up"))
        wait_for(lambda: len(self._reply_frames(listener)) == 2 or None)
        self.assertEqual(len(self._reply_frames(listener)), 2)

    def test_an_at_name_to_the_reply_target_logs_no_false_drop(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        tag = {"from": "cc-main", "sid": "s1", "reply": listener.path}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(
                sp_constants.Turn("t1", "ping", tag, "complete", "@cc-main done"))
            wait_for(lambda: listener.of_type("user"))
        self.assertNotIn("unsolicited", err.getvalue())

    def test_a_session_id_that_changed_under_the_socket_is_refused(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s-new")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(self._turn(listener.path, sid="s-old"))
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertIn("session id at", err.getvalue())

    def test_a_sender_that_has_exited_is_reported_not_retried(self):
        shim, _tid, _rollout = self.make_shim()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(self._turn(str(self.socks / "gone.sock")))
        self.assertIn("is gone", err.getvalue())

    def test_an_at_name_reply_needs_prior_contact(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-other", session_id="s2")
        turn = sp_constants.Turn("t1", "ping", None, "complete", "@cc-other here you go")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(turn)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertIn("unsolicited", err.getvalue())

    def test_an_at_name_reply_is_delivered_after_prior_contact(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-other", session_id="s2")
        shim.contacts["s2"] = {"name": "cc-other", "socket": listener.path}
        turn = sp_constants.Turn("t1", "ping", None, "complete", "@cc-other here you go")
        shim._handle_turn_end(turn)
        frames = wait_for(lambda: listener.of_type("user"))
        self.assertEqual(len(frames), 1)

    def test_the_unsolicited_override_lifts_the_contact_rule(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-other", session_id="s2")
        os.environ["SESSION_PEERS_ALLOW_UNSOLICITED"] = "1"
        turn = sp_constants.Turn("t1", "ping", None, "complete", "@cc-other hello")
        shim._handle_turn_end(turn)
        self.assertIsNotNone(wait_for(lambda: listener.of_type("user")))

    def test_a_reply_addressed_to_its_own_sender_is_delivered_once(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        tag = {"from": "cc-main", "sid": "s1", "reply": listener.path}
        turn = sp_constants.Turn("t1", "ping", tag, "complete", "@cc-main done")
        shim._handle_turn_end(turn)
        wait_for(lambda: listener.of_type("user"))
        time.sleep(0.2)
        self.assertEqual(len(listener.of_type("user")), 1)
        self.assertEqual(shim.reply_budget.budgets["s1"], 1)

    def test_the_reply_budget_stops_a_ping_pong(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for i in range(sp_constants.REPLY_BUDGET + 2):
                shim._handle_turn_end(self._turn(listener.path, turn_id="t%d" % i))
                time.sleep(0.05)
        wait_for(lambda: len(self._reply_frames(listener)) >= sp_constants.REPLY_BUDGET)
        time.sleep(0.2)
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET)
        self.assertIn("reply budget", err.getvalue())

    def test_the_fourth_reply_notifies_the_requesting_peer(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        for i in range(sp_constants.REPLY_BUDGET + 1):
            shim._handle_turn_end(
                self._turn(
                    listener.path,
                    turn_id="t%d" % i,
                    msg_id="m%d" % i,
                )
            )
        statuses = wait_for(
            lambda: listener.of_type("control", "peer_message_status")
        )
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET)
        self.assertEqual(statuses[-1]["status"], "failed")
        self.assertEqual(statuses[-1]["orig_msg_id"], "m3")
        self.assertIn("loop guard", statuses[-1]["detail"])

    def test_a_dropped_reply_sends_one_nonreplyable_notice(self):
        # The correlated status frame above is surfaced only when the peer is
        # tracking the originating message; an ordinary SendMessage-style send is
        # not, so a visible, non-replyable notice also fires on the drop. Distinct
        # msg ids here double as the guard-intact regression: same sid, fresh
        # inbound each turn, the 4th consecutive reply still dropped.
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(sp_constants.REPLY_BUDGET + 1):
                shim._handle_turn_end(
                    self._turn(listener.path, turn_id="t%d" % i, msg_id="m%d" % i)
                )
        # Synchronize on BOTH the delivered replies and the notice before
        # counting, so neither assertion races a frame still in flight.
        notices = wait_for(
            lambda: self._notice_frames(listener)
            if len(self._reply_frames(listener)) >= sp_constants.REPLY_BUDGET
            else None
        )
        self.assertIsNotNone(notices)
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET)
        self.assertEqual(len(notices), 1)
        self.assertNotIn("from", notices[0])  # no reply route: cannot loop back
        self.assertIn("budget reset", notices[0]["message"]["content"])
        self.assertTrue(
            wait_for(lambda: listener.of_type("control", "peer_message_status"))
        )

    def test_the_drop_notice_fires_once_per_sequence(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(sp_constants.REPLY_BUDGET + 3):  # several drops, one sequence
                shim._handle_turn_end(
                    self._turn(listener.path, turn_id="t%d" % i, msg_id="m%d" % i)
                )
                time.sleep(0.02)
        wait_for(lambda: self._notice_frames(listener))
        time.sleep(0.2)
        self.assertEqual(len(self._notice_frames(listener)), 1)

    def test_a_sequence_reset_clears_the_drop_notice_flag(self):
        shim, _tid, _rollout = self.make_shim()
        shim.reply_budget.budgets = {"s1": sp_constants.REPLY_BUDGET}
        shim.reply_budget.budget_notified = {"s1"}
        shim.reply_budget.budget_sender_sid = "s1"
        shim.reply_budget.budget_last_at = time.time()
        shim.reply_budget.advance_sequence({"sid": "s2"})  # an intervening peer
        self.assertEqual(shim.reply_budget.budgets, {})
        self.assertEqual(shim.reply_budget.budget_notified, set())

    def test_the_budget_counts_an_at_name_reply_too(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-other", session_id="s2")
        shim.contacts["s2"] = {"name": "cc-other", "socket": listener.path}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for i in range(sp_constants.REPLY_BUDGET + 1):
                tag = {
                    "from": "cc-other",
                    "sid": "s2",
                    "mid": "m%d" % i,
                    "reply": listener.path,
                }
                shim._handle_turn_end(
                    sp_constants.Turn(
                        "t%d" % i,
                        "p",
                        tag,
                        "complete",
                        "@cc-other again",
                    )
                )
                time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET)
        self.assertIn("reply budget", err.getvalue())

    def test_the_budget_marker_clears_the_counter(self):
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.budgets["s1"] = sp_constants.REPLY_BUDGET
        shim.reply_budget.budget_sender_sid = "s1"
        shim.reply_budget.budget_last_at = time.time()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(self._turn(listener.path, turn_id="t-blocked"))
        time.sleep(0.2)
        self.assertEqual(self._reply_frames(listener), [])  # a notice may appear
        self.cli("budget", "reset", tid)
        shim.reply_budget.consume_reset()
        shim._handle_turn_end(self._turn(listener.path, turn_id="t-after"))
        self.assertTrue(wait_for(lambda: self._reply_frames(listener)))

    def test_a_reply_without_a_message_id_has_no_header(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim._handle_turn_end(self._turn(listener.path, msg_id=None))
        frames = wait_for(lambda: self._reply_frames(listener))
        body, _attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(strip_origin(body), "the answer")

    def test_a_budget_dropped_reply_is_held_and_released_by_a_reset(self):
        # The guard still stops the 4th consecutive reply (same sid, fresh
        # inbound each turn), but the reply is HELD, not lost: an explicit
        # `budget reset` releases exactly the latest one, correlated to its
        # request and marked as held, and it opens the new sequence.
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(sp_constants.REPLY_BUDGET + 2):
                shim._handle_turn_end(
                    self._turn(
                        listener.path,
                        turn_id="t%d" % i,
                        msg_id="m%d" % i,
                        text="answer %d" % i,
                    )
                )
        wait_for(lambda: len(self._reply_frames(listener)) >= sp_constants.REPLY_BUDGET)
        time.sleep(0.2)
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET)
        self.assertEqual(sorted(shim.reply_budget.held), ["s1"])
        # Persisted, so a shim restart before the reset does not lose it.
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid), {})
        self.assertEqual(state["held"]["s1"]["mid"], "m%d" % (sp_constants.REPLY_BUDGET + 1))
        self.cli("budget", "reset", tid)
        with contextlib.redirect_stderr(io.StringIO()):
            shim.reply_budget.consume_reset()
        frames = wait_for(
            lambda: self._reply_frames(listener)
            if len(self._reply_frames(listener)) > sp_constants.REPLY_BUDGET
            else None
        )
        self.assertIsNotNone(frames)
        time.sleep(0.2)
        self.assertEqual(len(self._reply_frames(listener)), sp_constants.REPLY_BUDGET + 1)
        body, _attrs = sp_protocol.unwrap_message(frames[-1]["message"]["content"])
        last = sp_constants.REPLY_BUDGET + 1
        self.assertEqual(
            strip_origin(body), "[held reply, in reply to message m%d]\nanswer %d" % (last, last)
        )
        self.assertEqual(shim.reply_budget.held, {})
        self.assertEqual(shim.reply_budget.budgets["s1"], 1)  # the released reply opens the sequence

    def test_a_held_reply_is_discarded_when_the_sequence_moves_on(self):
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.budgets = {"s1": sp_constants.REPLY_BUDGET}
        shim.reply_budget.budget_sender_sid = "s1"
        shim.reply_budget.budget_last_at = time.time()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(self._turn(listener.path, turn_id="t-held"))
            self.assertEqual(sorted(shim.reply_budget.held), ["s1"])
            shim.reply_budget.advance_sequence({"sid": "s2"})  # an intervening peer
            self.assertEqual(shim.reply_budget.held, {})
            self.cli("budget", "reset", tid)
            shim.reply_budget.consume_reset()
        time.sleep(0.2)
        self.assertEqual(self._reply_frames(listener), [])

    def test_a_stale_held_reply_is_not_released(self):
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.held = {
            "s1": {
                "text": "old answer",
                "mid": "m-old",
                "turn_id": "t-old",
                "at": time.time() - shim.reply_budget.reply_budget_window - 1,
            }
        }
        self.cli("budget", "reset", tid)
        with contextlib.redirect_stderr(io.StringIO()):
            shim.reply_budget.consume_reset()
        time.sleep(0.2)
        self.assertEqual(self._reply_frames(listener), [])
        self.assertEqual(shim.reply_budget.held, {})

    def test_a_startup_reset_defers_the_held_release(self):
        # At startup the shim is not yet bound or registered, so a release then
        # would carry a `from` route to a socket that does not exist yet.
        shim, tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.held = {
            "s1": {"text": "held answer", "mid": "m-h", "turn_id": "t-h", "at": time.time()}
        }
        self.cli("budget", "reset", tid)
        shim.reply_budget.consume_reset(initial=True)
        time.sleep(0.2)
        self.assertEqual(self._reply_frames(listener), [])
        self.assertEqual(sorted(shim.reply_budget.held), ["s1"])
        self.assertTrue(shim.reply_budget.release_after_start)

    def test_an_expired_held_reply_is_purged_from_the_state_file(self):
        shim, tid, _rollout = self.make_shim()
        shim.reply_budget.held = {
            "s1": {
                "text": "secret-ish answer",
                "mid": "m-x",
                "turn_id": "t-x",
                "at": time.time() - shim.reply_budget.reply_budget_window - 1,
            }
        }
        shim._save_state()
        self.assertIn(
            "secret-ish answer", pathlib.Path(sp_storage.thread_state_path(tid)).read_text()
        )
        with contextlib.redirect_stderr(io.StringIO()):
            shim.reply_budget.expire_held()
        self.assertEqual(shim.reply_budget.held, {})
        self.assertNotIn(
            "secret-ish answer", pathlib.Path(sp_storage.thread_state_path(tid)).read_text()
        )

    def test_a_fresh_held_reply_survives_the_purge(self):
        shim, _tid, _rollout = self.make_shim()
        shim.reply_budget.held = {"s1": {"text": "a", "mid": "m", "turn_id": "t", "at": time.time()}}
        shim.reply_budget.expire_held()
        self.assertEqual(sorted(shim.reply_budget.held), ["s1"])

    def test_a_legacy_lifetime_counter_starts_a_fresh_sequence(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _ = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.budgets["s1"] = sp_constants.REPLY_BUDGET
        self.assertIsNone(shim.reply_budget.budget_sender_sid)
        shim._handle_turn_end(self._turn(listener.path, turn_id="new-sequence"))
        self.assertIsNotNone(wait_for(lambda: listener.of_type("user")))
        self.assertEqual(shim.reply_budget.budgets["s1"], 1)

    def test_an_intervening_peer_resets_the_consecutive_budget(self):
        shim, _tid, _rollout = self.make_shim()
        first, _ = self.add_listener(name="cc-first", session_id="s1")
        second, _ = self.add_listener(
            name="cc-second", session_id="s2", pid=os.getppid()
        )
        for i in range(sp_constants.REPLY_BUDGET):
            shim._handle_turn_end(
                self._turn(first.path, sid="s1", turn_id="a%d" % i)
            )
        shim._handle_turn_end(
            self._turn(second.path, sid="s2", turn_id="other")
        )
        shim._handle_turn_end(
            self._turn(first.path, sid="s1", turn_id="after")
        )
        self.assertTrue(wait_for(lambda: len(first.of_type("user")) == 4))

    def test_a_direct_codex_turn_resets_the_consecutive_budget(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _ = self.add_listener(name="cc-main", session_id="s1")
        for i in range(sp_constants.REPLY_BUDGET):
            shim._handle_turn_end(
                self._turn(listener.path, turn_id="a%d" % i)
            )
        shim._handle_turn_end(
            sp_constants.Turn("direct", "typed", None, "complete", "local answer")
        )
        shim._handle_turn_end(self._turn(listener.path, turn_id="after"))
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 4))

    def test_the_budget_resets_after_the_idle_window(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _ = self.add_listener(name="cc-main", session_id="s1")
        for i in range(sp_constants.REPLY_BUDGET):
            shim._handle_turn_end(
                self._turn(listener.path, turn_id="a%d" % i)
            )
        shim.reply_budget.budget_last_at = time.time() - shim.reply_budget.reply_budget_window - 1
        shim._handle_turn_end(self._turn(listener.path, turn_id="after"))
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 4))

    def test_a_reply_carrying_a_tag_line_has_it_stripped(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        echoed = sp_protocol.build_tag("cc-main", "s1", listener.path) + "\nthe answer"
        shim._handle_turn_end(self._turn(listener.path, text=echoed))
        frames = wait_for(lambda: listener.of_type("user"))
        body, _attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(strip_origin(body), "[in reply to message m-1]\nthe answer")


class TestStatusFrameShape(ShimBase):
    """The status frame must correlate the way the measured shape does."""

    def test_the_status_frame_correlates_on_orig_msg_id(self):
        # M6: nothing rendered in the sending session while the key was
        # `msg_id`, which is what an uncorrelatable status frame looks like.
        shim, _tid, rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        append(rollout, ev("task_started", turn_id="t9"), ev("turn_aborted", turn_id="t9"))
        shim.tail.poll()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(
                json.dumps(self.inbound_frame("hi", listener.path, msg_id="abc-123"))
            )
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertIsNotNone(status)
        frame = status[0]
        self.assertEqual(
            sorted(frame),
            ["action", "detail", "from", "orig_msg_id", "status", "type"],
        )
        self.assertEqual(frame["orig_msg_id"], "abc-123")
        self.assertNotIn("msg_id", frame)
        self.assertEqual(frame["from"], "uds:%s" % shim.sock_path)

    def test_every_status_value_uses_the_same_correlation_key(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        self.clear_holders()
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(
                json.dumps(self.inbound_frame("hi", listener.path, msg_id="dead-1"))
            )
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertEqual(status[0]["status"], "failed")
        self.assertEqual(status[0]["orig_msg_id"], "dead-1")


class TestDeliveryLog(ShimBase):
    """The shim logged every drop but no success, so nothing showed the wins."""

    def test_a_delivery_is_logged_by_turn_and_session_name(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        turn = sp_constants.Turn(
            "t-42", "ping",
            {"from": "cc-main", "sid": "s1", "reply": listener.path},
            "complete", "the secret answer", None,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(turn)
        wait_for(lambda: listener.of_type("user"))
        text = err.getvalue()
        self.assertIn("delivered turn t-42 to cc-main", text)
        self.assertNotIn("the secret answer", text)

    def test_a_dropped_reply_is_not_logged_as_delivered(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.reply_budget.budgets["s1"] = sp_constants.REPLY_BUDGET
        shim.reply_budget.budget_sender_sid = "s1"
        shim.reply_budget.budget_last_at = time.time()
        turn = sp_constants.Turn(
            "t-43", "ping",
            {
                "from": "cc-main",
                "sid": "s1",
                "mid": "m-43",
                "reply": listener.path,
            },
            "complete", "answer", None,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(turn)
        self.assertNotIn("delivered turn", err.getvalue())
        self.assertIn("reply budget", err.getvalue())


class TestShimIdleNotice(ShimBase):
    def test_notify_when_idle_answers_immediately_when_idle(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        shim._handle_line(
            json.dumps({"type": "control", "action": "notify_when_idle",
                        "msg_id": "sub-1", "from": "uds:%s" % listener.path})
        )
        notices = wait_for(lambda: listener.of_type("control", "peer_idle_notice"))
        self.assertEqual(notices[0]["orig_msg_id"], "sub-1")
        self.assertEqual(notices[0]["state"], "idle")
        self.assertIsInstance(notices[0]["finished_at"], int)

    def test_the_measured_notify_when_idle_frame_is_answered_exactly(self):
        # The frame and the answer are M0 measurements, not a guess: Claude
        # sends from_mode and msgV alongside from/msg_id, and only subscribes
        # at all when the record advertises peerFeatures ["notify_idle"].
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        self.assertEqual(shim.record()["peerFeatures"], ["notify_idle"])
        shim._handle_line(json.dumps({
            "type": "control",
            "action": "notify_when_idle",
            "from": "uds:%s" % listener.path,
            "from_mode": "prompting",
            "msgV": 1,
            "msg_id": "11111111-2222-3333-4444-555555555555",
        }))
        notices = wait_for(lambda: listener.of_type("control", "peer_idle_notice"))
        self.assertIsNotNone(notices, "no peer_idle_notice arrived")
        notice = notices[0]
        self.assertEqual(
            sorted(notice),
            ["action", "detail", "finished_at", "orig_msg_id", "state", "type"],
        )
        self.assertEqual(notice["orig_msg_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(notice["state"], "idle")
        self.assertIsInstance(notice["detail"], str)

    def test_notify_when_idle_fires_once_at_the_end_of_a_busy_turn(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(session_id="s1")
        shim.status = "busy"
        shim._handle_line(
            json.dumps({"type": "control", "action": "notify_when_idle",
                        "msg_id": "sub-2", "from": "uds:%s" % listener.path})
        )
        self.assertEqual(listener.of_type("control", "peer_idle_notice"), [])
        shim._handle_turn_end(
            sp_constants.Turn("t1", "ping", None, "complete", None)
        )
        notices = wait_for(lambda: listener.of_type("control", "peer_idle_notice"))
        self.assertEqual(len(notices), 1)
        shim._handle_turn_end(sp_constants.Turn("t2", "ping", None, "complete", None))
        time.sleep(0.2)
        self.assertEqual(len(listener.of_type("control", "peer_idle_notice")), 1)

    def test_a_queued_message_goes_busy_so_an_immediate_notify_waits(self):
        # A lock-only fresh thread starts idle with no rollout. Queueing a turn
        # must flip the shim busy, or a notify_when_idle arriving with the
        # message fires against the start-time idle instead of the turn's end.
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener()
        self.assertEqual(shim.status, "idle")
        shim._handle_line(json.dumps(self.inbound_frame("do it", listener.path)))
        self.assertEqual(len(self.queue_calls()), 1)
        self.assertEqual(shim.status, "busy")
        shim._handle_line(
            json.dumps({"type": "control", "action": "notify_when_idle",
                        "msg_id": "sub-x", "from": "uds:%s" % listener.path})
        )
        time.sleep(0.2)
        self.assertEqual(listener.of_type("control", "peer_idle_notice"), [])
        shim._handle_turn_end(sp_constants.Turn("t-x", "ping", None, "complete", None))
        notices = wait_for(lambda: listener.of_type("control", "peer_idle_notice"))
        self.assertEqual(len(notices), 1)

    def test_an_idle_subscription_to_a_bad_socket_is_dropped(self):
        shim, _tid, _rollout = self.make_shim()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_line(
                json.dumps({"type": "control", "action": "notify_when_idle",
                            "msg_id": "s", "from": "uds:%s" % (self.root / "x.sock")})
            )
        self.assertIn("not an allowed socket", err.getvalue())


class TestShimRecord(ShimBase):
    def test_the_record_is_the_shape_claude_accepts(self):
        shim, tid, _rollout = self.make_shim()
        rec = shim.record()
        self.assertEqual(rec["entrypoint"], "codex")
        self.assertEqual(rec["kind"], "interactive")
        self.assertEqual(rec["peerProtocol"], 1)
        self.assertEqual(rec["peerFeatures"], ["notify_idle"])
        self.assertEqual(rec["nameSource"], "user")
        self.assertEqual(rec["sessionId"], tid)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["pidDomain"], sys.platform)
        self.assertEqual(rec["procStart"], PS_LSTART)
        self.assertTrue(rec["version"].startswith("codex-"))
        self.assertEqual(rec["messagingSocketPath"], shim.sock_path)

    def test_a_thread_with_no_name_gets_a_stable_fallback(self):
        tid, rollout = self.one_thread(name=None)
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
        self.assertEqual(shim.name, "codex-%s" % tid[:8])

    def test_an_unusable_thread_title_gets_a_stable_fallback(self):
        shim, tid, _rollout = self.make_shim(name="Review PR 12")
        self.assertEqual(shim.name, "codex-%s" % tid[:8])

    def test_a_conflicting_thread_title_gets_a_stable_fallback(self):
        self.add_listener(name="codex-uzi", pid=os.getppid())
        shim, tid, _rollout = self.make_shim(name="codex-uzi")
        self.assertEqual(shim.name, "codex-%s" % tid[:8])

    def test_duplicate_codex_titles_use_a_deterministic_uuid_tiebreak(self):
        lower = "11111111-1111-4111-8111-111111111111"
        higher = "22222222-2222-4222-8222-222222222222"
        first = self.make_rollout("first.jsonl")
        second = self.make_rollout("second.jsonl")
        self.make_state_db(
            [
                {
                    "id": higher,
                    "name": "shared",
                    "rollout_path": str(second),
                },
                {
                    "id": lower,
                    "name": "shared",
                    "rollout_path": str(first),
                },
            ]
        )
        self.set_holder(first)
        self.set_holder(second)
        lower_shim = sp_shim.Shim(sp_codex.resolve_thread(lower))
        higher_shim = sp_shim.Shim(sp_codex.resolve_thread(higher))
        self.assertEqual(lower_shim.name, "shared")
        self.assertEqual(higher_shim.name, "codex-%s" % higher[:8])
        lower_shim._refresh_name()
        higher_shim._refresh_name()
        self.assertEqual(lower_shim.name, "shared")
        self.assertEqual(higher_shim.name, "codex-%s" % higher[:8])

    def test_a_rename_refreshes_the_record_state_and_registration(self):
        shim, tid, _rollout = self.make_shim(name="codex-old")
        sp_storage.register_thread({"id": tid, "name": "codex-old"})
        shim._write_record()
        before = shim.record()["nameSince"]
        conn = sqlite3.connect(sp_codex.find_state_db())
        conn.execute("UPDATE threads SET name = ? WHERE id = ?", ("codex-new", tid))
        conn.commit()
        conn.close()
        time.sleep(0.01)

        shim._refresh_name()

        rec = sp_runtime.read_json(shim.record_path)
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid))
        self.assertEqual(shim.name, "codex-new")
        self.assertEqual(rec["name"], "codex-new")
        self.assertGreater(rec["nameSince"], before)
        self.assertEqual(state["name"], "codex-new")
        self.assertEqual(state["thread_name"], "codex-new")
        self.assertEqual(sp_storage.read_registered()[tid]["name"], "codex-new")

    def test_alias_refresh_runs_less_often_than_liveness(self):
        shim, _tid, _rollout = self.make_shim()
        shim.liveness_interval = 5.0
        shim.alias_refresh_interval = 30.0
        calls = []
        shim._check_liveness = lambda: calls.append("liveness")
        shim._refresh_name = lambda: calls.append("alias")

        last_live, last_alias = shim._poll_maintenance(5.0, 0.0, 0.0)
        self.assertEqual(calls, ["liveness"])
        self.assertEqual((last_live, last_alias), (5.0, 0.0))

        last_live, last_alias = shim._poll_maintenance(
            30.0, last_live, last_alias
        )
        self.assertEqual(calls, ["liveness", "liveness", "alias"])
        self.assertEqual((last_live, last_alias), (30.0, 30.0))

    def test_alias_refresh_interval_is_configurable_and_positive(self):
        shim, tid, _rollout = self.make_shim()
        self.assertEqual(shim.alias_refresh_interval, 0.6)
        os.environ.pop("SESSION_PEERS_ALIAS_REFRESH_INTERVAL")
        default = sp_shim.Shim(sp_codex.resolve_thread(tid))
        self.assertEqual(default.alias_refresh_interval, 30.0)
        os.environ["SESSION_PEERS_ALIAS_REFRESH_INTERVAL"] = "0"
        fallback = sp_shim.Shim(sp_codex.resolve_thread(tid))
        self.assertEqual(fallback.alias_refresh_interval, 30.0)

    def test_a_blocked_lsof_probe_does_not_terminate_a_running_shim(self):
        shim, _tid, _rollout = self.make_shim()
        os.environ["FAKE_LSOF_RC"] = "126"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._check_liveness()
            shim._check_liveness()
            os.environ.pop("FAKE_LSOF_RC")
            shim._check_liveness()
        self.assertFalse(shim.stop.is_set())
        self.assertEqual(err.getvalue().count("liveness is unverified"), 1)
        self.assertIn("liveness probe recovered", err.getvalue())

    def test_the_record_is_rewritten_at_most_twice(self):
        shim, _tid, _rollout = self.make_shim()
        shim._write_record()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(5):
                if os.path.exists(shim.record_path):
                    os.unlink(shim.record_path)
                shim._ensure_record()
        self.assertEqual(shim.record_rewrites, sp_constants.MAX_RECORD_REWRITES)
        self.assertFalse(os.path.exists(shim.record_path))

    def test_a_signal_during_polling_cannot_recreate_the_record_after_cleanup(self):
        shim, _tid, _rollout = self.make_shim()
        shim._write_record()

        # A signal may arrive after _poll_loop's stop check. Model the rest of
        # that in-flight iteration before run() reaches its finally block.
        shim._on_signal(signal.SIGTERM, None)
        shim._ensure_record()
        shim._cleanup()

        self.assertFalse(os.path.exists(shim.record_path))

    def test_the_socket_path_fits_the_af_unix_limit(self):
        shim, _tid, _rollout = self.make_shim()
        self.assertLess(len(shim.sock_path.encode("utf-8")), 100)


class TestShimEndToEnd(Base):
    def start_shim(self, tid):
        log_path = self.root / "shim.log"
        self._log_path = log_path
        logfh = open(str(log_path), "a")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(PEERS), "shim", "--thread", tid],
                stdin=subprocess.DEVNULL,
                stdout=logfh,
                stderr=subprocess.STDOUT,
                env=dict(os.environ),
            )
        finally:
            logfh.close()
        self._children.append(proc)
        rec = wait_for(lambda: (self.shim_records() or [None])[0])
        self.assertIsNotNone(rec, "the shim never wrote its record: %s" % self.shim_log())
        wait_for(lambda: os.path.exists(rec["messagingSocketPath"]))
        return proc, rec

    def shim_log(self):
        path = getattr(self, "_log_path", None)
        return pathlib.Path(path).read_text() if path and os.path.exists(path) else ""

    def test_a_startup_reset_releases_the_held_reply_after_the_shim_is_up(self):
        # A marker left across a restart: the held reply is released only once
        # the shim is bound and registered, so its `from` route is live.
        tid, _rollout = self.one_thread()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(tid),
            {
                "thread_id": tid,
                "held": {
                    "s1": {
                        "text": "held across restart",
                        "mid": "m-restart",
                        "turn_id": "t-restart",
                        "at": time.time(),
                    }
                },
            },
            mode=0o600,
        )
        self.cli("budget", "reset", tid)
        _proc, rec = self.start_shim(tid)
        frames = wait_for(lambda: [f for f in listener.of_type("user") if f.get("from")])
        self.assertIsNotNone(frames, "no held reply arrived: %s" % self.shim_log())
        self.assertEqual(frames[0]["from"], "uds:%s" % rec["messagingSocketPath"])
        body, _attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        self.assertEqual(
            strip_origin(body), "[held reply, in reply to message m-restart]\nheld across restart"
        )
        log_text = self.shim_log()
        self.assertLess(log_text.index("shim up:"), log_text.index("released a held reply"))

    def test_first_start_mid_turn_replies_once_without_replaying_history(self):
        tid, rollout = self.one_thread()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        tagged = sp_protocol.build_tag("cc-main", "s1", listener.path) + "\nreview this"
        # A recent, tagged completion must still be skipped on FIRST startup.
        # Only the request that is already running belongs to the new shim.
        append(
            rollout,
            ev("task_started", turn_id="t-finished"), user_item(tagged),
            ev("task_complete", turn_id="t-finished", last_agent_message="old answer"),
            ev("task_started", turn_id="t-live"),
            user_item("repository instructions"), user_item(tagged),
        )
        proc, rec = self.start_shim(tid)
        self.assertEqual(rec["status"], "busy")
        append(rollout, ev("task_complete", turn_id="t-live",
                           last_agent_message="the review verdict"))
        self.assertTrue(wait_for(
            lambda: "delivered turn t-live to cc-main" in self.shim_log(), timeout=5
        ), "the running request lost its reply address: %s" % self.shim_log())
        proc.terminate()
        proc.wait(timeout=10)
        frames = listener.of_type("user")
        self.assertEqual(
            [strip_origin(sp_protocol.unwrap_message(f["message"]["content"])[0]) for f in frames],
            ["the review verdict"],
        )
        self.assertNotIn("delivered turn t-finished", self.shim_log())

    def test_recovered_sender_is_saved_before_a_crash_and_completion_while_down(self):
        tid, rollout = self.one_thread()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        tagged = sp_protocol.build_tag("cc-main", "s1", listener.path) + "\nreview this"
        append(rollout, ev("task_started", turn_id="t-live"), user_item(tagged))
        proc, _rec = self.start_shim(tid)
        # SIGKILL skips cleanup: startup must persist the recovered cursor and
        # sender before advertising a ready peer, not only at graceful exit.
        proc.kill()
        proc.wait(timeout=10)
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid), {})
        self.assertEqual(state.get("tail", {}).get("open_turn"), "t-live")
        append(rollout, ev("task_complete", turn_id="t-live",
                           last_agent_message="finished while down"))
        proc2, _rec2 = self.start_shim(tid)
        self.assertTrue(wait_for(
            lambda: "delivered turn t-live to cc-main" in self.shim_log(), timeout=5
        ), "restart lost the recovered request: %s" % self.shim_log())
        proc2.terminate()
        proc2.wait(timeout=10)
        self.assertEqual(
            [strip_origin(sp_protocol.unwrap_message(f["message"]["content"])[0])
             for f in listener.of_type("user")],
            ["finished while down"],
        )

    def test_a_shim_round_trips_a_message_and_its_reply(self):
        tid, rollout = self.one_thread(name="codex-uzi")
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        proc, shim_rec = self.start_shim(tid)
        self.assertEqual(shim_rec["name"], "codex-uzi")
        self.assertEqual(shim_rec["status"], "idle")

        frame = sp_protocol.build_user_frame(
            sp_protocol.build_wrapper("do it", listener.path, "s1", "cc-main"), listener.path
        )
        sp_claude.send_frame(shim_rec["messagingSocketPath"], frame)

        calls = wait_for(lambda: self.queue_calls())
        self.assertIsNotNone(calls, "nothing was queued: %s" % self.shim_log())
        self.assertEqual(calls[0][2], tid)
        tagged = calls[0][4]
        tag, body = sp_protocol.parse_tag(tagged)
        self.assertEqual(tag["reply"], listener.path)
        self.assertEqual(strip_origin(body), "do it")

        append(rollout, ev("task_started", turn_id="t1"), user_item(tagged))
        self.assertTrue(
            wait_for(lambda: (self.shim_records() or [{}])[0].get("status") == "busy"),
            "the shim never mirrored busy: %s" % self.shim_log(),
        )
        append(
            rollout,
            assistant_item("all done"),
            ev("task_complete", turn_id="t1", last_agent_message="all done"),
        )
        frames = wait_for(lambda: listener.of_type("user"))
        self.assertIsNotNone(frames, "no reply arrived: %s" % self.shim_log())
        reply_body, attrs = sp_protocol.unwrap_message(frames[0]["message"]["content"])
        # The header names the SENDER'S OWN frame msg_id (what SendMessage
        # returned), carried through the Codex turn tag end to end.
        self.assertEqual(
            strip_origin(reply_body), "[in reply to message %s]\nall done" % frame["msg_id"]
        )
        self.assertEqual(attrs["from-name"], "codex-uzi")
        self.assertTrue(
            wait_for(lambda: (self.shim_records() or [{}])[0].get("status") == "idle")
        )
        time.sleep(0.3)
        self.assertEqual(len(listener.of_type("user")), 1)
        # The delivery must be visible in the shim's own log file, with no
        # body text in it.
        self.assertTrue(wait_for(lambda: "delivered turn t1" in self.shim_log()))
        self.assertNotIn("all done", self.shim_log())

    def test_sigterm_removes_the_record_and_the_socket(self):
        tid, _rollout = self.one_thread()
        proc, shim_rec = self.start_shim(tid)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertTrue(wait_for(lambda: not self.shim_records()))
        self.assertFalse(os.path.exists(shim_rec["messagingSocketPath"]))
        self.assertIsNone(sp_lifecycle.shim_pid(tid))

    def test_a_restarted_shim_does_not_resend_a_completed_turn(self):
        tid, rollout = self.one_thread()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        proc, shim_rec = self.start_shim(tid)
        tagged = sp_protocol.build_tag("cc-main", "s1", listener.path) + "\nping"
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item(tagged),
            ev("task_complete", turn_id="t1", last_agent_message="answer one"),
        )
        self.assertIsNotNone(wait_for(lambda: listener.of_type("user")))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

        proc2, _rec2 = self.start_shim(tid)
        time.sleep(0.6)
        self.assertEqual(len(listener.of_type("user")), 1)
        append(
            rollout,
            ev("task_started", turn_id="t2"),
            user_item(tagged),
            ev("task_complete", turn_id="t2", last_agent_message="answer two"),
        )
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 2))
        proc2.send_signal(signal.SIGTERM)
        proc2.wait(timeout=10)

    def test_the_shim_exits_when_the_rollout_is_no_longer_held(self):
        tid, _rollout = self.one_thread()
        proc, shim_rec = self.start_shim(tid)
        self.clear_holders()
        proc.wait(timeout=15)
        self.assertTrue(wait_for(lambda: not self.shim_records()))
        self.assertFalse(os.path.exists(shim_rec["messagingSocketPath"]))

    def test_the_shim_refuses_to_start_for_a_thread_nothing_holds(self):
        tid, _rollout = self.one_thread()
        self.clear_holders()
        proc = self.spawn("shim", "--thread", tid)
        out, _ = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not held by a live codex process", out)

    def test_a_leftover_socket_file_does_not_block_the_bind(self):
        tid, _rollout = self.one_thread()
        stale = self.socks / "stale.sock"
        stale.write_text("")
        proc, shim_rec = self.start_shim(tid)
        self.assertTrue(os.path.exists(shim_rec["messagingSocketPath"]))
        self.assertEqual(
            stat.S_IMODE(os.stat(shim_rec["messagingSocketPath"]).st_mode), 0o600
        )
        record_mode = stat.S_IMODE(os.stat(shim_rec["_path"]).st_mode)
        self.assertEqual(record_mode, 0o644)


class TestRestartDeliveryWindow(ShimBase):
    """S7: a turn that finished while no shim ran is only posted if it is fresh."""

    def _turn(self, socket_path, completed_at, turn_id="t1"):
        return sp_constants.Turn(
            turn_id, "ping",
            {"from": "cc-main", "sid": "s1", "reply": socket_path},
            "complete", "the answer", completed_at,
        )

    def test_a_recent_completion_is_delivered_after_a_restart(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim._handle_turn_end(self._turn(listener.path, shim.started_at - 30))
        self.assertIsNotNone(wait_for(lambda: listener.of_type("user")))

    def test_an_old_completion_is_recorded_but_not_posted(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(
                self._turn(listener.path, shim.started_at - 4 * 3600)
            )
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertIn("without posting", err.getvalue())
        self.assertIn("t1", shim.processed_turns)

    def test_a_completion_with_no_timestamp_is_still_delivered(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim._handle_turn_end(self._turn(listener.path, None))
        self.assertIsNotNone(wait_for(lambda: listener.of_type("user")))

    def test_the_completion_time_comes_off_the_rollout(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item("ping"),
            ev("task_complete", turn_id="t1", last_agent_message="a",
               completed_at="2026-09-07T12:00:00.000Z"),
        )
        turn = tail.poll_turns()[0]
        expected = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(turn.completed_at, expected, places=0)

    def test_the_line_timestamp_is_the_fallback(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            ev("task_complete", turn_id="t1", last_agent_message="a"),
        )
        self.assertIsNotNone(tail.poll_turns()[0].completed_at)

    def test_parse_time_reads_iso_and_epoch_forms(self):
        expected = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(sp_runtime.parse_time("2026-09-07T12:00:00Z"), expected, 0)
        self.assertAlmostEqual(
            sp_runtime.parse_time("2026-09-07T12:00:00+00:00"), expected, 0
        )
        self.assertEqual(sp_runtime.parse_time(expected), expected)
        self.assertEqual(sp_runtime.parse_time(expected * 1000), expected)
        self.assertEqual(sp_runtime.parse_time(str(expected)), expected)
        self.assertIsNone(sp_runtime.parse_time("not a time"))
        self.assertIsNone(sp_runtime.parse_time(None))


class TestRecordFields(ShimBase):
    """S12: two fields Claude renders, measured wrong in the live M6 run."""

    def test_started_at_is_milliseconds_since_the_epoch(self):
        shim, _tid, _rollout = self.make_shim()
        started = shim.record()["startedAt"]
        self.assertIsInstance(started, int)
        # Milliseconds, not seconds: a seconds value would be ~1e9 and render
        # as "started 20703d ago" in ListAgents.
        self.assertGreater(started, 1_700_000_000_000)
        self.assertAlmostEqual(started / 1000.0, shim.started_at, delta=5)

    def test_every_record_timestamp_is_integer_milliseconds(self):
        # A live 2.1.263 record: startedAt, nameSince, updatedAt and
        # statusUpdatedAt are all integer ms; only procStart is a string.
        shim, _tid, _rollout = self.make_shim()
        rec = shim.record()
        for key in ("startedAt", "nameSince", "updatedAt", "statusUpdatedAt"):
            self.assertIsInstance(rec[key], int, key)
            self.assertGreater(rec[key], 1_700_000_000_000, key)
        self.assertIsInstance(rec["procStart"], str)

    def test_the_version_field_is_not_double_prefixed(self):
        tid, _rollout = self.one_thread()
        os.environ["FAKE_CODEX_VERSION"] = "codex-cli 0.153.4"
        proc = self.spawn("shim", "--thread", tid)
        try:
            rec = wait_for(lambda: (self.shim_records() or [None])[0])
            self.assertIsNotNone(rec)
            self.assertEqual(rec["version"], "codex-0.153.4")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


class TestBoundedState(ShimBase):
    """N1: contacts and budgets are as long-lived as the shim."""

    def test_contacts_and_budgets_are_capped(self):
        shim, _tid, _rollout = self.make_shim()
        for i in range(sp_constants.CONTACT_HISTORY + 25):
            shim.contacts["s%d" % i] = {"name": "n%d" % i}
            shim.reply_budget.budgets["s%d" % i] = 1
        shim._bound(shim.contacts)
        shim._bound(shim.reply_budget.budgets)
        self.assertEqual(len(shim.contacts), sp_constants.CONTACT_HISTORY)
        self.assertEqual(len(shim.reply_budget.budgets), sp_constants.CONTACT_HISTORY)
        self.assertNotIn("s0", shim.contacts)
        self.assertIn("s%d" % (sp_constants.CONTACT_HISTORY + 24), shim.contacts)

    def test_a_repeat_contact_moves_to_the_newest_slot(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim.contacts["s1"] = {"name": "cc-main"}
        for i in range(5):
            shim.contacts["filler%d" % i] = {"name": "f"}
        shim._handle_line(json.dumps(self.inbound_frame("hi", listener.path)))
        self.assertEqual(list(shim.contacts)[-1], "s1")


class TestTruncationNotice(ShimBase):
    """S10: a silent trim is a surprise; say so."""

    def test_the_sender_is_told_when_its_body_was_trimmed(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        big = "x" * (sp_constants.MAX_TEXT_CHARS + 10)
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_line(json.dumps(self.inbound_frame(big, listener.path)))
        status = wait_for(lambda: listener.of_type("control", "peer_message_status"))
        self.assertIsNotNone(status, "no truncation notice arrived")
        self.assertEqual(status[0]["status"], "truncated")
        self.assertIn(str(sp_constants.MAX_TEXT_CHARS + 10), status[0]["detail"])

    def test_a_body_that_fits_produces_no_notice(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        shim._handle_line(json.dumps(self.inbound_frame("small", listener.path)))
        time.sleep(0.2)
        self.assertEqual(listener.of_type("control", "peer_message_status"), [])


class TestCachedBoundary(ShimBase):
    """N6: the interrupt answer must not rescan a 194 MiB file per message."""

    def test_the_boundary_is_seeded_at_start_and_updated_by_polling(self):
        shim, _tid, rollout = self.make_shim()
        self.assertEqual(shim.tail.last_boundary, "complete")
        append(rollout, ev("task_started", turn_id="t1"))
        shim.tail.poll()
        self.assertEqual(shim.tail.last_boundary, "started")
        append(rollout, ev("turn_aborted", turn_id="t1"))
        shim.tail.poll()
        self.assertEqual(shim.tail.last_boundary, "aborted")

    def test_the_boundary_survives_a_restart_through_the_state_file(self):
        shim, tid, rollout = self.make_shim()
        append(rollout, ev("task_started", turn_id="t1"), ev("turn_aborted", turn_id="t1"))
        shim.tail.poll()
        shim._save_state()
        restarted = sp_shim.Shim(sp_codex.resolve_thread(tid))
        self.assertEqual(restarted.tail.last_boundary, "aborted")


class TestReplyNeedsASessionId(ShimBase):
    """P3: a socket is named after a pid, and pids are reused."""

    def test_a_tag_without_a_session_id_is_not_delivered(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        turn = sp_constants.Turn(
            "t1", "ping", {"from": "cc-main", "sid": None, "reply": listener.path},
            "complete", "the answer", None,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(turn)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertIn("no session id", err.getvalue())

    def test_a_tag_whose_sid_is_the_absent_sentinel_is_not_delivered(self):
        shim, _tid, _rollout = self.make_shim()
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        line = sp_protocol.build_tag("cc-main", None, listener.path)
        tag, _body = sp_protocol.parse_tag(line + "\nping")
        turn = sp_constants.Turn("t1", "ping", tag, "complete", "the answer", None)
        with contextlib.redirect_stderr(io.StringIO()):
            shim._handle_turn_end(turn)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])

    def test_send_fills_the_session_id_from_the_registry(self):
        tid, _r = self.one_thread()
        listener, _rec = self.add_listener(name="cc-main", session_id="s-real")
        rc, _out, _err = self.cli(
            "send", "--to", "codex:%s" % tid, "--message", "hi",
            "--from-socket", listener.path,
        )
        self.assertEqual(rc, 0)
        tag, _body = sp_protocol.parse_tag(self.queue_calls()[0][4])
        self.assertEqual(tag["sid"], "s-real")
        self.assertEqual(tag["from"], "cc-main")

    def test_send_refuses_a_from_socket_nothing_listens_on(self):
        tid, _r = self.one_thread()
        rc, _out, err = self.cli(
            "send", "--to", "codex:%s" % tid, "--message", "hi",
            "--from-socket", str(self.socks / "nobody.sock"),
        )
        self.assertEqual(rc, 1)
        self.assertIn("no live Claude session listens", err)
        self.assertEqual(self.queue_calls(), [])


class TestStatusAtStart(Base):
    """P7: a shim that starts mid-turn must not advertise idle."""

    def mid_turn_thread(self):
        tid = str(uuidlib.uuid4())
        rollout = self.make_rollout(lines=[
            ev("task_started", turn_id="t-old"),
            user_item("older"),
            ev("task_complete", turn_id="t-old", last_agent_message="older answer"),
            ev("task_started", turn_id="t-live"),
            user_item("a turn already running"),
        ])
        self.make_state_db([{"id": tid, "name": "codex-uzi",
                             "rollout_path": str(rollout)}])
        self.set_holder(rollout)
        return tid, rollout

    def test_a_shim_starting_during_a_turn_reports_busy(self):
        tid, _rollout = self.mid_turn_thread()
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
        self.assertEqual(shim.tail.last_boundary, "started")
        self.assertEqual(shim.status, "busy")
        self.assertEqual(shim.record()["status"], "busy")

    def test_the_record_goes_busy_then_idle_across_a_real_start(self):
        tid, rollout = self.mid_turn_thread()
        proc = subprocess.Popen(
            [sys.executable, str(PEERS), "shim", "--thread", tid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=dict(os.environ),
        )
        self._children.append(proc)
        try:
            rec = wait_for(lambda: (self.shim_records() or [None])[0])
            self.assertIsNotNone(rec)
            self.assertEqual(rec["status"], "busy")
            append(rollout, ev("task_complete", turn_id="t-live",
                               last_agent_message="done"))
            self.assertTrue(wait_for(
                lambda: (self.shim_records() or [{}])[0].get("status") == "idle"
            ))
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_an_idle_thread_still_starts_idle(self):
        tid, _rollout = self.one_thread()
        self.assertEqual(sp_shim.Shim(sp_codex.resolve_thread(tid)).status, "idle")


class TestStartupReplyRecovery(Base):
    """A first-start scan retains the active request, never completed replies."""

    def test_the_active_sender_survives_later_untagged_context(self):
        tid, rollout = self.one_thread()
        tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
        append(rollout, ev("task_started", turn_id="t-live"),
               user_item(tagged + "\nreview"), user_item("more context"))
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
        append(rollout, ev("task_complete", turn_id="t-live", last_agent_message="verdict"))
        turn = shim.tail.poll_turns()[0]
        self.assertEqual(turn.tag, sp_protocol.parse_tag(tagged)[0])
        self.assertEqual(turn.last_agent_message, "verdict")

    def test_a_partial_request_at_startup_keeps_the_turn_boundary(self):
        tid, rollout = self.one_thread()
        append(rollout, ev("task_started", turn_id="t-live"))
        tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
        line = user_item(tagged + "\nreview")
        with rollout.open("a") as fh:
            fh.write(line[:40])
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
        with rollout.open("a") as fh:
            fh.write(line[40:] + "\n")
        append(rollout, ev("task_complete", turn_id="t-live", last_agent_message="verdict"))
        turn = shim.tail.poll_turns()[0]
        self.assertEqual(turn.tag, sp_protocol.parse_tag(tagged)[0])

    def test_completed_and_aborted_senders_do_not_leak_into_a_typed_turn(self):
        tid, rollout = self.one_thread()
        for boundary in ("task_complete", "turn_aborted"):
            with self.subTest(boundary=boundary):
                tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
                append(rollout, ev("task_started", turn_id="t-tagged"), user_item(tagged),
                       ev(boundary, turn_id="t-tagged", last_agent_message="old answer"),
                       ev("task_started", turn_id="t-typed"), user_item("typed prompt"))
                shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
                self.assertEqual(shim.tail.poll_turns(), [])
                append(rollout, ev("task_complete", turn_id="t-typed", last_agent_message="typed answer"))
                turns = shim.tail.poll_turns()
                self.assertEqual([t.turn_id for t in turns], ["t-typed"])
                self.assertIsNone(turns[0].tag)

    def test_legacy_deduplication_state_is_migrated_without_claiming_delivery(self):
        tid, rollout = self.one_thread()
        tail = sp_rollout.RolloutTail(str(rollout))
        tail.poll()
        sp_runtime.write_json_atomic(sp_storage.thread_state_path(tid), {
            "tail": tail.state(), "delivered": ["t-processed"],
        })
        shim = sp_shim.Shim(sp_codex.resolve_thread(tid))
        # A legacy processed turn remains deduplicated, even with no final
        # message. Handling it again would log that missing final message.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            shim._handle_turn_end(sp_constants.Turn("t-processed", "", None, "complete", None))
        self.assertEqual(err.getvalue(), "")
        shim._save_state()
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid))
        self.assertEqual(state.get("processed_turns"), ["t-processed"])
        self.assertNotIn("delivered", state)


class TestReplyOrdering(ShimBase):
    @staticmethod
    def turn(listener, sid="s1", turn_id="order-1"):
        return sp_constants.Turn(turn_id, "ping",
                                {"from": "cc-main", "sid": sid, "mid": "m-" + turn_id,
                                 "reply": listener.path}, "complete", "answer")

    def test_session_is_verified_before_any_route_is_used(self):
        shim, _tid, _ = self.make_shim()
        listener, _ = self.add_listener(session_id="s-current")
        with mock.patch.object(sp_claude, "deliver_to_record", return_value=True) as route:
            shim._handle_turn_end(self.turn(listener, sid="s-old", turn_id="old-session"))
            self.assertEqual(route.call_count, 0)
            shim._handle_turn_end(self.turn(listener, sid="s-current", turn_id="current-session"))
            self.assertEqual(route.call_count, 1)

    def test_duplicate_selected_targets_are_deduplicated_before_delivery(self):
        shim, _tid, _ = self.make_shim()
        listener, record = self.add_listener(session_id="s1")
        with mock.patch.object(shim, "_reply_targets", return_value=[record, record]):
            with mock.patch.object(sp_claude, "deliver_to_record", return_value=True) as route:
                shim._handle_turn_end(self.turn(listener))
                self.assertEqual(route.call_count, 1)
        self.assertEqual(shim.reply_budget.budgets["s1"], 1)

    def test_failed_delivery_spends_neither_sequence_nor_binding_budget(self):
        shim, _tid, _ = self.make_shim()
        listener, _ = self.add_listener(session_id="s1")
        shim.reply_budget.binding = {"sid": "s1", "bind_id": "binding", "total": 5, "spent": 0}
        with mock.patch.object(sp_claude, "deliver_to_record", return_value=False) as route:
            shim._handle_turn_end(self.turn(listener, turn_id="failed"))
            self.assertEqual(route.call_count, 1)
        self.assertEqual(shim.reply_budget.budgets.get("s1", 0), 0)
        self.assertEqual(shim.reply_budget.binding["spent"], 0)
        with mock.patch.object(sp_claude, "deliver_to_record", return_value=True):
            shim._handle_turn_end(self.turn(listener, turn_id="succeeded"))
        self.assertEqual(shim.reply_budget.budgets["s1"], 1)
        self.assertEqual(shim.reply_budget.binding["spent"], 1)

    def test_processing_ledger_records_failure_without_claiming_delivery(self):
        shim, tid, _ = self.make_shim()
        listener, _ = self.add_listener(session_id="s1")
        turn = self.turn(listener, turn_id="failed-once")
        with mock.patch.object(sp_claude, "deliver_to_record", return_value=False) as route:
            shim._handle_turn_end(turn)
            self.assertEqual(route.call_count, 1)
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid))
        self.assertIn(turn.turn_id, state["processed_turns"])
        self.assertEqual(state["budgets"].get("s1", 0), 0)
        with mock.patch.object(sp_claude, "deliver_to_record", return_value=True) as route:
            shim._handle_turn_end(turn)
            self.assertEqual(route.call_count, 0)
            shim._handle_turn_end(self.turn(listener, turn_id="new-request"))
            self.assertEqual(route.call_count, 1)
