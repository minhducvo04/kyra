"""companion.resource_cap: hard caps that work with no watchdog and no ps.

The proof the owner asked for after the 2026-10-30 hog incident:
these tests run inside the agent wall, where /bin/ps is denied, and the
parent-side monitor still stops a synthetic hog at a 64 MB cap. All caps
here are small (64 MB, seconds); the hog also stops itself after 10 s.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from companion import resource_cap

# Allocates (and touches, so the pages count) 20 MB per 0.05 s.
HOG = ("import time\n"
       "blocks = []\n"
       "start = time.monotonic()\n"
       "while time.monotonic() - start < 10:  # its own deadline, capped or not\n"
       "    block = bytearray(20 * 1024 * 1024)\n"
       "    for i in range(0, len(block), 4096):\n"
       "        block[i] = 1\n"
       "    blocks.append(block)\n"
       "    time.sleep(0.05)\n")


def test_a_synthetic_hog_dies_at_a_64mb_cap_within_3s():
    started = time.monotonic()
    returncode, reason = resource_cap.run_capped([sys.executable, "-c", HOG], memory_mb=64, seconds=3)
    assert reason == "memory cap"
    assert returncode in (-15, -9)
    assert time.monotonic() - started < 3


def test_a_sleeper_dies_at_a_1s_time_cap():
    started = time.monotonic()
    returncode, reason = resource_cap.run_capped(
        [sys.executable, "-c", "import time; time.sleep(30)"], memory_mb=512, seconds=1)
    assert reason == "time cap"
    assert returncode in (-15, -9)
    assert time.monotonic() - started < 5


def test_an_ok_command_returns_0_ok():
    returncode, reason = resource_cap.run_capped([sys.executable, "-c", "pass"], memory_mb=64, seconds=3)
    assert (returncode, reason) == (0, "ok")


def test_cli_runs_a_command_under_the_caps():
    done = subprocess.run(
        [sys.executable, "-m", "companion.resource_cap", "--memory-mb", "512", "--seconds", "5",
         "--", sys.executable, "-c", "pass"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert done.returncode == 0, done.stderr


def test_fasttrack_local_batches_run_through_run_capped(tmp_path):
    """Item 4 wiring: a local batch with caps is enforced by run_capped itself,
    never by the injected subprocess runner."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "test_fasttrack_script_rc", Path(__file__).resolve().parents[1] / "scripts" / "test_fasttrack.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    def _never(argv):
        raise AssertionError(f"capped local batch must not reach the injected runner: {argv!r}")

    command = [sys.executable, "-c", HOG, f"--junitxml={tmp_path / 'runner-here.xml'}"]
    results = script.run_plan({"runner-here": ["tests/test_a.py"]}, {"runner-here": [command]},
                              ref="abc1234def", health_dir=str(tmp_path / "health"), run=_never,
                              caps={"runner-here": {"memory_mb": 64, "seconds": 3, "workers": 1}})
    assert results[0].cap_reason == "memory cap"
    assert results[0].failed == 1  # A2-FIX2: a killed batch is a failed batch, not 0/0/0
    assert results[0].error == "resource cap: memory cap"
    assert not results[0].green


def test_caps_for_an_8gb_8core_machine_gives_2_workers():
    caps = resource_cap.caps_for({"ram_gb": 8, "cores": 8})
    assert caps["workers"] == 2  # max(1, min(8, 8 // 3))
    assert caps["memory_mb"] == int(8 * 1024 * 0.6 / 2)
    assert caps["seconds"] == resource_cap.DEFAULT_SECONDS
    overridden = resource_cap.caps_for({"ram_gb": 8, "cores": 8}, workers=4, memory_mb=999, seconds=7)
    assert overridden == {"memory_mb": 999, "seconds": 7, "workers": 4}


def test_batch_caps_for_is_workers_times_the_per_process_cap():
    """A2-FIX2: a tree cap (controller + workers, what run_capped measures)
    is the total budget. The per-process share alone (2764 MB at 8 workers
    on 36 GB) killed the first real Mac fast-track batch."""
    per = resource_cap.caps_for({"ram_gb": 36, "cores": 12}, workers=8)
    batch = resource_cap.batch_caps_for({"ram_gb": 36, "cores": 12}, workers=8)
    assert per["memory_mb"] == int(36 * 1024 * 0.6 / 8)
    assert batch == {"memory_mb": 8 * per["memory_mb"], "seconds": per["seconds"], "workers": 8}
    overridden = resource_cap.batch_caps_for({"ram_gb": 8, "cores": 8}, workers=4, memory_mb=999, seconds=7)
    assert overridden == {"memory_mb": 4 * 999, "seconds": 7, "workers": 4}
