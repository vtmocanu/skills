"""Claude for the session-peers CLI."""

from __future__ import annotations
import hashlib
import json
import os
import socket
import stat
import sys
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import codex as sp_codex, lifecycle as sp_lifecycle, process as sp_process, storage as sp_storage

def read_claude_records():
    """Every parseable record in the registry, live or not, with its path."""
    out = []
    d = sp_storage.claude_sessions_dir()
    try:
        names = sorted(os.listdir(d))
    except (FileNotFoundError, NotADirectoryError):
        return out
    except OSError as exc:
        sp_runtime.log("cannot read %s: %s" % (d, exc))
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = sp_runtime.read_json(os.path.join(d, name))
        if isinstance(rec, dict):
            rec = dict(rec)
            rec["_path"] = os.path.join(d, name)
            out.append(rec)
    return out


def record_liveness(rec) -> str:
    """Pid alive, pidDomain equal, procStart equal to `ps -o lstart=`.

    Returns ``live``, ``dead`` or ``unverified``. A sandbox can deny ``ps``
    while the process and socket are healthy; collapsing that denial into
    ``dead`` made ``list`` and ``send`` falsely claim no Claude session existed.
    A missing process-start value is unverified because it cannot rule out PID
    reuse; a present value must match when the probe is available.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int) or not sp_process.pid_alive(pid):
        return "dead"
    domain = rec.get("pidDomain")
    if domain is not None and domain != sp_process.pid_domain():
        return "dead"
    recorded = rec.get("procStart")
    if not recorded:
        return "unverified"
    actual, error = sp_process.proc_start_checked(pid)
    if error is not None:
        return "unverified"
    if actual.strip() != str(recorded).strip():
        return "dead"
    return "live"


def record_is_live(rec) -> bool:
    return record_liveness(rec) == "live"


def live_claude_records():
    return [r for r in read_claude_records() if record_is_live(r)]


def unverified_claude_records():
    return [r for r in read_claude_records() if record_liveness(r) == "unverified"]


def claude_record_by_name(name, records=None):
    """Every live record carrying this exact name."""
    if records is None:
        records = live_claude_records()
    return [r for r in records if r.get("name") == name]


def claude_record_by_target(target, records=None):
    """Every record addressed by mutable name or stable session UUID."""
    if records is None:
        records = live_claude_records()
    if sp_runtime.is_uuid(target):
        return [r for r in records if r.get("sessionId") == target]
    return [r for r in records if r.get("name") == target]


def claude_record_by_socket(sock_path, records=None):
    """The live record whose messagingSocketPath is this socket, or None."""
    if records is None:
        records = live_claude_records()
    want = os.path.realpath(sock_path)
    for r in records:
        p = r.get("messagingSocketPath")
        if p and os.path.realpath(p) == want:
            return r
    return None


def peer_token_for(rec):
    """The optional `<pid>.<sha256(socket)>.key` payload, or None."""
    pid = rec.get("pid")
    sock = rec.get("messagingSocketPath")
    if not pid or not sock:
        return None
    digest = hashlib.sha256(sock.encode("utf-8")).hexdigest()
    path = os.path.join(sp_storage.claude_sessions_dir(), "%s.%s.key" % (pid, digest))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(parsed, dict):
        return parsed.get("peerToken") or parsed.get("token")
    return raw if not isinstance(parsed, str) else parsed


# --------------------------------------------------------------------------
# Socket directory allowlist
# --------------------------------------------------------------------------


def allowlisted_socket_dirs(platform=None, uid=None):
    """The directories Claude's 2.1.x sender will connect into.

    $TMPDIR is deliberately absent: it is never consulted (PRD Facts).
    """
    platform = platform or sys.platform
    uid = os.getuid() if uid is None else uid
    dirs = []
    if platform == "darwin":
        for base in ("/tmp", "/private/tmp"):
            dirs.append("%s/cc-socks" % base)
            dirs.append("%s/cc-socks-%d" % (base, uid))
    elif platform.startswith("linux"):
        dirs.append("/run/user/%d/cc-socks" % uid)
        dirs.append("/data/data/com.termux/files/usr/tmp/cc-socks")
    override = os.environ.get("SESSION_PEERS_SOCKET_DIR")
    if override:
        dirs.append(override)
    return dirs


def dir_is_allowlisted(path, platform=None, uid=None) -> bool:
    if not path:
        return False
    want = os.path.realpath(path)
    for d in allowlisted_socket_dirs(platform, uid):
        if os.path.realpath(d) == want:
            return True
    return False


def socket_path_ok(path) -> bool:
    """D8: refuse a symlinked endpoint or one outside the allowlisted dirs."""
    if not path:
        return False
    if os.path.islink(path):
        return False
    return dir_is_allowlisted(os.path.dirname(path))


def ensure_socket_dir(path):
    """Create and then VERIFY the socket directory (S2).

    Binding in the directory a live record names is the PRD's rule, but the
    directory is still shared state: a symlink, another user's ownership or a
    group-writable mode would all widen the same-uid model D8 assumes.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise SystemExit("refusing to bind: %s is a symlink" % path)
    if not stat.S_ISDIR(st.st_mode):
        raise SystemExit("refusing to bind: %s is not a directory" % path)
    if st.st_uid != os.getuid():
        raise SystemExit(
            "refusing to bind: %s is owned by uid %d, not %d"
            % (path, st.st_uid, os.getuid())
        )
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)  # an OSError here must surface, not be swallowed
        st = os.lstat(path)
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise SystemExit(
                "refusing to bind: %s is mode %o, not 700"
                % (path, stat.S_IMODE(st.st_mode))
            )
    return path


def default_socket_dir():
    """The directory of a live record's socket, else the platform default."""
    override = os.environ.get("SESSION_PEERS_SOCKET_DIR")
    if override:
        return override
    for rec in live_claude_records():
        p = rec.get("messagingSocketPath")
        if p:
            return os.path.dirname(p)
    dirs = allowlisted_socket_dirs()
    return dirs[0] if dirs else "/tmp/cc-socks"


def send_frame(sock_path, frame, auth_token=None, timeout=10.0):
    """One NDJSON frame to a peer socket. Raises OSError on a failed connect."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        if auth_token:
            s.sendall(
                json.dumps({"type": "auth", "token": auth_token}).encode("utf-8") + b"\n"
            )
        s.sendall(json.dumps(frame).encode("utf-8") + b"\n")
    finally:
        try:
            s.close()
        except OSError:
            pass


def deliver_to_record(rec, frame):
    """Send one frame to a Claude session record, with its auth line if any."""
    sock_path = rec.get("messagingSocketPath")
    if not socket_path_ok(sock_path):
        sp_runtime.log("refusing to write to %r: symlink or non-allowlisted directory" % sock_path)
        return False
    try:
        send_frame(sock_path, frame, auth_token=peer_token_for(rec))
    except OSError as exc:
        sp_runtime.log("delivery to %s failed: %s" % (rec.get("name") or rec.get("pid"), exc))
        return False
    return True


def _resolve_claude_record(target, exclude=None):
    records = read_claude_records()
    if exclude and not sp_runtime.is_uuid(target):
        # A shim's record carries its thread's UUID, so this also drops the
        # excluded Codex thread's own shim.
        records = [r for r in records if r.get("sessionId") != exclude]
    candidates = claude_record_by_target(target, records)
    classified = [(record, record_liveness(record)) for record in candidates]
    matches = [record for record, status in classified if status == "live"]
    if any(status == "unverified" for _record, status in classified):
        raise sp_codex.ResolveError(
            "cannot verify Claude target %r because the process-start probe is "
            "unavailable; retry outside the sandbox or with host permission" % target
        )
    if not matches:
        noun = "id" if sp_runtime.is_uuid(target) else "name"
        raise sp_codex.ResolveNotFound("no live Claude session with %s %r" % (noun, target))
    if len(matches) > 1:
        raise sp_codex.ResolveError(
            "%r names %d live sessions (%s); rename one"
            % (target, len(matches), ", ".join(str(m.get("pid")) for m in matches))
        )
    rec = matches[0]
    if not socket_path_ok(rec.get("messagingSocketPath")):
        raise sp_codex.ResolveError(
            "%s listens on %r, outside the allowlisted socket directories"
            % (target, rec.get("messagingSocketPath"))
        )
    return rec


def _deliver_claude(rec, message, thread_id=None, reply_route=True):
    thread_name = None
    shim_socket = None
    if thread_id:
        state = sp_runtime.read_json(sp_storage.thread_state_path(thread_id), {}) or {}
        thread_name = state.get("name")
        pid = sp_lifecycle.shim_pid(thread_id)
        if pid:
            rec_path = os.path.join(sp_storage.claude_sessions_dir(), "%d.json" % pid)
            shim_rec = sp_runtime.read_json(rec_path, {}) or {}
            shim_socket = shim_rec.get("messagingSocketPath")
    route = shim_socket if reply_route else None
    body = sp_protocol.build_cc_body(message, thread_id or "", thread_name, route)
    if len(body) > sp_constants.MAX_TEXT_CHARS or sp_runtime.utf8_len(body) > sp_constants.MAX_TEXT_CHARS:
        raise ValueError(
            "wrapped message exceeds the %d-character/UTF-8-byte peer cap"
            % sp_constants.MAX_TEXT_CHARS
        )
    frame = sp_protocol.build_user_frame(body, route)
    send_frame(rec["messagingSocketPath"], frame, auth_token=peer_token_for(rec))
    return frame["msg_id"], bool(route)


def _current_claude_session_id():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        rec = claude_record_by_socket(sock, read_claude_records())
        if rec:
            return rec.get("sessionId")
    return None
