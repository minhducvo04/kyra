"""Review finding A13-06: every chat input was labelled T2/{unknown}, because no front door supplied a label, so the
dry-run report said "unknown" about everything and an enforcing gate could never pass a greeting.

This slice labels what the owner types or says, locally and conservatively: it is always `conversation`; `health` is
ADDED when a local lexicon hits; `third_party` is ADDED when a name from the owner's own outreach contacts appears
(the names never leave the machine and never enter a tracked file: tests use the fictional contact). Detection only
ever adds a class. It is a lexicon, not a classifier, and the log says so: a miss means a health sentence is labelled
plain conversation, which is what happens to every sentence today.
"""
import pytest

from companion.input_labels import InputLabeller
from companion.privacy import PrivacyClass, Tier

C = PrivacyClass


def _classes(text, names=()):
    tier, classes = InputLabeller(third_party_names=lambda: list(names)).label(text)
    assert tier == Tier.T2
    return classes


@pytest.mark.parametrize("text", ["hello", "what's the weather like", "explain raft in one sentence", "set a reminder for 9"])
def test_ordinary_chat_is_conversation_and_nothing_else(text):
    assert _classes(text) == {C.conversation}


@pytest.mark.parametrize("text", [
    "my resting heart rate has been high this week", "I slept four hours and my HRV tanked",
    "took 200 mg of ibuprofen for the headache", "my doctor wants a blood test", "I've been feeling anxious lately",
    "fictional example: blood pressure is recorded", "I had two glasses of wine last night", "fictional example: surgery is discussed",
    # Misses found by hand on the first build, 2026-09-19:
    "remind me to take my meds at 9", "I ran out of pills", "the patient was discharged from the hospital",
    "book the dentist and the pharmacy run", "my therapist moved the session", "I think I have a fever and a cough",
])
def test_health_shaped_sentences_add_health(text):
    assert _classes(text) == {C.conversation, C.health}


@pytest.mark.parametrize("text", [
    "the heart of the algorithm is a heap", "this bug is a real headache... kidding, it's fine", "sleep(5) blocks the thread",
    "the patient pattern in this codebase", "a healthy test suite",
])
def test_the_lexicon_is_allowed_to_over_trigger_but_the_obvious_technical_uses_do_not(text):
    classes = _classes(text)
    assert C.conversation in classes and classes <= {C.conversation, C.health}
    if "sleep(5)" in text or "heap" in text:
        assert classes == {C.conversation}


def test_a_known_contact_s_name_adds_third_party_case_insensitively_and_by_whole_word():
    names = ["Alex Rivera"]
    assert _classes("draft a note to alex rivera at Northwind", names) == {C.conversation, C.third_party}
    assert _classes("what did Rivera say?", names) == {C.conversation, C.third_party}       # a distinctive surname alone
    assert _classes("the river is high", names) == {C.conversation}
    assert _classes("alexander the great", names) == {C.conversation}


def test_names_are_loaded_lazily_once_and_a_failing_store_costs_nothing_but_the_class():
    calls = []

    def loader():
        calls.append(1)
        return ["Alex Rivera"]

    labeller = InputLabeller(third_party_names=loader)
    assert calls == []
    labeller.label("hi"), labeller.label("hello again")
    assert calls == [1]

    def broken():
        raise RuntimeError("outreach.db is locked")

    assert InputLabeller(third_party_names=broken).label("note to Alex Rivera")[1] == {C.conversation}


def test_the_labeller_never_returns_unknown_never_lowers_and_never_raises_on_odd_input():
    for text in ("", "   ", "🙂", "a" * 20000):
        tier, classes = InputLabeller(third_party_names=lambda: []).label(text)
        assert tier == Tier.T2 and C.conversation in classes and C.unknown not in classes


def test_the_router_passes_the_label_to_both_kinds_of_turn():
    from companion.router import RoutingDecision, route_and_answer_verbose
    from companion.tools import ToolRegistry

    seen = []

    class _Conversation:
        llm = None
        awaiting_approval = None

        def handle_turn(self, user_input, on_token=None, register=None, **kwargs):
            seen.append(("text", kwargs.get("input_label")))
            return "ok"

        def handle_turn_with_tools(self, user_input, tool_backend, registry, **kwargs):
            seen.append(("tool", kwargs.get("input_label")))
            return "ok"

    class _Router:
        def __init__(self, path):
            self.path = path

        def route(self, _):
            return RoutingDecision(path=self.path, backend="claude", reason="t")

    label = (Tier.T2, frozenset({C.conversation, C.health}))
    backends = {"claude": object(), "local": object()}
    for path in ("text", "tool"):
        route_and_answer_verbose("my heart rate is up", _Conversation(), _Router(path), backends, ToolRegistry([]), input_label=label)
    assert seen == [("text", label), ("tool", label)]
    route_and_answer_verbose("hi", _Conversation(), _Router("text"), backends, ToolRegistry([]))        # callers that pass nothing still work
    assert seen[-1] == ("text", None)


def test_the_web_front_door_labels_what_it_receives(monkeypatch):
    import companion.webapp as webapp

    captured = {}

    def fake_route(message, conversation, router, backends, registry, **kwargs):
        captured.update(kwargs)
        return "ok", None

    from types import SimpleNamespace

    monkeypatch.setattr(webapp, "route_and_answer_verbose", fake_route)
    # Never the real runtime: its conversation builds the Chroma store and its embedding model, which CI lacks.
    monkeypatch.setattr(webapp, "_rt", SimpleNamespace(conversation=object()))
    monkeypatch.setattr(webapp, "_current_backend", "auto")
    webapp._answer("my resting heart rate is up")
    tier, classes = captured["input_label"]
    assert tier == Tier.T2 and {C.conversation, C.health} <= classes and C.unknown not in classes
