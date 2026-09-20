"""Local, additive labels for typed and transcribed conversation input.

This is a lexicon, not a classifier: a missed health phrase stays conversation.
Contact names stay local and are refreshed periodically when labelling input.
"""
import logging
import re
import time
from collections.abc import Callable, Iterable
from threading import Lock

from companion.privacy import PrivacyClass, Tier

logger = logging.getLogger(__name__)

_HEALTH_PATTERNS = (
    # Vitals.
    re.compile(r"\b(?:heart\s+rate|hrv|blood\s+pressure|pulse|oxygen)\b", re.IGNORECASE),
    # Sleep state; exclude the obvious function-call spelling.
    re.compile(r"\b(?:slept|insomnia|sleep\b(?!\s*\())\b", re.IGNORECASE),
    # Medication and dosage.
    re.compile(r"\b(?:mg|ibuprofen|prescriptions?|doses?|dosage|medications?|meds|pills?|tablets?|vitamins?|"
               r"antibiotics?|painkillers?|inhaler|pharmacy)\b", re.IGNORECASE),
    # Clinicians, procedures and symptoms.
    re.compile(r"\b(?:doctors?|dentist|therapists?|nurse|patients?|hospital|clinic|discharged|surgery|"
               r"blood\s+tests?|diagnos(?:is|ed)|therapy|symptoms?|fever|cough|flu|infection|allerg(?:y|ies|ic)|"
               r"injur(?:y|ed)|sick|nausea)\b", re.IGNORECASE),
    # Mental state.
    re.compile(r"\b(?:anxious|anxiety|depressed|depression|panic)\b", re.IGNORECASE),
    # Alcohol and caffeine intake (conservative even without an intake verb).
    re.compile(r"\b(?:alcohol|wine|beer|liquor|caffeine|coffee|espresso|tea)\b", re.IGNORECASE),
    # Pain words.
    re.compile(r"\b(?:pain|painful|aches?|aching|headaches?|migraine|sore|hurts?)\b", re.IGNORECASE),
)


class InputLabeller:
    def __init__(self, third_party_names: Callable[[], Iterable[str]], *, clock=time.monotonic, refresh_seconds=30):
        self._loader = third_party_names
        self._clock, self._refresh_seconds = clock, refresh_seconds
        self._refresh_at = 0.0
        self._name_patterns: tuple[re.Pattern, ...] | None = None
        self._names_lock = Lock()

    def _names(self) -> tuple[re.Pattern, ...]:
        with self._names_lock:
            now = self._clock()
            if self._name_patterns is None or now >= self._refresh_at:
                try:
                    names = set()
                    for name in self._loader():
                        parts = name.split()
                        if not parts:
                            continue
                        names.add(" ".join(parts))
                        if len(parts[-1]) >= 5:
                            names.add(parts[-1])
                    patterns = tuple(
                        re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, name.split())) + r"(?!\w)", re.IGNORECASE)
                        for name in sorted(names)
                    )
                except Exception as exc:
                    logger.warning("Input contact names unavailable: %s", type(exc).__name__)
                    patterns = self._name_patterns or ()
                self._name_patterns = patterns
                self._refresh_at = now + self._refresh_seconds
            return self._name_patterns

    def label(self, text: str) -> tuple[Tier, frozenset[PrivacyClass]]:
        classes = {PrivacyClass.conversation}
        if any(pattern.search(text) for pattern in _HEALTH_PATTERNS):
            classes.add(PrivacyClass.health)
        if any(pattern.search(text) for pattern in self._names()):
            classes.add(PrivacyClass.third_party)
        return Tier.T2, frozenset(classes)


def default_input_labeller() -> InputLabeller:
    def names() -> Iterable[str]:
        from companion.outreach import OutreachStore

        return (contact.name for contact in OutreachStore().list())

    return InputLabeller(third_party_names=names)
