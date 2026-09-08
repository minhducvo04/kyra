"""POST /api/correction - the "that was wrong" mark on a Kyra line.

The point is the data answer to a wrong reply, not the UI gesture: memory
notes are loaded in full into every system prompt, so a correction saved
here is in front of her on the very next turn rather than sitting in a log
nobody reads.
"""
import pytest
from fastapi.testclient import TestClient

from companion import webapp


@pytest.fixture(scope="module")
def client():
    with TestClient(webapp.app) as c:
        yield c


def _corrections_file():
    from companion.memory_notes import DEFAULT_DIR
    return DEFAULT_DIR / "corrections.md"


def test_marking_a_reply_wrong_lands_in_the_corrections_notes(client):
    res = client.post("/api/correction", json={"reply": "The capital of Australia is Sydney."})
    assert res.status_code == 200 and res.json()["category"] == "corrections"
    body = _corrections_file().read_text(encoding="utf-8")
    assert "Duc marked this reply as wrong: The capital of Australia is Sydney." in body


def test_it_shows_up_in_the_next_system_prompt(client):
    client.post("/api/correction", json={"reply": "Chroma raises on a duplicate id."})
    # The same render() ConversationManager._build_system() puts in front of her.
    assert "Chroma raises on a duplicate id." in webapp._memory_notes.render()


def test_a_long_reply_is_kept_to_an_excerpt(client):
    long_reply = "word " * 200
    client.post("/api/correction", json={"reply": long_reply})
    line = [ln for ln in _corrections_file().read_text(encoding="utf-8").splitlines() if "word word" in ln][0]
    assert line.endswith("…") and len(line) < 200


def test_an_empty_reply_is_rejected_rather_than_saved(client):
    res = client.post("/api/correction", json={"reply": "   "})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "empty_reply"
