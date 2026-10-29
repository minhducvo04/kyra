"""scripts/remote_test.py: guards never run on the runner, 75 means only transport failure, a timeout kills the run.

The runner is simulated by running the generated shell lines with bash on this Mac (ssh_argv -> bash -c) against a
tiny git repo, so pytest really collects and executes; only SSH itself is replaced.
"""
import importlib.util
import json
import os
import re

try:
    import resource
except ImportError:  # Windows has no rlimit
    resource = None
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("remote_test", ROOT / "scripts/remote_test.py")
remote_test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote_test)

GUARD = "tests/test_no_personal_files.py"
# a rootdir one level up turns the node id into <sha>/tests/..., which --deselect=tests/... no longer matches
ROOTDIR_ALIAS = ["--rootdir=..", GUARD]
RUNNER_ULIMIT = "ulimit -n 8192"  # the runner setup line's own request (remote_command)


def fresh_session(command: str) -> str:
    """The command as a runner session gets it. sshd starts a session with the runner's own limits; these fakes
    inherit this xdist worker's instead, and importing mlx_lm in any earlier test on the worker
    (mlx_lm/utils.py: setrlimit(NOFILE, (2048, 4096))) caps the hard open-file limit at 4096 for good, so the
    setup's `ulimit -n 8192` failed here only (16 pool tests on one worker, protected-tip run 2026-10-16). Lower
    that one request to the cap this process has; the command sent to ssh is asserted unchanged elsewhere."""
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < 8192:
        return command.replace(RUNNER_ULIMIT, f"ulimit -n {hard}")
    return command


TREE = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    GUARD: ("import os\n\ndef test_guard():\n"
            "    open(os.environ['GUARD_MARKER'], 'w').write('GUARD_EXECUTED')\n    assert False\n"),
    "tests/test_sleep.py": ("import os, subprocess, time\n\ndef test_sleep():\n"
                            "    if not os.environ.get('SLEEP_PIDS'):\n        return\n"
                            "    child = subprocess.Popen(['sleep', '300'])\n"
                            "    open(os.environ['SLEEP_PIDS'], 'w').write(f'{os.getpid()} {child.pid}')\n"
                            "    time.sleep(300)\n"),
    "conftest.py": ("import os, pytest\n\ndef pytest_sessionfinish(session, exitstatus):\n"
                    "    if os.environ.get('FAKE_EXIT'):\n"
                    "        pytest.exit('forced', returncode=int(os.environ['FAKE_EXIT']))\n"),
}


@pytest.fixture(autouse=True)
def _no_where_switches(monkeypatch):
    """train.py's --where switches never decide a test here unless the test sets them (o-rev91 env leak)."""
    for key in remote_test.WHERE_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """A committed tiny repo as ROOT and a temp dir as the runner home, reached through bash instead of ssh."""
    monkeypatch.setenv("KYRA_M1_HOST", "runner.example.test")
    monkeypatch.setattr(remote_test, "BUSY_WAIT_SECONDS", 0)  # these cases expect the immediate busy answer
    monkeypatch.setenv("KYRA_M1_KEY", "/fixture/key")
    repo = tmp_path / "repo"
    for rel, text in TREE.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "tree", "--no-verify"], check=True)
    base = tmp_path / "runner"
    (base / ".venv/bin").mkdir(parents=True)
    python = base / ".venv/bin/python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    marker = tmp_path / "guard-ran"
    monkeypatch.setenv("GUARD_MARKER", str(marker))
    monkeypatch.delenv("FAKE_EXIT", raising=False)
    monkeypatch.setattr(remote_test, "ROOT", repo)
    monkeypatch.setattr(remote_test, "BASE", str(base))
    monkeypatch.setattr(remote_test, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(remote_test, "ssh_argv", lambda command, **kw: ["bash", "-c", fresh_session(command)])
    return {"repo": repo, "base": base, "marker": marker, "tmp": tmp_path}


def test_a_subset_and_the_whole_tree_pass_without_running_the_guard(runner):
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    assert remote_test.run("HEAD", [], 1) == 0
    assert not runner["marker"].exists(), "directory discovery must skip the guard"
    log = next((runner["tmp"] / "logs").iterdir()).read_text()
    assert "passed" in log and "reported exit 0" in log


@pytest.mark.parametrize("args", [
    [GUARD], [f"{GUARD}::test_guard"], ["./tests/../tests/TEST_NO_PERSONAL_FILES.py"],
    ["/elsewhere/kyra/tests/test_no_third_party_pii.py"], ["-k", "no_personal_files"], ["-knot no_third_party_pii"],
    ["--pyargs", "tests"], ["@args.txt"], ["tests/test_no_target_companies.py[1]"],
])
def test_explicit_guard_targets_are_refused_before_any_transfer(args, monkeypatch, capsys):
    def no_subprocess(*a, **k):
        raise AssertionError("nothing may be sent for a refused target")

    monkeypatch.setattr(remote_test.subprocess, "run", no_subprocess)
    monkeypatch.setattr(remote_test.subprocess, "Popen", no_subprocess)
    assert remote_test.run("HEAD", args, 1) == remote_test.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


def test_absolute_alias_of_the_real_guard_is_refused():
    assert remote_test.refused_targets([str(ROOT / GUARD), "tests/test_ok.py"]) == [str(ROOT / GUARD)]


def test_the_runner_aborts_if_collection_still_finds_a_guard(runner, monkeypatch):
    monkeypatch.setattr(remote_test, "refused_targets", lambda args: [])  # pretend an alias slipped past
    assert remote_test.run("HEAD", ROOTDIR_ALIAS, 1) == remote_test.EXIT_REFUSED
    assert not runner["marker"].exists(), "the guard test must not execute"
    run_dir = next((runner["base"] / "runs").iterdir())
    assert not (run_dir / "pytest.log").exists() and "test_no_personal_files.py" in (run_dir / "collect.log").read_text().lower()


@pytest.mark.parametrize("line, refused", [
    ("tests/test_rq_brief.py::test_x[test_no_personal_files.py]", False),  # batch-b57d01ab722a3821, 2026-10-23
    ("tests/test_rq_brief.py::test_x[tests/test_no_third_party_pii.py]", False),
    ("tests/test_no_personal_files.py::test_y", True),
    ("../repo/tests/TEST_NO_PERSONAL_FILES.py::test_y", True),
])
def test_runner_check_matches_collected_file_paths_not_parameter_ids(tmp_path, line, refused):
    (tmp_path / "collect.log").write_text(f"tests/test_ok.py::test_ok\n{line}\n")
    found = subprocess.run(["bash", "-c", remote_test.GUARD_GREP], cwd=tmp_path, capture_output=True)
    assert (found.returncode == 0) is refused


def test_ignore_and_deselect_stop_the_exact_guard_path(runner, monkeypatch):
    monkeypatch.setattr(remote_test, "refused_targets", lambda args: [])
    assert remote_test.run("HEAD", [GUARD, f"{GUARD}::test_guard"], 1) == 5  # nothing selected
    assert not runner["marker"].exists()


def test_without_the_check_the_explicit_guard_would_really_run(runner):
    """Control for the collection check: without it the rootdir alias slips past --ignore and --deselect and runs."""
    sha = subprocess.run(["git", "-C", str(runner["repo"]), "rev-parse", "--short=12", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    run_dir = runner["base"] / "runs" / sha
    run_dir.mkdir(parents=True)
    subprocess.run(f"git -C {runner['repo']} archive HEAD | tar -x -C {run_dir}", shell=True, check=True)
    cmd = remote_test.remote_command(sha, ROOTDIR_ALIAS, 1, base=str(runner["base"]))
    assert f"if {remote_test.GUARD_GREP};" in cmd
    cmd = cmd.replace(f"if {remote_test.GUARD_GREP};", "if false;")
    done = subprocess.run(["bash", "-c", fresh_session(cmd)], capture_output=True, text=True)
    assert runner["marker"].read_text() == "GUARD_EXECUTED" and done.returncode == 1


@pytest.mark.parametrize("code", [75, 124])
def test_a_completed_run_ending_75_or_124_is_reported_as_1(runner, monkeypatch, code, capsys):
    monkeypatch.setenv("FAKE_EXIT", str(code))
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 1
    log = next((runner["tmp"] / "logs").iterdir()).read_text()
    assert f"{remote_test.EXIT_SENTINEL}{code}" in log and f"pytest exited {code} on the runner" in log
    assert f"pytest exited {code} on the runner" in capsys.readouterr().out


def test_only_transport_failure_maps_to_75(runner, monkeypatch):
    s = remote_test.EXIT_SENTINEL
    assert remote_test.outcome(255, "")[0] == 75
    assert remote_test.outcome(255, f"x\n{s}0\n")[0] == 0
    assert remote_test.outcome(0, f"{s}75\n")[0] == 1
    assert remote_test.outcome(75, "")[0] == 1, "setup failure on the runner is not unreachable"
    assert remote_test.outcome(0, f"{s}0\nmore output\n")[0] == 1, "a sentinel must be the final line"
    assert remote_test.outcome(3, f"{s}3\n")[0] == 3
    monkeypatch.setattr(remote_test, "ssh_argv", lambda command, **kw: ["bash", "-c", "cat >/dev/null; exit 255"])
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 75


def _group_members(pgid):
    out = subprocess.run(["ps", "-A", "-o", "pgid=", "-o", "pid="], capture_output=True, text=True).stdout
    return [line.split()[1] for line in out.splitlines() if line.split() and line.split()[0] == str(pgid)]


def test_a_timeout_kills_the_whole_process_group_and_verifies_it(runner, monkeypatch, capsys):
    pids = runner["tmp"] / "pids"
    monkeypatch.setenv("SLEEP_PIDS", str(pids))
    monkeypatch.setattr(remote_test, "TIMEOUT", 8)
    assert remote_test.run("HEAD", ["tests/test_sleep.py"], 1) == remote_test.EXIT_TIMEOUT
    pytest_pid, child_pid = (int(p) for p in pids.read_text().split())
    run_dir = next((runner["base"] / "runs").iterdir())
    pgid = int((run_dir / "pgid").read_text())
    assert pytest_pid == pgid, "pytest leads its own process group"
    assert _group_members(pgid) == [], "pytest and its sleep child must be gone"
    for pid in (pytest_pid, child_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert f"process group {pgid} verified empty" in capsys.readouterr().err
    assert not (runner["base"] / ".lock").exists(), "a timeout kill still releases the lock"


def test_cleanup_failure_is_reported_not_claimed(monkeypatch):
    monkeypatch.setattr(remote_test, "CLEANUP_TIMEOUT", 1)
    monkeypatch.setattr(remote_test, "ssh_argv", lambda command, **kw: ["bash", "-c", "sleep 5"])
    assert "timed out" in remote_test.stop_remote("abc") and "may still be going" in remote_test.stop_remote("abc")
    monkeypatch.setattr(remote_test, "ssh_argv", lambda command, **kw: ["bash", "-c", "echo hello; exit 255"])
    assert "gave no answer" in remote_test.stop_remote("abc")
    monkeypatch.setattr(remote_test, "ssh_argv", lambda command, **kw: ["/nonexistent/ssh"])
    assert "could not start" in remote_test.stop_remote("abc")
    monkeypatch.setattr(remote_test, "ssh_argv",
                        lambda command, **kw: ["bash", "-c", f"echo {remote_test.CLEANUP_SENTINEL}alive:9:9 10"])
    assert "still alive" in remote_test.stop_remote("abc")


def test_workers_add_xdist_flag_to_the_run_not_the_collection_check():
    cmd = remote_test.remote_command("abc123", ["-k", "a b"], workers=2)
    run_part = cmd.split("perl -e")[1]
    assert "-m pytest -p no:cacheprovider -n 2" in run_part and "'a b'" in cmd
    assert "--collect-only -q -k 'a b'" in cmd


def test_a_runner_shard_with_workers_runs_pytest_with_n():
    files = ["tests/test_a.py", "tests/test_b.py"]
    cmd = remote_test.remote_command("abc123", [*files, *remote_test.JUNIT_ARGS], workers=5)
    run_part = cmd.split("perl -e")[1]
    assert "-m pytest -p no:cacheprovider -n 5 tests/test_a.py tests/test_b.py" in run_part
    assert "--collect-only -q tests/test_a.py" in cmd and "-n 5" not in cmd.split("perl -e")[0]


def test_summary_line_prefers_pytest_summary():
    log = "....\nFAILED tests/x.py::t\n===== 1 failed, 12 passed in 3.40s =====\nKYRA_PYTEST_EXIT=1\n"
    assert remote_test.summary_line(log) == "1 failed, 12 passed in 3.40s"
    assert remote_test.summary_line("") == "(no output)"


def test_ssh_uses_batch_mode_and_the_runner_key():
    argv = remote_test.ssh_argv("true", host="user@host.example", key="/k")
    assert argv[:3] == ["ssh", "-i", "/k"] and "BatchMode=yes" in argv and argv[-2:] == ["user@host.example", "/bin/sh -c true"]


def _hold(runner, token="other", sha="deadbeef0000", age=5, pgid=None):
    import time
    lock = runner["base"] / ".lock"
    lock.mkdir(parents=True)
    (lock / "info").write_text(f"{token} 1 {sha} {int(time.time()) - age}\n")
    if pgid is not None:
        (runner["base"] / "runs" / sha).mkdir(parents=True)
        (runner["base"] / "runs" / sha / "pgid").write_text(str(pgid))
    return lock


def test_second_run_is_refused_while_a_live_lock_exists(runner, capsys):
    lock = _hold(runner)
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == remote_test.EXIT_BUSY == 73
    assert "another run holds the runner lock" in capsys.readouterr().err
    assert "other" in (lock / "info").read_text(), "the holder's lock is untouched"


def test_stale_lock_too_old_is_taken_over(runner):
    _hold(runner, age=remote_test.TIMEOUT + remote_test.TRANSFER_TIMEOUT + remote_test.CLEANUP_TIMEOUT + 5)
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    assert not (runner["base"] / ".lock").exists()


def test_stale_lock_with_dead_process_group_is_taken_over(runner):
    p = subprocess.Popen(["true"])
    p.wait()
    _hold(runner, age=120, pgid=p.pid)  # a finished pid: no group member left
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    assert not (runner["base"] / ".lock").exists()


def test_live_process_group_keeps_the_lock(runner):
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _hold(runner, age=120, pgid=p.pid)
        assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == remote_test.EXIT_BUSY
    finally:
        p.kill()
        p.wait()


def test_lock_is_released_after_success(runner):
    # The timeout-kill half (an 8 s real timeout) is asserted in
    # test_a_timeout_kills_the_whole_process_group_and_verifies_it, which runs the same kill (test diet, 2026-10-15).
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    assert not (runner["base"] / ".lock").exists()


def test_release_only_removes_our_own_lock(runner):
    lock = _hold(runner, token="theirs")
    remote_test.release_lock("mine")
    assert lock.exists()
    remote_test.release_lock("theirs")
    assert not lock.exists()


def test_takeover_backs_off_when_the_lock_changed_between_read_and_move(runner):
    lock = _hold(runner, age=remote_test.TIMEOUT + remote_test.TRANSFER_TIMEOUT + remote_test.CLEANUP_TIMEOUT + 5)
    cmd = remote_test.lock_command("mine", "abc", 10, base=str(runner["base"]))
    race = f'echo "rival 1 other $(date +%s)" > {lock}/info; if [ -n "$stale" ]'
    cmd = cmd.replace('if [ -n "$stale" ]', race, 1)
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout
    assert remote_test.LOCK_SENTINEL + "held" in out and "acquired" not in out
    assert "rival" in (lock / "info").read_text(), "the rival's lock is put back, not deleted"


def _stub_runner(tmp_path):
    """A runner dir whose python records each pytest call and exits with a chosen code per phase."""
    base = tmp_path / "rb"
    run = base / "runs/abc123"
    (run / "tests").mkdir(parents=True)
    (run / "tests/test_x_browser.py").write_text("")
    python = base / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\ncase "$*" in *--collect-only*) exit 0;; esac\n'
                      'echo "CALL $*" >> "$CALLS"\n'
                      'case "$*" in *" -n "*) exit $PHASE1;; *) exit $PHASE2;; esac\n')
    python.chmod(0o755)
    return base, run


def _run_stub(tmp_path, monkeypatch, args, workers, phase1, phase2):
    base, run = _stub_runner(tmp_path)
    monkeypatch.setenv("CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("PHASE1", str(phase1))
    monkeypatch.setenv("PHASE2", str(phase2))
    cmd = remote_test.remote_command("abc123", args, workers, base=str(base))
    proc = subprocess.run(["bash", "-c", fresh_session(cmd)], capture_output=True, text=True)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return proc, calls


def test_workers_run_non_browser_in_parallel_then_browser_serially(tmp_path, monkeypatch):
    proc, calls = _run_stub(tmp_path, monkeypatch, [], 3, 0, 0)
    assert len(calls) == 2 and proc.returncode == 0
    assert "-n 3" in calls[0] and "--ignore-glob=tests/test_*_browser.py" in calls[0].replace("'", "")
    assert " -n " not in calls[1] and "tests/test_x_browser.py" in calls[1]


@pytest.mark.parametrize(("phase1", "phase2", "expected"), [(0, 0, 0), (1, 0, 1), (0, 1, 1), (1, 2, 1)])
def test_exit_codes_of_both_phases_combine(tmp_path, monkeypatch, phase1, phase2, expected):
    proc, calls = _run_stub(tmp_path, monkeypatch, [], 2, phase1, phase2)
    assert len(calls) == 2, "the browser phase runs even when the first fails"
    assert proc.returncode == expected
    assert f"{remote_test.EXIT_SENTINEL}{expected}" in proc.stdout


def test_explicit_args_keep_the_single_command(tmp_path, monkeypatch):
    proc, calls = _run_stub(tmp_path, monkeypatch, ["-k", "a"], 3, 0, 0)
    assert len(calls) == 1 and "-n 3" in calls[0] and "_browser" not in calls[0]


def test_summary_shows_both_phases_so_a_red_first_phase_is_not_hidden():
    log = "== 1 failed, 8000 passed in 90s ==\n== 40 passed in 60s ==\nKYRA_PYTEST_EXIT=1\n"
    assert remote_test.summary_line(log) == "parallel: 1 failed, 8000 passed in 90s; browser: 40 passed in 60s"


def test_browser_phase_is_skipped_when_there_are_no_browser_files(tmp_path, monkeypatch):
    base, run = _stub_runner(tmp_path)
    (run / "tests/test_x_browser.py").rename(run / "tests/test_x.py")
    monkeypatch.setenv("CALLS", str(tmp_path / "calls"))
    monkeypatch.setenv("PHASE1", "0")
    monkeypatch.setenv("PHASE2", "1")
    cmd = remote_test.remote_command("abc123", [], 2, base=str(base))
    proc = subprocess.run(["bash", "-c", fresh_session(cmd)], capture_output=True, text=True)
    assert len((tmp_path / "calls").read_text().splitlines()) == 1 and proc.returncode == 0


def test_remote_report_keeps_failure_before_large_noise(tmp_path, monkeypatch):
    base, _ = _stub_runner(tmp_path)
    python = base / '.venv/bin/python'
    python.write_text('#!/bin/sh\ncase "$*" in *--collect-only*) exit 0;; esac\n'
                      'echo "FAILED tests/test_sample.py::test_bad - broken"\n'
                      'echo "PASSED tests/test_sample.py::test_good"\n'
                      'echo "1 failed in 1s"\n'
                      'head -c 210000 /dev/zero | tr "\\000" x\n'
                      'echo\nexit 1\n')
    command = remote_test.remote_command('abc123', ['--ignore-glob=tests/test_*_browser.py'], 2, base=str(base))
    result = subprocess.run(['bash', '-c', fresh_session(command)], capture_output=True, text=True)
    report = result.stdout.split('KYRA_PYTEST_REPORT\n')[-1]
    assert result.returncode == 1
    assert 'FAILED tests/test_sample.py::test_bad' in report
    assert 'PASSED tests/test_sample.py::test_good' in report
    assert '1 failed in 1s' in report
    assert 'xxx' not in report


def test_runner_settings_env_overrides_private_config(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/m1.json").write_text('{"host":"file.example.test","key":"/file/key"}')
    monkeypatch.setenv("KYRA_M1_HOST", "env.example.test")
    monkeypatch.setenv("KYRA_M1_KEY", "/env/key")
    assert remote_test.runner_settings() == ("env.example.test", "/env/key")
    monkeypatch.delenv("KYRA_M1_HOST")
    monkeypatch.delenv("KYRA_M1_KEY")
    assert remote_test.runner_settings() == ("file.example.test", "/file/key")


def test_missing_config_is_one_plain_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KYRA_M1_HOST", raising=False)
    monkeypatch.delenv("KYRA_M1_KEY", raising=False)
    assert remote_test.run("HEAD", [], 1) == 75
    assert capsys.readouterr().err == "remote_test: runner is not configured; use local tests\n"


def test_host_never_appears_in_output_or_saved_log(runner, monkeypatch, capsys):
    host = "private-runner.example.test"
    monkeypatch.setenv("KYRA_M1_HOST", "user@" + host)
    monkeypatch.setattr(remote_test, "acquire_lock", lambda *a: (True, ""))
    monkeypatch.setattr(remote_test, "release_lock", lambda *a: None)
    # The transport can echo either the destination or just its hostname.
    monkeypatch.setattr(remote_test, "ssh_argv", lambda *a, **k: [
        "bash", "-c", f"cat >/dev/null; echo '1 passed in 0.1s {host} user@{host}'; echo KYRA_PYTEST_EXIT=0"])
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    captured = capsys.readouterr()
    assert host not in captured.out + captured.err
    assert all(host not in p.read_text() for p in (runner["tmp"] / "logs").iterdir())


def test_runner_settings_from_clean_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "main"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "--allow-empty", "-qm", "fixture"], check=True)
    work = tmp_path / "clean"
    subprocess.run([*git, "worktree", "add", "--detach", str(work)], check=True, capture_output=True)
    config = repo / "data/config"
    config.mkdir(parents=True)
    (config / "m1.json").write_text('{"host":"runner.example.test","key":"/fixture/key"}')
    for name in ("KYRA_DATA_DIR", "KYRA_M1_HOST", "KYRA_M1_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(remote_test, "ROOT", work)
    assert not (work / "data").exists()
    assert remote_test.runner_settings() == ("runner.example.test", "/fixture/key")


@pytest.mark.parametrize("remote_code", [0, 1, 2, 5, 73, 75, 124, 127])
def test_landing_browser_falls_back_only_when_unreachable(tmp_path, monkeypatch, caplog, remote_code):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))  # the fallback imports deploy_local, as the CLI can
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        code = remote_code if "--ref" in argv else 0
        return subprocess.CompletedProcess(argv, code, "log: /tmp/runner-browser.log\n", "")

    browser = [sys.executable, "-m", "pytest", "-p", "no:xdist",
               "tests/test_x_browser.py::test_one"]
    wrapped = remote_test.m1_browser_runner(execute, "abc123")
    with caplog.at_level("INFO"):
        result = wrapped(browser, cwd=tmp_path, timeout=42)
    assert calls[0] == ([sys.executable, str(tmp_path / "scripts/remote_test.py"),
                         "--ref", "abc123", "--workers", "1", "--", *browser[3:]],
                        {"cwd": tmp_path, "timeout": 42})
    if remote_code == 75:
        assert calls[1] == (browser, {"cwd": tmp_path, "timeout": 42})
        assert result.returncode == 0
        assert "fallback" in caplog.text and "unreachable" in caplog.text
    else:
        assert len(calls) == 1 and result.returncode == remote_code
        assert "fallback" not in caplog.text
        assert "local browser seconds=0" in caplog.text
        assert "/tmp/runner-browser.log" in caplog.text


def test_landing_non_browser_commands_are_unchanged(tmp_path):
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    wrapped = remote_test.m1_browser_runner(execute, "abc123")
    for argv in (["git", "status"], [sys.executable, "-m", "ruff", "check", "src"],
                 [sys.executable, "-m", "pytest", "--ignore-glob=tests/test_*_browser.py"],
                 [sys.executable, "-m", "pytest", "-q", GUARD]):
        wrapped(argv, cwd=tmp_path)
        assert calls[-1] == (argv, {"cwd": tmp_path})


def test_split_landing_serializes_remote_phases(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    first_started, release_first, browser_started = (threading.Event() for _ in range(3))
    active = []

    def execute(argv, **kwargs):
        assert not active, "one train must not contend with itself for the M1 lock"
        active.append(argv)
        try:
            if "--ignore-glob=tests/test_*_browser.py" in argv:
                first_started.set()
                assert release_first.wait(5)
            else:
                browser_started.set()
            return subprocess.CompletedProcess(argv, 0, "", "")
        finally:
            active.pop()

    # this Mac is busy: the non-browser half goes to the M1 too, one phase at a time (today's order)
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: "load average 30.0 is above 16.2")
    wrapped = remote_test.m1_browser_runner(execute, "abc123")
    with ThreadPoolExecutor(max_workers=2) as pool:
        non_browser = pool.submit(wrapped, [sys.executable, str(tmp_path / "scripts/remote_test.py"),
                                           "--ref", "abc123", "--", "--ignore-glob=tests/test_*_browser.py"],
                                  cwd=tmp_path)
        try:
            assert first_started.wait(5)
            browser = pool.submit(wrapped, [sys.executable, "-m", "pytest", "-p", "no:xdist",
                                            "tests/test_x_browser.py"], cwd=tmp_path)
            assert not browser_started.wait(0.1)
        finally:
            release_first.set()
        assert non_browser.result().returncode == browser.result().returncode == 0
    assert browser_started.is_set()


def test_serial_browser_run_preserves_the_non_browser_log(runner):
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 0
    first_log = next((runner["tmp"] / "logs").iterdir())
    first_text = first_log.read_text()
    assert remote_test.run("HEAD", ["-p", "no:xdist", "tests/test_ok.py"], 1) == 0
    assert first_log.read_text() == first_text
    browser_log = next((runner["tmp"] / "logs").glob("*-browser.log")).read_text()
    assert "KYRA_TEST_PHASE=browser runner=M1" in browser_log
    assert "1 passed" in browser_log and "reported exit 0" in browser_log
    assert not runner["marker"].exists()


@pytest.mark.parametrize("unpack_code", [1, 75, 124])
def test_remote_unpack_failure_does_not_allow_local_fallback(runner, monkeypatch, unpack_code):
    monkeypatch.setattr(remote_test, "acquire_lock", lambda *a: (True, ""))
    monkeypatch.setattr(remote_test, "release_lock", lambda *a: None)
    monkeypatch.setattr(remote_test, "ssh_argv", lambda *a: ["bash", "-c", f"cat >/dev/null; exit {unpack_code}"])
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == 1


@pytest.mark.parametrize("split,remote", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("selected", [None, ["tests/test_x_browser.py::test_one"]])
def test_landing_phase_dispatch_preserves_scope_in_every_mode(tmp_path, monkeypatch, split, remote, selected):
    sys.path.insert(0, str(ROOT / "scripts"))
    import deploy_local
    import workflow_land

    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/remote_test.py").touch()
    (tmp_path / "tests").mkdir()
    for name in ("x", "y"):
        (tmp_path / f"tests/test_{name}_browser.py").touch()
    monkeypatch.setenv("KYRA_SPLIT_TESTS", "1" if split else "0")
    monkeypatch.setenv("KYRA_REMOTE_TESTS", "1" if remote else "0")
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    wrapped = workflow_land.browser_runner(remote_test.m1_browser_runner(execute, "abc123"), selected)
    deploy_local._pytest_phases(wrapped, sys.executable, tmp_path, "abc123")
    assert not any(argv[1:5] == ["-m", "pytest", "-p", "no:xdist"] for argv in calls)
    if remote and not split:
        assert len(calls) == 1 and "--ref" in calls[0]  # remote_test owns both phases in this mode
    else:
        browser = next(argv for argv in calls if "no:xdist" in argv)
        assert browser[browser.index("no:xdist") + 1:] == (
            selected or ["tests/test_x_browser.py", "tests/test_y_browser.py"])
        assert "--workers" in browser and browser[browser.index("--workers") + 1] == "1"


def test_landing_unconfigured_runner_logs_local_fallback(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("", ""))
    calls = []
    browser = [sys.executable, "-m", "pytest", "-p", "no:xdist", "tests/test_x_browser.py"]

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "1 failed", "")

    result = remote_test.m1_browser_runner(execute, "abc123")(browser, cwd=tmp_path)
    assert calls == [browser] and result.returncode == 1
    assert "runner not configured" in caplog.text and "fallback to local" in caplog.text


@pytest.mark.parametrize("code", [0, 1, 75, 124])
def test_runner_lock_setup_error_does_not_allow_fallback(runner, monkeypatch, code):
    monkeypatch.setattr(remote_test, "ssh_argv", lambda *a: ["bash", "-c", f"exit {code}"])
    assert remote_test.run("HEAD", ["tests/test_ok.py"], 1) == remote_test.EXIT_REFUSED
    assert not (runner["base"] / "runs").exists()


def _landing_tree(tmp_path):
    work = tmp_path / "checkout"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts/remote_test.py").write_text("# the candidate's copy\n")
    (work / "tests").mkdir()
    (work / "tests/test_x_browser.py").write_text("def test_x():\n    pass\n")
    return work


def _deploy_local():
    spec = importlib.util.spec_from_file_location("deploy_local_for_lb_tp", ROOT / "scripts/deploy_local.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("codes", [(0, 0), (1, 0), (0, 1)])
def test_the_landing_runs_both_phases_at_once_and_fails_if_either_fails(tmp_path, monkeypatch, caplog, codes):
    """lb-tp (2026-10-15): train46 ran 950 s non-browser then 2,031 s browser on the M1, one after the other. With
    this Mac free, the non-browser half runs here (xdist, the same selection) while the M1 runs the browser phase:
    both calls start before either returns (a two-party barrier), the gate passes only when both pass, and the log
    has one line per phase with its own seconds."""
    import threading

    deploy_local = _deploy_local()
    monkeypatch.setenv("KYRA_SPLIT_TESTS", "1")
    monkeypatch.setenv("KYRA_REMOTE_TESTS", "1")
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: None)
    monkeypatch.setattr(remote_test, "local_phase_helpers", lambda: {})  # never real pids from a test
    both_started = threading.Barrier(2, timeout=5)
    calls = []
    local_code, browser_code = codes

    def execute(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["-m", "pytest"] and "-n" in argv:
            both_started.wait()  # the M1 browser phase has started too
            return subprocess.CompletedProcess(argv, local_code, f"11000 {'passed' if not local_code else 'failed'}\n", "")
        if Path(argv[1]).name == "remote_test.py" and remote_test.NON_BROWSER_GLOB not in argv:
            both_started.wait()  # and so has the non-browser half here
            return subprocess.CompletedProcess(argv, browser_code, "log: /tmp/runner-browser.log\n", "")
        raise AssertionError(f"unexpected call {argv}")

    work = _landing_tree(tmp_path)
    wrapped = remote_test.m1_browser_runner(execute, "abc123")
    with caplog.at_level("INFO"):
        if codes == (0, 0):
            deploy_local._pytest_phases(wrapped, sys.executable, work, "abc123")
        else:
            with pytest.raises(deploy_local.Refused, match="the suite failed"):
                deploy_local._pytest_phases(wrapped, sys.executable, work, "abc123")
    local = [c for c in calls if "-n" in c]
    assert len(local) == 1 and remote_test.NON_BROWSER_GLOB in local[0]
    assert not any(Path(c[1]).name == "remote_test.py" and remote_test.NON_BROWSER_GLOB in c for c in calls)
    assert "non-browser phase: this Mac exit=" in caplog.text and "seconds=" in caplog.text
    assert "browser phase: M1 exit=" in caplog.text


def test_a_busy_mac_falls_back_to_the_m1_for_both_phases(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: "another heavy job is running: pid 7 mlx_lm")
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    non_browser = [sys.executable, str(tmp_path / "scripts/remote_test.py"), "--ref", "abc123", "--workers", "2",
                   "--", remote_test.NON_BROWSER_GLOB]
    with caplog.at_level("INFO"):
        assert remote_test.m1_browser_runner(execute, "abc123")(non_browser, cwd=tmp_path).returncode == 0
    assert calls == [non_browser]
    assert "sequential fallback: another heavy job" in caplog.text and "non-browser phase: M1 exit=0" in caplog.text


def test_the_local_check_ignores_this_train_and_refuses_other_load(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import low_power

    def probes(ps, load=1.0):
        return low_power.Probes(processes=lambda: ps, parents=lambda: {50: 40, 60: 50, 40: 1, 99: 1},
                                load=lambda: load, cores=lambda: 18, pid=lambda: 50, headroom=lambda: [])

    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    mine = "40 1.0 python scripts/train.py t\n50 300.0 python -m pytest -n auto\n60 250.0 ssh runner\n"
    assert remote_test.local_phase_refusal(probes(mine)) is None  # the train, its pytest and its ssh are its own
    assert "heavy job" in remote_test.local_phase_refusal(probes(mine + "99 400.0 python -m mlx_lm.generate\n"))
    assert "load average 30.0" in remote_test.local_phase_refusal(probes(mine, load=30.0))
    assert "cannot read" in remote_test.local_phase_refusal(probes(""))
    monkeypatch.setenv("KYRA_LOCAL_NON_BROWSER", "0")
    assert remote_test.local_phase_refusal(probes(mine)) == "KYRA_LOCAL_NON_BROWSER=0"


def test_the_unreachable_fallback_case_passes_when_run_alone():
    """local_browser_lock imported deploy_local by bare name, which worked only when something had already put
    scripts/ on sys.path; run alone, test_landing_browser_falls_back_only_when_unreachable[75] failed with "No module
    named 'deploy_local'" (2026-10-15). A fresh interpreter runs just that case."""
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",  # addopts already has -q
                           "tests/test_remote_test.py::test_landing_browser_falls_back_only_when_unreachable[75]"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "1 passed" in done.stdout


SHELLS = [shell for shell in ("/bin/sh", "/bin/zsh") if Path(shell).exists()]


def _stale_lock(base, sha, age, pgid):
    import time as _time

    (base / ".lock").mkdir(parents=True)
    (base / ".lock/info").write_text(f"oldtok 4242 {sha} {int(_time.time()) - age}\n")
    if pgid is not None:
        (base / "runs" / sha).mkdir(parents=True)
        (base / "runs" / sha / "pgid").write_text(f"{pgid}\n")


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("case", ["dead-group", "older-than-max-age"])
def test_a_stale_lock_is_taken_over_under_sh_and_zsh(tmp_path, shell, wrapped, case):
    """2026-10-15: the M1's login shell is zsh, which does not word-split `set -- $oinfo`, so start stayed empty and
    every held lock answered held:starting: a lock whose holder was dead was never taken over and every run was
    refused. The snippet now reads the info line shell-independently, and ssh_argv runs it under /bin/sh; both are
    checked, the raw snippet in each shell and the wrapped form as zsh would receive it."""
    base = tmp_path / "runner"
    sha = "deadbeef0000"
    if case == "dead-group":
        _stale_lock(base, sha, age=100, pgid=2 ** 22 + 11)  # older than 30 s, its process group gone
    else:
        _stale_lock(base, sha, age=5000, pgid=None)  # older than max_age (here 1000 s)
    snippet = remote_test.lock_command("newtok", "cafe00000000", 1000, base=str(base))
    command = remote_test.ssh_argv(snippet, host="h", key="/k")[-1] if wrapped else snippet
    done = subprocess.run([shell, "-c", command], capture_output=True, text=True, timeout=30)
    assert done.stdout.strip().splitlines()[-1] == "KYRA_LOCK=acquired", done.stdout + done.stderr
    assert (base / ".lock/info").read_text().startswith("newtok ")


@pytest.mark.parametrize("shell", SHELLS)
def test_a_live_holder_keeps_its_lock_under_sh_and_zsh(tmp_path, shell):
    base = tmp_path / "runner"
    holder = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        _stale_lock(base, "deadbeef0000", age=100, pgid=os.getpgid(holder.pid))
        done = subprocess.run([shell, "-c", remote_test.lock_command("newtok", "cafe", 1000, base=str(base))],
                              capture_output=True, text=True, timeout=30)
        assert done.stdout.strip().splitlines()[-1].startswith("KYRA_LOCK=held:deadbeef0000:")
        assert (base / ".lock/info").read_text().startswith("oldtok ")
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("shell", SHELLS)
def test_unlock_and_cleanup_parse_the_same_under_sh_and_zsh(tmp_path, shell):
    base = tmp_path / "runner"
    (base / ".lock").mkdir(parents=True)
    (base / ".lock/info").write_text("mytok 1 sha 1\n")
    subprocess.run([shell, "-c", remote_test.unlock_command("othertok", base=str(base))], check=True, timeout=30)
    assert (base / ".lock").exists()  # not ours: kept
    subprocess.run([shell, "-c", remote_test.unlock_command("mytok", base=str(base))], check=True, timeout=30)
    assert not (base / ".lock").exists()
    # a detached group whose leader exits, so the sleeper is reaped by launchd as on the runner (a child of this
    # test process would linger as a zombie that ps still lists)
    starter = subprocess.run(["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True,
                             start_new_session=True, check=True)
    sleeper = int(starter.stdout.strip())
    group = os.getpgid(sleeper)
    try:
        (base / "runs/cafe").mkdir(parents=True)
        (base / "runs/cafe/pgid").write_text(f"{group}\n")
        done = subprocess.run([shell, "-c", remote_test.cleanup_command("cafe", base=str(base), grace=3)],
                              capture_output=True, text=True, timeout=30)
        assert done.stdout.strip().splitlines()[-1] == f"KYRA_CLEANUP=stopped:{group}", done.stdout
    finally:
        try:
            os.killpg(group, 9)
        except ProcessLookupError:
            pass


# ---- RP4: the runner pool transport (--runner NAME --files ...), through fake ssh binaries on PATH ----------

FAKE_SSH = r"""#!/bin/bash
# records argv (fields split by \x1f), then runs the remote command locally the way sshd does: under zsh
(IFS=$'\x1f'; echo "$*") >> "$FAKE_SSH_LOG"
host="${@: -2:1}"
case "$host" in *dead*) echo "ssh: connect to host: Connection refused" >&2; exit 255;; esac
# sshd starts the runner's own limits; this fake inherits the client's (see fresh_session in the test file)
command="${@: -1}"
hard=$(ulimit -H -n)
if [ "$hard" != unlimited ] && [ "$hard" -lt 8192 ]; then command="${command//ulimit -n 8192/ulimit -n $hard}"; fi
# sshd forwards no client env: only what the fixture tests need crosses (KYRA_RP4_SECRET and TZ do not)
exec env -i HOME="$HOME" PATH="$PATH" SLEEP_PIDS="$SLEEP_PIDS" GUARD_MARKER="$GUARD_MARKER" EXPECT_TZ="$EXPECT_TZ" \
    "${FAKE_SHELL:-/bin/zsh}" -f -c "$command"
"""
POOL_TREE = {
    "tests/test_ok.py": "def test_ok():\n    assert True\n",
    "tests/test_bad.py": "def test_bad():\n    assert False\n\ndef test_fine():\n    assert True\n",
    "tests/test_env.py": ("import os\n\ndef test_env():\n"
                          "    assert os.environ.get('TZ', '') == os.environ.get('EXPECT_TZ', '')\n"
                          "    assert 'KYRA_RP4_SECRET' not in os.environ\n"),
    "tests/test_sleep.py": TREE["tests/test_sleep.py"],
    GUARD: TREE[GUARD],
    "data/secret.json": '{"private": true}\n',
    ".env": "KYRA_FAKE_KEY=not-a-real-key\n",
}


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A committed repo (with a tracked data/ and .env, to prove the archive excludes them), a runner home, a
    registry with ssh runners and a local one, and fake ssh first on PATH. ssh_argv is the real one."""
    repo = tmp_path / "repo"
    for rel, text in POOL_TREE.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "tree", "--no-verify"], check=True)
    base = tmp_path / "runner"
    (base / ".venv/bin").mkdir(parents=True)
    python = base / ".venv/bin/python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("GUARD_MARKER", str(tmp_path / "guard-ran"))
    monkeypatch.delenv("FAKE_EXIT", raising=False)
    monkeypatch.delenv("KYRA_M1_HOST", raising=False)
    monkeypatch.delenv("KYRA_M1_KEY", raising=False)
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "kyra-data"))  # no m1.json: the registry is the source
    registry = tmp_path / "kyra-data/team/runners.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"runners": [
        {"name": "mac", "os": "macos", "cores": 4, "ram_gb": 8, "reach": "local", "may_see_real_data": True},
        {"name": "box", "os": "macos", "cores": 4, "ram_gb": 8, "reach": "ssh",
         "host": "runner@box.example.test", "key": "/fixture/box-key"},
        {"name": "dead", "os": "macos", "cores": 4, "ram_gb": 8, "reach": "ssh",
         "host": "runner@dead.example.test", "key": "/fixture/dead-key"},
        {"name": "off", "os": "macos", "cores": 4, "ram_gb": 8, "reach": "ssh",
         "host": "runner@off.example.test", "key": "/fixture/off-key", "enabled": False},
    ]}))
    monkeypatch.setattr(remote_test, "ROOT", repo)
    monkeypatch.setattr(remote_test, "BASE", str(base))
    monkeypatch.setattr(remote_test, "LOG_DIR", tmp_path / "logs")
    from companion import runner_pool
    runners = {r.name: r for r in runner_pool.load_registry(registry)}
    return {"repo": repo, "base": base, "log": log, "tmp": tmp_path, "runners": runners, "registry": registry}


def _calls(pool):
    return [line.split("\x1f") for line in pool["log"].read_text().splitlines()] if pool["log"].exists() else []


def _attempt(pool, name, *paths, n=1):
    from companion.runner_exec import Attempt
    return Attempt(f"{name}#{n}", pool["runners"][name], tuple(paths))


def test_a_shard_runs_on_the_registry_host_under_zsh_and_reports_junit_per_file(pool):
    result = remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_ok.py", "tests/test_bad.py"), {})
    assert result.exit_code == 1
    assert {p: r.outcome for p, r in result.files.items()} == {"tests/test_ok.py": "pass", "tests/test_bad.py": "fail"}
    assert all(r.seconds >= 0 for r in result.files.values())
    calls = _calls(pool)
    assert calls and all(c[:3] == ["-i", "/fixture/box-key", "-o"] and c[-2] == "runner@box.example.test"
                         for c in calls), "every ssh call uses the registry entry's host and key"
    assert not (pool["base"] / ".lock").exists(), "the lock is released"
    assert not (pool["tmp"] / "guard-ran").exists()
    run_call = next(c for c in calls if "pytest.log" in c[-1])
    assert "--ignore=tests/test_no_personal_files.py" in run_call[-1]
    assert "--deselect=tests/test_no_personal_files.py" in run_call[-1]
    assert RUNNER_ULIMIT in run_call[-1], "the runner still gets its own open-file request, unchanged"


ULIMIT_PROBES = ["test_a_subset_and_the_whole_tree_pass_without_running_the_guard",
                 "test_a_shard_runs_on_the_registry_host_under_zsh_and_reports_junit_per_file",
                 "test_env_is_an_allowlist_and_a_refusal_sends_nothing",
                 "test_workers_run_non_browser_in_parallel_then_browser_serially"]


def test_the_fake_runner_survives_an_inherited_open_file_cap(tmp_path):
    """Regression probe: the same tests in a pytest whose hard open-file limit is 4096, as after an earlier test on
    the worker imported mlx_lm. Before fresh_session every real run failed there with 'setup on the runner failed
    before pytest ran (remote exit 1)'."""
    def cap():
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 4096))
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:xdist", "-p", "no:cacheprovider", "-o", "addopts=",
                           "-q", f"--basetemp={tmp_path / 'inner'}", *(f"{Path(__file__).resolve()}::{t}" for t in ULIMIT_PROBES)],
                          capture_output=True, text=True, cwd=ROOT, preexec_fn=cap, timeout=600)
    assert done.returncode == 0 and f"{len(ULIMIT_PROBES)} passed" in done.stdout, done.stdout[-3000:]


def test_the_archive_never_carries_data_or_env_even_when_tracked(pool):
    argv = remote_test.archive_argv("HEAD")
    assert argv[-2:] == [":(exclude,icase)data", ":(exclude,literal).env"] and "data" not in argv and ".env" not in argv
    result = remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_ok.py"), {})
    assert result.exit_code == 0
    run_dir = next((pool["base"] / "runs").iterdir())
    assert (run_dir / "tests/test_ok.py").exists()
    assert not (run_dir / "data").exists() and not (run_dir / ".env").exists()
    for call in _calls(pool):
        assert not any(re.search(r"(^|[\s/=])(data/|\.env\b)", arg) or "KYRA_DATA_DIR" in arg for arg in call)


def test_env_is_an_allowlist_and_a_refusal_sends_nothing(pool, monkeypatch):
    from companion.runner_exec import TransportRefused
    transport = remote_test.RemoteTransport("HEAD")
    for env in ({"KYRA_DATA_DIR": "/tmp/x"}, {"KYRA_RP4_SECRET": "x"}, {"OPENAI_API_KEY": "x"},
                {"TZ": str(pool["tmp"] / "data")}):
        with pytest.raises(TransportRefused):
            transport.run(_attempt(pool, "box", "tests/test_ok.py"), env)
    for paths in (("data/secret.json",), (GUARD,), ("tests/test_ok.py", "--basetemp=/x")):
        with pytest.raises(TransportRefused):
            transport.run(_attempt(pool, "box", *paths), {})
    with pytest.raises(TransportRefused):
        transport.run(_attempt(pool, "mac", "tests/test_ok.py"), {})
    assert _calls(pool) == [], "nothing reached ssh"
    monkeypatch.setenv("KYRA_RP4_SECRET", "must-not-travel")  # a local env var is not forwarded either
    monkeypatch.setenv("EXPECT_TZ", "UTC")
    result = transport.run(_attempt(pool, "box", "tests/test_env.py"), {"TZ": "UTC"})
    assert result.exit_code == 0 and result.files["tests/test_env.py"].outcome == "pass"
    run_call = next(c for c in _calls(pool) if "pytest.log" in c[-1])
    import shlex
    wrapped = shlex.split(run_call[-1])  # ssh_argv runs every snippet as /bin/sh -c <quoted> (lb-lk2)
    assert wrapped[:2] == ["/bin/sh", "-c"] and wrapped[2].startswith("export TZ=UTC; ")


def test_dead_runner_is_75_and_held_lock_is_73(pool):
    import time
    transport = remote_test.RemoteTransport("HEAD")
    assert transport.run(_attempt(pool, "dead", "tests/test_ok.py"), {}).exit_code == 75
    (pool["base"] / ".lock").mkdir(parents=True)
    (pool["base"] / ".lock/info").write_text(f"other 1 deadbeef {int(time.time())}\n")
    result = transport.run(_attempt(pool, "box", "tests/test_ok.py"), {})
    assert result.exit_code == 73 and not result.files
    assert "other" in (pool["base"] / ".lock/info").read_text()


def test_kill_stops_the_process_group_on_the_runner_and_returns_124(pool, monkeypatch):
    import threading
    import time
    pids = pool["tmp"] / "pids"
    monkeypatch.setenv("SLEEP_PIDS", str(pids))
    transport = remote_test.RemoteTransport("HEAD")
    attempt = _attempt(pool, "box", "tests/test_sleep.py")
    out = {}
    worker = threading.Thread(target=lambda: out.update(result=transport.run(attempt, {})))
    worker.start()
    deadline = time.monotonic() + 60
    while not (pids.exists() and pids.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert pids.exists(), "the test started on the runner"
    transport.kill(attempt)
    worker.join(60)
    assert not worker.is_alive() and out["result"].exit_code == 124 and not out["result"].files
    for pid in (int(x) for x in pids.read_text().split()):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (pool["base"] / ".lock").exists()


def test_kill_never_reaches_a_run_whose_lock_is_not_ours(pool):
    lock = pool["base"] / ".lock"
    lock.mkdir(parents=True)
    (lock / "info").write_text("someone-else 1 abc 1\n")
    out = subprocess.run(["/bin/zsh", "-f", "-c", remote_test.kill_command("mine", "abc")],
                         capture_output=True, text=True).stdout
    assert out.strip() == remote_test.CLEANUP_SENTINEL + "notours"


def test_deadline_timeout_is_124(pool, monkeypatch):
    monkeypatch.setenv("SLEEP_PIDS", str(pool["tmp"] / "pids"))
    monkeypatch.setattr(remote_test, "TIMEOUT", 6)
    result = remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_sleep.py"), {})
    assert result.exit_code == 124 and not result.files
    assert not (pool["base"] / ".lock").exists()


def test_runner_exec_replaces_a_dead_runner_shard_and_accounts_the_full_set(pool):
    from companion import runner_exec
    from companion.runner_pool import Job, Load
    runners = [pool["runners"]["dead"], pool["runners"]["box"]]
    jobs = [Job("tests/test_ok.py"), Job("tests/test_env.py")]
    lines = []
    report = runner_exec.execute({"dead": ["tests/test_ok.py"], "box": ["tests/test_env.py"]}, jobs, runners,
                                 remote_test.RemoteTransport("HEAD"), durations={}, probe=lambda r: Load(),
                                 log=lines.append, repo_root=pool["repo"])
    assert report.verdict == "pass" and report.failed_runners == ["dead"]
    assert report.files == {"tests/test_ok.py": ("pass", "box"), "tests/test_env.py": ("pass", "box")}
    assert any("dead#" in ln and "exit 75" in ln for ln in lines)


def test_junit_outcomes_take_the_worst_case_and_keep_strays(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite>'
                   '<testcase file="tests/test_a.py" classname="tests.test_a" name="t1" time="0.5"/>'
                   '<testcase file="tests/test_a.py" classname="tests.test_a" name="t2" time="0.25"><skipped/></testcase>'
                   '<testcase file="tests/test_b.py" classname="tests.test_b" name="t" time="1"><error/></testcase>'
                   '<testcase file="tests/test_c.py" classname="tests.test_c" name="t" time="1"/>'
                   '</testsuite></testsuites>')
    files = remote_test.shard_files(xml, ("tests/test_a.py", "tests/test_b.py"))
    assert files["tests/test_a.py"].outcome == "pass" and files["tests/test_a.py"].seconds == 0.75  # remote skip = pass
    assert files["tests/test_b.py"].outcome == "error"
    assert "tests/test_c.py" in files, "a stray file stays so runner_exec's accounting refuses the shard"


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash"])
def test_stale_lock_takeover_parses_the_info_line_in_zsh_and_bash(tmp_path, shell):
    """Lane 1 (2026-10-15): `set -- $oinfo` does not word-split in zsh, the M1's login shell, so a stale
    holder's lock was never read as stale there. The info line is now split with read."""
    base = tmp_path / "rb"
    (base / ".lock").mkdir(parents=True)
    (base / ".lock/info").write_text("old 1 deadbeef0000 1000\n")  # started in 1970: far older than max_age
    out = subprocess.run([shell, "-f", "-c", remote_test.lock_command("mine", "abc", 60, base=str(base))],
                         capture_output=True, text=True).stdout
    assert out.strip().endswith(remote_test.LOCK_SENTINEL + "acquired"), out
    assert (base / ".lock/info").read_text().startswith("mine ")


def test_cli_runner_flag_paths(pool, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(remote_test, "run", lambda ref, args, workers: called.append((ref, args, workers)) or 0)
    assert remote_test.main(["--ref", "HEAD", "--", "tests/test_ok.py"]) == 0
    assert called == [("HEAD", ["tests/test_ok.py"], 1)], "without --runner the old path is untouched"
    assert remote_test.main(["--ref", "HEAD", "--runner", "mac", "--files", "tests/test_ok.py"]) == 2
    assert remote_test.main(["--ref", "HEAD", "--runner", "nope", "--files", "tests/test_ok.py"]) == 2
    assert remote_test.main(["--ref", "HEAD", "--runner", "off", "--files", "tests/test_ok.py"]) == 75
    assert remote_test.main(["--ref", "HEAD", "--runner", "dead", "--files", "tests/test_ok.py"]) == 75
    for bad in (["--runner", "box"], ["--files", "tests/test_ok.py"],
                ["--runner", "box", "--files", "tests/test_ok.py", "--", "-x"]):
        with pytest.raises(SystemExit):
            remote_test.main(bad)
    capsys.readouterr()
    assert remote_test.main(["--ref", "HEAD", "--runner", "box", "--files", "tests/test_ok.py", "tests/test_bad.py"]) == 1
    out = capsys.readouterr()
    last = out.out.strip().splitlines()[-1]
    assert last.startswith("KYRA_SHARD_RESULT=")
    shard = json.loads(last.partition("=")[2])
    assert shard["exit"] == 1 and shard["files"]["tests/test_bad.py"]["outcome"] == "fail"
    assert "box.example.test" not in out.out + out.err and "/fixture/box-key" not in out.out + out.err
    pool["registry"].unlink()
    assert remote_test.main(["--ref", "HEAD", "--runner", "box", "--files", "tests/test_ok.py"]) == 75


# ---- review o-rev82 fix-first: its eight probes (ported from tests/test_zz_review82_probe.py) and the minors ----

def _commit(pool, files):
    repo = pool["repo"]
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "more", "--no-verify"], check=True)


def _add_runner(pool, name, host, key=True):
    data = json.loads(pool["registry"].read_text())
    entry = {"name": name, "os": "macos", "cores": 4, "ram_gb": 8, "reach": "ssh", "host": host}
    if key:
        entry["key"] = f"/fixture/{name}-key"
    data["runners"].append(entry)
    pool["registry"].write_text(json.dumps(data))


def _listing(argv):
    tree = subprocess.run(argv, capture_output=True, check=True).stdout
    return subprocess.run(["tar", "-t"], input=tree, capture_output=True, check=True).stdout.decode().split()


def test_probe_archive_keeps_tests_data_and_lookalikes(pool):
    _commit(pool, {"tests/data/heldout.jsonl": "{}\n", "database/x.py": "x = 1\n", "data_notes.md": "n\n"})
    listing = _listing(remote_test.archive_argv("HEAD"))
    assert "tests/data/heldout.jsonl" in listing and "database/x.py" in listing and "data_notes.md" in listing
    assert not any(n == "data/" or n.startswith("data/") or n == ".env" for n in listing)


def test_probe_archive_drops_every_env_variant(pool):
    _commit(pool, {".env.local": "KYRA_FAKE=1\n", "sub/.env": "KYRA_FAKE=2\n", ".env.example": "KYRA_FAKE=\n",
                   "sub/deep/.env.prod": "KYRA_FAKE=3\n"})
    listing = _listing(remote_test.archive_argv("HEAD"))
    assert ".env.example" in listing
    assert not {".env", ".env.local", "sub/.env", "sub/deep/.env.prod"} & set(listing)
    tree, why = remote_test.checked_archive("HEAD")
    assert tree is not None and why == ""


def test_archive_post_check_refuses_the_run_if_an_env_file_slips_through(pool, monkeypatch):
    """Hard constraint in code: whatever the pathspecs did, the bytes about to be sent are listed and a data/ or
    .env file in them refuses the run (exit 2) before any ssh call."""
    sha = remote_test.RemoteTransport("HEAD").sha
    monkeypatch.setattr(remote_test, "archive_argv", lambda s: ["git", "-C", str(pool["repo"]), "archive",
                                                                "--format=tar", s])
    tree, why = remote_test.checked_archive(sha)
    assert tree is None and why.startswith("refused") and ".env" in why
    result = remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_ok.py"), {})
    assert result.exit_code == 2 and not result.files
    assert not any("tar -x" in c[-1] or "pytest.log" in c[-1] for c in _calls(pool)), "nothing was sent"


def test_probe_env_values_are_inert_on_the_runner(pool, monkeypatch):
    value = "UTC; touch PWNED1 $(touch PWNED2) `touch PWNED3`\nx"
    monkeypatch.setenv("EXPECT_TZ", value)
    result = remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_env.py"), {"TZ": value})
    assert result.exit_code == 0 and result.files["tests/test_env.py"].outcome == "pass"
    assert list(pool["tmp"].rglob("PWNED*")) == []


@pytest.mark.parametrize("name,host", [("pc", "pc"), ("lab", "test")])
def test_probe_masking_never_rewrites_the_shard_result(pool, capsys, name, host):
    _add_runner(pool, name, host)
    code = remote_test.main(["--ref", "HEAD", "--runner", name, "--files", "tests/test_ok.py"])
    out = capsys.readouterr()
    assert code == 0
    shard = json.loads(out.out.strip().splitlines()[-1].partition("=")[2])
    assert shard["runner"] == name and set(shard["files"]) == {"tests/test_ok.py"}
    assert f"/fixture/{name}-key" not in out.out + out.err
    assert "runners/runner_ok" not in out.out + out.err and "pyrunner" not in out.out + out.err


def test_masking_hides_whole_tokens_only(monkeypatch):
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("u@test", "/k/test"))
    text = remote_test.safe_log("ssh -i /k/test u@test failed; tests/test_ok.py ran on test")
    assert text == "ssh -i runner runner failed; tests/test_ok.py ran on runner"


def test_probe_kill_during_transfer_stops_the_run(pool, monkeypatch):
    transport = remote_test.RemoteTransport("HEAD")
    attempt = _attempt(pool, "box", "tests/test_ok.py")
    real = subprocess.run
    fired = []

    def run(argv, *a, **k):
        res = real(argv, *a, **k)
        if argv and argv[0] == "ssh" and "tar -x" in argv[-1] and not fired:
            fired.append(1)
            transport.kill(attempt)
        return res

    monkeypatch.setattr(remote_test.subprocess, "run", run)
    result = transport.run(attempt, {})
    assert fired and result.exit_code == 124
    assert not any("pytest.log" in c[-1] for c in _calls(pool)), "pytest was started after the kill"
    assert not (pool["base"] / ".lock").exists()


def test_probe_interrupt_kills_the_remote_group_before_unlock(pool, monkeypatch):
    import signal
    import time
    pids = pool["tmp"] / "pids"
    monkeypatch.setenv("SLEEP_PIDS", str(pids))
    real = subprocess.run

    def run(argv, *a, **k):
        if argv and argv[0] == "ssh" and "pytest.log" in argv[-1]:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while not (pids.exists() and pids.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(0.2)
            proc.kill()  # the local ssh dies; the remote group does not
            proc.wait()
            raise KeyboardInterrupt
        return real(argv, *a, **k)

    monkeypatch.setattr(remote_test.subprocess, "run", run)
    try:
        with pytest.raises(KeyboardInterrupt):
            remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_sleep.py"), {})
        assert pids.exists(), "the run started on the runner"
        time.sleep(1)
        alive = []
        for pid in (int(x) for x in pids.read_text().split()):
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        assert alive == [], "remote pytest outlived the interrupt and the lock was released"
        assert not (pool["base"] / ".lock").exists()
    finally:
        if pids.exists():
            for pid in (int(x) for x in pids.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_probe_cli_writes_the_junit_rp6_requires(pool, tmp_path):
    from companion import runner_durations
    junit = tmp_path / "out" / "junit.xml"
    code = remote_test.main(["--ref", "HEAD", "--runner", "box", "--junit", str(junit),
                             "--files", "tests/test_ok.py", "tests/test_bad.py"])
    assert code == 1
    seconds, unmapped = runner_durations.parse_junit(junit)
    assert set(seconds) == {"tests/test_ok.py", "tests/test_bad.py"} and unmapped == []
    _commit(pool, {"tests/test_empty.py": "X = 1\n"})
    junit.unlink()
    assert remote_test.main(["--ref", "HEAD", "--runner", "box", "--junit", str(junit),
                             "--files", "tests/test_empty.py"]) == 5
    assert junit.is_file()
    with pytest.raises(runner_durations.BadJunit):  # no testcases: RP6 reads every file as error, never a pass
        runner_durations.parse_junit(junit)


def test_probe_cli_timeout_leaves_no_junit(pool, tmp_path, monkeypatch):
    monkeypatch.setenv("SLEEP_PIDS", str(pool["tmp"] / "pids"))
    monkeypatch.setattr(remote_test, "TIMEOUT", 6)
    junit = tmp_path / "junit.xml"
    junit.write_text("<stale/>")  # a report from an earlier run must not survive as this run's
    assert remote_test.main(["--ref", "HEAD", "--runner", "box", "--junit", str(junit),
                             "--files", "tests/test_sleep.py"]) == 124
    assert not junit.exists()
    assert remote_test.main(["--ref", "HEAD", "--runner", "dead", "--junit", str(junit),
                             "--files", "tests/test_ok.py"]) == 75
    assert not junit.exists()


def test_probe_remote_skip_and_xfail_are_pytest_passes(pool):
    from companion import runner_exec
    from companion.runner_pool import Job, Load
    _commit(pool, {"tests/test_skippy.py": ("import pytest\n\ndef test_ok():\n    assert True\n\n"
                                             "@pytest.mark.skip(reason='x')\ndef test_skip():\n    pass\n\n"
                                             "@pytest.mark.xfail(strict=True)\ndef test_xf():\n    assert False\n")})
    report = runner_exec.execute({"box": ["tests/test_skippy.py"]}, [Job("tests/test_skippy.py")],
                                 [pool["runners"]["box"]], remote_test.RemoteTransport("HEAD"), durations={},
                                 probe=lambda r: Load(), log=lambda s: None, repo_root=pool["repo"])
    assert report.verdict == "pass"


def test_a_real_data_or_guard_file_in_a_remote_junit_is_an_error(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite>'
                   '<testcase file="tests/test_router_train_real.py" classname="x" name="t" time="1"><skipped/></testcase>'
                   '<testcase file="tests/test_no_personal_files.py" classname="x" name="t" time="1"/>'
                   '<testcase file="tests/test_ok.py" classname="x" name="t" time="1"><skipped/></testcase>'
                   '</testsuite></testsuites>')
    files = remote_test.shard_files(xml, ("tests/test_router_train_real.py", "tests/test_ok.py"))
    assert files["tests/test_router_train_real.py"].outcome == "error"
    assert files["tests/test_no_personal_files.py"].outcome == "error"
    assert files["tests/test_ok.py"].outcome == "pass"


def test_keyless_runner_is_75_with_a_message_and_a_failed_lock_answer_is_2(pool, monkeypatch, capsys):
    _add_runner(pool, "nokey", "runner@nokey.example.test", key=False)
    assert remote_test.main(["--ref", "HEAD", "--runner", "nokey", "--files", "tests/test_ok.py"]) == 75
    assert "no ssh key" in capsys.readouterr().err
    monkeypatch.setattr(remote_test, "acquire_lock", lambda *a: (False, "failed: lock check gave no answer"))
    assert remote_test.RemoteTransport("HEAD").run(_attempt(pool, "box", "tests/test_ok.py"), {}).exit_code == 2


def _where_local(tmp_path, monkeypatch, refusal=None, helpers=None, workers="7"):
    """--where local fixtures: the env train.py hands workflow_land, the M1 configured (so local must not mean
    unreachable), no ssh, a scratch gpu gate and phase marker, and no real process is ever signalled."""
    signals = _Signals()
    monkeypatch.setenv("KYRA_BROWSER_LOCAL", "1")
    monkeypatch.setenv("KYRA_LOCAL_WORKERS", workers)
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data/team").mkdir(parents=True, exist_ok=True)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(remote_test, "PHASE_DIR", tmp_path / "phase")
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    monkeypatch.setattr(remote_test, "ssh_argv", lambda *a, **k: pytest.fail("ssh argv built"))
    monkeypatch.setattr(remote_test, "local_phase_helpers", lambda: dict(helpers or {}))
    monkeypatch.setattr(remote_test, "local_phase_refusal", refusal or (lambda: None))
    monkeypatch.setattr(remote_test, "_kill", signals.kill)
    monkeypatch.setattr(remote_test, "_start_sitter", signals.sitter)
    return signals


def _where_local_non_browser(tmp_path):
    return [sys.executable, str(tmp_path / "scripts/remote_test.py"), "--ref", "abc", "--",
            remote_test.NON_BROWSER_GLOB]


def test_where_local_runs_both_phases_here_and_never_builds_ssh(tmp_path, monkeypatch, caplog):
    """train.py --where local (KYRA_BROWSER_LOCAL=1): the split landing runs the non-browser half and the serial
    browser phase on this Mac, even with the M1 configured; no ssh argv is built and nothing calls remote_test.py."""
    deploy_local = _deploy_local()
    monkeypatch.setenv("KYRA_SPLIT_TESTS", "1")
    _where_local(tmp_path, monkeypatch)
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "3 passed\n", "")

    with caplog.at_level("INFO"):
        deploy_local._pytest_phases(remote_test.m1_browser_runner(execute, "abc123"), sys.executable,
                                    _landing_tree(tmp_path), "abc123")
    assert sorted(c[1:5] for c in calls) == [["-m", "pytest", "-n", "7"], ["-m", "pytest", "-p", "no:xdist"]]
    assert not any("ssh" in c or Path(c[1]).name == "remote_test.py" for c in calls)
    assert "browser phase: this Mac (--where local)" in caplog.text
    assert "non-browser phase: this Mac (--where local)" in caplog.text


# Helper pause (2026-10-15): helper agents' pytest runs pushed priority trains' local phase to the M1. The train now
# pauses helper pytest/headless Chromium for its local phase instead; another landing still refuses it.
PS_HELPERS = (
    "40 1.0 python scripts/train.py t\n50 300.0 python -m pytest -n auto\n"     # this train and its local pytest
    "70 1.0 python scripts/train.py other\n71 1.0 python scripts/workflow_land.py abc\n"
    "72 300.0 python -m pytest -n auto\n73 200.0 python -c import sys;exec(eval(sys.stdin.readline()))\n"
    "79 5.0 node /usr/local/bin/claude\n80 0.1 /bin/zsh -c .venv/bin/python -m pytest -n 2 tests/test_x.py\n"
    "81 150.0 .venv/bin/python -m pytest -n 2 tests/test_x.py\n"
    "82 120.0 python -c import sys;exec(eval(sys.stdin.readline()))\n"
    "83 80.0 /cache/ms-playwright/chromium-1/Chromium.app/Contents/MacOS/Chromium Helper (Renderer) --type=renderer\n"
    "90 60.0 /cache/chrome-headless-shell/chrome-headless-shell --headless --remote-debugging-pipe\n"
    "91 250.0 /cache/chrome-headless-shell/chrome-headless-shell --headless --remote-debugging-pipe\n"
    "92 2.0 python scripts/web_ui.py --port 8420\n"                          # the owner Kyra web UI and its browser
    "93 0.1 -zsh\n94 150.0 .venv/bin/python -m pytest tests/test_y.py\n"     # the owner own terminal pytest
    "95 30.0 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
    "96 40.0 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser\n"
    "97 90.0 /opt/homebrew/bin/codex exec\n")
PARENTS_HELPERS = {40: 1, 50: 40, 70: 79, 71: 70, 72: 71, 73: 72, 79: 1, 80: 79, 81: 80, 82: 81, 83: 82, 90: 79,
                   91: 92, 92: 1, 93: 1, 94: 93, 95: 1, 96: 1, 97: 1}


def _helper_probes(ps=PS_HELPERS, load=1.0, headroom=()):
    sys.path.insert(0, str(ROOT / "scripts"))
    import low_power
    return low_power.Probes(processes=lambda: ps, parents=lambda: dict(PARENTS_HELPERS), load=lambda: load,
                            cores=lambda: 18, pid=lambda: 50, headroom=lambda: list(headroom))


def _without(ps, *pids):
    return "".join(ln + "\n" for ln in ps.splitlines() if int(ln.split()[0]) not in pids)


def test_helpers_are_pytest_and_headless_chromium_never_a_landing_a_cli_or_owners_browser(monkeypatch):
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    helpers = remote_test.local_phase_helpers(_helper_probes())
    # the helper pytest, its xdist worker and its test browser, and an agent's own headless Chromium
    assert helpers == {81: "pytest", 82: "python", 83: "chromium", 90: "chromium"}
    assert not {70, 71, 72, 73} & set(helpers), "another landing's family is never paused"
    assert not {79, 80, 97} & set(helpers), "a CLI agent (or the shell it runs) is never paused"
    assert not {91, 92, 93, 94, 95, 96} & set(helpers), "the owner own runs, browsers and apps are never paused"
    assert not {40, 50} & set(helpers), "this train is never paused"


def test_a_second_landing_still_refuses_the_local_phase_but_helpers_do_not(monkeypatch):
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    no_cli = _without(PS_HELPERS, 91, 94, 97)  # codex, the web UI's browser, the owner pytest: heavy jobs
    reason = remote_test.local_phase_refusal(_helper_probes(no_cli))
    assert reason.startswith("another heavy job") and "pid 72" in reason and "pid 81" not in reason
    assert remote_test.local_phase_refusal(_helper_probes(_without(no_cli, 70, 71, 72, 73))) is None
    for owner in (91, 94):  # his web UI's browser, his own terminal pytest: still refuse, never paused
        others = {91, 94} - {owner}
        assert f"pid {owner}" in remote_test.local_phase_refusal(_helper_probes(_without(PS_HELPERS, 70, 71, 72, 73, 97, *others)))


def test_the_load_and_memory_guard_still_refuses_with_helpers_ignored(monkeypatch):
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    helpers_only = _without(PS_HELPERS, 70, 71, 72, 73, 91, 94, 97)
    assert "load average 30.0" in remote_test.local_phase_refusal(_helper_probes(helpers_only, load=30.0))
    memory = "memory pressure is warn (needs normal)"
    assert remote_test.local_phase_refusal(_helper_probes(helpers_only, headroom=[memory])) == memory


class _Signals:
    def __init__(self):
        self.sent, self.sitters = [], []

    def kill(self, pid, sig):
        self.sent.append((pid, sig.name))

    def sitter(self, pids):
        self.sitters.append(pids)
        return None


def _landing(tmp_path, monkeypatch, refusals, runner_effect=None):
    signals = _Signals()
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner.example.test", "/fixture/key"))
    monkeypatch.setattr(remote_test, "local_phase_helpers", lambda: {81: "pytest", 90: "chromium"})
    answers = iter(refusals)
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: next(answers))
    monkeypatch.setattr(remote_test, "_kill", signals.kill)
    monkeypatch.setattr(remote_test, "_start_sitter", signals.sitter)
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        if "-n" in argv and runner_effect:
            assert signals.sent == [(81, "SIGSTOP"), (90, "SIGSTOP")], "helpers are paused while the phase runs"
            raise runner_effect
        return subprocess.CompletedProcess(argv, 0, "10 passed\n", "")

    non_browser = [sys.executable, str(tmp_path / "scripts/remote_test.py"), "--ref", "abc123", "--",
                   remote_test.NON_BROWSER_GLOB]
    return signals, calls, lambda: remote_test.m1_browser_runner(execute, "abc123")(non_browser, cwd=tmp_path)


def test_a_helper_pytest_is_paused_for_the_local_phase_and_resumed_with_log_lines(tmp_path, monkeypatch, caplog):
    signals, calls, run = _landing(tmp_path, monkeypatch, [None, None])
    with caplog.at_level("INFO"):
        assert run().returncode == 0
    assert signals.sent == [(81, "SIGSTOP"), (90, "SIGSTOP"), (81, "SIGCONT"), (90, "SIGCONT")]
    assert signals.sitters == [[81, 90]]
    assert len(calls) == 1 and "-n" in calls[0], "the non-browser half ran on this Mac"
    assert "local phase: paused helper pids 81 (pytest), 90 (chromium)" in caplog.text
    assert "local phase: resumed helper pids 81 (pytest), 90 (chromium)" in caplog.text


@pytest.mark.parametrize("effect", [RuntimeError("phase crashed"), KeyboardInterrupt()])
def test_helpers_are_resumed_when_the_local_phase_raises(tmp_path, monkeypatch, caplog, effect):
    signals, _, run = _landing(tmp_path, monkeypatch, [None, None], runner_effect=effect)
    with caplog.at_level("INFO"), pytest.raises(type(effect)):
        run()
    assert signals.sent[-2:] == [(81, "SIGCONT"), (90, "SIGCONT")]
    assert "local phase: resumed helper pids 81 (pytest), 90 (chromium)" in caplog.text


def test_the_load_guard_after_pausing_resumes_helpers_and_falls_back_to_the_m1(tmp_path, monkeypatch, caplog):
    signals, calls, run = _landing(tmp_path, monkeypatch, [None, "load average 30.0 is above 16.2"])
    with caplog.at_level("INFO"):
        assert run().returncode == 0
    assert signals.sent == [(81, "SIGSTOP"), (90, "SIGSTOP"), (81, "SIGCONT"), (90, "SIGCONT")]
    assert len(calls) == 1 and Path(calls[0][1]).name == "remote_test.py", "the non-browser half went to the M1"
    assert "sequential fallback: load average 30.0" in caplog.text


def test_a_refusal_before_pausing_pauses_nothing(tmp_path, monkeypatch):
    signals, calls, run = _landing(tmp_path, monkeypatch, ["another heavy job is running: pid 72 pytest"])
    assert run().returncode == 0
    assert signals.sent == [] and signals.sitters == []


def _fake_helper(tmp_path, ready=False):
    """A real process whose program is "pytest" (a script of that name), so ps shows what a helper run shows.

    ``ready`` waits until that program is past startup. A signal sent while posix_spawn is still
    in progress is not a reliable stop, and under load that startup is the whole race.
    """
    script = tmp_path / "pytest"
    if not ready:
        script.write_text("import time\ntime.sleep(60)\n")
        return subprocess.Popen([sys.executable, str(script)])
    script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _state(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def _kernel_status(pid):
    """proc_bsdinfo.pbi_status for our child: 4 is SSTOP, the state ps prints as T. None if unread."""
    import ctypes

    # Field layout from xnu proc_info.h. proc_pidinfo rejects a short buffer, so the tail is padding.
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    libc = ctypes.CDLL(None)
    fn = getattr(libc, "proc_pidinfo", None)
    if fn is None:
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    info = ProcBsdInfo()
    if fn(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) < 8:
        return None
    return info.pbi_status


def _await_stopped(pid, want_stopped, timeout=30):
    """Poll until the stop bit matches. ps and the kernel status both lag the signal under load."""
    import time

    deadline = time.monotonic() + timeout
    counts = {}
    while time.monotonic() < deadline:
        status = _kernel_status(pid)
        if status is None:
            try:
                state = _state(pid)
            except OSError:
                state = ""
            observed = state.startswith("T") if state else None
        else:
            observed = status == 4  # SSTOP, the state ps prints as T
        counts[status] = counts.get(status, 0) + 1
        if observed is want_stopped:
            return True
        time.sleep(0.01)
    raise AssertionError(f"helper {pid} stopped={want_stopped} not observed; kernel statuses {counts}")


def test_a_real_helper_process_is_stopped_and_continued(tmp_path):
    import logging

    helper = _fake_helper(tmp_path, ready=True)
    try:
        with remote_test.paused_helpers({helper.pid: "pytest"}, logging.getLogger("t")):
            assert _await_stopped(helper.pid, True)
        assert _await_stopped(helper.pid, False)
        assert helper.poll() is None
    finally:
        helper.kill()
        helper.wait()


def test_a_killed_train_still_resumes_its_paused_helpers(tmp_path):
    """SIGKILL skips every finally; the sitter sees its stdin close and continues the helpers."""
    import time

    helper = _fake_helper(tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import importlib.util, logging, time\n"
        f"spec = importlib.util.spec_from_file_location('remote_test', {str(ROOT / 'scripts/remote_test.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"with m.paused_helpers({{{helper.pid}: 'pytest'}}, logging.getLogger('t')):\n"
        "    print('paused', flush=True); time.sleep(60)\n")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "paused"
        assert _state(helper.pid).startswith("T")
        holder.kill()
        holder.wait()
        deadline = time.monotonic() + 10
        while _state(helper.pid).startswith("T") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _state(helper.pid).startswith("T")
    finally:
        holder.kill()
        holder.wait()
        helper.kill()
        helper.wait()


def test_helpers_yield_only_to_a_local_phase_on_this_mac_or_a_load_of_12(tmp_path, monkeypatch):
    """Coordinator 2026-10-15: helpers idled while train50c ran wholly on the M1; a train.py process is no reason."""
    phase = tmp_path / "phase"
    assert remote_test.helpers_should_yield(phase, load=3.0) is None  # no local phase, any train on the M1
    assert "load average 12.0" in remote_test.helpers_should_yield(phase, load=12.0)
    with remote_test.local_phase_marker(phase) as marker:
        assert marker.exists()
        assert "local test phase" in remote_test.helpers_should_yield(phase, load=1.0)
    assert not marker.exists()
    (phase / "999999").mkdir(parents=True, exist_ok=True)  # a dead train's leftover marker is ignored
    assert remote_test.helpers_should_yield(phase, load=1.0, alive=lambda pid: False) is None


def test_the_local_phase_holds_the_marker_only_while_it_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_test, "PHASE_DIR", tmp_path / "phase")
    seen = []
    signals, _, run = _landing(tmp_path, monkeypatch, [None, None])
    original = remote_test._local_non_browser

    def spy(*args, **kwargs):
        seen.append(remote_test.helpers_should_yield(tmp_path / "phase", load=1.0))
        return original(*args, **kwargs)

    monkeypatch.setattr(remote_test, "_local_non_browser", spy)
    assert run().returncode == 0
    assert seen and "local test phase" in seen[0]
    assert remote_test.helpers_should_yield(tmp_path / "phase", load=1.0) is None


def test_should_yield_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(remote_test, "helpers_should_yield", lambda: None)
    assert remote_test.main(["--should-yield"]) == 1
    monkeypatch.setattr(remote_test, "helpers_should_yield", lambda: "load average 13.0 is 12 or more")
    assert remote_test.main(["--should-yield"]) == 0
    assert "load average 13.0" in capsys.readouterr().out


def test_where_local_non_browser_respects_the_heavy_job_refusal(tmp_path, monkeypatch):
    """FIX-FIRST on fe960bfc (lane 2): under --where local the non-browser half ran pytest -n beside a model job. It
    now waits (bounded, LOCAL_WAIT_SECONDS) for local_phase_refusal to clear, then refuses the landing naming the
    job, so the gate writes a fail receipt; the runner is never called. The off switch refuses the same way."""
    from deploy_local import Refused

    original = remote_test.local_phase_refusal
    _where_local(tmp_path, monkeypatch, refusal=lambda: "another heavy job is running: pid 1 mlx_lm")
    monkeypatch.setattr(remote_test, "LOCAL_WAIT_SECONDS", 0)
    calls = []
    wrapped = remote_test.m1_browser_runner(lambda argv, **k: calls.append(argv), "abc")
    with pytest.raises(Refused, match="--where local: another heavy job is running: pid 1 mlx_lm"):
        wrapped(_where_local_non_browser(tmp_path), cwd=tmp_path)
    monkeypatch.setenv("KYRA_LOCAL_NON_BROWSER", "0")
    monkeypatch.setattr(remote_test, "local_phase_refusal", original)
    monkeypatch.setattr(remote_test, "LOCAL_WAIT_SECONDS", 3600)  # the off switch is never waited on
    monkeypatch.setenv("KYRA_BROWSER_LOCAL", "1")  # a fresh landing: the first runner took the switches
    wrapped = remote_test.m1_browser_runner(lambda argv, **k: calls.append(argv), "abc")
    with pytest.raises(Refused, match="--where local: KYRA_LOCAL_NON_BROWSER=0"):
        wrapped(_where_local_non_browser(tmp_path), cwd=tmp_path)
    assert calls == []


def test_where_local_waits_out_a_short_heavy_job(tmp_path, monkeypatch, caplog):
    answers = iter(["another heavy job is running: pid 7 xcodebuild", None])
    _where_local(tmp_path, monkeypatch, refusal=lambda: next(answers))
    monkeypatch.setattr(remote_test, "LOCAL_WAIT_POLL", 0)
    run = remote_test.m1_browser_runner(lambda argv, **k: subprocess.CompletedProcess(argv, 0, "4 passed\n", ""), "abc")
    with caplog.at_level("INFO"):
        assert run(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0
    assert "waiting for this Mac (--where local): another heavy job is running: pid 7 xcodebuild" in caplog.text


def test_where_local_pauses_helpers_does_not_count_them_and_resumes_them(tmp_path, monkeypatch):
    seen = []
    signals = _where_local(tmp_path, monkeypatch, helpers={81: "pytest", 90: "chromium"},
                           refusal=lambda: seen.append(list(signals.sent)) or None)

    def execute(argv, **kwargs):
        assert signals.sent == [(81, "SIGSTOP"), (90, "SIGSTOP")], "helpers are paused while the suite runs"
        return subprocess.CompletedProcess(argv, 0, "4 passed\n", "")

    assert remote_test.m1_browser_runner(execute, "abc")(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0
    assert seen == [[(81, "SIGSTOP"), (90, "SIGSTOP")]], "the load and memory guard runs after the pause"
    assert signals.sent[2:] == [(81, "SIGCONT"), (90, "SIGCONT")] and signals.sitters == [[81, 90]]


def test_where_local_waits_for_a_gpu_job_before_the_non_browser_half(tmp_path, monkeypatch):
    """A gpu gate holder (a model run) finishes first; the landing then holds the gpu gate for the whole half, so
    no model job can start mid-suite, and releases it afterwards."""
    from companion import gate_lock

    _where_local(tmp_path, monkeypatch)
    gpu = gate_lock.lock_path(ROOT, "gpu")
    real_blocking, held = gate_lock.blocking, [{"pid": 1, "command": ["mlx_lm.generate"]}]
    monkeypatch.setattr(gate_lock, "blocking", lambda path: held.pop() if held else real_blocking(path))
    order = []

    def execute(argv, **kwargs):
        order.append((held == [], gate_lock.holder(gpu)["pid"] == os.getpid()))
        return subprocess.CompletedProcess(argv, 0, "5 passed\n", "")

    assert remote_test.m1_browser_runner(execute, "abc")(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0
    assert order == [(True, True)]  # ran only after the model job ended, holding the gpu gate
    assert gate_lock.holder(gpu) is None  # released afterwards


def test_a_headroom_refusal_in_the_local_half_is_a_refusal_not_a_failure(tmp_path, monkeypatch):
    """conftest's headroom admission exits 75 when this Mac is above the owner line: nothing failed, so the landing is
    refused (deploy_local.Refused, logged as 'refused: ...'), not reported as pytest exit 1."""
    from deploy_local import Refused

    _where_local(tmp_path, monkeypatch)
    refused = remote_test.m1_browser_runner(lambda argv, **k: subprocess.CompletedProcess(argv, 75, "", ""), "abc")
    with pytest.raises(Refused, match="headroom refused"):
        refused(_where_local_non_browser(tmp_path), cwd=tmp_path)
    _where_local(tmp_path, monkeypatch)
    passed = remote_test.m1_browser_runner(lambda argv, **k: subprocess.CompletedProcess(argv, 0, "1 passed\n", ""),
                                           "abc")
    assert passed(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0  # control


def test_where_env_never_reaches_a_pytest_this_landing_starts(tmp_path, monkeypatch):
    """o-rev91 BLOCKER: KYRA_BROWSER_LOCAL/KYRA_LOCAL_WORKERS reached the landing's own pytest, where tests pinning
    the M1 path then took the local path. m1_browser_runner takes all three --where switches out of the env before
    any phase starts."""
    _where_local(tmp_path, monkeypatch, workers="3")
    monkeypatch.setenv("KYRA_WHERE", "local")
    envs = []

    def execute(argv, **kwargs):
        envs.append({key: os.environ.get(key) for key in remote_test.WHERE_ENV})
        return subprocess.CompletedProcess(argv, 0, "2 passed\n", "")

    run = remote_test.m1_browser_runner(execute, "abc123def456")
    result = run([sys.executable, "scripts/remote_test.py", "--ref", "abc123def456", "--", remote_test.NON_BROWSER_GLOB],
                 cwd=tmp_path)
    run([sys.executable, "-m", "pytest", "-p", "no:xdist", "tests/test_x_browser.py"], cwd=tmp_path)
    assert envs == [dict.fromkeys(remote_test.WHERE_ENV)] * 2
    assert result.args[1:5] == ["-m", "pytest", "-n", "3"]  # the worker count still came through
    # o-rev91 log probe: under --where local the summary deploy_local logs never claims the M1
    assert "M1" not in result.stdout.splitlines()[-1] and "--where local" in result.stdout.splitlines()[-1]


@pytest.mark.parametrize("configured", [True, False])
def test_where_m1_never_falls_back_to_this_mac(tmp_path, monkeypatch, configured):
    """train.py --where m1 (KYRA_WHERE=m1): both phases go to the M1; unreachable is a refusal, never a local run,
    and the non-browser half is not tried here even when this Mac is free."""
    from deploy_local import Refused

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setenv("KYRA_WHERE", "m1")
    settings = ("runner.example.test", "/fixture/key") if configured else ("", "")
    monkeypatch.setattr(remote_test, "runner_settings", lambda: settings)
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: None)  # this Mac is free
    monkeypatch.setattr(remote_test, "local_browser_lock", lambda: pytest.fail("local browser fallback"))
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 75, "", "ssh: connect timed out")

    run = remote_test.m1_browser_runner(execute, "abc")
    assert "KYRA_WHERE" not in os.environ  # stripped from every pytest this landing starts
    with pytest.raises(Refused, match="--where m1: non-browser phase: M1 unreachable"):
        run(_where_local_non_browser(tmp_path), cwd=tmp_path)
    assert calls and all(Path(c[1]).name == "remote_test.py" for c in calls)  # no local pytest -n
    with pytest.raises(Refused, match="--where m1: browser phase: M1 unreachable"):
        run([sys.executable, "-m", "pytest", "-p", "no:xdist", "tests/test_x_browser.py"], cwd=tmp_path)
    assert not any(c[1:3] == ["-m", "pytest"] for c in calls)


def test_where_local_browser_phase_waits_for_the_heavy_job(tmp_path, monkeypatch):
    """o-rev91b: under --where local the serial browser phase (the other thread of _pytest_phases) took only the
    gpu gate and ran its whole selection beside a heavy job. Both halves now share one admission: the refusal comes
    before either half runs, and the runner is never called."""
    from deploy_local import Refused

    deploy_local = _deploy_local()
    monkeypatch.setenv("KYRA_SPLIT_TESTS", "1")
    _where_local(tmp_path, monkeypatch, refusal=lambda: "another heavy job is running: pid 7 xcodebuild")
    monkeypatch.setattr(remote_test, "LOCAL_WAIT_SECONDS", 0)
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    with pytest.raises((Refused, deploy_local.Refused), match="--where local: another heavy job"):
        deploy_local._pytest_phases(remote_test.m1_browser_runner(execute, "abc123"), sys.executable,
                                    _landing_tree(tmp_path), "abc123")
    assert calls == [], f"ran beside the heavy job: {calls}"


PS_LATE = ("40 1.0 python scripts/train.py t\n50 1.0 python scripts/workflow_land.py abc\n"
           "79 5.0 node /usr/local/bin/claude\n80 0.1 /bin/zsh -c .venv/bin/python -m pytest -n 2 tests/test_x.py\n"
           "81 150.0 .venv/bin/python -m pytest -n 2 tests/test_x.py\n")
PS_LATE_NEW = "85 0.1 /bin/zsh -c .venv/bin/python -m pytest -n 8\n84 400.0 .venv/bin/python -m pytest -n 8\n"
PARENTS_LATE = {40: 1, 50: 40, 79: 1, 80: 79, 81: 80, 85: 79, 84: 85}


def test_where_local_pauses_a_helper_that_started_during_the_wait(tmp_path, monkeypatch):
    """o-rev91b: the pause snapshot was taken once, before the (up to 10 min) wait, and every refusal poll ignores
    all helpers, so a helper pytest started during the wait ran unpaused beside the suite. The helpers are scanned
    again just before the suite starts and the new ones paused, then all are resumed."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import low_power

    real = remote_test.local_phase_helpers, remote_test.local_phase_refusal
    listings = iter([PS_LATE, PS_LATE + PS_LATE_NEW])
    signals = _where_local(tmp_path, monkeypatch)
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")

    def probes():
        listing = next(listings, PS_LATE + PS_LATE_NEW)
        return low_power.Probes(processes=lambda: listing, parents=lambda: dict(PARENTS_LATE), load=lambda: 1.0,
                                cores=lambda: 18, pid=lambda: 50, headroom=lambda: [])

    monkeypatch.setattr(remote_test, "local_phase_helpers", lambda: real[0](probes()))
    monkeypatch.setattr(remote_test, "local_phase_refusal", lambda: real[1](probes()))
    ran = []

    def execute(argv, **kwargs):
        ran.append(sorted(pid for pid, sig in signals.sent if sig == "SIGSTOP"))
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    assert remote_test.m1_browser_runner(execute, "abc")(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0
    assert ran and {81, 84} <= set(ran[0]), f"a helper ran unpaused beside the suite: {ran}"
    resumed = {pid for pid, sig in signals.sent if sig == "SIGCONT"}
    assert resumed == set(ran[0]), "every paused helper is resumed"


def test_a_gpu_refusal_is_shared_by_both_halves(tmp_path, monkeypatch):
    """o-rev91c: a gpu job that outlasted gate_lock.wait refused the first half, but the second half ran its own
    admission and waited again (up to 1800 s more). Every admission refusal is now recorded for the other half."""
    from companion import gate_lock

    deploy_local = _deploy_local()
    monkeypatch.setenv("KYRA_SPLIT_TESTS", "1")
    _where_local(tmp_path, monkeypatch)
    waits, calls = [], []
    monkeypatch.setattr(gate_lock, "wait", lambda path, **k: waits.append(path) and False)

    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    with pytest.raises(Exception, match="gpu job"):
        deploy_local._pytest_phases(remote_test.m1_browser_runner(execute, "abc123"), sys.executable,
                                    _landing_tree(tmp_path), "abc123")
    assert calls == [] and len(waits) == 1


def test_the_yield_marker_is_written_when_admission_starts(tmp_path, monkeypatch):
    """o-rev91c: helpers' --should-yield sees the local phase from the start of admission, not after the wait."""
    marked = []
    _where_local(tmp_path, monkeypatch, refusal=lambda: marked.append(remote_test.helpers_should_yield(
        tmp_path / "phase", load=0.0)) or None)
    run = remote_test.m1_browser_runner(lambda argv, **k: subprocess.CompletedProcess(argv, 0, "1 passed\n", ""), "abc")
    assert run(_where_local_non_browser(tmp_path), cwd=tmp_path).returncode == 0
    assert marked and marked[0] and "local test phase is running" in marked[0]
    assert remote_test.helpers_should_yield(tmp_path / "phase", load=0.0) is None  # removed afterwards


PS_APP = ("40 1.0 python scripts/train.py t\n50 1.0 python scripts/workflow_land.py abc\n"
          "60 2.0 /Applications/Claude.app/Contents/MacOS/Claude\n61 0.1 /bin/zsh -il\n"
          "62 150.0 .venv/bin/python -m pytest tests/test_y.py\n"
          "63 2.0 .venv/bin/python scripts/web_ui.py --port 8420\n"
          "64 80.0 /cache/chrome-headless-shell/chrome-headless-shell --headless --remote-debugging-pipe\n"
          "70 2.0 /Applications/Cursor.app/Contents/MacOS/Cursor\n71 0.1 /bin/zsh -il\n"
          "72 150.0 .venv/bin/python -m pytest tests/test_z.py\n"
          "79 5.0 node /usr/local/bin/claude\n80 0.1 /bin/zsh -c .venv/bin/python -m pytest -n 2 tests/test_x.py\n"
          "81 150.0 .venv/bin/python -m pytest -n 2 tests/test_x.py\n")
PARENTS_APP = {40: 1, 50: 40, 60: 1, 61: 60, 62: 61, 63: 60, 64: 63, 70: 1, 71: 70, 72: 71, 79: 1, 80: 79, 81: 80}


def test_owners_own_runs_under_an_app_terminal_are_never_paused(monkeypatch):
    """o-rev91c: CLI_AGENT matched the Claude and Cursor desktop apps case-insensitively, so the owner pytest in an app's
    terminal panel and the app preview's headless browser were paused. Only the agent CLIs count (control: the
    claude CLI's pytest is still a helper), and the owner runs stay heavy jobs that refuse, never paused."""
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    sys.path.insert(0, str(ROOT / "scripts"))
    import low_power

    probes = low_power.Probes(processes=lambda: PS_APP, parents=lambda: dict(PARENTS_APP), load=lambda: 1.0,
                              cores=lambda: 18, pid=lambda: 50, headroom=lambda: [])
    paused = remote_test.local_phase_helpers(probes)
    assert not {62, 64, 72} & set(paused), f"the owner own runs would be paused: {paused}"
    assert 81 in paused, "control: a pytest the claude CLI started is still a helper"
def test_a_held_lock_is_waited_for_and_the_run_starts_when_it_frees(monkeypatch, capsys):
    """Two trains collide on the one M1 (2026-10-15): exit 73 refused the second at once. A held lock is now polled
    every BUSY_POLL_SECONDS up to BUSY_WAIT_SECONDS, each wait logged; an unreachable runner is not waited for."""
    answers = iter([(False, "held:abc:12s"), (False, "held:abc:42s"), (True, "")])
    monkeypatch.setattr(remote_test, "acquire_lock", lambda token, sha: next(answers))
    monkeypatch.setattr(remote_test, "BUSY_WAIT_SECONDS", 1200)
    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    assert remote_test._acquire_waiting("t", "s", sleep=sleep, clock=lambda: now[0]) == (True, "")
    assert slept == [30, 30]
    err = capsys.readouterr().err
    assert "runner busy (held:abc:12s); waiting 30s, 1200s of 1200s left" in err and err.count("runner busy") == 2


def test_a_lock_that_never_frees_ends_busy_within_the_bound(monkeypatch):
    monkeypatch.setattr(remote_test, "acquire_lock", lambda token, sha: (False, "held:abc:1s"))
    monkeypatch.setattr(remote_test, "BUSY_WAIT_SECONDS", 100)
    now, slept = [0.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    ok, why = remote_test._acquire_waiting("t", "s", sleep=sleep, clock=lambda: now[0])
    assert not ok and why.startswith("held") and sum(slept) == 100 and slept == [30, 30, 30, 10]
    monkeypatch.setattr(remote_test, "acquire_lock", lambda token, sha: (False, "unreachable: no route"))
    assert remote_test._acquire_waiting("t", "s", sleep=lambda s: pytest.fail("no wait"),
                                        clock=lambda: 0.0) == (False, "unreachable: no route")


def test_the_wait_fits_inside_the_gate_and_a_bad_setting_never_breaks_the_import(monkeypatch, capsys):
    """A gate's timeout bounds the whole landing, and its kill would leave the M1 lock behind: the wait leaves
    BUSY_RESERVE_SECONDS for the run. KYRA_RUNNER_BUSY_WAIT is parsed with a fallback (workflow_land imports this)."""
    monkeypatch.setattr(remote_test, "BUSY_WAIT_SECONDS", 1200)
    monkeypatch.delenv("KYRA_GATE_TIMEOUT", raising=False)
    assert remote_test.busy_wait_seconds() == 1200
    monkeypatch.setenv("KYRA_GATE_TIMEOUT", "3600")  # a train: the full wait fits
    assert remote_test.busy_wait_seconds() == 1200
    monkeypatch.setenv("KYRA_GATE_TIMEOUT", "1800")  # deploy_local's default: no wait at all
    assert remote_test.busy_wait_seconds() == 0
    monkeypatch.setenv("KYRA_GATE_TIMEOUT", "2400")
    assert remote_test.busy_wait_seconds() == 600
    monkeypatch.setenv("KYRA_RUNNER_BUSY_WAIT", "twenty minutes")
    assert remote_test._env_seconds("KYRA_RUNNER_BUSY_WAIT", 1200) == 1200
    assert "is not a whole number of seconds; using 1200" in capsys.readouterr().err
    env = {**os.environ, "KYRA_RUNNER_BUSY_WAIT": "oops", "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                           "import remote_test; print(remote_test.BUSY_WAIT_SECONDS)"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "1200"


# ---- review of rp4c (dae9fd26): its probes, ported -------------------------------------------------------------

def _plumb(pool, entries):
    """Commit tree entries by name with git plumbing, independent of the file system's case folding."""
    git = ["git", "-C", str(pool["repo"]), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for path, text in entries.items():
        blob = subprocess.run([*git, "hash-object", "-w", "--stdin"], input=text.encode(), capture_output=True,
                              check=True).stdout.decode().strip()
        subprocess.run([*git, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], check=True)
    subprocess.run([*git, "commit", "-qm", "plumb", "--no-verify"], check=True)


@pytest.mark.parametrize("path", ["Data/team/runners.json", "DATA/private_docs/x.md", ".ENV", "sub/.Env.local",
                                  "deep/a/b/.env.local.bak", "é/.env"])
def test_rp4c_case_variants_never_reach_the_runner(pool, path):
    _plumb(pool, {path: "KYRA_FAKE=secret\n"})
    tree, why = remote_test.checked_archive("HEAD")
    assert tree is not None, why  # dropped by the pathspecs, not merely refused
    listing = subprocess.run(["tar", "-t"], input=tree, capture_output=True, check=True).stdout.decode().split()
    assert not any(remote_test.forbidden_path(n) for n in listing), listing
    assert "tests/test_ok.py" in listing


def test_rp4c_the_post_check_casefolds_too():
    for path in ("Data/x", "DATA", "x/.ENV", "x/.Env.Local"):
        assert remote_test.forbidden_path(path), path
    for path in (".env.example", ".ENV.EXAMPLE", "tests/data/x.json", "database/x.py", "data_notes.md"):
        assert not remote_test.forbidden_path(path), path


def test_rp4c_symlink_carries_link_text_only(pool):
    import io
    import tarfile
    repo = pool["repo"]
    (repo / "lnk").symlink_to("../data")
    (repo / "envlnk").symlink_to("tests/test_ok.py")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "lnk", "envlnk"], check=True)
    subprocess.run([*git, "commit", "-qm", "l", "--no-verify"], check=True)
    tree, why = remote_test.checked_archive("HEAD")
    assert tree is not None, why
    with tarfile.open(fileobj=io.BytesIO(tree)) as tar:
        links = {m.name: (m.issym(), m.linkname, m.size) for m in tar.getmembers() if m.name in ("lnk", "envlnk")}
    assert links == {"lnk": (True, "../data", 0), "envlnk": (True, "tests/test_ok.py", 0)}


@pytest.mark.parametrize("line", ["Could not resolve hostname box.example.test.",
                                  "identity file /fixture/box-key.pub type -1",
                                  "host=box.example.test;", "(box.example.test)"])
def test_rp4c_masking_edges(monkeypatch, line):
    monkeypatch.setattr(remote_test, "runner_settings", lambda: ("runner@box.example.test", "/fixture/box-key"))
    out = remote_test.safe_log(line)
    assert "box.example.test" not in out and "/fixture/box-key" not in out, out
    assert remote_test.safe_log("ran tests/test_ok.py on xbox.example.tests") == "ran tests/test_ok.py on xbox.example.tests"


def test_rp4c_module_level_skip_is_a_pass_remotely(pool):
    from companion import runner_exec
    from companion.runner_pool import Job, Load
    _commit(pool, {"tests/test_modskip.py": "import pytest\npytest.skip('x', allow_module_level=True)\n"})
    report = runner_exec.execute({"box": ["tests/test_modskip.py"]}, [Job("tests/test_modskip.py")],
                                 [pool["runners"]["box"]], remote_test.RemoteTransport("HEAD"), durations={},
                                 probe=lambda r: Load(), log=lambda s: None, repo_root=pool["repo"])
    assert report.verdict == "pass"


def test_rp4c_guard_in_remote_junit_is_error(tmp_path):
    junit = tmp_path / "j.xml"
    junit.write_text('<?xml version="1.0"?><testsuites><testsuite name="p" tests="2">'
                     '<testcase classname="tests.test_no_personal_files" name="t" '
                     'file="tests/test_no_personal_files.py" time="0.1"><skipped/></testcase>'
                     '<testcase classname="tests.test_ok" name="t" file="tests/test_ok.py" time="0.1"><skipped/></testcase>'
                     '</testsuite></testsuites>')
    files = remote_test.shard_files(junit, ("tests/test_ok.py",))
    assert files["tests/test_no_personal_files.py"].outcome == "error"
    assert files["tests/test_ok.py"].outcome == "pass"


def test_junit_copies_name_neither_the_runner_host_nor_its_user():
    xml = (b'<testsuite hostname="runner.example"><testcase file="tests/test_a.py" name="t">'
           b'<failure message="/tmp/example/kyra-runner/runs/x/tests/test_a.py:3"/></testcase></testsuite>')
    out = remote_test.scrub_junit(xml)
    assert b"m1-mini" not in out and b"someone" not in out and b'file="tests/test_a.py"' in out


def test_sigterm_on_the_runner_cli_kills_the_remote_group_before_unlock(pool, monkeypatch):
    """RP6 may stop a shard with SIGTERM: the CLI turns it into the same interrupt path as SIGINT."""
    import signal
    import time
    pids = pool["tmp"] / "pids"
    out = pool["tmp"] / "cli.out"
    env = {**os.environ, "SLEEP_PIDS": str(pids), "PYTHONPATH": str(ROOT / "src")}
    code = (f"import importlib.util, sys; spec = importlib.util.spec_from_file_location('rt', {str(ROOT / 'scripts/remote_test.py')!r}); "
            "rt = importlib.util.module_from_spec(spec); spec.loader.exec_module(rt); "
            f"rt.ROOT = __import__('pathlib').Path({str(pool['repo'])!r}); rt.BASE = {str(pool['base'])!r}; "
            f"rt.LOG_DIR = __import__('pathlib').Path({str(pool['tmp'] / 'logs')!r}); "
            "sys.exit(rt.main(['--ref', 'HEAD', '--runner', 'box', '--files', 'tests/test_sleep.py']))")
    with out.open("w") as sink:
        proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=sink, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 60
        while not (pids.exists() and pids.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert pids.exists(), out.read_text()
        proc.send_signal(signal.SIGTERM)
        proc.wait(60)
        assert "interrupted" in out.read_text()
        for pid in (int(x) for x in pids.read_text().split()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert not (pool["base"] / ".lock").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        if pids.exists():
            for pid in (int(x) for x in pids.read_text().split()):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_a_muse_workers_gate_pytest_is_a_helper_the_train_pauses(monkeypatch):
    """2026-10-17 FO3: a walled Muse worker's commit_gate pytest counted as "another heavy job" and refused two trains.
    The muse-bin CLI's pytest is now a helper like claude's; the owner own pytest still is not."""
    monkeypatch.setenv("KYRA_BROWSER_GATE_PID", "40")
    sys.path.insert(0, str(ROOT / "scripts"))
    import low_power

    ps = ("40 1.0 python scripts/train.py t\n50 1.0 python scripts/workflow_land.py abc\n"
          "90 2.0 /tmp/example/.local/bin/muse-bin-1.4.10-R6427.1 exec --model m\n"
          "91 1.0 python scripts/commit_gate.py -m x f.py\n92 150.0 .venv/bin/python -m pytest tests/test_a.py\n"
          "95 150.0 .venv/bin/python -m pytest tests/test_b.py\n")
    parents = {40: 1, 50: 40, 90: 1, 91: 90, 92: 91, 95: 1}
    probes = low_power.Probes(processes=lambda: ps, parents=lambda: dict(parents), load=lambda: 1.0,
                              cores=lambda: 18, pid=lambda: 50, headroom=lambda: [])
    paused = remote_test.local_phase_helpers(probes)
    assert 92 in paused and 95 not in paused
