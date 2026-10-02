"""Requests for the session-peers CLI."""

from __future__ import annotations
import os
import shlex
import time
import uuid as uuidlib
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import storage as sp_storage

def _bounded_timeout(value):
    timeout = sp_constants.REQUEST_TIMEOUT_DEFAULT if value is None else float(value)
    if timeout <= 0 or timeout > sp_constants.REQUEST_TIMEOUT_MAX:
        raise ValueError(
            "timeout must be greater than 0 and at most %.0f seconds"
            % sp_constants.REQUEST_TIMEOUT_MAX
        )
    return timeout


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except (FileNotFoundError, OSError):
        pass


def cleanup_expired_requests(now=None, dry_run=False):
    """Remove expired/orphaned request mailboxes; return removed request ids."""
    now = time.time() if now is None else float(now)
    removed = []
    try:
        names = os.listdir(sp_storage.request_dir())
    except OSError:
        return removed
    suffix = ".request.json"
    for name in names:
        if not name.endswith(suffix):
            continue
        request_id = name[: -len(suffix)]
        if not sp_runtime.is_uuid(request_id):
            continue
        path = sp_storage.request_path(request_id)
        data = sp_runtime.read_json(path, {}) or {}
        try:
            expires_at = float(data.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at > now:
            continue
        if not dry_run:
            _unlink_quiet(path)
            _unlink_quiet(sp_storage.request_reply_path(request_id))
        removed.append(request_id)
    for name in names:
        if not name.endswith(".reply.json"):
            continue
        request_id = name[: -len(".reply.json")]
        if not sp_runtime.is_uuid(request_id) or os.path.exists(sp_storage.request_path(request_id)):
            continue
        path = sp_storage.request_reply_path(request_id)
        try:
            stale = os.stat(path).st_mtime <= now - sp_constants.REQUEST_ORPHAN_TTL
        except OSError:
            stale = False
        if stale:
            if not dry_run:
                _unlink_quiet(path)
            if request_id not in removed:
                removed.append(request_id)
    return removed


def _request_envelope(request_id, message, timeout):
    script = sp_runtime.entrypoint_path()
    return (
        '<session-peers-request id="%s" timeout-seconds="%d">\n'
        "%s\n"
        "</session-peers-request>\n\n"
        "Reply contract: return the result to the waiting Codex turn, not its "
        "ordinary queue. Write the complete reply to a private temporary file, "
        "then run:\n"
        "%s reply --request %s --message-file <absolute-reply-file>\n"
        "Do not use SendMessage or `send --to codex:` for this request. The "
        "mailbox is single-use and expires with the timeout."
        % (
            request_id,
            int(timeout),
            sp_protocol.neutralise_request_markup(message),
            shlex.quote(script),
            request_id,
        )
    )


def _request_meta(request_id, requester_thread_id, rec, expires_at, reply_path):
    """The single-use mailbox metadata shared by `ask` and `dispatch`.

    Keeping one builder means the two entry points cannot drift in the fields
    `reply`, `await`, and `cleanup_expired_requests` all read back.
    """
    return {
        "request_id": request_id,
        "requester_thread_id": requester_thread_id,
        "target_session_id": rec.get("sessionId"),
        "target_session_name": rec.get("name"),
        "created_at": sp_runtime.now_iso(),
        "expires_at": expires_at,
        "reply_path": reply_path,
    }


def _reply_matches(response, request_id, target_session_id):
    """True when a reply file is the intended one and carries a text body."""
    return (
        isinstance(response, dict)
        and response.get("request_id") == request_id
        and response.get("session_id") == target_session_id
        and isinstance(response.get("message"), str)
    )


def _read_completed_reply(path, attempts=20):
    """Read a competing reply after its exclusive writer finishes."""
    for _index in range(attempts):
        value = sp_runtime.read_json(path, None)
        if isinstance(value, dict):
            return value
        time.sleep(0.01)
    return {}


def _claim_reply(reply_path):
    """Atomically take ownership of a reply file so it is consumed once.

    `os.rename` is atomic, so exactly one caller renames the single reply file
    away; a racing `await` sees it gone and stands down. Returns the parsed
    reply for the winner, or None if another consumer already claimed it.
    """
    claim_path = "%s.consumed.%d.%s" % (reply_path, os.getpid(), uuidlib.uuid4().hex)
    try:
        os.rename(reply_path, claim_path)
    except OSError:
        return None
    try:
        data = sp_runtime.read_json(claim_path, None)
    finally:
        _unlink_quiet(claim_path)
    return data if isinstance(data, dict) else None
