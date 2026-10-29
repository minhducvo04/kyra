"""Acceptance tests for scripts/deploy_local.py, written from the planner's contract alone
(data/private_docs/team-flow-v2/deploy-contract-claude.md) before the script exists.

ASSUMED INTERFACE. The contract says only "one injectable runner", so what follows is a guess. Reconcile it in the
block marked "the seam" below (`_kind`, `FakeRunner`, and `run()` inside the `deploy` fixture), and nowhere else.

* CLI: `deploy_local.py TARGET [--repo MAIN_CHECKOUT] [--dry-run]`.
* `main(argv, runner=fn) -> int` returns the exit code (a SystemExit is accepted too, including sys.exit("reason")).
* The runner has subprocess.run's shape: `fn(argv_list, cwd=DIR, check=False) -> CompletedProcess` with text output.
  Every git, pytest, ruff and launchctl call goes through it, and so does the health check, as one call whose argv
  holds http://127.0.0.1:8420/api/team/status (exit code 0 means it answered). git always gets `cwd=` or `-C DIR`.
* The suite runs with `cwd=` set to the temporary worktree, which is a clean checkout of TARGET.
* The deploy log is <repo>/data/deploys.log (companion.paths.DATA_DIR is pointed at <repo>/data as well).
* Waiting uses time.sleep with time.monotonic or time.time; all three are faked so a dead service costs no wall time.

Safety: git runs for real, but only inside the fixture's temp dirs. pytest, ruff, launchctl and the health check are
scripted. Any other subprocess, os.system or socket connect fails the test instead of running, so a wrong guess about
the seam cannot restart the real web UI.

Not covered here because the contract's acceptance list omits it: refusal when uncommitted local changes overlap
the files TARGET changes (contract rule 4).
"""
import importlib.util
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC
from pathlib import Path
from typing import NamedTuple

import pytest

from companion import paths

ROOT = Path(__file__).resolve().parent.parent
HEALTH_URL = "http://127.0.0.1:8420/api/team/status"
RESTART = f"gui/{os.getuid()}/com.kyra.web-ui"
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
BASE_FILES = {
    "src/app.py": "print('v1')\n",
    "notes.txt": "base notes\n",
    "AGENTS.md": "rules\n",
    "docs/permissions.md": "permissions\n",
}
FEATURE = ({"src/app.py": "print('v2')\n", "feature.txt": "new\n"},)  # an ordinary, unprotected change
LOCAL_EDIT = "local edit\n"  # an uncommitted change in the main checkout that TARGET never touches


# ---- real git, confined to throwaway dirs -------------------------------------------------------------------------

_REAL_POPEN_INIT = subprocess.Popen.__init__
_real_git_running = False  # True only while this file's own real git call is on the wire


def _real_git(argv, cwd):
    """Run git for real, isolated from the developer's git config and any inherited GIT_* variables."""
    global _real_git_running
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    _real_git_running = True
    try:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    finally:
        _real_git_running = False


class Repo:
    """A throwaway git repo standing in for the main checkout, on branch master."""

    def __init__(self, path):
        self.path = path
        path.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/master")
        self.commit(BASE_FILES, "base")

    def git(self, *args):
        done = _real_git(["git", *args], self.path)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def write(self, name, text):
        (self.path / name).parent.mkdir(parents=True, exist_ok=True)
        (self.path / name).write_text(text)

    def commit(self, files, message):
        for name, text in files.items():
            self.write(name, text)
        self.git("add", "-A")
        self.git("-c", "user.email=fixture@example.com", "-c", "user.name=Fixture", "commit", "-q", "-m", message)
        return self.head()

    def branch(self, name, *commits):
        """Branch `name` off master's tip with one commit per dict of files. Master stays checked out. Returns the tip."""
        self.git("checkout", "-q", "-b", name)
        for number, files in enumerate(commits, 1):
            self.commit(files, f"{name} {number}")
        tip = self.head()
        self.git("checkout", "-q", "master")
        return tip

    def state(self):
        """Everything a refused run or a dry run must leave exactly as it found it."""
        return {
            "head": self.head(),
            "refs": self.git("for-each-ref"),
            "status": self.git("status", "--porcelain"),
            "worktrees": self.git("worktree", "list", "--porcelain"),  # the temporary worktree must be gone again
        }


# ---- the seam: reconcile _kind, FakeRunner and run() in the deploy fixture with the real script --------------------


class Call(NamedTuple):
    kind: str  # "pytest", "ruff", "health", or "<program> <subcommand>" such as "git merge" or "launchctl kickstart"
    cmd: str
    log_size: int  # bytes in data/deploys.log when the call started


def _kind(argv):
    if HEALTH_URL in " ".join(argv):
        return "health"
    if "tests/test_no_target_companies.py" in argv:
        return "guards"  # the real-data privacy guard run after the suite
    program = Path(argv[0]).name
    if program.startswith("python") and "-m" in argv:  # python -m pytest, python -m ruff
        return argv[argv.index("-m") + 1]
    if program in ("pytest", "ruff"):
        return program
    if program in ("git", "launchctl"):
        words = iter(argv[1:])
        for word in words:
            if word in ("-C", "-c"):
                next(words, None)  # skip the option's value
            elif not word.startswith("-"):
                return f"{program} {word}"
    return "unknown"


class FakeRunner:
    """The script's runner: git runs for real in throwaway dirs, the rest is scripted and recorded in order."""

    def __init__(self, repo, **exit_codes):
        self.repo = repo
        self.exit = {"pytest": 0, "ruff": 0, "guards": 0, "health": 0, "launchctl kickstart": 0, **exit_codes}
        self.calls = []
        self.suite_runs = []  # (cwd, HEAD seen in that cwd) for every pytest and ruff call
        self.violations = []  # what this fake refused to do, in case the script swallowed the AssertionError

    def __call__(self, argv, cwd=None, check=False, **_ignored):
        argv = [str(word) for word in argv]
        cwd = Path(cwd).resolve() if cwd else None
        kind = _kind(argv)
        if len(self.calls) > 500:  # a wait that never sees the faked clock advance
            self._refuse("runaway polling: does the wait use a clock other than time.monotonic or time.time?")
        log = self.repo.path / "data" / "deploys.log"
        self.calls.append(Call(kind, " ".join(argv), log.stat().st_size if log.exists() else 0))
        if kind.startswith("git "):
            home = Path(argv[2]) if argv[1:2] == ["-C"] else cwd
            if home is None or not home.resolve().is_relative_to(TEMP_ROOT):
                self._refuse(f"git without cwd= or -C inside the throwaway dirs would touch a real checkout: {argv}")
            done = _real_git(argv, cwd)
        elif kind in self.exit:
            if kind in ("pytest", "ruff"):
                head = _real_git(["git", "rev-parse", "HEAD"], cwd).stdout.strip() if cwd else None
                self.suite_runs.append((cwd, head))
            done = subprocess.CompletedProcess(argv, self.exit[kind], "", "")
        else:
            self._refuse(f"unexpected command: {argv}")
        if check:
            done.check_returncode()
        return done

    def _refuse(self, message):
        self.violations.append(message)
        raise AssertionError(message)

    def kinds(self):
        return [call.kind for call in self.calls]

    def first(self, wanted, after=0):
        """Index of the first call at or after `after` that `wanted` accepts. Fails with the whole trail if none does."""
        for index in range(after, len(self.calls)):
            if wanted(self.calls[index]):
                return index
        trail = "\n".join(f"  {i}: {call.cmd}" for i, call in enumerate(self.calls))
        raise AssertionError(f"expected call not found at or after #{after}; the calls were:\n{trail}")


class Outcome(NamedTuple):
    rc: int
    out: str  # stdout, stderr and log records together
    runner: FakeRunner


def _load_script(monkeypatch):
    path = ROOT / "scripts" / "deploy_local.py"
    assert path.exists(), "scripts/deploy_local.py does not exist yet: these tests were written ahead of the build"
    spec = importlib.util.spec_from_file_location("deploy_local_under_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses and pickling look a module up by name
    spec.loader.exec_module(module)
    return module


def _seal_processes_and_network(monkeypatch, bypassed):
    def guarded_init(self, *args, **kwargs):
        if not _real_git_running:
            bypassed.append(f"subprocess {args[:1]}")
            raise AssertionError(f"a subprocess went around the injected runner: {args[:1]}")
        return _REAL_POPEN_INIT(self, *args, **kwargs)

    def refuse(what):
        def blocked(*args, **kwargs):
            bypassed.append(what)
            raise AssertionError(f"{what} is not allowed here: no launchctl, pytest or network in this test")

        return blocked

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    monkeypatch.setattr(os, "system", refuse("os.system"))
    monkeypatch.setattr(socket.socket, "connect", refuse("a socket connect"))


class _VirtualClock:
    """time.sleep only moves an offset, so waiting 30 s for a dead service takes no wall time."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        real_monotonic, real_time = time.monotonic, time.time
        monkeypatch.setattr(time, "sleep", self.sleep)
        monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + self.offset)
        monkeypatch.setattr(time, "time", lambda: real_time() + self.offset)

    def sleep(self, seconds):
        self.offset += seconds


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path / "main")


@pytest.fixture
def deploy(monkeypatch, capsys, caplog):
    """deploy(repo, target, *flags, pytest=0, ruff=0, health=0) runs the script; the keywords are exit codes."""
    caplog.set_level(logging.DEBUG)
    bypassed = []
    _seal_processes_and_network(monkeypatch, bypassed)
    _VirtualClock(monkeypatch)

    def run(repo, target, *flags, **exit_codes):
        monkeypatch.setattr(paths, "DATA_DIR", repo.path / "data")  # "data/deploys.log" is one file under both readings
        runner = FakeRunner(repo, **exit_codes)
        script = _load_script(monkeypatch)
        try:
            code = script.main([target, "--repo", str(repo.path), *flags], runner=runner)
        except SystemExit as stop:
            code = stop.code
        seen = capsys.readouterr()
        reason = code if isinstance(code, str) else ""  # sys.exit("reason") prints the reason and exits 1
        assert not runner.violations and not bypassed, (runner.violations, bypassed)
        return Outcome(1 if reason else (code or 0), seen.out + seen.err + caplog.text + reason, runner)

    return run


# ---- the acceptance list -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("protected", ["AGENTS.md", "docs/permissions.md"])
def test_refuses_a_target_that_touches_a_protected_path(deploy, repo, protected):
    # The protected file is in the first commit and the tip commit is innocent, so checking only the tip would miss it.
    repo.branch("feature", {protected: "changed\n"}, {"src/app.py": "print('v2')\n"})
    before = repo.state()

    outcome = deploy(repo, "feature")

    assert outcome.rc != 0, outcome.out
    assert "protected files need the owner yes" in outcome.out.lower(), outcome.out
    assert repo.state() == before
    assert "launchctl kickstart" not in outcome.runner.kinds()


@pytest.mark.parametrize("failing", ["pytest", "ruff"])
def test_refuses_and_changes_nothing_when_the_suite_fails(deploy, repo, failing):
    repo.branch("feature", *FEATURE)
    repo.write("notes.txt", LOCAL_EDIT)
    before = repo.state()

    outcome = deploy(repo, "feature", **{failing: 1})

    assert outcome.rc != 0, outcome.out
    assert failing in outcome.runner.kinds(), "the refusal must come from the suite, so the suite has to have run"
    assert outcome.out.strip(), "a refusal gives a reason"
    assert repo.state() == before  # same HEAD, refs and dirty files, and the temporary worktree is gone
    assert "launchctl kickstart" not in outcome.runner.kinds()


def test_refuses_a_target_that_is_not_a_fast_forward(deploy, repo):
    repo.branch("feature", {"feature.txt": "new\n"})
    repo.commit({"local.txt": "only on master\n"}, "master moves on")  # master and feature have now diverged
    before = repo.state()

    outcome = deploy(repo, "feature")

    assert outcome.rc != 0, outcome.out
    assert re.search(r"fast[- ]?forward|ff[- ]?only|diverge|ancestor", outcome.out, re.I), outcome.out
    assert repo.state() == before
    assert "launchctl kickstart" not in outcome.runner.kinds()


def test_refuses_when_uncommitted_local_changes_overlap_the_target(deploy, repo):
    # Contract rule 4, added by the planner: never merge over the owner uncommitted edit to a file TARGET also changes.
    repo.branch("feature", *FEATURE)  # changes src/app.py
    repo.write("src/app.py", "print('local work')\n")
    before = repo.state()

    outcome = deploy(repo, "feature")

    assert outcome.rc != 0, outcome.out
    assert repo.state() == before
    assert (repo.path / "src/app.py").read_text() == "print('local work')\n"
    assert "launchctl kickstart" not in outcome.runner.kinds()


def test_dry_run_runs_every_check_and_changes_nothing(deploy, repo):
    target = repo.branch("feature", *FEATURE)
    repo.write("notes.txt", LOCAL_EDIT)
    before = repo.state()

    outcome = deploy(repo, target, "--dry-run")  # TARGET may be a commit as well as a branch

    assert outcome.rc == 0, outcome.out
    kinds = outcome.runner.kinds()
    assert "pytest" in kinds and "ruff" in kinds, "a dry run still runs the suite"
    for cwd, head in outcome.runner.suite_runs:  # in a clean checkout of TARGET, never the main checkout
        assert cwd is not None and cwd != repo.path.resolve()
        assert head == target
    assert "launchctl kickstart" not in kinds and "health" not in kinds
    assert repo.state() == before
    assert not (repo.path / "data" / "deploys.log").exists()


def test_happy_path_fast_forwards_then_restarts_then_checks_health_then_logs(deploy, repo):
    target = repo.branch("feature", *FEATURE)
    repo.write("notes.txt", LOCAL_EDIT)
    start = repo.head()

    outcome = deploy(repo, target)

    assert outcome.rc == 0, outcome.out
    runner = outcome.runner
    pytest_run = runner.first(lambda c: c.kind == "pytest")
    ruff_run = runner.first(lambda c: c.kind == "ruff")
    merge = runner.first(lambda c: c.kind == "git merge" and "--ff-only" in c.cmd)
    restart = runner.first(lambda c: c.kind == "launchctl kickstart" and "-k" in c.cmd.split() and RESTART in c.cmd)
    health = runner.first(lambda c: c.kind == "health", restart)
    assert max(pytest_run, ruff_run) < merge < restart < health  # nothing changes until the suite has passed
    assert runner.calls[health].log_size == 0, "the log line comes after the health check"
    lines = (repo.path / "data" / "deploys.log").read_text().splitlines()
    assert len(lines) == 1
    assert start[:7] in lines[0] and target[:7] in lines[0], "the line names the from and to commits"
    assert "reset --keep" in lines[0], "the line carries the rollback command"
    assert repo.head() == target and repo.git("rev-parse", "master") == target  # master moved; HEAD is not detached
    assert (repo.path / "notes.txt").read_text() == LOCAL_EDIT


def test_a_failed_health_check_rolls_back_to_the_previous_commit(deploy, repo):
    target = repo.branch("feature", *FEATURE)
    repo.write("notes.txt", LOCAL_EDIT)
    start = repo.head()

    outcome = deploy(repo, target, health=7)  # the service never answers

    assert outcome.rc != 0, outcome.out
    assert "roll" in outcome.out.lower(), "the script says it rolled back"
    runner = outcome.runner
    merge = runner.first(lambda c: c.kind == "git merge" and "--ff-only" in c.cmd)
    restart = runner.first(lambda c: c.kind == "launchctl kickstart" and RESTART in c.cmd, merge)
    health = runner.first(lambda c: c.kind == "health", restart)
    reset = runner.first(lambda c: c.kind == "git reset" and "--keep" in c.cmd.split(), health)
    runner.first(lambda c: c.kind == "launchctl kickstart" and RESTART in c.cmd, reset)  # the old code is restarted
    assert repo.head() == start and repo.git("rev-parse", "master") == start
    assert (repo.path / "src" / "app.py").read_text() == BASE_FILES["src/app.py"]
    assert (repo.path / "notes.txt").read_text() == LOCAL_EDIT, "--keep, never --hard: local edits survive"


def _gate_receipt(repo, target_sha, **changes):
    import json
    from datetime import datetime

    receipt = {'t': datetime.now(UTC).isoformat(), 'sha': target_sha,
               'tree': repo.git('rev-parse', f'{target_sha}^{{tree}}'),
               'writer': 'workflow_land', 'suite': 'pass', 'mode': 'full', 'browser_tests': []}
    receipt.update(changes)
    repo.write('data/lands.log', 'time=legacy suite=pass\n' + json.dumps(receipt) + '\n')


@pytest.mark.parametrize('change', [
    {'t': '2000-01-01T00:00:00+00:00'}, {'sha': '0' * 40}, {'tree': '0' * 40},
    {'writer': 'other'}, {'suite': 'fail'}, {'t': '2999-01-01T00:00:00+00:00'},
    {'t': 'invalid'},
])
def test_trust_gate_rejects_invalid_receipt(repo, deploy, change):
    sha = repo.branch('feature', *FEATURE)
    _gate_receipt(repo, sha, **change)
    before = repo.head()
    outcome = deploy(repo, sha, '--trust-gate', sha)
    assert outcome.rc == 1
    assert repo.head() == before
    assert not any(call.kind in {'ruff', 'pytest', 'guards', 'git merge'} for call in outcome.runner.calls)


def test_trust_gate_missing_receipt(repo, deploy):
    sha = repo.branch('feature', *FEATURE)
    assert deploy(repo, sha, '--trust-gate', sha).rc == 1


@pytest.mark.parametrize('guard_exit', [0, 1])
def test_trust_gate_always_runs_guards_and_skips_suite(repo, deploy, guard_exit):
    sha = repo.branch('feature', *FEATURE)
    _gate_receipt(repo, sha)
    before = repo.head()
    outcome = deploy(repo, sha, '--trust-gate', sha, guards=guard_exit)
    kinds = [call.kind for call in outcome.runner.calls]
    assert 'guards' in kinds
    assert 'ruff' not in kinds and 'pytest' not in kinds
    assert outcome.rc == guard_exit
    assert repo.head() == (before if guard_exit else sha)
    if not guard_exit:
        assert f'trusted gate {sha} ' in (repo.path / 'data/deploys.log').read_text()


def test_trust_gate_sha_must_be_exact_target(repo, deploy):
    sha = repo.branch('feature', *FEATURE)
    _gate_receipt(repo, sha)
    assert deploy(repo, sha, '--trust-gate', repo.head()).rc == 1

def test_local_fallback_without_xdist(tmp_path, monkeypatch):
    module = _load_script(monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(module, "_remote_pytest", lambda *args: None)
    monkeypatch.delenv("KYRA_SPLIT_TESTS", raising=False)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")

    module._pytest_phases(runner, sys.executable, tmp_path, "abc")
    assert len(calls) == 1
    assert "pytest" in calls[0] and "-n" not in calls[0]
