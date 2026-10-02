"""Messaging for the session-peers CLI."""

from __future__ import annotations
import json
import os
import sys
import time
import uuid as uuidlib
from . import constants as sp_constants, protocol as sp_protocol, rollout as sp_rollout, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, diagnostics as sp_diagnostics, requests as sp_requests, storage as sp_storage
from . import buddy as sp_buddy, identity as sp_identity

def _print_send_result(args, payload, human):
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
    try:
        args.message = sp_runtime.message_from_args(args)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    target = args.to
    if target.startswith("codex:"):
        return _send_codex(target[len("codex:") :], args)
    if target.startswith("cc:"):
        return _send_claude(target[len("cc:") :], args)
    sys.stderr.write("error: --to must start with codex: or cc:\n")
    return 2


def _send_codex(target, args):
    try:
        thread = sp_codex.resolve_thread(target)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    degraded = thread.get("degraded")
    if degraded:
        sp_runtime.log("liveness unverified: the Codex state schema is unknown")
    else:
        held, _pid = sp_codex.thread_is_held(
            thread["rollout_path"], lock_path=sp_codex.writer_lock_path(thread["id"])
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
        if sp_rollout.thread_is_paused(thread["rollout_path"]):
            sp_runtime.log(
                "thread %s is paused after an interrupt: the message is queued "
                "but drains only when its user types the next prompt" % thread["id"]
            )
    from_name, from_sid = args.from_name, args.from_sid
    from_socket = args.from_socket or os.environ.get(
        "CLAUDE_CODE_MESSAGING_SOCKET"
    )
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not from_socket and not args.from_sid and sp_runtime.is_uuid(env_sid):
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
        from_sid = from_sid or rec.get("sessionId")
        from_name = from_name or rec.get("name")
    elif not from_name and not from_sid:
        sp_runtime.log(
            "sender identity absent: replies stay in the Codex TUI; when "
            "sending from Claude Code, use its Bash tool so "
            "CLAUDE_CODE_MESSAGING_SOCKET is available"
        )
    msg_id = str(uuidlib.uuid4())
    tag = sp_protocol.build_tag(from_name, from_sid, from_socket, msg_id)
    text = "%s\n%s" % (tag, args.message)
    try:
        sp_codex.codex_queue(thread["id"], text, cwd=thread.get("cwd"))
    except sp_codex.QueueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    name = thread.get("name") or thread["id"]
    _print_send_result(
        args,
        {
            "status": "queued",
            "target": "codex",
            "thread_id": thread["id"],
            "thread_name": thread.get("name"),
            "message_id": msg_id,
        },
        "queued to %s (%s), message %s" % (name, thread["id"], msg_id),
    )
    return 0


def _send_claude(target, args):
    try:
        rec = sp_claude._resolve_claude_record(target)
        thread_id = sp_identity._thread_from_args(args)
        msg_id, reply_capable = sp_claude._deliver_claude(rec, args.message, thread_id)
    except (sp_codex.ResolveError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except OSError as exc:
        sys.stderr.write("error: could not reach %s: %s\n" % (target, exc))
        return 1
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
            "reply_capable": reply_capable,
        },
        "sent to %s (pid %s), message %s" % (target, rec.get("pid"), msg_id),
    )
    return 0


def cmd_ask(args):
    """Send one correlated request to Claude and return its reply on stdout."""
    sp_diagnostics.warn_versions()
    failed = _expand_buddy_arg(args, "to", "ask")
    if failed is not None:
        return failed
    if not args.to.startswith("cc:"):
        sys.stderr.write("error: ask --to must start with cc:\n")
        return 2
    try:
        message = sp_runtime.message_from_args(args)
        timeout = sp_requests._bounded_timeout(args.timeout)
        thread_id = sp_identity._thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = sp_claude._resolve_claude_record(args.to[len("cc:") :])
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if not rec.get("sessionId"):
        sys.stderr.write(
            "error: Claude target %r has no session id, so its reply cannot be verified\n"
            % (rec.get("name") or args.to)
        )
        return 1

    sp_requests.cleanup_expired_requests()
    request_id = str(uuidlib.uuid4())
    meta_path = sp_storage.request_path(request_id)
    reply_path = sp_storage.request_reply_path(request_id)
    expires_at = time.time() + timeout
    sp_runtime.write_json_atomic(
        meta_path,
        sp_requests._request_meta(request_id, thread_id, rec, expires_at, reply_path),
    )
    try:
        try:
            message_id, _reply_capable = sp_claude._deliver_claude(
                rec,
                sp_requests._request_envelope(request_id, message, timeout),
                thread_id,
                reply_route=False,
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write("error: could not send request to %s: %s\n" % (args.to, exc))
            return 1
        sp_runtime.log(
            "request %s sent to %s; waiting up to %.0fs"
            % (request_id, rec.get("name") or rec.get("sessionId"), timeout)
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = sp_runtime.read_json(reply_path, None)
            if sp_requests._reply_matches(response, request_id, rec.get("sessionId")):
                payload = {
                    "status": "replied",
                    "request_id": request_id,
                    "request_message_id": message_id,
                    "session_id": rec.get("sessionId"),
                    "session_name": rec.get("name"),
                    "message": response["message"],
                }
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    sys.stdout.write(response["message"])
                    if not response["message"].endswith("\n"):
                        sys.stdout.write("\n")
                return 0
            time.sleep(sp_constants.REQUEST_POLL_INTERVAL)
        sys.stderr.write(
            "error: request %s timed out after %.0f seconds; no reply was queued\n"
            % (request_id, timeout)
        )
        return 124
    finally:
        sp_requests._unlink_quiet(meta_path)
        sp_requests._unlink_quiet(reply_path)


def cmd_reply(args):
    """Complete one pending ask mailbox from its intended Claude session."""
    try:
        message = sp_runtime.message_from_args(args)
        path = sp_storage.request_path(args.request)
        reply_path = sp_storage.request_reply_path(args.request)
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
    sid = sp_claude._current_claude_session_id()
    if not sid:
        sys.stderr.write(
            "error: reply must run inside the target Claude session so its "
            "session id can be verified\n"
        )
        return 1
    if sid != meta.get("target_session_id"):
        sys.stderr.write(
            "error: request %s belongs to Claude session %s, not %s\n"
            % (args.request, meta.get("target_session_id"), sid)
        )
        return 1
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
    _print_send_result(
        args,
        {"status": status, "request_id": args.request, "session_id": sid},
        "%s to request %s" % (status.replace("_", " "), args.request),
    )
    return 0


def cmd_dispatch(args):
    """Send a correlated request to Claude and return immediately.

    Same private mailbox, correlation envelope, and `reply_route=False` safety
    as `ask`, but the mailbox is expiry-scoped rather than process-scoped: it
    outlives this invocation so a later `await --request` can consume the
    reply. `--timeout` sets the request lifetime and the mailbox `expires_at`.
    """
    sp_diagnostics.warn_versions()
    failed = _expand_buddy_arg(args, "to", "dispatch")
    if failed is not None:
        return failed
    if not args.to.startswith("cc:"):
        sys.stderr.write("error: dispatch --to must start with cc:\n")
        return 2
    try:
        message = sp_runtime.message_from_args(args)
        timeout = sp_requests._bounded_timeout(args.timeout)
        thread_id = sp_identity._thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = sp_claude._resolve_claude_record(args.to[len("cc:") :])
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if not rec.get("sessionId"):
        sys.stderr.write(
            "error: Claude target %r has no session id, so its reply cannot be verified\n"
            % (rec.get("name") or args.to)
        )
        return 1

    sp_requests.cleanup_expired_requests()
    request_id = str(uuidlib.uuid4())
    meta_path = sp_storage.request_path(request_id)
    reply_path = sp_storage.request_reply_path(request_id)
    expires_at = time.time() + timeout
    meta = sp_requests._request_meta(request_id, thread_id, rec, expires_at, reply_path)
    sp_runtime.write_json_atomic(meta_path, meta)
    try:
        message_id, _reply_capable = sp_claude._deliver_claude(
            rec,
            sp_requests._request_envelope(request_id, message, timeout),
            thread_id,
            reply_route=False,
        )
    except (OSError, ValueError) as exc:
        # Delivery failed, so no reply can ever arrive: do not leave an orphan
        # mailbox that a later `await` would poll until it expired.
        sp_requests._unlink_quiet(meta_path)
        sp_requests._unlink_quiet(reply_path)
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "delivery_failed",
                        "request_id": request_id,
                        "target_session_id": rec.get("sessionId"),
                        "detail": str(exc),
                    },
                    sort_keys=True,
                )
            )
        sys.stderr.write("error: could not send request to %s: %s\n" % (args.to, exc))
        return 1
    sp_runtime.log(
        "dispatched request %s to %s; expires in %.0fs"
        % (request_id, rec.get("name") or rec.get("sessionId"), timeout)
    )
    payload = {
        "status": "socket_write_succeeded",
        "request_id": request_id,
        "request_message_id": message_id,
        "requester_thread_id": thread_id,
        "target_session_id": rec.get("sessionId"),
        "target_session_name": rec.get("name"),
        "created_at": meta["created_at"],
        "expires_at": expires_at,
    }
    _print_send_result(
        args,
        payload,
        "dispatched request %s to %s; await it with "
        "`peers.py await --request %s`"
        % (request_id, rec.get("name") or rec.get("sessionId"), request_id),
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
        meta_path = sp_storage.request_path(args.request)
        reply_path = sp_storage.request_reply_path(args.request)
        timeout = sp_requests._bounded_timeout(args.timeout)
        thread_id = sp_identity._thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    sp_requests.cleanup_expired_requests()
    meta = sp_runtime.read_json(meta_path, None)
    if not isinstance(meta, dict):
        # Expired-and-collected, already consumed, or never dispatched. Without
        # a durable terminal marker (deferred to a later slice) these cannot be
        # told apart, so all three report the terminal `expired` state.
        return _await_expired(args, "unknown, already consumed, or expired")
    if thread_id != meta.get("requester_thread_id"):
        sys.stderr.write(
            "error: request %s was dispatched by thread %s, not %s\n"
            % (args.request, meta.get("requester_thread_id"), thread_id)
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
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                sys.stdout.write(claimed["message"])
                if not claimed["message"].endswith("\n"):
                    sys.stdout.write("\n")
            return 0
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
    if not args.for_peer.startswith("cc:"):
        sys.stderr.write("error: wait --for must start with cc:\n")
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
    sid = rec.get("sessionId")
    deadline = time.monotonic() + timeout
    interval = sp_runtime._float_env(
        "SESSION_PEERS_WAIT_POLL_INTERVAL", sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    )
    if interval <= 0:
        interval = sp_constants.WAIT_POLL_INTERVAL_DEFAULT
    while time.monotonic() < deadline:
        candidates = [
            item for item in sp_claude.read_claude_records() if item.get("sessionId") == sid
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
