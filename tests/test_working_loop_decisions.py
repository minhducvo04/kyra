"""Acceptance tests for working-loop slice 2: owner decisions on reviews, reconciliation declarations (red until built).

Plan: docs/plans/2026-09-15-working-loop-decisions.md. Slice 1 contract: tests/test_working_loop.py. Only the process
boundary is faked (ScriptedRunner); store, controller and HTTP run for real on scratch storage.

Two facts this slice adds, and what they are not:
- A decision is the OWNER's approve/reject on one completed review, appended beside the model's comment. It binds the
  subject artifact hash and the reviewer's output hash at decision time; either changing makes it stale. Nothing
  parses the reviewer's prose into a decision.
- A reconciliation is the OWNER's declaration about a run whose outcome the controller could not confirm. It is
  recorded and shown; it changes nothing about the run, retries nothing, and is not proof of provider effects.

======================================================================================================
CONTRACT: companion/working_loop.py (additions; slice 1 names unchanged)
======================================================================================================

STALE_DISPATCH_GRACE_SECONDS = 30
DECISIONS = ("approve", "reject")
RECONCILIATION_OUTCOMES = ("nothing_happened", "provider_processed")
MAX_NOTE_CHARS = 2000

@dataclass(frozen=True) Decision:
    id: int; owner: str; review_id: int; subject_run_id: int; decision: str; artifact_sha256: str;
    reviewer_output_sha256: str; created_at: str
@dataclass(frozen=True) Reconciliation:
    id: int; owner: str; run_id: int; outcome: str; note_sha256: str; note_path: str; created_at: str
    # note text lives at note_path = <artifacts_dir>/<run_id>/reconciliation-<id>.md; the row holds hash + path only

LoopStore / DbLoopStore additions:
    decide_review(*, owner, review_id, decision) -> Decision
        # ReviewRefused unless: the review exists for owner; decision in DECISIONS; the review is not stale
        # (current_artifact_sha256(subject) == review.artifact_sha256 and the reviewer run's current output hash ==
        # reviewer.output_sha256). Append-only: the review row keeps verdict "comment"; a later decision is a new row.
    reviews_for(run_id, *, owner) -> list[dict]        # each dict gains "decisions": [asdict(Decision) + "stale"]
    add_reconciliation(*, owner, run_id, outcome, note) -> Reconciliation
        # persists only; eligibility lives in the controller. Writes the note file, stores its sha256 and path.
    reconciliations_for(run_id, *, owner) -> list[dict]  # asdict(Reconciliation) + "note": the text, read from file

LoopController additions:
    decide_review(review_id, *, decision) -> Decision          # owner-scoped wrapper over the store
    reconcile(run_id, *, outcome, note) -> Reconciliation
        # PolicyRefused unless the run is this owner's AND (status == "unreconciled" OR status == "dispatching" with
        # started_at older than timeout_seconds + STALE_DISPATCH_GRACE_SECONDS), outcome in RECONCILIATION_OUTCOMES,
        # note non-blank and <= MAX_NOTE_CHARS. Run status, error and receipt fields are unchanged afterwards.
        # Never calls the runner. A reconciled run still cannot be dispatched again (dispatch -> PolicyRefused).

companion/working_loop_process.py addition:
    class ProcessNotStarted(Exception)     # a ProcessRunner raises it before any process began, message = short code
    # SubprocessRunner raises ProcessNotStarted("codex_state_customized") for a customized managed Codex state
    # directory, before Popen. LoopController.dispatch maps ProcessNotStarted -> status "failed", error = str(exc).
    # FileNotFoundError keeps its slice 1 meaning (executable or auth file missing -> "provider_unavailable").

======================================================================================================
CONTRACT: companion/webapp.py (both routes sit under the existing /api/loop/ boundary: loopback, Host, Origin)
======================================================================================================
POST /api/loop/reviews/{review_id}/decision   {decision: "approve" | "reject"}   extra fields -> 422
      -> 200 {"decision": asdict(Decision)}; 404 code "not_found" for an unknown review; 409 code "decision_refused"
POST /api/loop/runs/{run_id}/reconcile        {outcome, note}                    extra fields -> 422
      -> 200 {"reconciliation": asdict(Reconciliation)}; 404 "not_found"; 409 code "reconcile_refused"
GET  /api/loop/runs/{run_id}  -> "reviews"[i]["decisions"] present; new key "reconciliations": reconciliations_for(...)
No worker or endpoint derives a decision or an outcome from model output.
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from companion import webapp
from companion.jobs import run_one
from companion.schema import loop_runs
from companion.settings import get_settings
from tests.test_working_loop import SENTINEL, ScriptedRunner, claude_stream, codex_stream, ok, sha


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def make_controller(wl, store, script, *, owner="duc", timeout_seconds=60):
    runner = ScriptedRunner(script)
    return wl.LoopController(store, runner, owner=owner, timeout_seconds=timeout_seconds), runner


def reviewed_pair(wl, store, *, reviewer_text="Reviewed. APPROVED. Ship it."):
    """A codex subject, a bound Claude review run, and the model's comment review attached, as the worker does."""
    controller, runner = make_controller(wl, store, [ok(wl, codex_stream("the draft")), ok(wl, claude_stream(reviewer_text))])
    subject = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="write").id)
    reviewer = controller.dispatch(controller.request_review(subject.id).id)
    review = store.add_review(owner="duc", subject_run_id=subject.id, reviewer_run_id=reviewer.id,
                              verdict="comment", artifact_sha256=subject.output_sha256)
    return controller, runner, subject, reviewer, review


def unreconciled_run(wl, store, *, owner="duc"):
    controller, runner = make_controller(wl, store, [wl.DispatchInterrupted("killed")], owner=owner)
    run = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p").id)
    assert run.status == "unreconciled"
    return controller, runner, run


# ----------------------------------------------------------------------------------------------------
# Decisions on reviews
# ----------------------------------------------------------------------------------------------------

def test_owner_decision_is_appended_beside_the_model_comment_and_binds_both_hashes(wl, store):
    controller, _, subject, reviewer, review = reviewed_pair(wl, store)
    assert review.verdict == "comment"
    assert store.reviews_for(subject.id, owner="duc")[0]["decisions"] == []       # prose said APPROVED; nobody decided
    approve = controller.decide_review(review.id, decision="approve")
    assert approve.decision == "approve" and approve.review_id == review.id and approve.subject_run_id == subject.id
    assert approve.artifact_sha256 == subject.output_sha256 == sha("the draft")
    assert approve.reviewer_output_sha256 == reviewer.output_sha256 == sha("Reviewed. APPROVED. Ship it.")
    assert approve.owner == "duc" and approve.created_at
    reject = controller.decide_review(review.id, decision="reject")            # the owner changed their mind: append
    listed = store.reviews_for(subject.id, owner="duc")
    assert listed[0]["verdict"] == "comment" and listed[0]["id"] == review.id   # the model's comment is untouched
    assert [(d["id"], d["decision"], d["stale"]) for d in listed[0]["decisions"]] == [
        (approve.id, "approve", False), (reject.id, "reject", False)]


@pytest.mark.parametrize("edit", ["subject", "reviewer"], ids=["subject-artifact-edited", "reviewer-output-edited"])
def test_decision_refused_once_either_bound_artifact_changed_and_earlier_decisions_go_stale(wl, store, tmp_path, edit):
    controller, _, subject, reviewer, review = reviewed_pair(wl, store)
    earlier = controller.decide_review(review.id, decision="approve")
    target = tmp_path / "artifacts" / str(subject.id if edit == "subject" else reviewer.id) / "output.md"
    target.write_text(target.read_text() + " (edited afterwards)")
    with pytest.raises(wl.ReviewRefused):
        controller.decide_review(review.id, decision="approve")
    decisions = store.reviews_for(subject.id, owner="duc")[0]["decisions"]
    assert [(d["id"], d["stale"]) for d in decisions] == [(earlier.id, True)]


def test_decision_is_owner_scoped_and_only_the_two_owner_values_exist(wl, store):
    _, _, subject, _, review = reviewed_pair(wl, store)
    other, other_runner = make_controller(wl, store, [], owner="someone-else")
    with pytest.raises(wl.ReviewRefused):
        other.decide_review(review.id, decision="approve")
    mine, _ = make_controller(wl, store, [])
    for bad in ("comment", "approved", "APPROVE", "", None):
        with pytest.raises(wl.ReviewRefused):
            mine.decide_review(review.id, decision=bad)
    with pytest.raises(wl.ReviewRefused):
        mine.decide_review(review.id + 999, decision="approve")
    assert store.reviews_for(subject.id, owner="duc")[0]["decisions"] == []
    assert other_runner.calls == []


# ----------------------------------------------------------------------------------------------------
# Reconciliation declarations
# ----------------------------------------------------------------------------------------------------

def test_reconciling_an_unreconciled_run_records_the_declaration_privately_and_changes_nothing_else(wl, store, tmp_path):
    controller, runner, run = unreconciled_run(wl, store)
    before = store.get_run(run.id, owner="duc")
    note = f"Checked the provider's own history: the answer was produced. {SENTINEL}"
    rec = controller.reconcile(run.id, outcome="provider_processed", note=note)
    assert rec.run_id == run.id and rec.outcome == "provider_processed" and rec.owner == "duc"
    assert rec.note_sha256 == sha(note)
    note_path = Path(rec.note_path)
    assert note_path.parent == tmp_path / "artifacts" / str(run.id) and note_path.read_text() == note
    after = store.get_run(run.id, owner="duc")
    assert after == before                                                        # status, error, receipt untouched
    assert after.status == "unreconciled"
    assert SENTINEL.encode() not in (tmp_path / "loop.db").read_bytes()
    listed = store.reconciliations_for(run.id, owner="duc")
    assert [(r["id"], r["outcome"], r["note"]) for r in listed] == [(rec.id, "provider_processed", note)]
    assert len(runner.calls) == 1                                                 # nothing was executed by reconciling
    with pytest.raises(wl.PolicyRefused):                                         # and still no re-dispatch
        controller.dispatch(run.id)
    assert len(runner.calls) == 1
    assert store.reconciliations_for(run.id, owner="someone-else") == []


def test_stuck_dispatching_run_can_be_reconciled_only_after_timeout_plus_grace(wl, store):
    controller, runner = make_controller(wl, store, [], timeout_seconds=60)
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    store.claim(run.id, owner="duc")                                              # the server died here
    assert store.get_run(run.id, owner="duc").status == "dispatching"
    with pytest.raises(wl.PolicyRefused):                                         # recent: the process may be live
        controller.reconcile(run.id, outcome="nothing_happened", note="server restarted")
    too_early = (datetime.now(UTC) - timedelta(seconds=60 + wl.STALE_DISPATCH_GRACE_SECONDS - 5)).isoformat()
    with store.engine.begin() as conn:
        conn.execute(update(loop_runs).where(loop_runs.c.id == run.id).values(started_at=too_early))
    with pytest.raises(wl.PolicyRefused):
        controller.reconcile(run.id, outcome="nothing_happened", note="server restarted")
    stale = (datetime.now(UTC) - timedelta(seconds=60 + wl.STALE_DISPATCH_GRACE_SECONDS + 5)).isoformat()
    with store.engine.begin() as conn:
        conn.execute(update(loop_runs).where(loop_runs.c.id == run.id).values(started_at=stale))
    rec = controller.reconcile(run.id, outcome="nothing_happened", note="server restarted")
    assert rec.outcome == "nothing_happened"
    assert store.get_run(run.id, owner="duc").status == "dispatching"             # the declaration does not relabel it
    assert runner.calls == []


@pytest.mark.parametrize("state", ["queued", "done", "failed"])
def test_reconcile_refuses_runs_with_a_known_or_pending_outcome(wl, store, state):
    script = {"queued": [], "done": [ok(wl, claude_stream("x"))], "failed": [FileNotFoundError("gone")]}[state]
    controller, runner = make_controller(wl, store, script)
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    if state != "queued":
        run = controller.dispatch(run.id)
    assert run.status == state
    with pytest.raises(wl.PolicyRefused):
        controller.reconcile(run.id, outcome="nothing_happened", note="n")
    assert store.reconciliations_for(run.id, owner="duc") == []


def test_reconcile_refuses_other_owners_bad_outcomes_and_bad_notes(wl, store):
    _, _, run = unreconciled_run(wl, store)
    other, _ = make_controller(wl, store, [], owner="someone-else")
    with pytest.raises(wl.PolicyRefused):
        other.reconcile(run.id, outcome="nothing_happened", note="not mine")
    mine, _ = make_controller(wl, store, [])
    for outcome in ("retry", "done", "", None):
        with pytest.raises(wl.PolicyRefused):
            mine.reconcile(run.id, outcome=outcome, note="n")
    for note in ("", "   \n", "x" * (wl.MAX_NOTE_CHARS + 1), None):
        with pytest.raises(wl.PolicyRefused):
            mine.reconcile(run.id, outcome="nothing_happened", note=note)
    with pytest.raises(wl.PolicyRefused):
        mine.reconcile(run.id + 999, outcome="nothing_happened", note="n")
    assert store.reconciliations_for(run.id, owner="duc") == []


# ----------------------------------------------------------------------------------------------------
# Pre-start failures are failed, not unreconciled (carried over from the slice 1 review)
# ----------------------------------------------------------------------------------------------------

def test_process_not_started_is_failed_with_its_code_and_never_unreconciled(wl, store):
    controller, runner = make_controller(wl, store, [wl.ProcessNotStarted("codex_state_customized")])
    run = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p").id)
    assert run.status == "failed" and run.error == "codex_state_customized"
    assert len(runner.calls) == 1
    with pytest.raises(wl.PolicyRefused):                                         # a failed run is not reconcilable
        controller.reconcile(run.id, outcome="nothing_happened", note="n")


def test_customized_codex_state_raises_process_not_started_before_any_process(wl, tmp_path, monkeypatch):
    from companion import working_loop_process as process

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"auth_mode":"chatgpt"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(process, "DATA_DIR", tmp_path / "data")
    state = tmp_path / "data" / "working_loop" / "codex-state"
    state.mkdir(parents=True)
    (state / "AGENTS.md").write_text("Someone added instructions here")
    command = [str(tmp_path / "no-such-codex"), "exec", "--ignore-user-config", "--json", "-"]
    with pytest.raises(wl.ProcessNotStarted) as excinfo:
        wl.SubprocessRunner().run(command, stdin="p", timeout_seconds=5)
    assert str(excinfo.value) == "codex_state_customized"                         # not FileNotFoundError: never started


# ----------------------------------------------------------------------------------------------------
# HTTP: both mutations through the gated loop API, inspectable in the detail view
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def http(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([ok(wl, codex_stream("the draft")), ok(wl, claude_stream("APPROVED, says the model")),
                             wl.DispatchInterrupted("killed")])
    controller = wl.LoopController(store, runner, owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        yield client, store, runner
    get_settings.cache_clear()


def _drain():
    while run_one(webapp._queue, webapp.HANDLERS):
        pass


def test_http_decision_and_reconciliation_are_recorded_by_the_owner_and_shown_in_detail(http):
    client, store, runner = http
    subject = client.post("/api/loop/runs", json={"choice": "codex-default", "prompt": "write", "topic": "t"}).json()["run"]
    _drain()
    review_run = client.post(f"/api/loop/runs/{subject['id']}/review").json()["run"]
    _drain()
    detail = client.get(f"/api/loop/runs/{subject['id']}").json()
    review = detail["reviews"][0]
    assert review["verdict"] == "comment" and review["decisions"] == [] and detail["reconciliations"] == []
    assert "APPROVED" in client.get(f"/api/loop/runs/{review_run['id']}").json()["artifact"]["output"]   # prose only

    decided = client.post(f"/api/loop/reviews/{review['id']}/decision", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"]["review_id"] == review["id"]
    detail = client.get(f"/api/loop/runs/{subject['id']}").json()
    assert [(d["decision"], d["stale"]) for d in detail["reviews"][0]["decisions"]] == [("approve", False)]
    assert detail["reviews"][0]["decisions"][0]["artifact_sha256"] == detail["run"]["output_sha256"]

    refused = client.post(f"/api/loop/runs/{subject['id']}/reconcile", json={"outcome": "nothing_happened", "note": "n"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "reconcile_refused"   # done runs are not

    lost = client.post("/api/loop/runs", json={"choice": "claude-fable-high", "prompt": "q", "topic": "t"}).json()["run"]
    _drain()
    assert client.get(f"/api/loop/runs/{lost['id']}").json()["run"]["status"] == "unreconciled"
    note = f"Provider history shows no answer. {SENTINEL}"
    rec = client.post(f"/api/loop/runs/{lost['id']}/reconcile", json={"outcome": "nothing_happened", "note": note})
    assert rec.status_code == 200, rec.text
    assert rec.json()["reconciliation"]["note_sha256"] == sha(note) and "note" not in rec.json()["reconciliation"]
    detail = client.get(f"/api/loop/runs/{lost['id']}").json()
    assert detail["run"]["status"] == "unreconciled"
    assert [(r["outcome"], r["note"]) for r in detail["reconciliations"]] == [("nothing_happened", note)]
    assert SENTINEL not in client.get("/api/loop/runs").text                        # the list stays content-free
    assert len(runner.calls) == 3                                                    # nothing executed by either action


@pytest.mark.parametrize("path,body,expected", [
    pytest.param("/api/loop/reviews/1/decision", {"decision": "comment"}, 422, id="decision-comment-is-not-an-owner-value"),
    pytest.param("/api/loop/reviews/1/decision", {"decision": "approve", "owner": "father"}, 422, id="decision-extra-field"),
    pytest.param("/api/loop/reviews/424242/decision", {"decision": "approve"}, 404, id="decision-unknown-review"),
    pytest.param("/api/loop/runs/1/reconcile", {"outcome": "retry", "note": "n"}, 422, id="reconcile-bad-outcome"),
    pytest.param("/api/loop/runs/1/reconcile", {"outcome": "nothing_happened", "note": "n", "run_id": 2}, 422, id="reconcile-extra-field"),
    pytest.param("/api/loop/runs/424242/reconcile", {"outcome": "nothing_happened", "note": "n"}, 404, id="reconcile-unknown-run"),
])
def test_http_mutations_validate_bodies_and_ids(http, path, body, expected):
    client, store, runner = http
    res = client.post(path, json=body)
    assert res.status_code == expected, res.text
    if expected == 404:
        assert res.json()["error"]["code"] == "not_found"
    assert runner.calls == []


def test_http_mutations_sit_behind_the_loop_boundary(http):
    client, _, runner = http
    for path, body in [("/api/loop/reviews/1/decision", {"decision": "approve"}),
                       ("/api/loop/runs/1/reconcile", {"outcome": "nothing_happened", "note": "n"})]:
        res = client.post(path, json=body, headers={"Origin": "http://evil.example"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "bad_origin", path
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("192.168.1.42", 51000)) as lan:
        res = lan.post("/api/loop/reviews/1/decision", json={"decision": "approve"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "loopback_only"
    assert runner.calls == []
    assert json.loads(client.get("/api/loop/runs").text)["runs"] == []
    # With a legitimate origin the routes exist and answer in the API's error shape, not FastAPI's default 404.
    ok_origin = {"Origin": "http://127.0.0.1:8420"}
    assert client.post("/api/loop/reviews/424242/decision", json={"decision": "approve"}, headers=ok_origin).json()["error"]["code"] == "not_found"
    assert client.post("/api/loop/runs/424242/reconcile", json={"outcome": "nothing_happened", "note": "n"}, headers=ok_origin).json()["error"]["code"] == "not_found"
