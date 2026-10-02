"""Buddy for the session-peers CLI."""

from __future__ import annotations
import json
import os
import shlex
import sys
import time
import uuid as uuidlib
from . import constants as sp_constants, rollout as sp_rollout, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, lifecycle as sp_lifecycle, requests as sp_requests, storage as sp_storage
from . import identity as sp_identity

class BuddyError(Exception):
    """A buddy lookup or direction that fails; carries the exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def _budget_target(value, args):
    """(thread uuid, None) for a budget command, or (None, (code, message))."""
    try:
        value = expand_buddy(value, args, "budget")
    except BuddyError as exc:
        return None, (exc.code, str(exc))
    if value.startswith("cc:"):
        return None, (2, "the reply budget belongs to a Codex thread's shim, not "
                         "a Claude session")
    if value.startswith("codex:"):
        value = value[len("codex:") :]
    try:
        return sp_codex.resolve_thread_prefer_live(value)["id"], None
    except sp_codex.ResolveError as exc:
        if not sp_runtime.is_uuid(value):
            return None, (1, str(exc))
        return value, None


def cmd_budget(args):
    if args.budget_cmd == "allow":
        return cmd_budget_allow(args)
    if args.budget_cmd != "reset":
        sys.stderr.write("error: budget subcommands are `reset` and `allow`\n")
        return 2
    tid, failure = _budget_target(args.thread, args)
    if failure:
        sys.stderr.write("error: %s\n" % failure[1])
        return failure[0]
    # An explicit reset drops any allowance, including one not yet consumed.
    sp_requests._unlink_quiet(sp_storage.budget_allow_path(tid))
    with open(sp_storage.budget_reset_path(tid), "w", encoding="utf-8") as fh:
        fh.write(sp_runtime.now_iso() + "\n")
    state_path = sp_storage.thread_state_path(tid)
    state = sp_runtime.read_json(state_path, None)
    if isinstance(state, dict) and (state.get("budgets") or state.get("allowance")):
        state["budgets"] = {}
        state["allowance"] = None
        sp_runtime.write_json_atomic(state_path, state, mode=0o600)
    print("reply budget reset for %s" % tid)
    return 0


def cmd_budget_allow(args):
    """Grant one requester a TOTAL reply allowance on one Codex thread."""
    if not 1 <= args.replies <= sp_constants.BUDGET_ALLOW_MAX:
        sys.stderr.write(
            "error: --replies must be between 1 and %d\n" % sp_constants.BUDGET_ALLOW_MAX
        )
        return 2
    sid = args.for_session
    if sid is None:
        try:
            caller = sp_identity.caller_identity(args)
        except ValueError:
            caller = None
        if caller is None or caller["kind"] != "cc":
            sys.stderr.write(
                "error: --for-session <uuid> is required when the caller is not "
                "a Claude session\n"
            )
            return 2
        sid = caller["uuid"]
    if not sp_runtime.is_uuid(sid):
        sys.stderr.write("error: --for-session must be a Claude session UUID\n")
        return 2
    tid, failure = _budget_target(args.thread, args)
    if failure:
        sys.stderr.write("error: %s\n" % failure[1])
        return failure[0]
    pid = sp_lifecycle.shim_pid(tid)
    if pid and not sp_lifecycle.shim_supports(tid, pid, "budget_allow"):
        sys.stderr.write(
            "error: cannot verify the running shim for %s (pid %d) supports "
            "allowances; a shim started from an older peers.py never reads the "
            "grant, so the cap would stay %d. Restart it: `"
            "peers.py restart %s` (keeps the budget and its spent replies), then "
            "grant again\n" % (tid, pid, sp_constants.REPLY_BUDGET, tid)
        )
        return 1
    path = sp_storage.budget_allow_path(tid)
    total = args.replies
    pending = sp_runtime.read_json(path, None)
    window = sp_runtime._float_env("SESSION_PEERS_REPLY_BUDGET_WINDOW", sp_constants.REPLY_BUDGET_WINDOW_DEFAULT)
    if window <= 0:
        window = sp_constants.REPLY_BUDGET_WINDOW_DEFAULT
    granted_at = sp_runtime.now_iso()
    if isinstance(pending, dict) and pending.get("sid") == sid:
        # Not yet consumed: two grants before the shim polls keep the higher,
        # with the higher's own time, so a merge never refreshes an old grant.
        previous = pending.get("total")
        previous_at = sp_runtime.parse_time(pending.get("at"))
        if (
            isinstance(previous, int)
            and not isinstance(previous, bool)
            and previous_at is not None
            and time.time() - previous_at <= window
            and min(previous, sp_constants.BUDGET_ALLOW_MAX) >= total
        ):
            total = min(previous, sp_constants.BUDGET_ALLOW_MAX)
            granted_at = pending["at"]
    sp_runtime.write_json_atomic(path, {"sid": sid, "total": total, "at": granted_at}, mode=0o600)
    print(
        "reply allowance for %s: up to %d consecutive replies to session %s "
        "(a total for this sequence; replies already delivered still count)"
        % (tid, max(sp_constants.REPLY_BUDGET, total), sid)
    )
    if not pid:
        print("no shim is running for %s; the grant applies once one starts" % tid)
    return 0


def read_buddy(owner):
    """The owner's buddy record, or None when absent or malformed."""
    rec = sp_runtime.read_json(sp_storage.buddy_path(owner), None)
    if not isinstance(rec, dict):
        return None
    buddy = rec.get("buddy")
    if (
        not isinstance(buddy, dict)
        or buddy.get("kind") not in sp_constants.BUDDY_KINDS
        or not sp_runtime.is_uuid(buddy.get("uuid"))
    ):
        return None
    return rec


def _parse_uses(value):
    if value is None:
        return list(sp_constants.BUDDY_USES)
    uses = []
    for word in value.split(","):
        word = word.strip()
        if word and word not in uses:
            uses.append(word)
    unknown = [word for word in uses if word not in sp_constants.BUDDY_USES]
    if unknown or not uses:
        raise ValueError(
            "unsupported --uses %s; supported: %s"
            % (", ".join(unknown) or "(empty)", ", ".join(sp_constants.BUDDY_USES))
        )
    return uses


def expand_buddy(value, args, purpose):
    """Turn `buddy`/`@buddy` into the caller's bound `cc:`/`codex:` UUID."""
    if value not in ("buddy", "@buddy"):
        return value
    try:
        owner = sp_identity.caller_identity(args)
    except ValueError as exc:
        raise BuddyError(str(exc), 2)
    rec = read_buddy(owner)
    if rec is None:
        raise BuddyError(
            "no buddy set for %s:%s; run `peers.py buddy set <name|uuid>`"
            % (owner["kind"], owner["uuid"])
        )
    buddy = rec["buddy"]
    if purpose in ("ask", "dispatch", "wait") and buddy["kind"] != "cc":
        raise BuddyError(
            "ask/dispatch/wait need a Claude buddy; use send (asynchronous)", 2
        )
    if purpose == "budget" and buddy["kind"] != "codex":
        raise BuddyError(
            "the reply budget belongs to a Codex thread's shim; this buddy is a "
            "Claude session", 2
        )
    return "%s:%s" % (buddy["kind"], buddy["uuid"])


def _claude_status(uuid):
    status = {"name": None, "live": None, "registered": None, "shim_pid": None,
              "status": None, "paused": None}
    records = [r for r in sp_claude.read_claude_records() if r.get("sessionId") == uuid]
    states = [(r, sp_claude.record_liveness(r)) for r in records]
    live = [r for r, state in states if state == "live"]
    if live:
        rec = live[0]
        status.update(live=True, name=rec.get("name"), status=rec.get("status"))
        if sp_claude.socket_path_ok(rec.get("messagingSocketPath")):
            status["route"] = "available"
        else:
            status["route"] = "unavailable: its socket is outside the allowlist"
    elif any(state == "unverified" for _r, state in states):
        status["name"] = states[0][0].get("name")
        status["route"] = "unavailable: liveness is unverified (process probe denied)"
    else:
        status["live"] = False
        status["route"] = "unavailable: no live Claude session with that id"
    return status


def _codex_status(uuid, attach):
    status = {"name": None, "live": None, "registered": None, "shim_pid": None,
              "status": None, "paused": None}
    try:
        thread = sp_codex.resolve_thread(uuid, require_live=False)
    except sp_codex.ResolveError as exc:
        status["route"] = "unavailable: %s" % exc
        return status
    status.update(
        name=thread.get("name"),
        live=thread.get("live"),
        registered=thread.get("registered"),
    )
    pid = sp_lifecycle.shim_pid(uuid)
    if attach and pid is None and thread.get("live") is True:
        # Transient attach only: `up` would also reset the reply budget,
        # release held replies and register the thread persistently.
        pid = sp_lifecycle.attach_thread(uuid, verbose=False)
    status["shim_pid"] = pid
    if pid:
        shim_rec = sp_runtime.read_json(os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % pid), None)
        if isinstance(shim_rec, dict) and shim_rec.get("sessionId") == uuid:
            status["status"] = shim_rec.get("status")
    rollout = thread.get("rollout_path")
    if rollout and os.access(rollout, os.R_OK):
        status["paused"] = sp_rollout.thread_is_paused(rollout)
    if thread.get("live") is None:
        status["route"] = "unavailable: liveness is unverified"
    elif not thread.get("live"):
        status["route"] = "unavailable: the thread is not live"
    elif not pid:
        status["route"] = "unavailable: no shim attached (`peers.py buddy ping` attaches one)"
    else:
        status["route"] = "available"
    return status


def buddy_status(buddy, attach=False):
    """Observed facts about a bound buddy; unknown values stay None."""
    if buddy["kind"] == "cc":
        status = _claude_status(buddy["uuid"])
    else:
        status = _codex_status(buddy["uuid"], attach)
    status["kind"] = buddy["kind"]
    status["uuid"] = buddy["uuid"]
    return status


def _buddy_line(rec, status):
    name = status.get("name") or rec["buddy"].get("name") or "(unnamed)"
    live = {True: "live", False: "not live", None: "liveness unknown"}[status.get("live")]
    parts = ["%s" % live]
    if status["kind"] == "codex":
        registered = status.get("registered")
        parts.append(
            "registered=%s"
            % ({True: "yes", False: "no"}.get(registered, "unknown"))
        )
        parts.append("shim=%s" % (status.get("shim_pid") or "none"))
    parts.append(status.get("status") or "status unknown")
    if status.get("paused"):
        parts.append("paused")
    route = status.get("route") or "unavailable: unknown"
    route_text = "route available" if route == "available" else "route %s" % route
    line = "buddy = %s (%s, %s) %s; %s; uses: %s" % (
        name,
        status["kind"],
        status["uuid"][:8],
        ", ".join(parts),
        route_text,
        ", ".join(rec.get("uses") or []),
    )
    left = _replies_left(rec)
    if left is not None:
        line += "; replies left: %d of %d" % (left, rec["replies"])
    elif _takes_reply_total(rec):
        line += (
            "; no buddy reply total recorded (default sequence cap is %d)"
            % sp_constants.REPLY_BUDGET
        )
    return line


def _takes_reply_total(rec):
    """True for the one binding kind a reply total applies to."""
    return rec["owner"]["kind"] == "cc" and rec["buddy"]["kind"] == "codex"


def _replies_left(rec):
    """Replies remaining on a binding's total, or None when it has none.

    Read from the shim's saved state once it has applied the grant; until then
    the whole total is pending.
    """
    total = rec.get("replies")
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        return None
    state = sp_runtime.read_json(sp_storage.thread_state_path(rec["buddy"]["uuid"]), None)
    binding = state.get("binding") if isinstance(state, dict) else None
    if (
        isinstance(binding, dict)
        and binding.get("bind_id") == rec.get("bind_id")
        and isinstance(binding.get("spent"), int)
    ):
        return max(0, binding.get("total", total) - binding["spent"])
    return total


def _print_buddy(args, rec, status):
    if getattr(args, "json", False):
        payload = dict(rec)
        payload["status"] = status
        left = _replies_left(rec)
        if left is not None:
            payload["replies_left"] = left
        if _takes_reply_total(rec):
            payload["reply_total_recorded"] = left is not None
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_buddy_line(rec, status))


def cmd_buddy(args):
    action = args.buddy_cmd or "show"
    try:
        owner = sp_identity.caller_identity(args)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    path = sp_storage.buddy_path(owner)
    if action == "clear":
        return _clear_buddy(owner, path)
    if action == "set":
        return _set_buddy(args, owner, path)
    rec = read_buddy(owner)
    if rec is None:
        sys.stderr.write("error: no buddy set; run `peers.py buddy set <name|uuid>`\n")
        return 1
    _print_buddy(args, rec, buddy_status(rec["buddy"], attach=(action == "ping")))
    return 0


def _clear_buddy(owner, path):
    try:
        with sp_storage.owner_lock(owner["uuid"]):
            old = read_buddy(owner)
            with sp_storage._binding_locks(_revocable_thread(owner, old)):
                # Revoke first: a failed revoke keeps the record, so clear
                # can be retried.
                try:
                    _revoke_binding(owner, old)
                except OSError as exc:
                    sys.stderr.write(
                        "error: could not revoke the reply total on %s (%s); "
                        "the buddy is still bound, retry `buddy clear`\n"
                        % (old["buddy"]["uuid"], exc)
                    )
                    return 1
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    print("no buddy was set")
                    return 0
    except sp_storage.BindingLockTimeout as exc:
        sys.stderr.write("error: %s; the buddy is still bound, retry\n" % exc)
        return 1
    print("buddy cleared")
    return 0


def _set_buddy(args, owner, path):
    try:
        uses = _parse_uses(args.uses)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    target = args.target
    if target is None:
        # No name given: look for a peer sharing this session's own name.
        try:
            target = sp_identity._resolve_typed_kind(owner["kind"], owner["uuid"]).get("name")
        except sp_codex.ResolveError:
            target = None
        if not target or sp_runtime.is_uuid(target):
            sys.stderr.write(
                "error: this session has no name to look up; name the buddy\n"
            )
            return 1
    try:
        buddy = sp_identity.resolve_typed(target, exclude=owner["uuid"])
    except sp_codex.ResolveNotFound as exc:
        if args.target is None:
            sys.stderr.write(
                "error: no other session is named %r; name the buddy\n" % target
            )
        else:
            sys.stderr.write("error: %s\n" % exc)
        return 1
    except sp_codex.ResolveAmbiguousKind as exc:
        sys.stderr.write("error: %s. Retry with one of:\n" % exc)
        for choice in exc.choices:
            sys.stderr.write("  %s\n" % _buddy_set_command(choice, args))
        return 1
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if buddy["kind"] == owner["kind"] and buddy["uuid"] == owner["uuid"]:
        sys.stderr.write("error: a session cannot be its own buddy\n")
        return 2
    # An explicit --replies must be honoured or the bind fails; the default
    # total for a Claude session's Codex buddy is best-effort (a warning).
    explicit = args.replies is not None
    grant = args.replies
    no_shim = False
    if not explicit and owner["kind"] == "cc" and buddy["kind"] == "codex":
        if sp_lifecycle.shim_pid(buddy["uuid"]):
            grant = sp_constants.BUDDY_REPLIES_DEFAULT
        else:
            no_shim = True
    if grant is not None:
        # An explicit grant attaches the shim when needed, so this runs
        # outside every lock; the default never attaches one.
        refusal = _check_buddy_replies(grant, owner, buddy, attach=explicit)
        if refusal and explicit:
            sys.stderr.write("error: %s\n" % refusal[1])
            return refusal[0]
        if refusal:
            sys.stderr.write(
                "warning: bound without the default reply total: %s\n" % refusal[1]
            )
            grant = None
    result = _bind_buddy_record(owner, buddy, uses, grant, explicit, path)
    if isinstance(result, int):
        return result
    rec = result
    if no_shim and _replies_left(rec) is None:
        # The default never attaches a shim, and the status line below may
        # show one that started meanwhile, so say the total is missing.
        sys.stderr.write(
            "warning: bound without the default reply total: no running shim "
            "for %s; once its shim runs, bind again with `peers.py buddy set "
            "codex:%s`\n" % (buddy["uuid"], buddy["uuid"])
        )
    _print_buddy(args, rec, buddy_status(buddy, attach=True))
    return 0


def _bind_buddy_record(owner, buddy, uses, grant, explicit, path):
    try:
        with sp_storage.owner_lock(owner["uuid"]):
            old = read_buddy(owner)
            same = (
                old is not None
                and old["buddy"]["kind"] == buddy["kind"]
                and old["buddy"]["uuid"] == buddy["uuid"]
            )
            kept, bind_id = None, None
            if same and old.get("bind_id") and isinstance(old.get("replies"), int):
                kept, bind_id = old["replies"], old["bind_id"]
            locked = set()
            if grant is not None:
                locked.add(buddy["uuid"])
            if not same and _revocable_thread(owner, old):
                locked.add(old["buddy"]["uuid"])
            with sp_storage._binding_locks(*locked):
                # The conflict check and every write below share the
                # locks, so a competing owner cannot slip between check
                # and publish.
                if grant is not None:
                    conflict = _binding_conflict(owner, buddy)
                    if conflict and explicit:
                        sys.stderr.write("error: %s\n" % conflict)
                        return 1
                    if conflict:
                        sys.stderr.write(
                            "warning: bound without the default reply total: %s\n"
                            % conflict
                        )
                        grant = None
                replies = kept
                if grant is not None:
                    replies = max(grant, kept or 0)
                    bind_id = bind_id or uuidlib.uuid4().hex
                rec = {
                    "owner": owner,
                    "buddy": buddy,
                    "uses": uses,
                    "set_at": sp_runtime.now_iso(),
                }
                if replies is not None:
                    rec["replies"] = replies
                    rec["bind_id"] = bind_id
                if not same:
                    try:
                        _revoke_binding(owner, old)
                    except OSError as exc:
                        sys.stderr.write(
                            "error: could not revoke the reply total on %s "
                            "(%s); the old buddy is still bound, retry\n"
                            % (old["buddy"]["uuid"], exc)
                        )
                        return 1
                # Record first, grant second: whatever fails in between,
                # no grant exists without a record that can revoke it, and
                # a retried set reuses the record's bind_id (so the shim
                # keeps the spent count).
                try:
                    sp_runtime.write_json_atomic(path, rec, mode=0o600)
                    if grant is not None:
                        sp_runtime.write_json_atomic(
                            sp_storage.budget_binding_path(buddy["uuid"]),
                            {
                                "sid": owner["uuid"],
                                "bind_id": bind_id,
                                "total": replies,
                                "at": sp_runtime.now_iso(),
                            },
                            mode=0o600,
                        )
                except OSError as exc:
                    sys.stderr.write(
                        "error: could not bind the buddy (%s); retry "
                        "`buddy set` (a retry keeps replies already spent)\n"
                        % exc
                    )
                    return 1
    except sp_storage.BindingLockTimeout as exc:
        sys.stderr.write("error: %s; nothing was changed, retry\n" % exc)
        return 1
    return rec


def _check_buddy_replies(replies, owner, buddy, attach=True):
    """None when ``buddy set --replies`` can be honoured, else (code, message).

    ``attach=False`` (the default total) uses only an already running shim.
    """
    if not 1 <= replies <= sp_constants.BUDDY_REPLIES_MAX:
        return 2, "--replies must be between 1 and %d" % sp_constants.BUDDY_REPLIES_MAX
    if owner["kind"] != "cc" or buddy["kind"] != "codex":
        return 2, (
            "--replies applies to a Claude session's Codex buddy only: the "
            "reply budget belongs to a Codex thread's shim"
        )
    # The shim is attached here, outside the binding lock: a shim takes that
    # lock itself when it reads a marker.
    pid = sp_lifecycle.shim_pid(buddy["uuid"])
    if not pid and attach:
        pid = sp_lifecycle.attach_thread(buddy["uuid"], verbose=False)
    if not pid:
        return 1, (
            "no running shim for %s, so a reply total cannot be granted; start "
            "it (`peers.py buddy ping`) and bind again" % buddy["uuid"]
        )
    if not sp_lifecycle.shim_supports(buddy["uuid"], pid, "binding_allowance"):
        return 1, (
            "cannot verify the running shim for %s (pid %d) supports buddy "
            "allowances; a shim started from an older peers.py never reads the "
            "grant, so the cap would stay %d. Restart it: `peers.py restart %s`, "
            "then bind again" % (buddy["uuid"], pid, sp_constants.REPLY_BUDGET, buddy["uuid"])
        )
    if replies > sp_constants.BUDGET_ALLOW_MAX and not sp_lifecycle.shim_supports(
        buddy["uuid"], pid, "binding_allowance_max500"
    ):
        return 1, (
            "the running shim for %s (pid %d) accepts a reply total of at most %d; "
            "restart it (`peers.py restart %s`) and bind again"
            % (buddy["uuid"], pid, sp_constants.BUDGET_ALLOW_MAX, buddy["uuid"])
        )
    return None


def _binding_conflict(owner, buddy):
    """A message when another session already holds a reply total on this thread.

    A shim keeps one binding per thread, so a second owner would overwrite the
    first one's spent count or revoke. Checked from the pending marker, the
    shim's saved binding and the other owners' buddy records. The caller holds
    the target thread's binding lock through this check and publication.
    """
    tid = buddy["uuid"]
    holders = set()
    marker = sp_runtime.read_json(sp_storage.budget_binding_path(tid), None)
    if isinstance(marker, dict) and isinstance(marker.get("sid"), str):
        holders.add(marker["sid"])
    state = sp_runtime.read_json(sp_storage.thread_state_path(tid), None)
    binding = state.get("binding") if isinstance(state, dict) else None
    if isinstance(binding, dict) and isinstance(binding.get("sid"), str):
        holders.add(binding["sid"])
    try:
        names = os.listdir(sp_storage.buddies_dir())
    except OSError:
        names = []
    for name in names:
        rec = sp_runtime.read_json(os.path.join(sp_storage.buddies_dir(), name), None)
        if not isinstance(rec, dict) or not rec.get("bind_id"):
            continue
        rec_buddy, rec_owner = rec.get("buddy"), rec.get("owner")
        if (
            isinstance(rec_buddy, dict)
            and rec_buddy.get("uuid") == tid
            and isinstance(rec_owner, dict)
            and isinstance(rec_owner.get("uuid"), str)
        ):
            holders.add(rec_owner["uuid"])
    holders.discard(owner["uuid"])
    if holders:
        return (
            "another session (%s) already holds a reply total on this Codex "
            "thread; a thread carries one at a time. It must `buddy clear` (or "
            "bind another buddy) first" % sorted(holders)[0]
        )
    return None


def _revocable_thread(owner, old):
    """The Codex thread holding ``old``'s reply total, or None when it has none."""
    if (
        old is None
        or not old.get("bind_id")
        or old["buddy"]["kind"] != "codex"
        or owner["kind"] != "cc"
    ):
        return None
    return old["buddy"]["uuid"]


def _revoke_binding(owner, old):
    """Revoke the reply total a replaced or cleared binding granted.

    Raises OSError when the revoke marker cannot be written, so the caller
    keeps the record and the revocation can be retried.
    """
    if _revocable_thread(owner, old) is None:
        return
    sp_runtime.write_json_atomic(
        sp_storage.budget_binding_path(old["buddy"]["uuid"]),
        {
            "sid": owner["uuid"],
            "bind_id": old["bind_id"],
            "revoke": True,
            "at": sp_runtime.now_iso(),
        },
        mode=0o600,
    )


def _buddy_set_command(target, args):
    """The `buddy set` command line for ``target``, keeping the user's options."""
    argv = ["peers.py", "buddy", "set", target]
    if getattr(args, "uses", None) is not None:
        argv += ["--uses", args.uses]
    if getattr(args, "replies", None) is not None:
        argv += ["--replies", str(args.replies)]
    if getattr(args, "as_identity", None):
        argv += ["--as", args.as_identity]
    return " ".join(shlex.quote(part) for part in argv)
