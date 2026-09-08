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


# --- the MEMORY tab's endpoints ------------------------------------------------
# Same store the corrections above write to, and the same one every system prompt
# and every resume draft reads. Duc could only see it by opening the Markdown.


def test_notes_list_returns_rows_a_panel_can_render(client):
    client.post("/api/memory-notes", json={"category": "preferences", "note": "likes standing desks"})
    body = client.get("/api/memory-notes").json()
    row = next(n for n in body["notes"] if n["text"] == "likes standing desks")
    assert row["category"].lower() == "preferences" and len(row["date"]) == 10
    assert "preferences" in [c.lower() for c in body["categories"]]


def test_an_empty_note_is_rejected(client):
    res = client.post("/api/memory-notes", json={"category": "preferences", "note": "  "})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "empty_note"


def test_deleting_a_note_removes_it_from_what_the_model_sees(client):
    client.post("/api/memory-notes", json={"category": "people", "note": "a fact to remove"})
    assert "a fact to remove" in webapp._memory_notes.render()
    res = client.post("/api/memory-notes/delete", json={"category": "people", "text": "a fact to remove"})
    assert res.status_code == 200 and res.json()["deleted"] is True
    assert "a fact to remove" not in webapp._memory_notes.render()


def test_deleting_something_that_is_not_there_is_a_404(client):
    res = client.post("/api/memory-notes/delete", json={"category": "people", "text": "never existed"})
    assert res.status_code == 404
