"""Chat mode of the web dashboard (PRD #162): the Browser feed state, the send gate and the single send worker.

``web.py`` owns the HTTP side and hands a validated ``POST /api/send`` to ``Chat.accept``. The only
code here that runs on a request thread is that call and the ``BrowserState`` methods ``webdata`` calls
(``page`` and the readers, plus ``apply_journal``, which ``webdata``'s ``_hydrate_browser`` and
``_apply_browser_updates`` use to write ACK state into rows); every one of them takes the state lock:

* ``SendGate`` is the one acceptance lock. Under it a send is checked against the rate limits and the
  worker's queue capacity, charged, given a slot, and handed to ``BrowserState`` (a ``queued`` status row,
  plus a log entry for a direct send) and to the worker's queue. A refusal charges and queues nothing.
* ``SendWorker`` is one daemon thread that takes jobs in order. It resolves recipients, writes the
  journal, delivers and records the outcomes in the row. It holds the discovery lock around resolution,
  the writes and each member delivery, and never across a pacing sleep. Nothing is retried.
* ``BrowserState`` is the in-memory feed, guarded by one lock: the bounded log of inbound messages and
  direct sends, the status rows of every send. A channel post has a row but never a log entry, so the feed
  never shows it as a message.

Everything a peer supplies (a body, a sender name, a status detail) is stored as received. The JSON
encoder and ``webdata``'s escaping make it safe on the wire and the page renders it as text; what is
logged to the terminal is formatted with ``%r``.
"""

from __future__ import annotations
import collections
import contextlib
import copy
import json
import os
import hashlib
import queue
import secrets
import sys
import threading
import time
import uuid
from . import channels as sp_channels, constants as sp_constants, journal as sp_journal
from . import runtime as sp_runtime, webpeer as sp_webpeer

SEND_QUEUE_MAX = 8  # queued plus running jobs
RATE_MIN_GAP = 1.0  # seconds between two accepted sends
RATE_PER_MINUTE = 30
RATE_WINDOW = 60.0
FEED_MAX_ENTRIES = 1000
FEED_MAX_BYTES = 4 * 1024 * 1024  # measured as the wire encodes it (see ``_size``)
ENTRY_TEXT_MAX = 256 * 1024  # chars of a log entry's text kept for display; the journal keeps the whole message
ROWS_MAX = 1000
ROW_TEXT_MAX = 2000  # chars of a channel post's text kept in its status row
DETAIL_MAX = 500
JOIN_TIMEOUT = 3.0
NONCE_TTL = 60  # seconds a preview's confirm nonce stays valid
NONCE_MAX = 32  # outstanding nonces; at the cap the oldest is evicted
CONFIRM_OK = "ok"
CONFIRM_STALE = "stale"  # missing, expired or not bound to this request
CONFIRM_LIMITED = "limited"  # a rate limit or the queue is full; the nonce stays usable

# The browser's own session identity: a copy inherited from the shell that started the server says
# nothing about the browser peer and must not become the sender of a delivery.
INHERITED_IDENTITY_ENV = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
    "OPENCODE_SESSION_ID",
)

STATE_QUEUED = "queued"
STATE_PENDING = "pending"
STATE_NOT_SENT = "not sent"
STATE_NOT_SENT_JOURNALED = "not sent (journaled)"
STATE_FAILED_AFTER_RECORDING = "failed after recording"

ACK_PENDING = "pending"
ACK_NOT_REQUESTED = "not requested"
ACK_ACKED = "acked"
ACK_UNVERIFIED = "unverified"
ACK_EXPIRED = "expired"
_ACK_SOFT = (ACK_PENDING, ACK_EXPIRED)


def scrub_environment():
    """Remove the inherited session identity variables from this process's environment."""
    for name in INHERITED_IDENTITY_ENV:
        os.environ.pop(name, None)


def _bounded(value, limit=DETAIL_MAX):
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _size(entry):
    """Bytes of ``entry`` as ``web`` serves it: ``ensure_ascii=True``, which escapes a non-ASCII character
    to up to 12 bytes and never raises on a lone surrogate."""
    return len(json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def _clip_text(text):
    """``(text, clipped)``: a log entry's text cut to ENTRY_TEXT_MAX chars so one entry cannot wedge the feed."""
    if isinstance(text, str) and len(text) > ENTRY_TEXT_MAX:
        return text[:ENTRY_TEXT_MAX], True
    return text, False


# --------------------------------------------------------------------------
# Browser state
# --------------------------------------------------------------------------


class BrowserState:
    """The in-memory Browser feed of one server run (one uuid); safe to call from any thread.

    Log entries (immutable once appended, each with an ``lseq``):
      inbound  ``{lseq, dir: "in", message_id, orig_msg_id, sender, sender_name, text, at}``
      outbound ``{lseq, dir: "out", message_id, to: [typed], text, ack_requested, at}``
    Status rows, one per accepted send, by message id:
      ``{mid, kind: "direct"|"channel", to, text (channel only), ack_requested, state, reason, note,
      journaled, delivering, recipients: {typed: {outcome, ack, unverified?, transport_error?, update_failed?, peer_status?}}, at, seq}``
    ``seq`` rises on every change, so a reader asks for the rows changed since a seq.
    """

    def __init__(self, sid, name, user):
        self.uuid = sid
        self.typed = "web:%s" % sid
        self.name = name
        self.user = user
        self._lock = threading.Lock()
        self._log = collections.deque()  # (entry, size)
        self._log_bytes = 0
        self._lseq = 0
        self._sends = collections.OrderedDict()  # mid -> row, ordered by last change
        self._seq = 0

    # -- log ---------------------------------------------------------------

    def _commit_locked(self, entry, size):
        self._lseq += 1
        entry["lseq"] = self._lseq
        self._log.append((entry, size))
        self._log_bytes += size
        while len(self._log) > 1 and (len(self._log) > FEED_MAX_ENTRIES or self._log_bytes > FEED_MAX_BYTES):
            _old, old_size = self._log.popleft()
            self._log_bytes -= old_size

    def _append_locked(self, entry):
        self._commit_locked(entry, self._log_size_locked(entry))

    def _log_size_locked(self, entry):
        """The size ``entry`` will have once it carries the next lseq; nothing changes if this raises."""
        return _size(dict(entry, lseq=self._lseq + 1))

    def add_inbound(self, entry):
        """``BrowserPeer.on_inbound``: the message the inbox has already journaled.

        The text is kept for display up to ENTRY_TEXT_MAX chars (``text_clipped`` then); a lone
        surrogate in a peer string is kept and leaves as an escape."""
        text, clipped = _clip_text(entry.get("body"))
        shown = {
            "dir": "in",
            "message_id": entry.get("message_id"),
            "orig_msg_id": entry.get("orig_msg_id"),
            "sender": entry.get("sender"),
            "sender_name": entry.get("sender_name"),
            "text": text,
            "at": entry.get("at"),
        }
        if clipped:
            shown["text_clipped"] = True
        with self._lock:
            self._append_locked(shown)

    # -- rows --------------------------------------------------------------

    def _touch_locked(self, row):
        self._seq += 1
        row["seq"] = self._seq
        self._sends.move_to_end(row["mid"])

    @staticmethod
    def _make_row(mid, kind, to, ack, text=None):
        row = {
            "mid": mid, "kind": kind, "to": to, "ack_requested": bool(ack), "state": STATE_QUEUED,
            "reason": None, "note": None, "journaled": False, "delivering": False, "recipients": {},
            "at": time.time(), "seq": 0,
        }
        if text is not None:
            row["text"] = text[:ROW_TEXT_MAX]
            row["text_clipped"] = len(text) > ROW_TEXT_MAX
        return row

    def _insert_row_locked(self, row):
        self._sends[row["mid"]] = row
        self._touch_locked(row)
        self._evict_rows_locked()

    def _evict_rows_locked(self):
        if len(self._sends) <= ROWS_MAX:
            return
        for mid in list(self._sends):
            if len(self._sends) <= ROWS_MAX:
                break
            row = self._sends[mid]
            if row["state"] == STATE_QUEUED or row["delivering"]:
                continue  # a send the worker still owns
            del self._sends[mid]

    def accept_direct(self, mid, to, text, ack):
        """A direct send accepted: its ``queued`` row and its outbound log entry.

        Everything is built and sized before either is stored, so a failure leaves neither."""
        shown, clipped = _clip_text(text)
        with self._lock:
            row = self._make_row(mid, "direct", list(to), ack)
            row["recipients"] = {t: {"outcome": None, "ack": ACK_PENDING if ack else ACK_NOT_REQUESTED} for t in to}
            entry = {
                "dir": "out", "message_id": mid, "to": list(to), "text": shown, "ack_requested": bool(ack),
                "at": row["at"],
            }
            if clipped:
                entry["text_clipped"] = True
            size = self._log_size_locked(entry)
            self._insert_row_locked(row)
            self._commit_locked(entry, size)

    def accept_channel(self, mid, key, text, ack):
        """A channel post accepted: its ``queued`` row only. A channel post is never logged, which is
        what keeps it out of the feed's entries; the page shows it from its row."""
        with self._lock:
            row = self._make_row(mid, "channel", "#" + key, ack, text)
            row["channel"] = key
            self._insert_row_locked(row)

    def discard(self, mid):
        """Remove an accepted send that never reached the worker: its row and its outbound log entry."""
        with self._lock:
            self._sends.pop(mid, None)
            kept = collections.deque()
            for entry, size in self._log:
                if entry["dir"] == "out" and entry["message_id"] == mid:
                    self._log_bytes -= size
                else:
                    kept.append((entry, size))
            self._log = kept

    def row(self, mid):
        """A copy of one status row, or None."""
        with self._lock:
            row = self._sends.get(mid)
            return copy.deepcopy(row) if row is not None else None

    def rows(self):
        with self._lock:
            return [copy.deepcopy(r) for r in self._sends.values()]

    def log_entries(self):
        with self._lock:
            return [copy.deepcopy(e) for e, _size in self._log]

    def update(self, mid, state=None, reason=None, journaled=None, delivering=None, recipients=None, note=None,
               update_failed=None):
        """Change fields of a row; the row's seq rises only when something differs.

        ``update_failed`` True sets the row-level flag: the journal could not record a transport failure
        of this channel post (the fan-out reports it once, not per recipient)."""
        with self._lock:
            row = self._sends.get(mid)
            if row is None:
                return
            before = copy.deepcopy(row)
            if state is not None:
                row["state"] = state
                row["reason"] = None if reason is None else _bounded(reason, 600)
            if journaled is not None:
                row["journaled"] = bool(journaled)
            if delivering is not None:
                row["delivering"] = bool(delivering)
            if recipients is not None:
                row["recipients"] = recipients
            if note is not None:
                row["note"] = _bounded(note, 600)
            if update_failed:
                row["update_failed"] = True
            if row != before:
                self._touch_locked(row)

    def merge_recipients(self, mid, planned):
        """Fold the final per-recipient outcomes (``{typed: {"outcome", "ack"}}``) into a row.

        A slot the row already has keeps what arrived during the fan-out (``peer_status``,
        ``transport_error``, and an ACK state past ``pending``); a ``not requested`` ack is applied."""
        with self._lock:
            row = self._sends.get(mid)
            if row is None:
                return
            changed = False
            for typed, new in planned.items():
                slot = row["recipients"].get(typed)
                if slot is None:
                    row["recipients"][typed] = dict(new)
                    changed = True
                    continue
                if new["outcome"] is not None and slot.get("outcome") != new["outcome"]:
                    slot["outcome"] = new["outcome"]
                    changed = True
                ack = new["ack"]
                if ack == ACK_NOT_REQUESTED and slot["ack"] not in (ACK_NOT_REQUESTED, ACK_ACKED):
                    slot["ack"] = ack
                    slot.pop("unverified", None)
                    changed = True
            if changed:
                self._touch_locked(row)

    def mark_update_failed(self, mid, typed):
        """Note on one recipient that the journal could not record its transport failure."""
        with self._lock:
            row = self._sends.get(mid)
            slot = row["recipients"].get(typed) if row is not None else None
            if slot is not None and not slot.get("update_failed"):
                slot["update_failed"] = True
                self._touch_locked(row)

    def set_outcome(self, mid, typed, outcome):
        with self._lock:
            row = self._sends.get(mid)
            if row is None:
                return
            slot = row["recipients"].setdefault(
                typed, {"outcome": None, "ack": ACK_PENDING if row["ack_requested"] else ACK_NOT_REQUESTED})
            if slot.get("outcome") != outcome:
                slot["outcome"] = _bounded(outcome, 600)
                self._touch_locked(row)

    def apply_journal(self, mid, recipient, ack=None, transport_error=None):
        """Fold one journal fact into a row, idempotently per ``(mid, recipient)``.

        ``recipient`` None means every recipient of the row. ``acked`` always lands, even on a slot
        that was ``not requested``; ``pending`` and ``expired`` replace only a still-open state
        (``pending`` or ``expired``). ``ack="unverified"`` is not an ACK value: it sets the slot's
        ``unverified`` flag (the journal could not say) and only on a slot whose ``ack`` is ``pending``,
        with or without a transport error, never on ``acked``, ``not requested`` or ``expired``. The
        flag never replaces ``ack``; any definite fact for the slot clears it (``acked``, ``expired``,
        ``pending`` read from a journal that was reached, or a transport error), and so does
        ``merge_recipients`` setting ``not requested``. An unknown message
        or recipient is ignored."""
        with self._lock:
            row = self._sends.get(mid) if isinstance(mid, str) else None
            if row is None:
                return
            names = [recipient] if recipient is not None else list(row["recipients"])
            changed = False
            for name in names:
                slot = row["recipients"].get(name) if isinstance(name, str) else None
                if slot is None:
                    continue
                if ack == ACK_UNVERIFIED:
                    # Only a guess: it marks a still-pending slot and never covers a fact.
                    if slot["ack"] == ACK_PENDING and not slot.get("unverified"):
                        slot["unverified"] = True
                        changed = True
                elif ack is not None:
                    if ack != slot["ack"] and (ack == ACK_ACKED or (slot["ack"] in _ACK_SOFT and ack in _ACK_SOFT)):
                        slot["ack"] = ack
                        changed = True
                    if slot["ack"] == ack and slot.pop("unverified", None):
                        changed = True
                if transport_error is not None:
                    detail = _bounded(transport_error)
                    if slot.get("transport_error") != detail:
                        slot["transport_error"] = detail
                        changed = True
                    if slot.pop("unverified", None):
                        changed = True
            if changed:
                self._touch_locked(row)

    def on_status(self, frame, sender):
        """``BrowserPeer.on_status``: a `peer_message_status` frame from a verified ``sender``.

        It updates a row only when ``orig_msg_id`` is a message this server sent and ``sender`` is one of
        that row's recipients; the frame's id alone is never trusted."""
        if not isinstance(frame, dict) or not isinstance(sender, str):
            return
        mid = frame.get("orig_msg_id")
        with self._lock:
            row = self._sends.get(mid) if isinstance(mid, str) else None
            slot = row["recipients"].get(sender) if row is not None else None
            if slot is None:
                return
            status = {"status": frame.get("status"), "detail": frame.get("detail")}
            if slot.get("peer_status") != status:
                slot["peer_status"] = status
                self._touch_locked(row)

    # -- reading (webdata's BrowserView) -------------------------------------

    def restorable_mids(self):
        """Message ids of the retained outbound entries whose send reached the journal, oldest first."""
        with self._lock:
            out = []
            for entry, _size in self._log:
                if entry["dir"] != "out":
                    continue
                row = self._sends.get(entry["message_id"])
                if row is not None and row["journaled"]:
                    out.append(entry["message_id"])
            return out

    def first_page_bytes(self):
        """The bytes a first page of everything now retained would take (entries and rows)."""
        with self._lock:
            return sum(size for _entry, size in self._log) + sum(_size(row) for row in self._sends.values())

    def page(self, after_log, after_rows, first, max_bytes):
        """The log entries and status rows past a position, within ``max_bytes``.

        ``first`` takes the newest retained entries (a reload); otherwise the entries after
        ``after_log`` oldest first. Rows are those whose seq is past ``after_rows``. A cut leaves the
        returned positions at the last item sent. Returns ``{entries, sends, bytes, gap, cut, log_seq,
        rows_seq}``; ``gap`` is true when entries after ``after_log`` were already evicted."""
        with self._lock:
            items = list(self._log)
            oldest = items[0][0]["lseq"] if items else self._lseq + 1
            gap = not first and after_log + 1 < oldest
            used, cut = 0, False
            entries = []
            if first:
                for entry, size in reversed(items):
                    if used + size > max_bytes:
                        cut = True
                        # The newest entries that fit contiguously are served; the cursor is set past
                        # everything older, so those are flagged lost, as a retention gap would be.
                        gap = True
                        break
                    entries.append(entry)
                    used += size
                entries.reverse()
                log_seq = self._lseq
            else:
                log_seq = after_log
                for entry, size in items:
                    if entry["lseq"] <= after_log:
                        continue
                    if used + size > max_bytes:
                        cut = True
                        break
                    entries.append(entry)
                    used += size
                    log_seq = entry["lseq"]
                if not cut:
                    log_seq = max(after_log, self._lseq)
            rows, rows_seq, rows_cut = [], after_rows, False
            for row in reversed(list(self._sends.values())):  # newest change first; stop at the old ones
                if row["seq"] <= after_rows:
                    break
                rows.append(row)
            rows.reverse()  # oldest change first, so a cut keeps a contiguous prefix
            sent = []
            for row in rows:
                size = _size(row)
                if used + size > max_bytes:
                    rows_cut = True
                    break
                sent.append(copy.deepcopy(row))
                used += size
                rows_seq = row["seq"]
            if not rows_cut:
                rows_seq = max(after_rows, self._seq)
            return {
                "entries": [copy.deepcopy(e) for e in entries], "sends": sent, "bytes": used, "gap": gap,
                "cut": cut or rows_cut, "log_seq": log_seq, "rows_seq": rows_seq,
            }


# --------------------------------------------------------------------------
# Send gate
# --------------------------------------------------------------------------


class SendGate:
    """The acceptance lock: rate limits and queue capacity, checked and charged together.

    At most one accepted send per RATE_MIN_GAP seconds and RATE_PER_MINUTE per RATE_WINDOW, and at most
    SEND_QUEUE_MAX sends queued or running. ``accept(build)`` runs ``build`` under the lock only after
    every limit passed; a refusal returns None having charged and reserved nothing."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._charges = collections.deque()
        self._held = 0
        self._nonces = collections.OrderedDict()  # nonce -> binding dict, oldest first; memory only

    def _limits_open_locked(self, now):
        while self._charges and now - self._charges[0] >= RATE_WINDOW:
            self._charges.popleft()
        if self._charges and now - self._charges[-1] < RATE_MIN_GAP:
            return False
        return len(self._charges) < RATE_PER_MINUTE and self._held < SEND_QUEUE_MAX

    def _charge_and_build_locked(self, now, build):
        self._charges.append(now)
        self._held += 1
        try:
            return build()
        except BaseException:
            self._charges.pop()  # nothing was accepted: give the charge and the slot back
            self._held -= 1
            raise

    def accept(self, build):
        with self._lock:
            now = self._clock()
            if not self._limits_open_locked(now):
                return None
            return self._charge_and_build_locked(now, build)

    def _prune_nonces_locked(self, now):
        for nonce in [n for n, b in self._nonces.items() if b["expires_at"] <= now]:
            del self._nonces[nonce]

    def issue_nonce(self, route, sender, recipients, text_sha256, ack):
        """Record a preview's binding and return its single-use nonce (valid NONCE_TTL seconds).

        Nothing is charged. At most NONCE_MAX are outstanding; the oldest is evicted for a new one."""
        with self._lock:
            now = self._clock()
            self._prune_nonces_locked(now)
            while len(self._nonces) >= NONCE_MAX:
                self._nonces.popitem(last=False)
            nonce = secrets.token_urlsafe(32)
            self._nonces[nonce] = {
                "route": route, "sender": sender, "recipients": tuple(sorted(recipients)),
                "text_sha256": text_sha256, "ack": bool(ack), "expires_at": now + NONCE_TTL,
            }
            return nonce

    def confirm(self, nonce, route, sender, text_sha256, ack, build):
        """Consume ``nonce`` for a send, atomically with the limit check and the charge.

        Returns ``(CONFIRM_OK, build(recipients))``, ``(CONFIRM_STALE, None)`` for a missing, expired
        or mismatched nonce (a mismatch leaves it in place, an expired one is pruned), or
        ``(CONFIRM_LIMITED, None)`` when a limit is hit (the nonce stays usable, nothing is charged)."""
        with self._lock:
            now = self._clock()
            self._prune_nonces_locked(now)
            binding = self._nonces.get(nonce)
            if binding is None:
                return CONFIRM_STALE, None
            if (binding["route"], binding["sender"], binding["text_sha256"], binding["ack"]) != (
                    route, sender, text_sha256, bool(ack)):
                return CONFIRM_STALE, None
            if not self._limits_open_locked(now):
                return CONFIRM_LIMITED, None
            result = self._charge_and_build_locked(now, lambda: build(binding["recipients"]))
            del self._nonces[nonce]  # consumed only once the send was accepted
            return CONFIRM_OK, result

    @property
    def nonces_outstanding(self):
        with self._lock:
            return len(self._nonces)

    def release(self):
        """Free one slot: the job ended (the rate charge stays)."""
        with self._lock:
            self._held = max(0, self._held - 1)

    @property
    def held(self):
        with self._lock:
            return self._held


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


class SendWorker:
    """One daemon thread that commits and delivers accepted sends in order."""

    def __init__(self, state, peer, gate, guard):
        self.state = state
        self.peer = peer
        self.gate = gate
        self._guard = guard  # factory of the context manager held around resolution, writes and each delivery
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="peers-web-send", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop taking jobs; queued jobs are dropped with the process, a running one is not waited for long."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT)

    def submit(self, job):
        self._jobs.put_nowait(job)

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except Exception as exc:  # the attempt is consumed; nothing is retried
                try:
                    self._fail(job, exc)
                except Exception:  # the one worker thread must outlive any failure of its own reporting
                    pass
            finally:
                self.gate.release()

    def web_sender(self):
        return {"sid": self.peer.sid, "name": self.peer.name, "socket": self.peer.socket_path}

    # -- one job -------------------------------------------------------------

    def _fail(self, job, exc):
        mid = job["mid"]
        row = self.state.row(mid)
        reason = "%s: %s" % (type(exc).__name__, exc)
        try:
            sp_runtime.log("web chat: send %s failed: %r" % (mid, reason))
        except Exception:  # a broken log stream must not keep the row from reading its outcome
            pass
        if row is not None and row["journaled"]:
            self.state.update(mid, state=STATE_FAILED_AFTER_RECORDING, reason=reason, delivering=False)
        else:
            self.state.update(mid, state=STATE_NOT_SENT, reason=reason, delivering=False)

    def _process(self, job):
        if job["kind"] == "channel":
            self._process_channel(job)
        else:
            self._process_direct(job)

    def _refuse(self, mid, exc):
        self.state.update(mid, state=STATE_NOT_SENT, reason=sp_channels._reason(exc), delivering=False)

    def _process_direct(self, job):
        mid, to, text, ack = job["mid"], job["to"], job["text"], job["ack"]
        sender = self.state.typed
        shown = text + "\n" + sp_journal.pointer_text(mid) if ack else text
        try:
            sp_channels.check_text_size(shown, to)
        except sp_channels.ChannelError as exc:
            return self._refuse(mid, exc)
        try:
            with self._guard():
                resolved = sp_journal._resolve_recipients(to)
                sp_journal.record_message(sender, resolved, text, message_id=mid)
        except (sp_journal.JournalError, ValueError, OSError) as exc:
            return self._refuse(mid, exc)
        recipients = {t: {"outcome": None, "ack": ACK_PENDING if ack else ACK_NOT_REQUESTED} for t in resolved}
        self.state.update(mid, state=STATE_PENDING, journaled=True, delivering=True, recipients=recipients)
        args = sp_channels._web_member_args(self.web_sender())
        last_claude = None
        for member in resolved:
            is_claude = member.startswith("cc:")
            if is_claude and last_claude is not None:
                wait = sp_channels.PACE_SECONDS - (sp_channels._clock() - last_claude)
                if wait > 0:
                    sp_channels._sleep(wait)  # never under the guard
            with self._guard():
                outcome = sp_channels._deliver_member(args, member, sender, mid, shown, None)
            if is_claude:
                last_claude = sp_channels._clock()
            self.state.set_outcome(mid, member, outcome)
            if outcome.startswith(sp_channels.OUTCOME_REFUSED):
                # transport_failed returns its rc (0 here) only when the append worked.
                if sp_journal.transport_failed(mid, member, outcome[len(sp_channels.OUTCOME_REFUSED):], rc=0):
                    self.state.mark_update_failed(mid, member)
        self.state.update(mid, delivering=False)

    def _process_channel(self, job):
        mid, ack = job["mid"], job["ack"]

        def on_journaled(deliver):
            # Seed the recipients now, so a status frame or ACK that arrives during the paced fan-out has a slot.
            seeded = {t: {"outcome": None, "ack": ACK_PENDING if ack else ACK_NOT_REQUESTED} for t in deliver}
            self.state.update(mid, state=STATE_PENDING, journaled=True, delivering=True, recipients=seeded)

        # `expected_recipients` is set only by a confirmed `@all` to #general (the nonce's recipients);
        # `send_as_web` then refuses when the live deliverable set differs. A stored channel's post
        # addresses its roster and passes None. An exception escaping after the journal write reaches
        # `_run`, which reads the row's `journaled`.
        payload = sp_channels.send_as_web(
            job["key"], self.web_sender(), job["text"], ack, mid,
            deliver_guard=self._guard, resolve_guard=self._guard, on_journaled=on_journaled,
            expected_recipients=job.get("expected_recipients"))
        state = payload.get("state")
        if state == sp_channels.WEB_SENT:
            asked = bool(job["ack"])
            outcomes = payload.get("outcomes") or {}
            notes = payload.get("notes") or {}
            excluded = {x["member"]: x["reason"] for x in payload.get("excluded") or ()}
            recipients = {}
            for member in payload.get("recipients") or ():
                outcome = outcomes.get(member)
                if member in excluded:
                    outcome = sp_channels.OUTCOME_REFUSED + excluded[member]
                no_ack = (not asked) or member in excluded or member in notes
                recipients[member] = {"outcome": outcome, "ack": ACK_NOT_REQUESTED if no_ack else ACK_PENDING}
            note = payload.get("outcome_problem")
            self.state.merge_recipients(mid, recipients)
            self.state.update(mid, state=STATE_PENDING, journaled=True, delivering=False, note=note,
                              update_failed=bool(payload.get("update_failed")))
        elif state == sp_channels.WEB_NOT_SENT_JOURNALED:
            # Nothing was delivered: the recipients `on_journaled` seeded would read as ACK pending forever.
            self.state.update(mid, state=STATE_NOT_SENT_JOURNALED, reason=payload.get("reason"), journaled=True,
                              delivering=False, recipients={})
        else:
            self.state.update(mid, state=STATE_NOT_SENT, reason=payload.get("reason"), delivering=False)


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


class Chat:
    """The chat-mode services of one server: browser peer, state, gate and worker.

    ``start()`` is the ordered startup (environment scrub, peer, output routers, worker) and cleans up
    whatever it created if a step fails; ``stop()`` undoes it and is idempotent."""

    def __init__(self, clock=time.monotonic, guard=None):
        self.gate = SendGate(clock)
        self._guard = guard if guard is not None else contextlib.nullcontext
        self.peer = None
        self.state = None
        self.worker = None
        self.ready = False
        self._routers = None
        self._stop_lock = threading.Lock()
        self._stopped = False

    def info(self):
        return {"peer": self.state.typed, "name": self.state.name, "user": self.state.user}

    def request_stop(self):
        """The one call a signal handler makes: it takes no lock."""
        peer = self.peer
        if peer is not None:
            peer.request_stop()

    def start(self):
        scrub_environment()
        peer = sp_webpeer.BrowserPeer(on_inbound=self._inbound, on_status=self._status)
        self.peer = peer
        self.state = BrowserState(peer.sid, peer.name, peer.name[len("web-"):])
        try:
            peer.start()
            self._install_routers()
            self.worker = SendWorker(self.state, peer, self.gate, self._guard)
            self.worker.start()
            self.ready = True
        except BaseException:
            self.stop()
            raise
        return self

    def _inbound(self, entry):
        state = self.state
        if state is not None:
            state.add_inbound(entry)

    def _status(self, frame, sender):
        state = self.state
        if state is not None:
            state.on_status(frame, sender)

    def _install_routers(self):
        out, err = sys.stdout, sys.stderr
        routers = (sp_runtime.ThreadRouter(out), sp_runtime.ThreadRouter(err))
        sys.stdout, sys.stderr = routers
        self._routers = routers

    def _restore_routers(self):
        routers, self._routers = self._routers, None
        if routers is None:
            return
        # Put back what was wrapped, but only where our router is still the current stream.
        if sys.stdout is routers[0]:
            sys.stdout = routers[0].original
        if sys.stderr is routers[1]:
            sys.stderr = routers[1].original

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.ready = False
        if self.worker is not None:
            self.worker.stop()
        if self.peer is not None:
            self.peer.stop()
        self._restore_routers()

    def accept(self, to, text, ack):
        """Accept a validated send: its message id, or None when a limit is hit (nothing charged or queued).

        ``to`` is a channel key string or a list of typed addresses."""
        def build():
            mid = str(uuid.uuid4())
            if isinstance(to, str):
                self.state.accept_channel(mid, to, text, ack)
                job = {"kind": "channel", "mid": mid, "key": to, "text": text, "ack": ack}
            else:
                self.state.accept_direct(mid, to, text, ack)
                job = {"kind": "direct", "mid": mid, "to": list(to), "text": text, "ack": ack}
            try:
                self.worker.submit(job)
            except BaseException:
                self.state.discard(mid)  # a queued row nobody owns would never be evicted
                raise
            return mid

        return self.gate.accept(build)

    # -- confirmed @all to #general ------------------------------------------

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def preview_all(self, text, recipients):
        """The confirm nonce for an `@all` to #general addressed to ``recipients``. Accepts and charges nothing.

        The ACK request is part of the binding and is always true for an `@all`."""
        return self.gate.issue_nonce(
            sp_constants.CHANNEL_GENERAL, self.state.typed, recipients, self._digest(text), True)

    def confirm_all(self, nonce, text):
        """Accept a confirmed `@all`: ``(CONFIRM_OK, mid)``, ``(CONFIRM_STALE, None)`` or ``(CONFIRM_LIMITED, None)``."""
        key = sp_constants.CHANNEL_GENERAL

        def build(recipients):
            mid = str(uuid.uuid4())
            self.state.accept_channel(mid, key, text, True)
            job = {"kind": "channel", "mid": mid, "key": key, "text": text, "ack": True,
                   "expected_recipients": list(recipients)}
            try:
                self.worker.submit(job)
            except BaseException:
                self.state.discard(mid)
                raise
            return mid

        return self.gate.confirm(nonce, key, self.state.typed, self._digest(text), True, build)
