"""Regressions for issues raised by Claude's independent build review."""
import json

from sqlalchemy import update

from companion import webapp
from companion.schema import loop_runs
from companion.working_loop import DbLoopStore, LoopController
from tests.test_working_loop import ScriptedRunner, claude_stream, ok


def controller_at(tmp_path):
    import companion.working_loop as wl
    runner = ScriptedRunner([ok(wl, claude_stream("answer"))])
    store = DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    return LoopController(store, runner, owner="duc"), runner


def test_preflight_missing_artifact_is_failed_before_any_process(tmp_path):
    controller, runner = controller_at(tmp_path)
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="request")
    (tmp_path / "artifacts" / str(run.id) / "prompt.md").unlink()
    result = controller.dispatch(run.id)
    assert result.status == "failed" and result.error == "input_unavailable"
    assert runner.calls == []


def test_changed_policy_job_becomes_failed_instead_of_waiting_forever(tmp_path, monkeypatch):
    controller, runner = controller_at(tmp_path)
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="request")
    with controller.store.engine.begin() as conn:
        conn.execute(update(loop_runs).where(loop_runs.c.id == run.id).values(policy_version="old"))
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    webapp.HANDLERS["loop_dispatch"]({"run_id": run.id}, lambda text: None)
    result = controller.store.get_run(run.id, owner="duc")
    assert result.status == "failed" and result.error == "policy_changed"
    assert runner.calls == []


def test_claude_final_text_split_across_events_is_reassembled(tmp_path):
    import companion.working_loop as wl
    events = [json.loads(line) for line in claude_stream("first second").splitlines()]
    index = next(i for i, e in enumerate(events) if e['type'] == 'assistant')
    original = events[index]
    first = json.loads(json.dumps(original))
    first['message']['content'] = [{'type': 'text', 'text': 'first '}]
    original['message']['content'] = [{'type': 'text', 'text': 'second'}]
    events.insert(index, first)
    controller, _ = controller_at(tmp_path)
    controller.runner = ScriptedRunner([ok(wl, '\n'.join(json.dumps(e) for e in events))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="request")
    result = controller.dispatch(run.id)
    assert result.status == "done"
    assert controller.store.read_artifact(run.id, owner="duc")['output'] == 'first second'
