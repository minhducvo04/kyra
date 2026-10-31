"""W0: scratch run records, fake channels, real contract and transition store."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.feature_team import contract, node_worker, runs


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.on_sleep = lambda: None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


@pytest.fixture
def case(tmp_path):
    root, repo = tmp_path / "data", tmp_path / "repo"
    repo.mkdir()
    clock = Clock()
    events = []
    store = runs.Runs(root / "team/loop", emit=events.append, clock=clock)
    node = {"id": "W0", "title": "Write a fixture", "inputs": [], "files": ["NEW fixture.py"],
            "acceptance": "fixture check passes", "intern_friendly": True, "tier": "T1"}
    dag = root / "team/feature-teams/demo.dag.json"
    dag.parent.mkdir(parents=True)
    dag.write_text(json.dumps({"nodes": [node]}))
    brief = contract.brief_from_node(
        node, run_id="demo-W0-first", feature="demo", channel="cursor:worker", model="fixture-model",
        effort="medium", base="origin/master", worktree=str(repo / ".claude/worktrees/initial"),
        inputs={}, deadline=clock() + 1200, result=str(root / "team/results/result.json"), rules="AGENTS.md")
    store.create(brief)
    made = []
    kwargs = dict(root=root, repo=repo, store=store, clock=clock, sleep=clock.sleep,
                  make_worktree=lambda command: made.append(command))
    return store, brief, clock, events, made, kwargs


def result(brief, **overrides):
    return dict(run_id=brief["run_id"], feature=brief["feature"], node=brief["node"], worker="fixture-worker",
                sha="abcdef0", branch="session/fixture", status="done", artifacts=[],
                proof=[{"command": "fixture-check", "exit": 0, "log": "fixture.log"}],
                tests=[], open_questions=[], tokens=None, started=1_800_000_000.0,
                finished=1_800_000_001.0, **overrides)


def write_result(brief, value=None):
    path = Path(brief["result"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result(brief) if value is None else value))


def test_valid_result_and_fresh_context_for_every_run(case):
    store, brief, _, events, made, kwargs = case
    calls = []

    def send(prepared, message, *, receipt_timeout):
        calls.append((prepared, message, receipt_timeout))
        write_result(prepared)
        return f"STARTED: cursor ledger_id=fixture-{len(calls)}"

    for i in range(3):
        current = dict(brief, run_id=f"demo-W0-{i}", result=str(Path(brief["result"]).with_name(f"{i}.json")))
        store.create(current)
        out = node_worker.run_node(current["run_id"], send=send, **kwargs)
        assert out["state"] == "result"
        assert store.load(current["run_id"])["sha"] == "abcdef0"
    assert len(calls) == len(made) == 3
    # The strict fake signature refuses resume, thread and session arguments on all three sends.
    assert all("resume" not in call[1].lower() for call in calls)
    assert sum(e["kind"] == "started" for e in events) == 3
    assert all(contract.validate_event(e) == e for e in events)
    assert all(json.loads(e["detail"])["search_calls"] == 0 for e in events if e["kind"] == "result")


def test_invalid_result_retried_once_with_error_previous_result_and_verify_output(case):
    store, brief, _, events, made, kwargs = case
    verify = kwargs["root"] / "verify.log"
    verify.write_text("fixture verification failed")
    rec = store.load(brief["run_id"])
    rec["verify"]["output"] = str(verify)
    runs._write_json(store.path(brief["run_id"]), rec)
    calls = []

    def send(prepared, message, *, receipt_timeout):
        calls.append(message)
        write_result(prepared, {"run_id": prepared["run_id"]})
        return f"STARTED: cursor ledger_id=fixture-{len(calls)}"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "escalated" and len(calls) == 2 and len(made) == 1
    assert "feature: missing" in calls[1] and "Previous result" in calls[1]
    assert "fixture verification failed" in calls[1]
    assert store.load(brief["run_id"])["attempts"]["T1"] == 2
    assert [e["kind"] for e in events][-5:] == ["started", "timeout", "dispatched", "started", "escalated"]


def test_retry_accepts_corrected_result_and_does_not_reconsume_stale_file(case):
    _, brief, clock, _, _, kwargs = case
    calls = []

    def send(prepared, message, *, receipt_timeout):
        calls.append(message)
        if len(calls) == 1:
            write_result(prepared, {})
        else:
            assert not Path(prepared["result"]).exists()
            clock.on_sleep = lambda: write_result(prepared)
        return f"STARTED: cursor ledger_id=fixture-{len(calls)}"

    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "result"
    assert len(calls) == 2


@pytest.mark.parametrize("receipt", [None, "UNCERTAIN: ledger_id=queued", "STARTED: cursor"])
def test_missing_start_receipt_times_out_without_accepting_result(case, receipt):
    store, brief, clock, events, _, kwargs = case
    start = clock()

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared)
        return receipt

    out = node_worker.run_node(brief["run_id"], send=send, receipt_timeout=45, **kwargs)
    assert out["state"] == "timeout" and clock() == start + 45
    assert "receipt" in events[-1]["detail"]
    assert not any(e["kind"] in {"started", "result"} for e in events)
    assert sum(e["kind"] == "launch_uncertain" for e in events) == int(bool(receipt and receipt.startswith("UNCERTAIN:")))
    assert store.load(brief["run_id"])["deadline"] == brief["deadline"]


def test_cursor_receipt_at_start_deadline_is_a_start(case):
    """Cursor's send blocks until its start deadline, so a valid ledger id arriving exactly at the
    deadline is a start, whatever the clock says (2026-10-19: every Cursor dispatch timed out while
    the worker kept running, producing duplicate builds)."""
    store, brief, clock, events, _, kwargs = case

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared)
        clock.now += receipt_timeout  # land exactly on start_deadline
        return "cursor started pid 74826 in /x (log /tmp/x.log)\nledger id: cursor-cursor-w2-abc"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "result"
    started = [e for e in events if e["kind"] == "started"]
    assert len(started) == 1 and "ledger_id=cursor-cursor-w2-abc" in started[0]["detail"]
    assert store.load(brief["run_id"])["state"] == "result"


def test_heartbeat_and_original_deadline_while_waiting(case):
    store, brief, clock, events, _, kwargs = case
    start = clock()
    out = node_worker.run_node(brief["run_id"], send=lambda *a, **kw: "STARTED: cursor ledger_id=fixture-1",
                               **kwargs)
    assert out["state"] == "timeout" and clock() == brief["deadline"]
    assert [e["t"] - start for e in events if e["kind"] == "heartbeat"] == [300, 600, 900]
    assert store.load(brief["run_id"])["deadline"] == brief["deadline"]


def test_foreign_result_cannot_complete_run(case):
    _, brief, _, _, _, kwargs = case

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared, dict(result(prepared), run_id="other-run"))
        return "STARTED: cursor ledger_id=fixture-1"

    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "escalated"


@pytest.mark.parametrize("channel", ["cursor:worker", "codex-exec:worker"])
def test_dry_run_cli_is_one_json_line_and_changes_nothing(case, channel):
    store, brief, _, _, _, kwargs = case
    rec = store.load(brief["run_id"])
    rec["brief"]["channel"] = channel
    runs._write_json(store.path(brief["run_id"]), rec)
    before = {p: p.read_bytes() for p in kwargs["root"].rglob("*") if p.is_file()}
    script = Path(__file__).resolve().parents[1] / "scripts/node_worker.py"
    out = subprocess.run([sys.executable, str(script), "run", "--run-id", brief["run_id"],
                          "--root", str(kwargs["root"]), "--dry-run"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stderr == "" and len(out.stdout.splitlines()) == 1
    report = json.loads(out.stdout)
    assert report["state"] == "dry_run" and report["channel_command"]
    if channel.startswith("codex-exec:"):
        assert " exec " in report["channel_command"] and "--resume" not in report["channel_command"]
        assert report["channel_blocker"] is None
    assert contract.validate_brief(report["brief"])["run_id"] == brief["run_id"]
    assert store.load(brief["run_id"])["state"] == "queued"
    assert before == {p: p.read_bytes() for p in kwargs["root"].rglob("*") if p.is_file()}


def test_latest_input_sha_becomes_worktree_base(case):
    store, brief, _, _, made, kwargs = case
    inputs = []
    for node, finished, sha in [("A0", 1_800_000_002, "aaaaaaa"), ("B0", 1_800_000_001, "bbbbbbb")]:
        path = kwargs["root"] / f"{node}.json"
        path.write_text(json.dumps(dict(result(brief), node=node, finished=finished, sha=sha)))
        inputs.append({"node": node, "result": str(path)})
    rec = store.load(brief["run_id"])
    rec["brief"]["inputs"] = inputs
    runs._write_json(store.path(brief["run_id"]), rec)
    dag_path = kwargs["root"] / "team/feature-teams/demo.dag.json"
    dag = json.loads(dag_path.read_text())
    dag["nodes"][0]["inputs"] = ["A0", "B0"]
    for item in inputs:
        dag["nodes"].append(dict(dag["nodes"][0], id=item["node"], inputs=[]))
    dag_path.write_text(json.dumps(dag))

    def send(prepared, message, *, receipt_timeout):
        assert prepared["base"] == "aaaaaaa"
        write_result(prepared)
        return "STARTED: cursor ledger_id=fixture-1"

    node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert made[0][-1] == "aaaaaaa"
    assert store.load(brief["run_id"])["brief"]["base"] == "aaaaaaa"


def test_already_dispatched_record_does_not_count_an_extra_attempt(case):
    store, brief, _, _, _, kwargs = case
    store.transition(brief["run_id"], "dispatched", deadline=brief["deadline"])

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared)
        return "STARTED: cursor ledger_id=fixture-1"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "result" and out["attempts"]["T1"] == 1


def test_cursor_adapter_calls_real_channel_with_fresh_argv_and_scratch_ledger(case, monkeypatch):
    _, brief, _, _, _, kwargs = case
    lane = node_worker._lane_send()
    calls = []
    Path(brief["worktree"]).mkdir(parents=True)

    def start(name, argv, cwd, wait, priority=None):
        calls.append(argv)
        assert name == "cursor" and cwd == Path(brief["worktree"]) and wait == 10
        return f"cursor started pid {len(calls)} in {cwd}"

    monkeypatch.setattr(lane, "_start_worker", start)
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    for i in range(3):
        assert node_worker.send_channel(brief, f"fixture-{i}", receipt_timeout=10,
                                        root=kwargs["root"]).startswith("STARTED:")
    assert len(calls) == 3
    for i, argv in enumerate(calls):
        assert argv == [lane.CURSOR, "-p", "--output-format", "text", f"fixture-{i}"]
        assert not any(flag in argv for flag in ("--resume", "--thread", "--session-id", "--continue"))
    rows = [json.loads(line) for line in (kwargs["root"] / "context/packages.jsonl").read_text().splitlines()]
    assert len({row["id"] for row in rows}) == 3


@pytest.mark.parametrize("channel,reason", [
    ("codex:1", "not fresh"), ("grok", "GROK_HOME"),
])
def test_base_channel_mismatches_fail_before_worktree_or_send(case, channel, reason):
    store, brief, _, events, made, kwargs = case
    rec = store.load(brief["run_id"])
    rec["brief"]["channel"] = channel
    runs._write_json(store.path(brief["run_id"]), rec)
    out = node_worker.run_node(brief["run_id"], **kwargs)
    assert out["state"] == "escalated" and reason in out["history"][-1]["detail"]
    assert made == [] and events[-1]["kind"] == "escalated"
    refused = [e for e in events if e["kind"] == "dispatch_refused"]
    assert len(refused) == 1 and reason in refused[0]["detail"]
    assert not (kwargs["root"] / "context/packages.jsonl").exists()


def test_claude_adapter_uses_known_cl_signature_and_preserves_ledger_id(case, monkeypatch):
    _, brief, _, _, _, kwargs = case
    real_lane = node_worker._lane_send()
    calls = []
    original_root = os.environ.get("KYRA_DATA_DIR")

    def claude(folder, message, effort="auto", model="opus", *, task_kind="default", wait=20):
        # CL owns its config/budget check and session creation; W0 supplies neither a session nor resume.
        assert os.environ["KYRA_DATA_DIR"] == str(kwargs["root"])
        assert folder == brief["worktree"] and effort == "medium" and model == "opus" and wait == 10
        calls.append(message)
        return f"STARTED: claude pid=42 ledger_id=claude-fresh-{len(calls)}"

    lane = SimpleNamespace(send_claude=claude, check_claude_worker=lambda model: None,
                           record_package=real_lane.record_package, brief_paths=real_lane.brief_paths)
    # record_package is the real function, with its own module globals.
    monkeypatch.setattr(real_lane, "ledger_path", lambda: kwargs["root"] / "context/packages.jsonl")
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    for i in range(2):
        receipt = node_worker.send_channel(dict(brief, channel="claude:worker", model="opus"), f"fixture-{i}",
                                          receipt_timeout=10, root=kwargs["root"])
        assert receipt == f"STARTED: claude pid=42 ledger_id=claude-fresh-{i + 1}"
    assert len(calls) == 2 and os.environ.get("KYRA_DATA_DIR") == original_root


def test_expired_run_cannot_send_or_extend_deadline(case):
    store, brief, clock, _, made, kwargs = case
    clock.now = brief["deadline"]

    def send(*args, **kwargs):
        pytest.fail("expired run sent work")

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "timeout" and made == []
    assert store.load(brief["run_id"])["deadline"] == brief["deadline"]


@pytest.mark.parametrize("channel,missing", [("claude:worker", False), ("claude:worker", True),
                                           ("codex-exec:worker", True)])
def test_gated_or_missing_channel_is_refused_with_reason(case, monkeypatch, channel, missing):
    store, brief, _, _, _, kwargs = case
    rec = store.load(brief["run_id"])
    rec["brief"]["channel"] = channel
    runs._write_json(store.path(brief["run_id"]), rec)
    lane = node_worker._lane_send()
    if missing:
        delattr(lane, "send_claude" if channel.startswith("claude:") else "send_codex_exec")
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    kwargs.pop("store")  # Exercise EventStore validation too.
    out = node_worker.run_node(brief["run_id"], **kwargs)
    assert out["state"] == "escalated"
    reason = out["history"][-1]["detail"]
    assert "SystemExit" not in reason
    assert ("no gated" if missing else "claude_cli_workers is off") in reason
    rows = [json.loads(line) for line in (kwargs["root"] / "team/loop/events.jsonl").read_text().splitlines()]
    assert [r["detail"] for r in rows if r["kind"] == "dispatch_refused"] == [reason]
    assert not any(r["kind"] == "started" for r in rows)


@pytest.mark.parametrize("uncertain", [False, True])
def test_codex_exec_send_records_fresh_receipt_and_result_searches(case, monkeypatch, uncertain):
    store, brief, _, _, _, kwargs = case
    rec = store.load(brief["run_id"])
    rec["brief"]["channel"] = "codex-exec:worker"
    runs._write_json(store.path(brief["run_id"]), rec)
    lane = node_worker._lane_send()
    calls = []
    log = kwargs["root"] / "worker log.txt"
    log.write_text("exec\n/bin/zsh -lc 'find src -name test.py' in /scratch succeeded\n"
                   "exec\n/bin/zsh -lc 'cat file.py' in /scratch succeeded\n"
                   "exec\n/bin/zsh -lc 'echo find src' in /scratch succeeded\n"
                   "assistant\nI mentioned find, Grep and Glob.\n")

    def send(folder, message, effort, model, *, out, wait):
        prepared = store.load(brief["run_id"])["brief"]
        assert folder == prepared["worktree"]
        assert effort == brief["effort"] and model == brief["model"] and wait == 60
        assert Path(out) == Path(folder) / ".lane-send" / f"{brief['run_id']}.final.md"
        calls.append(message)
        write_result(prepared)
        return (f"{'UNCERTAIN' if uncertain else 'STARTED'}: codex-exec pid=42 "
                f"ledger_id=codex-exec-fixture session=session-fixture log={log} final={out}")

    monkeypatch.setattr(lane, "send_codex_exec", send)
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    kwargs.pop("store")
    out = node_worker.run_node(brief["run_id"], **kwargs)
    assert len(calls) == 1 and out["state"] == ("timeout" if uncertain else "result")
    rows = [json.loads(line) for line in (kwargs["root"] / "team/loop/events.jsonl").read_text().splitlines()]
    ledger = kwargs["root"] / "context/packages.jsonl"
    if uncertain:
        assert sum(r["kind"] == "launch_uncertain" for r in rows) == 1
        assert not ledger.exists()
    else:
        started = next(r for r in rows if r["kind"] == "started")
        assert "ledger_id=codex-exec-fixture" in started["detail"]
        assert "session=session-fixture" in started["detail"]
        assert json.loads(rows[-1]["detail"])["search_calls"] == 1
        assert json.loads(ledger.read_text())["id"] == "codex-exec-fixture"


def test_search_calls_count_tools_not_results_or_prose(case):
    _, brief, _, events, _, kwargs = case
    log = kwargs["root"] / "channel.jsonl"
    log.write_text("\n".join(json.dumps(row) for row in [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {}},
            {"type": "tool_use", "name": "Glob", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "find src -type f"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo find src"}},
            {"type": "text", "text": "Grep Glob find"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Grep find"}]}},
    ]) + "\n{unfinished")

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared)
        return f"STARTED: claude ledger_id=fixture log={log}"

    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "result"
    assert json.loads(events[-1]["detail"])["search_calls"] == 3


def test_stale_result_refused_without_mutating_file(case):
    _, brief, _, _, made, kwargs = case
    write_result(brief)
    before = Path(brief["result"]).read_bytes()

    def send(*args, **kwargs):
        pytest.fail("stale result dispatched")

    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "escalated"
    assert not made and Path(brief["result"]).read_bytes() == before


def test_worktree_failure_emits_escalation_without_sending(case):
    _, brief, _, events, _, kwargs = case

    def fail(command):
        raise subprocess.CalledProcessError(128, command)

    def send(*args, **kwargs):
        pytest.fail("failed worktree dispatched")

    kwargs["make_worktree"] = fail
    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "escalated"
    assert events[-1]["kind"] == "escalated"


def test_default_store_emits_validated_events_under_requested_root(case):
    _, brief, _, _, _, kwargs = case
    kwargs.pop("store")

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared)
        return "STARTED: cursor ledger_id=fixture-1"

    assert node_worker.run_node(brief["run_id"], send=send, **kwargs)["state"] == "result"
    path = kwargs["root"] / "team/loop/events.jsonl"
    rows = [contract.validate_event(json.loads(line)) for line in path.read_text().splitlines()]
    assert [row["kind"] for row in rows] == ["queued", "dispatched", "started", "result"]
    assert rows[-1]["path"] == brief["result"]


def test_reused_start_receipt_cannot_prove_fresh_retry(case):
    _, brief, _, events, _, kwargs = case

    def send(prepared, message, *, receipt_timeout):
        write_result(prepared, {})
        return "STARTED: cursor ledger_id=reused-receipt"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "escalated" and "reused" in out["history"][-1]["detail"]
    assert sum(row["kind"] == "started" for row in events) == 1


TRANSIENT = [
    "claude send FAILED (exit 1); log /tmp/claude-x.log",
    "codex-exec send FAILED (exit 137); log /tmp/codex-x.log: killed",
    "claude send NOT STARTED: Claude budget stop (claude): weekly limit, resets 05:00",
    "claude send NOT STARTED: Claude budget state unavailable or invalid",
    "muse send FAILED: both muse slots are occupied (limit 2); retry after a worker exits",
    "muse send FAILED: all muse slots are occupied (limit 6); retry after a worker exits",
    "grok send FAILED: cannot read effort policy /x/effort-policy.json: boom",
]
CONTRACT = [
    "claude send NOT STARTED: --model must be opus, sonnet, haiku or fable",
    "claude send NOT STARTED: claude_cli_workers is off; pending the owner's confirmation",
    "claude send NOT STARTED: fable is paused; fable_window must be true",
    "claude send NOT STARTED: Claude CLI not found",
    "claude send NOT STARTED: /x is not its own git worktree of the repo",
    "codex-exec send NOT STARTED: the prompt is empty or starts with a dash",
    "codex-exec send NOT STARTED: unknown Codex lane 'x'",
    "cursor send FAILED: no folder /x",
    "grok send FAILED: invalid policy effort 'ultra'",
    "something nobody has seen before",
]


@pytest.mark.parametrize("reason", TRANSIENT + CONTRACT)
def test_send_failure_policy_retries_transient_and_escalates_contract(case, monkeypatch, reason):
    """Send-failure policy (2026-10-15): lane_send's own refusal strings decide. A transient failure (the CLI
    process died, a timed budget stop, an unreadable policy or budget file, both muse slots busy) leaves the run in
    `timeout` with the deadline unchanged, so T0 re-dispatches it on a later tick with fresh context and bounded
    attempts; a contract refusal (model id, flag off, folder, prompt shape, unknown text) escalates at once. The
    class is the first word of the event detail, so the inbox row and loop_fl say which it was."""
    store, brief, clock, _, made, kwargs = case
    rec = store.load(brief["run_id"])
    rec["brief"]["channel"] = "claude:worker"
    runs._write_json(store.path(brief["run_id"]), rec)
    real_lane = node_worker._lane_send()

    def refuse(*args, **kwargs):
        raise SystemExit(reason)

    lane = SimpleNamespace(send_claude=refuse, check_claude_worker=lambda model: None,
                           record_package=real_lane.record_package, brief_paths=real_lane.brief_paths)
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    kwargs.pop("store")  # Exercise EventStore validation too.
    expected = "transient" if reason in TRANSIENT else "contract"
    assert node_worker.send_failure_class(reason) == expected
    out = node_worker.run_node(brief["run_id"], **kwargs)
    assert out["state"] == ("timeout" if expected == "transient" else "escalated")
    assert out["deadline"] == brief["deadline"] and out["attempts"]["T1"] == 1
    detail = out["history"][-1]["detail"]
    assert detail.startswith(f"{expected}: ") and "SystemExit" not in detail and reason.split(";")[0] in detail
    rows = [json.loads(line) for line in (kwargs["root"] / "team/loop/events.jsonl").read_text().splitlines()]
    assert [r["detail"] for r in rows if r["kind"] == "dispatch_refused"] == [detail]
    assert [r["kind"] for r in rows][-1] == out["state"] and not any(r["kind"] == "started" for r in rows)
    assert runs.TRANSITIONS["timeout"] >= {"dispatched", "escalated"}  # the state T0 retries from


def _node_dag(root, node):
    dag = root / "team/feature-teams/demo.dag.json"
    dag.write_text(json.dumps({"nodes": [{"id": "X", "title": "Input", "inputs": [], "files": [], "acceptance": "x",
                                          "intern_friendly": False, "tier": "T1"}, node]}))


def _input_result(root):
    path = root / "team/loop/results/X.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = result(dict(run_id="demo-X-0", feature="demo", node="X"))
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("channel", ["claude:worker", "codex-exec:worker", "grok", "muse:0"])
def test_worktree_bound_brief_keeps_every_path_inside_the_worktree_and_stages_inputs(case, channel, monkeypatch,
                                                                                    tmp_path):
    """PLAN output contract (2026-10-15): a Claude CLI worker is denied every path under data/ and outside its
    worktree, so its brief's inputs, result and rules live inside the worktree; the accepted result is copied to
    data/team/loop/results afterwards."""
    store, brief, clock, events, made, kwargs = case
    root = kwargs["root"]
    # a grok run's folder is under GROK_HOME: keep it in scratch, never the real <user-home>/kyra-grok
    monkeypatch.setattr(node_worker, "_lane_send", lambda: type("L", (), {"GROK_HOME": tmp_path / "kyra-grok"}))
    _node_dag(root, {"id": "W1", "title": "Build", "inputs": ["X"], "files": ["NEW fixture2.py"], "acceptance": "check",
                     "intern_friendly": False, "tier": "T1"})
    x_result = _input_result(root)
    current = dict(brief, run_id="demo-W1-first", node="W1", channel=channel,
                   inputs=[{"node": "X", "result": str(x_result)}], result=str(root / "team/results/w1.json"))
    store.create(current)
    seen = []
    coordinator_build = channel in {"grok", "muse:0"}
    coordinator_sha = "1234567890abcdef1234567890abcdef12345678"
    committed = []

    def commit_build(prepared, value, receipt, custody, log_path):
        # This unit test owns staging/publication; build_handoff cases exercise the real gate and custody.
        assert coordinator_build and value["sha"] is None
        worktree = Path(prepared["worktree"])
        assert value["branch"] == f"session/{worktree.name}"
        assert value["artifacts"] == ["fixture2.py"]
        assert (worktree / "fixture2.py").read_text() == "VALUE = 42\n"
        assert receipt.process.poll() == 0
        committed.append(prepared["run_id"])
        return coordinator_sha

    monkeypatch.setattr(node_worker, "commit_build", commit_build)

    def send(prepared, message, *, receipt_timeout):
        seen.append(prepared)
        Path(prepared["result"]).parent.mkdir(parents=True, exist_ok=True)
        value = result(prepared)
        if coordinator_build:
            worktree = Path(prepared["worktree"])
            (worktree / "fixture2.py").write_text("VALUE = 42\n")
            value.update(sha=None, branch=f"session/{worktree.name}", artifacts=["fixture2.py"])
        Path(prepared["result"]).write_text(json.dumps(value))
        receipt = f"STARTED: {channel} pid=1 ledger_id=fixture-1"
        if coordinator_build:
            return node_worker.BuildReceipt(receipt, SimpleNamespace(pid=1, poll=lambda: 0))
        return receipt

    out = node_worker.run_node("demo-W1-first", send=send, **kwargs)
    assert out["state"] == "result"
    sent = seen[0]
    worktree = Path(sent["worktree"])
    assert Path(sent["result"]) == worktree / ".lane-send" / "demo-W1-first.result.json"
    assert sent["inputs"] == [{"node": "X", "result": str(worktree / ".lane-send/inputs/X.result.json")}]
    assert json.loads((worktree / ".lane-send/inputs/X.result.json").read_text()) == json.loads(x_result.read_text())
    assert sent["rules"] == str(worktree / "AGENTS.md")
    assert "You cannot read or write" not in str(sent)  # a code node, no plan block in the brief dict
    published = Path(store.load("demo-W1-first")["result"])
    assert published == root / "team/loop/results/demo-W1-first.json" and published.is_file()
    assert committed == (["demo-W1-first"] if coordinator_build else [])
    expected_sha = coordinator_sha if coordinator_build else "abcdef0"
    assert out["sha"] == json.loads(published.read_text())["sha"] == expected_sha


def test_brief_path_the_channel_cannot_write_is_a_contract_refusal_before_any_send(case):
    store, brief, _, _, made, kwargs = case
    root = kwargs["root"]
    _node_dag(root, {"id": "W1", "title": "Build", "inputs": [], "files": ["data/notes/x-plan.md"],
                     "acceptance": "check", "intern_friendly": False, "tier": "T1"})
    store.create(dict(brief, run_id="demo-W1-bad", node="W1", channel="claude:worker", files=["data/notes/x-plan.md"]))
    kwargs.pop("store")

    def send(*a, **k):
        pytest.fail("a refused brief must not be sent")

    out = node_worker.run_node("demo-W1-bad", send=send, **kwargs)
    assert out["state"] == "escalated" and made == []
    detail = out["history"][-1]["detail"]
    assert detail == "contract: brief names a path the channel cannot write in files: data/notes/x-plan.md"
    rows = [json.loads(line) for line in (root / "team/loop/events.jsonl").read_text().splitlines()]
    assert [r["detail"] for r in rows if r["kind"] == "dispatch_refused"] == [detail]


def test_plan_artifacts_are_published_to_data_and_the_result_copied(case):
    store, brief, _, _, _, kwargs = case
    root = kwargs["root"]
    _node_dag(root, {"id": "PLAN", "title": "Write the plan", "inputs": [], "kind": "plan",
                     "files": ["NEW PLAN-demo.md", "NEW demo-proposed.dag.json"], "acceptance": "Write the plan.",
                     "intern_friendly": False, "tier": "T1"})
    store.create(dict(brief, run_id="demo-PLAN-first", node="PLAN", channel="claude:worker", kind="plan",
                      files=["NEW PLAN-demo.md", "NEW demo-proposed.dag.json"]))

    def send(prepared, message, *, receipt_timeout):
        worktree = Path(prepared["worktree"])
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "PLAN-demo.md").write_text("# plan\n")
        (worktree / "demo-proposed.dag.json").write_text(json.dumps({"nodes": []}))
        Path(prepared["result"]).parent.mkdir(parents=True, exist_ok=True)
        Path(prepared["result"]).write_text(json.dumps(dict(result(prepared), artifacts=["PLAN-demo.md", "demo-proposed.dag.json"])))
        return "STARTED: claude pid=1 ledger_id=fixture-plan"

    out = node_worker.run_node("demo-PLAN-first", send=send, **kwargs)
    assert out["state"] == "result"
    folder = root / "team/feature-teams"
    assert (folder / "demo-plan.md").read_text() == "# plan\n" and json.loads((folder / "demo-proposed.dag.json").read_text()) == {"nodes": []}
    rec = store.load("demo-PLAN-first")
    stored = json.loads(Path(rec["result"]).read_text())
    assert rec["result"] == str(root / "team/loop/results/demo-PLAN-first.json")
    assert stored["artifacts"] == [str(folder / "demo-plan.md"), str(folder / "demo-proposed.dag.json")]


def test_worker_process_that_exits_without_a_result_fails_the_run_now(case, tmp_path):
    store, brief, clock, _, _, kwargs = case
    root = kwargs["root"]
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()  # a pid that is no longer alive
    log = tmp_path / "claude-fixture.log"
    log.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "I could not write the plan: every path under data/ is denied on this lane."}]}}) + "\n")
    current = dict(brief, run_id="demo-W0-gone", channel="claude:worker", result=str(root / "team/results/gone.json"))
    store.create(current)
    kwargs.pop("store")
    started = clock()

    def send(prepared, message, *, receipt_timeout):
        return f"STARTED: claude pid={gone.pid} ledger_id=fixture-gone log={log}"

    out = node_worker.run_node("demo-W0-gone", send=send, **kwargs)
    assert out["state"] == "result" and clock() - started < 600  # not the 1200 s deadline
    stored = json.loads(Path(out["result"]).read_text())
    assert stored["status"] == "failed" and stored["sha"] is None
    assert stored["open_questions"] == ["I could not write the plan: every path under data/ is denied on this lane."]
    assert stored["proof"][0]["command"] == "worker process exited without writing a result" and stored["proof"][0]["log"] == str(log)
    rows = [json.loads(line) for line in (root / "team/loop/events.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "dispatch_refused" and "exited without writing a result" in r["detail"] for r in rows)


def test_the_loop_brief_names_its_features_rank_from_the_queue(tmp_path):
    root = tmp_path / "data"
    (root / "team").mkdir(parents=True)
    (root / "team/feature-queue.json").write_text(json.dumps({"features": [{"name": "loop-building"}, {"name": "long-input"}]}))
    node = dict(id="N0", title="Synthetic work", inputs=[], intern_friendly=False, tier="T1", acceptance="check", files=[])
    brief = contract.brief_from_node(node, run_id="long-input-N0-x", feature="long-input", channel="codex-exec:0",
                                     model="m", effort="high", base="origin/master", worktree=str(tmp_path / "wt"),
                                     inputs={}, deadline=5000, result=str(root / "r.json"), rules="AGENTS.md")
    assert node_worker.render_brief(dict(brief=brief, feature="long-input"), root).rstrip().endswith("Priority: 2")
    assert node_worker.render_brief(dict(brief=brief, feature="audit"), root).rstrip().endswith("Priority: 99")
    assert "Priority:" not in node_worker.render_brief(dict(brief=brief, feature="long-input"))


def test_a_multi_line_node_acceptance_becomes_one_brief_item_per_line(case):
    """The site PLAN run of 2026-10-15 escalated at dispatch: its node's acceptance carried an inlined
    multi-line input and the contract takes one printable line per item. prepare_run splits the node text into
    lines, collapses whitespace, drops unprintable characters and duplicates, and the brief validates."""
    store, brief, _, _, _, kwargs = case
    dag_path = kwargs["root"] / "team/feature-teams/demo.dag.json"
    node = json.loads(dag_path.read_text())["nodes"][0]
    node["acceptance"] = "fixture check passes\n\nInput spec L1:\tline  one\u200b\nInput spec L1:\tline  one\n"
    dag_path.write_text(json.dumps({"nodes": [node]}))
    prepared = node_worker.prepare_run(store.load(brief["run_id"]), kwargs["root"], kwargs["repo"])
    assert contract.validate_brief(prepared["brief"])["acceptance"] == ["fixture check passes", "Input spec L1: line one"]
    assert contract.acceptance_items(" \n\t\n") == []


@pytest.mark.parametrize("gate_exit", [0, 1])
def test_a_done_plan_without_a_sha_is_committed_through_the_commit_gate(case, monkeypatch, gate_exit):
    """Site PLAN rehearsal (2026-10-15): the Claude channel denies pytest, the worker kept AGENTS.md rule 5 and
    returned done with sha null, and verify refuses a result without a sha. The node worker commits the plan's
    worktree artifacts through scripts/commit_gate.py (guards on the staged candidate); a gate refusal escalates."""
    store, brief, _, _, _, kwargs = case
    root = kwargs["root"]
    _node_dag(root, {"id": "PLAN", "title": "Write the plan", "inputs": [], "kind": "plan",
                     "files": ["NEW PLAN-demo.md", "NEW demo-proposed.dag.json"], "acceptance": "Write the plan.",
                     "intern_friendly": False, "tier": "T1"})
    store.create(dict(brief, run_id="demo-PLAN-nosha", node="PLAN", channel="claude:worker", kind="plan",
                      files=["NEW PLAN-demo.md", "NEW demo-proposed.dag.json"]))
    calls, real_run = [], subprocess.run

    def fake_run(argv, **kw):
        if "commit_gate.py" in " ".join(map(str, argv)):
            calls.append((argv, kw.get("cwd")))
            return subprocess.CompletedProcess(argv, gate_exit, "pytest refused: guard failed" if gate_exit else "ok", "")
        if argv[:2] == ["git", "-C"] and argv[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, "1234abcd\n", "")
        return real_run(argv, **kw)

    monkeypatch.setattr(node_worker.subprocess, "run", fake_run)

    def send(prepared, message, *, receipt_timeout):
        worktree = Path(prepared["worktree"])
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "PLAN-demo.md").write_text("# plan\n")
        (worktree / "demo-proposed.dag.json").write_text(json.dumps({"nodes": []}))
        Path(prepared["result"]).parent.mkdir(parents=True, exist_ok=True)
        Path(prepared["result"]).write_text(json.dumps(dict(result(prepared), sha=None,
                                                            artifacts=["PLAN-demo.md", "demo-proposed.dag.json"])))
        return "STARTED: claude pid=1 ledger_id=fixture-plan-nosha"

    out = node_worker.run_node("demo-PLAN-nosha", send=send, **kwargs)
    (argv, cwd), = calls
    assert argv[-2:] == ["PLAN-demo.md", "demo-proposed.dag.json"] and Path(cwd) == Path(out["brief"]["worktree"])
    if gate_exit:
        detail = out["history"][-1]["detail"]
        assert out["state"] == "escalated" and detail.startswith("node worker failed: ValueError: result: commit gate")
        assert "guard failed" in detail  # the cause is on the record, not only the exception type
    else:
        rec = store.load("demo-PLAN-nosha")
        assert out["state"] == "result" and rec["sha"] == "1234abcd"
        assert json.loads(Path(rec["result"]).read_text())["sha"] == "1234abcd"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "master")
    (path / "a.txt").write_text("1")
    _git(path, "add", "a.txt")
    _git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "one")
    _git(path, "update-ref", "refs/remotes/origin/master", "HEAD")
    return _git(path, "rev-parse", "HEAD")


def _advance(repo, name="b.txt"):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", name)
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    return _git(repo, "rev-parse", "HEAD")


def _handoff_personal_guard(source):
    """Index checks from the real personal-file guard, without the throwaway-repo cases below them.

    The commit gate and the verifier each run this file. Those cases init a git repo per planted path, and no
    assertion in this module depends on them. The checks above that helper are copied as they stand in the real file.
    """
    text = source.read_text(encoding="utf-8")
    marker = "\ndef _repo_with("
    head, sep, _tail = text.partition(marker)
    if not sep:
        raise RuntimeError("personal-file guard no longer has the throwaway repo helper")
    return head


@pytest.fixture
def build_handoff(case, monkeypatch):
    """Real disposable Git and verifier. Guards are the repo's own files, aside from the personal-file
    throwaway-repo cases (_handoff_personal_guard). Only the model is a local fixture."""
    from companion.feature_team import completion

    store, brief, clock, _, _, kwargs = case
    repo, root = kwargs["repo"], kwargs["root"]
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git_repo(repo)
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@localhost")
    (repo / ".gitignore").write_text(".lane-send/\n__pycache__/\n.pytest_cache/\n")
    if not (node_worker.REPO / "tests/test_no_personal_files.py").exists():
        pytest.skip("the repo's privacy guards are not part of this export")
    (repo / "tests").mkdir()
    for name in ("test_no_third_party_pii.py", "test_no_personal_files.py"):
        source = node_worker.REPO / "tests" / name
        dest = repo / "tests" / name
        if name == "test_no_personal_files.py":
            dest.write_text(_handoff_personal_guard(source), encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(source, dest)
    (repo / "tests/test_fixture.py").write_text(
        "import runpy\n\n\ndef test_candidate():\n    assert runpy.run_path('fixture.py')['VALUE'] == 42\n")
    _git(repo, "add", ".gitignore", "tests")
    _git(repo, "commit", "-qm", "fixture guards")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(completion, "DATA_DIR", root)
    kwargs["make_worktree"] = node_worker._worktree
    return store, brief, clock, kwargs, base


def _build_receipt(prepared, *, code=0):
    process = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({code})"])
    process.wait()
    return node_worker.BuildReceipt(f"STARTED: fixture pid={process.pid} ledger_id=build-fixture", process)


@pytest.mark.parametrize("channel, candidate_kind", [
    ("muse:0", "regular"), ("grok:g-build-1", "executable"), ("muse:0", "deletion")])
def test_build_handoff_commits_then_verifies_exact_candidate(build_handoff, monkeypatch, tmp_path, channel,
                                                            candidate_kind):
    from companion.feature_team import verify

    store, original, _, kwargs, base = build_handoff
    lane = node_worker._lane_send()
    lane.GROK_HOME = tmp_path / "grok"
    monkeypatch.setattr(node_worker, "_lane_send", lambda: lane)
    brief = dict(original, run_id="demo-W0-handoff", channel=channel)
    artifacts = ["fixture.py"]
    content = ("#!/usr/bin/env python3\n" if candidate_kind == "executable" else "") + "VALUE = 42\n"
    if candidate_kind == "deletion":
        artifacts.append("a.txt")
        brief["files"] = ["NEW fixture.py", "a.txt"]
        _node_dag(kwargs["root"], {"id": "W0", "title": "Fixture deletion", "inputs": [], "files": brief["files"],
                                  "acceptance": "fixture check passes", "intern_friendly": True, "tier": "T1"})
    store.create(brief)

    def send(prepared, message, *, receipt_timeout):
        worktree = Path(prepared["worktree"])
        (worktree / "fixture.py").write_text(content)
        if candidate_kind == "executable":
            (worktree / "fixture.py").chmod(0o755)
        if candidate_kind == "deletion":
            (worktree / "a.txt").unlink()
        write_result(prepared, dict(result(prepared), sha=None, artifacts=artifacts,
                                    branch=_git(worktree, "branch", "--show-current"),
                                    tests=["tests/test_fixture.py"]))
        assert not any("commit_gate.py" in c or c.startswith("git add") or c.startswith("git commit")
                       for c in _instructed_commands(message))
        assert "Leave changes uncommitted and unstaged" in message
        example = json.JSONDecoder().raw_decode(message.split("an integer):\n", 1)[1])[0]
        assert contract.validate_result(example)["sha"] is None
        assert example["branch"] == _git(worktree, "branch", "--show-current")
        assert example["artifacts"] == artifacts
        # Exercise send_channel's actual process capture. The substituted transport starts only a local no-op.
        def launch(folder, text, **kw):
            process = lane.subprocess.Popen([sys.executable, "-c", "pass"])
            process.wait()  # A fast worker may already be reaped when the receipt reaches run_node.
            log = tmp_path / "worker.log"
            log.write_text("fixture worker\n")
            return f"{channel.partition(':')[0]} started pid {process.pid} in {folder} (log {log}) ledger_id=build-fixture"

        monkeypatch.setattr(lane, f"send_{channel.partition(':')[0]}", launch)
        return node_worker.send_channel(prepared, message, receipt_timeout=receipt_timeout, root=kwargs["root"])

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "result", out["history"]
    checked = verify.verify_run(brief["run_id"], repo=kwargs["repo"], data_dir=kwargs["root"],
                                python=sys.executable, scratch_root=tmp_path / "verify")
    assert checked["passed"], checked["reason"]
    assert any("tests/test_fixture.py" in c["command"] and c["exit"] == 0 for c in checked["commands"])
    sha = out["sha"]
    assert len(sha) == 40 and sha != base
    assert _git(kwargs["repo"], "rev-parse", f"{sha}^") == base
    assert set(_git(kwargs["repo"], "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()) == set(artifacts)
    assert _git(kwargs["repo"], "show", f"{sha}:fixture.py") == content.strip()
    assert _git(kwargs["repo"], "ls-tree", sha, "--", "fixture.py").startswith(
        "100755" if candidate_kind == "executable" else "100644")
    if candidate_kind == "deletion":
        assert _git(kwargs["repo"], "ls-tree", sha, "--", "a.txt") == ""
    assert json.loads(Path(out["result"]).read_text())["sha"] == sha
    assert store.load(brief["run_id"])["state"] == "verify_pass"


@pytest.mark.parametrize("refusal", ["guard", "commit", "failed", "blocked", "exit", "live", "unproven",
                                     "foreign-result", "foreign-branch", "foreign-path", "foreign-artifacts",
                                     "staged", "head", "candidate-drift", "foreign-pid", "linked-path", "linked-result",
                                     "worker-sha"])
def test_build_handoff_refuses_without_committing(build_handoff, monkeypatch, tmp_path, refusal):
    store, original, clock, kwargs, base = build_handoff
    brief = dict(original, run_id="demo-W0-refusal", channel="muse:0")
    store.create(brief)
    if refusal == "guard":
        # A synthetic signing identifier triggers the unchanged index-reading privacy guard.
        node = {"id": "W0", "title": "Fixture signing", "inputs": [], "files": ["NEW fixture.pbxproj"],
                "acceptance": "fixture check", "intern_friendly": True, "tier": "T1"}
        _node_dag(kwargs["root"], node)
    elif refusal == "commit":
        hook = kwargs["repo"] / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    elif refusal == "candidate-drift":
        gate = node_worker._commit_gate()
        run = gate.run

        def change_candidate(argv, cwd):
            output = run(argv, cwd)
            if argv[1:3] == ["-m", "pytest"]:
                (Path(cwd) / "fixture.py").write_text("VALUE = 99\n")
            return output

        monkeypatch.setattr(gate, "run", change_candidate)
        monkeypatch.setattr(node_worker, "_commit_gate", lambda: gate)
    processes, seen = [], []

    def send(prepared, message, *, receipt_timeout):
        seen.append(prepared)
        worktree = Path(prepared["worktree"])
        name = "fixture.pbxproj" if refusal == "guard" else "fixture.py"
        content = "DEVELOPMENT_TEAM = ABC123DEF4;\n" if refusal == "guard" else "VALUE = 42\n"
        (worktree / name).write_text(content)
        value = dict(result(prepared), sha=None, artifacts=[name],
                     branch=_git(worktree, "branch", "--show-current"))
        if refusal == "worker-sha":
            value["sha"] = base
        if refusal in {"failed", "blocked"}:
            value.update(status=refusal, open_questions=["fixture refusal"])
        if refusal == "foreign-result":
            value["run_id"] = "another-run"
        if refusal == "foreign-branch":
            value["branch"] = "session/foreign"
        if refusal == "foreign-path":
            (worktree / "foreign.py").write_text("VALUE = 0\n")
            value["artifacts"].append("foreign.py")
        if refusal == "foreign-artifacts":
            value["artifacts"] = ["a.txt"]
        if refusal == "staged":
            _git(worktree, "add", name)
        if refusal == "head":
            _git(worktree, "checkout", "--detach", "HEAD")
        if refusal == "linked-path":
            (worktree / name).unlink()
            (worktree / name).symlink_to(worktree / "a.txt")
        write_result(prepared, value)
        if refusal == "linked-result":
            external = tmp_path / "external-result.json"
            Path(prepared["result"]).replace(external)
            Path(prepared["result"]).symlink_to(external)
        if refusal == "unproven":
            return "STARTED: fixture pid=1 ledger_id=build-fixture"
        if refusal == "live":
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            processes.append(process)
            return node_worker.BuildReceipt(f"STARTED: fixture pid={process.pid} ledger_id=build-fixture", process)
        receipt = _build_receipt(prepared, code=1 if refusal == "exit" else 0)
        if refusal == "foreign-pid":
            return node_worker.BuildReceipt("STARTED: fixture pid=1 ledger_id=build-fixture", receipt.process)
        return node_worker.BuildReceipt(str(receipt).replace("build-fixture", f"build-fixture-{len(seen)}"),
                                       receipt.process)

    try:
        out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    finally:
        for process in processes:
            process.terminate()
            process.wait()
    worktree = Path(seen[-1]["worktree"])
    assert _git(worktree, "rev-parse", "HEAD") == base
    assert out["sha"] is None
    if refusal in {"failed", "blocked"}:
        assert out["state"] == "result"
        assert json.loads(Path(out["result"]).read_text())["status"] == refusal
    elif refusal == "live":
        assert out["state"] == "timeout" and clock() == brief["deadline"]
    else:
        assert out["state"] == "escalated", out["history"]
    if refusal in {"guard", "commit"}:
        detail = out["history"][-1]["detail"]
        assert "commit gate refused" in detail
        assert ("pytest exit" if refusal == "guard" else "git commit failed") in detail
        assert _git(worktree, "diff", "--cached", "--name-only") == ""
    if refusal == "candidate-drift":
        assert "candidate or custody changed" in out["history"][-1]["detail"]
        assert _git(worktree, "diff", "--cached", "--name-only") == ""
    if refusal == "foreign-result":
        assert len(seen) == 2  # The bounded correction also cannot commit another run's result.
    if refusal == "worker-sha":
        assert "worker-supplied SHA" in out["history"][-1]["detail"]
        assert _git(worktree, "diff", "--cached", "--name-only") == ""
        assert not (kwargs["root"] / "team/loop/commits" / f"{brief['run_id']}.log").exists()


@pytest.fixture
def default_umask():
    """The landing runner runs with umask 077; this git blob test needs the usual 022 for its own files."""
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.mark.parametrize("drift", ["content", "mode"])
def test_build_handoff_refuses_committed_blob_drift(default_umask, build_handoff, monkeypatch, drift):
    """A writer changes only what git add sees, restores tested bytes, then matches the committed tree."""
    store, original, _, kwargs, base = build_handoff
    brief = dict(original, run_id="demo-W0-blob-drift", channel="muse:0")
    store.create(brief)
    gate = node_worker._commit_gate()
    run = gate.run
    seen = []

    def change_during_add(argv, cwd):
        path = Path(cwd) / "fixture.py"
        if argv[:1] == ["git"] and argv[3:4] == ["add"]:
            if drift == "content":
                path.write_text("VALUE = 99\n")
            else:
                path.chmod(0o755)
            output = run(argv, cwd)
            path.write_text("VALUE = 42\n")
            path.chmod(0o644)
            return output
        output = run(argv, cwd)
        if argv[:1] == ["git"] and argv[3:4] == ["commit"] and output.returncode == 0:
            if drift == "content":
                path.write_text("VALUE = 99\n")
            else:
                path.chmod(0o755)
        return output

    monkeypatch.setattr(gate, "run", change_during_add)
    monkeypatch.setattr(node_worker, "_commit_gate", lambda: gate)

    def send(prepared, message, *, receipt_timeout):
        seen.append(prepared)
        worktree = Path(prepared["worktree"])
        (worktree / "fixture.py").write_text("VALUE = 42\n")
        write_result(prepared, dict(result(prepared), sha=None, artifacts=["fixture.py"],
                                    branch=_git(worktree, "branch", "--show-current")))
        return _build_receipt(prepared)

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    worktree = Path(seen[0]["worktree"])
    # This probes the required post-commit boundary: retain the commit for inspection, never publish its SHA.
    assert _git(worktree, "rev-parse", "HEAD") != base
    assert _git(worktree, "status", "--porcelain") == ""
    assert _git(worktree, "show", "HEAD:fixture.py") == ("VALUE = 99" if drift == "content" else "VALUE = 42")
    assert _git(worktree, "ls-tree", "HEAD", "--", "fixture.py").startswith(
        "100755" if drift == "mode" else "100644")
    assert out["state"] == "escalated", out["history"]
    assert out["sha"] is None
    assert "committed blobs differ from the bound candidate" in out["history"][-1]["detail"]
    assert not (kwargs["root"] / "team/loop/results" / f"{brief['run_id']}.json").exists()


def test_build_handoff_exclusive_custody_prevents_another_send(build_handoff):
    import fcntl
    import hashlib

    store, original, _, kwargs, base = build_handoff
    brief = dict(original, run_id="demo-W0-busy", channel="muse:0")
    store.create(brief)
    prepared = node_worker.prepare_run(store.load(brief["run_id"]), kwargs["root"], kwargs["repo"])
    key = hashlib.sha256(str(Path(prepared["worktree"]).resolve()).encode()).hexdigest()
    folder = kwargs["root"] / "team/loop/build-custody"
    folder.mkdir(parents=True)
    with (folder / f"{key}.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def send(*args, **kwargs):
            pytest.fail("another coordinator owns this worktree")

        out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "escalated" and "another coordinator owns" in out["history"][-1]["detail"]
    assert _git(kwargs["repo"], "rev-parse", "HEAD") == base
    assert not Path(prepared["worktree"]).exists()


def test_a_second_attempt_on_the_same_day_gets_a_current_worktree(tmp_path):
    """Rehearsal of the PLAN loop (2026-10-15): every attempt of a node on one day shares one name, so a second
    attempt failed at `worktree add -b` (the branch existed), and the dispatcher's reuse ran the 3D PLAN retry in the
    first attempt's worktree, 61 commits behind master. plan_worktree decides before anything is sent: a free name
    gets a new branch; a leftover branch with nothing of its own is moved to the base; a clean worktree with nothing
    of its own is fast-forwarded to the base; anything holding unharvested work keeps its name and the run takes a
    suffixed one. Every command names the exact base commit, and running it gives a worktree at that base."""
    repo = tmp_path / "repo"
    first = _git_repo(repo)
    name, command = node_worker.plan_worktree(repo, "2026-10-15-demo-PLAN", "origin/master")
    assert name == "2026-10-15-demo-PLAN" and command[3:6] == ["worktree", "add", "-b"] and command[-1] == first
    node_worker._worktree(command)
    path = repo / ".claude/worktrees" / name
    # master moves on; the clean worktree of the first attempt is fast-forwarded, not reused as it was
    base = _advance(repo)
    (path / ".lane-send").mkdir()
    (path / ".lane-send/old.result.json").write_text("{}")  # the previous attempt's result file is not work
    again, command = node_worker.plan_worktree(repo, "2026-10-15-demo-PLAN", "origin/master")
    assert again == name and command == ["git", "-C", str(path), "merge", "--ff-only", "--quiet", base]
    node_worker._worktree(command)
    assert _git(path, "rev-parse", "HEAD") == base
    # a leftover branch with no folder and nothing of its own (the folder was removed): moved to the base
    _git(repo, "worktree", "remove", "--force", str(path))
    base = _advance(repo, "c.txt")
    again, command = node_worker.plan_worktree(repo, name, "origin/master")
    assert again == name and command[3:6] == ["worktree", "add", "-B"]
    node_worker._worktree(command)
    assert _git(path, "rev-parse", "HEAD") == base and _git(path, "branch", "--show-current") == f"session/{name}"
    # uncommitted work in it: kept, the run takes name-2
    (path / "draft.md").write_text("work in progress")
    suffixed, command = node_worker.plan_worktree(repo, name, "origin/master")
    assert suffixed == f"{name}-2" and command[-3:] == [f"session/{name}-2", str(repo / ".claude/worktrees" / suffixed), base]
    # its own commit (unharvested): kept as well, even once the folder is clean
    _git(path, "add", "draft.md")
    _git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "draft")
    assert node_worker.plan_worktree(repo, name, "origin/master")[0] == f"{name}-2"
    # a folder that is not this branch's worktree is never used silently
    (repo / ".claude/worktrees" / f"{name}-2").mkdir()
    assert node_worker.plan_worktree(repo, name, "origin/master")[0] == f"{name}-3"
    # a branch with nothing of its own that is checked out in some other folder is taken, not moved
    _git(repo, "worktree", "add", "-q", "-b", f"session/{name}-3", str(tmp_path / "elsewhere"), base)
    assert node_worker.plan_worktree(repo, name, "origin/master")[0] == f"{name}-4"


def test_the_brief_names_the_branch_and_worktree_the_run_uses(case, monkeypatch, tmp_path):
    """The rendered brief named a branch the run did not use (local date, lower-cased node: 'session/<today>-demo-
    plan' for a worktree on 'session/<UTC date>-demo-PLAN'). run_node now sends a brief whose worktree is the folder
    it made and whose branch line is that folder's branch; a second attempt that must keep the first worktree
    gets the suffixed path in both."""
    store, brief, clock, _, _, kwargs = case
    repo = kwargs["repo"]
    base = _git_repo(repo)
    kwargs.pop("make_worktree")
    sent = []

    def send(prepared, message, *, receipt_timeout):
        sent.append((prepared, message))
        write_result(prepared)
        return "STARTED: cursor ledger_id=fixture-1"

    for attempt in range(2):
        current = dict(brief, run_id=f"demo-W0-wt{attempt}", result=str(Path(brief["result"]).with_name(f"wt{attempt}.json")))
        store.create(current)
        assert node_worker.run_node(current["run_id"], send=send, **kwargs)["state"] == "result"
        worktree = Path(sent[-1][0]["worktree"])
        rec = store.load(current["run_id"])
        assert worktree.is_dir() and rec["worktree"] == str(worktree)
        assert _git(worktree, "branch", "--show-current") == f"session/{worktree.name}"
        assert _git(worktree, "rev-parse", "HEAD") == base
        assert str(worktree) in json.dumps(sent[-1][0])
        if attempt == 0:
            (worktree / "draft.md").write_text("unharvested")  # the second attempt must not take this folder
    assert Path(sent[1][0]["worktree"]).name == Path(sent[0][0]["worktree"]).name + "-2"


def test_a_watched_grok_receipt_keeps_its_pid_so_a_dead_worker_fails_the_run_now(case, tmp_path, monkeypatch):
    """The courier's watched grok/muse receipt ('<kind> started pid N ... ledger_id=<id>') was rewritten to a STARTED
    line without the pid, so run_node's dead-worker check never fired for those channels (found 2026-10-15). The pid
    now rides on the STARTED receipt, as it does for Claude and Codex; a plain courier receipt keeps it too."""
    store, brief, clock, _, _, kwargs = case
    root = kwargs["root"]
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    home = tmp_path / "kyra-grok"
    recorded = []

    class Lane:
        GROK_HOME = home

        @staticmethod
        def send_grok(folder, message, **kw):
            return f"grok started pid {gone.pid} in {folder} (log /tmp/lane-send/g.log) ledger_id=grok-from-courier"

        @staticmethod
        def brief_paths(text):
            return []

        @staticmethod
        def record_package(*args, **kw):
            recorded.append(kw)
            return kw.get("package_id") or "grok-plain-row"

    monkeypatch.setattr(node_worker, "_lane_send", lambda: Lane)
    monkeypatch.setattr(node_worker, "channel_blocker", lambda brief, lane: None)
    sent = dict(channel="grok:g-build-1", worktree=str(home / "g-build-1--x"), effort="high", model="grok-4.7",
                feature="demo", run_id="demo-W0-x")
    receipt = node_worker.send_channel(sent, "brief", receipt_timeout=5, root=root)
    assert node_worker._receipt_field(receipt, "pid") == str(gone.pid)
    assert node_worker._receipt_id(receipt) == "grok-from-courier" and recorded == []
    Lane.send_grok = staticmethod(lambda folder, message, **kw: f"grok started pid {gone.pid} in {folder} (log /tmp/x.log)")
    plain = node_worker.send_channel(sent, "brief", receipt_timeout=5, root=root)
    assert node_worker._receipt_field(plain, "pid") == str(gone.pid) and recorded  # the plain path records its row
    courier_log = tmp_path / "grok-courier.log"  # the courier's log exists; a missing one is an error by design
    courier_log.write_text("grok: started\n")
    Lane.send_grok = staticmethod(lambda folder, message, **kw:
                                  f"grok started pid {gone.pid} in {folder} (log {courier_log}) ledger_id=grok-x")
    current = dict(brief, run_id="demo-W0-grokgone", channel="grok:g-build-1",
                   result=str(root / "team/results/grokgone.json"))
    store.create(current)
    kwargs.pop("store")
    monkeypatch.setattr(node_worker, "_lane_send", lambda: Lane)
    started = clock()

    def send(prepared, message, *, receipt_timeout):
        return node_worker.send_channel(prepared, message, receipt_timeout=receipt_timeout, root=root)

    out = node_worker.run_node("demo-W0-grokgone", send=send, **kwargs)
    assert out["state"] == "result" and clock() - started < 600, store_history(root, "demo-W0-grokgone")
    # the dead-pid check fired (not the 1200 s deadline)
    stored = json.loads(Path(out["result"]).read_text())
    assert stored["status"] == "failed" and stored["proof"][0]["command"] == "worker process exited without writing a result"


def store_history(root, run_id):
    return [h["detail"][:160] for h in runs.Runs(root / "team/loop", emit=lambda row: None).load(run_id)["history"]]


def test_a_grok_run_worktree_sits_under_grok_home_and_passes_both_blockers(tmp_path):
    """2026-10-15: prepare_run put every run under .claude/worktrees and the grok preflight refuses anything not
    directly under GROK_HOME, so a grok loop channel could never dispatch. A grok run's worktree is now
    GROK_HOME/<target>--<run name>; codex-exec runs stay under .claude/worktrees; the lane id is grok-<target> and
    the watch row grok-<folder> starts with it."""
    from companion.feature_team import dispatch
    lane = node_worker._lane_send()
    repo = tmp_path / "repo"
    name = "2026-10-15-site-PLAN"
    grok_path = node_worker.run_worktree(repo, name, "grok:g-build-1")
    assert grok_path == lane.GROK_HOME / "g-build-1--2026-10-15-site-PLAN"
    assert node_worker.run_worktree(repo, name, "codex-exec:0") == repo / ".claude/worktrees" / name
    assert node_worker.run_worktree(repo, name, "claude:fable") == repo / ".claude/worktrees" / name
    root = tmp_path / "data"
    root.mkdir()
    (repo / "scripts").mkdir(parents=True)
    for kind, channel in (("plan", "grok:g-build-1"), ("code", "grok:g-build-2")):
        node = dict(id="PLAN", title="Write the plan", inputs=[], intern_friendly=False, tier="T1",
                    acceptance="Synthetic check", files=["NEW PLAN-site.md"], kind=kind)
        brief = contract.brief_from_node(node, run_id="site-PLAN-x", feature="site", channel=channel,
                                         model="grok-4.7", effort="high", base="origin/master",
                                         worktree=str(node_worker.run_worktree(repo, name, channel)), inputs={},
                                         deadline=5000, result=str(root / "team/loop/results/site-PLAN-x.json"),
                                         rules="AGENTS.md")
        bound, staging = node_worker.bind_to_worktree(brief, root, repo)
        assert node_worker.channel_blocker(bound, lane) is None and node_worker.path_blocker(bound) is None
        assert Path(bound["worktree"]).parent == lane.GROK_HOME.resolve() and staging == {}
        assert bound["result"].startswith(str(lane.GROK_HOME / f"{channel.partition(':')[2]}--{name}"))
    assert dispatch.lane_id("grok:g-build-1") == "grok-g-build-1"
    states = {"grok-g-build-1--2026-10-14-old-N1": "dead", "grok-g-build-1--2026-10-15-site-PLAN": "busy",
              "grok-g-build-2--2026-10-15-site-PLAN": "idle"}
    assert dispatch.observed_state(states, "grok:g-build-1") == "busy"  # a live run row wins
    assert dispatch.observed_state(states, "grok:g-build-2") == "idle"
    # no row: idle, because a loop seat's busy-ness is its run record (night 2026-10-17: grok:memory-scale never
    # ran, so "no idle evidence" kept it out of every tick)
    assert dispatch.observed_state(states, "grok:g-build-3") == "idle"
    assert dispatch.observed_state({"grok-g-build-1": "idle", **states}, "grok:g-build-1") == "busy"  # never masked
    # built from watch rows with timestamps: a live row wins, otherwise the newest row, exact or run
    rows = [dict(lane="grok-g-build-1", state="idle", t=100), dict(lane="grok-g-build-1--2026-10-15-site-PLAN", state="idle", t=300),
            dict(lane="grok-g-build-1--2026-10-15-alpha-N2", state="dead", t=200), dict(lane="grok-g-build-10--x", state="working", t=1)]
    folded = dispatch.lane_states(rows)
    assert folded["grok-g-build-1"] == "idle" and folded["grok-g-build-10"] == "working"
    rows[2]["state"] = "working"
    assert dispatch.lane_states(rows)["grok-g-build-1"] == "working"
    rows[2]["state"], rows[1]["state"] = "dead", "dead"
    assert dispatch.lane_states(rows)["grok-g-build-1"] == "dead"  # the newest row by time, not by name
    bare = contract.brief_from_node(node, run_id="site-PLAN-y", feature="site", channel="grok", model="grok-4.7",
                                    effort="high", base="origin/master", worktree=str(lane.GROK_HOME / name),
                                    inputs={}, deadline=5000, result=str(root / "r.json"), rules="AGENTS.md")
    assert "GROK_HOME" in node_worker.channel_blocker(bare, lane)


def test_a_grok_run_plans_its_worktree_under_grok_home(tmp_path, monkeypatch):
    """lb-gw puts a grok run's folder under GROK_HOME as <target>--<name>; the worktree planner must look there, keep
    the branch session/<name>, and take the next suffix there too (the rebase onto plan_worktree, 2026-10-15)."""
    repo = tmp_path / "repo"
    base = _git_repo(repo)
    home = tmp_path / "kyra-grok"
    monkeypatch.setattr(node_worker, "_lane_send", lambda: type("L", (), {"GROK_HOME": home}))
    name, command = node_worker.plan_worktree(repo, "2026-10-16-demo-N0", "origin/master", "grok:g-build-1")
    assert name == "2026-10-16-demo-N0"
    assert command[-3:] == ["session/2026-10-16-demo-N0", str(home / "g-build-1--2026-10-16-demo-N0"), base]
    node_worker._worktree(command)
    (home / "g-build-1--2026-10-16-demo-N0" / "draft.md").write_text("unharvested")
    again, command = node_worker.plan_worktree(repo, "2026-10-16-demo-N0", "origin/master", "grok:g-build-1")
    assert again == "2026-10-16-demo-N0-2" and command[-2] == str(home / "g-build-1--2026-10-16-demo-N0-2")


@pytest.mark.parametrize("log_text, pid, alive, launch", [
    ("", "999999", False, True),                    # exited at once, empty log: a launch failure
    ("worked for a while\n", "999999", False, False),  # it ran: the correction
    ("", "999999", True, False),                    # a live CLI that buffers its output: the correction
    ("", None, False, False),                       # no pid (cursor, codex-exec, grok): unknown, the correction
])
def test_an_invalid_result_with_an_empty_log_at_once_is_a_launch_failure_not_a_correction(case, tmp_path, monkeypatch,
                                                                                          log_text, pid, alive,
                                                                                          launch):
    """3d-inner-demo G1, 2026-10-16: claude:opus ended in 2.5 min with a 0-byte log and a blocked, invalid result.
    That is a launch failure on the channel (one timeout, no correction on the same channel); a worker whose log
    shows it ran still gets its one correction."""
    store, brief, _, events, _, kwargs = case
    log = tmp_path / "worker.log"
    log.write_text(log_text)
    monkeypatch.setattr(node_worker, "_alive", lambda p: alive)
    calls = []

    def send(prepared, message, *, receipt_timeout):
        calls.append(message)
        write_result(prepared, {"run_id": prepared["run_id"]})
        return "STARTED: cursor" + (f" pid={pid}" if pid else "") + f" ledger_id=fixture-{len(calls)} log={log}"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    if launch:
        assert out["state"] == "timeout" and len(calls) == 1
        assert out["history"][-1]["detail"].startswith(node_worker.LAUNCH_FAILURE)
        assert node_worker.is_launch_failure(out) and Path(out["partial"]).name.startswith("result.json.invalid-")
    else:
        assert out["state"] == "escalated" and len(calls) == 2 and not node_worker.is_launch_failure(out)


def test_a_worker_that_exits_at_once_with_an_empty_log_is_a_launch_failure(case, tmp_path, monkeypatch):
    store, brief, _, _, _, kwargs = case
    log = tmp_path / "worker.log"
    log.write_text("")
    monkeypatch.setattr(node_worker, "_alive", lambda pid: False)

    def send(prepared, message, *, receipt_timeout):
        return f"STARTED: cursor pid=999999 ledger_id=fixture-1 log={log}"

    out = node_worker.run_node(brief["run_id"], send=send, **kwargs)
    assert out["state"] == "timeout" and node_worker.is_launch_failure(out)
    assert not Path(brief["result"]).exists()                      # no failed_result written for a launch failure


def _claude_bash_rules(cwd):
    """The Bash allow and deny patterns lane_send really passes a Claude CLI worker (imported, never copied)."""
    flags = node_worker._lane_send().claude_permission_flags(cwd)

    def bash(flag):
        rules = flags[flags.index(flag) + 1].split(",")
        return [r[len("Bash("):-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]

    return bash("--allowedTools"), bash("--disallowedTools")


def _instructed_commands(text):
    """The indented lines under the brief's 'Commands (' header: what the worker is told to type."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Commands (")) + 1
    block = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        block.append(line.strip())
    return block


def test_worker_brief_commands_match_the_claude_allowlist(tmp_path):
    """3d-inner-demo G1 failed 4 times (2026-10-16): it wrote its files but its brief said bare pytest, ruff and
    date, which the claude:opus dontAsk allowlist refuses, so it could neither test nor commit."""
    from fnmatch import fnmatchcase

    node = {"id": "G1", "title": "Build the part", "inputs": [], "tier": "T1",
            "files": ["NEW src/companion/demo_part.py", "NEW tests/test_demo_part.py"]}
    brief = contract.brief_from_node(
        node, run_id="demo-G1-1", feature="demo", channel="claude:opus", model="claude-opus-5-5", effort="medium",
        base="origin/master", worktree=str(tmp_path / ".claude/worktrees/demo-G1"), inputs={},
        deadline=1_800_000_000.0, result=str(tmp_path / "result.json"), rules="AGENTS.md",
        acceptance=["pytest tests/test_demo_part.py", "ruff check src/companion/demo_part.py tests/test_demo_part.py",
                    "python scripts/demo_part.py --check", "bash -c 'echo hi | wc'", "the part renders"])
    commands = _instructed_commands(node_worker.render_brief({"brief": brief, "feature": "demo"}))
    allow, deny = _claude_bash_rules(Path(brief["worktree"]))
    assert any(c.endswith(" -m pytest tests/test_demo_part.py") for c in commands), commands
    assert any(" -m ruff check src/companion/demo_part.py" in c for c in commands), commands
    assert any(" scripts/commit_gate.py -m " in c for c in commands), commands
    assert not any("bash" in c for c in commands)  # a command the worker cannot run is left to verify.py
    for command in commands:
        assert not any(sep in command for sep in ("&&", "||", ";", "|")), command
        assert any(fnmatchcase(command, rule) for rule in allow), f"not in the claude allowlist: {command}"
        assert not any(fnmatchcase(command, rule) for rule in deny), f"denied for claude workers: {command}"


def test_site_worker_commits_with_plain_git_after_its_checks_not_commit_gate(tmp_path):
    """example-site S0 (2026-10-17) returned blocked: its brief told it to run scripts/commit_gate.py, which exists
    only in Kyra. A site worktree gets file checks, a personal-data grep and plain git add / git commit."""
    from fnmatch import fnmatchcase

    from companion.feature_team import repos

    worktree = repos.allowed()["example-site"].worktrees / "2026-10-17-example-site-S0-test"
    node = {"id": "S0", "title": "Survey", "inputs": [], "tier": "T0", "files": ["docs/site-survey.md"]}
    brief = contract.brief_from_node(
        node, run_id="example-site-S0-1", feature="example-site", channel="claude:haiku", model="claude-haiku-5",
        effort="medium", base="main", worktree=str(worktree), inputs={}, deadline=1_800_000_000.0,
        result=str(tmp_path / "result.json"), rules="AGENTS.md",
        acceptance=["test -s docs/site-survey.md", "pytest tests/test_x.py", "the survey lists the images"])
    text = node_worker.render_brief({"brief": brief, "feature": "example-site"})
    commands = _instructed_commands(text)
    assert not any("commit_gate" in c or "pytest" in c for c in commands), commands
    assert commands[0] == "test -s docs/site-survey.md", commands
    assert commands.index("git diff --check") < next(i for i, c in enumerate(commands) if c.startswith("grep "))
    assert [c for c in commands if c.startswith("git ")][-2:] == [
        "git add -- docs/site-survey.md",
        "git commit -m 'example-site S0: what changed and what passed'"]
    grep = next(c for c in commands if c.startswith("grep "))
    assert "@" in grep and "docs/site-survey.md" in grep
    allow, deny = _claude_bash_rules(worktree)
    for command in commands:
        assert not any(sep in command for sep in ("&&", "||", ";", "|")), command
        assert any(fnmatchcase(command, rule) for rule in allow), f"not in the claude allowlist: {command}"
        assert not any(fnmatchcase(command, rule) for rule in deny), command


def test_input_base_uses_master_when_inputs_sit_on_different_landed_branches(tmp_path):
    """MS1 (2026-10-17): inputs X1 and MS0B landed on master from two branches; the newest tip alone lacked X1."""
    first = _git_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "x1")
    x1 = _advance(tmp_path, "x1.txt")
    _git(tmp_path, "checkout", "-q", "master")
    _git(tmp_path, "checkout", "-q", "-b", "ms0b", first)
    ms0b = _advance(tmp_path, "ms0b.txt")
    _git(tmp_path, "checkout", "-q", "master")
    _git(tmp_path, "-c", "user.name=F", "-c", "user.email=f@localhost", "merge", "-q", "--no-ff", "-m", "m", "x1", "ms0b")
    _git(tmp_path, "update-ref", "refs/remotes/origin/master", "HEAD")
    master = _git(tmp_path, "rev-parse", "HEAD")
    assert node_worker.input_base(tmp_path, [(1, x1), (2, ms0b)], "origin/master") == master
    # nested inputs keep the newest tip; unlanded unrelated inputs keep it too; no inputs keep the default
    assert node_worker.input_base(tmp_path, [(1, first), (2, x1)], "d") == x1
    assert node_worker.input_base(tmp_path, [], "d") == "d"
