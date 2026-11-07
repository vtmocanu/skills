"""Messaging for the session-peers CLI."""

from __future__ import annotations
import json
import collections
import os
import socket
import sys
import time
import uuid as uuidlib
from . import constants as sp_constants, protocol as sp_protocol, rollout as sp_rollout, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, diagnostics as sp_diagnostics, requests as sp_requests, storage as sp_storage
from . import buddy as sp_buddy, identity as sp_identity, opencode as sp_opencode
from . import reply_wake as sp_reply_wake
from . import journal as sp_journal
from . import routes as sp_routes


def _validate_reply_wake(args):
    consent = getattr(args, "allow_reply_wake", None)
    reply = getattr(args, "in_reply_to", None)
    overrides = any(getattr(args, name, None) for name in
                    ("from_thread", "from_name", "from_sid", "from_socket"))
    if reply and (getattr(args, "wake", False) or overrides or consent is not None):
        raise ValueError("--in-reply-to cannot combine with --wake, --from-* or --allow-reply-wake")
    if reply and not args.to.startswith("oc:"):
        raise ValueError("--in-reply-to requires an oc: target")
    if consent is not None:
        if overrides or _opencode_sender(args) is None:
            raise ValueError("--allow-reply-wake requires this verified OpenCode sender, without --from-* overrides")
        sp_requests._bounded_timeout(consent)


def _issue_reply_wake(args, oc_sender, recipient, message_id):
    if getattr(args, "allow_reply_wake", None) is None:
        return None
    grant = sp_reply_wake.issue(message_id, "oc:" + oc_sender["id"], recipient, args.allow_reply_wake)
    args.reply_wake_result = {"message_id": message_id, "expires_at": grant["expires_at"]}
    return message_id

def _record_before_send(args, message_id, recipient):
    """With `--record`, journal the message just before its transport call.

    None when not recording or recorded; otherwise the exit code of a
    pre-send journal failure (nothing was sent). A recorded send remembers its
    slot in ``args.journal_slot`` so a transport failure can append a
    transport_error.
    """
    if not getattr(args, "record", False):
        return None
    web = _web_sender(args)
    sender = "web:%s" % web["sid"] if web else sp_journal.sender_or_none()
    try:
        sp_journal.record_message(sender, [recipient], args.message, message_id=message_id)
    except sp_journal.JournalError as exc:
        return sp_journal.presend_failed(exc, getattr(args, "json", False))
    if sender is None:
        sys.stderr.write("note: sender identity not detected; recorded with no sender\n")
    args.journal_slot = recipient
    args.journal_result = {"message_id": message_id, "recorded": True}
    return None


def _transport_failed(args, message_id, exc):
    """Exit code for a failed transport call: 1 as before, or 76 when `--record`
    could not append the transport_error."""
    slot = getattr(args, "journal_slot", None)
    if slot is None:
        return 1
    return sp_journal.transport_failed(message_id, slot, exc)


def _new_msg_id(args):
    """The logical message id: a channel fan-out picks one id for all its deliveries."""
    return getattr(args, "channel_msg_id", None) or str(uuidlib.uuid4())


def _print_send_result(args, payload, human):
    sink = getattr(args, "result_sink", None)
    if sink is not None:
        sink.append(dict(payload))
    if getattr(args, "reply_wake_result", None):
        payload["reply_wake"] = args.reply_wake_result
    if getattr(args, "journal_result", None):
        payload["journal"] = args.journal_result
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(human)


def _expand_buddy_arg(args, attr, purpose):
    """Replace `buddy` in args.<attr>; returns an exit code on failure."""
    try:
        setattr(args, attr, sp_buddy.expand_buddy(getattr(args, attr), args, purpose))
    except sp_buddy.BuddyError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return exc.code
    return None


def cmd_send(args):
    sp_diagnostics.warn_versions()
    failed = _expand_buddy_arg(args, "to", "send")
    if failed is not None:
        return failed
    if str(args.to).startswith("#"):
        # A channel send validates its own flags, before the reply-wake rules apply.
        from . import channels as sp_channels

        return sp_channels.send_channel(args)
    try:
        args.message = sp_runtime.message_from_args(args)
        _validate_reply_wake(args)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if (refused := sp_routes.check(args, "send", "to")) is not None:
        return refused
    return _dispatch_send(args)


def _dispatch_send(args, claude_sender_sid=None):
    """Deliver ``args.message`` to the already route-checked ``args.to`` through its runtime's sender.

    ``claude_sender_sid`` is set only by a channel fan-out run by a Claude
    session: its Claude members are then reached through `_send_claude_channel`.
    """
    target = args.to
    if target.startswith("oc:"):
        return _send_opencode(target[len("oc:") :], args)
    try:
        oc_sender = _opencode_sender(args)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if target.startswith("web:"):
        return _send_web(target[len("web:") :], args, oc_sender)
    if target.startswith("codex:"):
        return _send_codex(target[len("codex:") :], args, oc_sender)
    if target.startswith("cc:"):
        shim_thread = _codex_shim_thread(target[len("cc:") :], args)
        if shim_thread:
            return _send_codex(shim_thread, args, oc_sender)
        if claude_sender_sid:
            return _send_claude_channel(target[len("cc:") :], args, claude_sender_sid)
        return _send_claude(target[len("cc:") :], args, oc_sender)
    sys.stderr.write("error: --to must start with codex:, cc:, oc: or web:\n")
    return 2


def _web_sender(args):
    """The browser peer sending through the chat server, or None.

    ``args.web_sender`` ({"sid", "name", "socket"}) is set only by the server
    and has no CLI flag. When present it replaces every environment-derived
    identity. A malformed one raises ValueError rather than falling back to the
    environment, which would attribute the send to the wrong sender.
    """
    web = getattr(args, "web_sender", None)
    if web is None:
        return None
    try:
        sid = sp_identity.parse_address("web:%s" % web["sid"])["uuid"]
        return {"sid": sid, "name": web.get("name"), "socket": web.get("socket")}
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ValueError("malformed browser sender %r" % (web,))


def _record_kind(rec):
    """The typed-address kind a registry record is read as."""
    runtime = sp_protocol.sender_runtime(rec)
    return runtime if runtime in ("codex", "web") else "cc"


WEB_ACK_TIMEOUT = 15.0
_WEB_LINE_MAX = 65536


def _web_exchange(rec, frame, timeout=None):
    """Send ``frame`` to the browser peer's inbox and read its one NDJSON answer.

    Returns ("ack", None), ("refused", detail) or ("unknown", detail). The
    write side is half-closed after the frame, so the answer is one line (the
    inbox's `peer_inbox_ack` or `peer_inbox_refused` for this frame's msg_id);
    a timeout or a close without one leaves the outcome unknown. A failed
    connect or send raises OSError: nothing was delivered.
    """
    timeout = WEB_ACK_TIMEOUT if timeout is None else timeout
    path = rec.get("messagingSocketPath")
    if not sp_claude.socket_path_ok(path):
        raise OSError("refusing to write to %r: symlink or non-allowlisted directory" % (path,))
    deadline = time.monotonic() + timeout
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout)
        conn.connect(path)
        token = sp_claude.peer_token_for(rec)
        if token:
            conn.sendall(json.dumps({"type": "auth", "token": token}).encode("utf-8") + b"\n")
        conn.sendall(json.dumps(frame).encode("utf-8") + b"\n")
        conn.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            while b"\n" in buf:
                line, _sep, buf = buf.partition(b"\n")
                try:
                    answer = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(answer, dict) or answer.get("type") != "control":
                    continue
                if answer.get("orig_msg_id") != frame["msg_id"]:
                    continue
                if answer.get("action") == "peer_inbox_ack":
                    return "ack", None
                if answer.get("action") == "peer_inbox_refused":
                    return "refused", str(answer.get("detail") or "no detail")
            if len(buf) > _WEB_LINE_MAX:
                return "unknown", "the browser peer answered with an oversized line"
            left = deadline - time.monotonic()
            if left <= 0:
                return "unknown", "no answer within %g s" % timeout
            conn.settimeout(left)
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return "unknown", "no answer within %g s" % timeout
            except OSError as exc:
                return "unknown", "the connection failed before an answer: %s" % exc
            if not chunk:
                return "unknown", "the connection closed without an answer"
            buf += chunk
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _send_web(ident, args, oc_sender=None):
    """Send one message to the browser peer's inbox and wait for its acknowledgement.

    With no live record the message is not delivered: under `--record` it is
    journaled and a transport_error follows; routes.check refuses otherwise.
    Exit 0 only on `peer_inbox_ack`; a refusal is "not delivered" and a
    timeout or EOF "outcome unknown", both exit 1.
    """
    try:
        runtime, from_name, address = _caller_view(args, oc_sender)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if (refused := sp_routes.verify(args, "web", ident)) is not None:
        return refused
    try:
        rec = sp_routes.web_record(ident)
    except sp_routes._Refusal as exc:
        sys.stderr.write(exc.text + "\n")
        return exc.code
    msg_id = _new_msg_id(args)
    failed = _record_before_send(args, msg_id, "web:" + ident)
    if failed is not None:
        return failed
    if rec is None:
        sys.stderr.write("error: browser peer web:%s is not running; nothing was delivered\n" % ident)
        return _transport_failed(args, msg_id, "browser peer is not running")
    sender_ident = address.partition(":")[2] if address else None
    lines = [sp_protocol.build_origin(runtime, from_name, sender_ident)]
    sock = getattr(args, "from_socket", None) or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    sender_rec = sp_claude.claude_record_by_socket(sock) if sock else None
    reply_socket = sender_rec.get("messagingSocketPath") if sender_rec else None
    if address and not reply_socket:
        lines.append(sp_protocol.reply_route_hint(address))
    body = "\n".join(lines + [args.message])
    try:
        sp_claude.check_wrapped_size(body)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return _transport_failed(args, msg_id, exc)
    frame = sp_protocol.build_user_frame(body, reply_socket, msg_id)
    try:
        outcome, detail = _web_exchange(rec, frame)
    except OSError as exc:
        sys.stderr.write("error: could not reach browser peer web:%s: %s\n" % (ident, exc))
        return _transport_failed(args, msg_id, exc)
    if outcome == "refused":
        sys.stderr.write("error: browser peer web:%s refused the message: %s; not delivered\n" % (ident, detail))
        return _transport_failed(args, msg_id, "refused: %s" % detail)
    if outcome == "unknown":
        sys.stderr.write(
            "error: outcome unknown: browser peer web:%s did not acknowledge message %s (%s); "
            "it may or may not have been kept; check its inbox before resending\n" % (ident, msg_id, detail)
        )
        return _transport_failed(args, msg_id, "outcome unknown: %s" % detail)
    _print_send_result(
        args,
        {
            "status": "sent",
            "target": "web",
            "session_id": ident,
            "session_name": rec.get("name"),
            "message_id": msg_id,
            "acknowledged": True,
        },
        "sent to browser peer web:%s, message %s (acknowledged)" % (ident, msg_id),
    )
    return 0


def _opencode_sender(args):
    """The verified OpenCode session running this command, or None.

    Explicit sender flags keep their meaning. OpenCode's id beside a Claude or
    Codex id was inherited by one agent from the other, so it is refused
    rather than guessed; an id the service cannot confirm is refused too.
    """
    sid = sp_opencode.current_session_id()
    explicit = any(
        getattr(args, name, None)
        for name in ("from_thread", "from_name", "from_sid", "from_socket")
    )
    if not sid or explicit or _web_sender(args):
        return None
    if (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
            or sp_identity._thread_from_args(args)):
        raise ValueError(
            "this environment names OpenCode session %s and a Claude or Codex "
            "session; pass explicit --from-* flags" % sid
        )
    try:
        session = sp_opencode.get_session(sid)
    except sp_opencode.OpenCodeError as exc:
        raise ValueError("cannot verify OpenCode session %s: %s" % (sid, exc))
    return {"id": sid, "title": session.get("title")}


def _caller_view(args, oc_sender=None):
    """(runtime, name, typed address) of a non-OpenCode-target sender.

    Used where the recipient has no native route back: the address goes into
    an explicit reply hint. Unknown senders return ("unknown", None, None).
    """
    web = _web_sender(args)
    if web:
        return "web", web["name"], "web:%s" % web["sid"]
    if oc_sender:
        return "opencode", oc_sender.get("title"), "oc:%s" % oc_sender["id"]
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    rec = sp_claude.claude_record_by_socket(sock) if sock else None
    if rec is None:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if sp_runtime.is_uuid(sid):
            # include_web: a caller whose id names the browser peer is that peer.
            matches = sp_claude.claude_record_by_target(sid, include_web=True)
            rec = matches[0] if len(matches) == 1 else None
    if rec is not None and rec.get("sessionId"):
        runtime = sp_protocol.sender_runtime(rec)
        kind = runtime if runtime in ("codex", "web") else "cc"
        return runtime, rec.get("name"), "%s:%s" % (kind, rec["sessionId"])
    tid = sp_identity._thread_from_args(args)
    if tid:
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid), {}) or {}
        return "codex", state.get("name"), "codex:%s" % tid
    return "unknown", None, None


def _may_wake_opencode(args, session_id, owner=None):
    """Only an explicit wake or this caller's bound buddy starts a headless turn.

    A channel fan-out (``never_wake``) wakes nobody, not even a bound buddy."""
    if getattr(args, "never_wake", False):
        return False
    if getattr(args, "wake", False):
        return True
    if _web_sender(args):
        return False  # the browser peer has no buddy binding
    try:
        owner = owner or sp_identity.caller_identity(args)
    except ValueError:
        return False
    binding = sp_buddy.read_buddy(owner)
    return bool(binding and binding["buddy"]["kind"] == "oc"
                and binding["buddy"]["uuid"] == session_id)


_REPLY_WAKE_REFUSED = (
    "error: %s; nothing queued. Resend without --in-reply-to, and use --wake only if authorized.\n"
)


def _send_opencode(target, args):
    """Queue one message as an OpenCode session's next turn.

    Waking requires a bound buddy or explicit opt-in; `delivery: queue`
    never steers a running turn. OpenCode has no reply socket, so the
    body names the sender's typed address for an explicit `send` back.
    """
    try:
        oc_sender = _opencode_sender(args)
        runtime, from_name, address = _caller_view(args, oc_sender)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    exclude = oc_sender["id"] if oc_sender else None
    try:
        session = sp_opencode.resolve_session(target, exclude=exclude)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if (refused := sp_routes.verify(args, "opencode", session["id"])) is not None:
        return refused
    if exclude and session["id"] == exclude:
        sys.stderr.write("error: an OpenCode session cannot message itself\n")
        return 2
    if address is None:
        sp_runtime.log(
            "sender identity absent: the OpenCode session cannot reply; send "
            "from a Claude, Codex or OpenCode session's own shell"
        )
    msg_id = _new_msg_id(args)
    try:
        reply_id = _issue_reply_wake(args, oc_sender, "oc:" + session["id"], msg_id)
    except (ValueError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    ident = address.partition(":")[2] if address else None
    lines = [
        sp_protocol.build_tag(from_name, ident, None, msg_id),
        sp_protocol.build_origin(runtime, from_name, ident),
    ]
    if address:
        lines.append(sp_protocol.reply_route_hint(address, reply_id))
    text = "\n".join(lines + [args.message])
    try:
        sp_opencode.check_text_size(text)
    except ValueError as exc:
        # Refused before the journal, the wake grant or the API is touched.
        if reply_id:
            sp_reply_wake.forget(reply_id)
        sys.stderr.write("error: %s\n" % exc)
        return 1
    state = sp_opencode.state_of(session["id"])
    resume = _may_wake_opencode(args, session["id"])
    in_reply_to = getattr(args, "in_reply_to", None)
    grant_requester = "oc:" + session["id"]
    if in_reply_to:
        # Read-only check now; the grant is spent only after the journal accepted the entry.
        try:
            caller = sp_identity.caller_identity(args)
            caller_address = "%s:%s" % (caller["kind"], caller["uuid"])
            if caller_address != address:
                raise ValueError("reply sender identity is absent or ambiguous")
            grant_path = sp_reply_wake.find(in_reply_to)
            sp_reply_wake._check(sp_runtime.read_json(grant_path), in_reply_to, grant_requester, caller_address)
        except (ValueError, OSError) as exc:
            sys.stderr.write(_REPLY_WAKE_REFUSED % exc)
            return 1
        resume = True
    failed = _record_before_send(args, msg_id, "oc:" + session["id"])
    if failed is not None:
        if reply_id:
            sp_reply_wake.forget(reply_id)
        return failed
    if in_reply_to:
        try:
            sp_reply_wake.consume(in_reply_to, grant_requester, caller_address)
        except (ValueError, OSError) as exc:
            sys.stderr.write(_REPLY_WAKE_REFUSED % exc)
            return _transport_failed(args, msg_id, "reply-wake grant refused")
    try:
        inbox_id = sp_opencode.deliver(
            session["id"], text, {"message_id": msg_id, "from": address}, resume=resume
        )
    except (sp_opencode.OpenCodeError, ValueError) as exc:
        if reply_id:
            sp_reply_wake.forget(reply_id)
        if in_reply_to:
            sys.stderr.write("error: delivery failed or unconfirmed; grant spent; check the recipient before resending. Reissue consent or use --wake only if separately authorized.\n")
        sys.stderr.write("error: could not queue to OpenCode session %s: %s\n" % (session["id"], exc))
        return _transport_failed(args, msg_id, exc)
    _print_send_result(
        args,
        {
            "status": "queued",
            "target": "opencode",
            "session_id": session["id"],
            "session_name": session.get("title"),
            "session_state": state,
            "message_id": msg_id,
            "inbox_id": inbox_id,
            "reply_capable": bool(address),
            "resume": resume,
            **({"wake_grant": "consumed"} if in_reply_to else {}),
        },
        "queued to OpenCode session %s (%s, %s), message %s%s"
        % (session.get("title") or "(untitled)", session["id"], state, msg_id,
            ("; woke via reply grant %s" % in_reply_to) if in_reply_to else
            ("" if resume else "; resume false: idle sessions wait for resume; an active drain may consume it")),
    )
    return 0


def _codex_shim_thread(target, args):
    """Thread UUID when a Claude-side sender names a Codex shim's peer record.

    The shim's registry record is a Claude-facing alias of a Codex thread. A
    Claude sender goes through the Codex path, which stamps its own identity
    and reply route; the Claude-facing path is for Codex senders only.
    """
    try:
        if sp_identity._thread_from_args(args):
            return None
        rec = sp_claude._resolve_claude_record(target)
    except (sp_codex.ResolveError, ValueError):
        return None
    if rec.get("entrypoint") == "codex" and sp_runtime.is_uuid(rec.get("sessionId")):
        return rec["sessionId"]
    return None


def _send_codex(target, args, oc_sender=None):
    try:
        thread = sp_codex.resolve_thread(target)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if (refused := sp_routes.verify(args, "codex", thread["id"])) is not None:
        return refused
    degraded = thread.get("degraded")
    paused = False
    if degraded:
        sp_runtime.log("liveness unverified: the Codex state schema is unknown")
    else:
        held, _pid = sp_codex.thread_is_held(
            thread["rollout_path"],
            lock_path=sp_codex.writer_lock_path(thread["id"], home=thread.get("codex_home")),
        )
        if held is None:
            sys.stderr.write(
                "error: Codex thread liveness is unavailable; retry where lsof "
                "is permitted\n"
            )
            return 1
        if not held:
            sys.stderr.write(
                "error: no active session for thread %s; its process has exited "
                "(the queue would sit undrained)\n" % thread["id"]
            )
            return 1
        paused = bool(sp_rollout.thread_is_paused(thread["rollout_path"]))
        if paused:
            sp_runtime.log(
                "thread %s is paused after an interrupt: the message is queued "
                "but drains only when its user types the next prompt" % thread["id"]
            )
    from_name, from_sid = args.from_name, args.from_sid
    web = _web_sender(args)
    if oc_sender:
        # No socket: the Codex shim cannot forward the final message back, so
        # the body names the explicit route instead.
        msg_id = _new_msg_id(args)
        try:
            reply_id = _issue_reply_wake(args, oc_sender, "codex:" + thread["id"], msg_id)
        except (ValueError, OSError) as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        text = "\n".join([
            sp_protocol.build_tag(oc_sender.get("title"), oc_sender["id"], None, msg_id),
            sp_protocol.build_origin("opencode", oc_sender.get("title"), oc_sender["id"]),
            sp_protocol.reply_route_hint("oc:%s" % oc_sender["id"], reply_id),
            args.message,
        ])
        result = _queue_codex(thread, text, msg_id, args, paused)
        if result and reply_id:
            sp_reply_wake.forget(reply_id)
        return result
    if web:
        # The browser peer speaks for itself: never the server process's environment.
        from_socket, from_sid, from_name = web["socket"], web["sid"], web["name"]
    else:
        from_socket = args.from_socket or os.environ.get(
            "CLAUDE_CODE_MESSAGING_SOCKET"
        )
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not web and not from_socket and not args.from_sid and sp_runtime.is_uuid(env_sid):
        matches = sp_claude.claude_record_by_target(env_sid)
        if len(matches) == 1:
            from_socket = matches[0].get("messagingSocketPath")
    if from_socket:
        # P3: a reply address without a session id can be delivered to whoever
        # holds that socket next, so the id is resolved here, from the registry.
        rec = sp_claude.claude_record_by_socket(from_socket)
        if rec is None:
            sys.stderr.write(
                "error: no live Claude session listens on %s, so --from-socket "
                "would name a reply address nothing answers\n" % from_socket
            )
            return 1
        if web and (rec.get("entrypoint") != "web" or rec.get("sessionId") != web["sid"]):
            sys.stderr.write(
                "error: %s is not the browser peer web:%s, so it names no reply address for it\n"
                % (from_socket, web["sid"])
            )
            return 1
        from_sid = from_sid or rec.get("sessionId")
        from_name = from_name or rec.get("name")
    elif not from_name and not from_sid:
        sp_runtime.log(
            "sender identity absent: replies stay in the Codex TUI; when "
            "sending from Claude Code, use its Bash tool so "
            "CLAUDE_CODE_MESSAGING_SOCKET is available"
        )
    msg_id = _new_msg_id(args)
    tag = sp_protocol.build_tag(from_name, from_sid, from_socket, msg_id)
    origin = sp_protocol.build_origin(
        sp_protocol.sender_runtime(rec if from_socket else None), from_name, from_sid
    )
    text = "%s\n%s\n%s" % (tag, origin, args.message)
    return _queue_codex(thread, text, msg_id, args, paused)


def _queue_codex(thread, text, msg_id, args, paused=False):
    # A local size refusal comes before anything is recorded.
    try:
        sp_codex.check_queue_text(text)
    except sp_codex.QueueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return _transport_failed(args, msg_id, exc)
    failed = _record_before_send(args, msg_id, "codex:" + thread["id"])
    if failed is not None:
        return failed
    try:
        sp_codex.codex_queue(
            thread["id"], text, cwd=thread.get("cwd"), home=thread.get("codex_home")
        )
    except sp_codex.QueueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return _transport_failed(args, msg_id, exc)
    name = thread.get("name") or thread["id"]
    _print_send_result(
        args,
        {
            "status": "queued",
            "target": "codex",
            "thread_id": thread["id"],
            "thread_name": thread.get("name"),
            "message_id": msg_id,
            # Only a channel fan-out reads this; a plain send's JSON is unchanged.
            **({"paused": True} if paused and getattr(args, "result_sink", None) is not None else {}),
        },
        "queued to %s (%s), message %s" % (name, thread["id"], msg_id),
    )
    return 0


def _send_claude(target, args, oc_sender=None):
    reply_id = None
    # One id for the frame, a reply-wake grant and the journal entry.
    msg_id = _new_msg_id(args)
    try:
        rec = sp_claude._resolve_claude_record(target)
        if (refused := sp_routes.verify_record(args, rec)) is not None:
            return refused
        if oc_sender and getattr(args, "allow_reply_wake", None) is not None:
            reply_id = _issue_reply_wake(args, oc_sender, "cc:" + rec["sessionId"], msg_id)
            oc_sender = dict(oc_sender, reply_wake_id=reply_id)
        web = _web_sender(args)
        try:
            thread_id = None if oc_sender or web else sp_identity._thread_from_args(
                args, required=bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))
            )
        except ValueError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        if getattr(args, "record", False):
            # A near-cap message is refused before it is recorded.
            sp_claude.claude_body(args.message, thread_id, opencode_sender=oc_sender, web_sender=web)
            # A Codex shim's record is a `codex:` slot, the kind its owner reads as.
            failed = _record_before_send(args, msg_id, "%s:%s" % (_record_kind(rec), rec.get("sessionId")))
            if failed is not None:
                if reply_id:
                    sp_reply_wake.forget(reply_id)
                return failed
        msg_id, reply_capable = sp_claude._deliver_claude(
            rec, args.message, thread_id, opencode_sender=oc_sender, msg_id=msg_id, web_sender=web
        )
    except (sp_codex.ResolveError, ValueError) as exc:
        if reply_id:
            sp_reply_wake.forget(reply_id)
        sys.stderr.write("error: %s\n" % exc)
        return _transport_failed(args, msg_id, exc)
    except OSError as exc:
        if reply_id:
            sp_reply_wake.forget(reply_id)
        sys.stderr.write("error: could not reach %s: %s\n" % (target, exc))
        return _transport_failed(args, msg_id, exc)
    if thread_id and not reply_capable:
        sp_runtime.log(
            "Codex thread %s has no live shim; the message was sent, but a native "
            "peer reply cannot route back" % thread_id
        )
    _print_send_result(
        args,
        {
            "status": "sent",
            "target": "claude",
            "session_id": rec.get("sessionId"),
            "session_name": rec.get("name"),
            "message_id": msg_id,
            "from_thread": thread_id,
            "from_opencode": oc_sender["id"] if oc_sender else None,
            "reply_capable": reply_capable,
        },
        "sent to %s (pid %s), message %s" % (target, rec.get("pid"), msg_id),
    )
    return 0


def _send_claude_channel(target, args, sender_sid):
    """One channel delivery from a Claude session to a Claude member.

    `_send_claude` cannot serve a Claude sender (it asks for a Codex thread), and
    a direct `send` is refused by the route check. A channel fan-out reaches
    here only after `routes.check(..., "channel", ...)` accepted the member from
    a validated membership snapshot; the frame carries the Claude sender's own
    provenance line and reply socket, and `verify_record` still pins the target.
    """
    msg_id = _new_msg_id(args)
    try:
        rec = sp_claude._resolve_claude_record(target)
        if (refused := sp_routes.verify_record(args, rec)) is not None:
            return refused
        matches = sp_claude.claude_record_by_target(sender_sid)
        sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        sender_rec = matches[0] if len(matches) == 1 else (
            sp_claude.claude_record_by_socket(sock) if sock else None
        )
        if not isinstance(sender_rec, dict) or sender_rec.get("sessionId") != sender_sid:
            # No live registry record proves this environment's session id: the
            # origin is labelled unverified and no reply route is named.
            sender_rec = None
        name = sender_rec.get("name") if sender_rec else None
        sender_sock = sender_rec.get("messagingSocketPath") if sender_rec else None
        origin = sp_protocol.build_origin(sp_protocol.sender_runtime(sender_rec), name, sender_sid)
        inner = "%s\n%s" % (origin, args.message)
        # Without a registry record there is no reply socket to name: a bare body, as a shim-less Codex sender.
        # Cut the wrapper's name as build_tag cuts its own (control characters out, then
        # MAX_TAG_FIELD_CHARS characters) so it stays within what channels._check_size measured.
        wrapper_name = (
            sp_constants.C0_RE.sub("", str(name))[:sp_constants.MAX_TAG_FIELD_CHARS] if name is not None else None
        )
        body = (
            sp_protocol.build_wrapper(inner, sender_sock, sender_sid, wrapper_name) if sender_sock else inner
        )
        sp_claude.check_wrapped_size(body)
        frame = sp_protocol.build_user_frame(body, sender_sock, msg_id=msg_id)
        sp_claude.send_frame(rec["messagingSocketPath"], frame, auth_token=sp_claude.peer_token_for(rec))
    except (sp_codex.ResolveError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except OSError as exc:
        sys.stderr.write("error: could not reach %s: %s\n" % (target, exc))
        return 1
    _print_send_result(
        args,
        {
            "status": "sent",
            "target": "claude",
            "session_id": rec.get("sessionId"),
            "session_name": rec.get("name"),
            "message_id": msg_id,
            "reply_capable": bool(sender_sock),
        },
        "sent to %s (pid %s), message %s" % (target, rec.get("pid"), msg_id),
    )
    return 0


_RequestSetup = collections.namedtuple(
    "_RequestSetup",
    "message timeout requester oc_sender record request_id meta_path reply_path expires_at meta target_typed",
)


def _requester(args, target_kind):
    """(typed requester, verified OpenCode sender or None) for ask/dispatch/await.

    A Claude target is asked by a Codex thread (`--from-thread` or its env) or
    an OpenCode session; an OpenCode target by any of the three runtimes.
    Raises ValueError with the reason.
    """
    explicit = getattr(args, "from_thread", None)
    env_thread = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if (explicit and sp_runtime.is_uuid(explicit) and sp_runtime.is_uuid(env_thread)
            and explicit.lower() != env_thread.lower()):
        # The journal's `from` is this identity: a flag must not speak for another thread.
        raise ValueError(
            "--from-thread names thread %s but this environment is Codex thread %s" % (explicit, env_thread)
        )
    if explicit or target_kind == "cc" and not sp_opencode.current_session_id():
        tid = sp_identity._thread_from_args(args, required=True)
        return {"kind": "codex", "uuid": tid}, None
    try:
        caller = sp_identity.caller_identity(args)
    except ValueError:
        if target_kind == "cc":
            raise
        raise ValueError(
            "a request needs a Claude session, Codex thread or OpenCode session "
            "as its origin; run it from that session's shell"
        )
    if caller["kind"] == "cc" and target_kind == "cc":
        raise ValueError(
            "a request needs --from-thread or CODEX_THREAD_ID so its origin is explicit"
        )
    oc_sender = None
    if caller["kind"] == "oc":
        try:
            session = sp_opencode.get_session(caller["uuid"])
        except sp_opencode.OpenCodeError as exc:
            raise ValueError("cannot verify OpenCode session %s: %s" % (caller["uuid"], exc))
        oc_sender = {"id": caller["uuid"], "title": session.get("title")}
    return caller, oc_sender


def _opencode_record(target, exclude=None):
    """An OpenCode session in the shape the request code reads from a Claude record."""
    session = sp_opencode.resolve_session(target, exclude=exclude)
    return {"_runtime": "opencode", "sessionId": session["id"], "name": session.get("title")}


def _prepare_request(args, purpose):
    """Shared validation and mailbox creation; caller owns cleanup policy."""
    sp_diagnostics.warn_versions()
    failed = _expand_buddy_arg(args, "to", purpose)
    if failed is not None:
        return failed
    if (refused := sp_routes.check(args, purpose, "to")) is not None:
        return refused
    kind, _sep, target = args.to.partition(":")
    if kind not in ("cc", "oc") or not _sep:
        sys.stderr.write("error: %s --to must start with cc: or oc:\n" % purpose)
        return 2
    try:
        message = sp_runtime.message_from_args(args)
        timeout = sp_requests._bounded_timeout(args.timeout)
        requester, oc_sender = _requester(args, kind)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    sp_requests.warn_short_lifetime(timeout)
    try:
        if kind == "oc":
            exclude = requester["uuid"] if requester["kind"] == "oc" else None
            rec = _opencode_record(target, exclude=exclude)
            if rec["sessionId"] == exclude:
                sys.stderr.write("error: an OpenCode session cannot ask itself\n")
                return 2
            if not _may_wake_opencode(args, rec["sessionId"], requester):
                sys.stderr.write("error: an OpenCode request needs a bound buddy or --wake; no request was queued\n")
                return 2
        else:
            rec = sp_claude._resolve_claude_record(target)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if not rec.get("sessionId"):
        sys.stderr.write(
            "error: Claude target %r has no session id, so its reply cannot be verified\n"
            % (rec.get("name") or args.to)
        )
        return 1
    if (refused := sp_routes.verify_record(args, rec)) is not None:
        return refused

    sp_requests.cleanup_expired_requests()
    request_id = str(uuidlib.uuid4())
    try:
        # The final transport payload is checked before the journal or a mailbox exists.
        _check_request_payload(rec, requester, oc_sender, request_id, message, timeout)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    meta_path = sp_storage.request_path(request_id)
    reply_path = sp_storage.request_reply_path(request_id)
    expires_at = time.time() + timeout
    meta = sp_requests._request_meta(request_id, requester, rec, expires_at, reply_path)
    if kind == "oc":
        target_typed = "oc:%s" % rec["sessionId"]
    else:
        target_typed = "%s:%s" % (_record_kind(rec), rec["sessionId"])
    meta["target_typed"] = target_typed
    # Order: mailbox, then journal, then send. A journal refusal removes the mailbox again. The
    # uuid4 request id is not exposed to anyone until the send, so no reply or await can address
    # a mailbox that is not yet journaled. A crash between the mailbox write and the journal
    # record leaves only an unjournaled mailbox, which cleanup_expired_requests reclaims at expiry.
    # An interrupt after the request is journaled but before record_request returns (e.g. during
    # the meta write) removes the mailbox while the request stays journaled and unsent, the same
    # end state as the old order.
    try:
        sp_runtime.write_json_atomic(meta_path, meta)
    except OSError as exc:
        # write_json_atomic does not clean its temp file on failure, and the expiry sweep skips it.
        sp_requests._unlink_quiet(meta_path)
        sp_requests._unlink_quiet("%s.tmp.%d" % (meta_path, os.getpid()))
        sys.stderr.write("error: could not write request mailbox: %s\n" % exc)
        return 1
    try:
        sp_journal.record_request(
            request_id, "%s:%s" % (requester["kind"], requester["uuid"]), [target_typed],
            message, expires_at, meta_path, fresh_id=True,
        )
    except sp_journal.JournalError as exc:
        sp_requests._unlink_quiet(meta_path)
        sp_requests._unlink_quiet(reply_path)
        return sp_journal.presend_failed(exc, getattr(args, "json", False))
    except BaseException:
        sp_requests._unlink_quiet(meta_path)
        sp_requests._unlink_quiet(reply_path)
        raise
    return _RequestSetup(
        message, timeout, requester, oc_sender, rec, request_id, meta_path, reply_path, expires_at, meta,
        target_typed,
    )


def _opencode_request_text(record, requester, oc_sender, request_id, message, timeout):
    """The text a correlated request queues to an OpenCode session."""
    envelope = sp_requests._request_envelope(request_id, message, timeout)
    runtime = {"cc": "claude", "codex": "codex", "oc": "opencode"}[requester["kind"]]
    name = oc_sender.get("title") if oc_sender else None
    if requester["kind"] == "cc":
        matches = sp_claude.claude_record_by_target(requester["uuid"])
        name = matches[0].get("name") if len(matches) == 1 else None
    elif requester["kind"] == "codex":
        state = sp_runtime.read_json(sp_storage.thread_state_path(requester["uuid"]), {}) or {}
        name = state.get("name")
    return "%s\n%s" % (sp_protocol.build_origin(runtime, name, requester["uuid"]), envelope)


def _check_request_payload(record, requester, oc_sender, request_id, message, timeout):
    """Raise ValueError when the request's transport payload is over the peer cap."""
    if record.get("_runtime") == "opencode":
        sp_opencode.check_text_size(_opencode_request_text(record, requester, oc_sender, request_id, message, timeout))
    else:
        envelope = sp_requests._request_envelope(request_id, message, timeout)
        sp_claude.claude_body(
            envelope, requester["uuid"] if requester["kind"] == "codex" else None,
            reply_route=False, opencode_sender=oc_sender,
        )


def _send_prepared_request(request):
    envelope = sp_requests._request_envelope(request.request_id, request.message, request.timeout)
    requester = request.requester
    if request.record.get("_runtime") == "opencode":
        text = _opencode_request_text(
            request.record, requester, request.oc_sender, request.request_id, request.message, request.timeout
        )
        inbox_id = sp_opencode.deliver(
            request.record["sessionId"], text,
            {"request_id": request.request_id, "from": "%s:%s" % (requester["kind"], requester["uuid"])},
            resume=True,
        )
        return inbox_id, False
    return sp_claude._deliver_claude(
        request.record,
        envelope,
        requester["uuid"] if requester["kind"] == "codex" else None,
        reply_route=False,
        opencode_sender=request.oc_sender,
    )


def cmd_ask(args):
    """Send one correlated request to Claude and return its reply on stdout."""
    request = _prepare_request(args, "ask")
    if isinstance(request, int):
        return request
    try:
        try:
            message_id, _reply_capable = _send_prepared_request(request)
        except (OSError, ValueError, sp_opencode.OpenCodeError) as exc:
            sys.stderr.write("error: could not send request to %s: %s\n" % (args.to, exc))
            return sp_journal.transport_failed(request.request_id, request.target_typed, exc)
        sp_runtime.log(
            "request %s sent to %s; waiting up to %.0fs"
            % (request.request_id, request.record.get("name") or request.record.get("sessionId"), request.timeout)
        )
        deadline = time.monotonic() + request.timeout
        while time.monotonic() < deadline:
            response = sp_runtime.read_json(request.reply_path, None)
            if sp_requests._reply_matches(response, request.request_id, request.record.get("sessionId")):
                payload = {
                    "status": "replied",
                    "request_id": request.request_id,
                    "request_message_id": message_id,
                    "session_id": request.record.get("sessionId"),
                    "session_name": request.record.get("name"),
                    "message": response["message"],
                }
                # The mailbox goes before the settlement, never after.
                sp_requests._unlink_quiet(request.meta_path)
                sp_requests._unlink_quiet(request.reply_path)
                rc = sp_journal.settle_or_report(
                    sp_journal.reply_message_id(request.request_id), "consumed",
                    "%s:%s" % (request.requester["kind"], request.requester["uuid"]), payload,
                ) or 0
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    sys.stdout.write(response["message"])
                    if not response["message"].endswith("\n"):
                        sys.stdout.write("\n")
                return rc
            time.sleep(sp_constants.REQUEST_POLL_INTERVAL)
        sys.stderr.write(
            "error: request %s timed out after %.0f seconds; no reply was queued\n"
            % (request.request_id, request.timeout)
        )
        return 124
    finally:
        sp_requests._unlink_quiet(request.meta_path)
        sp_requests._unlink_quiet(request.reply_path)


def _after_reply_validation():
    """No-op seam between `reply`'s mailbox validation and its journal record.

    A test patches it in a child process to pause there and interleave a
    competing reply.
    """


def _reply_sender_typed(meta, sid):
    """The replier's typed id, matching the request entry's recipient slot."""
    typed = meta.get("target_typed")
    if isinstance(typed, str) and typed.partition(":")[2] == sid and typed.partition(":")[0] in ("cc", "codex", "oc"):
        return typed
    return "%s:%s" % ("oc" if meta.get("target_kind") == "oc" else "cc", sid)


def cmd_reply(args):
    """Complete one pending ask mailbox from its intended Claude session."""
    try:
        message = sp_runtime.message_from_args(args)
        path, reply_path = sp_storage.request_paths(args.request)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    sp_requests.cleanup_expired_requests()
    meta = sp_runtime.read_json(path, None)
    if not isinstance(meta, dict):
        sys.stderr.write("error: request %s is unknown or expired\n" % args.request)
        return 1
    try:
        expires_at = float(meta.get("expires_at", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= time.time():
        sp_requests.cleanup_expired_requests()
        sys.stderr.write("error: request %s is expired\n" % args.request)
        return 1
    noun = "OpenCode session" if meta.get("target_kind") == "oc" else "Claude session"
    if meta.get("target_kind") == "oc":
        sid = sp_opencode.current_session_id()
    else:
        sid = sp_claude._current_claude_session_id()
    if not sid:
        sys.stderr.write(
            "error: reply must run inside the target %s so its "
            "session id can be verified\n" % noun
        )
        return 1
    if sid != meta.get("target_session_id"):
        sys.stderr.write(
            "error: request %s belongs to %s %s, not %s\n"
            % (args.request, noun, meta.get("target_session_id"), sid)
        )
        return 1
    sid_typed = _reply_sender_typed(meta, sid)
    requester_typed = meta.get("requester") or "codex:%s" % meta.get("requester_thread_id")

    def revalidate():
        # Under the journal lock: the mailbox must still exist, be live and name this session.
        current = sp_runtime.read_json(path, None)
        if not isinstance(current, dict):
            raise sp_journal.JournalRefused("request %s is unknown or expired" % args.request)
        try:
            live = float(current.get("expires_at", 0)) > time.time()
        except (TypeError, ValueError):
            live = False
        if not live:
            raise sp_journal.JournalRefused("request %s is expired" % args.request)
        if current.get("target_session_id") != sid:
            raise sp_journal.JournalRefused("request %s no longer belongs to %s" % (args.request, sid))
        if os.path.exists(reply_path):
            # A different reply already won the file: record nothing for this one.
            existing = sp_requests._read_completed_reply(reply_path)
            if existing.get("session_id") != sid or existing.get("message") != message:
                raise sp_journal.JournalRefused("request %s already has a different reply" % args.request)

    _after_reply_validation()
    try:
        sp_journal.record_reply(
            args.request, sid_typed, [requester_typed], message, expires_at, path, revalidate=revalidate
        )
    except sp_journal.JournalError as exc:
        if "already has a recorded reply" in str(exc):
            # Keep the existing refusal wording and add the journal's reason.
            exc = sp_journal.JournalRefused("request %s already has a different reply (%s)" % (args.request, exc))
        return sp_journal.presend_failed(exc, getattr(args, "json", False))
    payload = {
        "request_id": args.request,
        "session_id": sid,
        "message": message,
        "replied_at": sp_runtime.now_iso(),
    }
    try:
        created = sp_runtime.write_json_exclusive(reply_path, payload)
    except OSError as exc:
        sys.stderr.write("error: could not write reply: %s\n" % exc)
        return 1
    if not created:
        existing = sp_requests._read_completed_reply(reply_path)
        if existing.get("session_id") != sid or existing.get("message") != message:
            sys.stderr.write("error: request %s already has a different reply\n" % args.request)
            return 1
        status = "already_replied"
    else:
        status = "replied"
    result = {"status": status, "request_id": args.request, "session_id": sid}
    rc = sp_journal.settle_or_report(args.request, "replied", sid_typed, result) or 0
    _print_send_result(args, result, "%s to request %s" % (status.replace("_", " "), args.request))
    return rc


def cmd_dispatch(args):
    """Send a correlated request to Claude and return immediately.

    Same private mailbox, correlation envelope, and `reply_route=False` safety
    as `ask`, but the mailbox is expiry-scoped rather than process-scoped: it
    outlives this invocation so a later `await --request` can consume the
    reply. `--timeout` sets the request lifetime and the mailbox `expires_at`.
    """
    request = _prepare_request(args, "dispatch")
    if isinstance(request, int):
        return request
    try:
        message_id, _reply_capable = _send_prepared_request(request)
    except (OSError, ValueError, sp_opencode.OpenCodeError) as exc:
        # Delivery failed, so no reply can ever arrive: do not leave an orphan
        # mailbox that a later `await` would poll until it expired.
        sp_requests._unlink_quiet(request.meta_path)
        sp_requests._unlink_quiet(request.reply_path)
        failure = {
            "status": "delivery_failed",
            "request_id": request.request_id,
            "target_session_id": request.record.get("sessionId"),
            "detail": str(exc),
        }
        sys.stderr.write("error: could not send request to %s: %s\n" % (args.to, exc))
        rc = sp_journal.transport_failed(request.request_id, request.target_typed, exc, payload=failure)
        if args.json:
            print(json.dumps(failure, sort_keys=True))
        return rc
    sp_runtime.log(
        "dispatched request %s to %s; expires in %.0fs"
        % (request.request_id, request.record.get("name") or request.record.get("sessionId"), request.timeout)
    )
    payload = {
        "status": "queued" if request.record.get("_runtime") == "opencode" else "socket_write_succeeded",
        "request_id": request.request_id,
        "request_message_id": message_id,
        "requester_thread_id": request.meta["requester_thread_id"],
        "requester": request.meta["requester"],
        "target_session_id": request.record.get("sessionId"),
        "target_session_name": request.record.get("name"),
        "created_at": request.meta["created_at"],
        "expires_at": request.expires_at,
    }
    _print_send_result(
        args,
        payload,
        "dispatched request %s to %s; await it with "
        "`peers.py await --request %s`"
        % (request.request_id, request.record.get("name") or request.record.get("sessionId"), request.request_id),
    )
    return 0


def _await_expired(args, detail):
    """Report a mailbox that is no longer awaitable and fail closed."""
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"status": "expired", "request_id": args.request, "detail": detail},
                sort_keys=True,
            )
        )
    sys.stderr.write("error: request %s is %s\n" % (args.request, detail))
    return 1


def cmd_await(args):
    """Consume the reply to one dispatched request, or report why not.

    `--timeout` bounds only this invocation, never the request lifetime. A
    call that times out while the request is still unexpired reports `pending`
    and leaves the mailbox intact so a later `await` resumes it; a request past
    its `expires_at` reports `expired`. The reply is consumed exactly once.
    """
    try:
        meta_path, reply_path = sp_storage.request_paths(args.request)
        timeout = sp_requests._bounded_timeout(args.timeout)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    sp_requests.cleanup_expired_requests()
    meta = sp_runtime.read_json(meta_path, None)
    if not isinstance(meta, dict):
        # Expired-and-collected, already consumed, or never dispatched. Without
        # a durable terminal marker (deferred to a later slice) these cannot be
        # told apart, so all three report the terminal `expired` state.
        # Still check who is asking, so a malformed call fails as before.
        any_kind = (sp_opencode.current_session_id() or sp_claude._current_claude_session_id())
        try:
            _requester(args, "oc" if any_kind and not getattr(args, "from_thread", None) else "cc")
        except ValueError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        return _await_expired(args, "unknown, already consumed, or expired")
    # A mailbox written before typed requesters names only a Codex thread.
    recorded = meta.get("requester") or "codex:%s" % meta.get("requester_thread_id")
    try:
        requester, _oc_sender = _requester(args, meta.get("target_kind") or "cc")
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    caller = "%s:%s" % (requester["kind"], requester["uuid"])
    if caller != recorded:
        if recorded.startswith("codex:") and requester["kind"] == "codex":
            sys.stderr.write(
                "error: request %s was dispatched by thread %s, not %s\n"
                % (args.request, recorded[len("codex:"):], requester["uuid"])
            )
        else:
            sys.stderr.write(
                "error: request %s was dispatched by %s, not %s\n"
                % (args.request, recorded, caller)
            )
        return 1
    target_sid = meta.get("target_session_id")
    try:
        expires_at = float(meta.get("expires_at", 0))
    except (TypeError, ValueError):
        expires_at = 0

    deadline = time.monotonic() + timeout
    while True:
        if expires_at <= time.time():
            sp_requests.cleanup_expired_requests()
            return _await_expired(args, "expired before a reply arrived")
        if not os.path.exists(meta_path):
            return _await_expired(args, "unknown, already consumed, or expired")
        response = sp_runtime.read_json(reply_path, None)
        if sp_requests._reply_matches(response, args.request, target_sid):
            claimed = sp_requests._claim_reply(reply_path)
            if claimed is None:
                # A concurrent await consumed this reply first.
                return _await_expired(args, "already consumed")
            sp_requests._unlink_quiet(meta_path)
            payload = {
                "status": "replied",
                "request_id": args.request,
                "session_id": target_sid,
                "session_name": meta.get("target_session_name"),
                "message": claimed["message"],
            }
            # Claimed and unlinked first; a failed settlement never re-reads or re-claims.
            rc = sp_journal.settle_or_report(
                sp_journal.reply_message_id(args.request), "consumed", caller, payload
            ) or 0
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                sys.stdout.write(claimed["message"])
                if not claimed["message"].endswith("\n"):
                    sys.stdout.write("\n")
            return rc
        if time.monotonic() >= deadline:
            break
        time.sleep(sp_constants.REQUEST_POLL_INTERVAL)

    if expires_at <= time.time():
        sp_requests.cleanup_expired_requests()
        return _await_expired(args, "expired before a reply arrived")
    # This call timed out, but the request is still live and re-awaitable.
    payload = {
        "status": "pending",
        "request_id": args.request,
        "expires_at": meta.get("expires_at"),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        sys.stderr.write(
            "request %s has no reply yet; still pending and re-awaitable\n"
            % args.request
        )
    return 124


def cmd_wait_peer(args):
    """Wait for a live Claude peer's registry status to reach one state."""
    failed = _expand_buddy_arg(args, "for_peer", "wait")
    if failed is not None:
        return failed
    if (refused := sp_routes.check(args, "wait", "for_peer")) is not None:
        return refused
    if args.for_peer.startswith("oc:"):
        return _wait_opencode(args)
    if not args.for_peer.startswith("cc:"):
        sys.stderr.write("error: wait --for must start with cc: or oc:\n")
        return 2
    target = args.for_peer[len("cc:") :]
    try:
        timeout = sp_requests._bounded_timeout(args.timeout)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = sp_claude._resolve_claude_record(target)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if (refused := sp_routes.verify_record(args, rec)) is not None:
        return refused
    sid = rec.get("sessionId")
    deadline = time.monotonic() + timeout
    interval = sp_runtime._float_env(
        "SESSION_PEERS_WAIT_POLL_INTERVAL", sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    )
    if interval <= 0:
        interval = sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    while time.monotonic() < deadline:
        candidates = [
            item for item in sp_claude.read_claude_records()
            if item.get("sessionId") == sid and not sp_claude.is_web_record(item)
        ]
        if candidates:
            current = candidates[0]
            if sp_claude.record_liveness(current) == "live" and current.get("status") == args.state:
                payload = {
                    "status": args.state,
                    "session_id": sid,
                    "session_name": current.get("name"),
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print("%s is %s" % (current.get("name") or sid, args.state))
                return 0
        time.sleep(interval)
    sys.stderr.write(
        "error: %s did not become %s within %.0f seconds\n"
        % (args.for_peer, args.state, timeout)
    )
    return 124


def _wait_opencode(args):
    """Wait for an OpenCode session's agent loop to be idle or running (busy).

    Polls `session.active`; an unreadable service is not a state change.
    """
    try:
        timeout = sp_requests._bounded_timeout(args.timeout)
        session = sp_opencode.resolve_session(args.for_peer[len("oc:") :])
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if (refused := sp_routes.verify(args, "opencode", session["id"])) is not None:
        return refused
    want = {"idle": "idle", "busy": "running"}[args.state]
    deadline = time.monotonic() + timeout
    interval = sp_runtime._float_env(
        "SESSION_PEERS_WAIT_POLL_INTERVAL", sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    )
    if interval <= 0:
        interval = sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    while time.monotonic() < deadline:
        if sp_opencode.state_of(session["id"]) == want:
            payload = {"status": args.state, "session_id": session["id"],
                       "session_name": session.get("title")}
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print("%s is %s" % (session.get("title") or session["id"], args.state))
            return 0
        time.sleep(interval)
    sys.stderr.write(
        "error: %s did not become %s within %.0f seconds\n"
        % (args.for_peer, args.state, timeout)
    )
    return 124
