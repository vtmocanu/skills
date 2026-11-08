"""Read model for the session-peers web dashboard (PRD #161).

Pure functions and small per-request objects that build every view's JSON from
the existing stores. Nothing here holds state between requests or writes
anything: history is read through a lock-free, bounded snapshot reader that
never calls ``channels.read_history``, ``topics.topic_tail`` or
``topics._topic_paths`` (they lock, create directories and may rewrite the log).
The only lock touched is the non-blocking per-send flock probe (``channels._probe_send_lock``), released
immediately. ``storage.thread_home`` (its ``_pidfile_is_held`` pidfile probe and its scan of every Claude
shim record) is never called here: the caller supplies each Codex thread's home (``discovery["homes"]``,
``member_details(..., home=...)``), and a thread without one has an unknown reply budget.

One request owns one ``Budget`` (REQUEST_BUDGET bytes). Every topic read, the
meta read, the outcome recheck and the journal read charge it, and each topic is
read at most TOPIC_BUDGET bytes from its end, except that a request never
returns nothing for want of bytes alone: when no whole entry fits it reads on to
the first whole line (at most ENTRY_LINE_MAX) while the request budget can hold
it, else it stops ``partial``.
"""

from __future__ import annotations
import base64
import binascii
import errno
import functools
import hashlib
import json
import math
import os
import re
import stat
import time
import types
import unicodedata
from . import budgets as sp_budgets, channels as sp_channels, constants as sp_constants
from . import journal as sp_journal, runtime as sp_runtime, topics as sp_topics

REQUEST_BUDGET = 8 * 1024 * 1024
HYDRATION_MARGIN = 8  # a first page's reserved bytes: its size plus 1/HYDRATION_MARGIN
TOPIC_BUDGET = 1024 * 1024
ENTRY_LINE_MAX = 6 * sp_constants.MAX_TEXT_CHARS + 512 * 1024
PAGE_MAX = 200
META_MAX = 64 * 1024
SHIM_STATE_MAX = 1024 * 1024
_CHUNK = 64 * 1024
_HEAD = 4096

CURSOR_MAX = 48 * 1024
CURSOR_KEYS_MAX = 256
CURSOR_UPDATES_MAX = 64
SEQ_LIMIT = 2 ** 53
JOURNAL_TAIL_BUDGET = 2 * 1024 * 1024
BASELINE_BUDGET = 256 * 1024
BATCH_CHANNELS = 32
SESSIONS_MAX = 512
BATCH_ENTRIES = 25
ALERT_WINDOW = 25
SEARCH_MATCHES_MAX = PAGE_MAX
TOKEN_MAX = 16 * 1024
QUERY_MAX = 256

BUDGET_LABEL = "as last saved by the shim"


class WebDataError(Exception):
    """A refused read-model request; ``status`` is the HTTP status the server maps it to."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Identifier hygiene
# --------------------------------------------------------------------------

# Characters shown escaped in identifiers: Cf (format) plus the Default_Ignorable_Code_Point
# characters Unicode files elsewhere, as (low, high) code point ranges.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x2800, 0x2800), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0),
    (0x1BCA0, 0x1BCA3), (0xE0000, 0xE0FFF), (0x2065, 0x2065), (0xFFF0, 0xFFF8),
)
_BIDI_CODES = frozenset(
    list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) + [0x200E, 0x200F, 0x061C, 0x2028, 0x2029])
# A message body keeps these only in context (see ``_body_keeps``): emoji joiners, variation selectors and
# subdivision-flag tag characters.
_ZWNJ, _ZWJ, _VS15, _VS16 = 0x200C, 0x200D, 0xFE0E, 0xFE0F
# The only subdivision flags that render (RGI): England, Scotland, Wales. Any other tag run
# shows as a plain black flag, so escaping it hides nothing a reader could see.
_FLAG_RE = re.compile("\U0001F3F4(?:%s)\U000E007F" % "|".join(
    "".join(chr(0xE0000 + ord(c)) for c in code) for code in ("gbeng", "gbsct", "gbwls")))
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x1F1E6, 0x1F1FF), (0x2190, 0x21FF), (0x2B00, 0x2BFF),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x00A9, 0x00A9),
    (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x231A, 0x23FF), (0x25AA, 0x25FE), (0x2934, 0x2935), (0x1F3FB, 0x1F3FF),
)
_INVISIBLE_RE = re.compile(
    "[" + "".join("\\U%08x-\\U%08x" % pair for pair in _INVISIBLE_RANGES) + "]")
_RUN_RE = re.compile("[^\u0020-\u007e]+")


@functools.lru_cache(maxsize=8192)
def _hidden(code):
    """True for a Cf character or one of the listed invisible ones."""
    if any(low <= code <= high for low, high in _INVISIBLE_RANGES):
        return True
    return unicodedata.category(chr(code)) in ("Cf", "Cs")


def _is_emoji(code):
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def _body_keeps(text, index, flags):
    """True when the hidden character at ``text[index]`` is legitimate in a message body: a tag character
    inside a well-formed subdivision-flag sequence (``flags`` holds those indices), a ZWJ between two emoji,
    a ZWNJ between two non-ASCII letters, or a variation selector after an emoji or a keycap base.
    Residual, accepted: a ZWJ between emoji and a ZWNJ between non-ASCII letters can each carry
    one hidden bit per visible pair, and a selector (or none) after an emoji about 1.6 bits per emoji."""
    code = ord(text[index])
    if code in _BIDI_CODES:
        return False
    if 0xE0020 <= code <= 0xE007F:
        return index in flags
    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if code == _ZWJ:
        if before and ord(before) == _VS16 and index > 1:
            before = text[index - 2]
        return bool(before and after and _is_emoji(ord(before)) and _is_emoji(ord(after)))
    if code == _ZWNJ:
        return bool(before and after and ord(before) > 0x7F and ord(after) > 0x7F
                    and unicodedata.category(before).startswith("L") and unicodedata.category(after).startswith("L"))
    if code in (_VS15, _VS16):
        if not before:
            return False
        prior = ord(before)
        # Only after an emoji, or a keycap base followed by U+20E3: anywhere else a selector
        # would carry hidden bits after ordinary text.
        return _is_emoji(prior) or (before in "0123456789#*" and after == "\u20e3")
    return False


def _escape_char(text, index, body, flags):
    ch = text[index]
    code = ord(ch)
    if code <= 0x1F or 0x7F <= code <= 0x9F or code in _BIDI_CODES:
        if body and code in (0x09, 0x0A):
            return ch
        return "\\u%04x" % code
    if _hidden(code) and not (body and _body_keeps(text, index, flags)):
        return "\\u%04x" % code if code <= 0xFFFF else "\\U%08x" % code
    return ch


def _escape_run(match, body, text, flags):
    run = match.group(0)
    if run.isprintable() and not _INVISIBLE_RE.search(run):
        return run
    start = match.start()
    return "".join(_escape_char(text, start + offset, body, flags) for offset in range(len(run)))


def _escape(value, body):
    text = value if isinstance(value, str) else str(value)
    flags = frozenset(i for m in _FLAG_RE.finditer(text) for i in range(m.start() + 1, m.end())) if body else ()
    return _RUN_RE.sub(lambda m: _escape_run(m, body, text, flags), text)


def escape_text(value):
    """``value`` as a string with control, bidi, line-separator and invisible format characters shown as
    ``\\uXXXX`` (``\\UXXXXXXXX`` for characters beyond the BMP)."""
    return _escape(value, False)


def escape_body(value):
    """Like ``escape_text`` but a message body keeps its newlines and tabs and its emoji joiners,
    variation selectors and subdivision-flag tag characters."""
    return _escape(value, True)


_BODY_KEYS = ("body", "text")


CLEAN_DEPTH = 32


# Subtrees whose keys and values are peer-written data: they are escaped as identifiers, never as bodies.
_PLAIN_KEYS = frozenset(["outcomes", "coverage"])


def _clean(value, key=None, depth=0, plain=False):
    """``value`` with every string escaped; containers nested deeper than CLEAN_DEPTH become a placeholder.

    A string under a key named ``body`` or ``text`` is escaped as a message body, except inside an
    ``outcomes`` or ``coverage`` subtree, where such a key is only peer-written data."""
    if isinstance(value, (dict, list, tuple)) and depth >= CLEAN_DEPTH:
        return "[nested too deeply]"
    if isinstance(value, dict):
        return {escape_text(k): _clean(v, k, depth + 1, plain or k in _PLAIN_KEYS) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v, None, depth + 1, plain) for v in value]
    if isinstance(value, str):
        return escape_body(value) if key in _BODY_KEYS and not plain else escape_text(value)
    return value


def _ident(value):
    """A peer-written identifier field as a string, else None (never a nested structure)."""
    return value if isinstance(value, str) else None


def _stamp(value):
    """Retention time of a history ``ts``: ``(runtime.parse_time(ts) or 0.0)`` as ``topics._topic_state`` reads it.

    A value too large or malformed to convert, and NaN (never ``>= cutoff``), read as 0.0. An
    infinite time stays infinite: +inf never expires, -inf does, as in the CLI."""
    try:
        seconds = sp_runtime.parse_time(value)
    except (OverflowError, ValueError, TypeError):
        return 0.0
    if seconds is None or seconds != seconds:
        return 0.0
    return seconds


_TS_INT_MAX = 2 ** 63


def _ts_view(value):
    """The ``ts`` shown for an entry: a string, a finite float, an int of sane size, else None."""
    if isinstance(value, str) or (isinstance(value, float) and math.isfinite(value)):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) < _TS_INT_MAX:
        return value
    return None


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


def _read_meta(meta_path, budget):
    """The topic meta file as a dict (at most META_MAX bytes, charged to ``budget``), else None."""
    try:
        fd, st = _open_regular(meta_path)
    except OSError:
        return None
    if fd is None:
        return None
    try:
        if st.st_size > META_MAX or st.st_size > budget.remaining():
            return None
        data = os.pread(fd, st.st_size, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > budget.remaining():
        return None
    budget.charge(len(data))
    try:
        meta = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return meta if isinstance(meta, dict) else None


def _read_record(key, budget):
    """The membership record of stored channel ``key`` as ``channels.read_channel`` validates it, else None.

    Own reader (``read_channel`` opens the file with a plain ``open``, which a planted FIFO
    would block): no-follow, non-blocking, regular files only, at most
    ``channels.RECORD_MAX_BYTES``, and the bytes read are charged to ``budget``. A file that
    does not fit the remaining budget reads as None."""
    try:
        fd, st = _open_regular(os.path.join(sp_channels.channels_root(), key + ".json"))
    except OSError:
        return None
    if fd is None:
        return None
    try:
        if st.st_size > sp_channels.RECORD_MAX_BYTES or st.st_size > budget.remaining():
            return None
        data = os.pread(fd, st.st_size, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > budget.remaining():
        return None
    budget.charge(len(data))
    try:
        rec = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict) or rec.get("key") != key or not isinstance(rec.get("members"), list):
        return None
    if not all(isinstance(rec.get(f), str) for f in ("display", "kind", "owner")):
        return None
    members = []
    for member in rec["members"]:
        if not isinstance(member, str):
            continue
        try:
            member = sp_journal._norm(member)
        except ValueError:
            continue
        if member not in members:
            members.append(member)
    return dict(rec, members=members)


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
                # The first entry is read whole when the request budget can hold it. The read
                # goes at most ENTRY_LINE_MAX bytes past ``cap``, so a line (or a torn tail) that
                # is longer than that is never read to its start.
                room = self.cap + ENTRY_LINE_MAX - self.read
                if room <= 0:
                    return ("stop", "cap")
                self.extending = True
                need = ENTRY_LINE_MAX - (self.part_len if self.seen_end else self.torn)
                remaining = self.budget.remaining()
                if remaining <= 0 or remaining < need:
                    return ("stop", "budget")
                step = min(step, remaining, room)
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
                and isinstance(data, dict) and _ident(data.get("message_id")) == message_id):
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
        meta = _read_meta(self.meta_path, self.budget)
        nxt = meta.get("next_seq") if meta is not None else None
        return nxt if isinstance(nxt, int) else None

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
            kind, payload = reader.next_line(need=(qualifying == 0) if since is None else not newest_first)
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
                if _stamp(item.get("ts")) < cutoff:
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


def read_journal_context(budget, want, with_position=False):
    """One ``journal.read_since(None, remaining, want=...)``; its bytes are charged to ``budget``.

    A journal that cannot be read, or no budget left for it, makes every ACK unverified.
    With ``with_position`` the answer is ``(JournalContext, position)``: ``position`` is
    ``_journal_position`` of that same read, or None when the read raised, was skipped for
    want of budget or ids, or did not complete."""
    ids = []
    for mid in want or ():
        if isinstance(mid, str) and mid not in ids:
            ids.append(mid)

    def answer(context, position=None):
        return (context, position) if with_position else context

    if not ids:
        return answer(JournalContext())
    room = budget.remaining()
    if room <= 0:
        return answer(JournalContext(available=False, budget_skipped=True))
    try:
        result = sp_journal.read_since(None, room, want=ids, strict=True)
    except Exception:  # an unreadable journal only makes ACK state unverified
        return answer(JournalContext(available=False))
    budget.charge(result.get("bytes") or 0)
    context = result.get("context") or {}
    # Partial only when a wanted message was not reached: an early stop once every wanted
    # record is in leaves the journal start unread but loses no ACK this page needs.
    unreached = any(isinstance(c, dict) and c.get("state") == "unreached" for c in context.values())
    position = None
    if result.get("complete"):
        position = _journal_position(result)
        if result.get("oversized") or result.get("window_regression"):
            position[2] = None
    return answer(JournalContext(context, True, result.get("journal_prefix") == "unread" and unreached), position)


# --------------------------------------------------------------------------
# Send state
# --------------------------------------------------------------------------


def _probe(message_id):
    try:
        return sp_channels._probe_send_lock(message_id)
    except (OSError, ValueError, TypeError):
        return "error"


def _text_of(value):
    try:
        return str(value)
    except RecursionError:  # a value nested too deeply to print
        return ""


def _outcomes_of(entry):
    """``channels._outcomes_of`` that survives a value nested too deeply to print."""
    outcomes = (entry.get("data") or {}).get("outcomes")
    if not isinstance(outcomes, dict):
        return {}
    return {_text_of(k): _text_of(v) for k, v in outcomes.items()}


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
    mid = _ident(data.get("message_id"))
    kind, found = snapshot.find_outcome(mid, seq)
    if kind == "found":
        return "complete", _outcomes_of(found), False
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
    return state, (_outcomes_of(again) if again is not None else {}), False


def resolve_send(snapshot, entry, journal_ctx=None):
    """The reader's view of one ``channel.send`` entry:
    ``{"state", "recipients": [{"recipient", "state", "ack"?, "transport_error"?}], "partial"?}``.

    The outcome index is the first ``channel.outcome`` after the send with its message
    id, from the snapshot only. ``journal_ctx`` (from ``read_journal_context``) supplies
    the ACKs; None reads them as unverified."""
    state, outcomes, partial = send_state(snapshot, entry)
    return _delivery(entry, state, outcomes, partial, journal_ctx)


def _rows_data(data):
    """``data`` with the fields ``channels.delivery_rows`` reads reduced to strings and flat lists."""
    excluded = data.get("excluded")
    return dict(
        data,
        recipients=[r for r in data.get("recipients") or () if isinstance(r, str)]
        if isinstance(data.get("recipients"), list) else [],
        excluded=[{"member": _ident(x.get("member")), "reason": _text_of(x.get("reason"))}
                  for x in excluded if isinstance(x, dict)] if isinstance(excluded, list) else [])


def _delivery(entry, state, outcomes, partial, journal_ctx):
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    acks, errors = None, {}
    if data.get("journaled"):
        if journal_ctx is not None:
            acks, errors, ack_partial = journal_ctx.acks_for(_ident(data.get("message_id")))
            partial = partial or ack_partial
        else:
            partial = True
    rows = sp_channels.delivery_rows(_rows_data(data), state, outcomes, acks)
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
    """The one line shown for a membership entry, its identifiers escaped."""
    data = data if isinstance(data, dict) else {}
    member = escape_text(_ident(data.get("member")))
    target = escape_text(_ident(data.get("to")))
    if kind == "channel.created" or kind == "created":
        return "created"
    if kind in ("channel.joined", "joined"):
        return "%s joined" % member
    if kind in ("channel.left", "left"):
        return "%s left" % member
    if kind in ("channel.removed", "removed"):
        return "%s removed" % member
    if kind in ("channel.handover", "handover"):
        return "%s handed over to %s" % (member, target)
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

    A refused recipient is outside the denominator, as in the CLI, and so is one whose ACK was
    never requested (a gone member is a recipient but is not journaled)."""
    total, acked, missing, unverified = 0, 0, [], 0
    for row in rows:
        if "ack" not in row or str(row.get("state", "")).startswith(sp_channels.OUTCOME_REFUSED):
            continue
        if row["ack"] == "not requested":
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
    sid = _ident(entry.get("from_sid"))
    return "%s:%s" % (_ident(entry.get("from_kind")), sid) if sid else "anonymous"


def _other_text(entry, data):
    if isinstance(data.get("body"), str):
        return data["body"]
    if "text" in entry and isinstance(entry.get("text"), str):
        return entry["text"]
    try:
        return json.dumps(data, sort_keys=True)[:2048]
    except (TypeError, ValueError, RecursionError):
        return ""


def entry_view(entry, delivery=None, general=False):
    """One history entry as dashboard JSON (before string hygiene)."""
    if entry.get("oversized"):
        return {"seq": entry.get("seq"), "kind": "oversized", "oversized": True}
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    raw = entry.get("kind") if isinstance(entry.get("kind"), str) else ""
    kind = entry_kind(raw)
    out = {"seq": entry.get("seq"), "ts": _ts_view(entry.get("ts")), "kind": kind, "event": raw, "actor": _actor(entry)}
    if kind in _MEMBERSHIP:
        out["text"] = membership_text(kind, data)
        if "member" in data:
            out["member"] = _ident(data.get("member"))
    elif kind == "send":
        body = data.get("body") if isinstance(data.get("body"), str) else ""
        out.update(body=body, sender=_ident(data.get("sender")), message_id=_ident(data.get("message_id")),
                   journaled=bool(data.get("journaled")), verdict=detect_verdict(body))
        if delivery is not None:
            out["delivery"] = delivery
            if sp_channels.ALL_RE.match(body) and not data.get("targeted"):
                out["tally"] = ack_tally(delivery["recipients"])
        if general:
            gaps = data.get("discovery_incomplete")
            out["coverage"] = {
                "opencode": data.get("opencode_coverage"),
                "discovery_incomplete": [g for g in gaps if isinstance(g, str)] if isinstance(gaps, list) else [],
            }
    elif kind == "outcome":
        outcomes = data.get("outcomes") if isinstance(data.get("outcomes"), dict) else {}
        mid = _ident(data.get("message_id"))
        out.update(message_id=mid, outcomes={escape_text(_text_of(k)): _text_of(v) for k, v in outcomes.items()},
                   text="outcomes of message %s for %d recipients" % (escape_text(mid), len(outcomes)))
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


def _build_views(groups, budget):
    """``([(views, partial)], journal_ctx)`` for ``groups`` of ``(snapshot, entries, general)``.

    Every send's state is resolved first (each may recheck its topic), then one bounded
    journal read joins the ACKs of all journaled sends of all groups."""
    states, want = [], []
    for snapshot, entries, _general in groups:
        found = {}
        for entry in entries:
            if entry.get("kind") == sp_channels.EVENT_SEND:
                found[entry["seq"]] = send_state(snapshot, entry)
                data = entry.get("data")
                if isinstance(data, dict) and data.get("journaled"):
                    want.append(data.get("message_id"))
        states.append(found)
    journal_ctx = read_journal_context(budget, want)
    built = []
    for (snapshot, entries, general), found in zip(groups, states):
        views, partial = [], False
        for entry in entries:
            delivery = None
            if entry.get("kind") == sp_channels.EVENT_SEND:
                state, outcomes, send_partial = found[entry["seq"]]
                delivery = _delivery(entry, state, outcomes, send_partial, journal_ctx)
                partial = partial or bool(delivery.get("partial"))
            views.append(entry_view(entry, delivery, general))
        built.append((views, partial))
    return built, journal_ctx


def channel_page(key, before=None, limit=PAGE_MAX, budget=None, members=None, now=None):
    """One page of a channel's history, oldest entry first, as JSON-ready data.

    ``{"channel", "info", "entries", "next", "partial", "gap", "oversized", "coverage",
    "journal_partial", "bytes"}``. ``next`` is the ``before`` cursor of the next older
    page, or None. ``partial`` is true when the byte budget left something out or a
    send could not be verified. ``members`` supplies #general's members (derived
    elsewhere); a stored channel's come from its membership record. ``info`` of a stored channel
    whose record does not read (or did not fit the budget) is ``{"key", "unreadable": true}`` and the
    page is ``partial``; a roster over CHANNEL_MAX_MEMBERS is cut with ``info["omitted_members"]``."""
    _check_key(key)
    limit = _int_arg(limit, "limit", 1, PAGE_MAX)
    if before is not None:
        before = _int_arg(before, "before", 0)
    budget = budget if budget is not None else Budget()
    general = key == sp_constants.CHANNEL_GENERAL
    snapshot = HistorySnapshot(key, budget, now=now).read(before=before, limit=limit)
    partial = snapshot.partial
    built, journal_ctx = _build_views([(snapshot, snapshot.page, general)], budget)
    views, view_partial = built[0]
    partial = partial or view_partial
    record = None if general else _read_record(key, budget)
    if members is None and record is not None:
        members = record["members"]
    info = {"key": key}
    if members is not None:
        members = list(members)
        omitted = max(0, len(members) - sp_constants.CHANNEL_MAX_MEMBERS)
        members = members[:sp_constants.CHANNEL_MAX_MEMBERS]
        if omitted:
            info["omitted_members"] = omitted
            partial = True
    if record is not None:
        info.update(display=record["display"], kind=record["kind"], owner=record["owner"],
                    members=list(record["members"][:sp_constants.CHANNEL_MAX_MEMBERS]))
    elif not general:  # listed but its record does not read, or the budget skipped it
        info["unreadable"] = True
        partial = True
    coverage = channel_coverage(members) if members is not None else {"native_claude": None}
    return _clean({
        "channel": key, "info": info, "entries": views, "next": snapshot.next_before,
        "partial": partial, "gap": snapshot.gap, "oversized": snapshot.oversized,
        "coverage": coverage, "journal_partial": journal_ctx.prefix_unread, "bytes": budget.used,
    })


# --------------------------------------------------------------------------
# Member details
# --------------------------------------------------------------------------

_RUNTIMES = {"cc": "Claude Code", "codex": "Codex", "oc": "OpenCode", "web": "Browser"}


def _read_shim_state(path, budget):
    """``(state dict or None, skipped)`` of a shim state file; ``skipped`` when it did not fit ``budget``.

    The file's size is charged to ``budget`` before it is read; one that is over SHIM_STATE_MAX
    reads as absent, one that does not fit the remaining budget is skipped unread."""
    try:
        fd, st = _open_regular(path)
    except OSError:
        return None, False
    if fd is None:
        return None, False
    try:
        if st.st_size > SHIM_STATE_MAX:
            return None, False
        if st.st_size > budget.remaining():
            return None, True
        data = os.pread(fd, st.st_size, 0)
    except OSError:
        return None, False
    finally:
        os.close(fd)
    budget.charge(len(data))
    try:
        state = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None, False
    return (state if isinstance(state, dict) else None), False


def _reply_budget(tid, home, now, budget, requester=None):
    """``(reply budget dict or None, skipped)``; see ``codex_reply_budget``."""
    if not isinstance(tid, str) or not sp_constants.UUID_RE.fullmatch(tid) or not isinstance(home, str) or not home:
        return None, False
    base = os.path.join(home, "session-peers")
    state, skipped = _read_shim_state(os.path.join(base, "%s.json" % tid), budget)
    if state is None:
        return None, skipped
    sid = requester if requester is not None else state.get("budget_sender_sid")
    if not isinstance(sid, str) or not sid:
        return {"requester": None, "label": BUDGET_LABEL}, False
    owner = types.SimpleNamespace(thread_id=tid)
    meter = sp_budgets.ReplyBudget(owner, state, records=None, socket_valid=None, deliver_frame=None)
    # The window and the grant check of ``ReplyBudget.configure_window``, without its stderr log
    # line (it names the peer-written sid).
    window = sp_runtime._float_env("SESSION_PEERS_REPLY_BUDGET_WINDOW", sp_constants.REPLY_BUDGET_WINDOW_DEFAULT)
    meter.reply_budget_window = window if window > 0 else sp_constants.REPLY_BUDGET_WINDOW_DEFAULT
    clock = now if now is not None else time.time()
    if meter.allowance and not (
            meter._allowance_waiting(clock)
            or (meter.allowance.get("bound", True) and meter.budget_last_at is not None
                and not meter._sequence_expired(clock))):
        meter.allowance = None
    expired = meter._sequence_expired(clock)
    if expired:
        meter.budgets = {}
    used = 0 if expired else meter.budgets.get(sid, 0)
    cap = meter.cap_for(sid)
    if isinstance(used, bool) or not isinstance(used, int) or isinstance(cap, bool) or not isinstance(cap, int):
        return None, False
    pending = [
        name for name, suffix in (("reset", "budget-reset"), ("allow", "budget-allow"), ("binding", "budget-binding"))
        if os.path.lexists(os.path.join(base, "%s.%s" % (tid, suffix)))
    ]
    return {
        "requester": sid, "used": used, "cap": cap, "remaining": max(0, cap - used),
        "expired": expired, "pending": bool(pending), "pending_kinds": pending, "label": BUDGET_LABEL,
    }, False


def codex_reply_budget(tid, home, now=None, budget=None, requester=None):
    """The reply budget toward a Codex thread's current requester, as last saved by its shim, or None.

    ``requester`` (a sender sid, e.g. the browser peer's) replaces the saved requester: the
    count and cap are then that sender's own.

    ``home`` is the CODEX_HOME the thread's shim runs under, supplied by the caller (finding it
    probes locks and scans shim records, which a request never does). Reads
    ``<home>/session-peers/<tid>.json`` and its
    pending-marker files by path; nothing is created, and the shim's own state is only
    replayed in memory. The state file is charged to ``budget`` (a fresh request budget when
    None); one that does not fit reads as None. Any malformed state reads as None."""
    try:
        return _reply_budget(tid, home, now, budget if budget is not None else Budget(), requester)[0]
    except Exception:  # the file is peer-writable: whatever it holds, the view still works
        return None


def _member_state(liveness, activity):
    if isinstance(activity, dict):
        activity = activity.get("state")
    if liveness == "gone":
        return "gone"
    return activity if activity in ("working", "idle") else "unverified"


def member_details(typed, liveness, activity=None, now=None, budget=None, home=None):
    """The details panel data for one member; liveness, activity and a Codex member's home come from the caller.

    ``state`` is ``gone`` when liveness says so, else ``working`` or ``idle`` from
    ``activity``, else ``unverified``. A Codex member's shim state file (under ``home``) is charged to ``budget``;
    one that does not fit leaves ``reply_budget`` None and sets ``partial``. Without a ``home``
    the budget is unknown: ``reply_budget`` None and ``budget_unknown`` true."""
    typed = typed if isinstance(typed, str) else str(typed)
    kind, _sep, ident = typed.partition(":")
    out = {
        "member": typed, "address": typed, "runtime": _RUNTIMES.get(kind, "unknown"),
        "liveness": liveness if liveness in ("live", "gone") else "unverified",
        "state": _member_state(liveness, activity),
    }
    if kind == "codex":
        budget = budget if budget is not None else Budget()
        if not isinstance(home, str) or not home:
            out["reply_budget"], skipped = None, False
            out["budget_unknown"] = True
        else:
            try:
                out["reply_budget"], skipped = _reply_budget(ident, home, now, budget)
            except Exception:  # the file is peer-writable: whatever it holds, the view still works
                out["reply_budget"], skipped = None, False
        if skipped:
            out["partial"] = True
    return _clean(out)


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FP_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ANCHOR_RE = re.compile(r"^[0-9a-f]{16}$")
_MID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")
_CURSOR_FIELDS = {"v", "c", "j", "u"}
GENERAL = sp_constants.CHANNEL_GENERAL


def _digest(text):
    """First 64 bits of the sha256 of ``text`` as 11 base64url characters."""
    raw = hashlib.sha256(text.encode("utf-8")).digest()[:8]
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text):
    if not isinstance(text, str) or not text or not _B64_RE.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def _is_seq(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < SEQ_LIMIT


def encode_cursor(obj):
    """The opaque cursor text of ``{"v":1,"c":[[key,seq,fp]],"j":[seq,fp,anchor],"u":[[ci,seq,mid]]}``.

    Chat mode adds ``"b":[browser uuid, log seq, status-row seq]``, the Browser feed position."""
    text = _b64(json.dumps(obj, separators=(",", ":"), sort_keys=True))
    if len(text) > CURSOR_MAX:
        raise WebDataError("cursor too large", 500)
    return text


def _validate_cursor(obj, keys, allow_browser=False):
    allowed = [_CURSOR_FIELDS]
    if allow_browser:
        allowed.append(_CURSOR_FIELDS | {"b"})  # chat mode: the Browser feed position
    if not isinstance(obj, dict) or set(obj) not in allowed or type(obj["v"]) is not int or obj["v"] != 1:
        raise ValueError("shape")
    browser = None
    if "b" in obj:
        b = obj["b"]
        if (not isinstance(b, list) or len(b) != 3 or not isinstance(b[0], str) or b[0] != b[0].lower()
                or not sp_constants.UUID_RE.fullmatch(b[0]) or not _is_seq(b[1]) or not _is_seq(b[2])):
            raise ValueError("browser position")
        browser = [b[0], b[1], b[2]]
    c, j, u = obj["c"], obj["j"], obj["u"]
    if not isinstance(c, list) or len(c) > CURSOR_KEYS_MAX + 1 or not isinstance(u, list) or len(u) > CURSOR_UPDATES_MAX:
        raise ValueError("size")
    known = set(sp_channels.list_keys() if keys is None else keys)
    known.add(GENERAL)
    kept, remap, previous = [], {}, None
    for index, item in enumerate(c):
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError("channel entry")
        key, seq, fp = item
        if not isinstance(key, str) or not sp_constants.CHANNEL_NAME_RE.fullmatch(key):
            raise ValueError("channel key")
        if not _is_seq(seq) or not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
            raise ValueError("channel position")
        rank = (key != GENERAL, key)
        if previous is not None and rank <= previous:
            raise ValueError("channel order")
        previous = rank
        if key in known:
            remap[index] = len(kept)
            kept.append([key, seq, fp])
    if sum(1 for item in c if item[0] != GENERAL) > CURSOR_KEYS_MAX:
        raise ValueError("too many channels")
    if not isinstance(j, list) or len(j) != 3 or not _is_seq(j[0]) or not isinstance(j[1], str) or not _FP_RE.fullmatch(j[1]):
        raise ValueError("journal position")
    if j[2] is not None and not (isinstance(j[2], str) and _ANCHOR_RE.fullmatch(j[2])):
        raise ValueError("journal anchor")
    pending = []
    for item in u:
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError("update entry")
        ci, seq, mid = item
        if isinstance(ci, bool) or not isinstance(ci, int) or not 0 <= ci < len(c):
            raise ValueError("update channel")
        if not _is_seq(seq) or not isinstance(mid, str) or not _MID_HEX_RE.fullmatch(mid):
            raise ValueError("update send")
        if ci in remap:
            pending.append([remap[ci], seq, mid])
    out = {"v": 1, "c": kept, "j": [j[0], j[1], j[2]], "u": pending}
    if browser is not None:
        out["b"] = browser
    return out


def decode_cursor(text, keys=None, allow_browser=False):
    """The validated cursor dict of ``text``; WebDataError 400 for anything malformed.

    Keys that are no longer stored channels are dropped (and the update entries of
    their sends with them); ``keys`` overrides ``channels.list_keys()`` for tests."""
    if not isinstance(text, str) or not text or len(text) > CURSOR_MAX:
        raise WebDataError("malformed cursor", 400)
    try:
        return _validate_cursor(json.loads(_unb64(text)), keys, allow_browser)
    except (ValueError, RecursionError, TypeError, OverflowError, binascii.Error, UnicodeError, KeyError):
        raise WebDataError("malformed cursor", 400)


def _mid_hex(mid):
    if isinstance(mid, str) and sp_constants.UUID_RE.fullmatch(mid):
        return mid.replace("-", "").lower()
    return None


def _mid_text(hexed):
    return "%s-%s-%s-%s-%s" % (hexed[:8], hexed[8:12], hexed[12:16], hexed[16:20], hexed[20:])


# --------------------------------------------------------------------------
# Channel positions
# --------------------------------------------------------------------------


def _stem(key):
    return sp_topics.topic_stem(sp_channels.topic_name(key), sp_channels.history_dir())


def _stat_text(key):
    """``"<size>:<ino>:<mtime_ns>"`` of the key's history log by a no-follow stat, ``absent`` or ``error:<errno>``."""
    try:
        st = os.stat(_stem(key) + ".jsonl", follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return "error:%s" % exc.errno
    return "%d:%d:%d" % (st.st_size, st.st_ino, st.st_mtime_ns)


def _snapshot_text(snapshot):
    if snapshot.fingerprint is None:
        return "absent"
    return "%d:%d:%d" % snapshot.fingerprint


def _clamp(seq):
    return max(0, min(seq, SEQ_LIMIT - 1))


def _meta_last(meta):
    nxt = meta.get("next_seq") if meta is not None else None
    if isinstance(nxt, int) and not isinstance(nxt, bool):
        return max(0, nxt - 1)
    return 0


def _stat_position(key, budget):
    """``(seq, fp digest)`` from the meta file and a stat only; meta first, so a racing append reads as newer."""
    seq = _meta_last(_read_meta(_stem(key) + ".meta.json", budget))
    return _clamp(seq), _digest(_stat_text(key))


def _stored_keys():
    return sorted(k for k in sp_channels.list_keys() if k != GENERAL)


def _tracked_keys():
    """``(keys, untracked)``: #general then at most CURSOR_KEYS_MAX stored keys, and the count left out."""
    stored = _stored_keys()
    return [GENERAL] + stored[:CURSOR_KEYS_MAX], max(0, len(stored) - CURSOR_KEYS_MAX)


# --------------------------------------------------------------------------
# Changes
# --------------------------------------------------------------------------


def _journal_digest(fingerprint):
    return _digest(fingerprint if isinstance(fingerprint, str) else "absent")


def _journal_position(res):
    anchor = res.get("next_anchor")
    if not (isinstance(anchor, str) and _ANCHOR_RE.fullmatch(anchor)):
        anchor = None
    return [_clamp(res.get("next") or 0), _journal_digest(res.get("fingerprint")), anchor]


def _journal_tail(j, budget, updates):
    """``(new j, gap, partial)``; appends ack and transport-error updates.

    Skipped (0 bytes) when the journal fingerprint digest equals the cursor's. The read is
    strict, so it never exceeds its allowance. The position advances when the read is
    complete, or when it had its full allowance and still could not reach the anchor (else a
    journal that grew past the allowance would be re-read forever); then the response says
    so through ``gap`` or an ``unverified`` update. A read short of the full allowance
    only because the request budget ran low keeps the old position."""
    if _journal_digest(sp_journal.journal_fingerprint()) == j[1]:
        return j, None, False
    allowance = min(budget.remaining(), JOURNAL_TAIL_BUDGET)
    if allowance <= 0:
        return j, None, True
    try:
        res = sp_journal.read_since(j[0], allowance, cursor_anchor=j[2], strict=True)
    except Exception:  # an unreadable journal only leaves the ACK state as it was
        return j, None, True
    budget.charge(res.get("bytes") or 0)
    unverified = set()
    partial = False
    for event in res.get("events") or ():
        mid, kind = event.get("message_id"), event.get("type")
        if kind not in ("ack", "transport_error"):
            continue
        if event.get("unverified"):
            partial = True
            if mid not in unverified:
                unverified.add(mid)
                updates.append({"message_id": mid, "ack": "unverified"})
        elif kind == "ack":
            updates.append({"message_id": mid, "recipient": event.get("for"), "ack": "acked", "state": "acked"})
        else:
            updates.append({"message_id": mid, "recipient": event.get("for"), "transport_error": str(event.get("detail"))})
    gap = res.get("gap")
    complete = bool(res.get("complete"))
    if complete or allowance >= JOURNAL_TAIL_BUDGET:
        partial = partial or not complete
        return _journal_position(res), gap, partial
    return j, gap, True


def _delivery_rows_of(outcomes):
    return [{"recipient": slot, "state": text} for slot, text in outcomes.items()]


def _change_update(key, mid, state, outcomes=None):
    return {"channel": key, "message_id": mid, "delivery": {"state": state, "recipients": _delivery_rows_of(outcomes or {})}}


def _browser_section(browser, position, first_page, restarted, budget):
    """``(section, new b position or None, partial)``: what the Browser feed holds past ``position``.

    A row's slots carry ``unverified: true`` themselves when hydration or the tail could not read
    their ACK state; nothing is overlaid here.

    ``position`` is the cursor's ``[uuid, log seq, status-row seq]``. The page is cut to the request
    budget (entries oldest first on a poll, newest first on a first page) and the position stays
    where the cut fell, so the rest follows on the next poll. A first page that sent nothing for
    want of budget returns no position, so the next poll is a first page again."""
    after_log, after_rows = (0, 0) if first_page else (position[1], position[2])
    page = browser.page(after_log, after_rows, first_page, budget.remaining())
    budget.charge(page["bytes"])
    section = {
        "entries": page["entries"], "sends": page["sends"], "gap": bool(page["gap"]),
        "restarted": restarted, "since_start": first_page,
    }
    new_position = [browser.uuid, page["log_seq"], page["rows_seq"]]
    if first_page and page["cut"] and not page["entries"] and not page["sends"]:
        new_position = None
    return section, new_position, bool(page["cut"])


def _after_first_page_hydration():
    """No-op seam between the first page's hydration read and its cursor (tests patch it)."""


def _hydrate_browser(browser, ctx, mids):
    """Merge one journal context read into the Browser feed's rows for ``mids`` (idempotent).

    ``ack`` only ever takes definite facts (``acked``, ``pending``, ``expired``; one the whole journal
    read does not hold is ``expired``, pruned from the journal), and a transport error is recorded.
    A message the read did not reach, or a journal it could not read, sets the ``unverified`` flag on
    the row's still ``pending`` slots (see ``BrowserState.apply_journal``); the flag lives in the
    shared rows, so it follows a row that is deferred, re-sent or read by another tab, and the next
    definite fact for the slot (including a later read that reaches the message) clears it."""
    for mid in mids:
        acks, errors, _partial = ctx.acks_for(mid)
        if acks is None:
            reached = ctx.available and (ctx.contexts.get(mid) or {}).get("state") != "unreached"
            browser.apply_journal(mid, None, ack="expired" if reached else "unverified")
            continue
        for slot, state in acks.items():
            browser.apply_journal(mid, slot, ack=state)
        for slot, detail in errors.items():
            browser.apply_journal(mid, slot, transport_error=str(detail))


def _apply_browser_updates(browser, updates):
    """Fold the journal tail's ack and transport-error updates into the Browser rows they name."""
    for update in updates:
        mid = update.get("message_id")
        if "transport_error" in update:
            browser.apply_journal(mid, update.get("recipient"), transport_error=update["transport_error"])
        elif update.get("ack") in ("acked", "unverified"):
            browser.apply_journal(mid, update.get("recipient"), ack=update["ack"])


def changes(cursor_text, budget=None, now=None, browser=None):
    """What changed since ``cursor_text``, bounded by one request budget.

    ``browser`` (chat mode) is the server's ``BrowserState``; then the cursor may carry ``"b"`` and the
    result gains ``browser: {"entries", "sends", "gap", "restarted", "since_start"}`` (see
    ``_browser_section``). A cursor without ``"b"`` (a reload) or with another server's uuid gets the
    first page: the retained entries, the journal state of the restored outbound sends read once, and a
    journal cursor that cannot skip an event written after that read.

    ``{"channels": {key: {"entries", "gap", "new"?}}, "updates", "cursor", "partial", "untracked"?,
    "journal_gap"?}``. A channel is read only when its meta ``next_seq`` is past the cursor's seq or
    its stat fingerprint differs; otherwise a meta read and a stat are all it costs. A channel not
    in the cursor is reported ``{"new": true, "entries": [], "gap": null}`` and entered at its current
    position: the page loads its latest page. A channel whose retained entries start after the
    cursor (pruned), whose seq went backwards, or that has more than PAGE_MAX new entries, or whose
    read hit the 1 MiB topic cap, returns no entries and a ``gap`` (``pruned``, ``regression``,
    ``limit``, ``budget``): the page reloads that channel's latest page. A channel the request
    budget could not read keeps its old position and the response is ``partial``.

    ``updates`` are, in order: ``{"message_id", "recipient", "ack": "acked", "state": "acked"}`` and
    ``{"message_id", "recipient", "transport_error"}`` from the journal tail, ``{"message_id", "ack":
    "unverified"}`` for an event whose message the bounded read could not reach, and ``{"channel",
    "message_id", "delivery": {"state", "recipients": [{"recipient", "state"}]}}`` for a send that
    was not among the new entries (its outcome arrived, or its in-progress state resolved to
    ``unknown`` or ``unverified``). The rows carry the outcome only; the page merges them by
    recipient into the delivery it has, and ACK state arrives through the journal updates. Sends in
    progress are remembered in the cursor and re-probed on every call."""
    budget = budget if budget is not None else Budget()
    cursor = decode_cursor(cursor_text, allow_browser=browser is not None)
    tracked, untracked = _tracked_keys()
    old = {key: (seq, fp) for key, seq, fp in cursor["c"]}
    old_u = [(cursor["c"][ci][0], seq, _mid_text(mid)) for ci, seq, mid in cursor["u"]]
    partial = untracked > 0
    updates = []
    first_page = restarted = False
    hydrated_at = None
    reserved = 0
    if browser is not None:
        position = cursor.get("b")
        restarted = position is not None and position[0] != browser.uuid
        first_page = position is None or restarted
        if first_page:
            # Hydrate before the tail read: `pre` is the position before it, `pos` the position of
            # the hydration read itself. An event written after either is re-read by a later poll.
            # The page's own bytes (plus a margin for rows that hydration changes) are set aside until
            # the section is built, but at most half the request budget: a journal that never reaches
            # every wanted id cannot starve the feed on every poll. A large feed cannot starve
            # hydration (it runs right after the reserve and the baseline), but hydration can starve
            # the tail and the channels on a first page: the response is then partial and they
            # recover on the next poll. A page that does not fit its reserve serves its newest
            # entries (see BrowserState.page).
            need = browser.first_page_bytes()
            reserved = min(budget.remaining() // 2, need + need // HYDRATION_MARGIN)
            budget.total -= reserved
            mids = browser.restorable_mids()
            if mids:
                pre, _skipped = _journal_baseline(budget)
                ctx, pos = read_journal_context(budget, mids, with_position=True)
                partial = partial or ctx.prefix_unread or not ctx.available
                _hydrate_browser(browser, ctx, mids)
                _after_first_page_hydration()
                hydrated_at = pos or pre
    new_j, journal_gap, journal_partial = _journal_tail(cursor["j"], budget, updates)
    partial = partial or journal_partial
    if hydrated_at is not None and not journal_partial:
        new_j = hydrated_at
    positions, skipped, out, snaps = {}, set(), {}, {}
    for key in tracked:
        if key not in old:
            if budget.remaining() <= 0:
                partial = True
                continue
            positions[key] = _stat_position(key, budget)
            out[key] = {"entries": [], "gap": None, "new": True}
            continue
        cseq, cfp = old[key]
        meta = _read_meta(_stem(key) + ".meta.json", budget) if budget.remaining() > 0 else None
        if _meta_last(meta) <= cseq and _digest(_stat_text(key)) == cfp:
            positions[key] = old[key]
            continue
        if budget.remaining() <= 0:
            positions[key], partial = old[key], True
            skipped.add(key)
            continue
        request_limited = min(TOPIC_BUDGET, budget.remaining()) < TOPIC_BUDGET
        snap = HistorySnapshot(key, budget, now=now).read(since=cseq)
        gap = snap.gap
        if snap.unavailable or (request_limited and gap is not None and gap.get("reason") == "budget"):
            positions[key], partial = old[key], True
            skipped.add(key)
            continue
        last = snap.next_seq - 1
        entries = snap.page
        if gap is None and last < cseq:
            gap = {"from": cseq, "to": last, "reason": "regression"}
        elif gap is None and len(entries) > PAGE_MAX:
            gap = {"from": cseq + 1, "to": last, "reason": "limit"}
        if gap is not None:
            entries = []
        positions[key] = (_clamp(last), _digest(_snapshot_text(snap)))
        snaps[key] = snap
        if entries or gap is not None:
            out[key] = {"entries": entries, "gap": gap}
    shown = [key for key in tracked if key in snaps and key in out]
    groups = [(snaps[key], out[key]["entries"], key == GENERAL) for key in shown]
    built, _ctx = _build_views(groups, budget)
    sent, outcome_mids, new_u = {}, {}, []
    for (_snap, entries, _general), (views, view_partial), key in zip(groups, built, shown):
        partial = partial or view_partial
        for entry, view in zip(entries, views):
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            if entry.get("kind") == sp_channels.EVENT_SEND:
                mid = _ident(data.get("message_id"))
                sent.setdefault(key, set()).add(mid)
                if _mid_hex(mid) and view.get("delivery", {}).get("state") == sp_channels.STATE_IN_PROGRESS:
                    new_u.append((key, entry["seq"], mid))
            elif entry.get("kind") == sp_channels.EVENT_OUTCOME:
                outcome_mids.setdefault(key, {}).setdefault(_ident(data.get("message_id")), entry)
        out[key]["entries"] = views
    done = set()
    for key, by_mid in outcome_mids.items():
        for mid, entry in by_mid.items():
            if mid not in sent.get(key, ()):
                done.add((key, mid))
                updates.append(_change_update(key, mid, "complete", _outcomes_of(entry)))
    survivors = []
    for key, send_seq, mid in old_u:
        if key not in positions and key not in skipped:
            continue
        if key in skipped:
            survivors.append((key, send_seq, mid))
            continue
        if (key, mid) in done:
            continue
        probe = _probe(mid)
        if probe == "held":
            survivors.append((key, send_seq, mid))
            continue
        if budget.remaining() <= 0:
            partial = True
            survivors.append((key, send_seq, mid))
            continue
        cseq, cfp = old[key]
        snap = snaps.get(key)
        stat_now = _stat_text(key)
        if (snap is None and positions.get(key) == old[key] and _digest(stat_now) == cfp
                and not stat_now.startswith("error:")):
            # Nothing was written after the cursor, and the lock was free before this look: no
            # outcome exists, so the lock alone decides, without reading the topic.
            state, outcomes, uncovered = sp_channels.send_state_from(False, probe, False), {}, False
        else:
            if snap is None:
                snap = snaps[key] = HistorySnapshot(key, budget, now=now).read(since=cseq)
            state, outcomes, uncovered = send_state(snap, {"seq": cseq, "data": {"message_id": mid}})
        if state == sp_channels.STATE_IN_PROGRESS:
            survivors.append((key, send_seq, mid))
            continue
        if state == sp_channels.STATE_UNVERIFIED:
            survivors.append((key, send_seq, mid))
            partial = partial or uncovered or (snap is not None and snap.unavailable is not None)
        updates.append(_change_update(key, mid, state, outcomes))
    order = [key for key in tracked if key in positions]
    index = {key: i for i, key in enumerate(order)}
    pending, seen = [], set()
    for key, send_seq, mid in new_u + survivors:
        hexed = _mid_hex(mid)
        if key in index and hexed and hexed not in seen:
            seen.add(hexed)
            pending.append([index[key], _clamp(send_seq), hexed])
    if len(pending) > CURSOR_UPDATES_MAX:
        pending, partial = pending[:CURSOR_UPDATES_MAX], True
    cursor_obj = {"v": 1, "c": [[key, positions[key][0], positions[key][1]] for key in order], "j": new_j, "u": pending}
    result = {"channels": out, "updates": updates, "partial": partial}
    if browser is not None:
        _apply_browser_updates(browser, updates)
        budget.total += reserved
        section, b_position, b_partial = _browser_section(browser, cursor.get("b"), first_page, restarted, budget)
        result["browser"] = section
        result["partial"] = partial or b_partial
        if b_position is not None:
            cursor_obj["b"] = b_position
        elif not first_page:
            cursor_obj["b"] = cursor["b"]
    result["cursor"] = encode_cursor(cursor_obj)
    if untracked:
        result["untracked"] = untracked
    if journal_gap is not None:
        result["journal_gap"] = journal_gap
    return _clean(result)


# --------------------------------------------------------------------------
# All channels and search
# --------------------------------------------------------------------------


def _all_keys():
    return [GENERAL] + _stored_keys()


def _activity_order(keys, budget):
    rows = []
    for key in keys:
        meta = _read_meta(_stem(key) + ".meta.json", budget) if budget.remaining() > 0 else None
        stamp = sp_runtime.parse_time(meta.get("last_ts")) if meta is not None and isinstance(meta.get("last_ts"), str) else None
        rows.append((-(stamp or 0.0), -_meta_last(meta), key))
    rows.sort()
    return [row[2] for row in rows]


def _plan(next_token, budget):
    """``(remaining keys in order, total, base)``: a fresh activity order, or the rest of a ``next`` token's.

    ``base`` is the sorted channel list this request works from; ``_next_token`` names it, so a
    channel created mid-request is never hidden by the token the request mints."""
    base = _all_keys()
    fingerprint = _digest("\n".join(base))
    if next_token is None:
        return _activity_order(base, budget), len(base), base
    try:
        if len(next_token) > TOKEN_MAX:
            raise ValueError("token size")
        token = json.loads(_unb64(next_token))
        if not isinstance(token, dict) or set(token) != {"v", "h", "r"} or type(token["v"]) is not int or token["v"] != 1:
            raise ValueError("token shape")
        picks = token["r"]
        if (not isinstance(picks, list) or len(picks) > len(base) or len(set(picks)) != len(picks)
                or not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(base) for i in picks)):
            raise ValueError("token order")
        if token["h"] != fingerprint:
            raise WebDataError("the channel list changed; start again", 409)
    except WebDataError:
        raise
    except (ValueError, RecursionError, TypeError, OverflowError, binascii.Error, UnicodeError, KeyError):
        raise WebDataError("malformed token", 400)
    return [base[i] for i in picks], len(base), base


def _next_token(rest, base):
    if not rest:
        return None
    index = {key: i for i, key in enumerate(base)}
    return _b64(json.dumps(
        {"v": 1, "h": _digest("\n".join(base)), "r": [index[k] for k in rest if k in index]}, separators=(",", ":")))


def _read_batch(remaining, budget, now, limit):
    """Snapshots of the next channels (at most BATCH_CHANNELS) until the budget runs out; ``[(key, snapshot)]``."""
    batch = []
    for key in remaining[:BATCH_CHANNELS]:
        if budget.remaining() <= 0:
            break
        snap = HistorySnapshot(key, budget, now=now).read(limit=limit)
        if snap.partial and not snap.page and budget.remaining() <= 0:
            break  # the budget ended the read before anything was in: not covered
        batch.append((key, snap))
    return batch


def _sort_stamp(view):
    return (_stamp(view.get("ts")), view.get("channel", ""), view.get("seq") or 0)


def all_channels(next_token=None, budget=None, now=None):
    """The next batch of channels' newest entries merged by time, each tagged with its ``channel``.

    Channels are ordered by recent activity (meta ``last_ts``, then last seq); a batch is at most
    BATCH_CHANNELS channels with at most BATCH_ENTRIES entries each, merged to the newest PAGE_MAX
    (``trimmed`` counts the older ones left out), oldest first. ``{"entries", "partial", "omitted",
    "total", "shown", "next", "trimmed", "channels"}``: ``channels`` lists every channel key this batch
    read, in order, including ones with no entries or whose entries were trimmed; ``shown`` channels so far plus ``omitted`` still to come
    equal ``total``; ``partial`` is true when any are omitted or any read was cut short; ``next``
    continues with the omitted channels (WebDataError 409 when the channel list changed
    meanwhile). Never presents a subset as all channels."""
    budget = budget if budget is not None else Budget()
    remaining, total, base = _plan(next_token, budget)
    batch = _read_batch(remaining, budget, now, BATCH_ENTRIES)
    built, _ctx = _build_views([(snap, snap.page, key == GENERAL) for key, snap in batch], budget)
    partial, merged = False, []
    for (key, snap), (views, view_partial) in zip(batch, built):
        partial = partial or snap.partial or view_partial
        for view in views:
            view["channel"] = key
            merged.append(view)
    merged.sort(key=_sort_stamp)
    trimmed = max(0, len(merged) - PAGE_MAX)
    rest = remaining[len(batch):]
    return _clean({
        "entries": merged[trimmed:], "partial": partial or bool(rest), "omitted": len(rest),
        "total": total, "shown": total - len(rest), "next": _next_token(rest, base), "trimmed": trimmed,
        "channels": [key for key, _snap in batch],
    })


def _haystack(view):
    parts = [view.get(name) for name in ("body", "actor", "text", "member", "sender")]
    return "\n".join(p for p in parts if isinstance(p, str)).casefold()


def search(q, scope="all", next_token=None, budget=None, now=None):
    """Case-insensitive substring search of body, actor and member text in each channel's newest PAGE_MAX entries.

    ``q`` is 1..QUERY_MAX characters (else WebDataError 400). ``scope`` is ``all`` (batched exactly as
    ``all_channels``, with a ``next`` token) or one channel key. ``{"matches", "partial",
    "searched_channels", "total_channels", "next", "window"}``, matches newest first and tagged with
    their ``channel``, at most SEARCH_MATCHES_MAX; ``partial`` is true when channels are left for
    ``next``, a channel has older entries than the window or its read was cut short, or matches were
    cut. A match carries the entry's facts without its delivery rows."""
    if not isinstance(q, str) or not 1 <= len(q) <= QUERY_MAX:
        raise WebDataError("q must be 1 to %d characters" % QUERY_MAX)
    budget = budget if budget is not None else Budget()
    if scope == "all":
        remaining, total, base = _plan(next_token, budget)
    else:
        _check_key(scope)
        if next_token is not None:
            raise WebDataError("a single channel search has no next token")
        remaining, total, base = [scope], 1, None
    needle = q.casefold()
    batch = _read_batch(remaining, budget, now, PAGE_MAX)
    partial, matches = False, []
    for key, snap in batch:
        partial = partial or snap.partial or snap.next_before is not None
        for entry in snap.page:
            if entry.get("oversized"):
                continue
            view = entry_view(entry, None, key == GENERAL)
            if needle in _haystack(view):
                view["channel"] = key
                matches.append(view)
    matches.sort(key=_sort_stamp, reverse=True)
    cut = len(matches) > SEARCH_MATCHES_MAX
    rest = remaining[len(batch):]
    return _clean({
        "matches": matches[:SEARCH_MATCHES_MAX], "partial": partial or cut or bool(rest),
        "searched_channels": total - len(rest), "total_channels": total,
        "next": _next_token(rest, base) if scope == "all" else None, "window": PAGE_MAX,
    })


# --------------------------------------------------------------------------
# State view
# --------------------------------------------------------------------------


def _members_of(members, liveness, activity, names):
    rows = []
    for typed in members:
        if not isinstance(typed, str):
            continue
        kind = typed.partition(":")[0]
        row = {"typed": typed, "runtime": _RUNTIMES.get(kind, "unknown"),
               "state": _member_state(liveness.get(typed), activity.get(typed))}
        if isinstance(names.get(typed), str):
            row["name"] = names[typed]
        rows.append(row)
    return rows


def _alert(snap, rows_of):
    """``missing_acks``, ``unverified`` or None from one channel's latest page.

    ``rows_of`` maps a send's seq to its delivery view. A definite missing ACK of an ``@all``
    send wins; an ACK that could not be checked, or a page the budget cut short, is
    ``unverified``. Covers the latest ALERT_WINDOW entries only."""
    unverified = snap.partial
    for entry in snap.page:
        if entry.get("kind") != sp_channels.EVENT_SEND:
            continue
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        body = data.get("body")
        if not (data.get("journaled") and isinstance(body, str) and sp_channels.ALL_RE.match(body)
                and not data.get("targeted")):
            continue
        delivery = rows_of[entry["seq"]]
        tally = ack_tally(delivery["recipients"])
        if len(tally["missing"]) - tally["unverified"] > 0:
            return "missing_acks"
        unverified = unverified or tally["unverified"] > 0 or bool(delivery.get("partial"))
    return "unverified" if unverified else None


def _handover_chain(snap):
    chain = []
    for entry in snap.page:
        if entry.get("kind") == sp_channels.EVENT_HANDOVER and isinstance(entry.get("data"), dict):
            data = entry["data"]
            chain.append({"member": _ident(data.get("member")), "to": _ident(data.get("to")), "seq": entry.get("seq")})
    return chain


def _journal_baseline(budget):
    """``(position, partial)``: the journal position a fresh cursor starts at (strict read, charged to ``budget``).

    A journal that cannot be read, or no budget for it, starts the cursor where the first poll reads
    it whole, with no anchor. The anchor is dropped as well when the read met an oversized line or
    could not reach the file start's neighbour (``oversized``, ``window_regression``)."""
    jpos = [0, _digest("unread"), None]
    allowance = min(budget.remaining(), BASELINE_BUDGET)
    if allowance <= 0:
        return jpos, True
    try:
        res = sp_journal.read_since(None, allowance, strict=True)
        budget.charge(res.get("bytes") or 0)
        jpos = _journal_position(res)
        if res.get("oversized") or res.get("window_regression"):
            jpos[2] = None
    except Exception:  # an unreadable journal starts the cursor where the first poll reads it whole
        return [0, _digest("unread"), None], True
    return jpos, False


def state_view(discovery, budget=None, now=None, chat=None):
    """Metadata for the page's first load: sessions, channels (#general first), the starting cursor.

    ``discovery`` is supplied by the caller: ``general`` = ``(members, opencode_coverage, gaps)`` from
    ``channels.general_members()``, ``liveness`` {typed: live|gone|unverified}, ``activity`` {typed:
    working|idle|unknown}, ``stale`` and ``pending`` flags, and optionally ``names`` {typed: name} and
    ``homes`` {Codex thread id: CODEX_HOME} (a Codex member without an entry has an unknown reply budget).
    No history entries are returned. Each channel's ``alert`` comes from its latest ALERT_WINDOW
    entries (``missing_acks`` when an ``@all`` send there lacks ACKs, ``unverified`` when that could
    not be established, e.g. the budget ran out); older entries are not examined. ``handover_chain``
    of a buddy channel is the ``member -> to`` sequence of the handover entries in that same page.
    The cursor starts every tracked channel at its current seq and fingerprint, the journal at its
    newest record and anchor, and remembers the sends in progress in those pages.

    ``chat`` (chat mode) is ``{"peer": "web:<uuid>", "name", "user"}``: it is returned as ``chat``, the
    browser peer is listed among the ``sessions`` (runtime ``Browser``), and a Codex session gains
    ``replies_left`` from ``discovery["replies_left"]`` (the browser peer's remaining replies, worked
    out by the discovery pass; absent or None is unknown)."""
    budget = budget if budget is not None else Budget()
    members, coverage, gaps = (tuple(discovery.get("general") or ((), None, ())) + ((), None, ()))[:3]
    liveness = discovery.get("liveness") or {}
    activity = discovery.get("activity") or {}
    names = discovery.get("names") or {}
    homes = discovery.get("homes") or {}
    tracked, untracked = _tracked_keys()
    # The journal baseline comes before any ACK is read for the page: an ACK written after this
    # point is past the cursor's position and arrives with the first ``changes`` poll.
    jpos, jpartial = _journal_baseline(budget)
    records = {key: _read_record(key, budget) for key in tracked if key != GENERAL}
    positions = [(key,) + _stat_position(key, budget) for key in tracked]
    reads = {}
    for key in tracked:
        if budget.remaining() > 0:
            reads[key] = HistorySnapshot(key, budget, now=now).read(limit=ALERT_WINDOW)
    states, want = {}, []
    for key, snap in reads.items():
        found = {}
        for entry in snap.page:
            if entry.get("kind") != sp_channels.EVENT_SEND:
                continue
            found[entry["seq"]] = send_state(snap, entry)
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            if (data.get("journaled") and isinstance(data.get("body"), str)
                    and sp_channels.ALL_RE.match(data["body"]) and not data.get("targeted")):
                want.append(data.get("message_id"))
        states[key] = found
    journal_ctx = read_journal_context(budget, want)
    partial = untracked > 0 or len(reads) < len(tracked) or jpartial
    rows, pending = [], []
    index = {key: i for i, key in enumerate(tracked)}
    for key in tracked:
        if key == GENERAL:
            record = {"display": "#general", "kind": "general", "owner": None}
            roster = [m for m in members if isinstance(m, str)]
        else:
            record = records[key]
            roster = list(record["members"]) if record is not None else []
        omitted = max(0, len(roster) - sp_constants.CHANNEL_MAX_MEMBERS)
        roster = roster[:sp_constants.CHANNEL_MAX_MEMBERS]
        snap = reads.get(key)
        alert, chain = "unverified", []
        if snap is not None:
            deliveries = {}
            for entry in snap.page:
                if entry.get("kind") == sp_channels.EVENT_SEND:
                    state, outcomes, send_partial = states[key][entry["seq"]]
                    deliveries[entry["seq"]] = _delivery(entry, state, outcomes, send_partial, journal_ctx)
                    mid = (entry.get("data") or {}).get("message_id") if isinstance(entry.get("data"), dict) else None
                    if state == sp_channels.STATE_IN_PROGRESS and _mid_hex(mid):
                        pending.append([index[key], _clamp(entry["seq"]), _mid_hex(mid)])
            alert = _alert(snap, deliveries)
            chain = _handover_chain(snap) if record is not None and record["kind"] == "buddy" else []
        else:
            partial = True
        if record is None:  # listed but its record does not read: shown as such, never dropped
            rows.append({"key": key, "unreadable": True, "alert": alert})
            partial = True
            continue
        cover = channel_coverage(roster)
        if key == GENERAL:
            cover.update(opencode=coverage, discovery_incomplete=[g if isinstance(g, str) else _text_of(g) for g in gaps or ()])
        row = {
            "key": key, "name": key, "display": record["display"], "kind": record["kind"], "owner": record["owner"],
            "members": _members_of(roster, liveness, activity, names), "handover_chain": chain,
            "alert": alert, "coverage": cover,
        }
        if omitted:
            row["omitted_members"] = omitted
            partial = True
        rows.append(row)
    seen_members, sessions = set(), []
    for row in rows:
        for member in row.get("members", ()):
            if member["typed"] not in seen_members:
                if len(sessions) >= SESSIONS_MAX:
                    partial = True
                    continue
                seen_members.add(member["typed"])
                details = member_details(
                    member["typed"], liveness.get(member["typed"]), activity.get(member["typed"]), now, budget,
                    home=homes.get(member["typed"].partition(":")[2]))
                partial = partial or bool(details.get("partial"))
                if chat is not None and member["typed"].startswith("codex:"):
                    left = (discovery.get("replies_left") or {}).get(member["typed"])
                    details["replies_left"] = left if isinstance(left, int) and not isinstance(left, bool) else None
                sessions.append(details)
    pending = pending[::-1]
    if len(pending) > CURSOR_UPDATES_MAX:
        pending, partial = pending[:CURSOR_UPDATES_MAX], True
    cursor = encode_cursor({
        "v": 1, "c": [[key, seq, fp] for key, seq, fp in positions], "j": jpos, "u": pending})
    if chat is not None:
        browser_session = member_details(chat["peer"], "live", "idle")
        browser_session["name"] = chat.get("name")
        sessions.insert(0, browser_session)
    result = {
        "sessions": sessions, "channels": rows, "cursor": cursor, "partial": partial,
        "discovery": {"stale": bool(discovery.get("stale")), "pending": bool(discovery.get("pending"))},
    }
    if chat is not None:
        result["chat"] = {"peer": chat["peer"], "name": chat.get("name"), "user": chat.get("user")}
    if untracked:
        result["untracked"] = untracked
    return _clean(result)
