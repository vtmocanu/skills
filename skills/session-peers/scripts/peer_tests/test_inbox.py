"""Own-session queued peer reads and explicit local deduplication."""

from peer_tests.support import *  # noqa: F401,F403
from session_peers import inbox

SID = "ses_aaaaaaaaaaaaAAAAAAAAAAAAAA"
OTHER = "ses_bbbbbbbbbbbbBBBBBBBBBBBBBB"


class OpenCodeInboxTests(Base):
    def setUp(self):
        super().setUp()
        self.sender = "cc:" + new_uuid()
        self.mid = new_uuid()

    def item(self, **meta):
        return {"id": "msg_inbox", "sessionID": SID, "type": "user",
                "time": {"created": time.time() * 1000}, "delivery": "queue",
                "payload": {"text": "peer body", "metadata": {"session_peers": {
                    "from": self.sender, "message_id": self.mid, **meta}}}}

    def listing(self, items, **kwargs):
        with mock.patch.object(sp_opencode, "api", return_value={"data": items}) as api:
            result = inbox.list_items(SID, **kwargs)
        api.assert_called_once_with("session.inbox.list", {"sessionID": SID})
        return result

    def test_list_is_nonconsuming_and_write_free(self):
        row = self.listing([self.item()])[0]
        self.assertEqual(row["text"], "peer body")
        self.assertEqual(row["from"], self.sender)
        self.assertFalse(pathlib.Path(inbox.ledger_path(SID)).parent.exists())
        self.assertEqual(len(self.listing([self.item()])), 1)

    def test_explicit_handled_pair_and_all(self):
        inbox.mark_handled(SID, self.sender, self.mid)
        inbox.mark_handled(SID, self.sender, self.mid)
        self.assertEqual(self.listing([self.item()]), [])
        self.assertTrue(self.listing([self.item()], include_handled=True)[0]["handled"])
        forged = self.item(**{"from": "cc:" + new_uuid()})
        self.assertEqual(len(self.listing([forged])), 1)
        self.assertFalse(inbox.is_handled(OTHER, self.sender, self.mid))

    def test_unverified_metadata_never_hidden(self):
        for values in ({"from": "bad"}, {"message_id": "../bad"}, {"from": None}):
            self.assertFalse(self.listing([self.item(**values)])[0]["verified_sender"])
        with self.assertRaises(ValueError):
            inbox.mark_handled(SID, "bad", self.mid)

    def test_nonpeer_excluded_and_foreign_session_refused(self):
        item = self.item()
        item["payload"]["metadata"] = {}
        self.assertEqual(self.listing([item]), [])
        item["sessionID"] = OTHER
        with self.assertRaises(ValueError):
            self.listing([item])

    def test_malformed_response_and_transport_fail_closed(self):
        for data in (None, {}, [None]):
            with mock.patch.object(sp_opencode, "api", return_value={"data": data}):
                with self.assertRaises(ValueError):
                    inbox.list_items(SID)
        with mock.patch.object(sp_opencode, "api", side_effect=sp_opencode.OpenCodeError("offline")):
            with self.assertRaises(sp_opencode.OpenCodeError):
                inbox.list_items(SID)

    def test_corrupt_ledger_is_error_not_empty(self):
        inbox.mark_handled(SID, self.sender, self.mid)
        for text in ("broken", "null", "{}"):
            pathlib.Path(inbox.ledger_path(SID)).write_text(text)
            with self.assertRaises(ValueError):
                self.listing([self.item()])

    def test_ttl_pruned_only_on_write_and_private_modes(self):
        with mock.patch.object(inbox.time, "time", return_value=1):
            inbox.mark_handled(SID, self.sender, self.mid)
        path = pathlib.Path(inbox.ledger_path(SID))
        before = path.read_bytes()
        self.assertFalse(inbox.is_handled(SID, self.sender, self.mid))
        self.assertEqual(path.read_bytes(), before)
        inbox.mark_handled(SID, self.sender, new_uuid())
        self.assertNotIn(self.mid, path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_human_text_sanitized_but_json_raw(self):
        text = "\x1b[31mred\x1b[0m\roverwrite\x1b]0;title\x07"
        safe = inbox.human_text(text)
        self.assertNotIn("\x1b", safe)
        self.assertNotIn("\r", safe)
        self.assertNotIn("title", safe)
        self.assertLess(len(inbox.human_text("x" * 9000)), 5000)
        item = self.item()
        item["payload"]["text"] = text
        self.assertEqual(self.listing([item])[0]["text"], text)

    def test_request_states_read_only_and_absent_unknown(self):
        rid = new_uuid()
        base = pathlib.Path(sp_storage.codex_home()) / "session-peers" / "requests"
        base.mkdir(parents=True)
        request = base / (rid + ".request.json")
        request.write_text(json.dumps({"request_id": rid, "target_session_id": SID,
                                       "target_kind": "oc", "expires_at": time.time() + 60}))
        self.assertEqual(self.listing([self.item(request_id=rid)])[0]["request_state"], "pending")
        reply = base / (rid + ".reply.json")
        reply.write_text(json.dumps({"request_id": rid, "session_id": SID, "message": "done"}))
        self.assertEqual(self.listing([self.item(request_id=rid)])[0]["request_state"], "replied")
        reply.unlink()
        request.write_text(json.dumps({"request_id": rid, "target_session_id": SID,
                                       "target_kind": "oc", "expires_at": 1}))
        self.assertEqual(self.listing([self.item(request_id=rid)])[0]["request_state"], "expired")
        self.assertTrue(request.exists())
        request.unlink()
        self.assertEqual(self.listing([self.item(request_id=rid)])[0]["request_state"], "unknown")

    def test_own_identity_required_and_no_target_override(self):
        from session_peers import cli
        with mock.patch.object(inbox.identity, "caller_identity", return_value={"kind": "codex", "uuid": new_uuid()}):
            with self.assertRaises(ValueError):
                inbox.own_session(argparse.Namespace())
        with mock.patch.object(inbox.identity, "caller_identity", return_value={"kind": "oc", "uuid": SID}):
            with mock.patch.object(sp_opencode, "current_session_id", return_value=None):
                with self.assertRaises(ValueError):
                    inbox.own_session(argparse.Namespace())
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["inbox", "list", "--to", "oc:" + OTHER])

    def test_cli_dispatch_list_status_and_handled(self):
        from session_peers import cli
        caller = {"kind": "oc", "uuid": SID}
        with mock.patch.object(inbox.identity, "caller_identity", return_value=caller):
            with mock.patch.object(sp_opencode, "current_session_id", return_value=SID):
                with mock.patch.object(sp_opencode, "api", return_value={"data": [self.item()]}):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main(["inbox", "list", "--json"]), 0)
                    self.assertEqual(json.loads(output.getvalue())[0]["message_id"], self.mid)
                for command in ("handled", "status"):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main(["inbox", command, self.sender, self.mid]), 0)
                    self.assertTrue(json.loads(output.getvalue())["handled"])

    def test_request_ambiguity_and_bad_metadata_are_errors(self):
        rid = new_uuid()
        homes = [self.root / "home-one", self.root / "home-two"]
        for home in homes:
            base = home / "session-peers" / "requests"
            base.mkdir(parents=True)
            (base / (rid + ".request.json")).write_text("null")
        with mock.patch.object(sp_storage, "candidate_homes", return_value=homes):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                inbox.request_state(rid, SID)
        with mock.patch.object(sp_storage, "candidate_homes", return_value=homes[:1]):
            with self.assertRaisesRegex(ValueError, "invalid"):
                inbox.request_state(rid, SID)
