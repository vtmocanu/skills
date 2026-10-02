"""Regression coverage for topics."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import time
from .support import (
    Base,
    PEERS,
    new_uuid,
    sp_codex,
    sp_constants,
    sp_process,
    sp_runtime,
    sp_topics,
)


class TestTopics(Base):
    TOPIC = "repo:example.com/owner/repo"

    def post(self, *extra, topic=None):
        rc, out, err = self.cli("topic", "post", self.TOPIC if topic is None else topic, *extra)
        return rc, out, err

    def tail(self, *extra, topic=None):
        rc, out, err = self.cli("topic", "tail", self.TOPIC if topic is None else topic, "--json", *extra)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_post_then_tail_returns_entries_in_order_with_a_cursor(self):
        for text in ("one", "two", "three"):
            rc, _out, err = self.post("--message", text, "--as", "cc:%s" % new_uuid())
            self.assertEqual(rc, 0, err)
        result = self.tail()
        self.assertEqual([e["seq"] for e in result["entries"]], [1, 2, 3])
        self.assertEqual([e["text"] for e in result["entries"]], ["one", "two", "three"])
        self.assertEqual(result["next"], 3)
        self.assertIsNone(result["gap"])
        self.assertEqual(result["entries"][0]["topic"], self.TOPIC)

    def test_since_and_limit_page_through_the_log(self):
        for i in range(5):
            self.assertEqual(self.post("--message", "m%d" % i, "--as", "cc:%s" % new_uuid())[0], 0)
        page = self.tail("--since", "1", "--limit", "2")
        self.assertEqual([e["seq"] for e in page["entries"]], [2, 3])
        page = self.tail("--since", str(page["next"]), "--limit", "2")
        self.assertEqual([e["seq"] for e in page["entries"]], [4, 5])
        page = self.tail("--since", str(page["next"]))
        self.assertEqual(page["entries"], [])
        self.assertEqual(page["next"], 5)
        self.assertEqual([e["seq"] for e in self.tail("--limit", "2")["entries"]], [4, 5])

    def test_human_tail_escapes_control_characters_and_prints_the_cursor(self):
        self.post("--message", "bell\x07 esc\x1b[31m", "--kind", "note", "--as", "cc:%s" % new_uuid())
        rc, out, err = self.cli("topic", "tail", self.TOPIC)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b[31m", out)
        self.assertIn("[note]", out)
        self.assertTrue(out.rstrip().endswith("next: --since 1"))

    def test_sender_identity_is_resolved_like_send(self):
        sid = new_uuid()
        listener, _rec = self.add_listener(name="cc-poster", session_id=sid)
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = listener.path
        self.assertEqual(self.post("--message", "from claude")[0], 0)
        del os.environ["CLAUDE_CODE_MESSAGING_SOCKET"]
        tid = new_uuid()
        os.environ["CODEX_THREAD_ID"] = tid
        self.assertEqual(self.post("--message", "from codex")[0], 0)
        del os.environ["CODEX_THREAD_ID"]
        rc, _out, err = self.post("--message", "from nobody")
        self.assertEqual(rc, 0, err)
        self.assertIn("posting anonymously", err)
        entries = self.tail()["entries"]
        self.assertEqual(
            [(e["from_kind"], e["from_sid"]) for e in entries],
            [("cc", sid), ("codex", tid), (None, None)],
        )
        self.assertEqual(entries[0]["from_name"], "cc-poster")

    def both_identities(self, pid):
        """A live Claude session (record pid ``pid``) and a Codex thread, both
        named in this environment."""
        sid, tid = new_uuid(), new_uuid()
        listener, _rec = self.add_listener(name="cc-poster", session_id=sid, pid=pid)
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = listener.path
        os.environ["CODEX_THREAD_ID"] = tid
        return sid, tid

    def process_view(self, ancestors, holder):
        for owner, name, fake in (
            (sp_process, "process_ancestors", lambda pid=None, limit=64: ancestors),
            (sp_codex, "_codex_holder_pid", lambda _tid: holder),
        ):
            self.addCleanup(setattr, owner, name, getattr(owner, name))
            setattr(owner, name, fake)

    def test_with_both_identities_the_nearer_ancestor_posts(self):
        claude_pid = os.getpid()
        sid, tid = self.both_identities(claude_pid)
        self.process_view([100, claude_pid, 7777, 1], holder=7777)
        self.assertEqual(self.post("--message", "claude is nearer")[0], 0)
        self.process_view([100, 7777, claude_pid, 1], holder=7777)
        self.assertEqual(self.post("--message", "codex is nearer")[0], 0)
        entries = self.tail()["entries"]
        self.assertEqual(
            [(e["from_kind"], e["from_sid"]) for e in entries],
            [("cc", sid), ("codex", tid)],
        )

    def test_with_both_identities_an_unverifiable_owner_is_refused(self):
        sid, tid = self.both_identities(os.getpid())
        for ancestors, holder in (([100, 1], 7777), (None, 7777), ([], None)):
            self.process_view(ancestors, holder)
            rc, _out, err = self.post("--message", "who am I")
            self.assertEqual(rc, 2, (ancestors, holder))
            self.assertIn("cannot be verified", err)
            self.assertIn("--as cc:%s or --as codex:%s" % (sid, tid), err)
        self.assertEqual(self.tail()["entries"], [])
        rc, _out, err = self.post("--message", "explicit", "--as", "codex:%s" % tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tail()["entries"][0]["from_sid"], tid)

    def test_process_ancestors_walks_the_ps_table(self):
        me = os.getpid()
        self._write_fake("ps", "print('  1 0\\n  50 1\\n  60 50\\n %d 60')\n" % me)
        self.assertEqual(sp_process.process_ancestors(), [60, 50, 1])
        self._write_fake("ps", "raise SystemExit(1)\n")
        self.assertIsNone(sp_process.process_ancestors())

    def test_json_file_is_stored_as_data(self):
        path = self.root / "payload.json"
        path.write_text(json.dumps({"state": "merged", "n": 3}))
        rc, out, err = self.post("--json-file", str(path), "--kind", "event", "--json",
                                 "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["seq"], 1)
        entry = self.tail()["entries"][0]
        self.assertEqual(entry["data"], {"state": "merged", "n": 3})
        self.assertNotIn("text", entry)
        self.assertEqual(entry["kind"], "event")
        path.write_text("{not json")
        rc, _out, err = self.post("--json-file", str(path), "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", err)

    def test_concurrent_posts_from_many_processes_get_unique_monotonic_seqs(self):
        writers, each = 6, 15
        script = (
            "import sys; sys.path.insert(0, %r); from session_peers import topics\n"
            "for i in range(%d):\n"
            "    topics.topic_post(%r, {'kind': 'cc', 'uuid': sys.argv[1]}, text=str(i))\n"
            % (str(PEERS.parent), each, self.TOPIC)
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", script, "w%d" % n], env=dict(os.environ))
            for n in range(writers)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        entries = self.tail("--since", "0", "--limit", "1000")["entries"]
        seqs = [e["seq"] for e in entries]
        self.assertEqual(seqs, list(range(1, writers * each + 1)))
        for n in range(writers):
            mine = [int(e["text"]) for e in entries if e["from_sid"] == "w%d" % n]
            self.assertEqual(mine, list(range(each)))

    def test_pruning_keeps_seq_numbering_and_reports_the_gap(self):
        os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"] = "3"
        for i in range(6):
            self.post("--message", "m%d" % i, "--as", "cc:%s" % new_uuid())
        result = self.tail("--since", "1")
        self.assertEqual(result["gap"], {"from": 2, "to": 3})
        self.assertEqual([e["seq"] for e in result["entries"]], [4, 5, 6])
        rc, out, _err = self.cli("topic", "tail", self.TOPIC, "--since", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "gap: entries 1..3 pruned")
        self.assertIsNone(self.tail("--since", "3")["gap"])
        # Pruning every entry by age still continues the numbering.
        os.environ["SESSION_PEERS_TOPIC_TTL_DAYS"] = "0.000001"
        time.sleep(0.2)
        result = self.tail("--since", "5")
        self.assertEqual((result["entries"], result["gap"]), ([], {"from": 6, "to": 6}))
        self.assertEqual(result["next"], 6)
        del os.environ["SESSION_PEERS_TOPIC_TTL_DAYS"]
        self.post("--message", "after", "--as", "cc:%s" % new_uuid())
        self.assertEqual([e["seq"] for e in self.tail()["entries"]], [7])

    def test_gc_applies_retention_to_idle_topics_without_resetting_seq(self):
        self.post("--message", "old", "--as", "cc:%s" % new_uuid())
        os.environ["SESSION_PEERS_TOPIC_TTL_DAYS"] = "0.000001"
        time.sleep(0.2)
        self.make_state_db([])
        rc, _out, err = self.cli("gc")
        self.assertEqual(rc, 0, err)
        log_path, meta_path, _lock = sp_topics._topic_paths(self.TOPIC)
        self.assertEqual(pathlib.Path(log_path).read_text(), "")
        self.assertEqual(sp_runtime.read_json(meta_path)["next_seq"], 2)

    def test_topic_names_cannot_escape_the_topics_directory(self):
        for topic in ("../../escape", "/etc/passwd", "a/../../b", "..", "x\\..\\y"):
            rc, _out, err = self.post("--message", "x", "--as", "cc:%s" % new_uuid(), topic=topic)
            self.assertEqual(rc, 0, err)
            self.assertEqual(self.tail(topic=topic)["entries"][0]["topic"], topic)
        topics = pathlib.Path(sp_topics.topics_dir())
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix in (".jsonl", ".lock") or path.name.endswith(".meta.json"):
                self.assertEqual(path.parent, topics, path)
        self.assertEqual(stat.S_IMODE(topics.stat().st_mode), 0o700)
        for path in topics.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        listed = json.loads(self.cli("topic", "list", "--json")[1])
        self.assertEqual(len(listed), 5)

    def test_invalid_topics_and_kinds_are_refused(self):
        for topic in ("", "   ", "a\nb", "tab\there", "x" * (sp_constants.TOPIC_MAX_CHARS + 1)):
            rc, _out, _err = self.post("--message", "x", "--as", "cc:%s" % new_uuid(), topic=topic)
            self.assertEqual(rc, 2, repr(topic))
        rc, _out, err = self.post("--message", "x", "--kind", "bad kind", "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 2)
        self.assertIn("--kind", err)
        self.assertFalse(os.listdir(sp_topics.topics_dir()))

    def test_oversize_and_invalid_utf8_entries_are_refused(self):
        big = self.root / "big.txt"
        big.write_text("x" * (sp_constants.MAX_TEXT_CHARS + 1))
        rc, _out, err = self.post("--message-file", str(big), "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 1)
        self.assertIn("over the", err)
        bad = self.root / "bad.txt"
        bad.write_bytes(b"\xff\xfe")
        rc, _out, err = self.post("--message-file", str(bad), "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 1)
        rc, _out, err = self.post("--message", "\udcff", "--as", "cc:%s" % new_uuid())
        self.assertEqual(rc, 1)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual(self.tail()["entries"], [])

    def test_list_reports_last_seq_and_time(self):
        self.post("--message", "a", "--as", "cc:%s" % new_uuid(), topic="t1")
        self.post("--message", "b", "--as", "cc:%s" % new_uuid(), topic="t1")
        self.post("--message", "c", "--as", "cc:%s" % new_uuid(), topic="t2")
        listed = json.loads(self.cli("topic", "list", "--json")[1])
        self.assertEqual([(t["topic"], t["last_seq"]) for t in listed], [("t1", 2), ("t2", 1)])
        self.assertTrue(all(t["last_ts"] for t in listed))
        self.assertEqual(self.cli("topic", "list")[0], 0)
