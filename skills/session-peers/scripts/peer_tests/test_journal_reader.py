"""Session-peers journal: the budgeted incremental read-only reader (journal.read_since)."""

from __future__ import annotations

import os
import sys
import time
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
        self.assertRegex(first["fingerprint"], r"^\d+(:\d+){7}$")
        self.assertFalse(first["unchanged"])
        self.assertEqual(first["fingerprint"], journal.journal_fingerprint())
        self.reset_preads()
        again = journal.read_since(first["next"], BIG, known_fingerprint=first["fingerprint"])
        self.assertEqual(self.preads, [])
        self.assertEqual(again["bytes"], 0)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["messages"] + again["events"], [])
        self.assertEqual(again["next"], first["next"])
        self.assertEqual(again["context"], {})
        for key in ("complete", "journal_prefix", "gap", "window_regression", "oversized", "next_anchor"):
            self.assertIn(key, again)
            self.assertIsNone(again[key])
        self.msg()
        changed = journal.read_since(first["next"], BIG, known_fingerprint=first["fingerprint"])
        self.assertFalse(changed["unchanged"])
        self.assertEqual(len(changed["messages"]), 1)

    def test_chmod_invalidates_fingerprint(self):
        self.msg()
        fp = journal.read_since(None, BIG)["fingerprint"]
        os.chmod(self.jpath, 0o644)
        self.assertNotEqual(journal.journal_fingerprint(), fp)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_since(None, BIG, known_fingerprint=fp)

    def test_dir_chmod_invalidates_fingerprint(self):
        self.msg()
        fp = journal.read_since(None, BIG)["fingerprint"]
        os.chmod(self.dir, 0o777)
        self.assertNotEqual(journal.journal_fingerprint(), fp)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_since(None, BIG, known_fingerprint=fp)

    def test_dir_replaced_by_symlink_invalidates_fingerprint(self):
        self.msg()
        fp = journal.read_since(None, BIG)["fingerprint"]
        real = self.dir + ".real"
        os.rename(self.dir, real)
        os.symlink(real, self.dir)
        self.assertNotEqual(journal.journal_fingerprint(), fp)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_since(None, BIG, known_fingerprint=fp)

    def test_same_size_rewrite_with_restored_mtime_changes_fingerprint(self):
        self.put(self.mrec(1, body="aaaa"))
        old = os.stat(self.jpath)
        fp = journal.read_since(None, BIG)["fingerprint"]
        time.sleep(0.05)
        data = self.read().replace(b"aaaa", b"bbbb")
        with open(self.jpath, "r+b") as fh:
            fh.write(data)
        os.utime(self.jpath, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(os.stat(self.jpath).st_size, old.st_size)
        self.assertEqual(os.stat(self.jpath).st_mtime_ns, old.st_mtime_ns)
        self.assertNotEqual(journal.journal_fingerprint(), fp)
        res = journal.read_since(None, BIG, known_fingerprint=fp)
        self.assertFalse(res["unchanged"])
        self.assertEqual([m["body"] for m in res["messages"]], ["bbbb"])

    def test_inode_change_invalidates_fingerprint(self):
        self.put(self.mrec(1), self.mrec(2))
        old = os.stat(self.jpath)
        fp = journal.journal_fingerprint()
        tmp = self.jpath + ".swap"
        with open(tmp, "wb") as fh:
            fh.write(self.read())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.jpath)
        os.utime(self.jpath, ns=(old.st_atime_ns, old.st_mtime_ns))
        new_fp = journal.journal_fingerprint()
        old_fields, new_fields = fp.split(":"), new_fp.split(":")
        self.assertEqual((old_fields[0], old_fields[2]), (new_fields[0], new_fields[2]))
        self.assertNotEqual(old_fields[1], new_fields[1])
        self.assertNotEqual(fp, new_fp)
        res = journal.read_since(None, BIG, known_fingerprint=fp)
        self.assertFalse(res["unchanged"])
        self.assertEqual(len(res["messages"]), 2)

    def test_final_complete_record_without_newline_ignored(self):
        self.put(self.mrec(1), tail=self.mrec(2).rstrip(b"\n"))
        res = journal.read_since(None, BIG)
        self.assertEqual([m["seq"] for m in res["messages"]], [1])
        self.assertEqual(res["next"], 1)

    def test_budget_just_below_size_leaves_prefix_unread(self):
        self.put(*[self.mrec(i) for i in range(1, 6)])
        size = os.path.getsize(self.jpath)
        res = journal.read_since(None, size - 1)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertFalse(res["complete"])
        self.assertEqual(res["bytes"], size - 1)
        exact = journal.read_since(None, size)
        self.assertEqual(exact["journal_prefix"], "read")

    def test_short_read_is_unavailable(self):
        self.put(*[self.mrec(i) for i in range(1, 6)])
        counting = os.pread
        with mock.patch.object(os, "pread", lambda fd, n, off: counting(fd, max(n - 1, 0), off)):
            with self.assertRaises(journal.JournalUnavailable):
                journal.read_since(None, BIG)

    def test_small_budget_reads_only_one_line_plus_a_step(self):
        self.put(*[self.mrec(i, body="x" * 250) for i in range(1, 1001)])
        size = os.path.getsize(self.jpath)
        line = len(self.mrec(1, body="x" * 250))
        self.assertGreater(size, 100 * line)
        for budget in (0, 10, 200):
            self.reset_preads()
            res = journal.read_since(None, budget)
            self.assertEqual(res["bytes"], sum(self.preads))
            self.assertLess(res["bytes"], 8 * 1024)
            self.assertGreaterEqual(len(res["messages"]), 1)
            self.assertEqual(res["next"], 1000)
        for budget in (1000, 5000):
            self.reset_preads()
            res = journal.read_since(None, budget)
            self.assertLessEqual(res["bytes"], budget)
            self.assertEqual(res["bytes"], sum(self.preads))
            self.assertGreaterEqual(len(res["messages"]), 1)

    def test_complete_line_in_window_reads_nothing_beyond_budget(self):
        self.put(*[self.mrec(i, body="x" * 250) for i in range(1, 1001)])
        line = len(self.mrec(1, body="x" * 250))
        res = journal.read_since(None, 3 * line)
        self.assertEqual(res["bytes"], 3 * line)
        self.assertEqual(sum(self.preads), 3 * line)

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
        self.assertEqual(res["gap"], {"from": 50, "to": 1, "reason": "regression"})

    def test_window_without_seq_is_not_a_regression(self):
        self.put(*[self.mrec(i, body="x" * 400) for i in range(1, 300)], b"garbage\n")
        res = journal.read_since(299, 1)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertIsNone(res["gap"])
        self.assertEqual(res["next"], 299)
        self.put(*[self.mrec(i, body="x" * 100000) for i in range(1, 4)], b"g" * 6000 + b"\n")
        res = journal.read_since(299, 1)
        self.assertEqual(res["gap"], {"from": 299, "to": None, "reason": "budget"})
        self.assertEqual(res["next"], 299)

    def test_unread_window_below_cursor_is_a_regression(self):
        self.put(*[self.mrec(i, body="x" * 400) for i in range(1, 100)])
        res = journal.read_since(500, 1000)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertEqual(res["gap"]["reason"], "regression")
        self.assertEqual(res["gap"]["from"], 500)

    def test_budget_too_small_to_reach_cursor(self):
        self.put(*[self.mrec(i, body="y" * 100) for i in range(1, 41)])
        res = journal.read_since(1, 500)
        self.assertEqual(res["gap"]["reason"], "budget")
        self.assertEqual((res["gap"]["from"], res["gap"]["to"]), (1, res["messages"][0]["seq"]))
        self.assertGreater(res["gap"]["to"], 2)
        self.assertEqual(res["journal_prefix"], "unread")
        near = journal.read_since(39, 500)
        self.assertIsNone(near["gap"])
        self.assertEqual([m["seq"] for m in near["messages"]], [40])

    def test_malformed_line_seqs_count_for_the_pruned_gap(self):
        bad = self.line({"seq": 4, "pad": "q"})
        self.put(bad, self.mrec(5))
        self.assertIsNone(journal.read_since(3, BIG)["gap"])
        self.assertEqual(journal.read_since(2, BIG)["gap"], {"from": 2, "to": 4, "reason": "pruned"})

    def test_start_reached_with_first_seq_beyond_cursor(self):
        self.put(self.mrec(10), self.mrec(11))
        self.assertEqual(journal.read_since(3, BIG)["gap"], {"from": 3, "to": 10, "reason": "pruned"})
        self.assertIsNone(journal.read_since(9, BIG)["gap"])
        self.assertIsNone(journal.read_since(None, BIG)["gap"])
        self.assertIsNone(journal.read_since(0, BIG)["gap"])

    def test_fully_read_journal_without_seq_below_cursor_is_a_regression(self):
        expected = {"from": 5, "to": None, "reason": "regression"}
        self.put()
        self.assertEqual(journal.read_since(5, BIG)["gap"], expected)
        self.put(b"garbage\n")
        self.assertEqual(journal.read_since(5, BIG)["gap"], expected)

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
        self.assertEqual([(e["seq"], e.get("unverified")) for e in res["events"]], [(60, True)])
        self.assertFalse(res["complete"])
        full = journal.read_since(None, BIG, want=[mid])
        self.assertEqual(full["context"][mid]["state"], "found")

    def filler_journal(self, count=1000):
        self.mid = str(uuid.uuid4())
        self.fill = [self.mrec(i, body="x" * 300) for i in range(1, count)]
        self.last = self.mrec(count, self.mid)
        self.put(*self.fill, self.last, self.ev(count + 1, "ack", self.mid, **{"for": self.A, "by": self.A}))
        return os.path.getsize(self.jpath)

    def test_want_stops_reading_at_the_wanted_message(self):
        size = self.filler_journal()
        res = journal.read_since(None, BIG, want=[self.mid])
        self.assertLess(res["bytes"], size // 4)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertEqual(res["context"][self.mid]["state"], "found")
        self.assertEqual(res["context"][self.mid]["acked"], [self.A])
        self.assertEqual([e["type"] for e in res["events"]], ["ack"])

    def test_cursor_stops_reading_at_the_anchor(self):
        lines = [self.mrec(i, body="x" * 300) for i in range(1, 1001)]
        self.put(*lines[:990])
        prev = journal.read_since(None, BIG)
        self.assertEqual(prev["next"], 990)
        self.put(*lines)
        size = os.path.getsize(self.jpath)
        res = journal.read_since(990, BIG, cursor_anchor=prev["next_anchor"])
        self.assertLess(res["bytes"], size // 4)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertIsNone(res["gap"])
        self.assertEqual([m["seq"] for m in res["messages"]][-1], 1000)

    def test_cursor_reads_on_to_the_message_of_a_later_event(self):
        mid = str(uuid.uuid4())
        lines = [self.mrec(1, mid)] + [self.mrec(i, body="x" * 300) for i in range(2, 1000)]
        self.put(*lines, self.ev(1000, "ack", mid, **{"for": self.A, "by": self.A}))
        res = journal.read_since(990, BIG)
        self.assertEqual(res["journal_prefix"], "read")
        self.assertEqual(res["context"][mid]["state"], "found")
        self.assertEqual(res["context"][mid]["acked"], [self.A])
        self.assertEqual([("unverified" in e) for e in res["events"]], [False])

    def test_event_with_unreached_message_is_unverified(self):
        mid = str(uuid.uuid4())
        ack = self.ev(60, "ack", mid, **{"for": self.A, "by": self.A})
        self.put(self.mrec(1, mid), *[self.mrec(i, body="f" * 50) for i in range(2, 60)], ack)
        res = journal.read_since(59, len(ack) + 5)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertEqual([(e["seq"], e["message_id"], e.get("unverified")) for e in res["events"]], [(60, mid, True)])
        self.assertEqual(res["context"][mid], {"state": "unreached"})
        self.assertEqual(res["next"], 60)
        self.assertFalse(res["complete"])

    def test_dangling_event_dropped_when_journal_fully_read(self):
        ghost = str(uuid.uuid4())
        self.put(self.mrec(1), self.ev(2, "ack", ghost, **{"for": self.A, "by": self.A}))
        res = journal.read_since(None, BIG)
        self.assertEqual(res["events"], [])
        self.assertEqual(res["next"], 2)

    def test_events_at_or_below_cursor_are_not_returned(self):
        mid = str(uuid.uuid4())
        self.put(self.mrec(1, mid, to=[self.A, self.B]),
                 self.ev(2, "ack", mid, **{"for": self.A, "by": self.A}),
                 self.mrec(3),
                 self.ev(4, "transport_error", mid, **{"for": self.B, "detail": "d"}))
        res = journal.read_since(2, BIG)
        self.assertEqual([e["seq"] for e in res["events"]], [4])
        self.assertEqual([m["seq"] for m in res["messages"]], [3])
        self.assertEqual(res["context"][mid]["acked"], [self.A])

    def test_foreign_records_in_window_are_ignored(self):
        self.put(self.mrec(1), self.line({"seq": 2, "schema": 2, "type": "zzz"}),
                 self.line({"seq": 3, "type": "future"}), self.mrec(4))
        res = journal.read_since(None, BIG)
        self.assertEqual([m["seq"] for m in res["messages"]], [1, 4])
        self.assertEqual((res["events"], res["oversized"], res["next"]), ([], 0, 4))

    def test_absent_when_whole_journal_read(self):
        self.put(self.mrec(1))
        ghost = str(uuid.uuid4())
        res = journal.read_since(None, BIG, want=[ghost])
        self.assertEqual(res["context"][ghost], {"state": "absent"})


class EarlyStopTests(ReaderBase):
    def stale_journal(self, stale):
        lines = [self.mrec(i, body="x" * 3000) for i in range(1, 101)]
        lines.append(stale)
        lines += [self.mrec(i, body="x" * 3000) for i in range(101, 111)]
        self.put(*lines)

    def test_stale_low_seq_line_does_not_stop_the_read(self):
        for stale in (self.mrec(5, body="x" * 3000), self.line({"seq": 5, "junk": True})):
            self.stale_journal(stale)
            res = journal.read_since(50, BIG)
            viewed = [e["message_id"] for e in journal.read_only_view()["entries"] if e["seq"] > 50]
            self.assertEqual(len(viewed), 60)
            self.assertEqual([m["message_id"] for m in res["messages"]], viewed)
            self.assertIsNone(res["gap"])
            if res["window_regression"]:
                self.assertFalse(res["complete"])

    def test_stale_line_with_budget_stop_is_not_complete(self):
        self.stale_journal(self.mrec(5, body="x" * 3000))
        res = journal.read_since(50, 20000)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertFalse(res["complete"])

    def test_regression_in_unread_window_is_not_complete(self):
        # File order 1..105, stale 5, 106..110. The window holds 105 (the anchor), the stale
        # line and 106..110, so the replay sees 105 then 5: a regression after the anchor.
        lines = [self.mrec(i, body="x" * 3000) for i in range(1, 111)]
        self.put(*lines[:105])
        prev = journal.read_since(None, BIG)
        stale = self.mrec(5, body="x" * 3000)
        lines.insert(105, stale)
        self.put(*lines)
        budget = sum(len(l) for l in lines[104:]) + 10
        res = journal.read_since(105, budget, cursor_anchor=prev["next_anchor"])
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertTrue(res["window_regression"])
        self.assertFalse(res["complete"])

    def test_foreign_ack_beyond_window_is_not_returned(self):
        mid = str(uuid.uuid4())
        foreign = self.line({"seq": 60, "schema": 2, "type": "ack", "message_id": mid,
                             "for": self.A, "by": self.A})
        self.put(self.mrec(1, mid), *[self.mrec(i, body="f" * 50) for i in range(2, 60)], foreign)
        res = journal.read_since(59, len(foreign) + 5)
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertEqual((res["events"], res["context"]), ([], {}))

    def test_cursor_with_want_reads_on_to_the_wanted_message(self):
        mid = str(uuid.uuid4())
        self.put(self.mrec(1, mid), *[self.mrec(i, body="x" * 300) for i in range(2, 1000)])
        res = journal.read_since(990, BIG, want=[mid])
        self.assertEqual(res["context"][mid]["state"], "found")
        self.assertEqual(res["context"][mid]["seq"], 1)

    def test_seqless_garbage_does_not_stop_the_read(self):
        lines = [self.mrec(i, body="x" * 300) for i in range(1, 101)]
        lines.insert(70, b"garbage\n")
        self.put(*lines)
        res = journal.read_since(60, BIG)
        self.assertEqual([m["seq"] for m in res["messages"]], list(range(61, 101)))
        self.assertIsNone(res["gap"])

    def test_two_wants_without_cursor_are_both_found(self):
        old, recent = str(uuid.uuid4()), str(uuid.uuid4())
        self.put(self.mrec(1, old), *[self.mrec(i, body="x" * 300) for i in range(2, 999)], self.mrec(999, recent))
        res = journal.read_since(None, BIG, want=[old, recent])
        self.assertEqual(res["context"][old]["state"], "found")
        self.assertEqual(res["context"][recent]["state"], "found")

    def test_events_across_messages_are_in_seq_order(self):
        m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
        self.put(self.mrec(1, m1), self.mrec(2, m2),
                 self.ev(3, "ack", m2, **{"for": self.A, "by": self.A}),
                 self.ev(4, "ack", m1, **{"for": self.A, "by": self.A}))
        res = journal.read_since(None, BIG)
        self.assertEqual([(e["seq"], e["message_id"]) for e in res["events"]], [(3, m2), (4, m1)])

    def test_foreign_record_sharing_a_wanted_id_does_not_end_the_read(self):
        mid = str(uuid.uuid4())
        foreign = self.line({"seq": 999, "schema": 2, "type": "message", "message_id": mid})
        self.put(self.mrec(1, mid), *[self.mrec(i, body="x" * 300) for i in range(2, 999)], foreign)
        res = journal.read_since(None, BIG, want=[mid])
        self.assertEqual(res["context"][mid]["state"], "found")
        self.assertEqual(res["context"][mid]["seq"], 1)


class AnchorTests(ReaderBase):
    BODY = "x" * 3000

    def mr(self, seq):
        return self.mrec(seq, body=self.BODY)

    def anchored(self, prefix, rest, cursor=None):
        """Write ``prefix``, read it to get the anchor at its last seq, then write the whole journal."""
        self.put(*prefix)
        prev = journal.read_since(None, BIG)
        self.assertEqual(prev["next"], cursor if cursor is not None else len(prefix))
        self.put(*prefix, *rest)
        return prev

    def assert_no_silent_loss(self, cursor, budget, anchor, label):
        expected = [e["seq"] for e in journal.read_only_view()["entries"] if e["seq"] > cursor]
        res = journal.read_since(cursor, budget, cursor_anchor=anchor)
        got = [m["seq"] for m in res["messages"]]
        if res["complete"]:
            self.assertEqual(got, expected, "%s budget=%s: complete with loss" % (label, budget))
        return res

    def check_variant(self, label, prefix, rest, budgets=(BIG,)):
        cursor = len(prefix)
        prev = self.anchored(prefix, rest)
        for chunk in (4096, 512):
            with mock.patch.object(journal, "_READ_CHUNK", chunk):
                for budget in budgets:
                    for anchor in (prev["next_anchor"], None):
                        self.assert_no_silent_loss(cursor, budget, anchor, "%s chunk=%d" % (label, chunk))

    def variant(self, stale, n=100, tail=10, cursor=50):
        lines = [self.mr(i) for i in range(1, n + 1)]
        lines += stale + [self.mr(i) for i in range(n + 1, n + tail + 1)]
        return lines[:cursor], lines[cursor:]

    def test_stale_runs_never_lose_records_silently(self):
        mr = self.mr
        cases = {
            "V3 stale 4,5": [mr(4), mr(5)],
            "V5 stale 3,4,5": [mr(3), mr(4), mr(5)],
            "V9 stale 49,50": [mr(49), mr(50)],
            "V1 one stale": [mr(5)],
        }
        for label, stale in cases.items():
            self.check_variant(label, *self.variant(stale))

    def test_budget_ending_next_to_a_stale_line_is_not_complete_with_loss(self):
        mr = self.mr
        for label, stale in (("V16", [mr(5)]), ("V17", [mr(4), mr(5)])):
            prefix, rest = self.variant(stale)
            lines = prefix + rest
            for extra in (0, 1, 2):
                size = sum(len(l) for l in lines[-(10 + len(stale) + extra):])
                self.check_variant("%s+%d" % (label, extra), prefix, rest, budgets=(size - 1, size, size + 1))

    def test_stale_run_in_the_middle_of_the_window(self):
        mr = self.mr
        lines = [mr(i) for i in range(1, 81)] + [mr(4), mr(5)] + [mr(i) for i in range(81, 111)]
        self.check_variant("V18", lines[:50], lines[50:])

    def test_stale_run_after_105(self):
        mr = self.mr
        lines = [mr(i) for i in range(1, 106)] + [mr(9), mr(10)] + [mr(i) for i in range(106, 111)]
        self.check_variant("V19", lines[:60], lines[60:])

    def test_stale_run_is_reported_not_complete_with_anchor(self):
        # With a matching anchor the read ends at it, and the stale run after it is a regression.
        prefix, rest = self.variant([self.mr(4), self.mr(5)])
        prev = self.anchored(prefix, rest)
        with mock.patch.object(journal, "_READ_CHUNK", 4096):
            res = journal.read_since(50, BIG, cursor_anchor=prev["next_anchor"])
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertTrue(res["window_regression"])
        self.assertFalse(res["complete"])

    def well_formed(self, count=300, cursor=290):
        lines = [self.mrec(i, body="x" * 300) for i in range(1, count + 1)]
        prev = self.anchored(lines[:cursor], lines[cursor:])
        return prev, os.path.getsize(self.jpath)

    def test_matching_anchor_stops_early_and_is_complete(self):
        prev, size = self.well_formed()
        with mock.patch.object(journal, "_READ_CHUNK", 4096):
            res = journal.read_since(290, BIG, cursor_anchor=prev["next_anchor"])
        self.assertLess(res["bytes"], size // 4)
        self.assertTrue(res["complete"])
        self.assertEqual(res["journal_prefix"], "unread")
        self.assertIsNone(res["gap"])
        expected = [e["seq"] for e in journal.read_only_view()["entries"] if e["seq"] > 290]
        self.assertEqual([m["seq"] for m in res["messages"]], expected)
        self.assertEqual(res["next"], 300)

    def test_without_anchor_the_read_does_not_stop_early(self):
        prev, size = self.well_formed()
        with mock.patch.object(journal, "_READ_CHUNK", 4096):
            res = journal.read_since(290, BIG)
            self.assertEqual(res["bytes"], size)
            self.assertEqual(res["journal_prefix"], "read")
            budget = size // 4
            self.reset_preads()
            part = journal.read_since(290, budget)
        self.assertGreaterEqual(part["bytes"], budget)
        self.assertFalse(part["complete"])
        self.assertEqual(part["journal_prefix"], "unread")

    def test_wrong_anchor_does_not_stop_early(self):
        prev, size = self.well_formed()
        # Rewrite so the record at seq 290 differs from the one the anchor was taken from.
        lines = [self.mrec(i, body="y" * 300) for i in range(1, 301)]
        self.put(*lines)
        size = os.path.getsize(self.jpath)
        with mock.patch.object(journal, "_READ_CHUNK", 4096):
            for anchor in (prev["next_anchor"], "0" * 16, "", 5):
                res = journal.read_since(290, BIG, cursor_anchor=anchor)
                self.assertEqual(res["bytes"], size)
                self.assertEqual(res["journal_prefix"], "read")

    def test_anchor_is_for_the_record_at_cursor_only(self):
        prev, size = self.well_formed()
        with mock.patch.object(journal, "_READ_CHUNK", 4096):
            res = journal.read_since(289, BIG, cursor_anchor=prev["next_anchor"])
        self.assertEqual(res["bytes"], size)

    def test_next_anchor_is_the_hash_of_the_record_at_next(self):
        import hashlib
        lines = [self.mrec(i) for i in range(1, 4)]
        self.put(*lines)
        res = journal.read_since(None, BIG)
        self.assertEqual(res["next"], 3)
        self.assertEqual(res["next_anchor"], hashlib.sha256(lines[2][:-1]).hexdigest()[:16])
        self.assertEqual(len(res["next_anchor"]), 16)

    def test_next_anchor_is_none_without_a_valid_record_at_next(self):
        self.assertIsNone(journal.read_since(None, BIG)["next_anchor"])
        self.put(b"garbage\n", self.line({"seq": 7, "junk": True}))
        res = journal.read_since(3, BIG)
        self.assertIsNone(res["next_anchor"])
        self.put(self.mrec(1), self.line({"seq": 2, "schema": 2, "type": "zzz"}))
        self.assertIsNone(journal.read_since(None, BIG)["next_anchor"])

    def test_next_anchor_is_none_when_a_malformed_line_sets_next(self):
        self.put(self.mrec(1), self.mrec(2), self.line({"seq": 3, "junk": True}))
        res = journal.read_since(None, BIG)
        self.assertEqual(res["next"], 3)
        self.assertIsNone(res["next_anchor"])

    def test_malformed_line_above_the_cursor_drops_the_callers_anchor(self):
        self.put(self.mrec(1), self.mrec(2))
        first = journal.read_since(None, BIG)
        self.put(self.mrec(1), self.mrec(2), self.line({"seq": 3, "junk": True}))
        res = journal.read_since(2, BIG, cursor_anchor=first["next_anchor"])
        self.assertEqual(res["next"], 3)
        self.assertIsNone(res["next_anchor"])

    def test_malformed_line_repeating_the_last_seq_keeps_the_anchor(self):
        self.put(self.mrec(1), self.mrec(2), self.line({"seq": 2, "junk": True}))
        res = journal.read_since(None, BIG)
        self.assertEqual(res["next"], 2)
        self.assertIsNotNone(res["next_anchor"])

    def test_unchanged_poll_keeps_the_callers_anchor(self):
        self.put(self.mrec(1), self.mrec(2))
        first = journal.read_since(None, BIG)
        res = journal.read_since(2, BIG, known_fingerprint=first["fingerprint"],
                                 cursor_anchor=first["next_anchor"])
        self.assertTrue(res["unchanged"])
        self.assertEqual(res["next"], 2)
        self.assertEqual(res["next_anchor"], first["next_anchor"])

    def test_next_falling_back_to_the_cursor_keeps_the_callers_anchor(self):
        self.put(b"garbage\n", b"more garbage\n")
        res = journal.read_since(3, BIG, cursor_anchor="a" * 16)
        self.assertEqual(res["next"], 3)
        self.assertEqual(res["next_anchor"], "a" * 16)

    def anchored_tail(self, extra=(), mids=None):
        """50 records, the anchor read, then 10 more records and ``extra`` lines."""
        self.mids = {i: str(uuid.uuid4()) for i in range(1, 61)}
        self.mids.update(mids or {})
        prefix = [self.mrec(i, mid=self.mids[i], body=self.BODY) for i in range(1, 51)]
        rest = [self.mrec(i, mid=self.mids[i], body=self.BODY) for i in range(51, 61)] + list(extra)
        prev = self.anchored(prefix, rest)
        return prefix, rest, prev["next_anchor"]

    def test_anchor_in_window_but_wanted_message_not_is_not_complete(self):
        prefix, rest, anchor = self.anchored_tail()
        lines = prefix + rest
        budget = sum(len(l) for l in lines[-11:]) + 10
        with mock.patch.object(journal, "_READ_CHUNK", 512):
            res = journal.read_since(50, budget, want=[self.mids[10]], cursor_anchor=anchor)
        self.assertFalse(res["complete"])
        self.assertEqual(res["context"][self.mids[10]]["state"], "unreached")

    def test_wanted_old_message_is_found_with_a_big_budget_after_the_anchor(self):
        prefix, rest, anchor = self.anchored_tail()
        with mock.patch.object(journal, "_READ_CHUNK", 512):
            res = journal.read_since(50, BIG, want=[self.mids[10]], cursor_anchor=anchor)
        self.assertTrue(res["complete"])
        self.assertEqual(res["context"][self.mids[10]]["state"], "found")

    def test_ack_for_an_old_message_after_the_anchor_is_verified(self):
        m5 = str(uuid.uuid4())
        ack = self.ev(61, "ack", m5, **{"for": self.A, "by": self.A})
        prefix, rest, anchor = self.anchored_tail([ack], {5: m5})
        with mock.patch.object(journal, "_READ_CHUNK", 512):
            res = journal.read_since(50, BIG, cursor_anchor=anchor)
        self.assertTrue(res["complete"])
        self.assertEqual(res["context"][m5]["state"], "found")
        self.assertEqual([e.get("unverified") for e in res["events"]], [None])

    def test_cursor_beyond_the_anchor_record_does_not_stop_early(self):
        prefix, rest, anchor = self.anchored_tail()
        size = os.path.getsize(self.jpath)
        with mock.patch.object(journal, "_READ_CHUNK", 512):
            res = journal.read_since(55, BIG, cursor_anchor=anchor)
        self.assertEqual(res["journal_prefix"], "read")
        self.assertEqual(res["bytes"], size)

    def test_anchor_with_oversized_line_in_window_is_not_complete(self):
        big = b'{"seq":99,"pad":"' + b"x" * (journal.JOURNAL_LINE_MAX + 10) + b'"}\n'
        prefix, rest, anchor = self.anchored_tail([big])
        res = journal.read_since(50, BIG, cursor_anchor=anchor)
        self.assertEqual(res["oversized"], 1)
        self.assertFalse(res["complete"])

    def test_anchor_with_a_missing_cursor_record_falls_back_to_reading_on(self):
        lines = [self.mrec(i, body="x" * 300) for i in range(1, 301)]
        self.put(*lines)
        size = os.path.getsize(self.jpath)
        res = journal.read_since(500, BIG, cursor_anchor="a" * 16)
        self.assertEqual(res["bytes"], size)
        self.assertEqual(res["gap"]["reason"], "regression")


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

    def test_oversized_first_line_is_counted_and_skipped(self):
        base = len(self.mrec(1, body="")) - 1
        over = self.mrec(1, body="x" * (journal.JOURNAL_LINE_MAX + 1 - base))
        self.assertEqual(len(over) - 1, journal.JOURNAL_LINE_MAX + 1)
        self.put(over, self.mrec(2))
        res = journal.read_since(None, BIG)
        self.assertEqual(res["oversized"], 1)
        self.assertEqual([m["seq"] for m in res["messages"]], [2])
        self.assertEqual(res["journal_prefix"], "read")


if __name__ == "__main__":
    unittest.main()
