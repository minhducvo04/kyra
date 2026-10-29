"""Private PR gates use trusted evidence, current CI and bounded repair decisions."""
from dataclasses import replace

import pytest

from companion.workflow_pr import GitHubPRAdapter, PRPolicy, ReviewEvidence, next_action

HEAD = "a" * 40
POLICY = PRPolicy(repo="example/private")
EVIDENCE = (
    ReviewEvidence(HEAD, "claude", "review-session", "review", "approve"),
    ReviewEvidence(HEAD, "codex", "builder-session", "code", "complete"),
)


def snapshot(**changes):
    value = dict(repo="example/private", private=True, number=1, head_sha=HEAD, head_branch="session/test",
                 base="master", state="open", draft=False, mergeable=True,
                 workflow_head=HEAD, workflow_status="completed", workflow_conclusion="success",
                 files_complete=True, files_head=HEAD, protected_changes=[],
                 deployment_checked=True, deployment_enabled=False,
                 jobs=[{"name": name, "status": "completed", "conclusion": "success"}
                       for name in POLICY.expected_jobs], infrastructure_blocked=False)
    return {**value, **changes}


def decide(value=None, evidence=EVIDENCE, **kwargs):
    return next_action(value or snapshot(), POLICY, evidence,
                       author_session="builder-session", repair_attempts=0, **kwargs)


def test_independent_current_approval_and_codex_participation_allow_merge():
    assert decide()["action"] == "mergeable"


@pytest.mark.parametrize("change", [{"private": False}, {"repo": "other/repo"},
                                    {"base": "other"}, {"draft": True}, {"mergeable": False}])
def test_repository_and_pull_request_constraints_fail_closed(change):
    assert decide(snapshot(**change))["action"] == "blocked"


@pytest.mark.parametrize("evidence", [(), EVIDENCE[:1], EVIDENCE[1:],
    (replace(EVIDENCE[0], head_sha="b" * 40), EVIDENCE[1]),
    (replace(EVIDENCE[0], session_id="builder-session"), EVIDENCE[1]),
    (replace(EVIDENCE[0], verdict="reject"), EVIDENCE[1])])
def test_missing_stale_self_or_negative_review_cannot_merge(evidence):
    assert decide(evidence=evidence)["action"] == "needs_review"


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled", None])
def test_non_success_required_jobs_never_pass(conclusion):
    value = snapshot()
    value["jobs"][0]["conclusion"] = conclusion
    assert decide(value)["action"] != "mergeable"


def test_stale_missing_pending_or_duplicate_jobs_never_pass():
    assert decide(snapshot(workflow_head="b" * 40))["action"] == "pending"
    assert decide(snapshot(jobs=[]))["action"] != "mergeable"
    value = snapshot()
    value["jobs"][0]["status"] = "in_progress"
    assert decide(value)["action"] == "pending"
    value = snapshot()
    value["jobs"].append(value["jobs"][0].copy())
    assert decide(value)["action"] == "blocked"


def test_billing_failure_parks_without_spending_repair_attempt():
    value = snapshot(infrastructure_blocked=True, workflow_conclusion="failure")
    assert decide(value)["action"] == "blocked_infrastructure"


def test_failed_ci_only_repairs_with_remaining_budget():
    value = snapshot(workflow_conclusion="failure")
    value["jobs"][0]["conclusion"] = "failure"
    assert decide(value)["action"] == "fix"
    assert next_action(value, POLICY, EVIDENCE, author_session="builder-session",
                       repair_attempts=POLICY.max_repairs)["action"] == "blocked"


class Runner:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        return next(self.replies)


def test_merge_reobserves_head_and_passes_atomic_expected_head(monkeypatch):
    runner = Runner([""])
    granted = []
    adapter = GitHubPRAdapter(POLICY, lambda **kw: (granted.append(kw), True)[1], runner=runner)
    monkeypatch.setattr(adapter, "observe", lambda number: snapshot())
    adapter.merge("task", 1, EVIDENCE, author_session="builder-session", repair_attempts=0)
    assert granted == [{"action": "merge_pr", "task_id": "task", "repo": POLICY.repo}]
    command = runner.calls[0]
    assert command[command.index("--match-head-commit") + 1] == HEAD
    assert "--admin" not in command and "--auto" not in command


def test_merge_never_uses_old_evidence_after_head_changed(monkeypatch):
    runner = Runner([])
    adapter = GitHubPRAdapter(POLICY, lambda **kw: None, runner=runner)
    monkeypatch.setattr(adapter, "observe", lambda number: snapshot(head_sha="b" * 40, workflow_head="b" * 40, files_head="b" * 40))
    with pytest.raises(ValueError, match="needs_review"):
        adapter.merge("task", 1, EVIDENCE, author_session="builder-session", repair_attempts=0)
    assert runner.calls == []


def test_ensure_reuses_matching_open_pr_without_mutation(tmp_path):
    runner = Runner([{"private": True}, [{"number": 3, "url": "https://github.com/example/private/pull/3"}]])
    adapter = GitHubPRAdapter(POLICY, lambda **kw: pytest.fail("no authorization needed for reuse"), runner=runner)
    assert adapter.ensure_pr("task", branch="session/test", title="test", body_file=tmp_path / "body")["number"] == 3
    assert all("create" not in args for args in runner.calls)


def test_public_repository_and_denied_authorization_never_create_pr(tmp_path):
    for replies in ([{"private": False}], [{"private": True}, []]):
        runner = Runner(replies)
        def deny(**kwargs):
            raise PermissionError("not granted")
        adapter = GitHubPRAdapter(POLICY, deny, runner=runner)
        with pytest.raises((ValueError, PermissionError)):
            adapter.ensure_pr("task", branch="session/test", title="test", body_file=tmp_path / "body")
        assert all("create" not in args for args in runner.calls)


def test_creation_requires_task_branch_prefix(tmp_path):
    adapter = GitHubPRAdapter(POLICY, lambda **kw: None, runner=Runner([]))
    with pytest.raises(ValueError, match="invalid_pr_request"):
        adapter.ensure_pr("task", branch="unrelated", title="test", body_file=tmp_path / "body")


def test_explicit_false_authorization_never_merges(monkeypatch):
    runner = Runner([])
    adapter = GitHubPRAdapter(POLICY, lambda **kw: False, runner=runner)
    monkeypatch.setattr(adapter, "observe", lambda number: snapshot())
    with pytest.raises(PermissionError):
        adapter.merge("task", 1, EVIDENCE, author_session="builder-session", repair_attempts=0)
    assert not runner.calls


def observed_replies(*, latest_conclusion="failure", annotations=None):
    replies = [
        {"private": True},
        {"head": {"sha": HEAD, "ref": "session/test", "repo": {"full_name": POLICY.repo}},
         "base": {"ref": "master"}, "state": "open", "draft": False, "mergeable": True},
        {"workflow_runs": [
            {"id": 1, "head_sha": HEAD, "event": "pull_request", "path": POLICY.workflow_path,
             "pull_requests": [{"number": 1}], "status": "completed", "conclusion": "success"},
            {"id": 2, "head_sha": HEAD, "event": "pull_request", "path": POLICY.workflow_path,
             "pull_requests": [{"number": 1}], "status": "completed", "conclusion": latest_conclusion},
            {"id": 3, "head_sha": HEAD, "event": "push", "path": POLICY.workflow_path,
             "pull_requests": [{"number": 1}], "status": "completed", "conclusion": "success"}]},
        {"total_count": 1, "jobs": [{"name": "test", "status": "completed", "conclusion": latest_conclusion,
                                     "check_run_url": "https://api.github.com/repos/example/private/check-runs/6"}]},
        annotations or [],
    ]
    replies[2:2] = [[{"filename": "src/companion/feature.py"}], dict(replies[1]),
                    {"total_count": 0, "names": []}]
    return replies


def test_observer_uses_latest_matching_run_and_classifies_billing():
    runner = Runner(observed_replies(annotations=[{"message": "The job was not started because recent account payments have failed."}]))
    adapter = GitHubPRAdapter(POLICY, lambda **kw: None, runner=runner)
    value = adapter.observe(1)
    assert value["workflow_run_id"] == 2
    assert next_action(value, POLICY, EVIDENCE, author_session="builder-session", repair_attempts=0)["action"] == "blocked_infrastructure"
    assert any("/actions/runs/2/jobs" in args[-1] for args in runner.calls)
    assert "payments" not in str(value)  # Return the classification, not raw account details.


def test_observer_does_not_treat_test_failure_as_infrastructure():
    adapter = GitHubPRAdapter(POLICY, lambda **kw: None,
                              runner=Runner(observed_replies(annotations=[{"message": "Assertion failed"}])))
    assert adapter.observe(1)["infrastructure_blocked"] is False


@pytest.mark.parametrize("response", [None, {}, {"allowed": False}, 0, 1, "yes"])
def test_only_explicit_true_permission_can_merge(monkeypatch, response):
    runner = Runner([])
    adapter = GitHubPRAdapter(POLICY, lambda **kw: response, runner=runner)
    monkeypatch.setattr(adapter, "observe", lambda number: snapshot())
    with pytest.raises(PermissionError):
        adapter.merge("task", 1, EVIDENCE, author_session="builder-session", repair_attempts=0)
    assert not runner.calls


def test_merge_gate_rejects_non_task_branch_even_for_existing_pr():
    assert decide(snapshot(head_branch="master"))["action"] == "blocked"


def test_bool_is_not_a_repair_count_and_unknown_roles_cannot_hide_rejection():
    assert next_action(snapshot(), POLICY, EVIDENCE, author_session="builder-session", repair_attempts=True)["action"] == "blocked"
    rejected = (*EVIDENCE, ReviewEvidence(HEAD, "claude", "another-session", "unknown", "reject"))
    assert decide(evidence=rejected)["action"] == "needs_review"


@pytest.mark.parametrize("kwargs", [{"max_repairs": True}, {"max_repairs": 1.5},
    {"max_repairs": 1000}, {"expected_jobs": (1,)}, {"expected_jobs": ("",)}])
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        PRPolicy(repo="example/private", **kwargs)


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "src/companion/workflow_pr.py",
    "tests/test_workflow_acceptance.py", "AGENTS.md", ".claude/skills/agent-coordination/SKILL.md"])
def test_protected_file_inventory_blocks_ordinary_automerge(path):
    value = snapshot(protected_changes=[path])
    assert decide(value)["action"] == "blocked_policy"


def test_missing_or_stale_file_inventory_cannot_authorize_merge():
    assert decide(snapshot(files_complete=False))["action"] == "blocked_policy"
    assert decide(snapshot(files_head="b" * 40))["action"] == "blocked_policy"


def test_rename_out_of_protected_path_is_still_protected():
    replies = observed_replies()
    replies[2] = [{"filename": "src/companion/feature.py", "previous_filename": "src/companion/workflow_pr.py"}]
    adapter = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies))
    assert "src/companion/workflow_pr.py" in adapter.observe(1)["protected_changes"]


def test_full_file_page_without_proven_end_fails_closed():
    replies = observed_replies()
    replies[2] = [{"filename": f"src/feature{i}.py"} for i in range(100)]
    adapter = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies))
    assert adapter.observe(1)["files_complete"] is False


@pytest.mark.parametrize("changes", [{"deployment_enabled": True}, {"deployment_checked": False}])
def test_deployment_requires_separate_authorization(changes):
    assert decide(snapshot(**changes))["action"] == "blocked_deployment"


@pytest.mark.parametrize("path", ["src/companion/paths.py", "src/companion/settings.py",
    "src/companion/__init__.py", "requirements.txt", "requirements-dev.txt", "requirements-web.txt",
    "tests/__init__.py", "setup.py"])
def test_direct_controller_dependencies_cannot_change_under_ordinary_automerge(path):
    replies = observed_replies()
    replies[2] = [{"filename": path}]
    value = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies)).observe(1)
    assert decide(value)["action"] == "blocked_policy"


def test_ordinary_feature_test_edits_still_reach_the_normal_ci_decision():
    replies = observed_replies()
    replies[2] = [{"filename": "tests/test_learning.py"}]
    value = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies)).observe(1)
    assert value["protected_changes"] == []
    assert decide(value)["action"] == "fix"  # Fixture contains a real failed CI job.


@pytest.mark.parametrize("path", ["docs/permissions.md", "docs/team-flow.md"])
def test_the_permission_list_and_team_flow_cannot_change_under_ordinary_automerge(path):
    # AGENTS.md rule 9: an agent never widens its own authority, so these need the owner yes, not an automatic merge.
    replies = observed_replies()
    replies[2] = [{"filename": path}]
    value = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies)).observe(1)
    assert decide(value)["action"] == "blocked_policy"


@pytest.mark.parametrize("path", ["scripts/deploy_local.py", "tests/test_deploy_local.py",
                                  "tests/test_deploy_local_builder.py"])
def test_the_local_deploy_gate_cannot_change_under_ordinary_automerge(path):
    # The deploy script refuses protected changes, so it must be protected itself, or an agent could weaken the gate.
    replies = observed_replies()
    replies[2] = [{"filename": path}]
    value = GitHubPRAdapter(POLICY, lambda **kw: True, runner=Runner(replies)).observe(1)
    assert decide(value)["action"] == "blocked_policy"
