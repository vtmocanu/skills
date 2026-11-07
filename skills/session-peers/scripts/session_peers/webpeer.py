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
import queue
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, identity as sp_identity, journal as sp_journal
from . import process as sp_process, storage as sp_storage

LOCK_DIR = "~/.codex/session-peers"
LOCK_NAME = "web-chat.lock"
LOCK_HELD = "another peers.py web --chat is running for this user"
_DETAIL_MAX = 300
NOTIFY_QUEUE_MAX = 32
_ORIG_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NOTIFIED_MAX = 1024
_STATUS_MAX = 32
_STATUS_DETAIL_MAX = 512
_JOIN_TIMEOUT = 2.0


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
        self._stop_requested = False
        self._max_clients = sp_constants.MAX_CONCURRENT_CLIENTS
        self._clients = threading.Semaphore(self._max_clients)
        self._conns = set()  # accepted connections, shut down by cleanup
        self._accept = None
        self._notifier = None
        self._notices = queue.Queue(maxsize=NOTIFY_QUEUE_MAX)
        self._notified = OrderedDict()  # senders already told about the current failure episode
        self._notified_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def stop_requested(self):
        return self._stop_requested or self.stop_event.is_set()

    def request_stop(self):
        """Ask the peer to stop. The only call to make from a signal handler.

        It assigns one plain attribute and takes no lock, so a handler that
        interrupts the owner inside ``stop()`` or ``start()`` cannot deadlock.
        The owner polls ``stop_requested`` / ``wait_for_stop()`` and then calls
        ``stop()`` from its own ``finally``, as the shim does in ``run()``.
        """
        self._stop_requested = True

    def wait_for_stop(self, timeout=None):
        """Block the owner until a stop is requested; True when it was."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.stop_requested:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def start(self):
        if self.stop_requested or self._cleaned:
            raise WebPeerError("the browser peer was already stopped; make a new one")
        try:
            self._take_lock()
            self._bind()
            self._write_record()
            self._accept = threading.Thread(target=self._accept_loop, name="web-peer-accept", daemon=True)
            self._accept.start()
            self._notifier = threading.Thread(target=self._notify_loop, name="web-peer-notify", daemon=True)
            self._notifier.start()
        except BaseException:
            self._cleanup()
            raise
        return self

    def stop(self):
        """Owner-thread teardown: idempotent, removes the record, socket and lock.

        Not for a signal handler (it takes locks): a handler calls
        ``request_stop()`` and the owner calls this from its ``finally``.
        """
        self._stop_requested = True
        self.stop_event.set()
        self._cleanup()
        me = threading.current_thread()
        for t in (self._accept, self._notifier):
            if t is not None and t is not me and t.is_alive():
                t.join(_JOIN_TIMEOUT)

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
            paths.append("%s.tmp.%d" % (self.record_path, os.getpid()))
        if self._bound:
            paths.append(self.socket_path)
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        with self._mutex:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)  # wakes a handler blocked in recv
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
        try:
            sp_claude.ensure_socket_dir(self.sock_dir)
        except SystemExit as exc:  # its refusals (symlink, wrong owner) are SystemExit
            raise WebPeerError(str(exc.code) if exc.code else "unsafe socket directory %s" % self.sock_dir)
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
        if self.stop_requested or self._cleaned:
            raise WebPeerError("stop requested before the registry record was written")
        d = sp_storage.claude_sessions_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        # Set before the write: a crash inside it may leave the temp file or the
        # record, and cleanup must try to remove the record either way.
        self._record_written = True
        sp_runtime.write_json_atomic(self.record_path, self.record(), mode=0o644)
        if self.stop_requested or self._cleaned:
            # A stop() on another thread may have run its cleanup before the
            # record existed, so remove what the write just made.
            for path in (self.record_path, "%s.tmp.%d" % (self.record_path, os.getpid())):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise WebPeerError("stop requested while the registry record was written")

    # -- connections -------------------------------------------------------

    def _accept_loop(self):
        while not self.stop_requested:
            try:
                conn, _addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._clients.acquire(blocking=False):
                sp_runtime.log("refusing a client: %d already in flight" % self._max_clients)
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._mutex:
                closed = self._cleaned
                if not closed:
                    self._conns.add(conn)
            if closed:
                try:
                    conn.close()
                except OSError:
                    pass
                self._clients.release()
                break
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
            while not self.stop_requested:
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
                    if self._handle_line(conn, line.decode("utf-8", "replace")) is False:
                        return
                if len(buf) > sp_constants.MAX_TEXT_CHARS:
                    return
            if buf.strip() and frames < sp_constants.MAX_FRAMES_PER_CONNECTION:
                self._handle_line(conn, buf.decode("utf-8", "replace"))
        finally:
            with self._mutex:
                self._conns.discard(conn)
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
                return self._handle_user(conn, frame)
            elif ftype == "control" and frame.get("action") == "peer_message_status":
                sender, _name, _rec = self._verified_sender(frame, {})
                self.on_status(self._clean_status(frame), sender)
            # `auth`, `peer_idle_notice` and every other frame: ignored.
        except Exception as exc:  # one bad frame must not kill the handler thread
            sp_runtime.log("browser peer: error handling a frame: %r" % (exc,))

    @staticmethod
    def _clean_status(frame):
        """A new dict of the status fields, each reduced to a plain printable value."""
        orig = frame.get("orig_msg_id")
        orig = orig if isinstance(orig, str) and _ORIG_ID_RE.fullmatch(orig) else None
        status = frame.get("status")
        if isinstance(status, str):
            status = re.sub(r"[^A-Za-z0-9._-]", "", status)[:_STATUS_MAX] or None
        else:
            status = None
        detail = frame.get("detail")
        if isinstance(detail, str):
            detail = "".join(c if c.isprintable() else " " for c in detail)[:_STATUS_DETAIL_MAX]
        else:
            detail = None
        return {
            "type": "control",
            "action": "peer_message_status",
            "orig_msg_id": orig,
            "status": status,
            "detail": detail,
        }

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
        # Echoed into logs, frames and on_inbound: only a plain token passes.
        orig = orig if isinstance(orig, str) and _ORIG_ID_RE.fullmatch(orig) else None
        typed, sender_name, sender_rec = self._verified_sender(frame, attrs)
        try:
            # No message_id: a fresh one, so the journal does not scan for a duplicate.
            message_id = sp_journal.record_message(typed, [self.typed], body)
        except sp_journal.JournalError as exc:
            self._refuse(conn, orig, typed, sender_rec, exc)
            return
        with self._notified_lock:
            self._notified.pop(typed, None)  # this sender's next failure may notify again
        entry = {
            "message_id": message_id,
            "orig_msg_id": orig,
            "sender": typed,
            "sender_name": sender_name,
            "body": body,
            "at": time.time(),
        }
        if self.stop_requested:
            # Journaled, but the feed is closing: close unacknowledged, as for a failed on_inbound.
            return False
        try:
            self.on_inbound(entry)
        except Exception as exc:
            # Journaled but never shown: no ack and no refusal, so the sender
            # sees a closed connection ("outcome unknown") rather than a false ack.
            sp_runtime.log("browser peer: on_inbound failed, dropping the connection unacknowledged: %r" % (exc,))
            return False
        self._answer(conn, {"type": "control", "action": "peer_inbox_ack", "orig_msg_id": orig})

    def _refuse(self, conn, orig, typed, rec, exc):
        text = "".join(c if c.isprintable() else " " for c in str(exc))
        detail = " ".join(text.split())[:_DETAIL_MAX] or type(exc).__name__
        sp_runtime.log("browser peer: not keeping message %r: %r" % (orig, detail))
        self._answer(
            conn,
            {"type": "control", "action": "peer_inbox_refused", "orig_msg_id": orig, "detail": detail},
        )
        if not rec:
            return
        if orig:
            self._notify(
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
        # SendMessage is not, so tell it once per failure episode with no reply
        # route (no loop). A successful journal write starts a new episode.
        with self._notified_lock:
            if typed in self._notified:
                return
            notice = "[session-peers] your message to %s was not kept: %s" % (self.name, detail)
            body = "%s\n%s" % (sp_protocol.build_origin("web", self.name, self.sid), notice)
            # Counts as told only once the notice is queued; a full queue may retry.
            if self._notify(rec, sp_protocol.build_user_frame(body, None)):
                if len(self._notified) >= _NOTIFIED_MAX:
                    self._notified.popitem(last=False)
                self._notified[typed] = True

    def _notify(self, rec, frame):
        """Queue a delivery for the notifier thread; never blocks a connection slot."""
        try:
            self._notices.put_nowait((rec, frame))
        except queue.Full:
            sp_runtime.log("browser peer: notifier queue full; dropping a notice")
            return False
        return True

    def _notify_loop(self):
        while not self.stop_requested:
            try:
                rec, frame = self._notices.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                sp_claude.deliver_to_record(rec, frame)  # bounded by send_frame's timeout
            except Exception as exc:
                sp_runtime.log("browser peer: notice delivery failed: %r" % (exc,))

    @staticmethod
    def _answer(conn, payload):
        try:
            conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError:
            pass  # EPIPE and friends: the sender stopped listening
