"""Web chat M2: an `@all` to #general from the browser, previewed then confirmed with a single-use nonce."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from unittest import mock

from peer_tests.support import *  # noqa: F401,F403
from peer_tests import test_web_chat
from peer_tests.test_web_chat import ChatBase
from peer_tests.test_web_server import wait_until
from session_peers import channels as sp_channels, journal as sp_journal, web, webchat

ALL_TEXT = "@all freeze main"
GONE_REASON = "recipients changed, preview again"


class ConfirmBase(ChatBase):
    """Two live Claude sessions in derived #general, and a stored channel #ops with the same roster."""

    def setUp(self):
        super().setUp()
        self.set_oc(sessions=[])  # the fixture's default OpenCode sessions would join #general
        self.l1, self.c1 = self.peer_cc("claude-one")
        self.l2, self.c2 = self.peer_cc("claude-two")
        self.members = sorted([self.c1, self.c2])
        self.channel("ops", self.c1, self.c2, owner=self.c1)

    # -- requests ---------------------------------------------------------------

    def preview(self, text=ALL_TEXT, ack=True, tick=True):
        return self.post("#general", text, ack, tick=tick)

    def confirm_body(self, nonce, text=ALL_TEXT, ack=True, to="#general"):
        return json.dumps({"to": to, "text": text, "ack": ack, "confirm": nonce}).encode("utf-8")

    def confirm(self, nonce, text=ALL_TEXT, ack=True, to="#general", tick=False):
        if tick:
            self.tick()
        return self.raw_post(self.confirm_body(nonce, text, ack, to))

    def nonce(self, text=ALL_TEXT, ack=True):
        resp = self.preview(text, ack)
        self.assertEqual(resp.status, 200, resp.body)
        return resp.json()["confirm"]

    def confirmed(self, nonce=None, text=ALL_TEXT, tick=False):
        nonce = nonce or self.nonce(text)
        resp = self.confirm(nonce, text, tick=tick)
        self.assertEqual(resp.status, 202, resp.body)
        self.assertEqual(set(resp.json()), {"message_id", "state"})
        self.assertEqual(resp.json()["state"], "queued")
        return resp.json()["message_id"]

    def general_sends(self):
        return [e for e in self.history("general") if e["kind"] == "channel.send"]

    def user_frames(self, listener):
        return listener.of_type("user")

    def assert_no_all_traffic(self):
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.general_sends(), [])
        self.assertEqual(self.state.rows(), [])
        for listener in (self.l1, self.l2):
            self.assertEqual(self.user_frames(listener), [])


class ConfirmFlowTests(ConfirmBase):
    def test_example_3_preview_then_confirm_sends_to_exactly_the_previewed_set_asking_for_acks(self):
        resp = self.preview()
        self.assertEqual(resp.status, 200, resp.body)
        self.assert_headers(resp, web.JSON_TYPE)
        body = resp.json()
        self.assertEqual(set(body), {"confirm", "expires_in", "recipients", "excluded"})
        self.assertEqual((body["expires_in"], body["recipients"], body["excluded"]), (60, self.members, []))
        self.assertIsInstance(body["confirm"], str)
        self.assertGreaterEqual(len(body["confirm"]), 32)
        # The preview accepted, charged, queued and delivered nothing.
        self.assert_no_all_traffic()
        self.assertEqual(self.chat.gate.held, 0)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        mid = self.confirmed(body["confirm"])
        row = self.wait_row(mid)
        self.assertEqual((row["state"], row["kind"], row["to"], row["journaled"]), ("pending", "channel", "#general", True))
        self.assertIs(row["ack_requested"], True)
        self.assertEqual(sorted(row["recipients"]), self.members)
        self.assertEqual({s["ack"] for s in row["recipients"].values()}, {"pending"})
        [record] = self.journal_entries()
        self.assertEqual((record["message_id"], record["from"], record["body"]), (mid, self.peer.typed, ALL_TEXT))
        self.assertEqual(sorted(record["to"]), self.members)
        for listener in (self.l1, self.l2):
            [frame] = wait_for(lambda: self.user_frames(listener)) or [None]
            text, attrs = sp_protocol.unwrap_message(frame["message"]["content"])
            self.assertEqual(attrs["from-session"], self.peer.sid)
            self.assertIn("browser peer", text)
            self.assertIn(sp_journal.pointer_text(mid), text)
        [send] = self.general_sends()
        self.assertEqual((send["data"]["message_id"], send["data"]["sender"]), (mid, self.peer.typed))
        self.assertIs(send["data"]["ack_requested"], True)
        # The nonce was consumed.
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assertEqual(self.confirm(body["confirm"]).status, 409)

    def test_ack_false_still_previews_and_confirms_with_acks_requested(self):
        nonce = self.nonce(ack=False)
        resp = self.confirm(nonce, ack=False)  # the nonce binds ack true, and so does the confirm
        self.assertEqual(resp.status, 202, resp.body)
        mid = resp.json()["message_id"]
        row = self.wait_row(mid)
        self.assertIs(row["ack_requested"], True)
        [send] = self.general_sends()
        self.assertIs(send["data"]["ack_requested"], True)
        [frame] = wait_for(lambda: self.user_frames(self.l1)) or [None]
        self.assertIn(sp_journal.pointer_text(mid), sp_protocol.unwrap_message(frame["message"]["content"])[0])

    def test_the_general_page_shows_the_browser_post_with_its_ack_tally(self):
        mid = self.confirmed()
        self.wait_row(mid)
        sp_journal.ack(mid, self.c1)
        [entry] = [e for e in self.ok("/api/channel/general")["entries"] if e["kind"] == "send"]
        self.assertEqual((entry["message_id"], entry["sender"], entry["body"]), (mid, self.peer.typed, ALL_TEXT))
        self.assertEqual(entry["tally"], {"acked": 1, "total": 2, "missing": [self.c2], "unverified": 0})
        self.assertEqual({r["recipient"] for r in entry["delivery"]["recipients"]}, set(self.members))

    def test_gone_members_are_excluded_from_the_preview_and_from_the_send(self):
        real = sp_channels.member_liveness
        gone = self.c2

        def liveness(typed, cache=None):
            return "gone" if typed == gone else real(typed, cache)

        with mock.patch.object(sp_channels, "member_liveness", liveness):
            body = self.preview().json()
            self.assertEqual((body["recipients"], body["excluded"]), ([self.c1], [{"member": gone, "reason": "gone"}]))
            row = self.wait_row(self.confirmed(body["confirm"]))
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["recipients"][self.c1]["ack"], "pending")
        [record] = self.journal_entries()
        self.assertEqual(record["to"], [self.c1])
        self.assertEqual(self.user_frames(self.l2), [])

    def test_a_preview_over_the_recipient_cap_is_409_and_records_nothing(self):
        with mock.patch.object(sp_channels, "ALL_MAX_RECIPIENTS", 1):
            self.assert_refused(self.preview(), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()

    def test_a_preview_with_nobody_to_address_is_409(self):
        with mock.patch.object(sp_channels, "member_liveness", lambda typed, cache=None: "gone"):
            self.assert_refused(self.preview(), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)

    def test_a_pending_discovery_is_409_and_records_nothing(self):
        pending = web._empty_discovery(pending=True)
        with mock.patch.object(self.server.discovery, "get", lambda wait=None: dict(pending)):
            self.assert_refused(self.preview(), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()

    def test_a_truncated_discovery_that_checked_every_general_member_previews_and_a_stale_one_is_flagged(self):
        # Changed from "truncated is 409": truncated alone no longer refuses, since it can concern other channels.
        real = self.server.discovery.get
        with mock.patch.object(self.server.discovery, "get", lambda wait=None: dict(real(wait), truncated=True)):
            self.assertEqual(self.preview().status, 200)
        with mock.patch.object(self.server.discovery, "get", lambda wait=None: dict(real(wait), stale=True)):
            body = self.preview().json()
        self.assertIs(body["stale"], True)
        self.assertEqual(body["recipients"], self.members)

    def test_a_general_member_left_unchecked_is_409(self):
        real = self.server.discovery.get
        found = lambda wait=None: dict(real(wait), unchecked=[self.c2])
        with mock.patch.object(self.server.discovery, "get", found):
            self.assert_refused(self.preview(), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()

    def test_a_general_member_with_no_liveness_entry_is_409(self):
        real = self.server.discovery.get

        def found(wait=None):
            got = dict(real(wait))
            got["liveness"] = {k: v for k, v in got["liveness"].items() if k != self.c2}
            return got

        with mock.patch.object(self.server.discovery, "get", found):
            self.assert_refused(self.preview(), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()


class ConfirmBindingTests(ConfirmBase):
    def test_a_mismatch_is_409_and_leaves_the_nonce_usable(self):
        nonce = self.nonce()
        self.assert_refused(self.confirm(nonce, text="@all freeze main now"), 409)
        self.assert_refused(self.confirm("not-a-nonce-" + "x" * 30), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assert_no_all_traffic()
        self.assertEqual(self.confirm(nonce).status, 202)

    def test_the_same_text_to_a_stored_channel_with_the_same_roster_is_refused(self):
        nonce = self.nonce()
        # A nonce belongs to #general; with `confirm` any other route is a malformed request.
        self.assert_refused(self.confirm(nonce, to="#ops"), 400)
        # Without `confirm`, an `@all` to a stored channel stays refused.
        self.assert_refused(self.post("#ops", ALL_TEXT), 403)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assert_no_all_traffic()
        self.assertEqual(self.confirm(nonce).status, 202)

    def test_a_request_without_confirm_is_a_new_preview_and_never_a_send(self):
        first = self.nonce()
        second = self.nonce()
        self.assertNotEqual(first, second)
        self.assertEqual(self.chat.gate.nonces_outstanding, 2)
        self.assert_no_all_traffic()

    def test_the_binding_of_a_sender_is_checked_by_the_gate(self):
        gate = self.chat.gate
        sha = self.chat._digest(ALL_TEXT)
        nonce = gate.issue_nonce("general", self.peer.typed, self.members, sha, True)
        for args in (
            ("general", "web:" + new_uuid(), sha, True), ("ops", self.peer.typed, sha, True),
            ("general", self.peer.typed, self.chat._digest("other"), True), ("general", self.peer.typed, sha, False),
        ):
            with self.subTest(args=args):
                self.assertEqual(gate.confirm(nonce, *args, build=lambda r: self.fail("built")), (webchat.CONFIRM_STALE, None))
                self.assertEqual(gate.nonces_outstanding, 1)
        self.assertEqual(gate.confirm(nonce, "general", self.peer.typed, sha, True, build=lambda r: r),
                         (webchat.CONFIRM_OK, tuple(self.members)))

    def test_a_nonce_is_valid_for_sixty_seconds(self):
        resp = self.preview()
        self.assertEqual(resp.json()["expires_in"], webchat.NONCE_TTL)
        self.clock[0] += webchat.NONCE_TTL - 1
        self.assertEqual(self.confirm(resp.json()["confirm"]).status, 202)

    def test_a_nonce_expires_at_exactly_the_ttl(self):
        nonce = self.nonce()
        self.clock[0] += webchat.NONCE_TTL
        self.assert_refused(self.confirm(nonce), 409)
        self.assert_no_all_traffic()

    def test_a_build_failure_keeps_the_nonce_and_restores_the_charge(self):
        nonce = self.nonce()
        with mock.patch.object(self.chat.worker, "submit", side_effect=RuntimeError("boom")):
            self.confirm(nonce)  # the response is not the point: nothing was accepted
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assertEqual(self.chat.gate.held, 0)
        self.assertEqual(self.state.rows(), [])
        self.assertEqual(self.confirm(nonce).status, 202)  # no charge was left: still inside the minimum gap

    def test_an_expired_nonce_is_409_and_sends_nothing(self):
        nonce = self.nonce()
        self.clock[0] += 61
        self.assert_refused(self.confirm(nonce), 409)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()

    def test_a_nonce_is_single_use(self):
        nonce = self.nonce()
        self.wait_row(self.confirmed(nonce))
        self.assert_refused(self.confirm(nonce, tick=True), 409)
        self.assertEqual(len(self.journal_entries()), 1)

    def test_two_concurrent_confirms_make_exactly_one_send(self):
        nonce = self.nonce()
        barrier = threading.Barrier(2)
        results = []

        def go():
            barrier.wait(10)
            results.append(self.confirm(nonce))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(sorted(r.status for r in results), [202, 409])
        rows = self.state.rows()
        self.assertEqual(len(rows), 1)
        self.wait_row(rows[0]["mid"])
        self.assertEqual(len(self.journal_entries()), 1)

    def test_the_oldest_nonce_is_evicted_at_the_cap(self):
        with mock.patch.object(webchat, "NONCE_MAX", 2):
            first, second, third = self.nonce(), self.nonce(), self.nonce()
            self.assertEqual(self.chat.gate.nonces_outstanding, 2)
            self.assert_refused(self.confirm(first), 409)
            self.assertEqual(self.confirm(third).status, 202)
            self.assertEqual(self.confirm(second, tick=True).status, 202)

    def test_malformed_confirms_are_400_and_charge_nothing(self):
        nonce = self.nonce()
        text_only = {"to": "#general", "text": "hello", "ack": True, "confirm": nonce}
        cases = {
            "confirm on a plain post": json.dumps(text_only).encode(),
            "confirm to a stored channel": self.confirm_body(nonce, to="#ops"),
            "confirm to a direct list": json.dumps({"to": [self.c1], "text": ALL_TEXT, "ack": True, "confirm": nonce}).encode(),
            "confirm not a string": json.dumps({"to": "#general", "text": ALL_TEXT, "ack": True, "confirm": 5}).encode(),
            "confirm null": json.dumps({"to": "#general", "text": ALL_TEXT, "ack": True, "confirm": None}).encode(),
            "confirm empty": self.confirm_body(""),
            "confirm too long": self.confirm_body("n" * (web.CONFIRM_MAX + 1)),
            "extra key": json.dumps({"to": "#general", "text": ALL_TEXT, "ack": True, "confirm": nonce, "x": 1}).encode(),
            "confirm without ack": json.dumps({"to": "#general", "text": ALL_TEXT, "confirm": nonce}).encode(),
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.assert_refused(self.raw_post(body), 400)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assert_no_all_traffic()
        self.assertEqual(self.confirm(nonce).status, 202)


class ConfirmStillRefusedTests(ConfirmBase):
    def test_plain_general_and_at_all_elsewhere_stay_403(self):
        cases = [
            ("#general", "hello"),
            ("#general", "@allx not an all"),
            ("#ops", ALL_TEXT),
            ("#ops", "@all"),
            ([self.c1], ALL_TEXT),
        ]
        for to, text in cases:
            with self.subTest(to=to, text=text):
                self.assert_refused(self.post(to, text, tick=False), 403)
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_no_all_traffic()
        self.assertEqual(self.chat.gate.held, 0)


class ConfirmLimitTests(ConfirmBase):
    def test_a_rate_limited_confirm_is_429_keeps_the_nonce_and_charges_nothing(self):
        nonce = self.nonce()
        ops = self.accepted("#ops", "heads up", tick=False)  # takes this second's rate slot
        self.assert_refused(self.confirm(nonce), 429)
        self.wait_row(ops)
        self.assertEqual([r["mid"] for r in self.state.rows()], [ops])
        self.assertEqual(self.general_sends(), [])
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assertEqual(self.chat.gate.held, 0)
        mid = self.confirmed(nonce, tick=True)  # the nonce is still good a little later
        row = self.wait_row(mid)
        self.assertEqual(row["state"], "pending")

    def test_a_429_confirm_does_not_use_up_a_rate_slot(self):
        with mock.patch.object(webchat, "RATE_PER_MINUTE", 2):
            nonce = self.nonce()
            self.accepted("#ops", "heads up", tick=False)  # charge 1 of 2
            self.assert_refused(self.confirm(nonce), 429)  # inside the minimum gap; must charge nothing
            self.assertEqual(self.confirm(nonce, tick=True).status, 202)  # charge 2 of 2

    def test_a_binding_mismatch_while_rate_limited_is_409_not_429(self):
        nonce = self.nonce()
        self.accepted("#ops", "heads up", tick=False)
        self.assert_refused(self.confirm(nonce, text="@all freeze main now"), 409)
        self.assert_refused(self.confirm(nonce), 429)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)

    def test_concurrent_confirms_while_rate_limited_are_all_429_and_the_nonce_survives(self):
        nonce = self.nonce()
        ops = self.accepted("#ops", "heads up", tick=False)  # takes this second's rate slot
        count = 4
        barrier = threading.Barrier(count)
        results = []

        def go():
            barrier.wait(10)
            results.append(self.confirm(nonce))

        threads = [threading.Thread(target=go) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual([r.status for r in results], [429] * count)
        self.wait_row(ops)
        self.assertEqual([r["mid"] for r in self.state.rows()], [ops])
        self.assertEqual(self.general_sends(), [])
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assertEqual(self.chat.gate.held, 0)
        # Past the window, within the nonce's life: the one nonce still makes exactly one send.
        mid = self.confirmed(nonce, tick=True)
        self.assertEqual(self.wait_row(mid)["state"], "pending")
        self.assertEqual(len(self.state.rows()), 2)

    def test_a_full_queue_is_429_keeps_the_nonce_and_a_later_confirm_succeeds(self):
        nonce = self.nonce()
        patch = mock.patch.object(webchat, "RATE_PER_MINUTE", webchat.SEND_QUEUE_MAX + 1)  # one more than the eight
        patch.start()
        self.addCleanup(patch.stop)
        self.block_worker()
        for index in range(webchat.SEND_QUEUE_MAX):
            self.accepted("#ops", "heads up %d" % index)
        self.assertTrue(self.started.acquire(timeout=10))
        self.tick()
        self.assert_refused(self.confirm(nonce), 429)
        self.assertEqual(self.chat.gate.nonces_outstanding, 1)
        self.assertEqual(len(self.state.rows()), webchat.SEND_QUEUE_MAX)
        self.release.set()
        wait_until(lambda: self.chat.gate.held == 0, "the queue to drain")
        # The 429 charged nothing: at the refusal's own clock the one-second gap is open and the minute
        # holds exactly one more send, which a charged 429 would have used up.
        mid = self.confirmed(nonce, tick=False)
        self.assertEqual(self.wait_row(mid)["state"], "pending")


class ConfirmWorkerTests(ConfirmBase):
    def assert_changed(self, mid):
        row = self.wait_row(mid)
        self.assertEqual((row["state"], row["reason"], row["journaled"]), ("not sent", GONE_REASON, False))
        self.assertEqual(row["recipients"], {})
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(self.general_sends(), [])
        for listener in self._listeners:
            self.assertEqual(self.user_frames(listener), [])
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        return row

    def test_a_member_that_went_away_after_the_preview_is_not_sent(self):
        nonce = self.nonce()
        for path in self.sessions.glob("*.json"):
            if self.c2.split(":", 1)[1] in path.read_text():
                path.unlink()
        mid = self.confirmed(nonce)
        row = self.assert_changed(mid)
        self.assertFalse(row["delivering"])
        # Not retried: the nonce is gone and the row stays as it was.
        self.assert_refused(self.confirm(nonce, tick=True), 409)
        self.assertEqual(self.state.row(mid), row)

    def test_a_member_that_appeared_after_the_preview_is_not_sent(self):
        nonce = self.nonce()
        self.peer_cc("claude-late")
        self.assert_changed(self.confirmed(nonce))

    def test_a_live_member_the_journal_cannot_resolve_is_a_recorded_refusal_not_a_change(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self._children.append(proc)
        sid = new_uuid()
        self.write_record(proc.pid, "noproc", sid, str(self.socks / "u.sock"), procStart=None)
        unresolved = "cc:" + sid
        body = self.preview().json()
        self.assertIn(unresolved, body["recipients"])
        row = self.wait_row(self.confirmed(body["confirm"]))
        self.assertEqual((row["state"], row["journaled"]), ("pending", True))
        self.assertTrue(row["recipients"][unresolved]["outcome"].startswith("refused: "), row)
        self.assertEqual(row["recipients"][unresolved]["ack"], "not requested")
        [record] = self.journal_entries()
        self.assertEqual(sorted(record["to"]), self.members)

    def test_a_worker_failure_consumes_the_nonce_and_keeps_the_charge(self):
        nonce = self.nonce()
        with mock.patch.object(sp_channels, "send_as_web", side_effect=RuntimeError("boom")):
            row = self.wait_row(self.confirmed(nonce))
        self.assertEqual(row["state"], "not sent")
        self.assertIn("boom", row["reason"])
        self.assertEqual(self.chat.gate.nonces_outstanding, 0)
        self.assert_refused(self.confirm(nonce), 409)
        # The charge stays: within the minimum gap another send is rate limited.
        self.assert_refused(self.post("#ops", "again", tick=False), 429)
        self.assertEqual(self.journal_entries(), [])

    def test_the_worker_passes_the_previewed_recipients_to_send_as_web(self):
        calls = []
        real = sp_channels.send_as_web

        def spy(*args, **kw):
            calls.append((args[0], args[2], args[3], dict(kw)))
            return real(*args, **kw)

        with mock.patch.object(sp_channels, "send_as_web", spy):
            self.wait_row(self.confirmed())
        [(key, text, ack, kw)] = calls
        self.assertEqual((key, text, ack), ("general", ALL_TEXT, True))
        self.assertEqual(sorted(kw["expected_recipients"]), self.members)

    def test_the_existing_at_all_allowance_applies_to_a_second_confirmed_post(self):
        self.wait_row(self.confirmed())
        self.tick()
        row = self.wait_row(self.confirmed(text="@all second"))
        self.assertEqual(row["state"], "not sent")
        self.assertIn("at most one @all per sender", row["reason"])
        self.assertEqual(len(self.journal_entries()), 1)
        self.assertEqual(len(self.general_sends()), 1)


class ConfirmTeardownTests(ConfirmBase):
    def test_teardown_joins_a_delivering_worker_before_the_environment_is_restored(self):
        # Regresses the teardown ordering of ChatBase. With the join removed from stop_server, `joining` never fires.
        # With the stop moved after the environment restore, a captured HOME or CODEX_HOME differs from this test's.
        entered, release, joining = threading.Event(), threading.Event(), threading.Event()
        captured, failures, workers = [], [], []
        listeners = {self.c1: self.l1, self.c2: self.l2}
        real_deliver = sp_channels._deliver_member
        real_join = test_web_chat.join_or_fail

        def deliver(args, member, *rest, **kw):
            workers.append(threading.current_thread())
            entered.set()
            if not release.wait(10):
                failures.append("the delivery was never released")
            pair = (os.environ.get("HOME"), os.environ.get("CODEX_HOME"))
            captured.append(pair)
            if pair != (str(self.home), str(self.codex_dir)):
                # A regressed teardown: never deliver against the restored environment.
                failures.append("the delivery ran with HOME/CODEX_HOME %r" % (pair,))
                return sp_channels.OUTCOME_REFUSED + "test env mismatch"
            result = real_deliver(args, member, *rest, **kw)
            listener = listeners[member]
            deadline = time.monotonic() + 10
            while not listener.of_type("user") and time.monotonic() < deadline:
                time.sleep(0.01)
            if not listener.of_type("user"):
                failures.append("no user frame reached %s" % member)
            return result

        def join(thread, timeout, label):
            joining.set()
            return real_join(thread, timeout, label)

        errors = []

        def run_teardown():
            try:
                self.tearDown()
            except BaseException as exc:  # re-raised below
                errors.append(exc)

        # A regressed teardown still journals and appends the refused outcome after `deliver` returns:
        # pin those stores to this test's own paths so the developer's restored HOME is never written.
        journal_at, history_at = sp_journal.journal_dir(), sp_channels.history_dir()
        with mock.patch.object(webchat, "JOIN_TIMEOUT", 0.01), \
                mock.patch.object(test_web_chat, "join_or_fail", join), \
                mock.patch.object(sp_journal, "journal_dir", lambda: journal_at), \
                mock.patch.object(sp_channels, "history_dir", lambda: history_at), \
                mock.patch.object(sp_channels, "_deliver_member", deliver):
            self.confirmed()
            self.assertTrue(entered.wait(10), "the worker never started delivering")
            tearing = threading.Thread(target=run_teardown, daemon=True)
            tearing.start()
            try:
                self.assertTrue(joining.wait(10), "the teardown never joined the worker")
            finally:
                release.set()
                tearing.join(30)
        self.assertFalse(tearing.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(failures, [])
        self.assertEqual(len(captured), 2)
        for home, codex_home in captured:
            self.assertEqual(home, str(self.home))
            self.assertEqual(codex_home, str(self.codex_dir))
        self.assertTrue(workers)
        for worker in workers:
            self.assertFalse(worker.is_alive())
        for listener in (self.l1, self.l2):
            self.assertTrue(listener.of_type("user"))
