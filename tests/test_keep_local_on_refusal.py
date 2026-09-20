"""Plan step A4, the policy half: a turn the gate will not release is kept on this Mac, not thrown away.

Under `enforce`, a text turn whose label has no release grant (a health sentence, say) used to end in "Outbound
release refused". The local model is always a permitted destination, so the turn is answered locally and the reply
says it stayed here and why. A tool turn has no local executor, so it says so and does nothing. A configured secret
is different: the turn is refused outright and nothing is answered or recorded, because the secret should not enter
the memory store either.
"""
import pytest

from companion.outbound import ReleaseRefused
from companion.privacy import PrivacyClass
from companion.router import RoutingDecision, route_and_answer_verbose
from companion.tools import ToolRegistry

C = PrivacyClass


class _Router:
    def __init__(self, path, backend="claude"):
        self.decision = RoutingDecision(path=path, backend=backend, reason="test")

    def route(self, _text):
        return self.decision


class _Backend:
    def __init__(self, name):
        self.name = name


BACKENDS = {"claude": _Backend("claude"), "local": _Backend("local"), "voice": _Backend("voice")}


class _Conversation:
    def __init__(self, refuse_with):
        self.llm, self.refuse_with, self.awaiting_approval = None, refuse_with, None
        self.turns, self.recorded, self.kwargs = [], [], []

    def handle_turn(self, user_input, on_token=None, register=None, **kwargs):
        name = self.llm.name
        self.turns.append(("text", name))
        self.kwargs.append(kwargs)
        if name in ("claude", "voice"):
            raise self.refuse_with
        return "a local answer"

    def handle_turn_with_tools(self, user_input, tool_backend, registry, **kwargs):
        self.turns.append(("tool", tool_backend.name))
        raise self.refuse_with

    def _record_turn(self, user_input, reply):
        self.recorded.append((user_input, reply))


def test_a_refused_text_turn_is_answered_locally_and_says_why():
    conversation = _Conversation(ReleaseRefused(frozenset({C.health})))
    reply, decision = route_and_answer_verbose("my resting heart rate is up", conversation, _Router("text"), BACKENDS, ToolRegistry([]))
    assert conversation.turns == [("text", "claude"), ("text", "local")]
    assert reply.endswith("a local answer") and "stayed on this Mac" in reply and "health" in reply
    assert (decision.backend, decision.fallback_from, decision.error) == ("local", "claude", "ReleaseRefused")
    assert "yourself" in conversation.kwargs[1]["system_note"].lower()


def test_a_spoken_refused_turn_also_stays_local():
    conversation = _Conversation(ReleaseRefused(frozenset({C.health})))
    reply, decision = route_and_answer_verbose("my resting heart rate is up", conversation, _Router("text"), BACKENDS,
                                               ToolRegistry([]), register="voice")
    assert conversation.turns == [("text", "voice"), ("text", "local")] and decision.backend == "local"


def test_a_refused_tool_turn_says_so_and_does_nothing():
    conversation = _Conversation(ReleaseRefused(frozenset({C.health, C.unknown})))
    reply, decision = route_and_answer_verbose("log my blood pressure", conversation, _Router("tool"), BACKENDS, ToolRegistry([]))
    assert conversation.turns == [("tool", "claude")]
    assert "stayed on this Mac" in reply and "tools" in reply.lower() and "health" in reply
    assert decision.error == "ReleaseRefused" and conversation.recorded == [("log my blood pressure", reply)]


def test_a_refused_tool_turn_names_what_already_ran():
    refusal = ReleaseRefused(frozenset({C.health}))
    refusal.completed_tools = ("add_reminder",)
    conversation = _Conversation(refusal)
    reply, _ = route_and_answer_verbose("remind me, then check my heart rate", conversation, _Router("tool"), BACKENDS, ToolRegistry([]))
    assert "add_reminder" in reply and "already" in reply.lower()


def test_a_configured_secret_is_refused_outright_and_nothing_is_answered_or_recorded():
    conversation = _Conversation(ReleaseRefused(secret=True))
    with pytest.raises(ReleaseRefused):
        route_and_answer_verbose("here is my key ...", conversation, _Router("text"), BACKENDS, ToolRegistry([]))
    assert conversation.turns == [("text", "claude")] and conversation.recorded == []


def test_a_turn_already_routed_local_is_untouched():
    conversation = _Conversation(ReleaseRefused(frozenset({C.health})))
    reply, decision = route_and_answer_verbose("hi", conversation, _Router("text", "local"), BACKENDS, ToolRegistry([]))
    assert reply == "a local answer" and decision.fallback_from is None
