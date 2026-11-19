"""Browser quiet posts and stored-channel broadcast confirmation use real HTTP."""

from peer_tests.test_web_chat import ChatBase
from peer_tests.support import *  # noqa: F401,F403
from session_peers import channels


class QuietBrowserTests(ChatBase):
    def quiet_ops(self):
        listener, cc, codex = self.ops_with_claude()
        def quiet(rec):
            rec["deliver"] = "mentions"
            return None, None
        channels.change_membership("ops", quiet, cc)
        return listener, cc, codex

    def test_plain_post_has_history_but_no_fake_journal_or_ack(self):
        listener, _cc, _codex = self.quiet_ops()
        row = self.wait_row(self.accepted("#ops", "quiet note"))
        self.assertFalse(row["journaled"])
        self.assertFalse(row["ack_requested"])
        self.assertEqual(row["recipients"], {})
        self.assertEqual(row["note"], "recorded, no notifications")
        self.assertEqual(self.journal_entries(), [])
        self.assertEqual(listener.of_type("user"), [])
        self.assertEqual(self.queue_calls(), [])
        [post] = self.send_entries()
        self.assertEqual(post["data"]["delivery"], "none")
        self.assertFalse(post["data"]["ack_requested"])

    def test_stored_channel_all_requires_preview_and_channel_bound_nonce(self):
        listener, cc, codex = self.quiet_ops()
        found = {"general": ([cc, codex], None, []), "liveness": {cc: "live", codex: "live"}}
        self.server.discovery.get = lambda **_kw: found
        preview = self.post("#ops", "please @all review")
        self.assertEqual(preview.status, 200, preview.body)
        self.assertEqual(self.send_entries(), [])
        self.assertEqual(self.journal_entries(), [])
        fields = {"to": "#ops", "text": "please @all review", "ack": True, "confirm": preview.json()["confirm"]}
        wrong = self.raw_post(json.dumps({**fields, "to": "#general"}).encode())
        self.assertEqual(wrong.status, 409)
        # A stale/wrong binding must not become a send to the other channel.
        self.assertEqual(self.send_entries(), [])
        preview = self.post("#ops", "please @all review")
        fields["confirm"] = preview.json()["confirm"]
        response = self.raw_post(json.dumps(fields).encode())
        self.assertEqual(response.status, 202, response.body)
        row = self.wait_row(response.json()["message_id"])
        self.assertTrue(row["journaled"])
        self.assertEqual(set(row["recipients"]), {cc, codex})
        self.assertEqual(len(listener.of_type("user")), 1)

