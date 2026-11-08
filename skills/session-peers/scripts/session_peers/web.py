"""Local web dashboard server for session-peers (PRD #161, chat mode PRD #162).

Standard-library ``http.server`` bound to 127.0.0.1. It serves three fixed static files and a
token-guarded JSON API built by ``webdata``; it never logs a request. By default it is read-only:
it writes to no store and refuses every method but GET. Started with ``--chat`` it also registers a
browser peer (``webpeer``) and accepts one write, ``POST /api/send``, whose checks run in this order:
host, method and target, token, ``Origin`` equal to the page's own origin, a single JSON
``Content-Type``, body framing (no ``Transfer-Encoding``, one digits-only ``Content-Length`` of 1 to
BODY_MAX), the body read inside the connection deadline, strict ``{to, text, ack}`` JSON (plus ``confirm`` for an ``@all`` to ``#general``), the refusals
(``#general`` unless an ``@all``, and an ``@all`` anywhere else; an ``@all`` to ``#general`` is a
preview answered ``200`` with a confirm nonce, then a confirm), and the send gate
(``webchat.SendGate``). A passed send is answered ``202 {"message_id", "state": "queued"}``: accepted in memory only, committed later by the
single worker in ``webchat``. Every refusal body is ``{}``.

Defences, in the order every request meets them: a connection slot (at most MAX_CONNECTIONS held
at once, extra connections are closed unanswered), a short HEADER_DEADLINE for the request line and
headers plus an absolute per-connection deadline, the Host header (DNS-rebinding guard), the method
(GET only), no request body, a 404 for a target that is not absolute-path shaped (no leading "/" or a
leading "//" in the raw request line), the ``X-Peers-Token`` header for ``/api/*``, then route and
parameter validation. Every response, errors included, leaves through ``Handler._send`` and carries
the same fixed security headers; error bodies are ``{}``.

Discovery (who is live, working or idle) shells out and walks registries, so it runs on one
single-flight worker thread under a process-wide lock inside ``storage.no_create()`` (no state
directory is created or chmod-ed by looking), and its result is cached for DISCOVERY_TTL seconds. A
request waits for it at most DISCOVERY_WAIT seconds and then serves the last result marked
``stale``, or an empty one marked ``pending``. A pass is bounded: it starts no new member checks
after DISCOVERY_PASS_BUDGET seconds (a check already running, lsof up to 20 s or an OpenCode API call
up to 30 s, can overrun it; requests never wait longer than DISCOVERY_WAIT) and checks at most
SUBPROCESS_MEMBERS_PER_PASS members that need a subprocess (Codex, OpenCode); members not reached are
``unverified`` and the result's ``discovery`` object carries ``truncated``. The members of each buddy
channel's current pair are checked first, so those limits cut the other members, and the channel
labels (``labels``) keep the previous pass's name and liveness for a pair member a pass could not
check. The pass runs with
``runtime.log`` muted on its thread.

Handler threads must never resolve CODEX_HOME: discovery's ``storage.codex_home_override`` mutates
``os.environ`` process-wide while a pass runs, so a handler reads only what discovery handed it
(``discovery["homes"]``).
"""

from __future__ import annotations
import hmac
import http
import http.server
import json
import os
import re
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from . import channels as sp_channels, constants as sp_constants, identity as sp_identity
from . import opencode as sp_opencode, runtime as sp_runtime, storage as sp_storage, webchat as sp_webchat
from . import webdata as sp_webdata, webpeer as sp_webpeer

MAX_CONNECTIONS = 16
CONNECTION_DEADLINE = 10.0  # seconds until the socket is shut down; the slot is held until the handler
# returns, and the webdata budget (not this timer) bounds the work a handler does
HEADER_DEADLINE = 2.0  # seconds for the request line and headers; then the connection is closed
RECV_TIMEOUT = 10.0  # seconds one recv may block
DISCOVERY_TTL = 5.0
DISCOVERY_WAIT = 7.0
DISCOVERY_PASS_BUDGET = 8.0  # seconds one pass may start new member checks
SUBPROCESS_MEMBERS_PER_PASS = 64  # Codex/OpenCode members checked per pass (each may shell out)
MEMBERS_PER_CHANNEL = 64
MEMBERS_TOTAL = 512
CHANNELS_MAX = 2048
ASSET_MAX = 4 * 1024 * 1024
BODY_MAX = 1024 * 1024  # largest POST body (Content-Length) a chat-mode send accepts
BODY_GRACE = 0.5  # the body read gives up this long before the connection cut, so the 408 can still be written
SEND_PATH = "/api/send"
SEND_RECIPIENTS_MAX = 64
CONFIRM_MAX = 128  # longest confirm nonce a send body may carry (a token_urlsafe(32) is 43 characters)
NUMERAL_MAX = 32  # longest JSON numeral a send body may carry (it has no numeric field)
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
JSON_TYPE = "application/json; charset=utf-8"
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
     "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)
_DIGITS_RE = re.compile(r"[0-9]{1,16}")
_JSON_TYPE_RE = re.compile(r"application/json(?:\s*;\s*charset=utf-8)?", re.IGNORECASE)
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,65536}")
_ACTIVITY = {"busy": "working", "idle": "idle"}
_OC_ACTIVITY = {"running": "working", "idle": "idle"}
DRAIN_READS = 16

_DISCOVERY_LOCK = threading.Lock()

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _empty_discovery(**flags):
    out = {"general": ([], None, []), "liveness": {}, "activity": {}, "names": {}, "homes": {},
           "labels": {}, "stale": False, "pending": False}
    out.update(flags)
    return out


def _read_records():
    """``{key: record}`` of every readable stored channel, each read once with its own budget.

    ``webdata._read_record`` opens without following links or blocking, so a planted FIFO or symlink
    reads as None and is skipped; a fresh budget per record keeps the total of the records from
    starving the later ones."""
    records = {}
    for key in sp_channels.list_keys()[:CHANNELS_MAX]:
        record = sp_webdata._read_record(key, sp_webdata.Budget())
        if record is not None:
            records[key] = record
    return records


def _rosters(records):
    """``(general_discovery, [member lists])``: #general's members, then every record's."""
    general, coverage, gaps = sp_channels.general_members()
    general = [m for m in general if isinstance(m, str)]
    rosters = [general[:MEMBERS_PER_CHANNEL]]
    rosters.extend(record["members"][:MEMBERS_PER_CHANNEL] for record in records.values())
    return (general, coverage, gaps), rosters


def _pair_identities(records):
    """``(live, named)``: the typed ids whose liveness a label needs (both members of each known pair)
    and those whose name it needs (every lead and current buddy), first CHANNEL_MAX_CHANNELS buddy
    records by key only, as ``channels.label_context``."""
    live, named = [], []
    buddies = sorted((r for r in records.values() if r.get("kind") == "buddy"), key=lambda r: r["key"])
    for record in buddies[:sp_constants.CHANNEL_MAX_CHANNELS]:
        lead, buddy, pair = sp_channels.pair_of(record)
        for typed in (lead, buddy):
            if typed and typed not in named:
                named.append(typed)
            if pair == sp_channels.PAIR_KNOWN and typed not in live:
                live.append(typed)
    return live, named


def _registry_view(typed, kind, ident, live, by_sid):
    """``(activity, name)`` of a Claude session or Codex shim from its registry record."""
    matches = sp_channels.registry_matches(kind, ident, by_sid)
    name = sp_channels.registry_name(kind, ident, by_sid)
    activity = "unknown"
    if live:
        for record in matches:
            if record.get("status") in _ACTIVITY:
                activity = _ACTIVITY[record["status"]]
                break
    return activity, name


def _opencode_view(members):
    """``({typed: activity}, {typed: title})`` of the OpenCode members; unreadable means unknown."""
    try:
        active = sp_opencode.active_ids()
    except Exception:
        active = None
    titles = sp_channels.opencode_titles()
    activity, names = {}, {}
    for typed in members:
        sid = typed.partition(":")[2]
        activity[typed] = "unknown" if active is None else _OC_ACTIVITY.get(sp_opencode.state_of(sid, active), "unknown")
        if titles.get(sid):
            names[typed] = titles[sid]
    return activity, names


def run_discovery(requester=None, previous=None):
    """One discovery pass: the ``discovery`` dict ``webdata.state_view`` takes. Reads only, never creates.

    ``requester`` (chat mode: the browser peer's sid) adds ``replies_left``: for each Codex member whose
    home was found, the replies that requester has left in the member's saved reply budget (None when it
    cannot be read). It is worked out here, on the discovery thread, never on a request thread.

    ``labels`` are ``channels.derive_labels`` over the channel records read here (the same records
    ``channel list`` labels). The identities of every known buddy pair are checked before any other
    member, so the pass limits cut the roster, not the labels; an identity this pass still could not
    check (``label_names``, ``label_liveness`` hold what labels used) keeps ``previous``'s name and
    liveness (the previous pass's result), else its short id and ``unverified``, and the pass is
    ``truncated``."""
    with _DISCOVERY_LOCK, sp_storage.no_create(), sp_runtime.quiet():
        records = _read_records()
        general, rosters = _rosters(records)
        live_ids, named_ids = _pair_identities(records)
        members, seen = [], set()
        for roster in [live_ids] + rosters:
            for typed in roster:
                if typed not in seen and len(members) < MEMBERS_TOTAL:
                    seen.add(typed)
                    members.append(typed)
        cache, liveness, activity, names, homes = {}, {}, {}, {}, {}
        deadline, subprocess_left, truncated = time.monotonic() + DISCOVERY_PASS_BUDGET, SUBPROCESS_MEMBERS_PER_PASS, False
        checked, unchecked = set(), []
        for typed in members:
            if time.monotonic() >= deadline:
                liveness[typed], truncated = "unverified", True
                unchecked.append(typed)
                continue
            if not typed.startswith("cc:"):
                if subprocess_left <= 0:
                    liveness[typed], truncated = "unverified", True
                    unchecked.append(typed)
                    continue
                subprocess_left -= 1
            checked.add(typed)
            try:
                liveness[typed] = sp_channels.member_liveness(typed, cache)
            except Exception:
                liveness[typed] = "unverified"
        by_sid = {}
        if any(not m.startswith("oc:") for m in members) or any(not m.startswith("oc:") for m in named_ids):
            by_sid = sp_channels.registry_by_session(cache)
        for typed in members:
            kind, _sep, ident = typed.partition(":")
            if kind in ("cc", "codex"):
                activity[typed], name = _registry_view(typed, kind, ident, liveness[typed] == "live", by_sid)
                if name:
                    names[typed] = name
            if kind == "codex" and typed in checked:
                try:
                    homes[ident] = sp_storage.thread_home(ident)
                except Exception:
                    pass
        oc_members = [m for m in members if m.startswith("oc:") and m in checked]
        if oc_members and time.monotonic() < deadline:
            oc_activity, oc_names = _opencode_view(oc_members)
            activity.update(oc_activity)
            names.update(oc_names)
        elif oc_members:
            truncated = True
        prev = previous if isinstance(previous, dict) else {}
        prev_names = prev.get("label_names") if isinstance(prev.get("label_names"), dict) else {}
        prev_live = prev.get("label_liveness") if isinstance(prev.get("label_liveness"), dict) else {}
        label_names, label_live = {}, {}
        oc_named = [t for t in named_ids if t.startswith("oc:")]
        for typed in named_ids:
            kind, _sep, ident = typed.partition(":")
            if kind in ("cc", "codex"):  # one registry read, no allowance
                name = sp_channels.registry_name(kind, ident, by_sid)
                if name:
                    label_names[typed] = name
        if oc_named and time.monotonic() < deadline:
            try:
                label_names.update(sp_channels.session_names(oc_named, cache))
            except Exception:
                pass
        elif oc_named:  # out of time: what the previous pass found stays
            truncated = True
            label_names.update({t: prev_names[t] for t in oc_named if isinstance(prev_names.get(t), str)})
        for typed in live_ids:
            if typed in checked:
                label_live[typed] = liveness[typed]
            else:
                label_live[typed] = prev_live.get(typed) if prev_live.get(typed) in ("live", "gone", "unverified") else "unverified"
        labels = sp_channels.derive_labels(
            list(records.values()), {**names, **label_names}, {**liveness, **label_live})
        found = {"general": general, "liveness": liveness, "activity": activity, "names": names,
                 "homes": homes, "stale": False, "pending": False, "truncated": truncated,
                 "unchecked": unchecked, "labels": labels, "label_names": label_names,
                 "label_liveness": label_live}
        if requester is not None:
            replies = {}
            for typed in members:
                kind, _sep, ident = typed.partition(":")
                if kind == "codex" and ident in homes:
                    left = sp_webdata.codex_reply_budget(ident, homes[ident], requester=requester)
                    replies[typed] = left.get("remaining") if isinstance(left, dict) else None
            found["replies_left"] = replies
        return found


class Discovery:
    """Single-flight, cached discovery: one worker thread, one result per DISCOVERY_TTL."""

    def __init__(self, requester=None):
        self._requester = requester  # chat mode: a callable giving the browser peer's sid, or None
        self._cond = threading.Condition()
        self._result = None
        self._at = 0.0
        self._running = False
        self._failed = False
        self._done = 0

    def _work(self):
        result = None
        try:
            requester = self._requester() if self._requester is not None else None
            with self._cond:
                kwargs = {"previous": self._result} if self._result is not None else {}
            result = run_discovery(requester, **kwargs) if requester is not None else run_discovery(**kwargs)
        except Exception:  # a failed pass marks the answer stale/pending; its cause is never logged
            result = None
        finally:
            with self._cond:
                if result is not None:
                    self._result, self._at = result, time.monotonic()
                self._failed = result is None
                self._done += 1
                self._running = False
                self._cond.notify_all()

    def _start_locked(self):
        worker = threading.Thread(target=self._work, name="peers-web-discovery", daemon=True)
        self._running = True
        try:
            worker.start()
        except Exception:
            self._running = False
            return False
        return True

    def _fallback_locked(self):
        if self._result is not None:
            return dict(self._result, stale=True)
        return _empty_discovery(pending=True)

    def get(self, wait=None):
        wait = DISCOVERY_WAIT if wait is None else wait
        with self._cond:
            now = time.monotonic()
            if self._result is not None and not self._failed and now - self._at < DISCOVERY_TTL:
                return dict(self._result)
            seen = self._done
            if not self._running and not self._start_locked():
                return self._fallback_locked()
            deadline = now + wait
            while self._done == seen:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            if self._done != seen and not self._failed and self._result is not None:
                return dict(self._result)
            return self._fallback_locked()


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class _Refused(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 32

    def server_bind(self):
        # HTTPServer.server_bind resolves the host's fully qualified name (reverse DNS); not needed here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(self, port=0, chat=False, clock=None):
        self.token = secrets.token_bytes(32).hex()
        # Chat mode: `chat` holds the (not yet started) services; None keeps the server read-only.
        self.chat = sp_webchat.Chat(clock or time.monotonic, lambda: _DISCOVERY_LOCK) if chat else None
        self.discovery = Discovery(self._requester if chat else None)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._holders = set()
        self._holders_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def port(self):
        return self.server_address[1]

    def _requester(self):
        state = self.chat.state if self.chat is not None else None
        return state.uuid if state is not None else None

    def _release(self, request):
        with self._holders_lock:
            held = request in self._holders
            self._holders.discard(request)
        if held:
            self._slots.release()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()  # over the cap: no slot was taken, so none may be released
            return
        with self._holders_lock:
            self._holders.add(request)
        try:
            worker = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
            worker.start()
        except BaseException:
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request):
        """Finish a connection: send FIN, discard what the client already sent, close, free the slot.

        Closing with unread request bytes (a refused body) would reset the connection and could
        discard the response the client has not read yet; the drain is non-blocking and bounded."""
        try:
            try:
                request.shutdown(socket.SHUT_WR)
                request.setblocking(False)
                for _ in range(DRAIN_READS):
                    if not request.recv(65536):
                        break
            except OSError:
                pass
            self.close_request(request)
        finally:
            self._release(request)

    def handle_error(self, request, client_address):
        pass  # a traceback could carry request data; nothing is logged


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "session-peers-web"
    sys_version = ""

    def setup(self):
        self.timeout = RECV_TIMEOUT
        super().setup()

    def log_message(self, format, *args):
        pass

    def handle(self):
        self._started = time.monotonic()
        timer = threading.Timer(CONNECTION_DEADLINE, self._cut)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        finally:
            timer.cancel()

    def _cut(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- responses ---------------------------------------------------------

    def _send(self, status, body, ctype=JSON_TYPE):
        try:
            reason = http.HTTPStatus(status).phrase
        except ValueError:
            reason = "Unknown"
        lines = ["HTTP/1.0 %d %s" % (status, reason), "Content-Type: %s" % ctype, "Content-Length: %d" % len(body)]
        lines.extend("%s: %s" % item for item in SECURITY_HEADERS)
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        self.close_connection = True
        self.wfile.write(head if self.command == "HEAD" else head + body)

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii"))

    def send_error(self, code, message=None, explain=None):
        self._send(code, b"{}")

    # -- request loop ------------------------------------------------------

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            self.send_error(400)
            return False
        return True

    def _cut_headers(self):
        self._headers_cut = True
        self._cut()

    def handle_one_request(self):
        self._headers_cut = False
        header_timer = threading.Timer(HEADER_DEADLINE, self._cut_headers)
        header_timer.daemon = True
        header_timer.start()
        try:
            try:
                self.raw_requestline = self.rfile.readline(65537)
                if len(self.raw_requestline) > 65536:
                    self.requestline = self.request_version = self.command = ""
                    self.send_error(414)
                    return
                if not self.raw_requestline:
                    self.close_connection = True
                    return
                parsed = self.parse_request()
            finally:
                header_timer.cancel()
            if self._headers_cut:  # the deadline truncated the headers: answer nothing
                self.close_connection = True
                return
            if not parsed:
                return
            try:
                self._dispatch()
            except _Refused as refused:
                self.send_error(refused.status)
            except sp_webdata.WebDataError as exc:
                self.send_error(exc.status)
            except OSError:
                raise
            except Exception:
                self.send_error(500)
            self.wfile.flush()
        except OSError:  # includes a timeout, a reset and the deadline's shutdown
            self.close_connection = True

    # -- checks, in order --------------------------------------------------

    def _single(self, name):
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else None

    def _check_host(self):
        host = self._single("Host")
        port = self.server.port
        if host not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            raise _Refused(403)

    def _check_body(self):
        lengths = self.headers.get_all("Content-Length")
        if lengths is not None and lengths != ["0"]:
            raise _Refused(400)
        if self.headers.get_all("Transfer-Encoding") is not None:
            raise _Refused(400)

    def _check_token(self):
        value = self._single("X-Peers-Token")
        try:
            given = value.encode("latin-1") if value is not None else None
        except UnicodeEncodeError:
            given = None
        expected = self.server.token.encode("ascii")
        if given is None or not hmac.compare_digest(given, expected):
            raise _Refused(403)

    def _parts(self):
        """The split request target; 404 for one that is not absolute-path shaped."""
        # Newer Pythons collapse a leading "//" in self.path, so judge the raw request line.
        words = self.requestline.split()
        raw_target = words[1] if len(words) >= 2 else self.path
        target = self.path
        if not raw_target.startswith("/") or raw_target.startswith("//") or not target.startswith("/"):
            raise _Refused(404)
        return urllib.parse.urlsplit(target)

    def _dispatch(self):
        self._check_host()
        if self.command == "POST" and self.server.chat is not None:
            return self._post()
        if self.command != "GET":
            raise _Refused(405)
        self._check_body()
        parts = self._parts()
        path = parts.path
        if path.startswith("/api/"):
            self._check_token()
            return self._api(path, parts.query)
        if path in _ASSETS:
            return self._static(path)
        raise _Refused(404)

    # -- POST /api/send (chat mode) ------------------------------------------

    def _post(self):
        """The checks of a send, in order; each refusal is an empty-object response."""
        parts = self._parts()
        if parts.path != SEND_PATH:
            raise _Refused(405)
        self._query(parts.query, ())
        self._check_token()
        self._check_origin()
        self._check_content_type()
        length = self._content_length()
        body = self._read_body(length)
        to, text, ack, confirm = self._parse_send(body)
        is_all = sp_channels.ALL_RE.match(text) is not None
        if to == sp_constants.CHANNEL_GENERAL or is_all:
            # Only an `@all` to #general is open (preview, then confirm); a plain #general post and an
            # `@all` anywhere else stay refused.
            if not (to == sp_constants.CHANNEL_GENERAL and is_all):
                raise _Refused(403)
            return self._post_all(text, confirm)
        chat = self.server.chat
        if not chat.ready:
            raise _Refused(503)
        mid = chat.accept(to, text, ack)
        if mid is None:
            raise _Refused(429)
        self._send_json(202, {"message_id": mid, "state": "queued"})

    def _post_all(self, text, confirm):
        """An `@all` to #general. ACK is always requested, so the request's ``ack`` is not consulted."""
        chat = self.server.chat
        if not chat.ready:
            raise _Refused(503)
        if confirm is not None:
            status, mid = chat.confirm_all(confirm, text)
            if status == sp_webchat.CONFIRM_STALE:
                raise _Refused(409)
            if status == sp_webchat.CONFIRM_LIMITED:
                raise _Refused(429)
            return self._send_json(202, {"message_id": mid, "state": "queued"})
        # Preview: the cached discovery only, no resolution; nothing is accepted, charged or queued.
        found = self.server.discovery.get()
        if found.get("pending"):
            raise _Refused(409)
        typed = chat.state.typed
        liveness = found.get("liveness") or {}
        unchecked = set(found.get("unchecked") or ())
        recipients, excluded = [], []
        for member in dict.fromkeys(found["general"][0]):
            if member == typed:
                continue
            # Only an incomplete check of #general itself refuses; `truncated` may concern other channels.
            if member in unchecked or member not in liveness:
                raise _Refused(409)
            if liveness.get(member) == "gone":
                excluded.append({"member": member, "reason": "gone"})
            else:
                recipients.append(member)
        if not recipients or len(recipients) > sp_channels.ALL_MAX_RECIPIENTS:
            raise _Refused(409)
        recipients.sort()
        nonce = chat.preview_all(text, recipients)
        reply = {
            "confirm": nonce, "expires_in": sp_webchat.NONCE_TTL, "recipients": recipients,
            "excluded": sorted(excluded, key=lambda x: x["member"]),
        }
        if found.get("stale"):
            reply["stale"] = True
        self._send_json(200, reply)

    def _check_origin(self):
        origin = self._single("Origin")
        if origin is None or origin != "http://" + self._single("Host"):
            raise _Refused(403)

    def _check_content_type(self):
        values = self.headers.get_all("Content-Type") or []
        if len(values) != 1 or not _JSON_TYPE_RE.fullmatch(values[0]):
            raise _Refused(415)

    def _content_length(self):
        if self.headers.get_all("Transfer-Encoding") is not None:
            raise _Refused(400)
        values = self.headers.get_all("Content-Length") or []
        if len(values) != 1 or not _DIGITS_RE.fullmatch(values[0]):
            raise _Refused(400)
        length = int(values[0])
        if length < 1:
            raise _Refused(400)
        if length > BODY_MAX:
            raise _Refused(413)
        return length

    def _read_body(self, length):
        """Exactly ``length`` bytes within the connection's remaining deadline; 408 on a short read or a timeout."""
        deadline = self._started + CONNECTION_DEADLINE - BODY_GRACE
        chunks, have = [], 0
        while have < length:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Refused(408)
            try:
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(65536, length - have))
            except OSError:  # includes socket.timeout
                raise _Refused(408)
            if not chunk:
                raise _Refused(408)
            chunks.append(chunk)
            have += len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_send(body):
        """``(to, text, ack, confirm)`` of a strict ``{to, text, ack}`` JSON body, or one that also carries
        ``confirm`` (a bounded string, allowed only for an `@all` to #general; None when absent). 400 for anything else."""
        def no_duplicates(pairs):
            keys = [k for k, _v in pairs]
            if len(set(keys)) != len(keys):
                raise ValueError("duplicate key")
            return dict(pairs)

        def no_constant(_name):
            raise ValueError("not JSON")

        def no_long_number(text):
            # No field is numeric; before Python 3.11 int() of a huge numeral is quadratic work.
            if len(text) > NUMERAL_MAX:
                raise ValueError("numeral too long")
            return int(text)

        def no_long_float(text):
            if len(text) > NUMERAL_MAX:
                raise ValueError("numeral too long")
            return float(text)

        try:
            obj = json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constant,
                             parse_int=no_long_number, parse_float=no_long_float)
        except (ValueError, RecursionError):  # includes UnicodeDecodeError
            raise _Refused(400)
        if not isinstance(obj, dict) or set(obj) not in ({"to", "text", "ack"}, {"to", "text", "ack", "confirm"}):
            raise _Refused(400)
        to, text, ack, confirm = obj["to"], obj["text"], obj["ack"], obj.get("confirm")
        if type(text) is not str or not 1 <= len(text) <= sp_constants.MAX_TEXT_CHARS or type(ack) is not bool:
            raise _Refused(400)
        if "confirm" in obj:
            # A nonce belongs to an `@all` to #general and to nothing else.
            if (type(confirm) is not str or not 1 <= len(confirm) <= CONFIRM_MAX or to != "#" + sp_constants.CHANNEL_GENERAL
                    or sp_channels.ALL_RE.match(text) is None):
                raise _Refused(400)
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate (a JSON ud800 escape) can be neither journaled nor delivered
            raise _Refused(400)
        if type(to) is str:
            if not to.startswith("#") or not sp_constants.CHANNEL_NAME_RE.fullmatch(to[1:]):
                raise _Refused(400)
            return to[1:], text, ack, confirm
        if type(to) is not list or not 1 <= len(to) <= SEND_RECIPIENTS_MAX or len(set(map(str, to))) != len(to):
            raise _Refused(400)
        for address in to:
            if type(address) is not str:
                raise _Refused(400)
            try:
                kind = sp_identity.parse_address(address)["kind"]
            except ValueError:
                raise _Refused(400)
            if kind == "web":
                raise _Refused(400)
        return to, text, ack, confirm

    # -- static ------------------------------------------------------------

    def _static(self, path):
        name, ctype = _ASSETS[path]
        try:
            with open(os.path.join(ASSET_DIR, name), "rb") as fh:
                body = fh.read(ASSET_MAX + 1)
        except OSError:
            raise _Refused(404)
        if len(body) > ASSET_MAX:
            raise _Refused(404)
        self._send(200, body, ctype)

    # -- API ---------------------------------------------------------------

    @staticmethod
    def _query(query, allowed):
        try:
            pairs = urllib.parse.parse_qsl(
                query, keep_blank_values=True, strict_parsing=bool(query), encoding="utf-8", errors="strict",
                max_num_fields=16)
        except ValueError:
            raise _Refused(400)
        out = {}
        for key, value in pairs:
            if key not in allowed or key in out:
                raise _Refused(400)
            out[key] = value
        return out

    @staticmethod
    def _number(value, low=0, high=None):
        if value is None:
            return None
        if not _DIGITS_RE.fullmatch(value):
            raise _Refused(400)
        number = int(value)
        if number < low or (high is not None and number > high):
            raise _Refused(400)
        return number

    def _api(self, path, query):
        if path == "/api/state":
            self._query(query, ())
            found = self.server.discovery.get()
            chat = self.server.chat
            view = sp_webdata.state_view(found, chat=chat.info() if chat is not None and chat.ready else None)
            if found.get("truncated"):
                view["discovery"]["truncated"] = True
            return self._send_json(200, view)
        if path.startswith("/api/channel/"):
            key = path[len("/api/channel/"):]
            if not sp_constants.CHANNEL_NAME_RE.fullmatch(key):
                raise _Refused(400)
            params = self._query(query, ("before", "limit"))
            before = self._number(params.get("before"))
            limit = self._number(params.get("limit"), 1, sp_webdata.PAGE_MAX)
            kwargs = {}
            if before is not None:
                kwargs["before"] = before
            if limit is not None:
                kwargs["limit"] = limit
            if key == sp_constants.CHANNEL_GENERAL:
                found = self.server.discovery.get()
                if not found.get("pending"):
                    kwargs["members"] = list(found["general"][0])
            return self._send_json(200, sp_webdata.channel_page(key, **kwargs))
        if path == "/api/changes":
            since = self._query(query, ("since",)).get("since")
            if since is None or not _CURSOR_RE.fullmatch(since):
                raise _Refused(400)
            chat = self.server.chat
            browser = chat.state if chat is not None and chat.ready else None
            return self._send_json(200, sp_webdata.changes(since, browser=browser))
        if path == "/api/all":
            token = self._query(query, ("next",)).get("next")
            if token is not None and not _CURSOR_RE.fullmatch(token):
                raise _Refused(400)
            return self._send_json(200, sp_webdata.all_channels(token))
        if path == "/api/search":
            params = self._query(query, ("q", "scope", "next"))
            q = params.get("q")
            if q is None:
                raise _Refused(400)
            scope = params.get("scope", "all")
            if scope != "all" and not sp_constants.CHANNEL_NAME_RE.fullmatch(scope):
                raise _Refused(400)
            token = params.get("next")
            if token is not None and not _CURSOR_RE.fullmatch(token):
                raise _Refused(400)
            return self._send_json(200, sp_webdata.search(q, scope, token))
        raise _Refused(404)


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def cmd_web(args):
    port = args.port
    chat = bool(getattr(args, "chat", False))
    if port != 0 and not 1024 <= port <= 65535:
        sys.stderr.write("error: --port must be 0 (any free port) or 1024-65535\n")
        return 2
    try:
        server = Server(port, chat=chat)
    except OSError as exc:
        sys.stderr.write("error: cannot listen on 127.0.0.1:%d: %s\n" % (port, exc.strerror or exc))
        return 1
    stopping = False

    def on_signal(_signum, _frame):
        # Only a plain assignment and `request_stop` (one attribute assignment): neither takes a lock,
        # so a signal that lands while the main thread is waiting or inside its own teardown cannot
        # deadlock it. (`threading.Event.set` takes the event's internal lock, so no Event is used.)
        # The main loop polls the flag and does the work in its `finally`.
        nonlocal stopping
        stopping = True
        if server.chat is not None:
            server.chat.request_stop()

    previous = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, on_signal)
    serving = None
    try:
        if chat:
            try:
                server.chat.start()
            except (sp_webpeer.WebPeerError, OSError) as exc:
                if stopping:
                    return 0  # asked to stop while starting
                sys.stderr.write("error: %s\n" % exc)
                return 1
        serving = threading.Thread(target=server.serve_forever, name="peers-web-serve", daemon=True)
        serving.start()
        print("http://127.0.0.1:%d/#token=%s" % (server.port, server.token), flush=True)
        while not stopping:
            time.sleep(0.5)
    finally:
        if serving is not None:
            server.shutdown()
            serving.join(5)
        if server.chat is not None:
            server.chat.stop()
        server.server_close()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 0
