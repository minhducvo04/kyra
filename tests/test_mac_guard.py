"""companion.mac_guard: the admission gate, the test guard and the pressure watchdog (owner, 2026-10-29, after a
watchdog panic). Probes and process tables are fakes; the one real subprocess is a synthetic memory hog the guard stops.
"""
import subprocess
import sys
import time

import pytest

from companion import headroom, mac_guard

PY = "/opt/homebrew/Cellar/python@3.12/3.12.15/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/" \
     "Contents/MacOS/Python"


REAL_PROBES = headroom.Probes


def probes(load=4.0, cores=18, free=60, pressure="1"):
    return REAL_PROBES(load=lambda: load, cores=lambda: cores,
                           memory=lambda: f"System-wide memory free percentage: {free}%\n",
                           pressure=lambda: pressure, vmstat=lambda: None, memsize=lambda: None,
                           battery=lambda: None, lid=lambda: None, thermal=lambda: None)


def proc(pid, ppid=1, rss_mb=10, age=60, command="/bin/zsh"):
    return mac_guard.Proc(pid, ppid, rss_mb, age, command)


# ---- admission ----------------------------------------------------------------------------------------------------

def test_the_gate_refuses_warn_or_critical_pressure_low_memory_and_load_over_ten(monkeypatch):
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    assert mac_guard.admit("worker", probes=probes(), data=None) is None
    assert mac_guard.admit("worker", probes=probes(load=10.0), data=None) is None  # the line itself
    assert "load average 10.1 is above 10 on 18 cores" == mac_guard.admit("worker", probes=probes(load=10.1), data=None)
    assert "memory pressure is warn" in mac_guard.admit("train", probes=probes(pressure="2"), data=None)
    assert "memory pressure is critical" in mac_guard.admit("test", probes=probes(pressure="4"), data=None)
    assert "free memory is 9%" in mac_guard.admit("worker", probes=probes(free=9), data=None)
    # never above headroom's 0.9 x cores: the 8-core M1 refuses over 7.2
    assert "above 7.2 on 8 cores" in mac_guard.admit("worker", probes=probes(load=8.0, cores=8), data=None)
    said = []
    mac_guard.admit("worker", probes=probes(pressure="2"), data=None, say=said.append)
    assert said == ["mac gate: worker deferred: memory pressure is warn (needs normal)"]


def test_train_smoke_and_test_admission_judge_memory_danger_not_load(monkeypatch):
    """the owner, 2026-10-30: the landing train's smoke and test phases refuse on memory danger (pressure warn or
    critical, unreadable probes, swap over 20 GiB), never on load; every other kind keeps the load line."""
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    gib, read_swap = 1024**3, mac_guard.swap_used_bytes
    monkeypatch.setattr(mac_guard, "swap_used_bytes", lambda: 8 * gib)
    for kind in sorted(mac_guard.TRAIN_KINDS):
        assert mac_guard.admit(kind, probes=probes(load=45.0, free=5), data=None) is None
        assert "memory pressure is warn" in mac_guard.admit(kind, probes=probes(load=1.0, pressure="2"), data=None)
        assert "memory pressure is critical" in mac_guard.admit(kind, probes=probes(pressure="4"), data=None)
        assert mac_guard.admit(kind, probes=probes(pressure=None), data=None) == "cannot read system memory pressure"
    assert "load average 45.0" in mac_guard.admit("worker", probes=probes(load=45.0), data=None)
    danger = mac_guard.memory_danger_refusal
    assert danger(probes(), swap=lambda: 20 * gib) is None  # the trigger itself
    assert "above 20 GiB" in danger(probes(), swap=lambda: 21 * gib)
    assert danger(probes(), swap=lambda: None) == "cannot read system swap"
    assert read_swap(lambda argv: "total = 9216.00M  used = 7967.50M  free = 1248.50M") == \
        int(7967.5 * 1024**2)
    assert read_swap(lambda argv: None) is None


def test_a_train_serial_preflight_ignores_load_but_not_memory_danger(monkeypatch):
    """KYRA_PREFLIGHT=1 and no xdist (the train's preflight and merged-tree guards): load 30 admits, warn refuses."""
    monkeypatch.setattr(mac_guard, "swap_used_bytes", lambda: 0)
    many = [f"tests/test_{i}.py" for i in range(8)]
    monkeypatch.setenv("KYRA_PREFLIGHT", "1")
    assert headroom.admit_pytest(None, many, None, probes=probes(load=30.0), sleep=pytest.fail) is None
    assert "memory pressure is warn" in headroom.admit_pytest(None, many, None, probes=probes(pressure="2"), wait=0)
    assert "load average 30.0" in headroom.admit_pytest(None, many, 4, probes=probes(load=30.0), wait=0)
    monkeypatch.delenv("KYRA_PREFLIGHT")
    assert "load average 30.0" in headroom.admit_pytest(None, many, None, probes=probes(load=30.0), wait=0)


def test_the_gate_cli_exits_75_under_simulated_warn_pressure(monkeypatch, capsys):
    monkeypatch.delenv("KYRA_MAC_GATE", raising=False)
    monkeypatch.setattr(headroom, "Probes", lambda: probes(pressure="2"))
    monkeypatch.setattr(headroom, "main_data_dir", lambda start=None: None)
    assert mac_guard.main(["admit", "--kind", "lane_send codex-exec"]) == 75
    assert "lane_send codex-exec deferred: memory pressure is warn" in capsys.readouterr().err
    monkeypatch.setattr(headroom, "Probes", lambda: probes())
    assert mac_guard.main(["admit"]) == 0


def test_the_gate_can_be_disabled_by_a_launcher_test_fixture(monkeypatch):
    monkeypatch.setenv("KYRA_MAC_GATE", "off")
    assert mac_guard.admit("worker", probes=probes(pressure="4"), data=None) is None


def test_a_focused_pytest_ignores_load_but_waits_out_memory_pressure_then_refuses():
    assert headroom.admit_pytest(None, ["tests/test_a.py"], None, probes=probes(load=30.0), sleep=pytest.fail) is None
    clock = [0.0]
    fake = probes(pressure="2")
    fake.clock = lambda: clock[0]
    refused = headroom.admit_pytest(None, ["tests/test_listening_vad.py"], None, probes=fake,
                                    sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert refused.startswith("no headroom for 60s: memory pressure is warn")


# ---- the process table ------------------------------------------------------------------------------------------------

def test_ps_rows_parse_with_elapsed_time_and_rss_in_mb():
    rows = mac_guard.parse_ps(f"  53589     1   40960 S+   30:05 {PY} -m pytest tests/test_listening_vad.py -x -q\n"
                              f"  53699 53589 5662310 T 1-02:29:59 {PY} -u -c import sys;exec(eval(sys.stdin.readline()))\n"
                              "garbage line\n")
    assert rows[53589].age == 1805 and rows[53589].rss_mb == 40 and not rows[53589].stopped
    assert rows[53699].age == 95399 and rows[53699].rss_mb == 5529 and rows[53699].ppid == 53589
    assert rows[53699].stopped  # paused by a --where local train
    assert mac_guard.descendants(rows, 53589) == [53589, 53699]


@pytest.mark.parametrize("command, kind", [
    (f"{PY} -m pytest tests/test_listening_vad.py -x -q", "pytest"),  # the 10-29 runaway's parent
    (f"{PY} -u -c import sys;exec(eval(sys.stdin.readline()))", "xdist-worker"),  # ... and its 5.4 GB worker
    ("/Users/x/kyra/.venv/bin/python3.12 -I -m pytest -n 4 tests", "pytest"),
    ("/Users/x/kyra/.venv/bin/python -I /Users/x/lanes/local-jobs/run_job.py /x/running/a.md", "job"),
    ("/Users/x/kyra/.venv/bin/python -m companion.local_agent /wt brief", "local-agent"),
    ("node /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex exec -C /wt brief", "worker"),
    ("/Users/x/.local/bin/claude -p --model sonnet", "worker"),
    ("/Users/x/Library/Caches/ms-playwright/chromium-1140/chrome-mac/Chromium.app/Contents/MacOS/Chromium --headless",
     "test-browser"),
    ("/Applications/Web Browser.app/Contents/MacOS/Web Browser", None),
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", None),
    ("/Applications/Claude.app/Contents/MacOS/Claude", None),
    ("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", None),
    ("/Users/x/kyra/.venv/bin/python -m uvicorn companion.webapp:app --port 8000", None),
    ("zsh /Users/x/kyra/data/team/lanes/lanes.sh", None),
    ("zsh -c echo pytest is mentioned here", None),
    ("/Users/x/.local/bin/claude --resume abc", None),  # an interactive session, not a worker
])
def test_only_agent_started_processes_are_candidates(command, kind):
    assert mac_guard.agent_kind(command) == kind


# ---- the test guard ---------------------------------------------------------------------------------------------------

def test_a_worker_over_three_gb_stops_its_own_tree_and_names_the_test():
    procs = {p.pid: p for p in [proc(100, 1, 80, command=f"{PY} -m pytest tests/test_listening_vad.py"),
                                proc(101, 100, 5500, command=f"{PY} -u -c import sys;exec(eval(sys.stdin.readline()))"),
                                proc(102, 100, 200, command="guard"), proc(103, 101, 20, command="helper"),
                                proc(104, 102, 5, command="ps")]}
    tests = {101: (0, "tests/test_listening_vad.py::test_vad_listener_no_speech_start")}
    pids, reason = mac_guard.test_verdict(procs, 100, tests, now=30, me=102)
    assert pids == [101, 103]
    procs[100].rss_mb = 4000  # the controller itself: its tree, never the guard or the guard's ps
    assert mac_guard.test_verdict(procs, 100, {}, now=30, me=102)[0] == [100, 101, 103]
    assert reason == ("pid 101 at 5500 MB is over the 3072 MB test ceiling "
                      "(test tests/test_listening_vad.py::test_vad_listener_no_speech_start)")


def test_a_test_over_ten_minutes_or_a_tree_over_its_ceiling_is_stopped():
    procs = {p.pid: p for p in [proc(100, 1, 80), proc(101, 100, 900), proc(102, 100, 900)]}
    assert mac_guard.test_verdict(procs, 100, {101: (0, "t::slow")}, now=600, me=0) is None
    pids, reason = mac_guard.test_verdict(procs, 100, {101: (0, "t::slow")}, now=601, me=0)
    assert pids == [101] and reason == "test t::slow has run 601s, over the 600s limit"
    pids, reason = mac_guard.test_verdict(procs, 100, {}, now=0, me=0, tree_mb=1500)
    assert pids == [100, 101, 102] and "1880 MB, over the 1500 MB ceiling" in reason


def test_after_stopping_a_worker_the_guard_watches_its_replacement(tmp_path):
    """xdist replaces a crashed worker and the run carries on (Codex pairing, 10-29): the next hog is stopped too."""
    looks = iter([{100: proc(100), 101: proc(101, 100, 5000)}, {100: proc(100), 102: proc(102, 100, 50)},
                  {100: proc(100), 102: proc(102, 100, 5000)}, {100: proc(100), 103: proc(103, 100, 50)}])
    stopped, lines = [], []
    mac_guard.heartbeat(tmp_path, "t::first", pid=101, clock=lambda: 0.0)
    line = mac_guard.guard_tests(100, read=lambda: next(looks, {1: proc(1)}), clock=lambda: 10.0, sleep=lambda s: None,
                                 stop=stopped.append, say=lambda text: lines.append(text) or text,
                                 directory=tmp_path, is_alive=lambda pid: True)
    assert stopped == [[101], [102]] and len(lines) == 2 and line == lines[-1]
    assert "pid 101 at 5000 MB" in lines[0] and "(test t::first)" in lines[0] and "pid 102 at 5000 MB" in lines[1]


def test_time_a_test_spends_paused_or_asleep_does_not_count(tmp_path):
    """A --where local train SIGSTOPs helper test runs (remote_test.paused_helpers) and the Mac sleeps with the lid
    closed; neither is a runaway. The outer guard skips stopped time and its own gaps; the self-guard skips its gaps."""
    now, states, stopped = [1000.0], iter([True] * 700 + [False] * 1000), []
    mac_guard.heartbeat(tmp_path, "t::paused", pid=101, clock=lambda: 1000.0)

    def read():
        return {100: proc(100), 101: mac_guard.Proc(101, 100, 50, 0, "w", stopped=next(states))}

    def sleep(seconds):
        now[0] += seconds
        if now[0] == 1300.0:
            now[0] += 3600  # the Mac slept an hour mid-run
        if len(stopped) == 0 and now[0] > 1000 + 700 + 3600 + 700:
            raise AssertionError("never stopped")

    line = mac_guard.guard_tests(100, read=read, clock=lambda: now[0], sleep=sleep, stop=stopped.append,
                                 say=lambda text: text, directory=tmp_path, seconds=600,
                                 is_alive=lambda pid: not stopped)
    assert stopped == [[101]] and "test t::paused has run 601s" in line
    assert now[0] - 1000 - 3600 >= 1290  # about 700 s stopped + 601 s running, never the hour asleep

    guard = mac_guard.SelfGuard(seconds=600, peak=lambda: 0, clock=lambda: 2000.0, say=lambda t: t, die=lambda: None)
    guard.test = (1000.0, "t::x")
    guard.resumed(500)
    assert guard.check() is None  # 1000 s of wall time, 500 of them stopped


def test_heartbeat_files_record_and_clear_the_running_test(tmp_path):
    mac_guard.heartbeat(tmp_path, "tests/test_a.py::test_x", pid=42, clock=lambda: 1000)
    assert mac_guard.running_tests(tmp_path) == {42: (1000.0, "tests/test_a.py::test_x")}
    mac_guard.heartbeat(tmp_path, None, pid=42)
    assert mac_guard.running_tests(tmp_path) == {}


def test_the_self_guard_kills_its_process_over_the_ceiling_or_the_time_limit():
    died, said = [], []
    guard = mac_guard.SelfGuard(rss_mb=3072, seconds=600, peak=lambda: 1000, clock=lambda: 1000.0,
                                say=lambda text: said.append(text) or text, die=lambda: died.append(1))
    assert guard.check() is None
    guard.test = (100.0, "tests/test_x.py::test_slow")
    assert "has run 900s, over the 600s limit (test tests/test_x.py::test_slow)" in guard.check() and died == [1]
    guard.test, guard.peak = None, lambda: 3100
    assert "reached 3100 MB, over the 3072 MB test ceiling" in guard.check() and died == [1, 1]


# A runaway test that limits itself (owner order, 2026-10-30, after an unguarded hog reached 171 GB and then 400 GB):
# it grows ~200 MB a second of written memory, stops growing at 2 GB and exits at 10 s even if no guard ever acts.
HOG = """
import resource, time
cap, deadline = 2 << 30, time.monotonic() + 10
try:
    resource.setrlimit(resource.RLIMIT_AS, (cap + (1 << 30), cap + (1 << 30)))
except (ValueError, OSError):
    pass  # macOS does not enforce RLIMIT_AS; the counter and the deadline are the real limits
blocks, held = [], 0
while time.monotonic() < deadline:
    if held + (50 << 20) <= cap:
        blocks.append(b"\\x01" * (50 << 20))  # written, so RSS grows (a zeroed bytearray stays unmapped)
        held += 50 << 20
    time.sleep(0.25)
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the macOS process table")
def test_replay_a_synthetic_memory_hog_is_stopped_at_the_ceiling_with_one_alert(tmp_path):
    """The proof replay: a fake runaway under a real guard with a 400 MB ceiling (the live one is 3 GB)."""
    if mac_guard.gate_off():
        pytest.skip("KYRA_MAC_GATE=off: the Mac guard is switched off here")
    if not mac_guard.read_procs():
        pytest.skip("mac_guard.read_procs() sees no processes here (ps denied in a sandbox): the guard would be blind")
    hog = subprocess.Popen([sys.executable, "-c", HOG])
    alerts = tmp_path / "ALERTS.md"
    started = time.monotonic()
    try:
        # me=0: in production the guard is its own process; here it runs in the hog's parent, which it must not skip
        line = mac_guard.guard_tests(hog.pid, directory=tmp_path / "guard", rss_mb=400, tree_mb=10_000, seconds=60,
                                     say=lambda text: mac_guard.alert(text, path=alerts), me=0)
        hog.wait(timeout=10)
    finally:
        if hog.poll() is None:
            hog.kill()
    assert hog.returncode in (-15, -9)
    assert time.monotonic() - started < 30
    written = alerts.read_text().splitlines()
    assert len(written) == 1 and written[0] == line
    assert f"[mac-guard] test guard stopped 1 process(es) (pytest pid {hog.pid}): pid {hog.pid} at " in line
    assert "over the 400 MB test ceiling" in line


# ---- the pressure watchdog --------------------------------------------------------------------------------------------

def table():
    return {p.pid: p for p in [
        proc(10, 1, 4000, 9000, "/Applications/Web Browser.app/Contents/MacOS/Web Browser"),
        proc(11, 1, 900, 50, "/Users/x/kyra/.venv/bin/python -m uvicorn companion.webapp:app"),
        proc(20, 1, 2500, 3600, f"{PY} -m pytest tests/test_big.py"),
        proc(30, 1, 60, 120, "zsh /Users/x/lanes/worker_pool.sh review-a sonnet out.md 10 prompt"),
        proc(31, 30, 700, 110, "/Users/x/.local/bin/claude -p --model sonnet"),
        proc(32, 31, 400, 100, f"{PY} -m pytest tests/test_a.py"),
        proc(40, 1, 200, 5, f"{PY} -m pytest tests/test_tiny.py"),  # newest, too small to matter
    ]}


def test_the_victim_is_the_newest_agent_heavy_process_never_owner_apps_or_the_web_ui():
    assert mac_guard.victim(table()).pid == 32
    without = {pid: p for pid, p in table().items() if pid not in (31, 32)}
    assert mac_guard.victim(without).pid == 20
    assert mac_guard.victim({10: table()[10], 11: table()[11]}) is None


def test_the_watchdog_stops_one_tree_only_after_sixty_seconds_of_critical():
    now, level, stopped, said = [0.0], ["critical"], [], []
    dog = mac_guard.Watchdog(level=lambda: level[0], read=table, stop=stopped.append,
                             say=lambda text: said.append(text) or text, clock=lambda: now[0], critical_seconds=60)
    for t in (0, 30, 55):
        now[0] = t
        assert dog.tick() is None
    level[0] = "warn"  # a dip resets the window
    now[0] = 58
    assert dog.tick() is None
    level[0] = "critical"
    for t in (60, 100):
        now[0] = t
        assert dog.tick() is None
    now[0] = 121
    line = dog.tick()
    assert stopped == [[32]] and "pressure watchdog stopped pid 32 (400 MB, 100s old, 1 process(es))" in line
    now[0] = 150
    assert dog.tick() is None  # the next stop waits a full window
    now[0] = 182
    dog.tick()
    assert len(stopped) == 2


def test_the_watchdog_stops_an_unguarded_runaway_test_at_any_pressure():
    """The night's worker: a lane worktree on an older base (no conftest guard), 5.4 GB, pressure only warn."""
    night = {**table(), 50: proc(50, 1, 90, 2000, f"{PY} -m pytest tests/test_listening_vad.py -x -q"),
             51: proc(51, 50, 5400, 1990, f"{PY} -u -c import sys;exec(eval(sys.stdin.readline()))")}
    stopped = []
    dog = mac_guard.Watchdog(level=lambda: "warn", read=lambda: night, stop=stopped.append, say=lambda t: t)
    line = dog.tick()
    assert stopped == [[51]] and "watchdog stopped pid 51 (5400 MB" in line and "over the 3686 MB ceiling" in line
    assert mac_guard.Watchdog(level=lambda: "warn", read=table, stop=stopped.append, say=lambda t: t).tick() is None
    assert stopped == [[51]]  # a browser at 4 GB and a 2.5 GB pytest are under no test ceiling here


def test_the_watchdog_dry_run_stops_nothing():
    stopped = []
    dog = mac_guard.Watchdog(level=lambda: "critical", read=table, stop=stopped.append, say=lambda t: t,
                             clock=lambda: 100.0, critical_seconds=0, dry_run=True)
    assert "would stop pid 32" in dog.tick() and stopped == []
