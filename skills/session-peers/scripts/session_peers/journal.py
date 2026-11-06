"""Session-peers journal: one user-wide, append-only record of peer messages.

The journal lives at ``~/.codex/session-peers/journal`` regardless of
CODEX_HOME, so every session of the OS user sees the same file. It holds
``journal.jsonl`` (one JSON record per line), ``journal.meta.json`` (a derived
accelerator, never authoritative) and ``journal.lock``.

Durability covers process crashes only: every write is fsynced and every
rewrite is an atomic rename, but nothing here claims power-loss safety.

Trust: a record's recipient list is an addressing rule between cooperating
sessions of one OS user, not confidentiality. Any process of that user can read
the file; ``read_only_view`` is that same-user trust exception and is not a
session.

Records (``schema`` 1, ``seq``, ``type``, ``at``):

* ``message``: ``message_id``, ``kind`` (message|request|reply), ``from``,
  ``to`` (typed ids) and ``body``; request/reply add ``request_id``,
  ``expires_at`` and ``mailbox`` (the request meta file), a reply also
  ``in_reply_to``.
* ``ack``: ``message_id``, ``for`` (the recipient slot) and ``by``.
* ``settled``: ``message_id``, ``state`` (replied|consumed) and ``by``.
* ``transport_error``: ``message_id``, ``for`` and ``detail``; informational.
* ``handover``: ``from``, ``to`` and ``through_seq``.

A handover covers every message with ``seq <= through_seq``. A session may read
or ACK a slot when it is that slot or reaches it through covering handovers.
Malformed lines are counted and never trusted. A rewrite (pruning, or an event
that enables pruning) drops them. A record dated ahead of the clock by at most
the age cap (JOURNAL_FUTURE_SKEW, 14 days) is valid, so a clock that stepped
back keeps its records through rewrites and they age out once real time passes
their ``at``. A record dated further ahead is malformed and a rewrite drops it:
the residual is that a clock step-back of more than 14 days can lose records at
the next rewrite. Every line with an integer ``seq`` advances the last seen seq
even when it is otherwise malformed, so a lost meta file cannot make a seq be
reused. A line that is a
JSON object with an integer ``seq`` and a finite ``at`` and either an integer
``schema`` above 1, or schema 1 with an unknown string ``type``, is a later
version's record: it is not interpreted or counted as malformed, a rewrite keeps
it verbatim, it counts toward the size, and only the age cap drops it.
"""

from __future__ import annotations
import errno
import fcntl
import functools
import json
import math
import os
import stat
import time
import uuid
from . import constants as sp_constants
from . import claude as sp_claude, codex as sp_codex, identity as sp_identity, opencode as sp_opencode

JOURNAL_NAME = "journal.jsonl"
META_NAME = "journal.meta.json"
LOCK_NAME = "journal.lock"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_KINDS = ("message", "request", "reply")
_TYPES = ("message", "ack", "settled", "transport_error", "handover")
_STATES = ("replied", "consumed")
_STATE_FOR_KIND = {"request": "replied", "reply": "consumed"}
_SLOT_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET")
_CODEX_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")

# Tests patch this to freeze or advance the clock.
_now = time.time


class JournalError(Exception):
    """Base of every journal failure."""


class JournalFull(JournalError):
    """Nothing eligible can be pruned to fit the record (CLI exit 75).

    The journal file is left byte-identical, except for the repair of a torn tail.
    """


class JournalUnavailable(JournalError):
    """The journal cannot be used safely (CLI exit 1)."""

    def __init__(self, detail):
        super().__init__("session-peers journal unavailable: %s" % detail)


class JournalRefused(JournalError):
    """Authorization or validation refused the request (CLI exit 1)."""


class JournalUpdateFailed(JournalError):
    """A post-transport journal update could not be written (CLI exit 76). Never swallow."""


def _failpoint(name):
    """No-op crash seam. A child-process test patches it to os._exit at ``name``.

    Names: mid_line, after_append_before_meta, before_prune_rename,
    after_prune_rename, meta_tmp_written.
    """


def reply_message_id(request_id):
    """The deterministic message id of the one reply to ``request_id``."""
    return str(uuid.uuid5(uuid.UUID(sp_constants.JOURNAL_REPLY_NAMESPACE), "reply:" + str(request_id)))


def journal_dir():
    """The journal directory, derived from HOME only (never CODEX_HOME)."""
    return os.path.join(os.path.expanduser("~/.codex"), "session-peers", "journal")


def _cap():
    """Size cap in line bytes; the env var may only lower the default."""
    default = sp_constants.JOURNAL_MAX_BYTES_DEFAULT
    raw = os.environ.get("SESSION_PEERS_JOURNAL_MAX_BYTES", "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 0 < value < default else default


def _age_cutoff(now):
    return now - sp_constants.JOURNAL_AGE_DAYS * 86400.0


# --------------------------------------------------------------------------
# Identities
# --------------------------------------------------------------------------


# ``fullmatch`` (not ``match``) so the ``$`` in the shared patterns cannot accept a trailing newline.
def _full_uuid(value):
    return isinstance(value, str) and sp_constants.UUID_RE.fullmatch(value) is not None


def _full_oc(value):
    return isinstance(value, str) and sp_constants.OPENCODE_SESSION_ID_RE.fullmatch(value) is not None


def _norm(value):
    """A typed id in canonical form (lowercase UUID), or ValueError."""
    typed = sp_identity.parse_typed(value)
    kind, ident = typed["kind"], typed["uuid"]
    if not (_full_oc(ident) if kind == "oc" else _full_uuid(ident)):
        raise ValueError("expected cc:<uuid>, codex:<uuid> or oc:<ses_id>, got %r" % (value,))
    return "%s:%s" % (kind, ident if kind == "oc" else ident.lower())


def _norm_caller(value):
    try:
        return _norm(value)
    except ValueError as exc:
        raise JournalRefused("caller identity is not valid: %s" % exc)


def caller():
    """The typed id of the calling session, from the environment only.

    Exactly one runtime may be named: Claude, Codex or OpenCode. An OpenCode
    id is verified against the service before anything is read or written.
    """
    claude_sid = sp_claude._current_claude_session_id()
    codex_vals = [os.environ[n] for n in _CODEX_ENV if os.environ.get(n)]
    oc_sid = sp_opencode.current_session_id()
    groups = []
    if claude_sid:
        groups.append(("Claude", [n for n in _SLOT_ENV if os.environ.get(n)]))
    if codex_vals:
        groups.append(("Codex", [n for n in _CODEX_ENV if os.environ.get(n)]))
    if oc_sid:
        groups.append(("OpenCode", ["OPENCODE_SESSION_ID"]))
    if not groups:
        raise JournalRefused(
            "cannot tell which session is asking: none of CLAUDE_CODE_SESSION_ID, "
            "CODEX_THREAD_ID, CODEX_SESSION_ID or OPENCODE_SESSION_ID is set"
        )
    if len(groups) > 1:
        names = [n for _label, envs in groups for n in envs]
        extra = [n for _label, envs in groups[1:] for n in envs]
        raise JournalRefused(
            "this environment names more than one session runtime (%s); the journal "
            "cannot tell which one is asking. Unset the variables of the others, e.g. "
            "env %s peers.py ..." % (", ".join(names), " ".join("-u " + n for n in extra))
        )
    label = groups[0][0]
    if label == "Claude":
        if not _full_uuid(claude_sid):
            raise JournalRefused("this Claude session's id %r is not a UUID" % claude_sid)
        return "cc:" + claude_sid.lower()
    if label == "Codex":
        if len(set(codex_vals)) > 1:
            raise JournalRefused("CODEX_THREAD_ID and CODEX_SESSION_ID name different threads")
        if not _full_uuid(codex_vals[0]):
            raise JournalRefused("this Codex thread's id %r is not a UUID" % codex_vals[0])
        return "codex:" + codex_vals[0].lower()
    if not _full_oc(oc_sid):
        raise JournalRefused("this OpenCode session's id %r is not a session id" % oc_sid)
    try:
        sp_opencode.get_session(oc_sid)
    except sp_opencode.OpenCodeError as exc:
        raise JournalRefused("cannot verify OpenCode session %s: %s" % (oc_sid, exc))
    return "oc:" + oc_sid


# --------------------------------------------------------------------------
# Record parsing and validation
# --------------------------------------------------------------------------


def _reject_constant(name):
    raise ValueError("non-finite number %s" % name)


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _typed_ok(value):
    return isinstance(value, str) and _norm(value) == value


def _uuid_ok(value):
    return _full_uuid(value) and value == value.lower()


def _check(cond, what):
    if not cond:
        raise ValueError(what)


class _Foreign(ValueError):
    """A record with a valid ``seq`` and ``at`` from a later version: int schema > 1, or
    schema 1 with a string type this version does not know."""


def _validate(d, now):
    """Raise ValueError unless ``d`` is one well-formed record.

    _Foreign if only unknown. A record dated more than JOURNAL_FUTURE_SKEW ahead is malformed.
    """
    try:
        _validate_fields(d)
    except _Foreign:
        _check(d["at"] <= now + sp_constants.JOURNAL_FUTURE_SKEW, "at is in the future")
        raise
    _check(d["at"] <= now + sp_constants.JOURNAL_FUTURE_SKEW, "at is in the future")
    return d


def _validate_fields(d):
    _check(isinstance(d, dict), "record is not an object")
    _check(_is_int(d.get("seq")) and d["seq"] >= 1, "seq")
    _check(_is_num(d.get("at")) and math.isfinite(d["at"]), "at")
    schema, kind = d.get("schema"), d.get("type")
    if _is_int(schema) and (schema > 1 or (schema == 1 and isinstance(kind, str) and kind not in _TYPES)):
        raise _Foreign("unknown schema or type")
    _check(_is_int(schema) and schema == 1, "schema")
    _check(isinstance(kind, str) and kind in _TYPES, "type")
    if kind == "message":
        _check(_uuid_ok(d.get("message_id")), "message_id")
        _check(d.get("kind") in _KINDS, "kind")
        _check(d.get("from") is None or _typed_ok(d["from"]), "from")
        to = d.get("to")
        _check(isinstance(to, list) and 1 <= len(to) <= sp_constants.JOURNAL_MAX_RECIPIENTS, "to")
        _check(all(_typed_ok(t) for t in to) and len(set(to)) == len(to), "to entries")
        _check(isinstance(d.get("body"), str), "body")
        if d["kind"] != "message":
            _check(_uuid_ok(d.get("request_id")), "request_id")
            _check(d.get("in_reply_to") is None or _uuid_ok(d["in_reply_to"]), "in_reply_to")
            _check(_is_num(d.get("expires_at")) and math.isfinite(d["expires_at"]), "expires_at")
            _check(isinstance(d.get("mailbox"), str) and os.path.isabs(d["mailbox"]), "mailbox")
    elif kind == "ack":
        _check(_uuid_ok(d.get("message_id")), "message_id")
        _check(_typed_ok(d.get("for")) and _typed_ok(d.get("by")), "for/by")
    elif kind == "settled":
        _check(_uuid_ok(d.get("message_id")), "message_id")
        _check(d.get("state") in _STATES, "state")
        _check(_typed_ok(d.get("by")), "by")
    elif kind == "transport_error":
        _check(_uuid_ok(d.get("message_id")), "message_id")
        _check(_typed_ok(d.get("for")), "for")
        _check(isinstance(d.get("detail"), str), "detail")
    elif kind == "handover":
        _check(_typed_ok(d.get("from")) and _typed_ok(d.get("to")), "from/to")
        _check(d["from"] != d["to"], "self handover")
        _check(_is_int(d.get("through_seq")) and d["through_seq"] >= 0, "through_seq")
    else:
        raise ValueError("type")


def _parse_line(raw, now):
    """``(record, foreign)`` for a usable line, None for a malformed one."""
    try:
        text = raw.decode("utf-8")
        d = json.loads(text, parse_constant=_reject_constant)
        try:
            return _validate(d, now), False
        except _Foreign:
            return d, True
    except (ValueError, RecursionError, TypeError, OverflowError, UnicodeError):
        return None


class _Rec:
    """One usable line. ``dead`` marks a record the next rewrite drops.

    ``foreign`` marks an unknown-schema or unknown-type record kept verbatim.
    """

    __slots__ = ("d", "size", "line", "dead", "foreign")

    def __init__(self, d, line, foreign=False):
        self.d = d
        self.line = line
        self.size = len(line) + 1
        self.dead = False
        self.foreign = foreign


def _loose_seq(raw):
    """The integer ``seq`` of a line that is otherwise unusable, or 0."""
    try:
        d = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError, TypeError, OverflowError, UnicodeError):
        return 0
    if isinstance(d, dict) and _is_int(d.get("seq")) and d["seq"] >= 1:
        return d["seq"]
    return 0


def _parse(data, now):
    """Usable records in file order, the malformed-line count and the last seq.

    Any line with an integer ``seq`` advances the last seq, even a malformed or
    skipped one, so a seq is never reused. A final line without its newline is a
    torn or in-flight write: ignored.
    """
    recs, malformed, last_seq, pos = [], 0, 0, 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        raw = data[pos:nl]
        pos = nl + 1
        parsed = _parse_line(raw, now)
        if parsed is None:
            malformed += 1
            last_seq = max(last_seq, _loose_seq(raw))
            continue
        if parsed[0]["seq"] <= last_seq:
            malformed += 1
            continue
        last_seq = parsed[0]["seq"]
        recs.append(_Rec(parsed[0], raw, parsed[1]))
    return recs, malformed, last_seq


def _encode(d):
    return json.dumps(d, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def _bucket(d):
    return d.get("from") or "null"


# --------------------------------------------------------------------------
# In-memory model: entries, grants and retention
# --------------------------------------------------------------------------


class _Entry:
    __slots__ = ("msg", "acks", "settled", "transport", "events")

    def __init__(self, rec):
        self.msg = rec
        self.acks = {}
        self.settled = None
        self.transport = None
        self.events = []

    @property
    def d(self):
        return self.msg.d


class _Model:
    def __init__(self, recs):
        self.recs = []
        self.entries = {}
        self.grants = {}  # (from, to) -> record, oldest first
        self.foreign = []
        self._index = None
        self.total = 0
        self.msg_bytes = 0
        self.by_sender = {}
        for rec in recs:
            self.add(rec)

    def _count(self, rec, sign):
        self.total += sign * rec.size
        d = rec.d
        if not rec.foreign and d["type"] == "message":
            key = _bucket(d)
            self.by_sender[key] = self.by_sender.get(key, 0) + sign * rec.size
            if d["kind"] == "message":
                self.msg_bytes += sign * rec.size

    def _kill(self, rec):
        if not rec.dead:
            rec.dead = True
            self._count(rec, -1)

    def add(self, rec):
        """Append ``rec``; a duplicate or dangling one is marked dead, not kept."""
        self.recs.append(rec)
        if rec.foreign:
            self.foreign.append(rec)
            self._count(rec, 1)
            return
        d = rec.d
        kind = d["type"]
        if kind == "handover":
            old = self.grants.pop((d["from"], d["to"]), None)
            if old is not None:
                self._kill(old)
            self.grants[(d["from"], d["to"])] = rec
            self._index = None
            self._count(rec, 1)
            if len(self.grants) > sp_constants.JOURNAL_MAX_GRANTS_TOTAL:
                self._drop_grant(next(iter(self.grants)))
            return
        if kind == "message":
            if d["message_id"] in self.entries:
                rec.dead = True
                return
            self.entries[d["message_id"]] = _Entry(rec)
            self._count(rec, 1)
            return
        entry = self.entries.get(d["message_id"])
        keep = False
        if entry is not None:
            if kind == "ack":
                keep = d["for"] in entry.d["to"] and d["for"] not in entry.acks
                if keep:
                    entry.acks[d["for"]] = rec
            elif kind == "settled":
                keep = entry.settled is None and _STATE_FOR_KIND.get(entry.d["kind"]) == d["state"]
                if keep:
                    entry.settled = rec
            else:
                keep = d["for"] in entry.d["to"]
                if keep:
                    entry.transport = rec
        if not keep:
            rec.dead = True
            return
        entry.events.append(rec)
        self._count(rec, 1)

    def drop_entry(self, entry):
        self.entries.pop(entry.d["message_id"], None)
        self._kill(entry.msg)
        for rec in entry.events:
            self._kill(rec)

    def _drop_grant(self, key):
        self._kill(self.grants.pop(key))
        self._index = None

    def age_prune(self, now):
        cutoff = _age_cutoff(now)
        for entry in list(self.entries.values()):
            if entry.d["at"] < cutoff:
                self.drop_entry(entry)
        for key, grant in list(self.grants.items()):
            if grant.d["at"] < cutoff:
                self._drop_grant(key)
        for rec in self.foreign:
            if rec.d["at"] < cutoff:
                self._kill(rec)

    def prune_grants(self, keep=None):
        """Keep only grants reachable from a retained recipient slot (``keep`` always stays).

        A grant is reachable when its ``from`` is a ``to`` slot of a retained entry
        with ``seq <= through_seq``, or the ``to`` of another kept grant (fixpoint).
        A grant is therefore kept only while its grantor is a retained recipient slot,
        or a successor chained from one. Sessions that are recipients of retained
        messages can still use up the global cap.
        """
        first = {}
        for entry in self.entries.values():
            for slot in entry.d["to"]:
                first[slot] = min(first.get(slot, entry.d["seq"]), entry.d["seq"])
        alive, targets, changed = set(), set(), True
        while changed:
            changed = False
            for key, grant in self.grants.items():
                if key in alive:
                    continue
                src = grant.d["from"]
                if grant is keep or first.get(src, grant.d["through_seq"] + 1) <= grant.d["through_seq"] \
                        or src in targets:
                    alive.add(key)
                    targets.add(grant.d["to"])
                    changed = True
        for key in list(self.grants):
            if key not in alive:
                self._drop_grant(key)

    def has_dead(self):
        return any(rec.dead for rec in self.recs)

    def live(self):
        return [rec for rec in self.recs if not rec.dead]

    def eligible(self, entry, gone):
        """Done (every slot ACKed or settled) and, if correlated, its mailbox is gone."""
        d = entry.d
        settled = entry.settled is not None
        if not settled and not all(slot in entry.acks for slot in d["to"]):
            return False
        return d["kind"] == "message" or gone(d["mailbox"])

    def prune_eligible(self, fits, helps, gone):
        """Drop the oldest eligible entries that help, until ``fits()``."""
        for entry in list(self.entries.values()):
            if fits():
                return
            if helps(entry) and self.eligible(entry, gone):
                self.drop_entry(entry)

    def meta(self, next_seq, malformed):
        live = self.live()
        return {
            "next_seq": next_seq,
            "total_bytes": self.total,
            "message_bytes": self.msg_bytes,
            "bytes_by_sender": {k: v for k, v in self.by_sender.items() if v},
            "oldest_at": min((r.d["at"] for r in live), default=None),
            "malformed_lines": malformed,
        }

    # -- authorization ------------------------------------------------------

    def reach(self, slot, seq):
        """Sessions that may act for ``slot`` on a message with ``seq``.

        Bounded: at most JOURNAL_MAX_LINKS (8) links deep and JOURNAL_MAX_EDGES (64)
        relevant edges (``through_seq >= seq``) examined; irrelevant edges are not
        counted. Beyond the bound the walk stops, so access fails closed.
        """
        if self._index is None:
            index = {}
            for g in self.grants.values():
                index.setdefault(g.d["from"], []).append((g.d["to"], g.d["through_seq"]))
            self._index = index
        seen, frontier, examined = {slot}, [slot], 0
        for _ in range(sp_constants.JOURNAL_MAX_LINKS):
            nxt = []
            for cur in frontier:
                for target, through in self._index.get(cur, ()):
                    if through < seq:
                        continue
                    examined += 1
                    if examined > sp_constants.JOURNAL_MAX_EDGES:
                        return seen
                    if target not in seen:
                        seen.add(target)
                        nxt.append(target)
            if not nxt:
                break
            frontier = nxt
        return seen

    def authorized(self, who, entry):
        """The slots of ``entry`` that ``who`` may read and ACK."""
        out = []
        for slot in entry.d["to"]:
            if slot == who or (self.grants and who in self.reach(slot, entry.d["seq"])):
                out.append(slot)
        return out


def _mailbox_gone(path):
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _gone_cache():
    cache = {}

    def gone(path):
        if path not in cache:
            cache[path] = _mailbox_gone(path)
        return cache[path]

    return gone


# --------------------------------------------------------------------------
# File access
# --------------------------------------------------------------------------


def _unavailable(detail):
    return JournalUnavailable(detail)


def _check_stat(st, name):
    if not stat.S_ISREG(st.st_mode):
        raise _unavailable("%s is not a regular file" % name)
    if st.st_uid != os.geteuid():
        raise _unavailable("%s is not owned by this user" % name)
    if st.st_nlink != 1:
        raise _unavailable("%s has %d hard links; it must have exactly one" % (name, st.st_nlink))
    return st


def _open_file(dfd, name, flags, writer):
    """Open ``name`` under ``dfd`` without following links and check it."""
    fd = os.open(name, flags | _NOFOLLOW | _NONBLOCK, 0o600, dir_fd=dfd)
    try:
        st = _check_stat(os.fstat(fd), name)
        mode = stat.S_IMODE(st.st_mode)
        if writer:
            if mode != 0o600:
                os.fchmod(fd, 0o600)
        elif mode & ~0o600:
            raise _unavailable("%s has mode %04o, wider than 0600; fix it with chmod 600" % (name, mode))
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _open_dir(path, writer):
    fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise _unavailable("%s is not a directory owned by this user" % path)
        mode = stat.S_IMODE(st.st_mode)
        if writer:
            if mode != 0o700:
                os.fchmod(fd, 0o700)
        elif mode & ~0o700:
            raise _unavailable("%s has mode %04o, wider than 0700; fix it with chmod 700" % (path, mode))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd(fd, size):
    if size > sp_constants.JOURNAL_READ_CEILING:
        raise _unavailable("journal is %d bytes, above the %d-byte read ceiling"
                           % (size, sp_constants.JOURNAL_READ_CEILING))
    chunks, left = [], size
    os.lseek(fd, 0, os.SEEK_SET)
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _write_all(fd, data, split=False):
    view = memoryview(data)
    if split and len(view) > 1:
        half = len(view) // 2
        _write_all(fd, bytes(view[:half]))
        _failpoint("mid_line")
        view = view[half:]
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _fsync_dir(dfd):
    try:
        os.fsync(dfd)
    except OSError:
        pass


def _tmp_name(name):
    return "%s.tmp.%d.%s" % (name, os.getpid(), uuid.uuid4().hex)


class _Writer:
    """The locked, repairing access path. One instance per operation."""

    def __init__(self):
        self.dfd = self.lfd = self.jfd = None

    def __enter__(self):
        try:
            path = journal_dir()
            os.makedirs(path, mode=0o700, exist_ok=True)
            self.dfd = _open_dir(path, True)
            self._lock()
            self._cleanup()
            self.jfd, _st = _open_file(self.dfd, JOURNAL_NAME, os.O_RDWR | os.O_CREAT, True)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for attr in ("jfd", "lfd", "dfd"):
            fd = getattr(self, attr)
            if fd is not None:
                setattr(self, attr, None)
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _lock(self):
        self.lfd, _st = _open_file(self.dfd, LOCK_NAME, os.O_RDWR | os.O_CREAT, True)
        deadline = time.monotonic() + sp_constants.JOURNAL_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise _unavailable("another peers.py command holds the session-peers journal lock")
                time.sleep(0.05)

    def _cleanup(self):
        """Remove this user's own regular leftover tmp files of interrupted rewrites."""
        for name in os.listdir(self.dfd):
            if not sp_constants.JOURNAL_TMP_RE.match(name):
                continue
            try:
                st = os.lstat(name, dir_fd=self.dfd)
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
                    os.unlink(name, dir_fd=self.dfd)
            except OSError:
                pass

    # -- journal ------------------------------------------------------------

    def size(self):
        return os.fstat(self.jfd).st_size

    def repair_tail(self):
        """Truncate a torn final line (no newline) back to the last complete one."""
        size = self.size()
        if size == 0:
            return
        os.lseek(self.jfd, size - 1, os.SEEK_SET)
        if os.read(self.jfd, 1) == b"\n":
            return
        data = _read_fd(self.jfd, size)
        os.ftruncate(self.jfd, data.rfind(b"\n") + 1)
        os.fsync(self.jfd)

    def read_meta(self):
        """The validated meta dict and whether it matches the journal file, or (None, False)."""
        try:
            fd, st = _open_file(self.dfd, META_NAME, os.O_RDONLY, True)
        except OSError:
            return None, False
        except JournalUnavailable:
            return None, False
        try:
            if st.st_size > sp_constants.JOURNAL_META_CEILING:
                return None, False
            meta = json.loads(_read_fd(fd, st.st_size).decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, RecursionError, OSError):
            return None, False
        finally:
            os.close(fd)
        try:
            valid = self._meta_valid(meta)
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            return None, False
        jst = os.fstat(self.jfd)
        return meta, meta["file_ino"] == jst.st_ino and meta["file_size"] == jst.st_size

    @staticmethod
    def _meta_valid(meta):
        if not isinstance(meta, dict):
            return False
        for key in ("next_seq", "file_ino", "file_size", "total_bytes", "message_bytes", "malformed_lines"):
            if not _is_int(meta.get(key)) or meta[key] < 0:
                return False
        if meta["next_seq"] < 1:
            return False
        senders = meta.get("bytes_by_sender")
        if not isinstance(senders, dict) or not all(isinstance(k, str) and _is_int(v) and v >= 0
                                                    for k, v in senders.items()):
            return False
        oldest = meta.get("oldest_at")
        return oldest is None or (_is_num(oldest) and math.isfinite(oldest))

    def load(self, now):
        """Repair the tail, parse the journal, and return (model, next_seq, malformed)."""
        self.repair_tail()
        meta, _trusted = self.read_meta()
        recs, malformed, last_seq = _parse(_read_fd(self.jfd, self.size()), now)
        next_seq = max(meta["next_seq"] if meta else 1, last_seq + 1)
        return _Model(recs), next_seq, malformed

    def trusted_meta(self):
        self.repair_tail()
        meta, trusted = self.read_meta()
        return meta if trusted else None

    # -- writing ------------------------------------------------------------

    def append(self, line, meta):
        size = self.size()
        try:
            os.lseek(self.jfd, 0, os.SEEK_END)
            _write_all(self.jfd, line + b"\n", split=True)
            os.fsync(self.jfd)
        except OSError:
            try:
                os.ftruncate(self.jfd, size)
            except OSError:
                pass
            raise
        _failpoint("after_append_before_meta")
        self.write_meta(meta)

    def rewrite(self, lines, meta):
        """Atomically replace the journal with ``lines``, then rewrite the meta."""
        tmp = _tmp_name(JOURNAL_NAME)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=self.dfd)
        try:
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, b"".join(line + b"\n" for line in lines))
                os.fsync(fd)
            finally:
                os.close(fd)
            _failpoint("before_prune_rename")
            os.replace(tmp, JOURNAL_NAME, src_dir_fd=self.dfd, dst_dir_fd=self.dfd)
        except OSError:
            try:
                os.unlink(tmp, dir_fd=self.dfd)
            except OSError:
                pass
            raise
        _failpoint("after_prune_rename")
        _fsync_dir(self.dfd)
        self.write_meta(meta)

    def write_meta(self, meta):
        st = os.stat(JOURNAL_NAME, dir_fd=self.dfd, follow_symlinks=False)
        meta = dict(meta, file_ino=st.st_ino, file_size=st.st_size)
        tmp = _tmp_name(META_NAME)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=self.dfd)
        try:
            try:
                os.fchmod(fd, 0o600)
                _write_all(fd, json.dumps(meta, sort_keys=True).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            _failpoint("meta_tmp_written")
            os.replace(tmp, META_NAME, src_dir_fd=self.dfd, dst_dir_fd=self.dfd)
        except OSError:
            try:
                os.unlink(tmp, dir_fd=self.dfd)
            except OSError:
                pass
            raise
        _fsync_dir(self.dfd)


def _commit(w, m, new, next_seq, malformed):
    """Persist ``m`` whose last record is ``new``: an append, or one atomic rewrite."""
    if m.has_dead():
        w.rewrite([rec.line for rec in m.live()], m.meta(next_seq + 1, 0))
    else:
        w.append(new.line, m.meta(next_seq + 1, malformed))


def _api(fn):
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            raise JournalUnavailable(str(exc)) from exc

    return inner


def _post_send(fn):
    """A post-transport update: any failure surfaces as JournalUpdateFailed."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (JournalError, ValueError) as exc:
            raise JournalUpdateFailed(str(exc)) from exc
        except OSError as exc:
            raise JournalUpdateFailed("session-peers journal update failed: %s" % exc) from exc

    return inner


# --------------------------------------------------------------------------
# Writing messages
# --------------------------------------------------------------------------


def _message_record(sender, to, body, kind, message_id, extra, now):
    try:
        to_list = []
        for slot in to:
            slot = _norm(slot)
            if slot not in to_list:
                to_list.append(slot)
        rec = {
            "schema": 1, "seq": 1, "type": "message", "at": now, "message_id": message_id,
            "kind": kind, "from": _norm(sender) if sender else None, "to": to_list, "body": body,
        }
        rec.update(extra)
        _validate(rec, now)
    except (ValueError, TypeError) as exc:
        raise JournalRefused("cannot record this message: %s" % exc)
    return rec


def _fast_fits(meta, rec, size, cap, now):
    if meta["total_bytes"] + size > cap:
        return False
    if rec["kind"] == "message" and meta["message_bytes"] + size > cap // sp_constants.JOURNAL_MESSAGE_QUOTA_DIVISOR:
        return False
    if meta["bytes_by_sender"].get(_bucket(rec), 0) + size > cap // sp_constants.JOURNAL_SENDER_QUOTA_DIVISOR:
        return False
    oldest = meta["oldest_at"]
    return oldest is None or oldest >= _age_cutoff(now)


def _refuse_taken_id(m, message_id):
    """Refuse an id of a retained entry, or the reply id of a retained request."""
    if message_id in m.entries:
        raise JournalRefused("message id %s is already recorded" % message_id)
    if reply_message_id(message_id) in m.entries:
        raise JournalRefused("the reply id of %s is already recorded" % message_id)
    if uuid.UUID(message_id).version == 5:
        for entry in m.entries.values():
            if entry.d["kind"] == "request" and reply_message_id(entry.d["request_id"]) == message_id:
                raise JournalRefused("message id %s is reserved for the reply to request %s"
                                     % (message_id, entry.d["request_id"]))


def _same_reply(old, rec):
    keys = ("kind", "request_id", "from", "to", "body")
    return all(old.d.get(k) == rec[k] for k in keys)


def _add_message(rec, revalidate=None, dedupe=False, check_id=True):
    """Record ``rec`` (seq assigned here). "recorded", or "same" for an equal replay.

    The fast path cannot see ids, so ``check_id`` (a caller-supplied id) takes the
    parsing path and refuses an id that is already taken. Only a generated
    uuid4 id may skip it.
    """
    with _Writer() as w:
        if revalidate is not None:
            revalidate()
        now = _now()
        cap = _cap()
        rec["at"] = now
        meta = None if dedupe or check_id else w.trusted_meta()
        if meta is not None:
            rec["seq"] = meta["next_seq"]
            size = len(_encode(rec)) + 1
            if _fast_fits(meta, rec, size, cap, now):
                new_meta = dict(meta)
                new_meta["next_seq"] += 1
                new_meta["total_bytes"] += size
                if rec["kind"] == "message":
                    new_meta["message_bytes"] += size
                senders = dict(meta["bytes_by_sender"])
                senders[_bucket(rec)] = senders.get(_bucket(rec), 0) + size
                new_meta["bytes_by_sender"] = senders
                new_meta["oldest_at"] = now if meta["oldest_at"] is None else min(meta["oldest_at"], now)
                w.append(_encode(rec), new_meta)
                return "recorded"
        m, next_seq, malformed = w.load(now)
        rec["seq"] = next_seq
        if dedupe:
            old = m.entries.get(rec["message_id"])
            if old is not None:
                if old.d["kind"] != "reply":
                    raise JournalRefused("reply id is taken by another record")
                if not _same_reply(old, rec):
                    raise JournalRefused("request already has a recorded reply")
                return "same"
        m.age_prune(now)
        if check_id and not dedupe:
            _refuse_taken_id(m, rec["message_id"])
        line = _encode(rec)
        size = len(line) + 1
        bucket = _bucket(rec)
        quota_msg = cap // sp_constants.JOURNAL_MESSAGE_QUOTA_DIVISOR
        quota_sender = cap // sp_constants.JOURNAL_SENDER_QUOTA_DIVISOR

        def violations():
            return (
                m.total + size > cap,
                rec["kind"] == "message" and m.msg_bytes + size > quota_msg,
                m.by_sender.get(bucket, 0) + size > quota_sender,
            )

        def helps(entry):
            total_v, msg_v, sender_v = violations()
            return (total_v or (msg_v and entry.d["kind"] == "message")
                    or (sender_v and _bucket(entry.d) == bucket))

        m.prune_eligible(lambda: not any(violations()), helps, _gone_cache())
        if any(violations()):
            raise JournalFull(
                "session-peers journal full: no finished entry can be pruned to fit %d more bytes; "
                "ACK or settle older messages, or retry after they expire" % size
            )
        new = _Rec(rec, line)
        m.add(new)
        m.prune_grants()
        _commit(w, m, new, next_seq, malformed)
        return "recorded"


def _resolve_recipients(to):
    to = list(to or [])
    if not 1 <= len(to) <= sp_constants.JOURNAL_MAX_RECIPIENTS:
        raise JournalRefused("a message needs 1 to %d recipients" % sp_constants.JOURNAL_MAX_RECIPIENTS)
    seen = []
    for slot in to:
        try:
            slot = _norm(slot)
        except ValueError as exc:
            raise JournalRefused("recipient is not a typed identity: %s" % exc)
        if slot not in seen:
            seen.append(slot)
            try:
                sp_identity.resolve_typed(slot)
            except sp_codex.ResolveError as exc:
                raise JournalRefused("recipient %s is not an existing session: %s" % (slot, exc))
    return seen


@_api
def record_message(sender, to, body, message_id=None):
    """Record a plain message; return its message id.

    ``sender`` is a typed id or None. Every recipient must be an existing
    session (resolved here, before the journal lock is taken). A caller-supplied
    ``message_id`` that is already recorded, or is the reply id of a retained
    request, raises JournalRefused. Raises JournalRefused, JournalFull or
    JournalUnavailable.
    """
    to = _resolve_recipients(to)
    supplied = bool(message_id)
    message_id = message_id or str(uuid.uuid4())
    rec = _message_record(sender, to, body, "message", message_id, {}, _now())
    _add_message(rec, check_id=supplied)
    return message_id


@_api
def record_request(request_id, sender, to, body, expires_at, mailbox, fresh_id=False):
    """Record a correlated request (``message_id`` is ``request_id``).

    The recipients come from the request's mailbox metadata, so they are not
    resolved again. ``mailbox`` is the absolute path of the request meta file.
    A ``request_id`` that is already recorded, or whose reply id is already
    recorded (a pre-squatted reply), raises JournalRefused. That check parses the
    journal. ``fresh_id=True`` is the caller's guarantee that ``request_id`` is a
    freshly generated uuid4 never exposed to anyone: the check is skipped and the
    fast append path may be used. The opt-out is enforced only in that
    ``request_id`` must be a version-4 uuid, else JournalRefused; a duplicate
    uuid4 on the fast path is not detected (the collision chance is negligible),
    so the caller must pass a freshly generated uuid4.
    """
    if fresh_id:
        try:
            fresh = uuid.UUID(str(request_id)).version == 4
        except ValueError:
            fresh = False
        if not fresh:
            raise JournalRefused("fresh_id requires a freshly generated uuid4 request id")
    extra = {"request_id": request_id, "expires_at": expires_at, "mailbox": mailbox}
    rec = _message_record(sender, to, body, "request", request_id, extra, _now())
    _add_message(rec, check_id=not fresh_id)


@_api
def record_reply(request_id, sender, to, body, expires_at, mailbox, revalidate=None):
    """Record the one reply to ``request_id``; return "recorded" or "same".

    Under the journal lock, ``revalidate()`` runs first (the caller passes a
    function that re-reads the request meta and raises to refuse). A different
    reply (or another record under the reply id) already recorded raises
    JournalRefused; an identical one (kind, request, sender, recipients and
    body) is idempotent.
    Every failure here happens before the reply is sent.
    """
    extra = {"request_id": request_id, "in_reply_to": request_id,
             "expires_at": expires_at, "mailbox": mailbox}
    rec = _message_record(sender, to, body, "reply", reply_message_id(request_id), extra, _now())
    return _add_message(rec, revalidate=revalidate, dedupe=True)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def _write_event(make):
    """Write one event and the pruning it enables in a single atomic step.

    ``make(model, now, seq)`` returns ``(fields, existing)``: ``fields`` is a
    new record's body (None for an idempotent no-op returning ``existing``).
    """
    with _Writer() as w:
        now = _now()
        cap = _cap()
        m, next_seq, malformed = w.load(now)
        fields, existing = make(m, now, next_seq)
        if fields is None:
            return existing
        rec = dict(fields, schema=1, seq=next_seq, at=now)
        try:
            _validate(rec, now)
        except ValueError as exc:
            raise JournalRefused("invalid journal event: %s" % exc)
        new = _Rec(rec, _encode(rec))
        m.add(new)
        m.age_prune(now)
        m.prune_grants(keep=new if rec["type"] == "handover" else None)
        if m.total > cap:
            m.prune_eligible(lambda: m.total <= cap, lambda _entry: True, _gone_cache())
        if m.total > cap + sp_constants.JOURNAL_EVENT_RESERVE:
            raise JournalFull(
                "session-peers journal full: even the event reserve is exhausted by entries that "
                "cannot be pruned yet"
            )
        _commit(w, m, new, next_seq, malformed)
        return rec


@_api
@_post_send
def settle(message_id, state, by):
    """Record that a correlated entry finished: a request "replied" or a reply "consumed".

    ``by`` must be one of the entry's recipient slots (the replier of a request,
    the requester of a reply) or a session covering one through handovers.
    Returns None, creating and repairing nothing, when ``message_id`` is not in the
    journal (pruned, or a legacy mailbox that never had an entry). An invalid
    ``message_id`` or ``state`` raises JournalUpdateFailed. Raises
    JournalUpdateFailed on any other failure, including an unauthorized ``by``;
    the caller must surface it. Idempotent.
    """
    if not _uuid_ok(message_id):
        raise JournalRefused("message id %r is not a lowercase UUID" % (message_id,))
    if state not in _STATES:
        raise JournalRefused("state must be one of %s, got %r" % (", ".join(_STATES), state))
    by = _norm(by)
    try:
        m, _malformed, _last = _reader_model(_now())
    except JournalUnavailable:
        pass  # let the writer path repair modes or report the real problem
    else:
        if message_id not in m.entries:
            return None  # absent: create and repair nothing

    def make(m, now, seq):
        entry = m.entries.get(message_id)
        if entry is None:
            return None, None
        if _STATE_FOR_KIND.get(entry.d["kind"]) != state:
            raise JournalRefused("a %s entry cannot be settled as %r" % (entry.d["kind"], state))
        if not m.authorized(by, entry):
            raise JournalRefused("%s may not settle %s: not a recipient of this %s"
                                 % (by, message_id, entry.d["kind"]))
        if entry.settled is not None:
            return None, entry.settled.d
        return {"type": "settled", "message_id": message_id, "state": state, "by": by}, None

    return _write_event(make)


@_api
@_post_send
def transport_error(message_id, slot, detail):
    """Record an informational delivery failure for ``slot`` (truncated to 512 characters).

    Raises JournalUpdateFailed on any failure.
    """
    slot = _norm(slot)
    detail = str(detail)[: sp_constants.JOURNAL_DETAIL_CHARS]

    def make(m, now, seq):
        entry = m.entries.get(message_id)
        if entry is None or slot not in entry.d["to"]:
            raise JournalRefused("message %s with recipient %s is not in the journal" % (message_id, slot))
        return {"type": "transport_error", "message_id": message_id, "for": slot, "detail": detail}, None

    return _write_event(make)


@_api
def ack(message_id, caller, for_slot=None):
    """Record that ``caller`` took the message for one recipient slot; return the ACK record.

    The slot defaults to the caller's own, else the single slot the caller covers
    through handovers. Not a recipient or covering several slots without
    ``for_slot`` raises JournalRefused. Idempotent: an existing ACK is returned.
    """
    who = _norm_caller(caller)
    wanted = None
    if for_slot is not None:
        try:
            wanted = _norm(for_slot)
        except ValueError as exc:
            raise JournalRefused("--for is not a typed identity: %s" % exc)

    def make(m, now, seq):
        entry = m.entries.get(message_id)
        if entry is None:
            raise JournalRefused("message %s is not in the journal" % message_id)
        slots = m.authorized(who, entry)
        if wanted is not None:
            if wanted not in slots:
                raise JournalRefused("%s may not acknowledge %s's slot of this message" % (who, wanted))
            slot = wanted
        elif who in slots:
            slot = who
        elif len(slots) == 1:
            slot = slots[0]
        elif not slots:
            raise JournalRefused("%s is not a recipient of this message" % who)
        else:
            raise JournalRefused("%s covers several recipient slots (%s); pass --for" % (who, ", ".join(slots)))
        if slot in entry.acks:
            return None, entry.acks[slot].d
        return {"type": "ack", "message_id": message_id, "for": slot, "by": who}, None

    return _write_event(make)


@_api
def handover(caller, target_typed):
    """Let ``target_typed`` (a full typed id of an existing session) act for ``caller``.

    Covers messages with ``seq`` up to the last one assigned now. Names are
    refused. At most 8 retained grants per grantor; re-granting the same pair
    replaces it. Returns the handover record.
    """
    who = _norm_caller(caller)
    try:
        target = _norm(target_typed)
    except ValueError:
        raise JournalRefused("a handover names a full typed identity, not a name "
                             "(cc:<uuid>, codex:<uuid> or oc:<ses_id>)")
    if target == who:
        raise JournalRefused("a session cannot hand over to itself")
    try:
        sp_identity.resolve_typed(target)
    except sp_codex.ResolveError as exc:
        raise JournalRefused("handover successor %s is not an existing session: %s" % (target, exc))

    def make(m, now, seq):
        m.age_prune(now)
        m.prune_grants()
        mine = [g for g in m.grants.values() if g.d["from"] == who and g.d["to"] != target]
        if len(mine) >= sp_constants.JOURNAL_MAX_GRANTS:
            raise JournalRefused("%s already has %d handovers; the journal keeps at most %d per session"
                                 % (who, len(mine), sp_constants.JOURNAL_MAX_GRANTS))
        if (who, target) not in m.grants and len(m.grants) >= sp_constants.JOURNAL_MAX_GRANTS_TOTAL:
            raise JournalRefused("the journal already keeps %d handovers, its limit; retry after older "
                                 "messages expire or are pruned" % len(m.grants))
        return {"type": "handover", "from": who, "to": target, "through_seq": seq - 1}, None

    return _write_event(make)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _reader_model(now):
    """Model of the journal on the no-repair, no-lock path; (model, malformed, last_seq)."""
    try:
        dfd = _open_dir(journal_dir(), False)
    except FileNotFoundError:
        return _Model([]), 0, 0
    try:
        try:
            fd, st = _open_file(dfd, JOURNAL_NAME, os.O_RDONLY, False)
        except FileNotFoundError:
            return _Model([]), 0, 0
        try:
            data = _read_fd(fd, st.st_size)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    recs, malformed, last_seq = _parse(data, now)
    return _Model(recs), malformed, last_seq


def _base_view(entry, now):
    d = entry.d
    out = {
        "message_id": d["message_id"], "seq": d["seq"], "kind": d["kind"], "from": d["from"],
        "to": list(d["to"]), "at": d["at"], "expired": d["at"] < _age_cutoff(now),
    }
    for key in ("request_id", "in_reply_to", "expires_at", "mailbox"):
        if key in d:
            out[key] = d[key]
    return out


def _recipients(entry, slots=None):
    out = []
    for slot in entry.d["to"] if slots is None else slots:
        rec = entry.acks.get(slot)
        item = {"slot": slot, "state": "acked" if rec else "pending"}
        if rec:
            item["by"], item["at"] = rec.d["by"], rec.d["at"]
        out.append(item)
    return out


def _settlement(entry):
    s = entry.settled
    return {"state": s.d["state"], "by": s.d["by"], "at": s.d["at"]} if s else None


def _transport(entry):
    t = entry.transport
    return {"for": t.d["for"], "detail": t.d["detail"], "at": t.d["at"]} if t else None


@_api
def read_only_view():
    """Every retained entry in seq order, with body, ACKs, settlement, transport and expiry.

    Same-OS-user trust exception, not a session: no identity is checked and the
    recipient list is an addressing rule, not confidentiality. Never repairs,
    prunes, locks or creates anything. Returns ``entries``, ``handovers``,
    ``malformed_lines`` and ``last_seq``.
    """
    now = _now()
    m, malformed, last_seq = _reader_model(now)
    entries = []
    for entry in m.entries.values():
        item = _base_view(entry, now)
        item["body"] = entry.d["body"]
        item["recipients"] = _recipients(entry)
        item["settlement"] = _settlement(entry)
        item["transport_error"] = _transport(entry)
        entries.append(item)
    grants = [dict(g.d) for g in m.grants.values()]
    return {"entries": entries, "handovers": grants, "malformed_lines": malformed, "last_seq": last_seq}


@_api
def get(message_id, caller):
    """One entry with its body for a recipient (or covered successor) of it.

    Returns the metadata plus ``body`` and separate ``acks``, ``settlement``,
    ``transport_error`` and ``expired``. The sender and other sessions are refused.
    """
    who = _norm_caller(caller)
    now = _now()
    m, _malformed, _last = _reader_model(now)
    entry = m.entries.get(message_id)
    slots = m.authorized(who, entry) if entry else []
    if not slots:
        raise JournalRefused("%s may not read message %s" % (who, message_id))
    out = _base_view(entry, now)
    out["body"] = entry.d["body"]
    out["slots"] = slots
    out["acks"] = [{"for": s, "by": r.d["by"], "at": r.d["at"]} for s, r in entry.acks.items()]
    out["settlement"] = _settlement(entry)
    out["transport_error"] = _transport(entry)
    return out


@_api
def list_for(caller, include_acked=False):
    """Metadata (no bodies) of the entries ``caller`` may read, in seq order.

    Entries whose caller-covered slots are all ACKed are hidden unless
    ``include_acked``.
    """
    who = _norm_caller(caller)
    now = _now()
    m, _malformed, _last = _reader_model(now)
    out = []
    for entry in m.entries.values():
        slots = m.authorized(who, entry)
        if not slots:
            continue
        recipients = _recipients(entry, slots)
        if not include_acked and all(r["state"] == "acked" for r in recipients):
            continue
        item = _base_view(entry, now)
        item["slots"] = recipients
        out.append(item)
    return out


@_api
def acks_view(message_id, caller):
    """Per-recipient ACK state, settlement and transport error, without the body.

    Allowed for the sender, any recipient and covered successors.
    """
    who = _norm_caller(caller)
    now = _now()
    m, _malformed, _last = _reader_model(now)
    entry = m.entries.get(message_id)
    if entry is None or not (entry.d["from"] == who or m.authorized(who, entry)):
        raise JournalRefused("%s may not see the acknowledgements of message %s" % (who, message_id))
    out = _base_view(entry, now)
    out["recipients"] = _recipients(entry)
    out["settlement"] = _settlement(entry)
    out["transport_error"] = _transport(entry)
    return out
