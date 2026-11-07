"""The browser peer: one registry record, socket and journaled inbox for `web --chat`.

It stands in the fabric as `web:<uuid>`. Peers reach it with the same user
frames they send a Claude session; an inbound message counts as received only
after `journal.record_message` returns, and only then is it shown and
acknowledged on the sender's connection (`peer_inbox_ack`, or
`peer_inbox_refused` when the journal refused it).
"""

from __future__ import annotations
import errno
import fcntl
import getpass
import json
import os
import re
import socket
import threading
import time
import uuid
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, identity as sp_identity, journal as sp_journal
from . import process as sp_process, storage as sp_storage

LOCK_DIR = "~/.codex/session-peers"
LOCK_NAME = "web-chat.lock"
LOCK_HELD = "another peers.py web --chat is running for this user"
_DETAIL_MAX = 300


class WebPeerError(RuntimeError):
    """The browser peer cannot start (lock held, unsafe lock directory, bad socket)."""


def _noop(*_args, **_kwargs):
    return None


def peer_name():
    """`web-<os user>`, restricted to the alias rules shims use."""
    try:
        user = getpass.getuser()
    except Exception:  # no passwd entry in a minimal container
        user = ""
    user = re.sub(r"[^A-Za-z0-9._-]", "-", user)[:59] or "user"
    return "web-" + user


class BrowserPeer:
    """One process standing in for the web chat; its inbox feeds ``on_inbound``.

    ``on_inbound(entry)`` gets ``{message_id, orig_msg_id, sender, sender_name,
    body, at}`` after the journal write; ``on_status(frame, sender)`` gets each
    `peer_message_status` control frame with the verified sender (or None).
    """

    def __init__(self, on_inbound=None, on_status=None):
        self.sid = str(uuid.uuid4())
        self.name = peer_name()
        self.typed = "web:" + self.sid
        self.on_inbound = on_inbound or _noop
        self.on_status = on_status or _noop
        self.sock_dir = sp_claude.default_socket_dir()
        self.socket_path = os.path.join(self.sock_dir, "%d.sock" % os.getpid())
        self.record_path = os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % os.getpid())
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.srv = None
        self._lock_fd = None
        self._bound = False
        self._record_written = False
        self._mutex = threading.Lock()
        self._cleaned = False
        self._clients = threading.Semaphore(sp_constants.MAX_CONCURRENT_CLIENTS)
        self._accept = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        try:
            self._take_lock()
            self._bind()
            self._write_record()
            self._accept = threading.Thread(target=self._accept_loop, name="web-peer-accept", daemon=True)
            self._accept.start()
        except BaseException:
            self._cleanup()
            raise
        return self

    def stop(self):
        """Idempotent; safe from a signal handler (an event and a cleanup)."""
        self.stop_event.set()
        self._cleanup()

    def _cleanup(self):
        with self._mutex:
            if self._cleaned:
                return
            self._cleaned = True
        # Only what this instance created: a start that lost the lock must not
        # remove the files of the instance that holds it.
        paths = []
        if self._record_written:
            paths.append(self.record_path)
        if self._bound:
            paths.append(self.socket_path)
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        if self.srv is not None:
            try:
                self.srv.close()
            except OSError:
                pass
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)  # drops the flock
            except OSError:
                pass
            self._lock_fd = None

    # -- lock, socket, record ----------------------------------------------

    def _take_lock(self):
        """F4: one browser peer per user, whatever CODEX_HOME says."""
        path = os.path.expanduser(LOCK_DIR)
        dfd = fd = None
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
            dfd = sp_journal._open_dir(path, True)
            fd, _st = sp_journal._open_file(dfd, LOCK_NAME, os.O_RDWR | os.O_CREAT, True)
        except (OSError, sp_journal.JournalError) as exc:
            raise WebPeerError("cannot take the web chat lock in %s: %s" % (path, exc))
        finally:
            if dfd is not None:
                os.close(dfd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise WebPeerError(LOCK_HELD)
            raise WebPeerError("cannot lock %s: %s" % (os.path.join(path, LOCK_NAME), exc))
        self._lock_fd = fd  # held for life: closing would drop the lock

    def _bind(self):
        sp_claude.ensure_socket_dir(self.sock_dir)
        if not sp_claude.socket_path_ok(self.socket_path):
            raise WebPeerError(
                "refusing to bind %s: outside the allowlisted socket directories" % self.socket_path
            )
        if len(self.socket_path.encode("utf-8")) > 100:
            raise WebPeerError(
                "socket path %s is too long for AF_UNIX (macOS caps at 104 bytes)" % self.socket_path
            )
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.socket_path)
        self._bound = True
        os.chmod(self.socket_path, 0o600)
        self.srv.listen(16)
        self.srv.settimeout(0.5)

    def record(self):
        stamp = sp_runtime.now_ms()
        started = int(self.started_at * 1000)
        return {
            "pid": os.getpid(),
            "sessionId": self.sid,
            "cwd": sp_runtime.stable_dir(),
            "startedAt": started,
            "procStart": sp_process.proc_start(os.getpid()),
            "version": "web-chat",
            "peerProtocol": sp_constants.PEER_PROTOCOL,
            "peerFeatures": list(sp_constants.PEER_FEATURES),
            "kind": "interactive",
            "entrypoint": "web",
            "pidDomain": sp_process.pid_domain(),
            "messagingSocketPath": self.socket_path,
            "name": self.name,
            "nameSource": "user",
            "nameSince": started,
            "status": "idle",
            "updatedAt": stamp,
            "statusUpdatedAt": stamp,
        }

    def _write_record(self):
        # Set before the write: a crash inside it may leave the temp file or the
        # record, and cleanup must try to remove the record either way.
        self._record_written = True
        sp_runtime.write_json_atomic(self.record_path, self.record(), mode=0o644)

    # -- connections -------------------------------------------------------

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                conn, _addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._clients.acquire(blocking=False):
                sp_runtime.log("refusing a client: %d already in flight" % sp_constants.MAX_CONCURRENT_CLIENTS)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn):
        try:
            uid = sp_process.peer_uid(conn)
            if uid != os.getuid():
                sp_runtime.log(
                    "refusing a client: peer uid %s is not %d"
                    % ("unavailable" if uid is None else uid, os.getuid())
                )
                return
            deadline = time.time() + sp_constants.CONN_TIMEOUT
            frames = 0
            buf = b""
            while not self.stop_event.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    return
                conn.settimeout(remaining)
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, OSError):
                    return
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    frames += 1
                    if frames > sp_constants.MAX_FRAMES_PER_CONNECTION:
                        return
                    self._handle_line(conn, line.decode("utf-8", "replace"))
                if len(buf) > sp_constants.MAX_TEXT_CHARS:
                    return
            if buf.strip() and frames < sp_constants.MAX_FRAMES_PER_CONNECTION:
                self._handle_line(conn, buf.decode("utf-8", "replace"))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._clients.release()

    def _handle_line(self, conn, line):
        line = line.strip()
        if not line:
            return
        try:
            frame = json.loads(line)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        try:
            ftype = frame.get("type")
            if ftype == "user":
                self._handle_user(conn, frame)
            elif ftype == "control" and frame.get("action") == "peer_message_status":
                sender, _name, _rec = self._verified_sender(frame, {})
                self.on_status(frame, sender)
            # `auth`, `peer_idle_notice` and every other frame: ignored.
        except Exception as exc:  # one bad frame must not kill the handler thread
            sp_runtime.log("browser peer: error handling a frame: %r" % (exc,))

    # -- inbox (F1) --------------------------------------------------------

    def _verified_sender(self, frame, attrs):
        """(typed id, record name, record) from a live record only; Nones otherwise.

        The frame's own `from-name` / `from-session` claims are never used.
        """
        none = (None, None, None)
        from_field = frame.get("from") or attrs.get("from") or ""
        if not isinstance(from_field, str):
            return none
        sock = from_field[4:] if from_field.startswith("uds:") else from_field
        if not sock or not sp_claude.socket_path_ok(sock):
            return none
        rec = sp_claude.claude_record_by_socket(sock)
        if not rec or sp_claude.is_web_record(rec):
            return none
        kind = "codex" if rec.get("entrypoint") == "codex" else "cc"
        typed = "%s:%s" % (kind, rec.get("sessionId"))
        try:
            sp_identity.parse_address(typed)
        except ValueError:
            return none
        return typed, rec.get("name"), rec

    def _handle_user(self, conn, frame):
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        body, attrs = sp_protocol.unwrap_message(content)
        if not body.strip():
            return
        orig = frame.get("msg_id")
        orig = orig if isinstance(orig, str) else None
        typed, sender_name, sender_rec = self._verified_sender(frame, attrs)
        message_id = str(uuid.uuid4())  # always fresh: no lookup accepts an existing record
        try:
            sp_journal.record_message(typed, [self.typed], body, message_id=message_id)
        except sp_journal.JournalError as exc:
            self._refuse(conn, orig, sender_rec, exc)
            return
        entry = {
            "message_id": message_id,
            "orig_msg_id": orig,
            "sender": typed,
            "sender_name": sender_name,
            "body": body,
            "at": time.time(),
        }
        try:
            self.on_inbound(entry)
        except Exception as exc:
            sp_runtime.log("browser peer: on_inbound failed: %r" % (exc,))
        self._answer(conn, {"type": "control", "action": "peer_inbox_ack", "orig_msg_id": orig})

    def _refuse(self, conn, orig, rec, exc):
        detail = " ".join(str(exc).split())[:_DETAIL_MAX] or type(exc).__name__
        sp_runtime.log("browser peer: not keeping message %s: %s" % (orig or "unknown", detail))
        self._answer(
            conn,
            {"type": "control", "action": "peer_inbox_refused", "orig_msg_id": orig, "detail": detail},
        )
        if not rec:
            return
        if orig:
            sp_claude.deliver_to_record(
                rec,
                {
                    "type": "control",
                    "action": "peer_message_status",
                    "orig_msg_id": orig,
                    "status": "failed",
                    "detail": detail,
                    "from": "uds:%s" % self.socket_path,
                },
            )
        # The status frame reaches only a sender tracking this message; a plain
        # SendMessage is not, so tell it once with no reply route (no loop).
        notice = "[session-peers] your message to %s was not kept: %s" % (self.name, detail)
        body = "%s\n%s" % (sp_protocol.build_origin("web", self.name, self.sid), notice)
        sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, None))

    @staticmethod
    def _answer(conn, payload):
        try:
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError:
            pass  # EPIPE and friends: the sender stopped listening
