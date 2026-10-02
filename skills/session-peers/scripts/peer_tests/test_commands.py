"""Regression coverage for commands."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import unittest
from .support import (
    Base,
    HAS_TOMLLIB,
    PEERS,
    sp_config,
    sp_diagnostics,
    sp_hooks,
    sp_lifecycle,
    sp_runtime,
    sp_storage,
    wait_for,
)


class TestVersionPin(Base):
    def test_version_is_newer_compares_dotted_components(self):
        self.assertTrue(sp_runtime.version_is_newer("2.1.264", "2.1.263"))
        self.assertTrue(sp_runtime.version_is_newer("2.2.0", "2.1.263"))
        self.assertFalse(sp_runtime.version_is_newer("2.1.263", "2.1.263"))
        self.assertFalse(sp_runtime.version_is_newer("2.1.200", "2.1.263"))

    def test_version_is_newer_unparseable_never_warns(self):
        self.assertFalse(sp_runtime.version_is_newer("unknown", "2.1.263"))
        self.assertFalse(sp_runtime.version_is_newer(None, "2.1.263"))
        self.assertFalse(sp_runtime.version_is_newer("2.1.263", "garbage"))

    def test_version_pulled_out_of_a_noisy_version_line(self):
        self.assertEqual(sp_runtime.parse_version("codex-cli 0.153.4"), (0, 153, 4))
        self.assertEqual(sp_runtime.parse_version("2.1.263 (Claude Code)"), (2, 1, 263))

    def test_warn_versions_prints_one_line_for_a_newer_install(self):
        self.add_claude_binary("2.9.0 (Claude Code)")
        os.environ["FAKE_CODEX_VERSION"] = "codex-cli 9.0.0"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sp_diagnostics.warn_versions()
        text = err.getvalue()
        self.assertIn("Claude Code 2.9.0", text)
        self.assertIn("codex-cli 9.0.0", text)
        self.assertIn("spike-checklist", text)

        again = io.StringIO()
        with contextlib.redirect_stderr(again):
            sp_diagnostics.warn_versions()
        self.assertEqual(again.getvalue(), "")

    def test_warn_versions_is_silent_on_the_pinned_versions(self):
        self.add_claude_binary("2.1.263 (Claude Code)")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sp_diagnostics.warn_versions()
        self.assertEqual(err.getvalue(), "")

    def test_missing_binaries_never_fail_a_run(self):
        os.environ["PATH"] = str(self.root / "nothing")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sp_diagnostics.warn_versions()
        self.assertEqual(err.getvalue(), "")


class TestDoctorAttachment(Base):
    def _doctor_text(self):
        rc, out, err = self.cli("doctor")
        self.assertEqual(rc, 0)
        return out + err

    def test_doctor_flags_an_unregistered_live_thread_with_no_shim(self):
        # one_thread() is live, holder-backed, unregistered, and un-shimmed —
        # exactly the state that hid 1393-review/skills. The registered loop
        # never walks it, so this new pass must.
        tid, _r = self.one_thread(name="skills")
        text = self._doctor_text()
        self.assertIn("live Codex thread, not attached", text)
        self.assertIn("peers.py up %s" % tid, text)

    def test_a_registered_live_no_shim_thread_warns_exactly_once(self):
        tid, _r = self.one_thread(name="skills")
        sp_storage.write_registered({tid: {"name": "skills"}})
        text = self._doctor_text()
        self.assertIn("thread is live but no shim", text)  # existing loop
        self.assertNotIn("not attached", text)             # new pass stays out

    def test_doctor_does_not_flag_an_unverified_thread_as_unattached(self):
        self.one_thread(name="skills")
        os.environ["FAKE_LSOF_RC"] = "126"
        text = self._doctor_text()
        self.assertNotIn("not attached", text)


class TestSessionHook(Base):
    def test_the_hook_prints_empty_json_for_every_source(self):
        for source in ("startup", "resume", "clear", "compact"):
            rc, out, _err = self.cli(
                "session-hook", stdin=json.dumps({"source": source, "cwd": "/tmp"})
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "{}")

    def test_malformed_stdin_does_not_fail_the_hook(self):
        rc, out, _err = self.cli("session-hook", stdin="not json")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")
        rc, out, _err = self.cli("session-hook", stdin="")
        self.assertEqual(rc, 0)

    def test_four_sources_start_at_most_one_shim_per_thread(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        for source in ("startup", "resume", "clear", "compact"):
            proc = self.spawn("session-hook")
            out, _ = proc.communicate(json.dumps({"source": source}), timeout=20)
            self.assertEqual(out.strip(), "{}")
        pid = wait_for(lambda: sp_lifecycle.shim_pid(tid))
        self.assertIsNotNone(pid, "the reconcile never started a shim")
        time.sleep(1.0)
        self.assertEqual(len(self.shim_records()), 1, self.shim_records())
        self.assertEqual(sp_lifecycle.shim_pid(tid), pid)

    def test_auto_attach_exposes_the_triggering_uuid_without_persisting_it(self):
        tid, _rollout = self.one_thread(name="codex-hook")
        proc = self.spawn("session-hook", "--auto-attach")
        out, _ = proc.communicate(
            json.dumps({"source": "startup", "session_id": tid}), timeout=20
        )
        self.assertEqual(out.strip(), "{}")
        self.assertIsNotNone(wait_for(lambda: sp_lifecycle.shim_pid(tid)))
        self.assertNotIn(tid, sp_storage.read_registered())

    def test_auto_attach_ignores_compaction(self):
        tid, _rollout = self.one_thread(name="codex-hook")
        proc = self.spawn("session-hook", "--auto-attach")
        out, _ = proc.communicate(
            json.dumps({"source": "compact", "session_id": tid}), timeout=20
        )
        self.assertEqual(out.strip(), "{}")
        time.sleep(0.5)
        self.assertIsNone(sp_lifecycle.shim_pid(tid))


class TestInstallHook(Base):
    EXISTING = {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": "third-party-start"}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "third-party-stop"}]}],
    }

    def hooks_path(self):
        return self.codex_dir / "hooks.json"

    def test_the_entry_is_appended_and_every_existing_one_survives(self):
        self.hooks_path().write_text(json.dumps(self.EXISTING))
        rc, out, _err = self.cli("install-hook")
        self.assertEqual(rc, 0)
        data = json.loads(self.hooks_path().read_text())
        commands = [
            h["command"]
            for entry in data["SessionStart"]
            for h in entry["hooks"]
        ]
        self.assertIn("third-party-start", commands)
        self.assertEqual(len(commands), 2)
        self.assertEqual(shlex.split(commands[1]),
                         ["python3", os.path.realpath(str(PEERS)), "session-hook"])
        self.assertTrue(commands[1].startswith("python3 "))
        self.assertEqual(data["SessionStart"][1]["matcher"], "startup|resume")
        self.assertEqual(
            [h["command"] for e in data["Stop"] for h in e["hooks"]],
            ["third-party-stop"],
        )
        self.assertIn("backed up", out)
        backups = list(self.codex_dir.glob("hooks.json.session-peers-bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.EXISTING)

    def test_a_second_install_changes_nothing(self):
        self.hooks_path().write_text(json.dumps(self.EXISTING))
        self.cli("install-hook")
        first = self.hooks_path().read_text()
        rc, out, _err = self.cli("install-hook")
        self.assertEqual(rc, 0)
        self.assertIn("already installed", out)
        self.assertEqual(self.hooks_path().read_text(), first)
        self.assertEqual(len(list(self.codex_dir.glob("hooks.json.*bak*"))), 1)

    def test_a_missing_hooks_file_is_created_in_the_shape_codex_uses(self):
        # P4: the real ~/.codex/hooks.json wraps the event map in "hooks".
        rc, out, _err = self.cli("install-hook")
        self.assertEqual(rc, 0)
        self.assertNotIn("backed up", out)
        data = json.loads(self.hooks_path().read_text())
        self.assertIn("hooks", data)
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["timeout"], 10)
        self.assertEqual(
            data["hooks"]["SessionStart"][0]["matcher"], "startup|resume"
        )
        self.assertNotIn("SessionStart", set(data) - {"hooks"})

    def test_auto_attach_installs_the_uuid_aware_hook(self):
        rc, _out, _err = self.cli("install-hook", "--auto-attach")
        self.assertEqual(rc, 0)
        data = json.loads(self.hooks_path().read_text())
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command),
                         ["python3", os.path.realpath(str(PEERS)), "session-hook", "--auto-attach"])

    def test_reinstall_can_upgrade_manual_reconcile_to_auto_attach(self):
        self.cli("install-hook")
        rc, out, _err = self.cli("install-hook", "--auto-attach")
        self.assertEqual(rc, 0)
        self.assertIn("updated", out)
        data = json.loads(self.hooks_path().read_text())
        self.assertEqual(len(data["hooks"]["SessionStart"]), 1)
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(command.endswith("--auto-attach"))

    def test_upgrade_preserves_a_sibling_handler_in_the_same_group(self):
        grouped = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": sp_hooks.hook_command(),
                                "timeout": 10,
                            },
                            {"type": "command", "command": "third-party"},
                        ]
                    }
                ]
            }
        }
        self.hooks_path().write_text(json.dumps(grouped))
        self.cli("install-hook", "--auto-attach")
        entries = json.loads(self.hooks_path().read_text())["hooks"]["SessionStart"]
        commands = [
            hook["command"] for entry in entries for hook in entry["hooks"]
        ]
        self.assertEqual(commands.count("third-party"), 1)
        self.assertEqual(commands.count(sp_hooks.hook_command(auto_attach=True)), 1)

    def test_same_mode_shared_group_is_already_installed(self):
        grouped = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [
                            sp_hooks.hook_entry()["hooks"][0],
                            {"type": "command", "command": "third-party"},
                        ],
                    }
                ]
            }
        }
        original = json.dumps(grouped)
        self.hooks_path().write_text(original)
        rc, out, _err = self.cli("install-hook")
        self.assertEqual(rc, 0)
        self.assertIn("already installed", out)
        self.assertEqual(self.hooks_path().read_text(), original)
        self.assertEqual(list(self.codex_dir.glob("hooks.json.*bak*")), [])

    def test_mode_upgrade_preserves_extra_entry_and_handler_keys(self):
        entry = sp_hooks.hook_entry()
        entry["description"] = "keep-entry"
        entry["hooks"][0]["statusMessage"] = "keep-handler"
        self.hooks_path().write_text(
            json.dumps({"hooks": {"SessionStart": [entry]}})
        )
        self.cli("install-hook", "--auto-attach")
        updated = json.loads(self.hooks_path().read_text())["hooks"]["SessionStart"][0]
        self.assertEqual(updated["description"], "keep-entry")
        self.assertEqual(updated["hooks"][0]["statusMessage"], "keep-handler")
        self.assertTrue(updated["hooks"][0]["command"].endswith("--auto-attach"))

    def test_the_nested_hooks_shape_is_handled_too(self):
        self.hooks_path().write_text(json.dumps({"hooks": self.EXISTING}))
        self.cli("install-hook")
        data = json.loads(self.hooks_path().read_text())
        self.assertIn("hooks", data)
        self.assertEqual(len(data["hooks"]["SessionStart"]), 2)

    def test_installer_never_changes_the_feature_flag(self):
        config = self.codex_dir / "config.toml"
        config.write_text("[features]\nhooks = false\n")
        self.cli("install-hook")
        self.assertEqual(
            config.read_text(), "[features]\nhooks = false\n"
        )

    def test_the_trust_step_is_printed(self):
        _rc, out, _err = self.cli("install-hook")
        self.assertIn("/hooks", out)


class TestTomlLite(Base):
    def test_sections_keys_and_scalars(self):
        (self.codex_dir / "config.toml").write_text(
            'sqlite_home = "/a/b"  # trailing comment\n'
            "\n"
            "[features]\n"
            "hooks = true\n"
            "count = 3\n"
            "\n"
            '[hooks.state."/x/hooks.json:session_start:0:0"]\n'
            'trusted_hash = "abc"\n'
            "enabled = false\n"
        )
        cfg = sp_config.read_toml_lite(str(self.codex_dir / "config.toml"))
        self.assertEqual(cfg[""]["sqlite_home"], "/a/b")
        self.assertIs(cfg["features"]["hooks"], True)
        self.assertEqual(cfg["features"]["count"], 3)
        key = 'hooks.state."/x/hooks.json:session_start:0:0"'
        self.assertEqual(cfg[key]["trusted_hash"], "abc")
        self.assertIs(cfg[key]["enabled"], False)

    def test_a_missing_file_is_an_empty_table(self):
        self.assertEqual(sp_config.read_toml_lite(str(self.root / "no.toml")), {"": {}})


class TestDoctor(Base):
    def test_doctor_reports_versions_paths_and_tools(self):
        self.add_claude_binary()
        self.one_thread()
        rc, out, _err = self.cli("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("Claude Code 2.1.263", out)
        self.assertIn("codex-cli 0.153.4", out)
        self.assertIn("codex on PATH", out)
        self.assertIn("lsof on PATH", out)
        self.assertIn("process-start probe", out)
        self.assertIn("Unix-socket bind", out)
        self.assertIn(str(self.claude_dir), out)
        self.assertIn(str(self.socks), out)
        self.assertIn("no registered threads", out)

    def test_doctor_names_a_live_thread_without_a_shim(self):
        tid, _rollout = self.one_thread(name="codex-uzi")
        sp_storage.register_thread({"id": tid, "name": "codex-uzi"})
        _rc, out, _err = self.cli("doctor")
        self.assertIn("thread is live but no shim", out)

    def test_doctor_reports_a_trust_block_as_unknown_rather_than_guessing(self):
        hooks_path = self.codex_dir / "hooks.json"
        self.cli("install-hook")
        (self.codex_dir / "config.toml").write_text(
            "[features]\nhooks = true\n\n"
            '[hooks.state."%s:session_start:0:0"]\n'
            'trusted_hash = "deadbeef"\n' % hooks_path
        )
        _rc, out, _err = self.cli("doctor")
        self.assertIn("trust block", out)
        self.assertIn("open /hooks", out)
        self.assertIn("[features] hooks = true", out)

    def test_doctor_flags_an_entry_with_no_trust_block(self):
        self.cli("install-hook")
        (self.codex_dir / "config.toml").write_text("[features]\nhooks = true\n")
        _rc, out, _err = self.cli("doctor")
        self.assertIn("no trust block", out)

    def test_doctor_flags_a_disabled_entry(self):
        hooks_path = self.codex_dir / "hooks.json"
        self.cli("install-hook")
        (self.codex_dir / "config.toml").write_text(
            "[features]\nhooks = true\n\n"
            '[hooks.state."%s:session_start:0:0"]\n'
            "enabled = false\n" % hooks_path
        )
        _rc, out, _err = self.cli("doctor")
        self.assertIn("disabled", out)

    def test_doctor_reports_an_explicit_global_hook_disable(self):
        self.cli("install-hook", "--auto-attach")
        (self.codex_dir / "config.toml").write_text(
            "[features]\nhooks = false\n"
        )
        _rc, out, _err = self.cli("doctor")
        self.assertIn("hooks = false explicitly disables", out)
        self.assertIn("SessionStart auto-attach entry", out)

    def test_doctor_reports_unset_hooks_as_enabled_by_default(self):
        self.cli("install-hook")
        _rc, out, _err = self.cli("doctor")
        self.assertIn("hooks is unset (enabled by default)", out)

    def test_doctor_names_a_blocked_process_probe(self):
        os.environ["FAKE_PS_RC"] = "126"
        os.environ["FAKE_PS_STDERR"] = "operation not permitted"
        _rc, out, _err = self.cli("doctor")
        self.assertIn("fail  process-start probe", out)
        self.assertIn("operation not permitted", out)

    def test_doctor_names_a_blocked_lsof_probe(self):
        self.one_thread(name="codex-uzi")
        os.environ["FAKE_LSOF_RC"] = "126"
        os.environ["FAKE_LSOF_STDERR"] = "operation not permitted"
        with contextlib.redirect_stderr(io.StringIO()):
            _rc, out, _err = self.cli("doctor")
        self.assertIn("fail  Codex liveness probe unavailable", out)
        self.assertIn("operation not permitted", out)
        self.assertIn("bridge GC skipped", out)

    def test_doctor_survives_an_unknown_codex_schema(self):
        self.make_state_db([], filename="state_1.sqlite", good=False)
        rc, out, _err = self.cli("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("recognised", out)

    def test_doctor_reports_missing_tools_without_failing(self):
        os.environ["PATH"] = str(self.root / "empty")
        rc, out, _err = self.cli("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("not on PATH", out)


class TestInstallHookSafety(Base):
    """S6: an unreadable hooks.json must never be replaced."""

    def test_a_corrupt_hooks_file_is_backed_up_and_left_alone(self):
        path = self.codex_dir / "hooks.json"
        original = '{"SessionStart": [ truncated'
        path.write_text(original)
        rc, _out, err = self.cli("install-hook")
        self.assertEqual(rc, 1)
        self.assertEqual(path.read_text(), original)
        self.assertIn("not valid JSON", err)
        self.assertIn("session-hook", err)
        backups = list(self.codex_dir.glob("hooks.json.session-peers-bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)

    def test_an_existing_file_keeps_its_own_mode(self):
        path = self.codex_dir / "hooks.json"
        path.write_text(json.dumps({"SessionStart": []}))
        os.chmod(str(path), 0o644)
        self.cli("install-hook")
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o644)

    def test_a_file_we_create_is_private(self):
        self.cli("install-hook")
        path = self.codex_dir / "hooks.json"
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)


class TestHookCommandQuoting(Base):
    """P8: a path with a space would split into two arguments."""

    def test_a_path_with_a_space_is_quoted(self):
        command = sp_hooks.hook_command("/Users/x/My Skills/peers.py")
        self.assertEqual(
            command, "python3 '/Users/x/My Skills/peers.py' session-hook"
        )
        import shlex as _shlex
        self.assertEqual(
            _shlex.split(command),
            ["python3", "/Users/x/My Skills/peers.py", "session-hook"],
        )

    def test_an_ordinary_path_is_left_unquoted(self):
        self.assertEqual(
            sp_hooks.hook_command("/Users/x/peers.py"),
            "python3 /Users/x/peers.py session-hook",
        )

    def test_the_installed_entry_uses_the_quoted_form(self):
        self.cli("install-hook")
        data = json.loads((self.codex_dir / "hooks.json").read_text())
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(command, sp_hooks.hook_command())


class TestConfigReaderPaths(Base):
    """R3: both readers must agree, and neither may read a string as config."""

    MULTILINE = (
        "developer_instructions = %s\n"
        "Point the bridge at another database like this:\n"
        'sqlite_home = "/tmp/not-a-real-home"\n'
        "[features]\n"
        "hooks = false\n"
        "%s\n"
        "\n"
        'model = "gpt-5"\n'
        "\n"
        "[features]\n"
        "hooks = true\n"
    ) % ('"' * 3, '"' * 3)

    QUOTED_HEADER = '["features"]\nhooks = true\nweb_search = false\n'

    def config(self):
        return self.codex_dir / "config.toml"

    @contextlib.contextmanager
    def lite_reader_only(self):
        """Force the line-reader fallback, as on Python 3.9 and 3.10."""
        real = sp_config._load_tomllib
        sp_config._load_tomllib = lambda: None
        try:
            yield
        finally:
            sp_config._load_tomllib = real

    # -- (a) a key inside a multiline string is not a key ------------------

    def test_the_parser_path_ignores_a_sqlite_home_inside_a_string(self):
        self.config().write_text(self.MULTILINE)
        cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertNotIn("sqlite_home", cfg[""])
        self.assertEqual(cfg[""]["model"], "gpt-5")
        self.assertIs(cfg["features"]["hooks"], True)

    def test_the_line_reader_ignores_a_sqlite_home_inside_a_string(self):
        self.config().write_text(self.MULTILINE)
        with self.lite_reader_only():
            cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertNotIn("sqlite_home", cfg[""])
        self.assertEqual(cfg[""]["model"], "gpt-5")
        self.assertIs(cfg["features"]["hooks"], True)

    def test_neither_reader_lets_a_string_redirect_the_database(self):
        # The bug this pins: reading that example would send every query to a
        # database Codex never writes.
        self.config().write_text(self.MULTILINE)
        self.assertEqual(sp_storage.codex_sqlite_home(), str(self.codex_dir))
        with self.lite_reader_only():
            self.assertEqual(sp_storage.codex_sqlite_home(), str(self.codex_dir))

    # -- (b) a quoted header stores its values under the normalised name ---

    def test_the_parser_path_stores_a_quoted_header_normalised(self):
        self.config().write_text(self.QUOTED_HEADER)
        cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertIn("features", cfg)
        self.assertNotIn('"features"', cfg)
        self.assertIs(cfg["features"]["hooks"], True)
        self.assertIs(cfg["features"]["web_search"], False)

    def test_the_line_reader_stores_a_quoted_header_normalised(self):
        self.config().write_text(self.QUOTED_HEADER)
        with self.lite_reader_only():
            cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertIn("features", cfg)
        self.assertNotIn('"features"', cfg)
        self.assertIs(cfg["features"]["hooks"], True)
        self.assertIs(cfg["features"]["web_search"], False)

    def test_doctor_reads_the_flag_through_a_quoted_header(self):
        self.cli("install-hook")
        self.config().write_text(self.QUOTED_HEADER)
        _rc, out, _err = self.cli("doctor")
        self.assertIn("[features] hooks = true", out)

    # -- the two readers agree, and the fallback is announced --------------

    def test_both_readers_agree_on_a_realistic_config(self):
        hooks_path = self.codex_dir / "hooks.json"
        body = (
            'model = "gpt-5"\n'
            'sqlite_home = "/tmp/dbs"\n'
            "\n"
            "[features] # flags\n"
            "hooks = true\n"
            "count = 3\n"
            "\n"
            '[hooks.state."%s:session_start:0:0"]\n'
            'trusted_hash = "abc"\n'
            "enabled = false\n"
        ) % hooks_path
        self.config().write_text(body)
        parsed = sp_config.read_toml_lite(str(self.config()))
        with self.lite_reader_only():
            lite = sp_config.read_toml_lite(str(self.config()))
        key = 'hooks.state."%s:session_start:0:0"' % hooks_path
        for cfg in (parsed, lite):
            self.assertEqual(cfg[""]["sqlite_home"], "/tmp/dbs")
            self.assertIs(cfg["features"]["hooks"], True)
            self.assertEqual(cfg["features"]["count"], 3)
            self.assertEqual(cfg[key]["trusted_hash"], "abc")
            self.assertIs(cfg[key]["enabled"], False)

    @unittest.skipUnless(HAS_TOMLLIB, "the warning is the parser path handing over; 3.9/3.10 have no parser")
    def test_a_file_that_does_not_parse_yields_nothing_with_a_warning(self):
        # Codex refuses the same file, so a partial read would act on settings
        # that are not in force.
        self.config().write_text('model = "gpt-5"\nthis line is not toml\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertEqual(cfg, {"": {}})
        self.assertIn("does not parse as TOML", err.getvalue())
        self.assertIn("environment and default paths", err.getvalue())

    @unittest.skipUnless(HAS_TOMLLIB, "invalid TOML rejection requires stdlib tomllib")
    def test_with_tomllib_an_invalid_file_cannot_route_the_database(self):
        self.config().write_text(
            'sqlite_home = "%s"\nthis line is not toml\n' % (self.root / "wrong")
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(sp_storage.codex_sqlite_home(), str(self.codex_dir))
            os.environ["CODEX_SQLITE_HOME"] = str(self.root / "from-env")
            self.assertEqual(sp_storage.codex_sqlite_home(), str(self.root / "from-env"))
        self.assertIn("does not parse as TOML", err.getvalue())

    def test_without_tomllib_an_invalid_file_still_reads_line_by_line(self):
        # The line reader is the only reader on 3.9 and 3.10, so it keeps
        # doing its best there rather than returning nothing.
        self.config().write_text('model = "gpt-5"\nthis line is not toml\n')
        with self.lite_reader_only():
            cfg = sp_config.read_toml_lite(str(self.config()))
        self.assertEqual(cfg[""]["model"], "gpt-5")

    def test_without_tomllib_the_database_path_is_best_effort_and_env_is_fallback(self):
        configured = str(self.root / "configured")
        override = str(self.root / "from-env")
        self.config().write_text('sqlite_home = "%s"\nthis line is not toml\n' % configured)
        with self.lite_reader_only():
            self.assertEqual(sp_storage.codex_sqlite_home(), configured)
            os.environ["CODEX_SQLITE_HOME"] = override
            self.assertEqual(sp_storage.codex_sqlite_home(), configured)
            self.config().write_text("this line is not toml\n")
            self.assertEqual(sp_storage.codex_sqlite_home(), override)

    def test_a_missing_file_is_an_empty_table_on_both_paths(self):
        missing = str(self.root / "nope.toml")
        self.assertEqual(sp_config.read_toml_lite(missing), {"": {}})
        with self.lite_reader_only():
            self.assertEqual(sp_config.read_toml_lite(missing), {"": {}})

    def test_a_header_segment_is_quoted_only_when_it_has_to_be(self):
        self.assertEqual(sp_config._toml_key_text("features"), "features")
        self.assertEqual(sp_config._toml_key_text("web_search"), "web_search")
        self.assertEqual(sp_config._toml_key_text("a/b:c"), '"a/b:c"')


class TestCliSurface(Base):
    def test_launcher_finds_its_bundled_package_with_isolated_import_paths(self):
        for options in ([], ["-I"]):
            with self.subTest(options=options):
                env = dict(os.environ)
                env["PYTHONSAFEPATH"] = "1"
                result = subprocess.run(
                    [sys.executable, *options, str(PEERS), "--help"],
                    cwd=str(self.root), env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("session-hook", result.stdout)

    def test_no_subcommand_prints_help(self):
        rc, out, _err = self.cli()
        self.assertEqual(rc, 2)
        self.assertIn("session-hook", out)
        self.assertIn("install-hook", out)

    def test_budget_without_reset_is_an_error(self):
        rc, _out, err = self.cli("budget")
        self.assertEqual(rc, 2)
        self.assertIn("reset", err)

    def test_the_script_is_executable_and_python3(self):
        mode = os.stat(str(PEERS)).st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertTrue(mode & stat.S_IXGRP)
        self.assertTrue(mode & stat.S_IXOTH)
        first = PEERS.read_text().splitlines()[0]
        self.assertEqual(first, "#!/usr/bin/env python3")
