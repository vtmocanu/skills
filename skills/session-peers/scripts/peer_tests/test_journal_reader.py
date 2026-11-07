"""Session-peers journal: the budgeted incremental read-only reader (journal.read_since)."""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from peer_tests.test_journal import JournalBase  # noqa: E402
from session_peers import constants as sp_constants, journal  # noqa: E402

BIG = 10 ** 9


class ReaderBase(JournalBase):
    def setUp(self):
        super().setUp()
        self.preads = []
        real = os.pread

        def counting(fd, n, offset):
            data = real(fd, n, offset)
            self.preads.append(len(data))
            return data

        patcher = mock.patch.object(os, "pread", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- raw fixtures ---------------------------------------------------------

    def line(self, d):
        d = dict(d)
        d.setdefault("schema", 1)
        d.setdefault("at", self.now)
        return journal._encode(d) + b"\n"

    def mrec(self, seq, mid=None, to=None, body="hi", sender=None):
        return self.line({
            "seq": seq, "type": "message", "message_id": mid or str(uuid.uuid4()), "kind": "message",
            "from": sender, "to": to or [self.A], "body": body,
        })

    def ev(self, seq, kind, mid, **kw):
        d = {"seq": seq, "type": kind, "message_id": mid}
        d.update(kw)
        return self.line(d)

    def put(self, *lines, tail=b""):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        with open(self.jpath, "wb") as fh:
            fh.write(b"".join(lines) + tail)
        os.chmod(self.jpath, 0o600)

    def reset_preads(self):
        self.preads.clear()


class BudgetTests(ReaderBase):
    def test_budget_honoured_and_bytes_exact(self):
        lines = [self.mrec(i, body="x" * 200) for i in range(1, 301)]
        self.put(*lines)
        for budget in (1000, 10000, 70000, 65536):
            self.reset_preads()
            res = journal.read_since(None, budget)
            self.assertLessEqual(sum(self.preads), budget)
            self.assertEqual(res["bytes"], sum(self.preads))
            self.assertTrue(max(self.preads) <= 64 * 1024)
            self.assertEqual(res["next"], 300)
            self.assertFalse(res["complete"])
            self.assertEqual(res["journal_prefix"], "unread")

    def test_whole_file_within_budget_is_complete(self):
        self.put(*[self.mrec(i) for i in range(1, 6)])
        res = journal.read_since(None, BIG)
        self.assertTrue(res["complete"])
        self.assertEqual(res["journal_prefix"], "read")
        self.assertEqual([m["seq"] for m in res["messages"]], [1, 2, 3, 4, 5])
        self.assertEqual(res["bytes"], os.path.getsize(self.jpath))

    def test_unchanged_poll_reads_nothing(self):
        self.msg()
        first = journal.read_since(None, BIG)
        self.assertRegex(first["fingerprint"], r"^\d+:\d+:\d+$")
        self.assertEqual(first["fingerprint"], journal.journal_fingerprint())
        self.reset_preads()
        again = journal.read_since(first["next"], BIG, known_fingerprint=first["fingerprint"])
        self.assertEqual(self.preads, [])
        self.assertEqual(again["bytes"], 0)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["messages"] + again["events"], [])
        self.assertEqual(again["next"], first["next"])
        self.msg()
        changed = journal.read_since(first["next"], BIG, known_fingerprint=first["fingerprint"])
        self.assertNotIn("unchanged", changed)
        self.assertEqual(len(changed["messages"]), 1)

    def test_torn_final_line_ignored(self):
        self.put(self.mrec(1), self.mrec(2), tail=b'{"seq":3,"schema":1,"type":"mess')
        res = journal.read_since(None, BIG)
        self.assertEqual([m["seq"] for m in res["messages"]], [1, 2])
        self.assertEqual(res["next"], 2)

    def test_missing_journal_is_empty(self):
        res = journal.read_since(None, BIG)
        self.assertEqual((res["messages"], res["events"], res["context"], res["bytes"]), ([], [], {}, 0))
        self.assertTrue(res["complete"])
        self.assertEqual(res["journal_prefix"], "read")
        self.assertIsNone(res["fingerprint"])
        self.assertFalse(os.path.exists(self.dir))
        os.makedirs(self.dir, mode=0o700)
        self.assertEqual(journal.read_since(None, BIG)["messages"], [])
        self.assertFalse(os.path.exists(self.jpath))


class GapTests(ReaderBase):
    def test_cursor_above_highest_seq(self):
        self.put(self.mrec(1), self.mrec(2))
        res = journal.read_since(50, BIG)
        self.assertIsNotNone(res["gap"])

    def test_budget_too_small_to_reach_cursor(self):
        self.put(*[self.mrec(i, body="y" * 100) for i in range(1, 41)])
        res = journal.read_since(1, 500)
        self.assertIsNotNone(res["gap"])
        self.assertEqual(res["journal_prefix"], "unread")
        near = journal.read_since(39, 500)
        self.assertIsNone(near["gap"])
        self.assertEqual([m["seq"] for m in near["messages"]], [40])

    def test_start_reached_with_first_seq_beyond_cursor(self):
        self.put(self.mrec(10), self.mrec(11))
        self.assertIsNotNone(journal.read_since(3, BIG)["gap"])
        self.assertIsNone(journal.read_since(9, BIG)["gap"])
        self.assertIsNone(journal.read_since(None, BIG)["gap"])
        self.assertIsNone(journal.read_since(0, BIG)["gap"])

    def test_no_gap_when_caught_up(self):
        self.put(self.mrec(1), self.mrec(2))
        res = journal.read_since(2, BIG)
        self.assertIsNone(res["gap"])
        self.assertEqual((res["messages"], res["events"]), ([], []))


class SafetyTests(ReaderBase):
    def test_unsafe_file_mode_raises_like_read_only_view(self):
        self.msg()
        os.chmod(self.jpath, 0o644)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_since(None, BIG)

    def test_unsafe_dir_mode_raises_like_read_only_view(self):
        self.msg()
        os.chmod(self.dir, 0o755)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_since(None, BIG)

    def test_bad_arguments(self):
        for cursor, budget in ((-1, 10), ("1", 10), (True, 10), (None, -1), (None, 1.5)):
            with self.assertRaises(ValueError):
                journal.read_since(cursor, budget)

    def test_reading_changes_nothing(self):
        mid = self.msg()
        journal.ack(mid, self.A)
        self.put(self.read() + self.mrec(99) + b'{"seq":100,"x"')
        names = sorted(os.listdir(self.dir))

        def state():
            out = {"dir": os.stat(self.dir).st_mtime_ns}
            for name in sorted(os.listdir(self.dir)):
                path = os.path.join(self.dir, name)
                out[name] = (os.stat(path).st_mtime_ns, os.stat(path).st_mode, self.read(path))
            return out

        before = state()
        journal.read_since(None, BIG)
        journal.read_since(1, 100, want=[mid])
        journal.read_since(0, 0)
        self.assertEqual(state(), before)
        self.assertEqual(sorted(os.listdir(self.dir)), names)


class ParityTests(ReaderBase):
    def check_parity(self, cursor):
        res = journal.read_since(cursor, BIG, want=[e["message_id"] for e in journal.read_only_view()["entries"]])
        self.assertEqual(res["journal_prefix"], "read")
        for entry in journal.read_only_view()["entries"]:
            ctx = res["context"][entry["message_id"]]
            self.assertEqual(ctx["state"], "found")
            acked = [r["slot"] for r in entry["recipients"] if r["state"] == "acked"]
            self.assertEqual(sorted(ctx["acked"]), sorted(acked))
            errors = {r["slot"]: r["transport_error"]["detail"] for r in entry["recipients"]
                      if "transport_error" in r}
            self.assertEqual(ctx["transport_errors"], errors)
        return res

    def fixture(self):
        self.m1, self.m2 = str(uuid.uuid4()), str(uuid.uuid4())
        self.put(
            self.mrec(1, self.m1, to=[self.A, self.B]),
            self.mrec(2, self.m2, to=[self.A]),
            self.ev(3, "ack", self.m1, **{"for": self.A, "by": self.A}),
            self.ev(4, "ack", self.m1, **{"for": self.A, "by": self.A}),
            self.ev(5, "ack", self.m1, **{"for": self.C, "by": self.C}),
            self.ev(6, "ack", str(uuid.uuid4()), **{"for": self.A, "by": self.A}),
            self.ev(7, "transport_error", self.m1, **{"for": self.B, "detail": "boom1"}),
            self.ev(8, "transport_error", self.m1, **{"for": self.B, "detail": "boom2"}),
            self.ev(4, "ack", self.m2, **{"for": self.A, "by": self.A}),
        )

    def test_parity_from_start(self):
        self.fixture()
        res = self.check_parity(None)
        self.assertEqual(res["context"][self.m1]["acked"], [self.A])
        self.assertEqual(res["context"][self.m1]["transport_errors"], {self.B: "boom2"})
        self.assertEqual(res["context"][self.m2]["acked"], [])
        acks = [e for e in res["events"] if e["type"] == "ack"]
        self.assertEqual([(e["seq"], e["for"]) for e in acks], [(3, self.A)])
        self.assertEqual({e["message_id"] for e in res["events"]}, {self.m1})
        self.assertEqual(res["next"], 8)

    def test_parity_with_ack_after_cursor_for_earlier_message(self):
        self.fixture()
        res = self.check_parity(2)
        self.assertEqual(res["messages"], [])
        self.assertEqual([e["seq"] for e in res["events"]], [3, 7, 8])
        self.assertEqual(res["context"][self.m1]["state"], "found")
        self.assertIsNone(res["gap"])

    def test_real_api_records(self):
        mid = self.msg(to=[self.A, self.B])
        journal.ack(mid, self.A)
        journal.transport_error(mid, self.B, "down")
        res = self.check_parity(None)
        self.assertEqual([e["type"] for e in res["events"]], ["ack", "transport_error"])
        self.assertEqual(res["messages"][0]["message_id"], mid)
        self.assertEqual(res["messages"][0]["body"], "hello")


class PrefixTests(ReaderBase):
    def build(self, head, filler=0):
        self.mid = str(uuid.uuid4())
        self.lines = [self.mrec(i) for i in range(1, filler + 1)]
        self.head = head
        self.m40 = self.mrec(40, self.mid)
        self.a70 = self.ev(70, "ack", self.mid, **{"for": self.A, "by": self.A})
        self.put(*self.lines, head, self.m40, self.a70)
        self.tail = len(self.m40) + len(self.a70)

    def valid_head(self):
        return self.mrec(100, body="z" * 50)

    def malformed_head(self):
        return self.line({"seq": 100, "pad": "q" * 60})

    def test_start_reached_matches_read_only_view(self):
        for head in (self.valid_head(), self.malformed_head()):
            self.build(head)
            res = journal.read_since(50, BIG)
            self.assertEqual(res["journal_prefix"], "read")
            self.assertFalse(res["window_regression"])
            viewed = [e["message_id"] for e in journal.read_only_view()["entries"] if e["seq"] > 50]
            self.assertEqual([m["message_id"] for m in res["messages"]], viewed)
            self.assertEqual(res["events"], [])

    def test_unread_prefix_reported(self):
        for head in (self.valid_head(), self.malformed_head()):
            self.build(head, filler=3)
            res = journal.read_since(50, self.tail + 5)
            self.assertEqual(res["journal_prefix"], "unread")
            self.assertFalse(res["window_regression"])
            self.assertEqual([e["seq"] for e in res["events"]], [70])

    def test_window_regression_flagged(self):
        for head in (self.valid_head(), self.malformed_head()):
            self.build(head, filler=3)
            res = journal.read_since(50, self.tail + len(head) + 5)
            self.assertEqual(res["journal_prefix"], "unread")
            self.assertTrue(res["window_regression"])

    def test_unreached_context(self):
        mid = str(uuid.uuid4())
        ack = self.ev(60, "ack", mid, **{"for": self.A, "by": self.A})
        self.put(self.mrec(1, mid), *[self.mrec(i, body="f" * 50) for i in range(2, 30)], ack)
        res = journal.read_since(None, len(ack) + 5, want=[mid])
        self.assertEqual(res["context"][mid], {"state": "unreached"})
        self.assertEqual(res["events"], [])
        self.assertFalse(res["complete"])
        full = journal.read_since(None, BIG, want=[mid])
        self.assertEqual(full["context"][mid]["state"], "found")

    def test_absent_when_whole_journal_read(self):
        self.put(self.mrec(1))
        ghost = str(uuid.uuid4())
        res = journal.read_since(None, BIG, want=[ghost])
        self.assertEqual(res["context"][ghost], {"state": "absent"})


class NearCapTests(ReaderBase):
    def test_one_complete_record_beyond_budget(self):
        body = "\x01" * sp_constants.MAX_TEXT_CHARS
        small = self.mrec(1)
        big = self.mrec(2, body=body)
        self.put(small, big)
        self.assertGreater(len(big), 6 * sp_constants.MAX_TEXT_CHARS)
        res = journal.read_since(None, 1024 * 1024)
        self.assertLessEqual(res["bytes"], journal.JOURNAL_LINE_MAX)
        self.assertGreater(res["bytes"], 1024 * 1024)
        self.assertEqual(res["bytes"], sum(self.preads))
        self.assertEqual(res["messages"][-1]["seq"], 2)
        self.assertEqual(res["messages"][-1]["body"], body)
        self.assertEqual(res["oversized"], 0)

    def test_line_beyond_limit_is_unreadable(self):
        huge = self.mrec(2, body="\x01" * (sp_constants.MAX_TEXT_CHARS + 100000))
        self.put(self.mrec(1), huge)
        self.assertGreater(len(huge), journal.JOURNAL_LINE_MAX)
        res = journal.read_since(None, 1024)
        self.assertEqual(res["messages"], [])
        self.assertEqual(res["oversized"], 1)
        self.assertLessEqual(res["bytes"], journal.JOURNAL_LINE_MAX + 128 * 1024)


if __name__ == "__main__":
    unittest.main()
