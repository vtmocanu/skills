"""Channels for the session-peers CLI: named member lists with a shared history.

Everything lives under one user-wide root derived from HOME only (like the
journal), so sessions running under different CODEX_HOME values see the same
channels:

    <root>/<key>.json     membership record, written with an atomic replace
    <root>/<key>.lock     per-channel flock
    <root>/.channels.lock store lock, taken by create
    <root>/history/       topic ``channel:<key>`` (see topics, ``base=``)

Every membership change writes the record first and appends history second,
so history never describes a change that did not happen.
"""

from __future__ import annotations
import argparse
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import time
import types
import uuid as uuidlib
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, identity as sp_identity
from . import journal as sp_journal, opencode as sp_opencode, topics as sp_topics

EVENT_CREATED = "channel.created"
EVENT_JOINED = "channel.joined"
EVENT_LEFT = "channel.left"
EVENT_REMOVED = "channel.removed"
EVENT_HANDOVER = "channel.handover"
EVENT_SEND = "channel.send"
EVENT_OUTCOME = "channel.outcome"

# The shared delivery vocabulary (PRD #160, "Delivery outcomes").
OUTCOME_RECORDED = "recorded"
OUTCOME_ACCEPTED = "transport accepted"
OUTCOME_PAUSED = "queued (thread paused)"
OUTCOME_NOT_WOKEN = "not woken (OpenCode idle)"
OUTCOME_GONE = "skipped: gone"
OUTCOME_REFUSED = "refused: "
STATE_IN_PROGRESS = "in progress"
STATE_UNKNOWN = "unknown"
STATE_UNVERIFIED = "unverified"
STATE_ACKED = "acked"
ACK_NOT_REQUESTED = "ACK not requested (unverified)"
ACK_NOT_REQUESTED_ASKED = "ACK not requested"

# Deliveries to Claude targets are at least this far apart (seconds). Tests
# patch ``_clock`` and ``_sleep``; nothing else sleeps.
PACE_SECONDS = 1.0
_clock = time.monotonic
_sleep = time.sleep

# `@all` (PRD #160): at most this many recipients per post, and one per sender
# per ALL_INTERVAL seconds across every channel. Tests patch ``_wall``.
ALL_RE = re.compile(r"^@all(\s|$)")
ALL_MAX_RECIPIENTS = 64
ALL_INTERVAL = 60.0
_wall = time.time

# A handover receipt lets `peers.py handover` proceed after the journal pruned its grant.
RECEIPT_MAX_AGE = 14 * 86400.0

# A leading dot keeps the store lock out of the key namespace: no channel
# name can produce it, so ``<key>.lock`` never aliases it.
STORE_LOCK = ".channels.lock"

# A membership record larger than this is refused unread (a real one is a few KiB).
RECORD_MAX_BYTES = 256 * 1024


class ChannelError(Exception):
    """A refused channel operation; carries the exit code."""

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def _failpoint(name):
    """No-op crash seam. Tests patch it to fail or exit at ``name``.

    Names: after_membership_before_history, send_after_journal, send_after_lock,
    send_after_history, send_before_outcome, allcast_after_reserve.
    """


# --------------------------------------------------------------------------
# Paths and locks
# --------------------------------------------------------------------------


def channels_root():
    """The channel store, derived from HOME only (never CODEX_HOME)."""
    return os.path.join(os.path.expanduser("~/.codex"), "session-peers", "channels")


def history_dir():
    return os.path.join(channels_root(), "history")


def sends_dir():
    """Per-send lock files, one ``<message id>.lock`` per fan-out in flight."""
    return os.path.join(channels_root(), "sends")


def send_lock_path(message_id):
    """The lock file of one send; ``message_id`` must be a UUID (it names a path)."""
    if not isinstance(message_id, str) or not sp_constants.UUID_RE.fullmatch(message_id):
        raise ValueError("a send lock is named by a message UUID, got %r" % (message_id,))
    return os.path.join(sends_dir(), message_id + ".lock")


def _ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _record_path(key):
    return os.path.join(channels_root(), key + ".json")


def _lock_path(key):
    return os.path.join(channels_root(), key + ".lock")


@contextlib.contextmanager
def _flock(path):
    _ensure_dir(channels_root())
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing drops the flock


def topic_name(key):
    return sp_constants.CHANNEL_TOPIC_PREFIX + key


# --------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------


def parse_name(value):
    """``NAME`` or ``#NAME`` as the bare channel name, validated."""
    name = str(value or "")
    if name.startswith("#"):
        name = name[1:]
    if not sp_constants.CHANNEL_NAME_RE.fullmatch(name):
        raise ChannelError(
            "a channel name is 1-64 characters of lowercase letters, digits and '-', got %r" % value
        )
    return name


def stored_key(value):
    """The key of a stored channel named by ``value``."""
    name = parse_name(value)
    if name == sp_constants.CHANNEL_GENERAL:
        raise ChannelError("#general is derived from live sessions and has no stored membership")
    return name


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def read_channel(key):
    """The membership record of ``key``, or None.

    The file is peer-writable, so it is not trusted: one over ``RECORD_MAX_BYTES``
    or with a non-string key, display, kind or owner reads as absent. Members are
    canonicalised, and a non-string, invalid or repeated one is dropped. A roster
    still over ``CHANNEL_MAX_MEMBERS`` is returned whole; ``_members_for`` refuses
    a send to it.
    """
    path = _record_path(key)
    try:
        if os.stat(path).st_size > RECORD_MAX_BYTES:
            return None
    except OSError:
        return None
    rec = sp_runtime.read_json(path, None)
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
    rec = dict(rec, members=members)
    if not isinstance(rec.get("handed_over_at"), str):
        rec.pop("handed_over_at", None)  # optional; a non-string reads as never handed over
    return rec


def _keys_strict():
    """The stored keys; ``[]`` when the store does not exist yet, OSError when it cannot be listed."""
    try:
        names = sorted(os.listdir(channels_root()))
    except FileNotFoundError:
        return []
    keys = []
    for name in names:
        if name.endswith(".json") and sp_constants.CHANNEL_NAME_RE.fullmatch(name[:-5]):
            keys.append(name[:-5])
    return keys


def list_keys():
    try:
        return _keys_strict()
    except OSError:
        return []


def _lifecycle_records(verb):
    """``([(key, record_or_None)], ok)`` for leave and handover.

    A key whose file exists but does not read (invalid, oversized or with a
    non-string field) pairs with None and is reported, since the caller's
    membership in it cannot be told. A store that cannot be listed gives
    ``([], False)``.
    """
    ok = True
    try:
        keys = _keys_strict()
    except OSError as exc:
        _err("could not %s: the channel store cannot be listed: %s" % (verb, _reason(exc)))
        return [], False
    pairs = []
    for key in keys:
        rec = read_channel(key)
        if rec is None:
            _err("could not read #%s (its record is unreadable or invalid); it was skipped" % key)
            ok = False
        pairs.append((key, rec))
    return pairs, ok


def list_channels():
    return [rec for rec in (read_channel(key) for key in list_keys()) if rec]


def _write_record(rec):
    sp_runtime.write_json_atomic(_record_path(rec["key"]), rec, mode=0o600)


def _clean_tmp(prefix=""):
    """Remove stale ``<key>.json.tmp.*`` files left by a crashed writer. Call under a lock."""
    root = channels_root()
    cutoff = time.time() - sp_constants.CHANNEL_TMP_MAX_AGE
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix) or ".json.tmp." not in name:
            continue
        path = os.path.join(root, name)
        try:
            if os.stat(path).st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            pass


def _check_member_cap(count):
    if count > sp_constants.CHANNEL_MAX_MEMBERS:
        raise ChannelError(
            "a channel holds at most %d members (limit CHANNEL_MAX_MEMBERS)"
            % sp_constants.CHANNEL_MAX_MEMBERS
        )


def _check_roster_cap(rec):
    """Refuse a stored roster over the cap (a tampered record), never truncate it."""
    if len(rec["members"]) > sp_constants.CHANNEL_MAX_MEMBERS:
        raise ChannelError(
            "the record of #%s lists %d members, over the CHANNEL_MAX_MEMBERS cap of %d"
            % (rec["key"], len(rec["members"]), sp_constants.CHANNEL_MAX_MEMBERS)
        )


def _actor_identity(typed):
    parsed = sp_identity.parse_address(typed)
    return {"kind": parsed["kind"], "uuid": parsed["uuid"]}


def append_history(key, kind, actor, data=None):
    """Append one history entry to ``key``'s topic and return it.

    ``actor`` is a typed id. Raises ChannelError(code 1) when the append fails.
    """
    try:
        _ensure_dir(history_dir())
        return sp_topics.topic_post(
            topic_name(key), _actor_identity(actor) if actor else {}, kind=kind,
            data=data if data is not None else {}, base=history_dir(),
        )
    except (OSError, sp_topics.TopicError) as exc:
        raise ChannelError("could not append to the history of #%s: %s" % (key, exc), 1)


def read_history(key, since=None, limit=sp_constants.TOPIC_TAIL_DEFAULT):
    """``{"topic", "entries", "next", "gap"}``; ``gap`` and ``next`` pass through unchanged."""
    return sp_topics.topic_tail(topic_name(key), since=since, limit=limit, base=history_dir())


def create_channel(name, owner, members=(), kind="named", display=None, reserved_ok=False):
    """Create a channel owned by ``owner`` (a typed id); the owner is a member.

    Membership is written before the ``channel.created`` entry. ``reserved_ok``
    lets the buddy flow create its own ``buddy-`` channels.
    """
    key = parse_name(name)
    if not reserved_ok and (
        key == sp_constants.CHANNEL_GENERAL or key.startswith(sp_constants.CHANNEL_BUDDY_PREFIX)
    ):
        raise ChannelError(
            "the name %r is reserved (#%s is derived, %s* channels belong to buddy set)"
            % (key, sp_constants.CHANNEL_GENERAL, sp_constants.CHANNEL_BUDDY_PREFIX)
        )
    roster = [owner]
    for member in members:
        if member not in roster:
            roster.append(member)
    _check_member_cap(len(roster))
    with _flock(os.path.join(channels_root(), STORE_LOCK)):
        _clean_tmp()
        if read_channel(key) is not None:
            raise ChannelError("channel #%s already exists" % key)
        if os.path.exists(_record_path(key)):
            raise ChannelError("channel #%s exists but its record is unreadable" % key)
        if len(list_keys()) >= sp_constants.CHANNEL_MAX_CHANNELS:
            raise ChannelError(
                "at most %d channels can exist (limit CHANNEL_MAX_CHANNELS)"
                % sp_constants.CHANNEL_MAX_CHANNELS
            )
        rec = {
            "key": key, "display": display or key, "kind": kind, "owner": owner,
            "members": roster, "created_at": sp_runtime.now_iso(),
        }
        with _flock(_lock_path(key)):
            _write_record(rec)
            _failpoint("after_membership_before_history")
            append_history(key, EVENT_CREATED, owner, {"members": list(roster)})
    return rec


def change_membership(key, mutate, actor):
    """Run ``mutate(record)`` under the channel lock and persist the result.

    ``mutate`` edits the record in place and returns ``(event_kind, data)``, or
    None for a no-op. The record is replaced FIRST; the history entry follows.
    Returns ``(record, entry_or_None)``.
    """
    with _flock(_lock_path(key)):
        rec = read_channel(key)
        if rec is None:
            raise ChannelError("no channel named #%s; run `peers.py channel list`" % key)
        _clean_tmp(key + ".json.tmp.")
        new = copy.deepcopy(rec)
        change = mutate(new)
        if change is None:
            return rec, None
        _write_record(new)
        _failpoint("after_membership_before_history")
        event, data = change
        return new, append_history(key, event, actor, data)


def prune_all():
    """Apply retention to every channel history and drop stale temp files (gc)."""
    if not os.path.isdir(channels_root()):
        return
    with _flock(os.path.join(channels_root(), STORE_LOCK)):
        _clean_tmp()
    # #general has no stored record but does have derived history.
    for key in list_keys() + [sp_constants.CHANNEL_GENERAL]:
        try:
            sp_topics.topic_prune(topic_name(key), history_dir())
        except (OSError, sp_topics.TopicError) as exc:
            sp_runtime.log("could not prune the history of #%s: %s" % (key, exc))
    _prune_send_locks()


def _prune_send_locks():
    """Remove send locks nobody holds: a finished or crashed send leaves one behind.

    A lock is taken with LOCK_NB, so a send still in progress keeps its file.
    """
    try:
        names = os.listdir(sends_dir())
    except OSError:
        return
    for name in names:
        if not name.endswith(".lock") or not sp_constants.UUID_RE.fullmatch(name[:-5]):
            continue
        path = os.path.join(sends_dir(), name)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                sp_runtime.log("could not remove the stale send lock %s: %s" % (name, exc))
        finally:
            os.close(fd)


# --------------------------------------------------------------------------
# Member liveness
# --------------------------------------------------------------------------


def member_liveness(typed, cache=None):
    """``live``, ``gone`` or ``unverified`` for a typed member.

    ``gone`` needs positive proof. ``cache`` (a dict the caller keeps for one
    operation) shares the registry read and the OpenCode walk across members.
    """
    cache = {} if cache is None else cache
    kind, _sep, ident = typed.partition(":")
    if kind == "cc":
        return _claude_liveness(ident, cache)
    if kind == "codex":
        return _codex_liveness(ident)
    if kind == "oc":
        return _opencode_liveness(ident, cache)
    return "unverified"


def _claude_liveness(sid, cache):
    if "cc" not in cache:
        cache["cc"] = sp_claude.read_claude_records_checked()
    records, error = cache["cc"]
    if error is not None:
        return "unverified"
    # A browser peer's record is not a Claude session, whatever its sessionId.
    matches = [
        r for r in records
        if not sp_claude.is_web_record(r) and str(r.get("sessionId")).lower() == sid.lower()
    ]
    if not matches:
        return "gone"
    states = {sp_claude.record_liveness(r) for r in matches}
    if "live" in states:
        return "live"
    return "gone" if states == {"dead"} else "unverified"


def _codex_liveness(tid):
    try:
        thread = sp_codex.resolve_thread(tid, require_live=False)
    except sp_codex.ResolveError:
        return "unverified"
    if thread.get("degraded"):
        return "unverified"
    lock = sp_codex.writer_lock_path(tid, home=thread.get("codex_home"))
    held, _pid = sp_codex.thread_is_held(thread.get("rollout_path"), lock_path=lock)
    if held is None:
        return "unverified"
    return "live" if held else "gone"


def _opencode_liveness(sid, cache):
    if "oc" not in cache:
        try:
            cache["oc"] = {s["id"] for s in sp_opencode.session_walk()}
        except sp_opencode.OpenCodeError:
            cache["oc"] = None
    walked = cache["oc"]
    if walked is None:
        return "unverified"
    if sid in walked:
        return "live"
    # Absent from the top-level walk: a child session or an unreachable one
    # still exists, so only NotFound proves it is gone.
    try:
        sp_opencode.get_session(sid)
    except sp_opencode.OpenCodeNotFound:
        return "gone"
    except sp_opencode.OpenCodeError:
        return "unverified"
    return "unverified"


# --------------------------------------------------------------------------
# Derived #general, leave markers, handover receipts, the @all allowance
# --------------------------------------------------------------------------


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def left_dir():
    """Leave markers, one ``<sha256(typed id)>.json`` per session that left #general."""
    return os.path.join(channels_root(), "left")


def _marker_path(typed):
    return os.path.join(left_dir(), _digest(typed) + ".json")


def set_left(typed):
    """Record that ``typed`` left derived #general (idempotent)."""
    _ensure_dir(channels_root())
    _ensure_dir(left_dir())
    sp_runtime.write_json_atomic(
        _marker_path(typed), {"member": typed, "at": sp_runtime.now_iso()}, mode=0o600)


def clear_left(typed):
    """Remove ``typed``'s leave marker; True when there was one."""
    try:
        os.unlink(_marker_path(typed))
    except FileNotFoundError:
        return False
    return True


def _left_digests():
    try:
        names = os.listdir(left_dir())
    except OSError:
        return set()
    return {n[: -len(".json")] for n in names if n.endswith(".json")}


def _hours_value():
    hours = sp_runtime._float_env("SESSION_PEERS_OPENCODE_RECENT_HOURS", 24.0)
    try:
        return int(hours) if hours == int(hours) else hours
    except (OverflowError, ValueError):
        return hours


def general_members():
    """``(members, opencode_coverage, discovery_gaps)`` of derived #general.

    Members are what `peers.py list` shows: live or unverified Claude sessions
    (a Codex shim counts as ``codex:``), Codex threads with ``live`` true or
    unknown, and OpenCode top-level sessions updated within
    SESSION_PEERS_OPENCODE_RECENT_HOURS, minus sessions that left. The
    coverage dict states what the OpenCode part covered, or that it failed;
    ``discovery_gaps`` names each Claude or Codex read that was incomplete.
    """
    discovered, coverage, gaps = _discover_general()
    return _without_left(discovered), coverage, gaps


def _without_left(discovered):
    """``discovered`` minus the sessions that ran `leave general`."""
    left = _left_digests()
    return [t for t in discovered if _digest(t) not in left]


def _discover_general():
    """``(discovered, opencode_coverage, discovery_gaps)``: the `peers.py list` sessions, leave markers not applied.

    Discovery stays best effort (one bad registry file does not empty #general),
    but every incomplete Claude or Codex read is named in ``discovery_gaps`` so a
    post and its ACK tally never silently omit a runtime.
    """
    members, gaps = [], []

    def add(typed):
        try:
            typed = sp_journal._norm(typed)
        except ValueError:
            return
        if typed not in members:
            members.append(typed)

    # One registry scan: the complete snapshot when it succeeds, the best-effort
    # read (with the gap named) only when it does not.
    records, claude_error = sp_claude.read_claude_records_checked()
    if claude_error is not None:
        gaps.append("Claude registry: %s" % _reason(claude_error))
        records = sp_claude.read_claude_records()
    for record in records:
        if sp_claude.is_web_record(record):
            continue  # the browser peer joins a channel by explicit membership, never by discovery
        if sp_claude.record_liveness(record) in ("live", "unverified"):
            kind = "codex" if record.get("entrypoint") == "codex" else "cc"
            add("%s:%s" % (kind, record.get("sessionId")))
    threads, schema_ok = sp_codex.codex_threads()
    if not schema_ok:
        gaps.append("Codex: the thread state could not be read, so Codex threads were not enumerated")
    threads = threads + sp_codex.threads_in_other_homes(t["id"] for t in threads)
    for thread in threads:
        if thread.get("live") is True or thread.get("live") is None:
            add("codex:%s" % thread["id"])
    try:
        sessions = sp_opencode.recent_sessions()
    except sp_opencode.OpenCodeError as exc:
        coverage = {"enumerated": False, "error": _reason(exc)}
    else:
        for session in sessions:
            add("oc:%s" % session["id"])
        coverage = {"scope": "top-level sessions updated within the window", "hours": _hours_value()}
    return members, coverage, gaps


def coverage_line(coverage):
    """The D2 statement printed with every #general post: what OpenCode discovery covered."""
    if coverage.get("enumerated") is False:
        return sp_journal._human("note: OpenCode was not enumerated: %s" % coverage.get("error"))
    return "OpenCode coverage: top-level sessions updated within the last %s h (as peers.py list)" % (
        coverage.get("hours"),)


def gaps_lines(gaps):
    """One escaped note per incomplete Claude or Codex discovery read."""
    return [sp_journal._human("note: discovery incomplete: %s" % gap) for gap in gaps or ()]


def gc_left_markers(dry_run=False, verbose=True):
    """Remove leave markers of sessions verified gone; never by age. Returns the names removed."""
    try:
        names = sorted(os.listdir(left_dir()))
    except OSError:
        return []
    removed, cache = [], {}
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(left_dir(), name)
        rec = sp_runtime.read_json(path, None)
        member = rec.get("member") if isinstance(rec, dict) else None
        try:
            member = sp_journal._norm(member)
        except (ValueError, TypeError, AttributeError):
            continue
        if _digest(member) + ".json" != name or member_liveness(member, cache) != "gone":
            continue
        if not dry_run:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                sp_runtime.log("could not remove the leave marker %s: %s" % (name, exc))
                continue
        removed.append(name)
        if verbose:
            print("%s leave marker %s" % ("would prune" if dry_run else "pruned", name))
    return removed


def receipts_dir():
    """Handover receipts, one ``<sha(from)>-<sha(to)>.json`` per recorded grant."""
    return os.path.join(channels_root(), "grants")


_RECEIPT_NAME = re.compile(r"^[0-9a-f]{64}-[0-9a-f]{64}\.json\Z")


def _receipt_path(frm, to):
    return os.path.join(receipts_dir(), "%s-%s.json" % (_digest(frm), _digest(to)))


def write_grant_receipt(grant):
    """Keep ``{from, to, through_seq, at}`` of a recorded `inbox handover` grant.

    The journal may prune the grant under pressure; the receipt lets
    `peers.py handover` proceed for ``RECEIPT_MAX_AGE`` after it was recorded.
    """
    _ensure_dir(channels_root())
    _ensure_dir(receipts_dir())
    data = {"from": grant["from"], "to": grant["to"], "through_seq": grant["through_seq"], "at": _wall()}
    sp_runtime.write_json_atomic(_receipt_path(grant["from"], grant["to"]), data, mode=0o600)


def _receipt_age(path):
    """Seconds since the receipt at ``path`` was written, or None when it is unreadable."""
    data = sp_runtime.read_json(path, None)
    at = data.get("at") if isinstance(data, dict) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return _wall() - at


def has_receipt(frm, to):
    age = _receipt_age(_receipt_path(frm, to))
    return age is not None and -300.0 <= age < RECEIPT_MAX_AGE


def gc_receipts(dry_run=False, verbose=True):
    """Remove handover receipts older than ``RECEIPT_MAX_AGE`` (they no longer authorise anything)."""
    try:
        names = sorted(os.listdir(receipts_dir()))
    except OSError:
        return []
    removed = []
    for name in names:
        if not _RECEIPT_NAME.match(name):
            continue
        path = os.path.join(receipts_dir(), name)
        age = _receipt_age(path)
        if age is None or age < RECEIPT_MAX_AGE:
            continue
        if not dry_run:
            try:
                os.unlink(path)
            except OSError:
                continue
        removed.append(name)
        if verbose:
            print("%s expired handover receipt %s" % (
                "would prune" if dry_run else "pruned", sp_journal._human(name)))
    return removed


class _Allowance:
    """The one-`@all`-per-60-seconds allowance of one sender, across every channel.

    ``<root>/allcast/<sha256(sender)>.lock`` is created, never replaced or
    unlinked. One exclusive flock on it is held from the stamp check through
    ``release``, which the caller calls right after journaling (delivery runs
    after the lock is dropped): read ``<sha>.json``, refuse when it is under
    ``ALL_INTERVAL`` old, write the new stamp before anything is journaled. The
    caller ``restore``s the previous stamp when the journal refuses; a crash
    after the reservation leaves the stamp, so the allowance stays spent.
    """

    def __init__(self, sender):
        root = os.path.join(channels_root(), "allcast")
        digest = _digest(sender)
        self.stamp_path = os.path.join(root, digest + ".json")
        self.sender = sender
        self.fd = None
        try:
            _ensure_dir(channels_root())
            _ensure_dir(root)
            fd = os.open(os.path.join(root, digest + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ChannelError("could not check the @all allowance: %s" % _reason(exc), 1)
        try:
            os.chmod(os.path.join(root, digest + ".lock"), 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            previous = sp_runtime.read_json(self.stamp_path, None)
            self.previous = previous if isinstance(previous, dict) else None
            now = _wall()
            at = self.previous.get("at") if self.previous else None
            # A future-dated stamp (clock step back, or a planted file) is refused
            # like a fresh one, however far ahead it is: fail closed.
            # A non-finite or non-numeric ``at`` (Infinity, NaN, a string) counts
            # as a fresh stamp: refused for the whole interval.
            if at is not None:
                if (isinstance(at, (int, float)) and not isinstance(at, bool)
                        and math.isfinite(at)):
                    wait = ALL_INTERVAL - (now - at)
                else:
                    wait = ALL_INTERVAL
                if wait > 0:
                    raise ChannelError(
                        "at most one @all per sender every %d seconds (limit ALL_INTERVAL); "
                        "try again in %d s" % (ALL_INTERVAL, max(1, int(min(wait, ALL_INTERVAL * 1000) + 0.999))))
            sp_runtime.write_json_atomic(self.stamp_path, {"sender": sender, "at": now}, mode=0o600)
            _failpoint("allcast_after_reserve")
        except OSError as exc:
            os.close(fd)
            raise ChannelError("could not check the @all allowance: %s" % _reason(exc), 1)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def restore(self):
        """Put the previous stamp back. Safe because the lock is still held."""
        try:
            if self.previous is not None:
                sp_runtime.write_json_atomic(self.stamp_path, self.previous, mode=0o600)
            else:
                os.unlink(self.stamp_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            sp_runtime.log("could not restore the @all allowance of %s: %s" % (self.sender, exc))

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _canon_member(value, exclude=None, typed_first=False):
    """A typed member id in canonical form. Names are resolved; ambiguity is refused.

    ``typed_first`` accepts a well-formed typed id without looking the session
    up, so a member that no longer exists can still be removed."""
    if typed_first:
        try:
            return sp_journal._norm(value)
        except ValueError:
            pass
    try:
        found = sp_identity.resolve_typed(value, exclude=exclude)
        return sp_journal._norm("%s:%s" % (found["kind"], found["uuid"]))
    except sp_codex.ResolveError as exc:
        lines = sp_identity.ambiguity_report(exc, value, exclude)
        raise ChannelError("\n".join([str(exc)] + lines))
    except ValueError as exc:
        raise ChannelError(str(exc))


def _err(text):
    sys.stderr.write("error: %s\n" % text)


def cmd_channel(args):
    try:
        me = sp_journal.caller()
    except sp_journal.JournalError as exc:
        _err(exc)
        return 2
    handlers = {
        "create": _cmd_create, "join": _cmd_join, "leave": _cmd_leave,
        "remove": _cmd_remove, "list": _cmd_list, "members": _cmd_members,
        "tail": _cmd_tail,
    }
    handler = handlers.get(getattr(args, "channel_cmd", None))
    if handler is None:
        _err("channel subcommands are %s" % ", ".join("`%s`" % n for n in handlers))
        return 2
    try:
        return handler(args, me)
    except ChannelError as exc:
        _err(exc)
        return exc.code


def _require(key):
    rec = read_channel(key)
    if rec is None:
        raise ChannelError("no channel named #%s; run `peers.py channel list`" % key)
    return rec


def _cmd_create(args, me):
    exclude = me.partition(":")[2]
    members = [_canon_member(m, exclude=exclude) for m in args.member or []]
    rec = create_channel(args.name, me, members)
    print("created #%s (%d members)" % (rec["key"], len(rec["members"])))
    return 0


def _general_marker(action, me):
    """`join general` / `leave general`: clear or set this session's leave marker."""
    try:
        if action == "join":
            print("joined #general" if clear_left(me) else "already in #general")
        else:
            set_left(me)
            print("left #general")
    except OSError as exc:
        raise ChannelError("could not update the #general leave marker: %s" % _reason(exc), 1)
    return 0


def _cmd_join(args, me):
    if parse_name(args.name) == sp_constants.CHANNEL_GENERAL:
        return _general_marker("join", me)
    key = stored_key(args.name)

    def mutate(rec):
        if rec["kind"] == "buddy":
            raise ChannelError("buddy channels are joined through buddy set")
        if me in rec["members"]:
            return None
        _check_member_cap(len(rec["members"]) + 1)
        rec["members"].append(me)
        return EVENT_JOINED, {"member": me}

    _rec, entry = change_membership(key, mutate, me)
    print(("joined #%s" if entry else "already a member of #%s") % key)
    return 0


def _cmd_leave(args, me):
    if parse_name(args.name) == sp_constants.CHANNEL_GENERAL:
        return _general_marker("leave", me)
    key = stored_key(args.name)

    def mutate(rec):
        if me not in rec["members"]:
            return None
        rec["members"].remove(me)
        return EVENT_LEFT, {"member": me}

    _rec, entry = change_membership(key, mutate, me)
    print(("left #%s" if entry else "not a member of #%s") % key)
    return 0


def _cmd_remove(args, me):
    key = stored_key(args.name)
    target = _canon_member(args.member, exclude=me.partition(":")[2], typed_first=True)
    removed = []

    def mutate(rec):
        if rec["owner"] != me:
            raise ChannelError("only the owner of #%s can remove members" % key)
        found = target
        if found not in rec["members"] and found.startswith("cc:"):
            # A Codex shim is stored as codex:<id>; accept its cc: spelling.
            found = "codex:" + found[3:]
        if found not in rec["members"]:
            raise ChannelError("%s is not a member of #%s" % (target, key))
        rec["members"].remove(found)
        removed.append(found)
        return EVENT_REMOVED, {"member": found}

    change_membership(key, mutate, me)
    print("removed %s from #%s" % (sp_journal._human(removed[0]), key))
    return 0


def _cmd_list(args, me):
    rows = []
    for rec in list_channels():
        role = "owner" if rec["owner"] == me else "member" if me in rec["members"] else "-"
        rows.append({
            "name": rec["key"], "display": rec.get("display"), "kind": rec.get("kind"),
            "owner": rec.get("owner"), "members": len(rec["members"]), "you": role,
        })
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no channels")
    for row in rows:
        print("#%s  %s  %d members  %s  owner %s" % (
            row["name"], sp_journal._human(row["kind"]), row["members"], row["you"],
            sp_journal._human(row["owner"]),
        ))
    return 0


def _cmd_general_members(args):
    members, coverage, gaps = general_members()
    cache = {}
    rows = [{"member": m, "owner": False, "liveness": member_liveness(m, cache)} for m in members]
    if args.json:
        print(json.dumps({"channel": sp_constants.CHANNEL_GENERAL, "display": sp_constants.CHANNEL_GENERAL,
                          "derived": True, "members": rows, "opencode_coverage": coverage,
                          **({"discovery_incomplete": gaps} if gaps else {})}))
        return 0
    for row in rows:
        print("%s  %s" % (sp_journal._human(row["member"]), row["liveness"]))
    print(coverage_line(coverage))
    for line in gaps_lines(gaps):
        print(line)
    return 0


def _cmd_members(args, me):
    if parse_name(args.name) == sp_constants.CHANNEL_GENERAL:
        return _cmd_general_members(args)
    rec = _require(stored_key(args.name))
    _check_roster_cap(rec)
    cache = {}
    rows = [
        {"member": m, "owner": m == rec["owner"], "liveness": member_liveness(m, cache)}
        for m in rec["members"]
    ]
    if args.json:
        print(json.dumps({"channel": rec["key"], "display": rec.get("display"), "members": rows}))
        return 0
    for row in rows:
        print("%s  %s%s" % (sp_journal._human(row["member"]), row["liveness"],
                            "  (owner)" if row["owner"] else ""))
    return 0


def describe_entry(entry):
    """One escaped human line for a history entry."""
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    who = entry.get("from_sid")
    who = "%s:%s" % (entry.get("from_kind"), who) if who else "anonymous"
    kind = entry.get("kind") or ""
    member = data.get("member")
    if kind == EVENT_CREATED:
        summary = "created"
    elif kind == EVENT_JOINED:
        summary = "%s joined" % member
    elif kind == EVENT_LEFT:
        summary = "%s left" % member
    elif kind == EVENT_REMOVED:
        summary = "%s removed" % member
    elif kind == EVENT_HANDOVER:
        summary = "%s handed over to %s" % (member, data.get("to"))
    elif kind == EVENT_OUTCOME:
        outcomes = data.get("outcomes")
        summary = "outcomes of message %s for %d recipients" % (
            data.get("message_id"), len(outcomes) if isinstance(outcomes, dict) else 0)
    elif isinstance(data.get("body"), str):
        summary = data["body"]
    else:
        summary = json.dumps(data, sort_keys=True)
    return "#%d %s %s [%s]: %s" % (
        entry["seq"], sp_journal._human(entry.get("ts")), sp_journal._human(who), sp_journal._human(kind),
        sp_journal._human(summary),
    )


def _cmd_tail(args, me):
    key = parse_name(args.name)
    if key != sp_constants.CHANNEL_GENERAL:  # derived #general has history but no record
        _require(key)
    if args.since is not None and args.since < 0:
        raise ChannelError("--since must be zero or greater")
    if not 1 <= args.limit <= sp_constants.TOPIC_TAIL_MAX:
        raise ChannelError("--limit must be between 1 and %d" % sp_constants.TOPIC_TAIL_MAX)
    result = read_history(key, since=args.since, limit=args.limit)
    journal_cache = {}
    views = {
        entry["seq"]: delivery_view(key, entry, journal_cache)
        for entry in result["entries"] if entry.get("kind") == EVENT_SEND
    }
    if args.json:
        entries = [dict(e, view=views[e["seq"]]) if e["seq"] in views else e for e in result["entries"]]
        print(json.dumps(dict(result, entries=entries, channel=key)))
        return 0
    gap = result["gap"]
    if gap:
        print("gap: entries %d..%d pruned" % (gap["from"], gap["to"]))
    for entry in result["entries"]:
        print(describe_entry(entry))
        if entry["seq"] in views:
            for line in describe_view(views[entry["seq"]]):
                print(line)
    print("next: --since %d" % result["next"])
    return 0


# --------------------------------------------------------------------------
# Lifecycle: leave and handover
# --------------------------------------------------------------------------


def _caller_or_none():
    try:
        return sp_journal.caller()
    except sp_journal.JournalError as exc:
        _err(exc)
        return None


def _leave_mutator(me):
    def mutate(rec):
        if me not in rec["members"]:
            return None
        rec["members"].remove(me)
        return EVENT_LEFT, {"member": me}

    return mutate


def cmd_leave(args):
    """`peers.py leave`: leave every stored channel the caller is in, then #general.

    Each channel is handled on its own (membership write, then ``channel.left``);
    a failure (including a record that cannot be read or a store that cannot be
    listed) is reported and the rest still run, with exit 1 at the end. The
    #general leave marker is written regardless. A session that already left everywhere does nothing and exits 0. Ownership is kept.
    """
    me = _caller_or_none()
    if me is None:
        return 2
    pairs, listed = _lifecycle_records("leave")
    failed = not listed
    for key, rec in pairs:
        if rec is None or me not in rec["members"]:
            continue
        try:
            _rec, entry = change_membership(key, _leave_mutator(me), me)
        except (ChannelError, OSError) as exc:
            _err("could not leave #%s: %s" % (key, _reason(exc)))
            failed = True
            continue
        if entry:
            print("left #%s" % key)
    try:
        set_left(me)
        print("left #%s" % sp_constants.CHANNEL_GENERAL)
    except OSError as exc:
        _err("could not record the #general leave marker: %s" % _reason(exc))
        failed = True
    return 1 if failed else 0


def _grant_recorded(me, target):
    """A live #157 grant from ``me`` to ``target``, or a receipt of one younger than RECEIPT_MAX_AGE."""
    try:
        for grant in sp_journal.read_only_view()["handovers"]:
            if grant.get("from") == me and grant.get("to") == target:
                return True
    except Exception:  # an unreadable journal falls back to the receipt
        pass
    return has_receipt(me, target)


def _successor_label(target):
    try:
        name = sp_identity.resolve_typed(target).get("name")
    except (sp_codex.ResolveError, ValueError, OSError):
        name = None
    return _reason(name) if isinstance(name, str) and name else target


def _handover_mutator(me, target, label):
    def mutate(rec):
        if me in rec["members"]:
            rec["members"].remove(me)
        elif rec["owner"] != me:
            return None
        if target not in rec["members"]:
            if len(rec["members"]) >= sp_constants.CHANNEL_MAX_MEMBERS:
                raise ChannelError(
                    "a channel holds at most %d members (limit CHANNEL_MAX_MEMBERS)"
                    % sp_constants.CHANNEL_MAX_MEMBERS)
            rec["members"].append(target)
        if rec["owner"] == me:
            rec["owner"] = target
            rec["handed_over_at"] = sp_runtime.now_iso()  # survives history retention
            if rec["kind"] == "buddy":
                # The display names the lead, so only the lead's handover renames it.
                rec["display"] = "buddy \u00b7 %s" % label
        return EVENT_HANDOVER, {"member": me, "to": target}

    return mutate


def buddy_channel_key(owner):
    """The channel key a lead's own buddy channel gets: ``buddy-`` and 12 hex of sha256 of its typed id."""
    digest = hashlib.sha256(sp_journal._norm(owner).encode("utf-8")).hexdigest()[:12]
    return sp_constants.CHANNEL_BUDDY_PREFIX + digest


def _latest_handover_to(key, owner):
    """The ``ts`` of the newest ``channel.handover`` in ``key`` that moved it to ``owner``, else ''."""
    entries = read_history(key, limit=sp_constants.TOPIC_TAIL_MAX)["entries"]
    stamps = [
        str(e.get("ts", "")) for e in entries
        if e.get("kind") == EVENT_HANDOVER and (e.get("data") or {}).get("to") == owner
    ]
    return max(stamps) if stamps else ""


def find_buddy_channel(owner):
    """The key of the buddy channel ``owner`` (a typed id) manages, or None.

    Rule: (1) the channel at ``buddy_channel_key(owner)`` when ``owner`` owns it;
    else (2) among ``kind == "buddy"`` channels ``owner`` owns, the one most recently
    handed over to ``owner`` (the record's ``handed_over_at``, else the latest
    ``channel.handover`` in retained history), ties and channels never handed
    over ordered by ``created_at`` and then key.
    """
    own = read_channel(buddy_channel_key(owner))
    if own is not None and own["owner"] == owner and own["kind"] == "buddy":
        return own["key"]
    best, best_rank = None, None
    for rec in list_channels():
        if rec["kind"] != "buddy" or rec["owner"] != owner:
            continue
        handed = rec.get("handed_over_at") or _latest_handover_to(rec["key"], owner)
        rank = (handed, str(rec.get("created_at", "")), rec["key"])
        if best_rank is None or rank > best_rank:
            best, best_rank = rec["key"], rank
    return best


def buddy_channel_add(owner, buddy):
    """Add ``buddy`` to ``owner``'s buddy channel, creating it on first use; returns its key.

    Takes only channel locks, so call it with no buddy lock held.
    """
    key = find_buddy_channel(owner)
    if key is None:
        taken = read_channel(buddy_channel_key(owner))
        if taken is not None and taken["owner"] != owner:
            raise ChannelError(
                "#%s was handed over to %s, so this session has no buddy channel of its own"
                % (taken["key"], taken["owner"]))
        try:
            create_channel(
                buddy_channel_key(owner), owner, [], kind="buddy",
                display="buddy · %s" % _successor_label(owner), reserved_ok=True)
        except ChannelError:
            if find_buddy_channel(owner) is None:  # a concurrent creator is fine
                raise
        key = find_buddy_channel(owner)
        if key is None:
            raise ChannelError("the buddy channel of %s could not be created" % owner)

    def mutate(rec):
        if rec["owner"] != owner:
            raise ChannelError("#%s is no longer owned by this session" % rec["key"])
        if buddy in rec["members"]:
            return None
        _check_member_cap(len(rec["members"]) + 1)
        rec["members"].append(buddy)
        return EVENT_JOINED, {"member": buddy}

    change_membership(key, mutate, owner)
    return key


def buddy_channel_remove(owner, buddy):
    """Remove ``buddy`` from ``owner``'s buddy channel; returns its key, or None when there is none."""
    key = find_buddy_channel(owner)
    if key is None:
        return None

    def mutate(rec):
        if rec["owner"] != owner:
            raise ChannelError("#%s is no longer owned by this session" % rec["key"])
        if buddy not in rec["members"]:
            return None
        rec["members"].remove(buddy)
        return EVENT_REMOVED, {"member": buddy}

    change_membership(key, mutate, owner)
    return key


def cmd_handover(args):
    """`peers.py handover --to TYPED`: move the caller's channels to a successor.

    Needs the grant `peers.py inbox handover --to TYPED` recorded (or its
    receipt, see ``write_grant_receipt``); it never creates one. Per channel one
    membership write adds the successor, removes the caller and moves ownership
    (and a buddy channel's display), then ``channel.handover`` is appended.
    Nothing else moves: no budgets, buddy bindings or reply-wake grants.
    """
    me = _caller_or_none()
    if me is None:
        return 2
    try:
        target = sp_journal._norm(args.to)
    except ValueError:
        _err("a handover names a full typed identity (cc:<uuid>, codex:<uuid> or oc:<ses_id>), not a name")
        return 2
    if target == me:
        _err("a session cannot hand over to itself")
        return 2
    if not _grant_recorded(me, target):
        _err("no handover from this session to %s is recorded; run "
             "`peers.py inbox handover --to %s` first, then `peers.py handover --to %s`"
             % (target, target, target))
        return 2
    label = _successor_label(target)
    pairs, listed = _lifecycle_records("hand over")
    failed = not listed
    moved = 0
    for key, rec in pairs:
        if rec is None or (me not in rec["members"] and rec["owner"] != me):
            continue
        try:
            _rec, entry = change_membership(key, _handover_mutator(me, target, label), me)
        except (ChannelError, OSError) as exc:
            _err("could not hand over #%s: %s" % (key, _reason(exc)))
            failed = True
            continue
        if entry:
            moved += 1
            print("handed over #%s to %s" % (key, sp_journal._human(target)))
    if not moved and not failed:
        print("no stored channels to hand over")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Send: journal, lock, history, serial delivery, one outcome entry
# --------------------------------------------------------------------------


class _Snapshot:
    """The members a fan-out was validated against.

    ``send_channel`` builds one only after the channel exists and the sender is
    a member; ``routes`` accepts a Claude-to-Claude channel delivery only for a
    target this lists. No command-line argument can carry one.
    """

    __slots__ = ("channel", "members")

    def __init__(self, channel, members):
        self.channel = channel
        self.members = tuple(members)


_REFUSED_FLAGS = (
    ("wake", "--wake"), ("allow_reply_wake", "--allow-reply-wake"), ("in_reply_to", "--in-reply-to"),
    ("from_thread", "--from-thread"), ("from_name", "--from-name"), ("from_sid", "--from-sid"),
    ("from_socket", "--from-socket"),
)


def require_target(value):
    """The key of the channel a ``send --to '#NAME'`` names; ChannelError when it cannot be sent to.

    The route check's channel branch. ``#general`` is derived and always exists.
    """
    name = parse_name(value)
    if name == sp_constants.CHANNEL_GENERAL:
        return name
    if read_channel(name) is None:
        raise ChannelError("no channel named #%s; run `peers.py channel list`" % name)
    return name


def _members_for(key, sender, authorised=False):
    """``(members, opencode_coverage, discovery_gaps)`` a send by ``sender`` to ``key`` addresses.

    ``authorised`` is set only by ``send_as_web``, whose server has already
    decided the browser may post: it skips the membership and discovery tests
    below. Nothing on the CLI path can set it.

    A stored channel needs the sender among its members. Derived ``#general``
    has no member cap, but its sender must be a session the #general discovery
    finds (leave markers not applied, so a session that left may still post).
    That check stops an id with no registry or thread record getting its own
    @all allowance and label; it does not bind the sender to the calling
    process, since ``journal.caller()`` trusts the environment.
    """
    if key == sp_constants.CHANNEL_GENERAL:
        discovered, coverage, gaps = _discover_general()
        if sender not in discovered and not authorised:
            if sender.startswith("oc:") and coverage.get("enumerated") is False:
                raise ChannelError(
                    "%s cannot be checked: OpenCode discovery failed (%s); only discovered "
                    "sessions can post to #general"
                    % (sp_journal._human(sender), coverage.get("error")))
            raise ChannelError(
                "%s is not a session peers.py list discovers; only discovered sessions can "
                "post to #general" % sp_journal._human(sender))
        return _without_left(discovered), coverage, gaps
    rec = read_channel(key)
    if rec is None:
        raise ChannelError("no channel named #%s; run `peers.py channel list`" % key)
    if sender not in rec["members"] and not authorised:
        raise ChannelError(
            "%s is not a member of #%s; only members can send to it (`peers.py channel join %s`)"
            % (sp_journal._human(sender), key, key)
        )
    _check_roster_cap(rec)  # refused, never truncated
    return list(rec["members"]), None, []


def _reason(value):
    """One escaped, bounded line of peer- or system-supplied text."""
    return sp_journal._human(value)[:300]


def _delivery_text(key, mid, sender, member, body, journaled):
    """The text one member receives: a header naming message and recipient, then the body."""
    lines = ["[channel #%s message %s from %s to %s]" % (key, mid, sender, member)]
    if journaled:
        lines.append(sp_journal.pointer_text(mid))
    return "\n".join(lines + [body])


def _check_size(key, mid, sender, members, body, journaled):
    """Refuse before anything is journaled when the largest wrapped delivery is over the peer cap.

    The wrapped text is bounded from above: the longest per-member text under
    the longest tag, provenance line, reply hint and Claude wrapper any
    transport adds.
    """
    if not members:
        return
    longest = max((_delivery_text(key, mid, sender, m, body, journaled) for m in members), key=len)
    check_text_size(longest, members)


def check_text_size(longest, members):
    """Refuse (ChannelError) when ``longest`` once wrapped for any transport exceeds a peer cap.

    ``longest`` is the longest per-member text; ``members`` are the typed ids it goes to.
    """
    # build_tag and build_origin cut a field by characters while the Codex and
    # Claude caps also count UTF-8 bytes, so each is measured with a one-byte
    # filler (the character cap binds) and a four-byte one (the byte cap binds).
    # Only the session name can carry non-ASCII; the id fields stay ASCII.
    ident = "i" * sp_constants.MAX_TAG_FIELD_CHARS
    # The Claude wrapper also XML-escapes the name: a double quote grows to six
    # bytes (&quot;), the worst per-character growth, so it is measured as well.
    for char in ("n", "\U0001F600", '"'):
        name = char * sp_constants.MAX_TAG_FIELD_CHARS
        tag_origin = "\n".join([
            sp_protocol.build_tag(name, ident, "/" + "p" * 107, ident),
            sp_protocol.build_origin("opencode", name, ident),
        ])
        framing = "\n".join([tag_origin, sp_protocol.reply_route_hint("oc:" + "s" * 40, "m" * 36)])
        try:
            sp_claude.check_wrapped_size(
                sp_protocol.build_wrapper(framing + "\n" + longest, "/" + "s" * 107, ident, name)
            )
        except ValueError as exc:
            raise ChannelError("%s; nothing was journaled or sent" % exc, 1)
        if any(m.startswith("codex:") for m in members):
            # The Codex transport queues "tag\norigin\nmessage" and checks that text
            # ahead of journaling, so the preflight must measure the same framing.
            try:
                sp_codex.check_queue_text(tag_origin + "\n" + longest)
            except sp_codex.QueueError as exc:
                raise ChannelError("%s; nothing was journaled or sent" % exc, 1)


def _journal_preflight(members):
    """Split ``members`` into those the journal can address and ``(member, reason)`` it cannot.

    Uses the journal's own recipient resolution, so a member listed here is one
    ``record_message`` accepts.
    """
    resolvable, excluded = [], []
    for member in members:
        try:
            sp_journal._resolve_recipients([member])
        except (sp_journal.JournalError, sp_codex.ResolveError, ValueError, OSError) as exc:
            excluded.append((member, _reason(exc)))
        else:
            resolvable.append(member)
    return resolvable, excluded


class _SendLock:
    """An exclusive flock on one send's lock file; the open descriptor holds it.

    The path is checked against the locked inode, so a garbage collector that
    unlinked the file between ``open`` and ``flock`` cannot leave this send
    holding a lock nobody can find.
    """

    def __init__(self, message_id):
        self.message_id = message_id
        path = send_lock_path(message_id)
        _ensure_dir(channels_root())
        _ensure_dir(sends_dir())
        self.fd = None
        for _attempt in range(3):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    same = os.fstat(fd).st_ino == os.stat(path).st_ino
                except FileNotFoundError:
                    same = False
                if same:
                    self.fd = fd
                    return
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)
        raise OSError("the send lock file kept disappearing")

    def release(self):
        """Unlink the lock file, then drop the lock."""
        if self.fd is None:
            return
        try:
            os.unlink(send_lock_path(self.message_id))
        except OSError:
            pass
        self.abandon()

    def abandon(self):
        """Drop the lock and leave the file, as a crashed sender does (no-op once dropped)."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _last_error(stderr_text, rc):
    lines = [ln for ln in stderr_text.splitlines() if ln.startswith("error: ")]
    return _reason(lines[-1][len("error: "):] if lines else "delivery failed (exit %d)" % rc)


def _outcome_of(payload, rc, stderr_text):
    """The shared-vocabulary outcome of one member's delivery attempt."""
    if rc != 0:
        return OUTCOME_REFUSED + _last_error(stderr_text, rc)
    if not payload:
        return STATE_UNKNOWN
    if payload.get("target") == "codex":
        return OUTCOME_PAUSED if payload.get("paused") else OUTCOME_ACCEPTED
    if payload.get("target") == "opencode":
        if payload.get("session_state") == "idle" and not payload.get("resume"):
            return OUTCOME_NOT_WOKEN
    return OUTCOME_ACCEPTED


def _deliver_member(args, member, sender, mid, text, snapshot):
    """Route-check and deliver ``text`` to one member; its outcome. Never raises on a delivery failure."""
    from . import messaging as sp_messaging, routes as sp_routes

    member_args = copy.copy(args)
    member_args.to = member
    member_args.message = text
    member_args.record = False
    member_args.wake = False
    member_args.never_wake = True
    member_args.result_sink = []
    member_args.channel_msg_id = mid
    member_args.channel_snapshot = snapshot
    rc = 1
    out = err = None
    try:
        with sp_runtime.capture_output() as (out, err):
            refused = sp_routes.check(member_args, "channel", "to")
            if refused is not None:
                rc = refused
            else:
                rc = sp_messaging._dispatch_send(
                    member_args, sender.partition(":")[2] if sender.startswith("cc:") else None
                )
    except Exception as exc:  # one member's failure is recorded and never stops the rest
        err.write("error: %s\n" % exc)
        rc = 1
    payload = member_args.result_sink[-1] if member_args.result_sink else None
    return _outcome_of(payload, rc, err.getvalue())


def send_channel(args):
    """`peers.py send --to '#NAME'`; the exit code."""
    try:
        return _send(args)
    except ChannelError as exc:
        _err(exc)
        return exc.code


def _prepare_send(key, sender, body, mid, record, authorised=False, pointer=None):
    """Everything a send decides before it writes: members, liveness, caps, size and the journal preflight.

    Shared by the CLI ``_send`` and ``send_as_web``. Raises ChannelError and
    writes nothing. ``record`` asks for a journal entry; an ``@all`` is always
    journaled. ``pointer`` says whether each delivery carries the ACK pointer
    text (default: whenever the message is journaled).
    """
    members, coverage, gaps = _members_for(key, sender, authorised)
    general = key == sp_constants.CHANNEL_GENERAL
    is_all = ALL_RE.match(body) is not None
    journaled = record or is_all
    pointer = journaled if pointer is None else bool(pointer)
    # A plain #general post is only recorded; everything else is delivered.
    delivers = is_all or not general
    others = [m for m in members if m != sender]

    cache = {}
    if general and not journaled:
        gone, live = [], []
    else:
        gone = [m for m in others if member_liveness(m, cache) == "gone"]
        live = [m for m in others if m not in gone]
    if (is_all or (general and record)) and len(live) > ALL_MAX_RECIPIENTS:
        raise ChannelError(
            "#%s would address %d recipients; at most %d are allowed per post "
            "(limit ALL_MAX_RECIPIENTS); nothing was journaled or sent"
            % (key, len(live), ALL_MAX_RECIPIENTS))
    snapshot = others if delivers else list(live)
    _check_size(key, mid, sender, live if delivers else [], body, pointer)

    deliver, excluded = list(live), []
    if journaled:
        deliver, excluded = _journal_preflight(live)
        if not deliver:
            detail = "; ".join("%s: %s" % (m, r) for m, r in excluded[:3])
            raise ChannelError(
                "no recipient of this message can be recorded for ACK%s; nothing was recorded or sent"
                % (" (%s)" % detail if detail else ""), 1)
    return types.SimpleNamespace(
        key=key, sender=sender, body=body, mid=mid, coverage=coverage, gaps=gaps, general=general,
        is_all=is_all, journaled=journaled, pointer=pointer, delivers=delivers, snapshot=snapshot,
        gone=gone, live=list(live), deliver=deliver, excluded=excluded)


def _record_journal(plan):
    """Journal the prepared message for its resolvable recipients.

    Takes the ``@all`` allowance first (a refused journal write gives it back).
    Raises ``sp_journal.JournalError`` when the journal refuses; ChannelError
    when the allowance does. Both are raised before anything is written; the
    caller runs the ``send_after_journal`` crash point once this returns.
    """
    allowance = _Allowance(plan.sender) if plan.is_all else None
    try:
        try:
            sp_journal.record_message(plan.sender, plan.deliver, plan.body, message_id=plan.mid)
        except sp_journal.JournalError:
            if allowance is not None:
                allowance.restore()  # a refused send does not spend the allowance
            raise
    finally:
        if allowance is not None:
            allowance.release()


def _append_send(plan, extra=None):
    """Append the ``channel.send`` entry, holding the message's send lock first when it fans out.

    Returns the held ``_SendLock`` (None when nothing is delivered). A failure
    releases the lock and raises ChannelError naming the message and whether it
    was journaled; no retry.
    """
    delivery = "fanout" if plan.delivers and plan.snapshot else "none"
    data = {
        "sender": plan.sender, "body": plan.body, "message_id": plan.mid, "journaled": plan.journaled,
        "delivery": delivery, "recipients": list(plan.snapshot),
        "excluded": [{"member": m, "reason": r} for m, r in plan.excluded],
        "opencode_coverage": plan.coverage,
        **({"discovery_incomplete": plan.gaps} if plan.gaps else {}),
        **(extra or {}),
    }
    lock = None
    try:
        try:
            if delivery == "fanout":
                # The lock is held before the history entry is published, so a reader
                # that sees the entry finds the sender holding it.
                lock = _SendLock(plan.mid)
                _failpoint("send_after_lock")
            append_history(plan.key, EVENT_SEND, plan.sender, data)
        except (ChannelError, OSError) as exc:
            if lock is not None:
                lock.release()
            raise ChannelError(
                "%s; message %s was %s" % (
                    exc, plan.mid, "journaled, not delivered" if plan.journaled else "not delivered"), 1)
    except BaseException:
        if lock is not None:
            lock.abandon()  # a crash or unexpected exception leaves the file, as a dead sender would
        raise
    return lock


def _send(args):
    from . import routes as sp_routes

    as_json = bool(getattr(args, "json", False))
    for attr, flag in _REFUSED_FLAGS:
        value = getattr(args, attr, None)
        if value is not None and value is not False:
            raise ChannelError("%s cannot be used when sending to a channel" % flag)
    try:
        args.message = sp_runtime.message_from_args(args)
    except ValueError as exc:
        raise ChannelError(_reason(exc), 1)
    refused = sp_routes.check(args, "send", "to")
    if refused is not None:
        return refused
    key = args.route_checked["id"]
    try:
        sender = sp_journal.caller()
    except sp_journal.JournalError as exc:
        raise ChannelError(_reason(exc))
    mid = str(uuidlib.uuid4())
    plan = _prepare_send(key, sender, args.message, mid, bool(getattr(args, "record", False)))
    if plan.journaled:
        try:
            _record_journal(plan)
        except sp_journal.JournalError as exc:
            return sp_journal.presend_failed(exc, as_json)
        _failpoint("send_after_journal")

    lock = _append_send(plan)
    try:
        payload = {"status": "recorded", "channel": key, "message_id": mid, "outcomes": {}}
        if plan.journaled:
            payload["journal"] = {"message_id": mid, "recorded": True}
        if plan.coverage is not None:
            payload["opencode_coverage"] = plan.coverage
        if plan.gaps:
            payload["discovery_incomplete"] = plan.gaps
        if lock is None:
            if not plan.delivers:
                # Journaled for the resolvable members, delivered to none.
                payload["outcomes"] = {m: OUTCOME_RECORDED for m in plan.deliver}
                for member, reason in plan.excluded:
                    payload["outcomes"][member] = OUTCOME_REFUSED + reason
                    payload.setdefault("notes", {})[member] = ACK_NOT_REQUESTED
                if plan.excluded:
                    payload["status"] = "partial"  # as _fan_out reports a mixed result
            return _report(payload, 1 if plan.excluded else 0, as_json, plan.journaled, "recorded in #%s (nothing delivered)" % key
                           if not plan.delivers else "recorded in #%s (no other members)" % key)
        return _fan_out(args, lock, payload, as_json, (key, plan.sender, mid, plan.body, plan.journaled),
                        (plan.snapshot, plan.gone, plan.deliver, plan.excluded))
    finally:
        if lock is not None:
            # Only a crash or an unexpected exception gets here with the lock
            # still held: drop it and leave the file, as a dead sender would.
            lock.abandon()


def _deliver_all(args, lock, send, members, guard=None, pointer=None, ack=True):
    """Deliver serially, append the one outcome entry, then release the lock.

    ``(outcomes, notes, update_failed, problem)``; ``problem`` is the ChannelError
    when the outcome entry could not be appended. Prints nothing. ``guard`` is a
    context-manager factory entered around each member's delivery and never
    across a pacing sleep; ``pointer`` says whether the text carries the ACK
    pointer (default: whenever ``journaled``); ``ack`` False notes every member
    as ACK not requested.
    """
    key, sender, mid, body, journaled = send
    snapshot, gone, deliver, excluded = members
    pointer = journaled if pointer is None else pointer
    _failpoint("send_after_history")
    outcomes = {m: None for m in snapshot}
    notes = {}
    for member in gone:
        outcomes[member] = OUTCOME_GONE
    for member, reason in excluded:
        outcomes[member] = OUTCOME_REFUSED + reason
        notes[member] = ACK_NOT_REQUESTED
    update_failed = False
    snap = _Snapshot(key, snapshot)
    last_claude = None
    for member in deliver:
        is_claude = member.startswith("cc:")
        if is_claude and last_claude is not None:
            wait = PACE_SECONDS - (_clock() - last_claude)
            if wait > 0:
                _sleep(wait)
        text = _delivery_text(key, mid, sender, member, body, pointer)
        with (guard() if guard is not None else contextlib.nullcontext()):
            outcomes[member] = _deliver_member(args, member, sender, mid, text, snap)
        if is_claude:
            last_claude = _clock()
        if journaled and outcomes[member].startswith(OUTCOME_REFUSED):
            # transport_failed returns its ``rc`` (0 here) only when the append worked.
            if sp_journal.transport_failed(mid, member, outcomes[member][len(OUTCOME_REFUSED):], rc=0):
                update_failed = True
    if not ack:
        for member in snapshot:
            notes.setdefault(member, ACK_NOT_REQUESTED_ASKED)

    _failpoint("send_before_outcome")
    record = {"message_id": mid, "outcomes": outcomes}
    if notes:
        record["notes"] = notes
    problem = None
    try:
        append_history(key, EVENT_OUTCOME, sender, record)
    except ChannelError as exc:
        problem = exc
    lock.release()
    return outcomes, notes, update_failed, problem


def _fan_out(args, lock, payload, as_json, send, members):
    """`_deliver_all` for the CLI: fold the result into ``payload`` and report it."""
    key, _sender, mid, _body, journaled = send
    outcomes, notes, update_failed, problem = _deliver_all(args, lock, send, members)
    payload["outcomes"] = outcomes
    if notes:
        payload["notes"] = notes
    accepted = [o for o in outcomes.values() if not o.startswith(OUTCOME_REFUSED)]
    refused_any = len(accepted) != len(outcomes)
    rc = sp_journal.EXIT_UPDATE_FAILED if update_failed else 1 if refused_any or problem else 0
    payload["status"] = "sent" if not refused_any else "partial" if accepted else "failed"
    if problem is not None:
        _err("%s; the delivery was made but its outcome is not recorded and reads as unknown" % problem)
    return _report(payload, rc, as_json, journaled, "sent to #%s, message %s" % (key, mid))


WEB_NOT_SENT = "not sent"
WEB_NOT_SENT_JOURNALED = "not sent (journaled)"
WEB_SENT = "sent"


def _web_member_args(web_sender):
    """The minimal ``Namespace`` a browser channel post hands ``_deliver_member``.

    Carries every attribute ``routes.check`` and ``messaging._dispatch_send``
    read, so no member delivery depends on the process's own argv or environment
    for who is sending. ``web_sender`` ({"sid", "name", "socket"}) attributes the
    delivery to the browser peer.
    """
    return argparse.Namespace(
        to=None, message=None, message_file=None, json=False, record=False, wake=False,
        never_wake=True, allow_reply_wake=None, in_reply_to=None, from_thread=None,
        from_name=None, from_sid=None, from_socket=None, timeout=None,
        web_sender=dict(web_sender), result_sink=[],
    )


def send_as_web(key, web_sender, body, ack, mid, deliver_guard=None, expected_recipients=None,
                resolve_guard=None, on_journaled=None):
    """Post ``body`` to stored channel ``key`` as the browser peer; a payload dict, never printed.

    ``web_sender`` is ``{"sid", "name", "socket"}``, set by the chat server,
    which has already authorised the browser: the membership test is skipped
    here and nowhere else (the CLI cannot reach this path). ``mid`` is the
    caller's lowercase uuid4. ``deliver_guard`` and ``resolve_guard`` are
    context-manager factories: the first is entered around each member's
    delivery, the second around the resolution and the two writes (journal,
    history); neither is held across a pacing sleep. ``on_journaled(deliver)`` is
    called once the journal write has succeeded, with the typed ids that will be
    delivered to, so a caller that sees an exception escape afterwards knows the
    message was recorded and can show its recipients during the fan-out. ``#general`` is refused
    unless ``expected_recipients`` (typed ids the caller previewed) is given and
    ``body`` is an ``@all``; the live members (before the journal preflight, whose
    exclusions are recorded outcomes) must then equal it.

    Payload: ``state`` (``sent``, ``not sent`` or ``not sent (journaled)``),
    ``reason`` (None when sent), ``message_id``, ``outcomes`` ({recipient:
    outcome}), ``recipients``, ``excluded`` ([{member, reason}]); a sent post
    may add ``notes``, ``update_failed`` and ``outcome_problem``. Every refusal
    before the journal write has state ``not sent`` and leaves no journal or
    history entry; a history failure after the journal write is ``not sent
    (journaled)`` and is not retried. Any other exception after the journal
    write propagates. ``ack`` False omits the ACK pointer from each delivery;
    an ``@all`` always asks for ACKs.
    """
    payload = {"state": WEB_NOT_SENT, "reason": None, "message_id": mid, "outcomes": {},
               "recipients": [], "excluded": []}

    def refuse(reason, state=WEB_NOT_SENT):
        payload["state"], payload["reason"] = state, _reason(reason)
        return payload

    if not (isinstance(mid, str) and sp_constants.UUID_RE.fullmatch(mid) and mid == mid.lower()):
        payload["message_id"] = None
        return refuse("invalid message id")
    guard = resolve_guard if resolve_guard is not None else contextlib.nullcontext
    with guard():
        # Only a failure before the journal write succeeds is `not sent`.
        try:
            sender = "web:%s" % sp_identity.parse_address("web:%s" % web_sender["sid"])["uuid"]
            key = parse_name(key)
            is_all = ALL_RE.match(body) is not None
            if key == sp_constants.CHANNEL_GENERAL and not (expected_recipients is not None and is_all):
                return refuse("#general is not open to browser posts")
            ask = bool(ack) or is_all
            plan = _prepare_send(key, sender, body, mid, True, authorised=True, pointer=ask)
            payload["recipients"] = list(plan.snapshot)
            payload["excluded"] = [{"member": m, "reason": r} for m, r in plan.excluded]
            if expected_recipients is not None and sorted(plan.live) != sorted(expected_recipients):
                return refuse("recipients changed, preview again")
            _record_journal(plan)
        except (ChannelError, sp_journal.JournalError, ValueError, KeyError, TypeError, OSError) as exc:
            return refuse(exc)
        if on_journaled is not None:
            on_journaled(list(plan.deliver))
        _failpoint("send_after_journal")
        try:
            lock = _append_send(plan, {"ack_requested": ask})
        except ChannelError as exc:
            return refuse(exc, WEB_NOT_SENT_JOURNALED)
    try:
        if lock is None:
            payload["state"] = WEB_SENT
            return payload
        outcomes, notes, update_failed, problem = _deliver_all(
            _web_member_args(web_sender), lock, (key, sender, mid, body, True),
            (plan.snapshot, plan.gone, plan.deliver, plan.excluded), guard=deliver_guard,
            pointer=ask, ack=ask)
    finally:
        if lock is not None:
            lock.abandon()  # a no-op after a normal release; after a crash the file stays, as for a dead sender
    payload["state"] = WEB_SENT
    payload["outcomes"] = outcomes
    if notes:
        payload["notes"] = notes
    if update_failed:
        payload["update_failed"] = True
    if problem is not None:
        payload["outcome_problem"] = _reason(problem)
    return payload


def _report(payload, rc, as_json, journaled, headline):
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return rc
    print(headline)
    if payload.get("opencode_coverage") is not None:
        print(coverage_line(payload["opencode_coverage"]))
    for line in gaps_lines(payload.get("discovery_incomplete")):
        print(line)
    if journaled:
        print(sp_journal.pointer_text(payload["message_id"]))
    notes = payload.get("notes") or {}
    for member, outcome in payload["outcomes"].items():
        print(sp_journal._human("  %s: %s%s" % (
            member, outcome, " (%s)" % notes[member] if member in notes else "")))
    return rc


# --------------------------------------------------------------------------
# Send state for readers
# --------------------------------------------------------------------------


def _find_outcome(key, message_id, after_seq):
    """The ``channel.outcome`` entry of ``message_id`` among ALL retained entries after ``after_seq``."""
    cursor = after_seq
    limit = sp_constants.TOPIC_TAIL_MAX
    while True:
        page = read_history(key, since=cursor, limit=limit)
        for entry in page["entries"]:
            data = entry.get("data")
            if (entry.get("kind") == EVENT_OUTCOME and isinstance(data, dict)
                    and data.get("message_id") == message_id):
                return entry
        if len(page["entries"]) < limit:
            return None
        cursor = page["next"]


def _probe_send_lock(message_id):
    """``held``, ``free``, ``absent`` or ``error`` for a send's lock file.

    Acquiring the lock means its sender is gone, whatever its pid became; no
    pid is read. The probe never keeps the lock. A symlink or a file that is not
    a regular file reads ``error``, as does any other failure to open or lock it.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(send_lock_path(message_id), flags)
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        return "error"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "error"
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "error"
        return "free"
    finally:
        os.close(fd)


def _outcomes_of(entry):
    outcomes = (entry.get("data") or {}).get("outcomes")
    if not isinstance(outcomes, dict):
        return {}
    return {str(k): str(v) for k, v in outcomes.items()}


def send_state_from(found, probe, recheck_found):
    """Pure send-state rule shared by ``_send_state`` and the web read model.

    ``found`` is whether an outcome entry exists; ``probe`` the ``_probe_send_lock``
    answer; ``recheck_found`` whether a second look after a free or absent lock found
    one. Returns ``complete``, ``in progress``, ``unverified`` or ``unknown``.
    """
    if found:
        return "complete"
    if probe == "held":
        return STATE_IN_PROGRESS
    if probe == "error":
        return STATE_UNVERIFIED
    return "complete" if recheck_found else STATE_UNKNOWN


def _send_state(key, entry, message_id):
    """``(state, outcomes)`` of a fan-out: ``complete`` with its outcomes, else ``in progress``, ``unknown`` or ``unverified``."""
    try:
        found = _find_outcome(key, message_id, entry["seq"])
        if found is not None:
            return "complete", _outcomes_of(found)
        probe = _probe_send_lock(message_id)
        if probe in ("held", "error"):
            return send_state_from(False, probe, False), None
        # The lock was free or already removed: the outcome may have landed in between.
        found = _find_outcome(key, message_id, entry["seq"])
        state = send_state_from(False, probe, found is not None)
        return state, (_outcomes_of(found) if found is not None else None)
    except (OSError, sp_topics.TopicError, KeyError, TypeError, ValueError):
        return STATE_UNVERIFIED, None


def _ack_states(message_id, cache):
    """``{slot: acked|pending}`` from the journal's read-only view, or None when it cannot be read."""
    if "view" not in cache:
        try:
            cache["view"] = sp_journal.read_only_view()
        except Exception:  # an unreadable journal only makes ACK state unverified
            cache["view"] = None
    view = cache["view"]
    if view is None:
        return None
    for item in view["entries"]:
        if item["message_id"] == message_id:
            return {r["slot"]: r["state"] for r in item["recipients"]}
    return None


def delivery_rows(data, state, outcomes, acks):
    """Pure per-recipient rows of a ``channel.send`` entry's ``data``.

    ``state`` is the send's state, ``outcomes`` the recorded ``{slot: outcome}`` (used
    when ``complete``) and ``acks`` ``{slot: acked|pending}`` or None when the journal
    could not be read. A refused recipient shows its refusal; an acked one overrides
    the delivery state.
    """
    recipients = [r for r in data.get("recipients") or [] if isinstance(r, str)]
    refused = {
        x.get("member"): str(x.get("reason"))
        for x in data.get("excluded") or [] if isinstance(x, dict)
    }
    rows = []
    for recipient in recipients:
        if recipient in refused:
            row_state = OUTCOME_REFUSED + refused[recipient]
        elif state == "complete":
            row_state = (outcomes or {}).get(recipient, STATE_UNKNOWN)
        else:
            row_state = state
        row = {"recipient": recipient, "state": row_state}
        if data.get("journaled"):
            if recipient in refused:
                row["ack"] = "not requested (unverified)"
            elif acks is None:
                row["ack"] = STATE_UNVERIFIED
            else:
                row["ack"] = acks.get(recipient, "not requested")
            if data.get("ack_requested") is False and row["ack"] != STATE_ACKED \
                    and recipient not in refused:
                # The post asked for no ACK: the journal's pending reads "not requested".
                row["ack"] = "not requested"
            if row["ack"] == STATE_ACKED:
                row["state"] = STATE_ACKED
        rows.append(row)
    return rows


def delivery_view(key, entry, journal_cache=None):
    """The reader's view of one ``channel.send`` entry.

    ``{"state", "recipients": [{"recipient", "state", "ack"?}]}``. ``state`` is
    ``recorded`` (nothing to deliver), ``complete``, ``in progress``,
    ``unknown`` or ``unverified``; a recipient's state is its recorded outcome
    (or ``acked``), else the send's.
    """
    journal_cache = {} if journal_cache is None else journal_cache
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    mid = data.get("message_id")
    outcomes = {}
    if data.get("delivery") == "none":
        state = OUTCOME_RECORDED
    else:
        state, outcomes = _send_state(key, entry, mid)
        outcomes = outcomes or {}
    acks = _ack_states(mid, journal_cache) if data.get("journaled") else None
    return {"state": state, "recipients": delivery_rows(data, state, outcomes, acks)}


def describe_view(view):
    """Escaped human lines for a ``delivery_view``."""
    lines = [sp_journal._human("    delivery: %s" % view["state"])]
    for row in view["recipients"]:
        text = "    %s: %s" % (row["recipient"], row["state"])
        if "ack" in row:
            text += " (ACK %s)" % row["ack"]
        lines.append(sp_journal._human(text))
    return lines
