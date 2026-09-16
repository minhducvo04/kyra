"""Regressions from Claude's independent review of slice 2 (owner decisions and reconciliation), 2026-09-15.

One finding: the reconciliation note is an owner-editable private file, and the page already labels an edited note,
but a deleted note made `reconciliations_for` raise, which turns the whole run detail (receipt, reviews, everything)
into a 500. A missing note must be reported as missing, beside the intact receipt.

Contract addition (Codex implements): `reconciliations_for` returns the row with `note: None` and
`note_changed: True` when the file at `note_path` is gone; `GET /api/loop/runs/{id}` keeps answering 200.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.jobs import run_one
from companion.settings import get_settings
from tests.test_working_loop import ScriptedRunner


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


def test_a_deleted_note_file_is_reported_missing_not_raised(wl, tmp_path):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    controller = wl.LoopController(store, ScriptedRunner([wl.DispatchInterrupted("killed")]), owner="duc", timeout_seconds=60)
    run = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p").id)
    rec = controller.reconcile(run.id, outcome="nothing_happened", note="checked the provider history")
    Path(rec.note_path).unlink()
    listed = store.reconciliations_for(run.id, owner="duc")
    assert [(r["id"], r["outcome"], r["note"], r["note_changed"]) for r in listed] == [
        (rec.id, "nothing_happened", None, True)]
    assert listed[0]["note_sha256"] == rec.note_sha256                         # the recorded hash survives the file


def test_run_detail_still_serves_the_receipt_when_a_note_file_is_gone(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    controller = wl.LoopController(store, ScriptedRunner([wl.DispatchInterrupted("killed")]), owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    try:
        with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
            run = client.post("/api/loop/runs", json={"choice": "claude-fable-high", "prompt": "p", "topic": "t"}).json()["run"]
            while run_one(webapp._queue, webapp.HANDLERS):
                pass
            rec = client.post(f"/api/loop/runs/{run['id']}/reconcile", json={"outcome": "nothing_happened", "note": "n"}).json()["reconciliation"]
            Path(rec["note_path"]).unlink()
            detail = client.get(f"/api/loop/runs/{run['id']}")
            assert detail.status_code == 200, detail.text
            body = detail.json()
            assert body["run"]["status"] == "unreconciled" and body["run"]["input_sha256"]
            assert [(r["note"], r["note_changed"]) for r in body["reconciliations"]] == [(None, True)]
    finally:
        get_settings.cache_clear()
