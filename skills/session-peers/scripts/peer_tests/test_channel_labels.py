"""Buddy-channel labels (#175): the ``current_buddy`` projection and ``derive_labels``.

A label is display only. It is derived from the record, never stored, and the
key stays the address.
"""

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_buddy import BuddyChannelBase
from peer_tests.test_opencode import OC_A, OC_B
from session_peers import channels as sp_channels, journal as sp_journal, storage as sp_storage, web, webdata as sp_webdata

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

    def edit_record(self, change):
        path = sp_channels._record_path(self.key)
        with open(path) as handle:
            rec = json.load(handle)
        change(rec)
        with open(path, "w") as handle:
            json.dump(rec, handle)

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

    def test_clear_on_a_record_without_the_field_sets_it_to_null(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.edit_record(lambda rec: rec.pop("current_buddy"))
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertIn("current_buddy", self.raw())
        self.assertIsNone(self.raw()["current_buddy"])
        row = self.row()
        self.assertEqual((row["label"], row["label_status"]), ("#lead", "stale"))

    def test_clear_after_the_buddy_was_removed_from_the_roster_still_nulls_the_field(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.ok("remove", self.key, self.x)
        self.assertEqual(self.raw()["current_buddy"], self.x)
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
        self.edit_record(lambda rec: rec.pop("current_buddy"))
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

    def test_list_json_emits_the_pair_buddy_not_the_raw_field(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        for bad in ({"a": 1}, ["codex:x"], 7, "garbage", self.lead):
            with self.subTest(bad=bad):
                self.edit_record(lambda rec: rec.update(current_buddy=bad))
                row = self.row()
                self.assertIsNone(row["current_buddy"])
                self.assertEqual(row["label_status"], "unverified")
        self.edit_record(lambda rec: rec.pop("current_buddy"))
        self.assertIsNone(self.row()["current_buddy"])

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


class FanOutTests(LabelBase):
    def test_get_session_lookups_are_capped_per_call(self):
        calls = []

        def get_session(sid):
            calls.append(sid)
            return {"id": sid, "title": "t"}

        typeds = ["oc:ses_%026d" % i for i in range(100)]
        with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", lambda *a, **k: []), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            names = sp_channels.session_names(typeds, {})
        self.assertEqual(len(calls), sp_channels.SESSION_LOOKUPS_MAX)
        self.assertEqual(set(names), set(typeds[:sp_channels.SESSION_LOOKUPS_MAX]))

    def test_get_session_stops_after_the_first_failure(self):
        calls = []

        def get_session(sid):
            calls.append(sid)
            raise sp_channels.sp_opencode.OpenCodeError("down")

        typeds = ["oc:ses_%026d" % i for i in range(10)]
        with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", lambda *a, **k: []), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            self.assertEqual(sp_channels.session_names(typeds, {}), {})
        self.assertEqual(len(calls), 1)

    def test_a_notfound_lookup_is_not_an_outage_in_either_order(self):
        gone, ok = "oc:ses_" + "0" * 26, "oc:ses_" + "1" * 26

        def get_session(sid):
            if sid == gone[3:]:
                raise sp_channels.sp_opencode.OpenCodeNotFound("deleted")
            return {"id": sid, "title": "fine"}

        for order in ([gone, ok], [ok, gone]):
            with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", lambda *a, **k: []), \
                    mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
                self.assertEqual(sp_channels.session_names(order, {}), {ok: "fine"})

    def test_notfound_still_counts_against_the_lookup_cap(self):
        calls = []

        def get_session(sid):
            calls.append(sid)
            raise sp_channels.sp_opencode.OpenCodeNotFound("deleted")

        typeds = ["oc:ses_%026d" % i for i in range(100)]
        with mock.patch.object(sp_channels.sp_opencode, "recent_sessions", lambda *a, **k: []), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            sp_channels.session_names(typeds, {})
        self.assertEqual(len(calls), sp_channels.SESSION_LOOKUPS_MAX)

    def test_non_cc_liveness_is_budgeted_and_the_rest_is_unverified(self):
        cap = sp_channels.SESSION_LOOKUPS_MAX
        records = [buddy_rec("buddy-%04d" % i, "cc:" + new_uuid(), "oc:ses_%026d" % i) for i in range(cap + 10)]
        calls = []

        def liveness_of(typed, cache):
            calls.append(typed)
            return "live"

        _names, live = sp_channels.label_context(records, {}, liveness_of=liveness_of, names_of=lambda t, c: {})
        self.assertEqual(len([t for t in calls if not t.startswith("cc:")]), cap)
        self.assertEqual(len([t for t in calls if t.startswith("cc:")]), cap + 10)
        self.assertEqual(sum(1 for v in live.values() if v == "unverified"), 10)
        labels = sp_channels.derive_labels(records, _names, live)
        self.assertTrue(all(v["status"] == "current" for v in labels.values()))

    def _oc_records(self, n):
        return [buddy_rec("buddy-%04d" % i, "cc:" + new_uuid(), "oc:ses_%026d" % i) for i in range(n)]

    def test_a_child_session_does_not_stop_later_opencode_lookups(self):
        records = self._oc_records(2)
        child, deleted = records[0]["current_buddy"][3:], records[1]["current_buddy"][3:]

        def get_session(sid):
            if sid == deleted:
                raise sp_channels.sp_opencode.OpenCodeNotFound("deleted")
            return {"id": sid, "title": "child"}

        with mock.patch.object(sp_channels.sp_opencode, "session_walk", lambda *a, **k: []), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            _names, live = sp_channels.label_context(records, {}, names_of=lambda t, c: {})
        self.assertEqual(live["oc:" + child], "unverified")
        self.assertEqual(live["oc:" + deleted], "gone")
        labels = sp_channels.derive_labels(records, {}, live)
        self.assertEqual(labels["buddy-0001"]["status"], "stale")

    def test_a_failed_opencode_walk_stops_further_opencode_lookups(self):
        records = self._oc_records(5)
        gets = []

        def walk(*a, **k):
            raise sp_channels.sp_opencode.OpenCodeError("down")

        with mock.patch.object(sp_channels.sp_opencode, "session_walk", walk), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", lambda sid: gets.append(sid)):
            calls = []
            real = sp_channels.member_liveness

            def liveness_of(typed, cache):
                calls.append(typed)
                return real(typed, cache)

            _names, live = sp_channels.label_context(records, {}, liveness_of=liveness_of, names_of=lambda t, c: {})
        self.assertEqual(len([t for t in calls if t.startswith("oc:")]), 1)
        self.assertEqual(gets, [])
        self.assertEqual({v for t, v in live.items() if t.startswith("oc:")}, {"unverified"})

    def test_opencode_members_in_the_walk_are_not_budgeted(self):
        n = sp_channels.SESSION_LOOKUPS_MAX + 10
        records = self._oc_records(n)
        ids = [r["current_buddy"][3:] for r in records]
        walked = ids[: n - 5]

        def get_session(sid):
            raise sp_channels.sp_opencode.OpenCodeNotFound("deleted")

        with mock.patch.object(sp_channels.sp_opencode, "session_walk", lambda *a, **k: [{"id": i} for i in walked]), \
                mock.patch.object(sp_channels.sp_opencode, "get_session", get_session):
            _names, live = sp_channels.label_context(records, {}, names_of=lambda t, c: {})
        for i in ids:
            self.assertEqual(live["oc:" + i], "live" if i in walked else "gone")

    def test_a_gone_opencode_answer_does_not_stop_opencode_lookups(self):
        records = [buddy_rec("buddy-%04d" % i, "cc:" + new_uuid(), "oc:ses_%026d" % i) for i in range(5)]
        calls = []

        def liveness_of(typed, cache):
            calls.append(typed)
            return "gone" if typed.startswith("oc:") else "live"

        sp_channels.label_context(records, {}, liveness_of=liveness_of, names_of=lambda t, c: {})
        self.assertEqual(len([t for t in calls if t.startswith("oc:")]), 5)

    def test_label_work_covers_at_most_the_channel_cap_of_buddy_records_in_key_order(self):
        cap = sp_constants.CHANNEL_MAX_CHANNELS
        records = [buddy_rec("buddy-%06d" % i, "cc:" + new_uuid(), "codex:" + new_uuid()) for i in range(cap + 20)]
        records.reverse()
        asked, live_calls = [], []

        def names_of(typeds, cache):
            asked.extend(typeds)
            return {}

        def liveness_of(typed, cache):
            live_calls.append(typed)
            return "live"

        names, live = sp_channels.label_context(records, {}, liveness_of=liveness_of, names_of=names_of)
        covered = {t for rec in sorted(records, key=lambda r: r["key"])[:cap] for t in (rec["owner"], rec["current_buddy"])}
        self.assertEqual(set(asked), covered)
        self.assertEqual(len(asked), len(set(asked)))
        self.assertTrue(set(live_calls) <= covered)
        self.assertTrue({t for t in covered if t.startswith("cc:")} <= set(live_calls))
        labels = sp_channels.derive_labels(records, names, live)
        beyond = records[0]  # the highest key
        self.assertEqual(labels[beyond["key"]]["status"], "current")
        self.assertEqual(labels[beyond["key"]]["label"],
                         "#%s-%s" % (self.short(beyond["owner"]), self.short(beyond["current_buddy"])))

    def test_label_context_resolves_its_defaults_at_call_time(self):
        rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
        with mock.patch.object(sp_channels, "session_names", lambda typeds, cache: {self.lead: "patched"}), \
                mock.patch.object(sp_channels, "member_liveness", lambda typed, cache: "gone"):
            names, live = sp_channels.label_context([rec], {})
        self.assertEqual(names, {self.lead: "patched"})
        self.assertEqual(live, {self.lead: "gone", self.x: "gone"})


class CollisionTests(LabelBase):
    def test_a_lead_named_general_never_labels_as_the_general_channel(self):
        rec, names = self.pair("buddy-abcd11111111", "general")
        rec["current_buddy"] = None
        out = sp_channels.derive_labels([rec], names, {})
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#general" + DOT + "abcd")

    def test_a_buddy_label_equal_to_another_buddys_key_form_is_suffixed(self):
        other = buddy_rec("buddy-eeee00000000", "cc:" + new_uuid(), "codex:" + new_uuid())
        rec, names = self.pair("buddy-abcd11111111", "buddy-eeee00000000")
        out = sp_channels.derive_labels([other, rec], names, {})
        self.assertEqual(out["buddy-abcd11111111"]["label"], "#buddy-eeee00000000" + DOT + "abcd")
        self.assertNotIn(out["buddy-abcd11111111"]["label"], {"#" + other["key"], out[other["key"]]["label"]})

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
        self.assertTrue(stale.endswith("  stale"), stale)
        rows = self.rows()
        self.assertEqual(set(rows["ops"]) - set(rows[self.key]), set())
        self.assertEqual(rows[self.key]["label_status"], "stale")
        self.edit_record(lambda rec: rec.pop("current_buddy"))
        unverified = [l for l in self.ok("list").splitlines() if self.key in l][0]
        self.assertTrue(unverified.endswith("  unverified"), unverified)

    def test_hostile_names_render_the_same_escaped_label_in_text_and_json(self):
        evil = "evil\x1b]0;pwned\x07\n<b>x</b>" + DOT + "\ue000\u0378"
        self.rename_lead(evil)
        self.assertEqual(self.bset(self.x)[0], 0)
        label = self.row()["label"]
        for bad in ("\x1b", "\x07", "\n", DOT):
            self.assertNotIn(bad, label)
        text = self.ok("list")
        self.assertNotIn("\x1b", text)
        self.assertIn(label, text)
        self.assertIn(ASCII_DOT, label)
        self.assertIn("\\ue000\\u0378", label)
        line = [l for l in text.splitlines() if self.key in l][0]
        cli_label = line.split("  buddy")[0]
        self.assertEqual(cli_label, sp_journal._human(label))
        self.assertEqual(cli_label, label)
        self.assertEqual(sp_webdata.escape_text(label), label)
        derived = sp_channels.derive_labels(sp_channels.list_channels(), *sp_channels.label_context(
            sp_channels.list_channels(), {}))
        self.assertEqual(derived[self.key]["label"], label)

    def test_every_other_category_c_character_is_a_fixed_point_of_human_and_escape_text(self):
        for ch in ("\ue000", "\u0378", "\U000f0000", "\U0010ffff", "\ud800", "\u00ad"):
            with self.subTest(ch=ch):
                rec = buddy_rec("buddy-aaaa00000000", self.lead, self.x)
                label = sp_channels.derive_labels([rec], {self.lead: "a" + ch + "b"}, {})[rec["key"]]["label"]
                self.assertEqual(sp_journal._human(label), label)
                self.assertEqual(sp_webdata.escape_text(label), label)
                self.assertNotIn(ch, label)


class AddressingTests(LabelBase):
    def test_a_label_is_refused_as_an_address_while_the_key_works(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        label = self.row()["label"]
        self.assertEqual(label, "#lead")  # a syntactically valid channel name, but not a stored channel
        for args in (("members", label), ("tail", label), ("tail", label, "--json")):
            with self.subTest(args=args):
                rc, _out, _err = self.ch(*args)
                self.assertNotEqual(rc, 0)
        rc, _out, _err = self.cli("send", "--to", label, "--message", "hello")
        self.assertNotEqual(rc, 0)
        for args in (("members", "#" + self.key), ("members", self.key), ("tail", self.key)):
            with self.subTest(args=args):
                rc, _out, err = self.ch(*args)
                self.assertEqual(rc, 0, err)
        self.assertNotIn(label, json.dumps(sp_channels.read_channel(self.key)))


class PageLabelTests(LabelBase):
    """The page's label (``state_view`` over a discovery pass) is the CLI's label, from any CODEX_HOME."""

    def page(self):
        view = sp_webdata.state_view(web.run_discovery())
        return {row["key"]: row for row in view["channels"]}

    def page_label(self):
        row = self.page()[self.key]
        return row["label"], row["label_status"]

    def agree(self):
        """The page and the CLI show one label from each of two homes; that label is returned."""
        seen = []
        (self.root / "orca").mkdir(exist_ok=True)
        try:
            for home in (self.codex_dir, self.root / "orca"):
                os.environ["CODEX_HOME"] = str(home)
                row = self.row()
                cli = (row["label"], row["label_status"])
                self.assertEqual(self.page_label(), cli)
                seen.append(cli)
        finally:
            os.environ["CODEX_HOME"] = str(self.codex_dir)
        self.assertEqual(seen[0], seen[1])
        return seen[0]

    def test_set_clear_and_handover_give_the_same_page_and_cli_label_from_both_homes(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.assertEqual(self.agree(), ("#lead-" + self.short(self.x), "current"))
        os.environ["CODEX_HOME"] = str(self.root / "orca")
        (self.root / "orca").mkdir(exist_ok=True)
        self.assertEqual(self.page_label()[0], "#lead-" + self.short(self.x))
        os.environ["CODEX_HOME"] = str(self.codex_dir)
        self.assertEqual(self.cli("buddy", "clear")[0], 0)
        self.assertEqual(self.agree(), ("#lead", "stale"))
        self.assertEqual(self.bset(self.y)[0], 0)
        _peer, successor = self.peer_cc("successor")
        self.assertEqual(self.cli("inbox", "handover", "--to", successor)[0], 0)
        self.assertEqual(self.cli("handover", "--to", successor)[0], 0)
        self.assertEqual(self.agree(), ("#successor", "stale"))

    def test_hostile_names_give_the_same_label_in_state_view_json_and_channel_list(self):
        self.rename_lead("evil\x1b]0;pwned\x07\n<b>x</b>" + DOT + "͸")
        self.assertEqual(self.bset(self.x)[0], 0)
        cli = self.row()["label"]
        view = sp_webdata.state_view(web.run_discovery())
        shown = json.loads(json.dumps(view))
        label = {row["key"]: row for row in shown["channels"]}[self.key]["label"]
        self.assertEqual(label, cli)
        self.assertIn(ASCII_DOT, label)
        for bad in ("\x1b", "\x07", "\n", DOT):
            self.assertNotIn(bad, label)


class OwnerHygieneTests(LabelBase):
    """A peer-written ``owner`` that is not a typed id of a buddy kind is never looked up."""

    EVIL = "codex:../../outside/victim"

    def record_calls(self):
        calls = []

        def note(name):
            def hook(*args, **kwargs):
                calls.append((name, args))
                return None
            return hook

        patches = [
            mock.patch.object(sp_storage, "thread_home", note("thread_home")),
            mock.patch.object(sp_storage, "_pidfile_is_held", note("_pidfile_is_held")),
            mock.patch.object(sp_channels.sp_codex, "resolve_thread", note("resolve_thread")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_pair_of_flags_an_unnormalizable_or_non_buddy_owner_unverified(self):
        for bad in (self.EVIL, "web:" + new_uuid(), "nonsense", ""):
            with self.subTest(bad=bad):
                lead, buddy, status = sp_channels.pair_of(buddy_rec("buddy-aa", bad, self.x))
                self.assertEqual((str(lead), buddy, status), (bad, None, "unverified"))
                self.assertIsInstance(lead, sp_channels.UnverifiedId)
                self.assertEqual(sp_channels.pair_lookup_ids([buddy_rec("buddy-aa", bad, self.x)]), ([], []))

    def test_the_owner_is_normalized_for_lookups(self):
        lead, buddy, status = sp_channels.pair_of(buddy_rec("buddy-aa", "cc:" + self.lead[3:].upper(), self.x))
        self.assertEqual((lead, buddy, status), (self.lead, self.x, "known"))
        self.assertNotIsInstance(lead, sp_channels.UnverifiedId)

    def test_the_cli_and_the_page_look_nothing_up_for_a_path_shaped_owner(self):
        self.assertEqual(self.bset(self.x)[0], 0)
        self.edit_record(lambda rec: rec.update(owner=self.EVIL, members=[self.EVIL, self.x]))
        calls = self.record_calls()
        row = self.row()
        self.assertEqual(row["label_status"], "unverified")
        self.assertEqual(row["label"], "#codex:../../ou")
        self.assertIsNone(row["current_buddy"])
        self.assertEqual(calls, [])  # the CLI looked nothing up: the lead is not a typed id
        view = sp_webdata.state_view(web.run_discovery())
        page = {r["key"]: r for r in view["channels"]}[self.key]
        self.assertEqual((page["label"], page["label_status"]), (row["label"], "unverified"))
        self.assertEqual([c for c in calls if "victim" in str(c) or ".." in str(c)], [])
