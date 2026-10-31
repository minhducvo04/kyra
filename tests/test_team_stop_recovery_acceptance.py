"""Claude acceptance for Finding 3 of the commit-1 re-review (2026-09-28): Stop-first and timeout-first recovery.

A process death between the durable shared-claim commit and the Team save leaves an ACTIVE row in phase 'pending':
no row claim on the first dispatch, or the old row claim with the next stage/attempt already persisted on a later
one. On freeze 776def3f, Stop or the pending timeout then records a known-ended terminal state ("before another
stage was sent" / "before dispatch"), skips or fails the stale release, and no supported command can clear the
claim. Written before the fix against Home's contract (a private design note with its 03:05 timeout
scope): one pure claim classification under controller -> Team locks in both TeamService.stop and step(). A shared
Team claim for the run that is not exactly run.claim makes the pending run uncertain (Stop also sets
stop_requested); nothing is released, adopted or resent, and the existing explicit recovery then frees the
namespace. Never-reserved, known-ended and in-flight runs keep today's results.

Simulated death raises a BaseException from the Team save, which no handler in _dispatch catches, so fail_run never
runs; the spawned cases kill a real child process with os._exit at the same point. Temp stores and fake transports.
"""
import importlib
import json
import multiprocessing
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from companion.workflow_control import WorkflowStore
from tests.test_team_chat_acceptance import BUILD, CHANGES, PLAN, FakeTransport, authorize, respond, start

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

TERMINAL = {"completed", "needs_input", "stopped", "uncertain"}
CRASHES = {
    "first_claim": [],
    "plan_to_build": [("plan", PLAN)],
    "build_to_review": [("plan", PLAN), ("build", BUILD)],
    "review_to_repair": [("plan", PLAN), ("build", BUILD), ("review", CHANGES)],
}
PENDING = {"plan_done": CRASHES["plan_to_build"], "build_done": CRASHES["build_to_review"],
           "review_changes": CRASHES["review_to_repair"]}


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


class Died(BaseException):
    """Stands in for process death: _dispatch catches only PermissionError, ValueError and OSError."""


def row(store, room, run):
    return next(item for item in store.get_room(room)["runs"] if item["id"] == run)


def held(controller, run):
    return [claim for claim in controller.claims() if claim["run_id"] == run]


def dispatching(value):
    return any(item["phase"] == "dispatching" for entry in value["rooms"].values() for item in entry["runs"])


def world(team, root):
    controller = authorize(root)
    transport = FakeTransport()
    store = team.TeamStore(Path(root) / "team", controller=controller)
    return store, team.TeamService(store, transport, controller=controller), controller, transport


def advance(service, transport, run, replies):
    """Send the plan, then answer every stage in `replies`, consuming all but the last; the next step is fatal."""
    for index, (stage, fields) in enumerate(replies):
        if index == 0:
            service.step(run)
        respond(transport, stage, run_id=run, **fields)
        if index < len(replies) - 1:
            service.step(run)


def restart(team, root):
    """Fresh objects over the same files, as after a restart; the new transport counts any resend."""
    controller = WorkflowStore(Path(root) / "workflow" / "control.json")
    store = team.TeamStore(Path(root) / "team", controller=controller)
    transport = FakeTransport()
    return store, team.TeamService(store, transport, controller=controller), controller, transport


def died_in_window(team, root, crash, monkeypatch):
    store, service, controller, transport = world(team, root)
    room = store.create_room("Room")["id"]
    run = store.submit(room, "Add a fixture field and test it.", "req-1")["run_id"]
    advance(service, transport, run, CRASHES[crash])
    real = team.write_json

    def dying(path, value, **kwargs):
        if Path(path).name == "state.json" and dispatching(value):
            raise Died()
        return real(path, value, **kwargs)

    monkeypatch.setattr(team, "write_json", dying)
    with pytest.raises(Died):
        service.step(run)
    monkeypatch.setattr(team, "write_json", real)
    return (*restart(team, root), room, run)


def act(action, store, service, room, run, monkeypatch):
    """Stop first, or the first step after a restart past the row's deadline."""
    if action == "stop":
        service.stop(room)
    else:
        later = row(store, room, run)["expires_at"] + 1
        monkeypatch.setattr(time, "time", lambda: later)
        service.step(run)


def recover_and_reuse(store, service, controller, transport, room, run, current, *, replacement):
    if replacement:
        with pytest.raises(PermissionError):
            service.reconcile(run, worker_ended=True)  # no silent adoption of a replaced claim
        assert held(controller, run) == [current] and transport.sends == []
    assert service.reconcile(run, worker_ended=True, expected_claim=current) == {
        "run_id": run, "status": "stopped", "retry_sent": False}
    final = row(store, room, run)
    assert final["status"] == "stopped" and final["claim"] == current
    assert held(controller, run) == [] and transport.sends == []
    second = start(store, service, store.create_room("Other")["id"], request_id="req-2")
    assert [send["run_id"] for send in transport.sends] == [second]  # the namespace is usable again


@pytest.mark.parametrize("crash", sorted(CRASHES))
@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_a_pending_orphan_becomes_uncertain_and_only_explicit_recovery_frees_it(team, tmp_path, monkeypatch,
                                                                               action, crash):
    store, service, controller, transport, room, run = died_in_window(team, tmp_path, crash, monkeypatch)
    left = row(store, room, run)
    (current,) = held(controller, run)
    assert left["phase"] == "pending" and left["status"] not in TERMINAL and left.get("claim") != current
    act(action, store, service, room, run, monkeypatch)  # returns normally: classification is not an error
    after = row(store, room, run)
    assert after["status"] == "uncertain"
    assert "before another stage was sent" not in after["reason"] and "before dispatch" not in after["reason"]
    assert after.get("claim") == left.get("claim")  # nothing adopted while classifying
    if action == "stop":
        assert after["stop_requested"] is True
    assert held(controller, run) == [current] and transport.sends == []
    recover_and_reuse(store, service, controller, transport, room, run, current, replacement=crash != "first_claim")


@pytest.mark.parametrize("crash", sorted(CRASHES))
def test_control_a_step_before_any_stop_or_deadline_still_ends_uncertain_and_recoverable(team, tmp_path, monkeypatch,
                                                                                          crash):
    store, service, controller, transport, room, run = died_in_window(team, tmp_path, crash, monkeypatch)
    (current,) = held(controller, run)
    try:
        service.step(run)
    except (PermissionError, ValueError):
        pass  # today's route: _dispatch meets the orphan, fail_run records uncertainty, then it re-raises
    assert row(store, room, run)["status"] == "uncertain"
    assert held(controller, run) == [current] and transport.sends == []
    recover_and_reuse(store, service, controller, transport, room, run, current, replacement=crash != "first_claim")


def _die_in_window_child(root, crash):
    """Spawned child: reach the window, then die for real at the Team save; no handler, finally or exit runs."""
    team = importlib.import_module("companion.team_chat")
    # Spawned process does not inherit pytest's fake-workspace fixture.
    team._task_workspace = lambda run_id, binding: Path(binding['workspace'])
    team.team_handbook = lambda: "Fixture handbook for crash-window isolation."
    store, service, controller, transport = world(team, root)
    room = store.create_room("Room")["id"]
    run = store.submit(room, "Add a fixture field and test it.", "req-1")["run_id"]
    advance(service, transport, run, CRASHES[crash])
    (Path(root) / "ids.json").write_text(json.dumps({"room": room, "run": run}))
    real = team.write_json

    def dying(path, value, **kwargs):
        if Path(path).name == "state.json" and dispatching(value):
            os._exit(0)
        return real(path, value, **kwargs)

    team.write_json = dying
    service.step(run)
    os._exit(3)  # the window was never reached


@pytest.mark.parametrize("crash", ["first_claim", "plan_to_build"])
@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_an_actual_process_death_in_the_window_is_classified_and_recoverable(team, tmp_path, monkeypatch, action,
                                                                             crash):
    child = multiprocessing.get_context("spawn").Process(target=_die_in_window_child, args=(str(tmp_path), crash))
    child.start()
    child.join(timeout=60)
    try:
        assert child.exitcode == 0  # died inside the window, not before or after it
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    ids = json.loads((tmp_path / "ids.json").read_text())
    room, run = ids["room"], ids["run"]
    store, service, controller, transport = restart(team, tmp_path)
    (current,) = held(controller, run)
    assert row(store, room, run)["phase"] == "pending"
    act(action, store, service, room, run, monkeypatch)
    after = row(store, room, run)
    assert after["status"] == "uncertain" and held(controller, run) == [current] and transport.sends == []
    recover_and_reuse(store, service, controller, transport, room, run, current, replacement=crash != "first_claim")


@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_a_lost_team_save_after_a_real_send_is_never_reported_as_unsent(team, tmp_path, monkeypatch, action):
    """Without fsync, power loss can drop the Team writes that preceded a send. The row then looks pending again
    although the next stage reached its native chat, so only uncertainty is truthful and nothing may be released."""
    store, service, controller, transport = world(team, tmp_path)
    room = store.create_room("Room")["id"]
    run = start(store, service, room)
    respond(transport, "plan", run_id=run, **PLAN)
    real, saved = team.write_json, []

    def recording(path, value, **kwargs):
        result = real(path, value, **kwargs)
        if Path(path).name == "state.json":
            saved.append(Path(path).read_bytes())
        return result

    monkeypatch.setattr(team, "write_json", recording)
    service.step(run)  # consumes the plan, reserves the build and sends it
    monkeypatch.setattr(team, "write_json", real)
    assert [send["stage"] for send in transport.sends] == ["plan", "build"]
    store.path.write_bytes(saved[0])  # only the reply-ingestion save survived
    store, service, controller, fresh = restart(team, tmp_path)
    left = row(store, room, run)
    (current,) = held(controller, run)
    assert left["phase"] == "pending" and left["stage"] == "build" and left["claim"] != current
    act(action, store, service, room, run, monkeypatch)
    after = row(store, room, run)
    assert after["status"] == "uncertain" and held(controller, run) == [current] and fresh.sends == []
    assert "before another stage was sent" not in after["reason"] and "before dispatch" not in after["reason"]
    recover_and_reuse(store, service, controller, fresh, room, run, current, replacement=True)


def audit_locks(team, monkeypatch):
    """Record each Team save with whether the controller lock was held when its Team edit began, and at the save."""
    depth, entries, saves = [0], [], []
    real_locked, real_edit, real_write = WorkflowStore._locked, team.TeamStore.edit, team.write_json

    @contextmanager
    def locked(self):
        with real_locked(self):
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    @contextmanager
    def edit(self):
        entries.append(depth[0] > 0)
        try:
            with real_edit(self) as data:
                yield data
        finally:
            entries.pop()

    def write(path, value, **kwargs):
        if Path(path).name == "state.json":
            statuses = {item["id"]: item["status"] for entry in value["rooms"].values() for item in entry["runs"]}
            saves.append((statuses, bool(entries and entries[-1]), depth[0] > 0))
        return real_write(path, value, **kwargs)

    monkeypatch.setattr(WorkflowStore, "_locked", locked)
    monkeypatch.setattr(team.TeamStore, "edit", edit)
    monkeypatch.setattr(team, "write_json", write)
    return saves


@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_the_pending_classification_is_saved_under_the_controller_lock(team, tmp_path, monkeypatch, action):
    """Controller -> Team order: the Team edit that ends a pending run begins under the controller lock, so it cannot
    interleave with a live reservation between that reservation's claim commit and its Team save."""
    store, service, controller, transport, room, run = died_in_window(team, tmp_path, "first_claim", monkeypatch)
    saves = audit_locks(team, monkeypatch)
    act(action, store, service, room, run, monkeypatch)
    ending = [(entered, saving) for statuses, entered, saving in saves if statuses.get(run) in TERMINAL]
    assert ending and ending[0] == (True, True)


@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_control_a_queued_run_that_never_reserved_keeps_its_known_ended_result(team, tmp_path, monkeypatch, action):
    store, service, controller, transport = world(team, tmp_path)
    first, second = store.create_room("First")["id"], store.create_room("Second")["id"]
    holder = start(store, service, first, request_id="req-a")
    queued = start(store, service, second, request_id="req-b")  # waits: the holder has the whole pair
    before = controller.claims()
    assert row(store, second, queued)["status"] == "queued" and held(controller, queued) == []
    act(action, store, service, second, queued, monkeypatch)
    after = row(store, second, queued)
    if action == "stop":
        assert after["status"] == "stopped" and "before another stage was sent" in after["reason"]
    else:
        assert after["status"] == "needs_input" and "before dispatch" in after["reason"]
    assert controller.claims() == before and [send["run_id"] for send in transport.sends] == [holder]


@pytest.mark.parametrize("state", sorted(PENDING))
@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_control_a_known_ended_predecessor_still_ends_and_releases_its_exact_claim(team, tmp_path, monkeypatch,
                                                                                    action, state):
    store, service, controller, transport = world(team, tmp_path)
    room = store.create_room("Room")["id"]
    run = store.submit(room, "Add a fixture field and test it.", "req-1")["run_id"]
    advance(service, transport, run, PENDING[state])
    real_dispatch = team.TeamService._dispatch
    monkeypatch.setattr(team.TeamService, "_dispatch", lambda self, run_id: None)
    service.step(run)  # consumes the last reply; no next stage is reserved
    monkeypatch.setattr(team.TeamService, "_dispatch", real_dispatch)
    left = row(store, room, run)
    (claim,) = held(controller, run)
    assert left["phase"] == "pending" and left["status"] not in TERMINAL and left["claim"] == claim
    sends = len(transport.sends)
    act(action, store, service, room, run, monkeypatch)
    after = row(store, room, run)
    if action == "stop":
        assert after["status"] == "stopped" and "before another stage was sent" in after["reason"]
    else:
        assert after["status"] == "needs_input" and "before dispatch" in after["reason"]
    assert held(controller, run) == [] and len(transport.sends) == sends  # the exact old claim, released; no send


@pytest.mark.parametrize("action", ["stop", "timeout"])
def test_control_an_in_flight_stage_keeps_its_current_handling(team, tmp_path, monkeypatch, action):
    store, service, controller, transport = world(team, tmp_path)
    room = store.create_room("Room")["id"]
    run = start(store, service, room)  # the plan is in flight and its claim is held
    (claim,) = held(controller, run)
    act(action, store, service, room, run, monkeypatch)
    after = row(store, room, run)
    if action == "stop":
        assert after["status"] == "stopping" and held(controller, run) == [claim]
        respond(transport, "plan", run_id=run, **PLAN)
        service.step(run)
        assert row(store, room, run)["status"] == "stopped" and held(controller, run) == []
    else:
        assert after["status"] == "uncertain" and "native chat" in after["reason"] and held(controller, run) == [claim]
    assert [send["stage"] for send in transport.sends] == ["plan"]
