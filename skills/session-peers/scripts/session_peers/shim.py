"""One owned process, socket, and rollout lane per Codex thread."""

from __future__ import annotations
import collections
import errno
import fcntl
import json
import os
import signal
import socket
import sys
import threading
import time
from . import constants as sp_constants, protocol as sp_protocol, rollout as sp_rollout, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, process as sp_process, storage as sp_storage
from . import budgets as sp_budgets

class Shim:
    """One process standing in for one Codex thread inside Claude's fabric.

    The record it writes carries its OWN pid, because Claude's sender checks
    that the process accepting on the socket is that pid: a forked handler
    would fail the check. Threads are used instead.
    """

    def __init__(self, thread):
        self.code_digest = sp_runtime.LOADED_CODE_DIGEST
        self.thread = thread
        self.thread_id = thread["id"]
        self.rollout_path = thread.get("rollout_path")
        self.lock_path = sp_codex.writer_lock_path(self.thread_id)
        self.holder_pid = thread.get("holder_pid")
        self.thread_name = thread.get("name")
        # B1: only a validated alias reaches Claude's wrapper. SessionStart can
        # attach before a title exists, and Codex-generated titles often carry
        # spaces, so an unusable title gets a UUID-derived alias.
        self.name = sp_codex.peer_name_for_thread(
            self.thread_name,
            self.thread_id,
            title_owner=sp_codex.codex_title_owner(self.thread_name),
        )
        self.cwd = thread.get("cwd") or sp_runtime.stable_dir()

        self.sock_dir = sp_claude.default_socket_dir()
        self.sock_path = os.path.join(self.sock_dir, "%d.sock" % os.getpid())
        self.record_path = os.path.join(
            sp_storage.claude_sessions_dir(), "%d.json" % os.getpid()
        )

        state = sp_runtime.read_json(sp_storage.thread_state_path(self.thread_id), {}) or {}
        self.tail = sp_rollout.RolloutTail.from_state(self.rollout_path, state.get("tail"))
        # This is an at-most-once processing ledger, including dropped replies,
        # not evidence of delivery. Read the legacy name when upgrading.
        self.processed_turns = collections.deque(
            state.get("processed_turns", state.get("delivered")) or [],
            maxlen=sp_constants.PROCESSED_TURN_HISTORY,
        )
        self.reply_budget = sp_budgets.ReplyBudget(
            self, state,
            records=lambda: sp_claude.live_claude_records(),
            socket_valid=lambda path: sp_claude.socket_path_ok(path),
            deliver_frame=lambda rec, frame: sp_claude.deliver_to_record(rec, frame),
        )
        self.contacts = dict(state.get("contacts") or {})
        self.fresh = not state
        if self.fresh:
            # A SessionStart hook can launch us AFTER the tagged request was
            # logged. Recover that open turn and sender, suppressing historical
            # events so already-completed replies are never replayed.
            self.tail.poll(emit_events=False)
        elif self.tail.last_boundary is None and self.rollout_path:
            # N6: one full scan at start seeds the interrupt state; every later
            # answer comes from the tail, not from rescanning the whole file.
            self.tail.last_boundary = sp_rollout.last_boundary(self.rollout_path)

        self.started_at = time.time()
        # A registry record describes this shim process. Its initial nameSince
        # cannot predate startedAt merely because an older shim saved the same
        # alias in state.
        self.name_since = self.started_at
        # First startup recovers the current boundary along with the sender;
        # saved state carries the last boundary seen by the previous shim.
        self.status = "busy" if self.tail.last_boundary == "started" else "idle"
        self.poll_interval = sp_runtime._float_env(
            "SESSION_PEERS_POLL_INTERVAL", sp_constants.POLL_INTERVAL_DEFAULT
        )
        self.liveness_interval = sp_runtime._float_env(
            "SESSION_PEERS_LIVENESS_INTERVAL", sp_constants.LIVENESS_INTERVAL_DEFAULT
        )
        self.alias_refresh_interval = sp_runtime._float_env(
            "SESSION_PEERS_ALIAS_REFRESH_INTERVAL",
            sp_constants.ALIAS_REFRESH_INTERVAL_DEFAULT,
        )
        if self.alias_refresh_interval <= 0:
            self.alias_refresh_interval = sp_constants.ALIAS_REFRESH_INTERVAL_DEFAULT
        self.reply_budget.configure_window()
        self._proc_start = None
        self.idle_subs = []
        self.record_rewrites = 0
        self.stop = threading.Event()
        self.srv = None
        self._lock = threading.Lock()
        self._cleaned = False
        self._pidfile_fd = None
        self._clients = threading.Semaphore(sp_constants.MAX_CONCURRENT_CLIENTS)
        self.codex_version = None
        self.liveness_unverified = False

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        if not self.rollout_path:
            sp_runtime.log("thread %s has no rollout path; nothing to tail" % self.thread_id)
            return 2
        held, pid = sp_codex.thread_is_held(self.rollout_path, self.holder_pid, self.lock_path)
        if held is None:
            sp_runtime.log(
                "thread %s liveness is unverified; not starting a shim"
                % self.thread_id
            )
            return 3
        if not held:
            sp_runtime.log(
                "thread %s is not held by a live codex process; not starting"
                % self.thread_id
            )
            return 3
        self.holder_pid = pid or self.holder_pid
        # S12: `codex --version` prints "codex-cli 0.153.4", so the raw string
        # rendered as "codex-codex-cli 0.153.4" in the record. Keep the number.
        raw = sp_runtime.tool_version("codex")
        parsed = sp_runtime.parse_version(raw)
        self.codex_version = ".".join(str(n) for n in parsed) if parsed else sp_constants.CODEX_TESTED

        # P2: ownership FIRST. Everything below mutates state another shim may
        # own (the pidfile, the per-thread state file, the budget marker), and
        # _cleanup would delete the live shim's pidfile on the way out.
        if not self._acquire_ownership():
            sp_runtime.log(
                "another shim already owns thread %s; exiting without touching "
                "its pidfile, state or record" % self.thread_id
            )
            return 4
        # From here on this process owns files that need removing, so the
        # handlers go in BEFORE the bind rather than after the record write.
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self.reply_budget.consume_reset(initial=True)
            self.reply_budget.consume_binding(initial=True)
            # Save recovered requests only after taking ownership, but before
            # advertising readiness. A crash followed by completion while down
            # must resume this cursor, not treat the answer as old history.
            self._save_state()
            self._bind()
            self._write_record()

            accept = threading.Thread(
                target=self._accept_loop, name="accept", daemon=True
            )
            accept.start()
            sp_runtime.log(
                "shim up: thread=%s name=%s socket=%s holder=%s"
                % (self.thread_id, self.name, self.sock_path, self.holder_pid)
            )
            if self.reply_budget.release_after_start:
                self.reply_budget.release_after_start = False
                self.reply_budget.release_held()
                self._save_state()
            self._poll_loop()
        finally:
            self._cleanup()
        return 0

    def _on_signal(self, signum, _frame):
        # Keep cleanup in run()'s finally block. A signal can interrupt the
        # poll loop after its stop check; cleaning here would let the rest of
        # that iteration recreate the registry record after cleanup had marked
        # itself complete.
        sp_runtime.log("signal %d; shutting down" % signum)
        self.stop.set()

    def _cleanup(self):
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        paths = [self.record_path, self.sock_path]
        if self._pidfile_fd is not None:
            # Only the owner removes the pidfile (P2).
            self._save_state()
            paths.append(sp_storage.thread_pid_path(self.thread_id))
        for path in paths:
            try:
                os.unlink(path)
            except (FileNotFoundError, OSError):
                pass
        if self.srv is not None:
            try:
                self.srv.close()
            except OSError:
                pass
        if self._pidfile_fd is not None:
            try:
                os.close(self._pidfile_fd)  # drops the ownership flock
            except OSError:
                pass
            self._pidfile_fd = None

    # -- socket and record -------------------------------------------------

    def _bind(self):
        sp_claude.ensure_socket_dir(self.sock_dir)
        if not sp_claude.socket_path_ok(self.sock_path):
            # S1: the shim held every other endpoint to the allowlist but not
            # its own, so a bad SESSION_PEERS_SOCKET_DIR bound anywhere.
            raise SystemExit(
                "refusing to bind %s: outside the allowlisted socket directories"
                % self.sock_path
            )
        if len(self.sock_path.encode("utf-8")) > 100:
            raise SystemExit(
                "socket path %s is too long for AF_UNIX (macOS caps at 104 bytes)"
                % self.sock_path
            )
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        self.srv.listen(16)
        self.srv.settimeout(0.5)

    def record(self):
        # S12: every timestamp in a live 2.1.263 record is integer milliseconds
        # (startedAt, nameSince, updatedAt, statusUpdatedAt); only procStart is
        # a string.
        stamp = sp_runtime.now_ms()
        if self._proc_start is None:
            self._proc_start = sp_process.proc_start(os.getpid())
        return {
            "pid": os.getpid(),
            "sessionId": self.thread_id,
            "cwd": self.cwd,
            # S12: milliseconds since the epoch, not an ISO string. Claude reads
            # this as a number, and an ISO string rendered as "started 20703d
            # ago" in ListAgents (measured in M6).
            "startedAt": int(self.started_at * 1000),
            "procStart": self._proc_start,
            "version": "codex-%s" % (self.codex_version or sp_constants.CODEX_TESTED),
            "peerProtocol": sp_constants.PEER_PROTOCOL,
            "peerFeatures": list(sp_constants.PEER_FEATURES),
            "kind": "interactive",
            "entrypoint": "codex",
            # Where this shim's own state lives; a caller with a different
            # CODEX_HOME finds the shim's budget markers through it.
            "codexHome": sp_storage.codex_home(),
            "pidDomain": sp_process.pid_domain(),
            "messagingSocketPath": self.sock_path,
            "name": self.name,
            "nameSource": "user",
            "nameSince": int(self.name_since * 1000),
            "status": self.status,
            "updatedAt": stamp,
            "statusUpdatedAt": stamp,
        }

    def _write_record(self):
        d = sp_storage.claude_sessions_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        sp_runtime.write_json_atomic(self.record_path, self.record(), mode=0o644)

    def _set_status(self, status):
        if status == self.status:
            return
        self.status = status
        if os.path.exists(self.record_path):
            self._write_record()

    @staticmethod
    def _bound(mapping, cap=sp_constants.CONTACT_HISTORY):
        """Drop the oldest entries so a long-lived shim cannot grow (N1)."""
        while len(mapping) > cap:
            mapping.pop(next(iter(mapping)))
        return mapping

    def _save_state(self):
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(self.thread_id),
            {
                "thread_id": self.thread_id,
                "name": self.name,
                "thread_name": self.thread_name,
                "name_since": int(self.name_since * 1000),
                "shim_pid": os.getpid(),
                "shim_features": list(sp_constants.SHIM_FEATURES),
                "code_digest": self.code_digest,
                "tail": self.tail.state(),
                "processed_turns": list(self.processed_turns),
                "budgets": self._bound(self.reply_budget.budgets),
                "budget_sender_sid": self.reply_budget.budget_sender_sid,
                "budget_last_at": self.reply_budget.budget_last_at,
                "budget_notified": sorted(self.reply_budget.budget_notified),
                "held": self._bound(self.reply_budget.held),
                "allowance": self.reply_budget.allowance,
                "binding": self.reply_budget.binding,
                "contacts": self._bound(self.contacts),
                "updated_at": sp_runtime.now_iso(),
            },
            mode=0o600,
        )

    def _acquire_ownership(self) -> bool:
        """Take the exclusive flock that proves this shim owns the thread.

        Held for the process's whole life; the kernel releases it on exit,
        crash included, so nothing else can mistake a recycled pid for us.
        Returns False when another shim holds it, having changed nothing.
        """
        path = sp_storage.thread_pid_path(self.thread_id)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        for attempt in range(5):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                # A concurrent `shim_pid` probe takes a shared lock for an
                # instant, so a single failure is not proof of another shim.
                if attempt == 4:
                    os.close(fd)
                    return False
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode("utf-8"))
        # Deliberately not closed: closing would drop the lock.
        self._pidfile_fd = fd
        return True

    # -- inbound -----------------------------------------------------------

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                conn, _addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._admit():
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            ).start()

    def _admit(self) -> bool:
        """S5: bound the handlers.

        Past the cap the connection is closed rather than queued, so a flood
        cannot grow threads without limit. Every admitted handler releases the
        slot in its own `finally`.
        """
        if self._clients.acquire(blocking=False):
            return True
        sp_runtime.log("refusing a client: %d already in flight" % sp_constants.MAX_CONCURRENT_CLIENTS)
        return False

    def _handle_connection(self, conn):
        try:
            uid = sp_process.peer_uid(conn)
            if uid != os.getuid():
                # S3: an unreadable peer uid is a refusal, not a shrug. Both
                # supported platforms answer (LOCAL_PEERCRED / SO_PEERCRED),
                # so "unavailable" means something is wrong, not permissive.
                sp_runtime.log(
                    "refusing a client: peer uid %s is not %d"
                    % ("unavailable" if uid is None else uid, os.getuid())
                )
                return
            # S9: one deadline for the whole connection, not per recv, so a
            # client dribbling a byte at a time cannot hold a slot for ever.
            deadline = time.time() + sp_constants.CONN_TIMEOUT
            frames = 0
            buf = b""
            while not self.stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    sp_runtime.log("client sent no complete line in %ds" % int(sp_constants.CONN_TIMEOUT))
                    return
                conn.settimeout(remaining)
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    sp_runtime.log("client sent no complete line in %ds" % int(sp_constants.CONN_TIMEOUT))
                    return
                except OSError:
                    return
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    frames += 1
                    if frames > sp_constants.MAX_FRAMES_PER_CONNECTION:
                        sp_runtime.log(
                            "dropping a client after %d frames on one connection"
                            % sp_constants.MAX_FRAMES_PER_CONNECTION
                        )
                        return
                    self._handle_line(line.decode("utf-8", "replace"))
                if len(buf) > sp_constants.MAX_TEXT_CHARS:
                    sp_runtime.log("dropping a client whose line exceeds %d chars" % sp_constants.MAX_TEXT_CHARS)
                    return
            if buf.strip() and frames < sp_constants.MAX_FRAMES_PER_CONNECTION:
                self._handle_line(buf.decode("utf-8", "replace"))
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._clients.release()

    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            frame = json.loads(line)
        except ValueError:
            sp_runtime.log("ignoring a non-JSON line from a client")
            return
        if not isinstance(frame, dict):
            return
        ftype = frame.get("type")
        if ftype == "auth":
            # Optional on macOS and Linux; the token is not verified here
            # because the uid check is the real boundary (D8).
            return
        if ftype == "user":
            self._handle_user(frame)
            return
        if ftype == "control":
            self._handle_control(frame)
            return
        sp_runtime.log("ignoring an unknown frame type %r" % ftype)

    def _handle_user(self, frame):
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        body, attrs = sp_protocol.unwrap_message(content)
        if not body.strip():
            sp_runtime.log("ignoring an empty inbound message")
            return

        sender, sender_name, sender_sid, sock_path = self._inbound_sender(frame, attrs)
        text, body, trimmed_from = self._prepare_inbound(
            frame, body, sender, sender_name, sender_sid, sock_path
        )
        paused = self._queue_inbound(frame, sender, text)
        if paused is None:
            return
        sp_runtime.log(
            "queued inbound message %s to thread %s"
            % (frame.get("msg_id") or "unknown", self.thread_id)
        )
        if not paused:
            # We just queued a turn that will run, so advertise busy now. The
            # tail would otherwise flip us busy only when it sees task_started
            # in the rollout, and on a lock-only fresh thread that file may not
            # exist yet -- leaving a notify_when_idle that arrives with (or just
            # after) this message to fire against the shim's start-time idle
            # instead of waiting for the turn to finish.
            self._set_status("busy")
        if trimmed_from is not None:
            # S10: the sender used to learn nothing about a silent trim.
            self._status_back(
                frame,
                sender,
                "truncated",
                "delivered, but trimmed from %d to %d characters: `codex queue` "
                "carries the message as one argument" % (trimmed_from, len(body)),
            )
        if paused:
            sp_runtime.log("thread %s is paused after an interrupt" % self.thread_id)
            self._status_back(
                frame,
                sender,
                "held",
                "queued, but the Codex thread is paused after an interrupt; it "
                "drains when its user types any prompt",
            )


    def _inbound_sender(self, frame, attrs):
        from_field = frame.get("from") or attrs.get("from") or ""
        sock_path = from_field[4:] if from_field.startswith("uds:") else from_field
        sender = None
        if sock_path and sp_claude.socket_path_ok(sock_path):
            sender = sp_claude.claude_record_by_socket(sock_path)
        elif sock_path:
            sp_runtime.log("ignoring a reply address outside the allowlisted directories")
            sock_path = ""

        sender_name = (sender or {}).get("name") or attrs.get("from-name")
        sender_sid = (sender or {}).get("sessionId") or attrs.get("from-session")
        if sender_sid:
            # Re-insert so the newest contact is last: _bound drops the oldest.
            self.contacts.pop(sender_sid, None)
            self.contacts[sender_sid] = {
                "name": sender_name,
                "socket": sock_path,
                "last_seen": sp_runtime.now_iso(),
            }
            self._bound(self.contacts)

        return sender, sender_name, sender_sid, sock_path


    def _prepare_inbound(self, frame, body, sender, sender_name, sender_sid, sock_path):
        tag = sp_protocol.build_tag(
            sender_name,
            sender_sid,
            sock_path if sender else None,
            frame.get("msg_id"),
        )
        # The provenance line follows the tag, so a Codex thread reading the
        # turn sees the sender is a Claude Code session; tag parsers (including
        # shims running older code) only read the first line.
        head = "%s\n%s" % (tag, sp_protocol.build_origin("claude", sender_name, sender_sid))
        # The head rides inside the same text Codex caps, so the body is trimmed
        # to leave room for it rather than pushing the whole message over.
        # P5: the argv budget is bytes; the Codex cap is characters. Both.
        room_bytes = sp_runtime.argv_text_budget() - sp_runtime.utf8_len(head) - 1
        room_chars = sp_constants.MAX_TEXT_CHARS - len(head) - 1
        trimmed_from = None
        if sp_runtime.utf8_len(body) > room_bytes or len(body) > room_chars:
            trimmed_from = len(body)
            body = sp_runtime.truncate_utf8(body, room_bytes)[:room_chars]
            sp_runtime.log("truncating an inbound body of %d chars to %d" % (trimmed_from, len(body)))
        text = "%s\n%s" % (head, body)

        return text, body, trimmed_from


    def _queue_inbound(self, frame, sender, text):
        """Return paused/live status after queueing; None means not queued."""
        held, _pid = sp_codex.thread_is_held(self.rollout_path, self.holder_pid, self.lock_path)
        if held is None:
            sp_runtime.log("thread %s liveness is unverified; not queueing" % self.thread_id)
            self._status_back(
                frame,
                sender,
                "failed",
                "the Codex thread liveness probe is unavailable; retry where "
                "lsof is permitted",
            )
            return
        if not held:
            sp_runtime.log("thread %s is no longer live; refusing to queue" % self.thread_id)
            self._status_back(
                frame, sender, "failed", "the Codex thread is no longer running"
            )
            self.stop.set()
            return

        # N6: the tail already knows the last boundary, so an inbound message
        # no longer rescans the whole rollout (194 MiB files exist).
        paused = self.tail.last_boundary == "aborted"
        try:
            sp_codex.codex_queue(self.thread_id, text, cwd=self.thread.get("cwd"))
        except sp_codex.QueueError as exc:
            sp_runtime.log(
                "queue failed for message %s: %s"
                % (frame.get("msg_id") or "unknown", exc)
            )
            self._status_back(frame, sender, "failed", str(exc))
            # The status frame reaches only a sender tracking this message; a
            # plain SendMessage is not, so the loss was silent. Tell it once,
            # with no reply route so the notice cannot start a loop.
            if sender:
                notice = (
                    "[session-peers] your message to %s was not queued: %s"
                    % (self.name or self.thread_id, exc)
                )
                sp_claude.deliver_to_record(
                    sender,
                    sp_protocol.build_user_frame(
                        sp_protocol.build_cc_body(notice, self.thread_id, self.name, None), None
                    ),
                )
            return
        return paused

    def _status_back(self, frame, sender, status, detail):
        """Tell a sender what happened to the message it just sent.

        The correlation key is `orig_msg_id`, matching the measured control
        shape and `peer_idle_notice`. It was `msg_id` until M6 found that no
        notice rendered in the sending session on 2.1.263, which is exactly
        what an uncorrelatable status frame would look like.
        """
        return self._status_to_record(
            sender, frame.get("msg_id"), status, detail
        )

    def _status_to_record(self, sender, msg_id, status, detail):
        """Send one correlated status when the original peer message is known."""
        if not sender or not msg_id:
            return False
        return sp_claude.deliver_to_record(
            sender,
            {
                "type": "control",
                "action": "peer_message_status",
                "orig_msg_id": msg_id,
                "status": status,
                "detail": detail,
                "from": "uds:%s" % self.sock_path,
            },
        )

    def _handle_control(self, frame):
        action = frame.get("action")
        if action != "notify_when_idle":
            sp_runtime.log("ignoring control action %r" % action)
            return
        from_field = frame.get("from") or ""
        sock_path = from_field[4:] if from_field.startswith("uds:") else from_field
        sub = (frame.get("msg_id"), sock_path)
        if self.status == "idle":
            self._fire_idle(sub, "the Codex thread is idle")
            return
        with self._lock:
            self.idle_subs.append(sub)

    def _fire_idle(self, sub, detail):
        msg_id, sock_path = sub
        if not sp_claude.socket_path_ok(sock_path):
            sp_runtime.log("cannot answer notify_when_idle: %r is not an allowed socket" % sock_path)
            return
        rec = sp_claude.claude_record_by_socket(sock_path)
        if not rec:
            sp_runtime.log("cannot answer notify_when_idle: no live session at %s" % sock_path)
            return
        sp_claude.deliver_to_record(
            rec,
            {
                "type": "control",
                "action": "peer_idle_notice",
                "orig_msg_id": msg_id,
                "state": "idle",
                "finished_at": sp_runtime.now_ms(),
                "detail": detail,
            },
        )

    # -- outbound ----------------------------------------------------------

    def _poll_loop(self):
        last_live = time.time()
        last_alias = last_live
        while not self.stop.wait(self.poll_interval):
            self.reply_budget.consume_reset()
            # Never at startup: a release it triggers needs the bound socket.
            self.reply_budget.consume_allowance()
            self.reply_budget.consume_binding()
            self.reply_budget.expire_held()
            try:
                events = self.tail.poll()
            except Exception as exc:  # never let a bad line kill the shim
                sp_runtime.log("rollout read failed: %s" % exc)
                events = []
            for event in events:
                if event.kind == "start":
                    self._set_status("busy")
                else:
                    self._set_status("idle")
                    self._handle_turn_end(event.turn)
            if events:
                self._save_state()
            self._ensure_record()
            last_live, last_alias = self._poll_maintenance(
                time.time(), last_live, last_alias
            )

    def _poll_maintenance(self, now, last_live, last_alias):
        if now - last_live >= self.liveness_interval:
            last_live = now
            self._check_liveness()
            if self.stop.is_set():
                return last_live, last_alias
            self.reply_budget.retry_held()
        if now - last_alias >= self.alias_refresh_interval:
            last_alias = now
            self._refresh_name()
        return last_live, last_alias

    def _check_liveness(self):
        held, _pid = sp_codex.thread_is_held(
            self.rollout_path, self.holder_pid, self.lock_path
        )
        if held is None:
            if not self.liveness_unverified:
                sp_runtime.log(
                    "codex pid %s liveness is unverified; keeping the shim"
                    % self.holder_pid
                )
            self.liveness_unverified = True
        elif held:
            if self.liveness_unverified:
                sp_runtime.log("codex pid %s liveness probe recovered" % self.holder_pid)
            self.liveness_unverified = False
        elif not held:
            sp_runtime.log(
                "codex pid %s no longer holds %s or its writer lock; exiting"
                % (self.holder_pid, self.rollout_path)
            )
            self.stop.set()

    def _refresh_name(self):
        """Converge the advertised alias after a Codex `/rename`."""
        threads, schema_ok = sp_codex.codex_threads()
        if not schema_ok:
            return
        thread = next(
            (candidate for candidate in threads if candidate["id"] == self.thread_id),
            None,
        )
        if thread is None or thread.get("live") is None:
            return
        title = thread.get("name")
        try:
            desired = sp_codex.peer_name_for_thread(
                title,
                self.thread_id,
                title_owner=sp_codex.codex_title_owner(title, threads),
            )
        except sp_protocol.NameError_ as exc:
            sp_runtime.log("cannot refresh the peer alias: %s" % exc)
            return
        self.thread_name = title
        if desired == self.name:
            sp_storage.refresh_registered_name(self.thread_id, desired)
            return
        previous = self.name
        self.name = desired
        self.name_since = time.time()
        sp_storage.refresh_registered_name(self.thread_id, desired)
        if os.path.exists(self.record_path):
            self._write_record()
        self._save_state()
        sp_runtime.log("peer alias changed from %s to %s" % (previous, desired))

    def _ensure_record(self):
        if os.path.exists(self.record_path):
            return
        if self.record_rewrites >= sp_constants.MAX_RECORD_REWRITES:
            return
        self.record_rewrites += 1
        sp_runtime.log(
            "registry record was removed; rewriting (%d of %d)"
            % (self.record_rewrites, sp_constants.MAX_RECORD_REWRITES)
        )
        self._write_record()


















    def _handle_turn_end(self, turn):
        if turn.turn_id in self.processed_turns:
            return
        self.processed_turns.append(turn.turn_id)

        with self._lock:
            subs, self.idle_subs = self.idle_subs, []
        detail = (
            "the Codex turn finished"
            if turn.outcome == "complete"
            else "the Codex turn was interrupted"
        )
        for sub in subs:
            self._fire_idle(sub, detail)

        if turn.outcome != "complete":
            sp_runtime.log("turn %s aborted; nothing to deliver" % turn.turn_id)
            return
        # S7: a turn that completed while no shim ran IS picked up from the
        # cursor on restart. Recent is useful; hours old is a surprise reply to
        # a conversation that moved on, so it is recorded and not posted.
        age = None if turn.completed_at is None else self.started_at - turn.completed_at
        if age is not None and age > sp_constants.RESTART_DELIVERY_WINDOW:
            sp_runtime.log(
                "turn %s completed %.0fs before this shim started; recording it "
                "as processed without posting" % (turn.turn_id, age)
            )
            return

        tag = turn.tag or {}
        self.reply_budget.advance_sequence(tag)
        text = sp_protocol.strip_tag(turn.last_agent_message or "").strip()

        records = sp_claude.live_claude_records()

        requester, reply_socket = self._verified_requester(turn, tag, records)

        if requester is not None:
            # A verified requester is a contact, however its message arrived: a
            # direct `peers.py send` queues straight into Codex and never passes
            # _handle_user, so without this a later @-addressed turn to that
            # same session was dropped as "no prior contact".
            rsid = requester.get("sessionId")
            self.contacts.pop(rsid, None)
            self.contacts[rsid] = {
                "name": requester.get("name") or tag.get("from"),
                "socket": reply_socket,
                "last_seen": sp_runtime.now_iso(),
            }
            self._bound(self.contacts)

        if not text:
            sp_runtime.log("turn %s finished with no agent message" % turn.turn_id)
            if requester is not None:
                # The requester otherwise waits for a reply that never comes.
                # One correlated status plus one plain notice with no reply
                # route, so the notice cannot start a loop.
                detail = "the Codex turn finished with no final message; nothing to deliver"
                self._status_to_record(requester, tag.get("mid"), "failed", detail)
                notice = "[session-peers] %s answered your message%s with no final message, so no reply was delivered. Ask again or check its TUI." % (
                    self.name or self.thread_id,
                    " %s" % tag.get("mid") if tag.get("mid") else "",
                )
                sp_claude.deliver_to_record(
                    requester,
                    sp_protocol.build_user_frame(
                        sp_protocol.build_cc_body(notice, self.thread_id, self.name, None), None
                    ),
                )
            return

        targets = self._reply_targets(text, requester, records)

        seen = set()
        for rec in targets:
            sid = rec.get("sessionId")
            if sid in seen:
                continue
            seen.add(sid)
            if self._deliver_target(rec, text, tag, turn) == "stop":
                break
        self._save_state()


    def _verified_requester(self, turn, tag, records):
        requester = None
        reply_socket = tag.get("reply")
        if reply_socket:
            if not sp_claude.socket_path_ok(reply_socket):
                sp_runtime.log("reply address %r is not an allowed socket" % reply_socket)
            else:
                rec = sp_claude.claude_record_by_socket(reply_socket, records)
                if rec is None:
                    sp_runtime.log("the session that queued turn %s is gone" % turn.turn_id)
                elif not tag.get("sid"):
                    # P3: sockets are named after a pid and pids are reused, so
                    # a tag with no session id cannot prove the session at that
                    # socket is the one that asked. No id, no auto-delivery.
                    sp_runtime.log(
                        "the tag for turn %s carries no session id; not "
                        "auto-delivering to %s" % (turn.turn_id, reply_socket)
                    )
                elif rec.get("sessionId") != tag.get("sid"):
                    # Claude's own sender guards do not run here.
                    sp_runtime.log("session id at %s changed; not delivering" % reply_socket)
                else:
                    requester = rec

        return requester, reply_socket


    def _reply_targets(self, text, requester, records):
        targets = []
        if requester is not None:
            targets.append(requester)

        addressed = sp_constants.AT_NAME_RE.match(text.lstrip())
        if addressed:
            name = addressed.group(1)
            matches = sp_claude.claude_record_by_name(name, records)
            if not matches:
                sp_runtime.log("no live Claude session named %r" % name)
            elif len(matches) > 1:
                sp_runtime.log("%r names %d live sessions; not delivering" % (name, len(matches)))
            elif any(t.get("sessionId") == matches[0].get("sessionId") for t in targets):
                # Addressed to the session the reply already goes to: the
                # reply path delivers it once, so there is nothing to check or
                # drop here (this used to log a false "unsolicited" drop).
                pass
            else:
                rec = matches[0]
                sid = rec.get("sessionId")
                allowed = sid in self.contacts or os.environ.get(
                    "SESSION_PEERS_ALLOW_UNSOLICITED"
                ) == "1"
                if allowed:
                    targets.append(rec)
                else:
                    sp_runtime.log(
                        "dropping an unsolicited reply to %r: no prior contact "
                        "(set SESSION_PEERS_ALLOW_UNSOLICITED=1 to allow)" % name
                    )

        return targets


    def _deliver_target(self, rec, text, tag, turn):
        """Return stop only when reply construction must end target iteration."""
        sid = rec.get("sessionId")
        spent = self.reply_budget.budgets.get(sid, 0)
        cap = self.reply_budget.cap_for(sid)
        if spent >= cap:
            detail = (
                "reply held, not delivered: the loop guard reached %d "
                "consecutive replies for this peer. `peers.py budget allow %s "
                "--replies N` raises this requester's total (up to %d) and "
                "releases the held reply; `peers.py budget reset %s` "
                "releases the latest held reply and clears the guard; another "
                "peer, a direct Codex turn, or %.0f seconds idle also clears "
                "it but discards the held reply"
                % (
                    cap,
                    self.thread_id,
                    sp_constants.BUDGET_ALLOW_MAX,
                    self.thread_id,
                    self.reply_budget.reply_budget_window,
                )
            )
            # Hold the latest reply instead of losing it: the guard still
            # stops the loop, and an explicit reset (the supervision signal)
            # releases it. Only the requester's own reply is held.
            if sid == tag.get("sid"):
                self.reply_budget.held[sid] = {
                    "text": text,
                    "mid": tag.get("mid"),
                    "turn_id": turn.turn_id,
                    "at": time.time(),
                }
            sp_runtime.log(
                "reply budget of %d spent for session %s; holding the reply "
                "(peers.py budget allow %s --replies N or peers.py budget "
                "reset %s releases it)"
                % (cap, rec.get("name") or sid, self.thread_id, self.thread_id)
            )
            self._status_to_record(
                rec,
                tag.get("mid") if sid == tag.get("sid") else None,
                "failed",
                detail,
            )
            # The status frame above is surfaced only when the requesting
            # session is tracking the originating peer message; an ordinary
            # SendMessage-style delivery has nothing correlating it, so
            # without this it drops silently. Deliver ONE non-replyable
            # notice per exhausted sequence (no `from` route, no answer
            # body) naming the reset command.
            if sid not in self.reply_budget.budget_notified:
                notice = (
                    "[session-peers] a reply from %s is held, not delivered: "
                    "the reply budget of %d consecutive replies is spent for "
                    "this peer. Run `peers.py budget allow %s --replies N` "
                    "(N up to %d, a total for this sequence) to continue and "
                    "release the held reply, or `peers.py budget reset %s` to "
                    "release the latest held reply and clear the guard. Another "
                    "peer, a direct Codex turn, or %.0fs idle also clears it but "
                    "discards the held reply." % (
                        self.name or self.thread_id,
                        cap,
                        self.thread_id,
                        sp_constants.BUDGET_ALLOW_MAX,
                        self.thread_id,
                        self.reply_budget.reply_budget_window,
                    )
                )
                body = sp_protocol.build_cc_body(notice, self.thread_id, self.name, None)
                if sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, None)):
                    self.reply_budget.budget_notified.add(sid)
            return "continue"
        out = (
            sp_protocol.reply_text(text, tag.get("mid")) if sid == tag.get("sid") else text
        )
        try:
            body = sp_protocol.build_cc_body(out, self.thread_id, self.name, self.sock_path)
        except ValueError as exc:
            sp_runtime.log("cannot build a reply for turn %s: %s" % (turn.turn_id, exc))
            return "stop"
        if sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, self.sock_path)):
            self.reply_budget.budgets[sid] = spent + 1
            self.reply_budget.spend_binding(sid)
            # Deliberately no body text: the log is a delivery record, not
            # a transcript, and it lands in a file the user may share.
            sp_runtime.log(
                "delivered turn %s to %s"
                % (turn.turn_id, rec.get("name") or sid)
            )
        return "continue"


def cmd_shim(args):
    try:
        thread = sp_codex.resolve_thread(args.thread, require_live=False)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    try:
        shim = Shim(thread)
    except sp_protocol.NameError_ as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    return shim.run()

