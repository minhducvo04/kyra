"""Regressions from Claude's independent review of slice 4 (bounded review context), 2026-09-15.

Two gaps, both raised by Codex in the review request and confirmed in the diff:

1. The subject's own request is not bound after a review is requested. request_review checks the subject's prompt
   hash once, but dispatch, add_review, decide_review and the stale flag compare only the subject's OUTPUT hash. An
   edited request under an unchanged answer therefore leaves a review looking current, although the reviewer judged
   the answer against a request that no longer exists. The receipt already carries the subject's input_sha256, so
   the fix is a check, not a schema change.
2. A lineage cycle that closes exactly at the depth bound is labelled "omitted" instead of refused. The next id is
   already known when the walk stops, so refusing a repeated id costs no extra read beyond the bound.
"""
import pytest
from sqlalchemy import update

from companion.schema import loop_runs
from tests.test_working_loop import ScriptedRunner, claude_stream, codex_stream, ok


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def _subject(wl, store, answer="answer"):
    controller = wl.LoopController(store, ScriptedRunner([ok(wl, claude_stream(answer))]), owner="duc", timeout_seconds=60)
    subject = controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="the request").id)
    assert subject.status == "done"
    return controller, subject


def test_an_edited_subject_request_stops_a_queued_review_before_any_process(wl, store, tmp_path):
    controller, subject = _subject(wl, store)
    controller.runner = ScriptedRunner([ok(wl, codex_stream("never sent"))])
    review = controller.request_review(subject.id)
    (tmp_path / "artifacts" / str(subject.id) / "prompt.md").write_text("the request, edited afterwards")
    result = controller.dispatch(review.id)
    assert result.status == "failed" and result.error == "subject_changed"
    assert controller.runner.calls == []


def test_an_edited_subject_request_makes_a_completed_review_and_its_decision_stale_and_refuses_new_evidence(wl, store, tmp_path):
    controller, subject = _subject(wl, store)
    controller.runner = ScriptedRunner([ok(wl, codex_stream("first review")), ok(wl, codex_stream("second review"))])
    first = controller.dispatch(controller.request_review(subject.id).id)
    attached = store.add_review(owner="duc", subject_run_id=subject.id, reviewer_run_id=first.id, verdict="comment",
                                artifact_sha256=subject.output_sha256)
    decided = controller.decide_review(attached.id, decision="approve")
    second = controller.dispatch(controller.request_review(subject.id).id)
    assert second.status == "done"
    prompt_path = tmp_path / "artifacts" / str(subject.id) / "prompt.md"
    prompt_path.write_text("the request, edited afterwards")                   # answer file untouched
    assert store.current_artifact_sha256(subject.id, owner="duc") == subject.output_sha256
    listed = store.reviews_for(subject.id, owner="duc")
    assert listed[0]["id"] == attached.id and listed[0]["stale"] is True
    assert [(d["id"], d["stale"]) for d in listed[0]["decisions"]] == [(decided.id, True)]
    with pytest.raises(wl.ReviewRefused):
        controller.decide_review(attached.id, decision="reject")
    with pytest.raises(wl.ReviewRefused):                                        # cannot attach on a changed request
        store.add_review(owner="duc", subject_run_id=subject.id, reviewer_run_id=second.id, verdict="comment",
                         artifact_sha256=subject.output_sha256)
    with pytest.raises(wl.PolicyRefused):                                        # and no new review of it either
        controller.request_review(subject.id)
    prompt_path.write_text("the request")                                        # restored: evidence is current again
    assert store.reviews_for(subject.id, owner="duc")[0]["stale"] is False


def test_a_cycle_closing_exactly_at_the_depth_bound_is_refused_without_reading_beyond_it(wl, store, tmp_path):
    depth = wl.REVIEW_CONTEXT_MAX_TURNS
    answers = [f"turn {i}" for i in range(depth + 1)]                            # depth ancestors + the subject
    controller = wl.LoopController(store, ScriptedRunner([ok(wl, claude_stream(a)) for a in answers]), owner="duc", timeout_seconds=60)
    runs = [controller.dispatch(controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="q0").id)]
    for i in range(1, len(answers)):
        runs.append(controller.dispatch(controller.continue_run(runs[-1].id, prompt=f"q{i}").id))
    subject, oldest = runs[-1], runs[0]
    with store.engine.begin() as conn:                                            # corrupt lineage: the oldest points at the subject
        conn.execute(update(loop_runs).where(loop_runs.c.id == oldest.id).values(continued_from_run_id=subject.id))
    controller.runner = ScriptedRunner([ok(wl, codex_stream("never sent"))])
    rows_before = len(store.list_runs(owner="duc"))
    with pytest.raises(wl.PolicyRefused):
        controller.request_review(subject.id)
    assert controller.runner.calls == [] and len(store.list_runs(owner="duc")) == rows_before
