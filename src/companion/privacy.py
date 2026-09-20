"""Typed prompt fragments and durable labels; no release or routing policy."""
import json
from dataclasses import dataclass
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


class Source(str, Enum):  # noqa: UP042 - match PrivacyClass and the shared contract
    system = "system"
    memory_note = "memory_note"
    retrieved_memory = "retrieved_memory"
    history = "history"
    user_input = "user_input"
    tool_result = "tool_result"


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


@dataclass(frozen=True)
class ContextBlock:
    content: str
    role: str
    source: Source
    source_ref: str
    tier: Tier
    classes: frozenset[PrivacyClass]

    def __post_init__(self):
        _validate_label(self.tier, self.classes)
        if not isinstance(self.content, str):
            raise ValueError("content must be text")
        if self.role not in ("system", "user", "assistant"):
            raise ValueError("invalid context role")
        if not isinstance(self.source, Source):
            raise ValueError("source must be a Source")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise ValueError("source_ref must be nonblank text")


def combine(labels) -> tuple[Tier, frozenset[PrivacyClass]]:
    labels = list(labels)
    if not labels:
        raise ValueError("cannot combine an empty label set")
    for tier, classes in labels:
        _validate_label(tier, classes)
    return max(tier for tier, _ in labels), frozenset().union(*(classes for _, classes in labels))


def encode_labels(block: ContextBlock) -> dict:
    return {
        "privacy_version": 1,
        "privacy_tier": int(block.tier),
        "privacy_classes": json.dumps(sorted(c.value for c in block.classes)),
        "privacy_source": block.source.value,
        "privacy_source_ref": block.source_ref,
    }


def decode_labels(metadata: dict | None) -> tuple[Tier, frozenset[PrivacyClass], str]:
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError("memory metadata must be a dictionary")
    keys = {key for key in metadata if isinstance(key, str) and key.startswith("privacy_")}
    if not keys:
        return *UNKNOWN, "legacy:untagged"
    if keys != {"privacy_version", "privacy_tier", "privacy_classes", "privacy_source", "privacy_source_ref"}:
        raise ValueError("incomplete or unsupported privacy metadata")
    try:
        if type(metadata["privacy_version"]) is not int or metadata["privacy_version"] != 1:
            raise ValueError("unsupported privacy version")
        if type(metadata["privacy_tier"]) is not int:
            raise ValueError("invalid stored tier")
        if not isinstance(metadata["privacy_classes"], str) or not isinstance(metadata["privacy_source"], str):
            raise ValueError("invalid stored labels")
        values = json.loads(metadata["privacy_classes"])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("invalid stored classes")
        block = ContextBlock(
            content="", role="system", source=Source(metadata["privacy_source"]),
            source_ref=metadata["privacy_source_ref"], tier=Tier(metadata["privacy_tier"]),
            classes=frozenset(PrivacyClass(value) for value in values),
        )
    except (TypeError, ValueError):
        raise ValueError("invalid privacy metadata") from None
    return block.tier, block.classes, block.source_ref


@dataclass(frozen=True)
class AssembledContext:
    blocks: tuple[ContextBlock, ...]

    def __post_init__(self):
        if not isinstance(self.blocks, tuple) or not self.blocks or any(
            not isinstance(block, ContextBlock) for block in self.blocks
        ):
            raise ValueError("context requires a nonempty tuple of ContextBlock values")

    def labels(self) -> tuple[Tier, frozenset[PrivacyClass]]:
        return combine((block.tier, block.classes) for block in self.blocks)

    def to_legacy(self):
        """Join verbatim system fragments; labels never become prompt text."""
        from companion.llm import Message

        system, history, inputs = [], [], []
        for block in self.blocks:
            if block.role == "system":
                system.append(block.content)
            elif block.source is Source.history:
                history.append(Message(block.role, block.content, context_block=block))
            elif block.source is Source.user_input and block.role == "user":
                inputs.append(block.content)
            else:
                raise ValueError("unsupported block in legacy conversation context")
        if len(inputs) != 1:
            raise ValueError("context requires exactly one current user input")
        return "".join(system), history, inputs[0]
