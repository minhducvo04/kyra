"""Acceptance checks for the team chat workflow (store and service), written by Claude before the build.

Contract as agreed in a private design note and a private design note:
TeamStore(path); create_room(title, local_only=False) -> {"id", ...}; submit(room_id, text, request_id,
share_with_team=True) -> {"run_id", ...}; get_room(id) -> {"room", "messages", "runs"}; stop(room_id);
TeamService(store, transport, controller=...).step(run_id), bound by `authorize()` below: a temp controller grant
and a v2 config written before any submit (commit-1 seams pin authority at submit). Home-accepted commit-1
restriction: a run holds the workspace and BOTH native participants until it ends, so no parallel work on the
same pair is claimed. The transport here is a fake, and the tests play the agents by writing the result JSON the service asked for. A result must echo
run_id, stage and attempt and pass the schema; a result file that merely exists is not completion. Nothing
is said on an agent's behalf before its validated result, and a local-only room never reaches a hosted agent.
"""
import contextlib
import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from companion.workflow_control import ACTIONS, WorkflowStore

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

REV, OTHER_REV = "a" * 40, "b" * 40
TASK = "fixture-task"
PLAN = {"summary": "Two small tasks.", "tasks": [
    {"title": "Add field", "instruction": "Add the fixture field."},
    {"title": "Test it", "instruction": "Cover the new fixture field."}]}
BUILD = {"summary": "Built both tasks.", "revision": REV}
PASS = {"summary": "Looks right.", "verdict": "pass", "revision": REV}
CHANGES = {"summary": "One gap remains.", "verdict": "changes_requested", "revision": REV}


class FakeTransport:
    """Records every delivery; `states` scripts the transport outcome of successive sends."""

    def __init__(self, states=()):
        self.sends, self.states = [], list(states)

    def send(self, *, role, run_id, stage, attempt, assignment_path, result_path, binding):
        self.sends.append({"role": role, "run_id": run_id, "stage": stage, "attempt": attempt,
                           "assignment_path": Path(assignment_path), "result_path": Path(result_path),
                           "binding": binding})
        return {"state": self.states.pop(0) if self.states else "queued"}

    def revision(self):
        return REV

    def stages(self):
        return [(send["role"], send["stage"]) for send in self.sends]


def respond(transport, stage, run_id=None, **fields):
    """Play the agent: answer the latest delivery of `stage` at the path the service chose."""
    send = [s for s in transport.sends if s["stage"] == stage and run_id in (None, s["run_id"])][-1]
    body = {"run_id": send["run_id"], "stage": stage, "attempt": send["attempt"], "status": "completed", **fields}
    send["result_path"].parent.mkdir(parents=True, exist_ok=True)
    send["result_path"].write_text(json.dumps(body), encoding="utf-8")
    return send


@pytest.fixture
def team():
    """Imported per test so an unbuilt module fails each test rather than erroring collection."""
    return importlib.import_module("companion.team_chat")


def authorize(root, *, expires_in=86400):
    """Trusted local setup written BEFORE any submit: one temp controller grant for one checkout with exact
    native pins, and the matching Team v2 binding. Returns the controller; Team state lives in root/team."""
    root = Path(root)
    checkout = root / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    pins = {"claude": {"id": "fixture-claude", "cwd": str(checkout)},
            "codex": {"id": "fixture-codex", "cwd": str(checkout)}}
    controller = WorkflowStore(root / "workflow" / "control.json")
    controller.configure({"task_ids": [TASK], "repo": str(checkout), "actions": sorted(ACTIONS),
                          "chats": [{"app": app, **pin} for app, pin in pins.items()],
                          "max_unknown_checks": 2, "max_checks": 50, "interval_seconds": 60,
                          "expires_at": time.time() + expires_in, "max_repair_attempts": 2})
    config = {"version": 2, "workspace": str(checkout), "scope": "Fixture engineering scope.",
              "participants": pins, "controller": {"task_id": TASK, "version": controller.status()["version"]}}
    (root / "team").mkdir(parents=True, exist_ok=True)
    (root / "team" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return controller


def bound(team, root, transport):
    controller = authorize(root)
    store = team.TeamStore(Path(root) / "team", controller=controller)
    return store, team.TeamService(store, transport, controller=controller)


@pytest.fixture
def world(team, tmp_path):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    return store, service, transport, store.create_room("Fixture room")["id"]


def start(store, service, room_id, text="Add a fixture field and test it.", request_id="req-1"):
    run_id = store.submit(room_id, text, request_id)["run_id"]
    service.step(run_id)
    return run_id


def run_status(store, room_id, run_id):
    return next(run for run in store.get_room(room_id)["runs"] if run["id"] == run_id)["status"]


def authors(store, room_id):
    return [message["author"] for message in store.get_room(room_id)["messages"]]


def test_control_plan_build_review_completes_in_order_with_real_agent_messages(world):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    assert transport.stages() == [("claude", "plan")]
    assert not {"claude", "codex"} & set(authors(store, room_id))
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    build = transport.sends[-1]
    assert (build["role"], build["stage"]) == ("codex", "build")
    assert "Add the fixture field." in build["assignment_path"].read_text(encoding="utf-8")
    respond(transport, "build", **BUILD)
    service.step(run_id)
    review = transport.sends[-1]
    assert (review["role"], review["stage"]) == ("claude", "review")
    assert REV in review["assignment_path"].read_text(encoding="utf-8")
    respond(transport, "review", **PASS)
    service.step(run_id)
    assert run_status(store, room_id, run_id) == "completed"
    assert transport.stages() == [("claude", "plan"), ("codex", "build"), ("claude", "review")]
    assert {"claude", "codex"} <= set(authors(store, room_id))


def test_state_survives_a_restart_and_the_run_continues(team, world, tmp_path):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    respond(transport, "plan", **PLAN)
    fresh_controller = WorkflowStore(tmp_path / "workflow" / "control.json")
    fresh_store = team.TeamStore(tmp_path / "team", controller=fresh_controller)
    team.TeamService(fresh_store, transport, controller=fresh_controller).step(run_id)
    assert transport.stages()[-1] == ("codex", "build")
    assert any(m["author"] == "user" and m["text"] == "Add a fixture field and test it."
               for m in fresh_store.get_room(room_id)["messages"])


def test_the_same_request_is_accepted_once_and_a_conflicting_reuse_is_refused(world):
    store, _, _, room_id = world
    first = store.submit(room_id, "Do the fixture thing.", "req-7")
    assert store.submit(room_id, "Do the fixture thing.", "req-7")["run_id"] == first["run_id"]
    assert authors(store, room_id).count("user") == 1
    with pytest.raises(ValueError):
        store.submit(room_id, "Do something else.", "req-7")


def test_a_note_not_shared_with_the_team_starts_nothing(world):
    store, _, transport, room_id = world
    assert store.submit(room_id, "A note for later.", "req-note", share_with_team=False)["run_id"] is None
    assert store.get_room(room_id)["runs"] == [] and transport.sends == []


def test_a_room_runs_one_workflow_at_a_time(world):
    store, service, transport, room_id = world
    start(store, service, room_id)
    with contextlib.suppress(ValueError):
        second = store.submit(room_id, "Another fixture request.", "req-2")
        if second.get("run_id"):
            service.step(second["run_id"])
    assert transport.stages() == [("claude", "plan")]


def test_stop_prevents_later_stages_even_when_the_running_answer_arrives(world):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    store.stop(room_id)
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    assert transport.stages() == [("claude", "plan")]
    assert run_status(store, room_id, run_id) == "stopped"


@pytest.mark.parametrize("defect", ["empty", "not_json", "wrong_stage", "wrong_attempt", "wrong_run", "no_status",
                                    "unhashable_status", "deep_nesting"])
def test_a_result_file_that_merely_exists_is_not_completion(world, defect):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    send = transport.sends[-1]
    body = {"run_id": send["run_id"], "stage": "plan", "attempt": send["attempt"], "status": "completed", **PLAN}
    if defect == "wrong_stage":
        body["stage"] = "review"
    elif defect == "wrong_attempt":
        body["attempt"] = send["attempt"] + 1
    elif defect == "wrong_run":
        body["run_id"] = "another-run"
    elif defect == "no_status":
        del body["status"]
    elif defect == "unhashable_status":
        body["status"] = ["completed"]
    text = {"empty": "{}", "not_json": "{not json",
            "deep_nesting": "[" * 30000 + "]" * 30000}.get(defect, json.dumps(body))
    send["result_path"].parent.mkdir(parents=True, exist_ok=True)
    send["result_path"].write_text(text, encoding="utf-8")
    service.step(run_id)
    assert transport.stages() == [("claude", "plan")]
    assert run_status(store, room_id, run_id) != "completed"


@pytest.mark.parametrize("plan", [
    {"summary": "Too many tasks.", "tasks": [{"title": f"T{i}", "instruction": "Do it."} for i in range(5)]},
    {"summary": "Missing instruction.", "tasks": [{"title": "T1"}]},
    {"summary": "No tasks.", "tasks": []},
])
def test_a_plan_outside_the_schema_never_reaches_the_builder(world, plan):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    respond(transport, "plan", **plan)
    service.step(run_id)
    assert ("codex", "build") not in transport.stages()
    assert run_status(store, room_id, run_id) != "completed"


def test_a_review_of_a_different_revision_is_not_a_pass(world):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    respond(transport, "build", **BUILD)
    service.step(run_id)
    respond(transport, "review", **{**PASS, "revision": OTHER_REV})
    service.step(run_id)
    assert run_status(store, room_id, run_id) != "completed"


def test_at_most_two_repairs_then_the_owner_is_asked(world):
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    for _ in range(3):
        respond(transport, "build", **BUILD)
        service.step(run_id)
        respond(transport, "review", **CHANGES)
        service.step(run_id)
    assert len([send for send in transport.sends if send["stage"] == "build"]) == 3
    assert run_status(store, room_id, run_id) == "needs_input"


def test_an_uncertain_delivery_is_never_resent(team, tmp_path):
    transport = FakeTransport(states=["ambiguous"])
    store, service = bound(team, tmp_path, transport)
    room_id = store.create_room("Fixture room")["id"]
    run_id = start(store, service, room_id)
    service.step(run_id)
    service.step(run_id)
    assert transport.stages() == [("claude", "plan")]
    assert run_status(store, room_id, run_id) == "uncertain"


def test_a_local_only_room_never_sends_work_to_hosted_agents(team, tmp_path):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room_id = store.create_room("Private fixture room", local_only=True)["id"]
    with contextlib.suppress(ValueError):
        run_id = store.submit(room_id, "Summarize the fixture notes.", "req-p")["run_id"]
        if run_id:
            service.step(run_id)
    assert transport.sends == []


def sent(transport, run_id):
    return [(send["role"], send["stage"]) for send in transport.sends if send["run_id"] == run_id]


def test_a_second_room_waits_for_the_whole_first_run_on_the_same_pair(team, tmp_path):
    """Home-accepted commit-1 restriction: a run holds the workspace and BOTH native participants until it
    ends, so another room on the same pair sends nothing, even between the first run's stages."""
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room_a, room_b = store.create_room("Room A")["id"], store.create_room("Room B")["id"]
    run_a = start(store, service, room_a, request_id="req-a")
    run_b = start(store, service, room_b, request_id="req-b")
    for stage, fields in (("plan", PLAN), ("build", BUILD), ("review", PASS)):
        assert sent(transport, run_b) == []  # room A still holds the pair
        respond(transport, stage, run_id=run_a, **fields)
        service.step(run_a)
        service.step(run_b)
    assert run_status(store, room_a, run_a) == "completed"
    assert sent(transport, run_a) == [("claude", "plan"), ("codex", "build"), ("claude", "review")]
    assert sent(transport, run_b) == [("claude", "plan")]  # the queued room starts once room A ends


def test_an_assignment_carries_only_its_own_rooms_text(team, tmp_path):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room_a, room_b = store.create_room("Room A")["id"], store.create_room("Room B")["id"]
    run_a = start(store, service, room_a, text="Room A fixture marker zebra-4471.", request_id="req-a")
    run_b = start(store, service, room_b, text="Room B fixture marker okapi-9032.", request_id="req-b")
    for stage, fields in (("plan", PLAN), ("build", BUILD), ("review", PASS)):
        respond(transport, stage, run_id=run_a, **fields)
        service.step(run_a)
    service.step(run_b)
    build_a = next(send for send in transport.sends if send["run_id"] == run_a and send["stage"] == "build")
    plan_b = next(send for send in transport.sends if send["run_id"] == run_b and send["stage"] == "plan")
    text_a = build_a["assignment_path"].read_text(encoding="utf-8")
    text_b = plan_b["assignment_path"].read_text(encoding="utf-8")
    assert "okapi-9032" not in text_a
    assert "okapi-9032" in text_b and "zebra-4471" not in text_b


def test_an_uncertain_build_delivery_keeps_the_workspace_writer_lease(team, tmp_path):
    """Contract point 6: an uncertain delivery keeps its claims until reconciliation, so no other room's build
    can overlap a builder that may still be working."""
    transport = FakeTransport(states=["queued", "ambiguous"])
    store, service = bound(team, tmp_path, transport)
    room_a, room_b = store.create_room("Room A")["id"], store.create_room("Room B")["id"]
    run_a = start(store, service, room_a, request_id="req-a")
    respond(transport, "plan", run_id=run_a, **PLAN)
    service.step(run_a)
    run_b = start(store, service, room_b, request_id="req-b")
    if sent(transport, run_b):
        respond(transport, "plan", run_id=run_b, **PLAN)
    for _ in range(2):
        service.step(run_a)
        service.step(run_b)
    assert [send["run_id"] for send in transport.sends if send["stage"] == "build"] == [run_a]


@pytest.mark.parametrize("text", ["", "   ", "x" * 1_000_000])
def test_empty_or_oversized_text_is_refused(world, text):
    store, _, transport, room_id = world
    with pytest.raises(ValueError):
        store.submit(room_id, text, "req-bad")
    assert transport.sends == []


def test_a_corrupt_store_fails_loudly_instead_of_resetting(team, tmp_path):
    """Agreed contract: TeamStore(root) keeps its state in the JSON file exposed as `store.path`."""
    root = tmp_path / "team"
    store = team.TeamStore(root)
    store.create_room("Fixture room")
    Path(store.path).write_text("{not json", encoding="utf-8")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        team.TeamStore(root).list_rooms()


def test_an_unhashable_review_verdict_neither_passes_nor_crashes_the_step(world):
    """Found in review: validate_result raised TypeError on a list verdict, which step() did not catch."""
    store, service, transport, room_id = world
    run_id = start(store, service, room_id)
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    respond(transport, "build", **BUILD)
    service.step(run_id)
    respond(transport, "review", **{**PASS, "verdict": ["pass"]})
    service.step(run_id)
    assert run_status(store, room_id, run_id) != "completed"
    assert transport.stages() == [("claude", "plan"), ("codex", "build"), ("claude", "review")]


def test_a_finished_reply_that_breaks_the_schema_ends_the_run_and_frees_the_planner(team, tmp_path):
    """Found in review: a complete, correctly addressed plan with five tasks kept the run waiting and held
    Claude's lease for every room until the deadline made it uncertain. A parseable reply is final."""
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room_a, room_b = store.create_room("Room A")["id"], store.create_room("Room B")["id"]
    run_a = start(store, service, room_a, request_id="req-a")
    run_b = start(store, service, room_b, request_id="req-b")
    too_many = [{"title": f"T{i}", "instruction": "Do it."} for i in range(5)]
    respond(transport, "plan", run_id=run_a, summary="Five tasks.", tasks=too_many)
    service.step(run_a)
    assert run_status(store, room_a, run_a) == "needs_input"
    service.step(run_b)
    assert sent(transport, run_b) == [("claude", "plan")]


def test_two_ordinary_length_stages_are_not_cut_off_by_one_whole_run_deadline(world, monkeypatch):
    """Found in review: expires_at was fixed at submit + 30 minutes for the whole run, queue time included.
    A 16-minute plan then a 16-minute build are ordinary; the build must be consumed and the review sent."""
    store, service, transport, room_id = world
    clock, reads = [time.time()], []

    def now():
        reads.append(clock[0])
        return clock[0]

    monkeypatch.setattr(time, "time", now)
    run_id = start(store, service, room_id)
    clock[0] += 16 * 60
    respond(transport, "plan", **PLAN)
    service.step(run_id)
    clock[0] += 16 * 60
    respond(transport, "build", **BUILD)
    service.step(run_id)
    assert reads  # the service reads time.time(); if it moves to another clock, move this probe with it
    assert transport.stages() == [("claude", "plan"), ("codex", "build"), ("claude", "review")]


def test_the_fingerprint_command_given_to_agents_runs_as_written(tmp_path):
    """Found in review: every assignment tells agents to run `python3 scripts/team_chat.py revision ...`. With a
    python3 that lacks the app's packages, the script failed importing settings (pydantic), so no build or review
    could report the revision. The command handed to agents must run as written on the Mac that sends it."""
    import shlex
    import subprocess

    from companion import team_transport

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "fixture@example.invalid"], ["config", "user.name", "Fixture"]):
        subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True)
    (workspace / "code.txt").write_text("fixture", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "Fixture"], check=True, capture_output=True)
    root = tmp_path / "team"
    root.mkdir()
    binding = {"id": "fixture-chat", "cwd": str(workspace)}
    config = {"version": 1, "workspace": str(workspace), "scope": "Fixture scope.",
              "participants": {"claude": binding, "codex": binding}}
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    context = team_transport.VisibleTeamTransport(root).context()
    command = context.split("get the exact workspace fingerprint with: ", 1)[1].splitlines()[0]
    done = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stdout.strip() == team_transport.workspace_revision(workspace)


def test_a_finished_build_that_cannot_be_fingerprinted_frees_the_workspace(team, tmp_path):
    """Found in review: when the workspace fingerprint could not be computed while consuming a finished build,
    step() raised and the worker marked the run uncertain, keeping the writer lease for every room although the
    builder had already replied. A finished reply that cannot be verified ends as needs_input and frees it."""
    class Unverifiable(FakeTransport):
        def revision(self):
            raise ValueError("fixture: fingerprint unavailable")

    transport = Unverifiable()
    store, service = bound(team, tmp_path, transport)
    room_a, room_b = store.create_room("Room A")["id"], store.create_room("Room B")["id"]
    run_a = start(store, service, room_a, request_id="req-a")
    respond(transport, "plan", run_id=run_a, **PLAN)
    service.step(run_a)
    respond(transport, "build", run_id=run_a, **BUILD)
    service.step(run_a)
    assert run_status(store, room_a, run_a) == "needs_input"
    run_b = start(store, service, room_b, request_id="req-b")
    respond(transport, "plan", run_id=run_b, **PLAN)
    service.step(run_b)
    assert sent(transport, run_b) == [("claude", "plan"), ("codex", "build")]
