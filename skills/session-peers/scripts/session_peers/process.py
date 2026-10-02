"""Process for the session-peers CLI."""

from __future__ import annotations
import os
import socket
import struct
import sys
from . import runtime as sp_runtime

# --------------------------------------------------------------------------
# Claude registry
# --------------------------------------------------------------------------


def pid_domain():
    """The record field Claude stamps so a pid from another namespace is stale."""
    return sys.platform


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def proc_start_checked(pid):
    """(`ps` start time, error), distinguishing denial from a dead record."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    rc, out, err = sp_runtime.run_cmd(
        ["ps", "-o", "lstart=", "-p", str(pid)], timeout=10, env=env
    )
    if rc != 0:
        detail = err.strip() or "ps exited %d" % rc
        return None, detail
    value = out.strip()
    if not value:
        return None, "ps returned no process start time"
    return value, None


def proc_start(pid):
    """`ps -o lstart= -p <pid>` under LC_ALL=C TZ=UTC, trimmed."""
    value, _error = proc_start_checked(pid)
    return value


def canon_path(path):
    """Canonicalize a path so holder keys and lookup keys agree.

    `lsof` reports the symlink-resolved (real) path in its `n` field, while the
    paths we probe come from the state DB and from CODEX_HOME unresolved. When
    CODEX_HOME is a symlink (e.g. a mackup-managed `~/.codex` -> a repo dir), the
    two never match and EVERY thread reads as dead -- `up`/`send` refuse and no
    shim starts. Resolving both sides with realpath makes them agree; a
    missing/None path (or one realpath cannot resolve) is returned unchanged.
    The socket-dir matching already relies on realpath (see
    `claude_record_by_socket`, `dir_is_allowlisted`); this applies the same rule
    to Codex holder matching.
    """
    if not path:
        return path
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def lsof_holders_checked(paths):
    """(holders, verified, error) for paths a Codex process may hold.

    Keyed by the canonical (realpath) form so a lookup by an unresolved probe
    path still matches a holder `lsof` reported at its symlink-resolved path.

    `lsof` exits 1 both for a verified no-match and for some failures. It can
    also exit 1 with valid holder data on stdout when another requested path is
    absent or has no holder. Remove paths proven absent before the batch, then
    accept exit 1 only when stderr is empty. A path whose existence cannot be
    checked remains unverified, never evidence that a thread is dead.
    """
    out = {}
    existing = []
    seen = set()
    for path in paths:
        if not path:
            continue
        canonical = canon_path(path)
        if canonical in seen:
            continue
        try:
            os.stat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return out, False, "cannot inspect %s: %s" % (path, exc)
        existing.append(path)
        seen.add(canonical)
    paths = existing
    if not paths:
        return out, True, None
    rc, stdout, stderr = sp_runtime.run_cmd(
        ["lsof", "-F", "pcn", "--"] + list(paths), timeout=20
    )
    if rc != 0 and not (rc == 1 and not stderr.strip()):
        detail = stderr.strip() or stdout.strip() or "lsof exited %d" % rc
        return out, False, detail
    pid = None
    cmd = ""
    for line in stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = None
            cmd = ""
        elif tag == "c":
            cmd = value
        elif tag == "n":
            if pid is None:
                continue
            base = os.path.basename(cmd or "")
            if "codex" not in base.lower():
                continue
            out.setdefault(canon_path(value), []).append((pid, cmd))
    return out, True, None


def lsof_holders(paths):
    """Compatibility wrapper returning only verified holder data."""
    holders, verified, error = lsof_holders_checked(paths)
    if not verified:
        sp_runtime.log("Codex thread liveness is unavailable: %s" % error)
    return holders


# --------------------------------------------------------------------------
# The shim (D2, D4, D8)
# --------------------------------------------------------------------------


def peer_uid(conn):
    """The connecting process's uid, or None when the platform will not say."""
    try:
        if sys.platform.startswith("linux"):
            so_peercred = getattr(socket, "SO_PEERCRED", 17)
            buf = conn.getsockopt(
                socket.SOL_SOCKET, so_peercred, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", buf)
            return uid
        if sys.platform == "darwin":
            sol_local = 0
            local_peercred = getattr(socket, "LOCAL_PEERCRED", 1)
            buf = conn.getsockopt(sol_local, local_peercred, 64)
            if len(buf) < 8:
                return None
            _version, uid = struct.unpack("2I", buf[:8])
            return uid
    except (OSError, struct.error, ValueError):
        return None
    return None


def process_ancestors(pid=None, limit=64):
    """Ancestor pids of ``pid`` (default: this process), nearest first, or
    None when the process table cannot be read."""
    rc, out, _err = sp_runtime.run_cmd(["ps", "-axo", "pid=,ppid="])
    if rc != 0:
        return None
    parent = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parent[int(fields[0])] = int(fields[1])
    current = os.getpid() if pid is None else pid
    if current not in parent:
        return None
    chain = []
    while len(chain) < limit:
        current = parent.get(current)
        if not current or current in chain:
            break
        chain.append(current)
    return chain


def _nearest_owner(claude_pid, codex_pid):
    """``cc`` or ``codex``: whose process is the nearer ancestor, else None."""
    ancestors = process_ancestors()
    if not ancestors:
        return None
    for pid in ancestors:
        if claude_pid and pid == claude_pid:
            return "cc"
        if codex_pid and pid == codex_pid:
            return "codex"
    return None
