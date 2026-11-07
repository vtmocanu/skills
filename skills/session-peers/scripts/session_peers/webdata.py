"""Read model for the session-peers web dashboard (PRD #161).

Pure functions and small per-request objects that build every view's JSON from
the existing stores. Nothing here holds state between requests or writes
anything: history is read through a lock-free, bounded snapshot reader that
never calls ``channels.read_history``, ``topics.topic_tail`` or
``topics._topic_paths`` (they lock, create directories and may rewrite the log).
The only lock touched is the per-send non-blocking flock probe, released at once.

One request owns one ``Budget`` (REQUEST_BUDGET bytes). Every topic read, the
meta read, the outcome recheck and the journal read charge it, and each topic is
read at most TOPIC_BUDGET bytes from its end, except that a request never
returns nothing for want of bytes alone: when no whole entry fits it reads on to
the first whole line (at most ENTRY_LINE_MAX) while the request budget can hold
it, else it stops ``partial``.
"""

from __future__ import annotations
import errno
import json
import os
import re
import stat
import time
import types
from . import budgets as sp_budgets, channels as sp_channels, constants as sp_constants
from . import journal as sp_journal, runtime as sp_runtime, storage as sp_storage, topics as sp_topics

REQUEST_BUDGET = 8 * 1024 * 1024
TOPIC_BUDGET = 1024 * 1024
ENTRY_LINE_MAX = 6 * sp_constants.MAX_TEXT_CHARS + 512 * 1024
PAGE_MAX = 200
META_MAX = 64 * 1024
SHIM_STATE_MAX = 1024 * 1024
_CHUNK = 64 * 1024
_HEAD = 4096

BUDGET_LABEL = "as last saved by the shim"


class WebDataError(Exception):
    """A refused read-model request; ``status`` is the HTTP status the server maps it to."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Identifier hygiene
# --------------------------------------------------------------------------

_ESCAPE_RE = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u2028\u2029]"
)
_BODY_ESCAPE_RE = re.compile(
    "[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u2028\u2029]"
)


def _escape_match(match):
    return "\\u%04x" % ord(match.group(0))


def escape_text(value):
    """``value`` as a string with control, bidi and line-separator characters shown as ``\\uXXXX``."""
    return _ESCAPE_RE.sub(_escape_match, value if isinstance(value, str) else str(value))


def escape_body(value):
    """Like ``escape_text`` but a message body keeps its newlines and tabs."""
    return _BODY_ESCAPE_RE.sub(_escape_match, value if isinstance(value, str) else str(value))


_BODY_KEYS = ("body", "text")


def _clean(value, key=None):
    if isinstance(value, dict):
        return {escape_text(k): _clean(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, key) for v in value]
    if isinstance(value, str):
        return escape_body(value) if key in _BODY_KEYS else escape_text(value)
    return value


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


class Budget:
    """The bytes one request may still read."""

    def __init__(self, total=REQUEST_BUDGET):
        self.total = total
        self.used = 0

    def remaining(self):
        return max(0, self.total - self.used)

    def charge(self, count):
        self.used += count


# --------------------------------------------------------------------------
# Bounded read-only history snapshot
# --------------------------------------------------------------------------


def _open_regular(path):
    """``(fd, stat)`` of a regular file opened read-only without following links.

    ``(None, None)`` when it does not exist; OSError for anything else (a symlink,
    a FIFO, a permission error). O_NONBLOCK keeps a planted FIFO from blocking the open."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
    except BaseException:
        os.close(fd)
        raise
    return fd, st


_HEAD_SEQ_RE = re.compile(
    rb'\{(?:"[a-z_]+": (?:"(?:[^"\\]|\\.)*"|null|true|false|-?[0-9]+), )*"seq": ([0-9]+)'
)


def _head_seq(head):
    """The seq of an oversized line when it sits in the line's own first bytes, else None."""
    match = _HEAD_SEQ_RE.match(head or b"")
    return int(match.group(1)) if match else None


class _BackReader:
    """Complete lines of a file from its end, in chunks, within a byte allowance.

    ``next_line`` returns ``("line", bytes)``, ``("oversized", head)`` for a line over
    ENTRY_LINE_MAX (only its first bytes are kept), ``("end", None)`` once the file
    start is reached, or ``("stop", reason)`` when the allowance ended the read. A
    final line without its newline is torn and never returned. When the caller still
    ``need``s an entry once ``cap`` is spent, the one line then being assembled is read
    whole (past ``cap``) while the request budget can hold ENTRY_LINE_MAX; after that
    line the read stops."""

    def __init__(self, fd, end, budget, cap):
        self.fd, self.pos, self.budget, self.cap = fd, end, budget, cap
        self.read = 0
        self.parts = []          # fragments of the line being assembled, nearest the file end first
        self.part_len = 0
        self.skipping = False    # the line being assembled is already over ENTRY_LINE_MAX
        self.seen_end = False    # the last newline of the file has been found
        self.torn = 0
        self.pending = []        # complete lines found in the last chunk, oldest first (pop() = newest)
        self.complete_end = None  # offset just past the newest complete line
        self.done = False
        self.extending = False   # reading past ``cap`` to finish one line
        self.extended = False    # that line has been returned

    @property
    def unfinished_oversized(self):
        return self.skipping or self.part_len > ENTRY_LINE_MAX

    def _line(self, tail):
        total = len(tail) + self.part_len
        if self.skipping or total > ENTRY_LINE_MAX:
            head = tail[:_HEAD]
            if len(head) < _HEAD and self.parts:
                head = (tail + b"".join(reversed(self.parts)))[:_HEAD]
            return ("oversized", head)
        return ("line", tail + b"".join(reversed(self.parts)))

    def _absorb(self, chunk):
        if not self.seen_end:
            idx = chunk.rfind(b"\n")
            if idx < 0:
                self.torn += len(chunk)
                return
            self.seen_end = True
            self.complete_end = self.pos + idx + 1
            chunk = chunk[:idx]
        pieces = chunk.split(b"\n")
        if len(pieces) == 1:
            self.parts.append(pieces[0])
            self.part_len += len(pieces[0])
            if not self.skipping and self.part_len > ENTRY_LINE_MAX:
                self.skipping, self.parts = True, []
            return
        found = [self._line(pieces[-1])]
        for piece in reversed(pieces[1:-1]):
            if len(piece) > ENTRY_LINE_MAX:
                found.append(("oversized", piece[:_HEAD]))
            else:
                found.append(("line", piece))
        self.pending = found[::-1]
        self.parts, self.part_len, self.skipping = [pieces[0]], len(pieces[0]), False
        if len(pieces[0]) > ENTRY_LINE_MAX:
            self.skipping, self.parts = True, []

    def _emit(self, item):
        if self.extending:
            self.extended = True
        return item

    def next_line(self, need=False):
        while True:
            if self.pending:
                return self._emit(self.pending.pop())
            if self.pos == 0:
                if self.seen_end and not self.done:
                    self.done = True
                    return self._emit(self._line(b""))
                return ("end", None)
            step = min(_CHUNK, self.pos)
            if self.read >= self.cap:
                if self.extended or not need:
                    return ("stop", "cap")
                self.extending = True
                # The first entry is read whole when the request budget can hold it.
                need = ENTRY_LINE_MAX - (self.part_len if self.seen_end else self.torn)
                remaining = self.budget.remaining()
                if remaining <= 0 or remaining < need:
                    return ("stop", "budget")
                step = min(step, remaining)
            else:
                step = min(step, self.cap - self.read)
            chunk = os.pread(self.fd, step, self.pos - step)
            if len(chunk) != step:
                return ("stop", "changed")
            self.pos -= step
            self.read += step
            self.budget.charge(step)
            self._absorb(chunk)


def _decode_line(raw):
    """``(entry, size)`` of a valid history line (size as topics counts it, newline included), else None."""
    try:
        text = raw.decode("utf-8")
        size = len(raw) + 1
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        size = len(text.encode("utf-8")) + 1
    try:
        entry = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
        return entry, size
    return None


def _first_outcome(entries, message_id, after_seq):
    """The first ``channel.outcome`` after ``after_seq`` for ``message_id`` (None equals None, as ``_find_outcome``)."""
    for entry in entries:
        seq = entry.get("seq")
        data = entry.get("data")
        if (isinstance(seq, int) and seq > after_seq and entry.get("kind") == sp_channels.EVENT_OUTCOME
                and isinstance(data, dict) and data.get("message_id") == message_id):
            return entry
    return None


class HistorySnapshot:
    """A bounded, lock-free, read-only view of one channel's history topic.

    ``read`` opens the existing log without creating, chmodding, pruning or rewriting
    anything and reads it from its end. Retention (TTL, the last ``max_entries``, the
    ``max_bytes`` suffix) is applied in memory exactly as ``topics._topic_state`` does,
    counted from the end of the file. ``items`` are the retained entries read, oldest
    first (an oversized line appears as ``{"seq": n or None, "oversized": True}``);
    ``page`` is the selection for the request; ``partial`` is true when the allowance
    stopped the read before the request was satisfied or the retention boundary was
    established."""

    def __init__(self, key, budget=None, now=None):
        self.key = key
        self.budget = budget if budget is not None else Budget()
        self.now = now
        self.topic = sp_channels.topic_name(key)
        stem = sp_topics.topic_stem(self.topic, sp_channels.history_dir())
        self.log_path = stem + ".jsonl"
        self.meta_path = stem + ".meta.json"
        self.exists = False
        self.unavailable = None
        self.items = []
        self.extra = []           # complete entries found by rechecks, newer than ``items``
        self.page = []
        self.next_before = None
        self.partial = False
        self.stop_reason = None
        self.gap = None
        self.oversized = 0
        self.covered_all = False  # the file start or the retention boundary was reached
        self.next_seq = 1
        self.last_seq = None      # the newest valid seq read
        self.fingerprint = None
        self.topic_bytes = 0
        self._end = 0
        self._consumed_end = None
        self._anchor = False

    # -- meta -------------------------------------------------------------

    def _read_meta_next(self):
        try:
            fd, st = _open_regular(self.meta_path)
        except OSError:
            return None
        if fd is None:
            return None
        try:
            if st.st_size > META_MAX or st.st_size > self.budget.remaining():
                return None
            data = os.read(fd, META_MAX + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        self.budget.charge(len(data))
        try:
            meta = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            return None
        if isinstance(meta, dict) and isinstance(meta.get("next_seq"), int):
            return meta["next_seq"]
        return None

    # -- read -------------------------------------------------------------

    def read(self, before=None, limit=PAGE_MAX, since=None):
        """Read the newest entries. Page mode (``since`` None) selects the newest ``limit``
        retained entries with seq < ``before`` and peeks one more to know whether older
        ones exist (``next_before``). Since mode selects every retained entry with seq >
        ``since`` and reads back only as far as the cursor."""
        meta_next = self._read_meta_next()
        cap = min(TOPIC_BUDGET, self.budget.remaining())
        try:
            fd, st = _open_regular(self.log_path)
        except OSError as exc:
            self.unavailable = "%s" % (exc.strerror or exc)
            self.partial = True
            self._finish_seq(meta_next)
            return self
        if fd is None:
            self.covered_all = True
            self._finish_seq(meta_next)
            if since is not None:
                self._since_gap(since)
            return self
        self.exists = True
        self.fingerprint = (st.st_size, st.st_ino, st.st_mtime_ns)
        self._end = st.st_size
        try:
            self._scan(fd, st.st_size, cap, before, limit, since)
        finally:
            os.close(fd)
        self._finish_seq(meta_next)
        if since is not None:
            self._since_gap(since)
        return self

    def _finish_seq(self, meta_next):
        base = meta_next if isinstance(meta_next, int) else 1
        if self.last_seq is not None:
            base = max(base, self.last_seq + 1)
        self.next_seq = base

    def _scan(self, fd, end, cap, before, limit, since):
        ttl_days, max_entries, max_bytes = sp_topics._topic_limits()
        cutoff = (self.now if self.now is not None else time.time()) - ttl_days * 86400.0
        reader = _BackReader(fd, end, self.budget, cap)
        newest_first = []
        retained, total, qualifying = 0, 0, 0
        stopped = None
        boundary = False
        reached_start = False
        anchor = False
        while True:
            kind, payload = reader.next_line(need=since is None and qualifying == 0)
            if kind == "end":
                reached_start = True
                break
            if kind == "stop":
                stopped = payload
                if reader.unfinished_oversized:
                    self.oversized += 1
                    newest_first.append({"seq": None, "oversized": True})
                break
            if kind == "oversized":
                self.oversized += 1
                item = {"seq": _head_seq(payload), "oversized": True}
                size = ENTRY_LINE_MAX + 1
            else:
                decoded = _decode_line(payload)
                if decoded is None:
                    continue
                item, size = decoded
                if self.last_seq is None:
                    self.last_seq = item["seq"]
                if (sp_runtime.parse_time(item.get("ts")) or 0.0) < cutoff:
                    continue
            retained += 1
            if retained > max_entries:
                boundary = True
                break
            total += size
            if total > max_bytes:
                boundary = True
                break
            newest_first.append(item)
            seq = item.get("seq")
            if since is None:
                if before is None or (isinstance(seq, int) and seq < before):
                    qualifying += 1
                    if qualifying >= limit + 1:
                        break
            elif isinstance(seq, int) and seq <= since and "oversized" not in item:
                anchor = True
                break
        self.topic_bytes = reader.read
        self._consumed_end = reader.complete_end if reader.seen_end else None
        if reader.seen_end is False and reached_start:
            self._consumed_end = 0
        self.items = newest_first[::-1]
        self.stop_reason = stopped
        self.covered_all = reached_start or boundary
        if since is None:
            self._select_page(newest_first, before, limit, stopped)
        else:
            self.page = [i for i in self.items if i.get("seq") is None or i["seq"] > since]
            self.partial = stopped is not None and not anchor and not self.covered_all
            self._anchor = anchor

    def _select_page(self, newest_first, before, limit, stopped):
        qual = [
            i for i in newest_first
            if before is None or (isinstance(i.get("seq"), int) and i["seq"] < before)
        ]
        chosen = qual[:limit]
        self.page = chosen[::-1]
        oldest = chosen[-1].get("seq") if chosen else None
        if len(qual) > limit:
            self.next_before = oldest
            if oldest is None:
                self.partial = True  # an older page exists but cannot be named
        elif stopped is not None and not self.covered_all:
            self.partial = True
            if chosen:
                self.next_before = oldest
            elif before is not None:
                self.gap = {"before": before, "reason": "budget"}

    def _since_gap(self, since):
        last = self.next_seq - 1
        seqs = [i["seq"] for i in self.items if isinstance(i.get("seq"), int)]
        if self.covered_all:
            oldest = seqs[0] if seqs else self.next_seq
            if since + 1 < oldest and since < last:
                self.gap = {"from": since + 1, "to": oldest - 1, "reason": "pruned"}
        elif not self._anchor and (not seqs or seqs[0] > since + 1):
            self.gap = {"from": since + 1, "to": (seqs[0] - 1) if seqs else last, "reason": "budget"}

    # -- outcome index ------------------------------------------------------

    def _covers_after(self, seq):
        """True when every retained entry after ``seq`` is among the entries read."""
        position = None
        for index, item in enumerate(self.items):
            if item.get("seq") == seq and "oversized" not in item:
                position = index
        newer = self.items[position + 1:] if position is not None else self.items
        for item in newer:
            if item.get("oversized") and (item.get("seq") is None or item["seq"] > seq):
                return False
        if self.covered_all or position is not None:
            return True
        oldest = next((i["seq"] for i in self.items if isinstance(i.get("seq"), int)), None)
        return oldest is not None and oldest <= seq

    def find_outcome(self, message_id, after_seq):
        """``("found", entry)``, ``("none", None)`` or ``("uncovered", reason)``.

        The first outcome entry of ``message_id`` after ``after_seq`` among what was
        read. When none is there and the read did not cover every retained entry after
        the send (the allowance stopped it, or an oversized line may hide one), the
        answer is ``uncovered``; the caller never resolves it by a further unbounded read."""
        found = _first_outcome(self.items, message_id, after_seq)
        if found is None:
            found = _first_outcome(self.extra, message_id, after_seq)
        if found is not None:
            return "found", found
        if not self._covers_after(after_seq):
            return "uncovered", "outcome lies outside what was read"
        return "none", None

    # -- recheck ------------------------------------------------------------

    def recheck(self):
        """Newer complete entries appended since the last read: ``("ok", [entries])`` or
        ``("unverified", reason)``. Same file: only the bytes after the last consumed
        newline are read. A replaced or truncated file is read backward until a seq at or
        below the last seq read. Bounded by the request budget; each call reads only bytes
        no earlier call read."""
        allow = min(self.budget.remaining(), TOPIC_BUDGET)
        try:
            fd, st = _open_regular(self.log_path)
        except OSError as exc:
            return "unverified", "history unreadable: %s" % (exc.strerror or exc)
        if fd is None:
            return ("ok", []) if self.fingerprint is None else ("unverified", "history is gone")
        try:
            if self.fingerprint is None:
                start = 0
            elif st.st_ino == self.fingerprint[1] and st.st_size >= self._end:
                if st.st_size == self._end:
                    return "ok", []
                start = self._consumed_end
                if start is None:
                    return "unverified", "the end of the read is unknown"
            else:
                return self._recheck_replaced(fd, st, allow)
            count = st.st_size - start
            if count > allow:
                return "unverified", "newer entries exceed the request budget"
            data = os.pread(fd, count, start)
            if len(data) != count:
                return "unverified", "history changed while it was being read"
            self.budget.charge(count)
            end = data.rfind(b"\n")
            entries = []
            for raw in data[: end + 1].split(b"\n")[:-1] if end >= 0 else []:
                decoded = _decode_line(raw)
                if decoded is not None:
                    entries.append(decoded[0])
            self.extra.extend(entries)
            self._rebase(st, start + end + 1 if end >= 0 else start, entries)
            return "ok", entries
        finally:
            os.close(fd)

    def _rebase(self, st, consumed_end, entries):
        self.fingerprint = (st.st_size, st.st_ino, st.st_mtime_ns)
        self._end = st.st_size
        self._consumed_end = consumed_end
        self.exists = True
        for entry in entries:
            self.last_seq = entry["seq"]
        if self.last_seq is not None:
            self.next_seq = max(self.next_seq, self.last_seq + 1)

    def _recheck_replaced(self, fd, st, allow):
        reader = _BackReader(fd, st.st_size, self.budget, allow)
        newest_first = []
        reached = False
        while True:
            kind, payload = reader.next_line(need=True)
            if kind == "end":
                reached = True
                break
            if kind == "stop":
                break
            if kind == "oversized":
                return "unverified", "an oversized line in the rewritten history"
            decoded = _decode_line(payload)
            if decoded is None:
                continue
            if self.last_seq is not None and decoded[0]["seq"] <= self.last_seq:
                reached = True
                break
            newest_first.append(decoded[0])
        if not reached:
            return "unverified", "the rewritten history exceeds the request budget"
        entries = newest_first[::-1]
        self.extra.extend(entries)
        self._rebase(st, reader.complete_end or 0, entries)
        return "ok", entries


# --------------------------------------------------------------------------
# Journal (ACK) join
# --------------------------------------------------------------------------


class JournalContext:
    """The per-message ACK and transport-error state from one bounded journal read."""

    def __init__(self, contexts=None, available=True, prefix_unread=False, budget_skipped=False):
        self.contexts = contexts or {}
        self.available = available
        self.prefix_unread = prefix_unread
        self.budget_skipped = budget_skipped

    def acks_for(self, message_id):
        """``(acks, transport_errors, partial)``; ``acks`` is ``{slot: acked|pending}`` or None (unverified)."""
        if not self.available:
            return None, {}, self.budget_skipped
        ctx = self.contexts.get(message_id)
        if ctx is not None and ctx.get("state") == "found":
            acked = set(ctx.get("acked") or ())
            acks = {slot: ("acked" if slot in acked else "pending") for slot in ctx.get("to") or ()}
            return acks, dict(ctx.get("transport_errors") or {}), False
        return None, {}, ctx is not None and ctx.get("state") == "unreached"


def read_journal_context(budget, want):
    """One ``journal.read_since(None, remaining, want=...)``; its bytes are charged to ``budget``.

    A journal that cannot be read, or no budget left for it, makes every ACK unverified."""
    ids = []
    for mid in want or ():
        if isinstance(mid, str) and mid not in ids:
            ids.append(mid)
    if not ids:
        return JournalContext()
    if budget.remaining() <= 0:
        return JournalContext(available=False, budget_skipped=True)
    try:
        result = sp_journal.read_since(None, budget.remaining(), want=ids)
    except Exception:  # an unreadable journal only makes ACK state unverified
        return JournalContext(available=False)
    budget.charge(result.get("bytes") or 0)
    return JournalContext(result.get("context") or {}, True, result.get("journal_prefix") == "unread")


# --------------------------------------------------------------------------
# Send state
# --------------------------------------------------------------------------


def _probe(message_id):
    try:
        return sp_channels._probe_send_lock(message_id)
    except (OSError, ValueError, TypeError):
        return "error"


def send_state(snapshot, entry):
    """``(state, outcomes, partial)`` of one ``channel.send`` entry with the rules of ``channels.delivery_view``.

    ``partial`` is true when the answer is ``unverified`` because the snapshot could
    not reach the outcome or a recheck exceeded the budget."""
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if data.get("delivery") == "none":
        return sp_channels.OUTCOME_RECORDED, {}, False
    seq = entry.get("seq")
    if not isinstance(seq, int):
        return sp_channels.STATE_UNVERIFIED, {}, False
    mid = data.get("message_id")
    kind, found = snapshot.find_outcome(mid, seq)
    if kind == "found":
        return "complete", sp_channels._outcomes_of(found), False
    if kind == "uncovered":
        return sp_channels.STATE_UNVERIFIED, {}, True
    probe = _probe(mid)
    if probe in ("held", "error"):
        return sp_channels.send_state_from(False, probe, False), {}, False
    status, _new = snapshot.recheck()
    if status != "ok":
        return sp_channels.STATE_UNVERIFIED, {}, True
    again = _first_outcome(snapshot.extra, mid, seq)
    state = sp_channels.send_state_from(False, probe, again is not None)
    return state, (sp_channels._outcomes_of(again) if again is not None else {}), False


def resolve_send(snapshot, entry, journal_ctx=None):
    """The reader's view of one ``channel.send`` entry:
    ``{"state", "recipients": [{"recipient", "state", "ack"?, "transport_error"?}], "partial"?}``.

    The outcome index is the first ``channel.outcome`` after the send with its message
    id, from the snapshot only. ``journal_ctx`` (from ``read_journal_context``) supplies
    the ACKs; None reads them as unverified."""
    state, outcomes, partial = send_state(snapshot, entry)
    return _delivery(entry, state, outcomes, partial, journal_ctx)


def _delivery(entry, state, outcomes, partial, journal_ctx):
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    acks, errors = None, {}
    if data.get("journaled"):
        if journal_ctx is not None:
            acks, errors, ack_partial = journal_ctx.acks_for(data.get("message_id"))
            partial = partial or ack_partial
        else:
            partial = True
    rows = sp_channels.delivery_rows(data, state, outcomes, acks)
    for row in rows:
        detail = errors.get(row["recipient"])
        if detail is not None:
            row["transport_error"] = str(detail)
    view = {"state": state, "recipients": rows}
    if partial:
        view["partial"] = True
    return view


# --------------------------------------------------------------------------
# Entry facts
# --------------------------------------------------------------------------

_KINDS = {
    sp_channels.EVENT_CREATED: "created",
    sp_channels.EVENT_JOINED: "joined",
    sp_channels.EVENT_LEFT: "left",
    sp_channels.EVENT_REMOVED: "removed",
    sp_channels.EVENT_HANDOVER: "handover",
    sp_channels.EVENT_SEND: "send",
    sp_channels.EVENT_OUTCOME: "outcome",
}
_MEMBERSHIP = ("created", "joined", "left", "removed", "handover")

_APPROVE_RE = re.compile(r"^APPROVE\b")
_REVISE_RE = re.compile(r"^REVISE: (\d+)")
_BLOCK_RE = re.compile(r"^BLOCK\b")
_SHA_RE = re.compile(r"\bat ([0-9a-fA-F]{7,40})\b")
_LEAD_RE = re.compile(r"^[\s:\-\u2013\u2014]+")


def entry_kind(raw_kind):
    """The short kind of a history entry (``send``, ``outcome``, ``joined``, ...; ``other`` otherwise)."""
    return _KINDS.get(raw_kind, "other")


def membership_text(kind, data):
    """The one line shown for a membership entry; the caller escapes it."""
    data = data if isinstance(data, dict) else {}
    member = data.get("member")
    if kind == "channel.created" or kind == "created":
        return "created"
    if kind in ("channel.joined", "joined"):
        return "%s joined" % member
    if kind in ("channel.left", "left"):
        return "%s left" % member
    if kind in ("channel.removed", "removed"):
        return "%s removed" % member
    if kind in ("channel.handover", "handover"):
        return "%s handed over to %s" % (member, data.get("to"))
    return ""


def detect_verdict(body):
    """``{"verdict", "label", "text", "sha"}`` for a body that starts with APPROVE, REVISE: N or BLOCK, else None."""
    if not isinstance(body, str):
        return None
    for regex, name in ((_APPROVE_RE, "approve"), (_REVISE_RE, "revise"), (_BLOCK_RE, "block")):
        match = regex.match(body)
        if match:
            rest = _LEAD_RE.sub("", body[match.end():])
            sha = _SHA_RE.search(rest)
            return {"verdict": name, "label": match.group(0), "text": rest, "sha": sha.group(1) if sha else None}
    return None


def ack_tally(rows):
    """``{"acked", "total", "missing": [slots], "unverified"}`` of a journaled post's rows.

    A refused recipient is outside the denominator, as in the CLI."""
    total, acked, missing, unverified = 0, 0, [], 0
    for row in rows:
        if "ack" not in row or str(row.get("state", "")).startswith(sp_channels.OUTCOME_REFUSED):
            continue
        total += 1
        if row["ack"] == sp_channels.STATE_ACKED:
            acked += 1
        else:
            missing.append(row["recipient"])
            if row["ack"] == sp_channels.STATE_UNVERIFIED:
                unverified += 1
    return {"acked": acked, "total": total, "missing": missing, "unverified": unverified}


def channel_coverage(members):
    """Per-channel coverage flags; ``native_claude`` is true when any member is a Claude Code session."""
    return {"native_claude": any(isinstance(m, str) and m.startswith("cc:") for m in members or ())}


def _actor(entry):
    sid = entry.get("from_sid")
    return "%s:%s" % (entry.get("from_kind"), sid) if sid else "anonymous"


def _other_text(entry, data):
    if isinstance(data.get("body"), str):
        return data["body"]
    if "text" in entry and isinstance(entry.get("text"), str):
        return entry["text"]
    try:
        return json.dumps(data, sort_keys=True)[:2048]
    except (TypeError, ValueError):
        return ""


def entry_view(entry, delivery=None, general=False):
    """One history entry as dashboard JSON (before string hygiene)."""
    if entry.get("oversized"):
        return {"seq": entry.get("seq"), "kind": "oversized", "oversized": True}
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    raw = entry.get("kind") if isinstance(entry.get("kind"), str) else ""
    kind = entry_kind(raw)
    out = {"seq": entry.get("seq"), "ts": entry.get("ts"), "kind": kind, "event": raw, "actor": _actor(entry)}
    if kind in _MEMBERSHIP:
        out["text"] = membership_text(kind, data)
        if "member" in data:
            out["member"] = data.get("member")
    elif kind == "send":
        body = data.get("body") if isinstance(data.get("body"), str) else ""
        out.update(body=body, sender=data.get("sender"), message_id=data.get("message_id"),
                   journaled=bool(data.get("journaled")), verdict=detect_verdict(body))
        if delivery is not None:
            out["delivery"] = delivery
            if sp_channels.ALL_RE.match(body):
                out["tally"] = ack_tally(delivery["recipients"])
        if general:
            gaps = data.get("discovery_incomplete")
            out["coverage"] = {
                "opencode": data.get("opencode_coverage"),
                "discovery_incomplete": [g for g in gaps if isinstance(g, str)] if isinstance(gaps, list) else [],
            }
    elif kind == "outcome":
        outcomes = data.get("outcomes") if isinstance(data.get("outcomes"), dict) else {}
        out.update(message_id=data.get("message_id"), outcomes={str(k): str(v) for k, v in outcomes.items()},
                   text="outcomes of message %s for %d recipients" % (data.get("message_id"), len(outcomes)))
    else:
        out["text"] = _other_text(entry, data)
    return out


# --------------------------------------------------------------------------
# Channel page
# --------------------------------------------------------------------------


def _check_key(key):
    if not isinstance(key, str) or not sp_constants.CHANNEL_NAME_RE.fullmatch(key):
        raise WebDataError("not a channel name", 400)
    if key != sp_constants.CHANNEL_GENERAL and key not in sp_channels.list_keys():
        raise WebDataError("no such channel", 404)


def _int_arg(value, name, low, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        raise WebDataError("%s must be an integer between %d and %s" % (name, low, high if high is not None else "max"))
    return value


def channel_page(key, before=None, limit=PAGE_MAX, budget=None, members=None, now=None):
    """One page of a channel's history, oldest entry first, as JSON-ready data.

    ``{"channel", "info", "entries", "next", "partial", "gap", "oversized", "coverage",
    "journal_partial", "bytes"}``. ``next`` is the ``before`` cursor of the next older
    page, or None. ``partial`` is true when the byte budget left something out or a
    send could not be verified. ``members`` supplies #general's members (derived
    elsewhere); a stored channel's come from its membership record."""
    _check_key(key)
    limit = _int_arg(limit, "limit", 1, PAGE_MAX)
    if before is not None:
        before = _int_arg(before, "before", 0)
    budget = budget if budget is not None else Budget()
    general = key == sp_constants.CHANNEL_GENERAL
    snapshot = HistorySnapshot(key, budget, now=now).read(before=before, limit=limit)
    partial = snapshot.partial
    states = {}
    for entry in snapshot.page:
        if entry.get("kind") == sp_channels.EVENT_SEND:
            states[entry["seq"]] = send_state(snapshot, entry)
    want = [
        e["data"].get("message_id") for e in snapshot.page
        if e.get("kind") == sp_channels.EVENT_SEND and isinstance(e.get("data"), dict) and e["data"].get("journaled")
    ]
    journal_ctx = read_journal_context(budget, want)
    views = []
    for entry in snapshot.page:
        delivery = None
        if entry.get("kind") == sp_channels.EVENT_SEND:
            state, outcomes, send_partial = states[entry["seq"]]
            delivery = _delivery(entry, state, outcomes, send_partial, journal_ctx)
            partial = partial or bool(delivery.get("partial"))
        views.append(entry_view(entry, delivery, general))
    record = None if general else sp_channels.read_channel(key)
    if members is None and record is not None:
        members = record["members"]
    info = {"key": key}
    if record is not None:
        info.update(display=record["display"], kind=record["kind"], owner=record["owner"], members=list(record["members"]))
    coverage = channel_coverage(members) if members is not None else {"native_claude": None}
    return _clean({
        "channel": key, "info": info, "entries": views, "next": snapshot.next_before,
        "partial": partial, "gap": snapshot.gap, "oversized": snapshot.oversized,
        "coverage": coverage, "journal_partial": journal_ctx.prefix_unread, "bytes": budget.used,
    })


# --------------------------------------------------------------------------
# Member details
# --------------------------------------------------------------------------

_RUNTIMES = {"cc": "Claude Code", "codex": "Codex", "oc": "OpenCode"}


def _read_shim_state(path):
    try:
        fd, st = _open_regular(path)
    except OSError:
        return None
    if fd is None:
        return None
    try:
        if st.st_size > SHIM_STATE_MAX:
            return None
        data = os.pread(fd, st.st_size, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        state = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return state if isinstance(state, dict) else None


def codex_reply_budget(tid, now=None):
    """The reply budget toward a Codex thread's current requester, as last saved by its shim, or None.

    Reads ``<home>/session-peers/<tid>.json`` of the home the shim runs under and its
    pending-marker files by path; nothing is created, and the shim's own state is only
    replayed in memory."""
    if not isinstance(tid, str) or not sp_constants.UUID_RE.fullmatch(tid):
        return None
    home = sp_storage.thread_home(tid)
    base = os.path.join(home, "session-peers")
    state = _read_shim_state(os.path.join(base, "%s.json" % tid))
    if state is None:
        return None
    sid = state.get("budget_sender_sid")
    if not isinstance(sid, str) or not sid:
        return {"requester": None, "label": BUDGET_LABEL}
    owner = types.SimpleNamespace(thread_id=tid)
    try:
        meter = sp_budgets.ReplyBudget(owner, state, records=None, socket_valid=None, deliver_frame=None)
        meter.configure_window()
    except (TypeError, ValueError, AttributeError, KeyError):
        return None
    expired = meter._sequence_expired(now if now is not None else time.time())
    if expired:
        meter.budgets = {}
    used = 0 if expired else meter.budgets.get(sid, 0)
    cap = meter.cap_for(sid)
    pending = [
        name for name, suffix in (("reset", "budget-reset"), ("allow", "budget-allow"), ("binding", "budget-binding"))
        if os.path.lexists(os.path.join(base, "%s.%s" % (tid, suffix)))
    ]
    return {
        "requester": sid, "used": used, "cap": cap, "remaining": max(0, cap - used),
        "expired": expired, "pending": bool(pending), "pending_kinds": pending, "label": BUDGET_LABEL,
    }


def member_details(typed, liveness, activity=None, now=None):
    """The details panel data for one member; liveness and activity come from the caller (discovery is elsewhere).

    ``state`` is ``gone`` when liveness says so, else ``working`` or ``idle`` from
    ``activity``, else ``unverified``."""
    typed = typed if isinstance(typed, str) else str(typed)
    kind, _sep, ident = typed.partition(":")
    if isinstance(activity, dict):
        activity = activity.get("state")
    if liveness == "gone":
        state = "gone"
    elif activity in ("working", "idle"):
        state = activity
    else:
        state = "unverified"
    out = {
        "member": typed, "address": typed, "runtime": _RUNTIMES.get(kind, "unknown"),
        "liveness": liveness if liveness in ("live", "gone") else "unverified", "state": state,
    }
    if kind == "codex":
        out["reply_budget"] = codex_reply_budget(ident, now)
    return _clean(out)
