"""Claude capacity acceptance (2026-09-28), written before the build against Home's reconciliation.

Governing contract: a private design note with the accepted seams of a private design note. Every Team
write keeps the 16 MiB hard canonical limit. An ordinary admission (room, note, shared message and run, any stage
dispatch) must leave both the durable state and the prospective state at or below 8 MiB, keeping an 8 MiB reserve for
settlement: replies, Stop, timeouts, failures and reconciliation may use the reserve, never the hard limit. Fresh state
is version 2 and records its resolved controller path; another namespace is refused before any claim is read.
Archive is explicit, exact-digest and process-crash safe. Legacy version 1 stays readable, takes no new work and is
never converted.

Full-size states throughout (no shrunken limits). Production default paths, as the web app wires them, under this
test's DATA_DIR; temporary stores and fake transports; no native, provider or network calls.

Seam assumptions to confirm (a private design note): team_chat.TeamCapacityError(ValueError);
team_chat.serialized_size(value); team_chat.require_admission(before, after, *, claims, candidate_claim=None);
TeamService.capacity_status() keys eligible, canonical_bytes, hard_limit_bytes, reserve_bytes, admission_limit_bytes,
admission_allowed, reason; TeamService.archive_room(room_id, *, expected_room_sha256) -> room_id, archived, removed,
archive_path, room_sha256; CLI `archive ROOM_ID --preview` (Home's service.archive_preview seam; only the keys
room_sha256 and a boolean eligible are pinned) and `archive ROOM_ID --expect-sha256 HEX`; archive files at
ROOT/archives/<room_id>-<room_sha256>.json; the room digest is SHA-256 of json.dumps(room, sort_keys=True,
separators=(",", ":"), ensure_ascii=True), as for claims; an existing archives directory that is a symlink or not
0700 is refused, not silently tightened. A capacity refusal reason names storage or archiving.
"""
import hashlib
import importlib
import json
import stat
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import workflow_control, workflow_service
from companion.workflow_control import WorkflowStore
from tests.test_team_chat_acceptance import BUILD, PASS, PLAN, FakeTransport, authorize, respond

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

MIB = 1024 * 1024
HARD, CEILING = 16 * MIB, 8 * MIB
ROCKET = "\U0001f680"  # 4 UTF-8 bytes on the wire, 12 bytes once escaped in the store
MARKER = "Ghi chú riêng 秘密 🚀"  # valid multilingual text that must never leak into a report
SETUP_REASON = "The submitted setup or authority no longer permits this stage. Inspect it on the Mac."


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


@pytest.fixture
def home(team, tmp_path, monkeypatch):
    """The production default paths (the web app's wiring) under this test's DATA_DIR."""
    data = tmp_path / "data"
    monkeypatch.setattr(workflow_control, "DATA_DIR", data)
    monkeypatch.setattr(team, "DATA_DIR", data)
    authorize(data)  # grant at DATA_DIR/workflow/control.json, v2 binding at DATA_DIR/team/config.json
    return attach(team, data)


def attach(team, data, transport=None):
    """A fresh store and service over the same files, as after a restart."""
    store = team.TeamStore()
    transport = transport or FakeTransport()
    service = team.TeamService(store, transport)
    return SimpleNamespace(team=team, data=data, store=store, service=service, transport=transport,
                           controller=service.controller)


def canonical(value):
    return len(json.dumps(value, indent=2).encode("utf-8"))


def note(index, text):
    """Shaped exactly like a local-only note from submit(share_with_team=False)."""
    return {"id": f"{index:032x}", "author": "user", "text": text, "created_at": 1.5,
            "request_id": f"pad-{index:05d}", "run_id": None, "shared": False}


def pad_value(value, target):
    """Add one History room of ordinary notes so the canonical state is exactly `target` bytes."""
    room = uuid.uuid4().hex
    value["rooms"][room] = {"room": {"id": room, "title": "History", "local_only": False, "created_at": 1.5,
                                     "updated_at": 1.5}, "messages": [], "runs": []}
    messages = value["rooms"][room]["messages"]
    for text in (ROCKET * 16000, "x" * 16000):
        messages.append(note(len(messages), text))
        grown = canonical(value)
        messages.append(note(len(messages), text))
        size = canonical(value)
        if size > target:
            del messages[-2:]
            continue
        extra, base = max(0, (target - size - 40_000) // (size - grown)), len(messages)
        messages.extend(note(base + i, text) for i in range(extra))
    size = canonical(value)
    while target - size > 15_000:
        messages.append(note(len(messages), "x" * 12_000))
        size = canonical(value)
    assert size < target, "the fixture state is already larger than the target"
    messages.append(note(len(messages), "x"))
    messages[-1]["text"] = "x" * (1 + target - canonical(value))
    assert canonical(value) == target
    return room


def pad(store, target):
    with store.edit() as data:  # fixture setup through the local primitive; production writers are tested below
        return pad_value(data, target)


def row(store, room, run):
    return next(item for item in store.get_room(room)["runs"] if item["id"] == run)


def claims_for(home, run):
    return [claim for claim in home.controller.claims() if claim["run_id"] == run]


def queued(home, title="Room", request="req-1"):
    room = home.store.create_room(title)["id"]
    return room, home.store.submit(room, "Add a fixture field and test it.", request)["run_id"]


def capacity_refusal(reason):
    return (reason != SETUP_REASON
            and any(word in reason.lower() for word in ("storage", "capacity", "space", "full", "archive")))


def results(store, room):
    return [message["text"] for message in store.get_room(room)["messages"] if message.get("kind") == "result"]


def spy_claim_saves(monkeypatch):
    saved, real = [], WorkflowStore._save_claims

    def spy(self, value):
        saved.append(deepcopy(value["claims"]))
        return real(self, value)

    monkeypatch.setattr(WorkflowStore, "_save_claims", spy)
    return saved


def answer(transport, stage, run, **fields):
    """A worker reply as compact UTF-8 JSON, so the largest valid replies fit the 65,536-byte file limit."""
    send = [item for item in transport.sends if item["stage"] == stage and item["run_id"] == run][-1]
    body = {"run_id": run, "stage": stage, "attempt": send["attempt"], "status": "completed", **fields}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(raw) <= 65_536
    send["result_path"].parent.mkdir(parents=True, exist_ok=True)
    send["result_path"].write_bytes(raw)
    return len(raw)


# Admission: nothing ordinary may spend the reserve


def test_a_room_that_would_cross_the_admission_ceiling_is_refused_and_nothing_is_written(home):
    pad(home.store, CEILING - 64)
    before = home.store.path.read_bytes()
    with pytest.raises(ValueError) as refused:
        home.store.create_room("One more room")
    assert type(refused.value).__name__ == "TeamCapacityError"
    assert home.store.path.read_bytes() == before


@pytest.mark.parametrize("shared", [False, True])
def test_a_message_that_would_cross_the_ceiling_is_refused_and_nothing_is_written(home, shared):
    room = home.store.create_room("Room")["id"]
    pad(home.store, CEILING - 64)
    before = home.store.path.read_bytes()
    with pytest.raises(ValueError) as refused:
        home.store.submit(room, "Add a fixture field and test it.", "req-1", share_with_team=shared)
    assert type(refused.value).__name__ == "TeamCapacityError"
    assert home.store.path.read_bytes() == before


def test_control_ordinary_writes_below_the_ceiling_are_admitted(home):
    room = home.store.create_room("Room")["id"]
    pad(home.store, CEILING - 40_000)
    home.store.create_room("One more room")
    home.store.submit(room, "A private note.", "req-1", share_with_team=False)
    run = home.store.submit(room, "Add a fixture field and test it.", "req-2")["run_id"]
    home.service.step(run)
    assert row(home.store, room, run)["status"] == "planning" and len(home.transport.sends) == 1


def test_a_first_dispatch_that_would_cross_the_ceiling_is_refused_before_any_claim_is_saved(home, monkeypatch):
    room, run = queued(home)
    pad(home.store, CEILING - 64)  # the queued run fits; its reservation would not
    saved = spy_claim_saves(monkeypatch)
    home.service.step(run)
    item = row(home.store, room, run)
    assert item["status"] == "needs_input" and capacity_refusal(item["reason"])
    assert saved == [] and claims_for(home, run) == [] and home.transport.sends == []


def test_the_next_stage_is_refused_after_its_reply_is_saved_exactly_once(home, monkeypatch):
    room, run = queued(home)
    home.service.step(run)  # the plan was admitted while the store was small
    pad(home.store, CEILING - 64)
    respond(home.transport, "plan", run_id=run, **PLAN)
    saved = spy_claim_saves(monkeypatch)
    home.service.step(run)  # the reply is settlement; the build is a new admission
    item = row(home.store, room, run)
    assert item["status"] == "needs_input" and capacity_refusal(item["reason"])
    assert results(home.store, room) == [PLAN["summary"]]
    assert not any(claim["stage"] == "build" for snapshot in saved for claim in snapshot)  # never persisted
    assert claims_for(home, run) == []  # the finished plan's own claim was released
    home.service.step(run)
    assert results(home.store, room) == [PLAN["summary"]] and [s["stage"] for s in home.transport.sends] == ["plan"]


# Settlement: the reserve covers the largest valid replies and recovery writes


def test_control_a_maximal_needs_input_reply_settles_above_the_ceiling_within_its_allowance(home):
    room, run = queued(home)
    pad(home.store, CEILING - 40_000)
    home.service.step(run)  # admitted below the ceiling
    pad(home.store, CEILING + 200_000)  # settlement must still work after concurrent growth
    before = home.store.path.stat().st_size
    answer(home.transport, "plan", run, status="needs_input", summary=ROCKET * 16000)
    home.service.step(run)
    item = row(home.store, room, run)
    assert item["status"] == "needs_input" and len(item["reason"]) <= 240
    assert len(item["reason"].splitlines()) <= 2
    message = home.store.get_room(room)['messages'][-1]
    team_log = importlib.import_module('companion.team_log')
    assert team_log.read(home.store.root, room, message['details_id'])['details'] == ROCKET * 16000
    after = home.store.path.stat().st_size
    assert CEILING < after < HARD and after - before <= 384_000 + 8_192
    assert claims_for(home, run) == []


def test_a_maximal_plan_settles_within_its_allowance_and_the_build_is_refused_truthfully(home):
    room, run = queued(home)
    home.service.step(run)
    pad(home.store, CEILING - 40_000)
    tasks = [{"title": "\x7f" * 160, "instruction": "\x7f" * 8000} for _ in range(4)]
    body = {"summary": "\x7f" * 16000, "tasks": tasks}
    base = len(json.dumps({"run_id": run, "stage": "plan", "attempt": 0, "status": "completed", **body},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    upgrades = (65_536 - base) // 3  # each DEL -> astral swap adds three wire bytes
    body["summary"] = ROCKET * upgrades + "\x7f" * (16000 - upgrades)
    before = home.store.path.stat().st_size
    answer(home.transport, "plan", run, **body)
    home.service.step(run)
    assert home.store.path.stat().st_size - before <= 325_632 + 8_192
    item = row(home.store, room, run)
    assert item["status"] == "needs_input" and capacity_refusal(item["reason"])
    assert [task["instruction"] for task in item["tasks"]] == ["\x7f" * 8000] * 4
    assert [s["stage"] for s in home.transport.sends] == ["plan"] and claims_for(home, run) == []


def test_control_stop_settles_for_queued_runs_above_the_ceiling(home):
    runs = [queued(home, f"Room {i}", f"req-{i}") for i in range(3)]
    pad(home.store, CEILING + 200_000)  # already above the ceiling, as after earlier settlements
    for room, _ in runs:
        home.service.stop(room)
    assert all(row(home.store, room, run)["status"] == "stopped" for room, run in runs)


class Died(BaseException):
    """Stands in for process death: no handler in _dispatch catches it, so fail_run never runs."""


def test_control_a_claim_only_crash_admitted_near_the_ceiling_still_recovers(home, monkeypatch):
    room, run = queued(home)
    pad(home.store, CEILING - 40_000)
    real = home.team.write_json

    def dying(path, value, **kwargs):
        if Path(path).name == "state.json" and any(item["phase"] == "dispatching" for entry in value["rooms"].values()
                                                   for item in entry["runs"]):
            raise Died()
        return real(path, value, **kwargs)

    monkeypatch.setattr(home.team, "write_json", dying)
    with pytest.raises(Died):
        home.service.step(run)
    monkeypatch.setattr(home.team, "write_json", real)
    fresh = attach(home.team, home.data)
    fresh.service.stop(room)
    assert row(fresh.store, room, run)["status"] == "uncertain"
    fresh.service.reconcile(run, worker_ended=True)
    assert row(fresh.store, room, run)["status"] == "stopped" and claims_for(fresh, run) == []
    assert fresh.store.path.stat().st_size < HARD and fresh.transport.sends == []


# The pure admission gates


def live(home, dispatched=True):
    room, run = queued(home)
    if dispatched:
        home.service.step(run)
    return home.store._read(), home.controller.claims(), room, run


def clone_room(state, room, *, status=None, with_claim=True):
    """Copy a room and its first run under new ids; return the copied run and any copied claim."""
    copy = deepcopy(state["rooms"][room])
    new_room, run = uuid.uuid4().hex, copy["runs"][0]
    copy["room"]["id"] = new_room
    run["id"] = uuid.uuid4().hex
    claims = []
    if run.get("claim") and with_claim:
        run["claim"] = {**run["claim"], "id": uuid.uuid4().hex, "run_id": run["id"]}
        claims.append(run["claim"])
    if status:
        run.update(status=status, phase="terminal")
    state["rooms"][new_room] = copy
    return run, claims


def test_admission_checks_the_durable_state_as_well_as_the_prospective_one(home):
    state, claims, room, run = live(home, dispatched=False)
    home.team.require_admission(state, state, claims=claims)  # an eligible small state is admitted
    durable = deepcopy(state)
    pad_value(durable, CEILING + 1_000)
    with pytest.raises(ValueError):  # a smaller prospective state does not excuse an oversized durable one
        home.team.require_admission(durable, state, claims=claims)


def test_a_third_unresolved_reply_obligation_is_refused(home):
    state, claims, room, run = live(home)  # one plan in flight
    before = deepcopy(state)
    _, second = clone_room(before, room)
    home.team.require_admission(state, before, claims=claims, candidate_claim=second[0])  # two slots fit
    after = deepcopy(before)
    _, third = clone_room(after, room)
    with pytest.raises(ValueError, match="More than 2 Team runs"):
        home.team.require_admission(before, after, claims=claims + second, candidate_claim=third[0])


def test_the_hundred_and_first_control_relevant_run_is_refused(home):
    state, claims, room, run = live(home, dispatched=False)
    for _ in range(98):
        clone_room(state, room)
    before = deepcopy(state)
    orphan = deepcopy(before["rooms"][room]["runs"][0])
    orphan.update(id=uuid.uuid4().hex, status="uncertain", phase="terminal")  # an unleased orphan still counts
    hundred = deepcopy(before)
    hundred["rooms"][room]["runs"].insert(0, orphan)
    home.team.require_admission(before, hundred, claims=claims)  # 100 control-relevant runs fit
    after = deepcopy(hundred)
    clone_room(after, next(key for key in before["rooms"] if key != room))
    with pytest.raises(ValueError, match="More than 100 Team runs"):
        home.team.require_admission(hundred, after, claims=claims)


def test_a_claim_larger_than_its_copy_allowance_is_refused(home):
    state, claims, room, run = live(home)
    (claim,) = claims
    before = deepcopy(state)
    other, _ = clone_room(before, room, with_claim=False)
    other.update(phase="pending", status="queued")
    other.pop("claim", None)
    after = deepcopy(before)
    oversized = {**claim, "id": uuid.uuid4().hex, "run_id": other["id"], "repo": "/" + "r" * 70_000}
    with pytest.raises(ValueError, match="recovery claim is too large"):
        home.team.require_admission(before, after, claims=claims, candidate_claim=oversized)


def test_only_claims_for_runs_in_this_root_count_and_a_duplicate_is_refused(home):
    state, claims, room, run = live(home)
    unrelated = {**claims[0], "id": uuid.uuid4().hex, "run_id": uuid.uuid4().hex}
    home.team.require_admission(state, state, claims=claims + [unrelated])  # adds no bytes to this root
    duplicate = {**claims[0], "id": uuid.uuid4().hex}
    with pytest.raises(ValueError, match="multiple recovery claims"):
        home.team.require_admission(state, state, claims=claims + [duplicate])


def test_unknown_run_metadata_makes_the_state_ineligible(home):
    state, claims, room, run = live(home, dispatched=False)
    after = deepcopy(state)
    after["rooms"][room]["runs"][0]["unbudgeted"] = "x"
    with pytest.raises(ValueError, match="Team state is invalid"):
        home.team.require_admission(state, after, claims=claims)


# Receipts


class Receipt(FakeTransport):
    def __init__(self, receipt):
        super().__init__()
        self.receipt = receipt

    def send(self, **kwargs):
        super().send(**kwargs)
        return dict(self.receipt)


@pytest.mark.parametrize("delivery", ["x" * 5000, 987654321, "ABCDEF0123456789ABCDEF0123456789",
                                      "abcdef0123456789abcdef012345678"])
def test_an_unbounded_or_malformed_receipt_id_ends_uncertain_and_is_not_stored(home, delivery):
    fresh = attach(home.team, home.data, Receipt({"state": "queued", "id": delivery}))
    room, run = queued(fresh)
    fresh.service.step(run)
    item = row(fresh.store, room, run)
    assert item["status"] == "uncertain" and item.get("delivery_id") is None
    assert len(claims_for(fresh, run)) == 1  # never inferred as a failed delivery


def test_control_a_bounded_receipt_id_is_recorded(home):
    delivery = "0123456789abcdef" * 2
    fresh = attach(home.team, home.data, Receipt({"state": "queued", "id": delivery}))
    room, run = queued(fresh)
    fresh.service.step(run)
    item = row(fresh.store, room, run)
    assert item["phase"] == "waiting" and item["delivery_id"] == delivery


# Provenance


def test_a_fresh_store_records_version_2_and_its_resolved_controller_path(home):
    home.store.create_room("Room")
    saved = json.loads(home.store.path.read_text(encoding="utf-8"))
    assert set(saved) == {"version", "controller_path", "rooms"}
    assert saved["version"] == 2 and saved["controller_path"] == str(home.controller.path)


def test_another_controller_namespace_is_refused_before_any_foreign_registry_exists(home, tmp_path, monkeypatch,
                                                                                     capsys):
    room, run = queued(home)
    before = home.store.path.read_bytes()
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setattr(workflow_control, "DATA_DIR", elsewhere)  # the default controller now resolves elsewhere
    from scripts import team_chat as script

    def cli_preview():
        try:
            code = script.main(["--root", str(home.store.root), "reconcile", run, "--preview"])
        except SystemExit as exc:
            code = exc.code
        capsys.readouterr()
        if code:
            raise ValueError("refused")

    attempts = {
        "create_room": lambda: attach(home.team, home.data).store.create_room("Another"),
        "submit": lambda: attach(home.team, home.data).store.submit(room, "A note.", "req-9", share_with_team=False),
        "step": lambda: attach(home.team, home.data).service.step(run),
        "stop": lambda: attach(home.team, home.data).service.stop(room),
        "fail_run": lambda: attach(home.team, home.data).service.fail_run(run),
        "reconcile": lambda: attach(home.team, home.data).service.reconcile(run, worker_ended=True),
        "preview": cli_preview,
    }
    refused = {}
    for name, attempt in attempts.items():
        try:
            attempt()
            refused[name] = False
        except (ValueError, PermissionError):
            refused[name] = True
    assert refused == dict.fromkeys(attempts, True)
    assert home.store.path.read_bytes() == before
    assert not (elsewhere / "workflow" / "claims.json").exists()


def test_control_a_symlinked_spelling_of_the_same_controller_is_the_same_namespace(home, tmp_path, monkeypatch):
    home.store.create_room("Room")
    alias = tmp_path / "alias"
    alias.symlink_to(home.data, target_is_directory=True)
    monkeypatch.setattr(workflow_control, "DATA_DIR", alias)  # resolves to the same controller file
    attach(home.team, home.data).store.create_room("Another room")
    assert len(home.store.list_rooms()) == 2


# Legacy version 1


def as_version_1(store):
    value = store._read()
    store.path.write_text(json.dumps({"version": 1, "rooms": value["rooms"]}, indent=2), encoding="utf-8")


def test_a_legacy_store_stays_readable_and_stoppable_but_takes_no_new_work(home):
    (first, stopped), (second, waiting) = queued(home, "First", "req-a"), queued(home, "Second", "req-b")
    as_version_1(home.store)
    legacy = attach(home.team, home.data)
    assert {room["id"] for room in legacy.store.list_rooms()} == {first, second}
    before = legacy.store.path.read_bytes()
    for attempt in (lambda: legacy.store.create_room("New"),
                    lambda: legacy.store.submit(second, "A note.", "req-c", share_with_team=False)):
        with pytest.raises(ValueError):
            attempt()
    assert legacy.store.path.read_bytes() == before
    legacy.service.step(waiting)  # legacy work is not dispatched under the new guarantee
    assert claims_for(legacy, waiting) == [] and legacy.transport.sends == []
    legacy.service.stop(first)
    assert row(legacy.store, first, stopped)["status"] == "stopped"
    saved = json.loads(legacy.store.path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and "controller_path" not in saved  # never silently converted


def test_control_a_legacy_store_at_the_hard_limit_refuses_stop_and_keeps_the_file(home):
    room, run = queued(home)
    value = home.store._read()
    legacy = {"version": 1, "rooms": value["rooms"]}
    pad_value(legacy, HARD)
    home.team.write_json(home.store.path, legacy)
    before = home.store.path.read_bytes()
    with pytest.raises(ValueError):
        attach(home.team, home.data).service.stop(room)
    assert home.store.path.read_bytes() == before


# Locks


def lock_audit(team, monkeypatch):
    audit, real_locked, real_edit = SimpleNamespace(depth=0, entries=[]), WorkflowStore._locked, team.TeamStore.edit

    @contextmanager
    def locked(self):
        if audit.depth:
            raise AssertionError("the controller lock was requested while already held")
        with real_locked(self):
            audit.depth += 1
            try:
                yield
            finally:
                audit.depth -= 1

    @contextmanager
    def edit(self):
        audit.entries.append(audit.depth > 0)
        with real_edit(self) as data:
            yield data

    monkeypatch.setattr(WorkflowStore, "_locked", locked)
    monkeypatch.setattr(team.TeamStore, "edit", edit)
    return audit


def test_admission_writers_take_the_controller_lock_first_and_no_path_takes_it_twice(home, monkeypatch):
    audit = lock_audit(home.team, monkeypatch)
    room = home.store.create_room("Room")["id"]
    home.store.submit(room, "A private note.", "req-1", share_with_team=False)
    run = home.store.submit(room, "Add a fixture field and test it.", "req-2")["run_id"]
    assert audit.entries == [True, True, True]  # controller, then Team, for every ordinary admission
    home.service.step(run)
    respond(home.transport, "plan", run_id=run, **PLAN)
    home.service.step(run)
    home.service.stop(room)
    monkeypatch.setattr(workflow_service, "observe_activity", lambda chats, repo, now: [
        dict(app=chat["app"], id=chat["id"], state="idle", observed_at=now) for chat in chats])
    workflow_service.check(home.controller, team_store=home.store, now=time.time())


# Archive


def room_digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                          .encode("utf-8")).hexdigest()


def cli(home, capsys, *args):
    from scripts import team_chat as script
    try:
        code = script.main(["--root", str(home.store.root), "--controller-path", str(home.controller.path), *args])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def old_chat(home):
    room = home.store.create_room("Old chat")["id"]
    home.store.submit(room, MARKER, "req-1", share_with_team=False)
    return room


def test_archive_preview_reports_the_room_digest_and_eligibility_without_content(home, capsys):
    room = old_chat(home)
    code, out, err = cli(home, capsys, "archive", room, "--preview")
    assert code == 0, err
    result = json.loads(out)
    assert result["room_sha256"] == room_digest(home.store.get_room(room)) and result["eligible"] is True
    assert MARKER not in out and "Ghi" not in out


def test_archiving_publishes_a_private_complete_copy_before_removing_the_room(home, capsys):
    room = old_chat(home)
    record, claims = home.store.get_room(room), home.controller.claims()
    digest = room_digest(record)
    code, out, err = cli(home, capsys, "archive", room, "--expect-sha256", digest)
    assert code == 0, err
    result = json.loads(out)
    path = home.store.root / "archives" / f"{room}-{digest}.json"
    assert result["archived"] is True and result["removed"] is True and result["room_sha256"] == digest
    assert Path(result["archive_path"]) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert record in json.loads(path.read_text(encoding="utf-8")).values()  # complete, multilingual text intact
    assert room not in {item["id"] for item in home.store.list_rooms()} and home.controller.claims() == claims


def test_a_stale_digest_archives_nothing(home, capsys):
    room = old_chat(home)
    code, out, err = cli(home, capsys, "archive", room, "--preview")
    assert code == 0, err  # the command exists, so the refusal below is about the stale digest
    digest = json.loads(out)["room_sha256"]
    home.store.submit(room, "A later note.", "req-2", share_with_team=False)
    code, out, err = cli(home, capsys, "archive", room, "--expect-sha256", digest)
    assert code not in (0, None)
    assert room in {item["id"] for item in home.store.list_rooms()}
    assert not (home.store.root / "archives").exists() or not any((home.store.root / "archives").iterdir())


def unfinished(home, kind, monkeypatch):
    if kind == "uncertain":
        home = attach(home.team, home.data, FakeTransport(states=["ambiguous"]))
    room, run = queued(home)
    if kind in ("in_flight", "uncertain", "claimed"):
        home.service.step(run)
    if kind == "claimed":  # looks finished, but its shared claim is still held after a failed release
        home.service.stop(room)
        respond(home.transport, "plan", run_id=run, **PLAN)
        real_release = WorkflowStore.release_claim

        def failing_release(self, claim, *, outcome):
            raise OSError("fixture: the release write failed")

        monkeypatch.setattr(WorkflowStore, "release_claim", failing_release)
        with pytest.raises(OSError):
            home.service.step(run)
        monkeypatch.setattr(WorkflowStore, "release_claim", real_release)
        assert row(home.store, room, run)["status"] == "stopped" and claims_for(home, run)
    return home, room


@pytest.mark.parametrize("kind", ["queued", "in_flight", "uncertain", "claimed"])
def test_archive_refuses_a_room_whose_work_is_unfinished_or_still_claimed(home, monkeypatch, kind):
    home, room = unfinished(home, kind, monkeypatch)
    before, claims = home.store.path.read_bytes(), home.controller.claims()
    with pytest.raises((ValueError, PermissionError)):
        home.service.archive_room(room, expected_room_sha256=room_digest(home.store.get_room(room)))
    assert home.store.path.read_bytes() == before and home.controller.claims() == claims
    assert not (home.store.root / "archives").exists() or not any((home.store.root / "archives").iterdir())


@pytest.mark.parametrize("hazard", ["symlinked_directory", "wide_directory", "different_existing_file",
                                    "path_like_room_id"])
def test_archive_refuses_unsafe_destinations_and_ids(home, tmp_path, hazard):
    room = old_chat(home)
    digest, archives = room_digest(home.store.get_room(room)), home.store.root / "archives"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if hazard == "symlinked_directory":
        archives.symlink_to(elsewhere, target_is_directory=True)
    elif hazard == "wide_directory":
        archives.mkdir()
        archives.chmod(0o777)
    elif hazard == "different_existing_file":
        archives.mkdir(mode=0o700)
        (archives / f"{room}-{digest}.json").write_text("{}", encoding="utf-8")
    before = home.store.path.read_bytes()
    with pytest.raises((ValueError, PermissionError, LookupError)):
        home.service.archive_room("../escape" if hazard == "path_like_room_id" else room, expected_room_sha256=digest)
    assert home.store.path.read_bytes() == before and not any(elsewhere.iterdir())
    if hazard == "different_existing_file":
        assert (archives / f"{room}-{digest}.json").read_text(encoding="utf-8") == "{}"


def test_a_failed_active_save_keeps_both_copies_and_the_retry_finishes_deterministically(home, monkeypatch):
    room = old_chat(home)
    digest = room_digest(home.store.get_room(room))
    real = home.team.write_json

    def failing(path, value, **kwargs):
        if Path(path).name == "state.json":
            raise OSError("fixture: the active save failed")
        return real(path, value, **kwargs)

    monkeypatch.setattr(home.team, "write_json", failing)
    with pytest.raises(OSError):
        home.service.archive_room(room, expected_room_sha256=digest)
    monkeypatch.setattr(home.team, "write_json", real)
    path = home.store.root / "archives" / f"{room}-{digest}.json"
    first = path.read_bytes()  # the preserved copy exists before the active removal
    assert room in {item["id"] for item in home.store.list_rooms()}
    assert home.service.archive_room(room, expected_room_sha256=digest)["removed"] is True
    assert path.read_bytes() == first  # retries publish identical bytes
    assert home.service.archive_room(room, expected_room_sha256=digest)["removed"] is False


# Measurement, diagnostics and long rooms


def test_serialized_size_is_the_saved_canonical_byte_count(home):
    home.store.create_room("Room")
    pad(home.store, 1_000_017)
    assert home.team.serialized_size(home.store._read()) == home.store.path.stat().st_size == 1_000_017


def test_capacity_status_is_a_content_free_snapshot(home):
    old_chat(home)
    pad(home.store, CEILING - 40_000)
    status = home.service.capacity_status()
    assert status["eligible"] is True and status["admission_allowed"] is True
    assert status["canonical_bytes"] == home.store.path.stat().st_size
    assert (status["hard_limit_bytes"], status["admission_limit_bytes"], status["reserve_bytes"]) == (
        HARD, CEILING, HARD - CEILING)
    assert "Ghi" not in json.dumps(status) and str(home.data) not in json.dumps(status)
    pad(home.store, CEILING + 1_000_000)
    assert home.service.capacity_status()["admission_allowed"] is False


def test_control_replies_can_take_a_room_past_five_hundred_messages(home):
    room = home.store.create_room("Long room")["id"]
    with home.store.edit() as data:
        data["rooms"][room]["messages"].extend(note(i, f"Earlier note {i}.") for i in range(499))
    run = home.store.submit(room, "Add a fixture field and test it.", "req-1")["run_id"]
    home.service.step(run)
    for stage, fields in (("plan", PLAN), ("build", BUILD), ("review", PASS)):
        respond(home.transport, stage, run_id=run, **fields)
        home.service.step(run)
    assert row(home.store, room, run)["status"] == "completed"
    messages = home.store.get_room(room)["messages"]
    # The capacity point is the 503 notes and replies; honest progress lines (queued, working) ride along, bounded.
    assert len([m for m in messages if m.get("kind") != "step"]) == 503
    assert len(messages) - 503 <= 3
