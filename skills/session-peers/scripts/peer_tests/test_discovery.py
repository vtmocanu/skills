"""Regression coverage for discovery."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid as uuidlib
from .support import (
    Base,
    peers,
    sp_claude,
    sp_codex,
    sp_process,
    sp_runtime,
    sp_storage,
    wait_for,
)


class TestRegistry(Base):
    def test_live_filter_accepts_a_matching_record(self):
        self.write_record(os.getpid(), "cc-main", "s1", str(self.socks / "1.sock"))
        live = sp_claude.live_claude_records()
        self.assertEqual([r["name"] for r in live], ["cc-main"])

    def test_live_filter_rejects_a_procstart_mismatch(self):
        self.write_record(
            os.getpid(), "cc-old", "s1", str(self.socks / "1.sock"),
            procStart="Sun Jan  1 00:00:00 2020",
        )
        self.assertEqual(sp_claude.live_claude_records(), [])

    def test_live_filter_rejects_a_foreign_pid_domain(self):
        self.write_record(
            os.getpid(), "cc-other", "s1", str(self.socks / "1.sock"),
            pidDomain="wsl-something",
        )
        self.assertEqual(sp_claude.live_claude_records(), [])

    def test_live_filter_rejects_a_dead_pid(self):
        dead = self._dead_pid()
        self.write_record(dead, "cc-dead", "s1", str(self.socks / "2.sock"))
        self.assertEqual(sp_claude.live_claude_records(), [])

    def test_a_record_without_procstart_is_unverified(self):
        rec = self.write_record(os.getpid(), "cc-lenient", "s1", str(self.socks / "1.sock"))
        path = self.sessions / ("%d.json" % os.getpid())
        rec.pop("procStart")
        path.write_text(json.dumps(rec))
        self.assertEqual(sp_claude.live_claude_records(), [])
        self.assertEqual(
            [r["name"] for r in sp_claude.unverified_claude_records()], ["cc-lenient"]
        )

    def test_a_blocked_process_probe_is_unverified_not_dead(self):
        self.write_record(os.getpid(), "cc-main", "s1", str(self.socks / "1.sock"))
        os.environ["FAKE_PS_RC"] = "126"
        self.assertEqual(sp_claude.live_claude_records(), [])
        self.assertEqual(
            [r["name"] for r in sp_claude.unverified_claude_records()], ["cc-main"]
        )
        self.assertEqual(
            sp_claude.record_liveness(sp_claude.read_claude_records()[0]), "unverified"
        )

    def test_a_corrupt_record_is_skipped_not_fatal(self):
        (self.sessions / "999999.json").write_text("{not json")
        self.write_record(os.getpid(), "cc-main", "s1", str(self.socks / "1.sock"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            live = sp_claude.live_claude_records()
        self.assertEqual([r["name"] for r in live], ["cc-main"])

    def test_a_missing_registry_directory_yields_no_records(self):
        shutil.rmtree(str(self.sessions))
        self.assertEqual(sp_claude.read_claude_records(), [])

    def test_lookup_by_name_and_by_socket(self):
        sock = str(self.socks / "1.sock")
        self.write_record(os.getpid(), "cc-main", "s1", sock)
        self.assertEqual(len(sp_claude.claude_record_by_name("cc-main")), 1)
        self.assertEqual(sp_claude.claude_record_by_name("nope"), [])
        self.assertEqual(sp_claude.claude_record_by_socket(sock)["sessionId"], "s1")
        self.assertIsNone(sp_claude.claude_record_by_socket(str(self.socks / "x.sock")))

    def test_the_registry_follows_claude_config_dir(self):
        other = self.root / "elsewhere"
        (other / "sessions").mkdir(parents=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(other)
        self.assertEqual(sp_storage.claude_sessions_dir(), str(other / "sessions"))
        self.assertEqual(sp_claude.read_claude_records(), [])

    @staticmethod
    def _dead_pid():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid


class TestCodexDiscovery(Base):
    def test_state_db_is_picked_by_schema_not_by_number(self):
        rollout = self.make_rollout()
        self.make_state_db([], filename="state_1.sqlite", good=False)
        good = self.make_state_db(
            [{"id": "t1", "name": "n", "rollout_path": str(rollout)}],
            filename="state_2.sqlite",
        )
        self.assertEqual(sp_codex.find_state_db(), str(good))

    def test_threads_carry_liveness_registration_and_holder(self):
        tid, rollout = self.one_thread(name="codex-uzi")
        threads, ok = sp_codex.codex_threads()
        self.assertTrue(ok)
        self.assertEqual(len(threads), 1)
        t = threads[0]
        self.assertEqual(t["name"], "codex-uzi")
        self.assertTrue(t["live"])
        self.assertEqual(t["holder_pid"], os.getpid())
        self.assertFalse(t["registered"])
        sp_storage.register_thread(t)
        self.assertTrue(sp_codex.codex_threads()[0][0]["registered"])

    def test_a_rollout_no_process_holds_is_not_live(self):
        self.one_thread()
        self.clear_holders()
        threads, _ok = sp_codex.codex_threads()
        self.assertFalse(threads[0]["live"])
        self.assertIsNone(threads[0]["holder_pid"])

    def test_a_non_codex_holder_does_not_count_as_live(self):
        _tid, rollout = self.one_thread()
        self.clear_holders()
        self.set_holder(rollout, cmd="tail")
        self.assertFalse(sp_codex.codex_threads()[0][0]["live"])

    def test_a_thread_live_only_by_its_writer_lock_is_live(self):
        # A just-created Codex thread: the row and the writer lock exist, but
        # the rollout `.jsonl` has not been created yet (it can appear during
        # the first turn). Liveness must come from the held
        # lock, not the absent rollout file.
        tid = "fresh-thread"
        rollout = self.codex_dir / "not-written-yet.jsonl"
        self.make_state_db([{"id": tid, "name": "hi", "rollout_path": str(rollout)}])
        self.assertFalse(rollout.exists())
        self.set_holder(sp_codex.writer_lock_path(tid))
        t = sp_codex.codex_threads()[0][0]
        self.assertTrue(t["live"])
        self.assertEqual(t["holder_pid"], os.getpid())

    def test_missing_stale_paths_do_not_poison_a_live_writer_lock(self):
        live_id = "11111111-1111-4111-8111-111111111111"
        stale_id = "22222222-2222-4222-8222-222222222222"
        live_rollout = self.codex_dir / "live-not-written-yet.jsonl"
        stale_rollout = self.codex_dir / "stale-missing.jsonl"
        self.make_state_db(
            [
                {
                    "id": live_id,
                    "name": "codex-test",
                    "rollout_path": str(live_rollout),
                },
                {
                    "id": stale_id,
                    "name": "old-thread",
                    "rollout_path": str(stale_rollout),
                },
            ]
        )
        self.set_holder(sp_codex.writer_lock_path(live_id))
        pathlib.Path(sp_codex.writer_lock_path(stale_id)).touch()

        threads, ok = sp_codex.codex_threads()
        by_id = {thread["id"]: thread for thread in threads}

        self.assertTrue(ok)
        self.assertTrue(by_id[live_id]["live"])
        self.assertEqual(by_id[live_id]["holder_pid"], os.getpid())
        self.assertIsNone(by_id[live_id]["liveness_error"])
        self.assertFalse(by_id[stale_id]["live"])
        self.assertIsNone(by_id[stale_id]["liveness_error"])

    def test_a_non_codex_lock_holder_does_not_count_as_live(self):
        tid = "fresh-thread"
        rollout = self.codex_dir / "not-written-yet.jsonl"
        self.make_state_db([{"id": tid, "name": "hi", "rollout_path": str(rollout)}])
        self.set_holder(sp_codex.writer_lock_path(tid), cmd="tail")
        self.assertFalse(sp_codex.codex_threads()[0][0]["live"])

    def test_liveness_roots_the_writer_lock_at_codex_home_not_sqlite_home(self):
        # The state DB can live under a separate sqlite_home, but Codex keeps
        # the writer lock under CODEX_HOME. Rooting the lock probe at sqlite_home
        # would miss it, and the fresh-thread bug would return whenever the two
        # homes differ.
        alt = self.root / "sqlite-home"
        alt.mkdir()
        os.environ["CODEX_SQLITE_HOME"] = str(alt)
        tid = "split-thread"
        rollout = self.codex_dir / "never-written.jsonl"
        conn = sqlite3.connect(str(alt / "state_2.sqlite"))
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, "
            "rollout_path TEXT, cwd TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            (tid, "hi", str(rollout), "/tmp", "2026-09-07T12:00:00Z"),
        )
        conn.commit()
        conn.close()
        # writer_lock_path roots at CODEX_HOME, so the holder is set there.
        self.assertTrue(sp_codex.writer_lock_path(tid).startswith(str(self.codex_dir)))
        self.set_holder(sp_codex.writer_lock_path(tid))
        t = sp_codex.codex_threads()[0][0]
        self.assertTrue(t["live"])
        self.assertEqual(t["holder_pid"], os.getpid())

    def test_liveness_matches_a_holder_through_a_symlinked_codex_home(self):
        # Regression: with a symlinked CODEX_HOME (e.g. mackup's ~/.codex -> a
        # repo dir), real lsof reports the holder at the resolved path while
        # peers probed the unresolved one, so EVERY thread read as dead and no
        # shim could start. canon_path() resolves both sides. The fake lsof
        # emits the realpath in `n`, exactly as the real tool does.
        real = self.root / "cx-real"
        (real / "sessions").mkdir(parents=True)
        (real / "thread-writer-locks").mkdir(parents=True)
        link = self.root / "cx-link"
        os.symlink(str(real), str(link))
        os.environ["CODEX_HOME"] = str(link)
        os.environ.pop("CODEX_SQLITE_HOME", None)
        tid = "01a07f92-882c-7953-9dfd-51e10f33184d"
        rollout = link / "sessions" / ("rollout-%s.jsonl" % tid)
        rollout.write_text("")
        conn = sqlite3.connect(str(link / "state_2.sqlite"))
        conn.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, "
            "rollout_path TEXT, cwd TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            (tid, "codex1", str(rollout), "/tmp", "2026-09-07T12:00:00Z"),
        )
        conn.commit()
        conn.close()
        # The holder lives at the resolved path, exactly as lsof reports it.
        self.set_holder(os.path.realpath(str(rollout)))
        threads, ok = sp_codex.codex_threads()
        self.assertTrue(ok)
        t = next(x for x in threads if x["id"] == tid)
        self.assertTrue(
            t["live"],
            "a holder lsof reports at the realpath must count through a "
            "symlinked CODEX_HOME",
        )
        self.assertEqual(t["holder_pid"], os.getpid())

    def test_thread_is_held_via_the_writer_lock_alone(self):
        lock = sp_codex.writer_lock_path("t")
        self.set_holder(lock, pid=4321)
        self.assertEqual(
            sp_codex.thread_is_held("/no/rollout.jsonl", lock_path=lock), (True, 4321)
        )

    def test_thread_is_held_matches_holder_pid_on_the_lock(self):
        lock = sp_codex.writer_lock_path("t")
        self.set_holder(lock, pid=4321)
        self.assertEqual(
            sp_codex.thread_is_held("/no/rollout.jsonl", holder_pid=4321, lock_path=lock),
            (True, 4321),
        )
        held, _pid = sp_codex.thread_is_held(
            "/no/rollout.jsonl", holder_pid=9999, lock_path=lock
        )
        self.assertFalse(held)

    def test_the_session_index_supplies_a_missing_name(self):
        rollout = self.make_rollout()
        self.make_state_db([{"id": "t-index", "name": None, "rollout_path": str(rollout)}])
        (self.codex_dir / "session_index.jsonl").write_text(
            json.dumps({"id": "t-index", "thread_name": "from-index",
                        "updated_at": "2026-09-07"}) + "\n"
        )
        threads, _ok = sp_codex.codex_threads(check_live=False)
        self.assertEqual(threads[0]["name"], "from-index")

    def test_the_session_index_overrides_a_stale_database_name(self):
        rollout = self.make_rollout("index-wins.jsonl")
        tid = "t-index-wins"
        self.make_state_db(
            [{"id": tid, "name": "old-name", "rollout_path": str(rollout)}]
        )
        (self.codex_dir / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": tid,
                    "thread_name": "new-name",
                    "updated_at": "2026-09-09T12:00:00Z",
                }
            )
            + "\n"
        )
        self.assertEqual(sp_codex.codex_threads(check_live=False)[0][0]["name"], "new-name")

    def test_an_unknown_schema_degrades_with_a_warning_not_a_traceback(self):
        self.make_state_db([], filename="state_1.sqlite", good=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            threads, ok = sp_codex.codex_threads()
        self.assertEqual(threads, [])
        self.assertFalse(ok)
        self.assertIn("recognised", err.getvalue())

    def test_a_missing_sqlite_home_is_not_fatal(self):
        os.environ["CODEX_SQLITE_HOME"] = str(self.root / "gone")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sp_codex.codex_threads(), ([], False))

    def test_configured_sqlite_home_beats_the_environment(self):
        # P6: Codex's own resolver prefers the configured value, so the bridge
        # must too, or it reads a different database than Codex writes.
        alt = self.root / "dbs"
        alt.mkdir()
        (self.codex_dir / "config.toml").write_text(
            'model = "gpt-5"\nsqlite_home = "%s"\n' % alt
        )
        self.assertEqual(sp_storage.codex_sqlite_home(), str(alt))
        os.environ["CODEX_SQLITE_HOME"] = str(self.root / "env-loses")
        self.assertEqual(sp_storage.codex_sqlite_home(), str(alt))

    def test_the_environment_is_used_when_the_config_says_nothing(self):
        (self.codex_dir / "config.toml").write_text('model = "gpt-5"\n')
        os.environ["CODEX_SQLITE_HOME"] = str(self.root / "from-env")
        self.assertEqual(sp_storage.codex_sqlite_home(), str(self.root / "from-env"))
        os.environ.pop("CODEX_SQLITE_HOME")
        self.assertEqual(sp_storage.codex_sqlite_home(), str(self.codex_dir))

    def test_a_sqlite_home_inside_another_table_is_ignored(self):
        # P6: that key belongs to that table, not to the bridge.
        (self.codex_dir / "config.toml").write_text(
            '[some_tool]\nsqlite_home = "%s"\n' % (self.root / "wrong")
        )
        self.assertEqual(sp_storage.codex_sqlite_home(), str(self.codex_dir))

    def test_lsof_missing_from_path_is_reported_not_fatal(self):
        rollout = self.make_rollout()
        os.environ["PATH"] = str(self.root / "empty")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sp_process.lsof_holders([str(rollout)]), {})
        self.assertIn("lsof", err.getvalue())

    def test_a_blocked_lsof_probe_is_unverified_not_dead(self):
        tid, rollout = self.one_thread(name="codex-uzi")
        os.environ["FAKE_LSOF_RC"] = "1"
        os.environ["FAKE_LSOF_STDERR"] = "operation not permitted"
        with contextlib.redirect_stderr(io.StringIO()):
            thread = sp_codex.codex_threads()[0][0]
            held = sp_codex.thread_is_held(str(rollout))
            with self.assertRaises(sp_codex.ResolveError) as ctx:
                sp_codex.resolve_thread("codex-uzi")
        self.assertEqual(thread["id"], tid)
        self.assertIsNone(thread["live"])
        self.assertIn("operation not permitted", thread["liveness_error"])
        self.assertEqual(held, (None, None))
        self.assertIn("liveness is unavailable", str(ctx.exception))

    def test_a_blocked_path_probe_is_unverified_not_dead(self):
        rollout = self.make_rollout()
        original = peers.os.stat

        def denied(path, *args, **kwargs):
            if str(path) == str(rollout):
                raise PermissionError("operation not permitted")
            return original(path, *args, **kwargs)

        peers.os.stat = denied
        try:
            holders, verified, error = sp_process.lsof_holders_checked([str(rollout)])
        finally:
            peers.os.stat = original

        self.assertEqual(holders, {})
        self.assertFalse(verified)
        self.assertIn("operation not permitted", error)


class TestResolveThread(Base):
    def test_a_uuid_resolves_directly(self):
        tid, _r = self.one_thread()
        self.assertEqual(sp_codex.resolve_thread(tid)["id"], tid)

    def test_a_name_resolves_to_its_live_thread(self):
        tid, _r = self.one_thread(name="codex-uzi")
        self.assertEqual(sp_codex.resolve_thread("codex-uzi")["id"], tid)

    def test_a_listed_alias_and_raw_uuid_prefix_resolve(self):
        tid = "1234abcd-1111-2222-3333-444444444444"
        self.one_thread(tid=tid, name="unsafe title with spaces")
        self.assertEqual(sp_codex.resolve_thread("codex-1234abcd")["id"], tid)
        self.assertEqual(sp_codex.resolve_thread("1234abcd1111")["id"], tid)
        rc, _out, _err = self.cli(
            "send", "--to", "codex:codex-1234abcd", "--message", "hi"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.queue_calls()[0][2], tid)

    def test_an_ambiguous_prefix_lists_both_live_candidates(self):
        first = "abcd1234-1111-2222-3333-444444444444"
        second = "abcd1234-aaaa-bbbb-cccc-dddddddddddd"
        r1 = self.make_rollout("a.jsonl")
        r2 = self.make_rollout("b.jsonl")
        self.make_state_db([
            {"id": first, "name": "review", "rollout_path": str(r1)},
            {"id": second, "name": "other", "rollout_path": str(r2)},
        ])
        self.set_holder(r1)
        self.set_holder(r2)
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("codex-abcd1234")
        self.assertIn(first, str(ctx.exception))
        self.assertIn(second, str(ctx.exception))
        self.assertIn("review", str(ctx.exception))
        self.assertIn("other", str(ctx.exception))

    def test_a_dead_prefix_collision_does_not_block_the_live_thread(self):
        first = "abcd1234-1111-2222-3333-444444444444"
        second = "abcd1234-aaaa-bbbb-cccc-dddddddddddd"
        r1 = self.make_rollout("a.jsonl")
        r2 = self.make_rollout("b.jsonl")
        self.make_state_db([
            {"id": first, "name": "past", "rollout_path": str(r1)},
            {"id": second, "name": "live", "rollout_path": str(r2)},
        ])
        self.set_holder(r2)
        self.assertEqual(sp_codex.resolve_thread("codex-abcd1234")["id"], second)

    def test_an_alias_colliding_with_an_exact_title_is_refused(self):
        r1 = self.make_rollout("a.jsonl")
        r2 = self.make_rollout("b.jsonl")
        first = "12345678-0000-0000-0000-000000000001"
        second = "aaaaaaaa-0000-0000-0000-000000000002"
        self.make_state_db([
            {"id": first, "name": "other", "rollout_path": str(r1)},
            {"id": second, "name": "codex-12345678", "rollout_path": str(r2)},
        ])
        self.set_holder(r1)
        self.set_holder(r2)
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("codex-12345678")
        self.assertIn(first, str(ctx.exception))
        self.assertIn(second, str(ctx.exception))

    def test_a_name_held_by_two_live_threads_is_refused(self):
        r1 = self.make_rollout("a.jsonl")
        r2 = self.make_rollout("b.jsonl")
        self.make_state_db(
            [
                {"id": "aaa", "name": "dup", "rollout_path": str(r1)},
                {"id": "bbb", "name": "dup", "rollout_path": str(r2)},
            ]
        )
        self.set_holder(r1)
        self.set_holder(r2)
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("dup")
        self.assertIn("register by UUID", str(ctx.exception))

    def test_a_dead_duplicate_does_not_block_the_live_one(self):
        r1 = self.make_rollout("a.jsonl")
        r2 = self.make_rollout("b.jsonl")
        self.make_state_db(
            [
                {"id": "aaa", "name": "dup", "rollout_path": str(r1)},
                {"id": "bbb", "name": "dup", "rollout_path": str(r2)},
            ]
        )
        self.set_holder(r2)
        self.assertEqual(sp_codex.resolve_thread("dup")["id"], "bbb")

    def test_a_name_resolves_before_its_first_rollout_line(self):
        # Regression: `up hi` on a just-renamed thread must not fail with
        # "no live Codex thread" only because the rollout file does not exist
        # yet. The held writer lock proves the thread is live.
        tid = "fresh-hi"
        rollout = self.codex_dir / "hi.jsonl"
        self.make_state_db([{"id": tid, "name": "hi", "rollout_path": str(rollout)}])
        self.set_holder(sp_codex.writer_lock_path(tid))
        self.assertFalse(rollout.exists())
        self.assertEqual(sp_codex.resolve_thread("hi")["id"], tid)

    def test_a_name_resolves_via_our_registration_when_the_db_name_is_stale(self):
        # `up <uuid>` records name->uuid; a later /rename may not have reached
        # the DB's `name` column yet (measured on codex-cli 0.153.4), so a live
        # thread we already registered under this name must still resolve even
        # though its DB row carries a different (stale) name.
        tid = "01a07f92-882c-7953-9dfd-51e10f33184d"
        rollout = self.make_rollout("r.jsonl")
        self.make_state_db(
            [{"id": tid, "name": "old-title", "rollout_path": str(rollout)}]
        )
        self.set_holder(rollout)
        sp_storage.write_registered(
            {tid: {"name": "codex1", "registered_at": "2026-09-08T00:00:00Z"}}
        )
        self.assertEqual(sp_codex.resolve_thread("codex1")["id"], tid)

    def test_a_name_whose_thread_has_exited_is_refused_not_guessed(self):
        # Codex's title suggester reuses names, so a dead match is not intent.
        self.one_thread(name="codex-uzi")
        self.clear_holders()
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("codex-uzi")
        self.assertIn("no live Codex thread", str(ctx.exception))
        self.assertEqual(sp_codex.resolve_thread("codex-uzi", require_live=False)["name"],
                         "codex-uzi")

    def test_an_unknown_name_is_refused_with_advice(self):
        self.one_thread(name="codex-uzi")
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("missing")
        self.assertIn("/rename", str(ctx.exception))
        self.assertIn("peers.py list", str(ctx.exception))

    def test_degraded_mode_resolves_a_uuid_but_refuses_a_name(self):
        self.make_state_db([], filename="state_1.sqlite", good=False)
        tid = str(uuidlib.uuid4())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            thread = sp_codex.resolve_thread(tid)
            self.assertTrue(thread["degraded"])
            with self.assertRaises(sp_codex.ResolveError):
                sp_codex.resolve_thread("some-name")


class TestSessionIndexMerge(Base):
    """S11: both index files are read, not just the first openable one."""

    def test_names_from_both_candidates_are_merged(self):
        alt = self.root / "dbs"
        alt.mkdir()
        (self.codex_dir / "config.toml").write_text('sqlite_home = "%s"\n' % alt)
        (self.codex_dir / "session_index.jsonl").write_text(
            json.dumps({"id": "a", "thread_name": "from-home",
                        "updated_at": "2026-09-01T00:00:00Z"}) + "\n"
        )
        (alt / "session_index.jsonl").write_text(
            json.dumps({"id": "b", "thread_name": "from-sqlite",
                        "updated_at": "2026-09-01T00:00:00Z"}) + "\n"
        )
        index = sp_codex.read_session_index()
        self.assertEqual(index, {"a": "from-home", "b": "from-sqlite"})

    def test_the_newest_updated_at_wins_for_a_shared_id(self):
        alt = self.root / "dbs"
        alt.mkdir()
        (self.codex_dir / "config.toml").write_text('sqlite_home = "%s"\n' % alt)
        (self.codex_dir / "session_index.jsonl").write_text(
            json.dumps({"id": "a", "thread_name": "older",
                        "updated_at": "2026-09-01T00:00:00Z"}) + "\n"
        )
        (alt / "session_index.jsonl").write_text(
            json.dumps({"id": "a", "thread_name": "newer",
                        "updated_at": "2026-09-06T00:00:00Z"}) + "\n"
        )
        self.assertEqual(sp_codex.read_session_index()["a"], "newer")
        (alt / "session_index.jsonl").write_text(
            json.dumps({"id": "a", "thread_name": "stale",
                        "updated_at": "2026-08-01T00:00:00Z"}) + "\n"
        )
        self.assertEqual(sp_codex.read_session_index()["a"], "older")


class TestStableCwd(Base):
    """A shim started from a since-deleted directory (a removed worktree)."""

    def setUp(self):
        super().setUp()
        self.cwd_log = self.root / "codex-cwd.log"
        os.environ["FAKE_CODEX_CWD_LOG"] = str(self.cwd_log)
        self.saved_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.saved_cwd)
        super().tearDown()

    def last_cwd(self):
        return self.cwd_log.read_text().splitlines()[-1]

    def test_codex_queue_runs_in_the_threads_own_directory(self):
        thread_dir = pathlib.Path(tempfile.mkdtemp(dir=str(self.root)))
        sp_codex.codex_queue(str(uuidlib.uuid4()), "hi", cwd=str(thread_dir))
        self.assertEqual(os.path.realpath(self.last_cwd()), os.path.realpath(str(thread_dir)))

    def test_codex_queue_falls_back_to_home_when_the_thread_dir_is_gone(self):
        gone = pathlib.Path(tempfile.mkdtemp(dir=str(self.root)))
        gone.rmdir()
        sp_codex.codex_queue(str(uuidlib.uuid4()), "hi", cwd=str(gone))
        self.assertEqual(
            os.path.realpath(self.last_cwd()),
            os.path.realpath(os.path.expanduser("~")),
        )

    def test_codex_queue_survives_a_deleted_process_cwd(self):
        # The reported failure: `up` ran from a worktree that was later
        # removed, so the shim's inherited cwd no longer exists.
        doomed = pathlib.Path(tempfile.mkdtemp(dir=str(self.root)))
        os.chdir(str(doomed))
        doomed.rmdir()
        sp_codex.codex_queue(str(uuidlib.uuid4()), "hi")  # raises QueueError if broken
        self.assertEqual(
            os.path.realpath(self.last_cwd()),
            os.path.realpath(os.path.expanduser("~")),
        )

    def test_a_detached_process_does_not_inherit_the_callers_directory(self):
        out = self.root / "detached-cwd.txt"
        caller = pathlib.Path(tempfile.mkdtemp(dir=str(self.root)))
        os.chdir(str(caller))
        pid = sp_runtime.spawn_detached(
            [
                sys.executable,
                "-c",
                "import os; open(%r, 'w').write(os.getcwd())" % str(out),
            ],
            str(self.root / "detached.log"),
        )
        self.assertTrue(wait_for(lambda: out.exists() and out.read_text()), pid)
        self.assertEqual(
            os.path.realpath(out.read_text()),
            os.path.realpath(os.path.expanduser("~")),
        )
