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
import json
import os
import sys
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, identity as sp_identity
from . import journal as sp_journal, opencode as sp_opencode, topics as sp_topics

EVENT_CREATED = "channel.created"
EVENT_JOINED = "channel.joined"
EVENT_LEFT = "channel.left"
EVENT_REMOVED = "channel.removed"

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

    Names: after_membership_before_history.
    """


# --------------------------------------------------------------------------
# Paths and locks
# --------------------------------------------------------------------------


def channels_root():
    """The channel store, derived from HOME only (never CODEX_HOME)."""
    return os.path.join(os.path.expanduser("~/.codex"), "session-peers", "channels")


def history_dir():
    return os.path.join(channels_root(), "history")


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
    print("removed %s from #%s" % (sp_journal._human(removed[0] if removed else target), key))
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
    if args.json:
        print(json.dumps(dict(result, channel=key), ensure_ascii=False))
        return 0
    gap = result["gap"]
    if gap:
        print("gap: entries %d..%d pruned" % (gap["from"], gap["to"]))
    for entry in result["entries"]:
        print(describe_entry(entry))
    print("next: --since %d" % result["next"])
    return 0
