import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from word_count import count_words  # noqa: E402


def test_empty_and_none_are_zero():
    assert count_words("") == 0
    assert count_words(None) == 0


def test_simple_sentence():
    assert count_words("Battle for the lead into turn one!") == 7


def test_contractions_and_alphanumerics_count_as_one_word():
    assert count_words("Car1 can't hold the gap to P2.") == 7


def test_punctuation_only_tokens_do_not_count():
    assert count_words("Wow --- incredible!!! ...") == 2


def test_whitespace_variations_do_not_affect_count():
    assert count_words("one\ttwo\nthree   four") == 4


def test_exactly_forty_five_words_boundary():
    text = " ".join(f"word{i}" for i in range(45))
    assert count_words(text) == 45
    over = text + " word45"
    assert count_words(over) == 46
