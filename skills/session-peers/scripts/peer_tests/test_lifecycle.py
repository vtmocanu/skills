"""Regression coverage for lifecycle."""

from __future__ import annotations

import contextlib
from datetime import datetime
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import time
from datetime import timezone
import uuid as uuidlib
from .support import (
    Base,
    HERE,
    PEERS,
    new_uuid,
    peers,
    sp_codex,
    sp_lifecycle,
    sp_maintenance,
    sp_process,
    sp_runtime,
    sp_storage,
    wait_for,
    wait_pid_gone,
)


class TestRegistration(Base):
    def test_registration_persists_the_uuid_not_the_name(self):
        tid, _r = self.one_thread(name="codex-uzi")
        sp_storage.register_thread(sp_codex.resolve_thread("codex-uzi"))
        registered = sp_storage.read_registered()
        self.assertIn(tid, registered)
        self.assertEqual(registered[tid]["name"], "codex-uzi")

    def test_unregister_removes_only_that_thread(self):
        sp_storage.write_registered({"a": {"name": "x"}, "b": {"name": "y"}})
        self.assertTrue(sp_storage.unregister_thread("a"))
        self.assertEqual(sorted(sp_storage.read_registered()), ["b"])
        self.assertFalse(sp_storage.unregister_thread("zzz"))

    def test_reconcile_ignores_a_live_thread_that_is_not_registered(self):
        self.one_thread(name="codex-uzi")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            started = sp_lifecycle.reconcile()
        self.assertEqual(started, 0)
        self.assertIn("no registered threads", out.getvalue())

    def test_reconcile_skips_a_registered_thread_that_is_not_live(self):
        tid, _r = self.one_thread()
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        self.clear_holders()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sp_lifecycle.reconcile(), 0)
        self.assertIn("not live", out.getvalue())

    def test_up_registers_and_starts_exactly_one_shim(self):
        tid, _r = self.one_thread(name="codex-uzi")
        rc, out, _err = self.cli("up", "codex-uzi")
        self.assertEqual(rc, 0)
        self.assertIn("registered", out)
        pid = wait_for(lambda: sp_lifecycle.shim_pid(tid))
        self.assertIsNotNone(pid, "no shim came up")
        rc, out, _err = self.cli("up")
        self.assertEqual(rc, 0)
        self.assertIn("already running", out)
        self.assertEqual(sp_lifecycle.shim_pid(tid), pid)

    def test_down_stops_the_shim_and_unregisters(self):
        tid, _r = self.one_thread(name="codex-uzi")
        self.cli("up", "codex-uzi")
        pid = wait_for(lambda: sp_lifecycle.shim_pid(tid))
        self.assertIsNotNone(pid)
        rc, out, _err = self.cli("down", "codex-uzi")
        self.assertEqual(rc, 0)
        self.assertIn("unregistered", out)
        self.assertEqual(sp_storage.read_registered(), {})
        self.assertTrue(wait_pid_gone(pid), "the shim survived `down`")

    def test_bare_down_stops_shims_but_keeps_registrations(self):
        tid, _r = self.one_thread(name="codex-uzi")
        self.cli("up", "codex-uzi")
        wait_for(lambda: sp_lifecycle.shim_pid(tid))
        rc, out, _err = self.cli("down")
        self.assertEqual(rc, 0)
        self.assertIn("registrations kept", out)
        self.assertIn(tid, sp_storage.read_registered())

    def test_a_pidfile_nothing_holds_is_never_signalled(self):
        # B2: the old code SIGTERMed whatever pid the file named. A pidfile no
        # process holds the lock on is stale by definition: drop it, kill
        # nothing. Demonstrated against a live, unrelated pid.
        tid, _r = self.one_thread()
        bystander = os.getppid()
        pathlib.Path(sp_storage.thread_pid_path(tid)).write_text("%d\n" % bystander)
        self.assertIsNone(sp_lifecycle.shim_pid(tid))
        self.assertFalse(sp_lifecycle.stop_shim(tid))
        self.assertTrue(sp_process.pid_alive(bystander), "a bystander was signalled")

    def test_a_probe_never_unlinks_a_pidfile_a_shim_is_about_to_lock(self):
        # Unlinking a lock-free pidfile would race a starting shim between its
        # open() and its flock(), leaving it holding an unlinked inode.
        tid, _r = self.one_thread()
        path = pathlib.Path(sp_storage.thread_pid_path(tid))
        path.write_text("%d\n" % os.getppid())
        self.assertIsNone(sp_lifecycle.shim_pid(tid))
        self.assertTrue(path.exists())

    def test_a_held_pidfile_whose_record_names_another_thread_is_ignored(self):
        tid, _r = self.one_thread()
        pid = self.hold_pidfile(tid)
        (self.sessions / ("%d.json" % pid)).write_text(
            json.dumps({"pid": pid, "entrypoint": "codex",
                        "sessionId": "a-different-thread"})
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(sp_lifecycle.shim_pid(tid))
        self.assertIn("names another thread", err.getvalue())

    def test_a_recycled_pid_does_not_block_a_restart(self):
        # B2: a stale pidfile naming a live unrelated pid used to read as "a
        # shim is running", so reconcile refused to start the real one.
        tid, _r = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        pathlib.Path(sp_storage.thread_pid_path(tid)).write_text("%d\n" % os.getppid())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            started = sp_lifecycle.reconcile()
        self.assertEqual(started, 1, out.getvalue())
        self.assertIsNotNone(sp_lifecycle.shim_pid(tid))

    def _live_and_dead_namesakes(self, name="helper"):
        """One live thread and two past threads that carry the same name."""
        live_tid, dead_a, dead_b = (str(uuidlib.uuid4()) for _ in range(3))
        live_rollout = self.make_rollout(name="live.jsonl", lines=[])
        dead_rollouts = [
            self.make_rollout(name="dead-%d.jsonl" % i, lines=[]) for i in range(2)
        ]
        self.make_state_db([
            {"id": dead_a, "name": name, "rollout_path": str(dead_rollouts[0])},
            {"id": live_tid, "name": name, "rollout_path": str(live_rollout)},
            {"id": dead_b, "name": name, "rollout_path": str(dead_rollouts[1])},
        ])
        self.set_holder(live_rollout)
        return live_tid, (dead_a, dead_b)

    def test_up_on_an_unknown_uuid_names_the_evidence_it_checked(self):
        self.make_state_db([])
        tid = new_uuid()
        rc, _out, err = self.cli("up", tid)
        self.assertEqual(rc, 1)
        self.assertIn("no Codex thread with id %s" % tid, err)
        self.assertIn("no row in the threads table", err)
        self.assertIn("no writer lock at", err)
        self.assertIn("no rollout under", err)
        self.assertIn("`peers.py list` and `peers.py doctor`", err)
        self.assertNotIn("retry", err.lower())
        self.assertEqual(sp_storage.read_registered(), {})

    def test_up_on_a_locked_thread_without_a_state_row_says_so(self):
        # Seen on Codex 0.158.0: the writer lock is held before the thread's
        # state-DB row exists, and discovery reads only the row.
        self.make_state_db([])
        tid = new_uuid()
        self.set_holder(sp_codex.writer_lock_path(tid), pid=4321)
        rc, _out, err = self.cli("up", tid)
        self.assertEqual(rc, 1)
        self.assertIn("held by pid 4321", err)
        self.assertIn("cannot attach the thread until Codex writes one", err)
        self.assertIn("`peers.py list` and `peers.py doctor`", err)
        self.assertNotIn("retry", err.lower())

    def test_budget_reset_by_name_picks_the_live_thread_over_past_namesakes(self):
        # Codex reuses thread titles: the live `helper` shared its name with
        # past threads, and resolving across dead ones made it "ambiguous",
        # reported as the misleading "no thread matches 'helper'".
        live_tid, dead = self._live_and_dead_namesakes()
        rc, out, err = self.cli("budget", "reset", "helper")
        self.assertEqual(rc, 0, err)
        self.assertIn(live_tid, out)
        self.assertTrue(os.path.exists(sp_storage.budget_reset_path(live_tid)))
        for tid in dead:
            self.assertFalse(os.path.exists(sp_storage.budget_reset_path(tid)))

    def test_budget_reset_reports_the_real_resolution_error(self):
        self.one_thread(name="someone-else")
        rc, _out, err = self.cli("budget", "reset", "no-such-name")
        self.assertEqual(rc, 1)
        self.assertIn("no Codex thread named 'no-such-name'", err)

    def test_down_by_name_picks_the_live_thread_over_past_namesakes(self):
        live_tid, _dead = self._live_and_dead_namesakes()
        rc, _out, err = self.cli("down", "helper")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("no thread matches", err)

    def test_budget_reset_writes_the_marker_and_clears_the_state_file(self):
        tid, _r = self.one_thread()
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(tid), {"thread_id": tid, "budgets": {"s1": 3}}
        )
        rc, out, _err = self.cli("budget", "reset", tid)
        self.assertEqual(rc, 0)
        self.assertIn("reset", out)
        self.assertTrue(os.path.exists(sp_storage.budget_reset_path(tid)))
        state = sp_runtime.read_json(sp_storage.thread_state_path(tid))
        self.assertEqual(state["budgets"], {})


class TestGarbageCollection(Base):
    def make_stale_bridge_thread(self, live=False):
        tid = str(uuidlib.uuid4())
        rollout = self.make_rollout("%s-rollout.jsonl" % tid)
        old = time.time() - 8 * 86400
        self.make_state_db(
            [
                {
                    "id": tid,
                    "name": "old-peer",
                    "rollout_path": str(rollout),
                    "updated_at": old,
                }
            ]
        )
        if live:
            self.set_holder(rollout)
        sp_storage.write_registered(
            {
                tid: {
                    "name": "old-peer",
                    "registered_at": datetime.fromtimestamp(
                        old, timezone.utc
                    ).isoformat(),
                }
            }
        )
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(tid),
            {
                "thread_id": tid,
                "name": "old-peer",
                "updated_at": datetime.fromtimestamp(old, timezone.utc).isoformat(),
            },
        )
        for path in (
            sp_storage.thread_state_path(tid),
            sp_storage.thread_log_path(tid),
            sp_storage.thread_pid_path(tid),
            sp_storage.budget_reset_path(tid),
        ):
            pathlib.Path(path).touch()
            os.utime(path, (old, old))
        return tid, rollout

    def test_gc_prunes_only_stale_bridge_metadata(self):
        tid, rollout = self.make_stale_bridge_thread()
        old = time.time() - 8 * 86400
        shared_paths = (
            sp_storage.registered_path(),
            os.path.join(sp_storage.state_dir(), "reconcile.lock"),
            os.path.join(sp_storage.state_dir(), "session-hook.log"),
        )
        pathlib.Path(shared_paths[1]).write_text("shared lock sentinel")
        pathlib.Path(shared_paths[2]).write_text("shared log sentinel")
        for path in shared_paths:
            os.utime(path, (old, old))

        removed = sp_maintenance.gc_bridge_state(days=7, verbose=False)
        self.assertEqual(removed, [tid])
        self.assertNotIn(tid, sp_storage.read_registered())
        for path in (
            sp_storage.thread_state_path(tid),
            sp_storage.thread_log_path(tid),
            sp_storage.thread_pid_path(tid),
            sp_storage.budget_reset_path(tid),
        ):
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(rollout.exists(), "GC touched a Codex rollout")
        for path in shared_paths:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(
            pathlib.Path(shared_paths[1]).read_text(), "shared lock sentinel"
        )
        self.assertEqual(
            pathlib.Path(shared_paths[2]).read_text(), "shared log sentinel"
        )

    def test_gc_dry_run_changes_nothing(self):
        tid, _rollout = self.make_stale_bridge_thread()
        removed = sp_maintenance.gc_bridge_state(days=7, dry_run=True, verbose=False)
        self.assertEqual(removed, [tid])
        self.assertIn(tid, sp_storage.read_registered())
        self.assertTrue(os.path.exists(sp_storage.thread_state_path(tid)))

    def test_gc_never_prunes_a_live_thread(self):
        tid, _rollout = self.make_stale_bridge_thread(live=True)
        self.assertEqual(sp_maintenance.gc_bridge_state(days=7, verbose=False), [])
        self.assertIn(tid, sp_storage.read_registered())

    def test_gc_uses_latest_activity_not_registration_age(self):
        tid, _rollout = self.make_stale_bridge_thread()
        pathlib.Path(sp_storage.thread_log_path(tid)).touch()
        self.assertEqual(sp_maintenance.gc_bridge_state(days=7, verbose=False), [])
        self.assertIn(tid, sp_storage.read_registered())

    def test_gc_rechecks_recency_after_taking_the_lock(self):
        tid, _rollout = self.make_stale_bridge_thread()
        original = sp_storage.reconcile_lock

        @contextlib.contextmanager
        def activity_during_lock(*args, **kwargs):
            with original(*args, **kwargs) as acquired:
                pathlib.Path(sp_storage.thread_log_path(tid)).touch()
                yield acquired

        sp_storage.reconcile_lock = activity_during_lock
        try:
            removed = sp_maintenance.gc_bridge_state(days=7, verbose=False)
        finally:
            sp_storage.reconcile_lock = original
        self.assertEqual(removed, [])
        self.assertIn(tid, sp_storage.read_registered())

    def test_gc_fails_closed_when_thread_discovery_is_unknown(self):
        tid = str(uuidlib.uuid4())
        sp_storage.write_registered(
            {tid: {"name": "old", "registered_at": "2020-01-01T00:00:00Z"}}
        )
        self.make_state_db([], filename="state_1.sqlite", good=False)
        self.assertEqual(sp_maintenance.gc_bridge_state(days=7, verbose=False), [])
        self.assertIn(tid, sp_storage.read_registered())

    def test_gc_fails_closed_when_lsof_is_blocked(self):
        tid, _rollout = self.make_stale_bridge_thread()
        os.environ["FAKE_LSOF_RC"] = "126"
        with contextlib.redirect_stderr(io.StringIO()):
            removed = sp_maintenance.gc_bridge_state(days=7, verbose=False)
        self.assertEqual(removed, [])
        self.assertIn(tid, sp_storage.read_registered())
        self.assertTrue(os.path.exists(sp_storage.thread_state_path(tid)))

    def test_gc_rejects_a_negative_retention(self):
        self.make_state_db([])
        rc, _out, err = self.cli("gc", "--days", "-1")
        self.assertEqual(rc, 2)
        self.assertIn("zero or greater", err)


class TestList(Base):
    def test_list_json_reports_both_sides(self):
        self.write_record(os.getpid(), "cc-main", "s1", str(self.socks / "1.sock"))
        tid, _r = self.one_thread(name="codex-uzi")
        rc, out, _err = self.cli("list", "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual([c["name"] for c in payload["claude"]], ["cc-main"])
        self.assertEqual(payload["codex"][0]["id"], tid)
        self.assertEqual(payload["codex"][0]["holder_pid"], os.getpid())
        self.assertFalse(payload["codex"][0]["registered"])
        self.assertTrue(payload["codex_schema_recognised"])
        self.assertEqual(payload["socket_dir"], str(self.socks))

    def test_list_reports_unverified_records_separately(self):
        self.write_record(os.getpid(), "cc-main", "s1", str(self.socks / "1.sock"))
        os.environ["FAKE_PS_RC"] = "126"
        _rc, out, _err = self.cli("list", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["claude"], [])
        self.assertEqual(
            [record["name"] for record in payload["claude_unverified"]],
            ["cc-main"],
        )

    def test_list_reports_lsof_blocked_codex_threads_separately(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        os.environ["FAKE_LSOF_RC"] = "126"
        with contextlib.redirect_stderr(io.StringIO()):
            _rc, out, _err = self.cli("list", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["codex"], [])
        self.assertEqual(payload["codex_unverified"][0]["id"], tid)
        self.assertIn(
            "operation not permitted",
            payload["codex_unverified"][0]["liveness_error"],
        )

    def test_list_hides_a_thread_no_process_holds(self):
        self.one_thread()
        self.clear_holders()
        _rc, out, _err = self.cli("list", "--json")
        self.assertEqual(json.loads(out)["codex"], [])

    def test_list_reports_the_safe_alias_for_an_unusable_title(self):
        tid, _rollout = self.one_thread(name="Review PR 12")
        _rc, out, _err = self.cli("list", "--json")
        thread = json.loads(out)["codex"][0]
        self.assertEqual(thread["id"], tid)
        self.assertEqual(thread["name"], "Review PR 12")
        self.assertEqual(thread["peer_name"], "codex-%s" % tid[:8])

    def test_list_human_output_names_the_degraded_mode(self):
        self.make_state_db([], filename="state_1.sqlite", good=False)
        _rc, out, _err = self.cli("list")
        self.assertIn("degraded", out)

    def test_list_json_exposes_live_true_on_a_codex_thread(self):
        self.one_thread(name="codex-uzi")
        _rc, out, _err = self.cli("list", "--json")
        self.assertIs(json.loads(out)["codex"][0]["live"], True)

    def test_list_json_marks_an_unverified_thread_live_null(self):
        self.one_thread(name="codex-uzi")
        os.environ["FAKE_LSOF_RC"] = "126"
        with contextlib.redirect_stderr(io.StringIO()):
            _rc, out, _err = self.cli("list", "--json")
        self.assertIsNone(json.loads(out)["codex_unverified"][0]["live"])


class TestAtomicWriteMode(Base):
    """N2: never widen a file, even for an instant."""

    def test_the_temp_file_is_created_at_its_final_mode(self):
        path = str(self.root / "state.json")
        sp_runtime.write_json_atomic(path, {"a": 1}, mode=0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        sp_runtime.write_json_atomic(path, {"a": 2}, mode=0o644)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(sp_runtime.read_json(path), {"a": 2})


class TestDetachedProcessBounds(Base):
    def test_a_negative_open_max_uses_a_real_fd_ceiling(self):
        original = peers.os.sysconf
        peers.os.sysconf = lambda _name: -1
        try:
            self.assertEqual(sp_runtime.safe_open_max(), 256)
        finally:
            peers.os.sysconf = original


class TestShimOwnershipIsExclusive(Base):
    """P2: a second shim must change nothing that belongs to the first."""

    def test_a_second_shim_leaves_the_first_shims_files_untouched(self):
        tid, _rollout = self.one_thread()
        owner = self.hold_pidfile(tid)
        state_path = sp_storage.thread_state_path(tid)
        sp_runtime.write_json_atomic(
            state_path, {"thread_id": tid, "sentinel": "do-not-touch",
                         "budgets": {"s1": 2}}
        )
        record = self.sessions / ("%d.json" % owner)
        record.write_text(json.dumps(
            {"pid": owner, "entrypoint": "codex", "sessionId": tid,
             "messagingSocketPath": str(self.socks / "owner.sock")}
        ))
        state_before = pathlib.Path(state_path).read_text()
        record_before = record.read_bytes()

        proc = self.spawn("shim", "--thread", tid)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 4, out)
        self.assertIn("already owns thread", out)
        self.assertEqual(
            pathlib.Path(sp_storage.thread_pid_path(tid)).read_text().strip(),
            str(owner),
        )
        self.assertEqual(pathlib.Path(state_path).read_text(), state_before)
        self.assertTrue(record.exists())
        self.assertEqual(record.read_bytes(), record_before)

    def test_the_owner_still_cleans_up_its_own_files(self):
        tid, _rollout = self.one_thread()
        proc = subprocess.Popen(
            [sys.executable, str(PEERS), "shim", "--thread", tid],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=dict(os.environ),
        )
        self._children.append(proc)
        # Wait for SERVING, not merely for the ownership lock: the lock is
        # taken first, and a shim killed before its handlers are in cannot
        # clean up.
        self.assertIsNotNone(wait_for(lambda: sp_lifecycle.shim_ready(tid)))
        proc.terminate()
        proc.wait(timeout=10)
        self.assertFalse(os.path.exists(sp_storage.thread_pid_path(tid)))
        self.assertEqual(self.shim_records(), [])

    def test_shim_ready_waits_for_the_record_not_just_the_lock(self):
        tid, _rollout = self.one_thread()
        self.hold_pidfile(tid)
        # The lock is held but no record exists, which is what a shim looks
        # like between taking ownership and binding its socket.
        self.assertIsNotNone(sp_lifecycle.shim_pid(tid))
        self.assertIsNone(sp_lifecycle.shim_ready(tid))


class TestRegistrationIsLocked(Base):
    """P9: two `up` calls at once must not lose one registration."""

    def test_two_concurrent_registrations_both_survive(self):
        script = self.root / "reg.py"
        script.write_text(
            "import importlib.util, sys\n"
            "sys.path.insert(0, %r)\n"
            "spec = importlib.util.spec_from_file_location('peers', %r)\n"
            "peers = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(peers)\n"
            "from session_peers import storage\n"
            "storage.register_thread({'id': sys.argv[1], 'name': sys.argv[2]})\n"
            % (str(HERE), str(PEERS))
        )
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), "thread-%d" % i, "codex-%d" % i],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=dict(os.environ), text=True,
            )
            for i in range(6)
        ]
        for proc in procs:
            out, _ = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, out)
        registered = sp_storage.read_registered()
        self.assertEqual(
            sorted(registered), ["thread-%d" % i for i in range(6)]
        )

    def test_unregister_is_locked_the_same_way(self):
        sp_storage.write_registered({"a": {"name": "x"}, "b": {"name": "y"}})
        self.assertTrue(sp_storage.unregister_thread("a"))
        self.assertEqual(sorted(sp_storage.read_registered()), ["b"])

    def test_name_refresh_never_waits_behind_shutdown(self):
        tid = str(uuidlib.uuid4())
        sp_storage.write_registered({tid: {"name": "old"}})
        lock = self.hold_reconcile_lock()
        started = time.monotonic()
        try:
            self.assertFalse(sp_storage.refresh_registered_name(tid, "new"))
        finally:
            lock.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(sp_storage.read_registered()[tid]["name"], "old")
        self.assertTrue(sp_storage.refresh_registered_name(tid, "new"))
        self.assertEqual(sp_storage.read_registered()[tid]["name"], "new")


class TestDownIsSerialised(Base):
    """R2: `down` must stop and unregister without a reconcile in between."""

    def test_a_reconcile_cannot_restart_the_thread_down_is_removing(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sp_lifecycle.reconcile(), 1)
        self.assertIsNotNone(sp_lifecycle.shim_ready(tid))

        lock = self.hold_reconcile_lock()
        proc = self.spawn("down", tid)
        # With the fix, `down` blocks here and the shim is still alive; without
        # it, `down` has already stopped the shim and this reconcile restarts
        # it behind `down`'s back.
        stopped_early = wait_for(lambda: sp_lifecycle.shim_pid(tid) is None, timeout=2.0)
        with contextlib.redirect_stdout(io.StringIO()):
            sp_lifecycle._reconcile(False)
        lock.close()

        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIsNone(
            stopped_early,
            "`down` stopped the shim before taking the lock, so a reconcile "
            "could restart it",
        )
        self.assertEqual(sp_storage.read_registered(), {})
        self.assertIsNone(sp_lifecycle.shim_pid(tid), "an unregistered shim is still running")

    def test_down_still_works_with_no_contention(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        with contextlib.redirect_stdout(io.StringIO()):
            sp_lifecycle.reconcile()
        rc, out, _err = self.cli("down", tid)
        self.assertEqual(rc, 0)
        self.assertIn("unregistered", out)
        self.assertEqual(sp_storage.read_registered(), {})
        self.assertIsNone(sp_lifecycle.shim_pid(tid))

    def test_bare_down_takes_the_lock_once_for_every_thread(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        with contextlib.redirect_stdout(io.StringIO()):
            sp_lifecycle.reconcile()
        rc, out, _err = self.cli("down")
        self.assertEqual(rc, 0)
        self.assertIn("registrations kept", out)
        self.assertIn(tid, sp_storage.read_registered())
        self.assertIsNone(sp_lifecycle.shim_pid(tid))
