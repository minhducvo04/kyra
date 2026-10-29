"""gate_lock: hold the landing-gate lock, wait for it, survive crashes. Real subprocesses, temp KYRA_DATA_DIR."""
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from companion import gate_lock as gl

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gate_lock.py"
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    return {**os.environ, "KYRA_DATA_DIR": str(tmp_path / "data")}


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "data" / "team" / "gate.lock"


def cli(env, *args, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60, **kwargs)


def spawn(env, *args):
    return subprocess.Popen([sys.executable, str(SCRIPT), *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def until(condition, seconds=15):
    """Poll until the condition holds or the deadline passes. The gap is a threading.Event."""
    end = time.monotonic() + seconds
    pause = threading.Event()
    while time.monotonic() < end:
        if condition():
            return True
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        pause.wait(min(0.005, remaining))
    return bool(condition())


def stderr_has(proc):
    """Return a predicate that is true once `proc.stderr` has contained the text."""
    lines = []
    lock = threading.Lock()

    def read():
        try:
            for line in proc.stderr:
                with lock:
                    lines.append(line)
        except (OSError, ValueError):
            return

    threading.Thread(target=read, daemon=True).start()

    def contains(text):
        with lock:
            return any(text in line for line in lines)

    return contains


def stays_up(proc, seconds):
    """True when `proc` is still running after `seconds`. False as soon as it exits."""
    return not until(lambda: proc.poll() is not None, seconds)


def test_run_holds_the_lock_for_the_command_and_removes_it_after(env, lock):
    probe = f"import json, pathlib; d = json.loads(pathlib.Path({str(lock)!r}).read_text()); print(d['pid'], d['command'][0] == 'x' or True)"
    done = cli(env, "run", "--", sys.executable, "-c", probe)
    assert done.returncode == 0 and done.stdout.split()[0].isdigit()
    assert not lock.exists()


def test_the_lock_names_pid_sha_start_and_command(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    try:
        assert until(lock.exists)
        info = json.loads(lock.read_text())
        assert info["pid"] == holder.pid and info["command"] == SLEEP
        assert isinstance(info["started_at"], float) and "sha" in info
    finally:
        holder.kill()
        holder.wait()


def test_run_returns_the_commands_exit_code_and_still_removes_the_lock(env, lock):
    assert cli(env, "run", "--", sys.executable, "-c", "raise SystemExit(7)").returncode == 7
    assert not lock.exists()


def test_a_command_that_cannot_start_is_a_refusal_and_leaves_no_lock(env, lock):
    done = cli(env, "run", "--", "definitely-not-a-program")
    assert done.returncode == 2 and "Could not start" in done.stderr and "Traceback" not in done.stderr
    assert not lock.exists()


def test_run_needs_a_command(env):
    done = cli(env, "run")
    assert done.returncode == 2 and "gate command" in done.stderr


def test_sigterm_forwards_to_the_command_and_clears_the_lock(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    assert until(lock.exists)
    holder.send_signal(signal.SIGTERM)
    holder.wait(timeout=20)
    assert not lock.exists()


def test_a_killed_wrapper_and_gate_command_leave_a_stale_lock_that_every_reader_clears(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    assert until(lambda: lock.exists() and json.loads(lock.read_text()).get("child_pid"))
    child_pid = json.loads(lock.read_text())["child_pid"]
    holder.kill()                                                       # no cleanup runs: a crash
    holder.wait()
    os.kill(child_pid, signal.SIGKILL)                                  # and the command is gone too
    assert until(lambda: not gl.alive(child_pid))
    assert lock.exists()
    status = cli(env, "status")
    assert status.returncode == 0 and "Gate is free." in status.stdout
    assert not lock.exists()


def test_a_new_run_replaces_a_stale_lock(env, lock):
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": 2 ** 22 + 12345, "sha": "old", "started_at": 1.0, "command": ["x"]}))
    done = cli(env, "run", "--", sys.executable, "-c", "print('ran')")
    assert done.returncode == 0 and "ran" in done.stdout
    assert not lock.exists()


@pytest.mark.parametrize("content", ["", "not json", "[]", json.dumps({"pid": "x"}), json.dumps({"pid": -1}), json.dumps({"pid": 0})])
def test_unreadable_or_pidless_locks_are_held_until_the_age_threshold(env, lock, content, monkeypatch):
    lock.parent.mkdir(parents=True)
    lock.write_text(content)
    assert gl.holder(lock) is not None
    with pytest.raises(gl.GateHeld):
        gl.acquire(lock, ["second"])
    assert "Gate running" in cli(env, "status").stdout
    assert cli(env, "wait", "--max-seconds", "0").returncode == gl.EXIT_TIMEOUT
    assert lock.read_text() == content
    modified = lock.stat().st_mtime
    with monkeypatch.context() as patch:
        patch.setattr(gl.time, "time", lambda: modified + gl.UNREADABLE_GRACE - 0.01)
        gl.clear_stale(lock)
        assert lock.read_text() == content
        patch.setattr(gl.time, "time", lambda: modified + gl.UNREADABLE_GRACE)
        gl.clear_stale(lock)
    assert cli(env, "wait", "--max-seconds", "5").returncode == 0
    assert not lock.exists()


def test_a_second_run_is_refused_while_a_live_gate_holds_the_lock(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    try:
        assert until(lock.exists)
        # The holder rewrites the lock once its child spawns; snapshot only after that.
        assert until(lambda: json.loads(lock.read_text()).get("child_pid") is not None)
        before = json.loads(lock.read_text())
        second = cli(env, "run", "--", sys.executable, "-c", "print('should not run')")
        assert second.returncode == 4 and "already holds" in second.stderr and "should not run" not in second.stdout
        after = json.loads(lock.read_text())
        assert after == before
    finally:
        holder.kill()
        holder.wait()


def test_wait_returns_at_once_when_the_gate_is_free(env):
    started = time.monotonic()
    done = cli(env, "wait")
    assert done.returncode == 0 and done.stderr == "" and time.monotonic() - started < 10


def test_wait_names_the_holder_and_gives_up_after_the_maximum(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    try:
        assert until(lock.exists)
        done = cli(env, "wait", "--max-seconds", "1", "--poll", "0.2")
        assert done.returncode == 3
        assert f"pid {holder.pid}" in done.stderr and "Waiting for the landing gate" in done.stderr and "Gave up after 1s" in done.stderr
    finally:
        holder.kill()
        holder.wait()


def test_concurrent_waiters_all_continue_when_the_gate_ends(env, lock):
    holder = spawn(env, "run", "--", sys.executable, "-c", "import time; time.sleep(3)")
    assert until(lock.exists)
    waiters = [spawn(env, "wait", "--max-seconds", "30", "--poll", "0.1") for _ in range(4)]
    codes = [w.wait(timeout=40) for w in waiters]
    assert codes == [0, 0, 0, 0]
    assert all("continuing" in w.stderr.read() for w in waiters)
    holder.wait(timeout=20)
    assert not lock.exists()


def test_a_waiter_clears_a_lock_whose_holder_dies_while_it_waits(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    assert until(lambda: lock.exists() and json.loads(lock.read_text()).get("child_pid"))
    child_pid = json.loads(lock.read_text())["child_pid"]
    waiter = spawn(env, "wait", "--max-seconds", "30", "--poll", "0.1")
    waiting = stderr_has(waiter)
    assert until(lambda: waiting("Waiting for the landing gate"))
    holder.kill()                                                       # the wrapper dies, the command is still a live gate
    holder.wait()
    assert stays_up(waiter, 0.5)                                        # poll is 0.1s; it must not drop the live command
    assert waiter.poll() is None
    os.kill(child_pid, signal.SIGKILL)                                  # now nothing is left: the waiter proceeds
    assert waiter.wait(timeout=30) == 0


def test_release_never_removes_someone_elses_lock(tmp_path):
    path = tmp_path / "gate.lock"
    path.write_text(json.dumps({"pid": os.getpid() + 1, "sha": "", "started_at": 1.0, "command": []}))
    gl.release(path)
    assert path.exists()
    path.write_text(json.dumps({"pid": os.getpid(), "sha": "", "started_at": 1.0, "command": []}))
    gl.release(path)
    assert not path.exists()


def test_status_and_stray_commands(env, lock):
    assert "Gate is free." in cli(env, "status").stdout
    holder = spawn(env, "run", "--", *SLEEP)
    try:
        assert until(lock.exists)
        shown = cli(env, "status")
        assert "Gate running" in shown.stdout and f"pid {holder.pid}" in shown.stdout
        assert cli(env, "status", "--", "x").returncode == 2
    finally:
        holder.kill()
        holder.wait()


# --- review findings (8bf00160) ------------------------------------------------------------------------------------


def test_a_recycled_pid_with_another_start_time_is_stale(env, lock):
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "pid_start": "Mon Jan 1 00:00:00 2001", "sha": "", "started_at": 1.0,
                                "command": ["x"], "child_pid": None, "child_start": ""}))
    done = cli(env, "wait", "--max-seconds", "5")              # the pid is alive (this test), but it is not that process
    assert done.returncode == 0 and not lock.exists()
    assert gl.alive(os.getpid(), gl.proc_start(os.getpid())) and not gl.alive(os.getpid(), "Mon Jan 1 00:00:00 2001")


def test_killing_the_wrapper_does_not_leave_the_gate_command_running_unlocked(env, lock):
    holder = spawn(env, "run", "--", *SLEEP)
    assert until(lambda: lock.exists() and json.loads(lock.read_text()).get("child_pid"))
    info = json.loads(lock.read_text())
    holder.kill()
    holder.wait()
    try:
        assert gl.alive(info["child_pid"], info["child_start"])      # the orphan keeps running, in its own session
        shown = cli(env, "status")
        assert "Gate running" in shown.stdout and f"gate command pid {info['child_pid']}" in shown.stdout
        second = cli(env, "run", "--", sys.executable, "-c", "print('second gate')")
        assert second.returncode == 4 and "second gate" not in second.stdout
        assert os.getpgid(info["child_pid"]) != os.getpgid(os.getpid())
    finally:
        os.kill(info["child_pid"], signal.SIGKILL)
    assert until(lambda: "Gate is free." in cli(env, "status").stdout)


def test_clearing_a_stale_lock_never_deletes_a_fresh_one(tmp_path, monkeypatch):
    path = tmp_path / "team" / "gate.lock"
    path.parent.mkdir()
    fresh = json.dumps({"pid": os.getpid(), "sha": "new", "started_at": 2.0, "command": ["new"]})
    path.write_text(fresh)
    real = os.rename

    def observe_rename(source, target):
        real(source, target)
        assert path.exists(), "cleanup exposed a live gate as free"

    monkeypatch.setattr(os, "rename", observe_rename)
    gl.clear_stale(path)
    assert path.read_text() == fresh
    assert list(path.parent.glob("gate.lock.stale.*")) == []


def test_owner_metadata_is_complete_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "gate.lock"
    real = json.dumps

    def observe_serialization(info):
        assert gl.read(path) is None, "lock became visible before owner metadata was ready"
        return real(info)

    monkeypatch.setattr(gl.json, "dumps", observe_serialization)
    monkeypatch.setattr(gl, "proc_start", lambda pid: "test process start")
    info = gl.acquire(path, ["probe"])
    assert gl.read(path) == info
    gl.release(path)


def _race_worker(path, connection):
    # Widen the original empty-file window while retaining real filesystem operations.
    real = json.dumps

    def slow_serialization(info):
        time.sleep(0.005)
        return real(info)

    gl.json.dumps = slow_serialization
    try:
        for _ in range(100):
            assert connection.recv() == "acquire"
            try:
                gl.acquire(path, ["race"])
                acquired = True
            except gl.GateHeld:
                acquired = False
            connection.send(acquired)
            # Hold the winner until BOTH acquisition results have been observed.
            assert connection.recv() == "release"
            if acquired:
                gl.release(path)
            connection.send("released")
    finally:
        connection.close()


def test_two_process_race_never_has_two_holders_in_100_rounds(tmp_path):
    path = tmp_path / "gate.lock"
    context = multiprocessing.get_context("spawn")
    pipes = [context.Pipe() for _ in range(2)]
    workers = [context.Process(target=_race_worker, args=(path, child)) for _, child in pipes]
    try:
        for worker in workers:
            worker.start()
        for _, child in pipes:
            child.close()
        for round_number in range(100):
            if round_number % 2:
                path.write_text(json.dumps({"pid": 2 ** 22 + 99, "command": ["stale"]}))
            for parent, _ in pipes:
                parent.send("acquire")
            results = []
            for parent, _ in pipes:
                assert parent.poll(15), f"acquisition stalled in round {round_number}"
                results.append(parent.recv())
            assert sum(results) == 1, (round_number, results)
            info = gl.read(path)
            assert info["pid"] == workers[results.index(True)].pid
            for parent, _ in pipes:
                parent.send("release")
            for parent, _ in pipes:
                assert parent.poll(15)
                assert parent.recv() == "released"
            assert not path.exists()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            if worker.pid is not None:
                worker.join(timeout=15)
        for parent, child in pipes:
            parent.close()
            child.close()


def test_exit_4_comes_from_a_typed_exception_not_message_text(env, lock, monkeypatch, capsys):
    assert issubclass(gl.GateHeld, gl.GateError)
    holder = spawn(env, "run", "--", *SLEEP)
    try:
        assert until(lock.exists)
        monkeypatch.setattr(gl, "describe", lambda info, lead: "the gate is busy")        # no 'already holds' in the text
        assert gl.main(["--repo", str(lock.parent), "run", "--", sys.executable, "-c", "pass"]) == 4
        assert "the gate is busy" in capsys.readouterr().err
        assert gl.main(["--repo", str(lock.parent), "run"]) == 2                          # other refusals stay 2
    finally:
        holder.kill()
        holder.wait()


def test_a_signal_before_the_command_starts_is_replayed_at_it(tmp_path, monkeypatch):
    path = tmp_path / "team" / "gate.lock"
    real = subprocess.Popen

    def popen_after_sigterm(*args, **kwargs):          # SIGTERM lands after the handler is set, before the command exists
        if kwargs.get("start_new_session"):            # only the gate command, not the ps calls around it
            signal.raise_signal(signal.SIGTERM)
        return real(*args, **kwargs)

    monkeypatch.setattr(gl.subprocess, "Popen", popen_after_sigterm)
    started = time.monotonic()
    code = gl.run(SLEEP, repo=str(tmp_path), path=path)
    assert code == -signal.SIGTERM and time.monotonic() - started < 20
    assert not path.exists()


def test_a_sigterm_right_after_the_lock_is_taken_still_clears_it(env, lock):
    """The gap: lock created, handler not yet installed. The signal must not hit the default action."""
    code = ("import os, signal, sys\nfrom companion import gate_lock as gl\nreal = gl.acquire\n"
            "def acquire(*a, **k):\n    info = real(*a, **k)\n    os.kill(os.getpid(), signal.SIGTERM)\n    return info\n"
            f"gl.acquire = acquire\nsys.exit(gl.run({SLEEP!r}))\n")
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode >= 0, done.stderr            # exited through run(), not killed by SIGTERM
    assert not lock.exists()


def test_landing_and_gpu_runs_do_not_block_each_other_but_two_gpu_runs_do(env, tmp_path):
    """Lock split (2026-10-15): a train under KYRA_REMOTE_TESTS=1 holds the landing kind while its heavy phases run
    on the M1; GPU jobs (profiles, intern shadow, LoRA) hold the gpu kind and no longer sit idle behind it."""
    team = tmp_path / "data" / "team"
    landing = spawn(env, "run", "--kind", "landing", "--", *SLEEP)
    assert until(lambda: (team / "gate-landing.lock").exists())
    gpu = spawn(env, "run", "--kind", "gpu", "--", *SLEEP)
    assert until(lambda: (team / "gate-gpu.lock").exists())
    assert gpu.poll() is None and landing.poll() is None  # both running at once
    second = cli(env, "run", "--kind", "gpu", "--", "true")
    assert second.returncode == gl.EXIT_HELD and "gate-gpu.lock" in second.stderr or "holds the lock" in second.stderr
    status = cli(env, "status")
    assert "gate-landing.lock" in status.stdout and "gate-gpu.lock" in status.stdout
    # the gpu kind waits only for what blocks it: the landing does not
    assert cli(env, "wait", "--kind", "landing", "--max-seconds", "1").returncode == gl.EXIT_TIMEOUT
    for proc in (landing, gpu):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    assert not (team / "gate-landing.lock").exists() and not (team / "gate-gpu.lock").exists()


def test_the_default_lock_excludes_every_kind_and_waits_for_every_kind(env, tmp_path):
    team = tmp_path / "data" / "team"
    gpu = spawn(env, "run", "--kind", "gpu", "--", *SLEEP)
    assert until(lambda: (team / "gate-gpu.lock").exists())
    plain = cli(env, "run", "--", "true")  # a caller that names no kind behaves as before: refused while anything runs
    assert plain.returncode == gl.EXIT_HELD
    assert cli(env, "wait", "--max-seconds", "1").returncode == gl.EXIT_TIMEOUT
    gpu.send_signal(signal.SIGTERM)
    gpu.wait(timeout=10)
    plain = spawn(env, "run", "--", *SLEEP)
    assert until(lambda: (team / "gate.lock").exists())
    assert cli(env, "run", "--kind", "gpu", "--", "true").returncode == gl.EXIT_HELD
    assert cli(env, "run", "--kind", "landing", "--", "true").returncode == gl.EXIT_HELD
    assert cli(env, "wait", "--kind", "gpu", "--max-seconds", "1").returncode == gl.EXIT_TIMEOUT
    plain.send_signal(signal.SIGTERM)
    plain.wait(timeout=10)
    assert cli(env, "run", "--kind", "gpu", "--", "true").returncode == 0
    with pytest.raises(gl.GateError, match="unknown lock kind"):
        gl.lock_path(tmp_path, "cpu")
