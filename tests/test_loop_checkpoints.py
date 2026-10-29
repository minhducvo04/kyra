"""P0 human checkpoints: artifact-bound questions, dedupe, expiry and morning-card lines."""

from pathlib import Path

import pytest

from companion.feature_team import checkpoints, contract

LOOP = Path("loop")


def store(tmp_path, now, events=None):
    events = events if events is not None else []
    return checkpoints.Checkpoints(
        tmp_path / LOOP,
        emit=events.append,
        clock=lambda: now[0],
        hasher=lambda path: _hashes[str(path)],
    )


_hashes: dict[str, str] = {}


def _artifact(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(text.encode()).hexdigest()
    _hashes[str(path)] = digest
    return str(path)


def _log(cp, tmp_path, **over):
    artifact = _artifact(tmp_path, over.pop("artifact_name", "art.txt"), over.pop("body", "v1"))
    sha = _hashes[artifact]
    defaults = {
        "feature": "demo",
        "node": "P0",
        "run_id": "demo-P0-20260101T000000-a1b2c3",
        "what": "ship this diff?",
        "why": "blocked on policy",
        "guess": "yes, land it",
        "artifact": artifact,
        "sha256": sha,
        "reversible": True,
        "expiry": 2_000.0,
    }
    defaults.update(over)
    return cp.log(**defaults)


def test_yes_on_hash_a_refused_when_artifact_is_hash_b(tmp_path):
    now = [1_000.0]
    events = []
    cp = store(tmp_path, now, events)
    row = _log(cp, tmp_path)
    cp.answer(row["id"], "yes", by="the owner", artifact_path=row["artifact"])
    _artifact(tmp_path, "art.txt", "v2-changed")
    refusal = cp.apply(row["id"], row["artifact"])
    assert refusal == {"ok": False, "reason": "artifact changed"}
    assert cp.apply(row["id"], str(tmp_path / "art.txt")) == {"ok": False, "reason": "artifact changed"}


def test_same_question_logged_twice_appears_once(tmp_path):
    now = [1_000.0]
    cp = store(tmp_path, now)
    first = _log(cp, tmp_path)
    second = _log(cp, tmp_path)
    assert first["id"] == second["id"]
    assert len(cp._load()) == 1
    assert len(cp.open_questions()) == 1


def test_expiry_reversible_proceeds_irreversible_stays_parked(tmp_path):
    now = [1_000.0]
    events = []
    rev = store(tmp_path, now, events)
    row_r = _log(rev, tmp_path, reversible=True, expiry=1_100.0)
    row_i = _log(
        rev,
        tmp_path,
        artifact_name="hard.txt",
        body="irreversible",
        what="delete prod?",
        reversible=False,
        expiry=1_100.0,
    )
    now[0] = 1_200.0
    open_rows = rev.open_questions()
    assert {r["id"] for r in open_rows} == {row_i["id"]}
    loaded = {r["id"]: r for r in rev._load()}
    assert loaded[row_r["id"]]["state"] == "proceeded_on_guess"
    assert loaded[row_i["id"]]["state"] == "parked"
    assert loaded[row_r["id"]]["answer"]["decision"] == "yes"
    assert loaded[row_i["id"]]["answer"] is None
    assert any(e["kind"] == "question" for e in events)


def test_card_lines_shows_each_open_question_once(tmp_path):
    now = [1_000.0]
    cp = store(tmp_path, now)
    row = _log(cp, tmp_path, what="approve plan", guess="land tonight")
    _log(cp, tmp_path, what="approve plan", guess="land tonight")
    lines = cp.card_lines()
    assert len(lines) == 1
    assert "approve plan" in lines[0]
    assert "land tonight" in lines[0]
    assert "proceed" in lines[0]
    assert f"@{row['sha256'][:8]}" in lines[0]
    now[0] = 2_500.0
    assert cp.card_lines() == []


def test_every_row_validates_fixed_fields_and_refuses_unknown(tmp_path):
    now = [1_000.0]
    cp = store(tmp_path, now)
    row = _log(cp, tmp_path)
    checkpoints.validate_question(row)
    bad = dict(row, extra="nope")
    with pytest.raises(ValueError, match="extra: unknown field"):
        checkpoints.validate_question(bad)
    with pytest.raises(ValueError, match="what: missing"):
        checkpoints.validate_question({k: v for k, v in row.items() if k != "what"})
    with pytest.raises(ValueError, match="default: irreversible"):
        checkpoints.validate_question(dict(row, reversible=False))
    with pytest.raises(ValueError, match="decision"):
        checkpoints.validate_answer({"decision": "maybe", "by": "x", "at": 1.0, "sha256": "a" * 64})
    event = contract.validate_event({
        "t": 1.0,
        "run_id": row["run_id"],
        "feature": row["feature"],
        "node": row["node"],
        "worker": "board",
        "channel": None,
        "kind": "question",
        "sha": None,
        "path": row["artifact"],
        "detail": "asked",
    })
    assert event["kind"] == "question"


def test_module_helpers_match_store(tmp_path):
    now = [1_000.0]
    events = []
    cp = store(tmp_path, now, events)
    row = _log(cp, tmp_path)
    assert checkpoints.open_questions(tmp_path / LOOP, clock=lambda: now[0]) == cp.open_questions()
    assert checkpoints.card_lines(tmp_path / LOOP, clock=lambda: now[0]) == cp.card_lines()
    cp.answer(row["id"], "yes", by="the owner")
    ok = checkpoints.apply(tmp_path / LOOP, row["id"], row["artifact"], hasher=lambda p: _hashes[str(p)])
    assert ok["ok"] is True
