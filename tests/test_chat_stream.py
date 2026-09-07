"""POST /api/chat/stream: the same turn as /api/chat, delivered token by token."""
import json

import pytest
from fastapi.testclient import TestClient

from companion import webapp
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
