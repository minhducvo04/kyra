"""W0: execute one durable node run, with a fresh channel invocation per attempt.

``root`` is a data directory, not team/loop. No sends happen in dry-run mode.
B0 can replace render_brief(run); R0 owns all lifecycle writes and E0 stores
their events. Each channel starts fresh; unavailable or gated channels fail
closed instead of silently reusing an app conversation.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from companion.feature_team import contract, dag, repos, runs
from companion.feature_team.events import EventStore

REPO = Path(__file__).resolve().parents[3]
POLL_SECONDS = 30
HEARTBEAT_SECONDS = 300
RECEIPT_TIMEOUT = 60


class ChannelUnavailable(ValueError):
    """A transport cannot meet the fresh-context or folder contract."""


# Worktree-bound channels (2026-10-15): the Claude CLI lane denies Read, Edit and Bash on every path under data/ and
# outside its worktree, and codex exec's sandbox writes only inside the worktree. A brief for these channels keeps
# every path it names inside the worktree: inputs are staged into .lane-send/inputs/, the result is written to
# .lane-send/<run_id>.result.json, the rules file is the worktree's own copy; the node worker copies the accepted
# result (and a plan's artifacts) out to data/ afterwards. Both PLAN runs of the first FL window finished in minutes
# and could not write their output; nothing was harvested and the runs timed out 90 minutes later.
# grok and muse joined 2026-10-15: G2's OS wall (companion.worker_sandbox) denies them data/ and .env too, so
# their result, rules and inputs must sit inside the worktree as well.
WORKTREE_BOUND_KINDS = ("claude", "codex-exec", "grok", "muse")
PLAN_FOLDER = "team/feature-teams"


def _worktree_bound(brief):
    return brief["channel"].partition(":")[0] in WORKTREE_BOUND_KINDS or brief.get("kind") == "plan"


def bind_to_worktree(brief, root, repo):
    """(brief, staging): the brief with every path inside its worktree, and {destination: source} copies to make
    once the worktree exists. Untouched for channels that can reach the shared paths."""
    if not _worktree_bound(brief):
        return brief, {}
    worktree = Path(brief["worktree"])
    staging, bound = {}, dict(brief)
    inputs = []
    for row in brief["inputs"]:
        dest = worktree / ".lane-send" / "inputs" / f"{row['node']}.result.json"
        staging[str(dest)] = str(_path(row["result"], root, repo))
        inputs.append({"node": row["node"], "result": str(dest)})
    bound["inputs"] = inputs
    bound["result"] = str(worktree / ".lane-send" / f"{brief['run_id']}.result.json")
    rules = _path(brief["rules"], root, repo)
    if rules.name == "AGENTS.md" and repos.for_path(repo).is_kyra:
        bound["rules"] = str(worktree / "AGENTS.md")  # the worktree's own copy of the rulebook
    elif rules.name == "AGENTS.md":  # another repo has no copy of Kyra's rulebook: it goes in with the brief
        dest = worktree / ".lane-send" / "AGENTS.md"
        staging[str(dest)] = str(rules)
        bound["rules"] = str(dest)
    else:
        dest = worktree / ".lane-send" / "rules.json"
        staging[str(dest)] = str(rules)
        bound["rules"] = str(dest)
    return contract.validate_brief(bound), staging


def path_blocker(brief):
    """A path the channel's own rules deny (outside the worktree, or under data/) must refuse the dispatch as a
    contract refusal instead of being sent: a retry would only repeat it."""
    if not _worktree_bound(brief):
        return None
    worktree = Path(brief["worktree"]).resolve()
    named = [*(("files", item) for item in brief["files"]), ("result", brief["result"]), ("rules", brief["rules"]),
             *(("inputs", row["result"]) for row in brief["inputs"])]
    for field, item in named:
        text = item[4:] if item.startswith("NEW ") else item
        path = Path(text.split(" (")[0].strip())
        if "data" in path.parts and not (path.is_absolute() and path.resolve().is_relative_to(worktree)):
            return f"brief names a path the channel cannot write in {field}: {text}"
        if path.is_absolute() and not path.resolve().is_relative_to(worktree):
            return f"brief names a path the channel cannot write in {field}: {text}"
    return None


def stage_inputs(staging):
    for dest, src in staging.items():
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


def commit_plan(brief, value):
    """Commit a done plan's worktree artifacts through the commit gate (privacy guards on the staged candidate) and
    return the sha. The Claude CLI channel denies pytest, so a worker that follows AGENTS.md rule 5 cannot commit:
    the site PLAN rehearsal of 2026-10-15 returned a done result with sha null and verify refuses a result without
    one. Verify still runs the guards at this sha."""
    worktree = Path(brief["worktree"])
    names = []
    for item in value["artifacts"]:
        path = Path(item) if Path(item).is_absolute() else worktree / item
        if not path.is_file() or not path.resolve().is_relative_to(worktree.resolve()):
            raise ValueError(f"result: plan artifact is not a file in the worktree: {item}")
        names.append(str(path.resolve().relative_to(worktree.resolve())))
    done = subprocess.run([sys.executable, str(REPO / "scripts/commit_gate.py"), "-m",
                           f"{brief['feature']} {brief['node']}: plan artifacts of run {brief['run_id']} (committed by the "
                           "node worker through the commit gate; the worker's channel cannot run pytest)", *names],
                          cwd=worktree, capture_output=True, text=True, timeout=600)
    if done.returncode:
        raise ValueError("result: commit gate refused the plan artifacts: "
                         + " ".join((done.stdout + done.stderr).split())[-300:])
    return subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def _coordinator_build(brief):
    return brief["channel"].partition(":")[0] in {"grok", "muse"} and brief.get("kind") != "plan"


class BuildReceipt(str):
    """A local receipt retaining the actual supervisor, including its wait status."""

    def __new__(cls, text, process):
        receipt = super().__new__(cls, text)
        receipt.process = process
        return receipt


def _build_identity(worktree):
    def git(*args):
        value = _git_out("-C", str(worktree), *args)
        if value is None:
            raise ValueError("build handoff: cannot read Git custody")
        return value

    return (git("rev-parse", "--show-toplevel"),
            git("rev-parse", "--path-format=absolute", "--git-common-dir"),
            git("branch", "--show-current"), git("rev-parse", "HEAD"))


def _build_changes(worktree):
    changed = set()
    for args in (("diff", "--name-only", "--no-renames", "-z", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        value = _git_out("-C", str(worktree), *args)
        if value is None:
            raise ValueError("build handoff: cannot read the candidate")
        changed.update(p for p in value.split("\0") if p and not p.startswith(".lane-send/"))
    return changed


def _build_path(worktree, item):
    path = Path(item[4:] if item.startswith("NEW ") else item)
    path = path if path.is_absolute() else worktree / path
    if not path.is_relative_to(worktree) or ".." in path.parts:
        raise ValueError("build handoff: candidate path is outside the assigned worktree")
    relative = path.relative_to(worktree)
    if (not relative.parts or relative.parts[0] in {".git", ".lane-send"}
            or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(worktree))
            or path.is_dir()):
        raise ValueError("build handoff: candidate must name exact files, without links or courier state")
    return relative.as_posix()


def _commit_gate():
    spec = importlib.util.spec_from_file_location("_node_worker_commit_gate", REPO / "scripts/commit_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commit_build(brief, value, receipt, custody, log_path):
    """Accept only this terminal worker's owned candidate; the existing gate checks and commits its exact tree."""
    worktree = Path(brief["worktree"]).resolve()
    process = getattr(receipt, "process", None)
    if (process is None or str(process.pid) != _receipt_field(receipt, "pid")
            or not _receipt_id(receipt) or process.poll() != 0):
        raise ValueError("build handoff: successful supervisor exit is not proven")
    if custody is None or _build_identity(worktree) != custody or value["branch"] != custody[2]:
        raise ValueError("build handoff: worktree, repository, branch or base changed")
    if _git_out("-C", str(worktree), "diff", "--cached", "--name-only") != "":
        raise ValueError("build handoff: worker left a staged candidate")
    owned = {_build_path(worktree, item) for item in brief["files"]}
    artifacts = {_build_path(worktree, item) for item in value["artifacts"]}
    changed = _build_changes(worktree)
    if not changed or not changed <= owned or artifacts != changed:
        raise ValueError("build handoff: changed files must be owned and exactly match result artifacts")
    names = sorted(changed)
    gate = _commit_gate()  # The coordinator's trusted copy, never the worker's scripts/commit_gate.py.
    try:
        gate.check_paths(worktree, names)
        gate.strip_markdown_whitespace(worktree, names)  # The gate's existing normalization, before binding bytes.
    except gate.Refused as exc:
        raise ValueError(f"build handoff: commit gate refused: {exc}") from None

    def snapshot():
        stamps = []
        for name in names:
            _build_path(worktree, name)
            path = worktree / name
            stamps.append((name, path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
                          if path.exists() else (name, None, None))
        return stamps

    candidate = snapshot()
    lines = [json.dumps({"run_id": brief["run_id"], "ledger_id": _receipt_id(receipt), "pid": process.pid,
                         "custody": custody, "candidate": candidate})]

    def checked(message):
        lines.append(message)
        # The existing gate calls say after lint and after the staged tests, before git commit.
        # An owned filename is insufficient: neither its bytes nor custody may change during those checks.
        if not message.startswith("committed ") and (
                _build_identity(worktree) != custody or _build_changes(worktree) != changed or snapshot() != candidate):
            raise gate.Refused("build candidate or custody changed during checks")

    try:
        committed = gate.gate(worktree, names, f"{brief['feature']} {brief['node']}: run {brief['run_id']} "
                              "(coordinator commit; commit gate checks passed)", say=checked)
    except gate.Refused as exc:
        lines.append(f"refused: {exc}")
        raise ValueError(f"build handoff: commit gate refused: {exc}") from None
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Bind to the gate's commit receipt, never whatever HEAD happens to name later.
    sha = (_git_out("-C", str(worktree), "rev-parse", "--verify", f"{committed}^{{commit}}")
           if committed else None)
    if not sha or _build_identity(worktree) != (*custody[:3], sha):
        raise ValueError("build handoff: gate receipt does not bind the current candidate")
    if _git_out("-C", str(worktree), "rev-list", "--parents", "-n", "1", sha) != f"{sha} {custody[3]}":
        raise ValueError("build handoff: committed candidate has a foreign parent")
    paths = _git_out("-C", str(worktree), "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", sha)
    if paths is None or set(filter(None, paths.split("\0"))) != changed or _build_changes(worktree):
        raise ValueError("build handoff: committed candidate differs from the owned files")
    # The gate stages before testing worktree bytes. Bind the immutable commit's blobs as well:
    # a writer can restore the tested bytes after git add, then leave a clean but different committed tree.
    tree = subprocess.run(["git", "-C", str(worktree), "ls-tree", "-r", "-z", sha, "--", *names],
                          capture_output=True, check=False)
    if tree.returncode:
        raise ValueError("build handoff: cannot read committed candidate blobs")
    entries = {}
    for row in tree.stdout.split(b"\0"):
        if row:
            header, name = row.split(b"\t", 1)
            entries[os.fsdecode(name)] = header.split()
    for name, mode, digest in candidate:
        entry = entries.get(name)
        if mode is None and entry is None:  # A snapshotted deletion must also be absent in the commit.
            continue
        expected_mode = b"100755" if mode is not None and mode & 0o111 else b"100644"
        if mode is None or entry is None or entry[:2] != [expected_mode, b"blob"]:
            raise ValueError("build handoff: committed blobs differ from the bound candidate")
        blob = subprocess.run(["git", "-C", str(worktree), "cat-file", "blob", entry[2].decode("ascii")],
                              capture_output=True, check=False)
        if blob.returncode or hashlib.sha256(blob.stdout).hexdigest() != digest:
            raise ValueError("build handoff: committed blobs differ from the bound candidate")
    return sha


def publish_result(brief, value, result_path, root):
    """After acceptance: a plan's artifacts are copied from the worktree into data/team/feature-teams
    (PLAN-<f>.md as <f>-plan.md, the DAG as <f>-proposed.dag.json, anything else by its name) and the result's
    artifact paths are rewritten to those destinations; a worktree-bound result is copied to
    data/team/loop/results/<run_id>.json, which the record points at from then on. Returns (path, value)."""
    worktree = Path(brief["worktree"])
    if brief.get("kind") == "plan" and value["status"] == "done" and not value["sha"]:
        value = dict(value, sha=commit_plan(brief, value))
    if brief.get("kind") == "plan":
        folder = Path(root) / PLAN_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        published = []
        for item in value["artifacts"]:
            src = Path(item) if Path(item).is_absolute() else worktree / item
            if not src.is_file():
                raise ValueError(f"result: plan artifact is not a file in the worktree: {item}")
            name = src.name
            if name == f"PLAN-{brief['feature']}.md":
                name = f"{brief['feature']}-plan.md"
            dest = folder / name
            shutil.copyfile(src, dest)
            published.append(str(dest))
        value = dict(value, artifacts=published)
    if _worktree_bound(brief):
        copy = Path(root) / "team" / "loop" / "results" / f"{brief['run_id']}.json"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(json.dumps(contract.validate_result(value), indent=1) + "\n", encoding="utf-8")
        return copy, value
    return result_path, value


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _final_text(log):
    """The worker's last assistant text from the courier's JSONL log, for a failed result's record."""
    texts = []
    try:
        for line in Path(log).read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "assistant":
                for part in (row.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        texts.append(part["text"])
            elif isinstance(row, dict) and row.get("type") == "result" and isinstance(row.get("result"), str):
                texts.append(row["result"])
    except OSError:
        return ""
    return texts[-1] if texts else ""


# A worker that ends within LAUNCH_FAILURE_SECONDS of its start with an empty log never ran the brief: a launch failure
# (a CLI that could not start, a denied tool at once), dead at start and counted against its channel, not an invalid
# result that earns a correction attempt on the same channel (3d-inner-demo G1, 2026-10-16: claude:opus wrote a
# blocked result in 2.5 min with a 0-byte log). The timeout's detail starts with LAUNCH_FAILURE; the dispatcher's loop
# tick leaves such a run to the inspector, which requeues it on another channel.
LAUNCH_FAILURE = "launch failure"
LAUNCH_FAILURE_SECONDS = 5 * 60


def launch_failure(receipt, elapsed):
    """The worker log the receipt names, when the worker's recorded pid is gone (proof it EXITED), its log exists
    and is empty, and the attempt is under five minutes old. The claude and codex-exec receipts carry pid=; the
    receipts send_channel returns for cursor, grok and muse carry none (their pid is only in the ledger row, and
    cursor's log stays empty until exit). A receipt with no pid, or a live pid, is not a launch failure: a CLI that
    buffers its output may write an early invalid result while still running, and it keeps its correction (review
    o-rev85c)."""
    log = _receipt_field(receipt, "log")
    pid = _receipt_field(receipt, "pid")
    if not log or not pid or _alive(pid) or elapsed > LAUNCH_FAILURE_SECONDS:
        return None
    try:
        return log if Path(log).stat().st_size == 0 else None
    except OSError:
        return None


def is_launch_failure(rec):
    return str(((rec.get("history") or [{}])[-1]).get("detail", "")).startswith(LAUNCH_FAILURE)


def failed_result(brief, receipt, now):
    """A worker process that exited without writing its result: the run fails now, with the worker's final words,
    instead of waiting for the deadline."""
    log = _receipt_field(receipt, "log") or "none"
    text = " ".join(_final_text(log).split())[-500:] if log != "none" else ""
    return contract.validate_result({
        "run_id": brief["run_id"], "feature": brief["feature"], "node": brief["node"], "worker": brief["model"],
        "sha": None, "branch": f"session/{Path(brief['worktree']).name}", "status": "failed", "artifacts": [],
        "proof": [{"command": "worker process exited without writing a result", "exit": 1, "log": log}],
        "tests": [], "open_questions": [text] if text else [], "tokens": None, "started": now, "finished": now})


# Send-failure policy (2026-10-15). The courier's own messages decide: a transient failure is one a later tick can
# outlive without anyone changing the brief, the config or the code; everything else is a contract refusal that a
# retry would only repeat, so it escalates at once. Patterns are lane_send's real strings, nothing invented.
TRANSIENT_SEND_FAILURES = (
    r"send FAILED \(exit \d+\)",                      # the CLI process died: lane_send's claude, codex-exec, cursor, grok
    r"Claude budget stop",                            # a timed pause; the dispatcher's own budget gate holds the retry
    r"Claude budget state unavailable or invalid",    # model-budget.json unreadable at that moment
    r"(?:both|all) muse slots are occupied",         # "retry after a worker exits", in the courier's own words
    r"cannot read effort policy",                     # effort-policy.json unreadable at that moment
)


# Matches "send FAILED (exit 1)" too, but no later tick fixes it: only the owner trusts a folder in Cursor (AGENTS.md rule 9).
# Night 2026-10-17: FO2 cycled on cursor:w2 against an untrusted fresh worktree.
# lane_send pastes only the last 300 characters of the Cursor log, which cut "Workspace Trust Required" off (review of
# aac0b09b), so the phrases that survive in the tail count too.
NEVER_TRANSIENT = re.compile(r"Workspace Trust Required|trust the contents of this directory|Pass --trust, --yolo", re.I)


def send_failure_class(reason):
    """``transient`` or ``contract`` for a courier refusal, by lane_send's message; unknown text is a contract
    refusal (a human reads it) rather than a silent retry."""
    text = " ".join(str(reason or "").split())
    if NEVER_TRANSIENT.search(text):
        return "contract"
    return "transient" if any(re.search(p, text) for p in TRANSIENT_SEND_FAILURES) else "contract"


def _path(value, root, repo):
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "data":
        return root.joinpath(*path.parts[1:])
    return repo / path


def worktree_name(run):
    """The default worktree folder and branch suffix of a run: <UTC date of the record>-<feature>-<node>."""
    return f"{time.strftime('%Y-%m-%d', time.gmtime(run['created']))}-{run['feature']}-{run['node']}"


MAX_WORKTREE_SUFFIX = 20


def _git_out(*args):
    done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
    return done.stdout.strip() if done.returncode == 0 else None


def plan_worktree(repo, name, base, channel=""):
    """(name, command): the worktree a run works in, decided read-only before anything is sent. Every attempt of a
    node on one day shares a name, so a second attempt used to fail at ``worktree add -b`` (the branch existed) or,
    through the dispatcher's reuse, ran in the first attempt's worktree as it was (the 3D PLAN retry of 2026-10-15
    was 61 commits behind master). Now, for ``name`` and then ``name-2``, ``name-3``...:
      - no branch and no folder: a new branch at ``base``;
      - a branch with no commits beyond ``base`` and no folder: the branch is moved to ``base`` (add -B; nothing
        on it is lost, it is already in base);
      - that branch checked out in its own clean folder: the folder is fast-forwarded to ``base``;
      - a branch with its own commits, a branch checked out in another folder, a folder with uncommitted work
        (``.lane-send/`` aside), or a folder that is not this branch's worktree: kept as it is, and the next suffix
        is tried.
    The command always names the exact base commit. Each folder is where run_worktree puts it for ``channel`` (a grok
    run's under GROK_HOME). Outside a git repository (test fixtures with a fake make_worktree) the plain add command
    is returned unchanged."""
    repo = Path(repo)
    base_sha = _git_out("-C", str(repo), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if not base_sha:
        path = run_worktree(repo, name, channel)
        return name, ["git", "-C", str(repo), "worktree", "add", "-b", f"session/{name}", str(path), base]
    checked_out = {}  # branch -> the folder it is checked out in
    folder = None
    for line in (_git_out("-C", str(repo), "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            folder = Path(line[len("worktree "):]).resolve()
        elif line.startswith("branch refs/heads/") and folder is not None:
            checked_out[line[len("branch refs/heads/"):]] = folder
    for n in range(1, MAX_WORKTREE_SUFFIX + 1):
        candidate = name if n == 1 else f"{name}-{n}"
        path, branch = run_worktree(repo, candidate, channel), f"session/{candidate}"
        branch_sha = _git_out("-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if not path.exists() and not branch_sha:
            return candidate, ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path), base_sha]
        if branch_sha and _git_out("-C", str(repo), "rev-list", "--count", f"{base_sha}..{branch_sha}") != "0":
            continue  # its own commits are unharvested work
        if branch in checked_out and checked_out[branch] != path.resolve():
            continue  # checked out in another folder: git refuses it there, and that folder is someone's
        if not path.exists():
            return candidate, ["git", "-C", str(repo), "worktree", "add", "-B", branch, str(path), base_sha]
        top = _git_out("-C", str(path), "rev-parse", "--show-toplevel")
        if (not top or Path(top).resolve() != path.resolve()
                or _git_out("-C", str(path), "branch", "--show-current") != branch):
            continue  # another checkout, or not this branch: never used silently
        status = _git_out("-C", str(path), "status", "--porcelain", "--untracked-files=all")
        if status is None or any(line[3:].split("/")[0] != ".lane-send" for line in status.splitlines() if line):
            continue  # uncommitted work
        return candidate, ["git", "-C", str(path), "merge", "--ff-only", "--quiet", base_sha]
    raise ValueError(f"worktree: no free name after {name}-{MAX_WORKTREE_SUFFIX}")


def input_base(repo, tips, default):
    """The ref a node's worktree starts from: the newest input commit, unless another input is not in it. Then, when
    every input has landed, origin/master holds them all (2026-10-17: MS1 started at MS0B's commit and the worker
    could not merge X1's, which had landed; the harness denies merges). Unverifiable shas keep the newest tip."""
    if not tips:
        return default
    newest = max(tips)[1]
    others = [sha for _, sha in tips if sha != newest]

    def ancestor(sha, ref):
        done = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, ref],
                              capture_output=True, timeout=30)
        return done.returncode == 0

    if all(ancestor(sha, newest) for sha in others):
        return newest
    if all(ancestor(sha, "origin/master") for _, sha in tips):
        return _git_out("-C", str(repo), "rev-parse", "origin/master") or newest
    return newest


def prepare_run(run, root, repo, name=None):
    """Build the minimal F0 brief from the DAG and validated input results, in worktree ``name`` (default
    worktree_name(run))."""
    old = contract.validate_brief(run["brief"])
    for key in ("run_id", "feature", "node"):
        if old[key] != run[key]:
            raise ValueError(f"{key}: brief does not match run record")
    node = dag.load(dag.live_path(root / "team/feature-teams", run["feature"])).node(run["node"])
    inputs, tips = {}, []
    input_rows = {item["node"]: item for item in old["inputs"]}
    for input_node in node["inputs"]:
        if input_node not in input_rows:
            raise ValueError(f"inputs: no result path for input node {input_node}")
        item = input_rows[input_node]
        path = _path(item["result"], root, repo)
        value = contract.validate_result(json.loads(path.read_text()))
        if value["feature"] != run["feature"] or value["node"] != item["node"] or value["status"] != "done":
            raise ValueError("inputs: result must belong to this feature and input node and be done")
        inputs[item["node"]] = str(path)
        if value["sha"]:
            tips.append((value["finished"], value["sha"]))
    name = name or worktree_name(run)
    brief = contract.brief_from_node(
        node, run_id=run["run_id"], feature=run["feature"], channel=old["channel"], model=old["model"],
        effort=old["effort"], base=input_base(repo, tips, repos.for_path(repo).base),
        worktree=str(run_worktree(repo, name, old["channel"])), inputs=inputs, deadline=run["deadline"],
        result=str(_path(old["result"], root, repo)), rules=str(_path(old["rules"], root, repo)),
        deny=old["deny"], acceptance=contract.acceptance_items(node["acceptance"]),
    )
    if old.get("fixback"):
        brief["fixback"] = old["fixback"]  # the failed verify this fix-back repairs; it also marks the run a fix-back
    brief, staging = bind_to_worktree(brief, root, repo)
    return dict(run, brief=brief, worktree=brief["worktree"], branch=f"session/{name}", staging=staging)


def run_worktree(repo, name, channel):
    """Where a run's fresh worktree goes: <repo>/.claude/worktrees/<name> for every channel except grok, whose
    courier accepts only a folder directly under GROK_HOME (its sandbox wall keeps data/ and .env out of reach
    there; 2026-10-15, found while adding the grok:g-build-N rows). The grok folder is <target>--<name>, so the
    lane watch's grok-<folder> row starts with the channel's lane id (grok-<target>) and the dispatcher can match
    it by that prefix. There is no worktree cleanup in the node worker; whoever removes one uses the same path."""
    kind, _, target = channel.partition(":")
    profile = repos.for_path(repo)
    if not profile.is_kyra:  # an allowlisted repo keeps its worktrees where it always has (the site: a sibling folder)
        return profile.worktrees / name
    if kind == "grok":
        return _lane_send().GROK_HOME / (f"{target}--{name}" if target else name)
    return repo / ".claude/worktrees" / name


def _bash_rule_ok(command, allow, deny):
    from fnmatch import fnmatchcase

    return any(fnmatchcase(command, r) for r in allow) and not any(fnmatchcase(command, r) for r in deny)


# A site node's personal-data check: an email address or a phone-like number in an owned file. grep exits 1 when
# nothing matches, which is the pass; any hit is read and removed before the commit.
SITE_PERSONAL_DATA = (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}",
                      r"\+?[0-9]{3}[ .()-]*[0-9]{3}[ .()-]*[0-9]{4}")


def _non_kyra_repo(brief):
    """The non-Kyra Repo (the site) the brief's worktree belongs to, else None."""
    from companion.feature_team import repos
    return repos.for_worktree(brief["worktree"])


def worker_commands(brief):
    """The exact command lines a worker is told to run, in the form lane_send's Claude CLI allowlist accepts
    (3d-inner-demo G1, 2026-10-16: bare pytest, ruff and date were refused under dontAsk, so it could not test or
    commit): the main checkout's venv python first, then relative paths only; commits go through commit_gate.py in
    the current worktree (git -C <absolute path> is denied). An acceptance command that has no allowed form (bash,
    make, git, npm, a pipe) is left to verify.py and not listed. Every line is checked against the real rules,
    read from the real lane_send (not the _lane_send seam, which tests replace with a fake channel)."""
    global _RULES_LANE
    if _RULES_LANE is None:
        _RULES_LANE = _load_lane_send()
    lane = _RULES_LANE
    py = str(lane._main_checkout() / ".venv/bin/python")
    files = [f[4:] if f.startswith("NEW ") else f for f in brief["files"]]
    lines = []
    site = _non_kyra_repo(brief)
    for item in brief["acceptance"]:
        try:
            words = shlex.split(item)
        except ValueError:
            continue
        if not words or any(w in ("&&", "||", ";", "|") or w.startswith(("|", ">", "<")) for w in words):
            continue
        if site:  # a site node: file checks only; the repo has no venv, pytest or commit_gate.py
            if words[0] != "test":
                continue
        elif words[0] in ("pytest", "ruff"):
            words = [py, "-m", *words]
        elif words[0] in ("python", "python3", ".venv/bin/python") or words[0].endswith("/python"):
            words = [py, *words[1:]]
        else:
            continue
        lines.append(shlex.join(words))
    # verify.py runs `git diff --check` at the sha; trailing spaces in worker markdown failed STORY-REHEARSAL and
    # README on it (2026-10-16 FL13), so the worker runs it first (git diff is an allowed read command)
    lines.append("git diff --check")
    message = f"{brief['feature']} {brief['node']}: what changed and what passed"
    if _coordinator_build(brief):
        pass  # Shared Git is read-only inside the worker wall.
    elif site:
        lines.append(shlex.join(["grep", "-rnEi", "-e", SITE_PERSONAL_DATA[0], "-e", SITE_PERSONAL_DATA[1], *files]))
        lines.append(shlex.join(["git", "add", "--", *files]))
        lines.append(shlex.join(["git", "commit", "-m", message]))
    else:
        lines.append(shlex.join([py, "scripts/commit_gate.py", "-m", message, *files]))
    lines.append(f'{py} -c "print(int(__import__(\'time\').time()))"')
    flags = lane.claude_permission_flags(Path(brief["worktree"]))

    def bash(flag):
        rules = flags[flags.index(flag) + 1].split(",")
        return [r[5:-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]

    allow, deny = bash("--allowedTools"), bash("--disallowedTools")
    return list(dict.fromkeys(line for line in lines if _bash_rule_ok(line, allow, deny)))


def worker_command_block(brief):
    """The brief text that carries worker_commands: one command per indented line."""
    commands = worker_commands(brief)
    if _coordinator_build(brief):
        return ("Commands (run each separately):\n" + "".join(f"    {c}\n" for c in commands)
                + "\nLeave changes uncommitted and unstaged. Shared Git is read-only in your wall. "
                  "For successful work, return status done, sha null, the assigned branch, and every changed "
                  "owned file in artifacts (including deletions). The coordinator waits for your successful exit, "
                  "then uses commit_gate.py before SHA verification. Failed or blocked work is never committed. "
                  "Do not list courier files in artifacts. Save proof logs with the Write tool.\n")
    if _non_kyra_repo(brief):
        return ("Commands (run each as its own Bash call, exactly as written; the worker allowlist refuses &&, ;, "
                "pipes, redirects and git -C). This is the site repo: there is no scripts/commit_gate.py, venv or "
                "pytest here.\n" + "".join(f"    {c}\n" for c in commands)
                + "\nRun the test lines first, then git diff --check (remove every trailing space it names), then "
                  "the grep: it must print nothing and exit 1; a line it prints is an email address or phone-like "
                  "number, so remove it. Then git add and git commit in this worktree, with the message text "
                  "changed and the form kept. Never push, never deploy. If a command is denied, write the result "
                  "with status blocked, the changed files in artifacts and the refusal text in open_questions; the "
                  "mentor commits. Save proof logs with the Write tool, never a shell redirect. The last line "
                  "prints the epoch time for started and finished.\n")
    return ("Commands (run each as its own Bash call, exactly as written; the worker allowlist refuses bare pytest, "
            "ruff or date, &&, ;, pipes, redirects and any absolute path after the first word):\n"
            + "".join(f"    {c}\n" for c in commands)
            + "\nBefore commit_gate.py, run git diff --check and remove every trailing space or tab it names: verify.py "
              "runs it at your sha and fails the node on any hit. commit_gate.py also strips them from the named .md "
              "files, which covers new files that git diff does not show.\n"
            + "\nThe acceptance list is what verify.py runs at your sha; run its pytest and ruff items through the "
              "lines above. Change the commit message text and keep the form. commit_gate.py stages exactly the named "
              "files, runs ruff, the impacted tests and the privacy guards, and commits in this worktree (git -C is "
              "refused). If the gate refuses or a command is denied, write the result with status blocked, the "
              "changed files in artifacts and the refusal text in open_questions; the mentor commits. Save proof "
              "logs with the Write tool, never a shell redirect. The last line prints the epoch time for "
              "started and finished.\n")


def render_brief(run, root=None):
    """B0 replacement seam: a complete, self-contained brief for one fresh worker. With ``root`` the brief names
    the feature's rank in the queue ('Priority: N'), which the courier records and admission reads."""
    from companion import priority as admission

    rank = admission.feature_rank(root, run["feature"]) if root is not None else None
    example = contract.result_example(run["brief"])
    if _coordinator_build(run["brief"]):
        value = json.loads(example)
        value.update(sha=None, branch=run.get("branch", value["branch"]),
                     artifacts=[f[4:] if f.startswith("NEW ") else f for f in run["brief"]["files"]])
        example = json.dumps(value, indent=2)
    return ("Execute this one node in a fresh context. Follow the rules and own only the listed files.\n"
            + json.dumps(contract.validate_brief(run["brief"]), indent=2)
            + "\n" + worker_command_block(run["brief"])
            + "\nWrite the result atomically at the result path, using contract.validate_result.\n"
            + "Required result fields: " + ", ".join(contract.RESULT_FIELDS) + ".\n"
            + "Copy run_id, feature and node from the brief. A done result needs proof rows "
              "{command, exit, log}, and a sha or at least one artifact. Times are epoch seconds.\n"
            + "Example (tests is a JSON list of command strings, never a summary string or a count dict; exit is "
              "an integer):\n" + example + "\n"
            + (admission.rank_line(rank) + "\n" if rank is not None else ""))


def _load_lane_send():
    spec = importlib.util.spec_from_file_location("_node_worker_lane_send", REPO / "scripts/lane_send.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lane_send():
    return _load_lane_send()


_RULES_LANE = None  # the real lane_send, loaded once, whose Claude worker rules worker_commands checks against


def channel_blocker(brief, lane):
    kind = brief["channel"].partition(":")[0]
    if kind == "codex":
        return "not fresh: lane_send.send_codex reuses an existing thread"
    if kind == "claude" and not all(hasattr(lane, name) for name in ("send_claude", "check_claude_worker")):
        return "this lane_send base has no gated fresh-context send_claude channel"
    if kind not in {"cursor", "grok", "muse", "claude", "codex-exec"}:
        return f"unsupported node-worker channel: {kind}"
    if not hasattr(lane, f"send_{kind.replace('-', '_')}"):
        return f"this lane_send base has no gated fresh-context send_{kind.replace('-', '_')} channel"
    if reason := contract.channel_refusal(brief["channel"]):
        return reason
    if kind == "grok" and Path(brief["worktree"]).parent != lane.GROK_HOME.resolve():
        return "lane_send.send_grok requires a worktree under GROK_HOME, not .claude/worktrees"
    return None


def channel_command(brief, timeout):
    """Show the sender's Codex argv or the other channels' function boundary."""
    kind = brief["channel"].partition(":")[0]
    if kind == "codex-exec":
        argv = _lane_send().codex_exec_argv(Path(brief["worktree"]), "<rendered brief>",
                                            brief["effort"], brief["model"], _final_path(brief))
        return shlex.join(argv)
    if kind == "cursor":
        return f"lane_send.send_cursor({brief['worktree']!r}, <rendered brief>, wait={timeout})"
    if kind == "grok":
        return (f"lane_send.send_grok({brief['worktree']!r}, <rendered brief>, "
                f"effort={brief['effort']!r}, model={brief['model']!r}, wait={timeout}, profile='build')")
    if kind == "muse":
        return (f"lane_send.send_muse({brief['worktree']!r}, <rendered brief>, "
                f"effort={brief['effort']!r}, wait={timeout})")
    if kind == "claude":
        return (f"lane_send.send_claude({brief['worktree']!r}, <rendered brief>, "
                f"effort={brief['effort']!r}, model={brief['model']!r}, wait={timeout})")
    return None


def _final_path(brief):
    return Path(brief["worktree"]) / ".lane-send" / f"{brief['run_id']}.final.md"


@contextmanager
def _data_root(root):
    """lane_send's config and budget readers use the process data-root setting."""
    old = os.environ.get("KYRA_DATA_DIR")
    os.environ["KYRA_DATA_DIR"] = str(root)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("KYRA_DATA_DIR", None)
        else:
            os.environ["KYRA_DATA_DIR"] = old


@contextmanager
def _capture_build_process(lane, enabled):
    """Keep the courier's Popen object, even when it has already reaped a fast supervisor."""
    processes = []
    original = getattr(lane, "subprocess", subprocess)

    def start(*args, **kwargs):
        process = original.Popen(*args, **kwargs)
        processes.append(process)
        return process

    if enabled:
        # This newly loaded courier module alone gets the proxy; never patch global subprocess.Popen.
        lane.subprocess = SimpleNamespace(**dict(vars(subprocess), Popen=start))
    try:
        yield processes
    finally:
        if enabled:
            lane.subprocess = original


def send_channel(brief, message, *, receipt_timeout, root):
    """Call the existing channel functions, never resume or reuse a session id."""
    lane = _lane_send()
    if reason := channel_blocker(brief, lane):
        raise ChannelUnavailable(reason)
    kind = brief["channel"].partition(":")[0]
    sent_at = time.time()
    # The rendered contract includes rules, input paths and a result example.
    # Supply the exception before Grok checks size, and retain it in either ledger path.
    oversize_reason = "rendered DAG node brief"
    with (_capture_build_process(lane, _coordinator_build(brief)) as processes,
          _data_root(root), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
        if kind == "cursor":
            receipt = lane.send_cursor(brief["worktree"], message, wait=receipt_timeout)
        elif kind == "muse":
            receipt = lane.send_muse(brief["worktree"], message, effort=brief["effort"], wait=receipt_timeout)
        elif kind == "claude":
            # CL's send_claude checks config + budget and mints a new session itself.
            receipt = lane.send_claude(brief["worktree"], message, effort=brief["effort"], model=brief["model"],
                                       wait=receipt_timeout)
        elif kind == "codex-exec":
            receipt = lane.send_codex_exec(brief["worktree"], message, effort=brief["effort"],
                                           model=brief["model"], out=str(_final_path(brief)), wait=receipt_timeout)
        else:
            receipt = lane.send_grok(brief["worktree"], message, effort=brief["effort"], model=brief["model"],
                                     wait=receipt_timeout, profile="build",
                                     package_meta={"oversize_reason": oversize_reason})
    confirmed_id = _receipt_id(receipt)
    if isinstance(receipt, str) and receipt.startswith("UNCERTAIN:"):
        return receipt
    watched = re.match(rf"^{kind} started pid [0-9]+\b.*\bledger_id=([A-Za-z0-9][A-Za-z0-9._-]*)\b", receipt or "")
    if watched:
        # a watched grok/muse build send: the courier recorded its ledger row before launch, and its exit supervisor
        # closes that id; record no second row (review o-rev70)
        log = re.search(r"\(log ([^)]+)\)", receipt)
        text = f"STARTED: {kind} pid={_courier_pid(receipt)} ledger_id={watched[1]}" + (f" log={log[1]}" if log else "")
        process = next((p for p in processes if str(p.pid) == _courier_pid(receipt)), None)
        return BuildReceipt(text, process) if _coordinator_build(brief) else text
    if not confirmed_id and not re.match(rf"^{kind} started pid [0-9]+\b", receipt):
        return None
    # Calling functions instead of lane_send.main means we must record the accepted
    # send ourselves. Keep --root isolation for the ledger as well as runs/events.
    lane.ledger_path = lambda: root / "context/packages.jsonl"
    ledger_id = lane.record_package(kind, brief["worktree"], message, lines=len(message.splitlines()),
                                    paths=len(lane.brief_paths(message)), oversize_reason=oversize_reason,
                                    team=brief["feature"], model=brief["model"], effort=brief["effort"],
                                    sent_at=sent_at, package_id=confirmed_id, receipt=receipt)
    if confirmed_id:
        return receipt  # Keep the session and log pointer supplied by the channel.
    log = re.search(r"\(log (.+)\)$", receipt)
    pid = _courier_pid(receipt)
    return f"STARTED: {kind}" + (f" pid={pid}" if pid else "") + f" ledger_id={ledger_id}" + (f" log={log[1]}" if log else "")


def _courier_pid(receipt):
    """The pid in a courier's '<kind> started pid N ...' line, kept on the STARTED receipt as pid=N so run_node's
    dead-worker check covers grok and muse too (it reads pid= from the receipt, as for Claude and Codex)."""
    match = re.search(r"\bstarted pid ([0-9]+)\b", receipt or "")
    return match[1] if match else None


def _worktree(command):
    subprocess.run(command, capture_output=True, text=True, check=True)


def _receipt_id(receipt):
    if not isinstance(receipt, str) or receipt.startswith("UNCERTAIN:"):
        return None
    if receipt.startswith("STARTED:"):
        match = re.search(r"\bledger_id=([A-Za-z0-9][A-Za-z0-9._-]*)\b", receipt)
        return match[1] if match else None
    # A courier's own start line plus lane_send's ledger line, e.g. Cursor:
    # "cursor started pid 74826 in /x (log /tmp/x.log)\nledger id: cursor-cursor-w2-abc".
    if re.match(r"^[a-z][a-z-]* started pid [0-9]+\b", receipt):
        match = (re.search(r"\bledger_id=([A-Za-z0-9][A-Za-z0-9._-]*)\b", receipt)
                 or re.search(r"\bledger id:\s*([A-Za-z0-9][A-Za-z0-9._-]*)\b", receipt))
        return match[1] if match else None
    return None


def _receipt_field(receipt, field):
    if not isinstance(receipt, str):
        return None
    match = re.search(rf"\b{field}=(.*?)(?=\s+[a-z_]+=|$)", receipt)
    return match[1] if match else None


def _find_call(command):
    """Recognize find in command position, including the shell wrapper in Codex logs."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) >= 3 and Path(words[0]).name in {"sh", "bash", "zsh"} and words[1] in {"-c", "-lc"}:
        return _find_call(words[2])
    return bool(re.search(r"(?:^|[;|&]\s*)(?:/usr/bin/)?find\s", command.strip()))


def _search_calls(receipt):
    """Count observed tool invocations, never mentions in assistant prose or results."""
    log = _receipt_field(receipt, "log")
    if not log:
        return 0
    count, command_next = 0, False
    # Missing/unreadable logs are errors, not evidence of zero searches.
    with Path(log).open(errors="replace") as stream:
        for line in stream:
            if command_next:
                count += int(_find_call(line))
            command_next = line.strip() == "exec"  # Codex's plain-text tool log.
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            message = row.get("message")
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for tool in content:
                if not isinstance(tool, dict) or tool.get("type") != "tool_use":
                    continue
                name = tool.get("name")
                if name in {"Grep", "Glob", "find"}:
                    count += 1
                elif name == "Bash" and isinstance(tool.get("input"), dict):
                    command = tool["input"].get("command", "")
                    if isinstance(command, str):
                        count += int(_find_call(command))
    return count


def repo_blocker(brief, repo):
    """A channel whose courier cannot work in this repo is a contract refusal, not a send that fails later."""
    profile = repos.for_path(repo)
    kind = brief["channel"].partition(":")[0]
    if profile.channels and kind not in profile.channels:
        return (f"contract: channel {kind} cannot work in the {profile.name} repo (its courier takes Kyra worktrees "
                f"only); this repo takes {', '.join(profile.channels)}")
    return None


def run_node(run_id, *, root=None, repo=None, dry_run=False, store=None, send=None,
             make_worktree=_worktree, clock=time.time, sleep=time.sleep, receipt_timeout=RECEIPT_TIMEOUT):
    """Run once, in the feature's repo (companion.feature_team.repos) unless ``repo`` is given. Injectable
    channels/clocks are for hermetic tests, not saved state."""
    if root is None:
        from companion.paths import DATA_DIR
        root = DATA_DIR
    root = Path(root).resolve()
    store = store or runs.Runs(root / "team/loop", emit=EventStore(root).append, clock=clock)
    rec = store.load(run_id)
    repo = Path(repo if repo is not None else repos.for_feature(root, rec["feature"]).path).resolve()
    if rec["state"] not in {"queued", "dispatched", "fixback", "timeout"}:
        raise ValueError(f"state: {rec['state']} cannot start a one-shot worker")
    if receipt_timeout <= 0:
        raise ValueError("receipt_timeout: must be positive")

    def transition(state, detail, **kwargs):
        return store.transition(run_id, state, worker="node-worker", detail=detail, **kwargs)

    def observe(kind, detail):
        store.emit(contract.validate_event(dict(
            t=clock(), run_id=run_id, feature=rec["feature"], node=rec["node"], worker="node-worker",
            channel=rec["brief"]["channel"], kind=kind, sha=None, path=None,
            detail=" ".join(detail.split())[:contract.DETAIL_MAX])))

    custody_lock, custody = None, None
    try:
        prepared = prepare_run(rec, root, repo)
        default = worktree_name(rec)  # the folder name may carry a grok target prefix; plan by the run's name
        name, command = plan_worktree(repo, default, prepared["brief"]["base"], prepared["brief"]["channel"])
        if name != default:
            prepared = prepare_run(rec, root, repo, name=name)
        brief = prepared["brief"]
        message = render_brief(prepared, root)
        blocker = (repo_blocker(brief, repo) or (channel_blocker(brief, _lane_send()) if send is None else None)
                   or path_blocker(brief))
        if dry_run:
            return dict(run_id=run_id, state="dry_run", brief=brief, rendered_brief=message,
                        worktree_command=command, channel_command=channel_command(brief, receipt_timeout),
                        channel_blocker=blocker)
        if clock() >= rec["deadline"]:
            return transition("timeout" if rec["state"] != "timeout" else "escalated", "deadline expired before dispatch")
        if blocker:
            raise ChannelUnavailable(blocker)
        if rec["state"] != "dispatched" and rec["attempts"][rec["tier"]] >= runs.ATTEMPTS_PER_TIER:
            return transition("escalated", "attempts exhausted on this tier")
        result_path = Path(brief["result"])
        if result_path.exists():
            raise ValueError("result: stale file exists before dispatch; reconcile the previous attempt first")
        if _coordinator_build(brief):
            folder = root / "team/loop/build-custody"
            folder.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(str(Path(brief["worktree"]).resolve()).encode()).hexdigest()
            custody_lock = (folder / f"{key}.lock").open("a")
            try:
                fcntl.flock(custody_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("build handoff: another coordinator owns this worktree") from None
        # R0 has no public brief-update method. Its commit seam keeps the rendered
        # snapshot, derived index and validated event together before dispatch.
        prepared["updated"] = clock()
        store._commit(prepared, worker="node-worker", channel=brief["channel"], path=None, detail="brief rendered")
        make_worktree(command)
        if _coordinator_build(brief) and _git_out("-C", brief["worktree"], "rev-parse", "HEAD"):
            custody = _build_identity(brief["worktree"])
            if (Path(custody[0]).resolve() != Path(brief["worktree"]).resolve()
                    or custody[2] != prepared["branch"] or custody[3] != command[-1]
                    or custody[1] != _git_out("-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir")
                    or _build_changes(brief["worktree"])
                    or _git_out("-C", brief["worktree"], "diff", "--cached", "--name-only") != ""):
                raise ValueError("build handoff: dispatch requires the assigned clean worktree at its exact base")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        stage_inputs(prepared.get("staging", {}))
        if send is None:
            def send(brief, message, *, receipt_timeout):
                return send_channel(brief, message, receipt_timeout=receipt_timeout, root=root)

        search_calls = 0
        for attempt in range(2):
            rec = store.load(run_id)
            if clock() >= rec["deadline"]:
                return transition("escalated" if rec["state"] == "timeout" else "timeout", "deadline expired")
            if rec["state"] != "dispatched" and rec["attempts"][rec["tier"]] >= runs.ATTEMPTS_PER_TIER:
                return transition("escalated", "attempts exhausted on this tier")
            if rec["state"] != "dispatched":
                transition("dispatched", "fresh channel dispatch", deadline=rec["deadline"])
            start_deadline = min(clock() + receipt_timeout, rec["deadline"])
            heartbeat_at = clock() + HEARTBEAT_SECONDS
            try:
                receipt = send(brief, message, receipt_timeout=start_deadline - clock())
            except SystemExit as exc:
                raise ChannelUnavailable(str(exc)) from None
            if isinstance(receipt, str) and receipt.startswith("UNCERTAIN:"):
                observe("launch_uncertain", "channel launch uncertain; no confirmed start receipt")
            ledger_id = _receipt_id(receipt)
            # A valid ledger id is a start, whatever the clock says: Cursor's send blocks until its
            # start deadline, so a receipt landing exactly on it must not read as a timeout.
            if ledger_id is None:
                while clock() < start_deadline:
                    if clock() >= heartbeat_at:
                        transition("heartbeat", "waiting for start receipt")
                        heartbeat_at = clock() + HEARTBEAT_SECONDS
                    sleep(min(POLL_SECONDS, start_deadline - clock()))
                return transition("timeout", "start receipt timeout; no confirmed ledger id")
            if any(h["state"] == "started" and _receipt_field(h["detail"], "ledger_id") == ledger_id
                   for h in rec["history"]):
                return transition("escalated", "channel reused a start receipt; fresh context is not proven")
            session = _receipt_field(receipt, "session")
            transition("started", f"ledger_id={ledger_id}" + (f" session={session}" if session else ""))
            started_t = clock()
            while clock() < rec["deadline"]:
                process = getattr(receipt, "process", None)
                writing = _coordinator_build(brief) and process is not None and process.poll() is None
                if result_path.exists() and not writing:
                    if _coordinator_build(brief) and result_path.resolve() != result_path.absolute():
                        raise ValueError("build handoff: result must not follow a link")
                    search_calls += _search_calls(receipt)
                    previous = result_path.read_text()
                    try:
                        value = contract.validate_result(json.loads(previous))
                        if any(value[key] != brief[key] for key in ("run_id", "feature", "node")):
                            raise ValueError("result: run_id, feature and node must match the brief")
                    except ValueError as exc:
                        if attempt == 1:
                            return transition("escalated", "result validation failed after one correction", path=str(result_path))
                        error = str(exc)
                        saved = result_path.with_name(f"{result_path.name}.invalid-{rec['attempts'][rec['tier']] + 1}")
                        if saved.exists():
                            raise ValueError("result: previous invalid-result archive already exists") from None
                        result_path.replace(saved)
                        empty = launch_failure(receipt, clock() - started_t)
                        if empty:
                            return transition("timeout", f"{LAUNCH_FAILURE}: the worker ended in "
                                              f"{int(clock() - started_t)} s with an empty log ({empty}); its result "
                                              f"was not valid ({error[:120]})", path=str(saved))
                        verify_path = rec["verify"]["output"]
                        verify = (_path(verify_path, root, repo).read_text() if verify_path else "No verify output recorded.")
                        message = (render_brief(prepared) + f"\nValidator error: {error}\n"
                                   + f"Previous result ({saved}):\n{previous}\nVerify output:\n{verify}\n"
                                   + "Correct the result and write it atomically to the original result path.\n")
                        transition("timeout", "invalid result; one correction dispatch follows", path=str(saved))
                        break
                    if _coordinator_build(brief) and value["status"] == "done":
                        if value["sha"] is not None:
                            raise ValueError("build handoff: worker-supplied SHA is not a coordinator candidate")
                        gate_log = root / "team/loop/commits" / f"{run_id}.log"
                        sha = commit_build(brief, value, receipt, custody, gate_log)
                        value = dict(value, sha=sha, proof=[*value["proof"], {
                            "command": "coordinator commit_gate.py on the bound build candidate",
                            "exit": 0, "log": str(gate_log)}])
                    published, value = publish_result(brief, value, result_path, root)
                    return transition("result", json.dumps({"search_calls": search_calls}),
                                      path=str(published), result=value)
                pid = _receipt_field(receipt, "pid")
                if pid and not _alive(pid) and not result_path.exists():
                    empty = launch_failure(receipt, clock() - started_t)
                    if empty:
                        return transition("timeout", f"{LAUNCH_FAILURE}: the worker exited in "
                                          f"{int(clock() - started_t)} s with an empty log ({empty}) and no result")
                    observe("dispatch_refused", "worker process exited without writing a result; recorded as a failed result")
                    result_path.write_text(json.dumps(failed_result(brief, receipt, clock()), indent=1) + "\n",
                                           encoding="utf-8")
                    continue
                if clock() >= heartbeat_at:
                    transition("heartbeat", "waiting for result", path=str(result_path))
                    heartbeat_at = clock() + HEARTBEAT_SECONDS
                sleep(min(POLL_SECONDS, rec["deadline"] - clock()))
            else:
                return transition("timeout", "result deadline expired", path=str(result_path))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, SystemExit) as exc:
        if dry_run:
            raise
        # The cause goes on the record: "node worker failed: ValueError" (site PLAN, 2026-10-15) and
        # "...: CalledProcessError" (an existing branch at git worktree add, the rehearsal) named no cause.
        why = (exc.stderr or "") if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        detail = (str(exc) if isinstance(exc, ChannelUnavailable) else
                  " ".join(f"node worker failed: {type(exc).__name__}: {why}".split())[:contract.DETAIL_MAX])
        if isinstance(exc, ChannelUnavailable):
            kind = send_failure_class(detail)
            detail = (f"{kind}: " + " ".join(detail.split()))[:contract.DETAIL_MAX]
            observe("dispatch_refused", detail)
            if kind == "transient" and store.load(run_id)["state"] in {"queued", "dispatched"}:
                # T0 re-dispatches a timed-out record on a later tick: fresh context, the same deadline, attempts
                # bounded per tier; a contract refusal would only repeat, so it escalates below.
                return transition("timeout", detail)
        return transition("escalated", detail)
    finally:
        if custody_lock is not None:
            custody_lock.close()
