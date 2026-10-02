"""Regression coverage for upgrade."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid as uuidlib
from .support import (
    Base,
    HERE,
    PEERS,
    append,
    ev,
    sp_lifecycle,
    sp_protocol,
    sp_runtime,
    sp_storage,
    user_item,
    wait_for,
)


class TestCodeDigest(Base):
    def test_default_runtime_files_and_loaded_digest_use_the_launcher(self):
        files = sp_runtime.runtime_code_files()
        self.assertEqual(files[0], os.path.realpath(str(PEERS)))
        self.assertEqual(sp_runtime.LOADED_CODE_DIGEST,
                         sp_runtime.code_digest(sp_runtime.runtime_code_files(PEERS)))

    def test_identical_installs_and_symlink_projections_share_a_digest(self):
        a, b = self.root / "a", self.root / "b"
        a.mkdir()
        b.mkdir()
        for directory in (a, b):
            (directory / "peers.py").write_text("print('same')\n")
            (directory / "session_peers").mkdir()
            (directory / "session_peers/leaf.py").write_text("VALUE = 1\n")
        (self.root / "linked.py").symlink_to(a / "peers.py")
        digest = sp_runtime.code_digest(sp_runtime.runtime_code_files(a / "peers.py"))
        self.assertEqual(digest, sp_runtime.code_digest(sp_runtime.runtime_code_files(b / "peers.py")))
        self.assertEqual(digest, sp_runtime.code_digest(sp_runtime.runtime_code_files(self.root / "linked.py")))
        (b / "session_peers/leaf.py").write_text("VALUE = 2\n")
        self.assertNotEqual(digest, sp_runtime.code_digest(sp_runtime.runtime_code_files(b / "peers.py")))
        self.assertIsNone(sp_runtime.code_digest([self.root / "absent.py"]))

    def test_missing_malformed_and_wrong_pid_state_are_unknown(self):
        tid, _ = self.one_thread()
        for state in ({}, {"shim_pid": 99, "code_digest": "a" * 64},
                      {"shim_pid": os.getpid()},
                      {"shim_pid": os.getpid(), "code_digest": "invalid"}):
            sp_runtime.write_json_atomic(sp_storage.thread_state_path(tid), state)
            self.assertEqual(sp_lifecycle.shim_code_status(tid, os.getpid(), "a" * 64), "unknown")
        sp_runtime.write_json_atomic(sp_storage.thread_state_path(tid), {"shim_pid": os.getpid(), "code_digest": "a" * 64})
        self.assertEqual(sp_lifecycle.shim_code_status(tid, os.getpid(), "a" * 64), "current")
        self.assertEqual(sp_lifecycle.shim_code_status(tid, os.getpid(), "b" * 64), "stale")
        self.assertEqual(sp_lifecycle.shim_code_status(tid, os.getpid(), None), "unknown")

    def test_list_and_doctor_report_stale_autoattached_code_without_resetting_it(self):
        tid, _ = self.one_thread()
        self.hold_pidfile(tid, pid=os.getpid())
        state = {"shim_pid": os.getpid(), "code_digest": "0" * 64,
                 "budgets": {"owner": 3}, "held": {"owner": {"text": "private"}}}
        sp_runtime.write_json_atomic(sp_storage.thread_state_path(tid), state)
        rc, out, err = self.cli("list", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["codex"][0]["shim_code_status"], "stale")
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("code stale", out)
        self.assertIn("peers.py restart " + tid, out)
        rc, out, err = self.cli("doctor")
        self.assertEqual(rc, 0, err)
        self.assertIn("code stale", out)
        self.assertIn("peers.py restart " + tid, out)
        self.assertEqual(sp_runtime.read_json(sp_storage.thread_state_path(tid)), state)
        self.assertFalse(os.path.exists(sp_storage.budget_reset_path(tid)))


class TestInstalledShimUpgrade(Base):
    def installed_cli(self, script, *args):
        result = subprocess.run([sys.executable, str(script), *args],
                                env=dict(os.environ), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return result.returncode, result.stdout, result.stderr

    def start_installed(self, script, tid):
        log_path = self.root / (script.parent.name + ".log")
        with log_path.open("a") as log:
            proc = subprocess.Popen([sys.executable, str(script), "shim", "--thread", tid],
                                    env=dict(os.environ), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT)
        self._children.append(proc)
        path = sp_storage.thread_state_path(tid)
        ready = wait_for(lambda: (sp_runtime.read_json(path, {}) or {}).get("shim_pid") == proc.pid)
        self.assertTrue(ready, log_path.read_text())
        self.assertTrue(wait_for(lambda: sp_lifecycle.shim_ready(tid) == proc.pid), log_path.read_text())
        return proc

    def test_upgrade_keeps_loaded_code_budget_held_reply_and_cursor_until_restart(self):
        installs = []
        for version in ("A", "B"):
            directory = self.root / ("install" + version)
            shutil.copytree(str(HERE), str(directory),
                            ignore=shutil.ignore_patterns("test*", "peer_tests", "__pycache__"))
            script = directory / "peers.py"
            files = [script] + sorted((directory / "session_peers").glob("*.py"))
            shim_source = next(path for path in files if "\nclass Shim:" in path.read_text())
            source, count = re.subn(
                r'"shim_features": list\((?:[A-Za-z_]\w*\.)?SHIM_FEATURES\),',
                lambda match: '"test_install": "%s", ' % version + match.group(0),
                shim_source.read_text(),
            )
            self.assertEqual(count, 1, "upgrade fixture must match the shim_features state anchor exactly once")
            shim_source.write_text(source)
            if version == "A":
                feature_source = next(path for path in files if "SHIM_FEATURES =" in path.read_text())
                feature_text = feature_source.read_text()
                feature_text, count = re.subn(
                    r'(?m)^(SHIM_FEATURES = \[[^\n]*), "binding_allowance_max500"(\])$',
                    r'\1\2', feature_text,
                )
                self.assertEqual(count, 1,
                                 "upgrade fixture must remove the SHIM_FEATURES max500 anchor exactly once")
                feature_source.write_text(feature_text)
            installs.append(script)
        script_a, script_b = installs
        digest_a = sp_runtime.code_digest(sp_runtime.runtime_code_files(script_a))
        digest_b = sp_runtime.code_digest(sp_runtime.runtime_code_files(script_b))
        self.assertNotEqual(digest_a, digest_b)
        tid, rollout = self.one_thread()
        sid = str(uuidlib.uuid4())
        listener, _ = self.add_listener(name="reviewer", session_id=sid)
        proc_a = self.start_installed(script_a, tid)
        state_path = sp_storage.thread_state_path(tid)
        self.assertEqual(sp_runtime.read_json(state_path)["code_digest"], digest_a)
        # Replace files under an already-running A, as an installer does, while
        # B remains a separate install through which commands and restart run.
        files_b = [script_b] + sorted((script_b.parent / "session_peers").glob("*.py"))
        for source in files_b:
            destination = script_a.parent / source.relative_to(script_b.parent)
            destination.write_bytes(source.read_bytes())
        rc, out, err = self.installed_cli(script_b, "list", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["codex"][0]["shim_code_status"], "stale")
        rc, _, err = self.installed_cli(script_b, "buddy", "set", "codex:" + tid,
                                      "--as", "cc:" + sid, "--replies", "100")
        self.assertEqual(rc, 1, err)  # Old A lacks the >20 capability.
        rc, _, err = self.installed_cli(script_b, "buddy", "set", "codex:" + tid,
                                      "--as", "cc:" + sid, "--replies", "3")
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_for(lambda: sp_runtime.read_json(state_path).get("binding")))

        def turn(number):
            turn_id = "upgrade-%d" % number
            tag = sp_protocol.build_tag("reviewer", sid, listener.path, "m%d" % number)
            append(rollout, ev("task_started", turn_id=turn_id),
                   user_item(tag + "\nping"),
                   ev("task_complete", turn_id=turn_id, last_agent_message="answer%d" % number))
            return turn_id

        for number in range(1, 5):
            turn_id = turn(number)
            self.assertTrue(wait_for(lambda: turn_id in sp_runtime.read_json(state_path).get("processed_turns", [])))
        state_a = sp_runtime.read_json(state_path)
        self.assertEqual(state_a["test_install"], "A")
        self.assertEqual(state_a["code_digest"], digest_a)
        self.assertEqual(state_a["budgets"][sid], 3)
        self.assertEqual(state_a["binding"]["spent"], 3)
        self.assertEqual(state_a["held"][sid]["text"], "answer4")
        self.assertEqual(len([f for f in listener.of_type("user") if f.get("from")]), 3)
        proc_a.terminate()
        proc_a.wait(timeout=5)
        proc_b = self.start_installed(script_b, tid)
        state_b = sp_runtime.read_json(state_path)
        self.assertEqual(state_b["test_install"], "B")
        self.assertEqual(state_b["code_digest"], digest_b)
        for key in ("budgets", "binding", "held", "processed_turns", "tail"):
            self.assertEqual(state_b[key], state_a[key], key)
        self.assertTrue(sp_lifecycle.shim_supports(tid, proc_b.pid, "binding_allowance_max500"))
        rc, out, err = self.installed_cli(script_b, "list", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["codex"][0]["shim_code_status"], "current")
        proc_b.terminate()
        proc_b.wait(timeout=5)
        last_turn = turn(5)  # A recent completion while no shim is running.
        self.start_installed(script_b, tid)
        self.assertTrue(wait_for(lambda: last_turn in sp_runtime.read_json(state_path).get("processed_turns", [])))
        recovered = sp_runtime.read_json(state_path)
        self.assertEqual(recovered["binding"]["spent"], 3)
        self.assertEqual(recovered["budgets"][sid], 3)
        self.assertEqual(recovered["held"][sid]["text"], "answer5")
        self.assertGreater(recovered["tail"]["cursor"], state_a["tail"]["cursor"])
        rc, _, err = self.installed_cli(script_b, "budget", "reset", tid)
        self.assertEqual(rc, 0, err)
        self.assertTrue(wait_for(lambda: len([f for f in listener.of_type("user") if f.get("from")]) == 4))
        self.assertEqual(sp_runtime.read_json(state_path)["binding"]["spent"], 3)
        self.assertFalse(sp_runtime.read_json(state_path)["held"])
