#!/usr/bin/env python3
"""Cross-session messaging between Claude Code sessions and Codex CLI threads.

One script, one CLI. See PRD #44 and ``../references/spike-checklist.md`` for the
measurements every mechanism here relies on; the decision letters (D1..D11) in
the comments point at that PRD's decision log.

Subcommands::

    peers.py list [--json]
    peers.py send --to codex:<name|uuid>|cc:<name> --message <text>
                  [--from-thread <uuid>] [--from-name N] [--from-sid S]
                  [--from-socket P]
    peers.py shim --thread <uuid>
    peers.py up [<name|uuid>]
    peers.py down [<name|uuid>]
    peers.py budget reset <name|uuid>
    peers.py session-hook
    peers.py install-hook
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

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# D11: pins, not requirements. A newer install warns, never fails.
CLAUDE_CODE_TESTED = "2.1.263"
CODEX_TESTED = "0.153.4"

PEER_PROTOCOL = 1
PEER_FEATURES = ["notify_idle"]

# Codex MAX_USER_INPUT_TEXT_CHARS, and roughly Claude's inbound line cap.
MAX_TEXT_CHARS = 1048576

# D4: replies per (thread, Claude session) before the shim goes quiet.
REPLY_BUDGET = 3

# Columns the `threads` table must have for the schema to count as recognised.
THREADS_COLUMNS = frozenset({"id", "rollout_path", "cwd", "name", "updated_at"})

TAG_PREFIX = "[session-peers"
TAG_RE = re.compile(
    r"^\[session-peers from=@(?P<from>\S*) sid=(?P<sid>\S*) reply=(?P<reply>.*)\]$"
)
WRAPPER_RE = re.compile(
    r"^\s*<cross-session-message\s+(?P<attrs>[^>]*)>\n?(?P<body>.*?)\n?</cross-session-message>\s*$",
    re.DOTALL,
)
ATTR_RE = re.compile(r'([A-Za-z][A-Za-z0-9-]*)="([^"]*)"')
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
AT_NAME_RE = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9_.\-]*)")

# Sentinel for a tag field the sender could not fill in. Parses back to None.
TAG_ABSENT = "-"

# A peer name reaches Claude inside a wrapper attribute and inside the tag line,
# so it is restricted at the door rather than escaped at every use (B1).
PEER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Substitute for the "<" of a wrapper tag appearing inside a body. Printable and
# visible in a transcript, unlike a zero-width character.
LT_SUBSTITUTE = "\u2039"
WRAPPER_MARKUP_RE = re.compile(r"<(/?)(cross-session-message)", re.IGNORECASE)
C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Tunables, read at shim start. The tests turn them down so a fixture rollout is
# picked up in milliseconds rather than seconds.
POLL_INTERVAL_DEFAULT = 1.0
LIVENESS_INTERVAL_DEFAULT = 5.0
CONN_TIMEOUT = 30.0
MAX_RECORD_REWRITES = 2
DELIVERED_HISTORY = 200
CONTACT_HISTORY = 200
MAX_CONCURRENT_CLIENTS = 8
MAX_FRAMES_PER_CONNECTION = 16
READ_CHUNK = 1024 * 1024
# A rollout line longer than this is not a Codex turn (its own text cap is 1
# MiB): skip it rather than buffer it, so one damaged file cannot exhaust RAM.
MAX_ROLLOUT_LINE = 8 * 1024 * 1024
# S7: a completion older than this at shim start is recorded, never posted.
RESTART_DELIVERY_WINDOW = 900.0

def parse_time(value):
    """Epoch seconds from an ISO-8601 string or a numeric epoch. None if unclear.

    Codex writes ISO timestamps on rollout lines and on `task_complete`; a
    numeric form is accepted in case a future version switches, and anything
    else is "unknown", which the caller treats as "deliver" rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # A value this large is milliseconds, not seconds (year 5138 vs 1970).
        return seconds / 1000.0 if seconds > 1e11 else seconds
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _float_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


Turn = collections.namedtuple(
    "Turn", "turn_id user_text tag outcome last_agent_message completed_at",
    defaults=(None,),
)
Event = collections.namedtuple("Event", "kind turn_id turn")

def log(msg: str) -> None:
    """One stderr line, timestamped. The shim's stderr is its log file."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write("[session-peers %s] %s\n" % (stamp, msg))
    sys.stderr.flush()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def read_json(path, default=None):
    """Parse a JSON file, tolerating absence and corruption (never raises)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log("ignoring unreadable %s: %s" % (path, exc))
        return default


def write_json_atomic(path, data, mode=0o600):
    """Write JSON through a temp file in the same directory, then rename."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    # N2: create at the final mode rather than widening it for a moment.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, mode)  # umask may have narrowed the mode above
    os.replace(tmp, path)


def is_uuid(value) -> bool:
    return bool(value) and bool(UUID_RE.match(str(value)))


def parse_version(text):
    """Pull the first dotted-numeric run out of a `--version` line."""
    if not text:
        return None
    m = VERSION_RE.search(text)
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def version_is_newer(installed, pinned) -> bool:
    """True when `installed` sorts above `pinned`; unparseable means False."""
    a, b = parse_version(installed), parse_version(pinned)
    if a is None or b is None:
        return False
    return a > b


def run_cmd(argv, timeout=10, env=None):
    """Run a command, returning (rc, stdout, stderr). A missing binary is rc 127."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "%s: not found" % argv[0]
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, "", str(exc)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def tool_version(binary):
    """`<binary> --version` output, or None. Never raises, never fails a run."""
    rc, out, err = run_cmd([binary, "--version"], timeout=10)
    if rc != 0:
        return None
    return (out or err).strip() or None


def warn_versions(kinds=("claude", "codex")) -> None:
    """D11: a newer install than the pin prints one line and keeps going."""
    if "claude" in kinds:
        v = tool_version("claude")
        if v and version_is_newer(v, CLAUDE_CODE_TESTED):
            log(
                "Claude Code %s is newer than the tested %s; if peers stop "
                "appearing, re-run references/spike-checklist.md"
                % (v.strip(), CLAUDE_CODE_TESTED)
            )
    if "codex" in kinds:
        v = tool_version("codex")
        if v and version_is_newer(v, CODEX_TESTED):
            log(
                "Codex CLI %s is newer than the tested %s; if discovery breaks, "
                "re-run references/spike-checklist.md" % (v.strip(), CODEX_TESTED)
            )


# --------------------------------------------------------------------------
# A very small TOML reader (D3: no tomllib on 3.9)
# --------------------------------------------------------------------------


def _toml_scalar(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw[0] in "\"'":
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            return raw[1:]
        return raw[1:end]
    # Strip an inline comment from an unquoted value.
    raw = raw.split("#", 1)[0].strip()
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def read_toml_lite(path):
    """Return {section_header: {key: value}} with "" for the root table.

    Enough for the three things the bridge reads: a root `sqlite_home`, the
    `[features] hooks` flag, and the `[hooks.state."..."]` blocks. Section keys
    are the raw text between the brackets, so a quoted dotted key survives.
    """
    out = {"": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return out
    except OSError as exc:
        log("ignoring unreadable %s: %s" % (path, exc))
        return out
    section = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section.startswith("[") and section.endswith("]"):
                section = section[1:-1].strip()  # array of tables
            out.setdefault(section, {})
            continue
        if "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        out.setdefault(section, {})[key.strip().strip("\"'")] = _toml_scalar(raw)
    return out


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
    """CODEX_SQLITE_HOME, else config.toml `sqlite_home`, else CODEX_HOME."""
    env = os.environ.get("CODEX_SQLITE_HOME")
    if env:
        return os.path.expanduser(env)
    cfg = read_toml_lite(codex_config_path())
    value = cfg.get("", {}).get("sqlite_home")
    if not value:
        # Tolerate the key living under a table rather than at the root.
        for section, keys in cfg.items():
            if section and "sqlite_home" in keys:
                value = keys["sqlite_home"]
                break
    if isinstance(value, str) and value:
        return os.path.expanduser(value)
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


def proc_start(pid):
    """`ps -o lstart= -p <pid>` under LC_ALL=C TZ=UTC, trimmed. None on failure."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    rc, out, _err = run_cmd(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10, env=env)
    if rc != 0:
        return None
    return out.strip() or None


def read_claude_records():
    """Every parseable record in the registry, live or not, with its path."""
    out = []
    d = claude_sessions_dir()
    try:
        names = sorted(os.listdir(d))
    except (FileNotFoundError, NotADirectoryError):
        return out
    except OSError as exc:
        log("cannot read %s: %s" % (d, exc))
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = read_json(os.path.join(d, name))
        if isinstance(rec, dict):
            rec = dict(rec)
            rec["_path"] = os.path.join(d, name)
            out.append(rec)
    return out


def record_is_live(rec) -> bool:
    """Pid alive, pidDomain equal, procStart equal to `ps -o lstart=`.

    Parsing is lenient (PRD Facts): a field the record omits is not checked, a
    field it carries must match.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    domain = rec.get("pidDomain")
    if domain is not None and domain != pid_domain():
        return False
    recorded = rec.get("procStart")
    if recorded:
        actual = proc_start(pid)
        if actual is None or actual.strip() != str(recorded).strip():
            return False
    return True


def live_claude_records():
    return [r for r in read_claude_records() if record_is_live(r)]


def claude_record_by_name(name, records=None):
    """Every live record carrying this exact name."""
    if records is None:
        records = live_claude_records()
    return [r for r in records if r.get("name") == name]


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
    path = os.path.join(claude_sessions_dir(), "%s.%s.key" % (pid, digest))
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


# --------------------------------------------------------------------------
# Codex discovery
# --------------------------------------------------------------------------


def find_state_db(sqlite_home=None):
    """The state_*.sqlite whose `threads` schema we recognise, or None.

    The numeric suffix is a schema version, so the newest file is not always
    the readable one: pick by schema, never by the largest number.
    """
    home = sqlite_home or codex_sqlite_home()
    for path in sorted(glob.glob(os.path.join(home, "state_*.sqlite"))):
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        except sqlite3.Error:
            cols = set()
        finally:
            conn.close()
        if THREADS_COLUMNS <= cols:
            return path
    return None


def read_session_index():
    """{thread id: name} merged from every session_index.jsonl (S11).

    CODEX_HOME and sqlite_home can differ, and the first openable file is not
    necessarily the fuller one, so both are read and the newest `updated_at`
    wins for an id present in both.
    """
    out = {}
    seen_at = {}
    candidates = [os.path.join(codex_home(), "session_index.jsonl")]
    alt = os.path.join(codex_sqlite_home(), "session_index.jsonl")
    if alt not in candidates:
        candidates.append(alt)
    for path in candidates:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not obj.get("id"):
                    continue
                name = obj.get("thread_name")
                if not name:
                    continue
                tid = str(obj["id"])
                when = parse_time(obj.get("updated_at"))
                previous = seen_at.get(tid)
                if tid in out and previous is not None and when is not None:
                    if when < previous:
                        continue
                out[tid] = name
                if when is not None:
                    seen_at[tid] = when
    return out


def lsof_holders(paths):
    """{path: [(pid, command)]} for paths a `codex` process holds open.

    One `lsof -F pcn` call for the whole set. A missing lsof, a non-zero exit
    (lsof exits 1 when nothing matches) and unparseable output all mean "no
    holder", never a traceback.
    """
    out = {}
    paths = [p for p in paths if p]
    if not paths:
        return out
    rc, stdout, _err = run_cmd(["lsof", "-F", "pcn", "--"] + list(paths), timeout=20)
    if rc == 127:
        log("lsof is not on PATH; Codex thread liveness is unavailable")
        return out
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
            out.setdefault(value, []).append((pid, cmd))
    return out


def codex_threads(check_live=True):
    """(threads, schema_ok). Each thread is a dict; degraded mode returns []."""
    db = find_state_db()
    if db is None:
        log(
            "no state_*.sqlite with a recognised `threads` schema under %s; "
            "Codex discovery is unavailable (send by UUID still works)"
            % codex_sqlite_home()
        )
        return [], False
    rows = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    except sqlite3.Error as exc:
        log("cannot open %s: %s" % (db, exc))
        return [], False
    try:
        cur = conn.execute(
            "SELECT id, name, rollout_path, cwd, updated_at FROM threads"
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        log("cannot read threads from %s: %s" % (db, exc))
        return [], False
    finally:
        conn.close()

    index = read_session_index()
    registered = read_registered()
    threads = []
    for row in rows:
        tid = str(row[0])
        threads.append(
            {
                "id": tid,
                "name": row[1] or index.get(tid),
                "rollout_path": row[2],
                "cwd": row[3],
                "updated_at": row[4],
                "registered": tid in registered,
                "holder_pid": None,
                "live": False,
            }
        )
    if check_live and threads:
        holders = lsof_holders([t["rollout_path"] for t in threads])
        for t in threads:
            found = holders.get(t["rollout_path"]) or []
            if found:
                t["live"] = True
                t["holder_pid"] = found[0][0]
    return threads, True


def thread_is_held(rollout_path, holder_pid=None):
    """True when a codex process still holds this rollout open.

    With `holder_pid` given, that exact pid must still hold it (D2: a live
    daemon can unload one thread while staying alive).
    """
    holders = lsof_holders([rollout_path]).get(rollout_path) or []
    if holder_pid is None:
        return bool(holders), (holders[0][0] if holders else None)
    for pid, _cmd in holders:
        if pid == holder_pid:
            return True, pid
    return False, (holders[0][0] if holders else None)


class ResolveError(Exception):
    """A thread target that cannot be turned into exactly one live thread."""


def resolve_thread(target, require_live=True):
    """Turn `<name|uuid>` into one thread dict, or raise ResolveError.

    D6/R7: a name held by more than one live thread is refused rather than
    guessed at; `codex queue`'s own name matching picks a match, so the bridge
    never delegates the decision.
    """
    threads, schema_ok = codex_threads()
    if not schema_ok:
        if is_uuid(target):
            # D11 degraded mode: queue by UUID, liveness unverified.
            return {
                "id": target,
                "name": None,
                "rollout_path": None,
                "cwd": None,
                "updated_at": None,
                "registered": target in read_registered(),
                "holder_pid": None,
                "live": None,
                "degraded": True,
            }
        raise ResolveError(
            "Codex thread discovery is unavailable (unknown state_*.sqlite "
            "schema); pass the thread UUID instead of a name"
        )
    if is_uuid(target):
        for t in threads:
            if t["id"] == target:
                return t
        raise ResolveError("no Codex thread with id %s" % target)
    matches = [t for t in threads if t.get("name") == target]
    if not matches:
        raise ResolveError(
            "no Codex thread named %r; /rename it in the TUI, or pass its UUID"
            % target
        )
    if require_live:
        live = [t for t in matches if t["live"]]
        if not live:
            # Never silently pick a thread whose process is gone: Codex's title
            # suggester reuses names, so a dead match is not evidence of intent.
            raise ResolveError(
                "no live Codex thread named %r (%d past thread(s) carried that "
                "name); /rename the running one, or pass its UUID"
                % (target, len(matches))
            )
        matches = live
    if len(matches) > 1:
        raise ResolveError(
            "the name %r is held by %d %sthreads (%s); register by UUID instead"
            % (
                target,
                len(matches),
                "live " if require_live else "",
                ", ".join(t["id"] for t in matches),
            )
        )
    return matches[0]


# --------------------------------------------------------------------------
# Registration (D6)
# --------------------------------------------------------------------------


def read_registered():
    data = read_json(registered_path(), {}) or {}
    threads = data.get("threads")
    return threads if isinstance(threads, dict) else {}


def write_registered(threads):
    write_json_atomic(registered_path(), {"threads": threads}, mode=0o600)


def register_thread(thread):
    """Record a thread as opted in. A name that cannot be a peer name is
    refused here rather than at delivery time, so the failure names the fix."""
    name = thread.get("name")
    if name is not None:
        require_peer_name(name)
    threads = read_registered()
    threads[thread["id"]] = {"name": name, "registered_at": now_iso()}
    write_registered(threads)


def unregister_thread(thread_id) -> bool:
    threads = read_registered()
    if thread_id in threads:
        del threads[thread_id]
        write_registered(threads)
        return True
    return False


# --------------------------------------------------------------------------
# The tag line (D4)
# --------------------------------------------------------------------------


def build_tag(from_name=None, sid=None, reply_socket=None) -> str:
    """The one line every bridged message carries into a Codex thread.

    A field the sender could not fill in renders as `-` and parses back to
    None, so an untagged-looking send is still distinguishable from a typed
    prompt while carrying no reply address.
    """

    def field(value):
        # A newline would end the tag line and forge a second one, so CR and
        # LF collapse to underscores exactly like spaces (N3).
        value = (value or "").strip()
        if not value:
            return TAG_ABSENT
        for ch in (" ", "\r", "\n", "\t"):
            value = value.replace(ch, "_")
        return C0_RE.sub("", value) or TAG_ABSENT

    reply = (reply_socket or "").strip()
    return "[session-peers from=@%s sid=%s reply=%s]" % (
        field(from_name),
        field(sid),
        ("uds:%s" % reply) if reply else TAG_ABSENT,
    )


def parse_tag(text):
    """(tag_dict_or_None, body_without_tag). Never raises."""
    if not text:
        return None, text or ""
    first, sep, rest = text.partition("\n")
    if not first.startswith(TAG_PREFIX):
        return None, text
    m = TAG_RE.match(first.strip())
    if not m:
        return None, text
    tag = {}
    for key in ("from", "sid", "reply"):
        value = m.group(key)
        if value == TAG_ABSENT or value == "":
            tag[key] = None
        elif key == "reply" and value.startswith("uds:"):
            tag[key] = value[4:]
        else:
            tag[key] = value
    return tag, rest if sep else ""


def strip_tag(text):
    return parse_tag(text)[1]


# --------------------------------------------------------------------------
# Frames (D7)
# --------------------------------------------------------------------------


class NameError_(ValueError):
    """A peer name that cannot be put into a wrapper attribute safely."""


def valid_peer_name(name) -> bool:
    return bool(name) and bool(PEER_NAME_RE.match(str(name)))


def require_peer_name(name, what="thread name"):
    """Refuse a name that could break out of a wrapper attribute (B1).

    A Codex thread name reaches Claude as `from-name="..."`, so a name
    containing a quote could assert `from-mode`, the one claim D7 forbids.
    Restricting the character set at registration beats escaping at every use.
    """
    if not valid_peer_name(name):
        raise NameError_(
            "%s %r is not usable as a peer name: allow only letters, digits, "
            "dot, underscore and hyphen (up to 64). /rename the thread."
            % (what, name)
        )
    return str(name)


def escape_attr(value) -> str:
    """XML-escape one wrapper attribute value and drop control characters."""
    text = "" if value is None else str(value)
    text = text.replace("\r", "").replace("\n", "")
    text = C0_RE.sub("", text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def neutralise_wrapper_markup(body) -> str:
    """Stop a body closing or forging the wrapper that carries it (B1)."""
    return WRAPPER_MARKUP_RE.sub(LT_SUBSTITUTE + r"\1\2", body or "")


def build_wrapper(body, from_socket, from_session, from_name) -> str:
    """The wrapper Claude's parser accepts, WITHOUT `from-mode` (D7).

    Codex has no Claude permission class, and an unclassified sender is
    exactly what a bypass-permissions session holds for approval. Asserting a
    mode here would be a lie with a security consequence, which is why every
    attribute is escaped and the body cannot close the element (B1).
    """
    socket_text = str(from_socket or "")
    if C0_RE.search(socket_text) or set('"<>&\r\n') & set(socket_text):
        # Never ship a mangled reply address: a Claude reply would go nowhere.
        raise ValueError("socket path %r cannot be put in a wrapper" % socket_text)
    return (
        '<cross-session-message from="uds:%s" from-session="%s" from-name="%s">\n'
        "%s\n</cross-session-message>"
        % (
            escape_attr(socket_text),
            escape_attr(from_session),
            escape_attr(from_name),
            neutralise_wrapper_markup(body),
        )
    )


def unwrap_message(content):
    """(body, attrs) for a wrapped frame; (content, {}) for a bare one."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        content = "\n".join(parts)
    if not isinstance(content, str):
        return "", {}
    m = WRAPPER_RE.match(content)
    if not m:
        return content, {}
    attrs = dict(ATTR_RE.findall(m.group("attrs")))
    return m.group("body"), attrs


def build_user_frame(body, from_socket=None):
    """The exact frame Claude itself sends between sessions."""
    frame = {
        "msgV": 1,
        "msg_id": str(uuidlib.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": body},
        "priority": "next",
    }
    if from_socket:
        frame["from"] = "uds:%s" % from_socket
    return frame


def build_cc_body(text, thread_id, thread_name, shim_socket):
    """Wrapped when the thread has a shim socket to reply to, bare otherwise.

    The bare form renders like a typed prompt with no peer name (measured), so
    it carries its own attribution line.
    """
    if shim_socket:
        return build_wrapper(text, shim_socket, thread_id, thread_name)
    label = thread_name or thread_id
    return "Message from Codex thread %s:\n%s" % (label, text)


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
        log("refusing to write to %r: symlink or non-allowlisted directory" % sock_path)
        return False
    try:
        send_frame(sock_path, frame, auth_token=peer_token_for(rec))
    except OSError as exc:
        log("delivery to %s failed: %s" % (rec.get("name") or rec.get("pid"), exc))
        return False
    return True


# --------------------------------------------------------------------------
# Rollout tail
# --------------------------------------------------------------------------


class RolloutTail:
    """Incremental turn-boundary reader over a Codex rollout JSONL.

    Correlation is by boundary events only: `task_started` opens a turn,
    the following `role: user` response_item (which carries NO turn_id) is its
    prompt, and `task_complete` or `turn_aborted` closes it. "Last user item
    before EOF" is never used, because a queued turn and a typed one interleave.
    """

    def __init__(self, path, cursor=0, open_turn=None, pending=None,
                 last_boundary=None):
        self.path = path
        self.cursor = int(cursor or 0)
        self.open_turn = open_turn
        self.pending = dict(pending or {})
        # N6: the newest of task_started / task_complete / turn_aborted seen,
        # so the interrupt question is answered without rescanning the file.
        self.last_boundary = last_boundary

    # -- state -------------------------------------------------------------

    def state(self):
        return {
            "cursor": self.cursor,
            "open_turn": self.open_turn,
            "pending": self.pending,
            "last_boundary": self.last_boundary,
        }

    @classmethod
    def from_state(cls, path, state):
        state = state or {}
        return cls(
            path,
            cursor=state.get("cursor", 0),
            open_turn=state.get("open_turn"),
            pending=state.get("pending"),
            last_boundary=state.get("last_boundary"),
        )

    def seek_end(self):
        try:
            self.cursor = os.path.getsize(self.path)
        except OSError:
            self.cursor = 0
        return self.cursor

    # -- reading -----------------------------------------------------------

    def _read_lines(self):
        """Complete lines since the cursor, read in bounded chunks (S4).

        Reading cursor-to-EOF in one allocation peaked at 587 MiB on a 194 MiB
        rollout. The cursor still advances only past newline-terminated lines,
        so a partial trailing write is re-read next poll rather than lost.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.cursor:
            # Truncated or rotated underneath us: resync rather than replay.
            log("%s shrank; resyncing the cursor to EOF" % self.path)
            self.cursor = size
            return []
        if size == self.cursor:
            return []
        lines = []
        remaining = size - self.cursor
        pending = b""
        skipping = False
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.cursor)
                while remaining > 0:
                    chunk = fh.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    parts = (pending + chunk).split(b"\n")
                    pending = parts.pop()
                    for part in parts:
                        self.cursor += len(part) + 1
                        if skipping:
                            skipping = False
                            continue
                        if part.strip():
                            lines.append(part.decode("utf-8", "replace"))
                    if len(pending) > MAX_ROLLOUT_LINE:
                        # No Codex turn is this long; drop it rather than grow.
                        log(
                            "skipping a rollout line over %d bytes in %s"
                            % (MAX_ROLLOUT_LINE, self.path)
                        )
                        self.cursor += len(pending)
                        pending = b""
                        skipping = True
        except OSError as exc:
            log("cannot read %s: %s" % (self.path, exc))
        return lines

    def poll(self):
        """Ordered events since the last call: start of a turn and its end."""
        events = []
        for line in self._read_lines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if kind == "event_msg":
                ptype = payload.get("type")
                if ptype == "task_started":
                    turn_id = payload.get("turn_id")
                    self.open_turn = turn_id
                    self.last_boundary = "started"
                    self.pending.setdefault(turn_id, {"tag": None, "text": ""})
                    events.append(Event("start", turn_id, None))
                elif ptype in ("task_complete", "turn_aborted"):
                    turn_id = payload.get("turn_id")
                    info = self.pending.pop(turn_id, {"tag": None, "text": ""})
                    if self.open_turn == turn_id:
                        self.open_turn = None
                    outcome = "complete" if ptype == "task_complete" else "aborted"
                    self.last_boundary = outcome
                    last = payload.get("last_agent_message") if outcome == "complete" else None
                    finished = parse_time(payload.get("completed_at"))
                    if finished is None:
                        finished = parse_time(obj.get("timestamp"))
                    events.append(
                        Event(
                            "end",
                            turn_id,
                            Turn(
                                turn_id,
                                info.get("text") or "",
                                info.get("tag"),
                                outcome,
                                last,
                                finished,
                            ),
                        )
                    )
            elif kind == "response_item":
                if payload.get("role") != "user":
                    continue
                text = self._item_text(payload)
                if text is None:
                    continue
                turn_id = self.open_turn
                if turn_id is None:
                    continue
                tag, body = parse_tag(text)
                info = self.pending.setdefault(turn_id, {"tag": None, "text": ""})
                info["text"] = body
                if tag is not None:
                    info["tag"] = tag
        return events

    def poll_turns(self):
        return [e.turn for e in self.poll() if e.kind == "end"]

    @staticmethod
    def _item_text(payload):
        content = payload.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts) if parts else None


def last_boundary(rollout_path):
    """The last turn boundary in a rollout: 'started', 'complete', 'aborted'.

    Used for the interrupt check: a `turn_aborted` with no later `task_started`
    means the queue is paused until the human types something (measured).
    """
    result = None
    try:
        fh = open(rollout_path, "r", encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError, TypeError):
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if ptype == "task_started":
                result = "started"
            elif ptype == "task_complete":
                result = "complete"
            elif ptype == "turn_aborted":
                result = "aborted"
    return result


def thread_is_paused(rollout_path) -> bool:
    return last_boundary(rollout_path) == "aborted"


# --------------------------------------------------------------------------
# Queueing into a Codex thread
# --------------------------------------------------------------------------


class QueueError(Exception):
    pass


def argv_text_budget():
    """What `codex queue --message <text>` can actually carry.

    The message is one argv element, so the real ceiling is ARG_MAX minus the
    environment, not Codex's MAX_USER_INPUT_TEXT_CHARS. On macOS both are
    1048576, so a message at Codex's own cap never reaches it: the exec fails
    with E2BIG (measured 2026-09-07, `getconf ARG_MAX` = 1048576).
    """
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError, AttributeError):
        return MAX_TEXT_CHARS
    env_bytes = sum(len(k) + len(v) + 2 for k, v in os.environ.items())
    budget = arg_max - env_bytes - 8192
    if sys.platform.startswith("linux"):
        # Linux caps ONE argv element at MAX_ARG_STRLEN (32 pages), far below
        # ARG_MAX; macOS has no separate per-argument limit.
        try:
            page = os.sysconf("SC_PAGESIZE")
        except (ValueError, OSError, AttributeError):
            page = 4096
        budget = min(budget, 32 * page - 1024)
    return max(4096, min(MAX_TEXT_CHARS, budget))


def codex_queue(thread_id, text):
    """`codex queue --thread <uuid> --message <text>`.

    rc != 0, or "No active session" on stderr, means the thread is not live.
    """
    budget = argv_text_budget()
    if len(text) > budget:
        raise QueueError(
            "message is %d chars, over the %d cap this machine can pass to "
            "`codex queue` (Codex itself stops at %d)"
            % (len(text), budget, MAX_TEXT_CHARS)
        )
    rc, out, err = run_cmd(
        ["codex", "queue", "--thread", str(thread_id), "--message", text], timeout=60
    )
    if rc == 127:
        raise QueueError("codex is not on PATH")
    blob = "%s\n%s" % (out, err)
    if rc != 0 or "No active session" in blob:
        raise QueueError(
            "codex queue failed (rc %d): %s" % (rc, (err or out).strip() or "no output")
        )
    return out.strip()


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


class Shim:
    """One process standing in for one Codex thread inside Claude's fabric.

    The record it writes carries its OWN pid, because Claude's sender checks
    that the process accepting on the socket is that pid: a forked handler
    would fail the check. Threads are used instead.
    """

    def __init__(self, thread):
        self.thread = thread
        self.thread_id = thread["id"]
        self.rollout_path = thread.get("rollout_path")
        self.holder_pid = thread.get("holder_pid")
        # B1: the name reaches Claude inside a wrapper attribute. Refuse a
        # name that could break out of it rather than escaping it downstream.
        self.name = require_peer_name(
            thread.get("name") or "codex-%s" % self.thread_id[:8]
        )
        self.cwd = thread.get("cwd") or os.getcwd()

        self.sock_dir = default_socket_dir()
        self.sock_path = os.path.join(self.sock_dir, "%d.sock" % os.getpid())
        self.record_path = os.path.join(
            claude_sessions_dir(), "%d.json" % os.getpid()
        )

        state = read_json(thread_state_path(self.thread_id), {}) or {}
        self.tail = RolloutTail.from_state(self.rollout_path, state.get("tail"))
        self.delivered = collections.deque(
            state.get("delivered") or [], maxlen=DELIVERED_HISTORY
        )
        self.budgets = dict(state.get("budgets") or {})
        self.contacts = dict(state.get("contacts") or {})
        self.fresh = not state
        if self.fresh:
            # No prior state: start at EOF so a restart cannot resend history.
            self.tail.seek_end()
        if self.tail.last_boundary is None and self.rollout_path:
            # N6: one full scan at start seeds the interrupt state; every later
            # answer comes from the tail, not from rescanning the whole file.
            self.tail.last_boundary = last_boundary(self.rollout_path)

        self.started_at = time.time()
        self.status = "idle"
        self.poll_interval = _float_env(
            "SESSION_PEERS_POLL_INTERVAL", POLL_INTERVAL_DEFAULT
        )
        self.liveness_interval = _float_env(
            "SESSION_PEERS_LIVENESS_INTERVAL", LIVENESS_INTERVAL_DEFAULT
        )
        self._proc_start = None
        self.idle_subs = []
        self.record_rewrites = 0
        self.stop = threading.Event()
        self.srv = None
        self._lock = threading.Lock()
        self._cleaned = False
        self._pidfile_fd = None
        self._clients = threading.Semaphore(MAX_CONCURRENT_CLIENTS)
        self.codex_version = None

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        if not self.rollout_path:
            log("thread %s has no rollout path; nothing to tail" % self.thread_id)
            return 2
        held, pid = thread_is_held(self.rollout_path, self.holder_pid)
        if not held:
            log(
                "thread %s is not held by a live codex process; not starting"
                % self.thread_id
            )
            return 3
        self.holder_pid = pid or self.holder_pid
        # S12: `codex --version` prints "codex-cli 0.153.4", so the raw string
        # rendered as "codex-codex-cli 0.153.4" in the record. Keep the number.
        raw = tool_version("codex")
        parsed = parse_version(raw)
        self.codex_version = ".".join(str(n) for n in parsed) if parsed else CODEX_TESTED

        self._consume_budget_marker(initial=True)
        self._bind()
        try:
            # Everything past the bind is inside the cleanup guard: a refusal
            # here must not leave a socket or a record behind.
            self._write_record()
            self._write_pidfile()
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)

            accept = threading.Thread(
                target=self._accept_loop, name="accept", daemon=True
            )
            accept.start()
            log(
                "shim up: thread=%s name=%s socket=%s holder=%s"
                % (self.thread_id, self.name, self.sock_path, self.holder_pid)
            )
            self._poll_loop()
        finally:
            self._cleanup()
        return 0

    def _on_signal(self, signum, _frame):
        # Python's default SIGTERM handler skips `finally`, so the record and
        # socket are removed here rather than on the way out (measured).
        log("signal %d; shutting down" % signum)
        self._cleanup()
        self.stop.set()

    def _cleanup(self):
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        self._save_state()
        for path in (self.record_path, self.sock_path, thread_pid_path(self.thread_id)):
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
        ensure_socket_dir(self.sock_dir)
        if not socket_path_ok(self.sock_path):
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
        stamp = now_ms()
        if self._proc_start is None:
            self._proc_start = proc_start(os.getpid())
        return {
            "pid": os.getpid(),
            "sessionId": self.thread_id,
            "cwd": self.cwd,
            # S12: milliseconds since the epoch, not an ISO string. Claude reads
            # this as a number, and an ISO string rendered as "started 20703d
            # ago" in ListAgents (measured in M6).
            "startedAt": int(self.started_at * 1000),
            "procStart": self._proc_start,
            "version": "codex-%s" % (self.codex_version or CODEX_TESTED),
            "peerProtocol": PEER_PROTOCOL,
            "peerFeatures": list(PEER_FEATURES),
            "kind": "interactive",
            "entrypoint": "codex",
            "pidDomain": pid_domain(),
            "messagingSocketPath": self.sock_path,
            "name": self.name,
            "nameSource": "user",
            "nameSince": int(self.started_at * 1000),
            "status": self.status,
            "updatedAt": stamp,
            "statusUpdatedAt": stamp,
        }

    def _write_record(self):
        d = claude_sessions_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        write_json_atomic(self.record_path, self.record(), mode=0o644)

    def _set_status(self, status):
        if status == self.status:
            return
        self.status = status
        if os.path.exists(self.record_path):
            self._write_record()

    @staticmethod
    def _bound(mapping, cap=CONTACT_HISTORY):
        """Drop the oldest entries so a long-lived shim cannot grow (N1)."""
        while len(mapping) > cap:
            mapping.pop(next(iter(mapping)))
        return mapping

    def _save_state(self):
        write_json_atomic(
            thread_state_path(self.thread_id),
            {
                "thread_id": self.thread_id,
                "name": self.name,
                "shim_pid": os.getpid(),
                "tail": self.tail.state(),
                "delivered": list(self.delivered),
                "budgets": self._bound(self.budgets),
                "contacts": self._bound(self.contacts),
                "updated_at": now_iso(),
            },
            mode=0o600,
        )

    def _write_pidfile(self):
        """Take the exclusive flock that proves this shim owns the thread.

        Held for the process's whole life; the kernel releases it on exit,
        crash included, so nothing else can mistake a recycled pid for us.
        """
        path = thread_pid_path(self.thread_id)
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
                    raise SystemExit(
                        "another shim already owns thread %s" % self.thread_id
                    )
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode("utf-8"))
        # Deliberately not closed: closing would drop the lock.
        self._pidfile_fd = fd

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
        log("refusing a client: %d already in flight" % MAX_CONCURRENT_CLIENTS)
        return False

    def _handle_connection(self, conn):
        try:
            uid = peer_uid(conn)
            if uid != os.getuid():
                # S3: an unreadable peer uid is a refusal, not a shrug. Both
                # supported platforms answer (LOCAL_PEERCRED / SO_PEERCRED),
                # so "unavailable" means something is wrong, not permissive.
                log(
                    "refusing a client: peer uid %s is not %d"
                    % ("unavailable" if uid is None else uid, os.getuid())
                )
                return
            # S9: one deadline for the whole connection, not per recv, so a
            # client dribbling a byte at a time cannot hold a slot for ever.
            deadline = time.time() + CONN_TIMEOUT
            frames = 0
            buf = b""
            while not self.stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    log("client sent no complete line in %ds" % int(CONN_TIMEOUT))
                    return
                conn.settimeout(remaining)
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    log("client sent no complete line in %ds" % int(CONN_TIMEOUT))
                    return
                except OSError:
                    return
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    frames += 1
                    if frames > MAX_FRAMES_PER_CONNECTION:
                        log(
                            "dropping a client after %d frames on one connection"
                            % MAX_FRAMES_PER_CONNECTION
                        )
                        return
                    self._handle_line(line.decode("utf-8", "replace"))
                if len(buf) > MAX_TEXT_CHARS:
                    log("dropping a client whose line exceeds %d chars" % MAX_TEXT_CHARS)
                    return
            if buf.strip() and frames < MAX_FRAMES_PER_CONNECTION:
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
            log("ignoring a non-JSON line from a client")
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
        log("ignoring an unknown frame type %r" % ftype)

    def _handle_user(self, frame):
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        body, attrs = unwrap_message(content)
        if not body.strip():
            log("ignoring an empty inbound message")
            return

        from_field = frame.get("from") or attrs.get("from") or ""
        sock_path = from_field[4:] if from_field.startswith("uds:") else from_field
        sender = None
        if sock_path and socket_path_ok(sock_path):
            sender = claude_record_by_socket(sock_path)
        elif sock_path:
            log("ignoring a reply address outside the allowlisted directories")
            sock_path = ""

        sender_name = (sender or {}).get("name") or attrs.get("from-name")
        sender_sid = (sender or {}).get("sessionId") or attrs.get("from-session")
        if sender_sid:
            # Re-insert so the newest contact is last: _bound drops the oldest.
            self.contacts.pop(sender_sid, None)
            self.contacts[sender_sid] = {
                "name": sender_name,
                "socket": sock_path,
                "last_seen": now_iso(),
            }
            self._bound(self.contacts)

        tag = build_tag(sender_name, sender_sid, sock_path if sender else None)
        # The tag rides inside the same text Codex caps, so the body is trimmed
        # to leave room for it rather than pushing the whole message over.
        room = argv_text_budget() - len(tag) - 1
        trimmed_from = None
        if len(body) > room:
            log("truncating an inbound body of %d chars to %d" % (len(body), room))
            trimmed_from = len(body)
            body = body[:room]
        text = "%s\n%s" % (tag, body)

        held, _pid = thread_is_held(self.rollout_path, self.holder_pid)
        if not held:
            log("thread %s is no longer live; refusing to queue" % self.thread_id)
            self._status_back(
                frame, sender, "failed", "the Codex thread is no longer running"
            )
            self.stop.set()
            return

        # N6: the tail already knows the last boundary, so an inbound message
        # no longer rescans the whole rollout (194 MiB files exist).
        paused = self.tail.last_boundary == "aborted"
        try:
            codex_queue(self.thread_id, text)
        except QueueError as exc:
            log("queue failed: %s" % exc)
            self._status_back(frame, sender, "failed", str(exc))
            return
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
            log("thread %s is paused after an interrupt" % self.thread_id)
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
        if not sender:
            return
        deliver_to_record(
            sender,
            {
                "type": "control",
                "action": "peer_message_status",
                "orig_msg_id": frame.get("msg_id"),
                "status": status,
                "detail": detail,
                "from": "uds:%s" % self.sock_path,
            },
        )

    def _handle_control(self, frame):
        action = frame.get("action")
        if action != "notify_when_idle":
            log("ignoring control action %r" % action)
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
        if not socket_path_ok(sock_path):
            log("cannot answer notify_when_idle: %r is not an allowed socket" % sock_path)
            return
        rec = claude_record_by_socket(sock_path)
        if not rec:
            log("cannot answer notify_when_idle: no live session at %s" % sock_path)
            return
        deliver_to_record(
            rec,
            {
                "type": "control",
                "action": "peer_idle_notice",
                "orig_msg_id": msg_id,
                "state": "idle",
                "finished_at": now_ms(),
                "detail": detail,
            },
        )

    # -- outbound ----------------------------------------------------------

    def _poll_loop(self):
        last_live = time.time()
        while not self.stop.wait(self.poll_interval):
            self._consume_budget_marker()
            try:
                events = self.tail.poll()
            except Exception as exc:  # never let a bad line kill the shim
                log("rollout read failed: %s" % exc)
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
            if time.time() - last_live >= self.liveness_interval:
                last_live = time.time()
                held, _pid = thread_is_held(self.rollout_path, self.holder_pid)
                if not held:
                    log(
                        "codex pid %s no longer holds %s; exiting"
                        % (self.holder_pid, self.rollout_path)
                    )
                    self.stop.set()

    def _ensure_record(self):
        if os.path.exists(self.record_path):
            return
        if self.record_rewrites >= MAX_RECORD_REWRITES:
            return
        self.record_rewrites += 1
        log(
            "registry record was removed; rewriting (%d of %d)"
            % (self.record_rewrites, MAX_RECORD_REWRITES)
        )
        self._write_record()

    def _consume_budget_marker(self, initial=False):
        path = budget_reset_path(self.thread_id)
        if not os.path.exists(path):
            return
        try:
            os.unlink(path)
        except OSError:
            return
        self.budgets = {}
        if not initial:
            log("reply budget reset for thread %s" % self.thread_id)
            self._save_state()

    def _handle_turn_end(self, turn):
        if turn.turn_id in self.delivered:
            return
        self.delivered.append(turn.turn_id)

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
            log("turn %s aborted; nothing to deliver" % turn.turn_id)
            return
        text = strip_tag(turn.last_agent_message or "").strip()
        if not text:
            log("turn %s finished with no agent message" % turn.turn_id)
            return
        # S7: a turn that completed while no shim ran IS picked up from the
        # cursor on restart. Recent is useful; hours old is a surprise reply to
        # a conversation that moved on, so it is recorded and not posted.
        age = None if turn.completed_at is None else self.started_at - turn.completed_at
        if age is not None and age > RESTART_DELIVERY_WINDOW:
            log(
                "turn %s completed %.0fs before this shim started; recording it "
                "as delivered without posting" % (turn.turn_id, age)
            )
            return

        records = live_claude_records()
        targets = []

        tag = turn.tag or {}
        reply_socket = tag.get("reply")
        if reply_socket:
            if not socket_path_ok(reply_socket):
                log("reply address %r is not an allowed socket" % reply_socket)
            else:
                rec = claude_record_by_socket(reply_socket, records)
                if rec is None:
                    log("the session that queued turn %s is gone" % turn.turn_id)
                elif tag.get("sid") and rec.get("sessionId") != tag.get("sid"):
                    # Pids are reused and Claude's own sender guards do not run
                    # here, so the session id is re-checked before delivery.
                    log("session id at %s changed; not delivering" % reply_socket)
                else:
                    targets.append(rec)

        addressed = AT_NAME_RE.match(text.lstrip())
        if addressed:
            name = addressed.group(1)
            matches = claude_record_by_name(name, records)
            if not matches:
                log("no live Claude session named %r" % name)
            elif len(matches) > 1:
                log("%r names %d live sessions; not delivering" % (name, len(matches)))
            else:
                rec = matches[0]
                sid = rec.get("sessionId")
                allowed = sid in self.contacts or os.environ.get(
                    "SESSION_PEERS_ALLOW_UNSOLICITED"
                ) == "1"
                if allowed:
                    targets.append(rec)
                else:
                    log(
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
            if spent >= REPLY_BUDGET:
                log(
                    "reply budget of %d spent for session %s; dropping the reply "
                    "(peers.py budget reset %s to clear)"
                    % (REPLY_BUDGET, rec.get("name") or sid, self.thread_id)
                )
                continue
            try:
                body = build_cc_body(text, self.thread_id, self.name, self.sock_path)
            except ValueError as exc:
                log("cannot build a reply for turn %s: %s" % (turn.turn_id, exc))
                break
            if deliver_to_record(rec, build_user_frame(body, self.sock_path)):
                self.budgets[sid] = spent + 1
                # Deliberately no body text: the log is a delivery record, not
                # a transcript, and it lands in a file the user may share.
                log(
                    "delivered turn %s to %s"
                    % (turn.turn_id, rec.get("name") or sid)
                )
        self._save_state()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(args):
    warn_versions()
    records = live_claude_records()
    threads, schema_ok = codex_threads()
    registered = read_registered()
    claude = [
        {
            "pid": r.get("pid"),
            "sessionId": r.get("sessionId"),
            "name": r.get("name"),
            "cwd": r.get("cwd"),
            "status": r.get("status"),
            "version": r.get("version"),
            "kind": r.get("kind"),
            "entrypoint": r.get("entrypoint"),
            "socket": r.get("messagingSocketPath"),
        }
        for r in records
    ]
    codex = [
        {
            "id": t["id"],
            "name": t.get("name"),
            "cwd": t.get("cwd"),
            "updated_at": t.get("updated_at"),
            "registered": t["id"] in registered,
            "holder_pid": t.get("holder_pid"),
            "shim_pid": shim_pid(t["id"]),
        }
        for t in threads
        if t.get("live")
    ]
    payload = {
        "claude": claude,
        "codex": codex,
        "codex_schema_recognised": schema_ok,
        "socket_dir": default_socket_dir(),
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
    print("Codex threads (%d live):" % len(codex))
    for t in codex:
        print(
            "  %-24s %s  %s  codex pid %s%s"
            % (
                t["name"] or "(unnamed)",
                t["id"],
                "registered" if t["registered"] else "not registered",
                t["holder_pid"],
                ", shim %s" % t["shim_pid"] if t["shim_pid"] else "",
            )
        )
    if not schema_ok:
        print("  (thread discovery degraded: unknown state_*.sqlite schema)")
    return 0


def cmd_send(args):
    warn_versions()
    target = args.to
    if target.startswith("codex:"):
        return _send_codex(target[len("codex:") :], args)
    if target.startswith("cc:"):
        return _send_claude(target[len("cc:") :], args)
    sys.stderr.write("error: --to must start with codex: or cc:\n")
    return 2


def _send_codex(target, args):
    try:
        thread = resolve_thread(target)
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    degraded = thread.get("degraded")
    if degraded:
        log("liveness unverified: the Codex state schema is unknown")
    else:
        held, _pid = thread_is_held(thread["rollout_path"])
        if not held:
            sys.stderr.write(
                "error: no active session for thread %s; its process has exited "
                "(the queue would sit undrained)\n" % thread["id"]
            )
            return 1
        if thread_is_paused(thread["rollout_path"]):
            log(
                "thread %s is paused after an interrupt: the message is queued "
                "but drains only when its user types the next prompt" % thread["id"]
            )
    tag = build_tag(args.from_name, args.from_sid, args.from_socket)
    text = "%s\n%s" % (tag, args.message)
    try:
        codex_queue(thread["id"], text)
    except QueueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    print("queued to %s (%s)" % (thread.get("name") or thread["id"], thread["id"]))
    return 0


def _send_claude(name, args):
    matches = claude_record_by_name(name)
    if not matches:
        sys.stderr.write("error: no live Claude session named %r\n" % name)
        return 1
    if len(matches) > 1:
        sys.stderr.write(
            "error: %r names %d live sessions (%s); rename one\n"
            % (name, len(matches), ", ".join(str(m.get("pid")) for m in matches))
        )
        return 1
    rec = matches[0]
    if not socket_path_ok(rec.get("messagingSocketPath")):
        sys.stderr.write(
            "error: %s listens on %r, outside the allowlisted socket directories\n"
            % (name, rec.get("messagingSocketPath"))
        )
        return 1

    thread_id = args.from_thread
    thread_name = None
    shim_socket = None
    if thread_id:
        state = read_json(thread_state_path(thread_id), {}) or {}
        thread_name = state.get("name")
        pid = shim_pid(thread_id)
        if pid:
            rec_path = os.path.join(claude_sessions_dir(), "%d.json" % pid)
            shim_rec = read_json(rec_path, {}) or {}
            shim_socket = shim_rec.get("messagingSocketPath")
    try:
        body = build_cc_body(args.message, thread_id or "", thread_name, shim_socket)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    frame = build_user_frame(body, shim_socket)
    try:
        send_frame(rec["messagingSocketPath"], frame, auth_token=peer_token_for(rec))
    except OSError as exc:
        sys.stderr.write("error: could not reach %s: %s\n" % (name, exc))
        return 1
    print("sent to %s (pid %s)" % (name, rec.get("pid")))
    return 0


def shim_pid(thread_id):
    """The pid of the shim that PROVABLY owns this thread, or None.

    Ownership is the flock the shim holds on its own pidfile for its whole
    life, not the pid written in it: the kernel drops that lock when the
    process dies, however it died, so a recycled pid can neither be signalled
    by mistake nor block a restart (B2). The registry record is a second,
    weaker proof and is only used to catch a pidfile naming another thread.
    """
    path = thread_pid_path(thread_id)
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
    if not pid_alive(pid):
        return None
    rec = read_json(os.path.join(claude_sessions_dir(), "%d.json" % pid), None)
    if (
        isinstance(rec, dict)
        and rec.get("entrypoint") == "codex"
        and rec.get("sessionId") != thread_id
    ):
        log("the pidfile for %s names another thread's shim; ignoring" % thread_id)
        return None
    return pid


def cmd_shim(args):
    try:
        thread = resolve_thread(args.thread, require_live=False)
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    try:
        shim = Shim(thread)
    except NameError_ as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    return shim.run()


def spawn_shim(thread):
    """Daemonise one shim: new session, no inherited fds, log to a file.

    Codex's hook runner waits for inherited stdout/stderr pipes, so a child
    started from `session-hook` MUST detach exactly like this (PRD Facts).
    """
    script = os.path.realpath(__file__)
    log_path = thread_log_path(thread["id"])
    try:
        logfh = open(log_path, "a", encoding="utf-8")
    except OSError:
        logfh = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [sys.executable, script, "shim", "--thread", thread["id"]],
            stdin=subprocess.DEVNULL,
            stdout=logfh if logfh is not subprocess.DEVNULL else subprocess.DEVNULL,
            stderr=logfh if logfh is not subprocess.DEVNULL else subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        if logfh is not subprocess.DEVNULL:
            try:
                logfh.close()
            except OSError:
                pass
    # The SHIM writes and locks the pidfile once it is serving, so the file is
    # never a claim without a holder. Wait for it so a following reconcile
    # inside the same lock sees the shim rather than starting a second one.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        pid = shim_pid(thread["id"])
        if pid:
            return pid
        if proc.poll() is not None:
            log(
                "the shim for %s exited immediately (rc %s); see %s"
                % (thread["id"], proc.returncode, log_path)
            )
            return None
        time.sleep(0.05)
    log("the shim for %s did not report ready in 10s; see %s" % (thread["id"], log_path))
    return None


@contextlib.contextmanager
def reconcile_lock():
    """Serialise concurrent reconciles.

    The optional SessionStart hook fires on `startup`, `resume`, `clear` and
    `compact`, so several `up` processes can race. Without the lock each would
    see no pidfile and spawn its own shim for the same thread, and two shims
    on one thread means two registry records and a doubled reply.
    """
    path = os.path.join(state_dir(), "reconcile.lock")
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def reconcile(verbose=True):
    """Start one shim per registered, live thread. Idempotent by design."""
    with reconcile_lock():
        return _reconcile(verbose)


def _reconcile(verbose):
    registered = read_registered()
    if not registered:
        if verbose:
            print("no registered threads; run `peers.py up <name|uuid>` first")
        return 0
    threads, schema_ok = codex_threads()
    if not schema_ok:
        if verbose:
            print("thread discovery is unavailable; no shim can be started")
        return 0
    by_id = {t["id"]: t for t in threads}
    started = 0
    for tid in sorted(registered):
        thread = by_id.get(tid)
        if thread is None or not thread.get("live"):
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
    warn_versions()
    if args.target:
        try:
            thread = resolve_thread(args.target)
        except ResolveError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        try:
            register_thread(thread)
        except NameError_ as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        # D4: an explicit `up` is one of the two things that clears the budget.
        try:
            with open(budget_reset_path(thread["id"]), "w", encoding="utf-8") as fh:
                fh.write(now_iso() + "\n")
        except OSError:
            pass
        print("registered %s (%s)" % (thread.get("name") or thread["id"], thread["id"]))
    reconcile()
    return 0


def cmd_down(args):
    if args.target:
        try:
            thread = resolve_thread(args.target, require_live=False)
            tid = thread["id"]
        except ResolveError:
            tid = args.target if is_uuid(args.target) else None
            if tid is None:
                sys.stderr.write("error: no thread matches %r\n" % args.target)
                return 1
        stopped = stop_shim(tid)
        if unregister_thread(tid):
            print("unregistered %s%s" % (tid, " and stopped its shim" if stopped else ""))
        else:
            print("%s was not registered%s" % (tid, "; shim stopped" if stopped else ""))
        return 0
    ids = sorted(read_registered())
    for tid in ids:
        if stop_shim(tid):
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
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.unlink(thread_pid_path(thread_id))
    except (FileNotFoundError, OSError):
        pass
    return True


def cmd_budget(args):
    if args.budget_cmd != "reset":
        sys.stderr.write("error: the only budget subcommand is `reset`\n")
        return 2
    try:
        thread = resolve_thread(args.thread, require_live=False)
        tid = thread["id"]
    except ResolveError:
        if not is_uuid(args.thread):
            sys.stderr.write("error: no thread matches %r\n" % args.thread)
            return 1
        tid = args.thread
    with open(budget_reset_path(tid), "w", encoding="utf-8") as fh:
        fh.write(now_iso() + "\n")
    state_path = thread_state_path(tid)
    state = read_json(state_path, None)
    if isinstance(state, dict) and state.get("budgets"):
        state["budgets"] = {}
        write_json_atomic(state_path, state, mode=0o600)
    print("reply budget reset for %s" % tid)
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
    script = os.path.realpath(__file__)
    log_path = os.path.join(state_dir(), "session-hook.log")
    try:
        logfh = open(log_path, "a", encoding="utf-8")
        out = logfh
    except OSError:
        logfh = None
        out = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, script, "up"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        log("could not start the reconcile: %s" % exc)
    finally:
        if logfh is not None:
            try:
                logfh.write("session-hook: source=%s\n" % source)
                logfh.close()
            except OSError:
                pass
    print("{}")
    return 0


# --------------------------------------------------------------------------
# install-hook
# --------------------------------------------------------------------------


def hook_command():
    return "python3 %s session-hook" % os.path.realpath(__file__)


def hook_entry():
    return {
        "hooks": [
            {"type": "command", "command": hook_command(), "timeout": 10}
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


def cmd_install_hook(_args):
    path = os.path.join(codex_home(), "hooks.json")
    os.makedirs(codex_home(), exist_ok=True)
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
            "  %s\n" % (path, data, backup, json.dumps(hook_entry()))
        )
        return 1
    created = status == "absent"
    if not isinstance(data, dict):
        data = {}
    events, root = _hooks_event_map(data)
    entries = events.get("SessionStart")
    if not isinstance(entries, list):
        entries = []
    command = hook_command()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and hook.get("command") == command:
                print("SessionStart entry already installed in %s" % path)
                _ensure_features_hooks()
                _print_trust_step()
                return 0
    if not created:
        try:
            print("backed up %s to %s" % (path, backup_file(path)))
        except OSError as exc:
            sys.stderr.write("error: could not back up %s: %s\n" % (path, exc))
            return 1
    entries.append(hook_entry())
    events["SessionStart"] = entries
    # S6: keep the file's own mode; only a file we create gets 0600.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = 0o600
    write_json_atomic(path, root, mode=mode)
    print("added one SessionStart entry to %s" % path)
    _ensure_features_hooks()
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


def write_text_preserving_mode(path, text):
    """Replace a file's contents through a temp file, keeping its mode."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = 0o600
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _set_features_hooks_lines(lines):
    """(new_lines, changed): set `hooks = true` inside [features], once.

    An existing `hooks = false` is REWRITTEN, never shadowed by a second key:
    a duplicate key makes the file invalid TOML and Codex rejects it, and the
    old code appended one on every run (B3).
    """
    out = list(lines)
    start = None
    for i, line in enumerate(out):
        if line.strip() == "[features]":
            start = i
            break
    if start is None:
        if out and out[-1].strip():
            out.append("")
        out.append("[features]")
        out.append("hooks = true")
        return out, True
    end = len(out)
    for i in range(start + 1, len(out)):
        stripped = out[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = i
            break
    for i in range(start + 1, end):
        stripped = out[i].strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip().strip("\"'") == "hooks":
            if stripped == "hooks = true":
                return out, False
            out[i] = "hooks = true"
            return out, True
    out.insert(start + 1, "hooks = true")
    return out, True


def _ensure_features_hooks():
    """Set `[features] hooks = true`, rewriting an existing key in place."""
    path = codex_config_path()
    existed = os.path.exists(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        sys.stderr.write("error: could not read %s: %s\n" % (path, exc))
        return
    out, changed = _set_features_hooks_lines(lines)
    if not changed:
        return
    if existed:
        try:
            print("backed up %s to %s" % (path, backup_file(path)))
        except OSError as exc:
            sys.stderr.write("error: could not back up %s: %s\n" % (path, exc))
            return
    write_text_preserving_mode(path, "\n".join(out) + "\n")
    print("set [features] hooks = true in %s" % path)


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


def cmd_doctor(_args):
    warn_versions()
    lines = []

    def add(status, text):
        lines.append("%-5s %s" % (status, text))

    claude_v = tool_version("claude")
    codex_v = tool_version("codex")
    add(
        "warn" if claude_v and version_is_newer(claude_v, CLAUDE_CODE_TESTED) else "ok",
        "Claude Code %s (tested against %s)"
        % (claude_v.strip() if claude_v else "not on PATH", CLAUDE_CODE_TESTED),
    )
    add(
        "warn" if codex_v and version_is_newer(codex_v, CODEX_TESTED) else "ok",
        "Codex CLI %s (tested against %s)"
        % (codex_v.strip() if codex_v else "not on PATH", CODEX_TESTED),
    )
    add("ok" if codex_v else "fail", "codex on PATH")
    rc, _out, _err = run_cmd(["lsof", "-v"], timeout=10)
    add("ok" if rc != 127 else "fail", "lsof on PATH (thread liveness needs it)")

    add("ok", "CLAUDE_CONFIG_DIR: %s" % claude_config_dir())
    add("ok", "CODEX_HOME: %s" % codex_home())
    if codex_sqlite_home() != codex_home():
        add("ok", "codex sqlite_home: %s" % codex_sqlite_home())

    sock_dir = default_socket_dir()
    add(
        "ok" if dir_is_allowlisted(sock_dir) else "warn",
        "socket directory: %s%s"
        % (sock_dir, "" if os.path.isdir(sock_dir) else " (does not exist yet)"),
    )

    db = find_state_db()
    add(
        "ok" if db else "warn",
        "Codex state database: %s"
        % (db or "none with a recognised `threads` schema (send by UUID only)"),
    )

    registered = read_registered()
    threads, _ok = codex_threads()
    live_ids = {t["id"] for t in threads if t.get("live")}
    for tid in sorted(registered):
        name = registered[tid].get("name") or tid
        pid = shim_pid(tid)
        if pid:
            add("ok", "%s: shim running (pid %d)" % (name, pid))
        elif tid in live_ids:
            add("warn", "%s: thread is live but no shim (run `peers.py up`)" % name)
        else:
            add("ok", "%s: registered, thread not running" % name)
    if not registered:
        add("warn", "no registered threads (run `peers.py up <name|uuid>`)")

    hooks_path = os.path.join(codex_home(), "hooks.json")
    data = read_json(hooks_path, None)
    if data is None:
        add("ok", "no %s (the SessionStart hook is optional)" % hooks_path)
    else:
        events, _root = _hooks_event_map(data)
        entries = events.get("SessionStart") or []
        cfg = read_toml_lite(codex_config_path())
        found = False
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for j, hook in enumerate(entry.get("hooks") or []):
                if not isinstance(hook, dict) or hook.get("command") != hook_command():
                    continue
                found = True
                key = '%s:session_start:%d:%d' % (hooks_path, i, j)
                block = None
                for section, values in cfg.items():
                    if section.startswith("hooks.state.") and key in section:
                        block = values
                        break
                if block is None:
                    add(
                        "warn",
                        "SessionStart entry present but no trust block; open "
                        "/hooks in Codex to trust it",
                    )
                elif block.get("enabled") is False:
                    add("warn", "SessionStart entry is disabled in config.toml")
                else:
                    add(
                        "ok",
                        "SessionStart entry present with a trust block "
                        "(entry sha256 %s; Codex's trusted_hash algorithm is "
                        "undocumented, so trust is unknown here, open /hooks)"
                        % entry_hash(entry)[:12],
                    )
        if not found:
            add("ok", "no session-peers SessionStart entry (optional)")
        features_on = cfg.get("features", {}).get("hooks") is True
        add(
            "ok" if features_on else "warn",
            "[features] hooks = %s in config.toml" % ("true" if features_on else "unset"),
        )

    print("\n".join(lines))
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

    p_send = sub.add_parser("send", help="send one message in either direction")
    p_send.add_argument(
        "--to", required=True, metavar="codex:<name|uuid>|cc:<name>", help="the peer"
    )
    p_send.add_argument("--message", required=True, help="the message body")
    p_send.add_argument(
        "--from-thread", metavar="UUID", help="the Codex thread sending (cc: targets)"
    )
    p_send.add_argument("--from-name", metavar="NAME", help="sender name for the tag")
    p_send.add_argument("--from-sid", metavar="ID", help="sender session id for the tag")
    p_send.add_argument(
        "--from-socket", metavar="PATH", help="sender socket for the reply tag"
    )
    p_send.set_defaults(func=cmd_send)

    p_shim = sub.add_parser("shim", help="run as one Codex thread's peer (foreground)")
    p_shim.add_argument("--thread", required=True, metavar="UUID")
    p_shim.set_defaults(func=cmd_shim)

    p_up = sub.add_parser("up", help="register a thread and start its shim")
    p_up.add_argument("target", nargs="?", metavar="name|uuid")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop a shim; with a target, unregister it")
    p_down.add_argument("target", nargs="?", metavar="name|uuid")
    p_down.set_defaults(func=cmd_down)

    p_budget = sub.add_parser("budget", help="reply budget maintenance")
    bsub = p_budget.add_subparsers(dest="budget_cmd")
    p_reset = bsub.add_parser("reset", help="clear a thread's reply budget")
    p_reset.add_argument("thread", metavar="name|uuid")
    p_reset.set_defaults(func=cmd_budget)
    p_budget.set_defaults(func=cmd_budget, budget_cmd=None, thread=None)

    p_hook = sub.add_parser("session-hook", help="Codex SessionStart entry point")
    p_hook.set_defaults(func=cmd_session_hook)

    p_install = sub.add_parser("install-hook", help="add the SessionStart entry")
    p_install.set_defaults(func=cmd_install_hook)

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
