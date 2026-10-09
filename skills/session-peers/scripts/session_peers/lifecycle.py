"""Lifecycle for the session-peers CLI."""

from __future__ import annotations
import contextlib
import fcntl
import os
import re
import signal
import sys
import time
from . import protocol as sp_protocol, runtime as sp_runtime
from . import codex as sp_codex, diagnostics as sp_diagnostics, process as sp_process, storage as sp_storage

def shim_code_status(thread_id, pid, installed_digest):
    """Unknown old/unreadable state is not evidence of stale running code."""
    state = sp_runtime.read_json(sp_storage.thread_state_path(thread_id), {})
    if not isinstance(state, dict) or state.get("shim_pid") != pid:
        return "unknown"
    running = state.get("code_digest")
    if not installed_digest or not isinstance(running, str) or not re.fullmatch(r"[0-9a-f]{64}", running):
        return "unknown"
    return "current" if running == installed_digest else "stale"


def shim_ready(thread_id):
    """The pid of a shim that owns the thread AND has written its record.

    The ownership lock is taken before the socket is bound, so `shim_pid`
    alone answers "starting", not "serving". `up` waits for this.
    """
    pid = shim_pid(thread_id)
    if pid is None:
        return None
    rec = sp_runtime.read_json(os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % pid), None)
    if not isinstance(rec, dict) or rec.get("sessionId") != thread_id:
        return None
    sock = rec.get("messagingSocketPath")
    return pid if sock and os.path.exists(sock) else None


def shim_pid(thread_id):
    """The pid of the shim that PROVABLY owns this thread, or None.

    Ownership is the flock the shim holds on its own pidfile for its whole
    life, not the pid written in it: the kernel drops that lock when the
    process dies, however it died, so a recycled pid can neither be signalled
    by mistake nor block a restart (B2). The registry record is a second,
    weaker proof and is only used to catch a pidfile naming another thread.
    """
    path = sp_storage.thread_pid_path(thread_id)
    try:
        fh = open(path, "r+")
    except (FileNotFoundError, OSError):
        return None
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            pass  # somebody holds it: a shim is running
        else:
            # Nobody holds it, so no shim is running for this thread. The file
            # is deliberately LEFT in place: unlinking it here would race a
            # shim between its open() and its flock(), and the next shim
            # truncates and rewrites it anyway.
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return None
        try:
            fh.seek(0)
            pid = int(fh.read().strip())
        except (OSError, ValueError):
            return None
    finally:
        try:
            fh.close()
        except OSError:
            pass
    if not sp_process.pid_alive(pid):
        return None
    rec = sp_runtime.read_json(os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % pid), None)
    if (
        isinstance(rec, dict)
        and rec.get("entrypoint") == "codex"
        and rec.get("sessionId") != thread_id
    ):
        sp_runtime.log("the pidfile for %s names another thread's shim; ignoring" % thread_id)
        return None
    return pid


def spawn_shim(thread):
    """Daemonise one shim: new session, no inherited fds, log to a file.

    Codex's hook runner waits for inherited stdout/stderr pipes, so a child
    started from `session-hook` MUST detach exactly like this (PRD Facts).
    """
    script = sp_runtime.entrypoint_path()
    log_path = sp_storage.thread_log_path(thread["id"])
    try:
        daemon_pid = sp_runtime.spawn_detached(
            [sys.executable, script, "shim", "--thread", thread["id"]],
            log_path,
        )
    except OSError as exc:
        sp_runtime.log("could not start the shim for %s: %s" % (thread["id"], exc))
        return None
    # The SHIM writes and locks the pidfile once it is serving, so the file is
    # never a claim without a holder. Wait for it so a following reconcile
    # inside the same lock sees the shim rather than starting a second one.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        pid = shim_ready(thread["id"])
        if pid:
            return pid
        if not sp_process.pid_alive(daemon_pid):
            sp_runtime.log("the shim for %s exited; see %s" % (thread["id"], log_path))
            return None
        time.sleep(0.05)
    sp_runtime.log("the shim for %s did not report ready in 10s; see %s" % (thread["id"], log_path))
    return None


def reconcile(verbose=True):
    """Start one shim per registered, live thread. Idempotent by design."""
    with sp_storage.reconcile_lock():
        return _reconcile(verbose)


def _in_thread_home(thread):
    """Run a block under the CODEX_HOME the thread lives in, when not the caller's."""
    home = thread.get("codex_home")
    return sp_storage.codex_home_override(home) if home else contextlib.nullcontext()


def attach_thread(thread_id, verbose=True):
    """Start a shim for one live UUID without making it a persistent opt-in."""
    return attach_thread_with_reason(thread_id, verbose)[0]


def attach_thread_with_reason(thread_id, verbose=True):
    """(pid or None, why): ``attach_thread`` plus the reason it did not attach.

    A thread found under another CODEX_HOME gets its shim spawned under that
    home, so the shim starts where the thread's lock, rollout and state DB are.
    """
    try:
        thread = sp_codex.resolve_thread(thread_id)
    except sp_codex.ResolveError as exc:
        if verbose:
            print("  %s: not attachable (%s)" % (thread_id, exc))
        return None, "not attachable: %s" % exc
    if thread.get("live") is not True:
        state = "unverified" if thread.get("live") is None else "not live"
        if verbose:
            print("  %s: %s, skipped" % (thread_id, state))
        return None, "%s, skipped" % state
    with _in_thread_home(thread), sp_storage.reconcile_lock():
        pid = shim_pid(thread_id)
        if pid:
            if verbose:
                print("  %s: shim already running (pid %s)" % (thread_id, pid))
            return pid, None
        pid = spawn_shim(thread)
    if verbose:
        if pid is None:
            print("  %s: shim failed to start (see its log)" % thread_id)
        else:
            print("  %s: shim started (pid %d)" % (thread_id, pid))
    return pid, (None if pid else "shim failed to start (see its log)")


def _reconcile(verbose):
    registered = sp_storage.read_registered()
    if not registered:
        if verbose:
            print("no registered threads; run `peers.py up <name|uuid>` first")
        return 0
    threads, schema_ok = sp_codex.codex_threads()
    if not schema_ok:
        if verbose:
            print("thread discovery is unavailable; no shim can be started")
        return 0
    by_id = {t["id"]: t for t in threads}
    started = 0
    for tid in sorted(registered):
        thread = by_id.get(tid)
        if thread is not None and thread.get("live") is None:
            if verbose:
                print("  %s: liveness unverified, skipped" % tid)
            continue
        if thread is None or thread.get("live") is not True:
            if verbose:
                print("  %s: not live, skipped" % tid)
            continue
        if shim_pid(tid):
            if verbose:
                print("  %s: shim already running (pid %s)" % (tid, shim_pid(tid)))
            continue
        pid = spawn_shim(thread)
        if pid is None:
            if verbose:
                print("  %s: shim failed to start (see its log)" % tid)
            continue
        started += 1
        if verbose:
            print("  %s: shim started (pid %d)" % (tid, pid))
    return started


def cmd_up(args):
    sp_diagnostics.warn_versions()
    if args.target:
        try:
            thread = sp_codex.resolve_thread(args.target)
        except sp_codex.ResolveError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        # A thread found under another CODEX_HOME is registered, budget-reset
        # and reconciled there, so its shim starts in the thread's own home.
        with _in_thread_home(thread):
            try:
                sp_storage.register_thread(thread)
            except sp_protocol.NameError_ as exc:
                sys.stderr.write("error: %s\n" % exc)
                return 1
            # D4: an explicit `up` is one of the two things that clears the budget.
            try:
                with open(sp_storage.budget_reset_path(thread["id"]), "w", encoding="utf-8") as fh:
                    fh.write(sp_runtime.now_iso() + "\n")
            except OSError:
                pass
            print("registered %s (%s)" % (thread.get("name") or thread["id"], thread["id"]))
            reconcile()
        return 0
    reconcile()
    return 0


def cmd_restart(args):
    """Restart one thread's shim on the current code, keeping its state.

    Unlike `down <uuid>` then `up <uuid>`, this neither unregisters the thread
    nor writes the budget-reset marker, so the shim comes back with the reply
    sequence it had: spent replies and any still-valid grant included.
    """
    try:
        thread = sp_codex.resolve_thread_prefer_live(args.target)
    except sp_codex.ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    tid = thread["id"]
    with sp_storage.reconcile_lock():
        stopped = stop_shim(tid)
    pid = attach_thread(tid, verbose=False)
    if pid is None:
        sys.stderr.write(
            "error: the shim for %s did not start (see its log)%s\n"
            % (tid, "; the old shim was stopped" if stopped else "")
        )
        return 1
    print(
        "%s the shim for %s (pid %d); reply budget, spent replies and valid "
        "grants kept" % ("restarted" if stopped else "started", tid, pid)
    )
    return 0


def cmd_down(args):
    if args.target:
        try:
            thread = sp_codex.resolve_thread_prefer_live(args.target)
            tid = thread["id"]
        except sp_codex.ResolveError as exc:
            thread = {}
            tid = args.target if sp_runtime.is_uuid(args.target) else None
            if tid is None:
                sys.stderr.write("error: %s\n" % exc)
                return 1
        # R2: stop and unregister under ONE hold of the reconcile lock. A bare
        # `up` landing between them would restart the still-registered thread,
        # leaving an unregistered peer running while `down` reported success.
        with _in_thread_home(thread), sp_storage.reconcile_lock():
            stopped = stop_shim(tid)
            removed = sp_storage._unregister_thread_unlocked(tid)
        if removed:
            print("unregistered %s%s" % (tid, " and stopped its shim" if stopped else ""))
        else:
            print("%s was not registered%s" % (tid, "; shim stopped" if stopped else ""))
        return 0
    with sp_storage.reconcile_lock():
        stopped = [tid for tid in sorted(sp_storage.read_registered()) if stop_shim(tid)]
    for tid in stopped:
        print("stopped the shim for %s" % tid)
    print("registrations kept; `peers.py up` brings them back")
    return 0


def stop_shim(thread_id) -> bool:
    """SIGTERM the shim for a thread. Never signals an unproven pid (B2)."""
    pid = shim_pid(thread_id)
    if pid is None:
        # shim_pid already dropped a pidfile nothing holds, so there is no
        # process this bridge can prove it owns. Fail closed.
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(30):
        if not sp_process.pid_alive(pid):
            break
        time.sleep(0.1)
    if sp_process.pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.unlink(sp_storage.thread_pid_path(thread_id))
    except (FileNotFoundError, OSError):
        pass
    return True


def shim_supports(thread_id, pid, feature):
    """True only when the state file proves shim ``pid`` has ``feature``.

    A shim saves its state, pid included, before it serves. Missing state, or
    state naming another pid, proves nothing about the running code, so it
    counts as unsupported rather than risking a grant nothing reads.
    """
    state = sp_runtime.read_json(sp_storage.thread_state_path(thread_id), None)
    if not isinstance(state, dict) or state.get("shim_pid") != pid:
        return False
    return feature in (state.get("shim_features") or [])
