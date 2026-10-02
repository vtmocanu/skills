"""Rollout for the session-peers CLI."""

from __future__ import annotations
import json
import os
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime

# --------------------------------------------------------------------------
# Rollout tail
# --------------------------------------------------------------------------


class RolloutTail:
    """Incremental turn-boundary reader over a Codex rollout JSONL.

    Correlation is by boundary events only: `task_started` opens a turn,
    the following `role: user` response_item (which carries NO turn_id) is its
    prompt, and `task_complete` or `turn_aborted` closes it. "Last user item
    before EOF" is never used, because a queued turn and a typed one interleave.
    """

    def __init__(self, path, cursor=0, open_turn=None, pending=None,
                 last_boundary=None):
        self.path = path
        self.cursor = int(cursor or 0)
        self.open_turn = open_turn
        self.pending = dict(pending or {})
        # N6: the newest of task_started / task_complete / turn_aborted seen,
        # so the interrupt question is answered without rescanning the file.
        self.last_boundary = last_boundary

    # -- state -------------------------------------------------------------

    def state(self):
        return {
            "cursor": self.cursor,
            "open_turn": self.open_turn,
            "pending": self.pending,
            "last_boundary": self.last_boundary,
        }

    @classmethod
    def from_state(cls, path, state):
        state = state or {}
        return cls(
            path,
            cursor=state.get("cursor", 0),
            open_turn=state.get("open_turn"),
            pending=state.get("pending"),
            last_boundary=state.get("last_boundary"),
        )

    # -- reading -----------------------------------------------------------

    def _read_lines(self):
        """Complete lines since the cursor, read in bounded chunks (S4).

        Reading cursor-to-EOF in one allocation peaked at 587 MiB on a 194 MiB
        rollout. The cursor still advances only past newline-terminated lines,
        so a partial trailing write is re-read next poll rather than lost.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.cursor:
            # Truncated or rotated underneath us: resync rather than replay.
            sp_runtime.log("%s shrank; resyncing the cursor to EOF" % self.path)
            self.cursor = size
            return
        if size == self.cursor:
            return
        remaining = size - self.cursor
        pending = b""
        skipping = False
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.cursor)
                while remaining > 0:
                    chunk = fh.read(min(sp_constants.READ_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    parts = (pending + chunk).split(b"\n")
                    pending = parts.pop()
                    for part in parts:
                        self.cursor += len(part) + 1
                        if skipping:
                            skipping = False
                            continue
                        if part.strip():
                            yield part.decode("utf-8", "replace")
                    if len(pending) > sp_constants.MAX_ROLLOUT_LINE:
                        # No Codex turn is this long; drop it rather than grow.
                        sp_runtime.log(
                            "skipping a rollout line over %d bytes in %s"
                            % (sp_constants.MAX_ROLLOUT_LINE, self.path)
                        )
                        self.cursor += len(pending)
                        pending = b""
                        skipping = True
        except OSError as exc:
            sp_runtime.log("cannot read %s: %s" % (self.path, exc))

    def poll(self, emit_events=True):
        """Read turn state and, normally, emit its ordered start/end events.

        First startup passes emit_events=False to recover the open request
        without replaying completed replies. The cursor and pending sender
        come from the same bounded read, including a partial trailing line.
        """
        events = []
        for line in self._read_lines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "event_msg":
                ptype = payload.get("type")
                if ptype == "task_started":
                    turn_id = payload.get("turn_id")
                    self.open_turn = turn_id
                    self.last_boundary = "started"
                    self.pending.setdefault(turn_id, {"tag": None, "text": ""})
                    if emit_events:
                        events.append(sp_constants.Event("start", turn_id, None))
                elif ptype in ("task_complete", "turn_aborted"):
                    turn_id = payload.get("turn_id")
                    info = self.pending.pop(turn_id, {"tag": None, "text": ""})
                    if self.open_turn == turn_id:
                        self.open_turn = None
                    outcome = "complete" if ptype == "task_complete" else "aborted"
                    self.last_boundary = outcome
                    if not emit_events:
                        continue
                    last = payload.get("last_agent_message") if outcome == "complete" else None
                    finished = sp_runtime.parse_time(payload.get("completed_at"))
                    if finished is None:
                        finished = sp_runtime.parse_time(obj.get("timestamp"))
                    events.append(
                        sp_constants.Event(
                            "end",
                            turn_id,
                            sp_constants.Turn(
                                turn_id,
                                info.get("text") or "",
                                info.get("tag"),
                                outcome,
                                last,
                                finished,
                            ),
                        )
                    )
            elif kind == "response_item":
                if payload.get("role") != "user":
                    continue
                text = self._item_text(payload)
                if text is None:
                    continue
                turn_id = self.open_turn
                if turn_id is None:
                    continue
                tag, body = sp_protocol.parse_tag(text)
                info = self.pending.setdefault(turn_id, {"tag": None, "text": ""})
                info["text"] = body
                if tag is not None:
                    info["tag"] = tag
        return events

    def poll_turns(self):
        return [e.turn for e in self.poll() if e.kind == "end"]

    @staticmethod
    def _item_text(payload):
        content = payload.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None


def last_boundary(rollout_path):
    """The last turn boundary in a rollout: 'started', 'complete', 'aborted'.

    Used for the interrupt check: a `turn_aborted` with no later `task_started`
    means the queue is paused until the human types something (measured).
    """
    result = None
    try:
        fh = open(rollout_path, "r", encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError, TypeError):
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if ptype == "task_started":
                result = "started"
            elif ptype == "task_complete":
                result = "complete"
            elif ptype == "turn_aborted":
                result = "aborted"
    return result


def thread_is_paused(rollout_path) -> bool:
    return last_boundary(rollout_path) == "aborted"
