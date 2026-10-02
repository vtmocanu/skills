"""Storage for the session-peers CLI."""

from __future__ import annotations
import contextlib
import errno
import fcntl
import os
import time
from . import config as sp_config, constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime

# --------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def claude_sessions_dir():
    return os.path.join(claude_config_dir(), "sessions")


def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def codex_config_path():
    return os.path.join(codex_home(), "config.toml")


def codex_sqlite_home():
    """config.toml `sqlite_home`, else CODEX_SQLITE_HOME, else CODEX_HOME.

    P6: the configured value wins, matching Codex's own resolver, and only a
    TOP-LEVEL key counts. A `sqlite_home` inside another table belongs to that
    table, and taking it would point the bridge at the wrong database.
    """
    cfg = sp_config.read_toml_lite(codex_config_path())
    value = cfg.get("", {}).get("sqlite_home")
    if isinstance(value, str) and value:
        return os.path.expanduser(value)
    env = os.environ.get("CODEX_SQLITE_HOME")
    if env:
        return os.path.expanduser(env)
    return codex_home()


def state_dir():
    """Where registration, per-thread state, pidfiles and logs live."""
    path = os.path.join(codex_home(), "session-peers")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def registered_path():
    return os.path.join(state_dir(), "registered.json")


def thread_state_path(thread_id):
    return os.path.join(state_dir(), "%s.json" % thread_id)


def thread_pid_path(thread_id):
    return os.path.join(state_dir(), "%s.pid" % thread_id)


def thread_log_path(thread_id):
    return os.path.join(state_dir(), "%s.log" % thread_id)


def budget_reset_path(thread_id):
    return os.path.join(state_dir(), "%s.budget-reset" % thread_id)


def budget_allow_path(thread_id):
    return os.path.join(state_dir(), "%s.budget-allow" % thread_id)


def budget_binding_path(thread_id):
    return os.path.join(state_dir(), "%s.budget-binding" % thread_id)


class BindingLockTimeout(Exception):
    """The per-thread binding lock stayed busy for the whole bounded wait."""


@contextlib.contextmanager
def _flock_file(path, timeout, what):
    """Exclusive flock on a mode-0600 file, polled without blocking.

    Waits at most ``timeout`` seconds (default BINDING_LOCK_TIMEOUT), then
    raises BindingLockTimeout naming ``what``.
    """
    timeout = sp_constants.BINDING_LOCK_TIMEOUT if timeout is None else timeout
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise BindingLockTimeout(
                        "another peers.py command holds the reply-total lock "
                        "for %s" % what
                    )
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def binding_lock(thread_id, timeout=None):
    """Exclusive per-thread lock over the binding marker and its conflict check.

    Held by `buddy set --replies`, `buddy clear`, a rebind's revoke and the
    shim's marker consumption, so a write is never lost to a concurrent read
    or unlink.
    """
    return _flock_file(
        os.path.join(state_dir(), "%s.binding-lock" % thread_id), timeout, thread_id
    )


def owner_lock(owner_uuid, timeout=None):
    """Exclusive per-owner-session lock over its whole buddy record operation.

    `buddy set` and `buddy clear` read the owner's record and act on it under
    this lock. Lock order everywhere: the owner lock first, then thread locks
    in sorted order; the shim only ever takes a thread lock.
    """
    return _flock_file(
        os.path.join(state_dir(), "%s.owner-lock" % owner_uuid), timeout,
        "session " + owner_uuid,
    )


@contextlib.contextmanager
def _binding_locks(*thread_ids):
    """Take several binding locks in sorted order, so two commands cannot deadlock."""
    with contextlib.ExitStack() as stack:
        for tid in sorted(set(thread_ids)):
            if tid:
                stack.enter_context(binding_lock(tid))
        yield


def buddies_dir():
    path = os.path.join(state_dir(), "buddies")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def buddy_path(owner):
    if owner.get("kind") not in sp_constants.BUDDY_KINDS or not sp_runtime.is_uuid(owner.get("uuid")):
        raise ValueError("a buddy owner must be cc:<uuid> or codex:<uuid>")
    return os.path.join(buddies_dir(), "%s-%s.json" % (owner["kind"], owner["uuid"]))


def version_warning_path():
    return os.path.join(state_dir(), "version-warnings.json")


def request_dir():
    path = os.path.join(state_dir(), "requests")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def request_path(request_id):
    if not sp_runtime.is_uuid(request_id):
        raise ValueError("request id must be a UUID")
    return os.path.join(request_dir(), "%s.request.json" % request_id)


def request_reply_path(request_id):
    if not sp_runtime.is_uuid(request_id):
        raise ValueError("request id must be a UUID")
    return os.path.join(request_dir(), "%s.reply.json" % request_id)


# --------------------------------------------------------------------------
# Registration (D6)
# --------------------------------------------------------------------------


def read_registered():
    data = sp_runtime.read_json(registered_path(), {}) or {}
    threads = data.get("threads")
    return threads if isinstance(threads, dict) else {}


def write_registered(threads):
    sp_runtime.write_json_atomic(registered_path(), {"threads": threads}, mode=0o600)


def register_thread(thread):
    """Record a thread as opted in. A name that cannot be a peer name is
    refused here rather than at delivery time, so the failure names the fix."""
    name = thread.get("name")
    if name is not None:
        sp_protocol.require_peer_name(name)
    # P9: a read-modify-write on one shared file, so it runs under the lock the
    # reconcile uses. Two `up` calls at once would otherwise lose one.
    with reconcile_lock():
        threads = read_registered()
        threads[thread["id"]] = {"name": name, "registered_at": sp_runtime.now_iso()}
        write_registered(threads)


def refresh_registered_name(thread_id, name):
    """Refresh the cached alias for a persistently registered UUID."""
    sp_protocol.require_peer_name(name, "peer alias")
    with reconcile_lock(blocking=False) as acquired:
        if not acquired:
            return False
        threads = read_registered()
        meta = threads.get(thread_id)
        if not isinstance(meta, dict) or meta.get("name") == name:
            return False
        meta = dict(meta)
        meta["name"] = name
        threads[thread_id] = meta
        write_registered(threads)
    return True


def _unregister_thread_unlocked(thread_id) -> bool:
    threads = read_registered()
    if thread_id in threads:
        del threads[thread_id]
        write_registered(threads)
        return True
    return False


def unregister_thread(thread_id) -> bool:
    with reconcile_lock():
        return _unregister_thread_unlocked(thread_id)


@contextlib.contextmanager
def reconcile_lock(blocking=True):
    """Serialise concurrent reconciles.

    SessionStart hooks and manual commands can race. Without the lock each
    would see no pidfile and spawn its own shim for the same thread, and two
    shims on one thread means two registry records and a doubled reply.
    """
    path = os.path.join(state_dir(), "reconcile.lock")
    fh = open(path, "a+")
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError:
            if blocking:
                raise
            yield False
            return
        acquired = True
        yield True
    finally:
        try:
            if acquired:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
