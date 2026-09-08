"""AnthropicLLM.respond() must stream: the SDK refuses non-streaming calls above ~21k max_tokens."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from companion.llm import CANCELLED_MARKER, TRUNCATION_MARKER, AnthropicLLM, Message, TurnCancelled


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


class _FakeTokenStreamClient(_FakeStreamingClient):
    """Adds the SDK's text_stream: the deltas respond() hands to on_token."""

    def __init__(self, deltas):
        super().__init__(text="".join(deltas))
        self._deltas = deltas

    @contextmanager
    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        yield SimpleNamespace(text_stream=iter(self._deltas), get_final_message=lambda: self._final)


def test_respond_streams_tokens_to_the_callback_and_still_returns_the_whole_reply():
    seen = []
    llm = AnthropicLLM(_FakeTokenStreamClient(["Hel", "lo ", "Duc"]))
    assert llm.supports_streaming
    assert llm.respond("sys", [], "hi", on_token=seen.append) == "Hello Duc"
    assert seen == ["Hel", "lo ", "Duc"]


def test_respond_without_callback_never_touches_text_stream():
    class Explodes(_FakeStreamingClient):
        @contextmanager
        def stream(self, **kwargs):
            def boom():
                raise AssertionError("text_stream must not be read when no callback is given")
            yield SimpleNamespace(text_stream=property(boom), get_final_message=lambda: self._final)

    assert AnthropicLLM(Explodes(text="ok")).respond("sys", [], "hi") == "ok"


def test_conversation_only_streams_when_the_backend_can():
    from companion.conversation import ConversationManager
    from tests.fakes import ScriptedLLM

    class NoStreamMemory:
        def retrieve(self, *_a, **_k):
            return []

        def add(self, *_a, **_k):
            pass

    class Notes:
        def render(self):
            return ""

    local = ScriptedLLM(["plain reply"])  # supports_streaming is False on the base class
    cm = ConversationManager.__new__(ConversationManager)
    cm.llm, cm.memory, cm.memory_notes, cm.history = local, NoStreamMemory(), Notes(), []
    from companion.persona import KYRA
    cm.persona = KYRA
    tokens = []
    assert cm.handle_turn("hi", on_token=tokens.append) == "plain reply"
    assert tokens == []  # never passed to a backend that cannot take it


class _FakeCancelClient(_FakeStreamingClient):
    """text_stream that keeps producing until someone stops it, and a
    get_final_message() that fails the test if respond() waits for the whole
    reply after being cancelled."""

    def __init__(self, deltas):
        super().__init__(text="".join(deltas))
        self._deltas = deltas
        self.exited = False

    @contextmanager
    def stream(self, **kwargs):
        self.stream_kwargs = kwargs

        def boom():
            raise AssertionError("a cancelled turn must not wait for the final message")

        try:
            yield SimpleNamespace(text_stream=iter(self._deltas), get_final_message=boom)
        finally:
            self.exited = True


def test_cancelled_turn_returns_what_was_already_said_with_a_marker():
    # The callback is the cancellation channel: it already runs per delta, so
    # raising from it stops generation without new plumbing through respond().
    client = _FakeCancelClient(["The ", "capital ", "of ", "France ", "is ", "Paris."])
    seen = []

    def on_token(delta):
        # Shaped like the real wrapper in webapp.py: check the signal, then
        # forward. A delta that arrives after the stop was pressed is never
        # forwarded, so it must not count as something Duc saw either.
        if len(seen) == 2:
            raise TurnCancelled()
        seen.append(delta)

    reply = AnthropicLLM(client).respond("sys", [], "hi", on_token=on_token)
    assert reply == "The capital " + CANCELLED_MARKER
    assert seen == ["The ", "capital "]
    assert client.exited, "the stream context must close, so the API stops generating"


def test_cancelling_before_the_first_delta_still_returns_the_marker():
    client = _FakeCancelClient(["never seen"])

    def on_token(_delta):
        raise TurnCancelled()

    assert AnthropicLLM(client).respond("sys", [], "hi", on_token=on_token) == CANCELLED_MARKER
