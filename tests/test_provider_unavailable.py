"""Plan step A4, the reachable half: when Claude cannot be reached, Kyra says so and stays honest.

Today a network failure inside `AnthropicLLM` is an unhandled exception: the HUD paints OFFLINE, chat and voice exit.
Rules under test (Codex critique C14): a TEXT turn falls back to the local model, because local is always a
permitted destination, and the reply says which brain answered and why; a TOOL turn has no second executor, so it
answers "unavailable" and never runs tools on a different model; a tool turn that failed AFTER a side effect names
what already ran and is never replayed; the fallback outcome is on the routing decision so the log and the HUD badge
show it; nothing here retries in a loop.
"""
import pytest

from companion.llm import ProviderUnavailable
from companion.router import RoutingDecision, route_and_answer_verbose
from companion.tools import Tool, ToolRegistry


class _Router:
    def __init__(self, path, backend="claude"):
        self.decision = RoutingDecision(path=path, backend=backend, reason="test")

    def route(self, _text):
        return self.decision


class _Conversation:
    def __init__(self, fail_with=None, fail_tools_with=None):
        self.llm = None
        self.fail_with, self.fail_tools_with = fail_with, fail_tools_with
        self.turns: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str]] = []
        self.kwargs: list[dict] = []
        self.inputs: list[str] = []
        self.awaiting_approval = None

    def handle_turn(self, user_input, on_token=None, register=None, **kwargs):
        name = getattr(self.llm, "name", "?")
        self.turns.append(("text", name))
        self.kwargs.append(kwargs)
        self.inputs.append(user_input)
        if name == "claude" and self.fail_with is not None:
            raise self.fail_with
        return f"answered by {name}"

    def handle_turn_with_tools(self, user_input, tool_backend, registry):
        self.turns.append(("tool", getattr(tool_backend, "name", "?")))
        if self.fail_tools_with is not None:
            raise self.fail_tools_with
        return "tool reply"

    def _record_turn(self, user_input, reply):
        self.recorded.append((user_input, reply))


class _Backend:
    def __init__(self, name):
        self.name = name


BACKENDS = {"claude": _Backend("claude"), "local": _Backend("local")}


def test_a_text_turn_falls_back_to_local_once_and_says_so():
    conversation = _Conversation(fail_with=ProviderUnavailable("connection refused"))
    reply, decision = route_and_answer_verbose("explain raft", conversation, _Router("text"), BACKENDS, ToolRegistry([]))
    assert conversation.turns == [("text", "claude"), ("text", "local")]          # one fallback, no retry loop
    assert reply.endswith("answered by local") and "Claude is unreachable" in reply
    assert (decision.backend, decision.fallback_from, decision.error) == ("local", "claude", "ProviderUnavailable")
    assert decision.as_log_fields()["fallback_from"] == "claude"


def test_a_turn_already_routed_to_local_is_untouched():
    conversation = _Conversation(fail_with=ProviderUnavailable("x"))
    reply, decision = route_and_answer_verbose("hi", conversation, _Router("text", "local"), BACKENDS, ToolRegistry([]))
    assert reply == "answered by local" and decision.fallback_from is None


def test_a_tool_turn_is_not_handed_to_another_model():
    conversation = _Conversation(fail_tools_with=ProviderUnavailable("timeout"))
    reply, decision = route_and_answer_verbose("remind me at nine", conversation, _Router("tool"), BACKENDS, ToolRegistry([]))
    assert conversation.turns == [("tool", "claude")]
    assert "unreachable" in reply.lower() and "tool" in reply.lower()
    assert decision.error == "ProviderUnavailable" and decision.fallback_from is None
    assert conversation.recorded == [("remind me at nine", reply)]                # the history shows what happened


def test_a_failure_after_a_side_effect_names_what_ran_and_is_never_replayed():
    ran = ProviderUnavailable("dropped on round 2", completed_tools=("add_reminder",))
    conversation = _Conversation(fail_tools_with=ran)
    reply, _ = route_and_answer_verbose("remind me and tell me the news", conversation, _Router("tool"), BACKENDS, ToolRegistry([]))
    assert "add_reminder" in reply and "already" in reply.lower()
    assert conversation.turns == [("tool", "claude")]


def test_other_exceptions_are_not_swallowed():
    conversation = _Conversation(fail_with=ValueError("a bug"))
    with pytest.raises(ValueError):
        route_and_answer_verbose("hi", conversation, _Router("text"), BACKENDS, ToolRegistry([]))


def test_the_anthropic_backend_translates_transport_failures_only():
    import anthropic
    import httpx

    from companion.llm import AnthropicLLM

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class _Client:
        def __init__(self, exc):
            self.messages, self.exc = self, exc

        def create(self, **kwargs):
            raise self.exc

    for exc in (anthropic.APIConnectionError(request=request), anthropic.APITimeoutError(request=request)):
        with pytest.raises(ProviderUnavailable) as excinfo:
            AnthropicLLM(_Client(exc)).respond_with_tools("sys", [], "hi", ToolRegistry([]))
        assert excinfo.value.completed_tools == ()
    bad_request = anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    with pytest.raises(anthropic.BadRequestError):                                  # our bug, not an outage
        AnthropicLLM(_Client(bad_request)).respond_with_tools("sys", [], "hi", ToolRegistry([]))


def test_completed_tools_are_reported_when_a_later_round_fails():
    from types import SimpleNamespace

    import anthropic
    import httpx

    from companion.llm import AnthropicLLM

    class _Reminder(Tool):
        side_effect = True
        name = "add_reminder"
        description = "x"
        input_schema = {"type": "object", "properties": {}}

        def run(self):
            return {"ok": True}

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class _Client:
        def __init__(self):
            self.messages, self.calls = self, 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[
                    SimpleNamespace(type="tool_use", name="add_reminder", input={}, id="t1")])
            raise anthropic.APIConnectionError(request=request)

    with pytest.raises(ProviderUnavailable) as excinfo:
        AnthropicLLM(_Client()).respond_with_tools("sys", [], "remind me", ToolRegistry([_Reminder()]))
    assert excinfo.value.completed_tools == ("add_reminder",)


def test_the_local_fallback_is_told_to_answer_itself():
    """Real run, 2026-09-19: after an earlier "Claude is unreachable" reply was in the history, the local model
    answered the next fallback turn with "Claude is unreachable, so I can't answer" instead of answering. The
    fallback turn carries one system note saying who is answering and why."""
    conversation = _Conversation(fail_with=ProviderUnavailable("connection refused"))
    route_and_answer_verbose("what is a mutex", conversation, _Router("text"), BACKENDS, ToolRegistry([]))
    first, second = conversation.kwargs
    assert "system_note" not in first or first["system_note"] is None
    assert "answer" in second["system_note"].lower() and "yourself" in second["system_note"].lower()


def test_a_system_note_is_a_public_block_at_the_end_of_the_system_prompt():
    from companion.conversation import ConversationManager
    from companion.persona import KYRA
    from companion.privacy import Source, Tier
    from tests.fakes import ScriptedLLM
    from tests.test_privacy_context import _Memory, _Notes

    manager = ConversationManager(persona=KYRA, memory=_Memory(), llm=ScriptedLLM(["ok"]), memory_notes=_Notes())
    context = manager.assemble("hi", system_note="Claude is unreachable. Answer the question yourself.")
    note = [b for b in context.blocks if b.source is Source.system][-1]
    assert note.tier == Tier.T0 and "yourself" in note.content
    assert context.to_legacy()[0].rstrip().endswith("Answer the question yourself.")
    assert manager.handle_turn("hi", system_note="Answer the question yourself.") == "ok"
    assert "yourself" not in manager.assemble("again").to_legacy()[0]      # one turn only


@pytest.mark.parametrize("text, expected", [
    ("ask claude: what is a mutex?", "what is a mutex?"),
    ("Use Claude, what is a mutex?", "what is a mutex?"),
    ("what is a mutex? claude please", "what is a mutex?"),
    ("what is a mutex?", "what is a mutex?"),
])
def test_the_local_fallback_does_not_see_a_request_to_ask_the_model_that_is_down(text, expected):
    """Real run, 2026-09-19, with the real local model: the system note alone was not enough. With "ask claude:" in the
    message and an earlier outage reply in the history, it answered "Claude is unreachable, so I can't get an answer";
    with the phrase removed, same history and note, it answered the question."""
    conversation = _Conversation(fail_with=ProviderUnavailable("connection refused"))
    route_and_answer_verbose(text, conversation, _Router("text"), BACKENDS, ToolRegistry([]))
    assert conversation.inputs[0] == text and conversation.inputs[1] == expected
