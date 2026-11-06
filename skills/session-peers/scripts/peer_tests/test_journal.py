"""Session-peers journal: identity, handovers, ACKs, retention, crashes, integrity, reader."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import time  # noqa: E402
from session_peers import constants as sp_constants, codex as sp_codex, identity as sp_identity  # noqa: E402
from session_peers import journal, opencode as sp_opencode  # noqa: E402

CAP_ENV = "SESSION_PEERS_JOURNAL_MAX_BYTES"
OC_ID = "ses_aaaaaaaaaaaaAAAAAAAAAAAAAA"
IDENTITY_ENV = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CODEX_THREAD_ID",
    "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "OPENCODE", CAP_ENV,
)


def cc():
    return "cc:" + str(uuid.uuid4())


class JournalBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="spj")
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in IDENTITY_ENV:
            os.environ.pop(name, None)
        os.environ["HOME"] = self.root
        os.environ["CODEX_HOME"] = os.path.join(self.root, "codex-a")
        self.now = time.time()
        self.missing = set()
        for patch in (
            mock.patch.object(journal, "_now", lambda: self.now),
            mock.patch.object(sp_identity, "resolve_typed", self._resolve),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.A, self.B, self.C = cc(), cc(), cc()
        self.X = "codex:" + str(uuid.uuid4())
        self.dir = journal.journal_dir()
        self.jpath = os.path.join(self.dir, "journal.jsonl")
        self.mpath = os.path.join(self.dir, "journal.meta.json")

    def _resolve(self, target, exclude=None):
        if target in self.missing:
            raise sp_codex.ResolveNotFound("no such session %s" % target)
        kind, _sep, ident = target.partition(":")
        return {"kind": kind, "uuid": ident, "name": None}

    # -- helpers ------------------------------------------------------------

    def msg(self, to=None, sender=None, body="hello"):
        return journal.record_message(sender or cc(), to or [self.A], body)

    def req(self, to=None, sender=None, body="ask", mailbox=None, expires=None):
        rid = str(uuid.uuid4())
        mailbox = mailbox or "/nonexistent/spj/%s.json" % rid
        journal.record_request(rid, sender or cc(), to or [self.A], body,
                               self.now + 600 if expires is None else expires, mailbox)
        return rid

    def read(self, path=None):
        with open(path or self.jpath, "rb") as fh:
            return fh.read()

    def records(self):
        return [json.loads(line) for line in self.read().splitlines() if line.strip()]

    def size(self):
        return os.path.getsize(self.jpath)

    def entry(self, mid):
        for item in journal.read_only_view()["entries"]:
            if item["message_id"] == mid:
                return item
        return None

    def ids(self):
        return [item["message_id"] for item in journal.read_only_view()["entries"]]

    def set_cap(self, value):
        os.environ[CAP_ENV] = str(value)

    def snapshot(self):
        out = {}
        for name in sorted(os.listdir(self.dir)):
            path = os.path.join(self.dir, name)
            st = os.stat(path)
            out[name] = (st.st_ino, st.st_size, self.read(path) if name != "journal.lock" else b"")
        return out

    def fill(self, ack_first):
        """A journal whose size equals its cap, holding one entry that a write can prune."""
        self.set_cap(60000)
        body = "b" * 2000
        first = journal.record_message(cc(), [self.A], body)
        if ack_first:
            journal.ack(first, self.A)
        while self.size() < 20000:
            self.req(body=body)
        self.set_cap(self.size())
        return first, body

    def child(self, failpoint, code):
        script = (
            "import os, sys\nsys.path.insert(0, %r)\n"
            "from session_peers import journal as j, identity as i\n"
            "i.resolve_typed = lambda t, *a, **k: {\"kind\": t.partition(\":\")[0]}\n"
            "def fp(name):\n    if name == %r:\n        os._exit(9)\n"
            "j._failpoint = fp\n%s\n" % (HERE, failpoint, code)
        )
        proc = subprocess.run([sys.executable, "-c", script], env=dict(os.environ),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 9, proc.stderr)

    def meta(self):
        with open(self.mpath) as fh:
            return json.load(fh)

    def leftovers(self):
        return [n for n in os.listdir(self.dir) if sp_constants.JOURNAL_TMP_RE.match(n)]


class JournalRetrievalTests(JournalBase):
    def test_each_runtime_reads_its_slot(self):
        mid = self.msg(to=[self.A, self.X, "oc:" + OC_ID], body="to all")
        cases = [
            ({"CLAUDE_CODE_SESSION_ID": self.A[3:]}, self.A),
            ({"CODEX_THREAD_ID": self.X[6:]}, self.X),
            ({"CODEX_SESSION_ID": self.X[6:]}, self.X),
            ({"OPENCODE_SESSION_ID": OC_ID}, "oc:" + OC_ID),
        ]
        for env, typed in cases:
            with self.subTest(typed=typed), mock.patch.dict(os.environ, env), \
                    mock.patch.object(sp_opencode, "get_session", return_value={}):
                who = journal.caller()
                self.assertEqual(who, typed)
                self.assertEqual(journal.get(mid, who)["body"], "to all")

    def test_sender_and_other_sessions_are_refused(self):
        sender = cc()
        mid = self.msg(sender=sender)
        for who in (sender, self.B):
            with self.subTest(who=who):
                with self.assertRaises(journal.JournalRefused):
                    journal.get(mid, who)

    def test_mixed_and_missing_runtime_env_refused(self):
        with self.assertRaises(journal.JournalRefused) as ctx:
            journal.caller()
        self.assertIn("OPENCODE_SESSION_ID", str(ctx.exception))
        env = {"CLAUDE_CODE_SESSION_ID": self.A[3:], "CODEX_THREAD_ID": self.X[6:]}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(journal.JournalRefused) as ctx:
                journal.caller()
        self.assertIn("CODEX_THREAD_ID", str(ctx.exception))
        self.assertIn("env -u", str(ctx.exception))

    def test_unverifiable_opencode_session_refused_and_nothing_written(self):
        err = sp_opencode.OpenCodeError("service down", unavailable=True)
        with mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": OC_ID}), \
                mock.patch.object(sp_opencode, "get_session", side_effect=err):
            with self.assertRaises(journal.JournalRefused) as ctx:
                journal.caller()
        self.assertIn("cannot verify OpenCode session " + OC_ID, str(ctx.exception))
        self.assertFalse(os.path.exists(self.dir))

    def test_non_uuid_claude_id_refused(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "not-a-uuid"}):
            with self.assertRaises(journal.JournalRefused):
                journal.caller()

    def test_two_codex_homes_share_one_journal(self):
        mid = self.msg()
        os.environ["CODEX_HOME"] = os.path.join(self.root, "codex-b")
        self.assertEqual(journal.get(mid, self.A)["body"], "hello")
        self.assertEqual(journal.journal_dir(), self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.root, "codex-b", "session-peers")))

    def test_recipient_must_exist_and_be_typed(self):
        self.missing.add(self.B)
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.B], "x")
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), ["buddy"], "x")
        self.assertFalse(os.path.exists(self.jpath))

    def test_list_for_hides_acked_and_carries_no_body(self):
        first, second = self.msg(body="one"), self.msg(body="two")
        self.msg(to=[self.B])
        journal.ack(first, self.A)
        shown = journal.list_for(self.A)
        self.assertEqual([e["message_id"] for e in shown], [second])
        self.assertNotIn("body", shown[0])
        every = journal.list_for(self.A, include_acked=True)
        self.assertEqual([e["message_id"] for e in every], [first, second])

    def test_reads_never_prune_and_flag_expired(self):
        mid = self.msg()
        self.now += 15 * 86400
        self.assertTrue(journal.get(mid, self.A)["expired"])
        self.assertEqual(len(self.records()), 1)

    def test_reply_is_idempotent_and_single(self):
        rid = self.req(to=[self.B], sender=self.A)
        reply_id = journal.reply_message_id(rid)
        self.assertEqual(reply_id, journal.reply_message_id(rid))
        args = (rid, self.B, [self.A], "answer", self.now + 600, "/nonexistent/spj/%s.json" % rid)
        self.assertEqual(journal.record_reply(*args), "recorded")
        self.assertEqual(journal.record_reply(*args), "same")
        with self.assertRaises(journal.JournalRefused) as ctx:
            journal.record_reply(rid, self.B, [self.A], "other", self.now + 600, args[5])
        self.assertIn("already has a recorded reply", str(ctx.exception))
        self.assertEqual(sum(1 for r in self.records() if r.get("kind") == "reply"), 1)
        self.assertEqual(journal.get(reply_id, self.A)["body"], "answer")

    def test_reply_revalidate_failure_writes_nothing(self):
        rid = self.req(to=[self.B], sender=self.A)
        before = self.read()

        def refuse():
            raise journal.JournalRefused("request meta changed")

        with self.assertRaises(journal.JournalRefused):
            journal.record_reply(rid, self.B, [self.A], "a", self.now + 600, "/nonexistent/x", refuse)
        self.assertEqual(self.read(), before)


class JournalSuccessorTests(JournalBase):
    def test_name_handover_refused(self):
        for target in ("buddy", "@buddy", "namesake"):
            with self.subTest(target=target):
                with self.assertRaises(journal.JournalRefused) as ctx:
                    journal.handover(self.A, target)
                self.assertIn("a handover names a full typed identity, not a name", str(ctx.exception))
        with self.assertRaises(journal.JournalRefused):
            journal.handover(self.A, self.A)

    def test_missing_successor_refused(self):
        self.missing.add(self.B)
        with self.assertRaises(journal.JournalRefused):
            journal.handover(self.A, self.B)

    def test_handover_allows_read_and_ack_with_for_and_by(self):
        mid = self.msg(body="inherited")
        with self.assertRaises(journal.JournalRefused):
            journal.get(mid, self.B)
        journal.handover(self.A, self.B)
        self.assertEqual(journal.get(mid, self.B)["body"], "inherited")
        record = journal.ack(mid, self.B)
        self.assertEqual((record["for"], record["by"]), (self.A, self.B))
        view = journal.acks_view(mid, self.B)
        self.assertEqual(view["recipients"][0]["by"], self.B)

    def test_non_recipient_handover_grants_nothing(self):
        mid = self.msg()
        journal.handover(self.X, self.B)
        with self.assertRaises(journal.JournalRefused):
            journal.get(mid, self.B)
        with self.assertRaises(journal.JournalRefused):
            journal.ack(mid, self.B)

    def test_message_after_grant_is_not_covered(self):
        old = self.msg(body="old")
        journal.handover(self.A, self.B)
        new = self.msg(body="new")
        self.assertEqual(journal.get(old, self.B)["body"], "old")
        with self.assertRaises(journal.JournalRefused):
            journal.get(new, self.B)

    def test_chain_works_and_respects_the_minimum_coverage(self):
        first = self.msg(body="first")
        journal.handover(self.A, self.B)
        second = self.msg(body="second")
        journal.handover(self.B, self.C)
        self.assertEqual(journal.get(first, self.C)["body"], "first")
        with self.assertRaises(journal.JournalRefused):
            journal.get(second, self.C)

    def test_cycle_terminates(self):
        mid = self.msg()
        journal.handover(self.A, self.B)
        journal.handover(self.B, self.A)
        self.assertEqual(journal.get(mid, self.B)["body"], "hello")
        with self.assertRaises(journal.JournalRefused):
            journal.get(mid, self.C)

    def test_chain_longer_than_eight_links_is_cut(self):
        mid = self.msg()
        chain = [self.A] + [cc() for _ in range(9)]
        for left, right in zip(chain, chain[1:]):
            journal.handover(left, right)
        self.assertEqual(journal.get(mid, chain[8])["body"], "hello")
        with self.assertRaises(journal.JournalRefused):
            journal.get(mid, chain[9])

    def test_ninth_grant_refused_and_regrant_replaces(self):
        self.msg()
        targets = [cc() for _ in range(8)]
        for target in targets:
            journal.handover(self.A, target)
        with self.assertRaises(journal.JournalRefused):
            journal.handover(self.A, cc())
        journal.handover(self.A, targets[0])
        grants = [g for g in journal.read_only_view()["handovers"] if g["from"] == self.A]
        self.assertEqual(len(grants), 8)

    def test_grant_is_retained_while_covering_then_pruned(self):
        self.set_cap(6000)
        first = self.msg(body="x" * 500)
        journal.ack(first, self.A)
        journal.handover(self.A, self.B)
        self.assertEqual(len(journal.read_only_view()["handovers"]), 1)
        self.msg(body="y" * 500, to=[self.C])
        self.assertEqual(len(journal.read_only_view()["handovers"]), 1)
        for _ in range(3):
            self.msg(body="z" * 500, to=[self.C])
        view = journal.read_only_view()
        self.assertNotIn(first, [e["message_id"] for e in view["entries"]])
        self.assertEqual(view["handovers"], [])


class JournalAckTests(JournalBase):
    def test_ack_records_for_and_by_and_is_idempotent(self):
        mid = self.msg()
        first = journal.ack(mid, self.A)
        self.assertEqual((first["for"], first["by"], first["message_id"]), (self.A, self.A, mid))
        before = self.read()
        again = journal.ack(mid, self.A)
        self.assertEqual(again["seq"], first["seq"])
        self.assertEqual(self.read(), before)

    def test_non_recipient_cannot_ack(self):
        mid = self.msg()
        with self.assertRaises(journal.JournalRefused):
            journal.ack(mid, self.B)
        with self.assertRaises(journal.JournalRefused):
            journal.ack(mid, self.A, for_slot=self.B)
        self.assertEqual(len(self.records()), 1)

    def test_two_recipients_one_acked_one_pending(self):
        mid = self.msg(to=[self.A, self.B])
        journal.ack(mid, self.B)
        view = journal.acks_view(mid, self.A)
        states = {r["slot"]: r["state"] for r in view["recipients"]}
        self.assertEqual(states, {self.A: "pending", self.B: "acked"})
        self.assertNotIn("body", view)

    def test_sender_sees_acks_but_strangers_do_not(self):
        sender = cc()
        mid = self.msg(sender=sender)
        self.assertEqual(journal.acks_view(mid, sender)["recipients"][0]["state"], "pending")
        with self.assertRaises(journal.JournalRefused):
            journal.acks_view(mid, self.B)

    def test_covering_several_slots_needs_for(self):
        mid = self.msg(to=[self.A, self.B])
        journal.handover(self.A, self.C)
        journal.handover(self.B, self.C)
        with self.assertRaises(journal.JournalRefused):
            journal.ack(mid, self.C)
        self.assertEqual(journal.ack(mid, self.C, for_slot=self.B)["for"], self.B)

    def test_settlement_is_not_an_ack(self):
        rid = self.req(to=[self.A])
        journal.settle(rid, "replied", self.A)
        view = journal.acks_view(rid, self.A)
        self.assertEqual(view["recipients"][0]["state"], "pending")
        self.assertEqual(view["settlement"]["state"], "replied")
        journal.settle(rid, "replied", self.A)
        self.assertEqual(sum(1 for r in self.records() if r["type"] == "settled"), 1)

    def test_transport_error_is_separate_and_truncated(self):
        mid = self.msg()
        journal.transport_error(mid, self.A, "e" * 2000)
        entry = journal.get(mid, self.A)
        self.assertEqual(len(entry["transport_errors"][0]["detail"]), 512)
        self.assertEqual(entry["transport_errors"][0]["for"], self.A)
        self.assertEqual(entry["acks"], [])
        self.assertIsNone(entry["settlement"])

    def test_post_send_updates_raise_update_failed(self):
        rid = self.req()
        mid = self.msg()
        with mock.patch.object(os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(journal.JournalUpdateFailed):
                journal.settle(rid, "replied", self.A)
            with self.assertRaises(journal.JournalUpdateFailed):
                journal.transport_error(mid, self.A, "boom")
        with self.assertRaises(journal.JournalUpdateFailed):
            journal.settle(mid, "consumed", self.A)
        self.assertFalse(any(r["type"] in ("settled", "transport_error") for r in self.records()))

    def test_malformed_bytes_cannot_escape_the_cap_through_the_append_path(self):
        self.set_cap(5000)
        keep = self.msg(body="x" * 300)
        with open(self.jpath, "ab") as fh:
            fh.write((b"garbage-" + b"g" * 100 + b"\n") * 45)
        self.assertGreater(self.size(), 5000)
        for _ in range(3):
            self.msg(body="y" * 100)
            self.assertNotIn(b"garbage-", self.read())
            self.assertLessEqual(self.size(), 5000)
        self.assertIn(keep, self.ids())
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)


class JournalRetentionTests(JournalBase):
    def test_age_cap_prunes_unacked_and_entries_with_a_live_mailbox(self):
        mailbox = os.path.join(self.root, "request.json")
        open(mailbox, "w").close()
        old_plain = self.msg()
        old_req = self.req(mailbox=mailbox)
        self.now += 14 * 86400 + 60
        fresh = self.msg(body="fresh")
        self.assertEqual(self.ids(), [fresh])
        self.assertNotIn(old_plain, self.ids())
        self.assertNotIn(old_req, self.ids())

    def test_oldest_eligible_entry_goes_first(self):
        ids = []
        for _ in range(6):
            rid = self.req(body="r" * 400)
            journal.ack(rid, self.A)
            ids.append(rid)
        self.set_cap(self.size())
        newest = self.req(body="r" * 400)
        self.assertEqual(self.ids(), ids[1:] + [newest])

    def test_young_unacked_entries_survive_a_full_journal(self):
        expired = self.req(body="q" * 400, expires=self.now - 5)
        failed = self.msg(body="m" * 400)
        journal.transport_error(failed, self.A, "no route")
        plain = self.msg(body="p" * 400)
        self.set_cap(self.size())
        before = self.snapshot()
        with self.assertRaises(journal.JournalFull):
            self.msg(body="n" * 400)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ids(), [expired, failed, plain])

    def test_live_mailbox_protects_acked_and_settled_entries(self):
        mailbox = os.path.join(self.root, "mailbox.json")
        open(mailbox, "w").close()
        for _ in range(3):
            self.req(body="f" * 400)
        acked = self.req(body="a" * 400, mailbox=mailbox)
        journal.ack(acked, self.A)
        settled = self.req(body="s" * 400, mailbox=mailbox)
        journal.settle(settled, "replied", self.A)
        self.set_cap(self.size())
        before = self.snapshot()
        with self.assertRaises(journal.JournalFull):
            self.req(body="n" * 400)
        self.assertEqual(self.snapshot(), before)
        os.unlink(mailbox)
        newest = self.req(body="n" * 400)
        self.assertNotIn(acked, self.ids())
        self.assertIn(newest, self.ids())

    def test_settled_entry_is_eligible_once_mailbox_is_gone(self):
        for _ in range(4):
            self.req(body="f" * 400)
        settled = self.req(body="s" * 400)
        journal.settle(settled, "replied", self.A)
        self.set_cap(self.size())
        newest = self.req(body="n" * 400)
        self.assertNotIn(settled, self.ids())
        self.assertIn(newest, self.ids())

    def test_message_quota_is_half_the_cap_for_plain_messages(self):
        self.set_cap(10000)
        body = "q" * 1100
        for _ in range(3):
            self.msg(body=body)
        calls = []
        real = journal._Writer.load

        def spy(writer, now):
            calls.append(1)
            return real(writer, now)

        with mock.patch.object(journal._Writer, "load", spy):
            with self.assertRaises(journal.JournalFull):
                self.msg(body=body)
        self.assertTrue(calls)
        self.assertLess(self.size() + 1400, 10000)
        self.req(body=body)

    def test_sender_quota_is_a_quarter_of_the_cap_on_fast_and_stale_meta(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.set_cap(10000)
                sender = cc()
                journal.record_message(sender, [self.A], "q" * 1100)
                if stale:
                    os.unlink(self.mpath)
                before = self.snapshot()
                with self.assertRaises(journal.JournalFull):
                    journal.record_message(sender, [self.A], "q" * 1100)
                self.assertEqual(self.snapshot(), before)
                with self.assertRaises(journal.JournalFull):
                    journal.record_request(str(uuid.uuid4()), sender, [self.A], "q" * 1100,
                                           self.now + 60, "/nonexistent/x")
                self.msg(body="q" * 1100)

    def test_fast_path_appends_without_parsing(self):
        self.msg()
        calls = []
        real = journal._Writer.load
        with mock.patch.object(journal._Writer, "load", lambda w, n: calls.append(1) or real(w, n)):
            self.msg(body="again")
        self.assertEqual(calls, [])
        self.assertEqual(len(self.records()), 2)

    def test_null_sender_is_one_bucket(self):
        self.set_cap(10000)
        journal.record_message(None, [self.A], "q" * 1100)
        with self.assertRaises(journal.JournalFull):
            journal.record_message(None, [self.A], "q" * 1100)

    def test_lowered_cap_with_only_protected_entries_refuses_new_messages(self):
        for _ in range(3):
            self.msg(body="p" * 500)
        self.set_cap(1000)
        before = self.snapshot()
        with self.assertRaises(journal.JournalFull):
            self.msg()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.ids()), 3)

    def test_higher_or_invalid_cap_env_is_ignored(self):
        big = "x" * (9 * 1024 * 1024)
        for value in (str(10 ** 12), "garbage", "-5", "0"):
            with self.subTest(value=value):
                shutil.rmtree(self.dir, ignore_errors=True)
                self.set_cap(value)
                with self.assertRaises(journal.JournalFull):
                    self.msg(body=big)

    def test_event_reserve_admits_an_ack_above_the_cap(self):
        mids = [self.msg(body="p" * 500) for _ in range(3)]
        self.set_cap(1000)
        record = journal.ack(mids[0], self.A)
        self.assertEqual(record["type"], "ack")
        self.assertNotIn(mids[0], self.ids())
        self.assertEqual(self.ids(), mids[1:])

    def test_event_beyond_the_reserve_is_refused_untouched(self):
        mids = [self.msg(body="p" * 500) for _ in range(3)]
        self.set_cap(1000)
        before = self.snapshot()
        with mock.patch.object(sp_constants, "JOURNAL_EVENT_RESERVE", 1):
            with self.assertRaises(journal.JournalFull):
                journal.ack(mids[0], self.A)
        self.assertEqual(self.snapshot(), before)

    def test_meta_matches_the_file(self):
        self.msg()
        self.req()
        meta = self.meta()
        st = os.stat(self.jpath)
        self.assertEqual((meta["file_ino"], meta["file_size"]), (st.st_ino, st.st_size))
        self.assertEqual(meta["total_bytes"], st.st_size)
        self.assertEqual(meta["next_seq"], 3)
        self.assertEqual(sum(meta["bytes_by_sender"].values()), st.st_size)
        self.assertLess(meta["message_bytes"], st.st_size)


class JournalCrashTests(JournalBase):
    def test_crash_after_append_before_meta(self):
        self.msg()
        self.child("after_append_before_meta",
                   "j.record_request(%r, %r, [%r], 'child', 1e12, '/nonexistent/c')" % (
                       str(uuid.uuid4()), self.B, self.A))
        mid = self.msg(body="after")
        records = [r for r in self.records() if r["type"] == "message"]
        self.assertEqual(len(records), 3)
        self.assertEqual([r["seq"] for r in records], [1, 2, 3])
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)
        self.assertIn(mid, self.ids())

    def test_crash_mid_line_leaves_a_torn_tail_that_is_removed(self):
        self.msg()
        self.child("mid_line", "j.record_message(%r, [%r], %r)" % (self.B, self.A, "t" * 200))
        self.assertFalse(self.read().endswith(b"\n"))
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)
        mid = self.msg(body="next")
        self.assertTrue(self.read().endswith(b"\n"))
        view = journal.read_only_view()
        self.assertEqual(view["malformed_lines"], 0)
        self.assertEqual([e["body"] for e in view["entries"]], ["hello", "next"])
        self.assertEqual(self.entry(mid)["seq"], 2)

    def test_crash_before_prune_rename_changes_nothing(self):
        self.fill(ack_first=True)
        before = self.read()
        self.child("before_prune_rename", "j.record_message(%r, [%r], %r)" % (cc(), self.A, "b" * 2000))
        self.assertEqual(self.read(), before)
        self.assertEqual(len(self.leftovers()), 1)
        mid = self.msg(body="b" * 2000)
        self.assertEqual(self.leftovers(), [])
        self.assertIn(mid, self.ids())

    def test_crash_after_prune_rename_keeps_the_prune_and_seq(self):
        first, _body = self.fill(ack_first=True)
        last_seq = journal.read_only_view()["last_seq"]
        self.child("after_prune_rename", "j.record_message(%r, [%r], %r)" % (cc(), self.A, "b" * 2000))
        self.assertNotIn(first, self.ids())
        view = journal.read_only_view()
        self.assertGreater(view["last_seq"], last_seq)
        record = journal.ack(view["entries"][-1]["message_id"], self.A)
        self.assertEqual(record["seq"], view["last_seq"] + 1)
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)

    def test_crash_with_meta_tmp_written(self):
        self.msg()
        self.child("meta_tmp_written", "j.record_message(%r, [%r], 'child')" % (self.B, self.A))
        self.assertEqual(len(self.leftovers()), 1)
        mid = self.msg(body="after")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.entry(mid)["seq"], 3)

    def test_deleted_meta_recovers_the_sequence(self):
        self.msg()
        self.msg()
        os.unlink(self.mpath)
        mid = self.msg()
        self.assertEqual(self.entry(mid)["seq"], 3)
        self.assertEqual(self.meta()["next_seq"], 4)

    def test_meta_never_lowers_the_sequence_after_everything_is_pruned(self):
        self.set_cap(5000)
        first = self.msg(body="x" * 500)
        journal.ack(first, self.A)
        self.now += 15 * 86400
        mid = self.msg()
        self.assertGreater(self.entry(mid)["seq"], 2)

    def test_interrupted_rewrites_leave_leftovers_a_fresh_writer_removes(self):
        self.fill(ack_first=True)
        before = self.read()
        stash = []
        for point in ("before_prune_rename", "before_prune_rename", "before_prune_rename"):
            self.child(point, "j.record_message(%r, [%r], %r)" % (cc(), self.A, "b" * 2000))
            # Each later writer would clean the previous leftover, so park it.
            (name,) = self.leftovers()
            parked = os.path.join(self.root, "parked-%d" % len(stash))
            os.rename(os.path.join(self.dir, name), parked)
            stash.append(parked)
        meta_tmp = os.path.join(self.dir, "journal.meta.json.tmp.%d.%s" % (os.getpid(), "c" * 32))
        stash.append(meta_tmp)
        open(meta_tmp, "w").close()
        for i, parked in enumerate(stash[:3]):
            os.rename(parked, os.path.join(self.dir, "journal.jsonl.tmp.%d.%s" % (i + 1, str(i) * 32)))
        self.assertEqual(len(self.leftovers()), 4)
        keep = os.path.join(self.dir, "journal.jsonl.tmp.notes")
        open(keep, "w").close()
        self.assertEqual(self.read(), before)
        self.msg(body="b" * 2000)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(os.path.exists(keep))

    def test_leftover_symlink_is_not_followed_or_deleted(self):
        self.msg()
        victim = os.path.join(self.root, "victim")
        open(victim, "w").close()
        link = os.path.join(self.dir, "journal.jsonl.tmp.1." + "a" * 32)
        os.symlink(victim, link)
        self.msg()
        self.assertTrue(os.path.exists(victim))

    def test_event_is_atomic_with_the_prune_it_enables(self):
        for point in ("before_prune_rename", "after_prune_rename"):
            with self.subTest(point=point):
                shutil.rmtree(self.dir, ignore_errors=True)
                first, _body = self.fill(ack_first=False)
                before = self.read()
                self.child(point, "j.ack(%r, %r)" % (first, self.A))
                after = self.read()
                if point == "before_prune_rename":
                    self.assertEqual(after, before)
                    self.assertIn(first, self.ids())
                else:
                    self.assertNotIn(first, self.ids())
                    self.assertFalse(any(r["type"] == "ack" and r["message_id"] == first
                                         for r in self.records()))
                self.assertEqual(journal.read_only_view()["malformed_lines"], 0)


class JournalIntegrityTests(JournalBase):
    def test_modes_are_private_and_repaired(self):
        self.msg()
        for path, mode in ((self.dir, 0o700), (self.jpath, 0o600), (self.mpath, 0o600),
                           (os.path.join(self.dir, "journal.lock"), 0o600)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode, path)
        os.chmod(self.dir, 0o755)
        os.chmod(self.jpath, 0o666)
        os.chmod(self.mpath, 0o644)
        self.msg()
        for path, mode in ((self.dir, 0o700), (self.jpath, 0o600), (self.mpath, 0o600)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode, path)

    def test_symlinked_journal_refused(self):
        os.makedirs(self.dir, mode=0o700)
        target = os.path.join(self.root, "elsewhere")
        open(target, "w").close()
        os.symlink(target, self.jpath)
        with self.assertRaises(journal.JournalUnavailable):
            self.msg()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        self.assertEqual(os.path.getsize(target), 0)

    def test_symlinked_directory_refused(self):
        real = os.path.join(self.root, "real")
        os.makedirs(real, mode=0o700)
        os.makedirs(os.path.dirname(self.dir))
        os.symlink(real, self.dir)
        with self.assertRaises(journal.JournalUnavailable):
            self.msg()
        self.assertEqual(os.listdir(real), [])

    def test_fifo_journal_refused(self):
        os.makedirs(self.dir, mode=0o700)
        os.mkfifo(self.jpath, 0o600)
        with self.assertRaises(journal.JournalUnavailable):
            self.msg()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()

    def test_foreign_owned_journal_refused(self):
        self.msg()
        with mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(journal.JournalUnavailable):
                self.msg()
            with self.assertRaises(journal.JournalUnavailable):
                journal.read_only_view()

    def test_file_above_the_read_ceiling_refused(self):
        self.msg()
        with mock.patch.object(sp_constants, "JOURNAL_READ_CEILING", 10):
            with self.assertRaises(journal.JournalUnavailable):
                journal.read_only_view()
            with self.assertRaises(journal.JournalUnavailable):
                self.msg()

    def test_unusable_location_is_unavailable(self):
        blocker = os.path.join(self.root, "afile")
        open(blocker, "w").close()
        os.environ["HOME"] = blocker
        with self.assertRaises(journal.JournalUnavailable) as ctx:
            self.msg()
        self.assertTrue(str(ctx.exception).startswith("session-peers journal unavailable: "))

    def test_held_lock_times_out_with_a_clear_message(self):
        self.msg()
        import fcntl
        fd = os.open(os.path.join(self.dir, "journal.lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(sp_constants, "JOURNAL_LOCK_TIMEOUT", 0.2):
            with self.assertRaises(journal.JournalUnavailable) as ctx:
                self.msg()
        self.assertIn("another peers.py command holds the session-peers journal lock", str(ctx.exception))

    def test_hostile_lines_are_counted_and_never_trusted(self):
        mid = self.msg()
        good = json.dumps({"schema": 1, "seq": 2, "type": "ack", "at": self.now, "message_id": mid,
                           "for": self.A, "by": self.A}, sort_keys=True)
        nan = good.replace('"seq": 2', '"seq": 3').replace('"at": %r' % self.now, '"at": NaN')
        future = good.replace('"seq": 2', '"seq": 4').replace('"at": %r' % self.now, '"at": %r' % (self.now + 20 * 86400))
        deep = "[" * 200000 + "]" * 200000
        huge = '{"schema":1,"seq":5,"type":"ack","at":1e999,"message_id":"%s","for":"%s","by":"%s"}' % (
            mid, self.A, self.A)
        self.assertIn("NaN", nan)
        with open(self.jpath, "ab") as fh:
            fh.write(good.encode() + b"\n")
            for line in (nan, future, deep, huge, "not json", "\xff"):
                fh.write(line.encode("latin-1") + b"\n")
        view = journal.read_only_view()
        self.assertEqual(view["malformed_lines"], 6)
        self.assertEqual(view["last_seq"], 5)
        self.assertEqual(view["entries"][0]["recipients"][0]["state"], "acked")

    def test_forged_ack_for_a_non_recipient_is_ignored(self):
        mid = self.msg()
        forged = {"schema": 1, "seq": 2, "type": "ack", "at": self.now, "message_id": mid,
                  "for": self.B, "by": self.B}
        with open(self.jpath, "ab") as fh:
            fh.write(json.dumps(forged).encode() + b"\n")
        self.assertEqual(journal.read_only_view()["entries"][0]["recipients"][0]["state"], "pending")

    def test_malformed_lines_are_dropped_by_the_next_rewrite(self):
        self.set_cap(5000)
        mid = self.msg(body="x" * 400)
        journal.ack(mid, self.A)
        with open(self.jpath, "ab") as fh:
            fh.write(b"garbage\n")
        self.assertEqual(journal.read_only_view()["malformed_lines"], 1)
        self.now += 15 * 86400
        self.msg()
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)


class JournalReaderTests(JournalBase):
    def test_missing_journal_is_an_empty_view_and_creates_nothing(self):
        view = journal.read_only_view()
        self.assertEqual(view, {"entries": [], "handovers": [], "malformed_lines": 0, "last_seq": 0})
        self.assertFalse(os.path.exists(os.path.join(self.root, ".codex")))
        self.assertEqual(journal.list_for(self.A), [])

    def test_view_needs_no_write_capability_and_changes_no_times(self):
        mid = self.msg(to=[self.A, self.B])
        journal.ack(mid, self.A)
        journal.settle(self.req(), "replied", self.A)
        paths = [self.dir] + [os.path.join(self.dir, n) for n in os.listdir(self.dir)]
        stamp = lambda: [(os.stat(p).st_ctime_ns, os.stat(p).st_mtime_ns) for p in paths]  # noqa: E731
        before = stamp()
        raw = self.snapshot()
        real_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                raise AssertionError("reader opened for writing: %r" % (path,))
            return real_open(path, flags, *args, **kwargs)

        def boom(*_a, **_k):
            raise AssertionError("reader mutated the filesystem")

        with mock.patch.object(os, "chmod", boom), mock.patch.object(os, "fchmod", boom), \
                mock.patch.object(os, "replace", boom), mock.patch.object(os, "rename", boom), \
                mock.patch.object(os, "truncate", boom), mock.patch.object(os, "ftruncate", boom), \
                mock.patch.object(os, "unlink", boom), mock.patch.object(os, "open", guarded_open):
            view = journal.read_only_view()
            journal.get(mid, self.A)
            journal.list_for(self.A)
            journal.acks_view(mid, self.B)
        self.assertEqual(len(view["entries"]), 2)
        self.assertEqual(stamp(), before)
        self.assertEqual(self.snapshot(), raw)

    def test_wider_mode_is_refused_not_repaired(self):
        self.msg()
        os.chmod(self.jpath, 0o644)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        with self.assertRaises(journal.JournalUnavailable):
            journal.get(str(uuid.uuid4()), self.A)
        self.assertEqual(stat.S_IMODE(os.stat(self.jpath).st_mode), 0o644)

    def test_wider_directory_mode_is_refused_not_repaired(self):
        self.msg()
        os.chmod(self.dir, 0o755)
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o755)

    def test_reader_does_not_prune_or_repair_a_torn_tail(self):
        self.msg()
        self.now += 15 * 86400
        with open(self.jpath, "ab") as fh:
            fh.write(b'{"torn":')
        before = self.read()
        view = journal.read_only_view()
        self.assertTrue(view["entries"][0]["expired"])
        self.assertEqual(view["malformed_lines"], 0)
        self.assertEqual(self.read(), before)

    def test_view_carries_body_acks_settlement_transport_and_grants(self):
        rid = self.req(to=[self.A, self.B], body="full body")
        journal.ack(rid, self.A)
        journal.settle(rid, "replied", self.A)
        journal.transport_error(rid, self.B, "refused")
        journal.handover(self.A, self.C)
        view = journal.read_only_view()
        item = view["entries"][0]
        self.assertEqual(item["body"], "full body")
        self.assertEqual({r["slot"]: r["state"] for r in item["recipients"]},
                         {self.A: "acked", self.B: "pending"})
        self.assertEqual(item["settlement"]["state"], "replied")
        by_slot = {r["slot"]: r for r in item["recipients"]}
        self.assertEqual(by_slot[self.B]["transport_error"]["detail"], "refused")
        self.assertNotIn("transport_error", by_slot[self.A])
        self.assertNotIn("transport_error", item)
        self.assertFalse(item["expired"])
        self.assertEqual([(g["from"], g["to"]) for g in view["handovers"]], [(self.A, self.C)])
        self.assertEqual(view["last_seq"], max(r["seq"] for r in self.records()))


class JournalMetaFailureTests(JournalBase):
    """A committed record or event survives a failing meta write."""

    def fail_meta(self):
        return mock.patch.object(journal._Writer, "write_meta",
                                 side_effect=OSError(errno.ENOSPC, "No space left on device"))

    def meta_matches_file(self):
        try:
            meta = self.meta()
        except (OSError, ValueError):
            return False
        st = os.stat(self.jpath)
        return meta.get("file_ino") == st.st_ino and meta.get("file_size") == st.st_size

    def last_seq(self):
        return journal.read_only_view()["last_seq"]

    def assert_committed_with_stale_meta(self, mid, last_before):
        self.assertIsNotNone(self.entry(mid))
        self.assertEqual(self.last_seq(), last_before + 1)
        self.assertFalse(self.meta_matches_file())
        self.assertEqual(self.leftovers(), [])

    def assert_next_write_recovers(self, last_before):
        self.set_cap(60000)
        journal.record_message(cc(), [self.A], "next")
        self.assertEqual(self.records()[-1]["seq"], last_before + 2)
        self.assertTrue(self.meta_matches_file())
        self.assertEqual(self.meta()["next_seq"], last_before + 3)
        self.assertEqual(self.leftovers(), [])

    def test_a_stale_meta_matching_inode_and_size_is_not_trusted(self):
        # Inode reuse after failed meta writes: inode and size match, next_seq is old.
        self.msg()
        self.msg(body="second")
        self.msg(body="third")
        meta = self.meta()
        meta["next_seq"] = 2
        with open(self.mpath, "w") as fh:
            json.dump(meta, fh)
        os.chmod(self.mpath, 0o600)
        rid = str(uuid.uuid4())
        journal.record_request(rid, cc(), [self.A], "ask", self.now + 600, "/nonexistent/x", fresh_id=True)
        self.assertIsNotNone(self.entry(rid))
        self.assertEqual(self.entry(rid)["seq"], 4)
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)

    def test_fast_path_append_succeeds_when_the_meta_write_fails(self):
        self.msg()
        last = self.last_seq()
        rid = str(uuid.uuid4())
        calls = []
        real = journal._Writer.load
        with mock.patch.object(journal._Writer, "load", lambda w, n: calls.append(1) or real(w, n)):
            with self.fail_meta():
                journal.record_request(rid, cc(), [self.A], "ask", self.now + 600, "/nonexistent/x",
                                       fresh_id=True)
        self.assertEqual(calls, [])
        self.assert_committed_with_stale_meta(rid, last)
        self.assert_next_write_recovers(last)

    def test_parse_path_append_succeeds_when_the_meta_write_fails(self):
        self.msg()
        last = self.last_seq()
        mid = str(uuid.uuid4())
        with self.fail_meta():
            self.assertEqual(journal.record_message(cc(), [self.A], "hi", message_id=mid), mid)
        self.assert_committed_with_stale_meta(mid, last)
        self.assert_next_write_recovers(last)

    def test_rewrite_succeeds_when_the_meta_write_fails(self):
        first, body = self.fill(ack_first=True)
        last = self.last_seq()
        ino = os.stat(self.jpath).st_ino
        with self.fail_meta():
            mid = journal.record_message(cc(), [self.A], "b" * 2000)
        self.assertNotEqual(os.stat(self.jpath).st_ino, ino)
        self.assertIsNone(self.entry(first))
        self.assert_committed_with_stale_meta(mid, last)
        self.assert_next_write_recovers(last)

    def test_an_ack_event_succeeds_when_the_meta_write_fails(self):
        mid = self.msg()
        last = self.last_seq()
        with self.fail_meta():
            journal.ack(mid, self.A)
        self.assertEqual(self.entry(mid)["recipients"][0]["state"], "acked")
        self.assertEqual(self.last_seq(), last + 1)
        self.assertFalse(self.meta_matches_file())
        self.assertEqual(self.leftovers(), [])
        self.assert_next_write_recovers(last)


class JournalTransportPerSlotTests(JournalBase):
    def two(self):
        mid = self.req(to=[self.A, self.B])
        journal.transport_error(mid, self.A, "err-a")
        journal.transport_error(mid, self.B, "err-b")
        return mid

    def test_each_view_shows_the_transport_error_of_each_recipient(self):
        mid = self.two()
        details = lambda rs: {r["slot"]: r["transport_error"]["detail"] for r in rs}
        want = {self.A: "err-a", self.B: "err-b"}
        item = self.entry(mid)
        self.assertEqual(details(item["recipients"]), want)
        self.assertNotIn("transport_error", item)
        acks = journal.acks_view(mid, self.A)
        self.assertEqual(details(acks["recipients"]), want)
        self.assertNotIn("transport_error", acks)
        got = journal.get(mid, self.A)
        self.assertEqual([(e["for"], e["detail"]) for e in got["transport_errors"]],
                         [(self.A, "err-a"), (self.B, "err-b")])
        self.assertIsInstance(got["transport_errors"][0]["at"], float)
        self.assertNotIn("transport_error", got)
        [listed] = journal.list_for(self.A)
        self.assertEqual(details(listed["slots"]), {self.A: "err-a"})
        [listed] = journal.list_for(self.B)
        self.assertEqual(details(listed["slots"]), {self.B: "err-b"})

    def test_a_later_error_replaces_only_its_own_slot(self):
        mid = self.two()
        journal.transport_error(mid, self.A, "err-a2")
        item = self.entry(mid)
        self.assertEqual({r["slot"]: r["transport_error"]["detail"] for r in item["recipients"]},
                         {self.A: "err-a2", self.B: "err-b"})
        self.assertEqual([e["detail"] for e in journal.get(mid, self.A)["transport_errors"]],
                         ["err-a2", "err-b"])

    def test_a_slot_without_an_error_has_no_transport_error_key(self):
        mid = self.req(to=[self.A, self.B])
        journal.transport_error(mid, self.B, "only-b")
        item = self.entry(mid)
        self.assertNotIn("transport_error", item["recipients"][0])
        self.assertEqual([e["for"] for e in journal.get(mid, self.A)["transport_errors"]], [self.B])


class JournalReworkTests(JournalBase):
    def append_lines(self, *records):
        with open(self.jpath, "ab") as fh:
            for item in records:
                fh.write((item if isinstance(item, str) else json.dumps(item)).encode() + b"\n")

    def last_seq(self):
        return journal.read_only_view()["last_seq"]

    def grant_line(self, seq, source=None, target=None, through=1):
        return {"schema": 1, "seq": seq, "type": "handover", "at": self.now,
                "from": source or cc(), "to": target or cc(), "through_seq": through}

    # -- clock step-back ----------------------------------------------------

    def test_clock_step_back_keeps_records_through_a_rewrite(self):
        mid = self.msg(body="kept")
        journal.handover(self.A, self.B)
        real = self.now
        self.now = real - 600
        journal.handover(self.A, self.B)  # a re-grant rewrites the journal
        self.assertIn(mid, self.ids())
        self.assertEqual(journal.get(mid, self.A)["body"], "kept")
        self.assertEqual(journal.ack(mid, self.A)["by"], self.A)
        self.now = real
        self.assertEqual(journal.get(mid, self.A)["acks"][0]["by"], self.A)
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)

    # -- forward compatibility ---------------------------------------------

    def test_unknown_records_survive_a_rewrite_and_age_out_by_at(self):
        first, _body = self.fill(ack_first=True)
        seq = self.last_seq()
        later = {"schema": 2, "seq": seq + 1, "type": "message", "at": self.now, "x": [1, 2]}
        odd = '{"schema": 1, "seq": %d, "type": "poll", "at": %r, "n": {"a": 1}}' % (seq + 2, self.now)
        no_seq = '{"schema": 3, "type": "poll", "at": %r}' % self.now
        self.append_lines(json.dumps(later), odd, no_seq)
        self.set_cap(self.size())
        self.msg(body="b" * 2000)
        self.assertNotIn(first, self.ids())
        data = self.read()
        self.assertIn(json.dumps(later).encode() + b"\n", data)
        self.assertIn(odd.encode() + b"\n", data)
        self.assertNotIn(no_seq.encode(), data)
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)
        self.assertEqual(self.meta()["total_bytes"], len(data))
        self.now += 15 * 86400
        self.msg()
        self.assertNotIn(odd.encode(), self.read())
        self.assertNotIn(json.dumps(later).encode(), self.read())

    def test_unknown_record_is_not_counted_as_malformed(self):
        self.msg()
        self.append_lines({"schema": 9, "seq": 2, "type": "future", "at": self.now})
        view = journal.read_only_view()
        self.assertEqual((view["malformed_lines"], view["last_seq"]), (0, 2))

    # -- message ids --------------------------------------------------------

    def test_duplicate_request_and_message_ids_are_refused(self):
        rid = self.req()
        mid = journal.record_message(cc(), [self.A], "x", message_id=str(uuid.uuid4()))
        before = self.read()
        with self.assertRaises(journal.JournalRefused):
            journal.record_request(rid, cc(), [self.A], "again", self.now + 60, "/nonexistent/x")
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.A], "again", message_id=mid)
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.A], "again", message_id=rid)
        os.unlink(self.mpath)  # without the meta accelerator it is still refused
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.A], "again", message_id=mid)
        self.assertEqual(self.read(), before)

    def test_reply_id_cannot_be_squatted(self):
        rid = self.req(to=[self.B], sender=self.A)
        before = self.read()
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.A], "x", message_id=journal.reply_message_id(rid))
        self.assertEqual(self.read(), before)
        args = (rid, self.B, [self.A], "answer", self.now + 600, "/nonexistent/x")
        self.assertEqual(journal.record_reply(*args), "recorded")

    def test_reply_dedupe_compares_every_field(self):
        rid = self.req(to=[self.B], sender=self.A)
        mailbox = "/nonexistent/x"
        self.assertEqual(journal.record_reply(rid, self.B, [self.A], "answer", self.now + 600, mailbox),
                         "recorded")
        for sender, to in ((self.C, [self.A]), (self.B, [self.X])):
            with self.subTest(sender=sender, to=to):
                with self.assertRaises(journal.JournalRefused) as ctx:
                    journal.record_reply(rid, sender, to, "answer", self.now + 600, mailbox)
                self.assertIn("already has a recorded reply", str(ctx.exception))
        self.assertEqual(journal.record_reply(rid, self.B, [self.A], "answer", self.now + 600, mailbox),
                         "same")

    def test_reply_dedupe_refuses_a_non_reply_under_the_reply_id(self):
        rid = str(uuid.uuid4())
        self.msg()
        self.append_lines({"schema": 1, "seq": 2, "type": "message", "at": self.now,
                           "message_id": journal.reply_message_id(rid), "kind": "message",
                           "from": self.B, "to": [self.A], "body": "answer"})
        with self.assertRaises(journal.JournalRefused):
            journal.record_reply(rid, self.B, [self.A], "answer", self.now + 600, "/nonexistent/x")

    # -- settle -------------------------------------------------------------

    def test_settle_needs_a_recipient_or_a_covering_session(self):
        rid = self.req(to=[self.A])
        before = self.read()
        with self.assertRaises(journal.JournalUpdateFailed) as ctx:
            journal.settle(rid, "replied", self.B)
        self.assertIn("may not settle", str(ctx.exception))
        self.assertEqual(self.read(), before)
        self.assertIsNone(self.entry(rid)["settlement"])
        journal.handover(self.A, self.C)
        self.assertEqual(journal.settle(rid, "replied", self.C)["by"], self.C)

    def test_consumed_is_settled_only_by_the_requester(self):
        rid = self.req(to=[self.B], sender=self.A)
        journal.record_reply(rid, self.B, [self.A], "answer", self.now + 600, "/nonexistent/x")
        reply_id = journal.reply_message_id(rid)
        for who in (self.B, self.C):
            with self.subTest(who=who):
                with self.assertRaises(journal.JournalUpdateFailed):
                    journal.settle(reply_id, "consumed", who)
        self.assertIsNone(self.entry(reply_id)["settlement"])
        self.assertEqual(journal.settle(reply_id, "consumed", self.A)["by"], self.A)

    def test_settle_on_an_absent_entry_is_a_no_op(self):
        self.msg()
        before = self.snapshot()
        self.assertIsNone(journal.settle(str(uuid.uuid4()), "replied", self.A))
        self.assertEqual(self.snapshot(), before)

    # -- handovers ----------------------------------------------------------

    def test_unrelated_grants_do_not_revoke_a_legitimate_one(self):
        mid = self.msg()
        journal.handover(self.A, self.B)
        for _ in range(72):
            journal.handover(cc(), cc())
        self.assertEqual(journal.get(mid, self.B)["body"], "hello")

    def test_grant_beyond_the_global_cap_is_refused(self):
        self.msg()
        chain = [self.A] + [cc() for _ in range(512)]  # every grantor is reachable from slot A
        grants = [self.grant_line(2 + i, source=chain[i], target=chain[i + 1]) for i in range(512)]
        first = grants[0]
        self.append_lines(*grants)
        self.assertEqual(len(journal.read_only_view()["handovers"]), 512)
        before = self.read()
        with self.assertRaises(journal.JournalRefused):
            journal.handover(self.A, self.B)
        self.assertEqual(self.read(), before)
        journal.handover(first["from"], first["to"])  # replacing a pair stays within the cap
        self.assertEqual(len(journal.read_only_view()["handovers"]), 512)

    def test_crafted_flood_loads_fast_and_keeps_the_newest_grants(self):
        self.msg()
        mid = self.ids()[0]
        lines = [self.grant_line(2 + i) for i in range(1999)]
        lines.append(self.grant_line(2001, source=self.A, target=self.B))
        self.append_lines(*lines)
        started = time.monotonic()
        view = journal.read_only_view()
        self.assertLess(time.monotonic() - started, 5.0)
        started = time.monotonic()
        self.assertEqual(journal.get(mid, self.B)["body"], "hello")  # reach() runs: B is not the slot
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual([(g["from"], g["to"]) for g in view["handovers"]],
                         [(g["from"], g["to"]) for g in lines[-512:]])

    # -- meta, links, identities --------------------------------------------

    def test_corrupt_meta_is_treated_as_absent(self):
        mid = self.msg()
        for step in ("message", "ack"):
            meta = self.meta()
            meta["oldest_at"] = 10 ** 400
            with open(self.mpath, "w") as fh:
                json.dump(meta, fh)
            if step == "message":
                self.msg(body="after")
            else:
                self.assertEqual(journal.ack(mid, self.A)["by"], self.A)
        self.assertEqual(len(self.ids()), 2)

    def test_hard_linked_journal_refused(self):
        self.msg()
        os.link(self.jpath, os.path.join(self.root, "alias"))
        with self.assertRaises(journal.JournalUnavailable):
            self.msg()
        with self.assertRaises(journal.JournalUnavailable):
            journal.read_only_view()

    def test_trailing_newline_is_no_identity(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.A[3:] + "\n"}):
            with self.assertRaises(journal.JournalRefused):
                journal.caller()
        with self.assertRaises(journal.JournalRefused):
            journal.record_message(cc(), [self.A + "\n"], "x")
        mid = self.msg()
        self.append_lines({"schema": 1, "seq": 2, "type": "ack", "at": self.now,
                           "message_id": mid + "\n", "for": self.A, "by": self.A})
        self.assertEqual(journal.read_only_view()["malformed_lines"], 1)

    # -- second rework ------------------------------------------------------

    def dangling_ack(self):
        """A valid-looking ack for an unknown message: dead in the model, so any commit rewrites."""
        return {"schema": 1, "seq": self.last_seq() + 1, "type": "ack", "at": self.now,
                "message_id": str(uuid.uuid4()), "for": self.A, "by": self.A}

    def test_clock_step_back_of_two_days_keeps_records_through_a_rewrite(self):
        real = self.now
        kept = self.msg(body="kept")
        self.now = real - 2 * 86400  # kept is now dated two days ahead
        other = self.msg(body="other")
        self.append_lines(self.dangling_ack())  # makes every commit a rewrite
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)
        self.assertEqual(sorted(self.ids()), sorted([kept, other]))
        ops = (
            ("slow-path message", lambda: self.msg()),
            ("event rewrite", lambda: journal.ack(other, self.A)),
            ("prune", lambda: journal.handover(self.A, self.B)),
        )
        for name, op in ops:
            with self.subTest(op=name):
                self.append_lines(self.dangling_ack())
                op()
                self.assertIn(kept, self.ids())
                self.assertEqual(journal.get(kept, self.A)["body"], "kept")
        self.now = real
        self.assertEqual(journal.get(kept, self.A)["body"], "kept")
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)

    def test_record_more_than_the_age_cap_ahead_is_malformed_and_dropped_on_rewrite(self):
        real = self.now
        kept = self.msg(body="kept")
        self.now = real - 20 * 86400  # kept is now dated 20 days ahead
        self.assertEqual(journal.read_only_view()["malformed_lines"], 1)
        self.assertEqual(self.ids(), [])
        self.append_lines(self.dangling_ack())  # makes the next commit a rewrite
        self.msg(body="new")
        self.assertEqual(journal.read_only_view()["malformed_lines"], 0)
        self.now = real
        self.assertNotIn(kept, self.ids())
        self.assertNotIn(b"kept", self.read())

    def test_no_rewrite_is_refused_because_of_a_future_dated_record(self):
        real = self.now
        self.msg(body="kept")
        for back in (2 * 86400, 13 * 86400, 30 * 86400):
            with self.subTest(back_days=back // 86400):
                self.now = real - back
                self.append_lines(self.dangling_ack())
                other = self.msg()
                self.append_lines(self.dangling_ack())
                journal.ack(other, self.A)
                self.append_lines(self.dangling_ack())
                journal.handover(self.A, self.B)

    def test_far_future_line_with_an_unknown_schema_is_dropped_but_a_near_one_kept(self):
        real = self.now
        self.msg()
        near = {"schema": 2, "seq": 2, "type": "message", "at": real + 5 * 86400, "x": 1}
        far = {"schema": 2, "seq": 3, "type": "message", "at": real + 20 * 86400, "x": 2}
        self.append_lines(near, far)
        self.assertEqual(journal.read_only_view()["malformed_lines"], 1)
        self.append_lines(self.dangling_ack())
        self.msg()
        data = self.read()
        self.assertIn(json.dumps(near).encode(), data)
        self.assertNotIn(json.dumps(far).encode(), data)

    def test_malformed_line_with_a_seq_advances_seq_when_meta_is_lost(self):
        self.msg()
        self.append_lines({"schema": 1, "seq": 7, "type": "bogus-shape", "at": "not a number"})
        os.unlink(self.mpath)
        self.msg(body="next")
        self.assertEqual([r["seq"] for r in self.records()][-1], 8)

    def test_fresh_id_with_a_non_v4_request_id_is_refused(self):
        rid = journal.reply_message_id(str(uuid.uuid4()))  # a uuid5
        before = self.read() if os.path.exists(self.jpath) else None
        with self.assertRaises(journal.JournalRefused):
            journal.record_request(rid, cc(), [self.A], "ask", self.now + 60, "/nonexistent/x",
                                   fresh_id=True)
        self.assertEqual(self.read() if os.path.exists(self.jpath) else None, before)

    def test_forged_grantors_cannot_fill_the_grant_cap(self):
        mid = self.msg()
        self.append_lines(*[self.grant_line(2 + i) for i in range(512)])
        self.assertEqual(len(journal.read_only_view()["handovers"]), 512)
        journal.handover(self.A, self.B)
        self.assertEqual([(g["from"], g["to"]) for g in journal.read_only_view()["handovers"]],
                         [(self.A, self.B)])
        self.assertEqual(journal.get(mid, self.B)["body"], "hello")

    def test_grant_chained_from_a_kept_grant_is_retained(self):
        mid = self.msg()
        self.append_lines(self.grant_line(2, source=self.C, target=self.X),
                          self.grant_line(3, source=self.A, target=self.B),
                          self.grant_line(4, source=self.B, target=self.C))
        journal.handover(cc(), cc())
        pairs = [(g["from"], g["to"]) for g in journal.read_only_view()["handovers"]]
        self.assertIn((self.C, self.X), pairs)
        self.assertEqual(journal.get(mid, self.X)["body"], "hello")

    def test_reply_id_cannot_be_pre_squatted_before_the_request(self):
        rid = str(uuid.uuid4())
        journal.record_message(cc(), [self.A], "squat", message_id=journal.reply_message_id(rid))
        before = self.read()
        with self.assertRaises(journal.JournalRefused):
            journal.record_request(rid, cc(), [self.A], "ask", self.now + 60, "/nonexistent/x")
        self.assertEqual(self.read(), before)

    def test_fresh_id_request_takes_the_fast_path(self):
        self.msg()
        calls = []
        real = journal._Writer.load
        with mock.patch.object(journal._Writer, "load", lambda w, n: calls.append(1) or real(w, n)):
            rid = str(uuid.uuid4())
            journal.record_request(rid, cc(), [self.A], "ask", self.now + 60, "/nonexistent/x",
                                   fresh_id=True)
        self.assertEqual(calls, [])
        self.assertIn(rid, self.ids())

    def edge_setup(self, count, through):
        self.msg()
        mid = self.msg()  # seq 2
        targets = [cc() for _ in range(count)]
        lines = [self.grant_line(3 + i, source=self.A, target=t, through=through) for i, t in enumerate(targets)]
        self.append_lines(*lines)
        return mid, targets

    def test_fan_out_beyond_the_edge_bound_fails_closed(self):
        mid, targets = self.edge_setup(70, through=2)
        self.assertEqual(journal.get(mid, targets[0])["body"], "hello")
        self.assertEqual(journal.get(mid, targets[63])["body"], "hello")
        for target in targets[64:]:
            with self.assertRaises(journal.JournalRefused):
                journal.get(mid, target)

    def test_irrelevant_edges_are_not_counted_against_the_bound(self):
        mid, targets = self.edge_setup(70, through=1)  # through < seq: irrelevant to the message
        relevant = cc()
        self.append_lines(self.grant_line(100, source=self.A, target=relevant, through=2))
        self.assertEqual(journal.get(mid, relevant)["body"], "hello")
        with self.assertRaises(journal.JournalRefused):
            journal.get(mid, targets[0])

    def test_settle_on_an_absent_entry_in_a_fresh_home_creates_nothing(self):
        self.assertIsNone(journal.settle(str(uuid.uuid4()), "replied", self.A))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".codex")))

    def test_settle_on_an_absent_entry_does_not_repair_a_torn_tail(self):
        self.msg()
        with open(self.jpath, "ab") as fh:
            fh.write(b'{"torn":')
        before = self.snapshot()
        self.assertIsNone(journal.settle(str(uuid.uuid4()), "replied", self.A))
        self.assertEqual(self.snapshot(), before)

    def test_settle_rejects_an_invalid_id_or_state(self):
        rid = self.req()
        before = self.snapshot()
        for mid, state in ((rid.upper(), "replied"), ("nope", "replied"), (rid + "\n", "replied"),
                           (rid, "bogus"), (rid, None)):
            with self.subTest(mid=mid, state=state):
                with self.assertRaises(journal.JournalUpdateFailed):
                    journal.settle(mid, state, self.A)
        self.assertEqual(self.snapshot(), before)

    def test_only_later_version_records_are_foreign(self):
        self.msg()
        self.append_lines({"schema": 2, "seq": 2, "type": "message", "at": self.now},
                          {"schema": 7, "seq": 3, "type": 5, "at": self.now},
                          {"schema": 1, "seq": 4, "type": "poll", "at": self.now})
        view = journal.read_only_view()
        self.assertEqual((view["malformed_lines"], view["last_seq"]), (0, 4))
        bad = [{"seq": 5, "type": "poll", "at": self.now},
               {"schema": "2", "seq": 5, "type": "poll", "at": self.now},
               {"schema": 1, "seq": 5, "type": 5, "at": self.now},
               {"schema": 1, "seq": 5, "type": ["ack"], "at": self.now},
               {"schema": 1, "seq": 5, "at": self.now},
               {"schema": 0, "seq": 5, "type": "poll", "at": self.now},
               {"schema": True, "seq": 5, "type": "poll", "at": self.now},
               {"schema": 2.0, "seq": 5, "type": "poll", "at": self.now}]
        self.append_lines(*bad)
        view = journal.read_only_view()
        self.assertEqual((view["malformed_lines"], view["last_seq"]), (len(bad), 5))

    def test_reply_id_held_by_a_non_reply_has_its_own_message(self):
        rid = str(uuid.uuid4())
        self.msg()
        self.append_lines({"schema": 1, "seq": 2, "type": "message", "at": self.now,
                           "message_id": journal.reply_message_id(rid), "kind": "message",
                           "from": self.B, "to": [self.A], "body": "answer"})
        with self.assertRaises(journal.JournalRefused) as ctx:
            journal.record_reply(rid, self.B, [self.A], "answer", self.now + 600, "/nonexistent/x")
        self.assertIn("reply id is taken by another record", str(ctx.exception))

    def test_handover_cap_refusal_names_expiry_or_pruning(self):
        self.msg()
        chain = [self.A] + [cc() for _ in range(512)]
        self.append_lines(*[self.grant_line(2 + i, source=chain[i], target=chain[i + 1])
                            for i in range(512)])
        with self.assertRaises(journal.JournalRefused) as ctx:
            journal.handover(self.A, self.B)
        self.assertIn("retry after older messages expire or are pruned", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
