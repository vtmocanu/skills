"""OpenCode V2 sessions for the session-peers CLI.

Every call goes through `opencode api <operationId>`, the CLI's own
authenticated client for the background service, so no auth or state file is
read here. Only session metadata (`session.get`, `session.list`,
`session.active`) is read; message bodies never are. `opencode api` starts a
stopped service (measured on 2.0.30, `service.ensure`), so `service status` is
checked first and a stopped service is reported, never started.
"""

from __future__ import annotations
import json
import os
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import codex as sp_codex

SESSION_ID_RE = sp_constants.OPENCODE_SESSION_ID_RE
API_TIMEOUT = 15.0
MAX_SESSION_PAGES = 50


class OpenCodeError(Exception):
    """An OpenCode call that failed; `unavailable` means the service was not asked."""

    def __init__(self, message, unavailable=False):
        super().__init__(message)
        self.unavailable = unavailable


class OpenCodeNotFound(OpenCodeError):
    """The service answered that the session does not exist."""


def is_session_id(value) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_RE.match(value))


def _binary():
    return os.environ.get("SESSION_PEERS_OPENCODE_BIN") or "opencode"


def service_url():
    """The running background service's URL; raises when stopped or absent."""
    rc, out, err = sp_runtime.run_cmd([_binary(), "service", "status"], timeout=API_TIMEOUT)
    if rc == 127:
        raise OpenCodeError("opencode is not installed", unavailable=True)
    text = out.strip()
    if rc != 0 or not text.startswith(("http://", "https://")):
        raise OpenCodeError(
            "the OpenCode service is not running (%s)" % (text or err.strip() or "rc %d" % rc),
            unavailable=True,
        )
    return text


def api(operation, params=None, data=None):
    """One `opencode api` call; the parsed JSON body, or OpenCodeError.

    The service is checked first on every call: `opencode api` would start it.
    """
    service_url()
    argv = [_binary(), "api", operation]
    for key, value in sorted((params or {}).items()):
        argv += ["--param", "%s=%s" % (key, value)]
    if data is not None:
        payload = json.dumps(data, ensure_ascii=False)
        if sp_runtime.utf8_len(payload) > sp_runtime.argv_text_budget():
            # The body is one argv element: past this the exec fails with E2BIG.
            raise ValueError(
                "message too large for one `opencode api` argument (%d bytes encoded, "
                "budget %d); send a shorter message or a file path"
                % (sp_runtime.utf8_len(payload), sp_runtime.argv_text_budget())
            )
        argv += ["--data", payload]
    rc, out, err = sp_runtime.run_cmd(argv, timeout=API_TIMEOUT)
    try:
        body = json.loads(out) if out.strip() else None
    except ValueError:
        body = None
    if rc != 0:
        tag = body.get("_tag") if isinstance(body, dict) else None
        detail = (body.get("message") if isinstance(body, dict) else None) or err.strip() or "rc %d" % rc
        if tag == "SessionNotFoundError" or "404" in err:
            raise OpenCodeNotFound(detail)
        raise OpenCodeError("opencode api %s failed: %s" % (operation, detail))
    if not isinstance(body, dict):
        raise OpenCodeError("opencode api %s returned no JSON object" % operation)
    return body


def _view(info):
    location = info.get("location") if isinstance(info.get("location"), dict) else {}
    times = info.get("time") if isinstance(info.get("time"), dict) else {}
    return {
        "id": info.get("id"),
        "title": info.get("title") if isinstance(info.get("title"), str) else None,
        "parent_id": info.get("parentID"),
        "directory": location.get("directory"),
        "updated_ms": times.get("updated"),
    }


def get_session(session_id):
    """Metadata of one session; OpenCodeNotFound when it does not exist."""
    if not is_session_id(session_id):
        raise OpenCodeError("%r is not an OpenCode session id (ses_...)" % session_id)
    info = api("session.get", {"sessionID": session_id}).get("data")
    if not isinstance(info, dict) or info.get("id") != session_id:
        raise OpenCodeError("the service returned no record for %s" % session_id)
    return _view(info)


def active_ids():
    """Ids of sessions with a running agent loop. Absent means idle, not dead."""
    data = api("session.active").get("data")
    if not isinstance(data, dict):
        raise OpenCodeError("session.active returned an unexpected shape")
    return {sid for sid in data if is_session_id(sid)}


def recent_sessions(hours=None):
    """Top-level sessions updated within ``hours`` (newest first)."""
    if hours is None:
        hours = sp_runtime._float_env("SESSION_PEERS_OPENCODE_RECENT_HOURS", 24.0)
    cutoff_ms = (time.time() - hours * 3600.0) * 1000.0
    out = []
    cursor, seen_cursors, seen_ids = None, set(), set()
    for _page in range(MAX_SESSION_PAGES):
        params = {"parentID": "null", "limit": "100"}
        if cursor is None:
            params["order"] = "desc"
        else:
            params["cursor"] = cursor
        body = api("session.list", params)
        data, page_cursor = body.get("data"), body.get("cursor")
        if not isinstance(data, list) or not isinstance(page_cursor, dict):
            raise OpenCodeError("session.list returned an unexpected pagination shape")
        for info in data:
            if not isinstance(info, dict) or not is_session_id(info.get("id")):
                raise OpenCodeError("session.list returned an invalid session record")
            view = _view(info)
            if view["parent_id"] is not None or view["id"] in seen_ids:
                continue
            seen_ids.add(view["id"])
            if isinstance(view["updated_ms"], (int, float)) and view["updated_ms"] < cutoff_ms:
                continue
            out.append(view)
        # Cursor order is opaque: an old row cannot prove later rows are old.
        if "next" not in page_cursor:
            raise OpenCodeError("session.list returned no next cursor")
        cursor = page_cursor["next"]
        if cursor is None:
            return out
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise OpenCodeError("session.list returned an invalid or repeated cursor; discovery incomplete")
        seen_cursors.add(cursor)
    raise OpenCodeError("session.list exceeded the page limit; discovery incomplete")


def state_of(session_id, active=None):
    """`running`, `idle`, or `unknown` when activity could not be read."""
    if active is None:
        try:
            active = active_ids()
        except OpenCodeError:
            return "unknown"
    return "running" if session_id in active else "idle"


def resolve_session(target, exclude=None):
    """One session by `ses_` id or by exact, unique recent title.

    ``exclude`` keeps the caller out of title matching. An absent or stopped
    service resolves nothing, so it never makes a bare name ambiguous.
    """
    target = str(target or "")
    try:
        if is_session_id(target):
            return get_session(target)
        matches = [
            s for s in recent_sessions() if s["title"] == target and s["id"] != exclude
        ]
    except OpenCodeNotFound:
        raise sp_codex.ResolveNotFound("no OpenCode session with id %r" % target)
    except OpenCodeError as exc:
        if exc.unavailable and not is_session_id(target):
            raise sp_codex.ResolveNotFound("no OpenCode session titled %r (%s)" % (target, exc))
        raise sp_codex.ResolveError("cannot resolve OpenCode session %r: %s" % (target, exc))
    if not matches:
        raise sp_codex.ResolveNotFound(
            "no recent OpenCode session titled %r; address it as oc:<ses_id>" % target
        )
    if len(matches) > 1:
        raise sp_codex.ResolveError(
            "%r titles %d OpenCode sessions (%s); address one as oc:<ses_id>"
            % (target, len(matches), ", ".join(m["id"] for m in matches))
        )
    return matches[0]


def current_session_id():
    """This process's OpenCode session, from the env its shell tool exports.

    OpenCode 2.0.30's shell tool sets OPENCODE_SESSION_ID (and OPENCODE=1) on
    every command it runs. A malformed value is no identity.
    """
    sid = os.environ.get("OPENCODE_SESSION_ID")
    return sid if is_session_id(sid) else None


def deliver(session_id, text, metadata, resume=False):
    """Queue one input for the session's next turn; the inbox item id.

    `delivery: queue` never steers a running turn. The service runs the turn
    itself when resume is true, even without a TUI being attached.
    """
    if len(text) > sp_constants.MAX_TEXT_CHARS or sp_runtime.utf8_len(text) > sp_constants.MAX_TEXT_CHARS:
        raise ValueError(
            "message exceeds the %d-character/UTF-8-byte peer cap" % sp_constants.MAX_TEXT_CHARS
        )
    body = api(
        "session.prompt",
        {"sessionID": session_id},
        {"text": text, "delivery": "queue", "resume": resume, "metadata": {"session_peers": metadata}},
    )
    item = body.get("data")
    return item.get("id") if isinstance(item, dict) else None
