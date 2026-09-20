"""The smallest HTTP surface over the learning reels (plan step D2, first half): what is due, and answering it.

The CLI was the only door (`scripts/reels.py`). Rules carried over from the library and pinned here at the HTTP edge:
only APPROVED moments are ever served; a rejected source serves nothing; a YouTube source is embed-only, so the API
returns a youtube-nocookie EMBED url with bounds and never a media url or a download; the correct option is never
in a response before the owner has answered; the question shown for a delayed review is the initial one; an answer
that is not one of the listed options is a 400; answering something that is not due is a 409.
"""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from companion.reels import ReelsStore
from tests.test_reels import (
    CORRECT_INITIAL,
    SRT_TEXT,
    T0,
    WRONG_INITIAL,
    _approved_moment,
    _youtube_source,
    parse_transcript,
)


@pytest.fixture()
def store(tmp_path):
    return ReelsStore(tmp_path / "reels.db")


@pytest.fixture()
def moment(store, tmp_path):
    source = store.add_source(_youtube_source(), transcript_text=SRT_TEXT)
    return _approved_moment(store, source, parse_transcript(SRT_TEXT), tmp_path)


@pytest.fixture()
def client(store, monkeypatch):
    import companion.webapp as webapp

    clock = {"now": T0}
    monkeypatch.setattr(webapp, "_reels_store", lambda: store)
    monkeypatch.setattr(webapp, "_utcnow", lambda: clock["now"])
    with TestClient(webapp.app) as c:
        c.clock = clock
        yield c


def _learn(store, moment, at):
    """Answer the initial question correctly so a delayed review gets scheduled."""
    store.record_watch("duc", moment.id, at=at)
    store.record_attempt("duc", moment.id, "initial", CORRECT_INITIAL, at=at)
    return store.learner_concept("duc", moment.id).next_review_at


def test_nothing_due_is_an_empty_list(client, moment):
    assert client.get("/api/reels/due").json() == {"due": []}


def test_a_due_review_carries_an_embed_url_a_question_and_no_answer_key(client, store, moment):
    next_review = _learn(store, moment, T0)
    client.clock["now"] = next_review + timedelta(minutes=1)
    (item,) = client.get("/api/reels/due").json()["due"]
    assert item["moment_id"] == moment.id and item["kind"] == "delayed"
    assert item["embed_url"].startswith("https://www.youtube-nocookie.com/embed/") and "start=" in item["embed_url"]
    assert item["question"]["stem"] and len(item["question"]["options"]) >= 2
    assert all(set(option) == {"text"} for option in item["question"]["options"])      # no "correct", no "hint"
    body = json.dumps(item)
    assert '"correct"' not in body and "googlevideo" not in body and "download" not in body.lower()


def test_answering_returns_feedback_and_then_it_is_no_longer_due(client, store, moment):
    client.clock["now"] = _learn(store, moment, T0) + timedelta(minutes=1)
    wrong = client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": WRONG_INITIAL})
    assert wrong.status_code == 200 and wrong.json()["correct"] is False and wrong.json()["message"]
    right = client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": CORRECT_INITIAL})
    assert right.status_code == 200 and right.json()["correct"] is True
    assert client.get("/api/reels/due").json() == {"due": []}


def test_bad_answers_are_refused_with_the_right_status(client, store, moment):
    assert client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": CORRECT_INITIAL}).status_code == 409  # not due
    client.clock["now"] = _learn(store, moment, T0) + timedelta(minutes=1)
    assert client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": "not an option"}).status_code == 400
    assert client.post(f"/api/reels/{moment.id}/answer", json={"kind": "bonus", "chosen": CORRECT_INITIAL}).status_code in (400, 422)
    assert client.post("/api/reels/99999/answer", json={"kind": "delayed", "chosen": CORRECT_INITIAL}).status_code == 404
    assert client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": CORRECT_INITIAL, "user": "someone-else"}).status_code in (400, 422)


def test_a_moment_that_is_no_longer_approved_is_not_served_or_answerable(client, store, moment):
    client.clock["now"] = _learn(store, moment, T0) + timedelta(minutes=1)
    store.set_status(moment.id, "rejected")
    assert client.get("/api/reels/due").json() == {"due": []}
    assert client.post(f"/api/reels/{moment.id}/answer", json={"kind": "delayed", "chosen": CORRECT_INITIAL}).status_code in (403, 404, 409)


def test_the_cli_and_the_endpoint_agree_on_what_is_due(client, store, moment):
    at = _learn(store, moment, T0) + timedelta(minutes=1)
    client.clock["now"] = at
    assert [m.id for m in store.due("duc", at=at)] == [item["moment_id"] for item in client.get("/api/reels/due").json()["due"]]
