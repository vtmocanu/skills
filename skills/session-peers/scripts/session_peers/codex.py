"""Codex for the session-peers CLI."""

from __future__ import annotations
import glob
import json
import os
import re
import sqlite3
from . import constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, process as sp_process, storage as sp_storage

# --------------------------------------------------------------------------
# Codex discovery
# --------------------------------------------------------------------------


def find_state_db(sqlite_home=None):
    """The state_*.sqlite whose `threads` schema we recognise, or None.

    The numeric suffix is a schema version, so the newest file is not always
    the readable one: pick by schema, never by the largest number.
    """
    home = sqlite_home or sp_storage.codex_sqlite_home()
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
        if sp_constants.THREADS_COLUMNS <= cols:
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
    candidates = [os.path.join(sp_storage.codex_home(), "session_index.jsonl")]
    alt = os.path.join(sp_storage.codex_sqlite_home(), "session_index.jsonl")
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
                when = sp_runtime.parse_time(obj.get("updated_at"))
                previous = seen_at.get(tid)
                if tid in out and previous is not None and when is not None:
                    if when < previous:
                        continue
                out[tid] = name
                if when is not None:
                    seen_at[tid] = when
    return out


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
    root = home or sp_storage.codex_home()
    return os.path.join(root, "thread-writer-locks", "%s.lock" % thread_id)


def codex_threads(check_live=True):
    """(threads, schema_ok). Each thread is a dict; degraded mode returns []."""
    db = find_state_db()
    if db is None:
        sp_runtime.log(
            "no state_*.sqlite with a recognised `threads` schema under %s; "
            "Codex discovery is unavailable (send by UUID still works)"
            % sp_storage.codex_sqlite_home()
        )
        return [], False
    rows = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    except sqlite3.Error as exc:
        sp_runtime.log("cannot open %s: %s" % (db, exc))
        return [], False
    try:
        cur = conn.execute(
            "SELECT id, name, rollout_path, cwd, updated_at FROM threads"
        )
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        sp_runtime.log("cannot read threads from %s: %s" % (db, exc))
        return [], False
    finally:
        conn.close()

    index = read_session_index()
    registered = sp_storage.read_registered()
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
        holders, verified, liveness_error = sp_process.lsof_holders_checked(probe)
        for t in threads:
            if not verified:
                t["live"] = None
                t["liveness_error"] = liveness_error
                continue
            # Either handle a live Codex process keeps proves the thread is
            # live; the lock covers a fresh thread whose rollout is not written
            # yet, the rollout covers an older Codex with no writer lock.
            found = (holders.get(sp_process.canon_path(t["rollout_path"])) or []) or (
                holders.get(sp_process.canon_path(lock_of[t["id"]])) or []
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
    holders, verified, _error = sp_process.lsof_holders_checked(paths)
    if not verified:
        return None, None
    found = []
    for p in paths:
        found += holders.get(sp_process.canon_path(p)) or []
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


class ResolveAmbiguousKind(ResolveError):
    """A bare target that could be either kind; ``choices`` are the typed
    targets (``cc:x``, ``codex:x``) that would each resolve it."""

    def __init__(self, message, choices):
        super().__init__(message)
        self.choices = choices


def missing_thread_message(thread_id):
    """Why discovery found no thread for a UUID, naming what it looked for."""
    db = find_state_db()
    evidence = ["no row in the threads table of %s" % (db or "the Codex state DB")]
    lock = writer_lock_path(thread_id)
    held = False
    if not os.path.exists(lock):
        evidence.append("no writer lock at %s" % lock)
    else:
        holders, verified, error = sp_process.lsof_holders_checked([lock])
        found = holders.get(sp_process.canon_path(lock)) or []
        if found:
            held = True
            evidence.append("writer lock %s held by pid %s" % (lock, found[0][0]))
        elif verified:
            evidence.append("writer lock %s present but not held" % lock)
        else:
            evidence.append("writer lock %s holder unverified (%s)" % (lock, error))
    pattern = os.path.join(sp_storage.codex_home(), "sessions", "*", "*", "*", "rollout-*%s.jsonl" % thread_id)
    rollouts = glob.glob(pattern)
    if rollouts:
        evidence.append("rollout %s exists" % rollouts[0])
    else:
        evidence.append("no rollout under %s" % os.path.join(sp_storage.codex_home(), "sessions"))
    for home in sp_storage.other_homes():
        with sp_storage.codex_home_override(home):
            other_db = find_state_db()
        evidence.append(
            "also searched %s (%s)"
            % (home, "no row in %s" % other_db if other_db else "no recognised state DB")
        )
    message = "no Codex thread with id %s: %s." % (thread_id, "; ".join(evidence))
    if held:
        message += (
            " A Codex process holds this thread, but discovery reads the "
            "state-DB row, so `up` cannot attach the thread until Codex "
            "writes one."
        )
    return message + " Check `peers.py list` and `peers.py doctor`."


def threads_in_other_homes(known_ids=()):
    """Threads from the other candidate homes, each tagged with ``codex_home``.

    Searches ``sp_storage.other_homes()`` under that home's own state DB and
    writer locks, so liveness and `codex queue` use the right home. A home
    without a recognised state DB contributes nothing (silently: it is a
    search, not the caller's own home). A thread already in ``known_ids`` or
    seen in an earlier home is skipped, so the first home wins.
    """
    seen = set(known_ids)
    found = []
    for home in sp_storage.other_homes():
        with sp_storage.codex_home_override(home):
            if find_state_db() is None:
                continue
            threads, schema_ok = codex_threads()
        if not schema_ok:
            continue
        for t in threads:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            t = dict(t)
            t["codex_home"] = home
            found.append(t)
    return found


def resolve_thread(target, require_live=True, exclude=None):
    """Turn `<name|uuid>` into one thread dict, or raise ResolveError.

    D6/R7: a name held by more than one live thread is refused rather than
    guessed at; `codex queue`'s own name matching picks a match, so the bridge
    never delegates the decision. ``exclude`` (a thread UUID) drops that thread
    from name matching only; a UUID target is never excluded.
    """
    threads, schema_ok = codex_threads()
    if sp_runtime.is_uuid(target):
        for t in threads:
            if t["id"] == target:
                return t
        for t in threads_in_other_homes(t["id"] for t in threads):
            if t["id"] == target:
                return t
        if not schema_ok:
            # D11 degraded mode: queue by UUID, liveness unverified.
            return {
                "id": target,
                "name": None,
                "rollout_path": None,
                "cwd": None,
                "updated_at": None,
                "registered": target in sp_storage.read_registered(),
                "holder_pid": None,
                "live": None,
                "degraded": True,
            }
        raise ResolveNotFound(missing_thread_message(target))
    # A name, alias or prefix is matched across every candidate home at once,
    # so a live match in two homes is ambiguous instead of silently resolved
    # to the caller's.
    threads = threads + threads_in_other_homes(t["id"] for t in threads)
    if not threads and not schema_ok:
        raise ResolveError(
            "Codex thread discovery is unavailable (unknown state_*.sqlite "
            "schema); pass the thread UUID instead of a name"
        )
    if exclude:
        threads = [t for t in threads if t["id"] != exclude]
    # Match the name from the state DB / session index, OR from our own
    # registration: `up <uuid>` records name->uuid, and a later `/rename` may
    # not have propagated to the DB's `name` column yet (measured on
    # codex-cli 0.153.4), so a thread we already registered under this name
    # must still resolve. Union by id, so a thread matched both ways counts once.
    reg = sp_storage.read_registered()
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
    if sp_runtime.is_uuid(prefix_target):
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
        searched = sp_storage.other_homes()
        raise ResolveNotFound(
            "no Codex thread named %r or matching that ID prefix%s; run `peers.py list` "
            "for current UUIDs, or /rename it in the TUI"
            % (target, " (also searched %s)" % ", ".join(searched) if searched else "")
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


def resolve_thread_prefer_live(target, exclude=None):
    """Resolve for commands that also act on a stopped thread (`budget`, `down`).

    Codex's title suggester reuses names, so a bare name usually also matches
    past threads; resolving across dead threads first made a unique LIVE name
    ambiguous. Prefer the live match; only when there is none, fall back to any
    thread with that name. Raises the more specific ResolveError otherwise.
    """
    try:
        return resolve_thread(target, require_live=True, exclude=exclude)
    except ResolveError as live_error:
        try:
            return resolve_thread(target, require_live=False, exclude=exclude)
        except ResolveError:
            raise live_error


def codex_title_owner(thread_name, threads=None):
    """Lowest live UUID for a title, providing a stable duplicate tiebreak."""
    if not sp_protocol.valid_peer_name(thread_name):
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
    records = sp_claude.live_claude_records() if records is None else records
    occupied = {
        rec.get("name")
        for rec in records
        if rec.get("sessionId") != thread_id and rec.get("name")
    }
    candidates = []
    if sp_protocol.valid_peer_name(thread_name) and title_owner in (None, thread_id):
        candidates.append(str(thread_name))
    candidates.extend(
        ["codex-%s" % thread_id[:8], "codex-%s" % thread_id]
    )
    for candidate in candidates:
        if sp_protocol.valid_peer_name(candidate) and candidate not in occupied:
            return candidate
    raise sp_protocol.NameError_("no unique peer alias is available for thread %s" % thread_id)


# --------------------------------------------------------------------------
# Queueing into a Codex thread
# --------------------------------------------------------------------------


class QueueError(Exception):
    pass


def codex_queue(thread_id, text, cwd=None, home=None):
    """`codex queue --thread <uuid> --message <text>`, run in `stable_dir(cwd)`.

    rc != 0, or "No active session" on stderr, means the thread is not live.
    Pass the thread's own cwd; a deleted one falls back to `$HOME`. ``home``
    is the thread's CODEX_HOME when it differs from the caller's.
    """
    budget = sp_runtime.argv_text_budget()
    size = sp_runtime.utf8_len(text)
    if size > budget:
        raise QueueError(
            "message is %d bytes, over the %d cap this machine can pass to "
            "`codex queue` (Codex itself stops at %d characters)"
            % (size, budget, sp_constants.MAX_TEXT_CHARS)
        )
    if len(text) > sp_constants.MAX_TEXT_CHARS:
        raise QueueError(
            "message is %d characters, over Codex's %d cap"
            % (len(text), sp_constants.MAX_TEXT_CHARS)
        )
    rc, out, err = sp_runtime.run_cmd(
        ["codex", "queue", "--thread", str(thread_id), "--message", text],
        timeout=60,
        cwd=sp_runtime.stable_dir(cwd),
        env=dict(os.environ, CODEX_HOME=home) if home else None,
    )
    if rc == 127:
        raise QueueError("codex is not on PATH")
    blob = "%s\n%s" % (out, err)
    if rc != 0 or "No active session" in blob:
        raise QueueError(
            "codex queue failed (rc %d): %s" % (rc, (err or out).strip() or "no output")
        )
    return out.strip()


def _codex_holder_pid(thread_id):
    threads, _schema_ok = codex_threads()
    for thread in threads:
        if thread.get("id") == thread_id:
            return thread.get("holder_pid")
    return None
