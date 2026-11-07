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

KINDS = ("cc", "codex", "oc", "web")
LABELS = {
    "cc": "Claude Code session", "codex": "Codex thread", "oc": "OpenCode session",
    "web": "browser peer",
}
_RUNTIME_KIND = {"claude": "cc", "codex": "codex", "opencode": "oc", "web": "web"}
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
    """`opencode`, `web` (the browser peer's record), `codex` (a shim's registry record) or `claude`."""
    if rec.get("_runtime") == "opencode":
        return "opencode"
    if rec.get("entrypoint") == "web":
        return "web"
    if rec.get("entrypoint") == "codex" and sp_runtime.is_uuid(rec.get("sessionId")):
        return "codex"
    return "claude"


def _typed(runtime, ident):
    return "%s:%s" % (_RUNTIME_KIND[runtime], ident)


def _shown(runtime, ident):
    """`_typed` with the id escaped by `display_id` (it may come from a hostile record)."""
    return _typed(runtime, sp_identity.display_id(_RUNTIME_KIND[runtime], ident))


def _claude_indicator():
    return bool(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))


def sender_kind(args):
    """The runtime running this command, as a Sender; raises _Refusal when unattributable.

    "unknown" is an OpenCode id beside a Claude or Codex one: delivery refuses it.
    """
    web = getattr(args, "web_sender", None)
    if isinstance(web, dict) and web.get("sid"):
        # The chat server sends for the browser peer; its own environment (a
        # scrubbed copy of whatever launched it) says nothing about the sender.
        return Sender("web", web["sid"])
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


_UNATTRIBUTED = (
    "error: this environment names an OpenCode session and a Claude Code or Codex "
    "session, so the sender cannot be attributed; pass explicit --from-* flags. "
    "Nothing was sent."
)


def _printable(text):
    return str(text).isprintable()


def _quoted(text):
    """``text`` in double quotes, or ``repr``-quoted when it holds a control character."""
    return '"%s"' % text if _printable(text) else repr(str(text))


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
        if not _printable(value):
            # shlex.quote keeps control characters: such a command would forge lines.
            return None, False
        if boolean:
            out.append(flag)
        elif isinstance(value, float):
            out.append("%s %g" % (flag, value))
        else:
            out.append("%s %s" % (flag, shlex.quote(str(value))))
    return out, note


def _wait_flag(verb):
    return "--for" if verb == "wait" else "--to"


NO_COMMAND = "  (no command is printed: the target's ID is not a well-formed typed ID)"
UNSAFE_OPTION = "  (no command is printed: an option holds a control character)"


def _run_lines(args, verb, kind, ident, restart=False):
    """The indented commands to run, with the other options kept.

    shlex.quote does not escape control characters, so a command is built only
    when ``ident`` is one `identity.display_id` would print raw; otherwise the
    one-line NO_COMMAND notice stands in. ``restart`` adds the restart line first.
    """
    if not (isinstance(ident, str) and sp_identity.display_id(kind, ident) == ident):
        return NO_COMMAND
    options, note = _options(args, verb)
    if options is None:
        return UNSAFE_OPTION
    typed = "%s:%s" % (kind, ident)
    extra = ["peers.py restart %s" % shlex.quote(ident)] if restart else []
    lines = extra + [" ".join(["peers.py", verb, _wait_flag(verb), shlex.quote(typed)] + options)]
    if note:
        lines.append("(write the message to PATH)")
    return "\n".join("  " + line for line in lines)


# --------------------------------------------------------------------------
# Advice
# --------------------------------------------------------------------------


def _request_to_codex(verb, sender, ident):
    text = (
        "error: %s cannot reach Codex thread codex:%s: correlated requests reach "
        "only Claude Code and OpenCode peers." % (verb, sp_identity.display_id("codex", ident))
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
    head = "error: wait reads a Claude-registry status; Codex thread codex:%s " % sp_identity.display_id("codex", ident)
    if _shim_running(ident):
        return head + "publishes it through its shim. Run:\n" + _run_lines(args, "wait", "cc", ident)
    try:
        thread = sp_codex.resolve_thread(ident, require_live=False)
    except sp_codex.ResolveError as exc:
        raise _Refusal(1, "error: %s" % exc)
    if thread.get("live") is True:
        return head + "has no running shim, so no status is published. Run:\n" + _run_lines(args, "wait", "cc", ident, restart=True)
    return head + "is not live, so no status can be published; there is no equivalent."


def _plain_name(name):
    """True for a peer name with no newline (the shared regex lets a trailing one through)."""
    return sp_protocol.valid_peer_name(name) and "\n" not in str(name)


def _verified_name(rec):
    """(name or None, reason): a SendMessage name proven unique, else why not.

    reason is "absent" (no usable name), "duplicate", "its registry record
    changed while it was checked" (no live record still holds the target's
    sessionId, pid and name), or an incomplete-read / unverifiable-contender
    detail.
    """
    name = rec.get("name")
    if not _plain_name(name):
        return None, "absent"
    records, error = sp_claude.read_claude_records_checked()
    if error is not None:
        return None, error
    if sp_claude.record_liveness(rec) != "live":
        return None, "absent"
    if not any(
        other.get("sessionId") == rec.get("sessionId") and other.get("pid") == rec.get("pid")
        and other.get("name") == name and sp_claude.record_liveness(other) == "live"
        for other in records
    ):
        return None, "its registry record changed while it was checked"
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
    shown = sp_identity.display_id("cc", sid)
    label = rec.get("name") if _plain_name(rec.get("name")) else "cc:%s" % shown
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
        "disambiguation only; do not pass it to SendMessage." % (label, how, shown)
    )
    if verb in REQUESTS:
        text += " A correlated ask has no Claude-to-Claude equivalent; SendMessage is asynchronous."
    return text


def _verdict(args, verb, sender, tgt):
    """The refusal text for this sender, verb and target, or None when allowed."""
    if tgt.runtime == "web":
        if verb in ("send", "channel"):
            return None
        return (
            "error: %s cannot reach the browser peer web:%s: a person answers it by hand, "
            "with no request id or await. Use send." % (verb, sp_identity.display_id("web", tgt.ident))
        )
    if tgt.runtime == "codex":
        if verb in REQUESTS:
            return _request_to_codex(verb, sender, tgt.ident)
        if verb == "wait" and tgt.via != "cc":
            return _wait_for_codex(args, tgt.ident)
    elif tgt.runtime == "claude" and verb != "wait":
        if sender.kind == "unknown":
            return _UNATTRIBUTED
        if _claudeish(sender) and not _channel_member(args, verb, tgt):
            return _claude_to_claude(verb, tgt.rec)
    return None


def _channel_member(args, verb, tgt):
    """True only for the internal channel verb with a validated snapshot listing this target.

    The snapshot is a ``channels._Snapshot`` that ``send_channel`` builds after the
    channel exists and the sender is a member; no CLI argument can supply one.
    """
    if verb != "channel":
        return False
    from . import channels as sp_channels  # channels imports the senders that import this module

    snapshot = getattr(args, "channel_snapshot", None)
    return (isinstance(snapshot, sp_channels._Snapshot)
            and "cc:%s" % str(tgt.ident).lower() in snapshot.members)


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
            raise _Refusal(2, "\n".join(["error: %s is ambiguous; pick one typed ID:" % _quoted(value)] + lines))
        lines = sp_identity.describe_candidates(name, exclude=exclude)
        raise _Refusal(2, "\n".join(["error: %s" % exc] + (["Candidates:"] + lines if lines else [])))
    except sp_codex.ResolveNoLive as exc:
        lines = sp_identity.ambiguity_lines(exc, name, exclude)
        if lines:
            raise _Refusal(2, "\n".join(["error: %s is ambiguous; pick one typed ID:" % _quoted(value)] + lines))
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


def web_record(ident):
    """The live browser-peer record for ``ident``, else None; raises _Refusal when unverifiable."""
    for rec in sp_claude.read_claude_records():
        if rec.get("entrypoint") != "web" or rec.get("sessionId") != ident:
            continue
        state = sp_claude.record_liveness(rec)
        if state == "live":
            return rec
        if state == "unverified":
            raise _Refusal(
                1,
                "error: cannot verify browser peer web:%s because the process-start probe is "
                "unavailable; retry outside the sandbox or with host permission"
                % sp_identity.display_id("web", ident),
            )
    return None


def _resolve_web(args, rest):
    try:
        ident = sp_identity.parse_address("web:" + rest)["uuid"]
    except ValueError as exc:
        raise _Refusal(2, "error: %s" % exc)
    rec = web_record(ident)
    if rec is None and not getattr(args, "record", False):
        # Without --record there is nothing to leave behind for a later page open.
        raise _Refusal(
            1,
            "error: browser peer web:%s is not running; nothing was sent. "
            "Use --record to leave the message in the journal for when it starts." % ident,
        )
    # No live record with --record: an offline verdict, `_send_web` journals it and reports the failed transport.
    return Target("web", ident, "web", rec.get("name") if rec else None, rec)


def _resolve_typed(args, verb, prefix, rest, sender):
    if prefix == "web":
        return _resolve_web(args, rest)
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


def _channel_route(value):
    """The route of a `send --to '#NAME'` target: a stored channel that exists."""
    from . import channels as sp_channels

    try:
        key = sp_channels.require_target(value)
    except sp_channels.ChannelError as exc:
        raise _Refusal(exc.code, "error: %s" % exc)
    return {"kind": "channel", "id": key, "runtime": "channel", "target": value}


def _route(args, verb, value):
    if verb == "send" and value.startswith("#"):
        return _channel_route(value)
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
    kind = {"claude": "cc", "codex": "codex", "opencode": "oc"}[tgt.runtime]
    liveness = _liveness(tgt)
    head = "error: %s needs a typed target; %s is %s %s (%s)." % (
        _wait_flag(verb), _quoted(value), LABELS[kind], _typed(tgt.runtime, sp_identity.display_id(kind, tgt.ident)),
        liveness,
    )
    if tgt.runtime == "codex" and liveness == "dead":
        # resolve_typed falls back to a single dead thread; no send can deliver to it.
        raise _Refusal(2, head + " It is not live, so no command can deliver to it; there is no equivalent.")
    raise _Refusal(2, "%s Run:\n%s" % (head, _run_lines(args, verb, kind, tgt.ident)))


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
        % (checked["target"], _shown(checked["runtime"], checked["id"]), _shown(runtime, ident))
    )
    return 1


def verify_record(args, rec):
    """`verify` for a registry-shaped record (a Claude session, shim or OpenCode request target)."""
    return verify(args, record_runtime(rec), rec.get("sessionId"))
