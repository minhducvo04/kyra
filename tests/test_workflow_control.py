"""Workflow authority survives restart; observation checks stop when no work can progress."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from companion.workflow_control import WorkflowStore


def envelope(tmp_path):
    return {"task_ids": ["task-1"], "repo": str(tmp_path),
            "actions": ["open_web", "use_claude", "run_tests", "create_pr", "fix_ci", "merge_pr"],
            "chats": [{"app": "codex", "id": "chat-1"}, {"app": "claude", "id": "chat-2"}],
            "max_unknown_checks": 2, "max_checks": 5, "interval_seconds": 10,
            "expires_at": 1000, "max_repair_attempts": 2}


def observations(state="idle", at=100):
    return [{"app": app, "id": uid, "state": state, "observed_at": at}
            for app, uid in [("codex", "chat-1"), ("claude", "chat-2")]]


@pytest.fixture
def store(tmp_path):
    store = WorkflowStore(tmp_path / "control.json")
    store.configure(envelope(tmp_path), now=100)
    return store


def test_idle_stops_future_checks_and_restart_preserves_it(store):
    stopped = store.tick(observations(), now=100)
    assert stopped["status"] == "stopped_idle"
    assert stopped["next_check_at"] is None
    fresh = WorkflowStore(store.path)
    assert fresh.status() == stopped
    assert fresh.tick(observations("working", 110), now=110) == stopped


def test_blocked_work_stops_but_explicit_resume_can_continue(store):
    assert store.tick(observations("needs_input"), now=100)["status"] == "stopped_blocked"
    store.resume(now=110)
    running = store.tick(observations("thinking", 110), now=110)
    assert running["status"] == "running"
    assert running["next_check_at"] == 120


@pytest.mark.parametrize("queue", ["runnable_task_ids", "pending_ci_task_ids"])
def test_authorized_queue_or_ci_keeps_checking_with_idle_chats(store, queue):
    assert store.tick(observations(), **{queue: ["task-1"]}, now=100)["status"] == "running"


@pytest.mark.parametrize("queue", ["runnable_task_ids", "pending_ci_task_ids"])
def test_out_of_scope_task_cannot_keep_scheduler_alive(store, queue):
    with pytest.raises(ValueError):
        store.tick(observations(), **{queue: ["injected-task"]}, now=100)
    assert store.status()["checks"] == 0


def test_unknown_or_missing_status_stops_after_bounded_checks(store):
    assert store.tick([], now=100)["status"] == "running"
    assert store.tick([], now=110)["status"] == "suspended_unknown"
    assert store.status()["next_check_at"] is None


@pytest.mark.parametrize("at", [0, 101])
def test_stale_or_future_observation_is_unknown(store, at):
    assert store.tick(observations("working", at), now=100)["unknown_checks"] == 1


def test_unregistered_busy_chat_does_not_count(store):
    rows = observations() + [{"app": "cursor", "id": "other", "state": "working", "observed_at": 100}]
    assert store.tick(rows, now=100)["status"] == "stopped_idle"


def test_due_time_and_lock_prevent_duplicate_checks(store):
    def check(_):
        return WorkflowStore(store.path).tick(observations("working"), now=100)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, range(8)))
    assert all(result["checks"] == 1 for result in results)
    assert store.status()["next_check_at"] == 110


def test_check_budget_cannot_be_reset_by_resume(store):
    for now in range(100, 150, 10):
        store.tick(observations("working", now), now=now)
    assert store.status()["status"] == "exhausted"
    assert store.status()["next_check_at"] is None
    with pytest.raises(ValueError):
        store.resume(now=150)


def test_scope_authorization_survives_restart_and_stops_on_revoke(store, tmp_path):
    fresh = WorkflowStore(store.path)
    assert fresh.authorize("open_web", "task-1", str(tmp_path), now=100)["allowed"]
    assert not fresh.authorize("grant_os_permission", "task-1", str(tmp_path), now=100)["allowed"]
    assert not fresh.authorize("open_web", "other", str(tmp_path), now=100)["allowed"]
    assert not fresh.authorize("open_web", "task-1", str(tmp_path.parent), now=100)["allowed"]
    fresh.revoke()
    assert store.status()["next_check_at"] is None
    assert not store.authorize("open_web", "task-1", str(tmp_path), now=100)["allowed"]
    with pytest.raises(ValueError):
        store.resume(now=100)


def test_expiry_stops_checks_and_authorization(store, tmp_path):
    assert store.tick(observations("working", 1000), now=1000)["status"] == "expired"
    assert not store.authorize("use_claude", "task-1", str(tmp_path), now=1000)["allowed"]


def test_reconfigure_versions_scope_without_mutating_callers(store, tmp_path):
    policy = envelope(tmp_path)
    state = store.configure(policy, now=100)
    policy["task_ids"].append("injected")
    state["envelope"]["task_ids"].append("injected")
    assert store.status()["version"] == 2
    assert store.status()["envelope"]["task_ids"] == ["task-1"]


@pytest.mark.parametrize("field,value", [("max_checks", True), ("max_checks", 0),
    ("max_unknown_checks", 0), ("interval_seconds", float("nan")), ("expires_at", float("inf")),
    ("repo", "relative"), ("actions", ["grant_os_permission"]), ("task_ids", ["task-1", "task-1"]),
    ("chats", [{"app": "codex", "id": "x", "permission": "all"}]), ("extra", "injected")])
def test_bad_envelopes_fail_without_changing_saved_authority(store, tmp_path, field, value):
    original = store.status()
    policy = envelope(tmp_path)
    policy[field] = value
    with pytest.raises(ValueError):
        store.configure(policy, now=100)
    assert store.status() == original


def test_corrupt_state_fails_closed_even_during_reconfigure(store, tmp_path):
    store.path.write_text('{"status":"running"}')
    with pytest.raises(ValueError):
        store.configure(envelope(tmp_path), now=100)
    with pytest.raises(ValueError):
        store.authorize("open_web", "task-1", str(tmp_path), now=100)


def test_duplicate_observations_are_ambiguous_not_latest_wins(store):
    with pytest.raises(ValueError):
        store.tick(observations() + observations("working"), now=100)
    assert store.status()["checks"] == 0


def test_unknown_lane_does_not_stop_independent_active_lane(store):
    for now in (100, 110, 120):
        row = {"app": "codex", "id": "chat-1", "state": "working", "observed_at": now}
        result = store.tick([row], now=now)
        assert result["status"] == "running"
        assert result["unknown_checks"] == 0
        assert result["unknown_chats"] == [{"app": "claude", "id": "chat-2"}]


def test_ci_only_scope_needs_no_chat(store, tmp_path):
    policy = envelope(tmp_path)
    policy["chats"] = []
    store.configure(policy, now=100)
    assert store.tick([], pending_ci_task_ids=["task-1"], now=100)["status"] == "running"
    assert store.tick([], now=110)["status"] == "stopped_idle"


def test_idle_stop_keeps_standing_authorization(store, tmp_path):
    store.tick(observations(), now=100)
    assert store.authorize("use_claude", "task-1", str(tmp_path), now=110)["allowed"]


def test_expiry_overrides_future_due_time(store, tmp_path):
    policy = envelope(tmp_path)
    policy["expires_at"] = 105
    store.configure(policy, now=100)
    assert store.tick(observations("working"), now=100)["next_check_at"] == 105
    assert store.tick(observations("working", 105), now=105)["status"] == "expired"


def test_explicit_chat_cwd_does_not_expand_action_repository(store, tmp_path):
    policy = envelope(tmp_path)
    observation_repo = tmp_path / 'visible-app-repo'
    policy['chats'][0]['cwd'] = str(observation_repo)
    configured = store.configure(policy, now=100)
    assert configured['envelope']['chats'][0]['cwd'] == str(observation_repo)
    assert not store.authorize('run_tests', 'task-1', str(observation_repo), now=100)['allowed']
    assert store.tick(observations('thinking'), now=100)['status'] == 'running'


def test_relative_chat_cwd_is_rejected(store, tmp_path):
    policy = envelope(tmp_path)
    policy['chats'][0]['cwd'] = 'relative'
    with pytest.raises(ValueError):
        store.configure(policy, now=100)


def test_billing_or_setup_blocked_task_stops_with_actionable_reason(store):
    result = store.tick(observations(), blocked_task_ids=['task-1'], now=100)
    assert result['status'] == 'stopped_blocked'
    assert result['reason'] == 'needs_input_or_setup'
    assert result['next_check_at'] is None


def test_out_of_scope_blocked_task_is_rejected(store):
    with pytest.raises(ValueError):
        store.tick(observations(), blocked_task_ids=['outside-scope'], now=100)
    assert store.status()['checks'] == 0
