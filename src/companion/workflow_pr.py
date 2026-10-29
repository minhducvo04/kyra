"""Private PR coordination; the caller authenticates review evidence and owns retry state.

This gate is not GitHub branch protection. Never feed it candidate-authored JSON as
trusted evidence. The supervisor must collect provider/session/commit attestations
and protect this policy and the CI definition from unreviewed candidate changes.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PRPolicy:
    repo: str
    base: str = "master"
    expected_jobs: tuple[str, ...] = ("test", "visionos", "docker", "terraform")
    workflow_path: str = ".github/workflows/ci.yml"
    max_repairs: int = 2

    def __post_init__(self):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo)
                or not _ref(self.base) or not self.expected_jobs
                or any(not isinstance(job, str) or not job.strip() for job in self.expected_jobs)
                or len(set(self.expected_jobs)) != len(self.expected_jobs)
                or type(self.max_repairs) is not int or not 0 <= self.max_repairs <= 20):
            raise ValueError("invalid_pr_policy")


@dataclass(frozen=True)
class ReviewEvidence:
    head_sha: str
    provider: str
    session_id: str
    role: str
    verdict: str


def _ref(value):
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value)
            and ".." not in value and not value.endswith(("/", ".", ".lock")))


def _protected_path(path):
    # These files define the gate, its inputs, or its tests. Feature tests remain editable.
    return (path.startswith((".github/", "src/companion/workflow_", "tests/test_workflow_",
                             "scripts/workflow_", "web/workflow."))
            or path.rsplit("/", 1)[-1] in {"AGENTS.md", "CLAUDE.md", ".gitignore"}
            or path in {"pyproject.toml", "pytest.ini", "setup.cfg", "setup.py",
                        "requirements.txt", "requirements-dev.txt", "requirements-web.txt",
                        "conftest.py", "tests/conftest.py", "tests/__init__.py",
                        "tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py",
                        "src/companion/__init__.py", "src/companion/app_tasks.py",
                        "src/companion/paths.py", "src/companion/settings.py",
                        "docs/standing-rules.md", "docs/agent-workflow.md", "docs/skills.md",
                        "docs/permissions.md", "docs/team-flow.md",
                        "scripts/deploy_local.py", "tests/test_deploy_local.py", "tests/test_deploy_local_builder.py"}
            or any(path.startswith(root + skill + "/")
                   for root in (".claude/skills/", ".agents/skills/", ".codex/skills/")
                   for skill in ("agent-coordination", "test-impact", "private-check",
                                 "code-review-and-quality", "test-driven-development")))


def next_action(snapshot, policy, evidence, *, author_session, repair_attempts):
    """Choose a bounded next step. No action is executed and no prose is parsed."""
    def result(action, reason):
        return {"action": action, "reasons": [reason] if reason else []}

    if (snapshot.get("repo") != policy.repo or snapshot.get("private") is not True
            or snapshot.get("base") != policy.base or snapshot.get("state") != "open"
            or not _ref(snapshot.get("head_branch"))
            or not snapshot.get("head_branch", "").startswith(("session/", "codex/"))
            or snapshot.get("draft") is not False or snapshot.get("mergeable") is False
            or not re.fullmatch(r"[a-f0-9]{40}", snapshot.get("head_sha", ""))
            or type(repair_attempts) is not int or repair_attempts < 0):
        return result("blocked", "repository_or_pr_constraint")
    if snapshot.get("infrastructure_blocked"):
        return result("blocked_infrastructure", "runner_or_billing_unavailable")
    head = snapshot["head_sha"]
    if (snapshot.get("files_complete") is not True or snapshot.get("files_head") != head
            or snapshot.get("protected_changes")):
        return result("blocked_policy", "protected_policy_change_or_incomplete_inventory")
    if snapshot.get("deployment_checked") is not True or snapshot.get("deployment_enabled") is not False:
        return result("blocked_deployment", "deployment_scope_not_authorized_or_unknown")
    if snapshot.get("workflow_head") != head or snapshot.get("workflow_status") != "completed":
        return result("pending", "current_ci_pending")
    jobs = snapshot.get("jobs", [])
    expected = [job for job in jobs if job.get("name") in policy.expected_jobs]
    names = [job.get("name") for job in expected]
    if len(set(names)) != len(names):
        return result("blocked", "ambiguous_ci_jobs")
    if any(job.get("status") != "completed" for job in expected):
        return result("pending", "current_ci_pending")
    if any(job.get("conclusion") == "failure" for job in expected):
        return result("fix" if repair_attempts < policy.max_repairs else "blocked",
                      "ci_failure" if repair_attempts < policy.max_repairs else "repair_budget_exhausted")
    if (set(names) != set(policy.expected_jobs)
            or any(job.get("conclusion") != "success" for job in expected)
            or snapshot.get("workflow_conclusion") != "success"):
        return result("blocked", "required_ci_not_successful")
    current = [item for item in evidence if item.head_sha == head and item.session_id]
    if (not author_session
            or any(item.verdict == "reject" for item in current)
            or not any(item.provider == "claude" and item.role == "review" and item.verdict == "approve"
                       and item.session_id != author_session for item in current)
            or not any(item.provider == "codex" and item.role in {"code", "supervise"}
                       and item.verdict == "complete" for item in current)):
        return result("needs_review", "independent_current_evidence_required")
    if snapshot.get("mergeable") is not True:
        return result("pending", "mergeability_unknown")
    return result("mergeable", "")


def _run_gh(args):
    """No shell, no inherited command strings, bounded wait; do not return raw errors."""
    try:
        process = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError("github_outcome_unknown") from None
    if process.returncode:
        raise RuntimeError("github_command_failed")
    if "api" in args or "--json" in args:
        try:
            return json.loads(process.stdout)
        except ValueError:
            raise RuntimeError("github_response_invalid") from None
    return process.stdout.strip()


class GitHubPRAdapter:
    """Supported gh CLI adapter. Authorization must return the boolean True to permit an action.

    observe is read-only. Mutators check the repository again and call authorization
    at execution time. Callers must serialize create/merge actions per PR/task and
    persist repair counts before dispatching repairs; failed/unknown writes are not
    blindly retried. A GitHub head compare prevents a concurrent changed-head merge;
    GitHub protection/merge queues are needed for stronger server-side guarantees.
    """
    def __init__(self, policy, authorize, *, runner=_run_gh):
        self.policy, self.authorize, self.runner = policy, authorize, runner

    def _api(self, suffix):
        return self.runner(["gh", "api", f"repos/{self.policy.repo}{suffix}"])

    def _grant(self, action, task_id):
        if self.authorize(action=action, task_id=task_id, repo=self.policy.repo) is not True:
            raise PermissionError("action_not_authorized")

    def ensure_pr(self, task_id, *, branch, title, body_file):
        if (not _ref(branch) or not branch.startswith(("session/", "codex/"))
                or branch == self.policy.base or not isinstance(title, str) or not title.strip()):
            raise ValueError("invalid_pr_request")
        if self._api("").get("private") is not True:
            raise ValueError("private_repository_required")
        existing = self.runner(["gh", "pr", "list", "--repo", self.policy.repo, "--state", "open",
                                "--head", branch, "--base", self.policy.base, "--json", "number,url"])
        if len(existing) > 1:
            raise ValueError("ambiguous_existing_pr")
        if existing:
            return existing[0]
        self._grant("create_pr", task_id)
        body_file = Path(body_file)
        if not body_file.is_file():
            raise ValueError("pr_body_file_missing")
        self.runner(["gh", "pr", "create", "--repo", self.policy.repo, "--base", self.policy.base,
                     "--head", branch, "--title", title, "--body-file", str(body_file)])
        created = self.runner(["gh", "pr", "list", "--repo", self.policy.repo, "--state", "open",
                               "--head", branch, "--base", self.policy.base, "--json", "number,url"])
        if len(created) != 1:
            raise RuntimeError("pr_creation_unconfirmed")
        return created[0]

    def observe(self, number):
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError("invalid_pr_number")
        repo, pr = self._api(""), self._api(f"/pulls/{number}")
        head = pr["head"]["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", head):
            raise ValueError("invalid_remote_head")
        value = dict(repo=self.policy.repo, private=repo.get("private") is True, number=number,
                     head_sha=head, head_branch=pr["head"]["ref"], base=pr["base"]["ref"],
                     state=pr["state"], draft=pr["draft"], merged=pr.get("merged", False),
                     mergeable=pr.get("mergeable"), workflow_head=None, workflow_status=None,
                     workflow_conclusion=None, jobs=[], infrastructure_blocked=False,
                     files_complete=False, files_head=None, protected_changes=[],
                     deployment_checked=False, deployment_enabled=None)
        if pr["head"].get("repo", {}).get("full_name") != self.policy.repo:
            value["private"] = False  # Fork input is outside this private, same-repo policy.
            return value
        files = self._api(f"/pulls/{number}/files?per_page=100")
        confirmed = self._api(f"/pulls/{number}")
        valid = (isinstance(files, list) and len(files) <= 100
                 and all(isinstance(item, dict) and isinstance(item.get("filename"), str)
                         and item["filename"] and ("previous_filename" not in item
                         or isinstance(item["previous_filename"], str)) for item in files))
        reported = pr.get("changed_files")
        complete = (valid and (len(files) < 100 or reported == len(files))
                    and (reported is None or type(reported) is int and reported == len(files))
                    and confirmed["head"]["sha"] == head and confirmed["base"] == pr["base"])
        paths = ({name for item in files for name in (item["filename"], item.get("previous_filename")) if name}
                 if valid else set())
        value.update(files_complete=complete, files_head=head if complete else None,
                     protected_changes=sorted(path for path in paths if _protected_path(path)))
        try:
            # gh filters before returning output; variable values never enter the snapshot or Python result.
            variables = self.runner(["gh", "api", f"repos/{self.policy.repo}/actions/variables?per_page=100",
                                     "--jq", "{total_count, names: [.variables[].name]}"])
            names = variables.get("names")
            if (type(variables.get("total_count")) is int and isinstance(names, list)
                    and variables["total_count"] == len(names) <= 100
                    and all(isinstance(name, str) for name in names)):
                value.update(deployment_checked=True, deployment_enabled="AWS_ROLE_ARN" in names)
        except (RuntimeError, OSError):
            pass  # Unknown deployment configuration is a distinct fail-closed merge gate.
        runs = self._api(f"/actions/runs?head_sha={head}&event=pull_request&per_page=100")["workflow_runs"]
        matching = [run for run in runs if run.get("head_sha") == head
                    and run.get("event") == "pull_request" and run.get("path") == self.policy.workflow_path
                    and any(p.get("number") == number for p in run.get("pull_requests", []))]
        if not matching:
            return value
        run = max(matching, key=lambda item: item["id"])
        value.update(workflow_head=run["head_sha"], workflow_status=run["status"],
                     workflow_conclusion=run.get("conclusion"), workflow_run_id=run["id"],
                     workflow_attempt=run.get("run_attempt"))
        response = self._api(f"/actions/runs/{int(run['id'])}/jobs?filter=latest&per_page=100")
        if response.get("total_count", 0) > 100:
            raise ValueError("too_many_ci_jobs")
        value["jobs"] = [{"name": j["name"], "status": j["status"], "conclusion": j.get("conclusion")}
                         for j in response["jobs"]]
        for job in response["jobs"]:
            if job.get("conclusion") != "failure":
                continue
            # Only use a numeric check ID from the returned URL, never follow its host.
            check_id = str(job.get("check_run_url", "")).rsplit("/", 1)[-1]
            if not check_id.isdigit():
                continue
            annotations = self._api(f"/check-runs/{check_id}/annotations?per_page=100")
            for annotation in annotations:
                message = str(annotation.get("message", "")).lower()
                if any(text in message for text in ("recent account payments have failed", "spending limit",
                                                     "no hosted runner", "runner is unavailable")):
                    value["infrastructure_blocked"] = True
        return value

    def merge(self, task_id, number, evidence, *, author_session, repair_attempts):
        snapshot = self.observe(number)
        decision = next_action(snapshot, self.policy, evidence, author_session=author_session,
                               repair_attempts=repair_attempts)
        if decision["action"] != "mergeable":
            raise ValueError(decision["action"] + ":" + ",".join(decision["reasons"]))
        self._grant("merge_pr", task_id)
        self.runner(["gh", "pr", "merge", str(number), "--repo", self.policy.repo, "--squash",
                     "--match-head-commit", snapshot["head_sha"]])
        return {"action": "merge_requested", "number": number, "head_sha": snapshot["head_sha"]}
