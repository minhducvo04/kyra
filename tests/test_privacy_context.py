"""Stage A1a of docs/plans/2026-09-18-whole-system-phase-1-final.md: every block of a prompt carries a tier, a set
of privacy classes and where it came from, and the labels survive the memory round-trip.

This slice changes no behaviour: no gate reads the labels yet (A3), routing does not use them yet (A4), and
`to_legacy()` must reproduce today's prompt text exactly. What it pins is the vocabulary and the three places a label
can be lost today: `_record_turn` stores only a role, `_build_system` throws retrieved metadata away, and old
memories have no label at all (Codex review R04, R06).

Rules under test: T2 needs at least one class, T0 and T1 carry none, T3 is refused outright; combining content takes
the maximum tier and the union of classes, and nothing lowers either; content with no label reads as T2/{unknown} at
the reader boundary and is never relabelled; a malformed label raises instead of being guessed.
"""
import json
from dataclasses import FrozenInstanceError

import pytest

from companion.conversation import ConversationManager
from companion.llm import Message
from companion.memory import MemoryRecord, MemoryStore
from companion.memory_notes import MemoryNotesStore
from companion.persona import KYRA
from companion.privacy import (
    AssembledContext,
    ContextBlock,
    PrivacyClass,
    Source,
    Tier,
    combine,
    decode_labels,
    encode_labels,
)
from tests.fakes import ScriptedLLM

HEALTH_CHAT = (Tier.T2, frozenset({PrivacyClass.conversation, PrivacyClass.health}))
PLAIN_CHAT = (Tier.T2, frozenset({PrivacyClass.conversation}))
UNKNOWN = (Tier.T2, frozenset({PrivacyClass.unknown}))


def _block(content="hello", role="user", source=Source.user_input, ref="turn:1", tier=Tier.T1, classes=frozenset()):
    return ContextBlock(content=content, role=role, source=source, source_ref=ref, tier=tier, classes=classes)


# --- the vocabulary -------------------------------------------------------------------------------------


def test_the_enums_are_the_ones_the_plan_names():
    assert [t.name for t in Tier] == ["T0", "T1", "T2", "T3"] and Tier.T2 > Tier.T1
    assert {c.value for c in PrivacyClass} == {
        "conversation", "job_search", "third_party", "health", "ledger", "activity", "intake", "busy", "unknown",
    }
    assert {s.value for s in Source} == {"system", "memory_note", "retrieved_memory", "history", "user_input", "tool_result"}


def test_a_block_is_frozen_and_every_field_is_required():
    block = _block()
    with pytest.raises(FrozenInstanceError):
        block.tier = Tier.T0
    with pytest.raises(TypeError):
        ContextBlock(content="x", role="user")  # no permissive defaults


@pytest.mark.parametrize("kwargs", [
    {"tier": Tier.T2, "classes": frozenset()},                           # T2 must say why
    {"tier": Tier.T1, "classes": frozenset({PrivacyClass.health})},      # T1 has no class
    {"tier": Tier.T0, "classes": frozenset({PrivacyClass.conversation})},
    {"tier": Tier.T3, "classes": frozenset()},                           # secrets never become a block
    {"tier": 2, "classes": frozenset({PrivacyClass.health})},            # a bare int is not a tier
    {"tier": Tier.T2, "classes": {"health"}},                            # strings are not classes
    {"ref": ""}, {"ref": "   "},                                         # provenance is not optional
    {"role": "tool"}, {"source": "user_input"},
])
def test_invalid_labels_raise(kwargs):
    with pytest.raises((ValueError, TypeError)):
        _block(**kwargs)


def test_combining_takes_the_maximum_tier_and_the_union_and_nothing_lowers_them():
    assert combine([(Tier.T0, frozenset()), (Tier.T1, frozenset())]) == (Tier.T1, frozenset())
    assert combine([PLAIN_CHAT, (Tier.T1, frozenset()), HEALTH_CHAT]) == HEALTH_CHAT
    assert combine([(Tier.T0, frozenset()), UNKNOWN]) == UNKNOWN
    with pytest.raises(ValueError):
        combine([])


def test_labels_round_trip_through_flat_metadata_and_legacy_reads_as_unknown():
    block = _block(tier=Tier.T2, classes=HEALTH_CHAT[1], ref="turn:42")
    metadata = encode_labels(block)
    assert metadata == {
        "privacy_version": 1, "privacy_tier": 2, "privacy_classes": json.dumps(["conversation", "health"]),
        "privacy_source": "user_input", "privacy_source_ref": "turn:42",
    }
    assert all(isinstance(v, (str, int)) for v in metadata.values())  # Chroma metadata cannot hold a list
    assert decode_labels({**metadata, "role": "user", "timestamp": 1.0}) == (Tier.T2, HEALTH_CHAT[1], "turn:42")
    assert decode_labels({"role": "user", "timestamp": 1.0})[:2] == UNKNOWN  # a memory from before labels existed


@pytest.mark.parametrize("broken", [
    {"privacy_version": 1, "privacy_tier": 2},                                                   # partial
    {"privacy_version": 2, "privacy_tier": 2, "privacy_classes": "[]", "privacy_source": "history", "privacy_source_ref": "x"},
    {"privacy_version": 1, "privacy_tier": 9, "privacy_classes": "[]", "privacy_source": "history", "privacy_source_ref": "x"},
    {"privacy_version": 1, "privacy_tier": 2, "privacy_classes": '["gossip"]', "privacy_source": "history", "privacy_source_ref": "x"},
    {"privacy_version": 1, "privacy_tier": 2, "privacy_classes": "not json", "privacy_source": "history", "privacy_source_ref": "x"},
])
def test_a_malformed_label_raises_instead_of_being_guessed(broken):
    with pytest.raises(ValueError):
        decode_labels(broken)


# --- assembly and the memory round-trip -------------------------------------------------------------------


class _Memory(MemoryStore):
    def __init__(self, records=()):
        self.records = list(records)
        self.added: list[tuple[str, dict]] = []
        self.retrievals = 0

    def add(self, text, metadata=None):
        self.added.append((text, dict(metadata or {})))

    def retrieve(self, query, k=5):
        self.retrievals += 1
        return list(self.records)


class _Notes(MemoryNotesStore):
    def __init__(self, text="- Duc prefers short answers."):
        self.text = text

    def render(self):
        return self.text

    def add(self, category, note):
        raise NotImplementedError

    def list_notes(self):
        return []

    def delete(self, category, text):
        return False


def _manager(memory=None, llm=None, notes=None):
    return ConversationManager(persona=KYRA, memory=memory or _Memory(), llm=llm or ScriptedLLM(["ok"]), memory_notes=notes or _Notes())


def test_to_legacy_reproduces_today_s_prompt_exactly():
    old = MemoryRecord("Duc said: I like tea", {"role": "user", "timestamp": 1.0})
    manager = _manager(memory=_Memory([old]))
    manager.history.append(Message(role="user", content="earlier"))
    manager.history.append(Message(role="assistant", content="earlier reply"))
    for register in (None, "voice"):
        context = manager.assemble("what do I like?", register=register)
        assert isinstance(context, AssembledContext)
        system, history, user_input = context.to_legacy()
        assert user_input == "what do I like?"
        assert [(m.role, m.content) for m in history] == [("user", "earlier"), ("assistant", "earlier reply")]
        expected = manager._build_system("what do I like?", register)
        # The clock line can tick between the two calls; everything else must be byte-identical.
        strip = lambda text: "\n".join(line for line in text.splitlines() if not line.startswith("Current date and time:"))  # noqa: E731
        assert strip(system) == strip(expected)


def test_each_source_is_its_own_block_with_the_right_label():
    labelled = MemoryRecord("Duc said: my resting heart rate is up", {
        "role": "user", "timestamp": 2.0, **encode_labels(_block(tier=Tier.T2, classes=HEALTH_CHAT[1], ref="turn:7")),
    })
    legacy = MemoryRecord("Duc said: I like tea", {"role": "user", "timestamp": 1.0})
    manager = _manager(memory=_Memory([labelled, legacy]))
    manager.history.append(Message(role="user", content="earlier"))
    context = manager.assemble("hello", input_label=PLAIN_CHAT)
    by_source = {}
    for block in context.blocks:
        by_source.setdefault(block.source, []).append(block)
    persona = by_source[Source.system][0]
    assert persona.tier == Tier.T0 and KYRA.system_prompt() in persona.content
    assert any(b.tier == Tier.T1 and b.content.startswith("Current date and time:") for b in by_source[Source.system])
    (notes,) = by_source[Source.memory_note]
    assert (notes.tier, notes.classes) == UNKNOWN  # A1b gives notes a reviewed label; until then they are unknown
    retrieved = by_source[Source.retrieved_memory]
    assert [(b.tier, b.classes) for b in retrieved] == [HEALTH_CHAT, UNKNOWN]
    assert retrieved[0].source_ref == "turn:7"  # retrieval changes the source, not the origin
    assert [(b.tier, b.classes) for b in by_source[Source.history]] == [UNKNOWN]  # a bare Message has no label
    (current,) = by_source[Source.user_input]
    assert (current.content, current.tier, current.classes) == ("hello", *PLAIN_CHAT)
    assert manager.memory.retrievals == 1  # assemble retrieves once and does nothing else
    assert manager.history == [Message(role="user", content="earlier")] and manager.memory.added == []


def test_user_input_without_a_trusted_label_is_unknown():
    (current,) = [b for b in _manager().assemble("hi").blocks if b.source is Source.user_input]
    assert (current.tier, current.classes) == UNKNOWN


def test_a_turn_records_labels_on_both_sides_and_the_reply_inherits_what_it_saw():
    memory = _Memory([MemoryRecord("Duc said: my resting heart rate is up", {
        "role": "user", "timestamp": 2.0, **encode_labels(_block(tier=Tier.T2, classes=HEALTH_CHAT[1], ref="turn:7")),
    })])
    manager = _manager(memory=memory, llm=ScriptedLLM(["noted"]))
    assert manager.handle_turn("so what should I do", input_label=PLAIN_CHAT) == "noted"
    (user_text, user_meta), (reply_text, reply_meta) = memory.added
    assert user_text == "Duc said: so what should I do" and user_meta["role"] == "user"
    assert decode_labels(user_meta)[:2] == PLAIN_CHAT
    # The reply saw a health memory and unknown notes, so it is health, conversation and unknown, never less.
    tier, classes, _ = decode_labels(reply_meta)
    assert tier == Tier.T2 and {PrivacyClass.health, PrivacyClass.conversation, PrivacyClass.unknown} <= classes
    user_message, assistant_message = manager.history
    assert (user_message.role, user_message.content) == ("user", "so what should I do")
    assert user_message.context_block.classes == PLAIN_CHAT[1]
    assert PrivacyClass.health in assistant_message.context_block.classes


def test_existing_callers_keep_working_unchanged():
    memory = _Memory()
    manager = _manager(memory=memory, llm=ScriptedLLM(["first", "second"]))
    assert manager.handle_turn("hello") == "first"           # positional, no label, as every front door calls it
    assert manager.handle_turn("again", None, "voice") == "second"
    assert Message("user", "two-argument constructor still works").context_block is None
    assert [decode_labels(meta)[:2] for _, meta in memory.added[:1]] == [UNKNOWN]
    assert all("role" in meta for _, meta in memory.added)


def test_a_mismatched_history_label_raises():
    manager = _manager()
    wrong = _block(content="something else", role="user", source=Source.history, ref="turn:1", tier=Tier.T2, classes=PLAIN_CHAT[1])
    manager.history.append(Message(role="user", content="earlier", context_block=wrong))
    with pytest.raises(ValueError):
        manager.assemble("hi")
