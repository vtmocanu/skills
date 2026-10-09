"""Hooks for the session-peers CLI."""

from __future__ import annotations
import hashlib
import json
import os
import shlex
import stat
import sys
import time
from datetime import datetime, timezone
from . import constants as sp_constants, runtime as sp_runtime
from . import lifecycle as sp_lifecycle, requests as sp_requests, storage as sp_storage
from . import maintenance as sp_maintenance

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
    script = sp_runtime.entrypoint_path()
    log_path = os.path.join(sp_storage.state_dir(), "session-hook.log")
    try:
        command = [sys.executable, script, "hook-reconcile"]
        if (
            _args.auto_attach
            and source in ("startup", "resume")
            and sp_runtime.is_uuid(session_id)
        ):
            command.extend(["--thread", session_id])
        sp_runtime.spawn_detached(command, log_path)
    except OSError as exc:
        sp_runtime.log("could not start the reconcile: %s" % exc)
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
    days = sp_runtime._float_env("SESSION_PEERS_GC_DAYS", sp_constants.GC_DAYS_DEFAULT)
    try:
        sp_maintenance.gc_bridge_state(days=days, verbose=False)
    except ValueError as exc:
        sp_runtime.log("GC skipped: %s" % exc)
    sp_requests.cleanup_expired_requests()
    sp_lifecycle.reconcile(verbose=False)
    if args.thread:
        _attach_with_log(args.thread)
    return 0


def _attach_with_log(thread_id):
    """Retry the attach, then leave one log line saying why it gave up.

    The process is detached, so a long wait costs nothing. Why a fresh thread
    is not attachable at hook time is unproven (its state-DB row, writer lock
    or rollout may appear after the hook runs), so the failure line records
    the last observed reason instead of asserting a cause.
    """
    started = time.time()
    why = None
    while True:
        elapsed = time.time() - started
        pid, why = sp_lifecycle.attach_thread_with_reason(thread_id, verbose=False)
        if pid:
            return pid
        if elapsed >= sp_constants.HOOK_ATTACH_WAIT:
            break
        fast = elapsed < sp_constants.HOOK_ATTACH_FAST_WINDOW
        time.sleep(
            sp_constants.HOOK_ATTACH_FAST_STEP if fast else sp_constants.HOOK_ATTACH_SLOW_STEP
        )
    try:
        log_path = os.path.join(sp_storage.state_dir(), "session-hook.log")
        with open(log_path, "a", encoding="utf-8") as logfh:
            logfh.write(
                "hook-reconcile: gave up attaching thread=%s after %.0fs: %s\n"
                % (thread_id, time.time() - started, why or "unknown")
            )
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# install-hook
# --------------------------------------------------------------------------


def hook_command(script=None, auto_attach=False):
    """The exact command string the SessionStart entry runs.

    The path is quoted: a checkout under a directory with a space would
    otherwise split into two arguments and the hook would fail (P8).
    """
    script = script or sp_runtime.entrypoint_path()
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
    path = os.path.join(sp_storage.codex_home(), "hooks.json")
    os.makedirs(sp_storage.codex_home(), exist_ok=True)
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
    sp_runtime.write_json_atomic(path, root, mode=mode)
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
