"""Claude acceptance for the read-only Team status projection (2026-09-28), written before the build.

Contract: a private design note with Home's five acceptance decisions. TeamStore.status_snapshot(*, now=None)
reads saved Team state, the store's own controller record and an already-existing claims registry, without locks,
file creation, discovery, transport or Team writes. It returns read_at; team_saved_at, controller_saved_at and
claims_saved_at (finite or None); notices [{code, text}] with codes team_unavailable, team_legacy, team_capacity,
controller_unavailable, controller_namespace and claims_unavailable, one per distinct issue; and runs [{key, room_id,
run_id, title, status, stage, reason_label, controller_label, claim_label, attention}] for active, needs_input,
uncertain and terminal-held runs. Terminal-held needs a matching shared claim or a retained local lease; a remembered
run.claim alone is history. Label wording is the builder's fixed choice, so these tests pin codes and properties,
never wording. Only the owner room title may appear as private text. Temporary stores and fake transports only.
"""
import importlib
import json
import math
import re
import time

import pytest

from companion import team_transport, workflow_control
from companion.workflow_control import WorkflowStore
from tests.test_team_capacity_acceptance import CEILING, as_version_1, attach, pad, queued
from tests.test_team_chat_acceptance import BUILD, PASS, PLAN, FakeTransport, authorize, respond

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

TITLE = "Owner title Tiêu đề 所有者 🚀"
CODES = {"team_unavailable", "team_legacy", "team_capacity", "controller_unavailable", "controller_namespace",
         "claims_unavailable"}
FIELDS = {"key", "room_id", "run_id", "title", "status", "stage", "reason_label", "controller_label", "claim_label",
          "attention"}


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


@pytest.fixture
def data(team, tmp_path, monkeypatch):
    """Production default paths (the web app's wiring) under this test's DATA_DIR."""
    data = tmp_path / "data"
    monkeypatch.setattr(workflow_control, "DATA_DIR", data)
    monkeypatch.setattr(team, "DATA_DIR", data)
    return data


@pytest.fixture
def home(team, data):
    authorize(data)
    return attach(team, data)


def listing(root):
    if not root.exists():
        return None
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*")}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def snapshot(team, data, monkeypatch, now=None):
    """One snapshot through a fresh store, proving it neither locks, creates, discovers, reserves nor writes."""
    store, before, calls = team.TeamStore(), listing(data), []

    def forbid(name):
        def refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"status_snapshot must not call {name}")
        return refuse

    with monkeypatch.context() as patch:
        for method in ("_locked", "_claims", "claims", "status", "reserve_dispatch", "release_claim", "configure"):
            patch.setattr(WorkflowStore, method, forbid(f"WorkflowStore.{method}"))
        patch.setattr(team.TeamStore, "edit", forbid("TeamStore.edit"))
        patch.setattr(team.TeamService, "capacity_status", forbid("TeamService.capacity_status"))
        patch.setattr(team_transport, "discover", forbid("discover"))
        value = store.status_snapshot() if now is None else store.status_snapshot(now=now)
    assert calls == [] and listing(data) == before
    assert set(value) == {"read_at", "team_saved_at", "controller_saved_at", "claims_saved_at", "notices", "runs"}
    assert finite(value["read_at"])
    assert all(value[key] is None or finite(value[key]) for key in ("team_saved_at", "controller_saved_at",
                                                                    "claims_saved_at"))
    codes = [notice["code"] for notice in value["notices"]]
    assert all(set(notice) == {"code", "text"} and notice["text"] for notice in value["notices"])
    assert set(codes) <= CODES and len(codes) == len(set(codes))
    for row in value["runs"]:
        assert set(row) == FIELDS and row["key"] == f"team:{row['run_id']}" and type(row["attention"]) is bool
        assert re.fullmatch("[a-f0-9]{32}", row["run_id"]) and re.fullmatch("[a-f0-9]{32}", row["room_id"])
        assert all(isinstance(row[key], str) for key in ("reason_label", "controller_label", "claim_label"))
    return value


def codes(value):
    return sorted(notice["code"] for notice in value["notices"])


def listed(value):
    return {row["run_id"]: row for row in value["runs"]}


def finish(home, run, replies):
    for stage, fields in replies:
        respond(home.transport, stage, run_id=run, **fields)
        home.service.step(run)


def held_by_failed_release(home, monkeypatch, title=TITLE):
    """A stopped run whose exact shared claim is still held because its release write failed."""
    room, run = queued(home, title, "req-held")
    home.service.step(run)
    home.service.stop(room)
    respond(home.transport, "plan", run_id=run, **PLAN)
    real = WorkflowStore.release_claim

    def failing(self, claim, *, outcome):
        raise OSError("fixture: the release write failed")

    monkeypatch.setattr(WorkflowStore, "release_claim", failing)
    with pytest.raises(OSError):
        home.service.step(run)
    monkeypatch.setattr(WorkflowStore, "release_claim", real)
    return room, run


def test_a_team_never_set_up_is_empty_and_nothing_is_created(team, data, monkeypatch):
    value = snapshot(team, data, monkeypatch)
    assert value["runs"] == [] and value["notices"] == []
    assert (value["team_saved_at"], value["controller_saved_at"], value["claims_saved_at"]) == (None, None, None)
    assert not data.exists()


def test_a_corrupt_team_file_is_one_notice_and_no_rows(team, home, monkeypatch):
    queued(home)
    home.store.path.write_text("{not json", encoding="utf-8")
    value = snapshot(team, home.data, monkeypatch)
    assert codes(value) == ["team_unavailable"] and value["runs"] == []


def test_live_work_is_listed_and_ordinary_history_is_not(team, home, monkeypatch):
    _, done = queued(home, "Done", "req-done")
    home.service.step(done)
    finish(home, done, (("plan", PLAN), ("build", BUILD), ("review", PASS)))
    stop_room, stopped = queued(home, "Stopped", "req-stop")
    home.service.step(stopped)
    home.service.stop(stop_room)
    finish(home, stopped, (("plan", PLAN),))
    _, needs = queued(home, "Needs", "req-needs")
    home.service.step(needs)
    finish(home, needs, (("plan", {"status": "needs_input", "summary": "A choice is needed."}),))
    _, active = queued(home, TITLE, "req-active")
    home.service.step(active)  # in flight, holding the whole pair
    _, waiting = queued(home, "Waiting", "req-waiting")  # same task id, queued behind it
    rows = listed(snapshot(team, home.data, monkeypatch))
    assert set(rows) == {needs, active, waiting}  # completed and released stopped runs stay in Team history
    assert home.store.find_run(home.store._read(), done)[1]["claim"]  # a remembered claim alone is not terminal-held
    assert (rows[needs]["attention"], rows[active]["attention"], rows[waiting]["attention"]) == (True, False, False)
    assert (rows[active]["title"], rows[active]["status"], rows[active]["stage"]) == (TITLE, "planning", "plan")
    assert rows[waiting]["status"] == "queued" and len({row["key"] for row in rows.values()}) == 3


def test_an_uncertain_run_is_listed_for_attention_beside_newer_queued_work(team, home, monkeypatch):
    ambiguous = attach(team, home.data, FakeTransport(states=["ambiguous"]))
    _, old = queued(ambiguous, "Older", "req-old")
    ambiguous.service.step(old)
    _, new = queued(home, "Newer", "req-new")
    rows = listed(snapshot(team, home.data, monkeypatch))
    assert rows[old]["status"] == "uncertain" and rows[old]["attention"] is True
    assert rows[new]["status"] == "queued"


def test_a_finished_run_still_holding_its_shared_claim_is_terminal_held(team, home, monkeypatch):
    _, run = held_by_failed_release(home, monkeypatch)
    row = listed(snapshot(team, home.data, monkeypatch))[run]
    assert row["status"] == "stopped" and row["attention"] is True and row["claim_label"]


def test_a_finished_run_with_a_retained_local_lease_is_terminal_held(team, home, monkeypatch):
    _, run = queued(home)
    home.service.step(run)
    finish(home, run, (("plan", PLAN), ("build", BUILD), ("review", PASS)))
    with home.store.edit() as saved:  # fixture: a completed run whose terminal write kept a local lease
        home.store.find_run(saved, run)[1]["write_lease"] = True
    row = listed(snapshot(team, home.data, monkeypatch))[run]  # listed only because of the lease
    assert row["status"] == "completed" and row["attention"] is True


@pytest.mark.parametrize("registry", ["valid_empty", "absent"])
def test_unclaimed_queued_work_is_quiet_whatever_the_registry(team, home, monkeypatch, registry):
    _, run = queued(home)  # admission leaves a valid empty registry; queued work never held a claim
    if registry == "absent":
        (home.data / "workflow" / "claims.json").unlink()
    value = snapshot(team, home.data, monkeypatch)
    assert value["notices"] == [] and listed(value)[run]["claim_label"] == ""


def test_an_invalid_legacy_title_is_one_unavailable_notice_without_echo_or_change(team, home, monkeypatch):
    room, _ = queued(home, "Legacy chat", "req-old")
    with home.store.edit() as saved:  # fixture: history saved before invalid Unicode was refused
        saved["rooms"][room]["room"]["title"] = "Legacy ZQ \ud800 chat"
    value = snapshot(team, home.data, monkeypatch)  # also proves the file is left unchanged
    assert "team_unavailable" in codes(value)
    text = json.dumps(value, ensure_ascii=False)
    text.encode("utf-8")  # renders as valid UTF-8, so the bad title was never echoed
    assert "Legacy ZQ \ud800 chat" not in text


@pytest.mark.parametrize("damage", ["absent", "invalid"])
def test_claimed_work_without_a_readable_registry_is_unconfirmed_once(team, home, monkeypatch, damage):
    _, done = queued(home, "Done", "req-done")
    home.service.step(done)
    finish(home, done, (("plan", PLAN), ("build", BUILD), ("review", PASS)))  # history with a remembered claim
    _, active = queued(home, TITLE, "req-active")
    home.service.step(active)
    registry = home.data / "workflow" / "claims.json"
    if damage == "absent":
        registry.unlink()
    else:
        registry.write_text("{broken", encoding="utf-8")
    value = snapshot(team, home.data, monkeypatch)
    assert codes(value) == ["claims_unavailable"]  # one notice, not a card per historical run
    rows = listed(value)
    assert set(rows) == {active} and rows[active]["claim_label"]  # unconfirmed, never proof of release


@pytest.mark.parametrize("state", ["missing", "revoked", "expired", "reconfigured", "stopped_idle", "stopped_blocked",
                                   "suspended_unknown", "exhausted"])
def test_active_work_under_a_controller_that_no_longer_supports_it_needs_attention(team, home, monkeypatch, state):
    _, run = queued(home)
    control, now = home.controller.path, None
    if state == "missing":
        control.unlink()
    elif state == "revoked":
        home.controller.revoke()
    elif state == "expired":
        now = home.controller.status()["envelope"]["expires_at"] + 1
    elif state == "reconfigured":
        home.controller.configure(home.controller.status()["envelope"])  # a new version; the run keeps the old one
    else:
        saved = json.loads(control.read_text(encoding="utf-8"))
        team.write_json(control, workflow_control._stop(saved, state, "fixture"))
    value = snapshot(team, home.data, monkeypatch, now=now)
    row = listed(value)[run]
    assert row["attention"] is True and row["controller_label"]
    if state == "missing":
        assert "controller_unavailable" in codes(value)


def test_control_healthy_active_work_needs_no_attention(team, home, monkeypatch):
    _, run = queued(home)
    home.service.step(run)
    value = snapshot(team, home.data, monkeypatch)
    row = listed(value)[run]
    assert row["attention"] is False and row["controller_label"] and value["notices"] == []
    assert finite(value["team_saved_at"]) and finite(value["controller_saved_at"]) and finite(value["claims_saved_at"])


def test_a_store_from_another_controller_namespace_joins_no_authority(team, home, tmp_path, monkeypatch):
    _, run = queued(home)
    home.service.step(run)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(workflow_control, "DATA_DIR", elsewhere)  # the store's default controller now resolves elsewhere
    value = snapshot(team, home.data, monkeypatch)
    assert "controller_namespace" in codes(value) and listed(value)[run]["attention"] is True
    assert not elsewhere.exists()  # no foreign lock or registry


def test_a_legacy_store_is_listed_with_a_notice_and_no_authority(team, home, monkeypatch):
    _, run = queued(home)
    as_version_1(home.store)
    value = snapshot(team, home.data, monkeypatch)
    assert "team_legacy" in codes(value) and listed(value)[run]["attention"] is True


def test_saved_bytes_above_the_admission_ceiling_add_one_capacity_notice(team, home, monkeypatch):
    _, run = queued(home)
    pad(home.store, CEILING + 1_000)
    value = snapshot(team, home.data, monkeypatch)
    assert codes(value) == ["team_capacity"] and list(listed(value)) == [run]


def test_control_saved_bytes_at_the_ceiling_add_no_notice(team, home, monkeypatch):
    queued(home)
    pad(home.store, CEILING)
    assert snapshot(team, home.data, monkeypatch)["notices"] == []


def test_worker_text_never_becomes_a_label_and_known_reasons_are_told_apart(team, home, monkeypatch):
    needs = []
    for index, secret in enumerate(("Worker secret alpha 7Q", "Worker secret beta 9Z")):
        _, run = queued(home, f"Needs {index}", f"req-needs-{index}")
        home.service.step(run)
        finish(home, run, (("plan", {"status": "needs_input", "summary": secret}),))
        needs.append(run)
    _, refused = queued(home, "Refused", "req-refused")
    pad(home.store, CEILING - 64)
    home.service.step(refused)  # the capacity refusal records the fixed storage reason
    value = snapshot(team, home.data, monkeypatch)
    rows = listed(value)
    generic = {rows[run]["reason_label"] for run in needs}
    assert len(generic) == 1 and "secret" not in generic.pop().lower()
    assert rows[refused]["status"] == "needs_input" and rows[refused]["reason_label"] != rows[needs[0]]["reason_label"]


def test_a_queued_expiry_is_told_apart_from_worker_text(team, home, monkeypatch):
    _, worker = queued(home, "Worker", "req-worker")
    home.service.step(worker)
    finish(home, worker, (("plan", {"status": "needs_input", "summary": "Worker words."}),))
    _, blocked = queued(home, "Holder", "req-holder")
    home.service.step(blocked)  # holds the pair so the next run cannot dispatch
    _, expired = queued(home, "Expired", "req-expired")
    later = time.time() + 31 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    home.service.step(expired)
    rows = listed(snapshot(team, home.data, monkeypatch, now=later))
    assert rows[expired]["status"] == "needs_input"
    assert rows[expired]["reason_label"] and rows[expired]["reason_label"] != rows[worker]["reason_label"]


def test_only_the_owner_title_appears_as_private_text(team, home, monkeypatch):
    room = home.store.create_room(TITLE)["id"]
    run = home.store.submit(room, "Private prompt 5K about my plans.", "req-1")["run_id"]
    home.service.step(run)
    finish(home, run, (("plan", {**PLAN, "summary": "Private plan summary 6L",
                                 "tasks": [{"title": "Private task 8M", "instruction": "Private instruction 3P"}]}),))
    text = json.dumps(snapshot(team, home.data, monkeypatch), ensure_ascii=False)
    assert TITLE in text
    config = team_transport.read_config(home.store.root)
    private = ["Private prompt 5K", "Private plan summary 6L", "Private task 8M", "Private instruction 3P",
               str(home.data), config["workspace"], config["controller"]["task_id"],
               *(pin["id"] for pin in config["participants"].values())]
    assert [secret for secret in private if secret in text] == []
