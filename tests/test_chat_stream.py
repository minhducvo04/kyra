"""POST /api/chat/stream: the same turn as /api/chat, delivered token by token."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.llm import CANCELLED_MARKER, TurnCancelled
from companion.webapp import ChatOut


@pytest.fixture(scope="module")
def client():
    with TestClient(webapp.app) as c:
        yield c


def _events(text: str) -> list[tuple[str, object]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_stream_yields_tokens_then_the_full_reply(client, monkeypatch):
    def fake_answer(message, on_token=None):
        for piece in ("Hel", "lo, ", message):
            on_token(piece)
        return ChatOut(reply="Hello, " + message, backend="auto", actual_backend="claude", path="text", reason="r")

    monkeypatch.setattr(webapp, "_answer", fake_answer)
    with client.stream("POST", "/api/chat/stream", json={"message": "Duc"}) as res:
        assert res.status_code == 200 and res.headers["content-type"].startswith("text/event-stream")
        events = _events(res.read().decode())
    assert events[:3] == [("token", "Hel"), ("token", "lo, "), ("token", "Duc")]
    kind, payload = events[-1]
    assert kind == "done" and payload["reply"] == "Hello, Duc" and payload["actual_backend"] == "claude"


def test_stream_without_a_streaming_backend_still_completes(client, monkeypatch):
    monkeypatch.setattr(webapp, "_answer", lambda message, on_token=None: ChatOut(reply="whole", backend="local"))
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as res:
        events = _events(res.read().decode())
    assert events == [("done", {"reply": "whole", "backend": "local", "actual_backend": None, "path": None, "reason": None})]


def test_stream_reports_a_failure_as_an_error_event(client, monkeypatch):
    def boom(message, on_token=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(webapp, "_answer", boom)
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as res:
        events = _events(res.read().decode())
    assert events == [("error", "RuntimeError: model down")]


# --- cancelling a reply mid-generation ---------------------------------------
# The client aborting its fetch is only half of a stop: the turn keeps running
# on the server, and ConversationManager._record_turn would file the whole
# reply into history and Chroma - so Kyra would remember saying something Duc
# never saw. /api/chat/cancel is the other half.


def test_cancel_with_nothing_in_flight_says_so(client):
    assert client.post("/api/chat/cancel").json() == {"cancelled": False}


def test_cancel_makes_the_in_flight_callback_raise(client, monkeypatch):
    started = threading.Event()
    outcome = {}

    def fake_answer(message, on_token=None):
        on_token("Paris ")  # one delta gets through before the stop
        started.set()
        for _ in range(300):
            try:
                on_token("is ")
            except TurnCancelled:
                outcome["cancelled_after"] = "Paris "
                return ChatOut(reply="Paris " + CANCELLED_MARKER, backend="auto")
            time.sleep(0.01)
        raise AssertionError("the callback never raised - cancel did not reach the running turn")

    monkeypatch.setattr(webapp, "_answer", fake_answer)

    def cancel_once_it_is_running():
        started.wait(5)
        outcome["response"] = webapp.chat_cancel()

    stopper = threading.Thread(target=cancel_once_it_is_running)
    stopper.start()
    with client.stream("POST", "/api/chat/stream", json={"message": "what is the capital of France"}) as res:
        events = _events(res.read().decode())
    stopper.join(10)

    assert outcome["cancelled_after"] == "Paris "
    assert outcome["response"] == {"cancelled": True}
    # The partial still comes back as a normal `done`, not an error: an
    # interruption is an outcome of the turn, and the reply it records is
    # exactly what Duc saw, marked.
    assert events[0] == ("token", "Paris ")
    assert events[-1][0] == "done"
    assert events[-1][1]["reply"] == "Paris " + CANCELLED_MARKER


def test_a_finished_turn_is_no_longer_cancellable(client, monkeypatch):
    # Otherwise a stop pressed a moment too late would kill the *next* turn.
    monkeypatch.setattr(webapp, "_answer", lambda message, on_token=None: ChatOut(reply="done", backend="auto"))
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as res:
        res.read()
    assert webapp.chat_cancel() == {"cancelled": False}


def test_a_cancelled_turn_is_remembered_as_what_he_actually_saw():
    """The point of stopping the model server-side rather than only in the
    browser: history and memory must hold the partial, marked - not the whole
    reply Kyra would have given, and not nothing at all."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from companion.conversation import ConversationManager
    from companion.llm import AnthropicLLM
    from companion.memory import MemoryStore
    from companion.persona import KYRA

    class RecordingMemory(MemoryStore):
        def __init__(self):
            self.added = []

        def add(self, text, metadata=None):
            self.added.append(text)

        def retrieve(self, query, k=5):
            return []

    class Client:
        messages = property(lambda self: self)

        @contextmanager
        def stream(self, **kwargs):
            def boom():
                raise AssertionError("cancelled turns must not wait for the full reply")

            yield SimpleNamespace(text_stream=iter(["Spaced ", "repetition ", "works ", "because"]), get_final_message=boom)

    seen = []

    def on_token(delta):
        if len(seen) == 2:
            raise TurnCancelled()
        seen.append(delta)

    memory = RecordingMemory()
    cm = ConversationManager(persona=KYRA, memory=memory, llm=AnthropicLLM(Client()))
    reply = cm.handle_turn("how does spaced repetition work", on_token=on_token)

    assert reply == "Spaced repetition " + CANCELLED_MARKER
    assert cm.history[-1].content == reply
    assert memory.added == ["Duc said: how does spaced repetition work", "Kyra replied: " + reply]
