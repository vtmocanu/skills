"""Buddy-channel labels (#175): the ``current_buddy`` projection and ``derive_labels``.

A label is display only. It is derived from the record, never stored, and the
key stays the address.
"""

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_buddy import BuddyChannelBase
from peer_tests.test_opencode import OC_A, OC_B
from session_peers import channels as sp_channels

DOT = "·"
ASCII_DOT = "\\u00b7"


def buddy_rec(key, lead, buddy=sp_channels._ABSENT, **extra):
    rec = {"key": key, "display": "x", "kind": "buddy", "owner": lead, "members": [lead], **extra}
    if buddy is not sp_channels._ABSENT:
        rec["current_buddy"] = buddy
    return rec


def named_rec(key):
    return {"key": key, "display": key, "kind": "named", "owner": "cc:" + new_uuid(), "members": []}


class LabelBase(BuddyChannelBase):
    def rows(self):
        return {r["name"]: r for r in json.loads(self.ok("list", "--json"))}

    def row(self):
        return self.rows()[self.key]

    def raw(self):
        return sp_channels.read_channel(self.key)

    def in_home(self, name):
        """Point CODEX_HOME at a second, empty home (HOME, and so the channel store, stays shared)."""
        other = self.root / name
        other.mkdir(exist_ok=True)
        os.environ["CODEX_HOME"] = str(other)

    def rename_lead(self, name):
        """The lead's registry record is the only thing that changes."""
        rec = self.sender_rec
        self.write_record(rec["pid"], name, rec["sessionId"], rec["messagingSocketPath"])

    def short(self, typed):
        kind, _sep, ident = typed.partition(":")
        return "%s:%s" % (kind, ident[:8])


class ProjectionTests(LabelBase):
    def test_buddy_set_writes_the_field_and_names_the_pair(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.raw()["current_buddy"], self.x)
        row = self.row()
        self.assertEqual(row["current_buddy"], self.x)
        self.assertEqual(row["label"], "#lead-" + self.short(self.x))
        self.assertEqual(row["label_status"], "current")

    def test_replacing_the_buddy_updates_the_field_and_keeps_the_earlier_one_in_the_roster(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.bset(self.y)[0], 0)
        self.assertEqual(self.members(), [self.lead, self.x, self.y])
        row = self.row()
        self.assertEqual(row["current_buddy"], self.y)
        self.assertEqual(row["label"], "#lead-" + self.short(self.y))
        self.assertNotIn(self.short(self.x), row["label"])

    def test_clear_sets_the_field_to_null_and_the_label_is_the_lead_alone_and_stale(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertIsNone(self.raw()["current_buddy"])
        row = self.row()
        self.assertEqual((row["label"], row["label_status"]), ("#lead", "stale"))

    def test_clear_leaves_a_field_that_names_a_different_buddy(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.bset(self.y)[0], 0)
        self.ok("remove", self.key, self.x)
        sp_channels.change_membership(
            self.key, lambda rec: rec.update(current_buddy=self.x) or (None, None), self.lead)
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertEqual(self.raw()["current_buddy"], self.x)

    def test_an_already_member_buddy_is_a_record_only_write_with_no_extra_history(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        sp_channels.change_membership(
            self.key, lambda rec: rec.update(current_buddy=None) or (None, None), self.lead)
        before = self.history(self.key)
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.raw()["current_buddy"], self.x)
        self.assertEqual(self.history(self.key), before)

    def test_a_record_without_the_field_is_unverified_then_backfilled_by_the_next_set(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        path = sp_channels._record_path(self.key)
        with open(path) as handle:
            rec = json.load(handle)
        del rec["current_buddy"]
        with open(path, "w") as handle:
            json.dump(rec, handle)
        row = self.row()
        self.assertEqual((row["label"], row["label_status"]), ("#lead", "unverified"))
        self.assertIsNone(row["current_buddy"])
        before = self.history(self.key)
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.raw()["current_buddy"], self.x)
        self.assertEqual(self.history(self.key), before)
        self.assertEqual(self.row()["label"], "#lead-" + self.short(self.x))

    def test_an_invalid_field_is_unverified(self):
        for bad in ("not-a-typed-id", 7, ["codex:x"], "web:" + new_uuid(), self.lead):
            with self.subTest(bad=bad):
                rec = buddy_rec(self.key, self.lead, bad)
                self.assertEqual(sp_channels.pair_of(rec)[2], "unverified")
                labels = sp_channels.derive_labels([rec], {}, {})
                self.assertEqual(labels[self.key]["status"], "unverified")

    def test_pair_of_reports_known_none_and_unverified(self):
        self.assertEqual(sp_channels.pair_of(buddy_rec("buddy-aa", self.lead, self.x)),
                         (self.lead, self.x, "known"))
        self.assertEqual(sp_channels.pair_of(buddy_rec("buddy-aa", self.lead, None)),
                         (self.lead, None, "none"))
        self.assertEqual(sp_channels.pair_of(buddy_rec("buddy-aa", self.lead)),
                         (self.lead, None, "unverified"))

    def test_handover_nulls_the_field_and_the_successors_set_records_its_buddy(self):
        _peer, successor = self.peer_cc("successor")
        self.assertEqual(self.bset(self.x)[0], 0)
        self.ok_cli("inbox", "handover", "--to", successor)
        self.ok_cli("handover", "--to", successor)
        self.assertEqual(self.raw()["owner"], successor)
        self.assertIsNone(self.raw()["current_buddy"])
        self.assertEqual(self.row()["label_status"], "stale")
        self.as_cc(successor.partition(":")[2])
        self.assertEqual(self.bset(self.y)[0], 0)
        self.assertEqual(self.raw()["current_buddy"], self.y)
        self.assertEqual(self.row()["label"], "#successor-" + self.short(self.y))

    def ok_cli(self, *args):
        rc, _out, err = self.cli(*args)
        self.assertEqual(rc, 0, err)

    def test_the_field_survives_read_channel_and_a_buddy_record_is_created_with_null(self):
        key = sp_channels.buddy_channel_key("codex:" + new_uuid())
        rec = sp_channels.create_channel(key, self.lead, [], kind="buddy", reserved_ok=True)
        self.assertIsNone(rec["current_buddy"])
        self.assertIsNone(sp_channels.read_channel(key)["current_buddy"])
        self.assertNotIn("current_buddy", sp_channels.create_channel("ops", self.lead))


class TwoHomeTests(LabelBase):
    def labels_everywhere(self):
        seen = []
        for home in (self.codex_dir, self.root / "orca"):
            os.environ["CODEX_HOME"] = str(home)
            (self.root / "orca").mkdir(exist_ok=True)
            row = self.row()
            seen.append((row["label"], row["label_status"], row["current_buddy"]))
            text = self.ok("list")
            self.assertIn(row["label"], text)
        self.assertEqual(seen[0], seen[1])
        os.environ["CODEX_HOME"] = str(self.codex_dir)
        return seen[0]

    def test_the_cli_label_is_the_same_from_either_home_through_set_clear_and_handover(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.labels_everywhere(), ("#lead-" + self.short(self.x), "current", self.x))
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertEqual(self.labels_everywhere(), ("#lead", "stale", None))
        self.assertEqual(self.bset(self.y)[0], 0)
        _peer, successor = self.peer_cc("successor")
        self.assertEqual(self.cli("inbox", "handover", "--to", successor)[0], 0)
        self.assertEqual(self.cli("handover", "--to", successor)[0], 0)
        self.assertEqual(self.labels_everywhere(), ("#successor", "stale", None))

    def test_derive_labels_does_not_depend_on_codex_home(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        out = []
        for home in (self.codex_dir, self.root / "orca"):
            (self.root / "orca").mkdir(exist_ok=True)
            os.environ["CODEX_HOME"] = str(home)
            records = sp_channels.list_channels()
            names, live = {self.lead: "lead"}, {self.lead: "live", self.x: "live"}
            out.append(sp_channels.derive_labels(records, names, live))
        os.environ["CODEX_HOME"] = str(self.codex_dir)
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0][self.key]["label"], "#lead-" + self.short(self.x))


class NameTests(LabelBase):
    def derive(self, rec, names=None, live=None):
        return sp_channels.derive_labels([rec], names or {}, live or {})[rec["key"]]

    def test_shared_and_differing_names(self):
        rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
        self.assertEqual(self.derive(rec, {self.lead: "fire", self.x: "fire"})["label"], "#fire")
        self.assertEqual(self.derive(rec, {self.lead: "fire", self.x: "ice"})["label"], "#fire-ice")
        self.assertEqual(self.derive(rec, {self.x: "ice"})["label"], "#%s-ice" % self.short(self.lead))

    def test_a_rename_changes_the_label_with_the_record_bytes_identical(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        before = self.store()
        first = self.row()["label"]
        self.rename_lead("renamed")
        second = self.row()["label"]
        self.assertEqual(first, "#lead-" + self.short(self.x))
        self.assertEqual(second, "#renamed-" + self.short(self.x))
        self.assertEqual(self.store(), before)

    def test_a_gone_member_is_stale_with_its_retained_name_or_short_id(self):
        rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
        kept = self.derive(rec, {self.lead: "fire", self.x: "ice"}, {self.lead: "live", self.x: "gone"})
        self.assertEqual((kept["label"], kept["status"]), ("#fire-ice", "stale"))
        bare = self.derive(rec, {}, {self.lead: "gone", self.x: "live"})
        self.assertEqual((bare["label"], bare["status"]),
                         ("#%s-%s" % (self.short(self.lead), self.short(self.x)), "stale"))

    def test_unverified_liveness_is_not_stale(self):
        rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
        label = self.derive(rec, {}, {self.lead: "unverified", self.x: "unverified"})
        self.assertEqual(label["status"], "current")

    def test_names_are_clipped_and_escaped_and_the_middle_dot_is_ascii(self):
        rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
        label = self.derive(rec, {self.lead: "a" * 500, self.x: "b\x1b[31m‮c" + DOT + "d"})["label"]
        self.assertEqual(label.count("a"), sp_channels.NAME_MAX)
        self.assertNotIn(DOT, label)
        self.assertNotIn("\x1b", label)
        self.assertNotIn("‮", label)
        self.assertIn(ASCII_DOT, label)

    def test_session_names_use_the_registry_and_get_session_for_older_and_child_opencode_sessions(self):
        oc_recent, oc_old, oc_child = "oc:" + OC_A, "oc:ses_" + "0" * 26, "oc:" + OC_B
        sessions = {"ses_" + "0" * 26: "older", OC_B: "child"}

        def get_session(sid):
            if sid in sessions:
                return {"id": sid, "title": sessions[sid]}
            raise sp_channels.sp_opencode.OpenCodeError("unknown")

        recent = [{"id": OC_A, "title": "recent"}]
        with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", lambda *a, **k: recent), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            names = sp_channels.session_names([self.lead, oc_recent, oc_old, oc_child, "oc:ses_" + "1" * 26], {})
        self.assertEqual(names, {self.lead: "lead", oc_recent: "recent", oc_old: "older", oc_child: "child"})

    def test_a_failing_opencode_lookup_gives_no_name_and_the_short_id(self):
        typed = "oc:" + OC_A

        def boom(*args, **kwargs):
            raise RuntimeError("down")

        with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", boom), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", boom):
            self.assertEqual(sp_channels.session_names([typed], {}), {})
        rec = buddy_rec("buddy-aaaa00000000", self.lead, typed)
        self.assertEqual(self.derive(rec, {self.lead: "lead"})["label"], "#lead-oc:" + OC_A[4:12])

    def test_label_context_asks_only_about_pairs_and_never_reads_a_binding(self):
        seen = []
        records = [
            buddy_rec("buddy-aaaa00000000", self.lead, self.x),
            buddy_rec("buddy-bbbb00000000", "codex:" + new_uuid(), None),
            named_rec("ops"),
        ]

        def names_of(typeds, cache):
            seen.append(list(typeds))
            return {self.lead: "fire"}

        names, live = sp_channels.label_context(
            records, {}, liveness_of=lambda typed, cache: "gone" if typed == self.x else "live", names_of=names_of)
        self.assertEqual(names, {self.lead: "fire"})
        self.assertEqual(live, {self.lead: "live", self.x: "gone"})
        self.assertEqual(seen[0][:2], [self.lead, self.x])
        self.assertEqual(len(seen[0]), 3)  # the second record's lead, no buddy


class CollisionTests(LabelBase):
    def pair(self, key, name):
        lead, buddy = "cc:" + new_uuid(), "codex:" + new_uuid()
        return buddy_rec(key, lead, buddy), {lead: name, buddy: name}

    def labels(self, pairs, extra=()):
        records = [p[0] for p in pairs] + list(extra)
        names = {}
        for _rec, n in pairs:
            names.update(n)
        return sp_channels.derive_labels(records, names, {})

    def test_equal_labels_get_a_four_character_suffix_and_all_are_distinct(self):
        a, b = self.pair("buddy-abcd11111111", "fire"), self.pair("buddy-ef0122222222", "fire")
        out = self.labels([a, b])
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#fire" + DOT + "abcd")
        self.assertEqual(out["buddy-ef0122222222"]["label"], "#fire" + DOT + "ef01")

    def test_the_suffix_lengthens_until_distinct_and_shortens_when_a_sibling_goes(self):
        a, b = self.pair("buddy-abcd11111111", "fire"), self.pair("buddy-abcd22222222", "fire")
        out = self.labels([a, b])
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#fire" + DOT + "abcd1")
        self.assertEqual(out["buddy-abcd22222222"]["label"], "#fire" + DOT + "abcd2")
        alone = self.labels([a])
        self.assertEqual(alone["buddy-abcd11111111"]["label"], "#fire")

    def test_an_unrelated_removal_never_changes_a_suffix(self):
        a, b = self.pair("buddy-abcd11111111", "fire"), self.pair("buddy-ef0122222222", "fire")
        other = self.pair("buddy-9999aaaaaaaa", "ice")
        with_other = self.labels([a, b, other])
        without = self.labels([a, b])
        for key in ("buddy-abcd11111111", "buddy-ef0122222222"):
            self.assertEqual(with_other[key], without[key])
        self.assertEqual(with_other["buddy-9999aaaaaaaa"]["label"], "#ice")

    def test_a_buddy_label_equal_to_a_named_key_is_suffixed_and_the_named_one_is_not(self):
        a = self.pair("buddy-abcd11111111", "fire")
        out = self.labels([a], extra=[named_rec("fire")])
        self.assertEqual(out["fire"]["label"], "#fire")
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#fire" + DOT + "abcd")

    def test_a_literal_name_that_looks_like_a_generated_suffix_stays_distinct_in_ascii(self):
        a, b = self.pair("buddy-abcd11111111", "fire"), self.pair("buddy-ef0122222222", "fire")
        literal = self.pair("buddy-777788889999", "fire" + DOT + "abcd")
        out = self.labels([a, b, literal])
        shown = [v["label"] for v in out.values()]
        self.assertEqual(len(set(shown)), len(shown))
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#fire" + DOT + "abcd")
        self.assertEqual(out["buddy-777788889999"]["label"], "#fire" + ASCII_DOT + "abcd")
        self.assertNotIn(DOT, out["buddy-777788889999"]["label"])


class OutputTests(LabelBase):
    def test_cli_text_and_json_for_current_stale_and_unverified_rows(self):
        self.ok("create", "ops")
        self.assertEqual(self.bset(self.x)[0], 0)
        text = self.ok("list")
        self.assertIn("#ops  named", text)
        self.assertIn("#lead-%s  buddy" % self.short(self.x), text)
        self.assertIn("key #" + self.key, text)
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        stale = [l for l in self.ok("list").splitlines() if self.key in l][0]
        self.assertTrue(stale.startswith("#lead  buddy"), stale)
        self.assertTrue(stale.endswith("stale"), stale)
        rows = self.rows()
        self.assertEqual(set(rows["ops"]) - set(rows[self.key]), set())
        self.assertEqual(rows[self.key]["label_status"], "stale")

    def test_hostile_names_render_the_same_escaped_label_in_text_and_json(self):
        evil = "evil\x1b]0;pwned\x07\n<b>x</b>" + DOT
        self.rename_lead(evil)
        self.assertEqual(self.bset(self.x)[0], 0)
        label = self.row()["label"]
        for bad in ("\x1b", "\x07", "\n", DOT):
            self.assertNotIn(bad, label)
        text = self.ok("list")
        self.assertNotIn("\x1b", text)
        self.assertIn(label, text)
        self.assertIn(ASCII_DOT, label)


class AddressingTests(LabelBase):
    def test_a_label_is_refused_as_an_address_while_the_key_works(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        label = self.row()["label"]
        rc, _out, err = self.ch("members", label)
        self.assertNotEqual(rc, 0)
        rc, _out, err = self.ch("members", "#" + self.key)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.ch("members", self.key)
        self.assertEqual(rc, 0, err)
        self.assertNotIn(label, json.dumps(sp_channels.read_channel(self.key)))
