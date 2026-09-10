"""Suggestions cannot turn into actions through the outer tool loop."""
from types import SimpleNamespace

import pytest

from companion.initiatives import SuggestInitiativesTool
from companion.llm import AnthropicLLM
from tests.fakes import ScriptedLLM
from tests.test_initiatives import BUNDLE, _reply, _Source


def _call(name):
    return SimpleNamespace(type="tool_use", name=name, input={}, id=name)


class _Client:
    def __init__(self, responses):
        self.messages = self
        self.responses = iter(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return next(self.responses)


class _Registry:
    def __init__(self, marker, suggestions):
        self.marker = marker
        self.suggestions = suggestions

    def schemas(self):
        return []

    def run(self, name, **kwargs):
        if name == "suggest_initiatives":
            return self.suggestions.run(**kwargs)
        self.marker.write_text("action executed")
        return {"done": True}


@pytest.mark.parametrize("names", [
    ["suggest_initiatives"],
    ["do_work", "suggest_initiatives"],
    ["suggest_initiatives", "do_work"],
    ["suggest_initiatives", "suggest_initiatives"],
])
def test_suggestions_prevent_sibling_and_followup_actions(tmp_path, names):
    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call(n) for n in names]),
        SimpleNamespace(stop_reason="tool_use", content=[_call("do_work")]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
    ])
    proposals = ScriptedLLM([_reply(("Review the experiment", ["e1"]))])
    suggestions = SuggestInitiativesTool([_Source(BUNDLE)], proposals)
    marker = tmp_path / "must-not-exist"
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "any ideas?", _Registry(marker, suggestions))
    assert not marker.exists()
    assert client.calls == 1
    assert len(proposals.calls) == 1
    assert "Review the experiment" in reply and "write it down" in reply
    assert "fact 1" in reply and "2026-09-09" in reply and "reminders" in reply


def test_other_tool_turns_still_complete_normally(tmp_path):
    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call("do_work")]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
    ])
    marker = tmp_path / "explicit-action"
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "do it", _Registry(marker, None))
    assert reply == "done" and marker.read_text() == "action executed"
    assert client.calls == 2


def test_empty_suggestions_end_the_turn_without_a_proposal_call(tmp_path):
    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call("suggest_initiatives")]),
    ])
    proposals = ScriptedLLM([])
    suggestions = SuggestInitiativesTool([_Source([])], proposals)
    reply = AnthropicLLM(client).respond_with_tools(
        "sys", [], "any ideas?", _Registry(tmp_path / "must-not-exist", suggestions),
    )
    assert "evidence" in reply.lower() and proposals.calls == []


def test_a_failed_source_cannot_fall_through_to_an_action(tmp_path):
    class BrokenSource(_Source):
        def collect(self):
            raise ValueError("bad stored data")

    client = _Client([
        SimpleNamespace(stop_reason="tool_use", content=[_call("do_work"), _call("suggest_initiatives")]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
    ])
    proposals = ScriptedLLM([])
    marker = tmp_path / "must-not-exist"
    suggestions = SuggestInitiativesTool([BrokenSource([])], proposals)
    reply = AnthropicLLM(client).respond_with_tools("sys", [], "any ideas?", _Registry(marker, suggestions))
    assert not marker.exists() and proposals.calls == []
    assert "could not" in reply.lower()
