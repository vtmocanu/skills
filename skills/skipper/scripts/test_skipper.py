#!/usr/bin/env python3
"""Run directly; all sessions, transcripts, exports and peer commands are fixtures."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("skipper")
loader = importlib.machinery.SourceFileLoader("skipper", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
sk = importlib.util.module_from_spec(spec)
loader.exec_module(sk)
NOW = 1800000000
SID = "00000000-0000-4000-8000-000000000001"
LEAD = "claude:" + SID
OWNER = "00000000-0000-4000-8000-000000000002"


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.claude = self.home / ".claude"
        self.exports = self.root / "exports"
        self.state_dir = self.root / "state"
        self.warnings = []
        self.state = sk.empty_state()
        self.state["owner"] = OWNER

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def reading(self, **changes):
        row = {"v": 1, "session_id": SID, "used_pct": 42, "used_tokens": 420000,
               "context_window_size": 1000000, "source": "native", "stale": False,
               "ts": NOW - 10, "model_id": "model-a", "effective_model_id": "model-a",
               "cc_version": "example-version"}
        row.update(changes)
        return row

    def transcript(self, usage=None, model="model-a", name="project-a", timestamp=None):
        row = {"type": "assistant", "timestamp": timestamp or "2027-01-15T08:00:00Z",
               "message": {"model": model, "usage": usage if usage is not None else {
                   "input_tokens": 20000, "cache_read_input_tokens": 100000,
                   "cache_creation_input_tokens": 30000}}}
        return self.write(self.claude / "projects" / name / (SID + ".jsonl"), row)

    def ctx(self, readings=None, default=None):
        return sk.context(SID, list((self.claude / "projects").glob("*/*.jsonl")),
                          readings or {}, NOW, default, self.warnings)

    def enroll(self, target=LEAD, key="enroll-a"):
        common = {"id": key, "kind": "enrollment", "target": target,
                  "skipper": OWNER, "scope": "assignments and relayed user answers"}
        sk.record(self.state, common, NOW)
        sk.record(self.state, dict(common, state="authorized", user_grant_ref="user-message"), NOW)
        sk.record(self.state, dict(common, state="acked", ack_ref="lead-message"), NOW)


class ContextTests(FixtureCase):
    def test_reverse_reader_cross_block_line_and_no_final_newline(self):
        path = self.root / "lines"
        path.write_bytes(b"first\n" + b"x" * 70000 + b"\nlast")
        with path.open("rb") as stream:
            self.assertEqual(list(sk.reverse_lines(stream)), [b"last", b"x" * 70000, b"first"])

    def test_export_exact_without_transcript(self):
        row = self.reading(extra="ignored")
        self.write(self.exports / (SID + ".json"), row)
        data = sk.exports(self.exports, NOW, self.warnings)
        ctx = self.ctx(data)
        self.assertEqual((ctx["pct"], ctx["quality"], ctx["runtime_version"]),
                         (42, "exact", "example-version"))

    def test_untrusted_export_variants_do_not_supply_exact_value(self):
        self.transcript()
        for change in ({"source": "held", "stale": True}, {"source": "unknown"},
                       {"v": 2}, {"used_pct": True}, {"used_pct": 101},
                       {"session_id": "different"}, {"stale": True}):
            with self.subTest(change=change):
                self.write(self.exports / (SID + ".json"), self.reading(**change))
                self.assertEqual(self.ctx(sk.exports(self.exports, NOW, []))["quality"], "est")

    def test_freshness_boundary_and_future(self):
        self.transcript()
        for age, quality in ((0, "exact"), (179, "exact"), (180, "est"), (-1, "est")):
            with self.subTest(age=age):
                self.assertEqual(self.ctx({SID: self.reading(ts=NOW - age)})["quality"], quality)

    def test_fallback_order_learned_configured_builtin(self):
        self.transcript()
        learned = self.ctx({SID: self.reading(ts=NOW - 200, context_window_size=300000)}, 600000)
        self.assertEqual((learned["pct"], learned["window_source"]), (50, "learned-export"))
        configured = self.ctx(default=600000)
        self.assertEqual((configured["pct"], configured["window_source"]), (25, "configured"))
        builtin = self.ctx()
        self.assertEqual((builtin["pct"], builtin["window_source"], builtin["window"]), (75, "builtin", 200000))

    def test_learning_uses_serving_model_and_24_hour_boundary(self):
        self.transcript()
        for changes in ({"effective_model_id": "other", "ts": NOW - 200},
                        {"ts": NOW - 86400}, {"ts": NOW + 1}):
            with self.subTest(changes=changes):
                self.assertEqual(self.ctx({SID: self.reading(**changes)})["window_source"], "builtin")
        row = self.reading(model_id="other", effective_model_id="model-a", ts=NOW - 200)
        self.assertEqual(self.ctx({SID: row})["window_source"], "learned-export")

    def test_missing_invalid_and_negative_usage_unknown(self):
        for usage in ({}, {"input_tokens": -1}, {"input_tokens": True},
                      {"input_tokens": 1, "cache_read_input_tokens": 2,
                       "cache_creation_input_tokens": float("nan")}):
            with self.subTest(usage=usage):
                self.transcript(usage=usage)
                self.assertEqual(self.ctx()["quality"], "unknown")

    def test_synthetic_and_partial_lines_do_not_replace_latest_usage(self):
        path = self.transcript()
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "assistant", "timestamp": "2028-01-01T00:00:00Z",
                                     "message": {"model": "<synthetic>", "usage": {}}}) + "\n")
            stream.write('{"type":')
        self.assertEqual(self.ctx()["pct"], 75)

    def test_latest_record_across_projects_wins(self):
        self.transcript(name="a")
        self.transcript(name="b", usage={"input_tokens": 500000, "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0}, timestamp="2028-01-01T00:00:00Z")
        self.assertEqual(self.ctx()["pct"], 250)


class DiscoveryTests(FixtureCase):
    def test_peer_failure_reports_selected_path_and_reason(self):
        for result, expected in (
                (subprocess.CompletedProcess([], 3, "", "fixture permission denied"), "exited 3: fixture permission denied"),
                (subprocess.CompletedProcess([], 0, "not JSON", ""), "JSONDecodeError")):
            with self.subTest(expected=expected), \
                    mock.patch.object(sk, "find_peers", return_value=Path("fixture-peers.py")), \
                    mock.patch.object(sk, "run", return_value=result):
                snapshot = sk.observe(self.home, self.claude, self.exports, self.state, NOW)
            diagnostic = next(w for w in snapshot["warnings"] if w.startswith("session-peers unavailable"))
            self.assertIn(expected, diagnostic)
            self.assertIn("fixture-peers.py", diagnostic)
            self.assertFalse(snapshot["complete"]["codex"])

    def test_peer_timeout_diagnostic_retains_duration(self):
        with mock.patch.object(sk, "find_peers", return_value=Path("fixture-peers.py")), \
                mock.patch.object(sk, "run", side_effect=subprocess.TimeoutExpired(["fixture-peers.py"], 15)):
            snapshot = sk.observe(self.home, self.claude, self.exports, self.state, NOW)
        diagnostic = next(w for w in snapshot["warnings"] if w.startswith("session-peers unavailable"))
        self.assertIn("timed out after 15 seconds", diagnostic)
        self.assertIn("fixture-peers.py", diagnostic)
        self.assertFalse(snapshot["complete"]["codex"])

    def test_peer_detection_prefers_existing_installed_path(self):
        path = self.home / ".agents/skills/session-peers/scripts/peers.py"
        path.parent.mkdir(parents=True)
        path.write_text("# fixture")
        self.assertEqual(sk.find_peers(self.home), path)

    def test_resolve_uses_roster_only_and_rejects_ambiguous_names(self):
        item = {"id": LEAD, "session_id": SID, "runtime": "claude", "name": "lead-a", "liveness": "live"}
        snapshot = {"sessions": {LEAD: item}, "changes": {LEAD: {"name": "lead-a"}}}
        self.assertEqual(sk.resolve(snapshot, "lead-a")["id"], LEAD)
        other = dict(item, id="codex:" + SID, runtime="codex")
        snapshot["sessions"][other["id"]] = other
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            sk.resolve(snapshot, "lead-a")
        self.assertEqual(sk.resolve(snapshot, "lead-a", "claude")["id"], LEAD)
        self.assertEqual(sk.resolve(snapshot, other["id"])["runtime"], "codex")

    def test_resolve_rejects_absent_prefixes_and_unverified_liveness(self):
        item = {"id": LEAD, "session_id": SID, "runtime": "claude", "name": "@lead-a", "liveness": "live"}
        snapshot = {"sessions": {LEAD: item}}
        self.assertEqual(sk.resolve(snapshot, "@lead-a")["session_id"], SID)
        self.assertEqual(sk.resolve(snapshot, SID)["id"], LEAD)
        with self.assertRaisesRegex(ValueError, "no observed session"):
            sk.resolve(snapshot, "@lead")
        item["liveness"] = "unverified"
        with self.assertRaisesRegex(ValueError, "unverified liveness"):
            sk.resolve(snapshot, "@lead-a")

    def test_pending_handover_report_displays_without_fake_task(self):
        self.registry()
        sk.record(self.state, {"kind": "session", "id": "ready", "target": LEAD,
                              "state": "pending-handover", "report_ref": "join-ack"}, NOW)
        with mock.patch.object(sk, "liveness", return_value="live"):
            table = sk.status(self.observe(), self.state)
        self.assertIn("pending-handover", table)
        self.assertIn("awaiting handover", table)
        self.assertNotIn("task", self.state["records"]["ready"])

    def test_overfull_estimate_display_keeps_raw_json_value(self):
        self.registry()
        for tokens, raw, displayed in ((200000, 100, "100%"), (202000, 101, ">100%"),
                                        (500000, 250, ">100%")):
            with self.subTest(raw=raw):
                self.transcript(usage={"input_tokens": tokens, "cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0})
                with mock.patch.object(sk, "liveness", return_value="live"):
                    snapshot = self.observe()
                self.assertEqual(snapshot["sessions"][LEAD]["context"]["pct"], raw)
                table = sk.status(snapshot, self.state)
                self.assertIn("| " + displayed + " |", table)
                self.assertEqual(json.loads(json.dumps(snapshot))["sessions"][LEAD]["context"]["pct"], raw)

    def test_foreign_pid_domain_never_probes_or_reports_vanished(self):
        self.registry(pidDomain="foreign-domain")
        self.state["observations"] = {"sessions": {LEAD: {"runtime": "claude"}}}
        with mock.patch.object(sk.os, "kill", side_effect=AssertionError("must not probe foreign PID")):
            snapshot = self.observe()
        self.assertEqual(snapshot["sessions"][LEAD]["liveness"], "unverified")
        self.assertNotIn(LEAD, snapshot["vanished"])

    def peer_observation(self, payload, include_internal=False):
        with mock.patch.object(sk, "find_peers", return_value=Path("fixture.py")), \
                mock.patch.object(sk, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")), \
                mock.patch.object(sk, "repo", return_value={"root": "/example/repo-a", "remote": "git@example.com:org/repo-a.git"}):
            if include_internal:
                return sk.observe(self.home, self.claude, self.exports, self.state, NOW,
                                  include_internal=True)
            return sk.observe(self.home, self.claude, self.exports, self.state, NOW)

    def peer_payload(self, **changes):
        row = {"id": SID, "name": "buddy-a", "cwd": "/example/repo-a", "holder_pid": 123, "shim_pid": 456}
        row.update(changes)
        return {"claude": [{"runtime": "codex", "sessionId": SID, "status": "busy"}],
                "codex": [row], "codex_unverified": [], "codex_schema_recognised": True}

    def test_default_name_repo_footer_are_short_and_verbose_keeps_ids(self):
        self.registry()
        with mock.patch.object(sk, "liveness", return_value="live"):
            snapshot = self.observe()
        snapshot["sessions"][LEAD]["repo"] = {"root": "/example/local-checkout", "remote": "git@example.com:org/repo-a.git"}
        table = sk.status(snapshot, self.state)
        self.assertIn("| @lead-a | repo-a |", table)
        self.assertIn("new: @lead-a (repo-a)", table)
        self.assertNotIn(SID, table)
        self.assertNotIn("/example/", table)
        verbose = sk.status(snapshot, self.state, verbose=True)
        self.assertIn(LEAD, verbose)
        self.assertIn("/example/local-checkout", verbose)
        self.assertEqual(sk.repo_name({"root": "/example/local-checkout", "remote": None}), "local-checkout")

    def test_codex_default_hidden_and_explicit_buddy_on_lead_row(self):
        self.registry()
        with mock.patch.object(sk, "liveness", return_value="live"):
            snapshot = self.peer_observation(self.peer_payload())
        sk.record(self.state, {"id": "report", "kind": "session", "target": LEAD,
                              "state": "running", "task": "task-a", "report_ref": "message",
                              "buddy": {"id": "codex:" + SID, "name": "old-name"}}, NOW)
        table = sk.status(snapshot, self.state)
        self.assertIn("| buddy-a |", table)
        self.assertNotIn("| buddy-a | repo-a |", table)
        self.assertNotIn("buddy-a (repo-a)", table)
        self.assertIn("| buddy-a | repo-a |", sk.status(snapshot, self.state, show_all=True))

    def test_no_buddy_pair_inferred_from_identical_names(self):
        self.registry(name="same")
        with mock.patch.object(sk, "liveness", return_value="live"):
            snapshot = self.peer_observation(self.peer_payload(name="same"))
        self.assertIn("| same | repo-a | ? |", sk.status(snapshot, self.state))

    def test_codex_status_from_shim_and_direct_peer_field(self):
        snapshot = self.peer_observation(self.peer_payload())
        self.assertEqual(snapshot["sessions"]["codex:" + SID]["state"], "busy")
        snapshot = self.peer_observation(self.peer_payload(status="idle"))
        self.assertEqual(snapshot["sessions"]["codex:" + SID]["state"], "idle")

    def test_codex_registry_status_not_overwritten_by_unknown(self):
        self.registry(entrypoint="codex", status="idle")
        payload = self.peer_payload()
        payload["claude"] = []
        with mock.patch.object(sk, "liveness", return_value="live"):
            snapshot = self.peer_observation(payload)
        self.assertEqual(snapshot["sessions"]["codex:" + SID]["state"], "idle")

    def test_guardian_shared_holder_filtered_all_restores_and_no_vanished_noise(self):
        payload = self.peer_payload()
        child = "00000000-0000-4000-8000-000000000003"
        payload["codex"].append({"id": child, "name": "Guardian review", "holder_pid": 123,
                                 "peer_name": "codex-" + child, "shim_pid": None})
        self.state["observations"] = {"sessions": {"codex:" + child: {"runtime": "codex"}}}
        snapshot = self.peer_observation(payload)
        self.assertNotIn("codex:" + child, snapshot["sessions"])
        self.assertEqual(snapshot["excluded"]["codex:" + child], "shared-holder-reviewer")
        self.assertNotIn("codex:" + child, snapshot["vanished"])
        self.assertIn("codex:" + child, self.peer_observation(payload, include_internal=True)["sessions"])

    def test_parent_alias_filters_renamed_child_without_hiding_same_pid_lead(self):
        payload = self.peer_payload()
        child = "11111111-0000-4000-8000-000000000003"
        payload["codex"].append({"id": child, "name": "internal renamed reviewer", "holder_pid": 123,
                                 "peer_name": "codex-" + SID, "shim_pid": None})
        snapshot = self.peer_observation(payload)
        self.assertEqual(snapshot["excluded"]["codex:" + child], "parent-peer-alias")
        payload["codex"][-1]["peer_name"] = "codex-" + child
        payload["codex"][-1]["name"] = "independent lead"
        self.assertIn("codex:" + child, self.peer_observation(payload)["sessions"])

    def test_title_fallback_preserves_registered_or_attached_guardian(self):
        child = "11111111-0000-4000-8000-000000000003"
        payload = self.peer_payload(id=child, name="Guardian review", holder_pid=None, shim_pid=None)
        self.assertEqual(self.peer_observation(payload)["excluded"]["codex:" + child], "reviewer-title-fallback")
        for field in ("registered", "shim_pid"):
            payload["codex"][0][field] = True
            self.assertIn("codex:" + child, self.peer_observation(payload)["sessions"])
            payload["codex"][0][field] = None

    def test_vanished_footer_names_survive_saved_baseline(self):
        self.registry()
        with mock.patch.object(sk, "liveness", return_value="live"):
            self.state["observations"] = self.observe()
        (self.claude / "sessions/1.json").unlink()
        snapshot = self.observe()
        self.state["observations"] = snapshot
        table = sk.status(snapshot, self.state)
        self.assertIn("vanished: @lead-a (example)", table)
        self.assertNotIn(SID, table)

    def observe(self):
        with mock.patch.object(sk, "find_peers", return_value=None), mock.patch.object(sk, "repo", return_value={"root": "example", "remote": None}):
            return sk.observe(self.home, self.claude, self.exports, self.state, NOW)

    def registry(self, **changes):
        row = {"sessionId": SID, "name": "@lead-a", "pid": 1, "cwd": "example", "status": "idle"}
        row.update(changes)
        return self.write(self.claude / "sessions/1.json", row)

    def test_new_and_vanished_diff_without_enrollment(self):
        path = self.registry()
        with mock.patch.object(sk, "liveness", return_value="live"):
            first = self.observe()
        self.assertEqual(first["new"], [LEAD])
        self.assertFalse(self.state["records"])
        self.state["observations"] = first
        path.unlink()
        self.assertEqual(self.observe()["vanished"], [LEAD])

    def test_export_file_alone_is_not_liveness(self):
        self.write(self.exports / (SID + ".json"), self.reading())
        self.assertEqual(self.observe()["sessions"], {})

    def test_bad_registry_cannot_prove_disappearance(self):
        path = self.registry()
        self.state["observations"] = {"sessions": {LEAD: {"runtime": "claude"}}}
        path.write_text("broken")
        snapshot = self.observe()
        self.assertEqual(snapshot["vanished"], [])
        self.assertEqual(snapshot["unverified_missing"], [LEAD])

    def test_failed_codex_discovery_preserves_unknown(self):
        self.state["observations"] = {"sessions": {"codex:" + SID: {"runtime": "codex"}}}
        self.assertEqual(self.observe()["unverified_missing"], ["codex:" + SID])

    def test_peer_schema_codex_and_unverified(self):
        payload = {"codex": [{"id": SID, "name": "buddy", "cwd": "example"}],
                   "codex_unverified": [], "codex_schema_recognised": True}
        with mock.patch.object(sk, "find_peers", return_value=Path("fixture.py")), \
                mock.patch.object(sk, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")), \
                mock.patch.object(sk, "repo", return_value={"root": "example", "remote": None}):
            snapshot = sk.observe(self.home, self.claude, self.exports, self.state, NOW)
        item = snapshot["sessions"]["codex:" + SID]
        self.assertEqual(item["liveness"], "live")
        self.assertIsNone(item["context"]["pct"])

    def test_registry_unverified_is_visible(self):
        self.registry()
        with mock.patch.object(sk, "liveness", return_value="unverified"):
            snapshot = self.observe()
        self.assertIn("liveness unverified", sk.status(snapshot, self.state))

    def test_pid_reuse_domain_and_probe_failure(self):
        row = {"pid": 123, "procStart": "expected"}
        with mock.patch.object(sk.os, "kill"), mock.patch.object(sk, "run", return_value=subprocess.CompletedProcess([], 0, "different", "")):
            self.assertEqual(sk.liveness(row), "dead")
            self.assertEqual(sk.liveness(dict(row, pidDomain="other")), "unverified")
        with mock.patch.object(sk.os, "kill"), mock.patch.object(sk, "run", side_effect=PermissionError()):
            self.assertEqual(sk.liveness(row), "unverified")
        with mock.patch.object(sk.os, "kill", side_effect=ProcessLookupError()):
            self.assertEqual(sk.liveness(row), "dead")

    def test_table_ctx_column_percentage_only_and_safe_cells(self):
        self.registry(name="lead|\x1b\n")
        self.transcript()
        with mock.patch.object(sk, "liveness", return_value="live"):
            table = sk.status(self.observe(), self.state)
        self.assertIn("| 75% |", table)
        self.assertIn("ctx est", table)
        self.assertNotIn("\x1b", table)


class LedgerTests(FixtureCase):
    def test_queued_command_done_uses_evidence_as_implicit_acceptance(self):
        self.enroll()
        sk.record(self.state, {"id": "c", "kind": "command", "target": LEAD, "task": "task-a"}, NOW)
        before = json.dumps(self.state, sort_keys=True)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "c", "state": "done"}, NOW)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        done = sk.record(self.state, {"id": "c", "state": "done", "evidence": "completion-message"}, NOW + 1)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["ack_ref"], "completion-message")
        self.assertEqual(sk.record(self.state, {"id": "c", "state": "done"}, NOW + 2)["state"], "done")

    def test_unfinished_handovers_can_be_superseded_with_reason(self):
        successor = "claude:00000000-0000-4000-8000-000000000003"
        for stage in ("requested", "ready", "acked"):
            with self.subTest(stage=stage):
                key = "void-" + stage
                sk.record(self.state, {"id": key, "kind": "handover", "predecessor": LEAD,
                                      "successor": successor, "task": "task-a"}, NOW)
                if stage != "requested":
                    sk.record(self.state, {"id": key, "state": "ready", "handover_ref": "artifact"}, NOW)
                if stage == "acked":
                    sk.record(self.state, {"id": key, "state": "acked", "successor_ack_ref": "ack"}, NOW)
                before = json.dumps(self.state, sort_keys=True)
                with self.assertRaises(ValueError):
                    sk.record(self.state, {"id": key, "state": "superseded"}, NOW)
                self.assertEqual(json.dumps(self.state, sort_keys=True), before)
                result = sk.record(self.state, {"id": key, "state": "superseded", "reason": "wrong successor"}, NOW + 1)
                self.assertEqual(result["state"], "superseded")
                self.assertFalse(sk.enrolled(self.state, successor))
                with self.assertRaises(ValueError):
                    sk.record(self.state, {"id": key, "state": "ready", "handover_ref": "late"}, NOW + 2)

    def test_owner_conflict_names_both_owners_and_preserves_ledger(self):
        self.write(self.state_dir / "state.json", self.state)
        before = (self.state_dir / "state.json").read_bytes()
        with self.assertRaises(ValueError) as raised:
            with sk.writer(self.state_dir, SID):
                pass
        self.assertIn(OWNER, str(raised.exception))
        self.assertIn(SID, str(raised.exception))
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def task_table(self):
        snapshot = {"sessions": {LEAD: {"name": "lead-a", "runtime": "claude",
                    "repo": {"root": "/example/repo-a", "remote": None}, "state": "idle",
                    "liveness": "live", "context": {"pct": None, "quality": "unknown"}}},
                    "new": [], "vanished": [], "unverified_missing": [], "warnings": []}
        return sk.status(snapshot, self.state)

    def test_status_falls_back_to_latest_open_command_for_enrolled_lead(self):
        self.enroll()
        sk.record(self.state, {"id": "older", "kind": "command", "target": LEAD, "task": "older-task"}, NOW)
        sk.record(self.state, {"id": "older", "state": "accepted", "ack_ref": "ack"}, NOW)
        sk.record(self.state, {"id": "newer", "kind": "command", "target": LEAD, "task": "newer-task"}, NOW + 1)
        self.assertIn("| newer-task |", self.task_table())
        sk.record(self.state, {"id": "newer", "state": "superseded"}, NOW + 2)
        self.assertIn("| older-task |", self.task_table())
        sk.record(self.state, {"id": "enroll-a", "state": "revoked"}, NOW + 3)
        self.assertNotIn("older-task", self.task_table())

    def test_status_falls_back_to_incoming_handover_but_not_void(self):
        self.enroll()
        predecessor = "claude:00000000-0000-4000-8000-000000000003"
        sk.record(self.state, {"id": "h", "kind": "handover", "predecessor": predecessor,
                              "successor": LEAD, "task": "incoming-task"}, NOW)
        self.assertIn("| incoming-task |", self.task_table())
        sk.record(self.state, {"id": "h", "state": "superseded", "reason": "abandoned"}, NOW + 1)
        self.assertNotIn("incoming-task", self.task_table())

    def test_explicit_session_report_wins_over_command_fallback(self):
        self.enroll()
        sk.record(self.state, {"id": "c", "kind": "command", "target": LEAD, "task": "command-task"}, NOW)
        sk.record(self.state, {"id": "r", "kind": "session", "target": LEAD,
                              "state": "pending-handover", "report_ref": "report"}, NOW)
        self.assertIn("awaiting handover", self.task_table())
        self.assertNotIn("command-task", self.task_table())

    def test_pending_handover_clears_task_bearing_report_for_table(self):
        sk.record(self.state, {"kind": "session", "id": "report", "target": LEAD,
                              "state": "running", "task": "old-task", "report_ref": "work-report"}, NOW)
        updated = sk.record(self.state, {"id": "report", "state": "pending-handover"}, NOW)
        self.assertNotIn("task", updated)
        snapshot = {"sessions": {LEAD: {"id": LEAD, "name": "lead-a", "runtime": "claude",
                    "repo": {"root": "/example/repo-a", "remote": None}, "state": "idle",
                    "liveness": "live", "context": {"pct": None, "quality": "unknown"}}},
                    "new": [], "vanished": [], "unverified_missing": [], "warnings": []}
        table = sk.status(snapshot, self.state)
        self.assertIn("awaiting handover", table)
        self.assertNotIn("old-task", table)

    def test_pending_handover_refuses_supplied_tasks_without_mutation(self):
        sk.record(self.state, {"kind": "session", "id": "report", "target": LEAD,
                              "state": "running", "task": "old-task", "report_ref": "work-report"}, NOW)
        before = json.dumps(self.state, sort_keys=True)
        for task in ("new-task", None, ""):
            with self.subTest(task=task), self.assertRaisesRegex(ValueError, "cannot include a task"):
                sk.record(self.state, {"id": "report", "state": "pending-handover", "task": task}, NOW)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)

    def test_pending_handover_ack_requires_no_task_and_grants_no_ownership(self):
        report = sk.record(self.state, {"kind": "session", "id": "ready", "target": LEAD,
                                       "state": "pending-handover", "report_ref": "join-ack"}, NOW)
        self.assertNotIn("task", report)
        self.assertFalse(sk.enrolled(self.state, LEAD))
        with self.assertRaisesRegex(ValueError, "missing task"):
            sk.record(self.state, {"id": "ready", "state": "running"}, NOW)
        assigned = sk.record(self.state, {"id": "ready", "state": "running", "task": "task-a"}, NOW)
        self.assertEqual(assigned["task"], "task-a")

    def test_revoked_existing_command_cannot_be_accepted_again(self):
        self.enroll()
        sk.record(self.state, {"id": "c1", "kind": "command", "target": LEAD, "task": "task-a"}, NOW)
        sk.record(self.state, {"id": "c1", "state": "accepted", "ack_ref": "accepted-message"}, NOW)
        sk.record(self.state, {"id": "enroll-a", "state": "revoked"}, NOW)
        before = json.dumps(self.state, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "revoked command updates"):
            sk.record(self.state, {"id": "c1", "state": "accepted", "ack_ref": "new-message"}, NOW)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        sk.record(self.state, {"id": "c1", "state": "blocked"}, NOW)
        with self.assertRaisesRegex(ValueError, "revoked command updates"):
            sk.record(self.state, {"id": "c1", "state": "accepted"}, NOW)

    def test_revoked_existing_command_can_record_blocked_done_or_superseded(self):
        for destination in ("blocked", "done", "superseded"):
            with self.subTest(destination=destination):
                self.state = sk.empty_state()
                self.state["owner"] = OWNER
                self.enroll()
                sk.record(self.state, {"id": "c1", "kind": "command", "target": LEAD, "task": "task-a"}, NOW)
                sk.record(self.state, {"id": "c1", "state": "accepted", "ack_ref": "accepted-message"}, NOW)
                sk.record(self.state, {"id": "enroll-a", "state": "revoked"}, NOW)
                result = sk.record(self.state, {"id": "c1", "state": destination, "evidence": "result-ref"}, NOW)
                self.assertEqual(result["state"], destination)

    def test_revoke_supersede_reassign_without_old_lead_enrollment(self):
        successor = "claude:00000000-0000-4000-8000-000000000003"
        self.enroll()
        self.enroll(successor, "enroll-b")
        sk.record(self.state, {"id": "c1", "kind": "command", "target": LEAD,
                              "task": "task-a"}, NOW)
        sk.record(self.state, {"id": "enroll-a", "kind": "enrollment", "state": "revoked"}, NOW)
        self.assertFalse(sk.enrolled(self.state, LEAD))
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "premature", "kind": "command", "target": successor,
                                  "task": "task-a"}, NOW)
        sk.record(self.state, {"id": "c1", "kind": "command", "state": "superseded"}, NOW)
        reassigned = sk.record(self.state, {"id": "c2", "kind": "command", "target": successor,
                                           "task": "task-a"}, NOW)
        self.assertEqual(reassigned["state"], "queued")
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "revoked-new", "kind": "command", "target": LEAD,
                                  "task": "another-task"}, NOW)

    def test_partial_record_update_retains_kind(self):
        self.enroll()
        sk.record(self.state, {"id": "c1", "kind": "command", "target": LEAD, "task": "task-a"}, NOW)
        result = sk.record(self.state, {"id": "c1", "state": "superseded"}, NOW)
        self.assertEqual((result["kind"], result["state"]), ("command", "superseded"))

    def test_existing_loose_state_directory_tightened(self):
        self.state_dir.mkdir(mode=0o755)
        self.state_dir.chmod(0o755)
        with sk.writer(self.state_dir, OWNER):
            self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)

    def test_failed_directory_tightening_does_not_write_state(self):
        self.state_dir.mkdir(mode=0o755)
        self.state_dir.chmod(0o755)
        with mock.patch.object(Path, "chmod", side_effect=PermissionError("fixture denied")):
            with self.assertRaises(PermissionError), sk.writer(self.state_dir, OWNER):
                pass
        self.assertFalse((self.state_dir / "state.json").exists())

    def test_active_task_cannot_be_assigned_to_two_leads(self):
        other = "claude:00000000-0000-4000-8000-000000000003"
        self.enroll()
        self.enroll(other, "enroll-b")
        sk.record(self.state, {"id": "a", "kind": "command", "task": "task-a", "target": LEAD}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "b", "kind": "command", "task": "task-a", "target": other}, NOW)

    def test_atomic_private_state_and_owner(self):
        with sk.writer(self.state_dir, OWNER) as state:
            sk.event(state, "probe", "fixture", NOW)
        self.assertEqual(sk.load_state(self.state_dir)["owner"], OWNER)
        self.assertEqual((self.state_dir / "state.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError), sk.writer(self.state_dir, "other"):
            pass

    def test_failed_mutation_preserves_previous_bytes(self):
        with sk.writer(self.state_dir, OWNER):
            pass
        before = (self.state_dir / "state.json").read_bytes()
        with self.assertRaises(ValueError), sk.writer(self.state_dir, OWNER) as state:
            sk.record(state, {"kind": "command", "target": LEAD, "task": "fixture"}, NOW)
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def test_simultaneous_writer_refused(self):
        with sk.writer(self.state_dir, OWNER):
            with self.assertRaises(ValueError), sk.writer(self.state_dir, OWNER):
                pass

    def test_state_inside_repo_refused(self):
        (self.root / ".git").mkdir()
        with self.assertRaises(ValueError), sk.writer(self.state_dir, OWNER):
            pass

    def test_corrupt_ledger_not_overwritten(self):
        self.state_dir.mkdir()
        (self.state_dir / "state.json").write_text("broken")
        with self.assertRaises(ValueError), sk.writer(self.state_dir, OWNER):
            pass
        self.assertEqual((self.state_dir / "state.json").read_text(), "broken")

    def test_events_bounded_but_records_retained(self):
        self.enroll()
        for n in range(250):
            sk.event(self.state, "observe", "roster", n)
        self.assertEqual(len(self.state["events"]), 200)
        self.assertIn("enroll-a", self.state["records"])

    def test_enrollment_requires_grant_ack_and_immutable_scope(self):
        sk.record(self.state, {"id": "e", "kind": "enrollment", "target": LEAD,
                              "skipper": OWNER, "scope": "assign"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "e", "kind": "enrollment", "state": "authorized"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "e", "kind": "enrollment", "scope": "broader"}, NOW)
        self.assertFalse(sk.enrolled(self.state, LEAD))

    def test_command_lifecycle_enrollment_reason_and_evidence(self):
        item = {"id": "c", "kind": "command", "target": LEAD, "task": "example"}
        with self.assertRaises(ValueError):
            sk.record(self.state, item, NOW)
        self.enroll()
        with self.assertRaises(ValueError):
            sk.record(self.state, dict(item, irreversible=True), NOW)
        sk.record(self.state, item, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "c", "kind": "command", "state": "done"}, NOW)
        sk.record(self.state, {"id": "c", "kind": "command", "state": "accepted", "ack_ref": "ack"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "c", "kind": "command", "task": "different"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "c", "kind": "command", "state": "done"}, NOW)
        sk.record(self.state, {"id": "c", "kind": "command", "state": "done", "evidence": "sha"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "c", "kind": "command", "state": "accepted"}, NOW)

    def test_question_answer_and_ack(self):
        sk.record(self.state, {"id": "q", "kind": "question", "target": LEAD,
                              "question": "Choose?", "options": ["a", "b"],
                              "pair_recommendation": "a", "dissent": "b"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "q", "kind": "question", "state": "answered", "answer": "a"}, NOW)
        sk.record(self.state, {"id": "q", "kind": "question", "state": "answered",
                              "answer": "a", "user_answer_ref": "user"}, NOW)
        sk.record(self.state, {"id": "q", "kind": "question", "state": "acked", "ack_ref": "ack"}, NOW)
        self.assertEqual(self.state["records"]["q"]["dissent"], "b")

    def test_handover_requires_successor_grant_and_predecessor_stop(self):
        successor = "claude:00000000-0000-4000-8000-000000000003"
        self.enroll()
        sk.record(self.state, {"id": "c", "kind": "command", "target": LEAD, "task": "example"}, NOW)
        sk.record(self.state, {"id": "h", "kind": "handover", "predecessor": LEAD,
                              "successor": successor, "task": "example"}, NOW)
        sk.record(self.state, {"id": "h", "kind": "handover", "state": "ready", "handover_ref": "file"}, NOW)
        sk.record(self.state, {"id": "h", "kind": "handover", "state": "acked", "successor_ack_ref": "ack"}, NOW)
        transfer = {"id": "h", "kind": "handover", "state": "transferred", "predecessor_stop_ref": "stop"}
        with self.assertRaises(ValueError):
            sk.record(self.state, transfer, NOW)
        self.enroll(successor, "enroll-b")
        self.assertEqual(sk.record(self.state, transfer, NOW)["state"], "transferred")
        self.assertEqual(self.state["records"]["c"]["state"], "superseded")
        sk.record(self.state, {"id": "next", "kind": "command", "target": successor, "task": "example"}, NOW)
        with self.assertRaises(ValueError):
            sk.record(self.state, {"id": "old", "kind": "command", "target": LEAD, "task": "example"}, NOW)


class BarrierTests(FixtureCase):
    def test_revocation_of_active_participant_requires_replacement(self):
        self.request()
        sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="ack")
        sk.barrier(self.state, "active", "hold", OWNER, NOW)
        before = json.dumps(self.state, sort_keys=True)
        with self.assertRaisesRegex(ValueError, "replace-participant"):
            sk.record(self.state, {"id": "enroll-a", "kind": "enrollment", "state": "revoked"}, NOW)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)

    def replace_setup(self, active=True):
        successor = "claude:00000000-0000-4000-8000-000000000003"
        self.request()
        self.enroll(successor, "enroll-b")
        if active:
            sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="old-ack")
            sk.barrier(self.state, "active", "hold", OWNER, NOW)
        return successor

    def test_replace_without_ack_stays_held_until_successor_ack(self):
        successor = self.replace_setup()
        row = sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 1,
                         participant=LEAD, successor=successor, reason="handover")
        self.assertEqual((row["participants"], row["state"], row["acks"]), ([successor], "requested", {}))
        sk.record(self.state, {"id": "enroll-a", "state": "revoked"}, NOW + 2)
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "active", "hold", OWNER, NOW + 2)
        sk.barrier(self.state, "ack", "hold", OWNER, NOW + 3, participant=successor, inflight=[], ack_ref="new-ack")
        self.assertEqual(sk.barrier(self.state, "active", "hold", OWNER, NOW + 4)["state"], "active")

    def test_replace_with_quiescent_ack_preserves_active_and_event(self):
        successor = self.replace_setup()
        row = sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 1,
                         participant=LEAD, successor=successor, reason="handover", ack_ref="new-ack", inflight=[])
        self.assertEqual((row["state"], row["participants"]), ("active", [successor]))
        self.assertEqual(row["acks"], {successor: {"ref": "new-ack", "inflight": [], "ts": NOW + 1}})
        event = self.state["events"][-1]
        self.assertEqual((event["kind"], event["old"], event["new"], event["reason"]),
                         ("barrier-replace-participant", LEAD, successor, "handover"))
        sk.record(self.state, {"id": "enroll-a", "state": "revoked"}, NOW + 2)
        self.assertEqual(row["state"], "active")
        with self.assertRaisesRegex(ValueError, "replace-participant"):
            sk.record(self.state, {"id": "enroll-b", "state": "revoked"}, NOW + 3)

    def test_replace_never_auto_activates_requested_or_busy_holds(self):
        successor = self.replace_setup(active=False)
        row = sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 1,
                         participant=LEAD, successor=successor, reason="handover", ack_ref="ack", inflight=[])
        self.assertEqual(row["state"], "requested")
        sk.barrier(self.state, "active", "hold", OWNER, NOW + 2)
        row = sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 3,
                         participant=successor, successor=LEAD, reason="return",
                         ack_ref="busy-ack", inflight=["push pending"])
        self.assertEqual(row["state"], "requested")
        self.assertEqual(row["acks"][LEAD]["inflight"], ["push pending"])
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "active", "hold", OWNER, NOW + 4)

    def test_replace_preserves_other_participants_and_acknowledgments(self):
        successor = self.replace_setup(active=False)
        other = "claude:00000000-0000-4000-8000-000000000004"
        self.enroll(other, "enroll-other")
        sk.barrier(self.state, "request", "multi", OWNER, NOW, "repo#main", "merges", [LEAD, other], "release")
        for member in (LEAD, other):
            sk.barrier(self.state, "ack", "multi", OWNER, NOW, participant=member, inflight=[], ack_ref=member + "-ack")
        sk.barrier(self.state, "active", "multi", OWNER, NOW)
        unchanged = dict(self.state["barriers"]["multi"]["acks"][other])
        row = sk.barrier(self.state, "replace-participant", "multi", OWNER, NOW + 1,
                         participant=LEAD, successor=successor, reason="handover", ack_ref="new-ack", inflight=[])
        self.assertEqual(row["participants"], [successor, other])
        self.assertEqual(row["acks"][other], unchanged)
        self.assertEqual((row["state"], row["resource"], row["scope"], row["reason"]),
                         ("active", "repo#main", "merges", "release"))

    def test_replace_refuses_invalid_identity_evidence_and_released_hold(self):
        successor = self.replace_setup()
        before = json.dumps(self.state, sort_keys=True)
        options = {"participant": LEAD, "successor": successor, "reason": "handover"}
        for changes in ({"participant": successor}, {"successor": LEAD}, {"successor": "invalid"},
                        {"reason": ""}, {"reason": " "}, {"reason": None}, {"ack_ref": "partial"},
                        {"inflight": []}, {"ack_ref": "", "inflight": []}, {"ack_ref": "ref", "inflight": "[]"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 1, **dict(options, **changes))
            self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "replace-participant", "hold", SID, NOW + 1, **options)
        for changes in ({"state": "requested"}, {"skipper": SID}):
            self.state["records"]["enroll-b"].update(changes)
            snapshot = json.dumps(self.state, sort_keys=True)
            with self.assertRaises(ValueError):
                sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 1, **options)
            self.assertEqual(json.dumps(self.state, sort_keys=True), snapshot)
            self.state = json.loads(before)
        sk.barrier(self.state, "request", "two", OWNER, NOW, "repo#main", "merges", [LEAD, successor], "release")
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "replace-participant", "two", OWNER, NOW + 1, **options)
        sk.barrier(self.state, "release", "hold", OWNER, NOW + 2, reason="finished")
        before = json.dumps(self.state, sort_keys=True)
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "replace-participant", "hold", OWNER, NOW + 3, **options)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)

    def request(self):
        self.enroll()
        return sk.barrier(self.state, "request", "hold", OWNER, NOW, "example#main", "merges", [LEAD], "release")

    def test_ack_inflight_does_not_activate(self):
        self.request()
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "active", "hold", OWNER, NOW)
        sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=["merge-a"], ack_ref="ack")
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "active", "hold", OWNER, NOW)
        sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="clear")
        self.assertEqual(sk.barrier(self.state, "active", "hold", OWNER, NOW)["state"], "active")

    def test_no_expiry_and_explicit_release(self):
        row = self.request()
        sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="ack")
        sk.barrier(self.state, "active", "hold", OWNER, NOW + 100000000)
        self.assertEqual(row["state"], "active")
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "release", "hold", OWNER, NOW)
        sk.barrier(self.state, "release", "hold", OWNER, NOW, reason="finished")
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="late")

    def test_unenrolled_missing_ack_and_reused_id_refused(self):
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "request", "hold", OWNER, NOW, "r", "merges", [LEAD], "reason")
        self.request()
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, ack_ref="ack")
        with self.assertRaises(ValueError):
            sk.barrier(self.state, "request", "hold", OWNER, NOW, "r", "pushes-ci", [LEAD], "reason")


class CLITests(FixtureCase):
    def test_replace_cli_and_revoke_guard_are_atomic(self):
        successor = "claude:00000000-0000-4000-8000-000000000003"
        self.enroll()
        self.enroll(successor, "enroll-b")
        sk.barrier(self.state, "request", "hold", OWNER, NOW, "repo#main", "merges", [LEAD], "release")
        sk.barrier(self.state, "ack", "hold", OWNER, NOW, participant=LEAD, inflight=[], ack_ref="ack")
        sk.barrier(self.state, "active", "hold", OWNER, NOW)
        self.write(self.state_dir / "state.json", self.state)
        revoke = self.write(self.root / "revoke.json", {"id": "enroll-a", "state": "revoked"})
        before = (self.state_dir / "state.json").read_bytes()
        code, _, err = self.invoke(["--owner", OWNER, "record", "--file", str(revoke)])
        self.assertEqual(code, 2)
        self.assertIn("replace-participant", err)
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)
        code, output, err = self.invoke(["--owner", OWNER, "barrier", "replace-participant", "--id", "hold",
                                        "--participant", LEAD, "--successor", successor, "--reason", "handover",
                                        "--ack-ref", "new-ack", "--inflight", "[]"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["state"], "active")
        self.assertEqual(self.invoke(["--owner", OWNER, "record", "--file", str(revoke)])[0], 0)

    def test_env_state_directory_and_explicit_override(self):
        other = self.root / "env-state"
        env = {"SKIPPER_STATE_DIR": str(other)}
        code, _, err = self.invoke(["--owner", OWNER, "status", "--save"], env, explicit_state=False)
        self.assertEqual(code, 0, err)
        self.assertTrue((other / "state.json").exists())
        self.assertFalse(self.state_dir.exists())
        code, _, err = self.invoke(["--owner", OWNER, "status", "--save"], env)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.state_dir / "state.json").exists())

    def test_readonly_status_names_owner_and_path_without_breaking_json(self):
        self.write(self.state_dir / "state.json", self.state)
        before = (self.state_dir / "state.json").read_bytes()
        for args in (["status"], ["status", "--json"]):
            with self.subTest(args=args):
                code, output, err = self.invoke(args)
                self.assertEqual(code, 0, err)
                self.assertIn(OWNER, err)
                self.assertIn(str(self.state_dir.resolve()), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                if "--json" in args:
                    self.assertIn("sessions", json.loads(output))
        self.assertEqual((self.state_dir / "state.json").read_bytes(), before)

    def test_resolve_cli_is_readonly_and_returns_observation_row(self):
        self.write(self.claude / "sessions/1.json", {"sessionId": SID, "name": "lead-a", "pid": 1})
        with mock.patch.object(sk, "liveness", return_value="live"), \
                mock.patch.object(sk, "repo", return_value={"root": "/example/repo-a", "remote": None}):
            code, text, err = self.invoke(["resolve", "lead-a", "--runtime", "claude", "--json"])
            self.assertEqual(code, 0, err)
            row = json.loads(text)
            self.assertEqual(row["id"], LEAD)
            self.assertEqual(row["repo"]["root"], "/example/repo-a")
            code, text, err = self.invoke(["resolve", "lead-a"])
            self.assertEqual(code, 0, err)
            self.assertEqual(text.strip(), SID)
        self.assertFalse(self.state_dir.exists())

    def test_routine_saved_refresh_lists_only_new_sessions(self):
        self.write(self.claude / "sessions/1.json", {"sessionId": SID, "name": "lead-a", "pid": 1})
        with mock.patch.object(sk, "liveness", return_value="live"), \
                mock.patch.object(sk, "repo", return_value={"root": "/example/repo-a", "remote": None}):
            args = ["--owner", OWNER, "status", "--save"]
            code, first, err = self.invoke(args)
            self.assertEqual(code, 0, err)
            self.assertIn("new: lead-a (repo-a)", first)
            code, second, err = self.invoke(args)
            self.assertEqual(code, 0, err)
            self.assertNotIn("new:", second)
            other = "00000000-0000-4000-8000-000000000003"
            self.write(self.claude / "sessions/2.json", {"sessionId": other, "name": "lead-b", "pid": 2})
            before = (self.state_dir / "state.json").read_bytes()
            code, inspection, err = self.invoke(["status"])
            self.assertEqual(code, 0, err)
            self.assertIn("new: lead-b (repo-a)", inspection)
            self.assertEqual((self.state_dir / "state.json").read_bytes(), before)
            code, third, err = self.invoke(args)
            self.assertEqual(code, 0, err)
            footers = [line for line in third.splitlines() if line.startswith("new:")]
            self.assertEqual(footers, ["new: lead-b (repo-a)"])
            self.assertNotIn("new:", self.invoke(args)[1])

    def test_status_all_and_verbose_flags(self):
        code, table, err = self.invoke(["status", "--all", "--verbose"])
        self.assertEqual(code, 0, err)
        self.assertIn("| buddy |", table)

    def invoke(self, args, env=None, explicit_state=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sk, "find_peers", return_value=None), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude),
                                            "CC_STATUSLINE_CTX_EXPORT_DIR": str(self.exports),
                                            "XDG_STATE_HOME": str(self.root / "xdg-state"),
                                            **(env or {})}, clear=True):
            state_args = ["--state-dir", str(self.state_dir)] if explicit_state else []
            code = sk.main(["--home", str(self.home)] + state_args + args)
        return code, out.getvalue(), err.getvalue()

    def test_observe_readonly_and_status_save_baseline(self):
        self.assertEqual(self.invoke(["observe"])[0], 0)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(self.invoke(["--owner", OWNER, "status", "--save"])[0], 0)
        self.assertTrue((self.state_dir / "state.json").exists())

    def test_mutations_require_owner(self):
        code, _, err = self.invoke(["barrier", "request"])
        self.assertEqual(code, 2)
        self.assertIn("owner", err)

    def test_cli_bad_json_preserves_ledger(self):
        file = self.root / "bad.json"
        file.write_text("broken")
        self.assertEqual(self.invoke(["--owner", OWNER, "record", "--file", str(file)])[0], 2)
        self.assertFalse((self.state_dir / "state.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
