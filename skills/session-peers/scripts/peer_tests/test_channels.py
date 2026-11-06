"""Channel store, membership commands, history gaps and member liveness."""

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_opencode import OC_A, OC_B, OC_CHILD, OpenCodeBase, oc_session
from session_peers import channels as sp_channels


class ChannelBase(OpenCodeBase):
    def setUp(self):
        super().setUp()
        self.me = self.as_cc()

    def as_cc(self, sid=None):
        """Make this process Claude session ``sid``; returns its typed id."""
        for name in ("CODEX_THREAD_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(name, None)
        sid = sid or new_uuid()
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        return "cc:" + sid

    def as_codex(self, tid=None):
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        tid = tid or new_uuid()
        os.environ["CODEX_THREAD_ID"] = tid
        return "codex:" + tid

    def ch(self, *args):
        return self.cli("channel", *args)

    def ok(self, *args):
        rc, out, err = self.ch(*args)
        self.assertEqual(rc, 0, err)
        return out

    def tail_json(self, name, *extra):
        rc, out, err = self.ch("tail", name, "--json", *extra)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def kinds(self, name):
        return [e["kind"] for e in self.tail_json(name)["entries"]]


class ChannelCommandTests(ChannelBase):
    def test_create_join_leave_remove_list_members_tail(self):
        codex_tid, _rollout = self.one_thread()
        self.ok("create", "ops", "--member", "codex:" + codex_tid, "--member", "oc:" + OC_B)
        rec = sp_channels.read_channel("ops")
        self.assertEqual(rec["owner"], self.me)
        self.assertEqual(rec["members"], [self.me, "codex:" + codex_tid, "oc:" + OC_B])
        self.assertEqual(rec["kind"], "named")

        joiner = self.as_cc()
        self.ok("join", "ops")
        self.assertEqual(self.ok("join", "#ops").strip(), "already a member of #ops")
        self.ok("leave", "ops")
        self.assertEqual(self.ok("leave", "ops").strip(), "not a member of #ops")
        self.ok("join", "ops")

        self.as_cc(self.me.partition(":")[2])
        self.ok("remove", "ops", "oc:" + OC_B)
        self.assertNotIn("oc:" + OC_B, sp_channels.read_channel("ops")["members"])
        self.assertEqual(
            self.kinds("ops"),
            ["channel.created", "channel.joined", "channel.left", "channel.joined", "channel.removed"],
        )

        listing = json.loads(self.ok("list", "--json"))
        self.assertEqual(
            listing,
            [{"name": "ops", "display": "ops", "kind": "named", "owner": self.me, "members": 3, "you": "owner"}],
        )
        self.assertIn("#ops  named  3 members  owner", self.ok("list"))
        members = json.loads(self.ok("members", "ops", "--json"))["members"]
        self.assertEqual([m["member"] for m in members], [self.me, "codex:" + codex_tid, joiner])
        self.assertEqual(members[0]["owner"], True)
        tail = self.tail_json("ops", "--since", "3")
        self.assertEqual([e["seq"] for e in tail["entries"]], [4, 5])
        self.assertEqual(tail["entries"][1]["data"], {"member": "oc:" + OC_B})
        self.assertEqual(tail["next"], 5)
        self.assertIn("removed", self.ok("tail", "ops", "--since", "4"))

    def test_remove_is_owner_only_and_needs_a_member(self):
        self.ok("create", "ops")
        other = self.as_cc()
        self.ok("join", "ops")
        rc, _out, err = self.ch("remove", "ops", self.me)
        self.assertEqual(rc, 2)
        self.assertIn("only the owner", err)
        self.assertEqual(len(sp_channels.read_channel("ops")["members"]), 2)
        self.as_cc(self.me.partition(":")[2])
        rc, _out, err = self.ch("remove", "ops", "cc:" + new_uuid())
        self.assertEqual(rc, 2)
        self.assertIn("is not a member", err)
        self.ok("remove", "ops", other)
        self.assertEqual(sp_channels.read_channel("ops")["members"], [self.me])

    def test_names_and_reserved_names_are_refused_with_a_reason(self):
        for bad in ("Bad", "a b", "x" * 65, "snow_man", ""):
            rc, _out, err = self.ch("create", bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("1-64 characters", err)
        for reserved in ("general", "buddy-abc"):
            rc, _out, err = self.ch("create", reserved)
            self.assertEqual(rc, 2, reserved)
            self.assertIn("reserved", err)
        self.ok("create", "x" * 64)
        self.ok("create", "ops")
        rc, _out, err = self.ch("create", "ops")
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        rc, _out, err = self.ch("tail", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no channel named #nope", err)

    def test_general_is_not_a_stored_channel(self):
        for verb in ("join", "leave", "tail", "members"):
            rc, _out, err = self.ch(verb, "general")
            self.assertEqual(rc, 2, verb)
            self.assertIn("#general is derived", err)

    def test_member_and_channel_caps_name_their_limit(self):
        listener, rec = self.add_listener(name="friend", session_id=new_uuid())
        tid, _rollout = self.one_thread()
        with mock.patch.object(sp_constants, "CHANNEL_MAX_MEMBERS", 2):
            rc, _out, err = self.ch("create", "big", "--member", "cc:" + rec["sessionId"], "--member", "codex:" + tid)
            self.assertEqual(rc, 2)
            self.assertIn("at most 2 members", err)
            self.assertIsNone(sp_channels.read_channel("big"))
        with mock.patch.object(sp_constants, "CHANNEL_MAX_MEMBERS", 2):
            self.ok("create", "pair", "--member", "cc:" + rec["sessionId"])
            self.as_cc()
            rc, _out, err = self.ch("join", "pair")
            self.assertEqual(rc, 2)
            self.assertIn("at most 2 members", err)
        with mock.patch.object(sp_constants, "CHANNEL_MAX_CHANNELS", 2):
            self.as_cc()
            self.ok("create", "one")
            rc, _out, err = self.ch("create", "three")
            self.assertEqual(rc, 2)
            self.assertIn("at most 2 channels", err)
            self.assertIsNone(sp_channels.read_channel("three"))

    def test_spec_limits(self):
        self.assertEqual(
            (sp_constants.CHANNEL_MAX_MEMBERS, sp_constants.CHANNEL_MAX_CHANNELS,
             sp_constants.CHANNEL_TMP_MAX_AGE),
            (64, 256, 3600.0),
        )

    def test_a_channel_named_channels_does_not_alias_the_store_lock(self):
        done = []

        def create():
            sp_channels.create_channel("channels", self.me)
            done.append(True)

        worker = threading.Thread(target=create, daemon=True)
        worker.start()
        worker.join(10)
        self.assertEqual(done, [True], "create_channel('channels') blocked on its own lock")
        self.assertEqual(sp_channels.read_channel("channels")["owner"], self.me)
        self.ok("create", "after")

    def test_shim_member_is_stored_as_codex_and_removable_by_its_cc_spelling(self):
        tid = new_uuid()
        self.write_record(os.getppid(), "shimmy", tid, str(self.socks / "shim.sock"), entrypoint="codex")
        self.ok("create", "mix", "--member", "cc:" + tid)
        self.assertEqual(sp_channels.read_channel("mix")["members"], [self.me, "codex:" + tid])
        self.assertIn("removed codex:" + tid, self.ok("remove", "mix", "cc:" + tid))
        self.assertEqual(sp_channels.read_channel("mix")["members"], [self.me])

    def test_ambiguous_member_is_refused_with_candidate_lines(self):
        a, b = new_uuid(), new_uuid()
        # Two live Codex threads with one title make the name ambiguous.
        self.make_state_db([
            {"id": a, "name": "twin", "rollout_path": str(self.make_rollout("a.jsonl"))},
            {"id": b, "name": "twin", "rollout_path": str(self.make_rollout("b.jsonl"))},
        ])
        for name in ("a.jsonl", "b.jsonl"):
            self.set_holder(self.codex_dir / name)
        rc, _out, err = self.ch("create", "ops", "--member", "codex:twin")
        self.assertEqual(rc, 2)
        self.assertIn("pick one typed ID:", err)
        self.assertIn("codex:%s" % b, err)
        self.assertIsNone(sp_channels.read_channel("ops"))

    def test_buddy_channels_are_joined_through_buddy_set(self):
        owner = "cc:" + new_uuid()
        sp_channels.create_channel(
            "buddy-abc123", owner, kind="buddy", display="buddy", reserved_ok=True
        )
        rc, _out, err = self.ch("join", "buddy-abc123")
        self.assertEqual(rc, 2)
        self.assertIn("buddy channels are joined through buddy set", err)
        self.assertEqual(sp_channels.read_channel("buddy-abc123")["members"], [owner])

    def test_mixed_runtime_environment_is_refused(self):
        os.environ["CODEX_THREAD_ID"] = new_uuid()
        for args in (("list",), ("create", "ops"), ("tail", "ops")):
            rc, _out, err = self.ch(*args)
            self.assertEqual(rc, 2)
            self.assertIn("more than one session runtime", err)
        self.assertEqual(sp_channels.list_keys(), [])

    def test_storage_modes_are_private(self):
        self.ok("create", "ops")
        root = pathlib.Path(sp_channels.channels_root())
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / "history").stat().st_mode), 0o700)
        for name in ("ops.json", "ops.lock", ".channels.lock"):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600, name)

    def test_human_output_escapes_peer_controlled_text(self):
        self.ok("create", "ops")
        sp_channels.append_history("ops", "channel.note", self.me, {"body": "line1\nline2\x1b[31m red\x07"})
        out = self.ok("tail", "ops")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        entry_lines = [l for l in out.splitlines() if l.startswith("#2 ")]
        self.assertEqual(len(entry_lines), 1)
        self.assertIn("line1 line2", entry_lines[0])


class ChannelHistoryTests(ChannelBase):
    def test_membership_is_written_before_history(self):
        self.ok("create", "ops")
        before = sp_channels.read_channel("ops")
        self.as_cc()

        def boom(name):
            raise RuntimeError("crash at " + name)

        with mock.patch.object(sp_channels, "_failpoint", boom):
            with self.assertRaisesRegex(RuntimeError, "after_membership_before_history"):
                self.ch("join", "ops")
        after = sp_channels.read_channel("ops")
        self.assertEqual(len(after["members"]), len(before["members"]) + 1)
        self.assertEqual(self.kinds("ops"), ["channel.created"])

    def test_create_writes_membership_before_history(self):
        with mock.patch.object(sp_channels, "_failpoint", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.ch("create", "ops")
        self.assertEqual(sp_channels.read_channel("ops")["members"], [self.me])
        self.assertEqual(sp_channels.read_history("ops")["entries"], [])

    def test_failed_atomic_write_leaves_the_old_record(self):
        self.ok("create", "ops")
        path = pathlib.Path(sp_channels.channels_root()) / "ops.json"
        before = path.read_bytes()
        self.as_cc()
        with mock.patch.object(os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self.ch("join", "ops")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.kinds("ops"), ["channel.created"])

    def test_stale_temp_files_are_removed_under_the_lock_and_by_gc(self):
        self.ok("create", "ops")
        root = pathlib.Path(sp_channels.channels_root())
        stale, fresh, stale2 = (root / n for n in ("ops.json.tmp.1", "ops.json.tmp.2", "other.json.tmp.9"))
        for p in (stale, fresh, stale2):
            p.write_text("{}")
        old = time.time() - 2 * 3600
        os.utime(str(stale), (old, old))
        os.utime(str(stale2), (old, old))
        self.as_cc()
        self.ok("join", "ops")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(stale2.exists())  # another channel's file: not this lock's to touch
        self.assertEqual(self.cli("gc")[0], 0)
        self.assertFalse(stale2.exists())
        self.assertTrue(fresh.exists())

    def test_gc_prunes_channel_history(self):
        self.ok("create", "ops")
        log = next(pathlib.Path(sp_channels.history_dir()).glob("*.jsonl"))
        old = {"seq": 1, "ts": "2020-01-01T00:00:00.000Z", "topic": "channel:ops", "kind": "channel.created"}
        log.write_text(json.dumps(old) + "\n")
        self.assertEqual(self.cli("gc")[0], 0)
        self.assertEqual(log.read_text(), "")

    def test_stale_cursor_reports_a_gap_in_json_and_human_output(self):
        os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"] = "2"
        self.ok("create", "ops")
        for i in range(4):
            sp_channels.append_history("ops", "channel.note", self.me, {"body": "m%d" % i})
        result = self.tail_json("ops", "--since", "0")
        self.assertEqual(result["gap"], {"from": 1, "to": 3})
        self.assertEqual([e["seq"] for e in result["entries"]], [4, 5])
        self.assertEqual(result["next"], 5)
        self.assertEqual(result["channel"], "ops")
        out = self.ok("tail", "ops", "--since", "0")
        self.assertEqual(out.splitlines()[0], "gap: entries 1..3 pruned")
        self.assertTrue(out.rstrip().endswith("next: --since 5"))
        again = self.tail_json("ops", "--since", "5")
        self.assertEqual((again["entries"], again["gap"], again["next"]), ([], None, 5))

    def test_torn_append_is_cut_before_the_next_post(self):
        for base in (None, sp_channels.history_dir()):
            topic = "t-%s" % ("plain" if base is None else "base")
            sp_topics.topic_post(topic, {"kind": "cc", "uuid": new_uuid()}, text="one", base=base)
            log, _meta, _lock = sp_topics._topic_paths(topic, base)
            with open(log, "ab") as fh:
                fh.write(b'{"seq": 2, "ts": "2026-01-01T00:00:00.000Z", "te')
            entry = sp_topics.topic_post(topic, {"kind": "cc", "uuid": new_uuid()}, text="two", base=base)
            self.assertEqual(entry["seq"], 2)
            result = sp_topics.topic_tail(topic, base=base)
            self.assertEqual([(e["seq"], e["text"]) for e in result["entries"]], [(1, "one"), (2, "two")])
            self.assertTrue(pathlib.Path(log).read_bytes().endswith(b"\n"))

    def test_complete_but_unterminated_last_line_is_never_read(self):
        """A crash that lands the JSON but not the newline: the next post cuts
        that line and reuses its seq, so no reader may have seen it."""
        topic = "unterminated"
        sp_topics.topic_post(topic, {}, text="one")
        log, _meta, _lock = sp_topics._topic_paths(topic)
        with open(log, "ab") as fh:
            fh.write(json.dumps({"seq": 2, "ts": sp_topics.sp_runtime.now_iso(), "text": "torn"}).encode())
        seen = sp_topics.topic_tail(topic, since=1)
        self.assertEqual((seen["entries"], seen["next"]), ([], 1))
        entry = sp_topics.topic_post(topic, {}, text="two")
        self.assertEqual(entry["seq"], 2)
        after = sp_topics.topic_tail(topic, since=seen["next"])
        self.assertEqual([(e["seq"], e["text"]) for e in after["entries"]], [(2, "two")])

    def test_torn_first_line_is_dropped_entirely(self):
        log, _meta, _lock = sp_topics._topic_paths("solo")
        pathlib.Path(log).write_bytes(b'{"seq": 1, "te')
        entry = sp_topics.topic_post("solo", {}, text="fresh")
        self.assertEqual(entry["seq"], 1)
        self.assertEqual([e["text"] for e in sp_topics.topic_tail("solo")["entries"]], ["fresh"])

    def test_topic_post_refuses_the_channel_prefix(self):
        rc, _out, err = self.cli("topic", "post", "channel:x", "--message", "hi", "--as", self.me)
        self.assertEqual(rc, 2)
        self.assertIn("reserved for channels", err)
        self.assertIn("send --to '#NAME'", err)
        self.assertEqual(self.cli("topic", "list", "--json")[1].strip(), "[]")

    def run_peers(self, codex_home, extra_env, *args):
        env = dict(os.environ, CODEX_HOME=str(codex_home), **extra_env)
        for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            if name not in extra_env:
                env.pop(name, None)
        proc = subprocess.run(
            [sys.executable, str(PEERS)] + list(args), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_channel_history_is_shared_across_codex_homes_but_topics_are_not(self):
        home_a, home_b = self.root / "cxa", self.root / "cxb"
        home_a.mkdir()
        home_b.mkdir()
        sid_a, tid_b = new_uuid(), new_uuid()
        env_a, env_b = {"CLAUDE_CODE_SESSION_ID": sid_a}, {"CODEX_THREAD_ID": tid_b}
        self.assertEqual(self.run_peers(home_a, env_a, "channel", "create", "ops")[0], 0)
        self.assertEqual(self.run_peers(home_b, env_b, "channel", "join", "ops")[0], 0)
        for home, env, who in ((home_a, env_a, "cc:" + sid_a), (home_b, env_b, "codex:" + tid_b)):
            rc, out, err = self.run_peers(home, env, "channel", "tail", "ops", "--json")
            self.assertEqual(rc, 0, err)
            entries = json.loads(out)["entries"]
            self.assertEqual([e["kind"] for e in entries], ["channel.created", "channel.joined"])
            self.assertEqual([e["seq"] for e in entries], [1, 2])
        # Ordinary topics stay scoped to each CODEX_HOME.
        for home, env, who in ((home_a, env_a, "cc:" + sid_a), (home_b, env_b, "codex:" + tid_b)):
            rc, _out, err = self.run_peers(home, env, "topic", "post", "t", "--message", who, "--as", who)
            self.assertEqual(rc, 0, err)
        for home, env, who in ((home_a, env_a, "cc:" + sid_a), (home_b, env_b, "codex:" + tid_b)):
            rc, out, err = self.run_peers(home, env, "topic", "tail", "t", "--json")
            self.assertEqual(rc, 0, err)
            self.assertEqual([e["text"] for e in json.loads(out)["entries"]], [who])
        root = pathlib.Path(sp_channels.channels_root())
        self.assertTrue(str(root).startswith(str(self.home)))
        self.assertTrue((root / "ops.json").exists())


class MemberLivenessTests(ChannelBase):
    def test_claude_member_live_dead_gone_and_unverified(self):
        sid = new_uuid()
        self.add_listener(name="peer", session_id=sid)
        self.assertEqual(sp_channels.member_liveness("cc:" + sid), "live")
        self.assertEqual(sp_channels.member_liveness("cc:" + new_uuid()), "gone")
        dead_sid = new_uuid()
        self.write_record(2 ** 22 + 12345, "ghost", dead_sid, str(self.socks / "x.sock"))
        self.assertEqual(sp_channels.member_liveness("cc:" + dead_sid), "gone")
        (self.sessions / "broken.json").write_text("{not json")
        self.assertEqual(sp_channels.member_liveness("cc:" + sid), "unverified")
        self.assertEqual(sp_channels.member_liveness("cc:" + new_uuid()), "unverified")

    def test_codex_member_live_gone_and_unverified(self):
        tid, rollout = self.one_thread()
        self.assertEqual(sp_channels.member_liveness("codex:" + tid), "live")
        self.clear_holders()
        self.assertEqual(sp_channels.member_liveness("codex:" + tid), "gone")
        self.set_holder(rollout)
        os.environ["FAKE_LSOF_RC"] = "1"
        self.assertEqual(sp_channels.member_liveness("codex:" + tid), "unverified")
        del os.environ["FAKE_LSOF_RC"]
        self.assertEqual(sp_channels.member_liveness("codex:" + new_uuid()), "unverified")

    def test_codex_writer_lock_alone_proves_a_thread_live(self):
        tid = new_uuid()
        self.make_state_db([{"id": tid, "name": "fresh", "rollout_path": str(self.codex_dir / "missing.jsonl")}])
        lock = pathlib.Path(sp_codex.writer_lock_path(tid))
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        self.set_holder(lock)
        self.assertEqual(sp_channels.member_liveness("codex:" + tid), "live")

    def test_opencode_member_gone_needs_a_complete_walk_and_not_found(self):
        self.assertEqual(sp_channels.member_liveness("oc:" + OC_A), "live")
        # A child session is retrievable but absent from the top-level walk.
        self.assertEqual(sp_channels.member_liveness("oc:" + OC_CHILD), "unverified")
        missing = "ses_zzzzzzzzzzzzZZZZZZZZZZZZZZ"
        self.assertEqual(sp_channels.member_liveness("oc:" + missing), "gone")

    def test_opencode_walk_includes_sessions_outside_the_recent_window(self):
        old = "ses_ddddddddddddDDDDDDDDDDDDDD"
        self.set_oc(sessions=[oc_session(OC_A, "a"), oc_session(old, "old", updated_ms=1000)])
        self.assertEqual(sp_channels.member_liveness("oc:" + old), "live")
        self.assertEqual([s["id"] for s in sp_opencode.recent_sessions()], [OC_A])

    def test_opencode_incomplete_walk_or_failed_lookup_is_unverified(self):
        missing = "ses_zzzzzzzzzzzzZZZZZZZZZZZZZZ"
        self.set_oc(page_size=1, repeat_cursor=True)
        self.assertEqual(sp_channels.member_liveness("oc:" + missing), "unverified")
        self.assertEqual(sp_channels.member_liveness("oc:" + OC_A), "unverified")
        self.set_oc(page_size=100, repeat_cursor=False, get_error_tag="Boom")
        self.assertEqual(sp_channels.member_liveness("oc:" + missing), "unverified")
        self.set_oc(stopped=True, get_error_tag=None)
        self.assertEqual(sp_channels.member_liveness("oc:" + missing), "unverified")

    def test_members_command_prints_liveness_per_runtime(self):
        sid = new_uuid()
        self.add_listener(name="peer", session_id=sid)
        tid, _rollout = self.one_thread()
        missing = "ses_zzzzzzzzzzzzZZZZZZZZZZZZZZ"
        sp_channels.create_channel(
            "mix", "cc:" + sid, ["codex:" + tid, "oc:" + OC_A, "oc:" + OC_CHILD, "oc:" + missing]
        )
        self.as_cc(sid)
        rows = json.loads(self.ok("members", "mix", "--json"))["members"]
        self.assertEqual(
            [(r["member"], r["liveness"]) for r in rows],
            [("cc:" + sid, "live"), ("codex:" + tid, "live"), ("oc:" + OC_A, "live"),
             ("oc:" + OC_CHILD, "unverified"), ("oc:" + missing, "gone")],
        )
        out = self.ok("members", "mix")
        self.assertIn("oc:%s  gone" % missing, out)
        self.assertIn("cc:%s  live  (owner)" % sid, out)
