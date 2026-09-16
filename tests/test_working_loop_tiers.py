"""Declared tier per run, and readiness for the owner's decision (red until Codex builds it).

Chosen after brainstorm B01 (Codex ranked the tier first and proposed readiness; Claude folded both into
one slice). With two approved developers the rule table changes nothing yet; the tier is stored so a later
rule can bind to it, and readiness makes "why can I not approve this" visible instead of implicit.

CONTRACT (companion/working_loop.py)
  TIERS == ("casual", "work", "life_changing")
  loop_runs.tier: String(16), not null, server default "work" (schema.py + one Alembic revision)
  ExecutionRecord.tier: str
  LoopStore.create_run(..., tier="work")            # unknown tier -> PolicyRefused("invalid_tier")
  LoopController.request(..., tier="work")          # same validation
  request_review(subject_id) creates the reviewer run with the subject's tier
  LoopController.readiness(run) -> {"tier": str, "ready": bool, "reasons": list[str]}
      status != "done"                      -> ready False, reasons ["not_complete"]
      done, no review                       -> ready False, reasons ["no_review"]
      done, every review stale              -> ready False, reasons ["review_stale"]
      done, one current review (any verdict) -> ready True, reasons []
  webapp: LoopRunIn.tier: str = "work"; POST /api/loop/runs with an unknown tier -> 400 "policy_refused";
          GET /api/loop/runs/{id} adds "readiness"; web/loop.html carries <select id="tier"> with the three values
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings
from tests.test_working_loop import ScriptedRunner, claude_stream, ok
from tests.test_working_loop_decisions import make_controller, reviewed_pair


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def test_tier_defaults_to_work_and_only_the_three_values_exist(wl, store):
    assert wl.TIERS == ("casual", "work", "life_changing")
    controller, _ = make_controller(wl, store, [])
    assert controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p").tier == "work"
    assert controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p", tier="casual").tier == "casual"
    with pytest.raises(wl.PolicyRefused):
        controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p", tier="urgent")
    assert store.get_run(1, owner="duc").tier == "work"  # persisted, not a default on the dataclass


def test_review_run_inherits_the_subject_tier(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("draft"))])
    subject = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high",
                                                     prompt="p", tier="life_changing").id)
    assert controller.request_review(subject.id).tier == "life_changing"


def test_readiness_names_the_reason_at_every_step(wl, store):
    controller, _, subject, _, review = reviewed_pair(wl, store)
    queued = controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="later")
    assert controller.readiness(queued) == {"tier": "work", "ready": False, "reasons": ["not_complete"]}
    assert controller.readiness(subject) == {"tier": "work", "ready": True, "reasons": []}
    Path(subject.artifact_dir, "output.md").write_text("edited after review", encoding="utf-8")
    assert controller.readiness(store.get_run(subject.id, owner="duc")) == {"tier": "work", "ready": False, "reasons": ["review_stale"]}


def test_done_without_any_review_is_not_ready(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("draft"))])
    done = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p").id)
    assert controller.readiness(done) == {"tier": "work", "ready": False, "reasons": ["no_review"]}


def test_http_accepts_a_tier_and_reports_readiness(wl, store, monkeypatch):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    controller = wl.LoopController(store, ScriptedRunner([]), owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        created = client.post("/api/loop/runs", json={"choice": "codex-default", "prompt": "p", "topic": "t", "tier": "casual"})
        assert created.status_code == 200 and created.json()["run"]["tier"] == "casual"
        bad = client.post("/api/loop/runs", json={"choice": "codex-default", "prompt": "p", "topic": "t", "tier": "urgent"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "policy_refused"
        shown = client.get(f"/api/loop/runs/{created.json()['run']['id']}").json()
        assert shown["readiness"] == {"tier": "casual", "ready": False, "reasons": ["not_complete"]}
        html = client.get("/loop").text
        assert 'id="tier"' in html and 'value="life_changing"' in html
    get_settings.cache_clear()
