"""Slice A3b-2 (Codex contract 2.13 to 2.16): the release label has to survive the three places where ambient
context does not. A ContextVar does not cross into a raw `threading.Thread`, does not cross into the job worker
(another process, or a long-lived thread that serves many jobs), and means nothing to the Claude Code subprocess,
which is a cloud call no SDK wrapper can see.

Rules: a job carries a server-owned label from the moment it is enqueued, the worker runs each job inside exactly
that scope and never inherits anyone else's; a payload cannot forge its own label; a missing label is unknown as a
ROOT scope, so a drafting scope inside it cannot become a clean job_search root; in enforce the Claude CLI is never
spawned; a helper runs thread targets inside a copy of the caller's context.
"""
import threading

import pytest

from companion.jobs import DbJobQueue, run_one
from companion.privacy import PrivacyClass, Tier
from companion.provider import current_release_label, release_label, run_in_scope
from tests.test_working_loop import SENTINEL, ScriptedRunner, claude_stream, codex_stream, ok

C = PrivacyClass
UNKNOWN = (Tier.T2, frozenset({C.unknown}))
HEALTH = (Tier.T2, frozenset({C.health}))
JOB = (Tier.T2, frozenset({C.job_search}))


@pytest.fixture()
def queue(tmp_path):
    from companion.db import engine_for_store

    return DbJobQueue(engine_for_store(tmp_path / "jobs.db", tmp_path / "jobs.db"))


def _observe(seen):
    def handler(payload, on_progress):
        seen.append((dict(payload), current_release_label()))
        with release_label(*JOB):                       # what a drafting handler does before its first send
            seen.append(("nested", current_release_label()))
        return {"ok": True}
    return handler


# --- jobs ---------------------------------------------------------------------------------------------------


def test_a_job_runs_inside_the_label_it_was_enqueued_with_and_the_handler_never_sees_the_label_field(queue):
    seen = []
    with release_label(*HEALTH):                        # the turn that queued it
        queue.enqueue("draft", {"role": "ML engineer"}, label=current_release_label())
    assert current_release_label() == UNKNOWN
    assert run_one(queue, {"draft": _observe(seen)}) is True
    payload, label = seen[0]
    assert payload == {"role": "ML engineer"} and label == HEALTH
    assert seen[1] == ("nested", (Tier.T2, frozenset({C.health, C.job_search})))   # the outer class survives drafting
    assert current_release_label() == UNKNOWN          # and nothing leaks out of the worker


def test_a_job_without_a_label_is_unknown_as_a_root_scope_not_an_empty_one(queue):
    seen = []
    queue.enqueue("draft", {"x": 1})
    run_one(queue, {"draft": _observe(seen)})
    assert seen[0][1] == UNKNOWN
    assert seen[1] == ("nested", (Tier.T2, frozenset({C.unknown, C.job_search})))   # NOT a clean job_search root


def test_sequential_jobs_do_not_share_a_scope_and_a_failing_job_resets_it(queue):
    seen = []

    def boom(payload, on_progress):
        seen.append(current_release_label())
        raise RuntimeError("handler failed")

    queue.enqueue("boom", {}, label=HEALTH)
    queue.enqueue("draft", {}, label=JOB)
    run_one(queue, {"boom": boom, "draft": _observe(seen)})
    run_one(queue, {"boom": boom, "draft": _observe(seen)})
    assert seen[0] == HEALTH and seen[1][1] == JOB and current_release_label() == UNKNOWN


@pytest.mark.parametrize("payload", [{"__privacy__": {"tier": 0, "classes": []}}, {"x": {"__privacy__": "T0"}}])
def test_a_payload_cannot_carry_its_own_label(queue, payload):
    with pytest.raises(ValueError, match="__privacy__"):
        queue.enqueue("draft", payload)


def test_a_stored_label_that_was_tampered_with_fails_closed(queue):
    from sqlalchemy import text

    job_id = queue.enqueue("draft", {"x": 1}, label=JOB)
    with queue._engine.begin() as conn:
        conn.execute(text("update jobs set payload = replace(payload, 'job_search', 'gossip') where id = :id"), {"id": job_id})
    seen = []
    run_one(queue, {"draft": _observe(seen)})
    assert seen == [] and queue.get(job_id).status == "failed"            # never run under a label nobody can read


# --- raw threads ----------------------------------------------------------------------------------------------


def test_a_raw_thread_loses_the_scope_and_run_in_scope_keeps_it():
    seen = {}

    def target(key):
        seen[key] = current_release_label()

    with release_label(*HEALTH):
        bare = threading.Thread(target=target, args=("bare",))
        kept = threading.Thread(target=run_in_scope(target), args=("kept",))
        bare.start(), kept.start(), bare.join(), kept.join()
    assert seen["bare"] == UNKNOWN and seen["kept"] == HEALTH


def test_each_wrapped_thread_gets_its_own_copy_so_concurrent_turns_do_not_share_a_scope():
    seen, barrier = {}, threading.Barrier(2)

    def target(key, label):
        with release_label(*label):
            barrier.wait(timeout=5)
            seen[key] = current_release_label()

    threads = [threading.Thread(target=run_in_scope(target), args=(name, label)) for name, label in (("a", HEALTH), ("b", JOB))]
    [t.start() for t in threads], [t.join() for t in threads]
    assert seen == {"a": HEALTH, "b": JOB}


def test_the_web_app_s_own_threads_are_started_through_it():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "src" / "companion" / "webapp.py").read_text(encoding="utf-8")
    bare = [node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "Thread"
            and not any(kw.arg == "target" and isinstance(kw.value, ast.Call) and getattr(kw.value.func, "id", "") == "run_in_scope"
                        for kw in node.keywords)]
    assert bare == [], f"threads started without run_in_scope at webapp.py lines {bare}"


# --- the Claude Code subprocess ---------------------------------------------------------------------------------


@pytest.fixture()
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture()
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def test_in_enforce_the_claude_cli_is_never_spawned_and_codex_is_untouched(wl, store, monkeypatch):
    monkeypatch.setenv("KYRA_OUTBOUND_GATE", "enforce")
    from companion.settings import get_settings
    get_settings.cache_clear()
    try:
        runner = ScriptedRunner([ok(wl, codex_stream("built"))])
        controller = wl.LoopController(store, runner, owner="duc")
        claude_run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt=SENTINEL)
        done = controller.dispatch(claude_run.id)
        assert runner.calls == [] and done.status == "failed" and done.error == "release_boundary_unsupported"
        codex_run = controller.request(project="kyra", topic="t2", choice_key="codex-default", prompt=SENTINEL)
        assert controller.dispatch(codex_run.id).status == "done" and len(runner.calls) == 1
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["off", "dry_run"])
def test_outside_enforce_the_claude_cli_behaves_as_before(wl, store, monkeypatch, mode):
    monkeypatch.setenv("KYRA_OUTBOUND_GATE", mode)
    from companion.settings import get_settings
    get_settings.cache_clear()
    try:
        runner = ScriptedRunner([ok(wl, claude_stream("fine"))])
        controller = wl.LoopController(store, runner, owner="duc")
        run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt=SENTINEL)
        assert controller.dispatch(run.id).status == "done" and len(runner.calls) == 1
    finally:
        get_settings.cache_clear()
