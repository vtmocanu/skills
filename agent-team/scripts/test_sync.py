#!/usr/bin/env python3
"""Regression tests for sync.py.

Run: python3 agent-team/scripts/test_sync.py    (stdlib unittest; no pytest)

Most tests here pin a defect review found in a draft of sync.py; a few pin a
property no version ever broke, and a few pin defects the suite itself caught
after a fix over-corrected. They are written as fixtures because that is the
form that would have caught them: each is a few lines of setup, and the review
rounds that found them cost considerably more.

The naming convention is deliberate — `test_<finding>_<what it must do now>` —
so a failure names the defect that came back rather than the assertion that
broke.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SYNC = HERE / "sync.py"

spec = importlib.util.spec_from_file_location("sync", SYNC)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


LIBRARY = textwrap.dedent(
    """\
    roles:
      - name: alpha
        version: 2
        description: The alpha role.
        tools: [Bash, Read, SendMessage, TaskUpdate, TaskList, TaskGet]
        model: opus
        prompt_body: |
          Alpha generic body line one.
          Alpha generic body line two.
      - name: beta
        version: 3
        description: The beta role.
        tools: []
        model: opus
        prompt_body: |
          Beta generic body.
          --oneline -3 is a line starting with two dashes.
          ++ and this one starts with two pluses.
    """
)


ALPHA_TOOLS = "Bash, Read, SendMessage, TaskUpdate, TaskList, TaskGet"
ALPHA_BODY = "Alpha generic body line one.\nAlpha generic body line two."
BETA_BODY = (
    "Beta generic body.\n"
    "--oneline -3 is a line starting with two dashes.\n"
    "++ and this one starts with two pluses."
)


def agent_file(version="1", body=ALPHA_BODY, tail=None, **fm):
    """A fixture that differs from the library ONLY where the test says so.

    `apply` syncs the body and the version stamp and deliberately does not
    touch description/tools/model, so a fixture missing those comes back
    MODIFIED after a perfectly correct apply — which reads as a script defect
    and is a fixture defect. Defaults match the library exactly.
    """
    head = {
        "name": "alpha",
        "version": version,
        "description": "The alpha role.",
        "tools": ALPHA_TOOLS,
        "model": "opus",
    }
    head.update(fm)
    lines = "\n".join(f"{k}: {v}" for k, v in head.items() if v is not None)
    text = f"---\n{lines}\n---\n\n{body}\n"
    if tail:
        text += f"\n## For this repo\n\n{tail}\n"
    return text


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.library = self.tmp / "roles.yaml"
        self.library.write_text(LIBRARY)
        self.agents = self.tmp / "repo" / ".claude" / "agents"
        self.agents.mkdir(parents=True)

    def write(self, name, text, newline="\n"):
        path = self.agents / f"{name}.md"
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace("\n", newline))
        return path

    def run_sync(self, *args, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(SYNC), "--library", str(self.library), *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or self.tmp / "repo"),
        )
        return proc

    def body_of(self, name):
        path = self.agents / f"{name}.md"
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()


class TestFlagPlacement(SyncTestCase):
    """A global flag before the subcommand was accepted and then discarded,
    so `--agents X apply role` wrote to ./.claude/agents and exited 0."""

    def test_agents_before_subcommand_is_honoured(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.write("alpha", agent_file(tail="Repo rule."))
        (elsewhere / "alpha.md").write_text(agent_file(tail="Elsewhere rule."))

        proc = self.run_sync("--agents", str(elsewhere), "apply", "alpha")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("version: 2", (elsewhere / "alpha.md").read_text())
        self.assertIn(
            "version: 1", self.body_of("alpha"), "the tree NOT named was written"
        )

    def test_agents_after_subcommand_is_honoured(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "alpha.md").write_text(agent_file(tail="Elsewhere rule."))
        proc = self.run_sync("check", "--agents", str(elsewhere))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("STALE", proc.stdout)

    def test_unreadable_library_before_subcommand_still_exits_2(self):
        broken = self.tmp / "broken.yaml"
        broken.write_text("roles: [oops\n")
        self.write("alpha", agent_file())
        proc = self.run_sync("--library", str(broken), "check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


class TestVersionStamp(SyncTestCase):
    """A non-bare-integer version line fell through to the insertion path and
    produced a duplicate key; PyYAML is last-wins, so the old value won and
    check never converged."""

    def _assert_converges(self, version_literal):
        self.write("alpha", agent_file(version=version_literal, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self.body_of("alpha")
        self.assertEqual(
            text.count("version:"), 1, f"duplicate version key: {text[:200]!r}"
        )
        after = self.run_sync("check")
        self.assertEqual(after.returncode, 0, after.stdout)

    def test_quoted_version_converges(self):
        self._assert_converges('"1"')

    def test_float_version_converges(self):
        self._assert_converges("1.0")

    def test_already_current_but_quoted_converges(self):
        self._assert_converges('"2"')

    def test_missing_version_is_inserted(self):
        self.write("alpha", agent_file(version=None, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("version: 2", self.body_of("alpha"))
        self.assertEqual(self.run_sync("check").returncode, 0)


class TestTailPreservation(SyncTestCase):
    def test_crlf_tail_survives_apply(self):
        """The post-write check compared two newline-normalized strings, so it
        could not see a CRLF file being rewritten to LF."""
        self.write("alpha", agent_file(tail="Repo rule."), newline="\r\n")
        before = (self.agents / "alpha.md").read_bytes()
        self.assertIn(b"\r\n", before)
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = (self.agents / "alpha.md").read_bytes()
        self.assertIn(b"\r\n## For this repo\r\n", after)
        self.assertIn(b"Repo rule.", after)

    def test_tail_is_byte_identical_after_apply(self):
        self.write("alpha", agent_file(tail="Repo rule with `backticks` and — dash."))
        before = sync.AgentFile(self.agents / "alpha.md").tail
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 0)
        self.assertEqual(sync.AgentFile(self.agents / "alpha.md").tail, before)

    def test_tail_heading_must_be_a_whole_line(self):
        """`## For this repository` is not the marker, and library bodies quote
        the literal `## For this repo` mid-sentence."""
        text = agent_file(body="See the `## For this repo` tail.\n\n## For this repositories\n\nNot a tail.")
        self.write("alpha", text)
        agent = sync.AgentFile(self.agents / "alpha.md")
        self.assertIsNone(agent.tail)

    def test_tail_heading_may_carry_a_suffix(self):
        """Real rosters write `## For this repo (uzi)`. Requiring an exact
        match reported 10 of 11 files in a live roster as tail-less, which
        routes every one of them to the inline-tuning refusal."""
        for heading in ("## For this repo", "## For this repo (uzi)", "## For this repo - notes"):
            with self.subTest(heading=heading):
                self.write(
                    "alpha",
                    f"---\nname: alpha\nversion: 1\ndescription: The alpha role.\n"
                    f"tools: {ALPHA_TOOLS}\nmodel: opus\n---\n\n{ALPHA_BODY}\n\n"
                    f"{heading}\n\nRepo rule.\n",
                )
                agent = sync.AgentFile(self.agents / "alpha.md")
                self.assertIsNotNone(agent.tail, f"{heading!r} was not seen as a tail")
                self.assertIn("Repo rule.", agent.tail)
                self.assertNotIn("Repo rule.", agent.generic)

    def test_a_prose_mention_of_the_heading_is_not_a_tail(self):
        """Four library bodies quote the literal mid-line, backticked."""
        self.write("alpha", agent_file(body="Your `## For this repo` tail names a command."))
        self.assertIsNone(sync.AgentFile(self.agents / "alpha.md").tail)


class TestInlineTuningWarning(SyncTestCase):
    """Lines in the repo body that the library lacks are DROPPED with a warning,
    not refused.

    Refusing was measured at 17 of 51 real library edits, 11 of 11 roles on one
    real bump, and 5 of 11 on a roster generated from an older library — the
    tool's primary use case. `adds` cannot tell hand-written content from
    previous-release library text: a line the library DELETED is present in the
    consumer's older copy and looks identical to one a human added. The
    unexplained case — a body differing at EQUAL version — is what still
    refuses.
    """

    HAND = ALPHA_BODY + "\nHAND WRITTEN"

    def test_the_line_is_dropped_with_a_warning_naming_the_count(self):
        self.write("alpha", agent_file(body=self.HAND, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("WILL BE DROPPED", proc.stderr)
        self.assertIn("1 line(s)", proc.stderr)
        self.assertNotIn("HAND WRITTEN", self.body_of("alpha"))
        self.assertIn("Repo rule.", self.body_of("alpha"), "the tail must survive")

    def test_a_drop_is_visible_on_stdout_and_in_the_exit_code(self):
        """Eleven files could lose a line each while every stdout line said
        `preserved` and the status said 0 — the two channels a caller reads."""
        self.write("alpha", agent_file(body=self.HAND, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("1 body line(s) DROPPED", proc.stdout)
        self.assertIn("alpha.md.pre-sync", proc.stdout)

    def test_the_dropped_content_is_recoverable_from_the_backup(self):
        """`git diff` is not a recovery path here: with the roster committed and
        the tuning added since, it shows one deletion — the version line — which
        is the drop-free all-clear signature."""
        self.write("alpha", agent_file(body=self.HAND, tail="Repo rule."))
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 3)
        backup = self.agents / "alpha.md.pre-sync"
        self.assertTrue(backup.exists(), "no backup written")
        self.assertIn("HAND WRITTEN", backup.read_text())

    def test_a_backup_is_not_picked_up_as_an_agent_file(self):
        """`.pre-sync` files sit in the agents directory; if check globbed them
        they would show as CUSTOM and hold the mandatory load-time pass red."""
        self.write("alpha", agent_file(body=self.HAND, tail="Repo rule."))
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 3)
        self.assertTrue((self.agents / "alpha.md.pre-sync").exists())
        proc = self.run_sync("check")
        self.assertNotIn("pre-sync", proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_a_clean_sync_writes_no_backup(self):
        self.write("alpha", agent_file(tail="Repo rule."))
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 0)
        self.assertFalse((self.agents / "alpha.md.pre-sync").exists())

    def test_the_closing_message_does_not_claim_only_version_lines_went(self):
        self.write("alpha", agent_file(body=self.HAND, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertIn("BODY LINES WERE DROPPED", proc.stdout)
        self.assertNotIn("only deletions should be", proc.stdout)

    def test_a_clean_sync_still_claims_only_version_lines_went(self):
        self.write("alpha", agent_file(tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertIn("only deletions should be the old", proc.stdout)

    def test_a_tail_less_file_is_treated_identically(self):
        """Two files differing only in whether a tail exists must not get
        opposite treatment — that inconsistency was the original finding."""
        self.write("alpha", agent_file(body=self.HAND))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("WILL BE DROPPED", proc.stderr)

    def test_a_line_starting_with_plus_plus_is_counted(self):
        """body_delta filtered unified-diff headers by prefix, which also ate
        content lines beginning ++ or --."""
        self.write("alpha", agent_file(body=ALPHA_BODY + "\n++ HAND WRITTEN"))
        proc = self.run_sync("apply", "alpha")
        self.assertIn("1 line(s)", proc.stderr)

    def test_an_equal_version_body_difference_still_refuses(self):
        self.write("alpha", agent_file(version="2", body=self.HAND, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("HAND WRITTEN", self.body_of("alpha"))

    def test_force_lifts_the_equal_version_refusal(self):
        self.write("alpha", agent_file(version="2", body=self.HAND, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha", "--force")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("Repo rule.", self.body_of("alpha"))

    def test_delta_counts_lines_starting_with_dashes(self):
        role = {"prompt_body": "a\n--oneline -3\nb\n"}
        agent = type("A", (), {"generic": "a\n"})()
        self.assertEqual(sync.body_delta(agent, role), (0, 2))


class TestPathContainment(SyncTestCase):
    def test_library_role_name_cannot_escape_the_agents_dir(self):
        self.library.write_text(
            LIBRARY.replace("- name: alpha", "- name: ../escaped", 1)
        )
        outside = self.agents.parent / "escaped.md"
        outside.write_text(agent_file(name="../escaped"))
        proc = self.run_sync("apply", "../escaped")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("version: 1", outside.read_text())

    def test_symlinked_agent_file_is_refused(self):
        target = self.tmp / "outside.md"
        target.write_text(agent_file(tail="Repo rule."))
        os.symlink(target, self.agents / "alpha.md")
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("version: 1", target.read_text())


class TestExitCodes(SyncTestCase):
    def test_clean_roster_is_0(self):
        self.write("alpha", agent_file(version="2", tail="Repo rule."))
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_empty_agents_dir_is_2(self):
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_malformed_library_entry_is_2(self):
        self.library.write_text("roles:\n  - name: alpha\n    version: 1\n")
        self.write("alpha", agent_file())
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_roles_as_mapping_is_2(self):
        self.library.write_text("roles:\n  alpha: whatever\n")
        self.write("alpha", agent_file())
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_unreadable_file_is_2_and_other_rows_still_print(self):
        self.write("alpha", agent_file(tail="Repo rule."))
        (self.agents / "broken.md").write_bytes(b"\xff\xfe\x00invalid utf-8")
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("alpha", proc.stdout)
        self.assertIn("ERROR", proc.stdout)

    def test_apply_returning_1_leaves_the_tree_untouched(self):
        """Guards, including the version stamp, all run before the first write."""
        self.write("alpha", agent_file(tail="Repo rule."))
        self.write(
            "beta",
            agent_file(name="beta", version="3", body=BETA_BODY + "\nHAND WRITTEN"),
        )
        before = self.body_of("alpha")
        # beta's body differs at its EQUAL version, so the batch must refuse.
        proc = self.run_sync("apply", "alpha", "beta")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(self.body_of("alpha"), before)

    def test_diff_and_apply_agree_on_an_unknown_role(self):
        self.write("gamma", agent_file())
        self.assertEqual(self.run_sync("diff", "gamma").returncode, 1)
        self.assertEqual(self.run_sync("apply", "gamma").returncode, 1)


class TestStatuses(SyncTestCase):
    def test_custom_file_is_not_drift(self):
        self.write("alpha", agent_file(version="2", tail="Repo rule."))
        (self.agents / "README.md").write_text("# notes\n")
        proc = self.run_sync("check")
        self.assertIn("CUSTOM", proc.stdout)
        self.assertEqual(proc.returncode, 0, "a CUSTOM file must not hold check red")

    def test_missing_coordination_tools_is_drift(self):
        self.library.write_text(
            LIBRARY.replace(
                "tools: [Bash, Read, SendMessage, TaskUpdate, TaskList, TaskGet]",
                "tools: [Bash, Read]",
                1,
            )
        )
        self.write("alpha", agent_file(version="2", tail="Repo rule.", tools="Bash, Read"))
        proc = self.run_sync("check")
        self.assertIn("MISSING COORDINATION TOOLS", proc.stdout)
        self.assertNotIn(" ok ", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_bad_frontmatter_is_reported_and_apply_refuses(self):
        self.write("alpha", agent_file(description="Validates: in a browser.", tail="Repo rule."))
        proc = self.run_sync("check")
        self.assertIn("BAD-FM", proc.stdout)
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 1)

    def test_tools_as_yaml_list_does_not_crash(self):
        path = self.agents / "alpha.md"
        path.write_text("---\nname: alpha\nversion: 1\ndescription: The alpha role.\ntools:\n  - Bash\n  - Read\n---\n\nAlpha generic body line one.\n\n## For this repo\n\nRepo rule.\n")
        proc = self.run_sync("check")
        self.assertIn("YAML list", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


class TestReadOnlySubcommands(SyncTestCase):
    def test_check_and_diff_write_nothing(self):
        self.write("alpha", agent_file(tail="Repo rule."))
        before = (self.agents / "alpha.md").read_bytes()
        self.run_sync("check")
        self.run_sync("diff", "alpha")
        self.assertEqual((self.agents / "alpha.md").read_bytes(), before)

    def test_diff_does_not_print_the_repo_private_tail(self):
        self.write("alpha", agent_file(tail="INTERNAL hostname db-eu-1.corp"))
        proc = self.run_sync("diff", "alpha")
        self.assertNotIn("db-eu-1.corp", proc.stdout)


class TestStaleBodySyncs(SyncTestCase):
    """The tool's primary use case: a consumer some releases behind, whose file
    is a pristine copy of an OLDER library body. Every line the library has
    since reworded is present in that file with the old wording, and an `adds`
    predicate counting `replace` opcodes read all of them as hand-tuning —
    refusing 11 of 11 roles on one real library bump."""

    OLD_BODY = "Alpha generic body line one.\nAlpha generic body line two, older wording."

    def test_a_stale_body_from_an_older_release_applies(self):
        self.write("alpha", agent_file(version="1", body=self.OLD_BODY, tail="Repo rule."))
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 0, "a plain stale sync was refused:\n" + proc.stderr)
        self.assertIn("Repo rule.", self.body_of("alpha"))
        self.assertEqual(self.run_sync("check").returncode, 0)

    def test_a_reworded_library_line_is_not_treated_as_hand_tuning(self):
        role = {"prompt_body": "a\nreworded line\nc\n"}
        agent = type("A", (), {"generic": "a\nold wording\nc\n"})()
        adds, dels = sync.body_delta(agent, role)
        self.assertEqual(adds, 0, "a replaced line must not count as an addition")
        self.assertEqual(dels, 1)

    def test_check_marks_an_inline_tuned_file_LEGACY_even_with_a_tail(self):
        """`check` must predict what `apply` DOES. The status used to be gated
        on the file having no tail while the write path never consulted it."""
        self.write("alpha", agent_file(body=ALPHA_BODY + "\nHAND WRITTEN", tail="Repo rule."))
        proc = self.run_sync("check")
        self.assertIn("LEGACY", proc.stdout)


class TestWriteSafety(SyncTestCase):
    def test_a_read_only_file_is_refused(self):
        """open(..., "w") needed write permission on the FILE; os.replace only
        needs it on the DIRECTORY, so making the write atomic silently removed
        this protection."""
        path = self.write("alpha", agent_file(tail="Repo rule."))
        before = path.read_bytes()
        os.chmod(path, 0o444)
        self.addCleanup(os.chmod, path, 0o644)
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_mode_survives_the_atomic_replace(self):
        path = self.write("alpha", agent_file(tail="Repo rule."))
        os.chmod(path, 0o600)
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_symlink_inside_the_agents_dir_is_refused(self):
        """Path.resolve() follows symlinks, so a check on the resolved path is
        unreachable; the case it missed rewrites another role's file."""
        victim = self.write("beta", agent_file(name="beta", tail="Beta rule."))
        before = victim.read_bytes()
        os.symlink(victim, self.agents / "alpha.md")
        proc = self.run_sync("apply", "alpha")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(victim.read_bytes(), before)

    def test_the_crlf_version_line_keeps_its_carriage_return(self):
        self.write("alpha", agent_file(tail="Repo rule."), newline="\r\n")
        self.assertEqual(self.run_sync("apply", "alpha").returncode, 0)
        raw = (self.agents / "alpha.md").read_bytes()
        self.assertIn(b"version: 2\r\n", raw)
        self.assertNotIn(b"version: 2\n", raw.replace(b"version: 2\r\n", b""))


class TestLibraryValidation(SyncTestCase):
    def test_a_null_value_is_an_instrument_failure_not_drift(self):
        """Emptying a block scalar while editing roles.yaml is an ordinary slip.
        It used to land as a traceback with exit 1, i.e. "drift found"."""
        for key in ("description", "prompt_body"):
            with self.subTest(key=key):
                # Written out rather than patched into LIBRARY: commenting the
                # `|` off a block scalar leaves the indented lines behind as a
                # folded plain scalar, which is a perfectly good string and
                # tests nothing.
                self.library.write_text(
                    "roles:\n"
                    "  - name: alpha\n"
                    "    version: 2\n"
                    + (f"    {key}:\n" if key == "description" else "    description: d\n")
                    + (f"    {key}:\n" if key == "prompt_body" else "    prompt_body: b\n")
                )
                self.write("alpha", agent_file())
                proc = self.run_sync("check")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_a_wrong_typed_value_is_an_instrument_failure(self):
        self.library.write_text(
            "roles:\n  - name: alpha\n    version: two\n"
            "    description: d\n    prompt_body: b\n"
        )
        self.write("alpha", agent_file())
        proc = self.run_sync("check")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TestDiffContainment(SyncTestCase):
    def test_diff_refuses_a_path_outside_the_agents_dir(self):
        """diff does not write, but it PRINTS the file — for an agent that is a
        disclosure into its context and possibly its report."""
        secret = self.agents.parent / "PRIVATE.md"
        secret.write_text("CONFIDENTIAL prod db db-eu-1.corp\n")
        self.library.write_text(LIBRARY.replace("- name: alpha", "- name: ../PRIVATE", 1))
        proc = self.run_sync("diff", "../PRIVATE")
        self.assertNotIn("db-eu-1.corp", proc.stdout)
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
