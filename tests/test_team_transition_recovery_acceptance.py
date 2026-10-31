"""Claude acceptance for Finding 1 of the commit-1 review (2026-09-28): stage-transition crash recovery.

A stage-advance commit replaces the run's shared claim (same id, next stage), then the Team save fails. The run keeps
its older claim, fail_run marks it uncertain, and the plain reconcile refuses the mismatch, so the namespace stays
blocked. Written before the fix against the Home-accepted contract in a private design note:
TeamService.reconcile(run_id, *, worker_ended, expected_claim=None). One explicit recovery path: the plain reconcile
never adopts a replacement claim. Also Finding 2: a queued run that was never sent expires without pointing at a
native chat. Temp stores and fake transports only.
"""
import importlib
import time
from pathlib import Path

import pytest

from companion.workflow_control import WorkflowStore
from tests.test_team_chat_acceptance import BUILD, CHANGES, PLAN, FakeTransport, authorize, respond, start

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

TRANSITIONS = {
    "plan_to_build": [("plan", PLAN)],
    "build_to_review": [("plan", PLAN), ("build", BUILD)],
    "review_to_repair": [("plan", PLAN), ("build", BUILD), ("review", CHANGES)],
}


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


def run_row(store, room, run):
    return next(item for item in store.get_room(room)["runs"] if item["id"] == run)


def run_claims(controller, run):
    return [claim for claim in controller.claims() if claim["run_id"] == run]


def crash_after(team, root, transition, monkeypatch):
    """Drive the real stage loop, then fail only the Team save that records the next stage's reservation."""
    controller = authorize(root)
    transport = FakeTransport()
    store = team.TeamStore(Path(root) / "team", controller=controller)
    service = team.TeamService(store, transport, controller=controller)
    room = store.create_room("Room")["id"]
    run = start(store, service, room)
    replies = TRANSITIONS[transition]
    for stage, fields in replies[:-1]:
        respond(transport, stage, run_id=run, **fields)
        service.step(run)
    stage, fields = replies[-1]
    respond(transport, stage, run_id=run, **fields)
    (old,) = run_claims(controller, run)
    real = team.write_json

    def failing(path, value, **kwargs):
        if Path(path).name == "state.json" and any(item["phase"] == "dispatching" for entry in value["rooms"].values()
                                                   for item in entry["runs"]):
            raise OSError("fixture: the Team save for the next reservation failed")
        return real(path, value, **kwargs)

    monkeypatch.setattr(team, "write_json", failing)
    with pytest.raises(OSError):
        service.step(run)
    monkeypatch.setattr(team, "write_json", real)
    (current,) = run_claims(controller, run)
    return store, service, controller, transport, room, run, old, current


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_control_the_failed_reservation_sends_nothing_and_plain_reconcile_still_refuses(team, tmp_path, monkeypatch,
                                                                                        transition):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, transition, monkeypatch)
    assert len(transport.sends) == len(TRANSITIONS[transition])  # every earlier stage; never the failed one
    assert current["id"] == old["id"] and (current["stage"], current["attempt"]) != (old["stage"], old["attempt"])
    assert run_row(store, room, run)["status"] == "uncertain"
    with pytest.raises(PermissionError):
        service.reconcile(run, worker_ended=True)
    assert run_claims(controller, run) == [current]


@pytest.mark.parametrize("transition", sorted(TRANSITIONS))
def test_selecting_the_exact_current_claim_ends_the_run_and_frees_the_namespace(team, tmp_path, monkeypatch,
                                                                                transition):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, transition, monkeypatch)
    result = service.reconcile(run, worker_ended=True, expected_claim=current)
    assert result["status"] == "stopped" and result["retry_sent"] is False
    row = run_row(store, room, run)
    assert row["status"] == "stopped" and row["claim"] == current
    assert run_claims(controller, run) == []
    assert len(transport.sends) == len(TRANSITIONS[transition])  # nothing resent
    second = start(store, service, store.create_room("Other")["id"], request_id="req-2")
    assert transport.sends[-1]["run_id"] == second  # the namespace is usable again


@pytest.mark.parametrize("change", ["revoked", "reconfigured"])
def test_recovery_needs_no_running_grant(team, tmp_path, monkeypatch, change):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    if change == "revoked":
        controller.revoke()
    else:
        controller.configure(controller.status()["envelope"])
    service.reconcile(run, worker_ended=True, expected_claim=current)
    assert run_row(store, room, run)["status"] == "stopped" and run_claims(controller, run) == []


def _tampered(current, old):
    other_pin = {**current["participants"]["codex"], "id": "another-codex-chat"}
    return {
        "previous_stage_claim": old,
        "other_stage": {**current, "stage": "review"},
        "other_attempt": {**current, "attempt": current["attempt"] + 1},
        "other_version": {**current, "version": current["version"] + 1},
        "other_workspace": {**current, "workspace_id": [current["workspace_id"][0], current["workspace_id"][1] + 1]},
        "other_participant": {**current, "participants": {**current["participants"], "codex": other_pin}},
        "other_run": {**current, "run_id": "another-run"},
        "other_task": {**current, "task_id": "another-task"},
        "other_owner": {**current, "owner": "pr"},
    }


@pytest.mark.parametrize("variant", ["previous_stage_claim", "other_stage", "other_attempt", "other_version",
                                     "other_workspace", "other_participant", "other_run", "other_task", "other_owner",
                                     "worker_not_asserted"])
def test_only_the_exact_asserted_current_claim_can_be_selected(team, tmp_path, monkeypatch, variant):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    sends = len(transport.sends)
    with pytest.raises((PermissionError, ValueError)):
        if variant == "worker_not_asserted":
            service.reconcile(run, worker_ended=False, expected_claim=current)
        else:
            service.reconcile(run, worker_ended=True, expected_claim=_tampered(current, old)[variant])
    assert run_claims(controller, run) == [current] and run_row(store, room, run)["status"] == "uncertain"
    assert len(transport.sends) == sends


def test_a_claim_that_is_not_the_persisted_next_transition_is_refused(team, tmp_path, monkeypatch):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    # A skipped stage made through the public seam; commit() alone does not know the Team transition table.
    with controller.reserve_dispatch(task_id=current["task_id"], version=current["version"], repo=current["repo"],
                                     owner="team", run_id=run, stage="review", attempt=0,
                                     participants={"claude": run_row(store, room, run)["submitted_config"]["participants"]["claude"]},
                                     previous_claim=current) as reservation:
        skipped = reservation.commit()
    with pytest.raises((PermissionError, ValueError)):
        service.reconcile(run, worker_ended=True, expected_claim=skipped)
    assert run_claims(controller, run) == [skipped] and run_row(store, room, run)["status"] == "uncertain"


def test_recovery_retries_after_a_failed_terminal_save_and_after_a_failed_release(team, tmp_path, monkeypatch):
    store, service, controller, transport, room, run, old, current = crash_after(team, tmp_path, "plan_to_build",
                                                                                 monkeypatch)
    real_write = team.write_json

    def no_write(path, value, **kwargs):
        raise OSError("fixture: terminal save failed")

    monkeypatch.setattr(team, "write_json", no_write)
    with pytest.raises(OSError):
        service.reconcile(run, worker_ended=True, expected_claim=current)
    monkeypatch.setattr(team, "write_json", real_write)
    assert run_claims(controller, run) == [current] and run_row(store, room, run)["status"] == "uncertain"
    real_release, calls = WorkflowStore.release_claim, []

    def flaky_release(self, claim, *, outcome):
        calls.append(outcome)
        if len(calls) == 1:
            raise OSError("fixture: release write failed")
        return real_release(self, claim, outcome=outcome)

    monkeypatch.setattr(WorkflowStore, "release_claim", flaky_release)
    with pytest.raises(OSError):
        service.reconcile(run, worker_ended=True, expected_claim=current)
    row = run_row(store, room, run)
    assert row["status"] == "stopped" and row["claim"] == current and run_claims(controller, run) == [current]
    service.reconcile(run, worker_ended=True)  # the existing exact release-retry branch finishes it
    assert run_claims(controller, run) == [] and len(transport.sends) == 1


def test_a_queued_run_that_was_never_sent_expires_without_pointing_at_a_native_chat(team, tmp_path, monkeypatch):
    """Finding 2: nothing reached a native chat, so the reason must say the time ran out before dispatch."""
    controller = authorize(tmp_path)
    transport = FakeTransport()
    store = team.TeamStore(tmp_path / "team", controller=controller)
    service = team.TeamService(store, transport, controller=controller)
    first, second = store.create_room("First")["id"], store.create_room("Second")["id"]
    start(store, service, first, request_id="req-a")
    queued = start(store, service, second, request_id="req-b")
    later = time.time() + 31 * 60  # the first run still holds the pair; the queued run's window has ended
    monkeypatch.setattr(time, "time", lambda: later)
    service.step(queued)
    row = run_row(store, second, queued)
    assert row["status"] == "needs_input" and not any(send["run_id"] == queued for send in transport.sends)
    assert "native chat" not in row["reason"] and "before dispatch" in row["reason"]
