"""Bounded assignments as records on the loop page (red until Codex builds A03).

Tonight's real process was: Claude writes an assignment (goal, allowed files, acceptance lines), Codex
builds it, Claude reviews the diff and commits. None of that is visible on /loop. This slice records it,
so the page shows what was asked, what was built, and how far it got. No automation: statuses move only
when a human or the coordinator says so, and only forward.

CONTRACT (companion/working_loop.py)
  ASSIGNMENT_STATUSES == ("assigned", "built", "reviewed", "committed")
  @dataclass(frozen=True) Assignment: id, owner, code, title, goal, allowed_files: list[str],
      acceptance: list[str], tier, status, builder_run_id: int | None, result_sha256: str | None,
      commit_hash: str | None, created_at, updated_at
  loop_assignments table (schema.py + one idempotent Alembic revision); (owner, code) unique
  LoopStore.create_assignment(*, owner, code, title, goal, allowed_files, acceptance, tier="work") -> Assignment
      PolicyRefused("invalid_assignment") for an empty code/title/goal, a tier not in TIERS, or a duplicate code
  LoopStore.list_assignments(*, owner) -> list[Assignment] newest first
  LoopStore.get_assignment(assignment_id, *, owner) -> Assignment | None
  LoopStore.advance_assignment(assignment_id, *, owner, status, builder_run_id=None, result_sha256=None,
                               commit_hash=None) -> Assignment
      only the next status in order is accepted (assigned -> built -> reviewed -> committed);
      anything else -> PolicyRefused("invalid_transition"); "built" requires a builder_run_id that is a run of
      this owner (PolicyRefused("run_not_owned") otherwise); "committed" requires a 7 to 40 char hex commit_hash
  webapp: GET /api/loop/assignments -> {"assignments": [...]}; POST /api/loop/assignments (AssignmentIn with
      extra="forbid") -> {"assignment": ...}, 400 "policy_refused" on refusal;
      POST /api/loop/assignments/{id}/advance {status, builder_run_id?, result_sha256?, commit_hash?}
      -> {"assignment": ...}, 400 "policy_refused" on a bad transition, 404 "not_found" for a missing id;
      web/loop.html carries a <section id="assignments">
"""
import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings
from tests.test_working_loop import ScriptedRunner, claude_stream, ok
from tests.test_working_loop_decisions import make_controller


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def _new(store, code="A01", **over):
    fields = dict(owner="duc", code=code, title="Usage ledger", goal="Aggregate stored receipts",
                  allowed_files=["src/companion/working_loop.py"], acceptance=["tests green", "no price shown"])
    fields.update(over)
    return store.create_assignment(**fields)


def test_create_and_list_newest_first_with_validation(wl, store):
    assert wl.ASSIGNMENT_STATUSES == ("assigned", "built", "reviewed", "committed")
    first = _new(store)
    second = _new(store, code="A02", tier="casual")
    assert first.status == "assigned" and first.tier == "work" and second.tier == "casual"
    assert [a.code for a in store.list_assignments(owner="duc")] == ["A02", "A01"]
    assert store.list_assignments(owner="someone-else") == []
    for bad in (dict(code=""), dict(title=""), dict(goal=""), dict(tier="urgent"), dict(code="A01")):
        with pytest.raises(wl.PolicyRefused, match="invalid_assignment"):
            _new(store, **{**dict(code="A03"), **bad})


def test_status_moves_forward_only_and_built_needs_an_owned_run(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("built it"))])
    run = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p").id)
    a = _new(store)
    with pytest.raises(wl.PolicyRefused, match="invalid_transition"):
        store.advance_assignment(a.id, owner="duc", status="reviewed")  # skipping built
    with pytest.raises(wl.PolicyRefused, match="run_not_owned"):
        store.advance_assignment(a.id, owner="duc", status="built", builder_run_id=999)
    built = store.advance_assignment(a.id, owner="duc", status="built", builder_run_id=run.id, result_sha256="a" * 64)
    assert built.status == "built" and built.builder_run_id == run.id and built.updated_at >= a.updated_at
    reviewed = store.advance_assignment(a.id, owner="duc", status="reviewed")
    with pytest.raises(wl.PolicyRefused, match="invalid_transition"):
        store.advance_assignment(a.id, owner="duc", status="committed")  # no hash
    done = store.advance_assignment(a.id, owner="duc", status="committed", commit_hash="b62b1bc")
    assert reviewed.status == "reviewed" and done.status == "committed" and done.commit_hash == "b62b1bc"
    with pytest.raises(wl.PolicyRefused, match="invalid_transition"):
        store.advance_assignment(a.id, owner="duc", status="assigned")  # never backwards
    assert store.get_assignment(a.id, owner="other") is None


def test_http_assignments(wl, store, monkeypatch):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    controller = wl.LoopController(store, ScriptedRunner([]), owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    body = {"code": "A01", "title": "Usage ledger", "goal": "Aggregate receipts", "allowed_files": ["x.py"], "acceptance": ["green"]}
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        created = client.post("/api/loop/assignments", json=body)
        assert created.status_code == 200 and created.json()["assignment"]["status"] == "assigned"
        assert client.post("/api/loop/assignments", json=body).status_code == 400  # duplicate code
        assert client.post("/api/loop/assignments", json={**body, "code": "A02", "extra": 1}).status_code == 422
        aid = created.json()["assignment"]["id"]
        bad = client.post(f"/api/loop/assignments/{aid}/advance", json={"status": "committed"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "policy_refused"
        assert client.post("/api/loop/assignments/999/advance", json={"status": "built"}).status_code == 404
        listed = client.get("/api/loop/assignments").json()["assignments"]
        assert [a["code"] for a in listed] == ["A01"]
        assert 'id="assignments"' in client.get("/loop").text
    get_settings.cache_clear()
