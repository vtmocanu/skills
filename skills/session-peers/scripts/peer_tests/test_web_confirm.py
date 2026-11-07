"""Web chat M2: an `@all` to #general from the browser, previewed then confirmed with a single-use nonce."""

from __future__ import annotations

import threading
from unittest import mock

from peer_tests.support import *  # noqa: F401,F403
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

    def test_a_full_queue_is_429_keeps_the_nonce_and_a_later_confirm_succeeds(self):
        nonce = self.nonce()
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
        mid = self.confirmed(nonce, tick=True)
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
