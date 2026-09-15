"""Regression from Claude's independent review of slice 3 (continuation), 2026-09-15.

The contract's dispatch-time revalidation is: parent still done, its files still hash to the receipt, same native
session id, current policy. The build also re-runs the "newest done run in scope" rule at dispatch. That rule
belongs at reservation time only: once a parent is reserved, a fresh question that happens to finish first in the
same topic must not kill the queued follow-up, label it "parent_changed" (nothing about the parent changed), and
burn the parent's single continuation slot.
"""
import pytest

from tests.test_working_loop import CLAUDE_SESSION, ScriptedRunner, claude_stream, ok


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


def test_a_fresh_run_finishing_in_the_same_scope_does_not_kill_a_reserved_follow_up(wl, tmp_path):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([ok(wl, claude_stream("parent")), ok(wl, claude_stream("unrelated fresh answer")),
                             ok(wl, claude_stream("the follow-up"))])
    controller = wl.LoopController(store, runner, owner="duc", timeout_seconds=60)
    parent = controller.dispatch(controller.request(project="kyra", topic="shared", choice_key="claude-fable-high", prompt="q1").id)
    child = controller.continue_run(parent.id, prompt="q1, continued")        # reserved while still queued
    fresh = controller.dispatch(controller.request(project="kyra", topic="shared", choice_key="claude-fable-high", prompt="q2").id)
    assert fresh.status == "done" and fresh.id > parent.id                    # a newer done run now exists in scope
    assert controller.can_continue(store.get_run(parent.id, owner="duc")) is False   # still reserved, as intended
    result = controller.dispatch(child.id)
    assert result.status == "done", result.error                              # the parent did not change
    assert result.provider_session_id == CLAUDE_SESSION == result.requested_session_id
    assert "--resume" in runner.calls[2]["command"] and runner.calls[2]["stdin"] == "q1, continued"
    assert store.read_artifact(child.id, owner="duc")["output"] == "the follow-up"
