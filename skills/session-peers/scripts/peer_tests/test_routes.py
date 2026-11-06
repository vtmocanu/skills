"""Typed route check: wrong transport, wrong verb and ambiguous names are refused with exact advice."""

from peer_tests.support import *  # noqa: F401,F403
from session_peers import identity as sp_identity
from peer_tests.test_opencode import OpenCodeBase, OC_A, OC_B, oc_session

NOT_EQUIVALENT = (
    " send is asynchronous and is not an equivalent: it has no request id, "
    "single-use reply or await."
)
NO_CORRELATED_CODEX = " A correlated Claude-to-Codex request does not exist yet (#88)."
SENDMESSAGE_TAIL = " is for disambiguation only; do not pass it to SendMessage."
ASK_TAIL = " A correlated ask has no Claude-to-Claude equivalent; SendMessage is asynchronous."
DEAD_PID = 99999990


def request_refusal(verb, tid):
    return (
        "error: %s cannot reach Codex thread codex:%s: correlated requests reach "
        "only Claude Code and OpenCode peers." % (verb, tid)
    )


def claude_refusal(label, how, sid, verb="send"):
    return (
        "error: this Claude Code session must reach Claude Code session %s with "
        "native SendMessage, not peers.py. %s Typed ID cc:%s%s%s"
        % (label, how, sid, SENDMESSAGE_TAIL, "" if verb == "send" else ASK_TAIL)
    )


class RoutesBase(OpenCodeBase, BuddyBase):
    def setUp(self):
        super().setUp()
        # An empty state DB: Codex discovery works and finds nothing by default.
        self.make_state_db([])

    def make_state_db(self, threads, **kwargs):
        # Later fixtures (one_thread, threads) replace the default empty table.
        for old in self.codex_dir.glob("state_*.sqlite"):
            old.unlink()
        return super().make_state_db(threads, **kwargs)

    def threads(self, specs):
        """specs: (id, name, live, updated_at); one state DB, holders for the live ones."""
        rows = []
        for tid, name, live, updated in specs:
            rollout = self.make_rollout("%s.jsonl" % tid)
            rows.append({"id": tid, "name": name, "rollout_path": str(rollout), "updated_at": updated})
            if live:
                self.set_holder(rollout)
        self.make_state_db(rows)

    def as_codex(self, tid=None):
        tid = tid or new_uuid()
        os.environ["CODEX_THREAD_ID"] = tid
        return tid

    def as_claude_session(self, sid=None):
        sid = sid or new_uuid()
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        return sid

    def second_listener(self, name, sid, pid=None, **over):
        """A live Claude record in another live process (this process's parent)."""
        pid = os.getppid() if pid is None else pid
        sock_path = str(self.socks / ("%d.sock" % pid))
        listener = Listener(sock_path)
        self._listeners.append(listener)
        return listener, self.write_record(pid, name, sid, sock_path, **over)

    def dead_record(self, name, sid, pid=DEAD_PID):
        return self.write_record(pid, name, sid, str(self.socks / ("%d.sock" % pid)))

    def nothing_sent(self, *listeners):
        time.sleep(0.1)
        self.assertEqual(self.queue_calls(), [])
        for listener in listeners:
            self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*")), [])
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def refused(self, expected, *argv, code=2, listeners=()):
        rc, out, err = self.cli(*argv)
        self.assertEqual(rc, code, err)
        self.assertEqual(err, expected + "\n")
        self.assertEqual(out, "")
        self.nothing_sent(*listeners)


class TestRoutesBareNames(RoutesBase):
    def test_a_bare_claude_name_gets_the_typed_command_with_the_message_file_placeholder(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_codex()
        self.refused(
            'error: --to needs a typed target; "cc-main" is Claude Code session cc:%s (live). Run:\n'
            "  peers.py send --to cc:%s --message-file <PATH>\n"
            "  (write the message to PATH)" % (rec["sessionId"], rec["sessionId"]),
            "send", "--to", "cc-main", "--message", "hi", listeners=[listener],
        )

    def test_a_bare_name_keeps_the_options_and_the_message_file(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        tid = new_uuid()
        path = self.root / "my msg.txt"
        path.write_text("hello")
        self.refused(
            'error: --to needs a typed target; "cc-main" is Claude Code session cc:%s (live). Run:\n'
            "  peers.py send --to cc:%s --message-file '%s' --from-thread %s --from-name n1 --json"
            % (rec["sessionId"], rec["sessionId"], path, tid),
            "send", "--to", "cc-main", "--message-file", str(path), "--from-thread", tid,
            "--from-name", "n1", "--json", listeners=[listener],
        )

    def test_a_bare_codex_name_gets_the_typed_command(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.as_codex()
        self.refused(
            'error: --to needs a typed target; "codex-uzi" is Codex thread codex:%s (live). Run:\n'
            "  peers.py send --to codex:%s --message-file <PATH>\n"
            "  (write the message to PATH)" % (tid, tid),
            "send", "--to", "codex-uzi", "--message", "hi",
        )

    def test_a_bare_opencode_title_gets_the_typed_command(self):
        self.refused(
            'error: --to needs a typed target; "oc-lead" is OpenCode session oc:%s (recent, idle). Run:\n'
            "  peers.py send --to oc:%s --message-file <PATH> --wake\n"
            "  (write the message to PATH)" % (OC_A, OC_A),
            "send", "--to", "oc-lead", "--message", "hi", "--wake",
        )

    def test_a_bare_wait_target_uses_the_for_flag(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.refused(
            'error: --for needs a typed target; "cc-main" is Claude Code session cc:%s (live). Run:\n'
            "  peers.py wait --for cc:%s --timeout 5 --state busy --json"
            % (rec["sessionId"], rec["sessionId"]),
            "wait", "--for", "cc-main", "--timeout", "5", "--state", "busy", "--json",
        )

    def test_an_unknown_bare_name_is_not_found(self):
        self.refused(
            "error: no Claude session, Codex thread or OpenCode session named 'nobody'; "
            "run `peers.py list`",
            "send", "--to", "nobody", "--message", "hi",
        )

    def test_ask_to_a_bare_codex_name_is_refused_as_a_request(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.as_codex()
        self.refused(
            request_refusal("ask", tid) + NOT_EQUIVALENT,
            "ask", "--to", "codex-uzi", "--message", "q",
        )

    def test_wait_on_a_bare_codex_name_names_the_shim_command(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.refused(
            "error: wait reads a Claude-registry status; Codex thread codex:%s has no "
            "running shim, so no status is published. Run:\n"
            "  peers.py restart %s\n"
            "  peers.py wait --for cc:%s --timeout 1 --state idle" % (tid, tid, tid),
            "wait", "--for", "codex-uzi", "--timeout", "1",
        )

    def test_a_claude_sender_is_sent_to_native_sendmessage_for_a_bare_claude_name(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude_session()
        self.refused(
            claude_refusal("cc-main", 'Use SendMessage with to: "cc-main".', rec["sessionId"]),
            "send", "--to", "cc-main", "--message", "hi", listeners=[listener],
        )


class TestRoutesAmbiguity(RoutesBase):
    def test_a_name_shared_by_a_claude_session_and_a_codex_thread_lists_both(self):
        sid, tid = new_uuid(), new_uuid()
        self.add_listener(name="twin", session_id=sid)
        self.threads([(tid, "twin", True, "2026-09-08T12:00:00Z")])
        self.refused(
            'error: "twin" is ambiguous; pick one typed ID:\n'
            "  codex:%s  Codex thread  live\n"
            "  cc:%s  Claude session  live" % (tid, sid),
            "send", "--to", "twin", "--message", "hi",
        )

    def test_live_and_dead_claude_duplicates_are_labelled(self):
        live_a, live_b, dead = new_uuid(), new_uuid(), new_uuid()
        self.add_listener(name="dup", session_id=live_a)
        self.second_listener("dup", live_b)
        self.dead_record("dup", dead)
        rc, _out, err = self.cli("send", "--to", "dup", "--message", "hi")
        self.assertEqual(rc, 2)
        head, *lines = err.splitlines()
        self.assertEqual(head, 'error: "dup" is ambiguous; pick one typed ID:')
        self.assertEqual(sorted(lines[:2]), sorted([
            "  cc:%s  Claude session  live" % live_a, "  cc:%s  Claude session  live" % live_b]))
        self.assertEqual(lines[2:], ["  cc:%s  Claude session  dead" % dead])

    def test_an_unverifiable_claude_namesake_lists_candidates_with_its_liveness(self):
        live, unverified = new_uuid(), new_uuid()
        self.add_listener(name="dup", session_id=live)
        self.second_listener("dup", unverified, procStart=None)
        rc, _out, err = self.cli("send", "--to", "dup", "--message", "hi")
        self.assertEqual(rc, 2)
        head, *rest = err.splitlines()
        self.assertEqual(head, (
            "error: cannot verify Claude target 'dup' because the process-start probe is "
            "unavailable; retry outside the sandbox or with host permission; pick the kind"))
        self.assertEqual(rest, [
            "Candidates:",
            "  cc:%s  Claude session  live" % live,
            "  cc:%s  Claude session  unverified" % unverified,
        ])

    def test_bare_duplicates_of_one_codex_kind_list_every_thread(self):
        a, b = new_uuid(), new_uuid()
        self.threads([(a, "dup", True, "2026-09-08T12:00:00Z"), (b, "dup", True, "2026-09-07T12:00:00Z")])
        self.refused(
            'error: "dup" is ambiguous; pick one typed ID:\n'
            "  codex:%s  Codex thread  live\n"
            "  codex:%s  Codex thread  live" % (a, b),
            "send", "--to", "dup", "--message", "hi",
        )

    def test_bare_duplicates_of_one_opencode_kind_list_every_session(self):
        self.set_oc(sessions=[
            oc_session(OC_A, "dup", updated_ms=time.time() * 1000),
            oc_session(OC_B, "dup", updated_ms=time.time() * 1000 - 5000),
        ])
        self.refused(
            'error: "dup" is ambiguous; pick one typed ID:\n'
            "  oc:%s  OpenCode session  recent (idle)\n"
            "  oc:%s  OpenCode session  recent (idle)" % (OC_A, OC_B),
            "send", "--to", "dup", "--message", "hi",
        )

    def test_typed_claude_duplicates_keep_the_message_and_list_candidates(self):
        a, b = new_uuid(), new_uuid()
        self.add_listener(name="dup", session_id=a)
        self.second_listener("dup", b)
        self.as_codex()
        rc, _out, err = self.cli("send", "--to", "cc:dup", "--message", "hi")
        self.assertEqual(rc, 1)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("error: 'dup' names 2 live sessions ("), err)
        self.assertTrue(lines[0].endswith("); rename one"), err)
        self.assertEqual(lines[1], "pick one typed ID:")
        self.assertEqual(sorted(lines[2:]), sorted([
            "  cc:%s  Claude session  live" % a, "  cc:%s  Claude session  live" % b]))
        self.nothing_sent()

    def test_typed_codex_duplicates_keep_the_message_and_list_candidates(self):
        a, b = new_uuid(), new_uuid()
        self.threads([(a, "dup", True, "2026-09-08T12:00:00Z"), (b, "dup", True, "2026-09-07T12:00:00Z")])
        rc, _out, err = self.cli("send", "--to", "codex:dup", "--message", "hi")
        self.assertEqual(rc, 1)
        lines = err.splitlines()
        self.assertIn("'dup' matches 2 live threads (", lines[0])
        self.assertTrue(lines[0].endswith("); register by UUID instead"), err)
        self.assertEqual(lines[1:], [
            "pick one typed ID:",
            "  codex:%s  Codex thread  live" % a,
            "  codex:%s  Codex thread  live" % b,
        ])
        self.nothing_sent()

    def dead_duplicates(self, count=2):
        ids = [new_uuid() for _ in range(count)]
        self.threads([
            (tid, "dup", False, "2026-09-%02dT12:00:00Z" % (10 + i)) for i, tid in enumerate(ids)
        ])
        # Newest first: the last id has the latest updated_at.
        return list(reversed(ids))

    def dead_lines(self, ids):
        return ["  codex:%s  Codex thread  dead" % tid for tid in ids]

    def test_bare_dead_only_codex_duplicates_are_ambiguous(self):
        ids = self.dead_duplicates()
        self.refused(
            "\n".join(['error: "dup" is ambiguous; pick one typed ID:'] + self.dead_lines(ids)),
            "send", "--to", "dup", "--message", "hi",
        )

    def test_typed_dead_only_codex_duplicates_keep_the_no_live_message_and_list_candidates(self):
        ids = self.dead_duplicates()
        self.refused(
            "\n".join([
                "error: no live Codex thread named 'dup' (2 past thread(s) carried that name); "
                "/rename the running one, or pass its UUID",
                "pick one typed ID:",
            ] + self.dead_lines(ids)),
            "send", "--to", "codex:dup", "--message", "hi", code=1,
        )

    def test_down_and_budget_reset_list_dead_only_duplicates(self):
        ids = self.dead_duplicates()
        expected = "\n".join([
            "error: no live Codex thread named 'dup' (2 past thread(s) carried that name); "
            "/rename the running one, or pass its UUID",
            "pick one typed ID:",
        ] + self.dead_lines(ids)) + "\n"
        for argv in (("down", "dup"), ("budget", "reset", "dup")):
            with self.subTest(argv=argv):
                rc, _out, err = self.cli(*argv)
                self.assertEqual(rc, 1)
                self.assertEqual(err, expected)

    def test_every_dead_candidate_is_listed_beyond_five(self):
        ids = self.dead_duplicates(7)
        rc, _out, err = self.cli("send", "--to", "dup", "--message", "hi")
        self.assertEqual(rc, 2)
        self.assertEqual(err.splitlines()[1:], self.dead_lines(ids))

    def test_buddy_set_on_a_duplicate_claude_name_lists_candidates(self):
        a, b = new_uuid(), new_uuid()
        self.add_listener(name="dup", session_id=a)
        self.second_listener("dup", b)
        owner = "codex:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "cc:dup")
        self.assertEqual(rc, 1)
        lines = err.splitlines()
        self.assertTrue(lines[0].startswith("error: 'dup' names 2 live sessions ("), err)
        self.assertEqual(lines[1], "pick one typed ID:")
        self.assertEqual(sorted(lines[2:]), sorted([
            "  cc:%s  Claude session  live" % a, "  cc:%s  Claude session  live" % b]))
        self.assertFalse(self.record_path(owner).exists())

    def test_buddy_set_on_a_bare_ambiguous_name_keeps_the_retry_commands_and_lists_candidates(self):
        sid, tid = new_uuid(), new_uuid()
        self.add_listener(name="twin", session_id=sid)
        self.threads([(tid, "twin", True, "2026-09-08T12:00:00Z")])
        owner = "codex:%s" % new_uuid()
        rc, _out, err = self.buddy(owner, "set", "twin")
        self.assertEqual(rc, 1)
        self.assertIn("  peers.py buddy set cc:%s --as %s" % (sid, owner), err)
        self.assertIn("  peers.py buddy set codex:%s --as %s" % (tid, owner), err)
        self.assertTrue(err.endswith(
            "pick one typed ID:\n  codex:%s  Codex thread  live\n  cc:%s  Claude session  live\n" % (tid, sid)), err)

    def test_up_and_restart_with_a_live_duplicate_list_candidates(self):
        a, b = new_uuid(), new_uuid()
        self.threads([(a, "dup", True, "2026-09-08T12:00:00Z"), (b, "dup", True, "2026-09-07T12:00:00Z")])
        for argv in (("up", "dup"), ("restart", "dup")):
            with self.subTest(argv=argv):
                rc, _out, err = self.cli(*argv)
                self.assertEqual(rc, 1)
                lines = err.splitlines()
                self.assertIn("'dup' matches 2 live threads (", lines[0])
                self.assertEqual(lines[1:], [
                    "pick one typed ID:",
                    "  codex:%s  Codex thread  live" % a,
                    "  codex:%s  Codex thread  live" % b,
                ])


class TestRoutesPreservedResolution(RoutesBase):
    def test_a_single_thread_with_unverified_liveness_still_resolves(self):
        tid, _rollout = self.one_thread(name="solo")
        os.environ["FAKE_LSOF_RC"] = "1"
        self.assertEqual(sp_codex.resolve_thread_prefer_live("solo")["id"], tid)
        rc, _out, err = self.cli("budget", "reset", "solo")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("down", "solo")
        self.assertEqual(rc, 0, err)
        self.assertIn(tid, out)

    def test_a_live_thread_beats_two_past_threads_with_the_same_name(self):
        live, old_a, old_b = new_uuid(), new_uuid(), new_uuid()
        self.threads([
            (live, "helper", True, "2026-09-09T12:00:00Z"),
            (old_a, "helper", False, "2026-09-08T12:00:00Z"),
            (old_b, "helper", False, "2026-09-07T12:00:00Z"),
        ])
        self.assertEqual(sp_codex.resolve_thread_prefer_live("helper")["id"], live)
        self.assertEqual(sp_identity.resolve_typed("helper")["uuid"], live)

    def test_a_single_dead_thread_resolves_for_a_bare_name(self):
        tid = new_uuid()
        self.threads([(tid, "gone", False, "2026-09-08T12:00:00Z")])
        found = sp_identity.resolve_typed("gone")
        self.assertEqual((found["kind"], found["uuid"]), ("codex", tid))


class TestRoutesWrongVerb(RoutesBase):
    def test_ask_and_dispatch_to_a_codex_thread_from_a_claude_sender_cite_the_missing_request(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.as_claude_session()
        for verb in ("ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    request_refusal(verb, tid) + NO_CORRELATED_CODEX + NOT_EQUIVALENT,
                    verb, "--to", "codex:%s" % tid, "--message", "q",
                )

    def test_ask_and_dispatch_to_a_codex_thread_from_a_codex_sender_omit_the_claude_note(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.as_codex()
        for verb in ("ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    request_refusal(verb, tid) + NOT_EQUIVALENT,
                    verb, "--to", "codex:codex-uzi", "--message", "q",
                )

    def test_ask_and_dispatch_to_a_codex_thread_from_an_opencode_sender_omit_the_claude_note(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.as_opencode()
        for verb in ("ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    request_refusal(verb, tid) + NOT_EQUIVALENT,
                    verb, "--to", "codex:%s" % tid, "--message", "q",
                )

    def test_ask_to_a_codex_shim_record_is_a_request_to_a_codex_thread(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.write_record(os.getpid(), "codex-uzi", tid, str(self.socks / "shim.sock"), entrypoint="codex")
        self.as_codex()
        self.refused(
            request_refusal("ask", tid) + NOT_EQUIVALENT,
            "ask", "--to", "cc:codex-uzi", "--message", "q",
        )

    def test_wait_for_a_codex_thread_with_a_live_shim_names_the_registry_command(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.write_record(os.getpid(), "codex-uzi", tid, str(self.socks / "shim.sock"), entrypoint="codex")
        self.refused(
            "error: wait reads a Claude-registry status; Codex thread codex:%s publishes it "
            "through its shim. Run:\n"
            "  peers.py wait --for cc:%s --timeout 1 --state idle" % (tid, tid),
            "wait", "--for", "codex:%s" % tid, "--timeout", "1",
        )

    def test_wait_for_a_live_codex_thread_without_a_shim_names_restart(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.refused(
            "error: wait reads a Claude-registry status; Codex thread codex:%s has no "
            "running shim, so no status is published. Run:\n"
            "  peers.py restart %s\n"
            "  peers.py wait --for cc:%s --state busy --json" % (tid, tid, tid),
            "wait", "--for", "codex:%s" % tid, "--state", "busy", "--json",
        )

    def test_wait_for_a_dead_codex_thread_has_no_equivalent(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.clear_holders()
        self.refused(
            "error: wait reads a Claude-registry status; Codex thread codex:%s is not live, "
            "so no status can be published; there is no equivalent." % tid,
            "wait", "--for", "codex:%s" % tid,
        )

    def test_a_codex_buddy_is_refused_for_ask_dispatch_and_wait(self):
        tid, _rollout = self.one_thread(name="buddy-codex")
        owner = new_uuid()
        self.assertEqual(self.buddy("codex:%s" % owner, "set", "codex:%s" % tid)[0], 0)
        os.environ["CODEX_THREAD_ID"] = owner
        for verb in ("ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    request_refusal(verb, tid) + NOT_EQUIVALENT,
                    verb, "--to", "buddy", "--message", "q",
                )
        self.refused(
            "error: wait reads a Claude-registry status; Codex thread codex:%s has no "
            "running shim, so no status is published. Run:\n"
            "  peers.py restart %s\n"
            "  peers.py wait --for cc:%s --timeout 1 --state idle" % (tid, tid, tid),
            "wait", "--for", "buddy", "--timeout", "1",
        )


class TestRoutesClaudeToClaude(RoutesBase):
    def test_a_unique_verified_name_is_addressed_by_native_sendmessage(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude_session()
        for verb in ("send", "ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    claude_refusal("cc-main", 'Use SendMessage with to: "cc-main".', rec["sessionId"], verb),
                    verb, "--to", "cc:%s" % rec["sessionId"], "--message", "q", listeners=[listener],
                )

    def test_a_duplicate_name_gets_no_sendmessage_address(self):
        a, b = new_uuid(), new_uuid()
        listener, _rec = self.add_listener(name="dup", session_id=a)
        other, _rec2 = self.second_listener("dup", b)
        self.as_claude_session()
        how = (
            "It has no unique verified session name, so SendMessage cannot be addressed "
            "safely: have it /rename to a unique name, or pick it in ListAgents."
        )
        for verb in ("send", "ask", "dispatch"):
            with self.subTest(verb=verb):
                self.refused(
                    claude_refusal("dup", how, a, verb),
                    verb, "--to", "cc:%s" % a, "--message", "q", listeners=[listener, other],
                )

    def test_a_session_without_a_name_gets_no_sendmessage_address(self):
        sid = new_uuid()
        listener, _rec = self.add_listener(name=None, session_id=sid)
        self.as_claude_session()
        how = (
            "It has no unique verified session name, so SendMessage cannot be addressed "
            "safely: have it /rename to a unique name, or pick it in ListAgents."
        )
        self.refused(
            claude_refusal("cc:%s" % sid, how, sid),
            "send", "--to", "cc:%s" % sid, "--message", "q", listeners=[listener],
        )

    def test_an_unverified_namesake_makes_the_name_unverifiable(self):
        a, b = new_uuid(), new_uuid()
        listener, _rec = self.add_listener(name="solo", session_id=a)
        self.second_listener("solo", b, procStart=None)
        self.as_claude_session()
        how = (
            "Its session name cannot be verified as unique (another session named 'solo' "
            "cannot be checked for liveness), so SendMessage cannot be addressed safely: "
            "pick it in ListAgents or have it /rename to a unique name."
        )
        self.refused(
            claude_refusal("solo", how, a),
            "send", "--to", "cc:%s" % a, "--message", "q", listeners=[listener],
        )

    def test_an_incomplete_registry_read_makes_the_name_unverifiable(self):
        sid = new_uuid()
        listener, _rec = self.add_listener(name="cc-main", session_id=sid)
        (self.sessions / "junk.json").write_text("{")
        self.as_claude_session()
        rc, _out, err = self.cli("send", "--to", "cc:%s" % sid, "--message", "q")
        self.assertEqual(rc, 2, err)
        # The lenient reader logs the junk file first; the refusal is the last line.
        err = err.splitlines(True)[-1]
        prefix = (
            "error: this Claude Code session must reach Claude Code session cc-main with "
            "native SendMessage, not peers.py. Its session name cannot be verified as unique "
            "(registry read incomplete: "
        )
        suffix = (
            "), so SendMessage cannot be addressed safely: pick it in ListAgents or have it "
            "/rename to a unique name. Typed ID cc:%s%s\n" % (sid, SENDMESSAGE_TAIL)
        )
        self.assertTrue(err.startswith(prefix) and err.endswith(suffix), err)
        self.nothing_sent(listener)

    def test_a_socket_only_session_with_an_invalid_codex_id_cannot_be_attributed(self):
        listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = rec["messagingSocketPath"]
        os.environ["CODEX_THREAD_ID"] = "not-a-uuid"
        for argv in (
            ("send", "--to", "cc:%s" % rec["sessionId"], "--message", "q"),
            ("wait", "--for", "cc:%s" % rec["sessionId"], "--timeout", "1"),
        ):
            with self.subTest(argv=argv[0]):
                self.refused(
                    "error: --from-thread/CODEX_THREAD_ID must be a UUID; this environment also "
                    "names a Claude Code session, so the sender cannot be attributed. "
                    "Nothing was sent.",
                    *argv, listeners=[listener],
                )

    def test_a_socket_only_session_without_a_codex_id_is_sent_to_sendmessage(self):
        sender, sender_rec = self.add_listener(name="cc-sender", session_id=new_uuid())
        target, target_rec = self.second_listener("cc-target", new_uuid())
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = sender_rec["messagingSocketPath"]
        self.refused(
            claude_refusal("cc-target", 'Use SendMessage with to: "cc-target".', target_rec["sessionId"]),
            "send", "--to", "cc:cc-target", "--message", "q", listeners=[sender, target],
        )


class TestRoutesConsistency(RoutesBase):
    def two_sessions(self):
        a, b = new_uuid(), new_uuid()
        listener_a, rec_a = self.add_listener(name="name-a", session_id=a)
        listener_b, rec_b = self.second_listener("name-b", b)
        return (listener_a, rec_a), (listener_b, rec_b)

    def swapping_resolver(self, first, later):
        calls = []

        def resolve(target, exclude=None):
            calls.append(target)
            return first if len(calls) == 1 else later

        return mock.patch.object(sp_claude, "_resolve_claude_record", side_effect=resolve), calls

    def test_a_target_that_changes_after_the_check_is_refused_at_delivery(self):
        (la, rec_a), (lb, rec_b) = self.two_sessions()
        self.as_codex()
        expected = (
            "error: cc:name-a changed between the route check and delivery "
            "(checked cc:%s, now cc:%s); nothing was sent" % (rec_a["sessionId"], rec_b["sessionId"])
        )
        for argv in (
            ("send", "--to", "cc:name-a", "--message", "q"),
            ("ask", "--to", "cc:name-a", "--message", "q"),
            ("dispatch", "--to", "cc:name-a", "--message", "q"),
        ):
            with self.subTest(argv=argv[0]):
                patch, _calls = self.swapping_resolver(rec_a, rec_b)
                with patch:
                    self.refused(expected, *argv, code=1, listeners=[la, lb])
        patch, _calls = self.swapping_resolver(rec_a, rec_b)
        with patch:
            self.refused(
                expected,
                "wait", "--for", "cc:name-a", "--timeout", "1", code=1, listeners=[la, lb],
            )

    def test_a_first_lookup_failure_is_the_error_even_if_a_later_lookup_would_succeed(self):
        (la, rec_a), (lb, _rec_b) = self.two_sessions()
        self.as_codex()
        for argv in (
            ("send", "--to", "cc:name-a", "--message", "q"),
            ("ask", "--to", "cc:name-a", "--message", "q"),
            ("wait", "--for", "cc:name-a", "--timeout", "1"),
        ):
            with self.subTest(argv=argv[0]):
                calls = []

                def resolve(target, exclude=None, calls=calls):
                    calls.append(target)
                    if len(calls) == 1:
                        raise sp_codex.ResolveNotFound("no live Claude session with name 'name-a'")
                    return rec_a

                with mock.patch.object(sp_claude, "_resolve_claude_record", side_effect=resolve):
                    self.refused(
                        "error: no live Claude session with name 'name-a'", *argv, code=1,
                        listeners=[la, lb],
                    )
                self.assertEqual(calls, ["name-a"])


class TestRoutesStillDeliver(RoutesBase):
    def setUp(self):
        super().setUp()
        self.listener, self.rec = self.add_listener(name="cc-main", session_id="s1")

    def test_send_ask_and_dispatch_from_a_codex_thread_still_reach_a_named_session(self):
        self.as_codex()
        rc, _out, err = self.cli("send", "--to", "cc:cc-main", "--message", "one")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("dispatch", "--to", "cc:cc-main", "--message", "two", "--json")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("ask", "--to", "cc:cc-main", "--message", "three", "--timeout", "1")
        self.assertEqual(rc, 124, err)
        self.assertTrue(wait_for(lambda: len(self.listener.of_type("user")) == 3))

    def test_wait_for_a_named_session_still_resolves(self):
        rc, out, err = self.cli("wait", "--for", "cc:cc-main", "--timeout", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("cc-main is idle", out)

    def test_a_claude_sender_still_reaches_a_codex_shim_through_its_thread(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.write_record(os.getppid(), "codex-uzi", tid, str(self.socks / "shim.sock"), entrypoint="codex")
        self.as_claude_session()
        rc, _out, err = self.cli("send", "--to", "cc:codex-uzi", "--message", "hi")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.queue_calls()[0][2], tid)

    def test_wait_for_a_shim_uuid_still_works(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        self.write_record(os.getppid(), "codex-uzi", tid, str(self.socks / "shim.sock"), entrypoint="codex")
        rc, out, err = self.cli("wait", "--for", "cc:%s" % tid, "--timeout", "2")
        self.assertEqual(rc, 0, err)
        self.assertIn("codex-uzi is idle", out)
