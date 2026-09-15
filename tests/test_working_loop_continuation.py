"""Acceptance tests for working-loop slice 3: explicit native topic continuation (red until built).

Plan: docs/plans/2026-09-15-working-loop-continuation.md. Slices 1 and 2: tests/test_working_loop.py,
tests/test_working_loop_decisions.py. Only the process boundary is faked; store, controller and HTTP run for real.

What continuation is: the owner asks to continue ONE specific completed answer (a parent run id from the browser,
never a native session id). The controller resumes that run's recorded native session with the same choice and the
same isolation flags, records the link, and verifies the provider came back on the requested session. What it is
not: automatic, retried, available to reviews (always fresh), or open to a parent that was already continued.

======================================================================================================
CONTRACT: companion/working_loop.py (additions; earlier names unchanged)
======================================================================================================

POLICY_VERSION  advances past "2026-09-15.1" (runs recorded under an older policy stay readable, cannot be parents)

ExecutionRecord gains:  continued_from_run_id: int | None = None;  requested_session_id: str | None = None
loop_runs gains the two columns; continued_from_run_id is UNIQUE (NULLs excluded, as both SQLite and Postgres do),
so the database, not Python, guarantees at most one child per parent. Additive migration on head f915b18d3e42.

LoopController.continue_run(parent_run_id, *, prompt) -> ExecutionRecord
    # PolicyRefused (zero rows, zero runner calls) unless ALL hold:
    #   parent is this owner's; status "done"; review_subject_id is None; provider_session_id is a non-empty str;
    #   policy_version == POLICY_VERSION; parent prompt.md and output.md still hash to input_sha256/output_sha256;
    #   parent is the newest "done" run for (owner, project, topic, provider) (find_session's answer);
    #   parent has no child yet (the UNIQUE column: a concurrent duplicate loses with PolicyRefused).
    # Child: same choice_key/project/topic as the parent, status "queued", continued_from_run_id=parent.id,
    #   requested_session_id=parent.provider_session_id, prompt written as usual. The reservation is never released:
    #   a failed, mismatched or unreconciled child still blocks a second continuation of that parent.
LoopController.can_continue(run) -> bool          # the same rules, minus the reservation race; used by the detail view
LoopController.dispatch(child):
    # before the process: revalidate parent (done, hashes intact, same provider_session_id, current policy) else
    #   "failed", error "parent_changed", zero processes. Command = command_for(choice, resume_session_id=...).
    # after the process: stream session id must equal requested_session_id, else status "mismatch",
    #   error "session_mismatch" (output kept). A stream without a session id is already "failed"/malformed.
request_review(subject) always creates a fresh run: continued_from_run_id None, requested_session_id None, even
    when the subject is a continuation. continue_run(<a review run>) -> PolicyRefused.

companion/working_loop_process.py:
    command_for(choice, *, resume_session_id: str | None = None) -> list[str]
    # Claude: every element of the fresh argv is still present, plus "--resume", <session_id>.
    # Codex:  every element of the fresh argv is still present, plus "resume", <thread_id> after "exec".
    # Prompt stays on stdin. No other change to isolation (managed state directory, tools off, read-only).

======================================================================================================
CONTRACT: companion/webapp.py (under the existing /api/loop/ boundary)
======================================================================================================
POST /api/loop/runs/{parent_id}/continue   {prompt: 1..65536 chars}   extra fields -> 422 (a session id or choice in the body is 422)
     -> 200 {"run": asdict(child)} status "queued", job enqueued; 404 "not_found"; 409 code "continuation_refused"
GET  /api/loop/runs/{id}   -> "can_continue": bool; run carries continued_from_run_id and requested_session_id
The page: a "Continue this conversation" control on an eligible completed answer, naming the parent, and a way back
to a fresh request. No automatic continuation.
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from companion import webapp
from companion.jobs import run_one
from companion.schema import loop_runs
from companion.settings import get_settings
from tests.test_working_loop import CLAUDE_SESSION, CODEX_THREAD, ScriptedRunner, claude_stream, codex_stream, ok

OTHER_CLAUDE_SESSION = "00000000-0000-4000-8000-00000000beef"
OTHER_CODEX_THREAD = "00000000-0000-4000-8000-000000000bad"


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def make_controller(wl, store, script, *, owner="duc"):
    runner = ScriptedRunner(script)
    return wl.LoopController(store, runner, owner=owner, timeout_seconds=60), runner


def done_parent(controller, *, choice_key="claude-fable-high", topic="t", prompt="first question"):
    run = controller.dispatch(controller.request(project="kyra", topic=topic, choice_key=choice_key, prompt=prompt).id)
    assert run.status == "done" and run.provider_session_id
    return run


def _backdate(store, run_id, **values):
    with store.engine.begin() as conn:
        conn.execute(update(loop_runs).where(loop_runs.c.id == run_id).values(**values))


# ----------------------------------------------------------------------------------------------------
# Creating a continuation
# ----------------------------------------------------------------------------------------------------

def test_policy_version_advanced_so_pre_isolation_runs_stay_readable_but_cannot_be_parents(wl, store):
    assert wl.POLICY_VERSION > "2026-09-15.1"
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("old answer"))])
    parent = done_parent(controller)
    _backdate(store, parent.id, policy_version="2026-09-15.1")
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(parent.id, prompt="and then?")
    old = store.get_run(parent.id, owner="duc")
    assert old.status == "done" and store.read_artifact(parent.id, owner="duc")["output"] == "old answer"
    assert [r.id for r in store.list_runs(owner="duc")] == [parent.id] and len(runner.calls) == 1


def test_continue_creates_a_linked_queued_child_with_the_parents_choice_and_session_and_no_process(wl, store):
    controller, runner = make_controller(wl, store, [ok(wl, codex_stream("answer one"))])
    parent = done_parent(controller, choice_key="codex-default", topic="alpha")
    assert controller.can_continue(parent) is True
    child = controller.continue_run(parent.id, prompt="now refine it")
    assert child.status == "queued" and child.continued_from_run_id == parent.id
    assert child.requested_session_id == parent.provider_session_id == CODEX_THREAD
    assert (child.choice_key, child.provider, child.project, child.topic) == ("codex-default", "codex", "kyra", "alpha")
    assert child.provider_session_id is None and child.output_sha256 is None
    assert store.read_artifact(child.id, owner="duc") == {"prompt": "now refine it", "output": None}
    assert len(runner.calls) == 1                                               # only the parent ever ran
    assert controller.can_continue(store.get_run(parent.id, owner="duc")) is False   # reserved
    assert [(r.id, r.continued_from_run_id) for r in store.list_runs(owner="duc")] == [(child.id, parent.id), (parent.id, None)]


@pytest.mark.parametrize("choice_key,stream,marker", [
    pytest.param("claude-fable-high", claude_stream, CLAUDE_SESSION, id="claude"),
    pytest.param("codex-default", codex_stream, CODEX_THREAD, id="codex"),
])
def test_continued_command_resumes_the_recorded_session_with_every_isolation_flag_kept(wl, store, choice_key, stream, marker):
    choice = wl.ALLOWLIST[choice_key]
    fresh = wl.command_for(choice)
    resumed = wl.command_for(choice, resume_session_id=marker)
    assert all(arg in resumed for arg in fresh)                                 # nothing relaxed
    assert marker in resumed and marker not in fresh
    if choice.provider == "claude_code":
        assert resumed[resumed.index("--resume") + 1] == marker
    else:
        assert resumed.index("exec") < resumed.index("resume") < resumed.index(marker)
    controller, runner = make_controller(wl, store, [ok(wl, stream("one")), ok(wl, stream("two"))])
    parent = done_parent(controller, choice_key=choice_key)
    child = controller.continue_run(parent.id, prompt="follow-up")
    controller.dispatch(child.id)
    assert runner.calls[1]["command"] == resumed
    assert runner.calls[1]["stdin"] == "follow-up"                              # prompt still travels on stdin


# ----------------------------------------------------------------------------------------------------
# Verifying the continuation
# ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("choice_key,stream,marker,other", [
    pytest.param("claude-fable-high", claude_stream, CLAUDE_SESSION, OTHER_CLAUDE_SESSION, id="claude"),
    pytest.param("codex-default", codex_stream, CODEX_THREAD, OTHER_CODEX_THREAD, id="codex"),
])
def test_child_must_come_back_on_the_requested_session_or_it_is_a_mismatch(wl, store, choice_key, stream, marker, other):
    same = stream("continued")
    elsewhere = stream("continued elsewhere").replace(marker, other)             # a whole different native session
    controller, _ = make_controller(wl, store, [ok(wl, stream("one")), ok(wl, same), ok(wl, stream("three")), ok(wl, elsewhere)])
    parent = done_parent(controller, choice_key=choice_key, topic="good")
    good = controller.dispatch(controller.continue_run(parent.id, prompt="go on").id)
    assert good.status == "done" and good.provider_session_id == parent.provider_session_id == marker
    assert store.read_artifact(good.id, owner="duc")["output"] == "continued"
    parent2 = done_parent(controller, choice_key=choice_key, topic="bad")
    wrong = controller.dispatch(controller.continue_run(parent2.id, prompt="go on").id)
    assert wrong.status == "mismatch" and wrong.error == "session_mismatch"
    assert wrong.requested_session_id == marker and wrong.provider_session_id == other
    assert store.read_artifact(wrong.id, owner="duc")["output"] == "continued elsewhere"   # kept for inspection
    assert store.find_session(owner="duc", project="kyra", topic="bad", provider=wl.ALLOWLIST[choice_key].provider) == marker


def test_parent_changed_before_child_dispatch_fails_without_a_process_and_keeps_the_reservation(wl, store, tmp_path):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("one")), ok(wl, claude_stream("never sent"))])
    parent = done_parent(controller)
    child = controller.continue_run(parent.id, prompt="go on")
    (tmp_path / "artifacts" / str(parent.id) / "output.md").write_text("one, edited afterwards")
    result = controller.dispatch(child.id)
    assert result.status == "failed" and result.error == "parent_changed"
    assert len(runner.calls) == 1
    with pytest.raises(wl.PolicyRefused):                                        # a failed child still holds the slot
        controller.continue_run(parent.id, prompt="try again")


# ----------------------------------------------------------------------------------------------------
# Refusals and the reservation
# ----------------------------------------------------------------------------------------------------

def test_parent_refusal_matrix(wl, store):
    controller, runner = make_controller(wl, store, [
        FileNotFoundError("gone"), wl.DispatchInterrupted("lost"),
        ok(wl, codex_stream("subject")), ok(wl, claude_stream("review text")), ok(wl, claude_stream("first")),
    ])
    queued = controller.request(project="kyra", topic="q", choice_key="claude-fable-high", prompt="p")
    failed = controller.dispatch(controller.request(project="kyra", topic="f", choice_key="claude-fable-high", prompt="p").id)
    lost = controller.dispatch(controller.request(project="kyra", topic="l", choice_key="claude-fable-high", prompt="p").id)
    subject = done_parent(controller, choice_key="codex-default", topic="r")
    review = controller.dispatch(controller.request_review(subject.id).id)
    first = done_parent(controller, topic="shared")
    assert (failed.status, lost.status, review.status, review.review_subject_id) == ("failed", "unreconciled", "done", subject.id)
    for run, why in [(queued, "queued"), (failed, "failed"), (lost, "unreconciled"), (review, "review run")]:
        with pytest.raises(wl.PolicyRefused):
            controller.continue_run(run.id, prompt="x")
        assert controller.can_continue(store.get_run(run.id, owner="duc")) is False, why
    other, _ = make_controller(wl, store, [], owner="someone-else")
    with pytest.raises(wl.PolicyRefused):                                        # not this owner's
        other.continue_run(first.id, prompt="x")
    with pytest.raises(wl.PolicyRefused):                                        # no such run
        controller.continue_run(first.id + 999, prompt="x")
    _backdate(store, first.id, provider_session_id=None)                         # a receipt without identity
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(first.id, prompt="x")
    assert controller.can_continue(store.get_run(first.id, owner="duc")) is False
    assert all(r.continued_from_run_id is None for r in store.list_runs(owner="duc"))
    assert len(runner.calls) == 5


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "", "5c51cabb; rm -rf ~", "00000000-0000-4000-8000-00000000c1a0 --dangerously-skip-permissions"],
                         ids=["plain-text", "empty", "shell-metacharacters", "uuid-plus-flag"])
def test_a_native_id_that_is_not_a_uuid_can_never_reach_argv(wl, store, bad_id):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("one"))])
    parent = done_parent(controller)
    _backdate(store, parent.id, provider_session_id=bad_id)                      # a corrupted or tampered receipt
    assert controller.can_continue(store.get_run(parent.id, owner="duc")) is False
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(parent.id, prompt="x")
    assert len(runner.calls) == 1 and all(r.continued_from_run_id is None for r in store.list_runs(owner="duc"))


@pytest.mark.parametrize("change", [
    pytest.param({"provider_session_id": OTHER_CLAUDE_SESSION}, id="parent-session-id-changed"),
    pytest.param({"effort": "low"}, id="parent-effort-changed"),
    pytest.param({"requested_model": "claude-sonnet-5"}, id="parent-model-changed"),
    pytest.param({"status": "mismatch"}, id="parent-no-longer-done"),
])
def test_parent_metadata_changed_after_the_child_was_created_fails_the_child_without_a_process(wl, store, change):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("one")), ok(wl, claude_stream("never sent"))])
    parent = done_parent(controller)
    child = controller.continue_run(parent.id, prompt="go on")
    _backdate(store, parent.id, **change)
    result = controller.dispatch(child.id)
    assert result.status == "failed" and result.error == "parent_changed"
    assert len(runner.calls) == 1
    assert store.get_run(child.id, owner="duc").requested_session_id == CLAUDE_SESSION   # the receipt keeps what was asked


def test_only_the_newest_completed_answer_in_a_scope_can_be_continued(wl, store):
    controller, _ = make_controller(wl, store, [ok(wl, claude_stream("older")), ok(wl, claude_stream("newer")), ok(wl, claude_stream("chain"))])
    older = done_parent(controller, topic="shared")
    newer = done_parent(controller, topic="shared")
    assert controller.can_continue(older) is False and controller.can_continue(newer) is True
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(older.id, prompt="x")
    child = controller.dispatch(controller.continue_run(newer.id, prompt="x").id)
    assert child.status == "done"
    assert controller.can_continue(store.get_run(newer.id, owner="duc")) is False          # reserved by its child
    assert controller.can_continue(child) is True                                           # the chain moves forward
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(newer.id, prompt="x")


def test_one_continuation_per_parent_is_reserved_atomically_under_concurrent_requests(wl, store):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("one"))])
    parent = done_parent(controller)
    gate = threading.Barrier(4)
    outcomes = []

    def contend(n):
        gate.wait()
        try:
            outcomes.append(controller.continue_run(parent.id, prompt=f"duplicate {n}").id)
        except wl.PolicyRefused:
            outcomes.append("refused")

    threads = [threading.Thread(target=contend, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    children = [o for o in outcomes if o != "refused"]
    assert len(children) == 1 and outcomes.count("refused") == 3
    assert [r.id for r in store.list_runs(owner="duc") if r.continued_from_run_id == parent.id] == children
    assert len(runner.calls) == 1                                                # no provider call during requests


def test_reviews_are_always_fresh_even_for_a_continued_answer(wl, store):
    controller, runner = make_controller(wl, store, [ok(wl, claude_stream("one")), ok(wl, claude_stream("two")), ok(wl, codex_stream("looks fine"))])
    parent = done_parent(controller)
    child = controller.dispatch(controller.continue_run(parent.id, prompt="go on").id)
    review = controller.request_review(child.id)
    assert review.review_subject_id == child.id
    assert review.continued_from_run_id is None and review.requested_session_id is None
    controller.dispatch(review.id)
    assert "resume" not in runner.calls[2]["command"] and "--resume" not in runner.calls[2]["command"]
    with pytest.raises(wl.PolicyRefused):
        controller.continue_run(review.id, prompt="continue the review")


# ----------------------------------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def http(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([ok(wl, claude_stream("first")), ok(wl, claude_stream("second")), ok(wl, codex_stream("reviewed"))])
    controller = wl.LoopController(store, runner, owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as client:
        yield client, runner
    get_settings.cache_clear()


def _drain():
    while run_one(webapp._queue, webapp.HANDLERS):
        pass


def test_http_continue_by_parent_id_only_once_and_never_with_a_session_id_from_the_browser(http):
    client, runner = http
    parent = client.post("/api/loop/runs", json={"choice": "claude-fable-high", "prompt": "first", "topic": "t"}).json()["run"]
    assert client.get(f"/api/loop/runs/{parent['id']}").json()["can_continue"] is False       # not done yet
    _drain()
    assert client.get(f"/api/loop/runs/{parent['id']}").json()["can_continue"] is True
    for bad in ({"prompt": "x", "session_id": CLAUDE_SESSION}, {"prompt": "x", "requested_session_id": CLAUDE_SESSION},
                {"prompt": "x", "choice": "codex-default"}, {"prompt": ""}):
        assert client.post(f"/api/loop/runs/{parent['id']}/continue", json=bad).status_code == 422, bad
    missing = client.post("/api/loop/runs/424242/continue", json={"prompt": "x"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    created = client.post(f"/api/loop/runs/{parent['id']}/continue", json={"prompt": "second question"})
    assert created.status_code == 200, created.text
    child = created.json()["run"]
    assert child["status"] == "queued" and child["continued_from_run_id"] == parent["id"]
    assert child["requested_session_id"] == CLAUDE_SESSION and len(runner.calls) == 1
    again = client.post(f"/api/loop/runs/{parent['id']}/continue", json={"prompt": "third"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "continuation_refused"
    assert client.get(f"/api/loop/runs/{parent['id']}").json()["can_continue"] is False
    _drain()
    detail = client.get(f"/api/loop/runs/{child['id']}").json()
    assert detail["run"]["status"] == "done" and detail["run"]["provider_session_id"] == CLAUDE_SESSION
    assert detail["artifact"]["output"] == "second" and detail["can_continue"] is True
    review = client.post(f"/api/loop/runs/{child['id']}/review").json()["run"]
    assert review["continued_from_run_id"] is None and review["requested_session_id"] is None
    _drain()
    assert client.get(f"/api/loop/runs/{review['id']}").json()["can_continue"] is False       # reviews never continue
    assert len(runner.calls) == 3


def test_http_continue_sits_behind_the_loop_boundary(http):
    client, runner = http
    res = client.post("/api/loop/runs/1/continue", json={"prompt": "x"}, headers={"Origin": "http://evil.example"})
    assert res.status_code == 403 and res.json()["error"]["code"] == "bad_origin"
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("192.168.1.42", 51000)) as lan:
        res = lan.post("/api/loop/runs/1/continue", json={"prompt": "x"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "loopback_only"
    assert client.post("/api/loop/runs/424242/continue", json={"prompt": "x"},
                       headers={"Origin": "http://127.0.0.1:8420"}).json()["error"]["code"] == "not_found"
    assert runner.calls == [] and json.loads(client.get("/api/loop/runs").text)["runs"] == []
