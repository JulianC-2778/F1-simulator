#!/usr/bin/env python3
"""Unit tests for the AI Race Engineer answer-shaping helpers in midware/runtime.py.

These guard the fixes made after live testing on real telemetry showed the
local model (1) not reliably stopping at one sentence on its own even when
the persona explicitly asked it to, and (2) generating unnecessarily long
answers for plain yes/no questions, both of which were adding real latency
for a real-time racing use case.
"""

import unittest

from midware.runtime import _EXPLAIN_REQUEST_RE, _first_sentence


class FirstSentenceTests(unittest.TestCase):
    def test_returns_text_unchanged_when_it_is_already_one_sentence(self):
        self.assertEqual(_first_sentence("Yes, you should push."), "Yes, you should push.")

    def test_trims_everything_after_the_first_sentence(self):
        text = "No, stay on the current line. The car is too close to the edge (track position 0.87)."
        self.assertEqual(_first_sentence(text), "No, stay on the current line.")

    def test_recovers_a_clean_sentence_even_when_the_rest_was_cut_off_mid_word(self):
        # Real example seen in testing: max_tokens cut the model off
        # mid-word after the first sentence had already completed.
        text = (
            "No, stay out and continue racing. Your current position and speed "
            "suggest you are in a good race position; pitting now would likely cost valuable"
        )
        self.assertEqual(_first_sentence(text), "No, stay out and continue racing.")

    def test_does_not_treat_a_decimal_point_as_a_sentence_end(self):
        text = "Track position is 0.87 and that is fine."
        self.assertEqual(_first_sentence(text), "Track position is 0.87 and that is fine.")

    def test_returns_stripped_text_unchanged_when_there_is_no_sentence_end(self):
        # e.g. generation got cut off by max_tokens before reaching any
        # punctuation at all -- nothing to trim to, so pass it through.
        text = "  No, stay out and continue racing  "
        self.assertEqual(_first_sentence(text), "No, stay out and continue racing")

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(_first_sentence("   "), "")


class ExplainRequestDetectionTests(unittest.TestCase):
    def test_plain_yes_no_question_is_not_an_explain_request(self):
        self.assertIsNone(_EXPLAIN_REQUEST_RE.search("should I push now?"))

    def test_why_question_is_an_explain_request(self):
        self.assertIsNotNone(_EXPLAIN_REQUEST_RE.search("why should I push?"))

    def test_explain_keyword_is_an_explain_request(self):
        self.assertIsNotNone(_EXPLAIN_REQUEST_RE.search("Can you explain the tyre wear?"))

    def test_match_is_case_insensitive(self):
        self.assertIsNotNone(_EXPLAIN_REQUEST_RE.search("WHY is my fuel low?"))

    def test_whole_word_boundary_does_not_match_reasoning_as_a_substring(self):
        # Guards against the regex silently degrading to a substring match
        # (e.g. "reason" inside "reasoning") if it's ever edited carelessly.
        self.assertIsNone(_EXPLAIN_REQUEST_RE.search("Please describe your reasoning here"))


if __name__ == "__main__":
    unittest.main()
