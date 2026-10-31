"""Claude acceptance for Team lanes (phase 3 package 1, 2026-09-29), written against Codex's contract in
a private design note while Codex builds it.

A v3 binding lists lanes: a worktree of the bound repository plus one Claude and one Codex chat. A package takes
the first free lane and keeps it for every later stage; each stage claims that lane's worktree and only the chat
it uses; a busy lane queues instead of failing; a workspace outside the repository is refused. v2 bindings keep
their existing behavior (covered by test_team_chat_acceptance). Real git worktrees in a temp dir; the tests play
the agents through a fake transport.
"""
import importlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from companion.workflow_control import ACTIONS, WorkflowStore
from tests.test_team_chat_acceptance import BUILD, PASS, PLAN, REV, TASK, respond

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")


class LaneTransport:
    """Records each delivery; accepts whatever lane details the service adds to a send."""

    def __init__(self, states=()):
        self.sends, self.states = [], list(states)

    def send(self, **kwargs):
        self.sends.append({**kwargs, "result_path": Path(kwargs["result_path"])})
        return {"state": self.states.pop(0) if self.states else "queued"}

    def revision(self, *args, **kwargs):
        return REV

    def stages(self):
        return [(send["role"], send["stage"]) for send in self.sends]


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def repo_with_worktrees(root, count):
    main = root / "repo"
    main.mkdir(parents=True)
    git("init", "-q", cwd=main)
    git("-c", "user.email=fixture@example.com", "-c", "user.name=Fixture", "commit", "--allow-empty", "-qm", "base",
        cwd=main)
    trees = []
    for index in range(count):
        tree = root / f"lane-{index}"
        git("worktree", "add", "-q", "-b", f"lane-{index}", str(tree), cwd=main)
        trees.append(tree)
    return main, trees


def envelope(main, trees, lanes):
    return {"task_ids": [TASK], "repo": str(main), "workspaces": [str(tree) for tree in trees],
            "actions": sorted(ACTIONS),
            "chats": [{"app": app, **pin} for lane in lanes for app, pin in lane["participants"].items()],
            "max_unknown_checks": 2, "max_checks": 50, "interval_seconds": 60,
            "expires_at": time.time() + 86400, "max_repair_attempts": 2}


def bind_lanes(team, root, count, states=()):
    main, trees = repo_with_worktrees(root, count)
    lanes = [{"workspace": str(tree),
              "participants": {"claude": {"id": f"fixture-claude-{i}", "cwd": str(tree)},
                               "codex": {"id": f"fixture-codex-{i}", "cwd": str(tree)}}}
             for i, tree in enumerate(trees)]
    controller = WorkflowStore(root / "workflow" / "control.json")
    controller.configure(envelope(main, trees, lanes))
    config = {"version": 3, "workspace": str(main), "scope": "Fixture engineering scope.", "lanes": lanes,
              "controller": {"task_id": TASK, "version": controller.status()["version"]}}
    (root / "team").mkdir(parents=True, exist_ok=True)
    (root / "team" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    transport = LaneTransport(states)
    store = team.TeamStore(root / "team", controller=controller)
    return store, team.TeamService(store, transport, controller=controller), transport, controller, lanes


@pytest.fixture
def team():
    return importlib.import_module("companion.team_chat")


def start(store, service, name, request_id):
    room_id = store.create_room(name)["id"]
    run_id = store.submit(room_id, "Add a fixture field and test it.", request_id)["run_id"]
    service.step(run_id)
    return room_id, run_id


def run(store, room_id, run_id):
    return next(item for item in store.get_room(room_id)["runs"] if item["id"] == run_id)


def claim_apps(controller, run_id):
    return [set(claim["participants"]) for claim in controller.claims() if claim.get("run_id") == run_id]


def test_packages_on_different_lanes_are_sent_in_the_same_cycle(team, tmp_path):
    store, service, transport, _, _ = bind_lanes(team, tmp_path, 2)
    room_a, run_a = start(store, service, "Room A", "req-a")
    room_b, run_b = start(store, service, "Room B", "req-b")
    assert sorted(send["run_id"] for send in transport.sends) == sorted([run_a, run_b])
    lanes = {run(store, room_a, run_a)["lane"]["workspace"], run(store, room_b, run_b)["lane"]["workspace"]}
    assert len(lanes) == 2  # each package holds its own worktree


def test_a_busy_lane_queues_and_is_sent_once_the_first_package_ends(team, tmp_path):
    store, service, transport, _, _ = bind_lanes(team, tmp_path, 1)
    room_a, run_a = start(store, service, "Room A", "req-a")
    room_b, run_b = start(store, service, "Room B", "req-b")
    assert [send["run_id"] for send in transport.sends] == [run_a]
    assert run(store, room_b, run_b)["status"] == "queued"
    for stage, fields in (("plan", PLAN), ("build", BUILD), ("review", PASS)):
        respond(transport, stage, run_a, **fields)
        service.step(run_a)
    assert run(store, room_a, run_a)["status"] == "completed"
    service.step(run_b)
    assert transport.sends[-1]["run_id"] == run_b


def test_each_stage_claims_only_its_own_chat_and_the_package_keeps_its_lane(team, tmp_path):
    store, service, transport, controller, _ = bind_lanes(team, tmp_path, 2)
    room_a, run_a = start(store, service, "Room A", "req-a")
    lane = run(store, room_a, run_a)["lane"]
    assert claim_apps(controller, run_a) == [{"claude"}]  # planning uses Claude only
    respond(transport, "plan", run_a, **PLAN)
    service.step(run_a)
    assert transport.stages()[-1] == ("codex", "build")
    assert claim_apps(controller, run_a) == [{"codex"}]  # building uses Codex only
    assert run(store, room_a, run_a)["lane"] == lane


def test_a_workspace_outside_the_repository_is_refused(tmp_path):
    main, trees = repo_with_worktrees(tmp_path / "bound", 1)
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    git("init", "-q", cwd=stranger)
    pins = {"claude": {"id": "fixture-claude-0", "cwd": str(stranger)},
            "codex": {"id": "fixture-codex-0", "cwd": str(stranger)}}
    lanes = [{"workspace": str(stranger), "participants": pins}]
    own = [{"workspace": str(trees[0]), "participants": {app: {**pin, "cwd": str(trees[0])} for app, pin in pins.items()}}]
    WorkflowStore(tmp_path / "accepted.json").configure(envelope(main, trees, own))  # a real worktree is accepted
    with pytest.raises(ValueError):
        WorkflowStore(tmp_path / "control.json").configure(envelope(main, [*trees, stranger], lanes))


def test_a_free_lane_is_used_and_only_unresolved_lanes_ask_the_owner(team, tmp_path):
    # Codex's question, 2026-09-29: an uncertain claim never frees itself, so it must not become a silent wait.
    store, service, transport, _, _ = bind_lanes(team, tmp_path, 2, states=["ambiguous", "ambiguous"])
    room_a, run_a = start(store, service, "Room A", "req-a")
    service.step(run_a)
    service.step(run_a)
    assert run(store, room_a, run_a)["status"] == "uncertain"
    room_b, run_b = start(store, service, "Room B", "req-b")
    assert [send["run_id"] for send in transport.sends] == [run_a, run_b]  # lane 1 was free, so B was sent
    service.step(run_b)
    service.step(run_b)
    room_c, run_c = start(store, service, "Room C", "req-c")
    assert run_c not in [send["run_id"] for send in transport.sends]
    assert run(store, room_c, run_c)["status"] == "needs_input"  # both lanes need reconciliation, so ask


def test_a_chat_shared_between_lanes_is_refused(tmp_path):
    # Review of 0c03bc2: a package holds its current chat while waiting for the next one, so lanes that share
    # chats can wait on each other until the time limit. One chat per lane removes that wait.
    from companion.team_transport import validate_config
    main, trees = repo_with_worktrees(tmp_path, 2)

    def config(claude_pins):
        lanes = [{"workspace": str(tree),
                  "participants": {"claude": claude_pins[i], "codex": {"id": f"fixture-codex-{i}", "cwd": str(tree)}}}
                 for i, tree in enumerate(trees)]
        return {"version": 3, "workspace": str(main), "scope": "Fixture engineering scope.", "lanes": lanes,
                "controller": {"task_id": TASK, "version": 1}}

    validate_config(config([{"id": f"fixture-claude-{i}", "cwd": str(tree)} for i, tree in enumerate(trees)]))
    shared = {"id": "fixture-claude-shared", "cwd": str(main)}
    with pytest.raises(ValueError):
        validate_config(config([shared, shared]))


def test_a_lane_whose_chat_was_closed_is_skipped_for_a_live_one(team, tmp_path):
    """2026-10-01: '@all' went to lane 1, whose Claude chat no longer existed, while lane 2 was live."""
    store, service, transport, _, lanes = bind_lanes(team, tmp_path, 2)
    dead = lanes[0]["participants"]["claude"]["id"]
    transport.participant_live = lambda config, lane, role: lane["participants"][role]["id"] != dead
    room_id, run_id = start(store, service, "Room A", "req-a")
    assert [send["run_id"] for send in transport.sends] == [run_id]
    assert run(store, room_id, run_id)["lane"]["workspace"] == lanes[1]["workspace"]
