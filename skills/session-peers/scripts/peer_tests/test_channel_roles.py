"""Public-channel admission and fan-out follow live bindings, not runtime names."""

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import ChannelSendBase
from peer_tests.test_channel_general import GeneralBase
from peer_tests.test_web_chat import ChatBase
from session_peers import channels, channel_roles


def bind(owner, buddy):
    kind, _, ident = owner.partition(":")
    target_kind, _, target_id = buddy.partition(":")
    path = sp_storage.buddy_path({"kind": kind, "uuid": ident})
    sp_runtime.write_json_atomic(path, {"owner": {"kind": kind, "uuid": ident},
                                      "buddy": {"kind": target_kind, "uuid": target_id}}, mode=0o600)
    return pathlib.Path(path)


class ChannelRoleTests(ChannelSendBase):
    def setUp(self):
        super().setUp()
        self.sender_cc()

    def public(self, name, *members, **kwargs):
        return channels.create_channel(name, self.me, members, **kwargs)

    def test_more_than_a_thousand_unrelated_bindings_do_not_hide_a_lead(self):
        directory = pathlib.Path(sp_storage.buddies_dir())
        unrelated = new_uuid()
        for _ in range(1100):
            owner = new_uuid()
            (directory / ("cc-" + owner + ".json")).write_text(json.dumps({
                "owner": {"kind": "cc", "uuid": owner}, "buddy": {"kind": "cc", "uuid": unrelated}}))
        self.assertEqual(channel_roles.roles([self.me])[self.me], "lead")

    def test_nonbinding_noise_and_legacy_shim_home_records_do_not_poison_roles(self):
        (self.sessions / "noise.json").write_text("{broken")
        (self.sessions / "legacy-shim.json").write_text(json.dumps({"entrypoint": "codex"}))
        directory = pathlib.Path(sp_storage.buddies_dir())
        (directory / "noise.json").write_text("[]")
        (directory / "large-noise.json").write_text("x" * 70000)
        os.mkfifo(directory / "noise-fifo.json")
        self.assertEqual(channel_roles.roles([self.me])[self.me], "lead")

    def test_malformed_bindings_still_consume_the_byte_budget(self):
        directory = pathlib.Path(sp_storage.buddies_dir())
        for _ in range(10):
            (directory / ("cc-" + new_uuid() + ".json")).write_text("{" + "x" * 180)
        empty = self.root / "empty-registry"
        empty.mkdir()
        total, pread = [0], os.pread
        def read(*args):
            data = pread(*args)
            total[0] += len(data)
            return data
        with mock.patch.object(channel_roles, "MAX_BYTES", 512), \
                mock.patch.object(sp_storage, "claude_sessions_dir", return_value=str(empty)), \
                mock.patch.object(os, "pread", side_effect=read):
            self.assertEqual(channel_roles.roles([self.me], known_owner_states={})[self.me], "unverified")
        self.assertLessEqual(total[0], 513)

    def test_buddy_join_refuses_until_explicit_override_and_leave_clears_it(self):
        _listener, buddy = self.peer_cc("buddy")
        self.public("ops")
        bind(self.me, buddy)
        self.as_cc(buddy.partition(":")[2])
        rc, _out, err = self.ch("join", "ops")
        self.assertEqual(rc, 2)
        self.assertIn("--allow-buddy", err)
        self.assertNotIn(buddy, channels.read_channel("ops")["members"])
        self.ok("join", "ops", "--allow-buddy")
        self.assertIn(buddy, channels.read_channel("ops")["allow_buddy"])
        self.ok("leave", "ops")
        self.assertNotIn(buddy, channels.read_channel("ops")["allow_buddy"])

    def test_create_refuses_buddy_members_unless_explicitly_allowed(self):
        _listener, buddy = self.peer_cc("buddy")
        bind(self.me, buddy)
        rc, _out, err = self.ch("create", "ops", "--member", buddy)
        self.assertEqual(rc, 2)
        self.assertIn("--allow-buddy", err)
        self.assertIsNone(channels.read_channel("ops"))
        self.ok("create", "ops", "--member", buddy, "--allow-buddy")
        self.assertIn(buddy, channels.read_channel("ops")["allow_buddy"])

    def test_solo_codex_is_a_lead_and_can_join(self):
        self.public("ops")
        tid, _rollout = self.one_thread()
        actor = self.as_codex(tid)
        bind(actor, self.me)  # owning a binding does not make its owner the buddy
        self.ok("join", "ops")
        self.assertIn(actor, channels.read_channel("ops")["members"])

    def test_here_all_and_all_mode_skip_a_member_that_became_a_buddy(self):
        listener, buddy = self.peer_cc("buddy")
        _other, keeper = self.peer_cc("keeper")
        for key in ("here", "broadcast", "allmode"):
            self.public(key, buddy, keeper, deliver="all" if key == "allmode" else "mentions")
        bind(self.me, buddy)
        for key, body in (("here", "@here review"), ("broadcast", "@all review"), ("allmode", "note")):
            rc, result, err = self.send(name=key, message=body)
            self.assertEqual(rc, 0, err)
            self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_BUDDY)
            self.assertEqual(result["outcomes"][keeper], channels.OUTCOME_ACCEPTED)
        self.assertEqual(listener.of_type("user"), [])

    def test_direct_name_reaches_buddy_even_when_another_binding_is_unreadable(self):
        self.one_thread()
        listener, buddy = self.peer_cc("reviewer")
        self.public("ops", buddy)
        path = bind(self.me, buddy)
        path.write_text("{")
        self.assertEqual(channel_roles.roles([buddy])[buddy], "unverified")
        rc, result, err = self.send(message="@reviewer please review")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_ACCEPTED)
        self.assertEqual(len(listener.of_type("user")), 1)

    def test_override_permits_fanout_and_buddy_channel_is_unchanged(self):
        listener, buddy = self.peer_cc("buddy")
        bind(self.me, buddy)
        self.public("ops", buddy, deliver="all", allow_buddy=True)
        rc, result, err = self.send(message="note")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_ACCEPTED)
        rec = channels.create_channel("buddy-test", self.me, [buddy], kind="buddy", reserved_ok=True)
        rc, result, err = self.send(name=rec["key"], message="buddy note")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_ACCEPTED)
        self.assertEqual(len(listener.of_type("user")), 2)

    def test_binding_is_rechecked_after_history_before_transport(self):
        listener, buddy = self.peer_cc("buddy")
        _other, keeper = self.peer_cc("keeper")
        self.public("ops", buddy, keeper)
        def after_history(point):
            if point == "send_after_history":
                bind(self.me, buddy)
        with mock.patch.object(channels, "_failpoint", side_effect=after_history):
            rc, result, err = self.send(message="@all review")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_BUDDY)
        self.assertEqual(listener.of_type("user"), [])
        rows = self.view_of()["recipients"]
        self.assertEqual(next(row for row in rows if row["recipient"] == buddy)["ack"], "not requested")

    def test_unreadable_binding_fails_closed_and_verified_gone_owner_does_not_block(self):
        listener, peer = self.peer_cc("peer")
        self.public("ops", peer, deliver="all")
        ghost = "cc:" + new_uuid()
        path = bind(ghost, peer)
        self.assertEqual(channel_roles.roles([peer])[peer], "lead")
        path.unlink()
        path = bind(self.me, peer)
        path.write_text("{")
        rc, result, err = self.send(message="note")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][peer], channels.OUTCOME_ROLE_UNKNOWN)
        self.assertEqual(listener.of_type("user"), [])
        with mock.patch("sys.stderr", new=io.StringIO()) as output:
            self.public("other", peer)
        self.assertIn("warning:", output.getvalue())
        path.unlink()
        rc, result, err = self.send(message="later note")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][peer], channels.OUTCOME_ACCEPTED)

    def test_binding_in_another_shim_home_is_found_and_fifo_does_not_block(self):
        _listener, peer = self.peer_cc("peer")
        other = self.root / "other-home"
        shim_path = self.socks / "home-record.json"
        sp_runtime.write_json_atomic(str(shim_path), dict(self.sender_rec, entrypoint="codex", codexHome=str(other), sessionId=new_uuid()))
        # The actual registry may be elsewhere than the socket directory.
        registry_path = pathlib.Path(sp_storage.claude_sessions_dir()) / "other-home.json"
        shim_path.replace(registry_path)
        directory = other / "session-peers" / "buddies"
        directory.mkdir(parents=True)
        owner_kind, _, owner_id = self.me.partition(":")
        (directory / (owner_kind + "-" + owner_id + ".json")).write_text(json.dumps({"owner": {"kind": owner_kind, "uuid": owner_id},
                                                          "buddy": {"kind": "cc", "uuid": peer.partition(":")[2]}}))
        self.assertEqual(channel_roles.roles([peer])[peer], "buddy")
        owner2 = "cc:" + new_uuid()
        os.mkfifo(directory / (owner2.replace(":", "-", 1) + ".json"))
        with mock.patch.object(channels, "member_liveness", return_value="unverified"):
            other = "cc:" + new_uuid()
            result = channel_roles.roles([peer, other])
            self.assertEqual(result[peer], "buddy")
            self.assertEqual(result[other], "unverified")

    def test_binding_filename_owns_record_and_uuid_case_does_not_hide_a_buddy(self):
        _listener, peer = self.peer_cc("peer")
        path = bind(self.me, peer.upper().replace("CC:", "cc:"))
        self.assertEqual(channel_roles.roles([peer])[peer], "buddy")
        value = json.loads(path.read_text())
        value["owner"]["uuid"] = new_uuid()  # not the owner whose binding file this is
        path.write_text(json.dumps(value))
        self.assertEqual(channel_roles.roles([peer])[peer], "buddy")

    def test_unknown_admission_warns_but_does_not_grant_fanout_override(self):
        _listener, peer = self.peer_cc("peer")
        self.public("ops")
        path = bind(self.me, peer)
        path.write_text("{")
        self.as_cc(peer.partition(":")[2])
        rc, _out, err = self.ch("join", "ops")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning:", err)
        self.assertNotIn(peer, channels.read_channel("ops")["allow_buddy"])

    def test_public_handover_refuses_a_buddy_target_without_mutating_membership(self):
        _listener, peer = self.peer_cc("buddy")
        self.public("ops")
        bind(self.me, peer)
        before = channels.read_channel("ops")
        with self.assertRaises(channels.ChannelError):
            channels.change_membership("ops", channels._handover_mutator(self.me, peer, "buddy"), self.me)
        self.assertEqual(channels.read_channel("ops"), before)

    def test_a_buddy_that_also_owns_a_binding_is_still_skipped(self):
        listener, peer = self.peer_cc("both")
        _leaf, leaf = self.peer_cc("leaf")
        self.public("ops", peer, deliver="all")
        bind(self.me, peer)
        bind(peer, leaf)
        rc, result, err = self.send(message="note")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][peer], channels.OUTCOME_BUDDY)
        self.assertEqual(listener.of_type("user"), [])

    def test_unverified_binding_owner_does_not_become_verified_gone(self):
        _listener, peer = self.peer_cc("peer")
        bind(self.me, peer)
        with mock.patch.object(channels, "member_liveness", return_value="unverified"):
            self.assertEqual(channel_roles.roles([peer])[peer], "buddy")


class GeneralChannelRoleTests(GeneralBase):
    def test_general_roster_stays_derived_but_broadcast_skips_buddy_and_name_reaches_it(self):
        buddy = "codex:" + self.tid
        bind(self.me, buddy)
        self.assertIn(buddy, [row["member"] for row in self.general_members()["members"]])
        rc, result, err = self.post("plain history")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"], {})
        self.nothing_delivered()
        rc, result, err = self.post("@all review")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_BUDDY)
        self.assertEqual(self.queue_calls(), [])
        rc, result, err = self.post("@" + buddy + " review directly")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_ACCEPTED)
        self.assertEqual(len(self.queue_calls()), 1)
        self.as_codex(self.tid)
        self.ok("leave", "general")
        self.ok("join", "general")
        self.ok("join", "general", "--allow-buddy")  # marker-only; never grants a fan-out override
        self.as_sender()
        self.wall[0] += 61
        rc, result, err = self.post("@all still skips buddies")
        self.assertEqual(rc, 0, err)
        self.assertEqual(result["outcomes"][buddy], channels.OUTCOME_BUDDY)
        self.assertEqual(len(self.queue_calls()), 1)


class BrowserChannelRoleTests(ChatBase):
    def public_ops(self):
        listener, owner, codex = self.ops_with_claude()
        def clear_overrides(rec):
            rec["allow_buddy"] = []
            return None, None
        channels.change_membership("ops", clear_overrides, owner)
        found = {"general": ([owner, codex], None, []), "liveness": {owner: "live", codex: "live"}}
        self.server.discovery.get = lambda **_kw: found
        return listener, owner, codex

    def test_preview_skips_buddy_without_resolving_handler_codex_home(self):
        _listener, owner, codex = self.public_ops()
        bind(owner, codex)
        with mock.patch.object(sp_storage, "codex_home", side_effect=AssertionError("handler resolved CODEX_HOME")):
            preview = self.post("#ops", "@all review")
        self.assertEqual(preview.status, 200, preview.body)
        self.assertEqual(preview.json()["recipients"], [owner])
        self.assertEqual(preview.json()["excluded"], [{"member": codex, "reason": channels.OUTCOME_BUDDY}])
        self.assertEqual(self.journal_entries(), [])

    def test_binding_change_after_preview_refuses_before_journal_or_transport(self):
        listener, owner, codex = self.public_ops()
        preview = self.post("#ops", "@all review")
        self.assertEqual(preview.status, 200, preview.body)
        bind(owner, codex)
        fields = {"to": "#ops", "text": "@all review", "ack": True, "confirm": preview.json()["confirm"]}
        accepted = self.raw_post(json.dumps(fields).encode())
        self.assertEqual(accepted.status, 202)
        row = self.wait_row(accepted.json()["message_id"])
        self.assertEqual(row["state"], "not sent")
        self.assertIn("recipients changed", row["reason"])
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.send_entries(), [])
        self.assertEqual(listener.of_type("user"), [])
