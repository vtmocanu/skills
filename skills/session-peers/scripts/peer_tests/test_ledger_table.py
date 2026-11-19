"""Shared table formats, link boundaries, explicit binding and live read model."""

import argparse
import contextlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

from peer_tests import test_ledger as fixture
from peer_tests.test_ledger import ME, OTHER
from peer_tests.test_web_server import WebBase
from session_peers import ledger, ledger_table, ledger_web, channels, cli, webdata
REAL_GIT = shutil.which("git")


class LedgerTableTests(unittest.TestCase):
    setUp = fixture.LedgerTests.setUp
    args = fixture.LedgerTests.args
    add = fixture.LedgerTests.add
    item = fixture.LedgerTests.item
    run_cli = fixture.LedgerTests.run_cli

    def test_forge_origin_scp_url_nested_group_and_credentials(self):
        for remote, expected in (
            ("git@github.com:acme/widgets.git", {"kind": "github", "base": "https://github.com/acme/widgets"}),
            ("ssh://git@github.com:2222/acme/widgets.git", {"kind": "github", "base": "https://github.com/acme/widgets"}),
            ("https://user:password@gitlab.example:8443/group/sub/repo.git", {"kind": "gitlab", "base": "https://gitlab.example:8443/group/sub/repo"}),
            ("https://unrecognised.example/a/b", None),
            ("/local/project", None),
            ("javascript://github.com/a/b", None),
        ):
            with self.subTest(remote=remote):
                self.assertEqual(ledger_table.forge_origin(remote), expected)

    def test_link_expansion_and_unsafe_urls(self):
        gh = {"kind": "github", "base": "https://github.com/acme/widgets"}
        gl = {"kind": "gitlab", "base": "https://gitlab.example/team/group"}
        for label, forge, suffix in (("#42", gh, "/issues/42"), ("PR #42", gh, "/pull/42"),
                                      ("!42", gh, "/pull/42"), ("#42", gl, "/-/issues/42"),
                                      ("PR #42", gl, "/-/merge_requests/42"), ("!42", gl, "/-/merge_requests/42")):
            self.assertEqual(ledger_table.expand_link(label, forge)["href"], forge["base"] + suffix)
        url = "https://example.invalid/runs/a?x=1&y=2"
        self.assertEqual(ledger_table.expand_link(url)["href"], url)
        for value in ("javascript:alert(1)", "data:text/html,x", "file:///secret", "//example.invalid/x", "https://x/\nalert", "https://x/\\evil"):
            self.assertIsNone(ledger_table.expand_link(value, gh)["href"])
        self.assertIsNone(ledger_table.expand_link("#42")["href"])
        self.assertIsNone(ledger_table.expand_link("run abc", custom={"run <id>": "javascript:{id}"})["href"])
        self.assertEqual(ledger_table.expand_link("run a:b", custom={"run <id>": "https://example.invalid/runs/{id}"})["href"],
                         "https://example.invalid/runs/a%3Ab")

    def test_tables_group_by_owner_and_escape_markdown(self):
        args = self.args()
        args.title = "Review | <img> [link]"
        args.link = ["#42", "javascript:alert(1)", "https://example.invalid/a|b"]
        self.book.mutate("add", args, self.owner)
        self.add("unowned")
        args.slug = "unowned"
        self.book.mutate("release", args, self.owner)
        head, _meta = self.book.snapshot()
        view = ledger_table.project(self.book, head, {"kind": "github", "base": "https://github.com/a/b"})
        self.assertEqual(view["unowned_open"], 1)
        self.assertEqual(view["groups"][0]["owner"], self.owner["name"])
        plain = ledger_table.render(view)
        self.assertIn("item", plain)
        self.assertNotIn(ME, plain)
        markdown = ledger_table.render(view, markdown=True)
        self.assertIn("\\| &lt;img&gt;", markdown)
        self.assertIn("[#42](<https://github.com/a/b/issues/42>)", markdown)
        self.assertNotIn("](<javascript:", markdown)
        self.assertIn("(<https://example.invalid/a%7Cb>)", markdown)
        self.assertEqual(len(ledger_table.project(self.book, head, all_items=True)["groups"]), 2)
        result = json.loads(self.run_cli("table", "--json").stdout)
        self.assertEqual(result["commit"], head)
        self.assertIn("| item | title |", self.run_cli("table", "--markdown").stdout)
        self.assertEqual(ledger_table.owner_label({"id": ME, "name": ME}), "cc:11111111")

    def test_custom_patterns_are_committed_and_body_is_excluded(self):
        args = self.args()
        args.link = ["run abc-123"]
        self.book.mutate("add", args, self.owner)
        self.book.commit({".ledger/links.json": json.dumps({"run <id>": "https://example.invalid/runs/{id}"})}, "local patterns", self.owner)
        head, _meta = self.book.snapshot()
        view = ledger_table.project(self.book, head)
        row = view["groups"][0]["items"][0]
        self.assertEqual(row["links"][0]["href"], "https://example.invalid/runs/abc-123")
        self.assertNotIn("body", row)
        (self.book.path / ".ledger/links.json").write_text("not committed")
        self.assertEqual(ledger_table.project(self.book, head), view)

    def test_custom_label_templates_share_cli_and_dashboard_projection(self):
        args = self.args()
        args.link = ["run abcdefgh-1234", "build abcdefgh-1234"]
        self.book.mutate("add", args, self.owner)
        config = {"run <id>": {"href": "https://example.invalid/runs/{id}",
                                "label": "run {id:8} [<{id}>] | *done*"},
                  "build <id>": "https://example.invalid/builds/{id}"}
        self.book.commit({".ledger/links.json": json.dumps(config)}, "label patterns", self.owner)
        head, _meta = self.book.snapshot()
        view = ledger_table.project(self.book, head)
        links = view["groups"][0]["items"][0]["links"]
        self.assertEqual(links[0], {"label": "run abcdefgh [<abcdefgh-1234>] | *done*",
                                    "href": "https://example.invalid/runs/abcdefgh-1234",
                                    "title": "run abcdefgh-1234"})
        self.assertEqual(links[1], {"label": "build abcdefgh-1234",
                                    "href": "https://example.invalid/builds/abcdefgh-1234"})
        plain = self.run_cli("table").stdout
        self.assertIn(links[0]["label"], plain)
        markdown = self.run_cli("table", "--markdown").stdout
        self.assertIn(r"run abcdefgh \[&lt;abcdefgh-1234&gt;\] \| \*done\*", markdown)
        self.assertIn("https://example.invalid/runs/abcdefgh-1234", markdown)
        self.assertIn(' "run abcdefgh-1234")', markdown)
        bound = {"key": self.book.key, "anchor": str(self.book.anchor), "forge": None}
        panel = ledger_web.view(bound, webdata.Budget())
        self.assertEqual(panel["groups"], view["groups"])

    def test_custom_template_validation_and_placeholder_bounds(self):
        def read(value):
            self.book.commit({".ledger/links.json": json.dumps(value)}, "pattern bounds", self.owner)
            head, _meta = self.book.snapshot()
            return ledger_table.patterns(self.book, head)
        valid = {"run <id>": {"href": "https://example.invalid/{id:64}", "label": "{id:1}/{id:64}/{id}"}}
        self.assertEqual(read(valid), valid)
        self.assertEqual(ledger_table.expand_link("run abc", custom=valid),
                         {"label": "a/abc/abc", "href": "https://example.invalid/abc", "title": "run abc"})
        invalid = [
            {"href": "https://example.invalid/{id}", "label": "x" * 129},
            {"href": "https://example.invalid/" + "x" * 2048 + "{id}", "label": "run"},
            {"href": "https://example.invalid/{id}/{id:8}", "label": "run"},
            {"href": "https://example.invalid/{id:0}", "label": "run"},
            {"href": "https://example.invalid/{id}", "label": "{id:65}"},
            {"href": "https://example.invalid/{id}", "label": "{other}"},
            {"href": "https://example.invalid/{id}", "label": "{id:8"},
            {"href": "https://example.invalid/{id}", "label": 42},
            {"href": "https://example.invalid/{id}"},
            {"href": "https://example.invalid/{id}", "label": "run", "lable": "typo"},
            "https://example.invalid/{id:999}",
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(ledger.LedgerError, "links.json"):
                read({"run <id>": value})
        for value in ({"run <id>": "https://example.invalid/{id}"} | {"p%d <id>" % i: "https://example.invalid/{id}" for i in range(32)},
                      {"x" * 129 + "<id>": "https://example.invalid/{id}"}):
            with self.assertRaises(ledger.LedgerError):
                read(value)

    def test_custom_label_never_makes_an_unsafe_href_clickable(self):
        config = {"run <id>": {"href": "javascript:{id}", "label": "safe looking {id:8}"}}
        self.assertEqual(ledger_table.expand_link("run abc", custom=config),
                         {"label": "safe looking abc", "href": None, "title": "run abc"})

    def test_default_owner_name_comes_from_discovery_and_explicit_name_wins(self):
        args = argparse.Namespace(as_identity=ME, name=None)
        with mock.patch.object(channels, "session_names", return_value={ME: "coordination"}) as discovery:
            self.assertEqual(ledger.caller(args), {"id": ME, "name": "coordination"})
            discovery.assert_called_once()
        args.name = "chosen"
        with mock.patch.object(channels, "session_names") as discovery:
            self.assertEqual(ledger.caller(args)["name"], "chosen")
            discovery.assert_not_called()
        args.name = None
        with mock.patch.object(channels, "session_names", side_effect=OSError("unavailable")):
            self.assertEqual(ledger.caller(args)["id"], ME)
            self.assertEqual(ledger_table.owner_label(ledger.caller(args)), "cc:11111111")

    def test_view_is_live_committed_and_has_no_filesystem_writes(self):
        self.add()
        with mock.patch.object(ledger_web.ledger, "Ledger", wraps=ledger.Ledger):
            bound = {"key": self.book.key, "anchor": str(self.book.anchor), "forge": None}
        def snapshot():
            return {str(path.relative_to(self.book.path)): path.read_bytes() for path in self.book.path.rglob("*") if path.is_file()}
        before = snapshot()
        view = ledger_web.view(bound, webdata.Budget())
        self.assertEqual(view["state"], "current")
        self.assertEqual(before, snapshot())
        args = self.args()
        args.status = "blocked"
        self.book.mutate("update", args, self.owner)
        current = ledger_web.view(bound, webdata.Budget())
        self.assertNotEqual(view["commit"], current["commit"])
        self.assertEqual(current["groups"][0]["items"][0]["status"], "blocked")
        bound["anchor"] = str(self.base / "wrong")
        self.assertEqual(ledger_web.view(bound, webdata.Budget())["state"], "unavailable")

    def test_byte_and_time_limits_are_unavailable_not_empty(self):
        self.add(body="large note" * 100)
        bound = {"key": self.book.key, "anchor": str(self.book.anchor), "forge": None}
        budget = webdata.Budget(total=16)
        self.assertEqual(ledger_web.view(bound, budget)["state"], "unavailable")
        self.assertLessEqual(budget.used, budget.total)
        with mock.patch.object(ledger_web, "TIMEOUT", 0):
            self.assertEqual(ledger_web.view(bound, webdata.Budget())["state"], "unavailable")
        bad = dict(bound, key="../outside")
        self.assertEqual(ledger_web.view(bad, webdata.Budget())["state"], "unavailable")

    def test_row_limit_refuses_before_reading_bodies_and_unicode_columns_align(self):
        self.add()
        head, _meta = self.book.snapshot()
        real_git = self.book._git
        calls = []
        def guarded(*args, **kwargs):
            calls.append(args[1])
            if args[1] == "ls-tree":
                return "\n".join("items/n%d.md" % i for i in range(257))
            return real_git(*args, **kwargs)
        with mock.patch.object(self.book, "_git", side_effect=guarded):
            with self.assertRaisesRegex(ledger.LedgerError, "item limit"):
                self.book.items(head, max_items=256)
        self.assertNotIn("cat-file", calls)
        self.assertEqual(ledger_table.cell_width("界e\u0301"), 3)


class LedgerHttpTests(WebBase):
    def test_owner_binding_and_live_endpoint_share_the_table(self):
        repo = self.root / "ledger-repo"
        repo.mkdir()
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "ledger-data"),
                                         "PATH": os.environ["PATH"] + os.pathsep + os.path.dirname(REAL_GIT)}):
            ledger.git(repo, "init", "--template=", "--quiet")
            book = ledger.Ledger(repo)
            owner = {"id": self.me, "name": "lead"}
            book.init(owner)
            args = argparse.Namespace(slug="task", title="Task", link=["#42"], body_file=None)
            book.mutate("add", args, owner)
            channels.create_channel("ops", self.me)
            bound = {"key": book.key, "anchor": str(book.anchor), "forge": {"kind": "github", "base": "https://github.com/a/b"}}
            with mock.patch.object(ledger_web, "binding", return_value=bound), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(channels._cmd_bind(argparse.Namespace(name="ops", clear_ledger=False), self.me), 0)
            view = self.ok("/api/ledger/ops")
            self.assertEqual(view["state"], "current")
            self.assertEqual(self.ok("/api/ledger/general")["state"], "unbound")
            self.assertEqual(view["groups"][0]["items"][0]["links"][0]["href"], "https://github.com/a/b/issues/42")
            self.assertEqual(self.api("/api/ledger/ops", token=False).status, 403)
            self.assertEqual(self.api("/api/ledger/ops?path=/etc/passwd").status, 400)
            self.assertEqual(self.api("/api/ledger/../outside").status, 400)
            with self.assertRaises(channels.ChannelError):
                channels._cmd_bind(argparse.Namespace(name="ops", clear_ledger=True), OTHER)
            with contextlib.redirect_stdout(io.StringIO()):
                channels._cmd_bind(argparse.Namespace(name="ops", clear_ledger=True), self.me)
            self.assertEqual(self.ok("/api/ledger/ops")["state"], "unbound")
