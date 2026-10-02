#!/usr/bin/env python3
"""Cross-session messaging between Claude Code sessions and Codex CLI threads.

One stable launcher, one CLI, with bundled runtime modules. See PRD #44 and ``../references/spike-checklist.md`` for the
measurements every mechanism here relies on; the decision letters (D1..D11) in
the comments point at that PRD's decision log.

Subcommands::

    peers.py list [--json]
    peers.py send --to codex:<name|uuid>|cc:<name|uuid>|buddy
                  (--message <text>|--message-file <path>) [--json]
                  [--from-thread <uuid>] [--from-name N] [--from-sid S]
                  [--from-socket P]
    peers.py ask --to cc:<name|uuid>|buddy (--message <text>|--message-file <path>)
                 [--from-thread <uuid>] [--timeout <seconds>] [--json]
    peers.py dispatch --to cc:<name|uuid>|buddy
                      (--message <text>|--message-file <path>)
                      [--from-thread <uuid>] [--timeout <seconds>] [--json]
    peers.py await --request <uuid> [--from-thread <uuid>]
                   [--timeout <seconds>] [--json]
    peers.py reply --request <uuid> (--message <text>|--message-file <path>)
                   [--json]
    peers.py wait --for cc:<name|uuid>|buddy [--state idle|busy] [--timeout <seconds>]
                  [--json]
    peers.py shim --thread <uuid>
    peers.py up [<name|uuid>]
    peers.py down [<name|uuid>]
    peers.py restart <name|uuid>
    peers.py budget reset <name|uuid|buddy>
    peers.py budget allow <name|uuid|buddy> --replies N [--for-session <uuid>]
                          [--as cc:<uuid>|codex:<uuid>]
    peers.py buddy [show|ping|clear] [--as cc:<uuid>|codex:<uuid>] [--json]
    peers.py buddy set [cc:|codex:|@]<name|uuid> [--uses a,b] [--replies N]
                       [--as cc:<uuid>|codex:<uuid>] [--json]
    peers.py session-hook
    peers.py install-hook [--auto-attach]
    peers.py topic post <topic> (--message <text>|--message-file <path>|
                       --json-file <path>) [--kind KIND] [--as cc:<uuid>|codex:<uuid>]
                       [--json]
    peers.py topic tail <topic> [--since SEQ] [--limit N] [--json]
    peers.py topic list [--json]
    peers.py gc [--days 7] [--dry-run]
    peers.py doctor

Python 3.9+, stdlib only (D3).  macOS and Linux only.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.realpath(__file__))
if sys.path[:1] != [_HERE]:
    sys.path.insert(0, _HERE)

from session_peers import config as sp_config, constants as sp_constants, protocol as sp_protocol, rollout as sp_rollout, runtime as sp_runtime

from session_peers import claude as sp_claude, codex as sp_codex, diagnostics as sp_diagnostics, lifecycle as sp_lifecycle, process as sp_process, requests as sp_requests, storage as sp_storage


def _bridge_thread_ids():
    """UUIDs represented by registrations or per-thread bridge artifacts."""
    out = {thread_id for thread_id in sp_storage.read_registered() if sp_runtime.is_uuid(thread_id)}
    try:
        names = os.listdir(sp_storage.state_dir())
    except OSError:
        return out
    for name in names:
        for suffix in sp_constants.THREAD_ARTIFACT_SUFFIXES:
            if not name.endswith(suffix):
                continue
            candidate = name[: -len(suffix)]
            if sp_runtime.is_uuid(candidate):
                out.add(candidate)
            break
    return out


def _thread_last_seen(thread_id, registered, threads):
    """Latest trustworthy activity timestamp for one bridge thread."""
    seen = []
    meta = registered.get(thread_id)
    if isinstance(meta, dict):
        for key in ("last_seen_at", "registered_at"):
            value = sp_runtime.parse_time(meta.get(key))
            if value is not None:
                seen.append(value)
    state = sp_runtime.read_json(sp_storage.thread_state_path(thread_id), {}) or {}
    value = sp_runtime.parse_time(state.get("updated_at")) if isinstance(state, dict) else None
    if value is not None:
        seen.append(value)
    thread = threads.get(thread_id)
    if thread is not None:
        value = sp_runtime.parse_time(thread.get("updated_at"))
        if value is not None:
            seen.append(value)
    for suffix in sp_constants.THREAD_ARTIFACT_SUFFIXES:
        path = os.path.join(sp_storage.state_dir(), thread_id + suffix)
        try:
            seen.append(os.stat(path).st_mtime)
        except OSError:
            pass
    return max(seen) if seen else None


def gc_bridge_state(days=sp_constants.GC_DAYS_DEFAULT, dry_run=False, verbose=True):
    """Prune exact bridge-owned artifacts for inactive threads older than days.

    Codex rollouts, writer locks and queued messages are outside ``state_dir``
    and are never touched. Persistent manual registrations are bridge metadata
    and intentionally expire too. Unknown discovery fails closed because a
    thread must be proven inactive before any metadata is removed.
    """
    if days < 0:
        raise ValueError("retention days must be zero or greater")
    threads, schema_ok = sp_codex.codex_threads()
    if not schema_ok:
        if verbose:
            print("GC skipped: Codex thread discovery is unavailable")
        return []
    if any(thread.get("live") is None for thread in threads):
        if verbose:
            print("GC skipped: Codex liveness is unverified")
        return []
    by_id = {thread["id"]: thread for thread in threads}
    registered = sp_storage.read_registered()
    cutoff = time.time() - days * 86400.0
    candidates = []
    for thread_id in sorted(_bridge_thread_ids()):
        thread = by_id.get(thread_id)
        if thread is not None and thread.get("live"):
            continue
        if sp_lifecycle.shim_pid(thread_id):
            continue
        last_seen = _thread_last_seen(thread_id, registered, by_id)
        if last_seen is None or last_seen > cutoff:
            continue
        candidates.append(thread_id)
    if dry_run:
        if verbose:
            for thread_id in candidates:
                print("would prune %s" % thread_id)
        return candidates
    if not candidates:
        return []

    removed = []
    with sp_storage.reconcile_lock():
        # Recheck after taking the same lock used by attach/up/down. A session
        # that resumed while the first scan ran must win over GC.
        current, current_ok = sp_codex.codex_threads()
        if not current_ok:
            return []
        if any(thread.get("live") is None for thread in current):
            return []
        current_by_id = {thread["id"]: thread for thread in current}
        live_ids = {thread["id"] for thread in current if thread.get("live")}
        registrations = sp_storage.read_registered()
        for thread_id in candidates:
            if thread_id in live_ids or sp_lifecycle.shim_pid(thread_id):
                continue
            last_seen = _thread_last_seen(
                thread_id, registrations, current_by_id
            )
            if last_seen is None or last_seen > cutoff:
                continue
            failed = False
            for suffix in sp_constants.THREAD_ARTIFACT_SUFFIXES:
                path = os.path.join(sp_storage.state_dir(), thread_id + suffix)
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failed = True
                    sp_runtime.log("could not prune %s: %s" % (path, exc))
            if failed:
                continue
            registrations.pop(thread_id, None)
            removed.append(thread_id)
        sp_storage.write_registered(registrations)
    if verbose:
        for thread_id in removed:
            print("pruned %s" % thread_id)
    return removed


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
        self.budgets = dict(state.get("budgets") or {})
        self.budget_sender_sid = state.get("budget_sender_sid")
        self.budget_last_at = sp_runtime.parse_time(state.get("budget_last_at"))
        # Sessions already told (once) that a reply of theirs was dropped for
        # budget; cleared whenever the budget sequence resets so a genuinely new
        # sequence can notify again.
        self.budget_notified = set(state.get("budget_notified") or [])
        # The latest budget-dropped reply per requesting session, held (not lost) so
        # an explicit `peers.py budget reset` can release it. Bounded to one per
        # session, expired with the idle window, discarded on any other sequence
        # reset, and kept only in this mode-0600 state file, never in the log.
        self.held = dict(state.get("held") or {})
        # `budget allow`: {"sid", "total", "at"} raises that one requester's cap
        # for the current sequence. Dropped whenever the sequence resets.
        allowance = state.get("allowance")
        self.allowance = allowance if self._valid_allowance(allowance) else None
        # `buddy set --replies N`: a finite TOTAL for one owner session on this
        # thread, spent across sequences, never replenished. Only `buddy clear`
        # or binding another buddy revokes it (a revoke marker).
        binding = state.get("binding")
        self.binding = binding if self._valid_binding(binding) else None
        # Set when a startup reset leaves held replies to release: the release
        # waits until this shim is bound and registered, so the reply's `from`
        # route names a socket that exists.
        self.release_after_start = False
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
        self.reply_budget_window = sp_runtime._float_env(
            "SESSION_PEERS_REPLY_BUDGET_WINDOW",
            sp_constants.REPLY_BUDGET_WINDOW_DEFAULT,
        )
        if self.reply_budget_window <= 0:
            self.reply_budget_window = sp_constants.REPLY_BUDGET_WINDOW_DEFAULT
        if self.allowance and not (
            self._allowance_waiting(time.time())
            or (
                self.allowance.get("bound", True)
                and self.budget_last_at is not None
                and not self._sequence_expired(time.time())
            )
        ):
            # A restart keeps a grant only while it would still apply.
            sp_runtime.log("reply allowance for %s expired while the shim was down"
                % self.allowance.get("sid"))
            self.allowance = None
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
            self._consume_budget_marker(initial=True)
            self._consume_budget_binding_marker(initial=True)
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
            if self.release_after_start:
                self.release_after_start = False
                self._release_held()
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
                "budgets": self._bound(self.budgets),
                "budget_sender_sid": self.budget_sender_sid,
                "budget_last_at": self.budget_last_at,
                "budget_notified": sorted(self.budget_notified),
                "held": self._bound(self.held),
                "allowance": self.allowance,
                "binding": self.binding,
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

        tag = sp_protocol.build_tag(
            sender_name,
            sender_sid,
            sock_path if sender else None,
            frame.get("msg_id"),
        )
        # The tag rides inside the same text Codex caps, so the body is trimmed
        # to leave room for it rather than pushing the whole message over.
        # P5: the argv budget is bytes; the Codex cap is characters. Both.
        room_bytes = sp_runtime.argv_text_budget() - sp_runtime.utf8_len(tag) - 1
        room_chars = sp_constants.MAX_TEXT_CHARS - len(tag) - 1
        trimmed_from = None
        if sp_runtime.utf8_len(body) > room_bytes or len(body) > room_chars:
            trimmed_from = len(body)
            body = sp_runtime.truncate_utf8(body, room_bytes)[:room_chars]
            sp_runtime.log("truncating an inbound body of %d chars to %d" % (trimmed_from, len(body)))
        text = "%s\n%s" % (tag, body)

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
            self._consume_budget_marker()
            # Never at startup: a release it triggers needs the bound socket.
            self._consume_budget_allow_marker()
            self._consume_budget_binding_marker()
            self._expire_held()
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
            self._retry_held()
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

    def _consume_budget_marker(self, initial=False):
        path = sp_storage.budget_reset_path(self.thread_id)
        if not os.path.exists(path):
            return
        try:
            os.unlink(path)
        except OSError:
            return
        self.budgets = {}
        self.budget_sender_sid = None
        self.budget_last_at = None
        self.budget_notified = set()
        self.allowance = None
        if initial:
            # Not yet bound or registered: defer the release (see run()).
            self.release_after_start = bool(self.held)
            return
        sp_runtime.log("reply budget reset for thread %s" % self.thread_id)
        # An explicit reset is the supervision signal the loop guard waits for,
        # so it also releases the reply the guard held back.
        self._release_held()
        self._save_state()

    def _expire_held(self, now=None):
        """Purge held replies past the idle window from memory AND the state file."""
        if not self.held:
            return
        now = time.time() if now is None else now
        stale = [
            sid
            for sid, entry in self.held.items()
            if not isinstance(entry, dict)
            or now - float(entry.get("at") or 0) > self.reply_budget_window
        ]
        if not stale:
            return
        for sid in stale:
            self.held.pop(sid, None)
        sp_runtime.log("purged %d expired held reply(s)" % len(stale))
        self._save_state()

    def _release_held(self):
        """Deliver each still-fresh held reply once; it opens the new sequence."""
        if not self.held:
            return
        records = sp_claude.live_claude_records()
        for sid in list(self.held):
            self._release_one(sid, "reset", records)

    def _release_one(self, sid, reason, records):
        """Release one held reply; drop it once delivered or undeliverable.

        A transient write failure keeps a fresh entry, tagged with why it was
        being released, so `_retry_held` tries it again. Usage is counted only
        by a delivery that succeeded, so a retry never double-counts.
        """
        entry = self.held.get(sid)
        outcome = self._deliver_held(sid, entry, records, new_sequence=(reason == "reset"))
        if outcome == "failed":
            entry["release"] = reason
        else:
            self.held.pop(sid, None)
        return outcome

    def _retry_held(self):
        """Retry held replies whose release failed on a transient write error."""
        pending = [
            (sid, entry.get("release"))
            for sid, entry in self.held.items()
            if isinstance(entry, dict) and entry.get("release")
        ]
        if not pending:
            return
        records = sp_claude.live_claude_records()
        changed = False
        for sid, reason in pending:
            if reason == "allow" and self.budgets.get(sid, 0) >= self._cap_for(sid):
                # The allowance that released it is gone or spent.
                self.held[sid].pop("release", None)
                changed = True
                continue
            changed = self._release_one(sid, reason, records) != "failed" or changed
        if changed:
            self._save_state()

    def _deliver_held(self, sid, entry, records, new_sequence):
        """One held reply to its live requester, if still fresh and routable.

        Returns ``delivered``, ``failed`` (worth a retry) or ``discarded``.
        """
        if not isinstance(entry, dict) or not entry.get("text"):
            return "discarded"
        now = time.time()
        age = now - float(entry.get("at") or 0)
        if age > self.reply_budget_window:
            sp_runtime.log("discarding a held reply for %s: %.0fs old" % (sid, age))
            return "discarded"
        rec = next((r for r in records if r.get("sessionId") == sid), None)
        if rec is None:
            sp_runtime.log("discarding a held reply: session %s is gone" % sid)
            return "discarded"
        if not sp_claude.socket_path_ok(rec.get("messagingSocketPath")):
            sp_runtime.log("discarding a held reply: %s listens outside the allowlist" % sid)
            return "discarded"
        text = sp_protocol.reply_text(entry["text"], entry.get("mid"), held_reply=True)
        try:
            body = sp_protocol.build_cc_body(text, self.thread_id, self.name, self.sock_path)
        except ValueError as exc:
            sp_runtime.log("cannot build a held reply for %s: %s" % (sid, exc))
            return "discarded"
        if not sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, self.sock_path)):
            sp_runtime.log("keeping the held reply for %s to retry" % sid)
            return "failed"
        # An explicit reset opens a new sequence; an allowance continues the
        # current one, so the release counts toward its usage.
        self.budgets[sid] = 1 if new_sequence else self.budgets.get(sid, 0) + 1
        self._spend_binding(sid)
        self.budget_sender_sid = sid
        self.budget_last_at = now
        sp_runtime.log(
            "released a held reply (turn %s) to %s"
            % (entry.get("turn_id"), rec.get("name") or sid)
        )
        return "delivered"

    @staticmethod
    def _valid_allowance(value):
        """Well-formed sid and total, and a grant time that parses."""
        if not isinstance(value, dict) or not isinstance(value.get("sid"), str):
            return False
        total = value.get("total")
        return (
            isinstance(total, int)
            and not isinstance(total, bool)
            and 1 <= total <= sp_constants.BUDGET_ALLOW_MAX
            and sp_runtime.parse_time(value.get("at")) is not None
        )

    def _allowance_fresh(self, at, now):
        """A grant counts only within the idle window of when it was GRANTED.

        Consumption can lag the grant (a shim that starts late), so the
        original time decides, never the time the shim read it. A grant stamped
        more than a minute ahead is refused rather than trusted to last.
        """
        return -60.0 <= now - at <= self.reply_budget_window

    def _cap_for(self, sid):
        """The consecutive-reply cap for one requesting session."""
        cap = sp_constants.REPLY_BUDGET
        if self.allowance and self.allowance.get("sid") == sid:
            cap = max(cap, self.allowance["total"])
        binding = self.binding
        if binding and binding["sid"] == sid:
            remaining = binding["total"] - binding["spent"]
            if remaining > 0:
                # Replies already sent this sequence were counted in `spent`,
                # so the cap sits `remaining` above them, whichever sequence.
                cap = max(cap, self.budgets.get(sid, 0) + remaining)
        return cap

    @staticmethod
    def _valid_binding(value):
        if not isinstance(value, dict):
            return False
        if not isinstance(value.get("sid"), str) or not value.get("sid"):
            return False
        if not isinstance(value.get("bind_id"), str) or not value.get("bind_id"):
            return False
        for key, low in (("total", 1), ("spent", 0)):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, int):
                return False
            if not low <= number <= sp_constants.BUDDY_REPLIES_MAX:
                return False
        return True

    def _spend_binding(self, sid):
        binding = self.binding
        if binding and binding["sid"] == sid and binding["spent"] < binding["total"]:
            binding["spent"] += 1

    def _consume_budget_binding_marker(self, initial=False):
        """Apply a `buddy set --replies` grant, or a `buddy clear`/rebind revoke."""
        path = sp_storage.budget_binding_path(self.thread_id)
        if not os.path.exists(path):
            return
        # Read and unlink under the lock: a marker written between the two
        # would be deleted unread. A busy lock waits for the next poll.
        try:
            with sp_storage.binding_lock(self.thread_id, timeout=2.0):
                marker = sp_runtime.read_json(path, None)
                try:
                    os.unlink(path)
                except OSError:
                    return
        except sp_storage.BindingLockTimeout:
            sp_runtime.log("binding marker for thread %s is locked; retrying" % self.thread_id)
            return
        if isinstance(marker, dict) and marker.get("revoke") is True:
            binding = self.binding
            if (
                binding
                and binding["sid"] == marker.get("sid")
                and binding["bind_id"] == marker.get("bind_id")
            ):
                sp_runtime.log("buddy reply allowance for %s revoked" % binding["sid"])
                self.binding = None
                self._save_state()
            return
        grant = None
        if isinstance(marker, dict):
            grant = {
                "sid": marker.get("sid"),
                "bind_id": marker.get("bind_id"),
                "total": marker.get("total"),
                "spent": 0,
            }
        if not self._valid_binding(grant):
            sp_runtime.log("ignoring a malformed buddy reply allowance for thread %s"
                % self.thread_id)
            return
        current = self.binding
        if (
            current
            and current["sid"] == grant["sid"]
            and current["bind_id"] == grant["bind_id"]
        ):
            # The same binding again: never a replenishment.
            grant["total"] = max(current["total"], grant["total"])
            grant["spent"] = min(current["spent"], grant["total"])
        sid = grant["sid"]
        old_cap = self._cap_for(sid)
        self.binding = grant
        new_cap = self._cap_for(sid)
        sp_runtime.log(
            "buddy reply allowance for %s set to %d total (%d spent)"
            % (sid, grant["total"], grant["spent"])
        )
        if new_cap > old_cap:
            self.budget_notified.discard(sid)
            if (
                not initial
                and self.budgets.get(sid, 0) < new_cap
                and sid in self.held
            ):
                self._release_one(sid, "allow", sp_claude.live_claude_records())
        self._save_state()

    def _consume_budget_allow_marker(self):
        """Apply a `budget allow` grant: a total, never additive or replenishing."""
        path = sp_storage.budget_allow_path(self.thread_id)
        if not os.path.exists(path):
            return
        grant = sp_runtime.read_json(path, None)
        try:
            os.unlink(path)
        except OSError:
            return
        if not self._valid_allowance(grant):
            sp_runtime.log("ignoring a malformed reply allowance for thread %s" % self.thread_id)
            return
        sid, total = grant["sid"], grant["total"]
        granted_at = sp_runtime.parse_time(grant["at"])
        if not self._allowance_fresh(granted_at, time.time()):
            sp_runtime.log(
                "ignoring a stale reply allowance for %s: granted %.0fs ago, "
                "outside the %.0fs idle window"
                % (sid, time.time() - granted_at, self.reply_budget_window)
            )
            return
        if self._sequence_expired(time.time()):
            # A sequence already past its idle window is over even though no
            # turn has said so yet: end it first, so this grant waits for and
            # binds to the NEXT sequence instead of the dead one.
            self._reset_sequence(
                "the %.0fs idle window elapsed" % self.reply_budget_window, time.time()
            )
        current = self.allowance
        if current and current.get("sid") == sid and current["total"] >= total:
            sp_runtime.log(
                "reply allowance of %d for %s already covers %d; unchanged"
                % (current["total"], sid, total)
            )
            self._save_state()  # an idle expiry above may have ended a sequence
            return
        old_cap = self._cap_for(sid)
        # Bound: the grant continues sid's running sequence and ends with it.
        # Otherwise it waits, while fresh, for sid's next sequence to open it.
        self.allowance = {
            "sid": sid,
            "total": total,
            "at": granted_at,
            "bound": self.budget_sender_sid == sid,
        }
        new_cap = self._cap_for(sid)
        sp_runtime.log(
            "reply allowance for %s set to %d (%d spent)"
            % (sid, new_cap, self.budgets.get(sid, 0))
        )
        if new_cap > old_cap:
            # The requester may hit the raised cap later and should hear so.
            self.budget_notified.discard(sid)
            if self.budgets.get(sid, 0) < new_cap and sid in self.held:
                self._release_one(sid, "allow", sp_claude.live_claude_records())
        self._save_state()

    def _advance_budget_sequence(self, tag):
        """Reset the loop guard when the peer sequence is genuinely broken."""
        sender_sid = tag.get("sid") if isinstance(tag, dict) else None
        now = time.time()
        expired = self._sequence_expired(now)
        changed_peer = sender_sid != self.budget_sender_sid
        if sender_sid is None or expired or changed_peer:
            if sender_sid is None:
                reason = "a direct Codex turn"
            elif expired:
                reason = "the %.0fs idle window elapsed" % self.reply_budget_window
            else:
                reason = "the requesting peer changed"
            self._reset_sequence(reason, now)
        if self.allowance and self.allowance.get("sid") == sender_sid:
            # The grantee's sequence is running: the grant now ends with it.
            self.allowance["bound"] = True
        self.budget_sender_sid = sender_sid
        self.budget_last_at = now if sender_sid else None

    def _sequence_expired(self, now):
        return (
            self.budget_last_at is not None
            and now - self.budget_last_at > self.reply_budget_window
        )

    def _reset_sequence(self, reason, now):
        """End the current sequence: its usage, notices, held replies and any
        grant bound to it go; a fresh grant still waiting for its grantee stays."""
        if self.budgets:
            sp_runtime.log("reply budget sequence reset: %s" % reason)
        if self.held:
            # Only an explicit reset releases a held reply; a sequence that
            # moved on must not receive a stale answer later.
            sp_runtime.log("discarding %d held reply(s): the sequence moved on" % len(self.held))
        self.budgets = {}
        self.budget_notified = set()
        self.held = {}
        if self.allowance and not self._allowance_waiting(now):
            sp_runtime.log("reply allowance for %s dropped: the sequence moved on"
                % self.allowance.get("sid"))
            self.allowance = None
        self.budget_sender_sid = None
        self.budget_last_at = None

    def _allowance_waiting(self, now):
        """True for a fresh grant still waiting for its grantee's next sequence.

        A grant made while another peer (or nobody) held the sequence applies
        to the grantee's next sequence, so that peer's turns or a direct turn
        do not spend it. A grant that already ran with a sequence of the
        grantee's (bound) ends when that sequence does, and an old one expires.
        """
        return (
            not self.allowance.get("bound", True)
            and self._allowance_fresh(sp_runtime.parse_time(self.allowance.get("at")) or 0.0, now)
        )

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
        self._advance_budget_sequence(tag)
        text = sp_protocol.strip_tag(turn.last_agent_message or "").strip()

        records = sp_claude.live_claude_records()
        targets = []

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

        seen = set()
        for rec in targets:
            sid = rec.get("sessionId")
            if sid in seen:
                continue
            seen.add(sid)
            spent = self.budgets.get(sid, 0)
            cap = self._cap_for(sid)
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
                        self.reply_budget_window,
                    )
                )
                # Hold the latest reply instead of losing it: the guard still
                # stops the loop, and an explicit reset (the supervision signal)
                # releases it. Only the requester's own reply is held.
                if sid == tag.get("sid"):
                    self.held[sid] = {
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
                if sid not in self.budget_notified:
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
                            self.reply_budget_window,
                        )
                    )
                    body = sp_protocol.build_cc_body(notice, self.thread_id, self.name, None)
                    if sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, None)):
                        self.budget_notified.add(sid)
                continue
            out = (
                sp_protocol.reply_text(text, tag.get("mid")) if sid == tag.get("sid") else text
            )
            try:
                body = sp_protocol.build_cc_body(out, self.thread_id, self.name, self.sock_path)
            except ValueError as exc:
                sp_runtime.log("cannot build a reply for turn %s: %s" % (turn.turn_id, exc))
                break
            if sp_claude.deliver_to_record(rec, sp_protocol.build_user_frame(body, self.sock_path)):
                self.budgets[sid] = spent + 1
                self._spend_binding(sid)
                # Deliberately no body text: the log is a delivery record, not
                # a transcript, and it lands in a file the user may share.
                sp_runtime.log(
                    "delivered turn %s to %s"
                    % (turn.turn_id, rec.get("name") or sid)
                )
        self._save_state()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(args):
    sp_diagnostics.warn_versions()
    installed_digest = sp_runtime.code_digest(sp_runtime.runtime_code_files())
    all_records = sp_claude.read_claude_records()
    classified = [(record, sp_claude.record_liveness(record)) for record in all_records]
    records = [record for record, status in classified if status == "live"]
    unverified = [
        record for record, status in classified if status == "unverified"
    ]
    threads, schema_ok = sp_codex.codex_threads()
    registered = sp_storage.read_registered()

    def claude_view(record):
        return {
            "pid": record.get("pid"),
            "sessionId": record.get("sessionId"),
            "name": record.get("name"),
            "cwd": record.get("cwd"),
            "status": record.get("status"),
            "version": record.get("version"),
            "kind": record.get("kind"),
            "entrypoint": record.get("entrypoint"),
            "socket": record.get("messagingSocketPath"),
        }

    claude = [claude_view(record) for record in records]
    claude_unverified = [claude_view(record) for record in unverified]
    def codex_view(thread):
        pid = sp_lifecycle.shim_pid(thread["id"])
        state = sp_runtime.read_json(sp_storage.thread_state_path(thread["id"]), {}) if pid else {}
        alias = state.get("name") if isinstance(state, dict) else None
        if not alias:
            try:
                alias = sp_codex.peer_name_for_thread(
                    thread.get("name"),
                    thread["id"],
                    records=records,
                    title_owner=sp_codex.codex_title_owner(thread.get("name"), threads),
                )
            except sp_protocol.NameError_:
                alias = None
        return {
            "id": thread["id"],
            "name": thread.get("name"),
            "peer_name": alias,
            "cwd": thread.get("cwd"),
            "updated_at": thread.get("updated_at"),
            "registered": thread["id"] in registered,
            "holder_pid": thread.get("holder_pid"),
            "shim_pid": pid,
            "shim_code_status": sp_lifecycle.shim_code_status(thread["id"], pid, installed_digest) if pid else None,
            # Liveness straight off the thread record: `true` for a codex[]
            # entry, `null` for a codex_unverified[] one. codex[] stays
            # live-only, so `false` never appears here (see the module docs).
            "live": thread.get("live"),
            "liveness_error": thread.get("liveness_error"),
        }

    codex = [
        codex_view(thread) for thread in threads if thread.get("live") is True
    ]
    codex_unverified = [
        codex_view(thread) for thread in threads if thread.get("live") is None
    ]
    payload = {
        "claude": claude,
        "claude_unverified": claude_unverified,
        "codex": codex,
        "codex_unverified": codex_unverified,
        "codex_schema_recognised": schema_ok,
        "socket_dir": sp_claude.default_socket_dir(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("Claude sessions (%d live):" % len(claude))
    for c in claude:
        print(
            "  %-24s pid %-7s %-6s %s"
            % (c["name"] or "(unnamed)", c["pid"], c["status"] or "?", c["cwd"] or "")
        )
    if claude_unverified:
        print(
            "Claude sessions (%d unverified; process probe unavailable):"
            % len(claude_unverified)
        )
        for c in claude_unverified:
            print("  %-24s pid %-7s %s" % (c["name"] or "(unnamed)", c["pid"], c["cwd"] or ""))
    print("Codex threads (%d live):" % len(codex))
    for t in codex:
        display = t["name"] or "(unnamed)"
        if t["peer_name"] and t["peer_name"] != t["name"]:
            display = "%s [peer %s]" % (display, t["peer_name"])
        print(
            "  %-24s %s  %s  codex pid %s%s"
            % (
                display,
                t["id"],
                "registered" if t["registered"] else "not registered",
                t["holder_pid"],
                (", shim %s, code %s%s" % (
                    t["shim_pid"], t["shim_code_status"],
                    " (run peers.py restart %s)" % t["id"] if t["shim_code_status"] == "stale" else "",
                )) if t["shim_pid"] else "",
            )
        )
    if codex_unverified:
        print("Codex threads (%d unverified; lsof unavailable):" % len(codex_unverified))
        for thread in codex_unverified:
            print(
                "  %-24s %s  %s"
                % (
                    thread["name"] or "(unnamed)",
                    thread["id"],
                    thread.get("cwd") or "",
                )
            )
    if not schema_ok:
        print("  (thread discovery degraded: unknown state_*.sqlite schema)")
    return 0


def _thread_from_args(args, required=False):
    explicit = getattr(args, "from_thread", None)
    value = explicit or os.environ.get("CODEX_THREAD_ID") or os.environ.get(
        "CODEX_SESSION_ID"
    )
    if value and not sp_runtime.is_uuid(value):
        if explicit or required:
            raise ValueError("--from-thread/CODEX_THREAD_ID must be a UUID")
        return None
    if required and not value:
        raise ValueError(
            "a request needs --from-thread or CODEX_THREAD_ID so its origin is explicit"
        )
    return value


def _print_send_result(args, payload, human):
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(human)


def _expand_buddy_arg(args, attr, purpose):
    """Replace `buddy` in args.<attr>; returns an exit code on failure."""
    try:
        setattr(args, attr, expand_buddy(getattr(args, attr), args, purpose))
    except BuddyError as exc:
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
        thread_id = _thread_from_args(args)
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
        thread_id = _thread_from_args(args, required=True)
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
        thread_id = _thread_from_args(args, required=True)
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
        thread_id = _thread_from_args(args, required=True)
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


class BuddyError(Exception):
    """A buddy lookup or direction that fails; carries the exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def _budget_target(value, args):
    """(thread uuid, None) for a budget command, or (None, (code, message))."""
    try:
        value = expand_buddy(value, args, "budget")
    except BuddyError as exc:
        return None, (exc.code, str(exc))
    if value.startswith("cc:"):
        return None, (2, "the reply budget belongs to a Codex thread's shim, not "
                         "a Claude session")
    if value.startswith("codex:"):
        value = value[len("codex:") :]
    try:
        return sp_codex.resolve_thread_prefer_live(value)["id"], None
    except sp_codex.ResolveError as exc:
        if not sp_runtime.is_uuid(value):
            return None, (1, str(exc))
        return value, None


def cmd_budget(args):
    if args.budget_cmd == "allow":
        return cmd_budget_allow(args)
    if args.budget_cmd != "reset":
        sys.stderr.write("error: budget subcommands are `reset` and `allow`\n")
        return 2
    tid, failure = _budget_target(args.thread, args)
    if failure:
        sys.stderr.write("error: %s\n" % failure[1])
        return failure[0]
    # An explicit reset drops any allowance, including one not yet consumed.
    sp_requests._unlink_quiet(sp_storage.budget_allow_path(tid))
    with open(sp_storage.budget_reset_path(tid), "w", encoding="utf-8") as fh:
        fh.write(sp_runtime.now_iso() + "\n")
    state_path = sp_storage.thread_state_path(tid)
    state = sp_runtime.read_json(state_path, None)
    if isinstance(state, dict) and (state.get("budgets") or state.get("allowance")):
        state["budgets"] = {}
        state["allowance"] = None
        sp_runtime.write_json_atomic(state_path, state, mode=0o600)
    print("reply budget reset for %s" % tid)
    return 0


def cmd_budget_allow(args):
    """Grant one requester a TOTAL reply allowance on one Codex thread."""
    if not 1 <= args.replies <= sp_constants.BUDGET_ALLOW_MAX:
        sys.stderr.write(
            "error: --replies must be between 1 and %d\n" % sp_constants.BUDGET_ALLOW_MAX
        )
        return 2
    sid = args.for_session
    if sid is None:
        try:
            caller = caller_identity(args)
        except ValueError:
            caller = None
        if caller is None or caller["kind"] != "cc":
            sys.stderr.write(
                "error: --for-session <uuid> is required when the caller is not "
                "a Claude session\n"
            )
            return 2
        sid = caller["uuid"]
    if not sp_runtime.is_uuid(sid):
        sys.stderr.write("error: --for-session must be a Claude session UUID\n")
        return 2
    tid, failure = _budget_target(args.thread, args)
    if failure:
        sys.stderr.write("error: %s\n" % failure[1])
        return failure[0]
    pid = sp_lifecycle.shim_pid(tid)
    if pid and not sp_lifecycle.shim_supports(tid, pid, "budget_allow"):
        sys.stderr.write(
            "error: cannot verify the running shim for %s (pid %d) supports "
            "allowances; a shim started from an older peers.py never reads the "
            "grant, so the cap would stay %d. Restart it: `"
            "peers.py restart %s` (keeps the budget and its spent replies), then "
            "grant again\n" % (tid, pid, sp_constants.REPLY_BUDGET, tid)
        )
        return 1
    path = sp_storage.budget_allow_path(tid)
    total = args.replies
    pending = sp_runtime.read_json(path, None)
    window = sp_runtime._float_env("SESSION_PEERS_REPLY_BUDGET_WINDOW", sp_constants.REPLY_BUDGET_WINDOW_DEFAULT)
    if window <= 0:
        window = sp_constants.REPLY_BUDGET_WINDOW_DEFAULT
    granted_at = sp_runtime.now_iso()
    if isinstance(pending, dict) and pending.get("sid") == sid:
        # Not yet consumed: two grants before the shim polls keep the higher,
        # with the higher's own time, so a merge never refreshes an old grant.
        previous = pending.get("total")
        previous_at = sp_runtime.parse_time(pending.get("at"))
        if (
            isinstance(previous, int)
            and not isinstance(previous, bool)
            and previous_at is not None
            and time.time() - previous_at <= window
            and min(previous, sp_constants.BUDGET_ALLOW_MAX) >= total
        ):
            total = min(previous, sp_constants.BUDGET_ALLOW_MAX)
            granted_at = pending["at"]
    sp_runtime.write_json_atomic(path, {"sid": sid, "total": total, "at": granted_at}, mode=0o600)
    print(
        "reply allowance for %s: up to %d consecutive replies to session %s "
        "(a total for this sequence; replies already delivered still count)"
        % (tid, max(sp_constants.REPLY_BUDGET, total), sid)
    )
    if not pid:
        print("no shim is running for %s; the grant applies once one starts" % tid)
    return 0


# --------------------------------------------------------------------------
# Buddies: one bound peer per session
# --------------------------------------------------------------------------


def parse_typed(value):
    """`cc:<uuid>` / `codex:<uuid>` into a typed identity, or ValueError."""
    kind, sep, ident = str(value or "").partition(":")
    if not sep or kind not in sp_constants.BUDDY_KINDS or not sp_runtime.is_uuid(ident):
        raise ValueError("expected cc:<uuid> or codex:<uuid>, got %r" % value)
    return {"kind": kind, "uuid": ident}


def caller_identity(args):
    """The typed identity of the session running this command."""
    explicit = getattr(args, "as_identity", None)
    if explicit:
        return parse_typed(explicit)
    sid = sp_claude._current_claude_session_id()
    if sid:
        if not sp_runtime.is_uuid(sid):
            raise ValueError("this Claude session's id %r is not a UUID" % sid)
        return {"kind": "cc", "uuid": sid}
    tid = _thread_from_args(args)
    if tid:
        return {"kind": "codex", "uuid": tid}
    raise ValueError("cannot tell which session is asking; pass --as")


def _resolve_typed_kind(kind, target, live_only=False, exclude=None):
    if kind == "cc":
        rec = sp_claude._resolve_claude_record(target, exclude=exclude)
        sid = rec.get("sessionId")
        if not sp_runtime.is_uuid(sid):
            # A bound buddy is addressed by UUID only; a non-UUID id would be
            # re-read as a name later.
            raise sp_codex.ResolveError("Claude session %r has no UUID session id" % target)
        return {"kind": "cc", "uuid": sid, "name": rec.get("name")}
    # Codex reuses titles, so a live thread usually shares its name with dead
    # ones: the live match wins, and dead threads count only when none is live.
    if live_only:
        thread = sp_codex.resolve_thread(target, require_live=True, exclude=exclude)
    else:
        thread = sp_codex.resolve_thread_prefer_live(target, exclude=exclude)
    return {"kind": "codex", "uuid": thread["id"], "name": thread.get("name")}


def resolve_typed(target, exclude=None):
    """`cc:x`, `codex:x` or a bare `[@]x` into one typed identity with its name.

    Names are resolved here, once; a bound buddy is used by UUID afterwards.
    ``exclude`` (the caller's UUID) keeps the caller out of NAME matching, so
    a session never competes with its own namesake.
    """
    target = str(target or "")
    if target.startswith("@"):
        target = target[1:]
    for kind in sp_constants.BUDDY_KINDS:
        if target.startswith(kind + ":"):
            return _resolve_typed_kind(
                kind, target[len(kind) + 1 :], exclude=exclude
            )
    if not target:
        raise sp_codex.ResolveError("an empty target names no session")
    found, errors, dead_codex = [], [], False
    for kind in sp_constants.BUDDY_KINDS:
        try:
            # Live Codex threads only here: a dead namesake must not compete
            # with a live Claude session for the same bare name.
            found.append(
                _resolve_typed_kind(kind, target, live_only=True, exclude=exclude)
            )
        except sp_codex.ResolveNotFound:
            pass
        except sp_codex.ResolveNoLive:
            dead_codex = True
        except sp_codex.ResolveError as exc:
            errors.append(exc)
    if dead_codex and not found and not errors:
        # Nothing live carries the name: bind the dead thread as `codex:` would.
        return _resolve_typed_kind("codex", target, exclude=exclude)
    if errors:
        # An ambiguous or unverifiable side could be the one meant: never guess.
        raise sp_codex.ResolveAmbiguousKind(
            "%s; pick the kind" % errors[0],
            ["%s:%s" % (kind, target) for kind in sp_constants.BUDDY_KINDS],
        )
    if len(found) > 1:
        # Routine for an attached Codex thread: its UUID also names its shim's
        # Claude-facing registry record.
        choices = ["%s:%s" % (i["kind"], i["uuid"]) for i in found]
        raise sp_codex.ResolveAmbiguousKind(
            "%r matches both %s; pick one" % (target, " and ".join(choices)),
            choices,
        )
    if not found:
        raise sp_codex.ResolveNotFound(
            "no Claude session or Codex thread named %r; run `peers.py list`" % target
        )
    return found[0]


def read_buddy(owner):
    """The owner's buddy record, or None when absent or malformed."""
    rec = sp_runtime.read_json(sp_storage.buddy_path(owner), None)
    if not isinstance(rec, dict):
        return None
    buddy = rec.get("buddy")
    if (
        not isinstance(buddy, dict)
        or buddy.get("kind") not in sp_constants.BUDDY_KINDS
        or not sp_runtime.is_uuid(buddy.get("uuid"))
    ):
        return None
    return rec


def _parse_uses(value):
    if value is None:
        return list(sp_constants.BUDDY_USES)
    uses = []
    for word in value.split(","):
        word = word.strip()
        if word and word not in uses:
            uses.append(word)
    unknown = [word for word in uses if word not in sp_constants.BUDDY_USES]
    if unknown or not uses:
        raise ValueError(
            "unsupported --uses %s; supported: %s"
            % (", ".join(unknown) or "(empty)", ", ".join(sp_constants.BUDDY_USES))
        )
    return uses


def expand_buddy(value, args, purpose):
    """Turn `buddy`/`@buddy` into the caller's bound `cc:`/`codex:` UUID."""
    if value not in ("buddy", "@buddy"):
        return value
    try:
        owner = caller_identity(args)
    except ValueError as exc:
        raise BuddyError(str(exc), 2)
    rec = read_buddy(owner)
    if rec is None:
        raise BuddyError(
            "no buddy set for %s:%s; run `peers.py buddy set <name|uuid>`"
            % (owner["kind"], owner["uuid"])
        )
    buddy = rec["buddy"]
    if purpose in ("ask", "dispatch", "wait") and buddy["kind"] != "cc":
        raise BuddyError(
            "ask/dispatch/wait need a Claude buddy; use send (asynchronous)", 2
        )
    if purpose == "budget" and buddy["kind"] != "codex":
        raise BuddyError(
            "the reply budget belongs to a Codex thread's shim; this buddy is a "
            "Claude session", 2
        )
    return "%s:%s" % (buddy["kind"], buddy["uuid"])


def _claude_status(uuid):
    status = {"name": None, "live": None, "registered": None, "shim_pid": None,
              "status": None, "paused": None}
    records = [r for r in sp_claude.read_claude_records() if r.get("sessionId") == uuid]
    states = [(r, sp_claude.record_liveness(r)) for r in records]
    live = [r for r, state in states if state == "live"]
    if live:
        rec = live[0]
        status.update(live=True, name=rec.get("name"), status=rec.get("status"))
        if sp_claude.socket_path_ok(rec.get("messagingSocketPath")):
            status["route"] = "available"
        else:
            status["route"] = "unavailable: its socket is outside the allowlist"
    elif any(state == "unverified" for _r, state in states):
        status["name"] = states[0][0].get("name")
        status["route"] = "unavailable: liveness is unverified (process probe denied)"
    else:
        status["live"] = False
        status["route"] = "unavailable: no live Claude session with that id"
    return status


def _codex_status(uuid, attach):
    status = {"name": None, "live": None, "registered": None, "shim_pid": None,
              "status": None, "paused": None}
    try:
        thread = sp_codex.resolve_thread(uuid, require_live=False)
    except sp_codex.ResolveError as exc:
        status["route"] = "unavailable: %s" % exc
        return status
    status.update(
        name=thread.get("name"),
        live=thread.get("live"),
        registered=thread.get("registered"),
    )
    pid = sp_lifecycle.shim_pid(uuid)
    if attach and pid is None and thread.get("live") is True:
        # Transient attach only: `up` would also reset the reply budget,
        # release held replies and register the thread persistently.
        pid = sp_lifecycle.attach_thread(uuid, verbose=False)
    status["shim_pid"] = pid
    if pid:
        shim_rec = sp_runtime.read_json(os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % pid), None)
        if isinstance(shim_rec, dict) and shim_rec.get("sessionId") == uuid:
            status["status"] = shim_rec.get("status")
    rollout = thread.get("rollout_path")
    if rollout and os.access(rollout, os.R_OK):
        status["paused"] = sp_rollout.thread_is_paused(rollout)
    if thread.get("live") is None:
        status["route"] = "unavailable: liveness is unverified"
    elif not thread.get("live"):
        status["route"] = "unavailable: the thread is not live"
    elif not pid:
        status["route"] = "unavailable: no shim attached (`peers.py buddy ping` attaches one)"
    else:
        status["route"] = "available"
    return status


def buddy_status(buddy, attach=False):
    """Observed facts about a bound buddy; unknown values stay None."""
    if buddy["kind"] == "cc":
        status = _claude_status(buddy["uuid"])
    else:
        status = _codex_status(buddy["uuid"], attach)
    status["kind"] = buddy["kind"]
    status["uuid"] = buddy["uuid"]
    return status


def _buddy_line(rec, status):
    name = status.get("name") or rec["buddy"].get("name") or "(unnamed)"
    live = {True: "live", False: "not live", None: "liveness unknown"}[status.get("live")]
    parts = ["%s" % live]
    if status["kind"] == "codex":
        registered = status.get("registered")
        parts.append(
            "registered=%s"
            % ({True: "yes", False: "no"}.get(registered, "unknown"))
        )
        parts.append("shim=%s" % (status.get("shim_pid") or "none"))
    parts.append(status.get("status") or "status unknown")
    if status.get("paused"):
        parts.append("paused")
    route = status.get("route") or "unavailable: unknown"
    route_text = "route available" if route == "available" else "route %s" % route
    line = "buddy = %s (%s, %s) %s; %s; uses: %s" % (
        name,
        status["kind"],
        status["uuid"][:8],
        ", ".join(parts),
        route_text,
        ", ".join(rec.get("uses") or []),
    )
    left = _replies_left(rec)
    if left is not None:
        line += "; replies left: %d of %d" % (left, rec["replies"])
    return line


def _replies_left(rec):
    """Replies remaining on a binding's total, or None when it has none.

    Read from the shim's saved state once it has applied the grant; until then
    the whole total is pending.
    """
    total = rec.get("replies")
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        return None
    state = sp_runtime.read_json(sp_storage.thread_state_path(rec["buddy"]["uuid"]), None)
    binding = state.get("binding") if isinstance(state, dict) else None
    if (
        isinstance(binding, dict)
        and binding.get("bind_id") == rec.get("bind_id")
        and isinstance(binding.get("spent"), int)
    ):
        return max(0, binding.get("total", total) - binding["spent"])
    return total


def _print_buddy(args, rec, status):
    if getattr(args, "json", False):
        payload = dict(rec)
        payload["status"] = status
        left = _replies_left(rec)
        if left is not None:
            payload["replies_left"] = left
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_buddy_line(rec, status))


def cmd_buddy(args):
    action = args.buddy_cmd or "show"
    try:
        owner = caller_identity(args)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    path = sp_storage.buddy_path(owner)
    if action == "clear":
        try:
            with sp_storage.owner_lock(owner["uuid"]):
                old = read_buddy(owner)
                with sp_storage._binding_locks(_revocable_thread(owner, old)):
                    # Revoke first: a failed revoke keeps the record, so clear
                    # can be retried.
                    try:
                        _revoke_binding(owner, old)
                    except OSError as exc:
                        sys.stderr.write(
                            "error: could not revoke the reply total on %s (%s); "
                            "the buddy is still bound, retry `buddy clear`\n"
                            % (old["buddy"]["uuid"], exc)
                        )
                        return 1
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        print("no buddy was set")
                        return 0
        except sp_storage.BindingLockTimeout as exc:
            sys.stderr.write("error: %s; the buddy is still bound, retry\n" % exc)
            return 1
        print("buddy cleared")
        return 0
    if action == "set":
        try:
            uses = _parse_uses(args.uses)
        except ValueError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        target = args.target
        if target is None:
            # No name given: look for a peer sharing this session's own name.
            try:
                target = _resolve_typed_kind(owner["kind"], owner["uuid"]).get("name")
            except sp_codex.ResolveError:
                target = None
            if not target or sp_runtime.is_uuid(target):
                sys.stderr.write(
                    "error: this session has no name to look up; name the buddy\n"
                )
                return 1
        try:
            buddy = resolve_typed(target, exclude=owner["uuid"])
        except sp_codex.ResolveNotFound as exc:
            if args.target is None:
                sys.stderr.write(
                    "error: no other session is named %r; name the buddy\n" % target
                )
            else:
                sys.stderr.write("error: %s\n" % exc)
            return 1
        except sp_codex.ResolveAmbiguousKind as exc:
            sys.stderr.write("error: %s. Retry with one of:\n" % exc)
            for choice in exc.choices:
                sys.stderr.write("  %s\n" % _buddy_set_command(choice, args))
            return 1
        except sp_codex.ResolveError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        if buddy["kind"] == owner["kind"] and buddy["uuid"] == owner["uuid"]:
            sys.stderr.write("error: a session cannot be its own buddy\n")
            return 2
        # An explicit --replies must be honoured or the bind fails; the default
        # total for a Claude session's Codex buddy is best-effort (a warning).
        explicit = args.replies is not None
        grant = args.replies
        if (
            not explicit
            and owner["kind"] == "cc"
            and buddy["kind"] == "codex"
            and sp_lifecycle.shim_pid(buddy["uuid"])
        ):
            grant = sp_constants.BUDDY_REPLIES_DEFAULT
        if grant is not None:
            # An explicit grant attaches the shim when needed, so this runs
            # outside every lock; the default never attaches one.
            refusal = _check_buddy_replies(grant, owner, buddy, attach=explicit)
            if refusal and explicit:
                sys.stderr.write("error: %s\n" % refusal[1])
                return refusal[0]
            if refusal:
                sys.stderr.write(
                    "warning: bound without the default reply total: %s\n" % refusal[1]
                )
                grant = None
        try:
            with sp_storage.owner_lock(owner["uuid"]):
                old = read_buddy(owner)
                same = (
                    old is not None
                    and old["buddy"]["kind"] == buddy["kind"]
                    and old["buddy"]["uuid"] == buddy["uuid"]
                )
                kept, bind_id = None, None
                if same and old.get("bind_id") and isinstance(old.get("replies"), int):
                    kept, bind_id = old["replies"], old["bind_id"]
                locked = set()
                if grant is not None:
                    locked.add(buddy["uuid"])
                if not same and _revocable_thread(owner, old):
                    locked.add(old["buddy"]["uuid"])
                with sp_storage._binding_locks(*locked):
                    # The conflict check and every write below share the
                    # locks, so a competing owner cannot slip between check
                    # and publish.
                    if grant is not None:
                        conflict = _binding_conflict(owner, buddy)
                        if conflict and explicit:
                            sys.stderr.write("error: %s\n" % conflict)
                            return 1
                        if conflict:
                            sys.stderr.write(
                                "warning: bound without the default reply total: %s\n"
                                % conflict
                            )
                            grant = None
                    replies = kept
                    if grant is not None:
                        replies = max(grant, kept or 0)
                        bind_id = bind_id or uuidlib.uuid4().hex
                    rec = {
                        "owner": owner,
                        "buddy": buddy,
                        "uses": uses,
                        "set_at": sp_runtime.now_iso(),
                    }
                    if replies is not None:
                        rec["replies"] = replies
                        rec["bind_id"] = bind_id
                    if not same:
                        try:
                            _revoke_binding(owner, old)
                        except OSError as exc:
                            sys.stderr.write(
                                "error: could not revoke the reply total on %s "
                                "(%s); the old buddy is still bound, retry\n"
                                % (old["buddy"]["uuid"], exc)
                            )
                            return 1
                    # Record first, grant second: whatever fails in between,
                    # no grant exists without a record that can revoke it, and
                    # a retried set reuses the record's bind_id (so the shim
                    # keeps the spent count).
                    try:
                        sp_runtime.write_json_atomic(path, rec, mode=0o600)
                        if grant is not None:
                            sp_runtime.write_json_atomic(
                                sp_storage.budget_binding_path(buddy["uuid"]),
                                {
                                    "sid": owner["uuid"],
                                    "bind_id": bind_id,
                                    "total": replies,
                                    "at": sp_runtime.now_iso(),
                                },
                                mode=0o600,
                            )
                    except OSError as exc:
                        sys.stderr.write(
                            "error: could not bind the buddy (%s); retry "
                            "`buddy set` (a retry keeps replies already spent)\n"
                            % exc
                        )
                        return 1
        except sp_storage.BindingLockTimeout as exc:
            sys.stderr.write("error: %s; nothing was changed, retry\n" % exc)
            return 1
        _print_buddy(args, rec, buddy_status(buddy, attach=True))
        return 0
    rec = read_buddy(owner)
    if rec is None:
        sys.stderr.write("error: no buddy set; run `peers.py buddy set <name|uuid>`\n")
        return 1
    _print_buddy(args, rec, buddy_status(rec["buddy"], attach=(action == "ping")))
    return 0


def _check_buddy_replies(replies, owner, buddy, attach=True):
    """None when ``buddy set --replies`` can be honoured, else (code, message).

    ``attach=False`` (the default total) uses only an already running shim.
    """
    if not 1 <= replies <= sp_constants.BUDDY_REPLIES_MAX:
        return 2, "--replies must be between 1 and %d" % sp_constants.BUDDY_REPLIES_MAX
    if owner["kind"] != "cc" or buddy["kind"] != "codex":
        return 2, (
            "--replies applies to a Claude session's Codex buddy only: the "
            "reply budget belongs to a Codex thread's shim"
        )
    # The shim is attached here, outside the binding lock: a shim takes that
    # lock itself when it reads a marker.
    pid = sp_lifecycle.shim_pid(buddy["uuid"])
    if not pid and attach:
        pid = sp_lifecycle.attach_thread(buddy["uuid"], verbose=False)
    if not pid:
        return 1, (
            "no running shim for %s, so a reply total cannot be granted; start "
            "it (`peers.py buddy ping`) and bind again" % buddy["uuid"]
        )
    if not sp_lifecycle.shim_supports(buddy["uuid"], pid, "binding_allowance"):
        return 1, (
            "cannot verify the running shim for %s (pid %d) supports buddy "
            "allowances; a shim started from an older peers.py never reads the "
            "grant, so the cap would stay %d. Restart it: `peers.py restart %s`, "
            "then bind again" % (buddy["uuid"], pid, sp_constants.REPLY_BUDGET, buddy["uuid"])
        )
    if replies > sp_constants.BUDGET_ALLOW_MAX and not sp_lifecycle.shim_supports(
        buddy["uuid"], pid, "binding_allowance_max500"
    ):
        return 1, (
            "the running shim for %s (pid %d) accepts a reply total of at most %d; "
            "restart it (`peers.py restart %s`) and bind again"
            % (buddy["uuid"], pid, sp_constants.BUDGET_ALLOW_MAX, buddy["uuid"])
        )
    return None


def _binding_conflict(owner, buddy):
    """A message when another session already holds a reply total on this thread.

    A shim keeps one binding per thread, so a second owner would overwrite the
    first one's spent count or revoke. Checked from the pending marker, the
    shim's saved binding and the other owners' buddy records. The caller holds
    the target thread's binding lock through this check and publication.
    """
    tid = buddy["uuid"]
    holders = set()
    marker = sp_runtime.read_json(sp_storage.budget_binding_path(tid), None)
    if isinstance(marker, dict) and isinstance(marker.get("sid"), str):
        holders.add(marker["sid"])
    state = sp_runtime.read_json(sp_storage.thread_state_path(tid), None)
    binding = state.get("binding") if isinstance(state, dict) else None
    if isinstance(binding, dict) and isinstance(binding.get("sid"), str):
        holders.add(binding["sid"])
    try:
        names = os.listdir(sp_storage.buddies_dir())
    except OSError:
        names = []
    for name in names:
        rec = sp_runtime.read_json(os.path.join(sp_storage.buddies_dir(), name), None)
        if not isinstance(rec, dict) or not rec.get("bind_id"):
            continue
        rec_buddy, rec_owner = rec.get("buddy"), rec.get("owner")
        if (
            isinstance(rec_buddy, dict)
            and rec_buddy.get("uuid") == tid
            and isinstance(rec_owner, dict)
            and isinstance(rec_owner.get("uuid"), str)
        ):
            holders.add(rec_owner["uuid"])
    holders.discard(owner["uuid"])
    if holders:
        return (
            "another session (%s) already holds a reply total on this Codex "
            "thread; a thread carries one at a time. It must `buddy clear` (or "
            "bind another buddy) first" % sorted(holders)[0]
        )
    return None


def _revocable_thread(owner, old):
    """The Codex thread holding ``old``'s reply total, or None when it has none."""
    if (
        old is None
        or not old.get("bind_id")
        or old["buddy"]["kind"] != "codex"
        or owner["kind"] != "cc"
    ):
        return None
    return old["buddy"]["uuid"]


def _revoke_binding(owner, old):
    """Revoke the reply total a replaced or cleared binding granted.

    Raises OSError when the revoke marker cannot be written, so the caller
    keeps the record and the revocation can be retried.
    """
    if _revocable_thread(owner, old) is None:
        return
    sp_runtime.write_json_atomic(
        sp_storage.budget_binding_path(old["buddy"]["uuid"]),
        {
            "sid": owner["uuid"],
            "bind_id": old["bind_id"],
            "revoke": True,
            "at": sp_runtime.now_iso(),
        },
        mode=0o600,
    )


def _buddy_set_command(target, args):
    """The `buddy set` command line for ``target``, keeping the user's options."""
    argv = ["peers.py", "buddy", "set", target]
    if getattr(args, "uses", None) is not None:
        argv += ["--uses", args.uses]
    if getattr(args, "replies", None) is not None:
        argv += ["--replies", str(args.replies)]
    if getattr(args, "as_identity", None):
        argv += ["--as", args.as_identity]
    return " ".join(shlex.quote(part) for part in argv)


def _owner_verified_gone(owner):
    """True only when the owner is PROVEN not live; unknown keeps the record."""
    if owner["kind"] == "cc":
        try:
            os.listdir(sp_storage.claude_sessions_dir())
        except FileNotFoundError:
            return True
        except OSError:
            return False
        states = [
            sp_claude.record_liveness(r)
            for r in sp_claude.read_claude_records()
            if r.get("sessionId") == owner["uuid"]
        ]
        return all(state == "dead" for state in states)
    try:
        thread = sp_codex.resolve_thread(owner["uuid"], require_live=False)
    except sp_codex.ResolveError:
        return False
    return thread.get("live") is False


def gc_buddy_records(days=sp_constants.GC_DAYS_DEFAULT, dry_run=False, verbose=True):
    """Prune buddy records whose owner is verified gone and older than days."""
    if days < 0:
        raise ValueError("retention days must be zero or greater")
    cutoff = time.time() - days * 86400.0
    try:
        names = sorted(os.listdir(sp_storage.buddies_dir()))
    except OSError:
        return []
    removed = []
    for name in names:
        kind, sep, rest = name.partition("-")
        if not sep or kind not in sp_constants.BUDDY_KINDS or not rest.endswith(".json"):
            continue
        ident = rest[: -len(".json")]
        if not sp_runtime.is_uuid(ident):
            continue
        path = os.path.join(sp_storage.buddies_dir(), name)
        try:
            if os.stat(path).st_mtime > cutoff:
                continue
        except OSError:
            continue
        if not _owner_verified_gone({"kind": kind, "uuid": ident}):
            continue
        if not dry_run:
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                sp_runtime.log("could not prune %s: %s" % (path, exc))
                continue
        removed.append(name)
        if verbose:
            print("%s buddy record %s" % ("would prune" if dry_run else "pruned", name))
    return removed


def cmd_gc(args):
    days = (
        args.days
        if args.days is not None
        else sp_runtime._float_env("SESSION_PEERS_GC_DAYS", sp_constants.GC_DAYS_DEFAULT)
    )
    try:
        removed = gc_bridge_state(days=days, dry_run=args.dry_run)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    buddies = gc_buddy_records(days=days, dry_run=args.dry_run)
    requests = sp_requests.cleanup_expired_requests(dry_run=args.dry_run)
    if not args.dry_run:
        topic_prune_all()
    for request_id in requests:
        print("%s expired request %s" % ("would prune" if args.dry_run else "pruned", request_id))
    if not removed and not requests and not buddies:
        print("no stale bridge metadata")
    return 0


def cmd_session_hook(_args):
    """Codex SessionStart: reconcile detached, answer `{}`, never block."""
    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
    except (ValueError, OSError):
        payload = {}
    source = payload.get("source", "unknown")
    session_id = payload.get("session_id")
    script = sp_runtime.entrypoint_path()
    log_path = os.path.join(sp_storage.state_dir(), "session-hook.log")
    try:
        command = [sys.executable, script, "hook-reconcile"]
        if (
            _args.auto_attach
            and source in ("startup", "resume")
            and sp_runtime.is_uuid(session_id)
        ):
            command.extend(["--thread", session_id])
        sp_runtime.spawn_detached(command, log_path)
    except OSError as exc:
        sp_runtime.log("could not start the reconcile: %s" % exc)
    try:
        with open(log_path, "a", encoding="utf-8") as logfh:
            logfh.write(
                "session-hook: source=%s session=%s auto_attach=%s\n"
                % (source, session_id or "-", bool(_args.auto_attach))
            )
    except OSError:
        pass
    print("{}")
    return 0


def cmd_hook_reconcile(args):
    """Detached SessionStart worker: GC, reconcile, then attach this UUID."""
    days = sp_runtime._float_env("SESSION_PEERS_GC_DAYS", sp_constants.GC_DAYS_DEFAULT)
    try:
        gc_bridge_state(days=days, verbose=False)
    except ValueError as exc:
        sp_runtime.log("GC skipped: %s" % exc)
    sp_requests.cleanup_expired_requests()
    sp_lifecycle.reconcile(verbose=False)
    if args.thread:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if sp_lifecycle.attach_thread(args.thread, verbose=False):
                break
            time.sleep(0.25)
    return 0


# --------------------------------------------------------------------------
# install-hook
# --------------------------------------------------------------------------


def hook_command(script=None, auto_attach=False):
    """The exact command string the SessionStart entry runs.

    The path is quoted: a checkout under a directory with a space would
    otherwise split into two arguments and the hook would fail (P8).
    """
    script = script or sp_runtime.entrypoint_path()
    command = "python3 %s session-hook" % shlex.quote(script)
    return command + (" --auto-attach" if auto_attach else "")


def hook_entry(auto_attach=False):
    return {
        "matcher": "startup|resume",
        "hooks": [
            {
                "type": "command",
                "command": hook_command(auto_attach=auto_attach),
                "timeout": 10,
            }
        ]
    }


def _hooks_event_map(data):
    """(event_map, wrapper) for both shapes a hooks.json is seen in."""
    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        return data["hooks"], data
    if isinstance(data, dict):
        return data, data
    return {}, {}


def read_json_strict(path):
    """(status, data): "absent", "unreadable" or "ok".

    read_json flattens a missing file and a corrupt one into the same default,
    which is the wrong call for a file the installer is about to REPLACE (S6).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "ok", json.load(fh)
    except FileNotFoundError:
        return "absent", None
    except (OSError, ValueError) as exc:
        return "unreadable", exc


def cmd_install_hook(args):
    path = os.path.join(sp_storage.codex_home(), "hooks.json")
    os.makedirs(sp_storage.codex_home(), exist_ok=True)
    status, data = read_json_strict(path)
    if status == "unreadable":
        # Never overwrite a file we could not read: the user's third-party
        # entries would go with it.
        try:
            backup = backup_file(path)
        except OSError as exc:
            sys.stderr.write("error: could not back up %s: %s\n" % (path, exc))
            return 1
        sys.stderr.write(
            "error: %s is not valid JSON (%s). It was copied to %s and left "
            "alone; fix it and re-run, or add this SessionStart entry by hand:\n"
            "  %s\n"
            % (path, data, backup, json.dumps(hook_entry(args.auto_attach)))
        )
        return 1
    created = status == "absent"
    if not isinstance(data, dict):
        data = {}
    if not data:
        # P4: the real ~/.codex/hooks.json wraps the event map in "hooks";
        # a file we create must match, and a file we read may use either shape.
        data = {"hooks": {}}
    events, root = _hooks_event_map(data)
    entries = events.get("SessionStart")
    if not isinstance(entries, list):
        entries = []
    desired = hook_entry(args.auto_attach)
    commands = {hook_command(), hook_command(auto_attach=True)}
    matched = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        for hook_index, hook in enumerate(entry.get("hooks") or []):
            if isinstance(hook, dict) and hook.get("command") in commands:
                matched = (index, hook_index)
                break
        if matched is not None:
            break
    if matched is not None and entries[matched[0]] == desired:
        print("SessionStart entry already installed in %s" % path)
        _print_trust_step()
        return 0
    if matched is not None:
        entry_index, hook_index = matched
        current_entry = entries[entry_index]
        current_hook = current_entry["hooks"][hook_index]
        desired_hook = desired["hooks"][0]
        if (
            current_entry.get("matcher") == desired["matcher"]
            and all(current_hook.get(key) == value for key, value in desired_hook.items())
        ):
            print("SessionStart entry already installed in %s" % path)
            _print_trust_step()
            return 0
    if not created:
        try:
            print("backed up %s to %s" % (path, backup_file(path)))
        except OSError as exc:
            sys.stderr.write("error: could not back up %s: %s\n" % (path, exc))
            return 1
    if matched is None:
        entries.append(desired)
        action = "added"
    else:
        entry_index, hook_index = matched
        existing = dict(entries[entry_index])
        sibling_hooks = list(existing.get("hooks") or [])
        updated_hook = dict(sibling_hooks[hook_index])
        updated_hook.update(desired["hooks"][0])
        del sibling_hooks[hook_index]
        if sibling_hooks:
            existing["hooks"] = sibling_hooks
            entries[entry_index] = existing
            moved = dict(existing)
            moved["matcher"] = desired["matcher"]
            moved["hooks"] = [updated_hook]
            entries.append(moved)
        else:
            existing["matcher"] = desired["matcher"]
            existing["hooks"] = [updated_hook]
            entries[entry_index] = existing
        action = "updated"
    events["SessionStart"] = entries
    # S6: keep the file's own mode; only a file we create gets 0600.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = 0o600
    sp_runtime.write_json_atomic(path, root, mode=mode)
    print("%s one SessionStart entry in %s" % (action, path))
    _print_trust_step()
    return 0


def backup_file(path, tag="session-peers"):
    """Copy `path` beside itself with a timestamp. Returns the backup path."""
    backup = "%s.%s-bak-%s" % (
        path,
        tag,
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )
    with open(path, "rb") as src, open(backup, "wb") as dst:
        dst.write(src.read())
    return backup


def _print_trust_step():
    print(
        "Next: open Codex and run /hooks to trust the new entry. Until you do, "
        "it is inert; `peers.py up` by hand does the same job."
    )


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def entry_hash(entry):
    """A stable sha256 over one hook entry's JSON.

    Codex's own `trusted_hash` algorithm is not documented and could not be
    read off the binary, so this is reported as information, never compared:
    doctor says "trust unknown, open /hooks" rather than guessing.
    """
    blob = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def unix_socket_probe(sock_dir):
    """Return None when AF_UNIX bind works, otherwise the exact error."""
    path = os.path.join(sock_dir, ".session-peers-doctor-%d.sock" % os.getpid())
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError as exc:
        return str(exc)
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(path)
        except (FileNotFoundError, OSError):
            pass
    return None


def cmd_doctor(_args):
    sp_diagnostics.warn_versions()
    installed_digest = sp_runtime.code_digest(sp_runtime.runtime_code_files())
    lines = []

    def add(status, text):
        lines.append("%-5s %s" % (status, text))

    claude_v = sp_runtime.tool_version("claude")
    codex_v = sp_runtime.tool_version("codex")
    add(
        "warn" if claude_v and sp_runtime.version_is_newer(claude_v, sp_constants.CLAUDE_CODE_TESTED) else "ok",
        "Claude Code %s (tested against %s)"
        % (claude_v.strip() if claude_v else "not on PATH", sp_constants.CLAUDE_CODE_TESTED),
    )
    add(
        "warn" if codex_v and sp_runtime.version_is_newer(codex_v, sp_constants.CODEX_TESTED) else "ok",
        "Codex CLI %s (tested against %s)"
        % (codex_v.strip() if codex_v else "not on PATH", sp_constants.CODEX_TESTED),
    )
    add("ok" if codex_v else "fail", "codex on PATH")
    rc, _out, _err = sp_runtime.run_cmd(["lsof", "-v"], timeout=10)
    add("ok" if rc != 127 else "fail", "lsof on PATH (thread liveness needs it)")
    _started, ps_error = sp_process.proc_start_checked(os.getpid())
    add(
        "fail" if ps_error else "ok",
        "process-start probe%s"
        % (": unavailable (%s)" % ps_error if ps_error else ""),
    )
    add("ok", "CLAUDE_CONFIG_DIR: %s" % sp_storage.claude_config_dir())
    add("ok", "CODEX_HOME: %s" % sp_storage.codex_home())
    if sp_storage.codex_sqlite_home() != sp_storage.codex_home():
        add("ok", "codex sqlite_home: %s" % sp_storage.codex_sqlite_home())

    sock_dir = sp_claude.default_socket_dir()
    add(
        "ok" if sp_claude.dir_is_allowlisted(sock_dir) else "warn",
        "socket directory: %s%s"
        % (sock_dir, "" if os.path.isdir(sock_dir) else " (does not exist yet)"),
    )
    if os.path.isdir(sock_dir):
        socket_error = unix_socket_probe(sock_dir)
        add(
            "fail" if socket_error else "ok",
            "Unix-socket bind%s"
            % (": unavailable (%s)" % socket_error if socket_error else ""),
        )
    else:
        add("warn", "Unix-socket bind not tested; socket directory is absent")

    db = sp_codex.find_state_db()
    add(
        "ok" if db else "warn",
        "Codex state database: %s"
        % (db or "none with a recognised `threads` schema (send by UUID only)"),
    )
    threads, _ok = sp_codex.codex_threads()
    unverified_threads = [
        thread for thread in threads if thread.get("live") is None
    ]
    if unverified_threads:
        detail = unverified_threads[0].get("liveness_error") or "lsof failed"
        add("fail", "Codex liveness probe unavailable: %s" % detail)
        add("warn", "bridge GC skipped while Codex liveness is unverified")
    elif db:
        stale = gc_bridge_state(
            days=sp_runtime._float_env("SESSION_PEERS_GC_DAYS", sp_constants.GC_DAYS_DEFAULT),
            dry_run=True,
            verbose=False,
        )
        add(
            "warn" if stale else "ok",
            "bridge GC: %d stale thread%s"
            % (len(stale), "" if len(stale) == 1 else "s"),
        )

    def running_shim(name, tid, pid):
        status = sp_lifecycle.shim_code_status(tid, pid, installed_digest)
        detail = "; code %s" % status
        if status == "stale":
            detail += " (run peers.py restart %s)" % tid
        add("ok" if status == "current" else "warn", "%s: shim running (pid %d)%s" % (name, pid, detail))

    registered = sp_storage.read_registered()
    live_ids = {t["id"] for t in threads if t.get("live") is True}
    unknown_ids = {t["id"] for t in threads if t.get("live") is None}
    for tid in sorted(registered):
        name = registered[tid].get("name") or tid
        pid = sp_lifecycle.shim_pid(tid)
        if pid:
            running_shim(name, tid, pid)
        elif tid in live_ids:
            add("warn", "%s: thread is live but no shim (run `peers.py up`)" % name)
        elif tid in unknown_ids:
            add("warn", "%s: thread liveness unverified" % name)
        else:
            add("ok", "%s: registered, thread not running" % name)
    for thread in threads:
        if thread["id"] not in registered:
            pid = sp_lifecycle.shim_pid(thread["id"])
            if pid:
                running_shim(thread.get("name") or thread["id"], thread["id"], pid)
    if not registered:
        add("warn", "no registered threads (run `peers.py up <name|uuid>`)")
    # A live thread that is neither registered nor shimmed is invisible to the
    # loop above (it walks `registered` only), yet it is exactly the state that
    # silently swallows messages: SessionStart never attached it, or its shim
    # died. Surface each with the command that attaches it. `live is None`
    # (unverified) threads are not reported here, and a registered thread is
    # already covered above, so it warns exactly once, never twice.
    for thread in sorted(
        (
            t
            for t in threads
            if t.get("live") is True
            and t["id"] not in registered
            and not sp_lifecycle.shim_pid(t["id"])
        ),
        key=lambda t: t["id"],
    ):
        add(
            "warn",
            "%s: live Codex thread, not attached (run `peers.py up %s`)"
            % (thread.get("name") or thread["id"], thread["id"]),
        )

    hooks_path = os.path.join(sp_storage.codex_home(), "hooks.json")
    data = sp_runtime.read_json(hooks_path, None)
    if data is None:
        add("ok", "no %s (the SessionStart hook is optional)" % hooks_path)
    else:
        events, _root = _hooks_event_map(data)
        entries = events.get("SessionStart") or []
        cfg = sp_config.read_toml_lite(sp_storage.codex_config_path())
        found = False
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for j, hook in enumerate(entry.get("hooks") or []):
                if (
                    not isinstance(hook, dict)
                    or hook.get("command")
                    not in {hook_command(), hook_command(auto_attach=True)}
                ):
                    continue
                found = True
                mode = (
                    "auto-attach"
                    if hook.get("command") == hook_command(auto_attach=True)
                    else "reconcile-only"
                )
                key = '%s:session_start:%d:%d' % (hooks_path, i, j)
                block = None
                for section, values in cfg.items():
                    if section.startswith("hooks.state.") and key in section:
                        block = values
                        break
                if block is None:
                    add(
                        "warn",
                        "SessionStart %s entry present but no trust block; open "
                        "/hooks in Codex to trust it" % mode,
                    )
                elif block.get("enabled") is False:
                    add("warn", "SessionStart entry is disabled in config.toml")
                else:
                    add(
                        "ok",
                        "SessionStart %s entry present with a trust block "
                        "(entry sha256 %s; Codex's trusted_hash algorithm is "
                        "undocumented, so trust is unknown here, open /hooks)"
                        % (mode, entry_hash(entry)[:12]),
                    )
        if not found:
            add("ok", "no session-peers SessionStart entry (optional)")
        feature_value = cfg.get("features", {}).get("hooks")
        if feature_value is False:
            add("warn", "[features] hooks = false explicitly disables the entry")
        elif feature_value is True:
            add("ok", "[features] hooks = true in config.toml")
        else:
            add("ok", "[features] hooks is unset (enabled by default)")

    print("\n".join(lines))
    return 0


class TopicError(Exception):
    """A refused topic operation; carries the exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def topics_dir():
    path = os.path.join(sp_storage.state_dir(), "topics")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def validate_topic(topic):
    """The topic string unchanged, or TopicError. It is opaque otherwise."""
    if not isinstance(topic, str) or not topic.strip():
        raise TopicError("a topic must be a non-empty string", 2)
    if len(topic) > sp_constants.TOPIC_MAX_CHARS:
        raise TopicError("a topic is at most %d characters" % sp_constants.TOPIC_MAX_CHARS, 2)
    if sp_constants.TOPIC_BAD_CHARS_RE.search(topic):
        raise TopicError("a topic must not contain control characters", 2)
    try:
        topic.encode("utf-8")
    except UnicodeError:
        raise TopicError("a topic must be valid UTF-8", 2)
    return topic


def _topic_paths(topic):
    """(log, meta, lock) paths. The file name is a digest, never the topic,
    so no topic string can name a path outside the topics directory."""
    digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:40]
    base = os.path.join(topics_dir(), digest)
    return base + ".jsonl", base + ".meta.json", base + ".lock"


@contextlib.contextmanager
def _topic_lock(lock_path):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing drops the flock


def _topic_limits():
    ttl = sp_runtime._float_env("SESSION_PEERS_TOPIC_TTL_DAYS", sp_constants.TOPIC_TTL_DAYS_DEFAULT)
    entries = int(sp_runtime._float_env("SESSION_PEERS_TOPIC_MAX_ENTRIES", sp_constants.TOPIC_MAX_ENTRIES_DEFAULT))
    size = int(sp_runtime._float_env("SESSION_PEERS_TOPIC_MAX_BYTES", sp_constants.TOPIC_MAX_BYTES_DEFAULT))
    return (
        ttl if ttl > 0 else sp_constants.TOPIC_TTL_DAYS_DEFAULT,
        entries if entries > 0 else sp_constants.TOPIC_MAX_ENTRIES_DEFAULT,
        size if size > 0 else sp_constants.TOPIC_MAX_BYTES_DEFAULT,
    )


def _read_topic_lines(log_path):
    """Parsed entries in file order; a torn or foreign line is skipped."""
    out = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                    out.append((entry, len(line.encode("utf-8"))))
    except FileNotFoundError:
        pass
    return out


def _topic_state(topic, log_path, meta_path, now):
    """Retained entries after pruning, and the next seq. Call under the lock.

    The next seq lives in the meta file, so it survives pruning every entry;
    the log's own last seq covers a meta write lost to a crash.
    """
    ttl_days, max_entries, max_bytes = _topic_limits()
    lines = _read_topic_lines(log_path)
    meta = sp_runtime.read_json(meta_path, {}) or {}
    next_seq = meta.get("next_seq") if isinstance(meta.get("next_seq"), int) else 1
    if lines:
        next_seq = max(next_seq, lines[-1][0]["seq"] + 1)
    cutoff = now - ttl_days * 86400.0
    kept = [
        (entry, size) for entry, size in lines
        if (sp_runtime.parse_time(entry.get("ts")) or 0.0) >= cutoff
    ]
    kept = kept[-max_entries:]
    total = sum(size for _entry, size in kept)
    while kept and total > max_bytes:
        total -= kept.pop(0)[1]
    if len(kept) != len(lines):
        _write_topic_log(log_path, [entry for entry, _size in kept])
    return [entry for entry, _size in kept], next_seq


def _write_topic_log(log_path, entries):
    tmp = "%s.tmp.%d" % (log_path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, log_path)


def _write_topic_meta(meta_path, topic, next_seq, last_ts):
    sp_runtime.write_json_atomic(
        meta_path,
        {"topic": topic, "next_seq": next_seq, "last_ts": last_ts},
        mode=0o600,
    )


def topic_post(topic, from_identity, kind=None, text=None, data=sp_constants._NO_DATA):
    """Append one entry and return it. Seq is unique and monotonic per topic."""
    validate_topic(topic)
    if kind is not None and not sp_constants.TOPIC_KIND_RE.match(kind):
        raise TopicError(
            "--kind must be 1-64 characters of letters, digits, '.', '_', ':' "
            "or '-', starting with a letter or digit", 2
        )
    log_path, meta_path, lock_path = _topic_paths(topic)
    with _topic_lock(lock_path):
        now = time.time()
        _entries, next_seq = _topic_state(topic, log_path, meta_path, now)
        entry = {
            "seq": next_seq,
            "ts": sp_runtime.now_iso(),
            "topic": topic,
            "from_kind": from_identity.get("kind"),
            "from_name": from_identity.get("name"),
            "from_sid": from_identity.get("uuid"),
            "kind": kind,
        }
        if data is not sp_constants._NO_DATA:
            entry["data"] = data
        else:
            entry["text"] = text
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(log_path, 0o600)
        _write_topic_meta(meta_path, topic, next_seq + 1, entry["ts"])
    return entry


def topic_tail(topic, since=None, limit=sp_constants.TOPIC_TAIL_DEFAULT):
    """{"entries", "next", "gap"}: entries with seq > since in order, or the
    last ``limit`` when since is None. ``gap`` names pruned seqs the cursor
    skipped, so a slow reader learns it lost entries instead of guessing."""
    validate_topic(topic)
    log_path, meta_path, lock_path = _topic_paths(topic)
    if not os.path.exists(meta_path) and not os.path.exists(log_path):
        return {"topic": topic, "entries": [], "next": since or 0, "gap": None}
    with _topic_lock(lock_path):
        entries, next_seq = _topic_state(topic, log_path, meta_path, time.time())
    last_seq = next_seq - 1
    gap = None
    if since is None:
        chosen = entries[-limit:]
    else:
        oldest = entries[0]["seq"] if entries else next_seq
        if since + 1 < oldest and since < last_seq:
            gap = {"from": since + 1, "to": oldest - 1}
        chosen = [e for e in entries if e["seq"] > since][:limit]
    if chosen:
        cursor = chosen[-1]["seq"]
    elif since is None:
        cursor = last_seq
    else:
        # Nothing newer: stay put, or move past a gap that ends the log.
        cursor = max(since, gap["to"]) if gap else since
    return {"topic": topic, "entries": chosen, "next": cursor, "gap": gap}


def topic_list():
    out = []
    try:
        names = sorted(os.listdir(topics_dir()))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".meta.json"):
            continue
        meta = sp_runtime.read_json(os.path.join(topics_dir(), name), None)
        if not isinstance(meta, dict) or not isinstance(meta.get("topic"), str):
            continue
        next_seq = meta.get("next_seq")
        out.append({
            "topic": meta["topic"],
            "last_seq": next_seq - 1 if isinstance(next_seq, int) else None,
            "last_ts": meta.get("last_ts"),
        })
    out.sort(key=lambda item: item["topic"])
    return out


def topic_prune_all():
    """Apply retention to every topic, including ones nobody posts to or reads."""
    for item in topic_list():
        log_path, meta_path, lock_path = _topic_paths(item["topic"])
        with _topic_lock(lock_path):
            _topic_state(item["topic"], log_path, meta_path, time.time())


def topic_sender(args):
    """This session's identity for a post, or None (anonymous).

    The same sources `send` uses: the Claude messaging socket or session id,
    and the Codex thread id. Either agent can inherit the other's variables
    when started from its shell, so with both present the nearer ancestor
    process owns the post; unverifiable means refused. ``--as`` overrides.
    """
    explicit = getattr(args, "as_identity", None)
    if explicit:
        ident = parse_typed(explicit)
        return _topic_named(ident)
    claude = None
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    rec = sp_claude.claude_record_by_socket(sock) if sock else None
    if rec and sp_runtime.is_uuid(rec.get("sessionId")):
        claude = {"kind": "cc", "uuid": rec["sessionId"], "name": rec.get("name"),
                  "pid": rec.get("pid")}
    else:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if sp_runtime.is_uuid(sid):
            matches = sp_claude.claude_record_by_target(sid)
            only = matches[0] if len(matches) == 1 else {}
            claude = {"kind": "cc", "uuid": sid, "name": only.get("name"),
                      "pid": only.get("pid")}
    tid = _thread_from_args(args)
    codex = _topic_named({"kind": "codex", "uuid": tid}) if tid else None
    if claude and codex:
        owner = sp_process._nearest_owner(claude.get("pid"), sp_codex._codex_holder_pid(tid))
        if owner is None:
            raise TopicError(
                "this environment names both Claude session %s and Codex thread "
                "%s, and which one runs this command cannot be verified; pass "
                "--as cc:%s or --as codex:%s" % (claude["uuid"], tid, claude["uuid"], tid),
                2,
            )
        chosen = claude if owner == "cc" else codex
    else:
        chosen = claude or codex
    if chosen is None:
        return None
    chosen.pop("pid", None)
    return chosen


def _topic_named(ident):
    if ident["kind"] == "cc":
        matches = sp_claude.claude_record_by_target(ident["uuid"])
        ident["name"] = matches[0].get("name") if len(matches) == 1 else None
    else:
        entry = sp_storage.read_registered().get(ident["uuid"])
        ident["name"] = entry.get("name") if isinstance(entry, dict) else None
    return ident


def _topic_printable(text):
    """Escape control characters except newline and tab for a terminal."""
    return re.sub(
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f]",
        lambda m: "\\x%02x" % ord(m.group(0)),
        text,
    )


def cmd_topic(args):
    try:
        if args.topic_cmd == "post":
            return _cmd_topic_post(args)
        if args.topic_cmd == "tail":
            return _cmd_topic_tail(args)
        if args.topic_cmd == "list":
            return _cmd_topic_list(args)
    except TopicError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return exc.code
    sys.stderr.write("error: topic subcommands are `post`, `tail` and `list`\n")
    return 2


def _cmd_topic_post(args):
    validate_topic(args.topic)
    data, text = sp_constants._NO_DATA, None
    try:
        if args.json_file:
            raw = sp_runtime.message_from_args(
                argparse.Namespace(message=None, message_file=args.json_file)
            )
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise TopicError("--json-file is not valid JSON: %s" % exc)
        else:
            text = sp_runtime.message_from_args(args)
    except UnicodeError:
        raise TopicError("the message is not valid UTF-8")
    except ValueError as exc:
        raise TopicError(str(exc))
    try:
        sender = topic_sender(args)
    except ValueError as exc:
        raise TopicError(str(exc), 2)
    if sender is None:
        sp_runtime.log(
            "sender identity absent: posting anonymously; from Claude Code use "
            "its Bash tool, from Codex its shell, or pass --as"
        )
        sender = {}
    entry = topic_post(args.topic, sender, kind=args.kind, text=text, data=data)
    if args.json:
        print(json.dumps({"topic": entry["topic"], "seq": entry["seq"], "ts": entry["ts"]}))
    else:
        print("posted %s #%d" % (args.topic, entry["seq"]))
    return 0


def _cmd_topic_tail(args):
    if args.since is not None and args.since < 0:
        raise TopicError("--since must be zero or greater", 2)
    if not 1 <= args.limit <= sp_constants.TOPIC_TAIL_MAX:
        raise TopicError("--limit must be between 1 and %d" % sp_constants.TOPIC_TAIL_MAX, 2)
    result = topic_tail(args.topic, since=args.since, limit=args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    gap = result["gap"]
    if gap:
        print("gap: entries %d..%d pruned" % (gap["from"], gap["to"]))
    for entry in result["entries"]:
        who = entry.get("from_name") or entry.get("from_sid") or "anonymous"
        kind = " [%s]" % entry["kind"] if entry.get("kind") else ""
        if "data" in entry:
            body = json.dumps(entry["data"], ensure_ascii=False)
        else:
            body = entry.get("text") or ""
        print("#%d %s %s%s: %s" % (
            entry["seq"], entry.get("ts"), _topic_printable(str(who)), kind,
            _topic_printable(body),
        ))
    print("next: --since %d" % result["next"])
    return 0


def _cmd_topic_list(args):
    topics = topic_list()
    if args.json:
        print(json.dumps(topics, ensure_ascii=False))
        return 0
    if not topics:
        print("no topics")
    for item in topics:
        print("%s  last #%s  %s" % (
            _topic_printable(item["topic"]), item["last_seq"], item["last_ts"] or "-",
        ))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="peers.py",
        description="Cross-session messaging between Claude Code and Codex CLI.",
    )
    sub = p.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="live Claude sessions and Codex threads")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=cmd_list)

    def add_message_source(parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--message", help="the message body")
        group.add_argument(
            "--message-file", metavar="PATH", help="read the message body from PATH"
        )

    p_send = sub.add_parser("send", help="send one message in either direction")
    p_send.add_argument(
        "--to",
        required=True,
        metavar="codex:<name|uuid>|cc:<name|uuid>|buddy",
        help="the peer",
    )
    add_message_source(p_send)
    p_send.add_argument(
        "--from-thread", metavar="UUID", help="the Codex thread sending (cc: targets)"
    )
    p_send.add_argument("--from-name", metavar="NAME", help="sender name for the tag")
    p_send.add_argument("--from-sid", metavar="ID", help="sender session id for the tag")
    p_send.add_argument(
        "--from-socket", metavar="PATH", help="sender socket for the reply tag"
    )
    p_send.add_argument("--json", action="store_true", help="machine-readable result")
    p_send.set_defaults(func=cmd_send)

    p_ask = sub.add_parser(
        "ask", help="send a correlated request to Claude and wait for its reply"
    )
    p_ask.add_argument(
        "--to", required=True, metavar="cc:<name|uuid>|buddy", help="the Claude peer"
    )
    add_message_source(p_ask)
    p_ask.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread asking (defaults to CODEX_THREAD_ID)",
    )
    p_ask.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait (default: 600; maximum: 3600)",
    )
    p_ask.add_argument("--json", action="store_true", help="machine-readable result")
    p_ask.set_defaults(func=cmd_ask)

    p_reply = sub.add_parser(
        "reply", help="reply exactly once to a pending correlated request"
    )
    p_reply.add_argument("--request", required=True, metavar="UUID")
    add_message_source(p_reply)
    p_reply.add_argument("--json", action="store_true", help="machine-readable result")
    p_reply.set_defaults(func=cmd_reply)

    p_dispatch = sub.add_parser(
        "dispatch",
        help="send a correlated request to Claude and return immediately",
    )
    p_dispatch.add_argument(
        "--to", required=True, metavar="cc:<name|uuid>|buddy", help="the Claude peer"
    )
    add_message_source(p_dispatch)
    p_dispatch.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread dispatching (defaults to CODEX_THREAD_ID)",
    )
    p_dispatch.add_argument(
        "--timeout",
        type=float,
        help="request lifetime in seconds (default: 600; maximum: 3600)",
    )
    p_dispatch.add_argument(
        "--json", action="store_true", help="machine-readable result"
    )
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_await = sub.add_parser(
        "await", help="consume the reply to a dispatched request by its id"
    )
    p_await.add_argument("--request", required=True, metavar="UUID")
    p_await.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread that dispatched it (defaults to CODEX_THREAD_ID)",
    )
    p_await.add_argument(
        "--timeout",
        type=float,
        help="seconds this call waits, distinct from the request lifetime "
        "(default: 600; maximum: 3600)",
    )
    p_await.add_argument("--json", action="store_true", help="machine-readable result")
    p_await.set_defaults(func=cmd_await)

    p_wait = sub.add_parser("wait", help="wait for a Claude peer registry state")
    p_wait.add_argument(
        "--for", dest="for_peer", required=True, metavar="cc:<name|uuid>|buddy"
    )
    p_wait.add_argument(
        "--state", choices=("idle", "busy"), default="idle", help="target state"
    )
    p_wait.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait (default: 600; maximum: 3600)",
    )
    p_wait.add_argument("--json", action="store_true", help="machine-readable result")
    p_wait.set_defaults(func=cmd_wait_peer)

    p_shim = sub.add_parser("shim", help="run as one Codex thread's peer (foreground)")
    p_shim.add_argument("--thread", required=True, metavar="UUID")
    p_shim.set_defaults(func=cmd_shim)

    p_up = sub.add_parser("up", help="register a thread and start its shim")
    p_up.add_argument("target", nargs="?", metavar="name|uuid")
    p_up.set_defaults(func=sp_lifecycle.cmd_up)

    p_down = sub.add_parser("down", help="stop a shim; with a target, unregister it")
    p_down.add_argument("target", nargs="?", metavar="name|uuid")
    p_down.set_defaults(func=sp_lifecycle.cmd_down)

    p_restart = sub.add_parser(
        "restart",
        help="restart a shim on the current code, keeping its reply budget and grants",
    )
    p_restart.add_argument("target", metavar="name|uuid")
    p_restart.set_defaults(func=sp_lifecycle.cmd_restart)

    p_budget = sub.add_parser("budget", help="reply budget maintenance")
    bsub = p_budget.add_subparsers(dest="budget_cmd")
    p_reset = bsub.add_parser("reset", help="clear a thread's reply budget")
    p_reset.add_argument("thread", metavar="name|uuid|buddy")
    p_reset.set_defaults(func=cmd_budget)
    p_allow = bsub.add_parser(
        "allow", help="let one requester receive up to N consecutive replies"
    )
    p_allow.add_argument("thread", metavar="name|uuid|buddy")
    p_allow.add_argument(
        "--replies",
        type=int,
        required=True,
        metavar="N",
        help="total consecutive replies for this sequence (1..%d)" % sp_constants.BUDGET_ALLOW_MAX,
    )
    p_allow.add_argument(
        "--for-session",
        metavar="UUID",
        help="the requesting Claude session (default: the calling session)",
    )
    p_allow.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the calling session, when it cannot be detected",
    )
    p_allow.set_defaults(func=cmd_budget)
    p_budget.set_defaults(func=cmd_budget, budget_cmd=None, thread=None)

    p_buddy = sub.add_parser("buddy", help="bind, show, ping or clear this session's buddy")
    p_buddy.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the calling session, when it cannot be detected",
    )
    p_buddy.add_argument("--json", action="store_true", help="machine-readable output")
    buddy_sub = p_buddy.add_subparsers(dest="buddy_cmd")

    def buddy_action(name, help_text, **kwargs):
        parser = buddy_sub.add_parser(name, help=help_text, **kwargs)
        # SUPPRESS: an option given before the action must survive the subparser.
        parser.add_argument(
            "--as", dest="as_identity", default=argparse.SUPPRESS,
            metavar="cc:<uuid>|codex:<uuid>",
        )
        parser.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="machine-readable output",
        )
        return parser

    buddy_action("show", "report the buddy's status without attaching")
    p_buddy_set = buddy_action(
        "set",
        "bind a buddy by name or UUID",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "A bare name or UUID that is both a Claude session and a Codex thread\n"
            "(an attached Codex thread's UUID also names its shim) is refused;\n"
            "prefix the kind. Name matching never matches the caller itself, and\n"
            "without a target the caller's own name is looked up:\n"
            "  peers.py buddy set codex:<uuid>\n"
            "  peers.py buddy set cc:<uuid>\n"
            "  peers.py buddy set codex:my-thread --uses review,brainstorm\n"
            "  peers.py buddy set"
        ),
    )
    p_buddy_set.add_argument(
        "target",
        nargs="?",
        metavar="[cc:|codex:|@]name|uuid",
        help="the peer to bind (default: the other peer sharing this session's name)",
    )
    p_buddy_set.add_argument(
        "--uses",
        metavar="a,b",
        help="advisory scope (default: all of %s)" % ", ".join(sp_constants.BUDDY_USES),
    )
    p_buddy_set.add_argument(
        "--replies",
        type=int,
        metavar="N",
        help="a finite TOTAL of replies (1..%d, default %d for a Claude "
        "session's Codex buddy) this buddy may send beyond the per-sequence "
        "cap, across sequences and shim restarts; revoked by `buddy clear` or "
        "binding another buddy" % (sp_constants.BUDDY_REPLIES_MAX, sp_constants.BUDDY_REPLIES_DEFAULT),
    )
    buddy_action("ping", "report status, attaching a Codex buddy's shim if needed")
    buddy_action("clear", "unbind the buddy")
    p_buddy.set_defaults(func=cmd_buddy, buddy_cmd=None)

    p_hook = sub.add_parser("session-hook", help="Codex SessionStart entry point")
    p_hook.add_argument(
        "--auto-attach", action="store_true", help="attach the triggering session UUID"
    )
    p_hook.set_defaults(func=cmd_session_hook)

    p_hook_reconcile = sub.add_parser("hook-reconcile", help=argparse.SUPPRESS)
    p_hook_reconcile.add_argument("--thread", metavar="UUID")
    p_hook_reconcile.set_defaults(func=cmd_hook_reconcile)

    p_install = sub.add_parser("install-hook", help="add the SessionStart entry")
    p_install.add_argument(
        "--auto-attach",
        action="store_true",
        help="automatically expose each starting or resumed Codex thread",
    )
    p_install.set_defaults(func=cmd_install_hook)

    p_topic = sub.add_parser("topic", help="pull-only topic logs any peer can post to")
    tsub = p_topic.add_subparsers(dest="topic_cmd")
    p_tpost = tsub.add_parser("post", help="append one entry to a topic")
    p_tpost.add_argument("topic", metavar="TOPIC")
    tsource = p_tpost.add_mutually_exclusive_group(required=True)
    tsource.add_argument("--message", help="the entry text")
    tsource.add_argument("--message-file", metavar="PATH", help="read the entry text from PATH")
    tsource.add_argument("--json-file", metavar="PATH", help="a JSON value stored as data")
    p_tpost.add_argument("--kind", metavar="KIND", help="an opaque label for readers")
    p_tpost.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the posting session, when it cannot be detected",
    )
    p_tpost.add_argument("--json", action="store_true", help="machine-readable result")
    p_tpost.set_defaults(func=cmd_topic)
    p_ttail = tsub.add_parser("tail", help="print entries after a cursor")
    p_ttail.add_argument("topic", metavar="TOPIC")
    p_ttail.add_argument(
        "--since", type=int, metavar="SEQ",
        help="print entries after SEQ (default: the last --limit entries)",
    )
    p_ttail.add_argument(
        "--limit", type=int, default=sp_constants.TOPIC_TAIL_DEFAULT, metavar="N",
        help="at most N entries (default %d, maximum %d)"
        % (sp_constants.TOPIC_TAIL_DEFAULT, sp_constants.TOPIC_TAIL_MAX),
    )
    p_ttail.add_argument("--json", action="store_true", help="machine-readable output")
    p_ttail.set_defaults(func=cmd_topic)
    p_tlist = tsub.add_parser("list", help="topics with their last seq and time")
    p_tlist.add_argument("--json", action="store_true", help="machine-readable output")
    p_tlist.set_defaults(func=cmd_topic)
    p_topic.set_defaults(func=cmd_topic, topic_cmd=None)

    p_gc = sub.add_parser("gc", help="prune inactive bridge metadata")
    p_gc.add_argument("--days", type=float, help="retention in days (default: 7)")
    p_gc.add_argument("--dry-run", action="store_true", help="print without deleting")
    p_gc.set_defaults(func=cmd_gc)

    p_doctor = sub.add_parser("doctor", help="check the bridge end to end")
    p_doctor.set_defaults(func=cmd_doctor)

    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
