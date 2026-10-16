#!/usr/bin/env python3
"""Exercise the real jq reporter with isolated JSONL fixtures."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("cache-report.sh")


def response(message_id, *, side=False, usage=None):
    message = {"usage": usage if usage is not None else {"input_tokens": 10, "cache_read_input_tokens": 100,
                                "cache_creation_input_tokens": 20, "output_tokens": 30,
                                "cache_creation": {"ephemeral_1h_input_tokens": 20}}}
    if message_id is not None:
        message["id"] = message_id
    return {"type": "assistant", "isSidechain": side, "message": message}


class CacheReportTests(unittest.TestCase):
    def report(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = subprocess.run([str(SCRIPT), str(path)], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            banner, output = result.stdout.split("\n", 1)
            self.assertEqual(banner, "log: " + str(path))
            return json.loads(output)

    def test_repeated_content_rows_count_each_response_once(self):
        first = response("msg-z")
        last = response("msg-a", usage={"input_tokens": 5, "cache_read_input_tokens": 200,
                                        "cache_creation_input_tokens": 40, "output_tokens": 50,
                                        "cache_creation": {"ephemeral_5m_input_tokens": 40}})
        result = self.report([first, first, last, first, last])
        self.assertEqual(result["assistant_turns"], 2)
        self.assertEqual(result["cache_read_input_tokens"], {"sum": 300, "pct": 65.9})
        self.assertEqual(result["cache_creation_input_tokens"], {"sum": 60, "pct": 13.2})
        self.assertEqual(result["input_tokens"], {"sum": 15, "pct": 3.3})
        self.assertEqual(result["output_tokens"], {"sum": 80, "pct": 17.6})
        self.assertEqual(result["grand_total"], 455)
        self.assertEqual(result["context_first_turn"], 130)
        self.assertEqual(result["context_last_turn"], 245)
        self.assertEqual(result["cache_ttl_in_use"], "mixed (1h+5m)")

    def test_different_response_ids_with_identical_usage_are_not_collapsed(self):
        result = self.report([response("msg-a"), response("msg-b")])
        self.assertEqual(result["assistant_turns"], 2)
        self.assertEqual(result["grand_total"], 320)

    def test_missing_null_or_empty_ids_are_counted_individually(self):
        null_id = response(None)
        null_id["message"]["id"] = None
        result = self.report([response(None), response(None), null_id, null_id, response(""), response("")])
        self.assertEqual(result["assistant_turns"], 6)
        self.assertEqual(result["grand_total"], 960)

    def test_sidechain_duplicates_do_not_change_main_context_order(self):
        child = response("child", side=True, usage={"input_tokens": 900, "output_tokens": 100})
        first = response("main-z")
        last = response("main-a", usage={"input_tokens": 50})
        result = self.report([child, child, first, first, last, child])
        self.assertEqual(result["assistant_turns"], 3)
        self.assertEqual(result["subagent_turns"], 1)
        self.assertEqual(result["grand_total"], 1210)
        self.assertEqual(result["context_first_turn"], 130)
        self.assertEqual(result["context_last_turn"], 50)
        self.assertEqual(result["context_basis"], "main-thread turns")

    def test_rows_without_usage_do_not_claim_a_response_id(self):
        first = response("msg-a")
        partial = {"type": "assistant", "message": {"id": "msg-a"}}
        result = self.report([partial, {"type": "user", "message": {"id": "msg-a"}}, first, first])
        self.assertEqual(result["assistant_turns"], 1)
        self.assertEqual(result["grand_total"], 160)

    def test_first_usage_for_an_id_is_retained(self):
        result = self.report([response("msg-a"), response("msg-a", usage={"input_tokens": 999})])
        self.assertEqual(result["assistant_turns"], 1)
        self.assertEqual(result["grand_total"], 160)

    def test_zero_usage_keeps_zero_percentages_and_unknown_ttl(self):
        result = self.report([response("msg-a", usage={}), response("msg-a", usage={})])
        self.assertEqual(result["assistant_turns"], 1)
        self.assertEqual(result["grand_total"], 0)
        self.assertEqual(result["input_tokens"], {"sum": 0, "pct": 0})
        self.assertEqual(result["context_first_turn"], 0)
        self.assertEqual(result["context_last_turn"], 0)
        self.assertEqual(result["cache_ttl_in_use"], "unknown (no ephemeral field)")

    def test_empty_input_retains_error_shape(self):
        self.assertEqual(self.report([]), {"error": "no assistant turns with usage in this log"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
