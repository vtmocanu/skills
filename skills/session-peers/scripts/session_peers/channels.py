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
import contextlib
import copy
import fcntl
import io
import json
import os
import sys
import time
import uuid as uuidlib
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, identity as sp_identity
from . import journal as sp_journal, opencode as sp_opencode, topics as sp_topics

EVENT_CREATED = "channel.created"
EVENT_JOINED = "channel.joined"
EVENT_LEFT = "channel.left"
EVENT_REMOVED = "channel.removed"
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

# Deliveries to Claude targets are at least this far apart (seconds). Tests
# patch ``_clock`` and ``_sleep``; nothing else sleeps.
PACE_SECONDS = 1.0
_clock = time.monotonic
_sleep = time.sleep

# A leading dot keeps the store lock out of the key namespace: no channel
# name can produce it, so ``<key>.lock`` never aliases it.
STORE_LOCK = ".channels.lock"


class ChannelError(Exception):
    """A refused channel operation; carries the exit code."""

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def _failpoint(name):
    """No-op crash seam. Tests patch it to fail or exit at ``name``.

    Names: after_membership_before_history, send_after_journal, send_after_lock,
    send_after_history, send_before_outcome.
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
    """The membership record of ``key``, or None."""
    rec = sp_runtime.read_json(_record_path(key), None)
    if not isinstance(rec, dict) or rec.get("key") != key or not isinstance(rec.get("members"), list):
        return None
    return rec


def list_keys():
    try:
        names = sorted(os.listdir(channels_root()))
    except OSError:
        return []
    keys = []
    for name in names:
        if name.endswith(".json") and sp_constants.CHANNEL_NAME_RE.fullmatch(name[:-5]):
            keys.append(name[:-5])
    return keys


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


def _actor_identity(typed):
    parsed = sp_identity.parse_typed(typed)
    return {"kind": parsed["kind"], "uuid": parsed["uuid"]}


def append_history(key, kind, actor, data=None):
    """Append one history entry to ``key``'s topic and return it.

    ``actor`` is a typed id. Raises ChannelError(code 1) when the append fails.
    """
    _ensure_dir(history_dir())
    try:
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
    for key in list_keys():
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
    matches = [r for r in records if str(r.get("sessionId")).lower() == sid.lower()]
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


def _cmd_join(args, me):
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
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no channels")
    for row in rows:
        print("#%s  %s  %d members  %s  owner %s" % (
            row["name"], row["kind"], row["members"], row["you"],
            sp_journal._human(row["owner"]),
        ))
    return 0


def _cmd_members(args, me):
    rec = _require(stored_key(args.name))
    cache = {}
    rows = [
        {"member": m, "owner": m == rec["owner"], "liveness": member_liveness(m, cache)}
        for m in rec["members"]
    ]
    if args.json:
        print(json.dumps({"channel": rec["key"], "display": rec.get("display"), "members": rows},
                         ensure_ascii=False))
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
    elif kind == EVENT_OUTCOME:
        summary = "outcomes of message %s for %d recipients" % (
            data.get("message_id"), len(data.get("outcomes") or {}))
    elif isinstance(data.get("body"), str):
        summary = data["body"]
    else:
        summary = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return "#%d %s %s [%s]: %s" % (
        entry["seq"], entry.get("ts"), sp_journal._human(who), sp_journal._human(kind),
        sp_journal._human(summary),
    )


def _cmd_tail(args, me):
    key = stored_key(args.name)
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
        print(json.dumps(dict(result, entries=entries, channel=key), ensure_ascii=False))
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

    The route check's channel branch. ``#general`` is refused until it is wired in.
    """
    name = parse_name(value)
    if name == sp_constants.CHANNEL_GENERAL:
        raise ChannelError("posting to #%s is not available yet" % name)
    if read_channel(name) is None:
        raise ChannelError("no channel named #%s; run `peers.py channel list`" % name)
    return name


def _members_for(key, sender):
    """``(members, opencode_coverage)`` a send by ``sender`` to ``key`` addresses.

    A stored channel needs the sender among its members. Derived ``#general``
    membership plugs in here.
    """
    rec = read_channel(key)
    if rec is None:
        raise ChannelError("no channel named #%s; run `peers.py channel list`" % key)
    if sender not in rec["members"]:
        raise ChannelError(
            "%s is not a member of #%s; only members can send to it (`peers.py channel join %s`)"
            % (sp_journal._human(sender), key, key)
        )
    return list(rec["members"]), None


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
    field = "n" * sp_constants.MAX_TAG_FIELD_CHARS
    framing = "\n".join([
        sp_protocol.build_tag(field, field, "/" + "p" * 107, field),
        sp_protocol.build_origin("opencode", field, field),
        sp_protocol.reply_route_hint("oc:" + "s" * 40, "m" * 36),
    ])
    try:
        sp_claude.check_wrapped_size(
            sp_protocol.build_wrapper(framing + "\n" + longest, "/" + "s" * 107, field, field)
        )
    except ValueError as exc:
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
    out, err = io.StringIO(), io.StringIO()
    rc = 1
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
    members, coverage = _members_for(key, sender)
    body = args.message
    journaled = bool(getattr(args, "record", False))
    snapshot = [m for m in members if m != sender]

    cache = {}
    gone = [m for m in snapshot if member_liveness(m, cache) == "gone"]
    live = [m for m in snapshot if m not in gone]
    mid = str(uuidlib.uuid4())
    _check_size(key, mid, sender, live, body, journaled)

    deliver, excluded = list(live), []
    if journaled:
        deliver, excluded = _journal_preflight(live)
        if not deliver:
            detail = "; ".join("%s: %s" % (m, r) for m, r in excluded[:3])
            raise ChannelError(
                "no recipient of this message can be recorded for ACK%s; nothing was recorded or sent"
                % (" (%s)" % detail if detail else ""), 1)
        try:
            sp_journal.record_message(sender, deliver, body, message_id=mid)
        except sp_journal.JournalError as exc:
            return sp_journal.presend_failed(exc, as_json)
        _failpoint("send_after_journal")

    delivery = "fanout" if snapshot else "none"
    data = {
        "sender": sender, "body": body, "message_id": mid, "journaled": journaled,
        "delivery": delivery, "recipients": list(snapshot),
        "excluded": [{"member": m, "reason": r} for m, r in excluded],
        "opencode_coverage": coverage,
    }
    lock = None
    try:
        try:
            if delivery == "fanout":
                # The lock is held before the history entry is published, so a reader
                # that sees the entry finds the sender holding it.
                lock = _SendLock(mid)
                _failpoint("send_after_lock")
            append_history(key, EVENT_SEND, sender, data)
        except (ChannelError, OSError) as exc:
            if lock is not None:
                lock.release()
            raise ChannelError(
                "%s; message %s was %s" % (
                    exc, mid, "journaled, not delivered" if journaled else "not delivered"), 1)
        payload = {"status": "recorded", "channel": key, "message_id": mid, "outcomes": {}}
        if journaled:
            payload["journal"] = {"message_id": mid, "recorded": True}
        if coverage is not None:
            payload["opencode_coverage"] = coverage
        if delivery == "none":
            return _report(payload, 0, as_json, journaled, "recorded in #%s (no other members)" % key)
        return _fan_out(args, lock, payload, as_json, (key, sender, mid, body, journaled),
                        (snapshot, gone, deliver, excluded))
    finally:
        if lock is not None:
            # Only a crash or an unexpected exception gets here with the lock
            # still held: drop it and leave the file, as a dead sender would.
            lock.abandon()


def _fan_out(args, lock, payload, as_json, send, members):
    """Deliver serially, record failures per member, append the one outcome entry, then release the lock."""
    key, sender, mid, body, journaled = send
    snapshot, gone, deliver, excluded = members
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
        text = _delivery_text(key, mid, sender, member, body, journaled)
        outcomes[member] = _deliver_member(args, member, sender, mid, text, snap)
        if is_claude:
            last_claude = _clock()
        if journaled and outcomes[member].startswith(OUTCOME_REFUSED):
            # transport_failed returns its ``rc`` (0 here) only when the append worked.
            if sp_journal.transport_failed(mid, member, outcomes[member][len(OUTCOME_REFUSED):], rc=0):
                update_failed = True

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


def _report(payload, rc, as_json, journaled, headline):
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return rc
    print(headline)
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
    pid is read. The probe never keeps the lock.
    """
    try:
        fd = os.open(send_lock_path(message_id), os.O_RDONLY)
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        return "error"
    try:
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


def _send_state(key, entry, message_id):
    """``(state, outcomes)`` of a fan-out: ``complete`` with its outcomes, else ``in progress``, ``unknown`` or ``unverified``."""
    try:
        found = _find_outcome(key, message_id, entry["seq"])
        if found is not None:
            return "complete", _outcomes_of(found)
        probe = _probe_send_lock(message_id)
        if probe == "held":
            return STATE_IN_PROGRESS, None
        if probe == "error":
            return STATE_UNVERIFIED, None
        # The lock was free or already removed: the outcome may have landed in between.
        found = _find_outcome(key, message_id, entry["seq"])
        if found is not None:
            return "complete", _outcomes_of(found)
        return STATE_UNKNOWN, None
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


def delivery_view(key, entry, journal_cache=None):
    """The reader's view of one ``channel.send`` entry.

    ``{"state", "recipients": [{"recipient", "state", "ack"?}]}``. ``state`` is
    ``recorded`` (nothing to deliver), ``complete``, ``in progress``,
    ``unknown`` or ``unverified``; a recipient's state is its recorded outcome
    (or ``acked``), else the send's.
    """
    journal_cache = {} if journal_cache is None else journal_cache
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    recipients = [r for r in data.get("recipients") or [] if isinstance(r, str)]
    refused = {
        x.get("member"): str(x.get("reason"))
        for x in data.get("excluded") or [] if isinstance(x, dict)
    }
    mid = data.get("message_id")
    outcomes = {}
    if data.get("delivery") == "none":
        state = OUTCOME_RECORDED
    else:
        state, outcomes = _send_state(key, entry, mid)
        outcomes = outcomes or {}
    acks = _ack_states(mid, journal_cache) if data.get("journaled") else None
    rows = []
    for recipient in recipients:
        if recipient in refused:
            row_state = OUTCOME_REFUSED + refused[recipient]
        elif state == "complete":
            row_state = outcomes.get(recipient, STATE_UNKNOWN)
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
            if row["ack"] == STATE_ACKED:
                row["state"] = STATE_ACKED
        rows.append(row)
    return {"state": state, "recipients": rows}


def describe_view(view):
    """Escaped human lines for a ``delivery_view``."""
    lines = [sp_journal._human("    delivery: %s" % view["state"])]
    for row in view["recipients"]:
        text = "    %s: %s" % (row["recipient"], row["state"])
        if "ack" in row:
            text += " (ACK %s)" % row["ack"]
        lines.append(sp_journal._human(text))
    return lines
