#!/usr/bin/env python3
"""Tests for publish_roles.py.

Two things are under test: that the generator emits files the downstream runtime
(uzi) can parse, tail-free, in a fixed shape; and that the drift mechanism is
real -- a roles.yaml change that is not regenerated is caught, and a NEW role
added to roles.yaml is published with no edit to this generator.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import re
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout

import yaml

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent.parent
REAL_ROLES = HERE / ".." / "roles.yaml"
REAL_OUT = REPO_ROOT / "product-agents"

spec = importlib.util.spec_from_file_location("publish_roles", HERE / "publish_roles.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)


# ---- a faithful port of uzi's api/internal/agenttmpl/builtins.go `parse` ----
# The published files exist to be read by that parser; the test owns a copy so a
# format regression here fails locally rather than only in the downstream repo.
ALLOWED_KEYS = {"name", "version", "description", "model", "tools"}


def uzi_parse(raw: str) -> dict:
    delim = "---\n"
    if not raw.startswith(delim):
        raise ValueError("missing opening frontmatter delimiter")
    rest = raw[len(delim):]
    idx = rest.find("\n" + delim)
    if idx < 0:
        raise ValueError("missing closing frontmatter delimiter")
    frontmatter = rest[:idx]
    after = rest[idx + len("\n" + delim):]
    if not after.startswith("\n"):
        raise ValueError("missing blank line after frontmatter")
    body = after[len("\n"):]
    d: dict = {"PromptBody": body, "Tools": []}
    for line in frontmatter.split("\n"):
        key, sep, val = line.partition(": ")
        if not sep:
            raise ValueError(f"malformed frontmatter line: {line!r}")
        if key == "name":
            d["Name"] = val
        elif key == "version":
            if not re.fullmatch(r"[0-9]+", val) or int(val) <= 0:
                raise ValueError(f"invalid version {val!r}")
            d["Version"] = int(val)
        elif key == "description":
            d["Description"] = val
        elif key == "model":
            d["Model"] = val
        elif key == "tools":
            d["Tools"] = [t for t in val.split(", ") if t]
        else:
            raise ValueError(f"unknown frontmatter key: {key!r}")
    if not d.get("Name"):
        raise ValueError("frontmatter missing name")
    if not d.get("Description"):
        raise ValueError("frontmatter missing description")
    if not d["PromptBody"]:
        raise ValueError("empty prompt body")
    return d


def write_roles(tmp: pathlib.Path, roles: list[dict]) -> pathlib.Path:
    path = tmp / "roles.yaml"
    path.write_text(yaml.safe_dump({"roles": roles}, sort_keys=False), encoding="utf-8")
    return path


def role(name, *, version=1, description="does a thing", tools=None, model="opus",
         body="Do the work.\n", **extra):
    r = {"name": name, "version": version, "description": description,
         "tools": tools or [], "model": model, "prompt_body": body}
    r.update(extra)
    return r


class TestRealRoster(unittest.TestCase):
    """The committed product-agents/ must match the real roles.yaml and parse."""

    def test_committed_folder_is_in_sync(self):
        rc = pr.check(REAL_ROLES.resolve(), REAL_OUT.resolve())
        self.assertEqual(rc, 0, "product-agents/ is stale; run publish_roles.py")

    def test_every_published_real_role_has_a_file_and_parses(self):
        data = pr.load_roles(REAL_ROLES.resolve())
        names = {str(r["name"]) for r in pr.published(data)}
        files = {p.stem for p in REAL_OUT.glob("*.md") if p.name != "README.md"}
        self.assertEqual(names, files)
        for p in REAL_OUT.glob("*.md"):
            if p.name == "README.md":
                continue
            uzi_parse(p.read_text(encoding="utf-8"))  # raises on any format defect

    def test_no_published_body_carries_a_tail_heading(self):
        for p in REAL_OUT.glob("*.md"):
            if p.name == "README.md":
                continue
            body = uzi_parse(p.read_text(encoding="utf-8"))["PromptBody"]
            self.assertIsNone(pr.TAIL_RE.search(body), f"{p.name} has a tail heading")

    def test_frontmatter_is_strict_yaml_with_only_allowed_keys(self):
        for p in REAL_OUT.glob("*.md"):
            if p.name == "README.md":
                continue
            fm = p.read_text(encoding="utf-8").split("\n---\n", 1)[0][len("---\n"):]
            loaded = yaml.safe_load(fm)
            self.assertLessEqual(set(loaded), ALLOWED_KEYS, p.name)


class TestRendering(unittest.TestCase):
    def test_field_order_is_name_version_description_tools_model(self):
        text = pr.render_role(role("r", tools=["Bash", "Read"], model="opus"))
        keys = [ln.split(":", 1)[0] for ln in text.splitlines()[1:5]]
        self.assertEqual(keys, ["name", "version", "description", "tools"])
        self.assertIn("model: opus", text)

    def test_tools_omitted_when_empty(self):
        text = pr.render_role(role("r", tools=[]))
        self.assertNotIn("tools:", text)

    def test_tools_are_comma_space_joined(self):
        text = pr.render_role(role("r", tools=["Bash", "Read", "Grep"]))
        self.assertIn("tools: Bash, Read, Grep\n", text)

    def test_model_omitted_when_empty(self):
        text = pr.render_role(role("r", model=""))
        self.assertNotIn("model:", text)

    def test_version_omitted_when_absent(self):
        r = role("r")
        del r["version"]
        text = pr.render_role(r)
        self.assertNotIn("version:", text)
        self.assertEqual(uzi_parse(text)["Version"] if "Version" in uzi_parse(text) else 0, 0)

    def test_description_with_colon_space_is_quoted_and_round_trips(self):
        desc = "Runs the gate: format, lint, and tests"
        text = pr.render_role(role("r", description=desc))
        self.assertIn(f'description: "{desc}"\n', text)
        fm = text.split("\n---\n", 1)[0][len("---\n"):]
        self.assertEqual(yaml.safe_load(fm)["description"], desc)

    def test_plain_description_is_not_quoted(self):
        text = pr.render_role(role("r", description="just words here"))
        self.assertIn("description: just words here\n", text)

    def test_body_has_single_trailing_newline(self):
        text = pr.render_role(role("r", body="line one\nline two\n\n\n"))
        self.assertTrue(text.endswith("line two\n"))
        self.assertFalse(text.endswith("line two\n\n"))

    def test_prose_mention_of_the_tail_heading_is_allowed(self):
        # A backticked mid-line mention is NOT a tail and must publish fine.
        r = role("r", body="run every slot in your `## For this repo` tail.\n")
        text = pr.render_role(r)  # must not raise
        self.assertIn("`## For this repo`", text)

    def test_a_real_tail_heading_is_refused(self):
        r = role("r", body="Body.\n\n## For this repo (x)\n- do y\n")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            pr.render_role(r)

    def test_a_double_quote_that_forces_quoting_is_refused(self):
        # A `"` mid-value that does NOT otherwise need quoting is emitted bare
        # (valid YAML, read verbatim downstream). The refusal is only for a value
        # that BOTH needs quoting (a `: `) AND carries a `"`, which the plain
        # double-quote wrap could not represent unambiguously.
        text = pr.render_role(role("r", description='mid " quote, no colon'))
        self.assertIn('description: mid " quote, no colon\n', text)  # bare, fine
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            pr.render_role(role("r", description='needs quoting: a " here'))


class TestPublishOptOut(unittest.TestCase):
    def test_absent_field_publishes(self):
        self.assertTrue(pr.is_published(role("r")))

    def test_publish_false_opts_out(self):
        self.assertFalse(pr.is_published(role("r", publish=False)))

    def test_publish_true_publishes(self):
        self.assertTrue(pr.is_published(role("r", publish=True)))

    def test_opt_out_role_is_not_emitted(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            roles_path = write_roles(tmp, [role("kept"), role("gone", publish=False)])
            out = tmp / "out"
            pr.generate(roles_path, out)
            self.assertTrue((out / "kept.md").exists())
            self.assertFalse((out / "gone.md").exists())


class TestDriftMechanism(unittest.TestCase):
    def test_adding_a_role_publishes_a_new_file_with_no_code_change(self):
        # The peer's hard requirement: a NEW role in roles.yaml yields a new
        # published file, using the SAME generator, with no edit here.
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            out = tmp / "out"
            roles_path = write_roles(tmp, [role("coder"), role("reviewer")])
            pr.generate(roles_path, out)
            self.assertEqual(
                {p.name for p in out.glob("*.md")}, {"coder.md", "reviewer.md"})

            # Add a brand-new role; regenerate with the identical generator.
            write_roles(tmp, [role("coder"), role("reviewer"), role("newbie")])
            self.assertEqual(pr.check(roles_path, out), 1)  # drift detected
            pr.generate(roles_path, out)
            self.assertTrue((out / "newbie.md").exists())
            uzi_parse((out / "newbie.md").read_text(encoding="utf-8"))
            self.assertEqual(pr.check(roles_path, out), 0)  # back in sync

    def test_editing_a_body_without_regen_is_out_of_sync(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            out = tmp / "out"
            roles_path = write_roles(tmp, [role("coder", body="original.\n")])
            pr.generate(roles_path, out)
            self.assertEqual(pr.check(roles_path, out), 0)
            write_roles(tmp, [role("coder", body="CHANGED body.\n")])
            self.assertEqual(pr.check(roles_path, out), 1)

    def test_removing_a_role_prunes_its_file_and_keeps_readme(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            out = tmp / "out"
            roles_path = write_roles(tmp, [role("a"), role("b")])
            pr.generate(roles_path, out)
            (out / "README.md").write_text("keep me\n", encoding="utf-8")
            # Remove role b; a stale b.md would otherwise linger.
            write_roles(tmp, [role("a")])
            self.assertEqual(pr.check(roles_path, out), 1)  # stale b.md
            written, removed = pr.generate(roles_path, out)
            self.assertEqual(removed, ["b.md"])
            self.assertFalse((out / "b.md").exists())
            self.assertTrue((out / "README.md").exists())  # never pruned
            self.assertEqual(pr.check(roles_path, out), 0)

    def test_generate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            out = tmp / "out"
            roles_path = write_roles(tmp, [role("a"), role("b", tools=["Bash", "SendMessage"])])
            pr.generate(roles_path, out)
            first = {p.name: p.read_text() for p in out.glob("*.md")}
            pr.generate(roles_path, out)
            second = {p.name: p.read_text() for p in out.glob("*.md")}
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
