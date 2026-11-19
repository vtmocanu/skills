"""Reading progress only, never delivery ACKs, claims or handover authority."""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from . import channels, constants, runtime


def generation(key):
    rec = channels.read_channel(key)
    return rec.get("created_at") if rec else "general"


def paths(key, reader):
    channels.parse_name(key)
    digest = hashlib.sha256((reader + "\0" + key).encode()).hexdigest()
    base = os.path.join(channels.channels_root(), "reads")
    return base, os.path.join(base, digest + ".json"), os.path.join(base, digest + ".lock")


def cursor(key, reader):
    from . import webdata
    _base, path, _lock = paths(key, reader)
    try:
        fd, st = webdata._open_regular(path)
        if fd is None:
            return 0
        try:
            if st.st_size > 4096:
                return 0
            value = json.loads(os.pread(fd, 4096, 0))
        finally:
            os.close(fd)
        seq = value.get("seq")
        return seq if value.get("generation") == generation(key) and type(seq) is int and seq >= 0 else 0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


@contextlib.contextmanager
def locked(key, reader):
    base, _path, lock = paths(key, reader)
    os.makedirs(base, mode=0o700, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 0.5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise channels.ChannelError("another unread read is in progress; try again")
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def advance(key, reader, seq):
    _base, path, _lock = paths(key, reader)
    runtime.write_json_atomic(path, {"generation": generation(key), "seq": max(cursor(key, reader), seq)}, mode=0o600)


def summaries(reader):
    """Existing bounded read-only history reader, no ACK lookups or writes."""
    from . import webdata
    budget, rows = webdata.Budget(), []
    for key in channels.list_keys()[:constants.CHANNEL_MAX_CHANNELS]:
        rec = webdata._read_record(key, budget)
        if not rec or reader not in rec["members"] or channels.delivery_mode(rec) != "mentions":
            continue
        since = cursor(key, reader)
        snap = webdata.HistorySnapshot(key, budget).read(since=since)
        if snap.partial or snap.unavailable:
            continue  # an incomplete count is not a complete unread total
        posts = [e for e in snap.items if e.get("seq", 0) > since and e.get("kind") == channels.EVENT_SEND
                 and (e.get("data") or {}).get("sender") != reader]
        if posts:
            mentioning = sum(bool(reader in (e["data"].get("mentioned") or []) or e["data"].get("mention_all")
                                  or e["data"].get("mention_here")) for e in posts)
            rows.append("#%s: %d unread (%d mentioning you)" % (key, len(posts), mentioning))
    return rows


def print_summary():
    """One bounded child, fail silent, even if a peer planted a blocking file."""
    process = None
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).parents[1] / "peers.py"),
                                    "channel", "unread-summary"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        out, _err = process.communicate(timeout=1)
        if process.returncode == 0 and len(out) <= 16384:
            text = out.decode("utf-8").strip()
            if text:
                print(text)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=0.2)
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
