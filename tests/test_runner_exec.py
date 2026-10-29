"""RP5: the shard executor with fake transports (no ssh, no rsync, no writes outside tmp)."""

import threading
import time

import pytest

from companion import runner_exec as rx
from companion import runner_pool as rp

MAC = {"name": "mac", "os": "macos", "cores": 18, "ram_gb": 36, "browsers": ["chromium"],
       "reach": "local", "may_see_real_data": True}
M1 = {"name": "m1", "os": "macos", "cores": 8, "ram_gb": 16, "reach": "ssh",
      "host": "runner@m1.example.invalid", "key": "<user-home>/.ssh/k", "speed": 0.6}
WSL = {"name": "windows-wsl", "os": "linux-wsl2", "cores": 16, "ram_gb": 64, "reach": "ssh",
       "host": "runner@pc.example.invalid"}
RUNNERS = rp.parse_registry({"runners": [dict(MAC), dict(M1), dict(WSL)]})
DURATIONS = {f"tests/test_f{i:02d}.py": float(1 + i % 7) for i in range(30)}
JOBS = [rp.Job(p) for p in DURATIONS]


def idle(runner):
    return rp.Load(load1=0.0)


def result(paths, outcome="pass", code=0):
    return rx.ShardResult(code, {p: rx.FileResult(outcome, 1.5) for p in paths})


class Fake:
    """Per-runner behaviour ``(attempt, env, fake) -> ShardResult``; default: every file passes."""

    def __init__(self, **behaviours):
        self.behaviours = behaviours
        self.calls = []
        self.kills = []
        self.killed = {r.name: threading.Event() for r in RUNNERS}

    def run(self, attempt, env):
        self.calls.append((attempt.id, attempt.runner.name, attempt.paths, dict(env)))
        key = attempt.runner.name.replace("-", "_")
        return self.behaviours.get(key, lambda a, e, f: result(a.paths))(attempt, env, self)

    def kill(self, attempt):
        self.kills.append(attempt.id)
        self.killed[attempt.runner.name].set()


def plan_for(jobs=JOBS):
    return rp.shard(jobs, RUNNERS, DURATIONS, idle)


def run(fake, plan=None, jobs=JOBS, **kw):
    kw.setdefault("log", lambda line: None)
    return rx.execute(plan or plan_for(jobs), jobs, RUNNERS, fake, durations=DURATIONS, probe=idle, **kw)


def assert_full_set(report, jobs=JOBS):
    assert sorted(report.files) == sorted(j.path for j in jobs)
    assert all(outcome in rx.OUTCOMES for outcome, _ in report.files.values())


def test_happy_path_runs_shards_in_parallel_and_records_durations(tmp_path):
    plan = plan_for()
    assert len(plan) == 3
    barrier = threading.Barrier(3, timeout=5)

    def together(attempt, env, fake):
        barrier.wait()  # breaks (and the run fails) unless all three shards are running at once
        return result(attempt.paths)

    store = tmp_path / "test-durations.json"
    lines = []
    report = run(Fake(mac=together, m1=together, windows_wsl=together), plan,
                 recorder=rx.store_recorder(store), log=lines.append)
    assert report.verdict == "pass" and report.failed_runners == [] and report.discarded == []
    assert_full_set(report)
    assert {report.files[p][1] for p in plan["m1"]} == {"m1"}
    assert len(report.timings) == 3 and all(t.startswith("runner-pool: ") for t in report.timings)
    assert lines == report.timings
    assert rp.load_durations(store) == {p: 1.5 for p in DURATIONS}


def test_a_failed_file_makes_the_verdict_fail():
    fake = Fake(windows_wsl=lambda a, e, f: rx.ShardResult(1, {p: rx.FileResult("fail" if i == 0 else "pass")
                                                               for i, p in enumerate(a.paths)}))
    report = run(fake)
    assert report.verdict == "fail" and report.failed_runners == []
    assert_full_set(report)


@pytest.mark.parametrize("how", [73, 75, 124, "raise"])
def test_runner_dies_mid_shard_is_reassigned_and_counted_once(how):
    plan = plan_for()
    m1_files = set(plan["m1"])

    def dies(attempt, env, fake):
        if how == "raise":
            raise ConnectionError("ssh dropped")
        half = attempt.paths[: len(attempt.paths) // 2]
        return result(half, code=how)  # partial results that must be thrown away

    fake = Fake(m1=dies)
    report = run(fake, plan)
    assert report.failed_runners == ["m1"] and report.verdict == "pass"
    assert_full_set(report)
    assert all(report.files[p][1] != "m1" for p in m1_files)
    ran = [p for _, name, paths, _ in fake.calls if name != "m1" for p in paths]
    assert sorted(ran) == sorted(DURATIONS)  # each file ran exactly once on a live runner
    assert fake.kills == [c[0] for c in fake.calls if c[1] == "m1"]


def test_runner_with_an_accepted_shard_that_then_fails_keeps_only_its_accepted_files():
    plan = plan_for()
    first = threading.Event()

    def m1(attempt, env, fake):
        first.wait(5)  # fail only after windows-wsl's first shard is accepted
        time.sleep(0.05)
        return result((), code=75)

    def wsl(attempt, env, fake):
        if not first.is_set():
            first.set()
            return result(attempt.paths)
        return result((), code=75)  # its second shard (moved m1 files) dies

    fake = Fake(m1=m1, windows_wsl=wsl)
    report = run(fake, plan)
    assert [c[1] for c in fake.calls].count("windows-wsl") == 2  # it got moved m1 files, then died
    assert report.failed_runners == ["m1", "windows-wsl"]
    assert_full_set(report)
    assert {report.files[p][1] for p in plan["windows-wsl"]} == {"windows-wsl"}


def test_late_results_from_a_dead_runner_are_discarded():
    plan = plan_for()
    m1_files = set(plan["m1"])
    returned = threading.Event()

    def hangs(attempt, env, fake):
        fake.killed["m1"].wait(5)  # stuck until the executor kills it at the deadline
        returned.set()
        return result(attempt.paths, "pass")  # late, and different from the real outcome below

    def other(attempt, env, fake):
        if set(attempt.paths) <= m1_files:  # the re-placed m1 shard
            returned.wait(5)
            time.sleep(0.2)  # let the dead attempt's late result reach the executor first
            return result(attempt.paths, "fail", code=1)
        return result(attempt.paths)

    fake = Fake(m1=hangs, mac=other, windows_wsl=other)
    report = run(fake, plan, deadline=0.5)
    assert report.failed_runners == ["m1"] and "m1#" in report.discarded[0]
    assert any("late" in t for t in report.timings)
    assert_full_set(report)
    assert all(report.files[p][0] == "fail" and report.files[p][1] != "m1" for p in m1_files)
    assert report.verdict == "fail"


@pytest.mark.parametrize("path", ["data/team/x.py", "./data/private_docs/a.py", ".env", "tests/../.env",
                                  "/tmp/example/kyra/tests/test_a.py", "tests/.env.local"])
def test_forbidden_paths_are_never_sent(path):
    fake = Fake()
    with pytest.raises(rx.TransportRefused):
        run(fake, {"mac": [path]}, jobs=[rp.Job(path)])
    assert fake.calls == []


@pytest.mark.parametrize("env", [{"KYRA_DATA_DIR": "/tmp/whatever"},
                                 {"PYTEST_ADDOPTS": f"--basetemp={rx._REPO}/data/t"},  # names the real dir, reads nothing
                                 {"X": "/tmp/example/kyra/.env"}])
def test_forbidden_env_is_refused_for_a_remote_runner(env):
    fake = Fake()
    with pytest.raises(rx.TransportRefused):
        run(fake, {"m1": ["tests/test_f00.py"]}, jobs=[rp.Job("tests/test_f00.py")], env=env)
    assert fake.calls == []


def test_local_runner_may_get_kyra_data_dir():
    fake = Fake()
    report = run(fake, {"mac": ["tests/test_f00.py"]}, jobs=[rp.Job("tests/test_f00.py")],
                 env={"KYRA_DATA_DIR": "/tmp/kyra-test"})
    assert report.verdict == "pass" and fake.calls[0][3] == {"KYRA_DATA_DIR": "/tmp/kyra-test"}


def test_real_data_test_never_goes_remote():
    guard = "tests/test_no_personal_files.py"
    fake = Fake()
    with pytest.raises(rx.TransportRefused):  # a forged plan is caught at dispatch
        run(fake, {"m1": [guard]}, jobs=[rp.Job(guard)])
    with pytest.raises(rx.TransportRefused):
        run(fake, {"windows-wsl": ["tests/test_f01.py"]}, jobs=[rp.Job("tests/test_f01.py", real_data=True)])
    assert fake.calls == []

    jobs = JOBS + [rp.Job(guard)]
    plan = rp.shard(jobs, RUNNERS, DURATIONS, idle)
    assert guard in plan["mac"]
    dead_mac = Fake(mac=lambda a, e, f: result((), code=75))
    with pytest.raises(rp.NoRunner):  # the Mac died: the guard has nowhere to go, it is not sent remote
        run(dead_mac, plan, jobs=jobs)
    assert all(guard not in paths for _, name, paths, _ in dead_mac.calls if name != "mac")


def test_shard_reporting_a_file_it_was_not_given_or_missing_one_is_refused():
    with pytest.raises(rx.AccountingError):
        run(Fake(m1=lambda a, e, f: result(a.paths + ("tests/test_extra.py",))))
    with pytest.raises(rx.AccountingError):
        run(Fake(m1=lambda a, e, f: result(a.paths[1:])))


def test_plan_that_misses_a_job_is_refused():
    plan = plan_for()
    plan["m1"] = plan["m1"][1:]
    with pytest.raises(rx.AccountingError):
        run(Fake(), plan)


def test_account_rejects_gaps_and_doubles():
    a = rx.Attempt("mac#1", RUNNERS[0], ("tests/a.py",))
    b = rx.Attempt("m1#2", RUNNERS[1], ("tests/a.py",))
    with pytest.raises(rx.AccountingError):
        rx.account(["tests/a.py", "tests/b.py"], [(a, result(a.paths))])
    with pytest.raises(rx.AccountingError):
        rx.account(["tests/a.py"], [(a, result(a.paths)), (b, result(b.paths))])
    assert rx.account(["tests/a.py"], [(a, result(a.paths))]) == {"tests/a.py": ("pass", "mac")}


# ---- mutation checks: break a rule and show the tests above catch it -----------------------------------

def _lenient_account(planned, accepted):
    out = {}  # first result wins, no checks
    for a, res in accepted:
        for p, r in res.files.items():
            out.setdefault(p, (r.outcome, a.runner.name))
    return out


def test_mutation_dropping_the_accounting_lets_a_missing_file_pass(monkeypatch):
    drops = Fake(m1=lambda a, e, f: result(a.paths[1:]))
    monkeypatch.setattr(rx, "_check_shard", lambda attempt, res: None)
    with pytest.raises(rx.AccountingError):  # the final accounting alone still catches the gap
        run(drops)
    monkeypatch.setattr(rx, "account", _lenient_account)
    report = run(Fake(m1=lambda a, e, f: result(a.paths[1:])))
    with pytest.raises(AssertionError):  # without accounting a file goes missing yet the run "passes"
        assert_full_set(report)
    assert report.verdict == "pass"


def test_mutation_counting_late_results_is_caught(monkeypatch):
    monkeypatch.setattr(rx, "_is_late", lambda attempt, dead: False)
    with pytest.raises(rx.AccountingError):  # counted twice: the accounting refuses
        test_late_results_from_a_dead_runner_are_discarded()
    monkeypatch.setattr(rx, "account", _lenient_account)
    with pytest.raises(AssertionError):  # with both rules gone, the dead runner's stale "pass" wins
        test_late_results_from_a_dead_runner_are_discarded()


def test_mutation_keeping_partial_results_is_caught(monkeypatch):
    monkeypatch.setattr(rx, "_accepts", lambda attempt, res: True)
    with pytest.raises((rx.AccountingError, AssertionError)):
        test_runner_dies_mid_shard_is_reassigned_and_counted_once(75)
