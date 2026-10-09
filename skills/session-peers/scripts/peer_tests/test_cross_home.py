"""Regression coverage for peers whose CODEX_HOME differs from the caller's.

A Claude session launched by an app can carry its own CODEX_HOME while the
Codex thread and its shim run under the default one. Commands that address
that live shim must act on the shim's state, never on the caller's home.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pathlib
import time
from unittest import mock

from .support import (
    Base,
    BuddyBase,
    new_uuid,
    sp_buddy,
    sp_codex,
    sp_constants,
    sp_lifecycle,
    sp_protocol,
    sp_runtime,
    sp_storage,
    wait_for,
)


class CrossHomeBase(BuddyBase):
    """A live shim under self.codex_dir, and a caller under another home."""

    def setUp(self):
        super().setUp()
        self.shim, self.tid, _rollout = self.make_shim(name="Fix the thing")
        self.shim_home = str(self.codex_dir)
        self.shim._save_state()
        self.hold_pidfile(self.tid, pid=os.getpid())
        self.shim._write_record()
        self.sid = new_uuid()
        self.listener, _rec = self.add_listener(
            name="cc-a", session_id=self.sid, pid=os.getppid()
        )

    def as_other_home(self):
        """Point CODEX_HOME at a second home holding its own, empty state DB."""
        other = self.root / "orca"
        other.mkdir()
        saved = self.codex_dir
        self.codex_dir = other
        try:
            self.make_state_db([])
        finally:
            self.codex_dir = saved
        os.environ["CODEX_HOME"] = str(other)
        return other

    def as_shim_home(self):
        os.environ["CODEX_HOME"] = self.shim_home

    def hold_reply(self, text="held answer"):
        self.shim.reply_budget.held = {
            self.sid: {"text": text, "mid": "m", "turn_id": "t", "at": time.time()}
        }

    def delivered(self):
        return self.listener.of_type("user")


class TestShimHomeRecord(CrossHomeBase):
    def test_the_shim_record_names_the_home_it_runs_under(self):
        self.assertEqual(self.shim.record()["codexHome"], self.shim_home)


class TestBudgetTargetsTheShimsHome(CrossHomeBase):
    def test_reset_from_another_home_releases_the_held_reply(self):
        self.hold_reply()
        self.as_other_home()
        rc, out, err = self.cli("budget", "reset", self.tid)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("no shim is running", out)
        self.assertFalse(
            (self.root / "orca" / "session-peers" / ("%s.budget-reset" % self.tid)).exists()
        )
        self.as_shim_home()
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_reset()
        self.assertTrue(wait_for(self.delivered))
        self.assertEqual(self.shim.reply_budget.held, {})

    def test_allow_from_another_home_reaches_the_shim_and_releases(self):
        self.shim.reply_budget.budgets[self.sid] = sp_constants.REPLY_BUDGET
        self.shim.reply_budget.budget_sender_sid = self.sid
        self.shim.reply_budget.budget_last_at = time.time()
        self.hold_reply()
        self.as_other_home()
        rc, out, err = self.cli(
            "budget", "allow", self.tid, "--replies", "10", "--for-session", self.sid
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("up to 10", out)
        self.assertNotIn("no shim is running", out)
        self.as_shim_home()
        with contextlib.redirect_stderr(io.StringIO()):
            self.shim.reply_budget.consume_allowance()
        self.assertEqual(self.shim.reply_budget.cap_for(self.sid), 10)
        self.assertTrue(wait_for(self.delivered))
        self.assertEqual(self.shim.reply_budget.held, {})

    def test_a_target_with_no_shim_in_any_home_says_so(self):
        # The caller's home is the only candidate left, and it must not read as
        # a grant the live shim will see.
        os.environ["CODEX_HOME"] = str(self.root / "elsewhere")
        (self.root / "elsewhere").mkdir()
        other = new_uuid()
        rc, out, _err = self.cli("budget", "reset", other)
        self.assertEqual(rc, 0)
        self.assertIn("no shim is running for %s" % other, out)


class TestBuddyAcrossHomes(CrossHomeBase):
    def setUp(self):
        super().setUp()
        self.owner = "cc:%s" % self.sid

    def test_binding_by_codex_uuid_resolves_a_thread_in_the_shims_home(self):
        self.as_other_home()
        rc, out, err = self.buddy(self.owner, "set", "codex:%s" % self.tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("(codex, %s)" % self.tid[:8], out)
        self.assertIn("Fix the thing", out)

    def test_binding_by_shim_name_records_a_codex_buddy(self):
        self.as_other_home()
        rc, out, err = self.buddy(self.owner, "set", self.shim.name)
        self.assertEqual(rc, 0, err)
        self.assertIn("(codex, %s)" % self.tid[:8], out)
        record = json.loads(self.record_path(self.owner).read_text())
        self.assertEqual(record["buddy"]["kind"], "codex")
        self.assertEqual(record["buddy"]["uuid"], self.tid)

    def test_budget_allow_buddy_works_for_a_buddy_bound_by_shim_name(self):
        self.as_other_home()
        rc, _out, err = self.buddy(self.owner, "set", self.shim.name)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("budget", "allow", "buddy", "--replies", "10", "--as", self.owner)
        self.assertEqual(rc, 0, err)
        self.assertIn(self.tid, out)
        self.assertTrue(
            (pathlib.Path(self.shim_home) / "session-peers" / ("%s.budget-allow" % self.tid)).exists()
        )

    def test_send_to_a_shim_named_buddy_takes_the_codex_path_with_a_reply_route(self):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = self.listener.path
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.sid
        self.as_other_home()
        rc, _out, err = self.buddy(self.owner, "set", self.shim.name)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("send", "--to", "buddy", "--message", "hello", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["target"], "codex")
        queued = self.queue_calls()
        self.assertEqual(len(queued), 1)
        text = queued[0][queued[0].index("--message") + 1]
        tag, body = sp_protocol.parse_tag(text)
        self.assertEqual(tag["reply"], self.listener.path)
        self.assertEqual(tag["sid"], self.sid)
        self.assertIn("Claude Code session", body.splitlines()[0])
        self.assertTrue(body.endswith("hello"))

    def test_send_to_the_shims_claude_alias_from_claude_takes_the_codex_path(self):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = self.listener.path
        self.as_other_home()
        rc, out, err = self.cli("send", "--to", "cc:%s" % self.shim.name, "--message", "hi", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["target"], "codex")
        self.assertEqual(len(self.queue_calls()), 1)


class TestListShowsCodexIdentity(CrossHomeBase):
    def test_list_labels_a_shim_record_with_its_thread_title(self):
        self.shim._save_state()
        self.as_other_home()
        rc, out, _err = self.cli("list")
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if self.shim.name in l)
        self.assertIn('[Codex thread "Fix the thing"]', line)

    def test_list_json_marks_the_runtime_of_each_peer(self):
        self.shim._save_state()
        rc, out, _err = self.cli("list", "--json")
        peers_by_name = {c["name"]: c for c in json.loads(out)["claude"]}
        self.assertEqual(peers_by_name[self.shim.name]["runtime"], "codex")
        self.assertEqual(peers_by_name[self.shim.name]["thread_title"], "Fix the thing")
        self.assertEqual(peers_by_name["cc-a"]["runtime"], "claude")


class TestSenderProvenance(CrossHomeBase):
    def test_a_codex_reply_to_claude_names_the_codex_thread(self):
        for sock in (self.listener.path, None):
            body = sp_protocol.build_cc_body("answer", self.tid, "codex-uzi", sock)
            self.assertIn(
                "[session-peers from Codex thread codex-uzi (%s)]" % self.tid, body
            )
            self.assertIn("\nanswer", body)

    def test_the_reply_header_stays_parseable_after_the_provenance_line(self):
        text = sp_protocol.reply_text("answer", "m-1")
        body = sp_protocol.build_cc_body(text, self.tid, "codex-uzi", None)
        lines = body.splitlines()
        self.assertTrue(lines[0].startswith("[session-peers from Codex thread"))
        self.assertEqual(lines[1], "[in reply to message m-1]")

    def test_a_hostile_name_cannot_end_the_provenance_line(self):
        line = sp_protocol.build_origin("claude", "a]\n[session-peers from=@x", self.sid)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(line.count("]"), 1)

    def test_a_message_relayed_into_codex_says_it_is_from_a_claude_session(self):
        frame = self.inbound_frame("ping", self.listener.path, from_session=self.sid)
        text, _body, _trim = self.shim._prepare_inbound(
            frame, "ping", self.listener_record(), "cc-a", self.sid, self.listener.path
        )
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("[session-peers from=@cc-a"))
        self.assertEqual(
            lines[1], "[session-peers from Claude Code session cc-a (%s)]" % self.sid
        )
        tag, body = sp_protocol.parse_tag(text)
        self.assertEqual(tag["sid"], self.sid)
        self.assertTrue(body.endswith("ping"))

    def listener_record(self):
        return next(r for r in sp_runtime_records() if r.get("sessionId") == self.sid)


def sp_runtime_records():
    from .support import sp_claude

    return sp_claude.live_claude_records()


class TestShortRequestLifetimeWarning(CrossHomeBase):
    def test_a_short_ask_lifetime_warns_and_the_default_does_not(self):
        os.environ["CODEX_THREAD_ID"] = self.tid
        rc, _out, err = self.cli(
            "dispatch", "--to", "cc:%s" % self.sid, "--message", "ping", "--timeout", "45", "--json"
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("45s request lifetime", err)
        rc, _out, err = self.cli(
            "dispatch", "--to", "cc:%s" % self.sid, "--message", "ping", "--json"
        )
        self.assertEqual(rc, 0, err)
        self.assertNotIn("request lifetime", err)


class TestShimOwnershipStaysInItsStartupHome(CrossHomeBase):
    def test_a_shim_cannot_take_another_homes_pidfile_when_that_shim_exits(self):
        other = self.as_other_home()
        first_pid = pathlib.Path(self.shim_home) / "session-peers" / (self.tid + ".pid")
        original_open = os.open

        def release_before_open(path, *args, **kwargs):
            if str(path) == str(first_pid):
                # The existing shim exits after resolution, before acquisition.
                for fh in self._held_pidfiles:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return original_open(path, *args, **kwargs)

        with mock.patch("os.open", side_effect=release_before_open):
            acquired = self.shim._acquire_ownership()
        try:
            self.assertTrue(acquired)
            self.assertEqual(
                pathlib.Path(sp_storage.thread_pid_path(self.tid)),
                other / "session-peers" / (self.tid + ".pid"),
            )
            self.assertEqual(self.shim.record()["codexHome"], str(other))
        finally:
            if self.shim._pidfile_fd is not None:
                os.close(self.shim._pidfile_fd)
                self.shim._pidfile_fd = None
            sp_storage.unpin_thread_home(self.tid)


class TestInboundRuntimeLabel(CrossHomeBase):
    def inbound_head(self, sender, name, sid):
        text, _body, _trim = self.shim._prepare_inbound(
            {"msg_id": "m"}, "hello", sender, name, sid, self.listener.path
        )
        return text.splitlines()[1]

    def test_a_codex_sender_is_labelled_a_codex_thread(self):
        sender = self.shim.record()
        line = self.inbound_head(sender, sender["name"], self.tid)
        self.assertEqual(
            line, "[session-peers from Codex thread %s (%s)]" % (sender["name"], self.tid)
        )

    def test_a_claude_sender_is_labelled_a_claude_session(self):
        sender = next(
            r for r in sp_runtime_records() if r.get("sessionId") == self.sid
        )
        line = self.inbound_head(sender, "cc-a", self.sid)
        self.assertEqual(line, "[session-peers from Claude Code session cc-a (%s)]" % self.sid)

    def test_an_unverified_sender_is_not_called_claude(self):
        line = self.inbound_head(None, "x", "y")
        self.assertNotIn("Claude", line)

    def test_send_from_a_codex_shim_socket_is_labelled_codex(self):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = self.shim.record()["messagingSocketPath"]
        with mock.patch.object(sp_claude_mod(), "claude_record_by_socket",
                               return_value=self.shim.record()):
            rc, _out, err = self.cli("send", "--to", "codex:%s" % self.tid, "--message", "hi")
        self.assertEqual(rc, 0, err)
        text = self.queue_calls()[0][4]
        self.assertIn("[session-peers from Codex thread", text.splitlines()[1])


def sp_claude_mod():
    from .support import sp_claude

    return sp_claude


class TestOriginLineBreaks(CrossHomeBase):
    BREAKS = ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\x00", "\x1b", "\x7f"]

    def test_no_line_boundary_survives_in_name_or_id(self):
        for ch in self.BREAKS:
            for runtime in ("claude", "codex"):
                line = sp_protocol.build_origin(runtime, "a%sb" % ch, "c%sd" % ch)
                self.assertEqual(len(line.splitlines()), 1, repr(ch))
                self.assertEqual(line, line.splitlines()[0], repr(ch))
                self.assertFalse(
                    any(c in line for c in self.BREAKS), repr(ch)
                )


class ForeignHomeBase(Base):
    """A caller under its own CODEX_HOME; the user's threads live in ~/.codex.

    No shim runs anywhere, which is the state a freshly renamed thread is in.
    """

    def setUp(self):
        super().setUp()
        self.default_home = self.home / ".codex"
        self.default_home.mkdir()
        self.tid, self.rollout = self.add_thread(self.default_home, "board")

    def add_thread(self, home, name, filename="state_5.sqlite"):
        tid = new_uuid()
        rollout = home / ("rollout-%s.jsonl" % tid)
        saved = self.codex_dir
        self.codex_dir = home
        try:
            self.make_state_db(
                [{"id": tid, "name": name, "rollout_path": str(rollout)}],
                filename=filename,
            )
            self.set_holder(rollout)
        finally:
            self.codex_dir = saved
        return tid, rollout


class TestNameResolvesAcrossHomes(ForeignHomeBase):
    def test_a_name_in_another_home_resolves_without_a_shim(self):
        thread = sp_codex.resolve_thread("board")
        self.assertEqual(thread["id"], self.tid)
        self.assertEqual(thread["codex_home"], str(self.default_home))
        self.assertTrue(thread["live"])

    def test_a_live_name_in_two_homes_is_ambiguous_and_lists_both(self):
        other_tid, _rollout = self.add_thread(self.codex_dir, "board", filename="state_2.sqlite")
        with self.assertRaises(sp_codex.ResolveError) as ctx:
            sp_codex.resolve_thread("board")
        self.assertIn(other_tid, str(ctx.exception))
        self.assertIn(self.tid, str(ctx.exception))

    def test_not_found_says_which_other_homes_were_searched(self):
        self.make_state_db([])
        with self.assertRaises(sp_codex.ResolveNotFound) as ctx:
            sp_codex.resolve_thread("nope")
        self.assertIn(str(self.default_home), str(ctx.exception))
        with self.assertRaises(sp_codex.ResolveNotFound) as ctx:
            sp_codex.resolve_thread(new_uuid())
        self.assertIn("also searched %s" % self.default_home, str(ctx.exception))

    def test_a_dead_thread_in_another_home_stays_dead(self):
        self.clear_holders()
        with self.assertRaises(sp_codex.ResolveNoLive):
            sp_codex.resolve_thread("board")


class TestUpUsesTheThreadsHome(ForeignHomeBase):
    def spawn_recorder(self):
        calls = []

        def fake_spawn(thread):
            calls.append((thread["id"], os.environ.get("CODEX_HOME")))
            return 4242

        return calls, mock.patch.object(sp_lifecycle, "spawn_shim", side_effect=fake_spawn)

    def test_up_by_name_registers_and_spawns_under_the_threads_home(self):
        calls, patch = self.spawn_recorder()
        with patch:
            rc, out, err = self.cli("up", "board")
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [(self.tid, str(self.default_home))])
        registered = json.loads(
            (self.default_home / "session-peers" / "registered.json").read_text()
        )
        self.assertIn(self.tid, registered["threads"])
        self.assertTrue(
            (self.default_home / "session-peers" / ("%s.budget-reset" % self.tid)).exists()
        )
        self.assertFalse((self.codex_dir / "session-peers" / "registered.json").exists())
        self.assertEqual(os.environ["CODEX_HOME"], str(self.codex_dir))

    def test_attach_spawns_under_the_threads_home(self):
        calls, patch = self.spawn_recorder()
        with patch:
            pid = sp_lifecycle.attach_thread(self.tid, verbose=False)
        self.assertEqual(pid, 4242)
        self.assertEqual(calls, [(self.tid, str(self.default_home))])
        self.assertEqual(os.environ["CODEX_HOME"], str(self.codex_dir))


class TestListAcrossHomes(ForeignHomeBase):
    def test_list_shows_the_foreign_thread_once_with_its_home(self):
        rc, out, _err = self.cli("list", "--json")
        self.assertEqual(rc, 0)
        live = [t for t in json.loads(out)["codex"] if t["id"] == self.tid]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["codex_home"], str(self.default_home))
        rc, text, _err = self.cli("list")
        self.assertIn("Codex threads (1 live)", text)
        self.assertEqual(sum(self.tid in line for line in text.splitlines()), 1)
        line = next(l for l in text.splitlines() if self.tid in l)
        self.assertIn("[home ", line)

    def test_a_thread_in_the_callers_home_has_no_home_suffix(self):
        local, _rollout = self.add_thread(self.codex_dir, "local", filename="state_2.sqlite")
        rc, text, _err = self.cli("list")
        line = next(l for l in text.splitlines() if local in l)
        self.assertNotIn("[home ", line)


class TestReplyFindsAnotherHomesMailbox(ForeignHomeBase):
    def test_reply_completes_a_request_written_under_another_home(self):
        sid = new_uuid()
        request_id = new_uuid()
        requests = self.default_home / "session-peers" / "requests"
        requests.mkdir(parents=True)
        (requests / ("%s.request.json" % request_id)).write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "requester_thread_id": self.tid,
                    "target_session_id": sid,
                    "expires_at": time.time() + 60,
                }
            )
        )
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        rc, _out, err = self.cli("reply", "--request", request_id, "--message", "answer")
        self.assertEqual(rc, 0, err)
        reply = json.loads((requests / ("%s.reply.json" % request_id)).read_text())
        self.assertEqual(reply["message"], "answer")
        self.assertEqual(reply["session_id"], sid)
        self.assertFalse(
            (self.codex_dir / "session-peers" / "requests" / ("%s.reply.json" % request_id)).exists()
        )

    def test_reply_still_checks_the_session_id_in_the_other_home(self):
        request_id = new_uuid()
        requests = self.default_home / "session-peers" / "requests"
        requests.mkdir(parents=True)
        (requests / ("%s.request.json" % request_id)).write_text(
            json.dumps({"target_session_id": new_uuid(), "expires_at": time.time() + 60})
        )
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, _out, err = self.cli("reply", "--request", request_id, "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("belongs to Claude session", err)

    def test_an_unknown_request_is_still_unknown(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, _out, err = self.cli("reply", "--request", new_uuid(), "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("unknown or expired", err)


class TestHookReconcileLogsWhyItGaveUp(ForeignHomeBase):
    def run_hook(self, thread_id):
        with mock.patch.multiple(
            sp_constants,
            HOOK_ATTACH_WAIT=0.3,
            HOOK_ATTACH_FAST_WINDOW=0.1,
            HOOK_ATTACH_FAST_STEP=0.05,
            HOOK_ATTACH_SLOW_STEP=0.05,
        ):
            rc, _out, _err = self.cli("hook-reconcile", "--thread", thread_id)
        self.assertEqual(rc, 0)
        log = self.codex_dir / "session-peers" / "session-hook.log"
        return log.read_text() if log.exists() else ""

    def test_a_thread_that_never_appears_leaves_one_line_with_the_reason(self):
        missing = new_uuid()
        self.make_state_db([])
        log = self.run_hook(missing)
        lines = [l for l in log.splitlines() if "gave up" in l]
        self.assertEqual(len(lines), 1, log)
        self.assertIn(missing, lines[0])
        self.assertIn("no Codex thread with id", lines[0])

    def test_a_thread_that_is_not_live_logs_that_state(self):
        self.clear_holders()
        log = self.run_hook(self.tid)
        lines = [l for l in log.splitlines() if "gave up" in l]
        self.assertEqual(len(lines), 1, log)
        self.assertIn("not live", lines[0])

    def test_a_successful_attach_logs_nothing(self):
        with mock.patch.object(sp_lifecycle, "spawn_shim", return_value=4242):
            log = self.run_hook(self.tid)
        self.assertNotIn("gave up", log)


class TestDoctorMentionsTheDefaultHome(ForeignHomeBase):
    def test_doctor_says_discovery_also_searches_the_default_home(self):
        rc, out, _err = self.cli("doctor")
        self.assertIn("discovery also searches %s" % self.default_home, out)
