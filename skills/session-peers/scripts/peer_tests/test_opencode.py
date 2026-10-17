"""OpenCode V2 sessions as peers: discovery, identity, send, requests, buddy."""

from peer_tests.support import *  # noqa: F401,F403

OC_A = "ses_aaaaaaaaaaaaAAAAAAAAAAAAAA"
OC_B = "ses_bbbbbbbbbbbbBBBBBBBBBBBBBB"
OC_CHILD = "ses_ccccccccccccCCCCCCCCCCCCCC"
OC_OLD = "ses_ddddddddddddDDDDDDDDDDDDDD"


def oc_session(sid, title, parent=None, updated_ms=None):
    return {
        "id": sid,
        "title": title,
        "parentID": parent,
        "projectID": "p1",
        "location": {"directory": "/work/%s" % title},
        "time": {"created": 1, "updated": updated_ms if updated_ms is not None else time.time() * 1000},
    }


class OpenCodeBase(Base):
    def setUp(self):
        super().setUp()
        self.oc_state = self.root / "opencode.json"
        self.oc_log = self.root / "opencode.log"
        os.environ["FAKE_OPENCODE_STATE"] = str(self.oc_state)
        os.environ["FAKE_OPENCODE_LOG"] = str(self.oc_log)
        self._write_fake("opencode", FAKE_OPENCODE)
        self.set_oc(sessions=[
            oc_session(OC_A, "oc-lead"),
            oc_session(OC_B, "oc-buddy"),
            oc_session(OC_CHILD, "a-subagent", parent=OC_A),
            oc_session(OC_OLD, "oc-old", updated_ms=1000),
        ])

    def set_oc(self, sessions=None, **state):
        current = json.loads(self.oc_state.read_text()) if self.oc_state.exists() else {}
        if sessions is not None:
            current["sessions"] = {s["id"]: s for s in sessions}
        current.update(state)
        self.oc_state.write_text(json.dumps(current))

    def oc_calls(self, op=None):
        if not self.oc_log.exists():
            return []
        calls = [json.loads(line) for line in self.oc_log.read_text().splitlines() if line.strip()]
        return [c for c in calls if op is None or c["op"] == op]

    def as_opencode(self, sid=OC_A):
        os.environ["OPENCODE_SESSION_ID"] = sid
        os.environ["OPENCODE"] = "1"

    def as_claude(self, listener_rec):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = listener_rec["messagingSocketPath"]
        os.environ["CLAUDE_CODE_SESSION_ID"] = listener_rec["sessionId"]


class OpenCodeDiscoveryTests(OpenCodeBase):
    def test_title_resolution_checks_later_pages(self):
        self.set_oc(sessions=[oc_session(OC_A, "twin"), oc_session(OC_B, "twin")], page_size=1)
        rc, _out, err = self.cli("send", "--to", "oc:twin", "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn(OC_A, err)
        self.assertIn(OC_B, err)
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_repeated_cursor_refuses_title_resolution(self):
        self.set_oc(page_size=1, repeat_cursor=True)
        rc, _out, err = self.cli("send", "--to", "oc:oc-lead", "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("cursor", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn("cursor", json.loads(out)["opencode_error"])

    def test_list_shows_recent_top_level_sessions_with_running_state(self):
        self.set_oc(active=[OC_B])
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertIsNone(payload["opencode_error"])
        rows = {r["id"]: r for r in payload["opencode"]}
        # A subagent child and a stale session are not peers to list.
        self.assertEqual(set(rows), {OC_A, OC_B})
        self.assertEqual(rows[OC_A]["state"], "idle")
        self.assertEqual(rows[OC_B]["state"], "running")
        self.assertEqual(rows[OC_A]["runtime"], "opencode")

    def test_list_never_calls_the_api_of_a_stopped_service(self):
        # `opencode api` starts a stopped service; discovery must not.
        self.set_oc(stopped=True)
        self.add_listener(name="cc-main", session_id="s1")
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertIn("not running", payload["opencode_error"])
        self.assertEqual(payload["opencode"], [])
        self.assertEqual([c for c in self.oc_calls() if c["op"] != "service.status"], [])
        # The other runtimes are still listed.
        self.assertEqual([c["sessionId"] for c in payload["claude"]], ["s1"])

    def test_list_without_opencode_installed_is_quiet(self):
        (self.bin / "opencode").unlink()
        rc, out, _err = self.cli("list")
        self.assertEqual(rc, 0)
        self.assertNotIn("OpenCode", out)
        rc, out, _err = self.cli("list", "--json")
        self.assertEqual(json.loads(out)["opencode_error"], "opencode is not installed")

    def test_active_failure_reports_unknown_state_not_idle(self):
        state = json.loads(self.oc_state.read_text())
        state["sessions"] = {OC_A: oc_session(OC_A, "oc-lead")}
        self.oc_state.write_text(json.dumps(state))
        original = sp_opencode.active_ids

        def boom():
            raise sp_opencode.OpenCodeError("active unreadable")

        sp_opencode.active_ids = boom
        try:
            rc, out, err = self.cli("list", "--json")
        finally:
            sp_opencode.active_ids = original
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["opencode"][0]["state"], "unknown")


class OpenCodeSendTests(OpenCodeBase):
    def test_claude_sends_to_an_opencode_session_as_a_queued_turn(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "hello", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["target"], "opencode")
        self.assertEqual(payload["session_id"], OC_B)
        self.assertTrue(payload["reply_capable"])
        prompt = self.oc_calls("session.prompt")[0]
        self.assertEqual(prompt["params"], {"sessionID": OC_B})
        self.assertEqual(prompt["data"]["delivery"], "queue")
        self.assertFalse(prompt["data"]["resume"])
        meta = prompt["data"]["metadata"]["session_peers"]
        self.assertEqual(meta["message_id"], payload["message_id"])
        self.assertEqual(meta["from"], "cc:%s" % rec["sessionId"])
        lines = prompt["data"]["text"].splitlines()
        tag, _body = sp_protocol.parse_tag(prompt["data"]["text"])
        self.assertEqual(tag["mid"], payload["message_id"])
        self.assertEqual(tag["sid"], rec["sessionId"])
        self.assertIsNone(tag["reply"])
        self.assertEqual(lines[1], "[session-peers from Claude Code session cc-main (%s)]" % rec["sessionId"])
        self.assertIn("--to cc:%s" % rec["sessionId"], lines[2])
        self.assertEqual(lines[-1], "hello")

    def test_send_by_unique_title_and_refuses_an_ambiguous_one(self):
        rc, out, err = self.cli("send", "--to", "oc:oc-buddy", "--message", "x", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["session_id"], OC_B)
        self.set_oc(sessions=[oc_session(OC_A, "twin"), oc_session(OC_B, "twin")])
        rc, _out, err = self.cli("send", "--to", "oc:twin", "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn(OC_A, err)
        self.assertIn(OC_B, err)
        self.assertEqual(len(self.oc_calls("session.prompt")), 1)

    def test_explicit_wake_resumes_an_opencode_recipient(self):
        rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--wake", "--message", "x")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.oc_calls("session.prompt")[0]["data"]["resume"])

    def test_send_to_an_unknown_session_fails_without_queueing(self):
        rc, _out, err = self.cli("send", "--to", "oc:ses_zzzzzzzzzzzzzzzz", "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no OpenCode session", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_send_to_a_stopped_service_never_starts_it(self):
        self.set_oc(stopped=True)
        rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not running", err)
        self.assertEqual([c for c in self.oc_calls() if c["op"] != "service.status"], [])

    def test_opencode_sends_to_claude_with_an_explicit_reply_route(self):
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        self.as_opencode(OC_A)
        rc, out, err = self.cli("send", "--to", "cc:cc-main", "--message", "hi claude", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["from_opencode"], OC_A)
        self.assertFalse(payload["reply_capable"])
        wait_for(lambda: listener.of_type("user"))
        frame = listener.of_type("user")[0]
        # No socket exists for OpenCode, so the frame claims none.
        self.assertNotIn("from", frame)
        lines = frame["message"]["content"].splitlines()
        self.assertEqual(lines[0], "[session-peers from OpenCode session oc-lead (%s)]" % OC_A)
        self.assertIn("--to oc:%s" % OC_A, lines[1])
        self.assertEqual(lines[-1], "hi claude")

    def test_opencode_sends_to_codex_with_its_identity_in_the_tag(self):
        tid, _rollout = self.one_thread()
        self.as_opencode(OC_A)
        rc, _out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "hi codex")
        self.assertEqual(rc, 0, err)
        text = self.queue_calls()[0][-1]
        tag, body = sp_protocol.parse_tag(text)
        self.assertEqual(tag["sid"], OC_A)
        self.assertEqual(tag["from"], "oc-lead")
        self.assertIsNone(tag["reply"])
        self.assertTrue(body.startswith("[session-peers from OpenCode session oc-lead (%s)]" % OC_A))
        self.assertIn("--to oc:%s" % OC_A, body)

    def test_opencode_sender_beside_a_claude_identity_is_refused(self):
        self.add_listener(name="cc-main", session_id="s1")
        self.as_opencode(OC_A)
        os.environ["CLAUDE_CODE_SESSION_ID"] = new_uuid()
        rc, _out, err = self.cli("send", "--to", "cc:cc-main", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("pass explicit --from-* flags", err)

    def test_opencode_sender_the_service_cannot_confirm_is_refused(self):
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        self.as_opencode("ses_notaknownsession0000")
        rc, _out, err = self.cli("send", "--to", "cc:cc-main", "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("cannot verify OpenCode session", err)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])

    def test_a_malformed_opencode_id_is_no_identity(self):
        os.environ["OPENCODE_SESSION_ID"] = "ses_../../etc"
        self.assertIsNone(sp_opencode.current_session_id())

    def test_an_opencode_session_cannot_message_itself(self):
        self.as_opencode(OC_A)
        rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_A, "--message", "x")
        self.assertEqual(rc, 2)
        self.assertIn("cannot message itself", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_oversized_message_fails_before_exec(self):
        original = sp_runtime.argv_text_budget
        sp_runtime.argv_text_budget = lambda: 4096
        try:
            rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "x" * 5000)
        finally:
            sp_runtime.argv_text_budget = original
        self.assertEqual(rc, 1)
        self.assertIn("too large", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_wait_for_an_opencode_session_to_be_idle(self):
        rc, out, err = self.cli("wait", "--for", "oc:%s" % OC_B, "--state", "idle", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["status"], "idle")
        self.set_oc(active=[OC_B])
        rc, _out, _err = self.cli("wait", "--for", "oc:%s" % OC_B, "--state", "idle", "--timeout", "0.2")
        self.assertEqual(rc, 124)


class OpenCodeRequestTests(OpenCodeBase):
    def _reply_as(self, env, request_id, message):
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return self.cli("reply", "--request", request_id, "--message", message)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_opencode_dispatches_to_claude_and_awaits_the_reply(self):
        listener, _rec = self.add_listener(name="cc-main", session_id="s1")
        self.as_opencode(OC_A)
        rc, out, err = self.cli("dispatch", "--to", "cc:cc-main", "--message", "review?", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["requester"], "oc:%s" % OC_A)
        self.assertIsNone(payload["requester_thread_id"])
        wait_for(lambda: listener.of_type("user"))
        body = listener.of_type("user")[0]["message"]["content"]
        self.assertTrue(body.startswith("[session-peers from OpenCode session oc-lead"))
        self.assertIn('<session-peers-request id="%s"' % payload["request_id"], body)
        # A correlated request is answered through its mailbox, never `send`.
        self.assertNotIn("reply route", body)
        os.environ.pop("OPENCODE_SESSION_ID")
        rc, _out, err = self._reply_as({"CLAUDE_CODE_SESSION_ID": "s1"}, payload["request_id"], "LGTM")
        self.assertEqual(rc, 0, err)
        # Another OpenCode session may not consume it.
        self.as_opencode(OC_B)
        rc, _out, err = self.cli("await", "--request", payload["request_id"], "--timeout", "1")
        self.assertEqual(rc, 1)
        self.assertIn("was dispatched by oc:%s" % OC_A, err)
        self.as_opencode(OC_A)
        rc, out, err = self.cli("await", "--request", payload["request_id"], "--timeout", "2", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["message"], "LGTM")

    def test_claude_dispatches_to_opencode_and_only_that_session_replies(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        rc, out, err = self.cli("dispatch", "--to", "oc:%s" % OC_B, "--wake", "--message", "plan?", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        request_id = payload["request_id"]
        self.assertEqual(payload["target_session_id"], OC_B)
        prompt = self.oc_calls("session.prompt")[0]
        self.assertEqual(prompt["data"]["metadata"]["session_peers"]["request_id"], request_id)
        self.assertIn('<session-peers-request id="%s"' % request_id, prompt["data"]["text"])
        self.assertTrue(prompt["data"]["text"].startswith("[session-peers from Claude Code session cc-main"))
        # The Claude requester cannot answer its own request, nor can another
        # OpenCode session.
        rc, _out, err = self.cli("reply", "--request", request_id, "--message", "self")
        self.assertEqual(rc, 1)
        self.assertIn("OpenCode session", err)
        rc, _out, err = self._reply_as({"OPENCODE_SESSION_ID": OC_A}, request_id, "wrong")
        self.assertEqual(rc, 1)
        self.assertIn("belongs to OpenCode session %s" % OC_B, err)
        saved = {k: os.environ.pop(k) for k in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SESSION_ID")}
        rc, _out, err = self._reply_as({"OPENCODE_SESSION_ID": OC_B}, request_id, "the plan")
        self.assertEqual(rc, 0, err)
        os.environ.update(saved)
        rc, out, err = self.cli("await", "--request", request_id, "--timeout", "2", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["message"], "the plan")

    def test_claude_cannot_ask_claude_without_a_codex_or_opencode_origin(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        rc, _out, err = self.cli("ask", "--to", "cc:cc-main", "--message", "x", "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertIn("--from-thread or CODEX_THREAD_ID", err)

    def test_dispatch_to_opencode_delivery_failure_leaves_no_mailbox(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        self.set_oc(prompt_fail=True)
        rc, out, _err = self.cli("dispatch", "--to", "oc:%s" % OC_B, "--wake", "--message", "x", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["status"], "delivery_failed")
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])

    def test_legacy_codex_mailbox_still_awaits_by_thread(self):
        tid = new_uuid()
        request_id = new_uuid()
        sp_runtime.write_json_atomic(sp_storage.request_path(request_id), {
            "request_id": request_id, "requester_thread_id": tid,
            "target_session_id": "s1", "expires_at": time.time() + 60,
        })
        rc, _out, err = self.cli("await", "--request", request_id, "--from-thread", new_uuid(),
                                 "--timeout", "0.1")
        self.assertEqual(rc, 1)
        self.assertIn("was dispatched by thread %s" % tid, err)
        rc, _out, _err = self.cli("await", "--request", request_id, "--from-thread", tid,
                                  "--timeout", "0.1")
        self.assertEqual(rc, 124)

    def test_correlated_opencode_request_needs_buddy_or_explicit_wake(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        rc, _out, err = self.cli("dispatch", "--to", "oc:%s" % OC_B, "--message", "plan?")
        self.assertEqual(rc, 2)
        self.assertIn("--wake", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])


class OpenCodeBuddyTests(OpenCodeBase):
    def test_unknown_opencode_owner_cannot_bind_a_buddy(self):
        self.add_listener(name="cc-main", session_id=new_uuid())
        unknown = "ses_notaknownsession0000"
        self.as_opencode(unknown)
        rc, _out, err = self.cli("buddy", "set", "cc:cc-main", "--json")
        self.assertEqual(rc, 2)
        self.assertIn("cannot verify OpenCode session", err)
        self.assertFalse(pathlib.Path(sp_storage.buddy_path({"kind": "oc", "uuid": unknown})).exists())

    def test_opencode_session_binds_a_claude_buddy_without_as(self):
        self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_opencode(OC_A)
        rc, out, err = self.cli("buddy", "set", "cc:cc-main", "--json")
        self.assertEqual(rc, 0, err)
        rec = json.loads(out)
        self.assertEqual(rec["owner"], {"kind": "oc", "uuid": OC_A})
        self.assertEqual(rec["buddy"]["kind"], "cc")
        rc, out, err = self.cli("buddy")
        self.assertEqual(rc, 0, err)
        self.assertIn("cc-main", out)

    def test_claude_binds_an_opencode_buddy_with_unknown_liveness(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.as_claude(rec)
        rc, out, err = self.cli("buddy", "set", "oc:oc-buddy", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["buddy"], {"kind": "oc", "uuid": OC_B, "name": "oc-buddy"})
        self.assertIsNone(payload["status"]["live"])
        self.assertEqual(payload["status"]["route"], "available")
        self.assertEqual(payload["status"]["status"], "idle")
        # No reply total: OpenCode has no shim that would spend one.
        self.assertNotIn("replies", payload)
        rc, out, err = self.cli("send", "--to", "buddy", "--message", "hi", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["session_id"], OC_B)
        self.assertTrue(self.oc_calls("session.prompt")[0]["data"]["resume"])

    def test_bare_name_shared_by_claude_and_opencode_is_ambiguous(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.add_listener(name="oc-buddy", session_id=new_uuid(), pid=os.getppid())
        self.as_claude(rec)
        rc, _out, err = self.cli("buddy", "set", "oc-buddy")
        self.assertEqual(rc, 1)
        self.assertIn("oc:oc-buddy", err)
        self.assertIn("cc:oc-buddy", err)

    def test_bare_name_resolves_to_opencode_when_unique(self):
        _listener, rec = self.add_listener(name="cc-main", session_id=new_uuid())
        self.make_state_db([])  # Codex discovery must answer "no such name"
        self.as_claude(rec)
        rc, out, err = self.cli("buddy", "set", "@oc-buddy", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["buddy"]["uuid"], OC_B)

    def test_buddy_of_a_deleted_opencode_session_is_reported_gone(self):
        self.as_opencode(OC_A)
        rc, _out, err = self.cli("buddy", "set", "oc:%s" % OC_B)
        self.assertEqual(rc, 0, err)
        self.set_oc(sessions=[oc_session(OC_A, "oc-lead")])
        rc, out, err = self.cli("buddy", "--json")
        self.assertEqual(rc, 0, err)
        status = json.loads(out)["status"]
        self.assertIs(status["live"], False)
        self.assertIn("no OpenCode session", status["route"])

    def test_gc_prunes_an_opencode_owner_only_when_the_service_denies_it(self):
        self.as_opencode(OC_B)
        rc, _out, err = self.cli("buddy", "set", "oc:%s" % OC_A)
        self.assertEqual(rc, 0, err)
        os.environ.pop("OPENCODE_SESSION_ID")
        path = pathlib.Path(sp_storage.buddy_path({"kind": "oc", "uuid": OC_B}))
        old = time.time() - 30 * 86400
        os.utime(str(path), (old, old))
        self.set_oc(stopped=True)
        self.assertEqual(sp_maintenance.gc_buddy_records(days=1, verbose=False), [])
        self.set_oc(stopped=False)
        self.assertEqual(sp_maintenance.gc_buddy_records(days=1, verbose=False), [])
        self.set_oc(sessions=[oc_session(OC_A, "oc-lead")])
        self.assertEqual(sp_maintenance.gc_buddy_records(days=1, verbose=False), [path.name])

    def test_parse_typed_accepts_opencode_ids_and_rejects_others(self):
        from session_peers import identity as sp_identity
        self.assertEqual(sp_identity.parse_typed("oc:%s" % OC_A), {"kind": "oc", "uuid": OC_A})
        for bad in ("oc:%s" % new_uuid(), "oc:ses_x/y", "codex:%s" % OC_A):
            with self.assertRaises(ValueError):
                sp_identity.parse_typed(bad)
