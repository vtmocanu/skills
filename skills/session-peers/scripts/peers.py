#!/usr/bin/env python3
"""Cross-session messaging between Claude Code sessions and Codex CLI threads.

One script, one CLI. See PRD #44 and ``../references/spike-checklist.md`` for the
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
    peers.py reply --request <uuid> (--message <text>|--message-file <path>)
                   [--json]
    peers.py wait --for cc:<name|uuid>|buddy [--state idle|busy] [--timeout <seconds>]
                  [--json]
    peers.py shim --thread <uuid>
    peers.py up [<name|uuid>]
    peers.py down [<name|uuid>]
    peers.py budget reset <name|uuid|buddy>
    peers.py budget allow <name|uuid|buddy> --replies N [--for-session <uuid>]
                          [--as cc:<uuid>|codex:<uuid>]
    peers.py buddy [show|ping|clear] [--as cc:<uuid>|codex:<uuid>] [--json]
    peers.py buddy set [cc:|codex:|@]<name|uuid> [--uses a,b]
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
# `budget allow` may raise one requester's cap to at most this many replies.
BUDGET_ALLOW_MAX = 20
# What a running shim's code supports, saved in its own state file (never the
# vendor-read registry record). A shim keeps the code it started with, so a
# command whose marker only newer shims consume checks this first.
SHIM_FEATURES = ["budget_allow"]
REPLY_BUDGET_WINDOW_DEFAULT = 30 * 60.0
REQUEST_TIMEOUT_DEFAULT = 10 * 60.0
REQUEST_TIMEOUT_MAX = 60 * 60.0
REQUEST_POLL_INTERVAL = 0.1
WAIT_POLL_INTERVAL_DEFAULT = 1.0
REQUEST_ORPHAN_TTL = 60.0
VERSION_WARNING_WINDOW = 24 * 60 * 60.0

# Columns the `threads` table must have for the schema to count as recognised.
THREADS_COLUMNS = frozenset({"id", "rollout_path", "cwd", "name", "updated_at"})

TAG_PREFIX = "[session-peers"
TAG_RE = re.compile(
    r"^\[session-peers from=@(?P<from>\S*) sid=(?P<sid>\S*)"
    r"(?: mid=(?P<mid>\S*))? reply=(?P<reply>.*)\]$"
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
MAX_TAG_FIELD_CHARS = 256

# Built rather than written literally so this file has no stray triple
# quotes; _scan_multiline compares against them.
TRIPLE_DQ = '"' * 3
TRIPLE_SQ = "'" * 3

# A TOML bare key needs no quoting in a table header.
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# `[features] # flags` is a valid TOML header; `# [features]` is a comment.
TOML_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")

# A peer name reaches Claude inside a wrapper attribute and inside the tag line,
# so it is restricted at the door rather than escaped at every use (B1).
PEER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Substitute for the "<" of a wrapper tag appearing inside a body. Printable and
# visible in a transcript, unlike a zero-width character.
LT_SUBSTITUTE = "\u2039"
WRAPPER_MARKUP_RE = re.compile(r"<(/?)(cross-session-message)", re.IGNORECASE)
REQUEST_MARKUP_RE = re.compile(r"<(/?)(session-peers-request)", re.IGNORECASE)
C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Tunables, read at shim start. The tests turn them down so a fixture rollout is
# picked up in milliseconds rather than seconds.
POLL_INTERVAL_DEFAULT = 1.0
LIVENESS_INTERVAL_DEFAULT = 5.0
ALIAS_REFRESH_INTERVAL_DEFAULT = 30.0
CONN_TIMEOUT = 30.0
MAX_RECORD_REWRITES = 2
PROCESSED_TURN_HISTORY = 200
CONTACT_HISTORY = 200
MAX_CONCURRENT_CLIENTS = 8
MAX_FRAMES_PER_CONNECTION = 16
READ_CHUNK = 1024 * 1024
# A rollout line longer than this is not a Codex turn (its own text cap is 1
# MiB): skip it rather than buffer it, so one damaged file cannot exhaust RAM.
MAX_ROLLOUT_LINE = 8 * 1024 * 1024
# S7: a completion older than this at shim start is recorded, never posted.
RESTART_DELIVERY_WINDOW = 900.0
GC_DAYS_DEFAULT = 7.0
# Per-thread files in state_dir(), named <thread uuid><suffix>. GC owns them.
THREAD_ARTIFACT_SUFFIXES = (".json", ".log", ".pid", ".budget-reset", ".budget-allow")

# What a buddy is for. Advisory scope only: nothing grants a permission from it.
BUDDY_USES = ("review", "brainstorm", "second-opinion", "co-steer", "ping", "sanity-check")
BUDDY_KINDS = ("cc", "codex")

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
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        pass
    else:
        return numeric / 1000.0 if numeric > 1e11 else numeric
    text = text.replace("Z", "+00:00")
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


def write_json_exclusive(path, data, mode=0o600):
    """Create one small JSON file exactly once; return False if it exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def message_from_args(args):
    """Read one CLI message source and enforce the shared character bound."""
    value = getattr(args, "message", None)
    path = getattr(args, "message_file", None)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read()
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read message file %s: %s" % (path, exc))
    if value is None:
        raise ValueError("one of --message or --message-file is required")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(
            "message is %d characters, over the %d cap"
            % (len(value), MAX_TEXT_CHARS)
        )
    size = utf8_len(value)
    if size > MAX_TEXT_CHARS:
        raise ValueError(
            "message is %d UTF-8 bytes, over the %d cap"
            % (size, MAX_TEXT_CHARS)
        )
    return value


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


def run_cmd(argv, timeout=10, env=None, cwd=None):
    """Run a command, returning (rc, stdout, stderr). A missing binary is rc 127."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            cwd=cwd,
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


def stable_dir(preferred=None):
    """The first existing directory of `preferred`, `$HOME`, `/`.

    A detached shim must not depend on the directory of whoever started it:
    `codex queue` resolves its config from the working directory and fails on
    a deleted one (a removed git worktree, say).
    """
    for candidate in (preferred, os.path.expanduser("~"), "/"):
        if candidate and os.path.isdir(candidate):
            return candidate
    return "/"


def spawn_detached(argv, log_path):
    """Double-fork and exec, returning the daemon pid with no Popen handle.

    The intermediate child is reaped immediately; the daemon is adopted by the
    OS. This is the standard detach pattern for the supported macOS/Linux
    platforms and avoids Python 3.14 ResourceWarnings from abandoning a live
    ``Popen`` object.
    """
    read_fd, write_fd = os.pipe()
    try:
        child = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if child == 0:
        os.close(read_fd)
        try:
            os.setsid()
            os.chdir(stable_dir())
            daemon = os.fork()
            if daemon > 0:
                os.write(write_fd, ("%d\n" % daemon).encode("ascii"))
                os._exit(0)
            os.close(write_fd)
            null_in = os.open(os.devnull, os.O_RDONLY)
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(null_in, 0)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)
            os.closerange(3, safe_open_max())
            os.execv(argv[0], argv)
        except BaseException as exc:
            try:
                os.write(2, ("detached exec failed: %s\n" % exc).encode("utf-8"))
            except OSError:
                pass
            os._exit(127)
    os.close(write_fd)
    try:
        raw = os.read(read_fd, 64).strip()
    finally:
        os.close(read_fd)
        os.waitpid(child, 0)
    if not raw:
        raise OSError("detached child did not report its pid")
    return int(raw)


def safe_open_max():
    """A usable exclusive closerange ceiling even when sysconf returns -1."""
    try:
        value = int(os.sysconf("SC_OPEN_MAX"))
    except (OSError, TypeError, ValueError):
        return 256
    return value if value >= 3 else 256


def tool_version(binary):
    """`<binary> --version` output, or None. Never raises, never fails a run."""
    rc, out, err = run_cmd([binary, "--version"], timeout=10)
    if rc != 0:
        return None
    return (out or err).strip() or None


def warn_versions(kinds=("claude", "codex")) -> None:
    """D11: warn once per installed/pinned pair per day, never fail a run."""
    previous = read_json(version_warning_path(), {}) or {}
    if not isinstance(previous, dict):
        previous = {}
    now = time.time()
    changed = False

    def warn_once(key, message):
        nonlocal changed
        try:
            last = float(previous.get(key, 0))
        except (TypeError, ValueError):
            last = 0
        if now - last < VERSION_WARNING_WINDOW:
            return
        log(message)
        previous[key] = now
        changed = True

    if "claude" in kinds:
        v = tool_version("claude")
        if v and version_is_newer(v, CLAUDE_CODE_TESTED):
            warn_once(
                "claude:%s>%s" % (v.strip(), CLAUDE_CODE_TESTED),
                "Claude Code %s is newer than the tested %s; if peers stop "
                "appearing, re-run references/spike-checklist.md"
                % (v.strip(), CLAUDE_CODE_TESTED)
            )
    if "codex" in kinds:
        v = tool_version("codex")
        if v and version_is_newer(v, CODEX_TESTED):
            warn_once(
                "codex:%s>%s" % (v.strip(), CODEX_TESTED),
                "Codex CLI %s is newer than the tested %s; if discovery breaks, "
                "re-run references/spike-checklist.md" % (v.strip(), CODEX_TESTED)
            )
    if changed:
        try:
            write_json_atomic(version_warning_path(), previous)
        except OSError:
            pass


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


def _toml_key_text(part):
    """One dotted-path segment as TOML would write it in a table header."""
    return part if BARE_KEY_RE.match(part) else '"%s"' % part


def _flatten_toml(data):
    """A parsed TOML document in the shape read_toml_lite returns.

    {"": root scalars, "features": {...}, 'hooks.state."<k>"': {...}}, so the
    two readers are interchangeable for every caller.
    """
    out = {}

    def walk(prefix, table):
        scalars = {}
        for key, value in table.items():
            if isinstance(value, dict):
                walk(prefix + [key], value)
            else:
                scalars[key] = value
        out[".".join(_toml_key_text(p) for p in prefix)] = scalars

    walk([], data)
    out.setdefault("", {})
    return out


def read_toml_lite(path):
    """Return {section_header: {key: value}} with "" for the root table.

    R3: a real parser reads the file where one exists, because a line reader
    cannot tell a key from the same text inside a multiline string. The line
    reader stays as the fallback for 3.9 and 3.10 and for a file that does not
    parse, where reading something beats reading nothing.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"": {}}
    except OSError as exc:
        log("ignoring unreadable %s: %s" % (path, exc))
        return {"": {}}
    tomllib = _load_tomllib()
    if tomllib is not None:
        try:
            return _flatten_toml(tomllib.loads(text))
        except Exception as exc:
            # A partial line-by-line read of an invalid file is worse than no
            # read: it could route the database off a key Codex never honours,
            # because Codex refuses the same file outright.
            log(
                "%s does not parse as TOML (%s); Codex would refuse it too, so "
                "the bridge is using environment and default paths" % (path, exc)
            )
            return {"": {}}
    # No tomllib (3.9, 3.10): the line reader is the only reader there is.
    return read_toml_lite_text(text)


def read_toml_lite_text(text):
    """The line-reader fallback.

    R3: lines inside a multiline string are skipped, and the active table name
    is normalised so `["features"]` stores its keys under `features`.
    """
    out = {"": {}}
    lines = text.splitlines()
    inside = _line_states(lines)
    section = ""
    for i, line in enumerate(lines):
        if inside[i]:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = TOML_HEADER_RE.match(line)
        if header:
            name = header.group(1).strip()
            if name.startswith("[") and name.endswith("]"):
                name = name[1:-1].strip()  # array of tables
            section = _unquote_table_name(name)
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
    """config.toml `sqlite_home`, else CODEX_SQLITE_HOME, else CODEX_HOME.

    P6: the configured value wins, matching Codex's own resolver, and only a
    TOP-LEVEL key counts. A `sqlite_home` inside another table belongs to that
    table, and taking it would point the bridge at the wrong database.
    """
    cfg = read_toml_lite(codex_config_path())
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


def buddies_dir():
    path = os.path.join(state_dir(), "buddies")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def buddy_path(owner):
    if owner.get("kind") not in BUDDY_KINDS or not is_uuid(owner.get("uuid")):
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
    if not is_uuid(request_id):
        raise ValueError("request id must be a UUID")
    return os.path.join(request_dir(), "%s.request.json" % request_id)


def request_reply_path(request_id):
    if not is_uuid(request_id):
        raise ValueError("request id must be a UUID")
    return os.path.join(request_dir(), "%s.reply.json" % request_id)


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
    rc, out, err = run_cmd(
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


def record_liveness(rec) -> str:
    """Pid alive, pidDomain equal, procStart equal to `ps -o lstart=`.

    Returns ``live``, ``dead`` or ``unverified``. A sandbox can deny ``ps``
    while the process and socket are healthy; collapsing that denial into
    ``dead`` made ``list`` and ``send`` falsely claim no Claude session existed.
    A missing process-start value is unverified because it cannot rule out PID
    reuse; a present value must match when the probe is available.
    """
    pid = rec.get("pid")
    if not isinstance(pid, int) or not pid_alive(pid):
        return "dead"
    domain = rec.get("pidDomain")
    if domain is not None and domain != pid_domain():
        return "dead"
    recorded = rec.get("procStart")
    if not recorded:
        return "unverified"
    actual, error = proc_start_checked(pid)
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
    if is_uuid(target):
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
    rc, stdout, stderr = run_cmd(
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
        log("Codex thread liveness is unavailable: %s" % error)
    return holders


def writer_lock_path(thread_id, home=None):
    """The per-thread writer lock a live Codex process holds, under
    `<CODEX_HOME>/thread-writer-locks/<uuid>.lock`.

    It is a more reliable liveness signal than the rollout file: Codex writes
    the rollout lazily, so a just-created or renamed thread can be live with
    the lock held and no rollout on disk yet. The file can appear DURING its
    first turn (verified on codex-cli 0.153.4, 2026-09-08). An older
    Codex that never creates the lock simply contributes no holder here, and
    the rollout stays the signal.

    Rooted at CODEX_HOME, where Codex keeps both the lock and the session
    rollouts, NOT at `sqlite_home`: the state DB can be relocated with
    `sqlite_home` while the locks and rollouts stay under CODEX_HOME, so rooting
    the lock at `sqlite_home` would probe the wrong directory when they differ.
    """
    root = home or codex_home()
    return os.path.join(root, "thread-writer-locks", "%s.lock" % thread_id)


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
                # session_index.jsonl is title-specific and appends on every
                # /rename. Prefer it over the threads row, whose name can lag.
                "name": index.get(tid) or row[1],
                "rollout_path": row[2],
                "cwd": row[3],
                "updated_at": row[4],
                "registered": tid in registered,
                "holder_pid": None,
                "live": False,
                "liveness_error": None,
            }
        )
    if check_live and threads:
        # The lock is rooted at CODEX_HOME, not at the state DB's home: the DB
        # can live under a separate `sqlite_home` while the locks stay put.
        lock_of = {t["id"]: writer_lock_path(t["id"]) for t in threads}
        probe = [t["rollout_path"] for t in threads if t["rollout_path"]]
        probe += list(lock_of.values())
        holders, verified, liveness_error = lsof_holders_checked(probe)
        for t in threads:
            if not verified:
                t["live"] = None
                t["liveness_error"] = liveness_error
                continue
            # Either handle a live Codex process keeps proves the thread is
            # live; the lock covers a fresh thread whose rollout is not written
            # yet, the rollout covers an older Codex with no writer lock.
            found = (holders.get(canon_path(t["rollout_path"])) or []) or (
                holders.get(canon_path(lock_of[t["id"]])) or []
            )
            if found:
                t["live"] = True
                t["holder_pid"] = found[0][0]
    return threads, True


def thread_is_held(rollout_path, holder_pid=None, lock_path=None):
    """(True/False/None, pid); None means the lsof probe was unavailable.

    Liveness comes from either handle a live Codex process keeps: the rollout
    file, or the writer lock (`lock_path`). The lock is held from thread
    creation, while the rollout is written lazily on the first completed turn
    (measured on codex-cli 0.153.4), so a just-created thread reads as live
    through the lock alone. With `holder_pid` given, that exact pid must still
    hold one of them (D2: a live daemon can unload one thread while staying
    alive).
    """
    paths = [p for p in (rollout_path, lock_path) if p]
    holders, verified, _error = lsof_holders_checked(paths)
    if not verified:
        return None, None
    found = []
    for p in paths:
        found += holders.get(canon_path(p)) or []
    if holder_pid is None:
        return bool(found), (found[0][0] if found else None)
    for pid, _cmd in found:
        if pid == holder_pid:
            return True, pid
    return False, (found[0][0] if found else None)


class ResolveError(Exception):
    """A thread target that cannot be turned into exactly one live thread."""


class ResolveNotFound(ResolveError):
    """Nothing carries that name or id (as opposed to ambiguous or unverified)."""


class ResolveNoLive(ResolveError):
    """Only threads whose process is verified gone carry that name."""


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
        raise ResolveNotFound("no Codex thread with id %s" % target)
    # Match the name from the state DB / session index, OR from our own
    # registration: `up <uuid>` records name->uuid, and a later `/rename` may
    # not have propagated to the DB's `name` column yet (measured on
    # codex-cli 0.153.4), so a thread we already registered under this name
    # must still resolve. Union by id, so a thread matched both ways counts once.
    reg = read_registered()
    reg_ids = {
        tid
        for tid, meta in reg.items()
        if isinstance(meta, dict) and meta.get("name") == target
    }
    matches = [t for t in threads if t.get("name") == target or t["id"] in reg_ids]
    # The peer list advertises UUID-derived aliases for unsafe or absent titles.
    # Check aliases alongside exact names: if they identify different threads,
    # refuse the collision instead of silently sending to either one.
    prefix_target = target[6:] if target.startswith("codex-") else target
    prefix = None
    if is_uuid(prefix_target):
        prefix = prefix_target.replace("-", "").lower()
    elif re.fullmatch(r"[0-9a-fA-F]{8,32}", prefix_target):
        prefix = prefix_target.lower()
    if prefix:
        prefix_matches = [
            t for t in threads
            if t["id"].replace("-", "").lower().startswith(prefix)
        ]
        matched_ids = {t["id"] for t in matches}
        matches.extend(t for t in prefix_matches if t["id"] not in matched_ids)
    if not matches:
        raise ResolveNotFound(
            "no Codex thread named %r or matching that ID prefix; run `peers.py list` "
            "for current UUIDs, or /rename it in the TUI"
            % target
        )
    if require_live:
        live = [t for t in matches if t["live"] is True]
        if not live:
            unverified = [t for t in matches if t["live"] is None]
            if unverified:
                detail = unverified[0].get("liveness_error") or "lsof failed"
                raise ResolveError(
                    "Codex thread liveness is unavailable (%s); retry where "
                    "lsof is permitted" % detail
                )
            # Never silently pick a thread whose process is gone: Codex's title
            # suggester reuses names, so a dead match is not evidence of intent.
            raise ResolveNoLive(
                "no live Codex thread named %r (%d past thread(s) carried that "
                "name); /rename the running one, or pass its UUID"
                % (target, len(matches))
            )
        matches = live
    if len(matches) > 1:
        candidates = ", ".join(
            "%s (%s)" % (t["id"], t.get("name") or "unnamed")
            for t in matches
        )
        raise ResolveError(
            "%r matches %d %sthreads (%s); register by UUID instead"
            % (target, len(matches), "live " if require_live else "", candidates)
        )
    return matches[0]


def resolve_thread_prefer_live(target):
    """Resolve for commands that also act on a stopped thread (`budget`, `down`).

    Codex's title suggester reuses names, so a bare name usually also matches
    past threads; resolving across dead threads first made a unique LIVE name
    ambiguous. Prefer the live match; only when there is none, fall back to any
    thread with that name. Raises the more specific ResolveError otherwise.
    """
    try:
        return resolve_thread(target, require_live=True)
    except ResolveError as live_error:
        try:
            return resolve_thread(target, require_live=False)
        except ResolveError:
            raise live_error


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
    # P9: a read-modify-write on one shared file, so it runs under the lock the
    # reconcile uses. Two `up` calls at once would otherwise lose one.
    with reconcile_lock():
        threads = read_registered()
        threads[thread["id"]] = {"name": name, "registered_at": now_iso()}
        write_registered(threads)


def refresh_registered_name(thread_id, name):
    """Refresh the cached alias for a persistently registered UUID."""
    require_peer_name(name, "peer alias")
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


def _bridge_thread_ids():
    """UUIDs represented by registrations or per-thread bridge artifacts."""
    out = {thread_id for thread_id in read_registered() if is_uuid(thread_id)}
    try:
        names = os.listdir(state_dir())
    except OSError:
        return out
    for name in names:
        for suffix in THREAD_ARTIFACT_SUFFIXES:
            if not name.endswith(suffix):
                continue
            candidate = name[: -len(suffix)]
            if is_uuid(candidate):
                out.add(candidate)
            break
    return out


def _thread_last_seen(thread_id, registered, threads):
    """Latest trustworthy activity timestamp for one bridge thread."""
    seen = []
    meta = registered.get(thread_id)
    if isinstance(meta, dict):
        for key in ("last_seen_at", "registered_at"):
            value = parse_time(meta.get(key))
            if value is not None:
                seen.append(value)
    state = read_json(thread_state_path(thread_id), {}) or {}
    value = parse_time(state.get("updated_at")) if isinstance(state, dict) else None
    if value is not None:
        seen.append(value)
    thread = threads.get(thread_id)
    if thread is not None:
        value = parse_time(thread.get("updated_at"))
        if value is not None:
            seen.append(value)
    for suffix in THREAD_ARTIFACT_SUFFIXES:
        path = os.path.join(state_dir(), thread_id + suffix)
        try:
            seen.append(os.stat(path).st_mtime)
        except OSError:
            pass
    return max(seen) if seen else None


def gc_bridge_state(days=GC_DAYS_DEFAULT, dry_run=False, verbose=True):
    """Prune exact bridge-owned artifacts for inactive threads older than days.

    Codex rollouts, writer locks and queued messages are outside ``state_dir``
    and are never touched. Persistent manual registrations are bridge metadata
    and intentionally expire too. Unknown discovery fails closed because a
    thread must be proven inactive before any metadata is removed.
    """
    if days < 0:
        raise ValueError("retention days must be zero or greater")
    threads, schema_ok = codex_threads()
    if not schema_ok:
        if verbose:
            print("GC skipped: Codex thread discovery is unavailable")
        return []
    if any(thread.get("live") is None for thread in threads):
        if verbose:
            print("GC skipped: Codex liveness is unverified")
        return []
    by_id = {thread["id"]: thread for thread in threads}
    registered = read_registered()
    cutoff = time.time() - days * 86400.0
    candidates = []
    for thread_id in sorted(_bridge_thread_ids()):
        thread = by_id.get(thread_id)
        if thread is not None and thread.get("live"):
            continue
        if shim_pid(thread_id):
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
    with reconcile_lock():
        # Recheck after taking the same lock used by attach/up/down. A session
        # that resumed while the first scan ran must win over GC.
        current, current_ok = codex_threads()
        if not current_ok:
            return []
        if any(thread.get("live") is None for thread in current):
            return []
        current_by_id = {thread["id"]: thread for thread in current}
        live_ids = {thread["id"] for thread in current if thread.get("live")}
        registrations = read_registered()
        for thread_id in candidates:
            if thread_id in live_ids or shim_pid(thread_id):
                continue
            last_seen = _thread_last_seen(
                thread_id, registrations, current_by_id
            )
            if last_seen is None or last_seen > cutoff:
                continue
            failed = False
            for suffix in THREAD_ARTIFACT_SUFFIXES:
                path = os.path.join(state_dir(), thread_id + suffix)
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    failed = True
                    log("could not prune %s: %s" % (path, exc))
            if failed:
                continue
            registrations.pop(thread_id, None)
            removed.append(thread_id)
        write_registered(registrations)
    if verbose:
        for thread_id in removed:
            print("pruned %s" % thread_id)
    return removed


# --------------------------------------------------------------------------
# The tag line (D4)
# --------------------------------------------------------------------------


def build_tag(from_name=None, sid=None, reply_socket=None, msg_id=None) -> str:
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
        return C0_RE.sub("", value)[:MAX_TAG_FIELD_CHARS] or TAG_ABSENT

    reply = (reply_socket or "").strip()
    return "[session-peers from=@%s sid=%s mid=%s reply=%s]" % (
        field(from_name),
        field(sid),
        field(msg_id),
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
    for key in ("from", "sid", "mid", "reply"):
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


def codex_title_owner(thread_name, threads=None):
    """Lowest live UUID for a title, providing a stable duplicate tiebreak."""
    if not valid_peer_name(thread_name):
        return None
    if threads is None:
        threads, schema_ok = codex_threads()
        if not schema_ok:
            return None
    owners = sorted(
        thread["id"]
        for thread in threads
        if thread.get("live") and thread.get("name") == thread_name
    )
    return owners[0] if owners else None


def peer_name_for_thread(
    thread_name, thread_id, records=None, title_owner=None
):
    """Choose a safe, unique peer alias for a mutable Codex title.

    A valid title is used verbatim. Unnamed, unsafe or conflicting titles fall
    back to a UUID-derived alias rather than preventing the SessionStart hook
    from attaching the thread. The full UUID fallback makes a collision
    deterministic and vanishingly unlikely without silently slugifying a title.
    """
    records = live_claude_records() if records is None else records
    occupied = {
        rec.get("name")
        for rec in records
        if rec.get("sessionId") != thread_id and rec.get("name")
    }
    candidates = []
    if valid_peer_name(thread_name) and title_owner in (None, thread_id):
        candidates.append(str(thread_name))
    candidates.extend(
        ["codex-%s" % thread_id[:8], "codex-%s" % thread_id]
    )
    for candidate in candidates:
        if valid_peer_name(candidate) and candidate not in occupied:
            return candidate
    raise NameError_("no unique peer alias is available for thread %s" % thread_id)


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


def neutralise_request_markup(body) -> str:
    """Stop request content closing or forging its correlation envelope."""
    return REQUEST_MARKUP_RE.sub(LT_SUBSTITUTE + r"\1\2", body or "")


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


def reply_text(text, msg_id, held_reply=False):
    """Prefix a reply with the id of the request it answers.

    `msg_id` is the requester's own SendMessage msg_id (carried in the turn
    tag), so a Claude session can tell which of its messages a reply answers
    when several crossed. No id, no prefix.
    """
    if not msg_id:
        return text
    label = "held reply, in reply to message" if held_reply else "in reply to message"
    return "[%s %s]\n%s" % (label, msg_id, text)


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
            return
        if size < self.cursor:
            # Truncated or rotated underneath us: resync rather than replay.
            log("%s shrank; resyncing the cursor to EOF" % self.path)
            self.cursor = size
            return
        if size == self.cursor:
            return
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
                            yield part.decode("utf-8", "replace")
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

    def poll(self, emit_events=True):
        """Read turn state and, normally, emit its ordered start/end events.

        First startup passes emit_events=False to recover the open request
        without replaying completed replies. The cursor and pending sender
        come from the same bounded read, including a partial trailing line.
        """
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
                    if emit_events:
                        events.append(Event("start", turn_id, None))
                elif ptype in ("task_complete", "turn_aborted"):
                    turn_id = payload.get("turn_id")
                    info = self.pending.pop(turn_id, {"tag": None, "text": ""})
                    if self.open_turn == turn_id:
                        self.open_turn = None
                    outcome = "complete" if ptype == "task_complete" else "aborted"
                    self.last_boundary = outcome
                    if not emit_events:
                        continue
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


def utf8_len(text) -> int:
    return len(text.encode("utf-8"))


def truncate_utf8(text, max_bytes):
    """Trim `text` to at most `max_bytes` UTF-8 bytes, on a character boundary.

    P5: the budget is a byte budget, so cutting by characters overshoots on any
    non-ASCII text, and cutting by bytes alone can split a code point.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 0:
        return ""
    return encoded[:max_bytes].decode("utf-8", "ignore")


def env_bytes():
    """UTF-8 bytes the environment occupies in the exec budget."""
    return sum(utf8_len(k) + utf8_len(v) + 2 for k, v in os.environ.items())


def argv_text_budget():
    """BYTES that `codex queue --message <text>` can actually carry.

    The message is one argv element, so the real ceiling is ARG_MAX minus the
    environment, not Codex's MAX_USER_INPUT_TEXT_CHARS. On macOS both are
    1048576, so a message at Codex's own cap never reaches it: the exec fails
    with E2BIG (measured 2026-09-07, `getconf ARG_MAX` = 1048576).
    """
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError, AttributeError):
        return MAX_TEXT_CHARS
    budget = arg_max - env_bytes() - 8192
    if sys.platform.startswith("linux"):
        # Linux caps ONE argv element at MAX_ARG_STRLEN (32 pages), far below
        # ARG_MAX; macOS has no separate per-argument limit.
        try:
            page = os.sysconf("SC_PAGESIZE")
        except (ValueError, OSError, AttributeError):
            page = 4096
        budget = min(budget, 32 * page - 1024)
    return max(4096, min(MAX_TEXT_CHARS, budget))


def codex_queue(thread_id, text, cwd=None):
    """`codex queue --thread <uuid> --message <text>`, run in `stable_dir(cwd)`.

    rc != 0, or "No active session" on stderr, means the thread is not live.
    Pass the thread's own cwd; a deleted one falls back to `$HOME`.
    """
    budget = argv_text_budget()
    size = utf8_len(text)
    if size > budget:
        raise QueueError(
            "message is %d bytes, over the %d cap this machine can pass to "
            "`codex queue` (Codex itself stops at %d characters)"
            % (size, budget, MAX_TEXT_CHARS)
        )
    if len(text) > MAX_TEXT_CHARS:
        raise QueueError(
            "message is %d characters, over Codex's %d cap"
            % (len(text), MAX_TEXT_CHARS)
        )
    rc, out, err = run_cmd(
        ["codex", "queue", "--thread", str(thread_id), "--message", text],
        timeout=60,
        cwd=stable_dir(cwd),
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
        self.lock_path = writer_lock_path(self.thread_id)
        self.holder_pid = thread.get("holder_pid")
        self.thread_name = thread.get("name")
        # B1: only a validated alias reaches Claude's wrapper. SessionStart can
        # attach before a title exists, and Codex-generated titles often carry
        # spaces, so an unusable title gets a UUID-derived alias.
        self.name = peer_name_for_thread(
            self.thread_name,
            self.thread_id,
            title_owner=codex_title_owner(self.thread_name),
        )
        self.cwd = thread.get("cwd") or stable_dir()

        self.sock_dir = default_socket_dir()
        self.sock_path = os.path.join(self.sock_dir, "%d.sock" % os.getpid())
        self.record_path = os.path.join(
            claude_sessions_dir(), "%d.json" % os.getpid()
        )

        state = read_json(thread_state_path(self.thread_id), {}) or {}
        self.tail = RolloutTail.from_state(self.rollout_path, state.get("tail"))
        # This is an at-most-once processing ledger, including dropped replies,
        # not evidence of delivery. Read the legacy name when upgrading.
        self.processed_turns = collections.deque(
            state.get("processed_turns", state.get("delivered")) or [],
            maxlen=PROCESSED_TURN_HISTORY,
        )
        self.budgets = dict(state.get("budgets") or {})
        self.budget_sender_sid = state.get("budget_sender_sid")
        self.budget_last_at = parse_time(state.get("budget_last_at"))
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
            self.tail.last_boundary = last_boundary(self.rollout_path)

        self.started_at = time.time()
        # A registry record describes this shim process. Its initial nameSince
        # cannot predate startedAt merely because an older shim saved the same
        # alias in state.
        self.name_since = self.started_at
        # First startup recovers the current boundary along with the sender;
        # saved state carries the last boundary seen by the previous shim.
        self.status = "busy" if self.tail.last_boundary == "started" else "idle"
        self.poll_interval = _float_env(
            "SESSION_PEERS_POLL_INTERVAL", POLL_INTERVAL_DEFAULT
        )
        self.liveness_interval = _float_env(
            "SESSION_PEERS_LIVENESS_INTERVAL", LIVENESS_INTERVAL_DEFAULT
        )
        self.alias_refresh_interval = _float_env(
            "SESSION_PEERS_ALIAS_REFRESH_INTERVAL",
            ALIAS_REFRESH_INTERVAL_DEFAULT,
        )
        if self.alias_refresh_interval <= 0:
            self.alias_refresh_interval = ALIAS_REFRESH_INTERVAL_DEFAULT
        self.reply_budget_window = _float_env(
            "SESSION_PEERS_REPLY_BUDGET_WINDOW",
            REPLY_BUDGET_WINDOW_DEFAULT,
        )
        if self.reply_budget_window <= 0:
            self.reply_budget_window = REPLY_BUDGET_WINDOW_DEFAULT
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
        self.liveness_unverified = False

    # -- lifecycle ---------------------------------------------------------

    def run(self):
        if not self.rollout_path:
            log("thread %s has no rollout path; nothing to tail" % self.thread_id)
            return 2
        held, pid = thread_is_held(self.rollout_path, self.holder_pid, self.lock_path)
        if held is None:
            log(
                "thread %s liveness is unverified; not starting a shim"
                % self.thread_id
            )
            return 3
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

        # P2: ownership FIRST. Everything below mutates state another shim may
        # own (the pidfile, the per-thread state file, the budget marker), and
        # _cleanup would delete the live shim's pidfile on the way out.
        if not self._acquire_ownership():
            log(
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
            log(
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
        log("signal %d; shutting down" % signum)
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
            paths.append(thread_pid_path(self.thread_id))
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
            "nameSince": int(self.name_since * 1000),
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
                "thread_name": self.thread_name,
                "name_since": int(self.name_since * 1000),
                "shim_pid": os.getpid(),
                "shim_features": list(SHIM_FEATURES),
                "tail": self.tail.state(),
                "processed_turns": list(self.processed_turns),
                "budgets": self._bound(self.budgets),
                "budget_sender_sid": self.budget_sender_sid,
                "budget_last_at": self.budget_last_at,
                "budget_notified": sorted(self.budget_notified),
                "held": self._bound(self.held),
                "allowance": self.allowance,
                "contacts": self._bound(self.contacts),
                "updated_at": now_iso(),
            },
            mode=0o600,
        )

    def _acquire_ownership(self) -> bool:
        """Take the exclusive flock that proves this shim owns the thread.

        Held for the process's whole life; the kernel releases it on exit,
        crash included, so nothing else can mistake a recycled pid for us.
        Returns False when another shim holds it, having changed nothing.
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

        tag = build_tag(
            sender_name,
            sender_sid,
            sock_path if sender else None,
            frame.get("msg_id"),
        )
        # The tag rides inside the same text Codex caps, so the body is trimmed
        # to leave room for it rather than pushing the whole message over.
        # P5: the argv budget is bytes; the Codex cap is characters. Both.
        room_bytes = argv_text_budget() - utf8_len(tag) - 1
        room_chars = MAX_TEXT_CHARS - len(tag) - 1
        trimmed_from = None
        if utf8_len(body) > room_bytes or len(body) > room_chars:
            trimmed_from = len(body)
            body = truncate_utf8(body, room_bytes)[:room_chars]
            log("truncating an inbound body of %d chars to %d" % (trimmed_from, len(body)))
        text = "%s\n%s" % (tag, body)

        held, _pid = thread_is_held(self.rollout_path, self.holder_pid, self.lock_path)
        if held is None:
            log("thread %s liveness is unverified; not queueing" % self.thread_id)
            self._status_back(
                frame,
                sender,
                "failed",
                "the Codex thread liveness probe is unavailable; retry where "
                "lsof is permitted",
            )
            return
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
            codex_queue(self.thread_id, text, cwd=self.thread.get("cwd"))
        except QueueError as exc:
            log(
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
                deliver_to_record(
                    sender,
                    build_user_frame(
                        build_cc_body(notice, self.thread_id, self.name, None), None
                    ),
                )
            return
        log(
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
        return self._status_to_record(
            sender, frame.get("msg_id"), status, detail
        )

    def _status_to_record(self, sender, msg_id, status, detail):
        """Send one correlated status when the original peer message is known."""
        if not sender or not msg_id:
            return False
        return deliver_to_record(
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
        last_alias = last_live
        while not self.stop.wait(self.poll_interval):
            self._consume_budget_marker()
            # Never at startup: a release it triggers needs the bound socket.
            self._consume_budget_allow_marker()
            self._expire_held()
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
        held, _pid = thread_is_held(
            self.rollout_path, self.holder_pid, self.lock_path
        )
        if held is None:
            if not self.liveness_unverified:
                log(
                    "codex pid %s liveness is unverified; keeping the shim"
                    % self.holder_pid
                )
            self.liveness_unverified = True
        elif held:
            if self.liveness_unverified:
                log("codex pid %s liveness probe recovered" % self.holder_pid)
            self.liveness_unverified = False
        elif not held:
            log(
                "codex pid %s no longer holds %s or its writer lock; exiting"
                % (self.holder_pid, self.rollout_path)
            )
            self.stop.set()

    def _refresh_name(self):
        """Converge the advertised alias after a Codex `/rename`."""
        threads, schema_ok = codex_threads()
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
            desired = peer_name_for_thread(
                title,
                self.thread_id,
                title_owner=codex_title_owner(title, threads),
            )
        except NameError_ as exc:
            log("cannot refresh the peer alias: %s" % exc)
            return
        self.thread_name = title
        if desired == self.name:
            refresh_registered_name(self.thread_id, desired)
            return
        previous = self.name
        self.name = desired
        self.name_since = time.time()
        refresh_registered_name(self.thread_id, desired)
        if os.path.exists(self.record_path):
            self._write_record()
        self._save_state()
        log("peer alias changed from %s to %s" % (previous, desired))

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
        self.budget_sender_sid = None
        self.budget_last_at = None
        self.budget_notified = set()
        self.allowance = None
        if initial:
            # Not yet bound or registered: defer the release (see run()).
            self.release_after_start = bool(self.held)
            return
        log("reply budget reset for thread %s" % self.thread_id)
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
        log("purged %d expired held reply(s)" % len(stale))
        self._save_state()

    def _release_held(self):
        """Deliver each still-fresh held reply once; it opens the new sequence."""
        if not self.held:
            return
        records = live_claude_records()
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
        records = live_claude_records()
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
            log("discarding a held reply for %s: %.0fs old" % (sid, age))
            return "discarded"
        rec = next((r for r in records if r.get("sessionId") == sid), None)
        if rec is None:
            log("discarding a held reply: session %s is gone" % sid)
            return "discarded"
        if not socket_path_ok(rec.get("messagingSocketPath")):
            log("discarding a held reply: %s listens outside the allowlist" % sid)
            return "discarded"
        text = reply_text(entry["text"], entry.get("mid"), held_reply=True)
        try:
            body = build_cc_body(text, self.thread_id, self.name, self.sock_path)
        except ValueError as exc:
            log("cannot build a held reply for %s: %s" % (sid, exc))
            return "discarded"
        if not deliver_to_record(rec, build_user_frame(body, self.sock_path)):
            log("keeping the held reply for %s to retry" % sid)
            return "failed"
        # An explicit reset opens a new sequence; an allowance continues the
        # current one, so the release counts toward its usage.
        self.budgets[sid] = 1 if new_sequence else self.budgets.get(sid, 0) + 1
        self.budget_sender_sid = sid
        self.budget_last_at = now
        log(
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
            and 1 <= total <= BUDGET_ALLOW_MAX
            and parse_time(value.get("at")) is not None
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
        if self.allowance and self.allowance.get("sid") == sid:
            return max(REPLY_BUDGET, self.allowance["total"])
        return REPLY_BUDGET

    def _consume_budget_allow_marker(self):
        """Apply a `budget allow` grant: a total, never additive or replenishing."""
        path = budget_allow_path(self.thread_id)
        if not os.path.exists(path):
            return
        grant = read_json(path, None)
        try:
            os.unlink(path)
        except OSError:
            return
        if not self._valid_allowance(grant):
            log("ignoring a malformed reply allowance for thread %s" % self.thread_id)
            return
        sid, total = grant["sid"], grant["total"]
        granted_at = parse_time(grant["at"])
        if not self._allowance_fresh(granted_at, time.time()):
            log(
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
            log(
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
        log(
            "reply allowance for %s set to %d (%d spent)"
            % (sid, new_cap, self.budgets.get(sid, 0))
        )
        if new_cap > old_cap:
            # The requester may hit the raised cap later and should hear so.
            self.budget_notified.discard(sid)
            if self.budgets.get(sid, 0) < new_cap and sid in self.held:
                self._release_one(sid, "allow", live_claude_records())
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
            log("reply budget sequence reset: %s" % reason)
        if self.held:
            # Only an explicit reset releases a held reply; a sequence that
            # moved on must not receive a stale answer later.
            log("discarding %d held reply(s): the sequence moved on" % len(self.held))
        self.budgets = {}
        self.budget_notified = set()
        self.held = {}
        if self.allowance and not self._allowance_waiting(now):
            log("reply allowance for %s dropped: the sequence moved on"
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
            and self._allowance_fresh(parse_time(self.allowance.get("at")) or 0.0, now)
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
            log("turn %s aborted; nothing to deliver" % turn.turn_id)
            return
        # S7: a turn that completed while no shim ran IS picked up from the
        # cursor on restart. Recent is useful; hours old is a surprise reply to
        # a conversation that moved on, so it is recorded and not posted.
        age = None if turn.completed_at is None else self.started_at - turn.completed_at
        if age is not None and age > RESTART_DELIVERY_WINDOW:
            log(
                "turn %s completed %.0fs before this shim started; recording it "
                "as processed without posting" % (turn.turn_id, age)
            )
            return

        tag = turn.tag or {}
        self._advance_budget_sequence(tag)
        text = strip_tag(turn.last_agent_message or "").strip()
        if not text:
            log("turn %s finished with no agent message" % turn.turn_id)
            return

        records = live_claude_records()
        targets = []

        reply_socket = tag.get("reply")
        if reply_socket:
            if not socket_path_ok(reply_socket):
                log("reply address %r is not an allowed socket" % reply_socket)
            else:
                rec = claude_record_by_socket(reply_socket, records)
                if rec is None:
                    log("the session that queued turn %s is gone" % turn.turn_id)
                elif not tag.get("sid"):
                    # P3: sockets are named after a pid and pids are reused, so
                    # a tag with no session id cannot prove the session at that
                    # socket is the one that asked. No id, no auto-delivery.
                    log(
                        "the tag for turn %s carries no session id; not "
                        "auto-delivering to %s" % (turn.turn_id, reply_socket)
                    )
                elif rec.get("sessionId") != tag.get("sid"):
                    # Claude's own sender guards do not run here.
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
                        BUDGET_ALLOW_MAX,
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
                log(
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
                            BUDGET_ALLOW_MAX,
                            self.thread_id,
                            self.reply_budget_window,
                        )
                    )
                    body = build_cc_body(notice, self.thread_id, self.name, None)
                    if deliver_to_record(rec, build_user_frame(body, None)):
                        self.budget_notified.add(sid)
                continue
            out = (
                reply_text(text, tag.get("mid")) if sid == tag.get("sid") else text
            )
            try:
                body = build_cc_body(out, self.thread_id, self.name, self.sock_path)
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
    all_records = read_claude_records()
    classified = [(record, record_liveness(record)) for record in all_records]
    records = [record for record, status in classified if status == "live"]
    unverified = [
        record for record, status in classified if status == "unverified"
    ]
    threads, schema_ok = codex_threads()
    registered = read_registered()

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
        pid = shim_pid(thread["id"])
        state = read_json(thread_state_path(thread["id"]), {}) if pid else {}
        alias = state.get("name") if isinstance(state, dict) else None
        if not alias:
            try:
                alias = peer_name_for_thread(
                    thread.get("name"),
                    thread["id"],
                    records=records,
                    title_owner=codex_title_owner(thread.get("name"), threads),
                )
            except NameError_:
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
                ", shim %s" % t["shim_pid"] if t["shim_pid"] else "",
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
    if value and not is_uuid(value):
        if explicit or required:
            raise ValueError("--from-thread/CODEX_THREAD_ID must be a UUID")
        return None
    if required and not value:
        raise ValueError(
            "a request needs --from-thread or CODEX_THREAD_ID so its origin is explicit"
        )
    return value


def _resolve_claude_record(target):
    records = read_claude_records()
    candidates = claude_record_by_target(target, records)
    classified = [(record, record_liveness(record)) for record in candidates]
    matches = [record for record, status in classified if status == "live"]
    if any(status == "unverified" for _record, status in classified):
        raise ResolveError(
            "cannot verify Claude target %r because the process-start probe is "
            "unavailable; retry outside the sandbox or with host permission" % target
        )
    if not matches:
        noun = "id" if is_uuid(target) else "name"
        raise ResolveNotFound("no live Claude session with %s %r" % (noun, target))
    if len(matches) > 1:
        raise ResolveError(
            "%r names %d live sessions (%s); rename one"
            % (target, len(matches), ", ".join(str(m.get("pid")) for m in matches))
        )
    rec = matches[0]
    if not socket_path_ok(rec.get("messagingSocketPath")):
        raise ResolveError(
            "%s listens on %r, outside the allowlisted socket directories"
            % (target, rec.get("messagingSocketPath"))
        )
    return rec


def _deliver_claude(rec, message, thread_id=None, reply_route=True):
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
    route = shim_socket if reply_route else None
    body = build_cc_body(message, thread_id or "", thread_name, route)
    if len(body) > MAX_TEXT_CHARS or utf8_len(body) > MAX_TEXT_CHARS:
        raise ValueError(
            "wrapped message exceeds the %d-character/UTF-8-byte peer cap"
            % MAX_TEXT_CHARS
        )
    frame = build_user_frame(body, route)
    send_frame(rec["messagingSocketPath"], frame, auth_token=peer_token_for(rec))
    return frame["msg_id"], bool(route)


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
    warn_versions()
    failed = _expand_buddy_arg(args, "to", "send")
    if failed is not None:
        return failed
    try:
        args.message = message_from_args(args)
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
        thread = resolve_thread(target)
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    degraded = thread.get("degraded")
    if degraded:
        log("liveness unverified: the Codex state schema is unknown")
    else:
        held, _pid = thread_is_held(
            thread["rollout_path"], lock_path=writer_lock_path(thread["id"])
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
        if thread_is_paused(thread["rollout_path"]):
            log(
                "thread %s is paused after an interrupt: the message is queued "
                "but drains only when its user types the next prompt" % thread["id"]
            )
    from_name, from_sid = args.from_name, args.from_sid
    from_socket = args.from_socket or os.environ.get(
        "CLAUDE_CODE_MESSAGING_SOCKET"
    )
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not from_socket and not args.from_sid and is_uuid(env_sid):
        matches = claude_record_by_target(env_sid)
        if len(matches) == 1:
            from_socket = matches[0].get("messagingSocketPath")
    if from_socket:
        # P3: a reply address without a session id can be delivered to whoever
        # holds that socket next, so the id is resolved here, from the registry.
        rec = claude_record_by_socket(from_socket)
        if rec is None:
            sys.stderr.write(
                "error: no live Claude session listens on %s, so --from-socket "
                "would name a reply address nothing answers\n" % from_socket
            )
            return 1
        from_sid = from_sid or rec.get("sessionId")
        from_name = from_name or rec.get("name")
    elif not from_name and not from_sid:
        log(
            "sender identity absent: replies stay in the Codex TUI; when "
            "sending from Claude Code, use its Bash tool so "
            "CLAUDE_CODE_MESSAGING_SOCKET is available"
        )
    msg_id = str(uuidlib.uuid4())
    tag = build_tag(from_name, from_sid, from_socket, msg_id)
    text = "%s\n%s" % (tag, args.message)
    try:
        codex_queue(thread["id"], text, cwd=thread.get("cwd"))
    except QueueError as exc:
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
        rec = _resolve_claude_record(target)
        thread_id = _thread_from_args(args)
        msg_id, reply_capable = _deliver_claude(rec, args.message, thread_id)
    except (ResolveError, ValueError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    except OSError as exc:
        sys.stderr.write("error: could not reach %s: %s\n" % (target, exc))
        return 1
    if thread_id and not reply_capable:
        log(
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


def _bounded_timeout(value):
    timeout = REQUEST_TIMEOUT_DEFAULT if value is None else float(value)
    if timeout <= 0 or timeout > REQUEST_TIMEOUT_MAX:
        raise ValueError(
            "timeout must be greater than 0 and at most %.0f seconds"
            % REQUEST_TIMEOUT_MAX
        )
    return timeout


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except (FileNotFoundError, OSError):
        pass


def cleanup_expired_requests(now=None, dry_run=False):
    """Remove expired/orphaned request mailboxes; return removed request ids."""
    now = time.time() if now is None else float(now)
    removed = []
    try:
        names = os.listdir(request_dir())
    except OSError:
        return removed
    suffix = ".request.json"
    for name in names:
        if not name.endswith(suffix):
            continue
        request_id = name[: -len(suffix)]
        if not is_uuid(request_id):
            continue
        path = request_path(request_id)
        data = read_json(path, {}) or {}
        try:
            expires_at = float(data.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at > now:
            continue
        if not dry_run:
            _unlink_quiet(path)
            _unlink_quiet(request_reply_path(request_id))
        removed.append(request_id)
    for name in names:
        if not name.endswith(".reply.json"):
            continue
        request_id = name[: -len(".reply.json")]
        if not is_uuid(request_id) or os.path.exists(request_path(request_id)):
            continue
        path = request_reply_path(request_id)
        try:
            stale = os.stat(path).st_mtime <= now - REQUEST_ORPHAN_TTL
        except OSError:
            stale = False
        if stale:
            if not dry_run:
                _unlink_quiet(path)
            if request_id not in removed:
                removed.append(request_id)
    return removed


def _request_envelope(request_id, message, timeout):
    script = os.path.abspath(__file__)
    return (
        '<session-peers-request id="%s" timeout-seconds="%d">\n'
        "%s\n"
        "</session-peers-request>\n\n"
        "Reply contract: return the result to the waiting Codex turn, not its "
        "ordinary queue. Write the complete reply to a private temporary file, "
        "then run:\n"
        "%s reply --request %s --message-file <absolute-reply-file>\n"
        "Do not use SendMessage or `send --to codex:` for this request. The "
        "mailbox is single-use and expires with the timeout."
        % (
            request_id,
            int(timeout),
            neutralise_request_markup(message),
            shlex.quote(script),
            request_id,
        )
    )


def _request_meta(request_id, requester_thread_id, rec, expires_at, reply_path):
    """The single-use mailbox metadata shared by `ask` and `dispatch`.

    Keeping one builder means the two entry points cannot drift in the fields
    `reply`, `await`, and `cleanup_expired_requests` all read back.
    """
    return {
        "request_id": request_id,
        "requester_thread_id": requester_thread_id,
        "target_session_id": rec.get("sessionId"),
        "target_session_name": rec.get("name"),
        "created_at": now_iso(),
        "expires_at": expires_at,
        "reply_path": reply_path,
    }


def _reply_matches(response, request_id, target_session_id):
    """True when a reply file is the intended one and carries a text body."""
    return (
        isinstance(response, dict)
        and response.get("request_id") == request_id
        and response.get("session_id") == target_session_id
        and isinstance(response.get("message"), str)
    )


def cmd_ask(args):
    """Send one correlated request to Claude and return its reply on stdout."""
    warn_versions()
    failed = _expand_buddy_arg(args, "to", "ask")
    if failed is not None:
        return failed
    if not args.to.startswith("cc:"):
        sys.stderr.write("error: ask --to must start with cc:\n")
        return 2
    try:
        message = message_from_args(args)
        timeout = _bounded_timeout(args.timeout)
        thread_id = _thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = _resolve_claude_record(args.to[len("cc:") :])
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if not rec.get("sessionId"):
        sys.stderr.write(
            "error: Claude target %r has no session id, so its reply cannot be verified\n"
            % (rec.get("name") or args.to)
        )
        return 1

    cleanup_expired_requests()
    request_id = str(uuidlib.uuid4())
    meta_path = request_path(request_id)
    reply_path = request_reply_path(request_id)
    expires_at = time.time() + timeout
    write_json_atomic(
        meta_path,
        _request_meta(request_id, thread_id, rec, expires_at, reply_path),
    )
    try:
        try:
            message_id, _reply_capable = _deliver_claude(
                rec,
                _request_envelope(request_id, message, timeout),
                thread_id,
                reply_route=False,
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write("error: could not send request to %s: %s\n" % (args.to, exc))
            return 1
        log(
            "request %s sent to %s; waiting up to %.0fs"
            % (request_id, rec.get("name") or rec.get("sessionId"), timeout)
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = read_json(reply_path, None)
            if _reply_matches(response, request_id, rec.get("sessionId")):
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
            time.sleep(REQUEST_POLL_INTERVAL)
        sys.stderr.write(
            "error: request %s timed out after %.0f seconds; no reply was queued\n"
            % (request_id, timeout)
        )
        return 124
    finally:
        _unlink_quiet(meta_path)
        _unlink_quiet(reply_path)


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


def _read_completed_reply(path, attempts=20):
    """Read a competing reply after its exclusive writer finishes."""
    for _index in range(attempts):
        value = read_json(path, None)
        if isinstance(value, dict):
            return value
        time.sleep(0.01)
    return {}


def cmd_reply(args):
    """Complete one pending ask mailbox from its intended Claude session."""
    try:
        message = message_from_args(args)
        path = request_path(args.request)
        reply_path = request_reply_path(args.request)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    cleanup_expired_requests()
    meta = read_json(path, None)
    if not isinstance(meta, dict):
        sys.stderr.write("error: request %s is unknown or expired\n" % args.request)
        return 1
    try:
        expires_at = float(meta.get("expires_at", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= time.time():
        cleanup_expired_requests()
        sys.stderr.write("error: request %s is expired\n" % args.request)
        return 1
    sid = _current_claude_session_id()
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
        "replied_at": now_iso(),
    }
    try:
        created = write_json_exclusive(reply_path, payload)
    except OSError as exc:
        sys.stderr.write("error: could not write reply: %s\n" % exc)
        return 1
    if not created:
        existing = _read_completed_reply(reply_path)
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
    warn_versions()
    failed = _expand_buddy_arg(args, "to", "dispatch")
    if failed is not None:
        return failed
    if not args.to.startswith("cc:"):
        sys.stderr.write("error: dispatch --to must start with cc:\n")
        return 2
    try:
        message = message_from_args(args)
        timeout = _bounded_timeout(args.timeout)
        thread_id = _thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = _resolve_claude_record(args.to[len("cc:") :])
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    if not rec.get("sessionId"):
        sys.stderr.write(
            "error: Claude target %r has no session id, so its reply cannot be verified\n"
            % (rec.get("name") or args.to)
        )
        return 1

    cleanup_expired_requests()
    request_id = str(uuidlib.uuid4())
    meta_path = request_path(request_id)
    reply_path = request_reply_path(request_id)
    expires_at = time.time() + timeout
    meta = _request_meta(request_id, thread_id, rec, expires_at, reply_path)
    write_json_atomic(meta_path, meta)
    try:
        message_id, _reply_capable = _deliver_claude(
            rec,
            _request_envelope(request_id, message, timeout),
            thread_id,
            reply_route=False,
        )
    except (OSError, ValueError) as exc:
        # Delivery failed, so no reply can ever arrive: do not leave an orphan
        # mailbox that a later `await` would poll until it expired.
        _unlink_quiet(meta_path)
        _unlink_quiet(reply_path)
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
    log(
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


def _claim_reply(reply_path):
    """Atomically take ownership of a reply file so it is consumed once.

    `os.rename` is atomic, so exactly one caller renames the single reply file
    away; a racing `await` sees it gone and stands down. Returns the parsed
    reply for the winner, or None if another consumer already claimed it.
    """
    claim_path = "%s.consumed.%d.%s" % (reply_path, os.getpid(), uuidlib.uuid4().hex)
    try:
        os.rename(reply_path, claim_path)
    except OSError:
        return None
    try:
        data = read_json(claim_path, None)
    finally:
        _unlink_quiet(claim_path)
    return data if isinstance(data, dict) else None


def cmd_await(args):
    """Consume the reply to one dispatched request, or report why not.

    `--timeout` bounds only this invocation, never the request lifetime. A
    call that times out while the request is still unexpired reports `pending`
    and leaves the mailbox intact so a later `await` resumes it; a request past
    its `expires_at` reports `expired`. The reply is consumed exactly once.
    """
    try:
        meta_path = request_path(args.request)
        reply_path = request_reply_path(args.request)
        timeout = _bounded_timeout(args.timeout)
        thread_id = _thread_from_args(args, required=True)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2

    cleanup_expired_requests()
    meta = read_json(meta_path, None)
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
            cleanup_expired_requests()
            return _await_expired(args, "expired before a reply arrived")
        if not os.path.exists(meta_path):
            return _await_expired(args, "unknown, already consumed, or expired")
        response = read_json(reply_path, None)
        if _reply_matches(response, args.request, target_sid):
            claimed = _claim_reply(reply_path)
            if claimed is None:
                # A concurrent await consumed this reply first.
                return _await_expired(args, "already consumed")
            _unlink_quiet(meta_path)
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
        time.sleep(REQUEST_POLL_INTERVAL)

    if expires_at <= time.time():
        cleanup_expired_requests()
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
        timeout = _bounded_timeout(args.timeout)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    try:
        rec = _resolve_claude_record(target)
    except ResolveError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1
    sid = rec.get("sessionId")
    deadline = time.monotonic() + timeout
    interval = _float_env(
        "SESSION_PEERS_WAIT_POLL_INTERVAL", WAIT_POLL_INTERVAL_DEFAULT
    )
    if interval <= 0:
        interval = WAIT_POLL_INTERVAL_DEFAULT
    while time.monotonic() < deadline:
        candidates = [
            item for item in read_claude_records() if item.get("sessionId") == sid
        ]
        if candidates:
            current = candidates[0]
            if record_liveness(current) == "live" and current.get("status") == args.state:
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


def shim_ready(thread_id):
    """The pid of a shim that owns the thread AND has written its record.

    The ownership lock is taken before the socket is bound, so `shim_pid`
    alone answers "starting", not "serving". `up` waits for this.
    """
    pid = shim_pid(thread_id)
    if pid is None:
        return None
    rec = read_json(os.path.join(claude_sessions_dir(), "%d.json" % pid), None)
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
        daemon_pid = spawn_detached(
            [sys.executable, script, "shim", "--thread", thread["id"]],
            log_path,
        )
    except OSError as exc:
        log("could not start the shim for %s: %s" % (thread["id"], exc))
        return None
    # The SHIM writes and locks the pidfile once it is serving, so the file is
    # never a claim without a holder. Wait for it so a following reconcile
    # inside the same lock sees the shim rather than starting a second one.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        pid = shim_ready(thread["id"])
        if pid:
            return pid
        if not pid_alive(daemon_pid):
            log("the shim for %s exited; see %s" % (thread["id"], log_path))
            return None
        time.sleep(0.05)
    log("the shim for %s did not report ready in 10s; see %s" % (thread["id"], log_path))
    return None


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


def reconcile(verbose=True):
    """Start one shim per registered, live thread. Idempotent by design."""
    with reconcile_lock():
        return _reconcile(verbose)


def attach_thread(thread_id, verbose=True):
    """Start a shim for one live UUID without making it a persistent opt-in."""
    with reconcile_lock():
        try:
            thread = resolve_thread(thread_id)
        except ResolveError as exc:
            if verbose:
                print("  %s: not attachable (%s)" % (thread_id, exc))
            return None
        if thread.get("live") is not True:
            if verbose:
                state = "unverified" if thread.get("live") is None else "not live"
                print("  %s: %s, skipped" % (thread_id, state))
            return None
        pid = shim_pid(thread_id)
        if pid:
            if verbose:
                print("  %s: shim already running (pid %s)" % (thread_id, pid))
            return pid
        pid = spawn_shim(thread)
        if verbose:
            if pid is None:
                print("  %s: shim failed to start (see its log)" % thread_id)
            else:
                print("  %s: shim started (pid %d)" % (thread_id, pid))
        return pid


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
            thread = resolve_thread_prefer_live(args.target)
            tid = thread["id"]
        except ResolveError as exc:
            tid = args.target if is_uuid(args.target) else None
            if tid is None:
                sys.stderr.write("error: %s\n" % exc)
                return 1
        # R2: stop and unregister under ONE hold of the reconcile lock. A bare
        # `up` landing between them would restart the still-registered thread,
        # leaving an unregistered peer running while `down` reported success.
        with reconcile_lock():
            stopped = stop_shim(tid)
            removed = _unregister_thread_unlocked(tid)
        if removed:
            print("unregistered %s%s" % (tid, " and stopped its shim" if stopped else ""))
        else:
            print("%s was not registered%s" % (tid, "; shim stopped" if stopped else ""))
        return 0
    with reconcile_lock():
        stopped = [tid for tid in sorted(read_registered()) if stop_shim(tid)]
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
        return resolve_thread_prefer_live(value)["id"], None
    except ResolveError as exc:
        if not is_uuid(value):
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
    _unlink_quiet(budget_allow_path(tid))
    with open(budget_reset_path(tid), "w", encoding="utf-8") as fh:
        fh.write(now_iso() + "\n")
    state_path = thread_state_path(tid)
    state = read_json(state_path, None)
    if isinstance(state, dict) and (state.get("budgets") or state.get("allowance")):
        state["budgets"] = {}
        state["allowance"] = None
        write_json_atomic(state_path, state, mode=0o600)
    print("reply budget reset for %s" % tid)
    return 0


def cmd_budget_allow(args):
    """Grant one requester a TOTAL reply allowance on one Codex thread."""
    if not 1 <= args.replies <= BUDGET_ALLOW_MAX:
        sys.stderr.write(
            "error: --replies must be between 1 and %d\n" % BUDGET_ALLOW_MAX
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
    if not is_uuid(sid):
        sys.stderr.write("error: --for-session must be a Claude session UUID\n")
        return 2
    tid, failure = _budget_target(args.thread, args)
    if failure:
        sys.stderr.write("error: %s\n" % failure[1])
        return failure[0]
    pid = shim_pid(tid)
    if pid and not shim_supports(tid, pid, "budget_allow"):
        sys.stderr.write(
            "error: cannot verify the running shim for %s (pid %d) supports "
            "allowances; a shim started from an older peers.py never reads the "
            "grant, so the cap would stay %d. Restart it: `peers.py down %s && "
            "peers.py up %s` (a fresh `up` also resets the budget), then grant "
            "again\n" % (tid, pid, REPLY_BUDGET, tid, tid)
        )
        return 1
    path = budget_allow_path(tid)
    total = args.replies
    pending = read_json(path, None)
    window = _float_env("SESSION_PEERS_REPLY_BUDGET_WINDOW", REPLY_BUDGET_WINDOW_DEFAULT)
    if window <= 0:
        window = REPLY_BUDGET_WINDOW_DEFAULT
    granted_at = now_iso()
    if isinstance(pending, dict) and pending.get("sid") == sid:
        # Not yet consumed: two grants before the shim polls keep the higher,
        # with the higher's own time, so a merge never refreshes an old grant.
        previous = pending.get("total")
        previous_at = parse_time(pending.get("at"))
        if (
            isinstance(previous, int)
            and not isinstance(previous, bool)
            and previous_at is not None
            and time.time() - previous_at <= window
            and min(previous, BUDGET_ALLOW_MAX) >= total
        ):
            total = min(previous, BUDGET_ALLOW_MAX)
            granted_at = pending["at"]
    write_json_atomic(path, {"sid": sid, "total": total, "at": granted_at}, mode=0o600)
    print(
        "reply allowance for %s: up to %d consecutive replies to session %s "
        "(a total for this sequence; replies already delivered still count)"
        % (tid, max(REPLY_BUDGET, total), sid)
    )
    if not pid:
        print("no shim is running for %s; the grant applies once one starts" % tid)
    return 0


def shim_supports(thread_id, pid, feature):
    """True only when the state file proves shim ``pid`` has ``feature``.

    A shim saves its state, pid included, before it serves. Missing state, or
    state naming another pid, proves nothing about the running code, so it
    counts as unsupported rather than risking a grant nothing reads.
    """
    state = read_json(thread_state_path(thread_id), None)
    if not isinstance(state, dict) or state.get("shim_pid") != pid:
        return False
    return feature in (state.get("shim_features") or [])


# --------------------------------------------------------------------------
# Buddies: one bound peer per session
# --------------------------------------------------------------------------


def parse_typed(value):
    """`cc:<uuid>` / `codex:<uuid>` into a typed identity, or ValueError."""
    kind, sep, ident = str(value or "").partition(":")
    if not sep or kind not in BUDDY_KINDS or not is_uuid(ident):
        raise ValueError("expected cc:<uuid> or codex:<uuid>, got %r" % value)
    return {"kind": kind, "uuid": ident}


def caller_identity(args):
    """The typed identity of the session running this command."""
    explicit = getattr(args, "as_identity", None)
    if explicit:
        return parse_typed(explicit)
    sid = _current_claude_session_id()
    if sid:
        if not is_uuid(sid):
            raise ValueError("this Claude session's id %r is not a UUID" % sid)
        return {"kind": "cc", "uuid": sid}
    tid = _thread_from_args(args)
    if tid:
        return {"kind": "codex", "uuid": tid}
    raise ValueError("cannot tell which session is asking; pass --as")


def _resolve_typed_kind(kind, target, live_only=False):
    if kind == "cc":
        rec = _resolve_claude_record(target)
        sid = rec.get("sessionId")
        if not is_uuid(sid):
            # A bound buddy is addressed by UUID only; a non-UUID id would be
            # re-read as a name later.
            raise ResolveError("Claude session %r has no UUID session id" % target)
        return {"kind": "cc", "uuid": sid, "name": rec.get("name")}
    # Codex reuses titles, so a live thread usually shares its name with dead
    # ones: the live match wins, and dead threads count only when none is live.
    if live_only:
        thread = resolve_thread(target, require_live=True)
    else:
        thread = resolve_thread_prefer_live(target)
    return {"kind": "codex", "uuid": thread["id"], "name": thread.get("name")}


def resolve_typed(target):
    """`cc:x`, `codex:x` or a bare `[@]x` into one typed identity with its name.

    Names are resolved here, once; a bound buddy is used by UUID afterwards.
    """
    target = str(target or "")
    if target.startswith("@"):
        target = target[1:]
    for kind in BUDDY_KINDS:
        if target.startswith(kind + ":"):
            return _resolve_typed_kind(kind, target[len(kind) + 1 :])
    if not target:
        raise ResolveError("an empty target names no session")
    found, errors, dead_codex = [], [], False
    for kind in BUDDY_KINDS:
        try:
            # Live Codex threads only here: a dead namesake must not compete
            # with a live Claude session for the same bare name.
            found.append(_resolve_typed_kind(kind, target, live_only=True))
        except ResolveNotFound:
            pass
        except ResolveNoLive:
            dead_codex = True
        except ResolveError as exc:
            errors.append(exc)
    if dead_codex and not found and not errors:
        # Nothing live carries the name: bind the dead thread as `codex:` would.
        return _resolve_typed_kind("codex", target)
    if errors:
        # An ambiguous or unverifiable side could be the one meant: never guess.
        raise ResolveError(
            "%s; pass cc:%s or codex:%s to pick the kind" % (errors[0], target, target)
        )
    if len(found) > 1:
        raise ResolveError(
            "%r matches both %s; pass one of them"
            % (target, " and ".join("%s:%s" % (i["kind"], i["uuid"]) for i in found))
        )
    if not found:
        raise ResolveError(
            "no Claude session or Codex thread named %r; run `peers.py list`" % target
        )
    return found[0]


def read_buddy(owner):
    """The owner's buddy record, or None when absent or malformed."""
    rec = read_json(buddy_path(owner), None)
    if not isinstance(rec, dict):
        return None
    buddy = rec.get("buddy")
    if (
        not isinstance(buddy, dict)
        or buddy.get("kind") not in BUDDY_KINDS
        or not is_uuid(buddy.get("uuid"))
    ):
        return None
    return rec


def _parse_uses(value):
    if value is None:
        return list(BUDDY_USES)
    uses = []
    for word in value.split(","):
        word = word.strip()
        if word and word not in uses:
            uses.append(word)
    unknown = [word for word in uses if word not in BUDDY_USES]
    if unknown or not uses:
        raise ValueError(
            "unsupported --uses %s; supported: %s"
            % (", ".join(unknown) or "(empty)", ", ".join(BUDDY_USES))
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
    records = [r for r in read_claude_records() if r.get("sessionId") == uuid]
    states = [(r, record_liveness(r)) for r in records]
    live = [r for r, state in states if state == "live"]
    if live:
        rec = live[0]
        status.update(live=True, name=rec.get("name"), status=rec.get("status"))
        if socket_path_ok(rec.get("messagingSocketPath")):
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
        thread = resolve_thread(uuid, require_live=False)
    except ResolveError as exc:
        status["route"] = "unavailable: %s" % exc
        return status
    status.update(
        name=thread.get("name"),
        live=thread.get("live"),
        registered=thread.get("registered"),
    )
    pid = shim_pid(uuid)
    if attach and pid is None and thread.get("live") is True:
        # Transient attach only: `up` would also reset the reply budget,
        # release held replies and register the thread persistently.
        pid = attach_thread(uuid, verbose=False)
    status["shim_pid"] = pid
    if pid:
        shim_rec = read_json(os.path.join(claude_sessions_dir(), "%d.json" % pid), None)
        if isinstance(shim_rec, dict) and shim_rec.get("sessionId") == uuid:
            status["status"] = shim_rec.get("status")
    rollout = thread.get("rollout_path")
    if rollout and os.access(rollout, os.R_OK):
        status["paused"] = thread_is_paused(rollout)
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
    return "buddy = %s (%s, %s) %s; %s; uses: %s" % (
        name,
        status["kind"],
        status["uuid"][:8],
        ", ".join(parts),
        route_text,
        ", ".join(rec.get("uses") or []),
    )


def _print_buddy(args, rec, status):
    if getattr(args, "json", False):
        payload = dict(rec)
        payload["status"] = status
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
    path = buddy_path(owner)
    if action == "clear":
        try:
            os.unlink(path)
        except FileNotFoundError:
            print("no buddy was set")
            return 0
        print("buddy cleared")
        return 0
    if action == "set":
        try:
            uses = _parse_uses(args.uses)
        except ValueError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 2
        try:
            buddy = resolve_typed(args.target)
        except ResolveError as exc:
            sys.stderr.write("error: %s\n" % exc)
            return 1
        if buddy["kind"] == owner["kind"] and buddy["uuid"] == owner["uuid"]:
            sys.stderr.write("error: a session cannot be its own buddy\n")
            return 2
        rec = {
            "owner": owner,
            "buddy": buddy,
            "uses": uses,
            "set_at": now_iso(),
        }
        write_json_atomic(path, rec, mode=0o600)
        _print_buddy(args, rec, buddy_status(buddy, attach=True))
        return 0
    rec = read_buddy(owner)
    if rec is None:
        sys.stderr.write("error: no buddy set; run `peers.py buddy set <name|uuid>`\n")
        return 1
    _print_buddy(args, rec, buddy_status(rec["buddy"], attach=(action == "ping")))
    return 0


def _owner_verified_gone(owner):
    """True only when the owner is PROVEN not live; unknown keeps the record."""
    if owner["kind"] == "cc":
        try:
            os.listdir(claude_sessions_dir())
        except FileNotFoundError:
            return True
        except OSError:
            return False
        states = [
            record_liveness(r)
            for r in read_claude_records()
            if r.get("sessionId") == owner["uuid"]
        ]
        return all(state == "dead" for state in states)
    try:
        thread = resolve_thread(owner["uuid"], require_live=False)
    except ResolveError:
        return False
    return thread.get("live") is False


def gc_buddy_records(days=GC_DAYS_DEFAULT, dry_run=False, verbose=True):
    """Prune buddy records whose owner is verified gone and older than days."""
    if days < 0:
        raise ValueError("retention days must be zero or greater")
    cutoff = time.time() - days * 86400.0
    try:
        names = sorted(os.listdir(buddies_dir()))
    except OSError:
        return []
    removed = []
    for name in names:
        kind, sep, rest = name.partition("-")
        if not sep or kind not in BUDDY_KINDS or not rest.endswith(".json"):
            continue
        ident = rest[: -len(".json")]
        if not is_uuid(ident):
            continue
        path = os.path.join(buddies_dir(), name)
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
                log("could not prune %s: %s" % (path, exc))
                continue
        removed.append(name)
        if verbose:
            print("%s buddy record %s" % ("would prune" if dry_run else "pruned", name))
    return removed


def cmd_gc(args):
    days = (
        args.days
        if args.days is not None
        else _float_env("SESSION_PEERS_GC_DAYS", GC_DAYS_DEFAULT)
    )
    try:
        removed = gc_bridge_state(days=days, dry_run=args.dry_run)
    except ValueError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    buddies = gc_buddy_records(days=days, dry_run=args.dry_run)
    requests = cleanup_expired_requests(dry_run=args.dry_run)
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
    script = os.path.realpath(__file__)
    log_path = os.path.join(state_dir(), "session-hook.log")
    try:
        command = [sys.executable, script, "hook-reconcile"]
        if (
            _args.auto_attach
            and source in ("startup", "resume")
            and is_uuid(session_id)
        ):
            command.extend(["--thread", session_id])
        spawn_detached(command, log_path)
    except OSError as exc:
        log("could not start the reconcile: %s" % exc)
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
    days = _float_env("SESSION_PEERS_GC_DAYS", GC_DAYS_DEFAULT)
    try:
        gc_bridge_state(days=days, verbose=False)
    except ValueError as exc:
        log("GC skipped: %s" % exc)
    cleanup_expired_requests()
    reconcile(verbose=False)
    if args.thread:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if attach_thread(args.thread, verbose=False):
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
    script = script or os.path.realpath(__file__)
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
    write_json_atomic(path, root, mode=mode)
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


def _load_tomllib():
    """The stdlib TOML parser, or None on 3.9 and 3.10.

    A seam, so a test can prove the no-parser path without a second runtime.
    """
    try:
        import tomllib
    except ImportError:
        return None
    return tomllib


def _unquote_table_name(name):
    """`["features"]` names the same table as `[features]` (R1c)."""
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
        inner = name[1:-1]
        if inner and '"' not in inner and "'" not in inner:
            return inner.strip()
    return name


def _scan_multiline(line, delim):
    """The open multiline-string delimiter after this line, or None.

    Good enough to tell whether a `[features]` line is real config or an
    example inside a triple-quoted block (R1); the parser diff backstops it.
    """
    i = 0
    while i < len(line):
        if delim is not None:
            j = line.find(delim, i)
            if j == -1:
                return delim
            i = j + 3
            delim = None
            continue
        if line.startswith(TRIPLE_DQ, i) or line.startswith(TRIPLE_SQ, i):
            delim = line[i:i + 3]
            i += 3
            continue
        ch = line[i]
        if ch == "#":
            return None
        if ch in "\"'":
            j = line.find(ch, i + 1)
            if j == -1:
                return None
            i = j + 1
            continue
        i += 1
    return delim


def _line_states(lines):
    """[bool] telling, per line, whether it STARTS inside a multiline string."""
    states = []
    delim = None
    for line in lines:
        states.append(delim is not None)
        delim = _scan_multiline(line, delim)
    return states


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
    _started, ps_error = proc_start_checked(os.getpid())
    add(
        "fail" if ps_error else "ok",
        "process-start probe%s"
        % (": unavailable (%s)" % ps_error if ps_error else ""),
    )
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
    if os.path.isdir(sock_dir):
        socket_error = unix_socket_probe(sock_dir)
        add(
            "fail" if socket_error else "ok",
            "Unix-socket bind%s"
            % (": unavailable (%s)" % socket_error if socket_error else ""),
        )
    else:
        add("warn", "Unix-socket bind not tested; socket directory is absent")

    db = find_state_db()
    add(
        "ok" if db else "warn",
        "Codex state database: %s"
        % (db or "none with a recognised `threads` schema (send by UUID only)"),
    )
    threads, _ok = codex_threads()
    unverified_threads = [
        thread for thread in threads if thread.get("live") is None
    ]
    if unverified_threads:
        detail = unverified_threads[0].get("liveness_error") or "lsof failed"
        add("fail", "Codex liveness probe unavailable: %s" % detail)
        add("warn", "bridge GC skipped while Codex liveness is unverified")
    elif db:
        stale = gc_bridge_state(
            days=_float_env("SESSION_PEERS_GC_DAYS", GC_DAYS_DEFAULT),
            dry_run=True,
            verbose=False,
        )
        add(
            "warn" if stale else "ok",
            "bridge GC: %d stale thread%s"
            % (len(stale), "" if len(stale) == 1 else "s"),
        )

    registered = read_registered()
    live_ids = {t["id"] for t in threads if t.get("live") is True}
    unknown_ids = {t["id"] for t in threads if t.get("live") is None}
    for tid in sorted(registered):
        name = registered[tid].get("name") or tid
        pid = shim_pid(tid)
        if pid:
            add("ok", "%s: shim running (pid %d)" % (name, pid))
        elif tid in live_ids:
            add("warn", "%s: thread is live but no shim (run `peers.py up`)" % name)
        elif tid in unknown_ids:
            add("warn", "%s: thread liveness unverified" % name)
        else:
            add("ok", "%s: registered, thread not running" % name)
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
            and not shim_pid(t["id"])
        ),
        key=lambda t: t["id"],
    ):
        add(
            "warn",
            "%s: live Codex thread, not attached (run `peers.py up %s`)"
            % (thread.get("name") or thread["id"], thread["id"]),
        )

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


# --------------------------------------------------------------------------
# Topics: pull-only, append-only logs any peer can post to and read
# --------------------------------------------------------------------------

TOPIC_MAX_CHARS = 256
TOPIC_TTL_DAYS_DEFAULT = 7.0
TOPIC_MAX_ENTRIES_DEFAULT = 1000
TOPIC_MAX_BYTES_DEFAULT = 16 * 1024 * 1024
TOPIC_TAIL_DEFAULT = 20
TOPIC_TAIL_MAX = 1000
TOPIC_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# C0 and C1 controls: a topic is one printable line.
TOPIC_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class TopicError(Exception):
    """A refused topic operation; carries the exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def topics_dir():
    path = os.path.join(state_dir(), "topics")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def validate_topic(topic):
    """The topic string unchanged, or TopicError. It is opaque otherwise."""
    if not isinstance(topic, str) or not topic.strip():
        raise TopicError("a topic must be a non-empty string", 2)
    if len(topic) > TOPIC_MAX_CHARS:
        raise TopicError("a topic is at most %d characters" % TOPIC_MAX_CHARS, 2)
    if TOPIC_BAD_CHARS_RE.search(topic):
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
    ttl = _float_env("SESSION_PEERS_TOPIC_TTL_DAYS", TOPIC_TTL_DAYS_DEFAULT)
    entries = int(_float_env("SESSION_PEERS_TOPIC_MAX_ENTRIES", TOPIC_MAX_ENTRIES_DEFAULT))
    size = int(_float_env("SESSION_PEERS_TOPIC_MAX_BYTES", TOPIC_MAX_BYTES_DEFAULT))
    return (
        ttl if ttl > 0 else TOPIC_TTL_DAYS_DEFAULT,
        entries if entries > 0 else TOPIC_MAX_ENTRIES_DEFAULT,
        size if size > 0 else TOPIC_MAX_BYTES_DEFAULT,
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
    meta = read_json(meta_path, {}) or {}
    next_seq = meta.get("next_seq") if isinstance(meta.get("next_seq"), int) else 1
    if lines:
        next_seq = max(next_seq, lines[-1][0]["seq"] + 1)
    cutoff = now - ttl_days * 86400.0
    kept = [
        (entry, size) for entry, size in lines
        if (parse_time(entry.get("ts")) or 0.0) >= cutoff
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
    write_json_atomic(
        meta_path,
        {"topic": topic, "next_seq": next_seq, "last_ts": last_ts},
        mode=0o600,
    )


_NO_DATA = object()


def topic_post(topic, from_identity, kind=None, text=None, data=_NO_DATA):
    """Append one entry and return it. Seq is unique and monotonic per topic."""
    validate_topic(topic)
    if kind is not None and not TOPIC_KIND_RE.match(kind):
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
            "ts": now_iso(),
            "topic": topic,
            "from_kind": from_identity.get("kind"),
            "from_name": from_identity.get("name"),
            "from_sid": from_identity.get("uuid"),
            "kind": kind,
        }
        if data is not _NO_DATA:
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


def topic_tail(topic, since=None, limit=TOPIC_TAIL_DEFAULT):
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
        meta = read_json(os.path.join(topics_dir(), name), None)
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
    then the Codex thread id. ``--as`` overrides when detection cannot work.
    """
    explicit = getattr(args, "as_identity", None)
    if explicit:
        ident = parse_typed(explicit)
    else:
        sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        rec = claude_record_by_socket(sock) if sock else None
        if rec and is_uuid(rec.get("sessionId")):
            return {"kind": "cc", "uuid": rec["sessionId"], "name": rec.get("name")}
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        tid = _thread_from_args(args)
        if is_uuid(sid):
            ident = {"kind": "cc", "uuid": sid}
        elif tid:
            ident = {"kind": "codex", "uuid": tid}
        else:
            return None
    if ident["kind"] == "cc":
        matches = claude_record_by_target(ident["uuid"])
        ident["name"] = matches[0].get("name") if len(matches) == 1 else None
    else:
        entry = read_registered().get(ident["uuid"])
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
    data, text = _NO_DATA, None
    try:
        if args.json_file:
            raw = message_from_args(
                argparse.Namespace(message=None, message_file=args.json_file)
            )
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise TopicError("--json-file is not valid JSON: %s" % exc)
        else:
            text = message_from_args(args)
    except UnicodeError:
        raise TopicError("the message is not valid UTF-8")
    except ValueError as exc:
        raise TopicError(str(exc))
    try:
        sender = topic_sender(args)
    except ValueError as exc:
        raise TopicError(str(exc), 2)
    if sender is None:
        log(
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
    if not 1 <= args.limit <= TOPIC_TAIL_MAX:
        raise TopicError("--limit must be between 1 and %d" % TOPIC_TAIL_MAX, 2)
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
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop a shim; with a target, unregister it")
    p_down.add_argument("target", nargs="?", metavar="name|uuid")
    p_down.set_defaults(func=cmd_down)

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
        help="total consecutive replies for this sequence (1..%d)" % BUDGET_ALLOW_MAX,
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

    def buddy_action(name, help_text):
        parser = buddy_sub.add_parser(name, help=help_text)
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
    p_buddy_set = buddy_action("set", "bind a buddy by name or UUID")
    p_buddy_set.add_argument(
        "target", metavar="[cc:|codex:|@]name|uuid", help="the peer to bind"
    )
    p_buddy_set.add_argument(
        "--uses",
        metavar="a,b",
        help="advisory scope (default: all of %s)" % ", ".join(BUDDY_USES),
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
        "--limit", type=int, default=TOPIC_TAIL_DEFAULT, metavar="N",
        help="at most N entries (default %d, maximum %d)"
        % (TOPIC_TAIL_DEFAULT, TOPIC_TAIL_MAX),
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
