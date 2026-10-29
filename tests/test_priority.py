"""Admission by priority (2026-10-15): ranks from the feature queue, yielding to protected lock holders, and the
pause of lower-priority workers' pytest children by process tree. Real processes where a signal is involved."""
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from companion import gate_lock, priority

QUEUE = ["loop-building", "auto-system", "long-input", "home-team", "example-site", "3d-inner-demo", "wrap-up"]


def _queue(data):
    (data / "team").mkdir(parents=True, exist_ok=True)
    (data / "team/feature-queue.json").write_text(json.dumps({"features": [{"name": n} for n in QUEUE]}))


def test_ranks_come_from_the_queue_order_and_briefs_name_theirs(tmp_path):
    _queue(tmp_path)
    assert priority.feature_rank(tmp_path, "loop-building") == 1
    assert priority.feature_rank(tmp_path, "long-input") == 3
    assert priority.feature_rank(tmp_path, "example-site") == 5
    assert priority.feature_rank(tmp_path, "audit-2026") == priority.LAST
    assert priority.feature_rank(tmp_path / "missing", "loop-building") == priority.LAST
    assert priority.brief_rank("Execute this node.\nPriority: 3\nmore") == 3
    assert priority.brief_rank("no rank named") == priority.LAST
    assert priority.brief_rank("Priority: 0") == 1 and priority.brief_rank("Priority: 999") == priority.LAST
    assert priority.brief_rank("the brief says Priority: 2 inline, not on its own line") == priority.LAST
    assert priority.rank_line(4) == "Priority: 4"


def test_lower_priority_work_yields_to_a_live_protected_landing_or_gpu_holder(tmp_path):
    team = tmp_path / "team"
    team.mkdir()
    live = lambda info: info.get("pid") == 4242  # noqa: E731
    assert priority.yields(7, team, live=live) is None  # nothing held
    (team / "gate-gpu.lock").write_text(json.dumps({"pid": 4242, "priority": 3, "command": ["profile"]}))
    assert priority.yields(7, team, live=live) == "yields to a priority 3 gpu job (pid 4242)"
    assert priority.yields(3, team, live=live) is None  # equal rank does not yield
    assert priority.yields(1, team, live=live) is None
    (team / "gate-landing.lock").write_text(json.dumps({"pid": 4242, "priority": 1, "command": ["train"]}))
    assert priority.yields(2, team, live=live) == "yields to a priority 1 landing job (pid 4242)"
    (team / "gate-landing.lock").write_text(json.dumps({"pid": 1, "priority": 1}))  # dead holder: ignored
    assert priority.yields(2, team, live=live) is None
    (team / "gate-gpu.lock").write_text(json.dumps({"pid": 4242, "priority": 5}))  # rank 5 protects nothing
    assert priority.yields(7, team, live=live) is None
    (team / "gate-gpu.lock").write_text(json.dumps({"pid": 4242}))  # no rank recorded: protects nothing
    assert priority.yields(7, team, live=live) is None


def test_pause_targets_follow_the_process_tree_and_the_childs_own_argv_never_the_cli_text():
    rows = [
        (100, 1, "claude -p 'build it; run python -m pytest tests/test_x.py and report'"),   # the P7 CLI itself
        (101, 100, "/usr/bin/bash -c python -m pytest tests"),                                 # a shell, not pytest
        (102, 101, "/opt/venv/bin/python3.12 -m pytest -n 2 tests/test_x.py"),                # the pytest run
        (103, 102, "/opt/venv/bin/python3.12 -c from multiprocessing.spawn import ..."),      # xdist worker
        (104, 100, "/opt/venv/bin/pytest -q tests/test_y.py"),                                # a pytest executable
        (200, 1, "/opt/venv/bin/python3.12 -m pytest tests/unrelated.py"),                    # not under a worker
        (300, 1, "codex exec 'pytest everything' -C /wt"),                                    # a P3 CLI
        (301, 300, "/opt/venv/bin/python3.12 -m pytest tests/test_p3.py"),                    # the P3's pytest
    ]
    assert priority.pause_targets(rows, [100]) == [102, 104]
    assert priority.pause_targets(rows, [100, 300]) == [102, 104, 301]
    assert priority.pause_targets(rows, [200]) == []  # a pytest process is never a worker root's own target
    assert priority.pause_targets(rows, []) == []
    assert priority.is_pytest_argv("claude -p 'python -m pytest'") is False
    assert priority.is_pytest_argv("python -m pytest") is True and priority.is_pytest_argv("pytest -q") is True
    assert priority.is_pytest_argv("") is False and priority.is_pytest_argv("python -m ruff") is False


def test_lower_workers_come_from_the_ledger_with_live_pids_only(tmp_path):
    ledger = tmp_path / "packages.jsonl"
    rows = [dict(id="a", pid=10, priority=7), dict(id="b", pid=11, priority=3), dict(id="c", pid=12, priority=9),
            dict(id="d", priority=9), dict(id="e", pid=13), "not json"]
    ledger.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n")
    alive = lambda pid: pid != 12  # noqa: E731
    assert priority.lower_workers(ledger, 3, alive=alive) == [10]
    assert priority.lower_workers(ledger, 5, alive=alive) == [10]
    assert priority.lower_workers(ledger, 1, alive=alive) == [10, 11]
    assert priority.lower_workers(tmp_path / "none.jsonl", 1, alive=alive) == []


def _worker_with_a_live_pytest(tmp_path, name):
    """A real worker process whose child is a real pytest run that sleeps while collecting."""
    folder = tmp_path / name
    folder.mkdir()
    # Sleep keeps the collected pytest alive so pause_targets can stop and resume it.
    (folder / "test_sleepy.py").write_text("import time\ntime.sleep(120)\n")
    script = textwrap.dedent(f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--co", "-q", {str(folder)!r}])
        p.wait()
    """)
    env = {**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    return subprocess.Popen([sys.executable, "-c", script], env=env)


def _pytest_child(worker_pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = priority.process_rows()
        targets = priority.pause_targets(rows, [worker_pid])
        if targets:
            return targets[0]
    raise AssertionError("the worker's pytest child never appeared")


def _state(pid):
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("run_rank, worker_rank, paused", [(3, 7, True), (5, 3, False)])
def test_a_protected_run_pauses_lower_priority_pytest_and_resumes_it(tmp_path, monkeypatch, run_rank, worker_rank, paused):
    """A P3 gpu run stops a P7 builder's pytest for its duration and resumes it after; a P5 run leaves a P3's
    pytest alone. Real processes, real signals; the CLI-like parent is never stopped."""
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path / "data"))
    worker = _worker_with_a_live_pytest(tmp_path, "builder")
    try:
        child = _pytest_child(worker.pid)
        ledger = tmp_path / "data/context/packages.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps(dict(id="w", pid=worker.pid, priority=worker_rank)) + "\n")
        out = tmp_path / "probe.txt"
        probe = textwrap.dedent(f"""
            import subprocess
            def st(p):
                return subprocess.run(["ps", "-o", "stat=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
            open({str(out)!r}, "w").write(st({child}) + "\\n" + st({worker.pid}) + "\\n")
        """)
        code = gate_lock.run([sys.executable, "-c", probe], repo=tmp_path, path=tmp_path / "data/team/gate-gpu.lock",
                             kind="gpu", priority=run_rank, pause_lower=True, ledger=ledger)
        assert code == 0
        child_state, worker_state = out.read_text().splitlines()[:2]
        assert child_state.startswith("T") is paused, (child_state, worker_state)  # stopped during the run, or not
        assert not worker_state.startswith("T")  # the worker itself was never stopped
        assert not _state(child).startswith("T")  # resumed after the run
        assert not (tmp_path / "data/team/gate-gpu.lock").exists()
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
        subprocess.run(["pkill", "-f", str(tmp_path / "builder")], capture_output=True)


def test_the_lock_records_the_runs_priority_and_the_cli_takes_it(tmp_path):
    env = {**os.environ, "KYRA_DATA_DIR": str(tmp_path / "data")}
    lock = tmp_path / "data/team/gate-gpu.lock"
    probe = f"import json, pathlib; print(json.loads(pathlib.Path({str(lock)!r}).read_text())['priority'])"
    script = Path(__file__).resolve().parents[1] / "scripts" / "gate_lock.py"
    done = subprocess.run([sys.executable, str(script), "run", "--kind", "gpu", "--priority", "2", "--",
                           sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "2"
    done = subprocess.run([sys.executable, str(script), "run", "--", sys.executable, "-c",
                           probe.replace("gate-gpu.lock", "gate.lock")], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.strip() == "None"  # no priority named: recorded as none
