"""RP6: the train's non-browser phase through the runner pool (companion.train_pool), with fake transports and one
real local pytest run on a tiny tree. The train-level cases (flag off unchanged, flag on lands or refuses) are in
tests/test_train.py."""
import json
import os
import signal
import textwrap
import threading
import time
from pathlib import Path

import pytest

from companion import runner_exec as rx
from companion import train_pool as tp
from companion.runner_pool import Load, Runner

MAC = Runner("mac", "macos", 8, 36, reach="local", may_see_real_data=True)
M1 = Runner("m1", "macos", 8, 16, reach="ssh", host="runner@m1.example.invalid", speed=0.6)
GUARDS = ("tests/test_no_third_party_pii.py", "tests/test_no_personal_files.py")


@pytest.fixture(autouse=True)
def _no_runner_pool_switches(monkeypatch):
    """The train's KYRA_RUNNER_POOL* switches belong to the outer train, never these tests (RP7, 2026-10-16)."""
    for key in [k for k in os.environ if k.startswith("KYRA_RUNNER_POOL")]:
        monkeypatch.delenv(key)


def idle(runner):
    return Load(0.0)


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


def tree(tmp_path, names):
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("import time\n\n\ndef test_ok():\n    time.sleep(0.02)\n")
    return tmp_path


def test_the_set_is_every_non_browser_file_and_splits_exactly_once(tmp_path):
    tree(tmp_path, ["tests/test_a.py", "tests/test_team_browser.py", "tests/test_no_target_companies.py",
                    *GUARDS, "tests/sub/test_deep.py"])
    files = tp.test_files(tmp_path)
    assert "tests/test_team_browser.py" not in files and "tests/sub/test_deep.py" in files
    local, pool = tp.split(files, GUARDS)
    assert local == ["tests/test_no_target_companies.py"]  # real data: this Mac, never a transport
    assert pool == ["tests/sub/test_deep.py", "tests/test_a.py"]
    assert not set(pool) & set(GUARDS)


def test_load_runners_falls_back_to_the_example_local_runner_and_refuses_a_bad_registry(tmp_path):
    lines = []
    runners = tp.load_runners(tmp_path, Path(__file__).resolve().parents[1], lines.append)
    assert [r.reach for r in runners] == ["local"] and "this Mac only" in lines[0]
    (tmp_path / "data/team").mkdir(parents=True)
    (tmp_path / "data/team/runners.json").write_text(json.dumps({"runners": [
        {"name": "m1", "os": "macos", "cores": 8, "ram_gb": 16, "reach": "ssh", "host": "x@example.invalid",
         "may_see_real_data": True}]}))
    with pytest.raises(tp.PoolError, match="registry refused"):
        tp.load_runners(tmp_path, tmp_path, lines.append)
    (tmp_path / "data/team/runners.json").write_text("{not json")
    with pytest.raises(tp.PoolError):
        tp.load_runners(tmp_path, tmp_path, lines.append)


def test_junit_outcomes_and_a_missing_file_is_an_error(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text(textwrap.dedent("""\
        <testsuites><testsuite>
          <testcase classname="tests.test_a" name="t1" time="1.5"/>
          <testcase classname="tests.test_a" name="t2" time="0.5"><skipped/></testcase>
          <testcase classname="tests.test_b.TestX" name="t1" time="2"><failure/></testcase>
          <testcase classname="" name="tests.test_c" time="0"><error/></testcase>
          <testcase classname="tests.test_no_target_companies" name="t" time="0"><skipped/></testcase>
        </testsuite></testsuites>"""))
    got = tp.junit_results(xml, ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py",
                                 "tests/test_no_target_companies.py"])
    assert got["tests/test_a.py"] == rx.FileResult("pass", 2.0)
    assert got["tests/test_b.py"].outcome == "fail" and got["tests/test_c.py"].outcome == "error"
    assert got["tests/test_d.py"].outcome == "error"  # not in the report: never a pass
    assert got["tests/test_no_target_companies.py"].outcome == "skip"  # RP5 refuses a skipped real-data file


def test_a_corrupt_duration_store_is_rebuilt_with_a_warning_and_never_stops_the_run(tmp_path):
    store = tmp_path / "test-durations.json"
    store.write_text("{corrupt")
    lines = []
    tp.recorder(store, lines.append)({"tests/test_a.py": 3.0, "tests/test_b.py": 0.0}, "mac")
    data = json.loads(store.read_text())
    assert data["tests/test_a.py"] == 3.0 and "tests/test_b.py" not in data  # zero seconds: not timed, not kept
    assert any("WARNING duration store corrupt" in line for line in lines)
    assert list(tmp_path.glob("test-durations.json.corrupt-*"))


class FakePopen:
    calls = []

    def __init__(self, argv, **kw):
        FakePopen.calls.append(argv)
        self.returncode = None
        self.argv = argv

    def communicate(self):
        self.returncode = 0
        return "FAILED tests/test_a.py::test_x - boom\n", None

    def poll(self):
        return self.returncode


def test_remote_adapter_calls_remote_test_runner_and_is_unreachable_until_rp4(tmp_path):
    attempt = rx.Attempt("m1#1", M1, ("tests/test_a.py", "tests/test_b.py"))
    for features in (set(), {"--runner", "--files"}):  # --junit is required too (review o-rev81)
        absent = tp.RemoteTestTransport(tmp_path / "remote_test.py", "abc123", features=features, popen=FakePopen)
        assert not absent.available and absent.run(attempt, {}).exit_code == rx.EXIT_UNREACHABLE
    FakePopen.calls.clear()
    adapter = tp.RemoteTestTransport(tmp_path / "remote_test.py", "abc123",
                                     features={"--runner", "--files", "--junit"}, popen=FakePopen, log=lambda line: None)
    adapter.workers = {"m1": 5}
    result = adapter.run(attempt, {})
    argv = FakePopen.calls[0]
    assert argv[1:6] == [str(tmp_path / "remote_test.py"), "--runner", "m1", "--ref", "abc123"]
    assert argv[6] == "--junit" and argv[8:10] == ["--workers", "5"]
    assert argv[10:] == ["--files", "tests/test_a.py", "tests/test_b.py"]
    assert result.exit_code == 0 and {r.outcome for r in result.files.values()} == {"error"}  # no junit came back


def test_remote_workers_are_one_per_free_core_the_plan_budgeted_and_never_below_one():
    # capacity() is free cores x per-core speed: speed (0.6) never shrinks the worker count
    assert tp.planned_workers(M1, Load(0.0)) == 8
    assert tp.planned_workers(M1, Load(5.0)) == 3
    assert tp.planned_workers(M1, Load(5.3)) == 2  # 2.7 free: floor, never oversubscribe
    assert tp.planned_workers(M1, Load(8.0)) == 1 and tp.planned_workers(M1, Load(12.0)) == 1
    assert tp.planned_workers(M1, Load(0.0, busy=True)) == 1 and tp.planned_workers(M1, None) == 1


FAKE_REMOTE = """import sys, json
from pathlib import Path
if "--help" in sys.argv:
    print("usage: remote_test [--runner NAME] [--junit P] [--workers N] [--files F ...]")
    raise SystemExit(0)
argv = sys.argv[1:]
Path(sys.argv[0]).with_name("argv.json").write_text(json.dumps(argv))
files = argv[argv.index("--files") + 1:]
cases = "".join(f'<testcase file="{f}" name="t" time="0.1"/>' for f in files)
Path(argv[argv.index("--junit") + 1]).write_text(f"<testsuite>{cases}</testsuite>")
"""


def test_the_plan_reading_sets_the_remote_worker_count_not_a_later_probe(tmp_path):
    """run_pool probes once: shard sizing and --workers use the same free-core count (3 of 8, not 8 later)."""
    script = tmp_path / "remote_test.py"
    script.write_text(FAKE_REMOTE)
    readings = iter([Load(5.0)])

    def probe(runner):
        return next(readings, Load(0.0))

    lines = []
    ok = tp.run_pool(tmp_path, "HEAD", tmp_path, ["tests/test_a.py"], lines.append, runners=[M1], probe=probe,
                     store=tmp_path / "d.json", remote_script=script)
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert ok, "\n".join(lines)
    assert argv[argv.index("--workers") + 1] == "3"


def test_feature_detection_reads_remote_test_help(tmp_path):
    script = tmp_path / "remote_test.py"
    script.write_text("print('usage: remote_test [--ref REF] [--runner NAME] [--junit P] [--files F ...]')\n")
    assert tp.remote_test_features(script) == {"--runner", "--files", "--junit"}
    script.write_text("print('usage: remote_test [--ref REF]')\n")
    assert tp.remote_test_features(script) == set()


def test_without_rp4_remote_runners_are_left_out_and_a_real_local_run_passes(tmp_path):
    """One real run: LocalTransport runs pytest on a tiny tree, the m1 entry is dropped because the fake
    remote_test.py has no --runner, durations land in the store."""
    work = tree(tmp_path / "w", ["tests/test_a.py", "tests/test_b.py"])
    script = tmp_path / "remote_test.py"
    script.write_text("print('usage: remote_test [--ref REF]')\n")
    lines = []
    store = tmp_path / "durations.json"
    ok = tp.run_pool(work, "HEAD", tmp_path, ["tests/test_a.py", "tests/test_b.py"], lines.append,
                     runners=[MAC, M1], probe=idle, store=store, remote_script=script)
    text = "\n".join(lines)
    assert ok, text
    assert "remote runners left out" in text and "mac ran 2 files" in text and "m1" not in text.split("left out")[1]
    assert set(json.loads(store.read_text())["_runners"]["mac"]) == {"tests/test_a.py", "tests/test_b.py"}


def test_a_real_local_failure_fails_the_phase(tmp_path):
    work = tmp_path / "w"
    tree(work, ["tests/test_a.py"])
    (work / "tests/test_bad.py").write_text("def test_bad():\n    assert False\n")
    lines = []
    transport = tp.LocalTransport(work, workers=1, log=lines.append)
    ok = tp.run_pool(work, "HEAD", tmp_path, ["tests/test_a.py", "tests/test_bad.py"], lines.append,
                     runners=[MAC], probe=idle, store=tmp_path / "d.json", transport=transport)
    assert not ok and any("FAILED tests/test_bad.py" in line for line in lines)


def test_no_runner_at_planning_is_todays_path():
    lines = []
    assert tp.run_pool(Path("."), "HEAD", Path("."), ["tests/test_a.py"], lines.append, runners=[MAC],
                       probe=lambda r: None, transport=object(), store=Path("/nonexistent/d.json"))
    assert "today's path" in lines[-1]


def test_enabled_only_for_exactly_one():
    assert tp.enabled({"KYRA_RUNNER_POOL": "1"})
    assert not any(tp.enabled(env) for env in ({}, {"KYRA_RUNNER_POOL": "0"}, {"KYRA_RUNNER_POOL": "yes"}))


# ---- review o-rev81 fix-first: the three defect probes, ported ------------------------------------------------

def _fake_python(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_exit_zero_with_no_per_file_evidence_is_never_a_pass(tmp_path):
    """o-rev81 defect 2: a run that exits 0 but writes no junit (it may have run only some files) is every file
    "error"; the remote adapter needs --junit before it is used at all."""
    work = tree(tmp_path / "w", ["tests/test_a.py", "tests/test_b.py"])
    script = _fake_python(tmp_path / "fake_py", 'echo "1 passed"\nexit 0\n')
    local = tp.LocalTransport(work, python=script, workers=1, env={"PATH": os.environ["PATH"]}, log=lambda line: None)
    result = local.run(rx.Attempt("mac#1", MAC, ("tests/test_a.py", "tests/test_b.py")), {})
    assert {r.outcome for r in result.files.values()} == {"error"}
    remote = tp.RemoteTestTransport(tmp_path / "remote_test.py", "abc", features={"--runner", "--files"},
                                    popen=FakePopen, log=lambda line: None)
    assert remote.run(rx.Attempt("m1#1", M1, ("tests/test_a.py",)), {}).exit_code == rx.EXIT_UNREACHABLE


def test_an_unreadable_registry_is_a_pool_error_not_a_crash(tmp_path):
    """o-rev81 defect 3: a runners.json that is a directory raises OSError; it must be a PoolError (step failure,
    exit 3), never a traceback."""
    (tmp_path / "data/team/runners.json").mkdir(parents=True)
    with pytest.raises(tp.PoolError, match="registry refused"):
        tp.load_runners(tmp_path, tmp_path, lambda line: None)


def test_deadline_kill_reaches_the_tests_children(tmp_path):
    """o-rev81 defect 1: the kill goes to the attempt's whole process group (its own session), SIGKILL after the
    grace, so a process a hung test started does not outlive the train."""
    work = tree(tmp_path / "w", ["tests/test_a.py"])
    pidfile = tmp_path / "child.pid"
    script = _fake_python(tmp_path / "fake_py", 'sleep 120 &\necho $! > "$PIDFILE"\nwait\n')
    transport = tp.LocalTransport(work, python=script, workers=1, env={"PATH": os.environ["PATH"]},
                                  log=lambda line: None)
    attempt = rx.Attempt("mac#1", MAC, ("tests/test_a.py",))
    thread = threading.Thread(target=transport.run, args=(attempt, {"PIDFILE": str(pidfile)}), daemon=True)
    thread.start()

    def child_pid_written():
        if pidfile.exists() and pidfile.read_text().strip():
            return True
        assert thread.is_alive(), "transport ended before the child pid was written"
        return False

    assert until(child_pid_written)
    child = int(pidfile.read_text())
    try:
        transport.kill(attempt)
        thread.join(25)

        def child_gone():
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return True
            return False

        assert until(child_gone)
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


class EnvPopen(FakePopen):
    envs = []

    def __init__(self, argv, **kw):
        super().__init__(argv, **kw)
        EnvPopen.envs.append(kw.get("env"))


def test_local_pool_shard_never_sees_runner_pool_env(tmp_path, monkeypatch):
    """RP7 (2026-10-16): the mac shard inherited the train's KYRA_RUNNER_POOL=1 and tests/test_train.py failed 4
    tests there. Neither shard's child env may carry a KYRA_RUNNER_POOL* key, even one passed in per attempt."""
    monkeypatch.setenv("KYRA_RUNNER_POOL", "1")
    monkeypatch.setenv("KYRA_RUNNER_POOL_X", "1")
    EnvPopen.envs.clear()
    attempt = rx.Attempt("mac#1", MAC, ("tests/test_a.py",))
    tp.LocalTransport(tmp_path, workers=1, popen=EnvPopen, log=lambda line: None).run(
        attempt, {"KYRA_RUNNER_POOL": "1"})
    remote = tp.RemoteTestTransport(tmp_path / "remote_test.py", "abc123", popen=EnvPopen, log=lambda line: None,
                                    features={"--runner", "--files", "--junit"})
    remote.run(rx.Attempt("m1#1", M1, ("tests/test_a.py",)), {"KYRA_RUNNER_POOL_X": "1"})
    assert len(EnvPopen.envs) == 2
    for env, argv in zip(EnvPopen.envs, EnvPopen.calls[-2:], strict=True):
        assert not [k for k in env if k.startswith("KYRA_RUNNER_POOL")], sorted(env)
        assert not [a for a in argv if "KYRA_RUNNER_POOL" in a]
        assert "PATH" in env  # the rest of the environment still goes through


def test_shards_never_see_the_trains_runner_state(tmp_path, monkeypatch):
    """rp7c (2026-10-16): the queue runner exports KYRA_RUNNER_STATE=free (the precheck's M1 reading) into the
    train; the mac shard passed it on and test_probe_preflight_judges_local_at_12 judged the M1 path. It is the
    train's switch for its own guard runs, never a shard's."""
    monkeypatch.setenv("KYRA_RUNNER_STATE", "free")
    EnvPopen.envs.clear()
    tp.LocalTransport(tmp_path, workers=1, popen=EnvPopen, log=lambda line: None).run(
        rx.Attempt("mac#1", MAC, ("tests/test_a.py",)), {})
    tp.RemoteTestTransport(tmp_path / "remote_test.py", "abc123", popen=EnvPopen, log=lambda line: None,
                           features={"--runner", "--files", "--junit"}).run(
        rx.Attempt("m1#1", M1, ("tests/test_a.py",)), {})
    assert len(EnvPopen.envs) == 2 and all("KYRA_RUNNER_STATE" not in env for env in EnvPopen.envs)


def test_a_real_local_shard_runs_without_the_pool_flag(tmp_path, monkeypatch):
    """End to end: a real LocalTransport pytest run of a test that asserts the flag is absent passes with the flag
    set in the parent."""
    monkeypatch.setenv("KYRA_RUNNER_POOL", "1")
    monkeypatch.setenv("KYRA_RUNNER_POOL_X", "1")
    work = tmp_path / "w"
    (work / "tests").mkdir(parents=True)
    (work / "tests/test_env.py").write_text(
        "import os\n\n\ndef test_no_pool_flag():\n"
        "    assert not [k for k in os.environ if k.startswith('KYRA_RUNNER_POOL')]\n")
    lines = []
    result = tp.LocalTransport(work, workers=1, log=lines.append).run(
        rx.Attempt("mac#1", MAC, ("tests/test_env.py",)), {})
    assert result.exit_code == 0 and result.files["tests/test_env.py"].outcome == "pass", lines


# ---- balance: the local shard runs the workers the plan budgeted (rp7e, 2026-10-16) ----------------------------

def _probes(load=0.0, cores=8, free=50, pressure="1"):
    from companion import headroom
    return headroom.Probes(load=lambda: load, cores=lambda: cores, memory=lambda: f"free percentage: {free}%",
                           pressure=lambda: pressure)


def test_local_shard_n_equals_the_plans_budget(tmp_path):
    """rp7e: the plan credited this Mac with its free cores, LocalTransport ran 4 xdist workers, the Mac took 442 s
    against the M1's 160 s. The local shard's -n is the plan's budget: free cores, capped by the headroom rule."""
    pytest.importorskip("xdist")
    FakePopen.calls.clear()
    lines = []
    transport = tp.LocalTransport(tmp_path, popen=FakePopen, log=lines.append)
    tp.run_pool(tmp_path, "HEAD", tmp_path, ["tests/test_a.py"], lines.append, runners=[MAC], probe=idle,
                transport=transport, store=tmp_path / "d.json", headroom_probes=_probes(load=0.0, cores=8))
    argv = FakePopen.calls[0]
    budget = tp.worker_budget(MAC, Load(0.0), _probes(load=0.0, cores=8))
    assert budget == 7  # 8 free cores, capped at floor(0.9 x 8 - 0) = 7
    assert argv[argv.index("-n") + 1] == str(budget)


def _free(runner, plan):
    from companion import runner_pool
    return runner_pool.free_cores(runner, plan.probe(runner))


def test_the_headroom_cap_applies_under_high_load_and_the_plan_uses_it(tmp_path):
    busy = _probes(load=6.5, cores=8)
    assert tp.worker_budget(MAC, Load(0.0), busy) == 1  # floor(7.2 - 6.5) = 0 -> at least 1
    assert tp.worker_budget(MAC, Load(0.0), _probes(load=2.0, cores=8)) == 5  # floor(7.2 - 2) = 5 < 8 free
    assert tp.worker_budget(MAC, Load(0.0), _probes(free=5)) == 1  # memory short: one worker
    assert tp.worker_budget(M1, Load(0.0), busy) == 8  # the headroom rule is this Mac's, not the M1's
    plan = tp.plan_pool(["tests/test_a.py", "tests/test_b.py"], [MAC, M1], {"mac": Load(0.0), "m1": Load(0.0)},
                        tmp_path / "d.json", busy)
    assert plan.budgets == {"mac": 1, "m1": 8}
    by_name = {r.name: r for r in plan.runners}
    assert _free(by_name["mac"], plan) == 1 and _free(by_name["m1"], plan) == 8  # the plan's figure is the run's


def test_measured_shard_speed_balances_the_next_plan_within_20_percent(tmp_path):
    """Synthetic set: the Mac really gets 50% out of its xdist workers, the M1 80% at 0.6x per-core speed. The first
    plan (no measurement) misses; each shard's measured seconds go into the store; the next plan is within 20%."""
    import random

    from companion import runner_durations
    rng = random.Random(7)
    files = [f"tests/test_s{i:03d}.py" for i in range(300)]
    core = {p: rng.uniform(0.2, 6.0) for p in files}  # this Mac's core-seconds per file
    truth = {"mac": (1.0, 0.5), "m1": (0.6, 0.8)}  # (per-core speed, xdist efficiency)
    mac = Runner("mac", "macos", 18, 36, reach="local", may_see_real_data=True)
    m1 = Runner("m1", "macos", 8, 16, reach="ssh", host="runner@m1.example.invalid", speed=0.6)
    loads = {"mac": Load(3.0), "m1": Load(0.0)}
    store = tmp_path / "d.json"
    runner_durations.atomic_write(store, {"_schema": runner_durations.SCHEMA, "_runners": {}, **core})

    def walls(plan):
        out = {}
        for name, paths in plan.shards.items():
            speed, eff = truth[name]
            out[name] = sum(core[p] / speed for p in paths) / (plan.budgets[name] * eff)
        return out

    def spread(wall):
        return max(wall.values()) / min(wall.values())

    first = tp.plan_pool(files, [mac, m1], loads, store, _probes(load=3.0, cores=18))
    assert first.budgets == {"mac": 13, "m1": 8}  # mac: 15 free, capped at floor(16.2 - 3) = 13
    assert spread(walls(first)) > 1.2  # the miss rp7e saw
    for name, paths in first.shards.items():
        speed, _ = truth[name]
        tp.record_shard(store, name, sum(core[p] / speed for p in paths), len(paths), walls(first)[name],
                        first.budgets[name])
    second = tp.plan_pool(files, [mac, m1], loads, store, _probes(load=3.0, cores=18))
    assert spread(walls(second)) <= 1.2, walls(second)
    shards = json.loads(store.read_text())["_shards"]
    assert set(shards) == {"mac", "m1"} and shards["mac"]["seconds_per_file"] > 0
    assert runner_durations.load_store(store)  # the store still loads: _shards never makes it corrupt


class _Timed:
    """An inner transport for _Measured: every file passes with its stored junit seconds."""

    def __init__(self, seconds):
        self.seconds = seconds

    def run(self, attempt, env):
        return rx.ShardResult(0, {f: rx.FileResult("pass", self.seconds[f]) for f in attempt.paths})


def test_rp7f_measured_walls_balance_the_next_plan_within_20_percent(tmp_path):
    """rp7f (2026-10-16): both shards were planned at 84 s; the m1 took 292.0 s, the Mac 382.9 s (23.7% apart). Each
    file's junit seconds are what the store holds (its EMA came from the runner that ran it). Fed through the real
    _Measured + recorder path, the next plan's true walls are within 20% and its predicted ends match them.
    Red before rp7f r2: efficiency from junit seconds divided the m1's own seconds by its 0.6 speed a second time."""
    import random

    from companion import runner_durations, runner_pool
    rng = random.Random(11)
    files = [f"tests/test_s{i:03d}.py" for i in range(665)]
    seconds = {p: rng.uniform(0.2, 4.0) for p in files}
    mac = Runner("mac", "macos", 18, 36, reach="local", may_see_real_data=True)  # the live registry's figures
    m1 = Runner("m1", "macos", 8, 16, reach="ssh", host="runner@m1.example.invalid", speed=0.6)
    speeds = {"mac": 1.0, "m1": 0.6}
    loads = {"mac": Load(3.0), "m1": Load(0.0)}
    store = tmp_path / "d.json"
    runner_durations.atomic_write(store, {"_schema": runner_durations.SCHEMA, "_runners": {}, **seconds})

    def plan():
        return tp.plan_pool(files, [mac, m1], loads, store, _probes(load=3.0, cores=18))

    def raw(p, name):  # the unscaled prediction: planned core-seconds over the budget
        return sum(seconds[f] for f in p.shards[name]) / (speeds[name] * p.budgets[name])

    def spread(wall):
        return (max(wall.values()) - min(wall.values())) / max(wall.values())

    first = plan()
    rp7f = {"mac": 382.9, "m1": 292.0}
    ratio = {name: rp7f[name] / raw(first, name) for name in rp7f}  # measured / planned, per runner

    def truth(p):
        return {name: raw(p, name) * ratio[name] for name in p.shards}

    assert truth(first) == pytest.approx(rp7f) and spread(truth(first)) == pytest.approx(0.237, abs=0.001)
    durations = runner_pool.load_durations(store)
    for runner in first.runners:
        paths = tuple(first.shards[runner.name])
        clock = iter([0.0, rp7f[runner.name]])
        measured = tp._Measured(_Timed(seconds), first.budgets, durations, speeds, clock=lambda c=clock: next(c))
        measured.run(rx.Attempt(f"{runner.name}#1", runner, paths), {})
        tp.recorder(store, print, measured)({f: seconds[f] for f in paths}, runner.name)
    assert set(json.loads(store.read_text())["_shards"]) == {"mac", "m1"}  # both measurements went in
    second = plan()
    wall = truth(second)
    assert spread(wall) < 0.20, wall
    ends = runner_pool.predicted_end(second.shards, second.runners, runner_pool.load_durations(store), second.probe)
    assert ends == pytest.approx(wall, rel=0.01)  # the plan predicts the measured speed, not 84 s


def test_run_pool_end_to_end_saves_measured_shard_speed(tmp_path, monkeypatch):
    """rp7h (2026-10-16): after a pool phase the store had per-file seconds but no "_shards". Through run_pool itself
    (plan, execute, recorder) every accepted shard's speed must be in the store afterward, and the next plan uses it."""
    from companion import runner_durations
    monkeypatch.setattr(tp, "MIN_SHARD_SECONDS", 0.0)  # recorded wall comes from the clock below
    files = [f"tests/test_e{i:02d}.py" for i in range(20)]
    seconds = {p: 1.0 for p in files}
    store = tmp_path / "d.json"
    runner_durations.atomic_write(store, {"_schema": runner_durations.SCHEMA, "_runners": {}, **seconds})
    clock_lock = threading.Lock()
    ticks = {"now": 0.0}

    def clock():
        with clock_lock:
            ticks["now"] += 0.05
            return ticks["now"]

    real_measured = tp._Measured

    def measured(*args, **kwargs):
        kwargs.setdefault("clock", clock)
        return real_measured(*args, **kwargs)

    monkeypatch.setattr(tp, "_Measured", measured)
    lines = []
    ok = tp.run_pool(tmp_path, "HEAD", tmp_path, files, lines.append, runners=[MAC, M1], transport=_Timed(seconds),
                     probe=idle, store=store, headroom_probes=_probes())
    assert ok, lines
    data = json.loads(store.read_text())
    assert set(data.get("_shards", {})) == {"mac", "m1"}, (sorted(k for k in data if k.startswith("_")), lines)
    assert runner_durations.load_store(store)
    assert set(tp.shard_efficiency(store)) == {"mac", "m1"}  # the next plan_pool reads these


def test_wsl_workers_leave_headroom_for_inference_keeper():
    from companion.runner_pool import Load, Runner
    from companion.train_pool import worker_budget
    runner = Runner(name='pc', os='wsl', cores=32, ram_gb=96, reach='ssh', host='runner@example.invalid')
    assert worker_budget(runner, Load(load1=4)) == 8
    assert worker_budget(runner, Load(load1=29)) == 3
