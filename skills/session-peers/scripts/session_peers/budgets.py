"""Finite reply accounting and held-reply lifecycle."""

from __future__ import annotations
import os
import time
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import storage as sp_storage

class ReplyBudget:
    """Reply accounting and held replies in the owner's existing state file.

    The owning shim supplies its thread ID, current name/socket, and the
    _save_state callback. Discovery/socket checks and frame delivery come from
    shim callbacks; this component adds no transport import, lock, or file format.
    """

    def __init__(self, owner, state, *, records, socket_valid, deliver_frame):
        self.owner = owner
        self._records = records
        self._socket_valid = socket_valid
        self._deliver_frame = deliver_frame
        self.budgets = dict(state.get("budgets") or {})
        self.budget_sender_sid = state.get("budget_sender_sid")
        self.budget_last_at = sp_runtime.parse_time(state.get("budget_last_at"))
        # Sessions already told (once) that a reply of theirs was dropped for
        # budget; cleared whenever the budget sequence resets so a genuinely new
        # sequence can notify again.
        self.budget_notified = set(state.get("budget_notified") or [])
        # The latest budget-dropped reply per requesting session, held (not lost) so
        # an explicit `peers.py budget reset` can release it. Bounded to one per
        # session, expired with the idle window, discarded on any other sequence
        # reset, and kept only in this mode-0600 state file, never in the log.
        self.held = dict(state.get("held") or {})
        # `budget allow`: {"sid", "total", "at"} raises that one requester's cap
        # for the current sequence. Dropped whenever the sequence resets.
        allowance = state.get("allowance")
        self.allowance = allowance if self._valid_allowance(allowance) else None
        # `buddy set --replies N`: a finite TOTAL for one owner session on this
        # thread, spent across sequences, never replenished. Only `buddy clear`
        # or binding another buddy revokes it (a revoke marker).
        binding = state.get("binding")
        self.binding = binding if self._valid_binding(binding) else None
        # Set when a startup reset leaves held replies to release: the release
        # waits until this shim is bound and registered, so the reply's `from`
        # route names a socket that exists.
        self.release_after_start = False

    def configure_window(self):
        self.reply_budget_window = sp_runtime._float_env(
            "SESSION_PEERS_REPLY_BUDGET_WINDOW",
            sp_constants.REPLY_BUDGET_WINDOW_DEFAULT,
        )
        if self.reply_budget_window <= 0:
            self.reply_budget_window = sp_constants.REPLY_BUDGET_WINDOW_DEFAULT
        if self.allowance and not (
            self._allowance_waiting(time.time())
            or (
                self.allowance.get("bound", True)
                and self.budget_last_at is not None
                and not self._sequence_expired(time.time())
            )
        ):
            # A restart keeps a grant only while it would still apply.
            sp_runtime.log("reply allowance for %s expired while the shim was down"
                % self.allowance.get("sid"))
            self.allowance = None


    def consume_reset(self, initial=False):
        path = sp_storage.budget_reset_path(self.owner.thread_id)
        if not os.path.exists(path):
            return
        try:
            os.unlink(path)
        except OSError:
            return
        self.budgets = {}
        self.budget_sender_sid = None
        self.budget_last_at = None
        self.budget_notified = set()
        self.allowance = None
        if initial:
            # Not yet bound or registered: defer the release (see run()).
            self.release_after_start = bool(self.held)
            return
        sp_runtime.log("reply budget reset for thread %s" % self.owner.thread_id)
        # An explicit reset is the supervision signal the loop guard waits for,
        # so it also releases the reply the guard held back.
        self.release_held()
        self.owner._save_state()


    def expire_held(self, now=None):
        """Purge held replies past the idle window from memory AND the state file."""
        if not self.held:
            return
        now = time.time() if now is None else now
        stale = [
            sid
            for sid, entry in self.held.items()
            if not isinstance(entry, dict)
            or now - float(entry.get("at") or 0) > self.reply_budget_window
        ]
        if not stale:
            return
        for sid in stale:
            self.held.pop(sid, None)
        sp_runtime.log("purged %d expired held reply(s)" % len(stale))
        self.owner._save_state()


    def release_held(self):
        """Deliver each still-fresh held reply once; it opens the new sequence."""
        if not self.held:
            return
        records = self._records()
        for sid in list(self.held):
            self._release_one(sid, "reset", records)


    def _release_one(self, sid, reason, records):
        """Release one held reply; drop it once delivered or undeliverable.

        A transient write failure keeps a fresh entry, tagged with why it was
        being released, so `_retry_held` tries it again. Usage is counted only
        by a delivery that succeeded, so a retry never double-counts.
        """
        entry = self.held.get(sid)
        outcome = self._deliver_held(sid, entry, records, new_sequence=(reason == "reset"))
        if outcome == "failed":
            entry["release"] = reason
        else:
            self.held.pop(sid, None)
        return outcome


    def retry_held(self):
        """Retry held replies whose release failed on a transient write error."""
        pending = [
            (sid, entry.get("release"))
            for sid, entry in self.held.items()
            if isinstance(entry, dict) and entry.get("release")
        ]
        if not pending:
            return
        records = self._records()
        changed = False
        for sid, reason in pending:
            if reason == "allow" and self.budgets.get(sid, 0) >= self.cap_for(sid):
                # The allowance that released it is gone or spent.
                self.held[sid].pop("release", None)
                changed = True
                continue
            changed = self._release_one(sid, reason, records) != "failed" or changed
        if changed:
            self.owner._save_state()


    def _deliver_held(self, sid, entry, records, new_sequence):
        """One held reply to its live requester, if still fresh and routable.

        Returns ``delivered``, ``failed`` (worth a retry) or ``discarded``.
        """
        if not isinstance(entry, dict) or not entry.get("text"):
            return "discarded"
        now = time.time()
        age = now - float(entry.get("at") or 0)
        if age > self.reply_budget_window:
            sp_runtime.log("discarding a held reply for %s: %.0fs old" % (sid, age))
            return "discarded"
        rec = next((r for r in records if r.get("sessionId") == sid), None)
        if rec is None:
            sp_runtime.log("discarding a held reply: session %s is gone" % sid)
            return "discarded"
        if not self._socket_valid(rec.get("messagingSocketPath")):
            sp_runtime.log("discarding a held reply: %s listens outside the allowlist" % sid)
            return "discarded"
        text = sp_protocol.reply_text(entry["text"], entry.get("mid"), held_reply=True)
        try:
            body = sp_protocol.build_cc_body(text, self.owner.thread_id, self.owner.name, self.owner.sock_path)
        except ValueError as exc:
            sp_runtime.log("cannot build a held reply for %s: %s" % (sid, exc))
            return "discarded"
        if not self._deliver_frame(rec, sp_protocol.build_user_frame(body, self.owner.sock_path)):
            sp_runtime.log("keeping the held reply for %s to retry" % sid)
            return "failed"
        # An explicit reset opens a new sequence; an allowance continues the
        # current one, so the release counts toward its usage.
        self.budgets[sid] = 1 if new_sequence else self.budgets.get(sid, 0) + 1
        self.spend_binding(sid)
        self.budget_sender_sid = sid
        self.budget_last_at = now
        sp_runtime.log(
            "released a held reply (turn %s) to %s"
            % (entry.get("turn_id"), rec.get("name") or sid)
        )
        return "delivered"


    @staticmethod
    def _valid_allowance(value):
        """Well-formed sid and total, and a grant time that parses."""
        if not isinstance(value, dict) or not isinstance(value.get("sid"), str):
            return False
        total = value.get("total")
        return (
            isinstance(total, int)
            and not isinstance(total, bool)
            and 1 <= total <= sp_constants.BUDGET_ALLOW_MAX
            and sp_runtime.parse_time(value.get("at")) is not None
        )


    def _allowance_fresh(self, at, now):
        """A grant counts only within the idle window of when it was GRANTED.

        Consumption can lag the grant (a shim that starts late), so the
        original time decides, never the time the shim read it. A grant stamped
        more than a minute ahead is refused rather than trusted to last.
        """
        return -60.0 <= now - at <= self.reply_budget_window


    def cap_for(self, sid):
        """The consecutive-reply cap for one requesting session."""
        cap = sp_constants.REPLY_BUDGET
        if self.allowance and self.allowance.get("sid") == sid:
            cap = max(cap, self.allowance["total"])
        binding = self.binding
        if binding and binding["sid"] == sid:
            remaining = binding["total"] - binding["spent"]
            if remaining > 0:
                # Replies already sent this sequence were counted in `spent`,
                # so the cap sits `remaining` above them, whichever sequence.
                cap = max(cap, self.budgets.get(sid, 0) + remaining)
        return cap


    @staticmethod
    def _valid_binding(value):
        if not isinstance(value, dict):
            return False
        if not isinstance(value.get("sid"), str) or not value.get("sid"):
            return False
        if not isinstance(value.get("bind_id"), str) or not value.get("bind_id"):
            return False
        for key, low in (("total", 1), ("spent", 0)):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int):
                return False
            if not low <= number <= sp_constants.BUDDY_REPLIES_MAX:
                return False
        return True


    def spend_binding(self, sid):
        binding = self.binding
        if binding and binding["sid"] == sid and binding["spent"] < binding["total"]:
            binding["spent"] += 1


    def consume_binding(self, initial=False):
        """Apply a `buddy set --replies` grant, or a `buddy clear`/rebind revoke."""
        path = sp_storage.budget_binding_path(self.owner.thread_id)
        if not os.path.exists(path):
            return
        # Read and unlink under the lock: a marker written between the two
        # would be deleted unread. A busy lock waits for the next poll.
        try:
            with sp_storage.binding_lock(self.owner.thread_id, timeout=2.0):
                marker = sp_runtime.read_json(path, None)
                try:
                    os.unlink(path)
                except OSError:
                    return
        except sp_storage.BindingLockTimeout:
            sp_runtime.log("binding marker for thread %s is locked; retrying" % self.owner.thread_id)
            return
        if isinstance(marker, dict) and marker.get("revoke") is True:
            binding = self.binding
            if (
                binding
                and binding["sid"] == marker.get("sid")
                and binding["bind_id"] == marker.get("bind_id")
            ):
                sp_runtime.log("buddy reply allowance for %s revoked" % binding["sid"])
                self.binding = None
                self.owner._save_state()
            return
        grant = None
        if isinstance(marker, dict):
            grant = {
                "sid": marker.get("sid"),
                "bind_id": marker.get("bind_id"),
                "total": marker.get("total"),
                "spent": 0,
            }
        if not self._valid_binding(grant):
            sp_runtime.log("ignoring a malformed buddy reply allowance for thread %s"
                % self.owner.thread_id)
            return
        current = self.binding
        if (
            current
            and current["sid"] == grant["sid"]
            and current["bind_id"] == grant["bind_id"]
        ):
            # The same binding again: never a replenishment.
            grant["total"] = max(current["total"], grant["total"])
            grant["spent"] = min(current["spent"], grant["total"])
        sid = grant["sid"]
        old_cap = self.cap_for(sid)
        self.binding = grant
        new_cap = self.cap_for(sid)
        sp_runtime.log(
            "buddy reply allowance for %s set to %d total (%d spent)"
            % (sid, grant["total"], grant["spent"])
        )
        if new_cap > old_cap:
            self.budget_notified.discard(sid)
            if (
                not initial
                and self.budgets.get(sid, 0) < new_cap
                and sid in self.held
            ):
                self._release_one(sid, "allow", self._records())
        self.owner._save_state()


    def consume_allowance(self):
        """Apply a `budget allow` grant: a total, never additive or replenishing."""
        path = sp_storage.budget_allow_path(self.owner.thread_id)
        if not os.path.exists(path):
            return
        grant = sp_runtime.read_json(path, None)
        try:
            os.unlink(path)
        except OSError:
            return
        if not self._valid_allowance(grant):
            sp_runtime.log("ignoring a malformed reply allowance for thread %s" % self.owner.thread_id)
            return
        sid, total = grant["sid"], grant["total"]
        granted_at = sp_runtime.parse_time(grant["at"])
        if not self._allowance_fresh(granted_at, time.time()):
            sp_runtime.log(
                "ignoring a stale reply allowance for %s: granted %.0fs ago, "
                "outside the %.0fs idle window"
                % (sid, time.time() - granted_at, self.reply_budget_window)
            )
            return
        if self._sequence_expired(time.time()):
            # A sequence already past its idle window is over even though no
            # turn has said so yet: end it first, so this grant waits for and
            # binds to the NEXT sequence instead of the dead one.
            self._reset_sequence(
                "the %.0fs idle window elapsed" % self.reply_budget_window, time.time()
            )
        current = self.allowance
        if current and current.get("sid") == sid and current["total"] >= total:
            sp_runtime.log(
                "reply allowance of %d for %s already covers %d; unchanged"
                % (current["total"], sid, total)
            )
            self.owner._save_state()  # an idle expiry above may have ended a sequence
            return
        old_cap = self.cap_for(sid)
        # Bound: the grant continues sid's running sequence and ends with it.
        # Otherwise it waits, while fresh, for sid's next sequence to open it.
        self.allowance = {
            "sid": sid,
            "total": total,
            "at": granted_at,
            "bound": self.budget_sender_sid == sid,
        }
        new_cap = self.cap_for(sid)
        sp_runtime.log(
            "reply allowance for %s set to %d (%d spent)"
            % (sid, new_cap, self.budgets.get(sid, 0))
        )
        if new_cap > old_cap:
            # The requester may hit the raised cap later and should hear so.
            self.budget_notified.discard(sid)
            if self.budgets.get(sid, 0) < new_cap and sid in self.held:
                self._release_one(sid, "allow", self._records())
        self.owner._save_state()


    def advance_sequence(self, tag):
        """Reset the loop guard when the peer sequence is genuinely broken."""
        sender_sid = tag.get("sid") if isinstance(tag, dict) else None
        now = time.time()
        expired = self._sequence_expired(now)
        changed_peer = sender_sid != self.budget_sender_sid
        if sender_sid is None or expired or changed_peer:
            if sender_sid is None:
                reason = "a direct Codex turn"
            elif expired:
                reason = "the %.0fs idle window elapsed" % self.reply_budget_window
            else:
                reason = "the requesting peer changed"
            self._reset_sequence(reason, now)
        if self.allowance and self.allowance.get("sid") == sender_sid:
            # The grantee's sequence is running: the grant now ends with it.
            self.allowance["bound"] = True
        self.budget_sender_sid = sender_sid
        self.budget_last_at = now if sender_sid else None


    def _sequence_expired(self, now):
        return (
            self.budget_last_at is not None
            and now - self.budget_last_at > self.reply_budget_window
        )


    def _reset_sequence(self, reason, now):
        """End the current sequence: its usage, notices, held replies and any
        grant bound to it go; a fresh grant still waiting for its grantee stays."""
        if self.budgets:
            sp_runtime.log("reply budget sequence reset: %s" % reason)
        if self.held:
            # Only an explicit reset releases a held reply; a sequence that
            # moved on must not receive a stale answer later.
            sp_runtime.log("discarding %d held reply(s): the sequence moved on" % len(self.held))
        self.budgets = {}
        self.budget_notified = set()
        self.held = {}
        if self.allowance and not self._allowance_waiting(now):
            sp_runtime.log("reply allowance for %s dropped: the sequence moved on"
                % self.allowance.get("sid"))
            self.allowance = None
        self.budget_sender_sid = None
        self.budget_last_at = None


    def _allowance_waiting(self, now):
        """True for a fresh grant still waiting for its grantee's next sequence.

        A grant made while another peer (or nobody) held the sequence applies
        to the grantee's next sequence, so that peer's turns or a direct turn
        do not spend it. A grant that already ran with a sequence of the
        grantee's (bound) ends when that sequence does, and an old one expires.
        """
        return (
            not self.allowance.get("bound", True)
            and self._allowance_fresh(sp_runtime.parse_time(self.allowance.get("at")) or 0.0, now)
        )

