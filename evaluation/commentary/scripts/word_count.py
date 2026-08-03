"""Single canonical English word-count function.

`CommentaryConfig.max_words` (default 45) is a *prompt-level* hint, not a
program-enforced constraint -- see docs/commentary_test_matrix.md section 4
for the full trace. Whether real commentary text stays within it can only be
answered by measuring real generated output with one consistent counting
rule; this module is that rule, shared by:

- tests/integration/test_commentary_runtime.py::test_max_words_is_not_enforced_by_code
- evaluation/commentary/scripts/analyse_stability.py (endurance-run
  "violations of 45-word cap" column)
"""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def count_words(text: str) -> int:
    """Count words in English commentary text: contiguous runs of letters,
    digits and apostrophes (so "don't" is one word, "P1" is one word).
    Punctuation-only tokens and whitespace don't count. Empty/None text is 0
    words, not an error."""
    if not text:
        return 0
    return len(_WORD_RE.findall(text))
