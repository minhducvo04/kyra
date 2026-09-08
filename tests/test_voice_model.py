"""A spoken turn may answer with a faster Claude model than a written one, and
that substitution must never reach the tool path (Claude stays the Agent
Specialist by measurement: docs/tool-calling-distill.md) or a local turn.

The switch is KYRA_VOICE_MODEL. Unset, nothing changes anywhere - the setting
is the plumbing for needs-your-input #24, and the decision to use it is Duc's.
"""
import pytest

from companion.llm import AnthropicLLM, LazyBackends
from companion.router import RoutingDecision, route_and_answer_verbose
from companion.settings import Settings
from tests.fakes import ScriptedLLM


class _Router:
    def __init__(self, path, backend):
        self._decision = RoutingDecision(path=path, backend=backend, reason="test")

    def route(self, _text):
        return self._decision


class _Conversation:
    """Records which backend answered, the way ConversationManager holds `llm`."""

    def __init__(self):
        self.llm = None
        self.tool_backend = None

    def handle_turn(self, user_input, on_token=None, register=None):
        return self.llm.respond("", [], user_input)

    def handle_turn_with_tools(self, user_input, tool_backend, registry):
        self.tool_backend = tool_backend
        return "tool reply"


def _backends(with_voice: bool):
    prebuilt = {"claude": ScriptedLLM(["written by sonnet"]), "local": ScriptedLLM(["written by local"])}
    if with_voice:
        prebuilt["voice"] = ScriptedLLM(["spoken by haiku"])
    return LazyBackends(**prebuilt)


def _answer(path, backend, register, with_voice=True):
    conversation = _Conversation()
    backends = _backends(with_voice)
    reply, _ = route_and_answer_verbose(
        "hello", conversation, _Router(path, backend), backends, registry=None, register=register
    )
    return reply, conversation, backends


def test_spoken_claude_text_turn_uses_the_voice_backend():
    reply, conversation, backends = _answer("text", "claude", register="voice")
    assert reply == "spoken by haiku"
    assert conversation.llm is backends["voice"]


def test_written_turn_never_uses_the_voice_backend():
    reply, conversation, backends = _answer("text", "claude", register=None)
    assert reply == "written by sonnet"
    assert conversation.llm is backends["claude"]


def test_local_spoken_turn_stays_local():
    reply, _, _ = _answer("text", "local", register="voice")
    assert reply == "written by local"


def test_tool_path_never_uses_the_voice_backend():
    """The one thing #24 said must not happen: a naive substitution would move
    tool-calling off Sonnet too."""
    _, conversation, backends = _answer("tool", "claude", register="voice")
    assert conversation.tool_backend is backends["claude"]


def test_without_a_voice_backend_spoken_turns_are_unchanged():
    reply, conversation, backends = _answer("text", "claude", register="voice", with_voice=False)
    assert reply == "written by sonnet"
    assert conversation.llm is backends["claude"]
    assert "voice" not in backends


def test_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("KYRA_VOICE_MODEL", raising=False)
    assert Settings(_env_file=None).voice_model == ""
    monkeypatch.setenv("KYRA_VOICE_MODEL", "claude-haiku-4-5")
    assert Settings(_env_file=None).voice_model == "claude-haiku-4-5"


def test_voice_backends_builds_the_named_model_only_when_set(monkeypatch):
    from companion import llm

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    claude = ScriptedLLM([])
    assert "voice" not in llm.voice_backends(claude, voice_model="")
    backends = llm.voice_backends(claude, voice_model="claude-haiku-4-5")
    assert backends["claude"] is claude
    voice = backends["voice"]
    assert isinstance(voice, AnthropicLLM)
    assert voice.model == "claude-haiku-4-5"


@pytest.mark.parametrize("register", [None, "voice"])
def test_lazy_backends_never_fabricates_a_voice_backend(register):
    """`backends["voice"]` on a store built without one must not fall through
    build_llm() and silently hand back a second Sonnet."""
    backends = LazyBackends(claude=ScriptedLLM([]))
    with pytest.raises(KeyError):
        backends["voice"]
