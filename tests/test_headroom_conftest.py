"""The conftest admission hook end to end (review o-rev66): a copy of tests/conftest.py in a scratch repo. A run with
xdist disabled still starts (the hook for -n auto is optional), travel mode refuses with exit 75 before any worker,
a light run starts at any load, and a checkout without data/ (the M1 shape) is never checked."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from companion import headroom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


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



def _repo(tmp_path, *, data, travel_on):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    shutil.copy2(ROOT / "tests/conftest.py", repo / "tests/conftest.py")
    (repo / "tests/test_ok.py").write_text("def test_ok():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if data:
        headroom.set_travel(repo / "data", travel_on, by="the owner", reason="probe66")
    return repo


def _pytest(repo, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "KYRA_ADMISSION_WAIT": "0", "PYTEST_ADDOPTS": ""}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("data", [True, False])
def test_defect_d_a_run_with_xdist_disabled_still_starts(tmp_path, data):
    """The browser phase (here and on the M1), the train's merged-tree precheck and perf_suite all run
    `pytest -p no:xdist`. With xdist not loaded, the conftest's pytest_xdist_auto_num_workers is an unknown hook."""
    repo = _repo(tmp_path, data=data, travel_on=False)
    done = _pytest(repo, "-p", "no:xdist", "tests/test_ok.py")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_xdist_sizing_hook_is_optional_and_auto_sizing_still_works(tmp_path):
    """Review o-rev66 D: the hook is declared optional, so -p no:xdist starts and -n auto still sizes the workers."""
    repo = _repo(tmp_path, data=False, travel_on=False)
    assert "@pytest.hookimpl(optionalhook=True)" in (repo / "tests/conftest.py").read_text()
    assert _pytest(repo, "-p", "no:xdist", "tests/test_ok.py").returncode == 0
    done = _pytest(repo, "-n", "auto", "tests/test_ok.py")
    assert done.returncode == 0, done.stdout + done.stderr


def test_conftest_hook_refuses_in_travel_mode_with_75_before_any_worker(tmp_path):
    repo = _repo(tmp_path, data=True, travel_on=True)
    for args in (["tests/test_ok.py"], ["-n", "2", "tests"]):
        done = _pytest(repo, *args)
        assert done.returncode == 75, done.stdout + done.stderr
        assert "kyra headroom: refused: travel mode is on (probe66, by the owner)" in done.stderr
        assert "created:" not in done.stdout + done.stderr  # the controller refused; no xdist worker started


def test_conftest_hook_admits_a_light_run_at_any_load_and_skips_a_checkout_without_data(tmp_path):
    repo = _repo(tmp_path, data=True, travel_on=False)
    done = _pytest(repo, "tests/test_ok.py")
    assert done.returncode == 0 and "1 passed" in done.stdout, done.stdout + done.stderr
    shutil.rmtree(repo / "data")  # the M1 shape: git, no data/ (any load, even -n with a 0 s wait)
    done = _pytest(repo, "-n", "2", "tests")
    assert done.returncode == 0 and "kyra headroom" not in done.stderr, done.stdout + done.stderr
