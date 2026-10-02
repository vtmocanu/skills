"""Regression coverage for rollout."""

from __future__ import annotations

import contextlib
import io
import os
from .support import (
    Base,
    append,
    assistant_item,
    ev,
    rollout_line,
    sp_constants,
    sp_protocol,
    sp_rollout,
    user_item,
)


class TestRolloutTail(Base):
    def test_turn_boundaries_pair_a_prompt_with_its_completion(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
        append(rollout, ev("task_started", turn_id="t1"), user_item(tagged + "\nping"))
        events = tail.poll()
        self.assertEqual([e.kind for e in events], ["start"])
        append(
            rollout,
            assistant_item("pong"),
            ev("task_complete", turn_id="t1", last_agent_message="pong"),
        )
        turns = tail.poll_turns()
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual(turn.turn_id, "t1")
        self.assertEqual(turn.user_text, "ping")
        self.assertEqual(turn.tag["from"], "cc-main")
        self.assertEqual(turn.outcome, "complete")
        self.assertEqual(turn.last_agent_message, "pong")

    def test_back_to_back_queued_items_are_two_sequential_turns(self):
        # M0: two queued items do not merge; each gets its own task_started and
        # task_complete, and one of them may complete with a null message.
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item(tagged + "\nfirst"),
            ev("task_complete", turn_id="t1", last_agent_message="answer one"),
            ev("task_started", turn_id="t2"),
            user_item(tagged + "\nsecond"),
            ev("task_complete", turn_id="t2", last_agent_message=None),
        )
        turns = tail.poll_turns()
        self.assertEqual([t.turn_id for t in turns], ["t1", "t2"])
        self.assertEqual([t.user_text for t in turns], ["first", "second"])
        self.assertEqual(turns[0].last_agent_message, "answer one")
        self.assertIsNone(turns[1].last_agent_message)
        self.assertEqual([t.tag["sid"] for t in turns], ["s1", "s1"])

    def test_an_untagged_prompt_yields_a_turn_with_no_tag(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item("typed by hand"),
            ev("task_complete", turn_id="t1", last_agent_message="ok"),
        )
        turn = tail.poll_turns()[0]
        self.assertIsNone(turn.tag)
        self.assertEqual(turn.user_text, "typed by hand")

    def test_an_interrupt_closes_the_turn_with_no_reply(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item("ping"),
            ev("turn_aborted", turn_id="t1", reason="interrupted"),
        )
        turn = tail.poll_turns()[0]
        self.assertEqual(turn.outcome, "aborted")
        self.assertIsNone(turn.last_agent_message)

    def test_a_null_completion_message_is_carried_through_as_none(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item("ping"),
            ev("task_complete", turn_id="t1", last_agent_message=None),
        )
        self.assertIsNone(tail.poll_turns()[0].last_agent_message)

    def test_a_partial_trailing_line_is_not_consumed(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        line = ev("task_started", turn_id="t1")
        with open(rollout, "a") as fh:
            fh.write(line[:20])
        self.assertEqual(tail.poll(), [])
        cursor = tail.cursor
        with open(rollout, "a") as fh:
            fh.write(line[20:] + "\n")
        self.assertEqual([e.kind for e in tail.poll()], ["start"])
        self.assertGreater(tail.cursor, cursor)

    def test_unknown_events_and_bad_lines_are_skipped(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            "{not json",
            rollout_line("compacted", {"type": "something_new"}),
            ev("task_started", turn_id="t1"),
            ev("token_count", turn_id="t1", total=5),
            ev("task_complete", turn_id="t1", last_agent_message="done"),
        )
        turns = tail.poll_turns()
        self.assertEqual([t.turn_id for t in turns], ["t1"])

    def test_a_restart_from_state_does_not_replay_a_delivered_turn(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(
            rollout,
            ev("task_started", turn_id="t1"),
            user_item("ping"),
            ev("task_complete", turn_id="t1", last_agent_message="pong"),
        )
        self.assertEqual(len(tail.poll_turns()), 1)
        state = tail.state()
        restarted = sp_rollout.RolloutTail.from_state(str(rollout), state)
        self.assertEqual(restarted.poll_turns(), [])
        append(
            rollout,
            ev("task_started", turn_id="t2"),
            user_item("again"),
            ev("task_complete", turn_id="t2", last_agent_message="second"),
        )
        self.assertEqual([t.turn_id for t in restarted.poll_turns()], ["t2"])

    def test_a_first_start_scan_suppresses_completed_turns(self):
        rollout = self.make_rollout(
            lines=[
                ev("task_started", turn_id="old"),
                user_item("old"),
                ev("task_complete", turn_id="old", last_agent_message="old answer"),
            ]
        )
        tail = sp_rollout.RolloutTail(str(rollout))
        self.assertEqual(tail.poll(emit_events=False), [])
        self.assertEqual(tail.poll_turns(), [])
        self.assertEqual(tail.pending, {})
        self.assertIsNone(tail.open_turn)

    def test_a_turn_open_across_a_restart_keeps_its_sender(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        tagged = sp_protocol.build_tag("cc-main", "s1", str(self.socks / "1.sock"))
        append(rollout, ev("task_started", turn_id="t1"), user_item(tagged + "\nping"))
        tail.poll()
        restarted = sp_rollout.RolloutTail.from_state(str(rollout), tail.state())
        append(rollout, ev("task_complete", turn_id="t1", last_agent_message="pong"))
        turn = restarted.poll_turns()[0]
        self.assertEqual(turn.tag["sid"], "s1")

    def test_a_truncated_rollout_resyncs_instead_of_replaying(self):
        rollout = self.make_rollout()
        tail = sp_rollout.RolloutTail(str(rollout))
        append(rollout, ev("task_started", turn_id="t1"))
        tail.poll()
        with open(rollout, "w"):
            pass
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(tail.poll(), [])
        self.assertEqual(tail.cursor, 0)

    def test_last_boundary_reports_the_interrupt_state(self):
        rollout = self.make_rollout(
            lines=[
                ev("task_started", turn_id="t1"),
                ev("task_complete", turn_id="t1", last_agent_message="a"),
            ]
        )
        self.assertEqual(sp_rollout.last_boundary(str(rollout)), "complete")
        self.assertFalse(sp_rollout.thread_is_paused(str(rollout)))
        append(rollout, ev("task_started", turn_id="t2"), ev("turn_aborted", turn_id="t2"))
        self.assertTrue(sp_rollout.thread_is_paused(str(rollout)))
        append(rollout, ev("task_started", turn_id="t3"))
        self.assertFalse(sp_rollout.thread_is_paused(str(rollout)))

    def test_last_boundary_on_a_missing_file_is_none(self):
        self.assertIsNone(sp_rollout.last_boundary(str(self.root / "nope.jsonl")))


class TestRolloutChunking(Base):
    """S4: the reader must not allocate the whole tail at once."""

    def test_lines_split_across_read_chunks_still_parse(self):
        rollout = self.make_rollout()
        original = sp_constants.READ_CHUNK
        sp_constants.READ_CHUNK = 64
        try:
            tail = sp_rollout.RolloutTail(str(rollout))
            append(
                rollout,
                ev("task_started", turn_id="t1"),
                user_item("a prompt long enough to straddle several chunks " * 4),
                ev("task_complete", turn_id="t1", last_agent_message="answer"),
                ev("task_started", turn_id="t2"),
                user_item("second"),
                ev("task_complete", turn_id="t2", last_agent_message="answer two"),
            )
            turns = tail.poll_turns()
        finally:
            sp_constants.READ_CHUNK = original
        self.assertEqual([t.turn_id for t in turns], ["t1", "t2"])
        self.assertEqual(turns[1].last_agent_message, "answer two")
        self.assertEqual(tail.cursor, os.path.getsize(str(rollout)))

    def test_an_absurdly_long_line_is_skipped_not_buffered(self):
        rollout = self.make_rollout()
        original_chunk, original_line = sp_constants.READ_CHUNK, sp_constants.MAX_ROLLOUT_LINE
        sp_constants.READ_CHUNK = 256
        sp_constants.MAX_ROLLOUT_LINE = 1024
        try:
            tail = sp_rollout.RolloutTail(str(rollout))
            append(
                rollout,
                ev("task_started", turn_id="t1"),
                user_item("x" * 5000),
                ev("task_complete", turn_id="t1", last_agent_message="answer"),
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                turns = tail.poll_turns()
        finally:
            sp_constants.READ_CHUNK = original_chunk
            sp_constants.MAX_ROLLOUT_LINE = original_line
        self.assertIn("skipping a rollout line", err.getvalue())
        self.assertEqual([t.turn_id for t in turns], ["t1"])
        self.assertEqual(turns[0].last_agent_message, "answer")
        self.assertEqual(tail.cursor, os.path.getsize(str(rollout)))

    def test_a_partial_line_still_survives_chunking(self):
        rollout = self.make_rollout()
        original = sp_constants.READ_CHUNK
        sp_constants.READ_CHUNK = 32
        try:
            tail = sp_rollout.RolloutTail(str(rollout))
            line = ev("task_started", turn_id="t1")
            with open(rollout, "a") as fh:
                fh.write(line[:40])
            self.assertEqual(tail.poll(), [])
            with open(rollout, "a") as fh:
                fh.write(line[40:] + "\n")
            self.assertEqual([e.kind for e in tail.poll()], ["start"])
        finally:
            sp_constants.READ_CHUNK = original
