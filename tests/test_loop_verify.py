"""X0: the external verify runner. A tiny real git repo, real subprocess commands, no model, no network.

A failing acceptance command leaves the node not done and writes verify_fail with the log path; a passing one
marks it done with the sha; a hand-written completion row, a worker's own "done" or a marker file never count
as verified; the browser phase is refused; the scratch worktree is removed afterwards.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from companion.feature_team import completion, contract, runs, verify

FIX = Path(__file__).resolve().parent / "data" / "loop"
BRIEF = json.loads((FIX / "brief-ok.json").read_text())
RESULT = json.loads((FIX / "result-ok.json").read_text())
# verify runs an allowlist (pytest under tests/, ruff check, read-only git): the acceptance commands are pytest runs
PASS = "pytest tests/test_thing.py"
FAIL = "pytest tests/test_broken.py"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _abs_worktree(path: Path) -> str:
    """The contract accepts only a leading slash. On Windows, drop the drive letter: Path('/folder') is that folder on the current drive."""
    text = str(path)
    if text.startswith("/"):
        return text
    return path.resolve().as_posix().split(":", 1)[1]


def _init_repo(r: Path) -> Path:
    """A real repository with one commit holding a tiny tests/ tree (one passing test, one failing test)."""
    (r / "src" / "companion").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "src" / "companion" / "__init__.py").write_text("")
    (r / "src" / "companion" / "thing.py").write_text("def one():\n    return 1\n")
    (r / "tests" / "test_thing.py").write_text("from companion.thing import one\n\ndef test_one():\n    assert one() == 1\n")
    (r / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    (r / "tests" / "test_thing_browser.py").write_text("def test_browser():\n    raise RuntimeError('browser ran')\n")
    # The race test commits its own polling test over this file. Nothing else runs it.
    (r / "tests" / "test_slow.py").write_text("def test_slow():\n    return None\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(r, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "one")
    return r


# These tests commit, register a worktree, or write into the checkout. The others only read, so they share one repo.
_OWN_REPO = frozenset({
    "test_verify_pass_lists_the_run_for_lane1_once_and_flags_protected_paths",
    "test_two_verifies_on_one_run_id_end_with_one_verdict_and_no_stale_registration",
    "test_stale_scratch_registration_is_pruned_before_the_next_verify",
    "test_landing_claim_is_verified_at_origin_master_not_the_bare_sha",
    "test_plan_kind_result_is_verified_by_its_artifacts",
    "test_landing_claim_commands_come_from_the_landed_diff_not_the_plan",
})


@pytest.fixture(scope="module")
def shared_repo(tmp_path_factory):
    return _init_repo(tmp_path_factory.mktemp("loop-repo") / "repo")


@pytest.fixture
def repo(request, tmp_path, shared_repo):
    if request.node.name in _OWN_REPO:
        return _init_repo(tmp_path / "repo")
    return shared_repo


@pytest.fixture
def data(tmp_path, tmp_path_factory, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(completion, "DATA_DIR", d)
    # verify's scratch roots go to this test's own temp dir, so a parallel test (same fixture run id) never sees them
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("systmp")))
    # Courier workers export PYTEST_ADDOPTS=-n 2, and verify keeps that variable, so each acceptance pytest
    # started its own workers. The assertions check the verdict, not how many workers pytest used.
    # no:anyio skips a plugin that imports asyncio. The trimmed verify environment has no SYSTEMROOT, and on
    # Windows that import dies with WinError 10106 before the tiny test runs.
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:xdist -p no:anyio -p no:cacheprovider")
    return d


def _setup(data, repo, *, acceptance, files=("src/companion/thing.py",), status="done", sha=None, tests=("tests/test_thing.py",)):
    """A run record in state result with a validated result file on disk; returns (store, run_id)."""
    sha = sha or _git(repo, "rev-parse", "HEAD")
    run_id = "demo-X1-20260101T000000-a1b2c3"
    result_path = data / "results" / f"{run_id}.json"
    result_path.parent.mkdir(parents=True)
    brief = contract.validate_brief(dict(BRIEF, acceptance=list(acceptance), files=list(files), result=str(result_path),
                                         worktree=_abs_worktree(repo)))
    result = dict(RESULT, sha=sha, status=status, tests=list(tests), open_questions=["why?"] if status == "blocked" else [])
    result_path.write_text(json.dumps(contract.validate_result(result)))
    store = runs.Runs(data / "team" / "loop", emit=lambda row: None, clock=lambda: 1_000_000.0)
    store.create(brief)
    store.transition(run_id, "dispatched")
    store.transition(run_id, "result", worker="Sol", result=result, path=str(result_path))
    return store, run_id


def test_passing_acceptance_marks_done_with_the_sha_and_logs_pass(data, repo):
    store, rid = _setup(data, repo, acceptance=[PASS, "latency under 2 s (measured)"])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is True and out["reason"] is None
    sha = _git(repo, "rev-parse", "HEAD")
    log = Path(out["log"]).read_text()
    assert log.rstrip().endswith(f"VERIFY PASS {sha}") and "not run (measurement" in log and "$ " in log
    assert "test-impact: " in log and "tests/test_thing.py" in log and "test_broken" not in log
    rec = store.load(rid)
    assert rec["state"] == "verify_pass" and rec["verify"]["output"] == out["log"] and "pytest tests/test_thing.py" in rec["verify"]["command"]
    row = completion.CompletionMap("demo")._read()["X1"]
    assert row["status"] == "done" and row["sha"] == sha and row["by"] == f"verify:{rid}"
    assert verify.verified("demo", "X1", data_dir=data) is True
    assert not list(Path(tempfile.gettempdir()).glob(f"kyra-verify-*/{rid}"))  # scratch worktree removed (o-rev94)
    assert rid not in _git(repo, "worktree", "list")


def test_verify_pass_lists_the_run_for_lane1_once_and_flags_protected_paths(data, repo):
    """A pass appends one ready-to-board row (lane 1 reviews, the loop never boards); a protected change is flagged."""
    from companion.feature_team import ready_board

    _git(repo, "branch", "master")  # the brief's base; the change below is what lane 1 would land
    (repo / "AGENTS.md").write_text("rules\n")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "rules")
    store, rid = _setup(data, repo, acceptance=[PASS])
    assert verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)["passed"] is True
    (entry,), bad = ready_board.view(data)
    assert bad == 0 and entry["state"] == "waiting" and entry["sha"] == _git(repo, "rev-parse", "HEAD")
    assert (entry["feature"], entry["node"], entry["run_id"]) == ("demo", "X1", rid)
    assert entry["outputs"] == ["src/companion/thing.py"] and entry["protected"] == ["AGENTS.md"]
    assert entry["flag"] == ready_board.PROTECTED_FLAG and ready_board.waiting_count(data) == 1


def test_failing_acceptance_leaves_the_node_not_done_and_logs_fail(data, repo):
    store, rid = _setup(data, repo, acceptance=[PASS, FAIL])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is False and "1 of 3 command(s) failed" in out["reason"] and "tests/test_broken.py" in out["reason"]
    assert Path(out["log"]).read_text().rstrip().startswith("verify ") and "VERIFY FAIL" in Path(out["log"]).read_text()
    rec = store.load(rid)
    assert rec["state"] == "verify_fail" and rec["verify"]["output"] == out["log"]
    assert completion.CompletionMap("demo")._read().get("X1") is None
    assert verify.verified("demo", "X1", data_dir=data) is False
    assert rid not in _git(repo, "worktree", "list")


def test_impacted_test_failure_fails_verify_even_when_acceptance_passes(data, repo):
    store, rid = _setup(data, repo, acceptance=[PASS], files=("tests/test_broken.py",))
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is False and "pytest tests/test_broken.py" in out["reason"]
    assert store.load(rid)["state"] == "verify_fail"


def test_browser_phase_is_refused_and_measurements_alone_are_not_a_pass(data, repo):
    store, rid = _setup(data, repo, acceptance=["pytest tests/test_thing_browser.py", "captures inspected"], files=("docs/x.md",))
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is False and out["reason"].startswith("nothing to run")
    log = Path(out["log"]).read_text()
    assert "refused (browser phase" in log and "browser ran" not in log
    assert [c.get("note") for c in out["commands"]] == ["refused: browser", "measurement"]
    assert completion.CompletionMap("demo")._read().get("X1") is None


def test_a_result_that_is_not_done_or_whose_sha_is_unknown_fails_without_running_anything(data, repo):
    store, rid = _setup(data, repo, acceptance=[PASS], status="blocked")
    out = verify.verify_run(rid, repo=repo, data_dir=data)
    assert out["passed"] is False and "not done" in out["reason"] and out["commands"] == []
    data2 = data / "second"
    store2, rid2 = _setup(data2, repo, acceptance=[PASS], sha="deadbeefdeadbeef")
    out2 = verify.verify_run(rid2, repo=repo, data_dir=data2)
    assert out2["passed"] is False and "scratch worktree failed" in out2["reason"]
    assert store2.load(rid2)["state"] == "verify_fail"
    with pytest.raises(ValueError, match="verify needs result"):
        verify.verify_run(rid2, repo=repo, data_dir=data2)


def test_mutation_bypassing_verify_is_refused_by_verified(data, repo):
    """A worker's own done, a marker file or a hand-written completion row never make a node verified."""
    store, rid = _setup(data, repo, acceptance=[PASS])
    sha = _git(repo, "rev-parse", "HEAD")
    cm = completion.CompletionMap("demo")
    (data / "team" / "features" / "demo").mkdir(parents=True, exist_ok=True)
    (data / "team" / "features" / "demo" / "X1.done").write_text(sha)  # a bare marker file
    assert verify.verified("demo", "X1", data_dir=data) is False
    cm.mark("X1", "done", sha=sha, result_path="/x", by="Sol")  # the worker marks itself done
    assert cm.done("X1") is True and verify.verified("demo", "X1", data_dir=data) is False
    cm.mark("X1", "done", sha=sha, result_path="/x", by=f"verify:{rid}")  # forged by-field, no verify_pass record
    assert verify.verified("demo", "X1", data_dir=data) is False
    cm.mark("X1", "done", sha="0123abcd", by=f"verify:{rid}")  # a different sha than the record will carry
    verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert verify.verified("demo", "X1", data_dir=data) is True  # verify rewrote the row with its own sha
    cm.mark("X1", "done", sha="0123abcd", by=f"verify:{rid}")
    assert verify.verified("demo", "X1", data_dir=data) is False  # sha no longer matches the verified record
    # the log is part of the proof: without the PASS line the row is not verified
    cm.mark("X1", "done", sha=sha, by=f"verify:{rid}")
    assert verify.verified("demo", "X1", data_dir=data) is True
    # LX: data_dir is honoured: another root with no completion map (or a different one) answers for itself
    assert verify.verified("demo", "X1", data_dir=data / "elsewhere") is False
    other = data / "other"
    shutil.copytree(data / "team", other / "team")
    assert verify.verified("demo", "X1", data_dir=other) is True
    json.dump({"nodes": {"X1": {"status": "done", "sha": sha, "by": "Sol"}}}, (other / "team/features/demo/completion.json").open("w"))
    assert verify.verified("demo", "X1", data_dir=other) is False and verify.verified("demo", "X1", data_dir=data) is True
    Path(store.load(rid)["verify"]["output"]).write_text("tampered\n")
    assert verify.verified("demo", "X1", data_dir=data) is False


def test_commands_run_without_a_shell_and_with_a_sandboxed_data_dir(data, repo):
    store, rid = _setup(data, repo, acceptance=[PASS, "pytest tests/test_thing.py && rm -rf /"], files=("docs/x.md",))
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    # no shell: "&&", "rm", "-rf" and "/" are pytest targets outside tests/, so the item is not allowlisted, never run
    assert out["passed"] is True and out["commands"][0]["exit"] == 0 and out["commands"][1]["exit"] is None
    assert "not run (not allowlisted" in Path(out["log"]).read_text() and repo.exists() and (repo / "tests").exists()
    assert verify.allowlisted("pytest tests/a.py -x", "/py") == (["/py", "-m", "pytest", "tests/a.py", "-x"], None)
    assert verify.allowlisted("ruff check src", "/py") == (["/py", "-m", "ruff", "check", "src"], None)
    assert verify._is_command("the dashboard shows 3 rows") is False and verify._is_command("bash scripts/x.sh") is True
    assert verify._browser("KYRA_BROWSER_TESTS=tests/test_a_browser.py pytest") is True


SLOW = "pytest tests/test_slow.py"


def test_prose_acceptance_with_an_apostrophe_is_a_measurement_not_a_crash(data, repo):
    """A PLAN brief's acceptance quotes the Board's note ("Follow referenced source notes for the owner's words"); shlex
    refuses the apostrophe. The browser check and the command check both treat it as prose, so verify runs the
    rest instead of failing with "No closing quotation" (the 3D PLAN run of 2026-10-15)."""
    prose = "Write the plan from the Board's feature note; follow source notes for the owner's words, don't guess"
    assert verify._browser(prose) is False and verify._is_command(prose) is False
    store, rid = _setup(data, repo, acceptance=[PASS, prose])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is True, out["reason"]
    assert "not run (measurement, not a command): Write the plan" in Path(out["log"]).read_text()


def _slow_test(flag: Path) -> str:
    """The first verify stays inside this test until the parent has observed the second verify skip."""
    return (
        "import time\nfrom pathlib import Path\n\n"
        "def test_slow():\n"
        f"    flag = Path({json.dumps(str(flag))})\n"
        "    deadline = time.time() + 30\n"
        "    while time.time() < deadline:\n"
        "        if flag.is_file():\n"
        "            return\n"
        "        time.sleep(0.01)\n"
        "    raise AssertionError('slow release flag was not written')\n"
    )


def test_two_verifies_on_one_run_id_end_with_one_verdict_and_no_stale_registration(data, repo, tmp_path):
    """Verify race (2026-10-15): the CLI's done and the dispatcher tick both verified T0, H2 and H8; the second
    rmtree'd the scratch worktree under the first pytest and left a stale git registration. Now the second verify
    skips with an event; the first verdict stands; nothing stale is left behind."""
    import threading

    flag = tmp_path / "slow-release"
    (repo / "tests" / "test_slow.py").write_text(_slow_test(flag))
    _git(repo, "add", "tests/test_slow.py")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "slow")
    store, rid = _setup(data, repo, acceptance=[SLOW, PASS])
    outs = {}

    def run(name):
        outs[name] = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)

    first = threading.Thread(target=run, args=("first",))
    first.start()
    second = None
    try:
        for _ in range(50):  # wait until the first verify holds the run lock (its log appears; scratch is in temp now)
            if (data / "team" / "loop" / "verify" / f"{rid}-0.log").exists():
                break
            threading.Event().wait(0.05)
        second = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    finally:
        # Release the first verify as soon as the second has returned. The join timeout stays 30 s.
        flag.write_text("go")
        first.join(timeout=30)
    assert second["skipped"] is True and second["passed"] is None and second["log"] is None
    assert outs["first"]["passed"] is True
    assert store.load(rid)["state"] == "verify_pass"
    logs = sorted((data / "team" / "loop" / "verify").glob(f"{rid}-*.log"))
    assert len(logs) == 1 and logs[0].read_text().rstrip().endswith(f"VERIFY PASS {_git(repo, 'rev-parse', 'HEAD')}")
    rows = [json.loads(line) for line in (data / "team" / "loop" / "events.jsonl").read_text().splitlines()]
    skipped = [r for r in rows if r["kind"] == "context" and "verify_skipped" in r["detail"]]
    assert len(skipped) == 1 and skipped[0]["run_id"] == rid and skipped[0]["worker"] == "verify"
    assert not list(Path(tempfile.gettempdir()).glob(f"kyra-verify-*/{rid}")) and rid not in _git(repo, "worktree", "list")
    # the next verify of a fresh run record on the same repo can register its scratch again
    with pytest.raises(ValueError, match="verify needs result"):
        verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)


def test_stale_scratch_registration_is_pruned_before_the_next_verify(data, repo):
    """A scratch directory deleted behind git's back (the old bare rmtree) must not block the next verify."""
    store, rid = _setup(data, repo, acceptance=[PASS])
    scratch = data / "team" / "loop" / "scratch" / rid
    scratch.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(scratch), _git(repo, "rev-parse", "HEAD"))
    shutil.rmtree(scratch)  # the stale registration the race used to leave
    listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    # git prints forward slashes. On Windows, str(path) uses backslashes, so the same path is checked in both forms.
    assert str(scratch) in listed or scratch.as_posix() in listed
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    assert out["passed"] is True and store.load(rid)["state"] == "verify_pass"
    assert rid not in _git(repo, "worktree", "list") and not scratch.exists()


def test_landing_claim_is_verified_at_origin_master_not_the_bare_sha(data, repo):
    """Verify race, second finding (2026-10-15): H8's landing claim was verified at its bare sha aaecc188, where the
    test the acceptance named did not exist yet; the landed tree (origin/master) had it. A result from the landing
    worker is checked out at origin/master; a worker's own result stays at its sha; a landing claim whose sha is
    not on origin/master is refused."""
    # the node's own commit: one module and its test; then the train merge adds the test the plan named
    (repo / "src/companion/thing2.py").write_text("def two():\n    return 2\n")
    (repo / "tests/test_thing2.py").write_text("from companion.thing2 import two\n\n\ndef test_two():\n    assert two() == 2\n")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "node work")
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_landed.py").write_text("def test_landed():\n    assert True\n")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "train merge")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")

    def claim(store, rid):
        result_path = Path(store.load(rid)["result"])
        result_path.write_text(json.dumps(dict(json.loads(result_path.read_text()), worker=contract.LANDING_WORKER)))

    # the node's commit is the first one; the test it must pass only exists on the landed tree
    # the acceptance names a test that exists only on the landed tree
    # a landing claim must touch the node's files (review o-rev58): a node that plans none, or a sha that touches
    # none of them, is refused before any command runs
    nofiles = data.parent / "data-nofiles"
    store, rid = _setup(nofiles, repo, acceptance=["pytest tests/test_landed.py"], files=(), sha=sha, tests=())
    claim(store, rid)
    out = verify.verify_run(rid, repo=repo, data_dir=nofiles, python=sys.executable)
    assert out["passed"] is False and "touches none of the node's files (the node lists none)" in out["reason"]
    other = data.parent / "data-other"
    store, rid = _setup(other, repo, acceptance=["pytest tests/test_landed.py"], files=("NEW src/elsewhere.py",),
                        sha=sha, tests=())
    claim(store, rid)
    out = verify.verify_run(rid, repo=repo, data_dir=other, python=sys.executable)
    assert out["passed"] is False and "touches none of the node's files" in out["reason"]
    store, rid = _setup(data, repo, acceptance=["pytest tests/test_landed.py"],
                        files=("NEW src/companion/thing2.py (the module)",), sha=sha, tests=())
    claim(store, rid)
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    log = Path(out["log"]).read_text()
    assert out["passed"] is True, (out["reason"], log[-1500:])
    assert f"checkout: origin/master (landing claim for {sha})" in log
    # a landing claim is judged on its own landed change set (here the root commit's files), not the planned command
    assert "planned acceptance not run for a landing claim: pytest tests/test_landed.py" in log
    assert "tests/test_thing2.py" in log and "exit 0" in log and log.rstrip().endswith(f"VERIFY PASS {sha}")
    assert "at origin/master" in store.load(rid)["history"][-1]["detail"]
    assert store.load(rid)["sha"] == sha and completion.CompletionMap("demo")._read()["X1"]["sha"] == sha
    assert rid not in _git(repo, "worktree", "list")
    # a worker's own result at the same bare sha: the landed test is not there, so the selection has nothing to run
    own = data.parent / "data-own"
    store, rid = _setup(own, repo, acceptance=["pytest tests/test_landed.py"], files=(), sha=sha, tests=())
    out = verify.verify_run(rid, repo=repo, data_dir=own, python=sys.executable)
    assert out["passed"] is False and "1 of 1 command(s) failed" in out["reason"]
    assert "at origin/master" not in store.load(rid)["history"][-1]["detail"]
    assert f"checkout: {sha}\n" in Path(out["log"]).read_text()
    # a landing claim whose sha is not on origin/master is refused before any checkout
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "unlanded")
    off = data.parent / "data-off"
    store, rid = _setup(off, repo, acceptance=[PASS])  # sha = HEAD, past origin/master
    claim(store, rid)
    out = verify.verify_run(rid, repo=repo, data_dir=off, python=sys.executable)
    assert out["passed"] is False and "not on origin/master" in out["reason"]
    assert "checkout:" not in Path(out["log"]).read_text()


def _plan_setup(data, repo, artifacts, kind="plan"):
    """A plan-kind run in state result: prose acceptance, a data-path file, the result naming its artifacts."""
    sha = _git(repo, "rev-parse", "HEAD")
    run_id = "demo-PLAN-20260101T000000-p1a2n3"
    result_path = data / "results" / f"{run_id}.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    brief = dict(BRIEF, run_id=run_id, node="PLAN", acceptance=["Write the plan and return the proposed DAG to the Board."],
                 files=["data/team/feature-teams/demo-plan.md"], result=str(result_path),
                 worktree=_abs_worktree(repo))
    if kind:
        brief["kind"] = kind
    brief = contract.validate_brief(brief)
    result = dict(RESULT, run_id=run_id, node="PLAN", sha=sha, status="done", tests=[], artifacts=list(artifacts))
    result_path.write_text(json.dumps(contract.validate_result(result)))
    store = runs.Runs(data / "team" / "loop", emit=lambda row: None, clock=lambda: 1_000_000.0)
    store.create(brief)
    store.transition(run_id, "dispatched")
    store.transition(run_id, "result", worker="Claude Opus", result=result, path=str(result_path))
    return store, run_id


def test_plan_kind_result_is_verified_by_its_artifacts(data, repo):
    """FL blockers (2026-10-15): every remaining queue feature is a PLAN node whose acceptance is prose and whose
    file is a data path, so verify ran nothing and failed. A plan-kind brief is checked by its artifacts: the plan
    file and, when returned, the DAG json exist and parse; a missing or unparsable one fails with its path; the
    same run without the kind still has nothing to run."""
    plan = repo / "data/team/feature-teams/demo-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# plan\n")
    dag = plan.with_name("demo.dag.json")
    dag.write_text(json.dumps({"nodes": []}))
    store, rid = _plan_setup(data, repo, [str(plan), "data/team/feature-teams/demo.dag.json"])
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    log = Path(out["log"]).read_text()
    # The relative artifact is logged from the brief's worktree. That string is the repo path. A Windows brief
    # uses a leading-slash path, so the log names the same file with the drive letter removed.
    logged_dag = Path(_abs_worktree(repo)) / "data/team/feature-teams/demo.dag.json"
    assert out["passed"] is True and "plan artifacts checked: 2" in log
    assert f"plan artifact: {dag}" in log or (f"plan artifact: {logged_dag}" in log and logged_dag.resolve() == dag.resolve())
    assert store.load(rid)["state"] == "verify_pass" and completion.CompletionMap("demo")._read()["PLAN"]["status"] == "done"
    dag.write_text("{not json")
    broken = data.parent / "data-broken"
    store, rid = _plan_setup(broken, repo, [str(plan), str(dag)])
    out = verify.verify_run(rid, repo=repo, data_dir=broken, python=sys.executable)
    assert out["passed"] is False and f"plan artifact unparsable: {dag}" in out["reason"]
    missing = data.parent / "data-missing"
    store, rid = _plan_setup(missing, repo, [str(plan.with_name("nope.md"))])
    out = verify.verify_run(rid, repo=repo, data_dir=missing, python=sys.executable)
    assert out["passed"] is False and "plan artifact missing or empty" in out["reason"]
    none = data.parent / "data-none"
    store, rid = _plan_setup(none, repo, [])
    assert "lists no artifacts" in verify.verify_run(rid, repo=repo, data_dir=none, python=sys.executable)["reason"]
    plain = data.parent / "data-plain"
    store, rid = _plan_setup(plain, repo, [str(plan)], kind=None)
    assert "nothing to run" in verify.verify_run(rid, repo=repo, data_dir=plain, python=sys.executable)["reason"]


def test_landing_claim_commands_come_from_the_landed_diff_not_the_plan(data, repo):
    """H8's reverify (2026-10-15): the DAG planned tests/test_correction_route.py, the renderer made it a pytest and
    a ruff command, and that file exists on no tree (the commit changed another test). A landing claim is judged on
    the sha's landed diff, filtered to origin/master, through the gate's impact selection; the planned paths that
    are not in the diff are logged and dropped. A worker result keeps the brief's commands and fails on them."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src/companion/thing2.py").write_text("def two():\n    return 2\n")
    (repo / "tests/test_thing2.py").write_text("from companion.thing2 import two\n\n\ndef test_two():\n    assert two() == 2\n")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "H8 as built")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/master", sha)
    planned = ("src/companion/thing2.py (one function only)", "NEW tests/test_never.py")
    acceptance = ["pytest tests/test_never.py", "ruff check tests/test_never.py"]
    store, rid = _setup(data, repo, acceptance=acceptance, files=planned, sha=sha, tests=("tests/test_thing2.py",))
    result_path = Path(store.load(rid)["result"])
    result_path.write_text(json.dumps(dict(json.loads(result_path.read_text()), worker=contract.LANDING_WORKER)))
    out = verify.verify_run(rid, repo=repo, data_dir=data, python=sys.executable)
    log = Path(out["log"]).read_text()
    assert out["passed"] is True, (out["reason"], log[-2500:])
    assert "planned path not in the diff: NEW tests/test_never.py" in log and "planned path not in the diff: src/companion/thing2.py (one function only)" not in log
    assert "planned acceptance not run for a landing claim: pytest tests/test_never.py" in log
    assert "landed diff: 2 path(s), 2 present at origin/master" in log
    assert "tests/test_thing2.py" in log and "ruff check src/companion/thing2.py tests/test_thing2.py" in log
    assert [c["command"] for c in out["commands"] if c["exit"] is not None][0].startswith("ruff check")
    assert store.load(rid)["state"] == "verify_pass" and base != sha
    # the same brief as a worker's own result keeps the planned commands and fails on the never-created file
    own = data.parent / "data-own"
    store, rid = _setup(own, repo, acceptance=acceptance, files=planned, sha=sha, tests=("tests/test_thing2.py",))
    out = verify.verify_run(rid, repo=repo, data_dir=own, python=sys.executable)
    assert out["passed"] is False and "command(s) failed: pytest tests/test_never.py" in out["reason"]


def test_impact_unknown_fallback_reads_test_files_out_of_command_strings(tmp_path, monkeypatch):
    """Lane 1 FIX-FIRST on c2998fad: the result example teaches command strings, and the fallback kept only entries
    that are files, so it selected none and ran fewer tests. Only existing files under tests/ are taken."""
    from companion import test_impact

    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    for name in ("tests/test_demo.py", "tests/test_other.py", "src/evil.py"):
        (tmp_path / name).write_text("")
    monkeypatch.setattr(test_impact, "impacted", lambda changed, root: None)
    named = [".venv/bin/python -m pytest tests/test_demo.py -q", "tests/test_other.py::test_one",
             "python -m pytest src/evil.py tests/../src/evil.py tests/test_missing.py", "guards: 19 passed"]
    targets, note = verify._pytest_targets(tmp_path, ["src/companion/demo.py"], named)
    assert note.startswith("impact unknown")
    assert targets == ["tests/test_demo.py", "tests/test_other.py"]
