"""R0: the durable run record. State machine refusals, attempts per tier, atomic writes under a real SIGKILL,
resume from records plus completion.json, the index, the event seam and the inbox replay. Fakes only."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from companion.feature_team import contract, runs

FIX = Path(__file__).resolve().parent / "data" / "loop"
BRIEF = json.loads((FIX / "brief-ok.json").read_text())
RESULT = json.loads((FIX / "result-ok.json").read_text())


@pytest.fixture
def store(tmp_path):
    events = []
    s = runs.Runs(tmp_path / "loop", emit=events.append, clock=lambda: 1_000_000.0)
    s.events = events
    return s


def _brief(run_id="demo-X1-20260101T000000-a1b2c3", node="X1", inputs=(), deadline=1_000_000.0 + 3600, **over):
    b = dict(BRIEF, run_id=run_id, node=node, inputs=[{"node": i, "result": f"/work/{i}.json"} for i in inputs],
             deadline=deadline, **over)
    return contract.validate_brief(b)


def _result(run_id="demo-X1-20260101T000000-a1b2c3", node="X1", **over):
    return contract.validate_result(dict(RESULT, run_id=run_id, node=node, **over))


def _tick(store, dt=60):
    now = store.clock() + dt
    store.clock = lambda: now
    return now


# ---- create and transitions -----------------------------------------------------------------------------------------

def test_create_writes_a_queued_record_an_index_and_one_event(store):
    rec = store.create(_brief(), verify_command="pytest tests/test_demo.py")
    assert rec["state"] == "queued" and rec["attempts"] == {"T1": 0, "T2": 0, "T3": 0} and rec["tier"] == "T1"
    assert store.load(rec["run_id"]) == rec and rec["verify"] == {"command": "pytest tests/test_demo.py", "output": None}
    assert store.index()["features"]["demo"]["X1"]["latest"] == rec["run_id"]
    (event,) = store.events
    assert contract.validate_event(event)["kind"] == "queued" and event["run_id"] == rec["run_id"]
    with pytest.raises(ValueError, match="already has a record"):
        store.create(_brief())
    with pytest.raises(ValueError, match="tier"):
        store.create(_brief(run_id="other"), tier="T9")
    with pytest.raises(ValueError, match="acceptance"):
        store.create(dict(_brief(run_id="bad"), acceptance=[]))


def test_only_allowed_transitions_and_every_one_emits_its_kind(store):
    rid = store.create(_brief())["run_id"]
    with pytest.raises(ValueError, match="queued -> result is not allowed"):
        store.transition(rid, "result", result=_result())
    with pytest.raises(ValueError, match="queued -> landed"):
        store.transition(rid, "landed")
    store.transition(rid, "dispatched", deadline=1_000_000.0 + 900)
    store.transition(rid, "started", worker="Sol")
    store.transition(rid, "heartbeat", worker="Sol", detail="still running")
    store.transition(rid, "heartbeat", worker="Sol")
    rec = store.transition(rid, "result", worker="Sol", result=_result(), path="/work/x1/result.json")
    assert rec["sha"] == "0123abcd" and rec["branch"].startswith("session/") and rec["tokens"] == 12345
    assert rec["result"] == "/work/x1/result.json" and rec["artifacts"] == ["/work/demo-x1/reports/demo-x1.md"]
    rec = store.transition(rid, "verify_pass", worker="verify", path="/work/logs/verify.log", sha="0123abcd")
    assert rec["verify"]["output"] == "/work/logs/verify.log"
    with pytest.raises(ValueError, match="verify_pass -> dispatched"):
        store.transition(rid, "dispatched")
    rec = store.transition(rid, "landed", worker="train", detail="train34")
    assert rec["state"] == "landed" and [h["state"] for h in rec["history"]] == [
        "queued", "dispatched", "started", "heartbeat", "heartbeat", "result", "verify_pass", "landed"]
    assert [e["kind"] for e in store.events] == [h["state"] for h in rec["history"]]
    assert all(contract.validate_event(e) for e in store.events)
    with pytest.raises(ValueError, match="landed -> "):
        store.transition(rid, "escalated")


def test_late_heartbeat_after_timeout_is_ignored_not_raised(store):
    """A heartbeat arriving after timeout (or any closed state) is recorded as an ignored late
    event and never raises; the run stays in its closed state (AC1 crashed and stalled 37 h)."""
    rid = store.create(_brief())["run_id"]
    # replay a history ending in timeout, then a late heartbeat
    store.transition(rid, "dispatched", deadline=1_000_000.0 + 900)
    store.transition(rid, "started", worker="Sol")
    store.transition(rid, "heartbeat", worker="Sol")
    store.transition(rid, "timeout", detail="deadline expired")
    before = store.load(rid)
    _tick(store, 3600)
    rec = store.transition(rid, "heartbeat", worker="Sol", detail="still running")
    assert rec["state"] == "timeout"
    assert store.load(rid)["state"] == "timeout"
    # the ignored observation is recorded outside the state history: history[-1] stays the closure
    # reason recovery reads, and `updated` keeps aging the closed state from when it closed
    assert rec["history"] == before["history"] and rec["history"][-1]["detail"] == "deadline expired"
    assert rec["updated"] == before["updated"]
    (late,) = rec["late_events"]
    assert "ignored" in late["detail"] and "late" in late["detail"] and "heartbeat" in late["detail"]
    assert late["t"] == before["updated"] + 3600
    assert store.events[-1]["t"] == before["updated"] + 3600 and "ignored" in store.events[-1]["detail"]


@pytest.mark.parametrize("closing", ["escalated", "landed"])
def test_late_heartbeat_after_final_state_is_ignored(store, closing):
    rid = store.create(_brief())["run_id"]
    store.transition(rid, "dispatched", deadline=1_000_000.0 + 900)
    if closing == "landed":
        store.transition(rid, "result", result=_result(), path="/work/x1/result.json")
        store.transition(rid, "verify_pass", worker="verify")
    store.transition(rid, closing, detail="closure reason")
    before = store.load(rid)
    rec = store.transition(rid, "heartbeat", worker="Sol", detail="late ping")
    assert rec["state"] == closing and store.load(rid)["state"] == closing
    assert rec["history"] == before["history"] and rec["updated"] == before["updated"]
    assert "ignored" in rec["late_events"][-1]["detail"]


def test_late_heartbeat_after_verify_fail_keeps_the_verify_detail_last(store):
    """dispatch.verify_fail_followup reads history[-1] as the verify reason; a racing last heartbeat
    from the finished worker must not bury it."""
    rid = store.create(_brief())["run_id"]
    store.transition(rid, "dispatched", deadline=1_000_000.0 + 900)
    store.transition(rid, "result", result=_result(), path="/work/x1/result.json")
    store.transition(rid, "verify_fail", worker="verify", detail="verify output: 3 failed")
    before = store.load(rid)
    rec = store.transition(rid, "heartbeat", worker="Sol", detail="last ping")
    assert rec["state"] == "verify_fail" and rec["history"] == before["history"]
    assert rec["history"][-1]["detail"] == "verify output: 3 failed" and rec["updated"] == before["updated"]


def test_heartbeat_on_a_queued_run_is_refused(store):
    """The late-heartbeat exception covers closed attempts only; a heartbeat before dispatch is a
    contract error and writes nothing (review FIX-FIRST finding 3)."""
    rid = store.create(_brief())["run_id"]
    before = store.load(rid)
    with pytest.raises(ValueError, match="queued -> heartbeat is not allowed"):
        store.transition(rid, "heartbeat", worker="Sol")
    assert store.load(rid) == before and len(store.events) == 1


def test_result_transition_checks_the_result_binding_and_shape(store):
    rid = store.create(_brief())["run_id"]
    store.transition(rid, "dispatched")
    with pytest.raises(ValueError, match="needs the validated result"):
        store.transition(rid, "result")
    with pytest.raises(ValueError, match="must match the record"):
        store.transition(rid, "result", result=_result(run_id="someone-else"))
    with pytest.raises(ValueError, match="proof"):
        store.transition(rid, "result", result=dict(_result(), proof=[]))
    assert store.load(rid)["state"] == "dispatched"  # a refused transition writes nothing


def test_attempts_are_capped_per_tier_then_escalation_is_the_only_move(store):
    rid = store.create(_brief())["run_id"]
    store.transition(rid, "dispatched")
    store.transition(rid, "timeout", path="/work/partial.json")
    assert store.load(rid)["partial"] == "/work/partial.json"
    store.transition(rid, "dispatched")  # second T1 attempt
    store.transition(rid, "timeout")
    with pytest.raises(ValueError, match="T1 is exhausted; re-dispatch on T2 or escalate"):
        store.transition(rid, "dispatched")
    with pytest.raises(ValueError, match="keeps its tier or moves up"):
        store.transition(rid, "dispatched", tier="T0")
    store.transition(rid, "dispatched", tier="T2")
    store.transition(rid, "result", result=_result())
    store.transition(rid, "verify_fail", worker="verify", path="/work/logs/verify.log")
    store.transition(rid, "fixback", worker="dispatcher", detail="sent to author")
    store.transition(rid, "dispatched")  # second T2 attempt
    store.transition(rid, "timeout")
    with pytest.raises(ValueError, match="keeps its tier or moves up"):
        store.transition(rid, "dispatched", tier="T1")
    with pytest.raises(ValueError, match="T2 is exhausted; re-dispatch on T3 or escalate"):
        store.transition(rid, "dispatched")
    store.transition(rid, "dispatched", tier="T3")
    store.transition(rid, "timeout")
    store.transition(rid, "dispatched")
    store.transition(rid, "timeout")
    with pytest.raises(ValueError, match="every tier is exhausted; the only move is escalated"):
        store.transition(rid, "dispatched")
    rec = store.transition(rid, "escalated", detail="attempts exhausted")
    assert rec["attempts"] == {"T1": 2, "T2": 2, "T3": 2} and rec["tier"] == "T3"


# ---- atomic writes under a real interrupted write ---------------------------------------------------------------------

CHILD = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from companion.feature_team import runs
store = runs.Runs(Path(sys.argv[1]), emit=lambda row: None, clock=lambda: 1000000.0)
runs._slow_write = 30
print("ready", flush=True)
store.transition("demo-X1-20260101T000000-a1b2c3", "dispatched")
"""


def test_a_process_killed_mid_write_leaves_the_record_and_index_parseable(store, tmp_path):
    rec = store.create(_brief())
    src = str(Path(runs.__file__).resolve().parents[2])
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(store.root), src], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "KYRA_DATA_DIR": str(tmp_path)})
    assert child.stdout.readline().strip() == "ready"
    # The child pauses in product code after the temp file exists. Poll until that file shows up.
    deadline = time.monotonic() + 10
    pause = threading.Event()
    while time.monotonic() < deadline and not list(store.runs_dir.glob(".tmp-*")):
        pause.wait(0.001)
    assert list(store.runs_dir.glob(".tmp-*")), "the child never reached the temp write"
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    leftover = list(store.runs_dir.glob(".tmp-*"))
    assert leftover, "the kill must land before the rename for this test to mean anything"
    assert store.load(rec["run_id"]) == rec  # the old record, intact
    assert json.loads(store.index_path.read_text())["features"]["demo"]["X1"]["state"] == "queued"
    assert set(store.load_all()) == {rec["run_id"]}  # the temp file is ignored
    store.transition(rec["run_id"], "dispatched")  # and the store keeps working afterwards
    assert store.load(rec["run_id"])["state"] == "dispatched"


def test_unparseable_record_files_are_skipped_on_read(store):
    rec = store.create(_brief())
    (store.runs_dir / "broken.json").write_text("{not json")
    (store.runs_dir / "wrong-id.json").write_text(json.dumps(dict(rec, run_id="other")))
    assert set(store.load_all()) == {rec["run_id"]}


# ---- resume -----------------------------------------------------------------------------------------------------------

DAG = {"A": {"inputs": []}, "B": {"inputs": ["A"]}, "C": {"inputs": ["A"]}, "D": {"inputs": ["B", "C"]}, "E": {"inputs": []}}


def test_resume_reexecutes_nothing_verified_and_redispatches_the_one_run_past_its_deadline(store):
    now = store.clock()
    a = store.create(_brief(run_id="demo-A-1", node="A"))["run_id"]
    store.transition(a, "dispatched")
    store.transition(a, "result", result=_result(run_id=a, node="A"))
    store.transition(a, "verify_pass", worker="verify")
    b = store.create(_brief(run_id="demo-B-1", node="B", inputs=["A"]))["run_id"]
    store.transition(b, "dispatched", deadline=now + 100)   # will be past its deadline
    store.transition(b, "started", worker="Sol")
    c = store.create(_brief(run_id="demo-C-1", node="C", inputs=["A"]))["run_id"]
    store.transition(c, "dispatched", deadline=now + 5000)  # still running
    # simulated crash: a fresh store over the same folder, no memory of anything
    fresh = runs.Runs(store.root, emit=lambda row: None, clock=lambda: now + 1000)
    view = fresh.resume("demo", DAG, completion={"E": {"status": "done", "sha": "abc1234"}})
    assert view["verified"] == ["A", "E"]
    assert view["redispatch"] == [b] and view["running"] == [c]
    assert view["ready"] == [] and fresh.stuck() == [b]
    # the re-dispatch goes through timeout (partial attached) and is one more attempt on the same record;
    # D becomes ready only once B and C are verified
    with pytest.raises(ValueError, match="started -> dispatched"):
        fresh.transition(b, "dispatched", deadline=now + 9000)
    fresh.transition(b, "timeout", detail="past deadline", path="/work/b-partial.json")
    fresh.transition(b, "dispatched", deadline=now + 9000)
    for rid, node in ((b, "B"), (c, "C")):
        fresh.transition(rid, "result", result=_result(run_id=rid, node=node))
        fresh.transition(rid, "verify_pass", worker="verify")
    view = fresh.resume("demo", DAG, completion={"E": {"status": "done"}})
    assert view["ready"] == ["D"] and view["redispatch"] == [] and fresh.stuck() == []
    assert fresh.load(b)["attempts"]["T1"] == 2


def test_resume_ignores_other_features_and_escalated_nodes(store):
    a = store.create(_brief(run_id="demo-A-1", node="A"))["run_id"]
    store.transition(a, "dispatched")
    store.transition(a, "escalated")
    store.create(_brief(run_id="other-A-1", node="A", feature="other"))
    view = store.resume("demo", DAG)
    assert view["escalated"] == ["A"] and view["ready"] == ["E"] and view["verified"] == []
    other = store.resume("other", {"A": {"inputs": []}})
    assert other["running"] == ["other-A-1"] and other["ready"] == []  # queued inside its deadline is live, not ready


# ---- the index --------------------------------------------------------------------------------------------------------

def test_index_lists_inputs_artifacts_and_the_latest_run_per_node(store):
    a = store.create(_brief(run_id="demo-A-1", node="A"))["run_id"]
    store.transition(a, "dispatched")
    store.transition(a, "result", result=_result(run_id=a, node="A", artifacts=["/work/a.md"]))
    store.transition(a, "verify_pass", worker="verify")
    b1 = store.create(_brief(run_id="demo-B-1", node="B", inputs=["A"]))["run_id"]
    store.transition(b1, "dispatched")
    store.transition(b1, "timeout")
    _tick(store)
    b2 = store.create(_brief(run_id="demo-B-2", node="B", inputs=["A"]))["run_id"]
    idx = json.loads(store.index_path.read_text())["features"]["demo"]
    assert idx["A"]["state"] == "verify_pass" and idx["A"]["sha"] == "0123abcd" and idx["A"]["artifacts"] == ["/work/a.md"]
    assert idx["B"]["run_ids"] == [b1, b2] and idx["B"]["latest"] == b2 and idx["B"]["state"] == "queued"
    assert idx["B"]["inputs"] == [{"node": "A", "result": "/work/A.json"}]
    store.index_path.write_text("broken")
    assert store.index()["features"]["demo"]["B"]["latest"] == b2  # rebuilt from the records


# ---- the event seam ---------------------------------------------------------------------------------------------------

def test_default_emit_appends_validated_events_under_the_data_dir(tmp_path, monkeypatch):
    from companion import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    s = runs.Runs(tmp_path / "team" / "loop", clock=lambda: 1_000_000.0)
    assert s.emit is runs.default_emit
    s.create(_brief())
    rows = [json.loads(line) for line in (tmp_path / "team" / "loop-events.jsonl").read_text().splitlines()]
    assert [r["kind"] for r in rows] == ["queued"] and contract.validate_event(rows[0])
    with pytest.raises(ValueError):
        runs.default_emit({"kind": "nope"})


def test_record_is_written_before_the_event_and_a_failing_emit_keeps_the_record(tmp_path):
    def boom(row):
        raise OSError("event store down")
    s = runs.Runs(tmp_path / "loop", emit=boom, clock=lambda: 1_000_000.0)
    with pytest.raises(OSError):
        s.create(_brief())
    assert s.load("demo-X1-20260101T000000-a1b2c3")["state"] == "queued"


# ---- the inbox replay -------------------------------------------------------------------------------------------------

def test_replay_turns_routed_and_result_rows_into_records(tmp_path):
    t0 = 1_791_900_000
    rows = [{"t": t0, "kind": "routed", "from": "dispatcher-fixback", "node": "E25", "feature": None, "text": "x"},
            {"t": "2027-01-15T08:00:04+00:00", "kind": "result", "from": "claude-lane-1", "node": "REJUVENATE",
             "path": "data/team/x/REJUVENATE.md", "summary": "s"},
            {"t": t0 + 5, "kind": "report", "from": "lane1", "text": "ignored"},
            {"t": t0 + 6, "kind": "routed", "from": "codex-1", "node": "bad node id"}]
    rows.append({"t": t0 + 7, "kind": "routed", "from": "dispatcher", "node": "E25", "text": "routed again"})
    out = runs.replay_inbox(rows, tmp_path / "scratch", now=t0 + 10 * 3600)
    assert out["records"] == 3 and out["skipped"] == 2 and out["states"] == {"dispatched": 2, "result": 1}
    assert out["stuck"] == [f"inbox-E25-{t0}-0", f"inbox-E25-{t0 + 7}-1"]
    assert out["artifacts"] == ["data/team/x/REJUVENATE.md"]
    assert out["events"] == 7  # 3 created, 3 dispatched, 1 result
    assert runs._channel("codex-1") == "codex:1" and runs._channel("claude-lane-1") == "claude:lane-1"
    assert runs._channel("dispatcher fixback!") == "local:dispatcher-fixback-"


def test_requeued_node_is_ready_again_behind_its_escalated_run(store):
    """Requeue vs resume (2026-10-15): after `feature_team.py requeue`, the completion row said queued but resume
    still hid the node behind its escalated latest run, so the autopilot started the next feature instead. A
    queued completion row wins over an escalated run; without it the escalated node stays excluded."""
    a = store.create(_brief(run_id="demo-A-1", node="A"))["run_id"]
    store.transition(a, "dispatched")
    store.transition(a, "escalated")
    hidden = store.resume("demo", DAG, completion={"A": {"status": "dispatched", "by": "Claude Opus"}})
    assert hidden["escalated"] == ["A"] and "A" not in hidden["ready"]
    back = store.resume("demo", DAG, completion={"A": {"status": "queued"}})
    assert back["escalated"] == [] and back["ready"] == ["A", "E"]
