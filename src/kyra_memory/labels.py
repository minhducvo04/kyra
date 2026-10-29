"""Durable privacy labels: a Tier and a set of classes travel with every memory row. No release or routing policy."""
from enum import Enum, IntEnum


class Tier(IntEnum):
    T0 = 0
    T1 = 1
    T2 = 2
    T3 = 3


class PrivacyClass(str, Enum):  # noqa: UP042 - the shared contract specifies str/Enum semantics
    conversation = "conversation"
    job_search = "job_search"
    third_party = "third_party"
    health = "health"
    ledger = "ledger"
    activity = "activity"
    intake = "intake"
    busy = "busy"
    unknown = "unknown"


UNKNOWN = (Tier.T2, frozenset({PrivacyClass.unknown}))


def _validate_label(tier: Tier, classes: frozenset[PrivacyClass]) -> None:
    if not isinstance(tier, Tier):
        raise ValueError("tier must be a Tier")
    if not isinstance(classes, frozenset) or any(not isinstance(c, PrivacyClass) for c in classes):
        raise ValueError("classes must be a frozenset of PrivacyClass values")
    if tier is Tier.T3:
        raise ValueError("T3 content cannot enter context")
    if (tier is Tier.T2) != bool(classes):
        raise ValueError("T2 requires classes; T0 and T1 cannot carry classes")


def combine(labels) -> tuple[Tier, frozenset[PrivacyClass]]:
    labels = list(labels)
    if not labels:
        raise ValueError("cannot combine an empty label set")
    for tier, classes in labels:
        _validate_label(tier, classes)
    return max(tier for tier, _ in labels), frozenset().union(*(classes for _, classes in labels))
