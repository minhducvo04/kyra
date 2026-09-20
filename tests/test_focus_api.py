"""HTTP layer for focus blocks, against the real app with the store isolated
under the scratch KYRA_DATA_DIR (conftest.py).

What these pin, beyond "the route works": the start response must carry enough
for the browser to synthesise the sound and cue the breaks, a second start must
be refused with a real status code rather than quietly replacing the first, and
the arm must not be named until the block ends - that last one is the whole
experiment.
"""
import pytest
from fastapi.testclient import TestClient

import companion.webapp as webapp
from companion import focus


@pytest.fixture
def client():
    with TestClient(webapp.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_focus_store():
    """Each test starts with no blocks: these run against one shared scratch
    database and a leftover running block would fail the next test's start."""
    from sqlalchemy import delete

    from companion.schema import focus_sessions as FS

    with webapp._focus_store._engine.begin() as conn:
        conn.execute(delete(FS))
    yield
    with webapp._focus_store._engine.begin() as conn:
        conn.execute(delete(FS))


def test_start_returns_a_playable_plan(client):
    res = client.post("/api/focus/start", json={"minutes": 50, "task": "resume tailoring"})
    assert res.status_code == 200
    body = res.json()
    plan = body["plan"]
    assert body["running"] is True
    assert body["session"]["task"] == "resume tailoring"
    assert plan["spec"]["kind"] in focus.PROCEDURAL_KINDS
    assert plan["gain"] <= focus.GAIN_CEILING
    assert plan["fade_seconds"] >= focus.MIN_FADE_SECONDS
    assert plan["break_offsets"] == [25]
    assert body["remaining_minutes"] <= 50


def test_a_second_start_is_refused_with_409(client):
    client.post("/api/focus/start", json={"minutes": 25, "task": "a"})
    res = client.post("/api/focus/start", json={"minutes": 25, "task": "b"})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "focus_running"


def test_an_impossible_length_is_a_400_not_a_500(client):
    res = client.post("/api/focus/start", json={"minutes": 600, "task": "a"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "focus_invalid"


def test_active_resumes_the_same_arm_after_a_reload(client):
    started = client.post("/api/focus/start", json={"minutes": 50, "task": "a"}).json()
    again = client.get("/api/focus/active").json()
    assert again["running"] is True
    assert again["plan"]["spec"] == started["plan"]["spec"]
    assert again["plan"]["gain"] == started["plan"]["gain"]


def test_active_when_nothing_runs_still_reports_the_theme(client):
    body = client.get("/api/focus/active").json()
    assert body["running"] is False
    assert "evening" in body and isinstance(body["evening"], bool)
    assert body["completed_blocks"] == 0


def test_probe_then_end_reports_the_delta_and_unblinds(client):
    started = client.post("/api/focus/start", json={"minutes": 25, "task": "a"}).json()
    sid = started["session"]["id"]
    assert client.post("/api/focus/probe",
                       json={"id": sid, "phase": "start", "median_ms": 280.0, "lapses": 0}).status_code == 200
    assert client.post("/api/focus/probe",
                       json={"id": sid, "phase": "end", "median_ms": 305.0, "lapses": 2}).status_code == 200
    ended = client.post("/api/focus/end", json={"rating": 4, "note": "steady"}).json()
    assert ended["probe_delta_ms"] == pytest.approx(25.0)
    assert ended["condition"] in focus.CONDITIONS  # named only now
    assert ended["session"]["rating"] == 4


def test_a_bad_probe_phase_is_a_400(client):
    started = client.post("/api/focus/start", json={"minutes": 25, "task": "a"}).json()
    res = client.post("/api/focus/probe",
                      json={"id": started["session"]["id"], "phase": "halfway", "median_ms": 1, "lapses": 0})
    assert res.status_code == 400


def test_a_probe_for_an_unknown_block_is_a_404(client):
    res = client.post("/api/focus/probe", json={"id": 999999, "phase": "start", "median_ms": 1, "lapses": 0})
    assert res.status_code == 404


def test_ending_nothing_is_a_404(client):
    assert client.post("/api/focus/end", json={"rating": 3, "note": ""}).status_code == 404


def test_a_bad_rating_is_a_400(client):
    client.post("/api/focus/start", json={"minutes": 25, "task": "a"})
    assert client.post("/api/focus/end", json={"rating": 11, "note": ""}).status_code == 400


def test_history_lists_finished_blocks_only(client):
    client.post("/api/focus/start", json={"minutes": 25, "task": "done one"})
    client.post("/api/focus/end", json={"rating": 5, "note": "good"})
    client.post("/api/focus/start", json={"minutes": 25, "task": "still going"})
    tasks = [s["task"] for s in client.get("/api/focus/history").json()["sessions"]]
    assert tasks == ["done one"]


def test_the_arm_is_not_named_anywhere_while_the_block_runs(client):
    """The spec has to travel (the browser makes the sound), but the arm's name
    is stripped until the block ends, so the panel cannot print it even by
    accident. Weak against devtools, real against reading the UI."""
    body = client.post("/api/focus/start", json={"minutes": 25, "task": "a"}).json()
    assert "condition" not in body
    assert "condition" not in body["plan"]
    assert "condition" not in body["session"]
    assert "condition" not in client.get("/api/focus/active").json()["plan"]
    # And it comes back the moment the block is over.
    assert client.post("/api/focus/end", json={"rating": 3, "note": ""}).json()["condition"] in focus.CONDITIONS
