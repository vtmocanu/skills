"""Regression coverage for buddy."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import stat
import time
from .support import (
    BuddyBase,
    new_uuid,
    peers,
    sp_constants,
    sp_lifecycle,
    sp_maintenance,
    sp_runtime,
    sp_storage,
    wait_for,
)


class TestBuddyRecords(BuddyBase):
    def test_set_show_clear_round_trip_with_private_modes(self):
        tid, _rollout = self.one_thread(name="fail-codex")
        owner = "cc:%s" % new_uuid()
        rc, out, err = self.buddy(owner, "set", "fail-codex")
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy = fail-codex (codex, %s)" % tid[:8], out)
        self.assertIn("registered=no", out)
        self.assertIn("uses: %s" % ", ".join(sp_constants.BUDDY_USES), out)
        for word in ("answered", "responsive"):
            self.assertNotIn(word, out)
        # `set` ensures the route like `ping`, through the transient attach.
        self.assertEqual(self.attach_calls, [(tid, False)])
        path = self.record_path(owner)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        record = json.loads(path.read_text())
        self.assertEqual(record["buddy"], {"kind": "codex", "uuid": tid, "name": "fail-codex"})
        self.assertEqual(record["owner"]["uuid"], owner[3:])

        rc, out, _err = self.cli("buddy", "--as", owner, "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["buddy"]["uuid"], tid)
        status = payload["status"]
        self.assertIs(status["live"], True)
        self.assertIs(status["registered"], False)
        self.assertIsNone(status["shim_pid"])
        self.assertIsNone(status["status"])
        self.assertIs(status["paused"], False)
        self.assertTrue(status["route"].startswith("unavailable: no shim"))
        # `show` never attaches.
        self.assertEqual(len(self.attach_calls), 1)

        rc, out, _err = self.buddy(owner, "clear")
        self.assertEqual((rc, out.strip()), (0, "buddy cleared"))
        self.assertFalse(path.exists())
        rc, _out, err = self.buddy(owner, "show")
        self.assertEqual(rc, 1)
        self.assertIn("no buddy set; run `peers.py buddy set <name|uuid>`", err)
        rc, out, _err = self.buddy(owner, "clear")
        self.assertEqual((rc, out.strip()), (0, "no buddy was set"))

    def test_a_leading_at_is_stripped_and_a_codex_owner_binds_a_claude_buddy(self):
        sid = new_uuid()
        self.add_listener(name="cc-other", session_id=sid)
        self.make_state_db([])
        owner = "codex:%s" % new_uuid()
        rc, out, err = self.buddy(owner, "set", "@cc-other", "--uses", "review,ping")
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy = cc-other (cc, %s) live" % sid[:8], out)
        self.assertIn("route available; uses: review, ping", out)
        record = json.loads(self.record_path(owner).read_text())
        self.assertEqual(record["buddy"]["kind"], "cc")
        self.assertEqual(record["buddy"]["uuid"], sid)
        self.assertEqual(self.attach_calls, [])

    def test_an_ambiguous_bare_name_is_refused_naming_both_typed_forms(self):
        sid = new_uuid()
        self.add_listener(name="twin", session_id=sid)
        tid, _rollout = self.one_thread(name="twin")
        owner = "cc:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "twin")
        self.assertEqual(rc, 1)
        self.assertIn("cc:%s" % sid, err)
        self.assertIn("codex:%s" % tid, err)
        self.assertFalse(self.record_path(owner).exists())
        # A typed target settles it.
        rc, _out, err = self.buddy(owner, "set", "codex:twin")
        self.assertEqual(rc, 0, err)

    def test_a_uuid_naming_a_codex_thread_and_its_shim_prints_retry_commands(self):
        # An attached Codex thread's UUID also names its shim's registry record.
        tid, _rollout = self.one_thread(name="fail-codex")
        self.add_listener(name="fail-codex", session_id=tid)
        owner = "cc:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", tid, "--uses", "review,brainstorm")
        self.assertEqual(rc, 1)
        self.assertIn("matches both cc:%s and codex:%s" % (tid, tid), err)
        for kind in ("cc", "codex"):
            self.assertIn(
                "  peers.py buddy set %s:%s --uses review,brainstorm --as %s"
                % (kind, tid, owner),
                err,
            )
        self.assertFalse(self.record_path(owner).exists())
        rc, _out, err = self.buddy(
            owner, "set", "codex:%s" % tid, "--uses", "review,brainstorm"
        )
        self.assertEqual(rc, 0, err)

    def test_buddy_set_help_shows_the_prefix_forms(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            peers.main(["buddy", "set", "--help"])
        self.assertIn("peers.py buddy set codex:<uuid>", out.getvalue())
        self.assertIn("peers.py buddy set cc:<uuid>", out.getvalue())

    def test_a_bare_name_is_refused_when_codex_discovery_is_unavailable(self):
        # No state DB: a Codex thread by that name cannot be ruled out.
        sid = new_uuid()
        self.add_listener(name="cc-other", session_id=sid)
        owner = "codex:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "cc-other")
        self.assertEqual(rc, 1)
        self.assertIn("pick the kind", err)
        self.assertIn("peers.py buddy set cc:cc-other --as %s" % owner, err)
        self.assertIn("peers.py buddy set codex:cc-other --as %s" % owner, err)
        rc, _out, err = self.buddy(owner, "set", "cc:cc-other")
        self.assertEqual(rc, 0, err)

    def namesakes(self, name="helper", live=True):
        """One live (optionally) and one dead Codex thread sharing a name."""
        live_tid, dead_tid = new_uuid(), new_uuid()
        live_rollout = self.make_rollout("%s.jsonl" % live_tid)
        dead_rollout = self.make_rollout("%s.jsonl" % dead_tid)
        self.make_state_db([
            {"id": live_tid, "name": name, "rollout_path": str(live_rollout)},
            {"id": dead_tid, "name": name, "rollout_path": str(dead_rollout)},
        ])
        if live:
            self.set_holder(live_rollout)
        return live_tid, dead_tid

    def test_a_live_codex_thread_wins_over_a_dead_namesake(self):
        live_tid, _dead = self.namesakes()
        for target in ("helper", "codex:helper"):
            owner = "cc:%s" % new_uuid()
            rc, _out, err = self.buddy(owner, "set", target)
            self.assertEqual(rc, 0, "%s: %s" % (target, err))
            record = json.loads(self.record_path(owner).read_text())
            self.assertEqual(record["buddy"]["uuid"], live_tid, target)

    def test_a_dead_codex_namesake_does_not_compete_with_a_live_claude_session(self):
        sid = new_uuid()
        self.add_listener(name="helper", session_id=sid)
        dead_tid = new_uuid()
        rollout = self.make_rollout("%s.jsonl" % dead_tid)
        self.make_state_db([{"id": dead_tid, "name": "helper", "rollout_path": str(rollout)}])
        owner = "codex:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "helper")
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path(owner).read_text())
        self.assertEqual(record["buddy"], {"kind": "cc", "uuid": sid, "name": "helper"})

    def test_a_bare_name_carried_only_by_a_dead_thread_binds_it_like_codex_does(self):
        dead_tid = new_uuid()
        rollout = self.make_rollout("%s.jsonl" % dead_tid)
        self.make_state_db([{"id": dead_tid, "name": "helper", "rollout_path": str(rollout)}])
        for target in ("helper", "codex:helper"):
            owner = "cc:%s" % new_uuid()
            rc, _out, err = self.buddy(owner, "set", target)
            self.assertEqual(rc, 0, "%s: %s" % (target, err))
            record = json.loads(self.record_path(owner).read_text())
            self.assertEqual(record["buddy"]["uuid"], dead_tid, target)
        self.assertEqual(self.attach_calls, [])  # a dead thread is never attached

    def test_an_unknown_target_is_refused(self):
        self.one_thread(name="someone")
        owner = "cc:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "nobody")
        self.assertEqual(rc, 1)
        self.assertIn("no Claude session or Codex thread named 'nobody'", err)

    def test_a_session_cannot_be_its_own_buddy(self):
        tid, _rollout = self.one_thread(name="self-codex")
        rc, _out, err = self.buddy("codex:%s" % tid, "set", tid)
        self.assertEqual(rc, 2)
        self.assertIn("its own buddy", err)
        self.assertFalse(self.record_path("codex:%s" % tid).exists())

    def test_a_bare_name_never_matches_the_caller_itself(self):
        # The caller shares its name with the peer it means: not ambiguous.
        tid, _rollout = self.one_thread(name="pair")
        sid = new_uuid()
        self.add_listener(name="pair", session_id=sid)
        owner = "cc:%s" % sid
        rc, _out, err = self.buddy(owner, "set", "pair")
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path(owner).read_text())
        self.assertEqual(record["buddy"], {"kind": "codex", "uuid": tid, "name": "pair"})

    def test_set_without_a_target_binds_the_claude_callers_namesake(self):
        tid, _rollout = self.one_thread(name="pair")
        sid = new_uuid()
        self.add_listener(name="pair", session_id=sid)
        owner = "cc:%s" % sid
        rc, out, err = self.buddy(owner, "set")
        self.assertEqual(rc, 0, err)
        self.assertIn("buddy = pair (codex, %s)" % tid[:8], out)

    def test_set_without_a_target_binds_the_codex_callers_namesake(self):
        tid, _rollout = self.one_thread(name="pair")
        sid = new_uuid()
        self.add_listener(name="pair", session_id=sid)
        owner = "codex:%s" % tid
        rc, _out, err = self.buddy(owner, "set")
        self.assertEqual(rc, 0, err)
        record = json.loads(self.record_path(owner).read_text())
        self.assertEqual(record["buddy"], {"kind": "cc", "uuid": sid, "name": "pair"})

    def test_set_without_a_target_and_no_namesake_asks_for_a_name(self):
        self.one_thread(name="other")
        sid = new_uuid()
        self.add_listener(name="pair", session_id=sid)
        owner = "cc:%s" % sid
        rc, _out, err = self.buddy(owner, "set")
        self.assertEqual(rc, 1)
        self.assertIn("no other session is named 'pair'; name the buddy", err)
        self.assertFalse(self.record_path(owner).exists())

    def test_set_without_a_target_needs_a_named_caller(self):
        self.one_thread(name="pair")
        owner = "cc:%s" % new_uuid()  # no registry record: no name to look up
        rc, _out, err = self.buddy(owner, "set")
        self.assertEqual(rc, 1)
        self.assertIn("has no name to look up", err)
        self.assertFalse(self.record_path(owner).exists())

    def test_an_unknown_uses_word_is_rejected_listing_the_supported_ones(self):
        self.one_thread(name="fail-codex")
        owner = "cc:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "fail-codex", "--uses", "review,deploy")
        self.assertEqual(rc, 2)
        self.assertIn("deploy", err)
        for word in sp_constants.BUDDY_USES:
            self.assertIn(word, err)
        self.assertFalse(self.record_path(owner).exists())

    def test_two_owners_keep_independent_buddies(self):
        tid, other = self.two_threads()
        first, second = "cc:%s" % new_uuid(), "cc:%s" % new_uuid()
        self.assertEqual(self.buddy(first, "set", "fail-codex")[0], 0)
        self.assertEqual(self.buddy(second, "set", "other-codex")[0], 0)
        _rc, out, _err = self.cli("buddy", "--as", first, "--json")
        self.assertEqual(json.loads(out)["buddy"]["uuid"], tid)
        _rc, out, _err = self.cli("buddy", "--as", second, "--json")
        self.assertEqual(json.loads(out)["buddy"]["uuid"], other)
        self.assertEqual(self.buddy(first, "clear")[0], 0)
        _rc, out, _err = self.cli("buddy", "--as", second, "--json")
        self.assertEqual(json.loads(out)["buddy"]["uuid"], other)

    def test_no_caller_identity_is_a_usage_error(self):
        rc, _out, err = self.cli("buddy")
        self.assertEqual(rc, 2)
        self.assertIn("cannot tell which session is asking; pass --as", err)

    def test_the_caller_defaults_to_the_claude_session_then_the_codex_thread(self):
        tid, _rollout = self.one_thread(name="fail-codex")
        sid = new_uuid()
        os.environ["CODEX_THREAD_ID"] = new_uuid()
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        self.assertEqual(self.cli("buddy", "set", "fail-codex")[0], 0)
        self.assertTrue(self.record_path("cc:%s" % sid).exists())
        del os.environ["CLAUDE_CODE_SESSION_ID"]
        rc, _out, err = self.cli("buddy", "show")
        self.assertEqual(rc, 1)  # the Codex thread has no buddy of its own
        self.assertIn("no buddy set", err)


class TestBuddyRouting(BuddyBase):
    def test_a_renamed_buddy_is_still_reached_by_uuid_not_by_its_old_name(self):
        sid = new_uuid()
        bound, _rec = self.add_listener(name="cc-other", session_id=sid)
        self.make_state_db([])
        owner_sid = new_uuid()
        self.assertEqual(self.buddy("cc:%s" % owner_sid, "set", "cc-other")[0], 0)
        # The buddy renames itself; a newcomer takes the old name.
        self.write_record(os.getpid(), "renamed", sid, bound.path)
        newcomer, _rec = self.add_listener(name="cc-other", session_id=new_uuid(), pid=1)
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner_sid
        rc, _out, err = self.cli("send", "--to", "buddy", "--message", "hello")
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_for(lambda: bound.of_type("user")))
        time.sleep(0.2)
        self.assertEqual(newcomer.of_type("user"), [])
        rc, out, _err = self.cli("buddy", "show")
        self.assertIn("buddy = renamed (cc, %s)" % sid[:8], out)

    def test_a_claude_owner_sends_to_its_codex_buddy_by_uuid(self):
        tid, _other = self.two_threads()
        owner_sid = new_uuid()
        self.assertEqual(self.buddy("cc:%s" % owner_sid, "set", "fail-codex")[0], 0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner_sid
        rc, _out, err = self.cli("send", "--to", "@buddy", "--message", "hi")
        self.assertEqual(rc, 0, err)
        calls = self.queue_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn(tid, calls[0])

    def test_a_codex_owner_sends_and_dispatches_to_its_claude_buddy(self):
        sid = new_uuid()
        listener, _rec = self.add_listener(name="cc-other", session_id=sid)
        self.make_state_db([])
        owner_tid = new_uuid()
        self.assertEqual(self.buddy("codex:%s" % owner_tid, "set", "cc-other")[0], 0)
        os.environ["CODEX_THREAD_ID"] = owner_tid
        rc, _out, err = self.cli("send", "--to", "buddy", "--message", "note")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli(
            "dispatch", "--to", "buddy", "--message", "review this", "--json"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["target_session_id"], sid)
        self.assertTrue(wait_for(lambda: len(listener.of_type("user")) == 2))

    def test_ask_dispatch_and_wait_refuse_a_codex_buddy_and_send_nothing(self):
        self.one_thread(name="fail-codex")
        owner_sid = new_uuid()
        self.assertEqual(self.buddy("cc:%s" % owner_sid, "set", "fail-codex")[0], 0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner_sid
        for argv in (
            ("ask", "--to", "buddy", "--message", "q", "--from-thread", new_uuid()),
            ("dispatch", "--to", "buddy", "--message", "q", "--from-thread", new_uuid()),
            ("wait", "--for", "buddy", "--timeout", "1"),
        ):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(
                "ask/dispatch/wait need a Claude buddy; use send (asynchronous)", err
            )
        self.assertEqual(self.queue_calls(), [])
        self.assertEqual(os.listdir(sp_storage.request_dir()), [])

    def test_budget_commands_refuse_a_claude_buddy(self):
        sid = new_uuid()
        self.add_listener(name="cc-other", session_id=sid)
        self.make_state_db([])
        owner_tid = new_uuid()
        self.assertEqual(self.buddy("codex:%s" % owner_tid, "set", "cc-other")[0], 0)
        os.environ["CODEX_THREAD_ID"] = owner_tid
        rc, _out, err = self.cli("budget", "reset", "buddy")
        self.assertEqual(rc, 2)
        self.assertIn("Codex thread's shim", err)
        rc, _out, err = self.cli(
            "budget", "allow", "buddy", "--replies", "5", "--for-session", sid
        )
        self.assertEqual(rc, 2)
        leftovers = [
            name for name in os.listdir(sp_storage.state_dir())
            if name.endswith((".budget-reset", ".budget-allow"))
        ]
        self.assertEqual(leftovers, [])

    def test_budget_commands_accept_a_codex_buddy(self):
        tid, _rollout = self.one_thread(name="fail-codex")
        owner_sid = new_uuid()
        self.assertEqual(self.buddy("cc:%s" % owner_sid, "set", "fail-codex")[0], 0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner_sid
        rc, _out, err = self.cli("budget", "allow", "buddy", "--replies", "6")
        self.assertEqual(rc, 0, err)
        marker = sp_runtime.read_json(sp_storage.budget_allow_path(tid))
        self.assertEqual((marker["sid"], marker["total"]), (owner_sid, 6))
        rc, _out, err = self.cli("budget", "reset", "buddy")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(sp_storage.budget_reset_path(tid)))
        # An explicit reset drops the grant not yet consumed.
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(tid)))

    def test_to_buddy_without_a_record_is_a_clear_error(self):
        self.one_thread(name="fail-codex")
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, _out, err = self.cli("send", "--to", "buddy", "--message", "hi")
        self.assertEqual(rc, 1)
        self.assertIn("no buddy set", err)
        self.assertIn("peers.py buddy set", err)
        self.assertEqual(self.queue_calls(), [])

    def test_repeated_ping_is_budget_neutral(self):
        tid, _rollout = self.one_thread(name="fail-codex")
        owner = "cc:%s" % new_uuid()
        self.assertEqual(self.buddy(owner, "set", "fail-codex")[0], 0)
        held = {"s1": {"text": "held", "mid": "m", "turn_id": "t", "at": time.time()}}
        sp_runtime.write_json_atomic(
            sp_storage.thread_state_path(tid),
            {"thread_id": tid, "budgets": {"s1": 3}, "held": held},
        )
        before = pathlib.Path(sp_storage.thread_state_path(tid)).read_bytes()
        saved_up = sp_lifecycle.cmd_up
        sp_lifecycle.cmd_up = lambda _args: self.fail("ping must not run `up`")
        try:
            for _ in range(3):
                rc, _out, err = self.buddy(owner, "ping")
                self.assertEqual(rc, 0, err)
        finally:
            sp_lifecycle.cmd_up = saved_up
        self.assertEqual(self.attach_calls, [(tid, False)] * 4)  # set + 3 pings
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(tid)))
        self.assertFalse(os.path.exists(sp_storage.budget_allow_path(tid)))
        self.assertEqual(pathlib.Path(sp_storage.thread_state_path(tid)).read_bytes(), before)
        self.assertEqual(sp_storage.read_registered(), {})

    def test_ping_reports_a_running_shim_without_attaching(self):
        tid, _rollout = self.one_thread(name="fail-codex")
        owner = "cc:%s" % new_uuid()
        pid = self.hold_pidfile(tid)
        rc, out, err = self.buddy(owner, "set", "fail-codex")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.attach_calls, [])
        self.assertIn("shim=%d" % pid, out)
        self.assertIn("route available", out)


class TestBuddyGarbageCollection(BuddyBase):
    def write_buddy(self, owner, age_days=8):
        kind, _sep, ident = owner.partition(":")
        path = self.record_path(owner)
        sp_runtime.write_json_atomic(
            str(path),
            {
                "owner": {"kind": kind, "uuid": ident},
                "buddy": {"kind": "codex", "uuid": new_uuid(), "name": "x"},
                "uses": ["review"],
                "set_at": "2026-09-01T00:00:00Z",
            },
        )
        old = time.time() - age_days * 86400
        os.utime(str(path), (old, old))
        return path

    def test_a_verified_gone_owner_with_an_old_record_is_pruned(self):
        self.make_state_db([])
        path = self.write_buddy("cc:%s" % new_uuid())
        rc, out, _err = self.cli("gc")
        self.assertEqual(rc, 0)
        self.assertIn("pruned buddy record", out)
        self.assertFalse(path.exists())

    def test_a_recent_record_is_kept_even_when_the_owner_is_gone(self):
        path = self.write_buddy("cc:%s" % new_uuid(), age_days=1)
        self.assertEqual(sp_maintenance.gc_buddy_records(days=7, verbose=False), [])
        self.assertTrue(path.exists())

    def test_an_unverified_or_live_claude_owner_keeps_its_record(self):
        sid = new_uuid()
        self.add_listener(name="cc-owner", session_id=sid)
        path = self.write_buddy("cc:%s" % sid)
        self.assertEqual(sp_maintenance.gc_buddy_records(days=7, verbose=False), [])
        os.environ["FAKE_PS_RC"] = "126"
        self.assertEqual(sp_maintenance.gc_buddy_records(days=7, verbose=False), [])
        self.assertTrue(path.exists())

    def test_a_codex_owner_is_pruned_only_when_verified_not_live(self):
        tid, _rollout = self.one_thread(name="owner-codex")
        path = self.write_buddy("codex:%s" % tid)
        self.assertEqual(sp_maintenance.gc_buddy_records(days=7, verbose=False), [])  # live
        os.environ["FAKE_LSOF_RC"] = "126"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sp_maintenance.gc_buddy_records(days=7, verbose=False), [])
        self.assertTrue(path.exists())
        del os.environ["FAKE_LSOF_RC"]
        unknown = self.write_buddy("codex:%s" % new_uuid())  # not in the DB
        self.clear_holders()
        removed = sp_maintenance.gc_buddy_records(days=7, verbose=False)
        self.assertEqual(removed, [path.name])
        self.assertTrue(unknown.exists())

    def test_a_stale_budget_allow_marker_is_pruned_with_its_thread(self):
        tid = new_uuid()
        rollout = self.make_rollout("%s.jsonl" % tid)
        old = time.time() - 8 * 86400
        self.make_state_db([{"id": tid, "name": "old", "rollout_path": str(rollout),
                             "updated_at": old}])
        marker = pathlib.Path(sp_storage.budget_allow_path(tid))
        marker.write_text("{}")
        os.utime(str(marker), (old, old))
        self.assertEqual(sp_maintenance.gc_bridge_state(days=7, verbose=False), [tid])
        self.assertFalse(marker.exists())
