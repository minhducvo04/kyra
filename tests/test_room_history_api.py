"""The ROOM panel's history: what the sampler wrote, as a series the page can draw. Read-only.

A gap stays a gap: an `unavailable` slot is returned with value null so the page draws a break, never a line through
it. The endpoint reads the history store only; it never calls the device, so opening the panel costs no cloud call.
"""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from companion.home import EnvHistory, Reading, slot_for

NOW = datetime(2026, 9, 19, 14, 27, tzinfo=UTC)


@pytest.fixture()
def webapp():
    import companion.webapp as module

    return module


@pytest.fixture()
def history(webapp, monkeypatch, tmp_path):
    store = EnvHistory(engine=create_engine(f"sqlite:///{tmp_path}/env.db"))
    monkeypatch.setattr(webapp, "_env_history", lambda: store)
    monkeypatch.setattr(webapp, "_utcnow", lambda: NOW)
    return store


def _put(store, minutes_ago, value, quality="ok", metric="humidity"):
    when = NOW - timedelta(minutes=minutes_ago)
    store.record(slot_for(when), [Reading("humidifier.room", metric, value, "%", quality, when)])


def test_the_series_is_ordered_carries_gaps_as_null_and_summarises(webapp, history):
    _put(history, 20, 61.0)
    _put(history, 15, None, "unavailable")
    _put(history, 10, 64.0)
    _put(history, 5, 66.0)
    _put(history, 60 * 30, 40.0)               # older than the window
    _put(history, 5, 45.0, metric="target_humidity")
    with TestClient(webapp.app) as client:
        body = client.get("/api/room/history?metric=humidity&hours=24").json()
    assert body["entity"] == "humidifier.room" and body["metric"] == "humidity" and body["unit"] == "%"
    assert [(p["value"], p["quality"]) for p in body["points"]] == [(61.0, "ok"), (None, "unavailable"), (64.0, "ok"), (66.0, "ok")]
    assert body["points"][0]["slot"].endswith("+00:00") and body["points"][0]["slot"] < body["points"][-1]["slot"]
    assert body["summary"] == {"min": 61.0, "max": 66.0, "latest": 66.0, "samples": 3, "unavailable": 1}


def test_an_empty_history_is_an_empty_series_not_an_error(webapp, history):
    with TestClient(webapp.app) as client:
        body = client.get("/api/room/history").json()   # defaults: humidity, 24 hours
    assert body["points"] == [] and body["summary"] == {"min": None, "max": None, "latest": None, "samples": 0, "unavailable": 0}


@pytest.mark.parametrize("query", ["metric=../../etc", "metric=password", "hours=0", "hours=1000", "hours=abc"])
def test_bad_parameters_are_refused(webapp, history, query):
    with TestClient(webapp.app) as client:
        assert client.get(f"/api/room/history?{query}").status_code in (400, 422)


def test_the_endpoint_never_touches_the_device(webapp, history, monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("the history endpoint called a tool")

    monkeypatch.setattr(webapp._registry, "run", _never)
    monkeypatch.setattr(webapp._registry, "dispatch", _never)
    with TestClient(webapp.app) as client:
        assert client.get("/api/room/history").status_code == 200
