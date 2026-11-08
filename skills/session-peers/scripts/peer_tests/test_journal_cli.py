"""Journal command line: send --record, correlated requests, exit codes, ACK and inbox commands."""

from __future__ import annotations

import errno
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib
from unittest import mock

from peer_tests.support import (
    HERE,
    Base,
    sp_constants,
    sp_storage,
    wait_for,
)
from peer_tests.test_opencode import OC_A, OC_B, OpenCodeBase
from session_peers import identity as sp_identity, journal
from session_peers import runtime as sp_runtime
from session_peers import reply_wake as sp_reply_wake

CAP_ENV = "SESSION_PEERS_JOURNAL_MAX_BYTES"


def new_id():
    return str(uuidlib.uuid4())


class JournalHelpers:
    """A private writable HOME for the journal, and readers of its state.

    Base keeps its sockets and registry under /tmp; only HOME moves, because the
    journal lives at ~/.codex/session-peers/journal whatever CODEX_HOME says.
    """

    def setUp(self):
        super().setUp()
        self.jhome = tempfile.mkdtemp(prefix="spjh")
        os.environ["HOME"] = self.jhome
        os.environ.pop(CAP_ENV, None)
        self.addCleanup(shutil.rmtree, self.jhome, True)

    # -- identities ---------------------------------------------------------

    def as_cc(self, rec_or_sid):
        sid = rec_or_sid["sessionId"] if isinstance(rec_or_sid, dict) else rec_or_sid
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(name, None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        if isinstance(rec_or_sid, dict):
            os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = rec_or_sid["messagingSocketPath"]
        else:
            os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)

    def as_codex(self, tid):
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "OPENCODE_SESSION_ID"):
            os.environ.pop(name, None)
        os.environ["CODEX_THREAD_ID"] = tid

    def peer(self, name="cc-target", sid=None, pid=None):
        """A live Claude peer with a UUID session id (the journal keys by typed ids)."""
        sid = sid or new_id()
        listener, rec = self.add_listener(name=name, session_id=sid, pid=pid)
        return listener, rec

    # -- journal state ------------------------------------------------------

    def jpath(self):
        return os.path.join(journal.journal_dir(), "journal.jsonl")

    def records(self):
        try:
            with open(self.jpath(), "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in data.splitlines() if line.strip()]

    def entries(self):
        return journal.read_only_view()["entries"]

    def entry(self, message_id):
        for item in self.entries():
            if item["message_id"] == message_id:
                return item
        return None

    def assert_no_acks(self, message_id):
        self.assertEqual([r for r in self.records() if r["type"] == "ack"], [])
        item = self.entry(message_id)
        self.assertIsNotNone(item)
        self.assertEqual([r["state"] for r in item["recipients"]], ["pending"] * len(item["to"]))

    def prefill_full(self):
        """Fill the journal with unprunable (unacknowledged, recent) entries, then cap it at its size."""
        os.environ[CAP_ENV] = "60000"
        body = "b" * 2000
        with mock.patch.object(sp_identity, "resolve_typed", lambda target, exclude=None: {"kind": target.partition(":")[0]}):
            while sum(len(json.dumps(r)) for r in self.records()) < 20000:
                journal.record_message("cc:" + new_id(), ["cc:" + new_id()], body)
        os.environ[CAP_ENV] = str(os.path.getsize(self.jpath()))

    # -- child processes ----------------------------------------------------

    def run_patched(self, patch_code, *argv, wait=True):
        """Run peers.main in a child after ``patch_code`` patched the child's own modules."""
        script = (
            "import json, os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from session_peers import journal as j, messaging as m\n"
            "%s\n"
            "from session_peers.cli import main\n"
            "sys.exit(main(json.loads(sys.argv[1])))\n" % (str(HERE), patch_code)
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script, json.dumps(list(argv))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ),
        )
        self._children.append(proc)
        if not wait:
            return proc
        out, err = proc.communicate(timeout=60)
        return proc.returncode, out, err

    # -- correlated requests ------------------------------------------------

    def dispatch(self, target, tid, message="please review", extra=()):
        rc, out, err = self.cli(
            "dispatch", "--to", target, "--from-thread", tid, "--message", message,
            "--timeout", "300", "--json", *extra,
        )
        return rc, (json.loads(out) if out.strip().startswith("{") else out), err

    def reply_as(self, sid, request_id, message, *extra):
        saved = {k: os.environ.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")}
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        os.environ.pop("CODEX_THREAD_ID", None)
        try:
            return self.cli("reply", "--request", request_id, "--message", message, *extra)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def mailbox(self, request_id):
        return sp_storage.request_path(request_id), sp_storage.request_reply_path(request_id)


class JournalCliBase(JournalHelpers, Base):
    pass


class JournalOpenCodeBase(JournalHelpers, OpenCodeBase):
    pass


# ==========================================================================
# send --record
# ==========================================================================


class JournalSendRecordTests(JournalCliBase):
    def test_codex_target_stores_the_queue_message_id(self):
        _listener, sender = self.peer("cc-sender", pid=os.getpid())
        self.as_cc(sender)
        tid, _rollout = self.one_thread()
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "hello", "--record", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["journal"], {"message_id": payload["message_id"], "recorded": True})
        queued = json.dumps(self.queue_calls())
        self.assertIn("mid=%s" % payload["message_id"], queued)
        entry = self.entry(payload["message_id"])
        self.assertEqual(entry["to"], ["codex:" + tid])
        self.assertEqual(entry["from"], "cc:" + sender["sessionId"])
        self.assertEqual(entry["body"], "hello")
        self.assertEqual(entry["kind"], "message")

    def test_claude_target_stores_the_frame_message_id(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--message", "ping", "--record", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        frames = wait_for(lambda: listener.of_type("user"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["msg_id"], payload["message_id"])
        self.assertEqual(payload["journal"]["message_id"], frames[0]["msg_id"])
        entry = self.entry(frames[0]["msg_id"])
        self.assertEqual(entry["to"], ["cc:" + rec["sessionId"]])
        self.assertEqual(entry["from"], "codex:" + tid)
        self.assertEqual(entry["body"], "ping")

    def test_from_is_the_env_caller_not_from_thread_codex(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        other = new_id()
        self.as_codex(other)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--from-thread", tid, "--message", "ping",
                                "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(json.loads(out)["message_id"])["from"], "codex:" + other)

    def test_from_is_the_env_caller_not_from_thread_claude(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        _l2, sender = self.peer("cc-sender", pid=os.getpid())
        tid, _rollout = self.one_thread()
        self.as_cc(sender)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--from-thread", tid, "--message", "ping",
                                "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(json.loads(out)["message_id"])["from"], "cc:" + sender["sessionId"])

    def _codex_shim(self, tid, name="codex-shim"):
        """A live Claude-facing registry record of a Codex shim for thread ``tid``."""
        listener, rec = self.peer(name, sid=tid, pid=os.getppid())
        rec = self.write_record(os.getppid(), name, tid, rec["messagingSocketPath"], entrypoint="codex")
        return listener, rec

    def test_a_codex_sender_to_a_codex_shim_record_stores_the_codex_slot(self):
        tid, _rollout = self.one_thread()
        listener, _rec = self._codex_shim(tid)
        sender = new_id()
        self.as_codex(sender)
        rc, out, err = self.cli("send", "--to", "cc:codex-shim", "--message", "to a shim", "--record", "--json")
        self.assertEqual(rc, 0, err)
        mid = json.loads(out)["message_id"]
        self.assertEqual(wait_for(lambda: listener.of_type("user"))[0]["msg_id"], mid)
        self.assertEqual(self.entry(mid)["to"], ["codex:" + tid])
        self.as_codex(tid)
        rc, out, err = self.cli("inbox", "get", mid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["body"], "to a shim")

    def test_the_journal_refuses_a_slot_whose_kind_differs_from_the_resolved_kind(self):
        tid, _rollout = self.one_thread()
        self._codex_shim(tid)
        with self.assertRaises(journal.JournalRefused) as raised:
            journal.record_message("codex:" + new_id(), ["cc:" + tid], "wrong kind")
        self.assertIn("codex:" + tid, str(raised.exception))
        self.assertEqual(self.records(), [])
        journal.record_message("codex:" + new_id(), ["codex:" + tid], "right kind")

    def test_send_record_without_a_detectable_caller_notes_it_and_records_no_sender(self):
        tid, _rollout = self.one_thread()
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "anon", "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn("note: sender identity not detected; recorded with no sender\n", err)
        self.assertNotIn("note: sender identity", out)
        self.assertEqual(json.loads(out)["journal"]["recorded"], True)
        self.assertIsNone(self.entry(json.loads(out)["message_id"])["from"])
        self.as_codex(new_id())
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "known", "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("note: sender identity", err)

    def test_without_record_nothing_is_written_and_no_journal_key_appears(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--message", "ping", "--json")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("journal", json.loads(out))
        self.assertEqual(self.records(), [])
        self.assertFalse(os.path.exists(journal.journal_dir()))

    def test_codex_sender_to_codex_target_records_the_queue_id(self):
        tid, _rollout = self.one_thread()
        other = new_id()
        self.as_codex(other)
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "via codex", "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(json.loads(out)["message_id"])["to"], ["codex:" + tid])
        self.assertEqual(self.entry(json.loads(out)["message_id"])["from"], "codex:" + other)


class JournalOpenCodeSendTests(JournalOpenCodeBase):
    def test_opencode_target_stores_the_queue_message_id(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "oc hello", "--record", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        prompts = self.oc_calls("session.prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["data"]["metadata"]["session_peers"]["message_id"], payload["message_id"])
        self.assertEqual(payload["journal"], {"message_id": payload["message_id"], "recorded": True})
        entry = self.entry(payload["message_id"])
        self.assertEqual(entry["to"], ["oc:" + OC_B])
        self.assertEqual(entry["from"], "cc:" + rec["sessionId"])

    def test_opencode_sender_with_reply_wake_to_claude_uses_one_id_everywhere(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        self.as_opencode(OC_A)
        rc, out, err = self.cli(
            "send", "--to", "cc:cc-target", "--message", "wake me", "--allow-reply-wake", "--record", "--json",
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        frames = wait_for(lambda: listener.of_type("user"))
        self.assertEqual(frames[0]["msg_id"], payload["message_id"])
        self.assertEqual(payload["reply_wake"]["message_id"], payload["message_id"])
        self.assertEqual(payload["journal"]["message_id"], payload["message_id"])
        entry = self.entry(payload["message_id"])
        self.assertEqual(entry["from"], "oc:" + OC_A)
        self.assertEqual(entry["to"], ["cc:" + rec["sessionId"]])

    def test_opencode_sender_to_codex_records_the_queue_id(self):
        tid, _rollout = self.one_thread()
        self.as_opencode(OC_A)
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "x", "--allow-reply-wake",
                                "--record", "--json")
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertIn("mid=%s" % payload["message_id"], json.dumps(self.queue_calls()))
        self.assertEqual(self.entry(payload["message_id"])["from"], "oc:" + OC_A)

    def test_journal_full_refuses_an_opencode_send_before_queueing(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        self.prefill_full()
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "x", "--record", "--json")
        self.assertEqual(rc, 75)
        self.assertEqual(json.loads(out)["status"], "journal_full")
        self.assertIn("error: ", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def _reply_wake_grant(self, sender_sid):
        mid = new_id()
        sp_reply_wake.issue(mid, "oc:" + OC_B, "cc:" + sender_sid, 600)
        return mid, pathlib.Path(sp_reply_wake.path_for(mid))

    def test_a_journal_refusal_keeps_the_reply_wake_grant(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        grant_id, grant = self._reply_wake_grant(rec["sessionId"])
        self.prefill_full()
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "reply", "--in-reply-to", grant_id,
                                "--record", "--json")
        self.assertEqual(rc, 75, err)
        self.assertEqual(json.loads(out)["status"], "journal_full")
        self.assertTrue(grant.exists())
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_a_recorded_reply_wake_send_spends_the_grant_and_delivers(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        grant_id, grant = self._reply_wake_grant(rec["sessionId"])
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "reply", "--in-reply-to", grant_id,
                                "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertFalse(grant.exists())
        self.assertEqual(len(self.oc_calls("session.prompt")), 1)
        self.assertEqual(self.entry(json.loads(out)["message_id"])["to"], ["oc:" + OC_B])

    def test_a_grant_refused_after_the_record_marks_a_transport_error_and_sends_nothing(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        grant_id, _grant = self._reply_wake_grant(rec["sessionId"])
        with mock.patch.object(sp_reply_wake, "consume", side_effect=ValueError("raced away")):
            rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "reply",
                                     "--in-reply-to", grant_id, "--record")
        self.assertEqual(rc, 1)
        self.assertIn("raced away; nothing queued", err)
        self.assertEqual(self.oc_calls("session.prompt"), [])
        [entry] = self.entries()
        [recipient] = entry["recipients"]
        self.assertEqual(recipient["slot"], "oc:" + OC_B)
        self.assertEqual(recipient["transport_error"]["detail"], "reply-wake grant refused")

    def test_an_opencode_transport_failure_appends_a_per_slot_transport_error(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        self.set_oc(prompt_fail=True)
        rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "x", "--record")
        self.assertEqual(rc, 1)
        self.assertIn("could not queue to OpenCode session", err)
        [entry] = self.entries()
        self.assertIn("transport_error", entry["recipients"][0])
        self.assertEqual(entry["recipients"][0]["slot"], "oc:" + OC_B)

    def test_a_journal_refusal_on_the_claude_path_forgets_the_issued_reply_wake_grant(self):
        self.peer("cc-target", pid=os.getppid())
        self.as_opencode(OC_A)
        self.prefill_full()
        rc, _out, err = self.cli("send", "--to", "cc:cc-target", "--message", "x", "--allow-reply-wake", "--record")
        self.assertEqual(rc, 75, err)
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.reply-wake.json")), [])

    def test_opencode_inbox_handled_and_status_never_acknowledge(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        rc, out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", "x", "--record", "--json")
        self.assertEqual(rc, 0, err)
        mid = json.loads(out)["message_id"]
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET"):
            os.environ.pop(name, None)
        self.as_opencode(OC_B)
        for name in ("handled", "status"):
            rc, _out, err = self.cli("inbox", name, "cc:" + rec["sessionId"], mid, "--json")
            self.assertEqual(rc, 0, err)
        self.assert_no_acks(mid)
        rc, out, err = self.cli("inbox", "acks", mid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["state"] for r in json.loads(out)["recipients"]], ["pending"])


# ==========================================================================
# Pre-send failures
# ==========================================================================


class JournalPreSendFailureTests(JournalCliBase):
    def test_journal_full_refuses_every_sending_command_with_75_and_sends_nothing(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        # A live request whose reply the journal must refuse for lack of room.
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        wait_for(lambda: listener.of_type("user"))
        frames_before = len(listener.of_type("user"))
        self.prefill_full()
        before = self.records()

        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--message", "m", "--record", "--json")
        self.assertEqual(rc, 75)
        self.assertEqual(json.loads(out)["status"], "journal_full")
        self.assertIn("error: ", err)
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "m", "--record")
        self.assertEqual(rc, 75, err)
        rc, out, err = self.cli("dispatch", "--to", "cc:cc-target", "--from-thread", tid, "--message", "m")
        self.assertEqual(rc, 75, err)
        rc, out, err = self.cli("ask", "--to", "cc:cc-target", "--from-thread", tid, "--message", "m",
                                "--timeout", "5", "--json")
        self.assertEqual(rc, 75, err)
        self.assertEqual(json.loads(out)["status"], "journal_full")
        rc, _out, err = self.reply_as(rec["sessionId"], request["request_id"], "an answer")
        self.assertEqual(rc, 75, err)

        time.sleep(0.2)
        self.assertEqual(len(listener.of_type("user")), frames_before)
        self.assertEqual(self.queue_calls(), [])
        mailboxes = sorted(p.name for p in pathlib.Path(sp_storage.request_dir()).glob("*.json"))
        self.assertEqual(mailboxes, ["%s.request.json" % request["request_id"]])
        self.assertFalse(os.path.exists(self.mailbox(request["request_id"])[1]))
        self.assertEqual(self.records(), before)

    def test_unwritable_journal_directory_refuses_with_1_and_sends_nothing(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        # ~/.codex is a file, so the journal directory cannot be created.
        pathlib.Path(self.jhome, ".codex").write_text("not a directory")
        for argv in (
            ("send", "--to", "cc:cc-target", "--message", "m", "--record"),
            ("send", "--to", "codex:%s" % tid, "--message", "m", "--record"),
            ("dispatch", "--to", "cc:cc-target", "--from-thread", tid, "--message", "m"),
            ("ask", "--to", "cc:cc-target", "--from-thread", tid, "--message", "m", "--timeout", "5"),
        ):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 1, (argv, err))
            self.assertIn("error: ", err)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(self.queue_calls(), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])

    def request_argv(self, verb, tid):
        argv = [verb, "--to", "cc:cc-target", "--from-thread", tid, "--message", "m"]
        return argv + (["--timeout", "5"] if verb == "ask" else [])

    def assert_nothing_left_or_sent(self, listener):
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(self.queue_calls(), [])
        self.assertEqual(self.records(), [])
        left = sorted(p.name for p in pathlib.Path(sp_storage.request_dir()).glob("*"))
        self.assertEqual(left, [])

    def test_a_mailbox_write_failure_refuses_with_1_and_leaves_nothing(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        real = sp_runtime.write_json_atomic

        def failing(path, data, *a, **kw):
            if str(path).endswith(".request.json"):
                # A failure after the temp file exists, as a full disk would produce.
                pathlib.Path("%s.tmp.%d" % (path, os.getpid())).write_text("partial")
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data, *a, **kw)

        for verb in ("dispatch", "ask"):
            with mock.patch.object(sp_runtime, "write_json_atomic", failing):
                rc, _out, err = self.cli(*self.request_argv(verb, tid))
            self.assertEqual(rc, 1, (verb, err))
            self.assertIn("could not write request mailbox", err)
            self.assert_nothing_left_or_sent(listener)

    def test_a_journal_refusal_removes_the_mailbox_it_was_recorded_after(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        real = journal.record_request
        for verb, exc_type, want in (
            ("dispatch", journal.JournalUnavailable, 1),
            ("dispatch", journal.JournalFull, 75),
            ("ask", journal.JournalFull, 75),
        ):
            seen = []

            def refusing(request_id, requester, to, message, expires_at, mailbox, **kw):
                seen.append(os.path.exists(mailbox))
                raise exc_type("x")

            with mock.patch.object(journal, "record_request", refusing):
                rc, _out, err = self.cli(*self.request_argv(verb, tid))
            self.assertEqual(rc, want, (verb, exc_type, err))
            self.assertEqual(seen, [True], (verb, exc_type))
            self.assert_nothing_left_or_sent(listener)
        self.assertIs(journal.record_request, real)

    def test_an_interrupt_while_recording_removes_the_mailbox_and_exits_130(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        for verb in ("dispatch", "ask"):
            seen = []

            def interrupted(request_id, requester, to, message, expires_at, mailbox, **kw):
                seen.append(os.path.exists(mailbox))
                raise KeyboardInterrupt()

            # cli.main turns the re-raised KeyboardInterrupt into exit 130.
            with mock.patch.object(journal, "record_request", interrupted):
                rc, _out, err = self.cli(*self.request_argv(verb, tid))
            self.assertEqual(rc, 130, (verb, err))
            self.assertEqual(seen, [True], verb)
            self.assert_nothing_left_or_sent(listener)

    def test_a_near_cap_claude_message_is_refused_before_it_is_recorded(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        body = "x" * sp_constants.MAX_TEXT_CHARS
        rc, _out, err = self.cli("send", "--to", "cc:cc-target", "--message", body, "--record")
        self.assertEqual(rc, 1)
        self.assertIn("peer cap", err)
        self.assertEqual(self.records(), [])
        self.assertEqual(listener.of_type("user"), [])

    def test_a_non_uuid_correlated_identity_is_refused_before_sending(self):
        listener, _rec = self.peer("cc-main", sid="s1", pid=os.getppid())
        tid, _rollout = self.one_thread()
        for verb in ("dispatch", "ask"):
            rc, _out, err = self.cli(verb, "--to", "cc:cc-main", "--from-thread", tid,
                                     "--message", "m", "--timeout", "5")
            self.assertEqual(rc, 1, (verb, err))
            self.assertIn("cannot record this message", err)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])
        self.assertEqual(self.records(), [])

    def test_from_thread_naming_another_thread_than_the_codex_environment_is_refused(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        mine, _rollout = self.one_thread()
        victim = new_id()
        self.as_codex(mine)
        request = new_id()
        for argv in (
            ("ask", "--to", "cc:cc-target", "--from-thread", victim, "--message", "m", "--timeout", "5"),
            ("dispatch", "--to", "cc:cc-target", "--from-thread", victim, "--message", "m"),
            ("await", "--request", request, "--from-thread", victim, "--timeout", "1"),
        ):
            rc, _out, err = self.cli(*argv)
            self.assertEqual(rc, 2, (argv, err))
            self.assertIn("--from-thread names thread %s but this environment is Codex thread %s" % (victim, mine), err)
        time.sleep(0.2)
        self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])
        self.assertEqual(self.records(), [])

    def test_a_refused_await_leaves_a_live_mailbox_alone(self):
        self.peer("cc-target", pid=os.getppid())
        mine, _rollout = self.one_thread()
        self.as_codex(mine)
        rc, request, err = self.dispatch("cc:cc-target", mine)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("await", "--request", request["request_id"], "--from-thread", new_id(),
                                 "--timeout", "1")
        self.assertEqual(rc, 2, err)
        self.assertTrue(os.path.exists(self.mailbox(request["request_id"])[0]))

    def test_a_matching_from_thread_and_a_claude_environment_are_still_allowed(self):
        _listener, _rec = self.peer("cc-target", pid=os.getppid())
        _lo, other = self.peer("cc-other", pid=os.getpid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(request["request_id"])["from"], "codex:" + tid)
        # A Claude session has no CODEX_THREAD_ID to disagree with.
        self.as_cc(other)
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(request["request_id"])["from"], "codex:" + tid)

    def test_send_record_keeps_the_environment_caller_as_from_whatever_from_thread_says(self):
        self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        other = new_id()
        self.as_codex(other)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--from-thread", tid, "--message", "ping",
                                "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(json.loads(out)["message_id"])["from"], "codex:" + other)


# ==========================================================================
# Post-transport failures (exit 76)
# ==========================================================================


class JournalPostTransportTests(JournalCliBase):
    def test_a_transport_failure_appends_a_per_slot_transport_error(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        listener.close()  # the socket file stays, so the connect is refused
        time.sleep(0.6)  # the accept loop holds the socket until its timeout ends
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 1)
        self.assertEqual(request["status"], "delivery_failed")
        entry = self.entry(request["request_id"])
        self.assertEqual(entry["recipients"][0]["slot"], "cc:" + rec["sessionId"])
        self.assertIn("transport_error", entry["recipients"][0])
        self.assertIn("could not send request", err)

    def test_a_claude_path_transport_failure_with_record_appends_a_per_slot_transport_error(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        listener.close()
        time.sleep(0.6)
        rc, _out, err = self.cli("send", "--to", "cc:cc-target", "--message", "lost", "--record")
        self.assertEqual(rc, 1, err)
        [entry] = self.entries()
        self.assertEqual(entry["recipients"][0]["slot"], "cc:" + rec["sessionId"])
        self.assertIn("transport_error", entry["recipients"][0])
        self.assertEqual(entry["body"], "lost")

    def test_an_ask_transport_failure_appends_a_per_slot_transport_error(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        listener.close()
        time.sleep(0.6)
        rc, _out, err = self.cli("ask", "--to", "cc:cc-target", "--from-thread", tid, "--message", "q?",
                                 "--timeout", "5")
        self.assertEqual(rc, 1, err)
        [entry] = self.entries()
        self.assertEqual(entry["kind"], "request")
        self.assertEqual(entry["recipients"][0]["slot"], "cc:" + rec["sessionId"])
        self.assertIn("transport_error", entry["recipients"][0])

    def test_a_dispatch_json_carries_journal_error_when_the_transport_error_cannot_be_appended(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        listener.close()
        time.sleep(0.6)
        patch = (
            "def boom(*a, **k):\n    raise j.JournalUpdateFailed('injected transport_error failure')\n"
            "j.transport_error = boom\n"
        )
        rc, out, err = self.run_patched(patch, "dispatch", "--to", "cc:cc-target", "--from-thread", tid,
                                        "--message", "m", "--json")
        self.assertEqual(rc, 76, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "delivery_failed")
        self.assertEqual(payload["journal_error"], "injected transport_error failure")
        self.assertIn("journal update failed: injected transport_error failure", err)

    def test_an_unexpected_settlement_error_still_prints_the_body_and_exits_76(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "the answer")[0], 0)
        patch = "def boom(*a, **k):\n    raise KeyError('unexpected')\nj.settle = boom\n"
        self.as_codex(tid)
        rc, out, err = self.run_patched(patch, "await", "--request", rid, "--from-thread", tid, "--timeout", "5")
        self.assertEqual(rc, 76, err)
        self.assertEqual(out, "the answer\n")
        self.assertIn("journal update failed: ", err)
        self.assertIn("unexpected", err)

    def test_an_unexpected_transport_error_append_failure_exits_76(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        listener.close()
        time.sleep(0.6)
        patch = "def boom(*a, **k):\n    raise KeyError('odd')\nj.transport_error = boom\n"
        rc, _out, err = self.run_patched(patch, "dispatch", "--to", "cc:cc-target", "--from-thread", tid,
                                         "--message", "m")
        self.assertEqual(rc, 76, err)
        self.assertIn("journal update failed: ", err)

    def test_transport_error_append_failure_reports_the_original_error_and_exits_76(self):
        os.environ["FAKE_CODEX_RC"] = "1"
        os.environ["FAKE_CODEX_STDERR"] = "queue backend exploded"
        tid, _rollout = self.one_thread()
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_cc(rec)
        patch = (
            "def boom(*a, **k):\n    raise j.JournalUpdateFailed('injected transport_error failure')\n"
            "j.transport_error = boom\n"
        )
        rc, out, err = self.run_patched(patch, "send", "--to", "codex:%s" % tid, "--message", "m", "--record")
        self.assertEqual(rc, 76, err)
        self.assertIn("queue backend exploded", err)
        self.assertIn("journal update failed: injected transport_error failure", err)
        self.assertEqual(len(self.queue_calls()), 1)
        self.assertEqual(len([r for r in self.records() if r["type"] == "transport_error"]), 0)
        self.assertEqual(len([r for r in self.records() if r["type"] == "message"]), 1)

    def test_reply_settlement_failure_reports_replied_exits_76_and_keeps_the_file(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        rid = request["request_id"]
        os.environ["CLAUDE_CODE_SESSION_ID"] = rec["sessionId"]
        patch = (
            "real = j.settle\n"
            "def flaky(mid, state, by):\n"
            "    if state == 'replied':\n        raise j.JournalUpdateFailed('injected replied failure')\n"
            "    return real(mid, state, by)\n"
            "j.settle = flaky\n"
        )
        rc, out, err = self.run_patched(patch, "reply", "--request", rid, "--message", "the answer", "--json")
        self.assertEqual(rc, 76, err)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "replied")
        self.assertEqual(payload["journal_error"], "injected replied failure")
        self.assertIn("journal update failed: injected replied failure", err)
        self.assertTrue(os.path.exists(self.mailbox(rid)[1]))
        # The reply is intact: await still receives it, once.
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        rc, out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "5", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["message"], "the answer")
        rc, out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "1", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["status"], "expired")

    def test_consume_settlement_failure_still_prints_the_body_exits_76_and_never_reclaims(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        rid = request["request_id"]
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "the answer")
        self.assertEqual(rc, 0, err)
        patch = (
            "real = j.settle\n"
            "def flaky(mid, state, by):\n"
            "    if state == 'consumed':\n        raise j.JournalUpdateFailed('injected consumed failure')\n"
            "    return real(mid, state, by)\n"
            "j.settle = flaky\n"
        )
        self.as_codex(tid)
        rc, out, err = self.run_patched(patch, "await", "--request", rid, "--from-thread", tid,
                                        "--timeout", "5", "--json")
        self.assertEqual(rc, 76, err)
        payload = json.loads(out)
        self.assertEqual(payload["message"], "the answer")
        self.assertEqual(payload["journal_error"], "injected consumed failure")
        self.assertIn("journal update failed: injected consumed failure", err)
        meta, reply = self.mailbox(rid)
        self.assertFalse(os.path.exists(meta))
        self.assertFalse(os.path.exists(reply))
        # No second claim: the next await finds nothing to consume.
        rc, out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "1", "--json")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["status"], "expired")
        reply_entry = self.entry(journal.reply_message_id(rid))
        self.assertIsNone(reply_entry["settlement"])

    def test_a_failed_consume_settlement_is_not_retried(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "once")[0], 0)
        patch = (
            "real = j.settle\n"
            "calls = []\n"
            "def flaky_once(mid, state, by):\n"
            "    if state == 'consumed' and not calls:\n"
            "        calls.append(1)\n"
            "        raise j.JournalUpdateFailed('first consumed failure')\n"
            "    return real(mid, state, by)\n"
            "j.settle = flaky_once\n"
        )
        self.as_codex(tid)
        rc, out, err = self.run_patched(patch, "await", "--request", rid, "--from-thread", tid,
                                        "--timeout", "5", "--json")
        self.assertEqual(rc, 76, err)
        self.assertIsNone(self.entry(journal.reply_message_id(rid))["settlement"])

    def test_human_output_of_a_failed_consume_settlement_still_prints_the_body(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        self.reply_as(rec["sessionId"], rid, "plain answer")
        patch = "def boom(*a, **k):\n    raise j.JournalUpdateFailed('nope')\nj.settle = boom\n"
        rc, out, err = self.run_patched(patch, "await", "--request", rid, "--from-thread", tid, "--timeout", "5")
        self.assertEqual(rc, 76)
        self.assertEqual(out, "plain answer\n")
        self.assertIn("journal update failed: nope", err)


# ==========================================================================
# Settlement
# ==========================================================================


class JournalSettlementTests(JournalCliBase):
    def test_replied_and_consumed_are_recorded_and_the_mailbox_goes_first(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        rid = request["request_id"]
        entry = self.entry(rid)
        self.assertEqual(entry["from"], "codex:" + tid)
        self.assertEqual(entry["to"], ["cc:" + rec["sessionId"]])
        self.assertEqual(entry["body"], "please review")
        rc, out, err = self.reply_as(rec["sessionId"], rid, "approved", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(rid)["settlement"]["state"], "replied")
        reply = self.entry(journal.reply_message_id(rid))
        self.assertEqual(reply["body"], "approved")
        self.assertEqual(reply["to"], ["codex:" + tid])
        self.assertIsNone(reply["settlement"])

        seen = {}
        real = journal.settle

        def watch(message_id, state, by):
            seen["state"] = state
            seen["mailbox"] = [os.path.exists(p) for p in self.mailbox(rid)]
            return real(message_id, state, by)

        with mock.patch.object(journal, "settle", watch):
            rc, out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "5", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["message"], "approved")
        self.assertEqual(seen, {"state": "consumed", "mailbox": [False, False]})
        self.assertEqual(self.entry(journal.reply_message_id(rid))["settlement"]["state"], "consumed")
        self.assertEqual(self.entry(journal.reply_message_id(rid))["settlement"]["by"], "codex:" + tid)

    def _ask_child(self, tid, patch):
        """An `ask` child with ``patch`` applied; returns (process, request id) once the request is out."""
        listener, rec = self.peer("cc-target", pid=os.getppid())
        proc = self.run_patched(patch, "ask", "--to", "cc:cc-target", "--from-thread", tid,
                                "--message", "q?", "--timeout", "30", "--json", wait=False)
        frame = wait_for(lambda: listener.of_type("user"))[0]
        rid = frame["message"]["content"].split('id="')[1].split('"')[0]
        return proc, rec, rid

    def test_ask_unlinks_the_mailbox_then_settles_consumed(self):
        tid, _rollout = self.one_thread()
        seen = os.path.join(self.jhome, "seen-%s" % new_id())
        patch = (
            "from session_peers import storage as st\n"
            "real = j.settle\n"
            "def watch(mid, state, by):\n"
            "    open(%r, 'w').write(json.dumps([state, sorted(os.listdir(st.request_dir()))]))\n"
            "    return real(mid, state, by)\n"
            "j.settle = watch\n" % seen
        )
        proc, rec, rid = self._ask_child(tid, patch)
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "ans")
        self.assertEqual(rc, 0, err)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["message"], "ans")
        state, names = json.loads(pathlib.Path(seen).read_text())
        self.assertEqual(state, "consumed")
        self.assertEqual([n for n in names if rid in n], [])
        self.assertFalse(os.path.exists(self.mailbox(rid)[0]))
        self.assertEqual(self.entry(rid)["settlement"]["state"], "replied")
        self.assertEqual(self.entry(journal.reply_message_id(rid))["settlement"]["state"], "consumed")

    def test_ask_exits_76_when_its_consumed_settlement_fails_and_still_prints_the_body(self):
        tid, _rollout = self.one_thread()
        patch = (
            "real = j.settle\n"
            "def flaky(mid, state, by):\n"
            "    if state == 'consumed':\n        raise j.JournalUpdateFailed('injected ask failure')\n"
            "    return real(mid, state, by)\n"
            "j.settle = flaky\n"
        )
        proc, rec, rid = self._ask_child(tid, patch)
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "ans")[0], 0)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 76, err)
        payload = json.loads(out)
        self.assertEqual(payload["message"], "ans")
        self.assertEqual(payload["journal_error"], "injected ask failure")
        self.assertIn("journal update failed: injected ask failure", err)
        self.assertIsNone(self.entry(journal.reply_message_id(rid))["settlement"])

    def test_a_different_reply_file_on_disk_is_refused_without_journaling_the_reply(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        pathlib.Path(self.mailbox(rid)[1]).write_text(json.dumps(
            {"request_id": rid, "session_id": rec["sessionId"], "message": "X: on disk", "replied_at": "now"}))
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "Y: late")
        self.assertEqual(rc, 1, err)
        self.assertIn("already has a different reply", err)
        self.assertIsNone(self.entry(journal.reply_message_id(rid)))
        self.assertEqual(json.loads(pathlib.Path(self.mailbox(rid)[1]).read_text())["message"], "X: on disk")

    def test_a_reply_id_taken_by_another_record_exits_1_with_that_message(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        taken = journal.JournalRefused("reply id is taken by another record")
        with mock.patch.object(journal, "record_reply", side_effect=taken):
            rc, _out, err = self.reply_as(rec["sessionId"], request["request_id"], "ans")
        self.assertEqual(rc, 1)
        self.assertIn("error: reply id is taken by another record", err)
        self.assertFalse(os.path.exists(self.mailbox(request["request_id"])[1]))

    def test_a_same_body_reply_retry_is_idempotent(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "same")[0], 0)
        rc, out, err = self.reply_as(rec["sessionId"], rid, "same")
        self.assertEqual(rc, 0, err)
        self.assertIn("already replied", out)
        replies = [r for r in self.records() if r["type"] == "message" and r["kind"] == "reply"]
        self.assertEqual(len(replies), 1)
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "different")
        self.assertEqual(rc, 1)
        self.assertIn("different reply", err)

    def test_a_retry_after_a_kill_following_the_record_works_and_the_requester_can_get_it(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        meta_path, reply_path = self.mailbox(rid)
        meta = json.loads(pathlib.Path(meta_path).read_text())
        # The replier was killed after the journal record, before the reply file existed.
        journal.record_reply(rid, "cc:" + rec["sessionId"], [meta["requester"]], "after the kill",
                             meta["expires_at"], meta_path)
        self.assertFalse(os.path.exists(reply_path))
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "after the kill")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(reply_path))
        self.assertEqual(self.entry(rid)["settlement"]["state"], "replied")
        self.as_codex(tid)
        rc, out, err = self.cli("inbox", "get", journal.reply_message_id(rid), "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["body"], "after the kill")


# ==========================================================================
# Local size refusals leave nothing behind
# ==========================================================================


def _near_cap():
    """A message that passes the CLI bound but whose wrapped payload does not."""
    return "x" * (sp_constants.MAX_TEXT_CHARS - 50)


class JournalLocalSizeRefusalTests(JournalCliBase):
    def _nothing_left(self, listener=None):
        self.assertEqual(self.records(), [])
        self.assertEqual(self.queue_calls(), [])
        if listener is not None:
            time.sleep(0.2)
            self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])

    def test_an_oversize_codex_queue_is_refused_before_it_is_recorded(self):
        _listener, sender = self.peer("cc-sender", pid=os.getpid())
        self.as_cc(sender)
        tid, _rollout = self.one_thread()
        rc, _out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", _near_cap(), "--record")
        self.assertEqual(rc, 1)
        self.assertIn("error: ", err)
        self._nothing_left()

    def test_an_oversize_correlated_claude_request_is_refused_before_any_record_or_mailbox(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        for verb in ("dispatch", "ask"):
            rc, _out, err = self.cli(verb, "--to", "cc:cc-target", "--from-thread", tid,
                                     "--message", _near_cap(), "--timeout", "5")
            self.assertEqual(rc, 2, (verb, err))
            self.assertIn("peer cap", err)
        self._nothing_left(listener)

    def test_a_handover_to_a_codex_shim_by_its_cc_address_is_refused(self):
        tid, _rollout = self.one_thread()
        JournalSendRecordTests._codex_shim(self, tid)
        _la, first = self.peer("cc-first", pid=os.getpid())
        self.as_cc(first)
        rc, _out, err = self.cli("inbox", "handover", "--to", "cc:" + tid)
        self.assertEqual(rc, 1)
        self.assertIn("codex:" + tid, err)
        self.assertEqual([r for r in self.records() if r["type"] == "handover"], [])
        self.as_codex(new_id())
        _rc, out, _err = self.cli("record", "--to", "cc:%s" % first["sessionId"], "--message", "carry on", "--json")
        mid = json.loads(out)["message_id"]
        self.as_cc(first)
        rc, out, err = self.cli("inbox", "handover", "--to", "codex:" + tid, "--json")
        self.assertEqual(rc, 0, err)
        self.as_codex(tid)
        rc, out, err = self.cli("inbox", "get", mid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["body"], "carry on")


class JournalOpenCodeLocalSizeRefusalTests(JournalOpenCodeBase):
    def test_an_oversize_opencode_send_is_refused_with_the_grant_intact(self):
        _listener, rec = self.peer("cc-sender", pid=os.getpid())
        self.as_claude(rec)
        grant_id = new_id()
        sp_reply_wake.issue(grant_id, "oc:" + OC_B, "cc:" + rec["sessionId"], 600)
        grant = pathlib.Path(sp_reply_wake.path_for(grant_id))
        rc, _out, err = self.cli("send", "--to", "oc:%s" % OC_B, "--message", _near_cap(),
                                 "--in-reply-to", grant_id, "--record")
        self.assertEqual(rc, 1)
        self.assertIn("peer cap", err)
        self.assertTrue(grant.exists())
        self.assertEqual(self.records(), [])
        self.assertEqual(self.oc_calls("session.prompt"), [])

    def test_an_oversize_opencode_request_is_refused_before_any_record_or_mailbox(self):
        _listener, rec = self.peer("cc-main", pid=os.getpid())
        self.as_claude(rec)
        rc, _out, err = self.cli("dispatch", "--to", "oc:%s" % OC_B, "--wake", "--message", "x" * (
            sp_constants.MAX_TEXT_CHARS - 50), "--json")
        self.assertEqual(rc, 2)
        self.assertIn("peer cap", err)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.oc_calls("session.prompt"), [])
        self.assertEqual(list(pathlib.Path(sp_storage.request_dir()).glob("*.json")), [])


# ==========================================================================
# Race interleavings
# ==========================================================================


class JournalReplyRaceTests(JournalCliBase):
    def _paused_reply(self, sid, rid, body):
        ready = os.path.join(self.jhome, "ready-%s" % new_id())
        go = os.path.join(self.jhome, "go-%s" % new_id())
        patch = (
            "def hook():\n"
            "    open(%r, 'w').close()\n"
            "    deadline = time.time() + 30\n"
            "    while not os.path.exists(%r) and time.time() < deadline:\n        time.sleep(0.02)\n"
            "m._after_reply_validation = hook\n" % (ready, go)
        )
        saved = os.environ.get("CLAUDE_CODE_SESSION_ID")
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        try:
            proc = self.run_patched(patch, "reply", "--request", rid, "--message", body, wait=False)
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved
        self.assertTrue(wait_for(lambda: os.path.exists(ready), timeout=30), "child never reached the hook")
        return proc, go

    def _pressure(self, rounds=1):
        """Add finished (acknowledged) filler entries, lower the cap to the journal's
        size, then write an unrelated record: it only fits if the oldest eligible
        entries are pruned. Asserts the record was written and something was pruned."""
        slot = "cc:" + new_id()
        with mock.patch.object(sp_identity, "resolve_typed", lambda target, exclude=None: {"kind": target.partition(":")[0]}):
            for _ in range(rounds):
                os.environ.pop(CAP_ENV, None)
                before = [e["message_id"] for e in self.entries()]
                fillers = [journal.record_message("cc:" + new_id(), [slot], "f" * 500) for _ in range(4)]
                for mid in fillers:
                    journal.ack(mid, slot)
                os.environ[CAP_ENV] = str(os.path.getsize(self.jpath()))
                unrelated = journal.record_message("cc:" + new_id(), ["cc:" + new_id()], "unrelated")
                self.assertIsNotNone(self.entry(unrelated), "the pressure record was not written")
                self.assertTrue([m for m in before + fillers if self.entry(m) is None], "pressure pruned nothing")

    def test_a_live_mailbox_keeps_the_first_reply_when_a_paused_reply_resumes(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        proc, go = self._paused_reply(rec["sessionId"], rid, "Y: the loser")
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "X: the winner")
        self.assertEqual(rc, 0, err)
        reply_id = journal.reply_message_id(rid)
        self.as_codex(tid)
        self.assertEqual(self.cli("inbox", "get", reply_id)[0], 0)
        self.assertEqual(self.cli("ack", reply_id)[0], 0)
        self._pressure()
        # The mailbox is live: neither the request nor the reply may be pruned.
        self.assertIsNotNone(self.entry(rid))
        self.assertEqual(self.entry(reply_id)["body"], "X: the winner")
        pathlib.Path(go).touch()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("different reply", err)
        self.assertEqual(self.entry(reply_id)["body"], "X: the winner")
        reply_file = json.loads(pathlib.Path(self.mailbox(rid)[1]).read_text())
        self.assertEqual(reply_file["message"], "X: the winner")

    def test_a_consumed_request_refuses_a_paused_reply_with_a_different_body(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        proc, go = self._paused_reply(rec["sessionId"], rid, "Y: too late")
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "X: first")[0], 0)
        rc, out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "5", "--json")
        self.assertEqual(rc, 0, err)
        reply_id = journal.reply_message_id(rid)
        self.assertIsNotNone(self.entry(reply_id))
        for _ in range(5):
            if self.entry(reply_id) is None:
                break
            self._pressure()
        self.assertIsNone(self.entry(reply_id), "the consumed reply was never pruned")
        pathlib.Path(go).touch()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, err)
        self.assertIn("error: ", err)
        self.assertFalse(os.path.exists(self.mailbox(rid)[1]))
        self.assertFalse(os.path.exists(self.mailbox(rid)[0]))
        self.assertEqual([e for e in self.entries() if e["body"] == "Y: too late"], [])


    def _mailbox_changed_during_pause(self, change):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        proc, go = self._paused_reply(rec["sessionId"], rid, "Z: stale")
        meta_path, reply_path = self.mailbox(rid)
        change(meta_path)
        pathlib.Path(go).touch()
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 1, err)
        self.assertIsNone(self.entry(journal.reply_message_id(rid)))
        self.assertFalse(os.path.exists(reply_path))
        return err

    def test_mailbox_removed_during_pause(self):
        err = self._mailbox_changed_during_pause(os.unlink)
        self.assertIn("unknown or expired", err)

    def test_mailbox_expired_during_pause(self):
        def expire(path):
            meta = json.loads(pathlib.Path(path).read_text())
            meta["expires_at"] = time.time() - 1
            pathlib.Path(path).write_text(json.dumps(meta))
        err = self._mailbox_changed_during_pause(expire)
        self.assertIn("is expired", err)

    def test_mailbox_reassigned_during_pause(self):
        def reassign(path):
            meta = json.loads(pathlib.Path(path).read_text())
            meta["target_session_id"] = new_id()
            pathlib.Path(path).write_text(json.dumps(meta))
        err = self._mailbox_changed_during_pause(reassign)
        self.assertIn("no longer belongs", err)


# ==========================================================================
# No ACK from any other path
# ==========================================================================


class JournalNoImplicitAckTests(JournalCliBase):
    def test_codex_queue_acceptance_is_not_an_ack(self):
        _listener, sender = self.peer("cc-sender", pid=os.getpid())
        self.as_cc(sender)
        tid, _rollout = self.one_thread()
        rc, out, err = self.cli("send", "--to", "codex:%s" % tid, "--message", "x", "--record", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.queue_calls()), 1)
        self.assert_no_acks(json.loads(out)["message_id"])

    def test_a_claude_socket_write_is_not_an_ack(self):
        listener, _rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--message", "x", "--record", "--json")
        self.assertEqual(rc, 0, err)
        wait_for(lambda: listener.of_type("user"))
        self.assert_no_acks(json.loads(out)["message_id"])

    def test_waiting_for_idle_is_not_an_ack(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, out, err = self.cli("send", "--to", "cc:cc-target", "--message", "x", "--record", "--json")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("wait", "--for", "cc:cc-target", "--state", "idle", "--timeout", "5")
        self.assertEqual(rc, 0, err)
        self.assert_no_acks(json.loads(out)["message_id"])

    def test_a_fully_settled_exchange_has_no_ack(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        _rc, request, _err = self.dispatch("cc:cc-target", tid)
        rid = request["request_id"]
        self.assertEqual(self.reply_as(rec["sessionId"], rid, "done")[0], 0)
        rc, _out, err = self.cli("await", "--request", rid, "--from-thread", tid, "--timeout", "5")
        self.assertEqual(rc, 0, err)
        self.assert_no_acks(rid)
        self.assert_no_acks(journal.reply_message_id(rid))
        self.assertEqual(self.entry(rid)["settlement"]["state"], "replied")
        self.assertEqual(self.entry(journal.reply_message_id(rid))["settlement"]["state"], "consumed")

    def test_a_failed_meta_write_after_the_reply_settlement_still_exits_0(self):
        _listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        rc, request, err = self.dispatch("cc:cc-target", tid)
        self.assertEqual(rc, 0, err)
        rid = request["request_id"]
        real = journal._Writer.write_meta

        def flaky(writer, meta):
            if self.records()[-1]["type"] == "settled":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(writer, meta)

        with mock.patch.object(journal._Writer, "write_meta", flaky):
            rc, _out, err = self.reply_as(rec["sessionId"], rid, "approved")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.records()[-1]["type"], "settled")
        self.assertEqual(self.entry(rid)["settlement"]["state"], "replied")


# ==========================================================================
# Pointer flow and the new commands
# ==========================================================================


class JournalCommandTests(JournalCliBase):
    def test_record_get_ack_for_a_claude_recipient(self):
        _la, sender = self.peer("cc-sender", pid=os.getpid())
        _lb, target = self.peer("cc-target", pid=os.getppid())
        self.as_cc(sender)
        rc, out, err = self.cli("record", "--to", "cc:%s" % target["sessionId"], "--message", "act on this now")
        self.assertEqual(rc, 0, err)
        mid = out.splitlines()[0].split()[2].rstrip(":")
        self.assertEqual(
            out.splitlines()[0],
            "critical message %s: run peers.py inbox get %s, then peers.py ack %s" % (mid, mid, mid),
        )
        self.assertEqual(listener_frames(_lb), [])  # record-only: nothing is sent
        self.as_cc(target)
        rc, out, err = self.cli("inbox", "journal", "--json")
        self.assertEqual(rc, 0, err)
        listing = json.loads(out)
        self.assertEqual([i["message_id"] for i in listing], [mid])
        self.assertNotIn("body", listing[0])
        rc, out, err = self.cli("inbox", "get", mid)
        self.assertEqual(rc, 0, err)
        self.assertIn("act on this now", out)
        rc, out, err = self.cli("ack", mid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["for"], "cc:" + target["sessionId"])
        self.assertEqual(json.loads(self.cli("inbox", "journal", "--json")[1]), [])
        self.assertEqual(len(json.loads(self.cli("inbox", "journal", "--all", "--json")[1])), 1)
        rc, out, err = self.cli("inbox", "acks", mid)
        self.assertEqual(rc, 0, err)
        self.assertIn("acked", out)

    def test_acks_and_journal_human_output_show_each_recipients_transport_error(self):
        _la, first = self.peer("cc-one", pid=os.getpid())
        _lb, second = self.peer("cc-two", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        a, b = "cc:" + first["sessionId"], "cc:" + second["sessionId"]
        rc, out, err = self.cli("record", "--to", a, "--to", b, "--message", "both", "--json")
        self.assertEqual(rc, 0, err)
        mid = json.loads(out)["message_id"]
        journal.transport_error(mid, a, "err-one")
        journal.transport_error(mid, b, "err-two")
        rc, out, err = self.cli("inbox", "acks", mid)
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"  %s: pending; transport error at \S+: err-one\n" % a)
        self.assertRegex(out, r"  %s: pending; transport error at \S+: err-two\n" % b)
        self.assertNotIn("transport error for", out)
        self.as_cc(first)
        rc, out, err = self.cli("inbox", "journal")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"%s=pending \(transport error at \S+: err-one\)" % a)
        self.assertNotIn("err-two", out)
        rc, out, err = self.cli("inbox", "get", mid)
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"transport error for %s at \S+: err-one" % a)
        self.assertRegex(out, r"transport error for %s at \S+: err-two" % b)

    def test_record_to_several_recipients_acks_per_recipient(self):
        _la, first = self.peer("cc-one", pid=os.getpid())
        _lb, second = self.peer("cc-two", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        rc, out, err = self.cli(
            "record", "--to", "cc:%s" % first["sessionId"], "--to", "cc:%s" % second["sessionId"],
            "--message", "both of you", "--json",
        )
        self.assertEqual(rc, 0, err)
        mid = json.loads(out)["message_id"]
        self.assertEqual(sorted(json.loads(out)["to"]),
                         sorted(["cc:" + first["sessionId"], "cc:" + second["sessionId"]]))
        self.as_cc(second)
        self.assertEqual(self.cli("ack", mid)[0], 0)
        rc, out, err = self.cli("inbox", "acks", mid, "--json")
        self.assertEqual(rc, 0, err)
        states = {r["slot"]: r["state"] for r in json.loads(out)["recipients"]}
        self.assertEqual(states, {"cc:" + first["sessionId"]: "pending", "cc:" + second["sessionId"]: "acked"})
        # The sender sees the same per-recipient state.
        self.as_codex(tid)
        rc, out, err = self.cli("inbox", "acks", mid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual({r["slot"]: r["state"] for r in json.loads(out)["recipients"]}, states)

    def test_record_needs_an_identifiable_caller_and_at_most_64_recipients(self):
        _lb, target = self.peer("cc-target", pid=os.getppid())
        rc, _out, err = self.cli("record", "--to", "cc:%s" % target["sessionId"], "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("cannot tell which session is asking", err)
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        args = []
        for _i in range(sp_constants.JOURNAL_MAX_RECIPIENTS + 1):
            args += ["--to", "cc:%s" % target["sessionId"]]
        rc, _out, err = self.cli("record", *args, "--message", "x")
        self.assertEqual(rc, 1)
        self.assertIn("at most", err)

    def test_handover_lets_the_successor_read_and_ack(self):
        _la, first = self.peer("cc-first", pid=os.getpid())
        _lb, second = self.peer("cc-second", pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        _rc, out, _err = self.cli("record", "--to", "cc:%s" % first["sessionId"], "--message", "carry on", "--json")
        mid = json.loads(out)["message_id"]
        self.as_cc(first)
        rc, out, err = self.cli("inbox", "handover", "--to", "cc:%s" % second["sessionId"], "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["to"], "cc:" + second["sessionId"])
        self.as_cc(second)
        self.assertEqual(self.cli("inbox", "get", mid)[0], 0)
        rc, out, err = self.cli("ack", mid, "--for", "cc:%s" % first["sessionId"], "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["by"], "cc:" + second["sessionId"])

    def test_a_handover_names_a_typed_identity_not_a_name(self):
        _la, first = self.peer("cc-first", pid=os.getpid())
        _lb, _second = self.peer("cc-second", pid=os.getppid())
        self.as_cc(first)
        rc, _out, err = self.cli("inbox", "handover", "--to", "cc-second")
        self.assertEqual(rc, 1)
        self.assertIn("typed identity", err)
        self.assertEqual([r for r in self.records() if r["type"] == "handover"], [])

    def test_the_new_commands_take_no_as_or_from_thread(self):
        mid = new_id()
        for argv in (
            ("record", "--to", "cc:x", "--message", "m"),
            ("inbox", "get", mid),
            ("inbox", "journal"),
            ("inbox", "acks", mid),
            ("inbox", "handover", "--to", "cc:" + new_id()),
            ("ack", mid),
        ):
            for flag in (("--as", "cc:" + new_id()), ("--from-thread", new_id())):
                with self.subTest(argv=argv, flag=flag[0]):
                    with self.assertRaises(SystemExit) as raised:
                        self.cli(*argv, *flag)
                    self.assertEqual(raised.exception.code, 2)

    def test_existing_inbox_commands_keep_their_own_routing(self):
        # `list` still needs an OpenCode session; the journal subcommands do not.
        self.as_codex(new_id())
        rc, _out, err = self.cli("inbox", "list")
        self.assertEqual(rc, 1)
        self.assertIn("OPENCODE_SESSION_ID", err)


def listener_frames(listener):
    time.sleep(0.2)
    return listener.of_type("user")


# ==========================================================================
# Regressions
# ==========================================================================


class JournalRegressionTests(JournalCliBase):
    def test_twenty_expired_requests_do_not_block_the_next_ask(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        tid, _rollout = self.one_thread()
        sender, recipient = "codex:" + tid, "cc:" + rec["sessionId"]
        body = "q" * (200 * 1024)
        expires_at = time.time() - 60
        expired = [new_id() for _ in range(20)]
        for rid in expired:
            mailbox, _reply = self.mailbox(rid)
            journal.record_request(rid, sender, [recipient], body, expires_at, mailbox, fresh_id=True)

        self.assertEqual([e["message_id"] for e in self.entries()], expired)
        self.as_cc(rec)
        rc, out, err = self.cli("inbox", "acks", expired[0], "--json")
        self.assertEqual(rc, 0, err)
        view = json.loads(out)
        self.assertEqual(view["recipients"][0]["state"], "pending")
        self.assertIsNone(view["settlement"])

        self.as_codex(tid)
        message = pathlib.Path(self.jhome, "ask.txt")
        message.write_text(body, encoding="utf-8")
        proc = self.spawn("ask", "--to", recipient, "--message-file", str(message),
                          "--timeout", "30", "--json")
        self.assertTrue(wait_for(lambda: listener.of_type("user") or proc.poll() is not None),
                        "ask did not send its request or exit")
        if proc.poll() is not None:
            out, _ = proc.communicate(timeout=5)
            self.fail("ask exited %d before sending: %s" % (proc.returncode, out))
        frame = listener.of_type("user")[0]
        rid = frame["message"]["content"].split('id="')[1].split('"')[0]
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "ok")
        self.assertEqual(rc, 0, err)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["message"], "ok")
        self.assertIsNone(self.entry(expired[0]))
        self.assertEqual(self.entry(expired[1])["recipients"][0]["state"], "pending")

    def test_a_mixed_claude_and_codex_environment_ask_behaves_as_before_and_records_the_requester(self):
        listener, rec = self.peer("cc-target", pid=os.getppid())
        _lo, other = self.peer("cc-other", pid=os.getpid())
        tid, _rollout = self.one_thread()
        os.environ["CLAUDE_CODE_SESSION_ID"] = other["sessionId"]
        os.environ["CODEX_THREAD_ID"] = tid
        proc = self.spawn("ask", "--to", "cc:cc-target", "--message", "q?", "--timeout", "30", "--json")
        frame = wait_for(lambda: listener.of_type("user"))[0]
        rid = frame["message"]["content"].split('id="')[1].split('"')[0]
        entry = self.entry(rid)
        self.assertEqual(entry["from"], "codex:" + tid)
        self.assertEqual(entry["to"], ["cc:" + rec["sessionId"]])
        rc, _out, err = self.reply_as(rec["sessionId"], rid, "ok")
        self.assertEqual(rc, 0, err)
        out, _unused = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["message"], "ok")

    def test_control_characters_in_names_and_bodies_are_sanitized_in_human_output_only(self):
        hostile = "evil\x1b[31mred\x1b[0m\x07\nsecond line"
        _lb, target = self.peer(hostile, pid=os.getppid())
        tid, _rollout = self.one_thread()
        self.as_codex(tid)
        body = "a\x1b[31mred\x1b[0m\x07b\x00c"
        rc, out, err = self.cli("record", "--to", "cc:%s" % target["sessionId"], "--message", body)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out.splitlines()), 2, out)
        for bad in ("\x1b", "\x07", "\x00"):
            self.assertNotIn(bad, out)
        self.assertIn("evilred", out)
        mid = out.splitlines()[0].split()[2].rstrip(":")
        self.as_cc(target)
        rc, out, err = self.cli("inbox", "get", mid)
        self.assertEqual(rc, 0, err)
        for bad in ("\x1b", "\x07", "\x00"):
            self.assertNotIn(bad, out)
        rc, out, err = self.cli("inbox", "get", mid, "--json")
        self.assertEqual(json.loads(out)["body"], body)
