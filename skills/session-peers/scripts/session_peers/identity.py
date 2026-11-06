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
        if os.environ.get("CLAUDE_CODE_SESSION_ID"):
            raise ValueError(
                "this is a Claude Code session; use SendMessage for a Claude "
                "peer instead of a Codex origin"
            )
        raise ValueError(
            "a Codex sender needs --from-thread or CODEX_THREAD_ID so its origin is explicit"
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
    found, errors, dead_codex, ambiguous = [], [], False, False
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
        except sp_codex.ResolveAmbiguous as exc:
            errors.append(exc)
            ambiguous = True
        except sp_codex.ResolveError as exc:
            errors.append(exc)
    if dead_codex and not found and not errors:
        # Nothing live carries the name: bind the dead thread as `codex:` would.
        return _resolve_typed_kind("codex", target, exclude=exclude)
    if errors:
        # An ambiguous or unverifiable side could be the one meant: never guess.
        choices = ["%s:%s" % (kind, target) for kind in sp_constants.BUDDY_KINDS]
        if ambiguous:
            raise sp_codex.ResolveAmbiguousKind(
                "%r matches several peers; pick one" % target, choices, ambiguous=True
            )
        raise sp_codex.ResolveAmbiguousKind("%s; pick the kind" % errors[0], choices)
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
            ambiguous=True,
        )
    if not found:
        raise sp_codex.ResolveNotFound(
            "no Claude session, Codex thread or OpenCode session named %r; run "
            "`peers.py list`" % target
        )
    return found[0]


# --------------------------------------------------------------------------
# Candidate listings for an ambiguous name
# --------------------------------------------------------------------------

_LABELS = {"cc": "Claude session", "codex": "Codex thread", "oc": "OpenCode session"}


def display_id(kind, ident):
    """``ident`` as printed in advice: raw for a well-formed id, else ``%r``-quoted."""
    ok = sp_opencode.is_session_id(ident) if kind == "oc" else sp_runtime.is_uuid(ident)
    return str(ident) if ok and "\n" not in str(ident) else repr(ident)


def dead_only_duplicates(name, exclude=None):
    """The threads carrying ``name`` when there are two or more and none is live.

    `resolve_thread` reports that case as "no live thread", which hides that
    the name is also ambiguous among the past threads.
    """
    matches = sp_codex.matching_threads(name, exclude=exclude)
    if len(matches) >= 2 and all(t.get("live") is False for t in matches):
        return matches
    return []


def _thread_liveness(thread):
    live = thread.get("live")
    return "live" if live is True else "dead" if live is False else "unverified"


def describe_candidates(name, kinds=None, exclude=None):
    """One line per session ``name`` could mean: typed ID, kind, liveness.

    Live first, then unverified, then dead, newest first inside each group.
    Nothing is capped: the caller needs every typed ID to pick one. A Codex
    shim's registry record is the same peer as its thread and merges into the
    `codex:<uuid>` line.
    """
    kinds = tuple(kinds) if kinds else ("cc", "codex", "oc")
    rows = {}  # typed id -> (kind label, liveness, rank, recency)
    ranks = {"live": 0, "recent": 1, "unverified": 2, "dead": 3}
    if "codex" in kinds:
        for thread in sp_codex.matching_threads(name, exclude=exclude):
            state = _thread_liveness(thread)
            when = sp_runtime.parse_time(thread.get("updated_at")) or 0
            rows["codex:%s" % display_id("codex", thread["id"])] = (_LABELS["codex"], state, ranks[state], when)
    if "cc" in kinds:
        for rec in sp_claude.claude_record_by_target(name, sp_claude.read_claude_records()):
            sid = rec.get("sessionId")
            state = sp_claude.record_liveness(rec)
            when = sp_runtime.parse_time(rec.get("startedAt")) or 0
            shim = rec.get("entrypoint") == "codex" and sp_runtime.is_uuid(sid)
            typed = "%s:%s" % ("codex" if shim else "cc", display_id("cc", sid))
            if typed in rows:
                continue
            rows[typed] = (_LABELS["codex" if shim else "cc"], state, ranks[state], when)
    if "oc" in kinds:
        try:
            sessions = [s for s in sp_opencode.recent_sessions() if s["title"] == name]
            try:
                active = sp_opencode.active_ids()
            except sp_opencode.OpenCodeError:
                active = None
        except sp_opencode.OpenCodeError:
            sessions, active = [], None
        for session in sessions:
            state = "unknown" if active is None else sp_opencode.state_of(session["id"], active)
            when = (session.get("updated_ms") or 0) / 1000.0
            rows["oc:%s" % display_id("oc", session["id"])] = (_LABELS["oc"], "recent (%s)" % state, ranks["recent"], when)
    ordered = sorted(rows.items(), key=lambda item: (item[1][2], -item[1][3], item[0]))
    return ["  %s  %s  %s" % (typed, label, state) for typed, (label, state, _r, _w) in ordered]


def ambiguity_lines(exc, name, exclude=None):
    """Candidate lines for an ambiguous-name error or a dead-only duplicate, else []."""
    if isinstance(exc, sp_codex.ResolveAmbiguous):
        return describe_candidates(exc.name or name, kinds=(exc.kind,) if exc.kind else None, exclude=exclude)
    if isinstance(exc, sp_codex.ResolveNoLive):
        # The caller's target may still carry `@` or a `codex:` prefix.
        name = str(name or "").lstrip("@")
        if name.startswith(("codex:", "cc:", "oc:")):
            name = name.partition(":")[2]
        if dead_only_duplicates(name, exclude):
            return describe_candidates(name, kinds=("codex",), exclude=exclude)
    return []


def ambiguity_report(exc, name, exclude=None):
    """`pick one typed ID:` and the candidate lines to print after ``exc``, or []."""
    lines = ambiguity_lines(exc, name, exclude)
    return ["pick one typed ID:"] + lines if lines else []
