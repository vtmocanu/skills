"""Web dashboard read model: parity with the CLI views, bounded reads, hygiene and no mutation."""

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from unittest import mock

from peer_tests.support import *  # noqa: F401,F403
from peer_tests.test_channel_send import ChannelSendBase, HOLDER
from peer_tests.test_opencode import OC_B
from session_peers import channels as sp_channels, journal as sp_journal, topics as sp_topics, webdata

MIB = 1024 * 1024


def log_path(key):
    return pathlib.Path(sp_topics.topic_stem(sp_channels.topic_name(key), sp_channels.history_dir()) + ".jsonl")


def meta_path(key):
    return pathlib.Path(sp_topics.topic_stem(sp_channels.topic_name(key), sp_channels.history_dir()) + ".meta.json")


def encode(entry):
    """One history line exactly as topics writes it."""
    return json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n"


def make_entry(key, seq, kind, data=None, text=None, sid="s1", ts=None):
    entry = {
        "seq": seq, "ts": ts or sp_runtime.now_iso(), "topic": sp_channels.topic_name(key),
        "from_kind": "cc", "from_name": None, "from_sid": sid, "kind": kind,
    }
    if data is not None:
        entry["data"] = data
    else:
        entry["text"] = text
    return entry


def send_data(body, mid=None, **extra):
    data = {
        "sender": "cc:s1", "body": body, "message_id": mid or new_uuid(), "journaled": False,
        "delivery": "none", "recipients": [], "excluded": [], "opencode_coverage": None,
    }
    data.update(extra)
    return data


def write_topic(key, lines, next_seq=None, meta=True):
    """Write ``lines`` (encoded strings or raw bytes) as the key's history log, as a crash-free writer would."""
    path = log_path(key)
    os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
    with open(str(path), "wb") as fh:
        for line in lines:
            fh.write(line if isinstance(line, bytes) else line.encode("utf-8"))
    if meta:
        meta_path(key).write_text(json.dumps({"topic": sp_channels.topic_name(key), "next_seq": next_seq or len(lines) + 1}))


def tree_state(*roots):
    """(path, kind, size, mtime_ns, sha256) of everything under the roots, directories included."""
    rows = []
    for root in roots:
        if not os.path.exists(root):
            rows.append((root, "missing"))
            continue
        for base, dirs, files in os.walk(root):
            rows.append((base, "dir", os.stat(base).st_mtime_ns))
            for name in sorted(files):
                path = os.path.join(base, name)
                st = os.lstat(path)
                digest = None
                if stat.S_ISREG(st.st_mode):
                    with open(path, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                rows.append((path, "file", st.st_size, st.st_mtime_ns, digest))
    return sorted(rows, key=str)


class ReadMeter:
    """Counts the bytes os.pread and os.read return, per opened path, and the pread ranges."""

    def __init__(self):
        self.fd_path = {}
        self.ranges = {}
        self.bytes = {}

    def __enter__(self):
        self._open, self._pread, self._read = os.open, os.pread, os.read
        meter = self

        def op(path, flags, *args, **kwargs):
            fd = meter._open(path, flags, *args, **kwargs)
            meter.fd_path[fd] = str(path)
            return fd

        def pread(fd, count, offset):
            data = meter._pread(fd, count, offset)
            name = meter.fd_path.get(fd)
            if name is not None:
                meter.ranges.setdefault(name, []).append((offset, offset + len(data)))
                meter.bytes[name] = meter.bytes.get(name, 0) + len(data)
            return data

        def read(fd, count):
            data = meter._read(fd, count)
            name = meter.fd_path.get(fd)
            if name is not None:
                meter.bytes[name] = meter.bytes.get(name, 0) + len(data)
            return data

        os.open, os.pread, os.read = op, pread, read
        return self

    def __exit__(self, *exc):
        os.open, os.pread, os.read = self._open, self._pread, self._read

    def total(self):
        return sum(self.bytes.values())

    def topic_bytes(self):
        return {p: n for p, n in self.bytes.items() if p.endswith(".jsonl")}

    def overlaps(self, name):
        spans = sorted(self.ranges.get(name, []))
        return [(a, b) for a, b in zip(spans, spans[1:]) if b[0] < a[1]]


class WebdataPureTests(unittest.TestCase):
    def test_escape_text_shows_controls_bidi_and_separators(self):
        raw = "a\x00b\x1b[31m\x7f\x9bc‮d⁦e⁩‎‏؜  \n\tz"
        out = webdata.escape_text(raw)
        for ch in "\x00\x1b\x7f\x9b‮⁦⁩‎‏؜  \n\t":
            self.assertNotIn(ch, out)
        self.assertEqual(
            out, "a\\u0000b\\u001b[31m\\u007f\\u009bc\\u202ed\\u2066e\\u2069\\u200e\\u200f\\u061c\\u2028\\u2029\\u000a\\u0009z")

    def test_escape_body_keeps_newline_and_tab_only(self):
        self.assertEqual(webdata.escape_body("a\nb\tc\x1b\r‮"), "a\nb\tc\\u001b\\u000d\\u202e")

    def test_verdicts(self):
        cases = {
            "APPROVE": ("approve", "APPROVE", "", None),
            "APPROVE at 1a2b3c4d: looks good": ("approve", "APPROVE", "at 1a2b3c4d: looks good", "1a2b3c4d"),
            "REVISE: 3 rename x at abcdef0": ("revise", "REVISE: 3", "rename x at abcdef0", "abcdef0"),
            "BLOCK - do not merge": ("block", "BLOCK", "do not merge", None),
        }
        for body, (verdict, label, text, sha) in cases.items():
            self.assertEqual(
                webdata.detect_verdict(body), {"verdict": verdict, "label": label, "text": text, "sha": sha}, body)
        for body in ("APPROVED", "approve", " APPROVE", "REVISE 3", "REVISE: x", "BLOCKED", "see APPROVE", "", None):
            self.assertIsNone(webdata.detect_verdict(body), body)
        # A too-short hex token is no sha.
        self.assertIsNone(webdata.detect_verdict("APPROVE at abc12")["sha"])

    def test_membership_text(self):
        self.assertEqual(webdata.membership_text("channel.created", {}), "created")
        self.assertEqual(webdata.membership_text("channel.joined", {"member": "cc:a"}), "cc:a joined")
        self.assertEqual(webdata.membership_text("channel.left", {"member": "cc:a"}), "cc:a left")
        self.assertEqual(webdata.membership_text("channel.removed", {"member": "cc:a"}), "cc:a removed")
        self.assertEqual(
            webdata.membership_text("channel.handover", {"member": "cc:a", "to": "cc:b"}), "cc:a handed over to cc:b")

    def test_entry_kinds(self):
        self.assertEqual(
            [webdata.entry_kind("channel." + k) for k in ("created", "joined", "left", "removed", "handover", "send", "outcome")],
            ["created", "joined", "left", "removed", "handover", "send", "outcome"])
        self.assertEqual(webdata.entry_kind("channel.note"), "other")
        self.assertEqual(webdata.entry_kind(None), "other")

    def test_ack_tally_excludes_refused_and_lists_missing(self):
        rows = [
            {"recipient": "a", "state": "acked", "ack": "acked"},
            {"recipient": "b", "state": "transport accepted", "ack": "pending"},
            {"recipient": "c", "state": "refused: x", "ack": "not requested (unverified)"},
            {"recipient": "d", "state": "transport accepted", "ack": "unverified"},
        ]
        self.assertEqual(
            webdata.ack_tally(rows), {"acked": 1, "total": 3, "missing": ["b", "d"], "unverified": 1})
        self.assertEqual(webdata.ack_tally([{"recipient": "a", "state": "recorded"}])["total"], 0)

    def test_coverage_native_claude(self):
        self.assertEqual(webdata.channel_coverage(["codex:x", "cc:y"]), {"native_claude": True})
        self.assertEqual(webdata.channel_coverage(["codex:x", "oc:y"]), {"native_claude": False})
        self.assertEqual(webdata.channel_coverage([]), {"native_claude": False})

    def test_pure_send_state_and_rows_rules(self):
        send_state_from = sp_channels.send_state_from
        self.assertEqual(send_state_from(True, "held", False), "complete")
        self.assertEqual(send_state_from(False, "held", True), "in progress")
        self.assertEqual(send_state_from(False, "error", True), "unverified")
        self.assertEqual(send_state_from(False, "free", True), "complete")
        self.assertEqual(send_state_from(False, "absent", False), "unknown")
        data = {
            "journaled": True, "recipients": ["a", "b", "c"],
            "excluded": [{"member": "c", "reason": "no journal"}],
        }
        rows = sp_channels.delivery_rows(data, "complete", {"a": "transport accepted", "b": "queued (thread paused)"},
                                         {"a": "acked", "b": "pending"})
        self.assertEqual(rows, [
            {"recipient": "a", "state": "acked", "ack": "acked"},
            {"recipient": "b", "state": "queued (thread paused)", "ack": "pending"},
            {"recipient": "c", "state": "refused: no journal", "ack": "not requested (unverified)"},
        ])
        rows = sp_channels.delivery_rows(data, "in progress", {}, None)
        self.assertEqual([r["ack"] for r in rows], ["unverified", "unverified", "not requested (unverified)"])
        self.assertEqual(rows[0]["state"], "in progress")

    def test_entry_view_facts(self):
        entry = make_entry("ops", 4, "channel.handover", {"member": "cc:a", "to": "cc:b"})
        view = webdata.entry_view(entry)
        self.assertEqual((view["kind"], view["text"], view["actor"]), ("handover", "cc:a handed over to cc:b", "cc:s1"))
        anon = dict(make_entry("ops", 1, "channel.note", text="hi"), from_sid=None)
        self.assertEqual(webdata.entry_view(anon)["actor"], "anonymous")
        self.assertEqual(webdata.entry_view(anon)["text"], "hi")
        outcome = make_entry("ops", 5, "channel.outcome", {"message_id": "m", "outcomes": {"cc:a": "transport accepted"}})
        view = webdata.entry_view(outcome)
        self.assertEqual(view["outcomes"], {"cc:a": "transport accepted"})
        self.assertEqual(view["text"], "outcomes of message m for 1 recipients")
        # Malformed outcome data degrades to nothing.
        bad = webdata.entry_view(make_entry("ops", 6, "channel.outcome", {"message_id": "m", "outcomes": "nope"}))
        self.assertEqual(bad["outcomes"], {})
        self.assertEqual(webdata.entry_view({"seq": 7, "oversized": True}), {"seq": 7, "kind": "oversized", "oversized": True})

    def test_the_largest_send_envelope_fits_the_entry_line_limit(self):
        # 64 members, every one excluded with the longest reason, plus coverage and gaps, around a maximal body.
        reason = "r" * 4096
        excluded = [{"member": "oc:ses_" + "x" * 26, "reason": reason} for _ in range(64)]
        data = send_data(
            "", recipients=["oc:ses_" + "x" * 26] * 64, excluded=excluded,
            opencode_coverage={"enumerated": False, "error": "e" * 4096},
            discovery_incomplete=["g" * 4096] * 8)
        line = encode(make_entry("general", 2 ** 31, "channel.send", data))
        envelope = len(line.encode("utf-8"))
        self.assertLess(envelope, webdata.ENTRY_LINE_MAX - 6 * sp_constants.MAX_TEXT_CHARS)


class WebdataBase(ChannelSendBase):
    def setUp(self):
        super().setUp()
        self.sender_cc()

    def roots(self):
        return (sp_channels.channels_root(), sp_journal.journal_dir(), os.path.join(str(self.codex_dir), "session-peers"))

    def page(self, key="ops", **kw):
        return webdata.channel_page(key, **kw)

    def web_send_view(self, key="ops", index=-1, **kw):
        sends = [e for e in webdata.channel_page(key, **kw)["entries"] if e["kind"] == "send"]
        return sends[index]["delivery"]

    def new_channel(self, key="cap"):
        sp_channels.create_channel(key, self.me, [])
        return key

    _serial = 0

    def fresh_channel(self, stem):
        WebdataBase._serial += 1
        return self.new_channel("%s%d" % (stem, WebdataBase._serial))


class WebdataParityTests(WebdataBase):
    def setUp(self):
        super().setUp()
        self.tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + self.tid, "oc:" + OC_B)

    def assert_parity(self, index=-1):
        web = self.web_send_view(index=index)
        self.assertNotIn("partial", web)
        self.assertEqual(web, self.view_of("ops", index))
        return web

    def test_complete_send_matches_the_cli_view(self):
        rc, _result, err = self.send()
        self.assertEqual(rc, 0, err)
        view = self.assert_parity()
        self.assertEqual(view["state"], "complete")
        self.assertEqual(
            [(r["recipient"], r["state"]) for r in view["recipients"]],
            [("codex:" + self.tid, "transport accepted"), ("oc:" + OC_B, "not woken (OpenCode idle)")])

    def test_journaled_send_matches_for_pending_acked_and_refused_rows(self):
        self.set_oc(get_error_tag="Boom")  # OC_B cannot be addressed by the journal: refused
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 1, err)
        view = self.assert_parity()
        rows = {r["recipient"]: r for r in view["recipients"]}
        self.assertEqual((rows["codex:" + self.tid]["state"], rows["codex:" + self.tid]["ack"]), ("transport accepted", "pending"))
        self.assertEqual(rows["oc:" + OC_B]["ack"], "not requested (unverified)")
        self.assertTrue(rows["oc:" + OC_B]["state"].startswith("refused: "))
        sp_journal.ack(result["message_id"], "codex:" + self.tid)
        view = self.assert_parity()
        row = [r for r in view["recipients"] if r["recipient"] == "codex:" + self.tid][0]
        self.assertEqual((row["state"], row["ack"]), ("acked", "acked"))

    def test_nothing_delivered_send_reads_recorded(self):
        sp_channels.create_channel("solo", self.me, [])
        rc, _result, err = self.send(name="solo")
        self.assertEqual(rc, 0, err)
        web = self.web_send_view("solo")
        self.assertEqual(web, {"state": "recorded", "recipients": []})
        self.assertEqual(web, self.view_of("solo"))

    def test_unreadable_journal_reads_unverified_acks(self):
        rc, _result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        with mock.patch.object(sp_journal, "read_since", side_effect=sp_journal.JournalUnavailable("denied")):
            view = self.web_send_view()
        self.assertEqual([r["ack"] for r in view["recipients"]], ["unverified", "unverified"])
        self.assertEqual(view["recipients"][0]["state"], "transport accepted")

    def test_transport_errors_show_apart_from_acks(self):
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        sp_journal.transport_error(result["message_id"], "codex:" + self.tid, "socket closed")
        row = self.web_send_view()["recipients"][0]
        self.assertEqual((row["ack"], row["transport_error"]), ("pending", "socket closed"))
        self.assertNotIn("transport_error", self.web_send_view()["recipients"][1])

    def test_acks_beyond_the_journal_budget_read_unverified_and_partial(self):
        rc, result, err = self.send("--record")
        self.assertEqual(rc, 0, err)
        # Newer journal traffic larger than the budget pushes the message out of the window.
        for i in range(4):
            sp_journal.record_message(self.me, ["codex:" + self.tid], "filler %d %s" % (i, "x" * 30000))
        budget = webdata.Budget(70000)
        page = webdata.channel_page("ops", budget=budget)
        send = [e for e in page["entries"] if e["kind"] == "send"][0]
        self.assertTrue(page["journal_partial"])
        self.assertTrue(page["partial"])
        self.assertTrue(send["delivery"]["partial"])
        self.assertEqual({r["ack"] for r in send["delivery"]["recipients"]}, {"unverified"})
        self.assertLessEqual(budget.used, 70000 + sp_journal.JOURNAL_LINE_MAX)

    def test_all_post_tally(self):
        sp_channels.create_channel("two", self.me, ["codex:" + self.tid, "oc:" + OC_B])
        rc, result, err = self.send("--record", name="two", message="@all freeze")
        self.assertEqual(rc, 0, err)
        sp_journal.ack(result["message_id"], "codex:" + self.tid)
        send = [e for e in webdata.channel_page("two")["entries"] if e["kind"] == "send"][0]
        self.assertEqual(
            send["tally"], {"acked": 1, "total": 2, "missing": ["oc:" + OC_B], "unverified": 0})
        plain = self.send(name="two", message="no tally")[1]
        self.assertTrue(plain)
        last = [e for e in webdata.channel_page("two")["entries"] if e["kind"] == "send"][-1]
        self.assertNotIn("tally", last)

    def test_membership_entries_and_outcome_rows(self):
        self.send()
        entries = webdata.channel_page("ops")["entries"]
        self.assertEqual([e["kind"] for e in entries], ["created", "send", "outcome"])
        self.assertEqual(entries[0]["text"], "created")
        self.assertEqual(entries[2]["outcomes"], {
            "codex:" + self.tid: "transport accepted", "oc:" + OC_B: "not woken (OpenCode idle)"})
        joiner = self.as_cc()
        self.ok("join", "ops")
        self.ok("leave", "ops")
        kinds = [(e["kind"], e["text"]) for e in webdata.channel_page("ops")["entries"][-2:]]
        self.assertEqual(kinds, [("joined", "%s joined" % joiner), ("left", "%s left" % joiner)])

    def test_verdict_on_a_real_send(self):
        self.send(message="APPROVE at 0123abc: ship it")
        send = [e for e in webdata.channel_page("ops")["entries"] if e["kind"] == "send"][0]
        self.assertEqual(send["verdict"], {"verdict": "approve", "label": "APPROVE", "text": "at 0123abc: ship it", "sha": "0123abc"})
        self.assertEqual(webdata.channel_page("ops")["coverage"], {"native_claude": True})


class WebdataInterruptedSendTests(WebdataBase):
    def setUp(self):
        super().setUp()
        self.tid, self.rollout = self.one_thread()
        self.channel("ops", "codex:" + self.tid)

    def crash_at(self, point):
        def hook(name):
            if name == point:
                raise RuntimeError("killed at " + name)

        with mock.patch.object(sp_channels, "_failpoint", hook):
            with self.assertRaisesRegex(RuntimeError, "killed at"):
                self.cli("send", "--to", "#ops", "--message", "freeze main", "--record")
        return self.send_entries()[0]["data"]["message_id"]

    def lock_file(self, mid):
        return pathlib.Path(sp_channels.send_lock_path(mid))

    def test_a_held_lock_reads_in_progress_like_the_cli(self):
        mid = self.crash_at("send_after_history")
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.lock_file(mid))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self._children.append(holder)
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        web = self.web_send_view()
        self.assertEqual(web["state"], "in progress")
        self.assertEqual(web, self.view_of())
        holder.stdin.close()
        holder.wait(timeout=10)
        web = self.web_send_view()
        self.assertEqual((web["state"], web["recipients"][0]["state"]), ("unknown", "unknown"))
        self.assertEqual(web, self.view_of())

    def test_a_free_lock_and_no_outcome_reads_unknown_like_the_cli(self):
        self.crash_at("send_before_outcome")
        web = self.web_send_view()
        self.assertEqual(web["state"], "unknown")
        self.assertEqual(web, self.view_of())

    def test_an_absent_lock_reads_unknown(self):
        mid = self.crash_at("send_before_outcome")
        self.lock_file(mid).unlink()
        self.assertEqual(self.web_send_view()["state"], "unknown")
        self.assertEqual(self.view_of()["state"], "unknown")

    def test_a_lock_that_cannot_be_opened_reads_unverified_like_the_cli(self):
        mid = self.crash_at("send_after_history")
        real_open = os.open
        target = str(self.lock_file(mid))

        def deny(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(os, "open", deny):
            web = self.web_send_view()
            cli = self.view_of()
        self.assertEqual(web["state"], "unverified")
        self.assertEqual(web, cli)
        self.assertNotIn("partial", web)

    def test_an_outcome_appended_between_the_read_and_the_probe_reads_complete(self):
        mid = self.crash_at("send_before_outcome")
        real_probe = sp_channels._probe_send_lock

        def append_then_free(message_id):
            outcome = make_entry(
                "ops", 3, sp_channels.EVENT_OUTCOME,
                {"message_id": message_id, "outcomes": {"codex:" + self.tid: "queued (thread paused)"}})
            with open(str(log_path("ops")), "ab") as fh:
                fh.write(encode(outcome).encode("utf-8"))
            return "free"

        with ReadMeter() as meter:
            with mock.patch.object(sp_channels, "_probe_send_lock", append_then_free):
                view = self.web_send_view()
        self.assertEqual(view["state"], "complete")
        self.assertEqual(view["recipients"][0]["state"], "queued (thread paused)")
        self.assertEqual(meter.overlaps(str(log_path("ops"))), [])
        self.assertTrue(real_probe(mid) in ("free", "absent"))

    def test_an_outcome_after_a_rewritten_log_is_found_by_the_backward_recheck(self):
        mid = self.crash_at("send_before_outcome")
        path = log_path("ops")
        outcome = make_entry(
            "ops", 99, sp_channels.EVENT_OUTCOME, {"message_id": mid, "outcomes": {"codex:" + self.tid: "transport accepted"}})

        def replace_then_free(message_id):
            old = path.read_bytes()
            tmp = path.with_name(path.name + ".new")
            tmp.write_bytes(old + encode(outcome).encode("utf-8"))
            os.replace(str(tmp), str(path))  # a new inode, as a prune rewrite makes
            return "free"

        with mock.patch.object(sp_channels, "_probe_send_lock", replace_then_free):
            view = self.web_send_view()
        self.assertEqual((view["state"], view["recipients"][0]["state"]), ("complete", "transport accepted"))

    def test_a_recheck_over_the_budget_reads_unverified_and_partial(self):
        self.crash_at("send_before_outcome")

        def grow_then_free(message_id):
            with open(str(log_path("ops")), "ab") as fh:
                fh.write(encode(make_entry("ops", 50, "channel.note", text="n" * 20000)).encode("utf-8"))
            return "free"

        # The page read leaves too little budget to read what was appended.
        size = log_path("ops").stat().st_size
        budget = webdata.Budget(size + 3000)
        with mock.patch.object(sp_channels, "_probe_send_lock", grow_then_free):
            page = webdata.channel_page("ops", budget=budget)
        send = [e for e in page["entries"] if e["kind"] == "send"][0]
        self.assertEqual(send["delivery"]["state"], "unverified")
        self.assertTrue(send["delivery"]["partial"])
        self.assertTrue(page["partial"])


class WebdataBoundedReadTests(WebdataBase):
    def test_a_send_outside_what_the_snapshot_read_is_unverified_and_partial(self):
        key = self.new_channel()
        mid = new_uuid()
        lines = [encode(make_entry(key, 1, "channel.send", send_data("old", mid, delivery="fanout", recipients=["cc:z"])))]
        lines += [encode(make_entry(key, n, "channel.note", text="f" * 4096)) for n in range(2, 402)]
        write_topic(key, lines)
        old = json.loads(lines[0])
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read(limit=webdata.PAGE_MAX)
        self.assertIsNotNone(snap.next_before)
        self.assertNotIn(1, [i["seq"] for i in snap.items])
        with mock.patch.object(sp_channels, "_probe_send_lock", side_effect=AssertionError("probed")):
            view = webdata.resolve_send(snap, old, None)
        self.assertEqual(view["state"], "unverified")
        self.assertTrue(view["partial"])
        self.assertEqual(view["recipients"][0]["state"], "unverified")

    def test_an_outcome_on_a_newer_page_resolves_an_older_send(self):
        key = self.new_channel()
        mid = new_uuid()
        lines = [encode(make_entry(key, 1, "channel.send", send_data("old", mid, delivery="fanout", recipients=["cc:z"])))]
        lines += [encode(make_entry(key, n, "channel.note", text="n")) for n in range(2, 6)]
        lines.append(encode(make_entry(key, 6, "channel.outcome", {"message_id": mid, "outcomes": {"cc:z": "transport accepted"}})))
        write_topic(key, lines)
        page = webdata.channel_page(key, before=3)
        send = [e for e in page["entries"] if e["kind"] == "send"][0]
        self.assertEqual(send["delivery"]["state"], "complete")
        self.assertEqual(send["delivery"]["recipients"][0]["state"], "transport accepted")
        self.assertFalse(page["partial"])

    def test_an_oversized_line_after_a_send_may_hide_its_outcome(self):
        key = self.new_channel()
        mid = new_uuid()
        lines = [
            encode(make_entry(key, 1, "channel.send", send_data("s", mid, delivery="fanout", recipients=["cc:z"]))),
            encode(make_entry(key, 2, "channel.note", text="x" * (webdata.ENTRY_LINE_MAX + 10))),
        ]
        write_topic(key, lines)
        with mock.patch.object(webdata, "TOPIC_BUDGET", webdata.REQUEST_BUDGET):
            page = webdata.channel_page(key)
        self.assertEqual(page["oversized"], 1)
        self.assertEqual(page["entries"][1], {"seq": 2, "kind": "oversized", "oversized": True})
        self.assertEqual(page["entries"][0]["delivery"]["state"], "unverified")
        self.assertTrue(page["entries"][0]["delivery"]["partial"])
        self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)

    def padded_oversized_topic(self, key, big_entry):
        pad = [encode(make_entry(key, n, "channel.note", text="p" * 30000)) for n in range(1, 7)]
        write_topic(key, pad + [encode(big_entry)] + [
            encode(make_entry(key, n, "channel.note", text="after %d" % n)) for n in (8, 9)])

    def test_an_oversized_line_becomes_a_placeholder_and_the_read_goes_on(self):
        key = self.new_channel()
        self.padded_oversized_topic(
            key, make_entry(key, 7, "channel.note", text="x" * (webdata.ENTRY_LINE_MAX + 10)))
        # Behind a cursor the first entry rule reads the oversized line whole and keeps only its head.
        page = webdata.channel_page(key, before=8)
        seqs = [e["seq"] for e in page["entries"]]
        self.assertEqual(seqs[-1], 7)
        self.assertEqual(page["entries"][-1], {"seq": 7, "kind": "oversized", "oversized": True})
        self.assertEqual(page["oversized"], 1)
        self.assertTrue(page["partial"])
        self.assertEqual(page["next"], seqs[0])
        self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)
        page = webdata.channel_page(key, before=7)
        self.assertTrue(all(e["kind"] == "other" for e in page["entries"]))
        self.assertEqual(page["entries"][-1]["seq"], 6)

    def test_an_oversized_data_line_has_no_seq_and_never_stalls_the_read(self):
        key = self.new_channel()
        self.padded_oversized_topic(
            key, make_entry(key, 7, "channel.send", send_data("y" * (webdata.ENTRY_LINE_MAX + 10))))
        page = webdata.channel_page(key, before=8)
        self.assertEqual(page["oversized"], 1)
        self.assertTrue(page["partial"])
        self.assertTrue(all(e["kind"] == "other" for e in page["entries"]))
        self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)
        with mock.patch.object(webdata, "TOPIC_BUDGET", webdata.REQUEST_BUDGET):
            page = webdata.channel_page(key)
        self.assertEqual(
            [(e["seq"], e["kind"]) for e in page["entries"]],
            [(n, "other") for n in range(1, 7)] + [(None, "oversized"), (8, "other"), (9, "other")])
        self.assertFalse(page["partial"])
        self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)

    def test_a_topic_read_stays_within_one_mib_and_each_byte_is_read_once(self):
        keys = []
        for i in range(10):
            key = self.new_channel("big%d" % i)
            write_topic(key, [encode(make_entry(key, n, "channel.note", text="t" * 8000)) for n in range(1, 191)])
            keys.append(key)
        budget = webdata.Budget()
        with ReadMeter() as meter:
            pages = [webdata.channel_page(key, budget=budget) for key in keys]
        for name, count in meter.topic_bytes().items():
            self.assertLessEqual(count, webdata.TOPIC_BUDGET, name)
            self.assertEqual(meter.overlaps(name), [], name)
        self.assertLessEqual(meter.total(), webdata.REQUEST_BUDGET)
        self.assertEqual(budget.used, meter.total())
        self.assertLessEqual(max(p["bytes"] for p in pages), webdata.REQUEST_BUDGET)
        self.assertTrue(all(p["partial"] for p in pages))
        self.assertTrue(pages[0]["entries"])
        self.assertIsNotNone(pages[0]["next"])
        # The budget ran out before the last topics: nothing is read for them, and it says so.
        self.assertEqual(pages[-1]["entries"], [])
        self.assertTrue(pages[-1]["partial"])

    def test_paging_back_reaches_every_entry_once(self):
        key = self.new_channel()
        write_topic(key, [encode(make_entry(key, n, "channel.note", text="t" * 5000)) for n in range(1, 301)])
        seen, before, pages = [], None, 0
        while True:
            page = webdata.channel_page(key, before=before, limit=50)
            pages += 1
            seen = [e["seq"] for e in page["entries"]] + seen
            self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)
            if page["next"] is None:
                break
            before = page["next"]
            self.assertLess(pages, 40)
        # 300 x ~5 KB is 1.5 MB: the later pages cannot reach back past 1 MiB, and say so instead of looping.
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(seen[-1], 300)
        self.assertTrue(page["partial"])
        self.assertIsNotNone(page["gap"])
        self.assertEqual(page["gap"]["reason"], "budget")

    def test_near_cap_bodies_read_back_alone_and_behind_newer_entries(self):
        top = sp_constants.MAX_TEXT_CHARS
        bodies = {
            "ascii": "a" * (top - 1),
            "multibyte": "\U0001F600" * (top - 1),
            "control": "\x01" * top,
        }
        for name, body in bodies.items():
            for behind in (False, True):
                with self.subTest(body=name, behind=behind):
                    key = self.fresh_channel("cap")
                    lines = [encode(make_entry(key, 1, "channel.created", {})),
                             encode(make_entry(key, 2, "channel.send", send_data(body)))]
                    if behind:
                        lines += [encode(make_entry(key, n, "channel.note", text="small %d" % n)) for n in range(3, 33)]
                    write_topic(key, lines)
                    size = log_path(key).stat().st_size
                    self.assertLessEqual(size, webdata.ENTRY_LINE_MAX + 20000)
                    shown = body if name != "control" else webdata.escape_body(body)
                    with ReadMeter() as meter:
                        page = webdata.channel_page(key)
                        found = [e for e in page["entries"] if e["kind"] == "send"]
                        if behind:
                            self.assertEqual(len(page["entries"]), 30)
                            self.assertTrue(page["partial"])
                            self.assertEqual(page["next"], 3)
                            self.assertEqual(found, [])
                            page = webdata.channel_page(key, before=page["next"])
                            found = [e for e in page["entries"] if e["kind"] == "send"]
                    self.assertLessEqual(meter.total(), webdata.REQUEST_BUDGET)
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]["body"], shown)
                    self.assertLessEqual(page["bytes"], webdata.REQUEST_BUDGET)
                    snap = webdata.HistorySnapshot(key, webdata.Budget()).read(before=3 if behind else None)
                    self.assertEqual(snap.page[-1]["data"]["body"], body)

    def test_a_line_too_big_for_the_remaining_budget_stops_partial(self):
        key = self.new_channel()
        write_topic(key, [encode(make_entry(key, 1, "channel.send", send_data("\x01" * sp_constants.MAX_TEXT_CHARS)))])
        budget = webdata.Budget()
        budget.charge(webdata.REQUEST_BUDGET - 2 * MIB)
        with ReadMeter() as meter:
            page = webdata.channel_page(key, budget=budget)
        self.assertEqual(page["entries"], [])
        self.assertTrue(page["partial"])
        self.assertLessEqual(meter.total(), 2 * MIB)

    def test_retention_matches_the_cli_for_byte_entry_and_ttl_limits(self):
        cases = {
            "SESSION_PEERS_TOPIC_MAX_BYTES": "9000",
            "SESSION_PEERS_TOPIC_MAX_ENTRIES": "7",
            "SESSION_PEERS_TOPIC_TTL_DAYS": "1",
        }
        for var, value in cases.items():
            with self.subTest(limit=var):
                key = self.fresh_channel("ret")
                old = "2001-01-01T00:00:00.000Z"
                lines = [encode(make_entry(key, n, "channel.note", text="e" * 300, ts=old if n % 9 < 4 and n < 20 else None))
                         for n in range(1, 41)]
                write_topic(key, lines)
                self.assertGreater(log_path(key).stat().st_size, 9000)
                os.environ[var] = value
                try:
                    page = webdata.channel_page(key)
                    unchanged = log_path(key).read_bytes()
                    self.assertEqual(unchanged, "".join(lines).encode("utf-8"))
                    cli = self.tail_json(key, "--limit", "1000")
                finally:
                    del os.environ[var]
                self.assertEqual([e["seq"] for e in page["entries"]], [e["seq"] for e in cli["entries"]])
                self.assertLess(len(page["entries"]), 40)
                self.assertFalse(page["partial"])

    def test_since_mode_matches_topic_tail_including_the_pruned_gap(self):
        key = self.new_channel()
        write_topic(key, [encode(make_entry(key, n, "channel.note", text="n%d" % n)) for n in range(1, 51)])
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read(since=40)
        self.assertEqual([e["seq"] for e in snap.page], list(range(41, 51)))
        self.assertIsNone(snap.gap)
        self.assertEqual(snap.next_seq, 51)
        os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"] = "5"
        try:
            snap = webdata.HistorySnapshot(key, webdata.Budget()).read(since=2)
            tail = sp_channels.read_history(key, since=2, limit=1000)
        finally:
            del os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"]
        self.assertEqual([e["seq"] for e in snap.page], [e["seq"] for e in tail["entries"]])
        self.assertEqual(snap.gap, {"from": tail["gap"]["from"], "to": tail["gap"]["to"], "reason": "pruned"})

    def test_since_mode_past_the_reach_of_the_budget_reports_a_budget_gap(self):
        key = self.new_channel()
        write_topic(key, [encode(make_entry(key, n, "channel.note", text="t" * 5000)) for n in range(1, 301)])
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read(since=1)
        self.assertTrue(snap.partial)
        self.assertEqual(snap.gap["reason"], "budget")
        self.assertEqual(snap.gap["from"], 2)
        self.assertEqual(snap.page[-1]["seq"], 300)
        self.assertLessEqual(snap.topic_bytes, webdata.TOPIC_BUDGET)

    def test_torn_foreign_and_invalid_lines_are_skipped_and_next_seq_follows_meta(self):
        key = self.new_channel()
        lines = [
            encode(make_entry(key, 1, "channel.note", text="one")),
            "not json\n", "[1, 2]\n", '{"seq": "7"}\n', "\n",
            encode(make_entry(key, 2, "channel.note", text="two")),
            b'{"seq": 3, "kind": "channel.note", "text": "torn',
        ]
        write_topic(key, lines, next_seq=40)
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read()
        self.assertEqual([i["seq"] for i in snap.items], [1, 2])
        self.assertEqual(snap.next_seq, 40)
        self.assertFalse(snap.partial)
        write_topic(key, lines, next_seq=1)
        self.assertEqual(webdata.HistorySnapshot(key, webdata.Budget()).read().next_seq, 3)
        # A torn final line that happens to parse is still ignored.
        write_topic(key, lines[:2] + [encode(make_entry(key, 9, "channel.note", text="x")).rstrip("\n")], meta=False)
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read()
        self.assertEqual([i["seq"] for i in snap.items], [1])

    def test_missing_unsafe_and_oversized_meta_files(self):
        key = self.new_channel()
        log_path(key).unlink()
        meta_path(key).unlink()
        page = webdata.channel_page(key)
        self.assertEqual((page["entries"], page["partial"], page["next"]), ([], False, None))
        write_topic(key, [encode(make_entry(key, 1, "channel.note", text="one"))], meta=False)
        target = pathlib.Path(self.root) / "elsewhere.json"
        target.write_text(json.dumps({"next_seq": 77}))
        meta_path(key).symlink_to(target)
        self.assertEqual(webdata.HistorySnapshot(key, webdata.Budget()).read().next_seq, 2)
        meta_path(key).unlink()
        meta_path(key).write_text(json.dumps({"next_seq": 55, "pad": "x" * webdata.META_MAX}))
        self.assertEqual(webdata.HistorySnapshot(key, webdata.Budget()).read().next_seq, 2)

    def test_a_symlinked_or_fifo_log_is_never_followed_or_blocked_on(self):
        key = self.new_channel()
        target = pathlib.Path(self.root) / "other.jsonl"
        target.write_text(encode(make_entry(key, 1, "channel.note", text="planted")))
        log_path(key).unlink()
        log_path(key).symlink_to(target)
        page = webdata.channel_page(key)
        self.assertEqual(page["entries"], [])
        self.assertTrue(page["partial"])
        log_path(key).unlink()
        os.mkfifo(str(log_path(key)))
        page = webdata.channel_page(key)
        self.assertEqual(page["entries"], [])
        self.assertTrue(page["partial"])

    def test_the_snapshot_records_a_fingerprint_of_the_file_it_read(self):
        key = self.new_channel()
        snap = webdata.HistorySnapshot(key, webdata.Budget()).read()
        st = log_path(key).stat()
        self.assertEqual(snap.fingerprint, (st.st_size, st.st_ino, st.st_mtime_ns))

    def test_key_and_argument_validation(self):
        self.new_channel("ok")
        for bad, status in (("Bad", 400), ("../x", 400), ("", 400), ("x" * 65, 400), ("nope", 404), (None, 400)):
            with self.assertRaises(webdata.WebDataError, msg=repr(bad)) as ctx:
                webdata.channel_page(bad)
            self.assertEqual(ctx.exception.status, status)
        for kw in ({"limit": 0}, {"limit": 201}, {"limit": "5"}, {"limit": True}, {"before": -1}, {"before": "x"}):
            with self.assertRaises(webdata.WebDataError, msg=repr(kw)):
                webdata.channel_page("ok", **kw)
        self.assertEqual(webdata.channel_page("general")["entries"], [])
        self.assertEqual(webdata.channel_page("ok", limit=200)["channel"], "ok")

    def test_general_posts_carry_their_coverage_and_gaps(self):
        write_topic("general", [encode(make_entry("general", 1, "channel.send", send_data(
            "hello", opencode_coverage={"enumerated": False, "error": "boom"},
            discovery_incomplete=["Claude registry: x\x1b[2J"])))])
        page = webdata.channel_page("general")
        send = page["entries"][0]
        self.assertEqual(send["coverage"]["opencode"], {"enumerated": False, "error": "boom"})
        self.assertEqual(send["coverage"]["discovery_incomplete"], ["Claude registry: x\\u001b[2J"])
        self.assertEqual(page["coverage"], {"native_claude": None})
        page = webdata.channel_page("general", members=["codex:a", "cc:b"])
        self.assertEqual(page["coverage"], {"native_claude": True})

    def test_identifiers_and_bodies_are_hygienic_in_the_page(self):
        key = self.new_channel()
        rec_path = sp_channels._record_path(key)
        rec = json.loads(pathlib.Path(rec_path).read_text())
        rec["display"] = "ops‮\x1b[31m"
        pathlib.Path(rec_path).write_text(json.dumps(rec))
        evil = "\x1b]0;x\x07‮\x9b  line\nsecond\tcol"
        write_topic(key, [encode(dict(
            make_entry(key, 1, "channel.send", send_data(evil, recipients=["cc:a‮b"], sender="cc:\x1bx"),
                       sid="s⁦x")))])
        page = webdata.channel_page(key)
        text = json.dumps(page, ensure_ascii=False)
        for ch in "\x1b\x07‮\x9b ⁦":
            self.assertNotIn(ch, text)
        self.assertEqual(page["info"]["display"], "ops\\u202e\\u001b[31m")
        send = page["entries"][0]
        self.assertEqual(send["body"], "\\u001b]0;x\\u0007\\u202e\\u009b\\u2028 line\nsecond\tcol")
        self.assertEqual(send["actor"], "cc:s\\u2066x")
        self.assertEqual(send["sender"], "cc:\\u001bx")


class WebdataMemberTests(WebdataBase):
    def shim_state(self, tid, **state):
        path = pathlib.Path(self.codex_dir) / "session-peers" / (tid + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        return path

    def test_runtime_and_state_rules(self):
        cc, codex, oc = "cc:" + new_uuid(), "codex:" + new_uuid(), "oc:ses_x"
        self.assertEqual(webdata.member_details(cc, "live", "working")["runtime"], "Claude Code")
        self.assertEqual(webdata.member_details(codex, "live", "idle")["runtime"], "Codex")
        self.assertEqual(webdata.member_details(oc, "live", None)["runtime"], "OpenCode")
        self.assertEqual(webdata.member_details("zz:1", "live", None)["runtime"], "unknown")
        self.assertEqual(webdata.member_details(cc, "live", "working")["state"], "working")
        self.assertEqual(webdata.member_details(cc, "live", {"state": "idle"})["state"], "idle")
        self.assertEqual(webdata.member_details(cc, "live", None)["state"], "unverified")
        self.assertEqual(webdata.member_details(cc, "unverified", "bogus")["state"], "unverified")
        self.assertEqual(webdata.member_details(cc, "gone", "working")["state"], "gone")
        self.assertNotIn("reply_budget", webdata.member_details(cc, "live", None))

    def test_the_address_is_hygienic(self):
        details = webdata.member_details("oc:a‮\x1bb", "live", None)
        self.assertEqual(details["address"], "oc:a\\u202e\\u001bb")
        self.assertEqual(details["member"], details["address"])

    def test_codex_reply_budget_from_the_shim_state_file(self):
        tid, sid = new_uuid(), new_uuid()
        self.shim_state(tid, budgets={sid: 2}, budget_sender_sid=sid, budget_last_at=sp_runtime.now_iso())
        budget = webdata.member_details("codex:" + tid, "live", "idle")["reply_budget"]
        self.assertEqual(
            (budget["requester"], budget["used"], budget["cap"], budget["remaining"], budget["expired"], budget["pending"]),
            (sid, 2, sp_constants.REPLY_BUDGET, sp_constants.REPLY_BUDGET - 2, False, False))
        self.assertEqual(budget["label"], "as last saved by the shim")

    def test_an_expired_sequence_counts_nothing_and_an_allowance_raises_the_cap(self):
        tid, sid = new_uuid(), new_uuid()
        self.shim_state(tid, budgets={sid: 3}, budget_sender_sid=sid, budget_last_at="2001-01-01T00:00:00Z")
        budget = webdata.member_details("codex:" + tid, "live", None)["reply_budget"]
        self.assertEqual((budget["used"], budget["expired"], budget["remaining"]), (0, True, sp_constants.REPLY_BUDGET))
        self.shim_state(
            tid, budgets={sid: 1}, budget_sender_sid=sid, budget_last_at=sp_runtime.now_iso(),
            allowance={"sid": sid, "total": 9, "at": sp_runtime.now_iso(), "bound": True})
        budget = webdata.member_details("codex:" + tid, "live", None)["reply_budget"]
        self.assertEqual((budget["used"], budget["cap"], budget["remaining"]), (1, 9, 8))

    def test_pending_marker_files_are_reported_by_path(self):
        tid, sid = new_uuid(), new_uuid()
        self.shim_state(tid, budgets={sid: 1}, budget_sender_sid=sid, budget_last_at=sp_runtime.now_iso())
        marker = pathlib.Path(self.codex_dir) / "session-peers" / (tid + ".budget-reset")
        marker.write_text("")
        budget = webdata.member_details("codex:" + tid, "live", None)["reply_budget"]
        self.assertTrue(budget["pending"])
        self.assertEqual(budget["pending_kinds"], ["reset"])
        self.assertTrue(marker.exists())

    def test_no_requester_missing_or_hostile_state(self):
        tid = new_uuid()
        self.assertIsNone(webdata.member_details("codex:" + tid, "live", None)["reply_budget"])
        self.shim_state(tid, budgets={})
        self.assertEqual(webdata.member_details("codex:" + tid, "live", None)["reply_budget"]["requester"], None)
        self.shim_state(tid, budgets="oops", budget_sender_sid="s", budget_last_at=5)
        self.assertIsNone(webdata.member_details("codex:" + tid, "live", None)["reply_budget"])
        self.assertIsNone(webdata.member_details("codex:../../etc/passwd", "live", None)["reply_budget"])
        target = pathlib.Path(self.root) / "elsewhere.json"
        target.write_text(json.dumps({"budget_sender_sid": "x"}))
        link = pathlib.Path(self.codex_dir) / "session-peers" / (tid + ".json")
        link.unlink()
        link.symlink_to(target)
        self.assertIsNone(webdata.member_details("codex:" + tid, "live", None)["reply_budget"])


class WebdataNoMutationTests(WebdataBase):
    def test_nothing_is_written_by_any_read(self):
        tid, _rollout = self.one_thread()
        self.channel("ops", "codex:" + tid, "oc:" + OC_B)
        self.send("--record")
        self.send(message="APPROVE")
        sp_channels.create_channel("empty", self.me, [])
        # A send whose outcome lies outside the first page of a big topic.
        key = self.new_channel("outside")
        mid = new_uuid()
        write_topic(key, [encode(make_entry(key, 1, "channel.send", send_data("old", mid, delivery="fanout", recipients=["cc:z"])))]
                    + [encode(make_entry(key, n, "channel.note", text="f" * 4096)) for n in range(2, 402)])
        crash_tid = new_uuid()
        shim = pathlib.Path(self.codex_dir) / "session-peers" / (crash_tid + ".json")
        shim.write_text(json.dumps({"budgets": {"s": 1}, "budget_sender_sid": "s", "budget_last_at": sp_runtime.now_iso()}))
        before = tree_state(*self.roots())
        self.assertTrue(any(row[0].endswith(".jsonl") for row in before))

        calls = [
            lambda: webdata.channel_page("ops"),
            lambda: webdata.channel_page("ops", limit=1),
            lambda: webdata.channel_page("empty"),
            lambda: webdata.channel_page("general"),
            lambda: webdata.channel_page("outside"),
            lambda: webdata.channel_page("outside", before=300, limit=5),
            lambda: webdata.HistorySnapshot("outside", webdata.Budget()).read(since=3),
            lambda: webdata.member_details("codex:" + crash_tid, "live", "idle"),
            lambda: webdata.member_details("cc:" + new_uuid(), "gone", None),
            lambda: webdata.read_journal_context(webdata.Budget(), ["x", mid]),
        ]
        for call in calls:
            call()
            self.assertEqual(tree_state(*self.roots()), before)
        snap = webdata.HistorySnapshot("outside", webdata.Budget()).read()
        old = json.loads(log_path("outside").read_text().splitlines()[0])
        self.assertEqual(webdata.resolve_send(snap, old, None)["state"], "unverified")
        self.assertEqual(tree_state(*self.roots()), before)

    def test_the_web_read_leaves_a_prunable_log_unrewritten(self):
        key = self.new_channel("prune")
        write_topic(key, [encode(make_entry(key, n, "channel.note", text="e" * 300)) for n in range(1, 41)])
        os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"] = "3"
        try:
            before = tree_state(*self.roots())
            webdata.channel_page(key)
            self.assertEqual(tree_state(*self.roots()), before)
        finally:
            del os.environ["SESSION_PEERS_TOPIC_MAX_ENTRIES"]

    def test_no_forbidden_reader_is_called(self):
        self.new_channel("plain")
        write_topic("plain", [encode(make_entry("plain", 1, "channel.note", text="one"))])
        with mock.patch.object(sp_channels, "read_history", side_effect=AssertionError("read_history")), \
                mock.patch.object(sp_topics, "topic_tail", side_effect=AssertionError("topic_tail")), \
                mock.patch.object(sp_topics, "_topic_paths", side_effect=AssertionError("_topic_paths")), \
                mock.patch.object(sp_topics, "_topic_lock", side_effect=AssertionError("_topic_lock")), \
                mock.patch.object(sp_journal, "read_only_view", side_effect=AssertionError("read_only_view")):
            self.assertEqual(len(webdata.channel_page("plain")["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
