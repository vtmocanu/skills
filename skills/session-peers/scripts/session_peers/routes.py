"""Typed route checks for the session-peers CLI.

`check` runs before send/ask/dispatch/wait deliver: it resolves the target to a
typed identity, then allows the route or refuses it with the working command
(or the reason none exists). `verify` runs at delivery and refuses when the
target it resolved is not the one `check` approved.
"""

from __future__ import annotations
import collections
import os
import shlex
import sys
from . import protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, identity as sp_identity, opencode as sp_opencode, requests as sp_requests

KINDS = ("cc", "codex", "oc")
LABELS = {"cc": "Claude Code session", "codex": "Codex thread", "oc": "OpenCode session"}
_RUNTIME_KIND = {"claude": "cc", "codex": "codex", "opencode": "oc"}
REQUESTS = ("ask", "dispatch")
# Never matched by `verify`: the existing prefix check refuses before delivery.
UNCHECKED = "unchecked-prefix"

Sender = collections.namedtuple("Sender", "kind uuid")
# via: the prefix the user typed (or None for a bare name); runtime: claude, codex or opencode.
Target = collections.namedtuple("Target", "via ident runtime name rec")

_MIXED_UUID = (
    "error: --from-thread/CODEX_THREAD_ID must be a UUID; this environment also "
    "names a Claude Code session, so the sender cannot be attributed. Nothing was sent."
)
_OPTIONS = (
    ("message_file", "--message-file", False), ("from_thread", "--from-thread", False),
    ("from_name", "--from-name", False), ("from_sid", "--from-sid", False),
    ("from_socket", "--from-socket", False), ("timeout", "--timeout", False),
    ("state", "--state", False), ("json", "--json", True), ("wake", "--wake", True),
    ("allow_reply_wake", "--allow-reply-wake", False), ("in_reply_to", "--in-reply-to", False),
)


class _Refusal(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code
        self.text = text


def record_runtime(rec):
    """`opencode`, `codex` (a shim's registry record) or `claude`."""
    if rec.get("_runtime") == "opencode":
        return "opencode"
    if rec.get("entrypoint") == "codex" and sp_runtime.is_uuid(rec.get("sessionId")):
        return "codex"
    return "claude"


def _typed(runtime, ident):
    return "%s:%s" % (_RUNTIME_KIND[runtime], ident)


def _claude_indicator():
    return bool(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))


def sender_kind(args):
    """The runtime running this command, as a Sender; raises _Refusal when unattributable.

    "unknown" is an OpenCode id beside a Claude or Codex one: delivery refuses it.
    """
    claude = _claude_indicator()
    explicit = getattr(args, "from_thread", None)
    raw = explicit or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    valid = raw if sp_runtime.is_uuid(raw) else None
    if raw and not valid and claude:
        raise _Refusal(2, _MIXED_UUID)
    flags = any(getattr(args, name, None) for name in ("from_thread", "from_name", "from_sid", "from_socket"))
    oc_sid = sp_opencode.current_session_id()
    if oc_sid and not flags and (claude or valid):
        # The existing mixed-identity errors refuse it before delivery.
        return Sender("unknown", None)
    if valid:
        return Sender("codex", valid)
    if oc_sid and not flags:
        return Sender("oc", oc_sid)
    if claude:
        return Sender("cc", sp_claude._current_claude_session_id())
    return Sender("shell", None)


def _claudeish(sender):
    return sender.kind == "cc"


def _options(args, verb):
    out = []
    note = False
    if verb != "wait" and getattr(args, "message_file", None) is None and getattr(args, "message", None) is not None:
        out.append("--message-file <PATH>")
        note = True
    for attr, flag, boolean in _OPTIONS:
        value = getattr(args, attr, None)
        if value is None or value is False:
            continue
        if boolean:
            out.append(flag)
        elif isinstance(value, float):
            out.append("%s %g" % (flag, value))
        else:
            out.append("%s %s" % (flag, shlex.quote(str(value))))
    return out, note


def _wait_flag(verb):
    return "--for" if verb == "wait" else "--to"


def _run_lines(args, verb, typed, extra=()):
    """The indented commands to run: ``extra`` first, then the verb with the other options kept."""
    options, note = _options(args, verb)
    lines = list(extra) + [" ".join(["peers.py", verb, _wait_flag(verb), typed] + options)]
    if note:
        lines.append("(write the message to PATH)")
    return "\n".join("  " + line for line in lines)


# --------------------------------------------------------------------------
# Advice
# --------------------------------------------------------------------------


def _request_to_codex(verb, sender, ident):
    text = (
        "error: %s cannot reach Codex thread codex:%s: correlated requests reach "
        "only Claude Code and OpenCode peers." % (verb, ident)
    )
    if _claudeish(sender):
        text += " A correlated Claude-to-Codex request does not exist yet (#88)."
    return text + " send is asynchronous and is not an equivalent: it has no request id, single-use reply or await."


def _shim_running(ident):
    return any(
        rec.get("sessionId") == ident and rec.get("entrypoint") == "codex"
        and sp_claude.record_liveness(rec) == "live"
        for rec in sp_claude.read_claude_records()
    )


def _wait_for_codex(args, ident):
    head = "error: wait reads a Claude-registry status; Codex thread codex:%s " % ident
    if _shim_running(ident):
        return head + "publishes it through its shim. Run:\n" + _run_lines(args, "wait", "cc:" + ident)
    try:
        thread = sp_codex.resolve_thread(ident, require_live=False)
    except sp_codex.ResolveError as exc:
        raise _Refusal(1, "error: %s" % exc)
    if thread.get("live") is True:
        return head + "has no running shim, so no status is published. Run:\n" + _run_lines(
            args, "wait", "cc:" + ident, extra=("peers.py restart %s" % ident,)
        )
    return head + "is not live, so no status can be published; there is no equivalent."


def _verified_name(rec):
    """(name or None, reason): a SendMessage name proven unique, else why not.

    reason is "absent" (no usable name), "duplicate", or an incomplete-read /
    unverifiable-contender detail.
    """
    name = rec.get("name")
    if not sp_protocol.valid_peer_name(name):
        return None, "absent"
    records, error = sp_claude.read_claude_records_checked()
    if error is not None:
        return None, error
    if sp_claude.record_liveness(rec) != "live":
        return None, "absent"
    unverified = False
    for other in records:
        if other.get("name") != name or (other.get("pid") == rec.get("pid") and other.get("sessionId") == rec.get("sessionId")):
            continue
        state = sp_claude.record_liveness(other)
        if state == "live":
            return None, "duplicate"
        if state == "unverified":
            unverified = True
    if unverified:
        return None, "another session named %r cannot be checked for liveness" % name
    return name, None


def _claude_to_claude(verb, rec):
    sid = rec.get("sessionId")
    name, reason = _verified_name(rec)
    label = rec.get("name") or "cc:%s" % sid
    if name:
        how = 'Use SendMessage with to: "%s".' % name
    elif reason in ("absent", "duplicate"):
        how = (
            "It has no unique verified session name, so SendMessage cannot be "
            "addressed safely: have it /rename to a unique name, or pick it in ListAgents."
        )
    else:
        how = (
            "Its session name cannot be verified as unique (%s), so SendMessage "
            "cannot be addressed safely: pick it in ListAgents or have it /rename "
            "to a unique name." % reason
        )
    text = (
        "error: this Claude Code session must reach Claude Code session %s with "
        "native SendMessage, not peers.py. %s Typed ID cc:%s is for "
        "disambiguation only; do not pass it to SendMessage." % (label, how, sid)
    )
    if verb in REQUESTS:
        text += " A correlated ask has no Claude-to-Claude equivalent; SendMessage is asynchronous."
    return text


def _verdict(args, verb, sender, tgt):
    """The refusal text for this sender, verb and target, or None when allowed."""
    if tgt.runtime == "codex":
        if verb in REQUESTS:
            return _request_to_codex(verb, sender, tgt.ident)
        if verb == "wait" and tgt.via != "cc":
            return _wait_for_codex(args, tgt.ident)
    elif tgt.runtime == "claude" and verb != "wait" and _claudeish(sender):
        return _claude_to_claude(verb, tgt.rec)
    return None


def _liveness(tgt):
    """The target's liveness label for the "needs a typed target" advice."""
    if tgt.runtime == "claude":
        return "live"
    if tgt.runtime == "opencode":
        return "recent, %s" % sp_opencode.state_of(tgt.ident)
    try:
        live = sp_codex.resolve_thread(tgt.ident, require_live=False).get("live")
    except sp_codex.ResolveError:
        live = None
    return "live" if live is True else "dead" if live is False else "unverified"


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def _failed(exc, name, exclude=None):
    """The first-lookup failure as delivery would print it, plus any candidates."""
    lines = sp_identity.ambiguity_report(exc, name, exclude)
    return _Refusal(1, "\n".join(["error: %s" % exc] + lines))


def _resolve_bare(args, verb, value, sender):
    name = value.lstrip("@")
    exclude = sender.uuid
    try:
        found = sp_identity.resolve_typed(name, exclude=exclude)
    except sp_codex.ResolveAmbiguousKind as exc:
        if exc.ambiguous:
            lines = sp_identity.describe_candidates(name, exclude=exclude)
            raise _Refusal(2, "\n".join(['error: "%s" is ambiguous; pick one typed ID:' % value] + lines))
        lines = sp_identity.describe_candidates(name, exclude=exclude)
        raise _Refusal(2, "\n".join(["error: %s" % exc] + (["Candidates:"] + lines if lines else [])))
    except sp_codex.ResolveNoLive as exc:
        lines = sp_identity.ambiguity_lines(exc, name, exclude)
        if lines:
            raise _Refusal(2, "\n".join(['error: "%s" is ambiguous; pick one typed ID:' % value] + lines))
        raise _Refusal(2, "error: %s" % exc)
    except sp_codex.ResolveError as exc:
        raise _Refusal(2, "error: %s" % exc)
    kind, ident = found["kind"], found["uuid"]
    rec = None
    if kind == "cc":
        try:
            rec = sp_claude._resolve_claude_record(ident)
        except sp_codex.ResolveError as exc:
            raise _Refusal(2, "error: %s" % exc)
    runtime = {"cc": "claude", "codex": "codex", "oc": "opencode"}[kind]
    return Target(None, ident, runtime, found.get("name"), rec)


def _resolve_typed(args, verb, prefix, rest, sender):
    if prefix == "cc":
        try:
            rec = sp_claude._resolve_claude_record(rest)
        except sp_codex.ResolveError as exc:
            raise _failed(exc, rest)
        return Target("cc", rec.get("sessionId"), record_runtime(rec), rec.get("name"), rec)
    if prefix == "codex":
        if sp_runtime.is_uuid(rest):
            # The allowed send and the request advice need no lookup of their own.
            return Target("codex", rest, "codex", None, None)
        try:
            if verb == "send":
                thread = sp_codex.resolve_thread(rest)
            else:
                thread = sp_codex.resolve_thread_prefer_live(rest)
        except sp_codex.ResolveError as exc:
            raise _failed(exc, rest)
        return Target("codex", thread["id"], "codex", thread.get("name"), None)
    if sp_opencode.is_session_id(rest):
        return Target("oc", rest, "opencode", None, None)
    exclude = sender.uuid if sender.kind == "oc" and verb != "wait" else None
    try:
        session = sp_opencode.resolve_session(rest, exclude=exclude)
    except sp_codex.ResolveError as exc:
        raise _failed(exc, rest)
    return Target("oc", session["id"], "opencode", session.get("title"), None)


def _route(args, verb, value):
    sender = sender_kind(args)
    prefix, sep, rest = value.partition(":")
    if not value or (sep and prefix not in KINDS):
        return {"kind": UNCHECKED, "id": None, "runtime": None, "target": value}
    if sep:
        tgt = _resolve_typed(args, verb, prefix, rest, sender)
        refusal = _verdict(args, verb, sender, tgt)
        if refusal:
            raise _Refusal(2, refusal)
        return {"kind": prefix, "id": tgt.ident, "runtime": tgt.runtime, "target": value}
    tgt = _resolve_bare(args, verb, value, sender)
    refusal = _verdict(args, verb, sender, tgt)
    if refusal:
        raise _Refusal(2, refusal)
    typed = _typed(tgt.runtime, tgt.ident)
    kind = {"claude": "cc", "codex": "codex", "opencode": "oc"}[tgt.runtime]
    raise _Refusal(
        2,
        'error: %s needs a typed target; "%s" is %s %s (%s). Run:\n%s'
        % (_wait_flag(verb), value, LABELS[kind], typed, _liveness(tgt), _run_lines(args, verb, typed)),
    )


def check(args, verb, attr):
    """None to proceed (args.route_checked is set), else the exit code after the error."""
    args.route_checked = None
    try:
        # Argument errors keep precedence over target lookup, as in delivery.
        if hasattr(args, "timeout"):
            sp_requests._bounded_timeout(args.timeout)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        args.route_checked = _route(args, verb, getattr(args, attr, None) or "")
    except _Refusal as exc:
        sys.stderr.write(exc.text + "\n")
        return exc.code
    return None


def verify(args, runtime, ident):
    """None when the target resolved for delivery is the one `check` approved.

    A different id or runtime, or no recorded check, writes the error and
    returns 1 before anything is sent.
    """
    checked = getattr(args, "route_checked", None)
    if not isinstance(checked, dict) or checked.get("kind") == UNCHECKED:
        sys.stderr.write("error: the route was not checked before delivery; nothing was sent\n")
        return 1
    if checked["id"] == ident and checked["runtime"] == runtime:
        return None
    sys.stderr.write(
        "error: %s changed between the route check and delivery (checked %s, now %s); nothing was sent\n"
        % (checked["target"], _typed(checked["runtime"], checked["id"]), _typed(runtime, ident))
    )
    return 1


def verify_record(args, rec):
    """`verify` for a registry-shaped record (a Claude session, shim or OpenCode request target)."""
    return verify(args, record_runtime(rec), rec.get("sessionId"))
