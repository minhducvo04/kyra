"""Decision log: append, outcome join, bad dir, rotation, and the health refusal."""
import json
import time

import pytest

from companion import decision_log


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(decision_log, "decisions_dir", lambda: tmp_path / "decisions")
    return tmp_path / "decisions"


def test_log_appends_row_with_state_hash(store):
    h = decision_log.log("router", {"intent": "reorder backlog"}, ["a", "b"], "a", "rules", 0.9, 12.5)
    assert isinstance(h, str) and len(h) == 16
    rows = [json.loads(x) for x in (store / "router.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["state_hash"] == h
    assert rows[0]["choice"] == "a" and rows[0]["source"] == "rules"
    assert rows[0]["confidence"] == 0.9 and rows[0]["latency_ms"] == 12.5
    assert isinstance(rows[0]["t"], float)


def test_outcome_joins_to_decision_by_state_hash(store):
    h = decision_log.log("router", {"intent": "x"}, ["a"], "a", "rules")
    assert decision_log.outcome("router", h, "accepted") is True
    got = list(decision_log.read("router"))
    assert [r["state_hash"] for r in got] == [h, h]
    assert got[1]["outcome"] == "accepted" and "choice" not in got[1]


def test_read_filters_by_since(store):
    decision_log.log("router", {"n": 1}, [], "a", "rules")
    cutoff = time.time() + 60
    assert list(decision_log.read("router", since=cutoff)) == []
    assert len(list(decision_log.read("router"))) == 1


def test_bad_dir_never_raises(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    monkeypatch.setattr(decision_log, "decisions_dir", lambda: blocker)
    assert decision_log.log("router", {"n": 1}, [], "a", "rules") is None
    assert decision_log.outcome("router", "abc", "ok") is False
    assert list(decision_log.read("router")) == []
    assert "could not write" in caplog.text


def test_bad_family_name_is_refused_not_raised(store, caplog):
    assert decision_log.log("../escape", {"n": 1}, [], "a", "rules") is None
    assert not (store.parent / "escape.jsonl").exists()
    assert list(decision_log.read("../escape")) == []


def test_rotation_at_size_limit(store, monkeypatch):
    monkeypatch.setattr(decision_log, "ROTATE_BYTES", 200)
    for i in range(20):
        decision_log.log("router", {"n": i}, [], "a", "rules")
    current = store / "router.jsonl"
    rotated = list(store.glob("router.*.jsonl"))
    assert rotated, "an over-size file must be moved aside before the next append"
    total = sum(len(p.read_text().splitlines()) for p in [*rotated, current])
    assert total == 20 and len(current.read_text().splitlines()) < 20


def test_health_worded_state_is_refused_and_not_written(store, caplog):
    h = decision_log.log("needs", {"text": "I slept badly, 90 bpm"}, [], "a", "rules")
    assert h is None
    assert not (store / "needs.jsonl").exists()
    assert "refused" in caplog.text


def test_family_word_state_is_refused(store):
    assert decision_log.log("needs", {"text": "family pickup"}, [], "a", "rules") is None
    assert not (store / "needs.jsonl").exists()


def test_plain_state_still_logs(store):
    assert decision_log.log("sprint", {"text": "reorder the sprint backlog"}, [], "a", "rules") is not None
