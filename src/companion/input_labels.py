"""Local, additive labels for typed and transcribed conversation input.

This is a lexicon, not a classifier: a missed health phrase stays conversation.
Contact names stay local and are refreshed periodically when labelling input.
"""
import logging
import re
import time
from collections.abc import Callable, Iterable
from threading import Lock

from companion import health_words
from companion.privacy import PrivacyClass, Tier

logger = logging.getLogger(__name__)

# Input-only health words on top of the shared detector (companion.health_words: sleep, vitals, medication,
# clinicians, readings). These over-trigger more than the shared list, so Home and chat memory do not use them.
_HEALTH_PATTERNS = (
    # Medication and dosage.
    re.compile(r"\b(?:mg|oxygen|ibuprofen|doses?|pills?|tablets?|vitamins?|antibiotics?|painkillers?|inhaler)\b", re.IGNORECASE),
    # Common medication generics and brands (Team labeller only; Home and chat memory use the shared list).
    re.compile(r"\b(?:lipitor|atorvastatin|crestor|rosuvastatin|simvastatin|metformin|eliquis|apixaban|xarelto|"
               r"rivaroxaban|warfarin|coumadin|ozempic|wegovy|mounjaro|semaglutide|insulin|tylenol|acetaminophen|"
               r"paracetamol|advil|motrin|aleve|naproxen|aspirin|lisinopril|losartan|amlodipine|metoprolol|atenolol|"
               r"levothyroxine|synthroid|omeprazole|prilosec|nexium|gabapentin|prednisone|amoxicillin|zoloft|"
               r"sertraline|prozac|fluoxetine|lexapro|escitalopram|xanax|adderall|ambien|melatonin|benadryl|"
               r"zyrtec|claritin)\b", re.IGNORECASE),
    # Clinicians and symptoms the shared list leaves out.
    re.compile(r"\b(?:dentist|nurse|patients?|discharged|cough|flu|infection|allerg(?:y|ies|ic)|injur(?:y|ed)|sick)\b",
               re.IGNORECASE),
    # Mental state.
    re.compile(r"\b(?:anxious|anxiety|depressed|depression|panic)\b", re.IGNORECASE),
    # Alcohol and caffeine intake (conservative even without an intake verb). A "coffee chat" is a meeting,
    # not intake.
    re.compile(r"\b(?:alcohol|wine|beer|liquor|caffeine|coffees?(?![\s-]+chats?\b)|espresso|tea)\b", re.IGNORECASE),
    # Pain words.
    re.compile(r"\b(?:pain|painful|aches?|aching|sore|hurts?)\b", re.IGNORECASE),
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
        if health_words.is_health(text) or any(pattern.search(text) for pattern in _HEALTH_PATTERNS):
            classes.add(PrivacyClass.health)
        if any(pattern.search(text) for pattern in self._names()):
            classes.add(PrivacyClass.third_party)
        return Tier.T2, frozenset(classes)


def default_input_labeller() -> InputLabeller:
    def names() -> Iterable[str]:
        return ()  # public build stand-in: the contact store is private

    return InputLabeller(third_party_names=names)
