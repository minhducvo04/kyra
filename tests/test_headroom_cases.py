"""Headroom and travel mode, the cases from review o-rev66 (2026-10-15): browser files are never light, low_power's
memory check is this Mac only, the train's short-timeout callers wait less than their timeout, this Mac is detected by
data/ beside the git dir, travel mode fails closed, the worker cap never drops below one, and a pytest exit 75 is a
failure, never "runner unreachable". Fake probes and tmp roots only."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from companion import headroom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import low_power  # noqa: E402


def _probes(**kw):
    base = dict(load=lambda: 4.0, cores=lambda: 18, memory=lambda: "System-wide memory free percentage: 60%\n",
                pressure=lambda: "1", battery=lambda: "Now drawing from 'AC Power'\n",
                lid=lambda: '"AppleClamshellState" = No', thermal=lambda: "", clock=lambda: 1000.0)
    base.update(kw)
    return headroom.Probes(**base)


def _module(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def test_defect_a_browser_file_is_never_a_light_run():
    """headroom's docstring lists browser tests as heavy, but is_light admits up to five browser files with no load
    or memory check: a scoped local browser fallback (KYRA_BROWSER_TESTS) or a builder's browser file starts Chromium
    beside a desktop browser at any load or memory pressure."""
    assert headroom.is_light(["tests/test_home_browser.py"], None) is False
    assert headroom.is_light(["tests/test_a.py", "tests/test_studio_browser.py::test_x"], None) is False


def test_defect_b_low_power_memory_check_is_this_mac_only(monkeypatch):
    """_headroom_extra says 'this Mac only (no data/ dir means not this Mac)', but memory_refusal runs everywhere:
    on the M1 runner (no data/) low_power refuses on memory, and test_low_power's real-run test does not skip on it."""
    monkeypatch.setattr(headroom, "main_data_dir", lambda start=None: None)
    warn = _probes(pressure=lambda: "2")
    monkeypatch.setattr(headroom, "Probes", lambda: warn)
    assert low_power._headroom_extra() == []


def test_this_mac_is_detected_by_data_next_to_the_git_dir_and_the_m1_run_dir_has_git_but_no_data(tmp_path):
    run = tmp_path / "runs" / "abc"  # remote_test's setup does `git init` in <user-home>/kyra-runner/runs/<sha>
    run.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(run)], check=True)
    assert headroom.main_data_dir(run) is None
    (run / "data").mkdir()  # would only exist if data/ were tracked or a test wrote it there
    assert headroom.main_data_dir(run) == run / "data"
    assert headroom.main_data_dir(tmp_path / "runs") is None  # no git at all


def test_travel_file_missing_is_off_and_anything_unreadable_is_on(tmp_path):
    assert headroom.travel_state(tmp_path) == {"on": False}
    path = headroom.travel_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("")
    assert headroom.travel_state(tmp_path)["on"] is True
    path.write_text('{"on": "false"}')
    assert headroom.travel_state(tmp_path)["on"] is True
    path.unlink()
    path.mkdir()
    assert headroom.travel_state(tmp_path)["on"] is True


def test_failed_power_probes_never_turn_travel_on_and_never_turn_auto_travel_off(tmp_path):
    dead = _probes(battery=lambda: None, lid=lambda: None, thermal=lambda: None)
    assert headroom.auto_travel(tmp_path, dead) == {"on": False}
    assert not headroom.travel_path(tmp_path).exists()
    headroom.set_travel(tmp_path, True, by="auto", reason="on battery with the lid closed")
    assert headroom.auto_travel(tmp_path, dead)["on"] is True


def test_worker_cap_never_drops_below_one():
    for cores, load, asked in ((1, 0.0, 4), (1, 0.9, 1), (18, 16.2, 30), (18, 0.0, 0), (8, 100.0, 8)):
        assert headroom.worker_cap(_probes(cores=lambda c=cores: c, load=lambda v=load: v), asked) >= 1


def test_a_remote_pytest_exit_75_is_a_failure_not_unreachable():
    remote_test = _module("rt_probe66", "scripts/remote_test.py")
    code, note = remote_test.outcome(0, "kyra headroom: refused: ...\nKYRA_PYTEST_EXIT=75\n")
    assert code == 1 and "not read as unreachable" in note


def test_a_local_headroom_refusal_fails_the_landing_and_is_not_a_second_fallback(tmp_path, monkeypatch):
    deploy = _module("dl_probe66", "scripts/deploy_local.py")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample_browser.py").touch()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/remote_test.py").touch()
    monkeypatch.setenv("KYRA_REMOTE_TESTS", "1")
    calls = []

    def runner(argv, cwd):
        calls.append(argv)
        if argv[1].endswith("remote_test.py"):
            return subprocess.CompletedProcess(argv, 75, "", "remote_test: runner is not configured")
        return subprocess.CompletedProcess(argv, 75, "", "kyra headroom: refused: no headroom for 900s")

    with pytest.raises(deploy.Refused, match="pytest exited 75"):
        deploy._pytest_phases(runner, "python", tmp_path, "abc")
    assert sum(argv[1].endswith("remote_test.py") for argv in calls) == 1  # one remote try, no re-dispatch


def test_the_train_s_short_timeout_callers_wait_less_than_their_timeout(monkeypatch):
    """Review o-rev66 C: train.preflight (timeout 60) and the merged-tree precheck (timeout 90) run heavy pytest; the
    admission wait must end before the caller's timeout so a refusal is logged as exit 75, not a killed test."""
    train = _module("train_hr_cases", "scripts/train.py")
    assert train.PREFLIGHT_ADMISSION_WAIT < 60 and train.PRECHECK_ADMISSION_WAIT < 90
    env = train._merged_tree_runner_env(ROOT, ROOT, ("free", "ssh exit 0"))
    assert env["KYRA_ADMISSION_WAIT"] == str(train.PRECHECK_ADMISSION_WAIT)
    import io
    seen = {}

    def runner(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 75, "", "kyra headroom: refused: load average 30.0 is above 16.2")

    log = io.StringIO()
    monkeypatch.setattr(train, "_runner_state", lambda main: ("unreachable", "fake: no M1 probe here"))
    assert train.preflight(ROOT, log, runner)
    assert seen["KYRA_ADMISSION_WAIT"] == str(train.PREFLIGHT_ADMISSION_WAIT)
    assert "preflight: headroom refused (exit 75)" in log.getvalue()


def test_no_readable_memory_signal_fails_fast_not_after_the_long_wait():
    """A sandbox that blocks sysctl, memory_pressure and vm_stat must not hang a caller past its time limit."""
    clock = iter(range(1000, 100000, 15))
    blind = _probes(pressure=lambda: None, memory=lambda: None, vmstat=lambda: None, memsize=lambda: None,
                    clock=lambda: float(next(clock)))
    refused = headroom.admit_pytest(None, ["tests"], 2, probes=blind, sleep=lambda s: None, wait=900)
    assert refused and "cannot read the free memory percentage" in refused
    assert f"{headroom.UNREADABLE_WAIT}s" in refused


def test_unreadable_pressure_level_admits_on_the_vm_stat_fallback():
    vmstat = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100000.\nPages inactive: 100000.\n"
    ok = _probes(pressure=lambda: None, memory=lambda: None, vmstat=lambda: vmstat,
                 memsize=lambda: str(16384 * 1000000))
    assert headroom.admit_pytest(None, ["tests"], 2, probes=ok, sleep=lambda s: pytest.fail("no wait"), wait=900) is None


def test_a_walled_worker_denied_data_reads_as_off_this_mac(tmp_path, monkeypatch):
    """Medic 2026-10-17: the muse sandbox denies a stat of data/, and the PermissionError crashed every pytest run."""
    run = tmp_path / "run"
    (run / "data").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(run)], check=True)
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: (_ for _ in ()).throw(PermissionError(1, "denied"))
                        if self.name == "data" else real(self))
    assert headroom.main_data_dir(run) is None
