#!/usr/bin/env python3
"""Unit tests for the AI Race Engineer answer-shaping helpers in midware/runtime.py.

These guard the fixes made after live testing on real telemetry showed the
local model (1) not reliably stopping at one sentence on its own even when
the persona explicitly asked it to, and (2) generating unnecessarily long
answers for plain yes/no questions, both of which were adding real latency
for a real-time racing use case.
"""

import unittest

from midware.runtime import (
    _EXPLAIN_REQUEST_RE,
    _compute_alert_signal,
    _first_sentence,
    _next_engineer_alert,
    _unavailable_data_topic,
)


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


class UnavailableDataTopicDetectionTests(unittest.TestCase):
    def test_tire_pressure_question_is_flagged(self):
        self.assertEqual(_unavailable_data_topic("what's my tire pressure?"), "tire pressure")

    def test_british_spelling_tyre_pressure_is_also_flagged(self):
        self.assertEqual(_unavailable_data_topic("what's my tyre pressure?"), "tire pressure")

    def test_weather_question_is_flagged(self):
        self.assertEqual(_unavailable_data_topic("is it raining out there?"), "weather or track temperature")

    def test_sector_time_question_is_flagged(self):
        self.assertEqual(_unavailable_data_topic("how was my sector 2?"), "sector times")

    def test_drs_question_is_flagged(self):
        self.assertEqual(_unavailable_data_topic("should I use DRS now?"), "DRS/ERS/KERS systems")

    def test_laps_remaining_question_is_flagged(self):
        self.assertEqual(_unavailable_data_topic("how many laps are left?"), "total race laps remaining")

    def test_match_is_case_insensitive(self):
        self.assertIsNotNone(_unavailable_data_topic("WHAT'S MY TIRE PRESSURE?"))

    def test_tire_wear_question_is_not_flagged(self):
        # Real, answerable data (tire_wear_pct is computed by tire_strategy.py)
        # -- must not collide with the "tire pressure"/"tire temperature" patterns.
        self.assertIsNone(_unavailable_data_topic("how's my tire wear?"))

    def test_plain_pit_strategy_question_is_not_flagged(self):
        self.assertIsNone(_unavailable_data_topic("should I pit now?"))

    def test_fuel_question_is_not_flagged(self):
        self.assertIsNone(_unavailable_data_topic("how much fuel do I have left?"))


def _pit_window(urgency="low", reasons=None):
    return {"urgency": urgency, "reasons": reasons or []}


class ComputeAlertSignalTests(unittest.TestCase):
    def test_no_problems_and_low_pit_urgency_returns_no_signal(self):
        self.assertIsNone(_compute_alert_signal({"problems": []}, _pit_window(urgency="low")))

    def test_medium_pit_urgency_does_not_alert(self):
        signal = _compute_alert_signal({"problems": []}, _pit_window(urgency="medium", reasons=["tires"]))
        self.assertIsNone(signal)

    def test_high_pit_urgency_returns_a_strategic_signal(self):
        signal = _compute_alert_signal({"problems": []}, _pit_window(urgency="high", reasons=["tires", "fuel"]))
        self.assertEqual(signal, {"key": "pit now", "reason": "tires, fuel", "category": "strategic"})

    def test_off_track_returns_a_physical_signal_even_with_low_pit_urgency(self):
        signal = _compute_alert_signal({"problems": ["off track"]}, _pit_window(urgency="low"))
        self.assertEqual(signal["key"], "get back on track")
        self.assertEqual(signal["category"], "physical")

    def test_near_track_edge_also_counts_as_off_track(self):
        signal = _compute_alert_signal({"problems": ["near track edge"]}, _pit_window(urgency="low"))
        self.assertEqual(signal["category"], "physical")

    def test_off_track_beats_high_pit_urgency(self):
        # Alarm-fatigue design (aviation/medical HMI): an immediate physical
        # danger outranks a strategic pit decision -- see
        # _compute_alert_signal's docstring.
        signal = _compute_alert_signal({"problems": ["off track"]}, _pit_window(urgency="high", reasons=["fuel"]))
        self.assertEqual(signal["category"], "physical")


def _signal(key="pit now", reason="tires", category="strategic"):
    return {"key": key, "reason": reason, "category": category}


class NextEngineerAlertTests(unittest.TestCase):
    def test_no_signal_never_alerts(self):
        text, key = _next_engineer_alert(None, active_key=None)
        self.assertIsNone(text)
        self.assertIsNone(key)

    def test_signal_fires_an_alert(self):
        text, key = _next_engineer_alert(_signal(key="pit now", reason="tires", category="strategic"), None)
        self.assertEqual(text, "🔧 Pit now -- tires")
        self.assertEqual(key, "pit now")

    def test_physical_and_strategic_categories_get_different_prefixes(self):
        # Alarm-fatigue design (aviation/medical HMI): an immediate physical
        # danger (off track) shouldn't be announced identically to a
        # strategic reminder (pit now) -- see _ALERT_CATEGORY_PREFIX.
        physical_text, _ = _next_engineer_alert(
            _signal(key="get back on track", reason="off track", category="physical"), None
        )
        strategic_text, _ = _next_engineer_alert(
            _signal(key="pit now", reason="tires", category="strategic"), None
        )
        self.assertTrue(physical_text.startswith("⚠️"))
        self.assertTrue(strategic_text.startswith("🔧"))
        self.assertNotEqual(physical_text[0], strategic_text[0])

    def test_unknown_category_gets_no_prefix(self):
        text, _ = _next_engineer_alert(_signal(key="pit now", reason="tires", category="informational"), None)
        self.assertEqual(text, "Pit now -- tires")

    def test_same_active_signal_does_not_fire_again(self):
        # Edge-triggered: still "pit now" from last tick -- stay quiet.
        text, key = _next_engineer_alert(_signal(key="pit now"), active_key="pit now")
        self.assertIsNone(text)
        self.assertEqual(key, "pit now")

    def test_a_different_signal_fires_a_new_alert(self):
        # e.g. was alerting "pit now", now it's "get back on track" instead.
        text, key = _next_engineer_alert(
            _signal(key="get back on track", reason="off track", category="physical"), active_key="pit now"
        )
        self.assertEqual(text, "⚠️ Get back on track -- off track")
        self.assertEqual(key, "get back on track")

    def test_signal_clearing_resets_the_active_key(self):
        text, key = _next_engineer_alert(None, active_key="pit now")
        self.assertIsNone(text)
        self.assertIsNone(key)

    def test_same_signal_fires_again_after_clearing_and_returning(self):
        # First tick: signal clears, active key clears.
        _, key = _next_engineer_alert(None, active_key="pit now")
        self.assertIsNone(key)
        # Second tick: same signal returns -- re-arms.
        text, key = _next_engineer_alert(_signal(key="pit now"), key)
        self.assertIsNotNone(text)
        self.assertEqual(key, "pit now")

    def test_alert_text_has_no_trailing_dash_when_reason_is_empty(self):
        text, _ = _next_engineer_alert(_signal(key="pit now", reason="", category="strategic"), None)
        self.assertEqual(text, "🔧 Pit now")


if __name__ == "__main__":
    unittest.main()
