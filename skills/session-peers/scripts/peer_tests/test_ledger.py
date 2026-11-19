"""Real local Git ledgers; no live agents, network, or user notes."""

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from session_peers import ledger, cli, buddy, channels, journal

ME = "cc:11111111-1111-4111-8111-111111111111"
OTHER = "codex:22222222-2222-4222-8222-222222222222"
SCRIPT = Path(ledger.__file__).parents[1] / "peers.py"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peers-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "project"
        self.repo.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.base / "data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        ledger.git(self.repo, "init", "--quiet", "--template=", "--initial-branch=main")
        ledger.git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "fixture", author="fixture")
        self.book = ledger.Ledger(self.repo)
        self.owner = {"id": ME, "name": "fixture lead"}
        self.book.init(self.owner)

    def args(self, **kwargs):
        return argparse.Namespace(slug="work", title="Owed check", body_file=None,
                                  append_file=None, status=None, link=None, lease="24h", **kwargs)

    def add(self, slug="work", body=""):
        args = self.args()
        args.slug = slug
        if body:
            path = self.base / "body.md"
            path.write_text(body)
            args.body_file = str(path)
        return self.book.mutate("add", args, self.owner)

    def item(self, slug="work"):
        head, _meta = self.book.snapshot()
        return self.book.read(head, slug)

    def run_cli(self, *args, check=True, cwd=None):
        result = subprocess.run([sys.executable, str(SCRIPT), "ledger", *args],
                                cwd=str(cwd or self.repo), env=os.environ.copy(),
                                capture_output=True, text=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_real_git_worktrees_bare_layout_and_symlinks_share_identity(self):
        wt = self.base / "linked"
        ledger.git(self.repo, "worktree", "add", "--detach", str(wt))
        link = self.base / "alias"
        link.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(ledger.Ledger(wt).key, self.book.key)
        self.assertEqual(ledger.Ledger(link).key, self.book.key)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(wt / "inside")}):
            with self.assertRaisesRegex(ledger.LedgerError, "outside the project"):
                ledger.Ledger(wt)
        root = self.base / "bare-layout"
        root.mkdir()
        subprocess.run(["git", "clone", "--bare", "--template=", str(self.repo), str(root / ".bare")],
                       env=ledger.git_env(), capture_output=True, check=True, timeout=5)
        ledger.git(root / ".bare", "worktree", "add", "--detach", str(root / "main"))
        self.assertEqual(ledger.anchor_for(root / "main"), root)
        self.assertEqual(ledger.anchor_for(root / ".bare"), root)
        self.assertEqual(ledger.default_key(Path("/a space/ødd")), "-a-space--dd")
        custom = self.base / "git-storage"
        custom.mkdir()
        ledger.git(custom, "init", "--bare", "--template=")
        self.assertEqual(ledger.anchor_for(custom), custom)

    def test_collision_and_explicit_override(self):
        first, second = self.base / "a-b/c", self.base / "a/b-c"
        for directory in (first, second):
            directory.mkdir(parents=True)
            ledger.git(directory, "init", "--quiet", "--template=")
        a, b = ledger.Ledger(first), ledger.Ledger(second)
        self.assertEqual(a.key, b.key)
        a.init(self.owner)
        with self.assertRaisesRegex(ledger.LedgerError, "anchor mismatch.*stored.*current.*--repo-key"):
            b.snapshot()
        separate = ledger.Ledger(second, "second-project")
        separate.init(self.owner)
        self.assertNotEqual(a.path, separate.path)
        # An override selects a ledger; it never bypasses attribution.
        with self.assertRaisesRegex(ledger.LedgerError, "anchor mismatch"):
            ledger.Ledger(second, a.key).snapshot()

    def test_relocate_round_trip_preserves_history(self):
        self.add(body="pending\n")
        original_head = self.book.snapshot()[0]
        moved = self.base / "moved-project"
        self.repo.rename(moved)
        destination = ledger.Ledger(moved)
        self.run_cli("relocate", "--from=" + self.book.key, "--as", ME, "--json", cwd=moved)
        self.assertFalse(self.book.path.exists())
        head, meta = destination.snapshot()
        self.assertEqual(meta["anchor"], str(moved))
        self.assertEqual(destination.read(head, "work")["body"], "pending\n")
        self.assertEqual(ledger.git(destination.path, "rev-parse", head + "^").strip(), original_head)
        moved.rename(self.repo)
        self.book.relocate(destination.key, self.owner)
        self.assertEqual(self.item()["body"], "pending\n")

    def test_read_committed_head_and_refuse_dirty_or_failed_commit(self):
        self.add(body="committed\n")
        path = self.book.path / "items/work.md"
        path.write_text(path.read_text().replace("committed", "not committed"))
        self.assertEqual(self.item()["body"], "committed\n")
        with self.assertRaisesRegex(ledger.LedgerError, "uncommitted files.*preserved"):
            self.book.mutate("done", self.args(), self.owner)
        ledger.git(self.book.path, "add", "--", "items/work.md")
        ledger.git(self.book.path, "commit", "--quiet", "-m", "manual reconcile", author="fixture")
        real_git = ledger.git

        def fail(directory, *args, **kwargs):
            if args[0] == "commit":
                raise ledger.LedgerError("ambiguous commit; files preserved")
            return real_git(directory, *args, **kwargs)

        with mock.patch.object(ledger, "git", side_effect=fail):
            with self.assertRaisesRegex(ledger.LedgerError, "ambiguous"):
                self.book.mutate("done", self.args(), self.owner)
        self.assertEqual(self.item()["status"], "open")
        self.assertIn('status: "done"', path.read_text())
        with self.assertRaisesRegex(ledger.LedgerError, "uncommitted"):
            self.book.mutate("claim", self.args(), self.owner)

    def test_ambiguous_commit_that_landed_is_reobserved(self):
        self.add()
        real_git = ledger.git

        def land_then_error(directory, *args, **kwargs):
            result = real_git(directory, *args, **kwargs)
            if args[0] == "commit":
                raise ledger.LedgerError("commit response lost")
            return result

        with mock.patch.object(ledger, "git", side_effect=land_then_error):
            with self.assertRaisesRegex(ledger.LedgerError, "response lost"):
                self.book.mutate("release", self.args(), self.owner)
        self.assertIsNone(self.item()["owner"])
        self.book.mutate("claim", self.args(), self.owner)
        self.assertEqual(self.item()["owner"], self.owner)

    def test_lease_expiry_does_not_steal_from_live_or_unknown_owner(self):
        with mock.patch.object(ledger, "_now", return_value=1_000_000):
            self.add()
        next_owner = {"id": OTHER, "name": "fixture successor"}
        for state in ("live", "unverified"):
            with self.subTest(state=state), mock.patch.object(channels, "_grant_recorded", return_value=False), \
                    mock.patch.object(channels, "member_liveness", return_value=state), \
                    mock.patch.object(ledger, "_now", return_value=2_000_000):
                with self.assertRaisesRegex(ledger.LedgerError, "expiry is not a handover"):
                    self.book.mutate("claim", self.args(), next_owner)
        with mock.patch.object(channels, "_grant_recorded", return_value=False), \
                mock.patch.object(channels, "member_liveness", return_value="gone"):
            self.book.mutate("claim", self.args(), next_owner)
        for action in ("update", "release", "done"):
            with self.subTest(action=action), self.assertRaisesRegex(ledger.LedgerError, "current owner"):
                self.book.mutate(action, self.args(), self.owner)
        self.assertEqual(self.item()["owner"], next_owner)

    def test_renew_release_handover_and_done_retention(self):
        with mock.patch.object(ledger, "_now", return_value=1_000_000):
            self.add()
        before = self.item()["lease_until"]
        with mock.patch.object(ledger, "_now", return_value=1_000_060):
            self.book.mutate("claim", self.args(), self.owner)
        self.assertGreater(self.item()["lease_until"], before)
        next_owner = {"id": OTHER, "name": "successor"}
        with mock.patch.object(channels, "_grant_recorded", return_value=True):
            self.book.mutate("claim", self.args(), next_owner)
        self.book.mutate("release", self.args(), next_owner)
        with self.assertRaisesRegex(ledger.LedgerError, "claim this item"):
            self.book.mutate("done", self.args(), next_owner)
        self.book.mutate("claim", self.args(), self.owner)
        self.book.mutate("done", self.args(), self.owner)
        self.assertEqual(self.item()["status"], "done")
        self.assertEqual(json.loads(self.run_cli("list", "--json").stdout), [])
        self.assertEqual(len(json.loads(self.run_cli("list", "--all", "--json").stdout)), 1)

    def test_lease_and_path_validation(self):
        for value in ("../outside", "..", "/tmp/out", "a/b", "", ".", "x\ny"):
            with self.subTest(value=value), self.assertRaises(ledger.LedgerError):
                ledger.safe_key(value)
        for value in ("0h", "169h", "-1h", "8d", "240m0"):
            with self.subTest(value=value), self.assertRaises(ledger.LedgerError):
                ledger.lease_end(value)
        self.assertTrue(ledger.lease_end("15m").endswith("Z"))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.repo / "inside")}):
            with self.assertRaisesRegex(ledger.LedgerError, "outside the project"):
                ledger.Ledger(self.repo)

    def test_default_lease_is_one_day_and_explicit_lease_is_at_most_seven_days(self):
        with mock.patch.object(ledger, "_now", return_value=1_000_000):
            item = self.add()
            self.assertEqual(ledger.timestamp(item["lease_until"]), 1_000_000 + 86400)
            args = cli.build_parser().parse_args(["ledger", "claim", "work"])
            self.assertEqual(args.lease, "24h")
            self.assertEqual(ledger.timestamp(ledger.lease_end("7d")), 1_000_000 + 7 * 86400)
            self.assertEqual(ledger.timestamp(ledger.lease_end("168h")), 1_000_000 + 7 * 86400)
            with self.assertRaises(ledger.LedgerError):
                ledger.lease_end("10081m")

    def test_import_uses_first_h1_title_and_preserves_document(self):
        source = self.base / "titled-source"
        source.mkdir()
        body = "## Context\r\n# Pending rollout\r\n# Later heading\r\nKeep original text.\r\n"
        (source / "rollout.md").write_bytes(body.encode("utf-8"))
        (source / "untitled.md").write_text("## Not a title\nKeep this too.\n")
        self.book.mutate("import-dir", argparse.Namespace(directory=str(source)), self.owner)
        self.assertEqual(self.item("rollout")["title"], "Pending rollout")
        self.assertEqual(self.item("rollout")["body"], body)
        self.assertEqual(self.item("untitled")["title"], "untitled")

    def test_inbox_handover_json_stays_json_when_ledger_has_items(self):
        self.add()
        grant = {"type": "handover", "from": ME, "to": OTHER, "through_seq": 1}
        self.assertIsNotNone(ledger.summary(self.repo))
        with mock.patch.object(journal, "caller", return_value=ME), \
                mock.patch.object(journal, "handover", return_value=grant), \
                mock.patch.object(journal, "_write_receipt"), \
                mock.patch.object(ledger, "print_summary") as reminder, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["inbox", "handover", "--to", OTHER, "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), grant)
        reminder.assert_not_called()

    def test_no_remote_and_inherited_git_config_cannot_redirect_or_execute(self):
        marker = self.base / "hook-ran"
        hookdir = self.base / "hooks"
        hookdir.mkdir()
        hook = hookdir / "pre-commit"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 1\n")
        hook.chmod(0o755)
        global_config = self.base / "global-config"
        global_config.write_text("[core]\n hooksPath = " + str(hookdir) + "\n[remote \"foreign\"]\n url = https://example.invalid/x\n")
        injected = {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_DIR": "/nonexistent",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
                    "GIT_CONFIG_VALUE_0": str(hookdir)}
        # Local settings are not covered by ignoring global configuration.
        ledger.git(self.book.path, "config", "core.hooksPath", str(hookdir))
        ledger.git(self.book.path, "config", "commit.gpgsign", "true")
        ledger.git(self.book.path, "config", "gpg.program", "/nonexistent-signing-helper")
        with mock.patch.dict(os.environ, injected):
            self.add()
            self.book.snapshot()
        self.assertFalse(marker.exists())
        self.assertEqual(ledger.git(self.book.path, "remote"), "")
        ledger.git(self.book.path, "config", "remote.forbidden.url", "https://example.invalid/x")
        for call in (self.book.snapshot, lambda: self.book.mutate("done", self.args(), self.owner)):
            with self.assertRaisesRegex(ledger.LedgerError, "configured remote"):
                call()
        self.assertFalse(marker.exists())

    def test_symlink_ledger_and_item_are_refused(self):
        self.add()
        path = self.book.path / "items/work.md"
        path.unlink()
        path.symlink_to(self.base / "outside.md")
        with self.assertRaises(ledger.LedgerError):
            self.book.mutate("done", self.args(), self.owner)
        alternate = ledger.Ledger(self.repo, "linked-ledger")
        alternate.path.symlink_to(self.book.path, target_is_directory=True)
        with self.assertRaisesRegex(ledger.LedgerError, "own local Git repository"):
            alternate.snapshot()

    def test_import_is_one_commit_preserves_original_and_refuses_collisions(self):
        source = self.base / "source"
        source.mkdir()
        original = "---\r\ntitle: Legacy note\r\nowner: old\r\n---\r\n## Grants\r\nPointer only.\r\n"
        (source / "one.md").write_bytes(original.encode("utf-8"))
        (source / "README.md").write_text("not an item")
        (source / "REPO.md").write_text("not an item")
        args = argparse.Namespace(directory=str(source))
        before = self.book.snapshot()[0]
        self.assertEqual(self.book.mutate("import-dir", args, self.owner), {"imported": 1})
        item = self.item("one")
        self.assertEqual(item["body"], original)
        self.assertIsNone(item["owner"])
        self.assertEqual(item["provenance"]["file"], str(source / "one.md"))
        after = self.book.snapshot()[0]
        self.assertEqual(ledger.git(self.book.path, "rev-parse", after + "^").strip(), before)
        (source / "aaa-new.md").write_text("must not partly import")
        with self.assertRaisesRegex(ledger.LedgerError, "collision"):
            self.book.mutate("import-dir", args, self.owner)
        self.assertFalse((self.book.path / "items/aaa-new.md").exists())
        self.assertEqual(self.book.snapshot()[0], after)

    def test_concurrent_add_and_append_do_not_lose_changes(self):
        def launch(*args):
            return subprocess.Popen([sys.executable, str(SCRIPT), "ledger", *args, "--as", ME, "--json"],
                                    cwd=str(self.repo), env=os.environ.copy(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)

        workers = [launch("add", slug, "--title", slug) for slug in ("a", "b")]
        for process in workers:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr + stdout)
        files = [self.base / "append-a", self.base / "append-b"]
        for file, body in zip(files, ("first\n", "second\n")):
            file.write_text(body)
        workers = [launch("update", "a", "--append-file", str(file)) for file in files]
        for process in workers:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertCountEqual(self.item("a")["body"].splitlines(), ["first", "second"])

    def test_summary_and_json_cli(self):
        with mock.patch.object(ledger, "_now", return_value=1_000_000):
            self.add()
            self.add("blocked")
            args = self.args()
            args.slug = "blocked"
            args.status = "blocked"
            self.book.mutate("update", args, self.owner)
            self.book.mutate("release", args, self.owner)
        with mock.patch.object(ledger, "_now", return_value=2_000_000):
            self.assertEqual(ledger.summary(self.repo),
                             "ledger: 2 open (1 unowned, 1 stale): peers.py ledger list")
        payload = json.loads(self.run_cli("list", "--mine", "--as", ME, "--json").stdout)
        self.assertEqual([item["slug"] for item in payload], ["work"])
        self.assertEqual(json.loads(self.run_cli("path", "--json").stdout)["key"], self.book.key)
        self.assertTrue(json.loads(self.run_cli("log", "work", "--json").stdout)[0]["commit"])
        show = self.run_cli("show", "work").stdout
        self.assertIn("\nstatus:", show)
        self.assertEqual(json.loads(self.run_cli("show", "work", "--json").stdout)["slug"], "work")

    def test_large_backlog_uses_one_batch_for_item_bodies(self):
        source = self.base / "backlog"
        source.mkdir()
        for number in range(79):
            (source / ("note-%02d.md" % number)).write_text("Waiting on a checked condition.\n")
        self.book.mutate("import-dir", argparse.Namespace(directory=str(source)), self.owner)
        with mock.patch.object(ledger, "git", wraps=ledger.git) as calls:
            self.assertEqual(ledger.summary(self.repo),
                             "ledger: 79 open (79 unowned, 0 stale): peers.py ledger list")
        batches = [call for call in calls.call_args_list if call.args[1:3] == ("cat-file", "--batch")]
        self.assertEqual(len(batches), 1)

    def test_permissions_and_template_refresh_preserve_repo_rules(self):
        for root, directories, files in os.walk(self.book.path):
            self.assertEqual(Path(root).stat().st_mode & 0o777, 0o700)
            for filename in files:
                self.assertEqual((Path(root) / filename).stat().st_mode & 0o777, 0o600)
        rules = self.book.path / "REPO.md"
        rules.write_text("Local rules.\n")
        readme = self.book.path / "README.md"
        readme.write_text("My generic rules.\n")
        ledger.git(self.book.path, "add", "--", "README.md", "REPO.md")
        ledger.git(self.book.path, "commit", "--quiet", "-m", "local rules", author="fixture")
        self.assertIn("preserved", self.book.init(self.owner)["readme"])
        self.assertEqual(readme.read_text(), "My generic rules.\n")
        self.assertEqual(rules.read_text(), "Local rules.\n")

    def test_bounded_lock_and_summary_fail_silent(self):
        with ledger.locked(self.book.root, [self.book.key]), mock.patch.object(ledger, "LOCK_TIMEOUT", 0):
            with self.assertRaisesRegex(ledger.LedgerError, "busy"):
                self.book.mutate("add", self.args(), self.owner)
        process = mock.Mock(returncode=None, pid=123456)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 1), (b"", b"")]
        with mock.patch.object(subprocess, "Popen", return_value=process), \
                mock.patch.object(ledger.os, "killpg") as killed, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            ledger.print_summary()
        self.assertEqual(output.getvalue(), "")
        killed.assert_called_once_with(123456, ledger.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_no_ledger_list_is_empty_and_does_not_create_data(self):
        uninitialized = ledger.Ledger(self.repo, "no-ledger")
        result = self.run_cli("list", "--repo-key", "no-ledger", "--mine", "--json")
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse(uninitialized.path.exists())


class LedgerReminderTests(unittest.TestCase):
    def test_buddy_reminder_only_in_human_show_and_set_output(self):
        for action in (None, "show", "set", "ping"):
            for machine in (False, True):
                args = argparse.Namespace(buddy_cmd=action, json=machine)
                with mock.patch.object(buddy, "_buddy_line", return_value="buddy fixture"), \
                        mock.patch.object(buddy, "_replies_left", return_value=None), \
                        mock.patch.object(buddy, "_takes_reply_total", return_value=False), \
                        mock.patch.object(ledger, "print_summary") as reminder, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    buddy._print_buddy(args, {}, {})
                self.assertEqual(reminder.call_count, int(not machine and action != "ping"))
                if machine:
                    self.assertIsInstance(json.loads(output.getvalue()), dict)

    def test_handover_reminds_but_does_not_transfer_ledger_owner(self):
        with mock.patch.object(channels, "_caller_or_none", return_value=ME), \
                mock.patch.object(channels, "_grant_recorded", return_value=True), \
                mock.patch.object(channels, "_successor_label", return_value="successor"), \
                mock.patch.object(channels, "_lifecycle_records", return_value=([], True)), \
                mock.patch.object(ledger, "print_summary") as reminder, \
                mock.patch.object(ledger.Ledger, "mutate") as mutation, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(channels.cmd_handover(argparse.Namespace(to=OTHER)), 0)
        reminder.assert_called_once_with()
        mutation.assert_not_called()

    def test_handover_human_reminder_guard_and_current_parser_contract(self):
        with mock.patch.object(channels, "_caller_or_none", return_value=ME), \
                mock.patch.object(channels, "_grant_recorded", return_value=True), \
                mock.patch.object(channels, "_successor_label", return_value="successor"), \
                mock.patch.object(channels, "_lifecycle_records", return_value=([], True)), \
                mock.patch.object(ledger, "print_summary") as reminder, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(channels.cmd_handover(argparse.Namespace(to=OTHER, json=True)), 0)
        reminder.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as refused:
            cli.build_parser().parse_args(["handover", "--to", OTHER, "--json"])
        self.assertEqual(refused.exception.code, 2)
