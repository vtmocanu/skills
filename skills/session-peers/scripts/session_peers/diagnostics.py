"""Diagnostics for the session-peers CLI."""

from __future__ import annotations
import time
from . import constants as sp_constants, runtime as sp_runtime
from . import storage as sp_storage

def warn_versions(kinds=("claude", "codex")) -> None:
    """D11: warn once per installed/pinned pair per day, never fail a run."""
    previous = sp_runtime.read_json(sp_storage.version_warning_path(), {}) or {}
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
        if now - last < sp_constants.VERSION_WARNING_WINDOW:
            return
        sp_runtime.log(message)
        previous[key] = now
        changed = True

    if "claude" in kinds:
        v = sp_runtime.tool_version("claude")
        if v and sp_runtime.version_is_newer(v, sp_constants.CLAUDE_CODE_TESTED):
            warn_once(
                "claude:%s>%s" % (v.strip(), sp_constants.CLAUDE_CODE_TESTED),
                "Claude Code %s is newer than the tested %s; if peers stop "
                "appearing, re-run references/spike-checklist.md"
                % (v.strip(), sp_constants.CLAUDE_CODE_TESTED)
            )
    if "codex" in kinds:
        v = sp_runtime.tool_version("codex")
        if v and sp_runtime.version_is_newer(v, sp_constants.CODEX_TESTED):
            warn_once(
                "codex:%s>%s" % (v.strip(), sp_constants.CODEX_TESTED),
                "Codex CLI %s is newer than the tested %s; if discovery breaks, "
                "re-run references/spike-checklist.md" % (v.strip(), sp_constants.CODEX_TESTED)
            )
    if changed:
        try:
            sp_runtime.write_json_atomic(sp_storage.version_warning_path(), previous)
        except OSError:
            pass

import json
import os
import socket
from . import config as sp_config, constants as sp_constants, protocol as sp_protocol, runtime as sp_runtime
from . import claude as sp_claude, codex as sp_codex, lifecycle as sp_lifecycle, process as sp_process, storage as sp_storage
from . import hooks as sp_hooks, maintenance as sp_maintenance

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(args):
    warn_versions()
    installed_digest = sp_runtime.code_digest(sp_runtime.runtime_code_files())
    all_records = sp_claude.read_claude_records()
    classified = [(record, sp_claude.record_liveness(record)) for record in all_records]
    records = [record for record, status in classified if status == "live"]
    unverified = [
        record for record, status in classified if status == "unverified"
    ]
    threads, schema_ok = sp_codex.codex_threads()
    registered = sp_storage.read_registered()

    def codex_title(record):
        """The Codex thread title a shim's peer record fronts, else None."""
        if record.get("entrypoint") != "codex" or not record.get("sessionId"):
            return None
        state = sp_runtime.read_json(sp_storage.thread_state_path(record["sessionId"]), {})
        title = state.get("thread_name") if isinstance(state, dict) else None
        return title if isinstance(title, str) and title else None

    def claude_view(record):
        return {
            "runtime": "codex" if record.get("entrypoint") == "codex" else "claude",
            "thread_title": codex_title(record),
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
        pid = sp_lifecycle.shim_pid(thread["id"])
        state = sp_runtime.read_json(sp_storage.thread_state_path(thread["id"]), {}) if pid else {}
        alias = state.get("name") if isinstance(state, dict) else None
        if not alias:
            try:
                alias = sp_codex.peer_name_for_thread(
                    thread.get("name"),
                    thread["id"],
                    records=records,
                    title_owner=sp_codex.codex_title_owner(thread.get("name"), threads),
                )
            except sp_protocol.NameError_:
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
            "shim_code_status": sp_lifecycle.shim_code_status(thread["id"], pid, installed_digest) if pid else None,
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
        "socket_dir": sp_claude.default_socket_dir(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("Claude sessions (%d live):" % len(claude))
    for c in claude:
        label = c["name"] or "(unnamed)"
        if c["runtime"] == "codex":
            label += ' [Codex thread%s]' % (
                ' "%s"' % c["thread_title"] if c["thread_title"] else ", untitled"
            )
        print("  %-24s pid %-7s %-6s %s" % (label, c["pid"], c["status"] or "?", c["cwd"] or ""))
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
                (", shim %s, code %s%s" % (
                    t["shim_pid"], t["shim_code_status"],
                    " (run peers.py restart %s)" % t["id"] if t["shim_code_status"] == "stale" else "",
                )) if t["shim_pid"] else "",
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
    installed_digest = sp_runtime.code_digest(sp_runtime.runtime_code_files())
    lines = []

    def add(status, text):
        lines.append("%-5s %s" % (status, text))

    _doctor_environment(add)

    _doctor_peers(add, installed_digest)

    _doctor_hooks(add)

    print("\n".join(lines))
    return 0


def _doctor_environment(add):
    claude_v = sp_runtime.tool_version("claude")
    codex_v = sp_runtime.tool_version("codex")
    add(
        "warn" if claude_v and sp_runtime.version_is_newer(claude_v, sp_constants.CLAUDE_CODE_TESTED) else "ok",
        "Claude Code %s (tested against %s)"
        % (claude_v.strip() if claude_v else "not on PATH", sp_constants.CLAUDE_CODE_TESTED),
    )
    add(
        "warn" if codex_v and sp_runtime.version_is_newer(codex_v, sp_constants.CODEX_TESTED) else "ok",
        "Codex CLI %s (tested against %s)"
        % (codex_v.strip() if codex_v else "not on PATH", sp_constants.CODEX_TESTED),
    )
    add("ok" if codex_v else "fail", "codex on PATH")
    rc, _out, _err = sp_runtime.run_cmd(["lsof", "-v"], timeout=10)
    add("ok" if rc != 127 else "fail", "lsof on PATH (thread liveness needs it)")
    _started, ps_error = sp_process.proc_start_checked(os.getpid())
    add(
        "fail" if ps_error else "ok",
        "process-start probe%s"
        % (": unavailable (%s)" % ps_error if ps_error else ""),
    )
    add("ok", "CLAUDE_CONFIG_DIR: %s" % sp_storage.claude_config_dir())
    add("ok", "CODEX_HOME: %s" % sp_storage.codex_home())
    if sp_storage.codex_sqlite_home() != sp_storage.codex_home():
        add("ok", "codex sqlite_home: %s" % sp_storage.codex_sqlite_home())



def _doctor_peers(add, installed_digest):
    sock_dir = sp_claude.default_socket_dir()
    add(
        "ok" if sp_claude.dir_is_allowlisted(sock_dir) else "warn",
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

    db = sp_codex.find_state_db()
    add(
        "ok" if db else "warn",
        "Codex state database: %s"
        % (db or "none with a recognised `threads` schema (send by UUID only)"),
    )
    threads, _ok = sp_codex.codex_threads()
    unverified_threads = [
        thread for thread in threads if thread.get("live") is None
    ]
    if unverified_threads:
        detail = unverified_threads[0].get("liveness_error") or "lsof failed"
        add("fail", "Codex liveness probe unavailable: %s" % detail)
        add("warn", "bridge GC skipped while Codex liveness is unverified")
    elif db:
        stale = sp_maintenance.gc_bridge_state(
            days=sp_runtime._float_env("SESSION_PEERS_GC_DAYS", sp_constants.GC_DAYS_DEFAULT),
            dry_run=True,
            verbose=False,
        )
        add(
            "warn" if stale else "ok",
            "bridge GC: %d stale thread%s"
            % (len(stale), "" if len(stale) == 1 else "s"),
        )

    def running_shim(name, tid, pid):
        status = sp_lifecycle.shim_code_status(tid, pid, installed_digest)
        detail = "; code %s" % status
        if status == "stale":
            detail += " (run peers.py restart %s)" % tid
        add("ok" if status == "current" else "warn", "%s: shim running (pid %d)%s" % (name, pid, detail))

    registered = sp_storage.read_registered()
    live_ids = {t["id"] for t in threads if t.get("live") is True}
    unknown_ids = {t["id"] for t in threads if t.get("live") is None}
    for tid in sorted(registered):
        name = registered[tid].get("name") or tid
        pid = sp_lifecycle.shim_pid(tid)
        if pid:
            running_shim(name, tid, pid)
        elif tid in live_ids:
            add("warn", "%s: thread is live but no shim (run `peers.py up`)" % name)
        elif tid in unknown_ids:
            add("warn", "%s: thread liveness unverified" % name)
        else:
            add("ok", "%s: registered, thread not running" % name)
    for thread in threads:
        if thread["id"] not in registered:
            pid = sp_lifecycle.shim_pid(thread["id"])
            if pid:
                running_shim(thread.get("name") or thread["id"], thread["id"], pid)
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
            and not sp_lifecycle.shim_pid(t["id"])
        ),
        key=lambda t: t["id"],
    ):
        add(
            "warn",
            "%s: live Codex thread, not attached (run `peers.py up %s`)"
            % (thread.get("name") or thread["id"], thread["id"]),
        )



def _doctor_hooks(add):
    hooks_path = os.path.join(sp_storage.codex_home(), "hooks.json")
    data = sp_runtime.read_json(hooks_path, None)
    if data is None:
        add("ok", "no %s (the SessionStart hook is optional)" % hooks_path)
    else:
        events, _root = sp_hooks._hooks_event_map(data)
        entries = events.get("SessionStart") or []
        cfg = sp_config.read_toml_lite(sp_storage.codex_config_path())
        found = False
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for j, hook in enumerate(entry.get("hooks") or []):
                if (
                    not isinstance(hook, dict)
                    or hook.get("command")
                    not in {sp_hooks.hook_command(), sp_hooks.hook_command(auto_attach=True)}
                ):
                    continue
                found = True
                mode = (
                    "auto-attach"
                    if hook.get("command") == sp_hooks.hook_command(auto_attach=True)
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
                        % (mode, sp_hooks.entry_hash(entry)[:12]),
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
