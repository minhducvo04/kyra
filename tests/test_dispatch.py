"""Dispatch from the page: an assignment plans, builds and asks for review (red until Codex builds W2).

Plan: docs/plans/2026-09-16-kyra-as-front-door.md, W2. Every external step is behind the confirm gate;
nothing runs because a model said so; the build runs in its own worktree on its own branch.

CONTRACT (companion/dispatch.py)
  loop_assignments gains nullable plan_run_id, review_run_id, worktree (Text) (schema + one Alembic revision);
      Assignment carries them
  class Dispatcher:
      __init__(store, runner: ProcessRunner, *, owner, repo_root: Path, worktrees_dir: Path, timeout_seconds=1800)
      plan(assignment_id) -> ExecutionRecord
          PolicyRefused("assignment_not_plannable") unless status is "assigned" and plan_run_id is None;
          creates a queued run: choice "claude-fable-high", topic "assignment:<code>", tier = assignment.tier,
          prompt = PLAN_INSTRUCTIONS + goal, allowed files and acceptance lines; stores plan_run_id
      build(assignment_id, *, confirmed: bool) -> BuildReceipt
          PolicyRefused("confirmation_required") unless confirmed is True (checked before anything else);
          PolicyRefused("plan_missing") unless the plan run is done with an output;
          creates worktree <worktrees_dir>/<code> on branch session/<YYYY-MM-DD>-<code> from repo_root's HEAD
          (real git; PolicyRefused("worktree_exists") if the directory exists); writes the brief to
          <worktree>/data/private_docs/assignment-<code>.md (goal, allowed files, acceptance, the plan output,
          result path); creates a run (choice "codex-default", prompt = the brief text), claims it, runs
          runner.run(codex_exec_command(worktree, brief_path), stdin="", timeout_seconds), finishes it
          (status "done" on returncode 0 else "failed", output = stdout); advances the assignment to "built"
          with builder_run_id = that run and result_sha256 = sha256 of the result file when present, and
          stores worktree; returns BuildReceipt(assignment_id, worktree: str, branch: str, returncode: int,
          files_changed: list[str] (git status --porcelain paths in the worktree), result_present: bool)
      review(assignment_id) -> ExecutionRecord
          PolicyRefused("not_built") unless status is "built"; creates a queued run for "claude-fable-high",
          topic "assignment:<code>", prompt = REVIEW_INSTRUCTIONS + acceptance lines + `git diff` of the
          worktree against its branch point, truncated to fit MAX_PROMPT_BYTES with an omission note;
          stores review_run_id
  codex_exec_command(worktree: Path, brief: Path) -> list[str]
      contains "exec", "-C", str(worktree), "-s", "workspace-write", "--skip-git-repo-check", "--json"
      and a final prompt argument mentioning str(brief) and "Do not edit tests"
  webapp: POST /api/loop/assignments/{id}/plan -> {"assignment", "run"}; POST .../build {"confirmed": StrictBool}
      -> {"assignment", "receipt"}, 409 "confirmation_required" when false; POST .../review -> {"assignment", "run"};
      400 "policy_refused" on other refusals; 404 "not_found"; the build route runs the build on the job queue
      as kind "loop_build" (the test calls the Dispatcher directly)
  web/loop.js: assignment cards carry buttons data-action="plan", "build" (confirm() first) and "review"
"""
import subprocess
from pathlib import Path

import pytest

from tests.test_working_loop import ScriptedRunner, claude_stream, codex_stream, ok


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def dp():
    import companion.dispatch as dispatch

    return dispatch


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root"], cwd=root, check=True)
    (root / "hello.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "hello.py"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "hello"], cwd=root, check=True)
    return root


def _assignment(store, code="W9"):
    return store.create_assignment(owner="duc", code=code, title="Tiny slice", goal="Make hello say two",
                                   allowed_files=["hello.py"], acceptance=["hello.py sets x to 2"])


def _dispatcher(dp, wl, store, runner, repo, tmp_path):
    return dp.Dispatcher(store, runner, owner="duc", repo_root=repo, worktrees_dir=tmp_path / "wt", timeout_seconds=60)


def _drain(wl, store, runner):
    """Dispatch every queued run through the controller, as the worker would."""
    controller = wl.LoopController(store, runner, owner="duc", timeout_seconds=60)
    for run in store.list_runs(owner="duc"):
        if run.status == "queued":
            controller.dispatch(run.id)


def test_plan_creates_a_bound_claude_run(dp, wl, tmp_path, repo):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    a = _assignment(store)
    d = _dispatcher(dp, wl, store, ScriptedRunner([]), repo, tmp_path)
    run = d.plan(a.id)
    assert run.status == "queued" and run.developer == "Anthropic" and run.topic == "assignment:W9" and run.tier == "work"
    prompt = store.read_artifact(run.id, owner="duc")["prompt"]
    assert "Make hello say two" in prompt and "hello.py sets x to 2" in prompt and dp.PLAN_INSTRUCTIONS in prompt
    assert store.get_assignment(a.id, owner="duc").plan_run_id == run.id
    with pytest.raises(wl.PolicyRefused, match="assignment_not_plannable"):
        d.plan(a.id)


def test_plan_prompt_states_tools_are_unavailable(dp, wl, tmp_path):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    assignment = _assignment(store)
    dispatcher = _dispatcher(dp, wl, store, ScriptedRunner([]), tmp_path, tmp_path)
    run = dispatcher.plan(assignment.id)
    prompt = store.read_artifact(run.id, owner="duc")["prompt"]
    assert "No tools, files or commands are available." in prompt


def test_build_is_gated_then_runs_codex_in_a_fresh_worktree(dp, wl, tmp_path, repo):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    a = _assignment(store)
    runner = ScriptedRunner([ok(wl, claude_stream("1. edit hello.py so x = 2")), ok(wl, codex_stream("changed hello.py"))])
    d = _dispatcher(dp, wl, store, runner, repo, tmp_path)
    with pytest.raises(wl.PolicyRefused, match="confirmation_required"):
        d.build(a.id, confirmed=False)
    with pytest.raises(wl.PolicyRefused, match="plan_missing"):
        d.build(a.id, confirmed=True)
    d.plan(a.id)
    _drain(wl, store, runner)

    wt = tmp_path / "wt" / "W9"  # where the dispatcher puts this assignment's worktree

    def fake_codex():  # the "build" edits a file in the worktree, as codex exec would
        (wt / "hello.py").write_text("x = 2\n")
        (wt / "data" / "private_docs").mkdir(parents=True, exist_ok=True)
        (wt / "data" / "private_docs" / "assignment-W9-result.md").write_text("done\n")
    runner._on_run = fake_codex
    receipt = d.build(a.id, confirmed=True)
    assert receipt.returncode == 0 and receipt.files_changed == ["hello.py"] and receipt.result_present is True
    assert receipt.branch.startswith("session/") and receipt.branch.endswith("-W9")
    assert Path(receipt.worktree).is_dir() and (Path(receipt.worktree) / "hello.py").read_text() == "x = 2\n"
    cmd = runner.calls[-1]["command"]
    assert cmd[cmd.index("-C") + 1] == receipt.worktree and "--skip-git-repo-check" in cmd and "workspace-write" in cmd
    assert "Do not edit tests" in cmd[-1] and "assignment-W9.md" in cmd[-1]
    after = store.get_assignment(a.id, owner="duc")
    assert after.status == "built" and after.worktree == receipt.worktree and after.result_sha256 and after.builder_run_id
    built_run = store.get_run(after.builder_run_id, owner="duc")
    assert built_run.developer == "OpenAI" and built_run.status == "done"
    with pytest.raises(wl.PolicyRefused, match="worktree_exists"):
        d.build(a.id, confirmed=True)


def test_build_stores_final_message_and_private_raw_stream(dp, wl, tmp_path, repo):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    assignment = _assignment(store)
    stream = codex_stream("Updated hello.py and verified the change.")
    runner = ScriptedRunner([ok(wl, claude_stream("plan")), ok(wl, stream)])
    dispatcher = _dispatcher(dp, wl, store, runner, repo, tmp_path)
    dispatcher.plan(assignment.id)
    _drain(wl, store, runner)
    receipt = dispatcher.build(assignment.id, confirmed=True)
    built = store.get_assignment(assignment.id, owner="duc")
    assert store.read_artifact(built.builder_run_id, owner="duc")["output"] == "Updated hello.py and verified the change."
    raw_stream = Path(receipt.raw_stream_path)
    assert raw_stream == Path(receipt.worktree) / "data" / "private_docs" / "raw-stream.jsonl"
    assert raw_stream.read_text() == stream
    assert raw_stream.stat().st_mode & 0o777 == 0o600
    assert receipt.files_changed == []


def test_build_stores_final_message_after_commands(dp, wl, tmp_path, repo):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    assignment = _assignment(store)
    stream = '{"type":"item.completed","item":{"type":"agent_message","text":"Starting"}}\n'
    stream += codex_stream("Updated hello.py and verified the change.", tool_event=True)
    stream += 'not JSON\nnull\n{"type":"item.completed","item":null}\n'
    runner = ScriptedRunner([ok(wl, claude_stream("plan")), ok(wl, stream)])
    dispatcher = _dispatcher(dp, wl, store, runner, repo, tmp_path)
    dispatcher.plan(assignment.id)
    _drain(wl, store, runner)
    dispatcher.build(assignment.id, confirmed=True)
    built = store.get_assignment(assignment.id, owner="duc")
    assert store.read_artifact(built.builder_run_id, owner="duc")["output"] == "Updated hello.py and verified the change."


def test_review_carries_the_diff_to_the_other_company(dp, wl, tmp_path, repo):
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    a = _assignment(store)
    runner = ScriptedRunner([ok(wl, claude_stream("plan")), ok(wl, codex_stream("built"))])
    d = _dispatcher(dp, wl, store, runner, repo, tmp_path)
    with pytest.raises(wl.PolicyRefused, match="not_built"):
        d.review(a.id)
    d.plan(a.id)
    _drain(wl, store, runner)
    runner._on_run = lambda: (tmp_path / "wt" / "W9" / "hello.py").write_text("x = 2\n")
    d.build(a.id, confirmed=True)
    review = d.review(a.id)
    prompt = store.read_artifact(review.id, owner="duc")["prompt"]
    assert review.developer == "Anthropic" and "+x = 2" in prompt and "-x = 1" in prompt and "hello.py sets x to 2" in prompt
    assert store.get_assignment(a.id, owner="duc").review_run_id == review.id


def test_command_shape(dp, tmp_path):
    cmd = dp.codex_exec_command(tmp_path / "wt", tmp_path / "wt" / "data" / "private_docs" / "assignment-X.md")
    assert "exec" in cmd and cmd[cmd.index("-C") + 1] == str(tmp_path / "wt") and cmd[cmd.index("-s") + 1] == "workspace-write"
    assert "--json" in cmd and "--skip-git-repo-check" in cmd and "assignment-X.md" in cmd[-1] and "Do not edit tests" in cmd[-1]


def test_http_routes_and_page_buttons(dp, wl, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from companion import webapp
    from companion.settings import get_settings

    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    from sqlalchemy import create_engine

    from companion.jobs import DbJobQueue
    from companion.schema import metadata

    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    controller = wl.LoopController(store, ScriptedRunner([]), owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    eng = create_engine(f"sqlite:///{tmp_path / 'q.db'}", connect_args={"check_same_thread": False})
    metadata.create_all(eng)
    monkeypatch.setattr(webapp, "_queue", DbJobQueue(eng))  # a private queue: no job leaks into later tests
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        a = client.post("/api/loop/assignments", json={"code": "W9", "title": "t", "goal": "g", "allowed_files": [], "acceptance": ["a"]}).json()["assignment"]
        planned = client.post(f"/api/loop/assignments/{a['id']}/plan")
        assert planned.status_code == 200 and planned.json()["run"]["topic"] == "assignment:W9"
        gate = client.post(f"/api/loop/assignments/{a['id']}/build", json={"confirmed": False})
        assert gate.status_code == 409 and gate.json()["error"]["code"] == "confirmation_required"
        assert client.post(f"/api/loop/assignments/{a['id']}/review").status_code == 400
        assert client.post("/api/loop/assignments/999/plan").status_code == 404
    get_settings.cache_clear()
    js = (Path(__file__).resolve().parent.parent / "web" / "loop.js").read_text(encoding="utf-8")
    for action in ("plan", "build", "review"):
        assert f'data-action="{action}"' in js or f"data-action=\\\"{action}\\\"" in js or f"dataset.action = \"{action}\"" in js or f"'{action}'" in js
