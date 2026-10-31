"""Public build stand-in for the shared health-word detector.

The private build ships a larger multilingual detector with its own test set, plus a private refused-words file.
This stand-in keeps the same `is_health(text)` contract with a short English list, and `denied(text)` adds a few
family words, so the callers here (decision_log, lessons_log and input_labels) hold private wording the same way.
Fail safe over precise; a call such as "time.sleep(1)" passes.
"""
from __future__ import annotations

import re

_PATTERN = re.compile(
    r"(?<![.\w])(?:sleep\w*|slept|asleep|insomnia|naps?|heart[\s_-]*rates?|pulse|bpm|blood\s+(?:pressure|sugar|tests?)|"
    r"medications?|medicines?|medical|prescriptions?|pills?|doses?|symptoms?|doctors?|clinics?|hospitals?|surgery|"
    r"headaches?|migraines?|fever|illness|sick|injur(?:y|ies|ed)|therapy|therapist|vitals?|weight|calories)"
    r"(?!\w)(?!\s*\()",
    re.IGNORECASE,
)
_FAMILY = re.compile(r"\b(?:health|family|parents?|mom|mum|mother|dad|son|daughter|sibling)\b", re.IGNORECASE)


def is_health(text: str) -> bool:
    return bool(_PATTERN.search(text or ""))


def denied(text: str) -> bool:
    """Health wording or a family word: the text is refused and never written."""
    return is_health(text) or bool(_FAMILY.search(text or ""))
