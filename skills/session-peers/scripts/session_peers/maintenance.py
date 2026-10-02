"""Maintenance for the session-peers CLI."""

from __future__ import annotations
import os
import sys
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, lifecycle as sp_lifecycle, requests as sp_requests, storage as sp_storage
from . import topics as sp_topics

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
        sp_topics.topic_prune_all()
    for request_id in requests:
        print("%s expired request %s" % ("would prune" if args.dry_run else "pruned", request_id))
    if not removed and not requests and not buddies:
        print("no stale bridge metadata")
    return 0
