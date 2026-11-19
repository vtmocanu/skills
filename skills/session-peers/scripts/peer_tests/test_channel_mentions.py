"""Mention grammar and quiet-delivery intent, with no live sessions."""

import unittest
import subprocess
import sys

from session_peers import channel_mentions


class MentionGrammarTests(unittest.TestCase):
    def test_large_nonmention_prefix_finishes_within_bounded_child(self):
        code = "from session_peers.channel_mentions import parse; assert parse('x'*200000+' https://example.invalid/@all')['tokens'] == []"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_many_distinct_mentions_finish_within_bounded_child(self):
        code = "from session_peers.channel_mentions import parse; body=' '.join('@u%d'%i for i in range(80000)); assert len(body)<1048576; assert len(parse(body)['tokens'])==80000"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_word_boundaries_typed_ids_and_sentence_punctuation(self):
        ident = "codex:11111111-1111-4111-8111-111111111111"
        body = "Hi @lead, (@here) and @" + ident + ". @lead: @all!"
        self.assertEqual(channel_mentions.parse(body),
                         {"tokens": ["lead", "here", ident, "all"], "all": True, "here": True,
                          "names": ["lead", ident]})

    def test_email_url_and_escaped_mentions_are_literal(self):
        for body in ("person@example.invalid", "person+@example.invalid", "föø@host",
                     "https://example.invalid/@all", "mailto:joe+@example.invalid", r"\@all \@lead",
                     "word@lead", "@@lead"):
            with self.subTest(body=body):
                self.assertEqual(channel_mentions.parse(body)["tokens"], [])

    def test_backtick_spans_fences_and_unclosed_code_are_literal(self):
        for body in ("`@all`", "``code ` @all``", "```python\n@lead @here\n```", "before ` @all",
                     "```\n@all\nnot closed"):
            with self.subTest(body=body):
                self.assertEqual(channel_mentions.parse(body)["tokens"], [])
        self.assertEqual(channel_mentions.parse("`@all` then @lead")["names"], ["lead"])

    def test_unicode_names_and_reserved_case_are_explicit(self):
        result = channel_mentions.parse("@élise @ALL @all @oc:ses_abcdefgh12")
        self.assertEqual(result["names"], ["élise", "ALL", "oc:ses_abcdefgh12"])
        self.assertTrue(result["all"])
