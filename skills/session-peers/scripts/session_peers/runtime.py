"""Runtime for the session-peers CLI."""

from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from . import constants as sp_constants

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
    if len(value) > sp_constants.MAX_TEXT_CHARS:
        raise ValueError(
            "message is %d characters, over the %d cap"
            % (len(value), sp_constants.MAX_TEXT_CHARS)
        )
    size = utf8_len(value)
    if size > sp_constants.MAX_TEXT_CHARS:
        raise ValueError(
            "message is %d UTF-8 bytes, over the %d cap"
            % (size, sp_constants.MAX_TEXT_CHARS)
        )
    return value


def is_uuid(value) -> bool:
    return bool(value) and bool(sp_constants.UUID_RE.match(str(value)))


def parse_version(text):
    """Pull the first dotted-numeric run out of a `--version` line."""
    if not text:
        return None
    m = sp_constants.VERSION_RE.search(text)
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
        return sp_constants.MAX_TEXT_CHARS
    budget = arg_max - env_bytes() - 8192
    if sys.platform.startswith("linux"):
        # Linux caps ONE argv element at MAX_ARG_STRLEN (32 pages), far below
        # ARG_MAX; macOS has no separate per-argument limit.
        try:
            page = os.sysconf("SC_PAGESIZE")
        except (ValueError, OSError, AttributeError):
            page = 4096
        budget = min(budget, 32 * page - 1024)
    return max(4096, min(sp_constants.MAX_TEXT_CHARS, budget))


def runtime_code_files(script=None):
    """Launcher and runtime modules, resolving installed skill symlinks."""
    script = os.path.realpath(script or entrypoint_path())
    root = os.path.dirname(script)
    files = [script]
    package = os.path.join(root, "session_peers")
    if os.path.isdir(package):
        for directory, subdirs, names in os.walk(package):
            subdirs[:] = sorted(d for d in subdirs if d != "__pycache__")
            files.extend(os.path.join(directory, n) for n in sorted(names) if n.endswith(".py"))
    return files


def code_digest(files):
    """Source identity for diagnostics; None if the runtime cannot be read.

    Relative names make identical installations and symlink projections agree.
    Call once when a shim starts, never when it saves its state later.
    """
    if not files:
        return None
    paths = [os.path.realpath(path) for path in files]
    root = os.path.dirname(paths[0])
    digest = hashlib.sha256()
    try:
        for relative, path in sorted((os.path.relpath(path, root), path) for path in paths):
            with open(path, "rb") as fh:
                content = fh.read()
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except OSError:
        return None
    return digest.hexdigest()


LOADED_CODE_DIGEST = None  # Captured after eager package loading.


def entrypoint_path():
    """The stable executable sibling of this runtime package."""
    return os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, "peers.py"))
