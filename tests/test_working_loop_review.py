"""Regressions from Claude's independent review of the working-loop build (2026-09-15).

Each case pins a behaviour the acceptance suite (tests/test_working_loop.py) did not cover but the build relies on:
the Host boundary against DNS rebinding, tool-disabling proof read from the CLI's own init event, multi-event
Claude streams, Codex failure events, the review endpoint end to end, and the real SubprocessRunner's process
cleanup. The runner tests use real child processes (python, never a CLI) so they are hermetic.
"""
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from companion import webapp
from companion.settings import get_settings
from tests.test_working_loop import CLAUDE_SESSION, ScriptedRunner, claude_stream, codex_stream, ok


@pytest.fixture
def wl():
    import companion.working_loop as working_loop

    return working_loop


@pytest.fixture
def store(wl, tmp_path):
    return wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")


def _controller(wl, store, script):
    runner = ScriptedRunner(script)
    return wl.LoopController(store, runner, owner="duc"), runner


# ----------------------------------------------------------------------------------------------------
# Parser: proof of tool disabling and stream shapes the acceptance fixtures did not exercise
# ----------------------------------------------------------------------------------------------------

def test_claude_init_advertising_tools_or_mcp_servers_fails_even_with_a_clean_answer(wl, store):
    # The init event is the CLI's own statement of what the model could call. If the flags ever stop
    # disabling tools, this is where it shows, before any tool is used.
    events = [json.loads(line) for line in claude_stream("fine").splitlines()]
    events[0]["tools"] = ["Bash", "Edit"]
    stdout = "".join(json.dumps(e) + "\n" for e in events)
    controller, _ = _controller(wl, store, [ok(wl, stdout)])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    failed = controller.dispatch(run.id)
    assert failed.status == "failed" and failed.error.startswith("tool_use_forbidden")
    assert store.read_artifact(run.id, owner="duc")["output"] is None

    events[0]["tools"], events[0]["mcp_servers"] = [], [{"name": "filesystem", "status": "connected"}]
    controller, _ = _controller(wl, store, [ok(wl, "".join(json.dumps(e) + "\n" for e in events))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    assert controller.dispatch(run.id).error.startswith("tool_use_forbidden")


def test_claude_thinking_event_before_the_text_event_still_parses_as_done(wl, store):
    # With effort high the CLI emits the thinking block and the text block as separate assistant events
    # sharing one message id; the answer is the text, and the thinking block is neither output nor a tool.
    events = [json.loads(line) for line in claude_stream("The answer.").splitlines()]
    text_event = next(e for e in events if e["type"] == "assistant")
    thinking_event = json.loads(json.dumps(text_event))
    thinking_event["message"]["content"] = [{"type": "thinking", "thinking": "private", "signature": "sig"}]
    events.insert(events.index(text_event), thinking_event)
    controller, _ = _controller(wl, store, [ok(wl, "".join(json.dumps(e) + "\n" for e in events))])
    run = controller.request(project="kyra", topic="t", choice_key="claude-fable-high", prompt="p")
    done = controller.dispatch(run.id)
    assert done.status == "done" and done.served_model == "claude-fable-5-1"
    assert store.read_artifact(run.id, owner="duc")["output"] == "The answer."
    assert done.provider_session_id == CLAUDE_SESSION


@pytest.mark.parametrize("extra_event", [
    pytest.param({"type": "turn.failed", "error": {"message": "quota"}}, id="turn-failed"),
    pytest.param({"type": "error", "message": "stream error"}, id="top-level-error"),
    pytest.param({"type": "item.completed", "item": {"id": "item_e", "type": "error", "message": "Rate limited"}},
                 id="error-item-that-is-not-the-code-mode-notice"),
])
def test_codex_failure_events_are_failed_not_done(wl, store, extra_event):
    events = [json.loads(line) for line in codex_stream("looks fine").splitlines()]
    events.insert(3, extra_event)
    controller, _ = _controller(wl, store, [ok(wl, "".join(json.dumps(e) + "\n" for e in events))])
    run = controller.request(project="kyra", topic="t", choice_key="codex-default", prompt="p")
    failed = controller.dispatch(run.id)
    assert failed.status == "failed" and failed.error.startswith("provider_reported_error")
    assert store.read_artifact(run.id, owner="duc")["output"] is None


# ----------------------------------------------------------------------------------------------------
# HTTP: Host boundary and the review endpoint end to end
# ----------------------------------------------------------------------------------------------------

@pytest.fixture
def http(wl, monkeypatch, tmp_path):
    monkeypatch.setenv("KYRA_API_TOKEN", "")
    monkeypatch.setenv("KYRA_TRUST_LOOPBACK", "true")
    get_settings.cache_clear()
    store = wl.DbLoopStore(tmp_path / "loop.db", artifacts_dir=tmp_path / "artifacts")
    runner = ScriptedRunner([ok(wl, codex_stream("the draft")), ok(wl, claude_stream("one defect: none"))])
    controller = wl.LoopController(store, runner, owner=wl.PERSONAL_OWNER)
    monkeypatch.setattr(webapp, "_loop_controller", lambda: controller)
    yield store, runner
    get_settings.cache_clear()


def _drain():
    from companion.jobs import run_one

    while run_one(webapp._queue, webapp.HANDLERS):
        pass


def test_a_rebound_host_header_is_refused_on_loopback(http):
    # DNS rebinding: the attacker's page resolves its own name to 127.0.0.1, so the peer is loopback and
    # there may be no Origin header on a plain GET. The Host header is the remaining tell.
    with TestClient(webapp.app, base_url="http://evil.example", client=("127.0.0.1", 4321)) as c:
        for path in ("/loop", "/api/loop/runs", "/api/loop/runs/1"):
            res = c.get(path)
            assert res.status_code == 403 and res.json()["error"]["code"] == "bad_host", path
        res = c.post("/api/loop/runs", json={"choice": "codex-default", "prompt": "p", "topic": "t"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "bad_host"
    with TestClient(webapp.app, base_url="http://localhost:8420", client=("127.0.0.1", 4321)) as c:
        assert c.get("/api/loop/runs").status_code == 200
        assert c.get("/loop").status_code == 200


def test_review_endpoint_runs_the_other_provider_on_the_exact_artifact_and_attaches_a_comment(http):
    store, runner = http
    with TestClient(webapp.app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 4321)) as c:
        subject = c.post("/api/loop/runs", json={"choice": "codex-default", "prompt": "write it", "topic": "t"}).json()["run"]
        early = c.post(f"/api/loop/runs/{subject['id']}/review")
        assert early.status_code == 409 and early.json()["error"]["code"] == "review_refused"   # not done yet
        _drain()
        review = c.post(f"/api/loop/runs/{subject['id']}/review")
        assert review.status_code == 200, review.text
        review_run = review.json()["run"]
        assert review_run["status"] == "queued" and review_run["provider"] == "claude_code"
        assert review_run["review_subject_id"] == subject["id"]
        assert len(runner.calls) == 1                                        # nothing ran inside the request
        _drain()
        assert "the draft" in runner.calls[1]["stdin"] and "write it" in runner.calls[1]["stdin"]
        detail = c.get(f"/api/loop/runs/{subject['id']}").json()
        assert detail["run"]["status"] == "done"
        assert [(r["reviewer_run_id"], r["verdict"], r["stale"]) for r in detail["reviews"]] == [
            (review_run["id"], "comment", False)]
        reviewer = c.get(f"/api/loop/runs/{review_run['id']}").json()
        assert reviewer["run"]["status"] == "done" and reviewer["artifact"]["output"] == "one defect: none"
        # A second review request is a new bound run, never a re-dispatch of the finished one.
        again = c.post(f"/api/loop/runs/{subject['id']}/review").json()["run"]
        assert again["id"] != review_run["id"] and again["status"] == "queued"
        assert c.get(f"/api/loop/runs/{review_run['id']}").json()["run"]["status"] == "done"


# ----------------------------------------------------------------------------------------------------
# The real runner, against real child processes: cleanup, limits, isolation. No CLI involved.
# ----------------------------------------------------------------------------------------------------

def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _gone(pid: int, seconds: float = 5.0) -> bool:
    """True once the pid is dead (or a zombie awaiting launchd), polling briefly after SIGKILL."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def test_runner_delivers_stdin_and_isolates_env_and_cwd(wl, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-reach-the-cli")
    monkeypatch.setenv("KYRA_DATA_DIR", os.environ["KYRA_DATA_DIR"])
    code = ("import json, os, sys; print(json.dumps({'stdin': sys.stdin.read(), 'env': sorted(os.environ), "
            "'cwd_entries': sorted(os.listdir('.')), 'cwd_is_tmp': os.getcwd().startswith(os.path.realpath(os.environ['TMPDIR']))}))")
    result = wl.SubprocessRunner().run(_python(code), stdin="hello prompt", timeout_seconds=30)
    assert result.returncode == 0
    seen = json.loads(result.stdout)
    assert seen["stdin"] == "hello prompt"
    assert not [k for k in seen["env"] if k.startswith(("ANTHROPIC", "KYRA", "OPENAI", "CLAUDE", "CODEX"))]
    assert set(seen["env"]) <= {"HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME", "__CF_USER_TEXT_ENCODING"}
    assert seen["cwd_entries"] == ["prompt"] and seen["cwd_is_tmp"]         # an empty scratch dir, no project files


def test_runner_timeout_kills_the_whole_process_group_including_a_grandchild(wl, tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    with pytest.raises(wl.DispatchInterrupted):
        wl.SubprocessRunner().run(_python(code), stdin="", timeout_seconds=1)
    assert time.monotonic() - started < 10
    grandchild = int(pid_file.read_text())
    assert _gone(grandchild), f"grandchild {grandchild} survived the timeout"


def test_runner_output_limit_stops_a_runaway_process(wl):
    code = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n    sys.stdout.flush()\n"
    started = time.monotonic()
    with pytest.raises(wl.DispatchInterrupted):
        wl.SubprocessRunner().run(_python(code), stdin="", timeout_seconds=60)
    assert time.monotonic() - started < 30


def test_runner_reports_a_missing_executable_as_never_started(wl, tmp_path):
    with pytest.raises(FileNotFoundError):
        wl.SubprocessRunner().run([str(tmp_path / "no-such-cli")], stdin="p", timeout_seconds=5)


def test_runner_nonzero_exit_and_stderr_come_back_without_raising(wl):
    result = wl.SubprocessRunner().run(_python("import sys; sys.stderr.write('boom'); sys.exit(3)"), stdin="", timeout_seconds=30)
    assert result.returncode == 3 and result.stderr == "boom" and result.stdout == ""
