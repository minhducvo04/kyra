"""AnthropicLLM.respond() must stream: the SDK refuses non-streaming calls above ~21k max_tokens."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion.llm import TRUNCATION_MARKER, AnthropicLLM, Message


class _FakeStreamingClient:
    """Records whether respond() went through messages.stream() or messages.create()."""

    def __init__(self, stop_reason: str = "end_turn", text: str = "done"):
        self.messages = self
        self.stream_kwargs = None
        self.create_called = False
        self._final = SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=text)],
        )

    def create(self, **kwargs):
        self.create_called = True
        raise AssertionError("respond() must not use the non-streaming create() path")

    @contextmanager
    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        yield SimpleNamespace(get_final_message=lambda: self._final)


def test_respond_uses_streaming_with_full_budget():
    client = _FakeStreamingClient(text="hello")
    llm = AnthropicLLM(client, max_tokens=40000)
    reply = llm.respond("sys", [Message("user", "a"), Message("assistant", "b")], "hi")
    assert reply == "hello"
    assert not client.create_called
    assert client.stream_kwargs["max_tokens"] == 40000
    assert client.stream_kwargs["system"] == "sys"
    assert client.stream_kwargs["messages"] == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "hi"},
    ]


def test_respond_marks_truncation_on_max_tokens():
    client = _FakeStreamingClient(stop_reason="max_tokens", text="\\documentclass{article} partial")
    reply = AnthropicLLM(client, max_tokens=40000).respond("sys", [], "hi")
    assert reply.endswith(TRUNCATION_MARKER)
    assert reply.startswith("\\documentclass{article} partial")


@pytest.mark.parametrize("budget", [500, 40000])
def test_budget_is_passed_through_unchanged(budget):
    client = _FakeStreamingClient()
    AnthropicLLM(client, max_tokens=budget).respond("sys", [], "hi")
    assert client.stream_kwargs["max_tokens"] == budget
