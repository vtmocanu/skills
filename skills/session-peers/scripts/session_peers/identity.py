"""Identity for the session-peers CLI."""

from __future__ import annotations
import os
from . import constants as sp_constants, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, opencode as sp_opencode

def _thread_from_args(args, required=False):
    explicit = getattr(args, "from_thread", None)
    value = explicit or os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "CODEX_SESSION_ID"
    )
    if value and not sp_runtime.is_uuid(value):
        if explicit or required:
            raise ValueError("--from-thread/CODEX_THREAD_ID must be a UUID")
        return None
    if required and not value:
        raise ValueError(
            "a request needs --from-thread or CODEX_THREAD_ID so its origin is explicit"
        )
    return value


# --------------------------------------------------------------------------
# Buddies: one bound peer per session
# --------------------------------------------------------------------------


def valid_ident(kind, ident) -> bool:
    """A typed identity's id: a UUID, or a `ses_` id for an OpenCode session.

    The `uuid` key of a typed identity carries the OpenCode session id as
    issued; no UUID is derived for it.
    """
    if kind == "oc":
        return sp_opencode.is_session_id(ident)
    return kind in sp_constants.BUDDY_KINDS and sp_runtime.is_uuid(ident)


def parse_typed(value):
    """`cc:<uuid>` / `codex:<uuid>` / `oc:<ses_id>` into a typed identity, or ValueError."""
    kind, sep, ident = str(value or "").partition(":")
    if not sep or not valid_ident(kind, ident):
        raise ValueError("expected cc:<uuid>, codex:<uuid> or oc:<ses_id>, got %r" % value)
    return {"kind": kind, "uuid": ident}


def caller_identity(args):
    """The typed identity of the session running this command."""
    explicit = getattr(args, "as_identity", None)
    if explicit:
        return parse_typed(explicit)
    oc_sid = sp_opencode.current_session_id()
    if oc_sid:
        # OpenCode exports its id into every shell command; a Claude or Codex
        # id beside it was inherited from whichever agent launched the other,
        # and which one runs this command cannot be told apart.
        if sp_claude._current_claude_session_id() or _thread_from_args(args):
            raise ValueError(
                "this environment names OpenCode session %s and a Claude or Codex "
                "session; pass --as" % oc_sid
            )
        try:
            sp_opencode.get_session(oc_sid)
        except sp_opencode.OpenCodeError as exc:
            raise ValueError("cannot verify OpenCode session %s: %s" % (oc_sid, exc))
        return {"kind": "oc", "uuid": oc_sid}
    sid = sp_claude._current_claude_session_id()
    if sid:
        if not sp_runtime.is_uuid(sid):
            raise ValueError("this Claude session's id %r is not a UUID" % sid)
        return {"kind": "cc", "uuid": sid}
    tid = _thread_from_args(args)
    if tid:
        return {"kind": "codex", "uuid": tid}
    raise ValueError("cannot tell which session is asking; pass --as")


def _resolve_typed_kind(kind, target, live_only=False, exclude=None):
    if kind == "oc":
        session = sp_opencode.resolve_session(target, exclude=exclude)
        return {"kind": "oc", "uuid": session["id"], "name": session.get("title")}
    if kind == "cc":
        rec = sp_claude._resolve_claude_record(target, exclude=exclude)
        sid = rec.get("sessionId")
        if not sp_runtime.is_uuid(sid):
            # A bound buddy is addressed by UUID only; a non-UUID id would be
            # re-read as a name later.
            raise sp_codex.ResolveError("Claude session %r has no UUID session id" % target)
        if rec.get("entrypoint") == "codex":
            # A Codex shim registers itself as a Claude peer so Claude can
            # reach it, but the peer is a Codex thread: bind it as one so the
            # reply budget and Codex routing apply.
            return {"kind": "codex", "uuid": sid, "name": rec.get("name")}
        return {"kind": "cc", "uuid": sid, "name": rec.get("name")}
    # Codex reuses titles, so a live thread usually shares its name with dead
    # ones: the live match wins, and dead threads count only when none is live.
    if live_only:
        thread = sp_codex.resolve_thread(target, require_live=True, exclude=exclude)
    else:
        thread = sp_codex.resolve_thread_prefer_live(target, exclude=exclude)
    return {"kind": "codex", "uuid": thread["id"], "name": thread.get("name")}


def resolve_typed(target, exclude=None):
    """`cc:x`, `codex:x`, `oc:x` or a bare `[@]x` into one typed identity with its name.

    Names are resolved here, once; a bound buddy is used by UUID afterwards.
    ``exclude`` (the caller's UUID) keeps the caller out of NAME matching, so
    a session never competes with its own namesake.
    """
    target = str(target or "")
    if target.startswith("@"):
        target = target[1:]
    for kind in sp_constants.BUDDY_KINDS:
        if target.startswith(kind + ":"):
            return _resolve_typed_kind(
                kind, target[len(kind) + 1 :], exclude=exclude
            )
    if not target:
        raise sp_codex.ResolveError("an empty target names no session")
    found, errors, dead_codex = [], [], False
    for kind in sp_constants.BUDDY_KINDS:
        try:
            # Live Codex threads only here: a dead namesake must not compete
            # with a live Claude session for the same bare name.
            found.append(
                _resolve_typed_kind(kind, target, live_only=True, exclude=exclude)
            )
        except sp_codex.ResolveNotFound:
            pass
        except sp_codex.ResolveNoLive:
            dead_codex = True
        except sp_codex.ResolveError as exc:
            errors.append(exc)
    if dead_codex and not found and not errors:
        # Nothing live carries the name: bind the dead thread as `codex:` would.
        return _resolve_typed_kind("codex", target, exclude=exclude)
    if errors:
        # An ambiguous or unverifiable side could be the one meant: never guess.
        raise sp_codex.ResolveAmbiguousKind(
            "%s; pick the kind" % errors[0],
            ["%s:%s" % (kind, target) for kind in sp_constants.BUDDY_KINDS],
        )
    unique = []
    for item in found:
        if not any(i["kind"] == item["kind"] and i["uuid"] == item["uuid"] for i in unique):
            unique.append(item)
    found = unique
    if len(found) > 1:
        # Routine for an attached Codex thread: its UUID also names its shim's
        # Claude-facing registry record.
        choices = ["%s:%s" % (i["kind"], i["uuid"]) for i in found]
        raise sp_codex.ResolveAmbiguousKind(
            "%r matches both %s; pick one" % (target, " and ".join(choices)),
            choices,
        )
    if not found:
        raise sp_codex.ResolveNotFound(
            "no Claude session, Codex thread or OpenCode session named %r; run "
            "`peers.py list`" % target
        )
    return found[0]
