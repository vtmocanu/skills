#!/usr/bin/env python3
"""Reader safety, installer isolation, metadata, and refresh-lock regressions."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import publish_skills
import refresh_skills


def body(version: str) -> bytes:
    return f"---\nname: example\ndescription: Tests readable skill replacement.\n---\n{version}\n".encode()


def entry(source: str = "org/catalog", version: str = "new") -> dict:
    return {"source": source, "sourceType": "github", "skillPath": "skills/example/SKILL.md",
            "computedHash": version * 16}


def stage_skill(cwd: Path, name: str = "example", content: bytes | None = None,
                metadata: dict | None = None) -> None:
    skill = cwd / ".agents" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_bytes(content or body("new"))
    lock_path = cwd / "skills-lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"version": 1, "skills": {}}
    lock["skills"][name] = metadata or entry()
    lock_path.write_text(json.dumps(lock))


def concurrent_worker(root: str, barrier) -> None:
    directory = Path(root)

    def runner(*args, cwd):
        with (directory / "calls.log").open("a") as log:
            log.write(f"start {os.getpid()}\n")
        time.sleep(0.05)
        stage_skill(cwd)
        with (directory / "calls.log").open("a") as log:
            log.write(f"end {os.getpid()}\n")
        return 0

    barrier.wait()
    with redirect_stdout(io.StringIO()):
        result = refresh_skills.refresh(["org/catalog"], [], directory / "refresh.lock", runner,
                                        home=directory / "home", project=directory / "project")
    raise SystemExit(result)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="skills-refresh-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = self.home / ".agents" / "skills"
        self.claude = self.home / ".claude" / "skills"
        self.lock = self.home / ".agents" / ".skill-lock.json"
        self.old = body("old")
        self.new = body("new")
        self.live = self.store / "example"
        self.live.mkdir(parents=True)
        (self.live / "SKILL.md").write_bytes(self.old)
        self.claude.mkdir(parents=True)
        (self.claude / "example").symlink_to(self.live, target_is_directory=True)
        self.initial = {"version": 3, "dismissed": {"notice": True}, "skills": {
            "example": {"source": "org/catalog", "sourceType": "github",
                        "sourceUrl": "https://github.com/org/catalog.git",
                        "skillPath": "skills/example/SKILL.md", "skillFolderHash": "a" * 40,
                        "installedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"}}}
        self.lock.write_text(json.dumps(self.initial))
        self.calls = []

    def runner(self, *args, cwd=None):
        # This assertion fails on the original refresher at the actual bug seam:
        # it asked the installer to modify live global files.
        self.assertNotIn("-g", args, "installer must never target the live global store")
        self.assertNotIn("--global", args)
        self.assertIsNotNone(cwd, "installer must run in an isolated staging project")
        self.assertNotEqual(cwd, self.project)
        self.assertNotEqual(cwd, self.home)
        self.calls.append(args)
        stage_skill(cwd)
        return 0

    def refresh(self, runner=None, sources=("org/catalog",), optional=()):
        # Same entry point as before the fix, so the baseline regression can run
        # against the original implementation without failing on a new API.
        with patch.object(Path, "home", return_value=self.home), \
                patch.object(Path, "cwd", return_value=self.project), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return refresh_skills.refresh(sources, optional, self.root / "refresh.lock", runner or self.runner)

    def test_installer_never_mutates_live_store(self):
        self.assertEqual(self.refresh(), 0)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.new)
        self.assertEqual((self.claude / "example" / "SKILL.md").read_bytes(), self.new)
        self.assertEqual(len(self.calls), 1)

    def test_reader_during_installer_delete_and_copy(self):
        reached_gap, resume = threading.Event(), threading.Event()
        errors = []

        def destructive_installer(*args, cwd):
            stage_skill(cwd)
            staged = cwd / ".agents" / "skills" / "example"
            shutil.rmtree(staged)
            reached_gap.set()
            if not resume.wait(5):
                raise AssertionError("reader did not inspect installation gap")
            stage_skill(cwd)
            return 0

        def writer():
            try:
                self.assertEqual(self.refresh(destructive_installer), 0)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(reached_gap.wait(5))
            for target in (self.live, self.claude / "example"):
                self.assertEqual((target / "SKILL.md").read_bytes(), self.old)
        finally:
            resume.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.new)

    def test_publication_has_no_missing_or_partial_reads(self):
        original_replace = os.replace
        seen = []

        def inspect_before_replace(source, target):
            if Path(target) == self.live / "SKILL.md":
                # Publishing a temp file must leave the prior body complete up
                # to the rename, and make the complete replacement available.
                seen.append(Path(target).read_bytes())
                self.assertEqual(Path(source).read_bytes(), self.new)
            original_replace(source, target)

        with patch.object(publish_skills.os, "replace", side_effect=inspect_before_replace):
            self.assertEqual(self.refresh(), 0)
        self.assertEqual(seen, [self.old])

    def test_reader_stress_sees_only_complete_versions(self):
        stop = threading.Event()
        observed, failures = set(), []

        def reader():
            while not stop.is_set():
                try:
                    value = (self.claude / "example" / "SKILL.md").read_bytes()
                    observed.add(value)
                    if value not in (self.old, self.new):
                        failures.append(value)
                except OSError as error:
                    failures.append(error)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(100):
                publish_skills.publish(self.live, {Path("SKILL.md"): (
                    self.new if index % 2 else self.old, 0o644)})
        finally:
            stop.set()
            thread.join(5)
        self.assertFalse(failures, failures[:3])
        self.assertEqual(observed, {self.old, self.new})

    def test_staging_failure_retains_files_and_lock(self):
        before = self.lock.read_bytes()

        def fail(*args, cwd):
            stage_skill(cwd)
            return 7

        self.assertEqual(self.refresh(fail), 1)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.old)
        self.assertEqual(self.lock.read_bytes(), before)

    def test_incomplete_zero_exit_batch_is_not_published(self):
        def incomplete(*args, cwd):
            stage_skill(cwd)
            stage_skill(cwd, "broken")
            (cwd / ".agents" / "skills" / "broken" / "SKILL.md").unlink()
            return 0

        self.assertEqual(self.refresh(incomplete), 1)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.old)

    def test_optional_failure_does_not_block_required_catalog(self):
        calls = []

        def runner(*args, cwd):
            calls.append(args[1])
            if args[1] == "org/offline":
                return 7
            return self.runner(*args, cwd=cwd)

        self.assertEqual(self.refresh(runner, optional=("org/offline",)), 0)
        self.assertEqual(calls, ["org/catalog", "org/offline"])
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.new)

    def test_required_failure_still_tries_optional_catalog(self):
        calls = []

        def runner(*args, cwd):
            calls.append(args[1])
            if args[1] == "org/catalog":
                return 7
            stage_skill(cwd, "optional", metadata=entry("org/optional"))
            return 0

        self.assertEqual(self.refresh(runner, optional=("org/optional",)), 1)
        self.assertEqual(calls, ["org/catalog", "org/optional"])
        self.assertTrue((self.store / "optional" / "SKILL.md").is_file())

    def test_new_skill_support_files_and_executable_modes(self):
        def runner(*args, cwd):
            stage_skill(cwd, "added")
            support = cwd / ".agents" / "skills" / "added" / "scripts" / "tool.sh"
            support.parent.mkdir()
            support.write_text("#!/bin/sh\nexit 0\n")
            support.chmod(0o755)
            return 0

        self.assertEqual(self.refresh(runner), 0)
        tool = self.store / "added" / "scripts" / "tool.sh"
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)
        self.assertEqual((self.claude / "added" / "SKILL.md").read_bytes(), self.new)

    def test_noop_keeps_inode_mode_and_mtime(self):
        def runner(*args, cwd):
            stage_skill(cwd, content=self.old)
            return 0

        before = (self.live / "SKILL.md").stat()
        self.assertEqual(self.refresh(runner), 0)
        after = (self.live / "SKILL.md").stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_mode),
                         (before.st_ino, before.st_mtime_ns, before.st_mode))
        self.assertEqual(json.loads(self.lock.read_text())["skills"]["example"]["skillFolderHash"], "a" * 40)

    def test_retired_support_file_remains_readable(self):
        retired = self.live / "old-script.py"
        retired.write_text("old reference")
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(retired.read_text(), "old reference")

    def test_support_files_published_before_entrypoint(self):
        def runner(*args, cwd):
            stage_skill(cwd)
            (cwd / ".agents" / "skills" / "example" / "helper.txt").write_text("new helper")
            return 0

        original_replace = os.replace

        def replace(source, target):
            if Path(target) == self.live / "SKILL.md":
                self.assertEqual((self.live / "helper.txt").read_text(), "new helper")
            original_replace(source, target)

        with patch.object(publish_skills.os, "replace", side_effect=replace):
            self.assertEqual(self.refresh(runner), 0)

    def test_untracked_skill_is_never_overwritten(self):
        self.initial["skills"] = {}
        self.lock.write_text(json.dumps(self.initial))
        self.assertEqual(self.refresh(), 1)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.old)

    def test_untracked_broken_projection_is_not_adopted(self):
        projection = self.claude / "untracked"
        projection.symlink_to(self.root / "missing", target_is_directory=True)

        def runner(*args, cwd):
            stage_skill(cwd, "untracked")
            return 0

        self.assertEqual(self.refresh(runner), 1)
        self.assertEqual(projection.readlink(), self.root / "missing")
        self.assertFalse((self.store / "untracked").exists())

    def test_interrupted_first_install_resumes_without_claiming_authored_files(self):
        def runner(*args, cwd):
            stage_skill(cwd, "added", metadata=entry("org/catalog"))
            return 0

        with patch.object(refresh_skills, "project_claude", side_effect=OSError("interrupted")):
            self.assertEqual(self.refresh(runner), 1)
        pending = json.loads(self.lock.read_text())
        self.assertNotIn("added", pending["skills"])
        self.assertEqual(pending.get("refreshPending", {}).get("added"), "github.com/org/catalog")
        self.assertEqual((self.store / "added" / "SKILL.md").read_bytes(), self.new)
        self.assertEqual(self.refresh(runner), 0)
        recovered = json.loads(self.lock.read_text())
        self.assertIn("added", recovered["skills"])
        self.assertNotIn("refreshPending", recovered)
        self.assertEqual((self.claude / "added" / "SKILL.md").read_bytes(), self.new)

    def test_pending_reservation_cannot_be_claimed_by_another_source(self):
        self.initial["skills"] = {}
        self.initial["refreshPending"] = {"example": "github.com/another/catalog"}
        self.lock.write_text(json.dumps(self.initial))
        self.assertEqual(self.refresh(), 1)
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.old)

    def test_relative_catalog_is_resolved_from_launch_directory(self):
        def runner(*args, cwd):
            self.assertEqual(args[1], str(self.project / "catalog"))
            stage_skill(cwd)
            return 0

        self.assertEqual(self.refresh(runner, sources=("./catalog",)), 0)

    def test_local_dependency_metadata_survives_stage_cleanup(self):
        source = self.root / "local-source"
        source.mkdir()
        project_lock = self.project / "skills-lock.json"
        original = {"source": "../local-source", "sourceType": "local", "computedHash": "old"}
        project_lock.write_text(json.dumps({"version": 1, "skills": {"dependency": original}}))

        def runner(*args, cwd):
            if args[1] == str(source):
                metadata = original | {"source": os.path.relpath(source, cwd), "computedHash": "new"}
                stage_skill(cwd, "dependency", metadata=metadata)
            else:
                stage_skill(cwd)
            return 0

        self.assertEqual(self.refresh(runner), 0)
        saved = json.loads(project_lock.read_text())["skills"]["dependency"]
        self.assertEqual((self.project / saved["source"]).resolve(), source.resolve())
        self.assertEqual(saved["computedHash"], "new")

    def test_symlink_and_shape_conflicts_fail_before_any_publish(self):
        outside = self.root / "outside.txt"
        outside.write_text("private")
        (self.live / "z-helper.txt").symlink_to(outside)

        def runner(*args, cwd):
            stage_skill(cwd)
            (cwd / ".agents" / "skills" / "example" / "z-helper.txt").write_text("replacement")
            return 0

        self.assertEqual(self.refresh(runner), 1)
        self.assertEqual(outside.read_text(), "private")
        self.assertEqual((self.live / "SKILL.md").read_bytes(), self.old)

    def test_real_claude_copy_keeps_path_and_gets_update(self):
        projection = self.claude / "example"
        projection.unlink()
        projection.mkdir()
        (projection / "SKILL.md").write_bytes(self.old)
        self.assertEqual(self.refresh(), 0)
        self.assertFalse(projection.is_symlink())
        self.assertEqual((projection / "SKILL.md").read_bytes(), self.new)

    def test_directory_wide_claude_alias_is_preserved(self):
        shutil.rmtree(self.claude)
        self.claude.symlink_to(self.store, target_is_directory=True)
        self.assertEqual(self.refresh(), 0)
        self.assertTrue(self.claude.is_symlink())
        self.assertFalse(self.live.is_symlink())
        self.assertEqual((self.claude / "example" / "SKILL.md").read_bytes(), self.new)

    def test_project_lock_only_updates_dependencies_and_preserves_ref(self):
        metadata = entry("org/project") | {"ref": "stable"}
        project_lock = self.project / "skills-lock.json"
        project_lock.write_text(json.dumps({"version": 1, "skills": {"dependency": metadata}}))
        authored = self.project / ".agents" / "skills" / "authored"
        authored.mkdir(parents=True)
        (authored / "SKILL.md").write_bytes(body("authored"))
        calls = []

        def runner(*args, cwd):
            calls.append(args)
            if args[1] == "org/project#stable":
                stage_skill(cwd, "dependency", metadata=metadata)
            else:
                stage_skill(cwd)
            return 0

        self.assertEqual(self.refresh(runner), 0)
        self.assertEqual(calls[1][1], "org/project#stable")
        self.assertIn("dependency", calls[1])
        self.assertNotIn("*", calls[1])
        self.assertEqual((authored / "SKILL.md").read_bytes(), body("authored"))
        self.assertEqual(json.loads(project_lock.read_text())["skills"]["dependency"]["ref"], "stable")

    def test_global_metadata_preserves_other_state(self):
        self.assertEqual(self.refresh(), 0)
        lock = json.loads(self.lock.read_text())
        self.assertEqual(lock["dismissed"], {"notice": True})
        record = lock["skills"]["example"]
        self.assertEqual(record["installedAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(record["skillPath"], "skills/example/SKILL.md")
        self.assertEqual(record["sourceUrl"], "https://github.com/org/catalog.git")
        self.assertEqual(record["skillFolderHash"], "new" * 16)

    def test_other_tracked_global_source_updates_by_name(self):
        other = entry("org/other") | {"skillFolderHash": "old", "ref": "stable"}
        self.initial["skills"]["other"] = other
        self.lock.write_text(json.dumps(self.initial))
        calls = []

        def runner(*args, cwd):
            calls.append(args)
            if args[1] == "org/other#stable":
                stage_skill(cwd, "other", metadata=other)
            else:
                stage_skill(cwd)
            return 0

        self.assertEqual(self.refresh(runner), 0)
        self.assertEqual(calls[1][1], "org/other#stable")
        self.assertIn("other", calls[1])
        self.assertIn("--full-depth", calls[1])
        self.assertNotIn("*", calls[1])
        self.assertEqual((self.store / "other" / "SKILL.md").read_bytes(), self.new)
        self.assertEqual(json.loads(self.lock.read_text())["skills"]["other"]["ref"], "stable")

    def test_malformed_project_lock_does_not_block_other_global_sources(self):
        other = entry("org/other") | {"skillFolderHash": "old"}
        self.initial["skills"]["other"] = other
        self.lock.write_text(json.dumps(self.initial))
        project_lock = self.project / "skills-lock.json"
        project_lock.write_text("invalid json")

        def runner(*args, cwd):
            if args[1] == "org/other":
                stage_skill(cwd, "other", metadata=other)
            else:
                stage_skill(cwd)
            return 0

        self.assertEqual(self.refresh(runner), 1)
        self.assertEqual((self.store / "other" / "SKILL.md").read_bytes(), self.new)
        self.assertEqual(project_lock.read_text(), "invalid json")

    def test_unknown_lock_schema_is_rejected_without_installing(self):
        self.initial["version"] = 999
        self.lock.write_text(json.dumps(self.initial))
        with self.assertRaisesRegex(ValueError, "schema"):
            self.refresh()
        self.assertFalse(self.calls)

    def test_serializes_entire_refreshes(self):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        processes = [context.Process(target=concurrent_worker, args=(str(self.root), barrier)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        lines = (self.root / "calls.log").read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].split()[1], lines[1].split()[1])
        self.assertEqual(lines[2].split()[1], lines[3].split()[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
