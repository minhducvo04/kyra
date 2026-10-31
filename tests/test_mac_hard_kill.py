"""scripts/mac_hard_kill.py: the footprint kill switch (owner order, 2026-10-30). Fake process tables for the rules; the
two real runs use a synthetic hog that limits itself (3 GB, 20 s) and a killer scoped to that one pid, so neither a
failed killer nor a lowered threshold can touch anything else on the Mac.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mac_hard_kill as hk  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
GB, MB, ME = hk.GB, 1024**2, os.getuid()

# Grows `step` MB of touched memory every `pause` s, stops growing at `cap` MB and exits at `seconds`, killer or not.
HOG = """
import resource, sys, time
cap, seconds, step, pause = (int(sys.argv[1]) << 20), float(sys.argv[2]), (int(sys.argv[3]) << 20), float(sys.argv[4])
deadline = time.monotonic() + seconds
try:
    resource.setrlimit(resource.RLIMIT_AS, (cap + (1 << 30), cap + (1 << 30)))
except (ValueError, OSError):
    pass  # macOS does not enforce RLIMIT_AS; the counter and the deadline below are the real limits
blocks, held = [], 0
while time.monotonic() < deadline:
    if held + step <= cap:
        blocks.append(b"\\x01" * step)  # written, so it counts in the footprint (a zeroed bytearray would not)
        held += step
    time.sleep(pause)
"""


def hog(cap_mb=3072, seconds=20, step_mb=100, pause=0.5):
    return subprocess.Popen([sys.executable, "-c", HOG, str(cap_mb), str(seconds), str(step_mb), str(pause)])


def until_dead(proc, killer, limit=15):
    started = time.monotonic()
    lines = []
    while proc.poll() is None and time.monotonic() - started < limit:
        lines += killer.tick()
        time.sleep(0.5)
    return lines, time.monotonic() - started


def need_libproc():
    if hk.footprint(os.getpid()) is None or hk.info(os.getpid()) is None:
        pytest.skip("libproc cannot read this process here (sandboxed run); the switch runs outside any sandbox")


# ---- the rules on a fake table ----------------------------------------------------------------------------------------

def test_only_window_server_kernel_task_and_other_users_are_exempt():
    assert hk.exempt(0, "launchd", me=501) and hk.exempt(88, "anything", me=501)
    assert hk.exempt(501, "WindowServer", me=501) and hk.exempt(501, "kernel_task", me=501)
    for name in ("Python", "python3.12", "claude", "Claude", "Claude Helper (Renderer)", "node", "Web Browser"):
        assert not hk.exempt(501, name, me=501), name


def test_the_ceiling_and_the_growth_rule_on_a_fake_table():
    now, killed = [0.0], []
    sizes = {10: 50 * GB, 11: 50 * GB, 12: 13 * GB, 13: 5 * GB, 14: 1 * GB, 15: 4 * GB}
    table = {10: (0, 1, "kernel_task", 1), 11: (88, 1, "WindowServer", 1), 12: (501, 1, "Python", 1),
             13: (501, 1, "claude", 1), 14: (501, 1, "node", 1), 15: (501, 1, "Web Browser", 1)}
    killer = hk.Killer(pids=lambda: list(table), read=table.get, measure=sizes.get, clock=lambda: now[0],
                       kill=lambda pid: killed.append(pid) or "SIGTERM", me=501)
    lines = killer.tick()
    assert killed == [12] and "pid 12 (SIGTERM): footprint 13.0 GB is over the 12.0 GB ceiling" in lines[0]
    del table[12]
    for t in (2, 4, 6, 8, 10):  # 13 holds at 5 GB, 14 grows fast under 4 GB, 15 grows 0.4 GB per 10 s
        now[0] = t
        sizes[14] += GB // 2
        sizes[15] += int(0.08 * GB)
        assert killer.tick() == []
    sizes[13] += int(1.2 * GB)  # over 4 GB and 1.2 GB in the last 10 s: the runaway
    now[0] = 12
    lines = killer.tick()
    assert killed == [12, 13] and "pid 13" in lines[0] and "grew 1.2 GB in 10s" in lines[0]
    assert not any(key[0] == 13 for key in killer.history)


def test_a_reused_pid_starts_a_fresh_history():
    now, sizes, start = [0.0], {7: 4.5 * GB}, [1]
    killer = hk.Killer(pids=lambda: [7], read=lambda pid: (501, 1, "x", start[0]), measure=sizes.get,
                       clock=lambda: now[0], kill=pytest.fail, me=501)
    killer.tick()
    now[0], start[0], sizes[7] = 4, 2, 6 * GB  # a different process with the same pid
    assert killer.tick() == [] and list(killer.history) == [(7, 2)]


def test_the_parent_chain_and_the_log_lines(tmp_path):
    table = {300: (501, 200, "Python", 1), 200: (501, 1, "zsh", 1), 1: (0, 0, "launchd", 1)}
    assert hk.parent_chain(300, read=table.get) == "300 Python <- 200 zsh <- 1 launchd"
    assert hk.parent_chain(300, read={300: (501, 99, "Python", 1)}.get) == "300 Python <- 99 ?"
    hk.logger(tmp_path)("killed pid 300 (SIGKILL): footprint 13.0 GB is over the 12.0 GB ceiling")
    assert "killed pid 300 (SIGKILL)" in (tmp_path / "team/mac-hard-kill.log").read_text()
    inbox = (tmp_path / "team/inbox.md").read_text().splitlines()
    assert len(inbox) == 1 and inbox[0].startswith("- 20") and " mac-hard-kill: killed pid 300" in inbox[0]


# ---- real readings and real hogs --------------------------------------------------------------------------------------

def test_the_footprint_reading_counts_written_memory():
    need_libproc()
    uid, ppid, name, _start = hk.info(os.getpid())
    assert uid == ME and ppid == os.getppid() and name
    before = hk.footprint(os.getpid())
    block = b"\x01" * (200 * MB)
    after = hk.footprint(os.getpid())
    assert after - before >= 150 * MB, (before, after)
    del block
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert hk.footprint(gone.pid) is None and hk.info(gone.pid) is None
    assert os.getpid() in hk.all_pids()


@pytest.mark.parametrize("limits, rule", [
    (dict(hard=1 * GB, floor=100 * GB), "over the 1.0 GB ceiling"),  # the ceiling, lowered under the hog's 3 GB cap
    (dict(hard=100 * GB, floor=400 * MB), "(limit 1.0 GB per 10s)"),  # the real growth rate, a lowered floor
])
def test_a_synthetic_hog_is_killed_within_fifteen_seconds(tmp_path, limits, rule):
    need_libproc()
    child = hog()  # 200 MB/s of written memory, at most 3 GB and 20 s
    try:
        killer = hk.Killer(pids=lambda: [child.pid], log=hk.logger(tmp_path), me=ME, **limits)
        lines, took = until_dead(child, killer)
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert child.returncode == -15 and "(SIGTERM)" in lines[0], (child.returncode, lines)
    assert took < 15 and len(lines) == 1 and f"killed pid {child.pid}" in lines[0] and rule in lines[0]
    assert "command: " in lines[0] and f"parents: {child.pid} " in lines[0]
    assert f"killed pid {child.pid}" in (tmp_path / "team/mac-hard-kill.log").read_text()
