"""Hard limits that keep the agent fleet from starving the host Mac (2026-10-29, after a watchdog-timeout kernel panic).

That night a single-file pytest (tests/test_listening_vad.py on a voice lane branch) looped forever on mocked silence and
grew one xdist worker to 5.4 GB for 30+ minutes, twice, while lane workers, local packages and two browsers ran; memory
pressure sat at warn for hours and nothing could stop the test. Three limits, in code, no AI anywhere:

1. admit(): the one admission answer for every launcher on this Mac (lane_send, worker_pool.sh, the hardware keeper's
   run_job.py starts, the landing runner's local placement). Travel mode, memory pressure warn or critical, free memory
   under 10%, or a 1-minute load over ADMIT_LOAD (10 on 18 cores) refuses; the caller queues and retries (exit 75).
   Exception (owner, 2026-10-30): the landing train's smoke and test admission (TRAIN_KINDS) judges real memory
   danger only: travel mode, pressure warn or critical (or unreadable), or swap over SWAP_LIMIT_BYTES (20 GiB, the
   brain-safety trigger). Load no longer refuses them; the running-test ceilings below still apply.
2. The test guard API (automatic pytest hooks await a separate protected-file landing). The hooks start
   `python -m companion.mac_guard guard-tests <pytest pid>` beside every pytest
   controller on this Mac. Each second it reads the process table: one process of the run over TEST_RSS_MB (3 GB), the
   run's tree over TREE_RSS_MB, or one test running over TEST_SECONDS (10 min, from the heartbeat files the conftest
   writes per test) gets that process tree stopped (TERM, then KILL) and one ALERTS line.
3. The pressure watchdog, `python -m companion.mac_guard watchdog`: every 5 s it reads the kernel's pressure level; when
   it has been critical for CRITICAL_SECONDS (60 s) it stops the newest agent-started heavy process tree (pytest runs,
   local jobs, worker CLIs, test browsers) and logs it. Never the owner's apps (anything under /Applications except the Codex
   CLI's `exec` workers), never the web UI, never the loops (lanes, hierarchy, inspector, hardware_keeper).
   At any pressure, each tick also stops a pytest or xdist worker over STRAY_TEST_MB (1.2 x TEST_RSS_MB): a run with no
   test guard (a lane worktree on a base older than this guard, --noconftest) cannot grow without bound either. The
   night's 5.4 GB worker ran in such a worktree, at pressure warn, where the critical rule alone would not have acted.

Every knob is an environment variable (KYRA_ADMIT_LOAD, KYRA_TEST_RSS_MB, KYRA_TEST_TREE_MB, KYRA_TEST_SECONDS,
KYRA_PRESSURE_CRITICAL_S, KYRA_TEST_GUARD_DEADLINE_S); KYRA_MAC_GATE=off turns the admission gate off. Launcher test
fixtures set it locally, so those cases do not depend on how busy the machine running them is.

Fail closed when blind (2026-10-30, after two test hogs froze this Mac from inside agent walls where `ps` was
denied): read_procs() returns None when the process table cannot be read, admission then refuses heavy work, and
guard_tests stops the guarded process group instead of waiting forever. guard_tests also has its own monotonic
deadline (30 min by default) that needs no process table, and an expected-but-unreadable repo data/ folder refuses
admission instead of being silently skipped.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from companion import headroom

ADMIT_LOAD = float(os.environ.get("KYRA_ADMIT_LOAD", "10"))
TEST_RSS_MB = int(os.environ.get("KYRA_TEST_RSS_MB", "3072"))
TREE_RSS_MB = int(os.environ.get("KYRA_TEST_TREE_MB", "12288"))  # a third of the laptop's: room for a capped -n run
TEST_SECONDS = int(os.environ.get("KYRA_TEST_SECONDS", "600"))
CRITICAL_SECONDS = int(os.environ.get("KYRA_PRESSURE_CRITICAL_S", "60"))
SWAP_LIMIT_BYTES = 20 * 1024**3  # the brain-safety swap trigger
TRAIN_KINDS = frozenset({"real-data smoke", "configured landing phase", "configured landing shard"})
HEAVY_MB = 300  # the watchdog never stops a process smaller than this: it would free nothing
STRAY_TEST_MB = int(TEST_RSS_MB * 1.2)  # the watchdog's ceiling for a pytest the conftest guard is not watching
WATCH_TICK = 5
GUARD_TICK = 1
GUARD_DEADLINE_SECONDS = int(os.environ.get("KYRA_TEST_GUARD_DEADLINE_S", "1800"))  # guard_tests' own clock limit
KILL_GRACE = 3
GROUP_KILL_GRACE = 1  # blind or past the deadline: TERM the group, give it a second, then KILL it
REFUSED_EXIT = headroom.REFUSED_EXIT
GUARD_DIR_ENV = "KYRA_TEST_GUARD_DIR"
LOOPS = ("lanes", "hierarchy", "inspector", "hardware_keeper")
PRESSURE_NAMES = {"1": "normal", "2": "warn", "4": "critical"}


# ---- the process table --------------------------------------------------------------------------------------------

@dataclass
class Proc:
    pid: int
    ppid: int
    rss_mb: int
    age: int  # seconds since start
    command: str
    stopped: bool = False  # SIGSTOPped (state T): a --where local train pauses helper test runs (remote_test)


def etime_seconds(text):
    """ps etime ([[dd-]hh:]mm:ss) in seconds; 0 when unreadable."""
    match = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_ps(text):
    """Rows of `ps -o pid=,ppid=,rss=,stat=,etime=,args=`."""
    procs = {}
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5 or not all(f.isdigit() for f in fields[:3]):
            continue
        pid, ppid, rss = (int(f) for f in fields[:3])
        procs[pid] = Proc(pid, ppid, rss // 1024, etime_seconds(fields[4]), fields[5] if len(fields) > 5 else "",
                          stopped=fields[3].startswith("T"))
    return procs


def read_procs(run=None):
    """This user's processes, or None when the process table cannot be read: `ps` cannot run, exits nonzero, or
    lists nothing at all (a wall denying `ps` must read as "cannot see", never as an empty, safe machine)."""
    run = subprocess.run if run is None else run
    try:
        out = run(["ps", "-U", str(os.getuid()), "-o", "pid=,ppid=,rss=,stat=,etime=,args="],
                  capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(out, "returncode", 0) != 0:
        return None
    return parse_ps(out.stdout or "") or None


def descendants(procs, root):
    """``root`` and every process under it, parents before children."""
    if not procs:
        return []
    children = {}
    for proc in procs.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    order, todo = [], [root]
    while todo:
        pid = todo.pop(0)
        if pid in procs and pid not in order:
            order.append(pid)
            todo.extend(children.get(pid, []))
    return order


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_tree(pids, *, kill=os.kill, sleep=time.sleep, alive=alive, grace=KILL_GRACE):
    """TERM every pid (children first, so a parent cannot respawn them), then KILL what is left after ``grace``."""
    pids = [p for p in reversed(pids) if p != os.getpid()]
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(p) for p in pids):
        sleep(0.2)
    for pid in pids:
        if alive(pid):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return pids


def stop_process_group(root, *, grace=GROUP_KILL_GRACE, sleep=time.sleep, is_alive=alive):
    """SIGTERM ``root``'s whole process group, then SIGKILL it after ``grace`` if the root is still there. This is
    the stop that needs no process table. Never signal our own group: a root that shares it (an in-process caller
    whose child is no session leader) is stopped on its own instead."""
    try:
        pgid = os.getpgid(root)
    except OSError:
        return []
    if pgid == os.getpgrp():
        return stop_tree([root], grace=grace)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return []
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and is_alive(root):
        sleep(0.05)
    if is_alive(root):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return [root]


# ---- alerts --------------------------------------------------------------------------------------------------------

def alerts_path(data=None):
    data = data if data is not None else headroom.main_data_dir(Path(__file__).resolve().parent)
    return Path(data) / "team/ALERTS.md" if data is not None else None


def alert(text, *, path=None, clock=time.time):
    """One `MM-DD HH:MM [mac-guard] text` line in the alerts file, and always on stderr (a walled worker or the M1
    runner has no data/ folder to write to)."""
    line = f"{time.strftime('%m-%d %H:%M', time.localtime(clock()))} [mac-guard] {text}"
    print(line, file=sys.stderr, flush=True)
    path = path or alerts_path()
    if path is None:
        return line
    try:
        with open(path, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    except OSError:
        pass
    return line


# ---- 1. admission --------------------------------------------------------------------------------------------------

def gate_off():
    return os.environ.get("KYRA_MAC_GATE", "").lower() in ("off", "0", "false")


BLIND_REFUSAL = "cannot read the process table (ps): cannot see what is already running"


def _blind_refusal(read):
    """BLIND_REFUSAL when the process table cannot be read (``read`` returns None or raises), else None."""
    try:
        seen = (read_procs if read is None else read)()
    except Exception:  # a guard that crashes open is the bug this fixes
        return BLIND_REFUSAL
    return BLIND_REFUSAL if seen is None else None


def _data_refusal(data):
    """Why an explicitly named repo data/ folder cannot be used, or None. ``data=None`` means the caller needs no
    data folder (off this Mac); a named folder that is missing, not a folder or unreadable refuses."""
    if data is None:
        return None
    path = Path(data)
    try:
        if not stat.S_ISDIR(path.stat().st_mode):
            return f"cannot read the repo data folder {path}: not a folder"
        next(path.iterdir(), None)
    except OSError as error:
        return f"cannot read the repo data folder {path}: {error.strerror or error}"
    return None


def _expected_data_refusal(start=None):
    """Why this checkout's expected data/ folder cannot be read, or None. Only a git checkout expects one (the M1
    runner's archive has neither git nor data/); a folder that simply is not there expects nothing, one that is
    there but unreadable (a wall denying data/) refuses instead of being silently skipped."""
    start = Path(start or Path(__file__).resolve().parent)
    out = headroom._run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not out:
        return None
    common = Path(out.strip())
    if common.name != ".git":
        return None
    expected = common.parent / "data"
    try:
        expected.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"cannot read the repo data folder {expected}: {error.strerror or error}"
    return _data_refusal(expected)


def admission_refusal(probes=None, data=None, *, limit=None, read=None):
    """Why no new worker, test or train may start on this Mac now, or None. Blindness first (an unreadable process
    table, or a named data/ folder that cannot be read), then travel mode, then memory (pressure warn or
    critical, free under 10%, an unreadable probe), then the 1-minute load over ``limit`` (ADMIT_LOAD, never above
    headroom's 0.9 x cores)."""
    if refused := _blind_refusal(read):
        return refused
    if refused := _data_refusal(data):
        return refused
    probes = probes or headroom.Probes()
    if refused := headroom.travel_refusal(data, probes):
        return refused
    if refused := headroom.memory_refusal(probes):
        return refused
    line = min(ADMIT_LOAD if limit is None else limit, headroom.load_limit(probes))
    load = probes.load()
    if load > line:
        return f"load average {load:.1f} is above {line:g} on {probes.cores()} cores"
    return None


def swap_used_bytes(run=headroom._run):
    """Used swap from ``sysctl -n vm.swapusage`` ("used = 7967.50M"), or None when it cannot be read."""
    match = re.search(r"used\s*=\s*([\d.]+)([KMGT])", run(["sysctl", "-n", "vm.swapusage"]) or "")
    if not match:
        return None
    return int(float(match.group(1)) * 1024 ** "KMGT".index(match.group(2)) * 1024)


def memory_danger_refusal(probes=None, data=None, *, swap=None, read=None):
    """The train smoke and test admission (TRAIN_KINDS): blindness (an unreadable process table, or a named data/
    folder that cannot be read), travel mode, memory pressure warn or critical, an unreadable pressure or swap
    probe, or swap over SWAP_LIMIT_BYTES. Load is not a reason (owner, 2026-10-30)."""
    if refused := _blind_refusal(read):
        return refused
    if refused := _data_refusal(data):
        return refused
    probes = probes or headroom.Probes()
    if refused := headroom.travel_refusal(data, probes):
        return refused
    level = (probes.pressure() or "").strip()
    if level in ("2", "4"):
        return f"memory pressure is {PRESSURE_NAMES[level]} (needs normal)"
    if level != "1":
        return "cannot read system memory pressure"
    used = (swap or swap_used_bytes)()
    if used is None:
        return "cannot read system swap"
    if used > SWAP_LIMIT_BYTES:
        return f"system swap {used / 1024**3:.1f} GiB is above 20 GiB"
    return None


_FIND = object()


def admit(kind, *, probes=None, data=_FIND, say=None, read=None):
    """None to go ahead, else the refusal text (``say`` gets one line). ``data`` defaults to the main checkout's data/
    (travel mode); off this Mac (no data/, or data=None) only memory and load count, but a checkout whose expected
    data/ folder is there and unreadable refuses, as does an unreadable process table. KYRA_MAC_GATE=off admits all.
    The CLI turns a refusal into the defer exit (75)."""
    if gate_off():
        return None
    expected_refusal = None
    if data is _FIND:
        data = headroom.main_data_dir(Path(__file__).resolve().parent)
        if data is None:
            expected_refusal = _expected_data_refusal()
    if expected_refusal is not None:
        reason = expected_refusal
    elif kind in TRAIN_KINDS:
        reason = memory_danger_refusal(probes, data, read=read)
    else:
        reason = admission_refusal(probes, data, read=read)
    if reason and say:
        say(f"mac gate: {kind} deferred: {reason}")
    return reason


# ---- 2. the test guard ---------------------------------------------------------------------------------------------

def guard_dir(root_pid):
    return Path(tempfile.gettempdir()) / "kyra-test-guard" / str(root_pid)


def heartbeat(directory, nodeid, *, pid=None, clock=time.time):
    """The conftest's per-test marker: this process is running ``nodeid`` since now. ``None`` clears it."""
    path = Path(directory) / str(pid or os.getpid())
    try:
        if nodeid is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{clock():.0f}\t{nodeid}\n", encoding="utf-8")
    except OSError:
        pass


def running_tests(directory):
    """{pid: (start epoch, nodeid)} from the heartbeat files."""
    found = {}
    for path in Path(directory).glob("*"):
        try:
            start, nodeid = path.read_text(encoding="utf-8").strip().split("\t", 1)
            found[int(path.name)] = (float(start), nodeid)
        except (OSError, ValueError):
            continue
    return found


def test_verdict(procs, root, tests, now, *, rss_mb=TEST_RSS_MB, tree_mb=TREE_RSS_MB, seconds=TEST_SECONDS, me=None):
    """(pids to stop, reason) for one look at a pytest run, or None. One process over ``rss_mb`` stops its own tree
    (an xdist worker: xdist reports the crash and carries on; the controller: the whole run); the run over ``tree_mb``
    or a test over ``seconds`` stops the tree that runs it."""
    if not procs:
        return None
    me = os.getpid() if me is None else me
    mine = set(descendants(procs, me))  # the guard is the controller's child; it and its ps calls are not the run
    def subtree(pid):
        return [p for p in descendants(procs, pid) if p not in mine]
    tree = subtree(root)
    if not tree:
        return None
    for pid in tree:
        if procs[pid].rss_mb > rss_mb:
            test = tests.get(pid, (0, None))[1] or _owner_test(procs, pid, tests)
            return subtree(pid), (f"pid {pid} at {procs[pid].rss_mb} MB is over the {rss_mb} MB test "
                                  f"ceiling{f' (test {test})' if test else ''}")
    total = sum(procs[p].rss_mb for p in tree)
    if total > tree_mb:
        return tree, f"the pytest run's processes hold {total} MB, over the {tree_mb} MB ceiling"
    for pid, (start, nodeid) in tests.items():
        if pid in procs and pid in tree and now - start > seconds:
            return subtree(pid), f"test {nodeid} has run {now - start:.0f}s, over the {seconds}s limit"
    return None


def _owner_test(procs, pid, tests):
    """The test that a helper subprocess belongs to: the nearest ancestor with a heartbeat."""
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        if pid in tests:
            return tests[pid][1]
        pid = procs[pid].ppid
    return None


def guard_tests(root, *, read=None, clock=time.time, sleep=time.sleep, stop=stop_tree, say=alert,
                directory=None, rss_mb=None, tree_mb=None, seconds=None, is_alive=alive, me=None,
                deadline=None, deadline_seconds=None, monotonic=time.monotonic, stop_group=None):
    """Watch one pytest run until its controller exits; stop what crosses a ceiling. Stopping an xdist worker does not
    end the watch: xdist starts a replacement and carries on, and the replacement is watched too. Returns the last
    alert line or None.

    Fail closed, with no process table needed: ``read`` returning None ("cannot see") stops the guarded process
    group at once instead of waiting, and a monotonic deadline (``deadline_seconds``, alias ``deadline``; default
    GUARD_DEADLINE_SECONDS, 30 min) stops that group however the table reads."""
    read = read_procs if read is None else read
    directory = Path(directory or guard_dir(root))
    limits = dict(rss_mb=rss_mb or TEST_RSS_MB, tree_mb=tree_mb or TREE_RSS_MB, seconds=seconds or TEST_SECONDS)
    limit = deadline_seconds if deadline_seconds is not None else deadline
    limit = GUARD_DEADLINE_SECONDS if limit is None else limit
    kill_group = stop_process_group if stop_group is None else stop_group
    deadline_at = monotonic() + limit
    child = os.getppid() == root  # started by the run itself: reparented the moment it exits, even as a zombie
    paused, last = {}, clock()  # seconds each test process spent stopped (state T): they do not count toward the limit
    line = None

    def gone():
        return not is_alive(root) or (child and os.getppid() != root)

    try:
        while True:
            if monotonic() >= deadline_at:
                if gone():
                    return line
                kill_group(root)
                return say(f"test guard stopped the process group of pytest pid {root}: the guard's "
                           f"{limit:g}s deadline passed")
            try:
                procs = read()
            except Exception:  # a reader that raises is blind, not a reason to die and leave the run unguarded
                procs = None
            if procs is None:
                if gone():
                    return line
                kill_group(root)
                return say(f"test guard stopped the process group of pytest pid {root}: cannot read the "
                           "process table (ps), so the run cannot be watched")
            if (procs and root not in procs) or gone():
                return line
            now = clock()
            step, last = now - last, now
            tests = {}
            for pid, (start, nodeid) in running_tests(directory).items():
                held = paused.get(pid, (nodeid, 0.0))[1] if paused.get(pid, (nodeid,))[0] == nodeid else 0.0
                if step > 5 * GUARD_TICK:  # the guard itself did not run (the Mac slept): nobody's test ran either
                    held += step - GUARD_TICK
                elif pid in procs and procs[pid].stopped:
                    held += step
                paused[pid] = (nodeid, held)
                tests[pid] = (start + held, nodeid)
            verdict = test_verdict(procs, root, tests, now, me=me, **limits) if procs else None
            if verdict:
                pids, reason = verdict
                command = procs[pids[0]].command[:120] if pids and pids[0] in procs else ""
                stop(pids)
                line = say(f"test guard stopped {len(pids)} process(es) (pytest pid {root}): {reason}; "
                           f"command: {command}")
                if root in pids:
                    return line
                for pid in pids:  # the stopped worker's test is over; its clock must not stop the next one
                    heartbeat(directory, None, pid=pid)
                    paused.pop(pid, None)
            sleep(GUARD_TICK)
    finally:
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass


def peak_rss_mb():
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // (1024 * 1024) if sys.platform == "darwin" else peak // 1024  # bytes on macOS, KiB on Linux


class SelfGuard:
    """The same two ceilings from inside each process that runs tests (the controller, or each xdist worker), on a
    daemon thread. It needs no process table, so it also works inside a worker sandbox that hides `ps`; the external
    guard covers what this cannot: child processes, and a C call that holds the GIL. Its lines sit 10% and 30 s past
    the outer guard's, so where both can act the outer one stops the run first and there is one alert, not two. Over a
    ceiling it writes the alert and kills its own process (xdist reports the worker's crash and carries on)."""

    def __init__(self, *, rss_mb=None, seconds=None, peak=peak_rss_mb, clock=time.time, say=alert,
                 die=lambda: os.kill(os.getpid(), signal.SIGKILL)):
        self.rss_mb, self.seconds = rss_mb or int(TEST_RSS_MB * 1.1), seconds or TEST_SECONDS + 30
        self.peak, self.clock, self.say, self.die = peak, clock, say, die
        self.test = None  # (start epoch, nodeid) of the running test

    def check(self):
        """The alert line when over a ceiling (after ``die``), else None."""
        test = self.test
        peak = self.peak()
        if peak > self.rss_mb:
            reason = f"pid {os.getpid()} reached {peak} MB, over the {self.rss_mb} MB test ceiling"
        elif test and self.clock() - test[0] > self.seconds:
            reason = f"test has run {self.clock() - test[0]:.0f}s, over the {self.seconds}s limit"
        else:
            return None
        line = self.say(f"test guard stopped pytest pid {os.getpid()}: {reason}"
                        f"{f' (test {test[1]})' if test else ''}")
        self.die()
        return line

    def resumed(self, gap):
        """The process was stopped for ``gap`` seconds (a train pauses helper runs): the test's clock skips them."""
        test = self.test
        if test is not None:
            self.test = (test[0] + gap, test[1])

    def start(self):
        import threading

        def loop():
            last = self.clock()
            while self.check() is None:
                time.sleep(GUARD_TICK)
                now = self.clock()
                if now - last > 5 * GUARD_TICK:  # this thread did not run: the process was stopped or the Mac slept
                    self.resumed(now - last - GUARD_TICK)
                last = now
        threading.Thread(target=loop, name="kyra-self-guard", daemon=True).start()
        return self


def start_test_guard(root_pid=None, *, popen=subprocess.Popen):
    """Start the guard beside this pytest controller and point the run's heartbeat files at its folder (the env reaches
    xdist workers, which start later). Returns the Popen or None (no guard: not macOS, or turned off)."""
    if sys.platform != "darwin" or os.environ.get("KYRA_TEST_GUARD", "").lower() in ("off", "0", "false"):
        return None
    root_pid = root_pid or os.getpid()
    directory = guard_dir(root_pid)
    directory.mkdir(parents=True, exist_ok=True)
    os.environ[GUARD_DIR_ENV] = str(directory)
    src = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    try:
        # No inherited pipe: a caller that captures the run's output waits for EOF before it reaps the pytest, and a
        # guard holding stderr open would wait for that pytest forever (its alerts go to ALERTS.md)
        return popen([sys.executable, "-m", "companion.mac_guard", "guard-tests", str(root_pid)], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    except OSError:
        return None


# ---- 3. the pressure watchdog --------------------------------------------------------------------------------------

_PROTECTED = re.compile(
    r"^(/System/|/usr/(libexec|sbin)/|/sbin/|/Library/Apple/)"  # macOS itself
    r"|companion\.webapp|uvicorn|com\.kyra\.web-ui"  # the web UI
    r"|^(/bin/)?zsh \S*/lanes/(" + "|".join(LOOPS) + r")\.sh"  # the four loops
    r"|companion\.mac_guard")  # this guard
_JOB_SCRIPTS = ("/local-jobs/run_job.py", "scripts/train.py", "scripts/workflow_land.py")


def agent_kind(command):
    """What an agent-started process is (pytest, xdist-worker, worker, local-agent, job, test-browser), or None.
    Anchored on the executable: a shell whose arguments only mention pytest is not one."""
    argv = command.split()
    if not argv:
        return None
    if Path(argv[0]).name.lower() in ("node", "nodejs", "bun") and len(argv) > 1:
        argv = argv[1:]
    path, rest = argv[0], argv[1:]
    exe = Path(path).name.lower()
    if "/ms-playwright/" in command.split(" --", 1)[0] or exe.startswith("chrome-headless-shell"):
        return "test-browser"
    if path.startswith("/Applications/") and "/codex-cli/" not in path:
        return None  # the owner's apps (browsers, chat and editor apps): never
    if exe in ("codex", "codex.js") and rest[:1] == ["exec"]:
        return "worker"
    if exe == "claude" and any(a in ("-p", "--print") for a in rest[:4]):
        return "worker"
    if exe in ("grok", "grok.js", "cursor-agent", "muse"):
        return "worker"
    if exe == "pytest":
        return "pytest"
    if re.fullmatch(r"python(\d+(\.\d+)*)?", exe):
        while rest and rest[0].startswith("-") and rest[0] not in ("-m", "-c"):
            rest = rest[1:]
        if rest[:2] == ["-m", "pytest"] or (rest and Path(rest[0]).name == "pytest"):
            return "pytest"
        if rest[:1] == ["-c"] and "sys.stdin.readline" in command:
            return "xdist-worker"
        if rest[:2] == ["-m", "companion.local_agent"]:
            return "local-agent"
        if rest and rest[0].endswith(_JOB_SCRIPTS):
            return "job"
    return None


def agent_heavy(proc, heavy_mb=HEAVY_MB):
    """An agent-started process big enough to matter, never one of the owner's apps, the web UI, the loops or macOS."""
    if proc.rss_mb < heavy_mb or _PROTECTED.search(proc.command):
        return False
    return agent_kind(proc.command) is not None


def victim(procs, heavy_mb=HEAVY_MB):
    """The newest agent-started heavy process (youngest first, the biggest on a tie), or None."""
    if not procs:
        return None
    found = [p for p in procs.values() if agent_heavy(p, heavy_mb)]
    return min(found, key=lambda p: (p.age, -p.rss_mb), default=None)


def pressure_level(run=headroom._run):
    level = (run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]) or "").strip()
    return PRESSURE_NAMES.get(level, "unknown")


class Watchdog:
    """One tick every WATCH_TICK seconds; ``tick`` returns the alert line of a stop, else None."""

    def __init__(self, *, level=pressure_level, read=None, stop=stop_tree, say=alert, clock=time.monotonic,
                 critical_seconds=None, heavy_mb=HEAVY_MB, stray_mb=STRAY_TEST_MB, dry_run=False):
        self.level, self.stop, self.say, self.clock = level, stop, say, clock
        self.read = read_procs if read is None else read
        self.critical_seconds = CRITICAL_SECONDS if critical_seconds is None else critical_seconds
        self.heavy_mb, self.stray_mb, self.dry_run = heavy_mb, stray_mb, dry_run
        self.critical_since = None

    def _seen(self):
        try:
            return self.read()
        except Exception:
            return None

    def stray_test(self):
        """Stop the biggest pytest or xdist worker over STRAY_TEST_MB, at any pressure; the alert line or None."""
        procs = self._seen()
        if procs is None:
            return None  # blind: no victim can be named; admission and guard_tests are the fail-closed stops
        found = [p for p in procs.values() if p.rss_mb > self.stray_mb and not _PROTECTED.search(p.command)
                 and agent_kind(p.command) in ("pytest", "xdist-worker")]
        if not found:
            return None
        target = max(found, key=lambda p: p.rss_mb)
        tree = [p for p in descendants(procs, target.pid) if not _PROTECTED.search(procs[p].command)]
        if not self.dry_run:
            self.stop(tree)
        verb = "would stop" if self.dry_run else "stopped"
        return self.say(f"test ceiling: watchdog {verb} pid {target.pid} ({target.rss_mb} MB, {target.age}s old, "
                        f"{len(tree)} process(es)), over the {self.stray_mb} MB ceiling for a test; "
                        f"command: {target.command[:120]}")

    def tick(self):
        if line := self.stray_test():
            return line
        now = self.clock()
        if self.level() != "critical":
            self.critical_since = None
            return None
        if self.critical_since is None:
            self.critical_since = now
        if now - self.critical_since < self.critical_seconds:
            return None
        procs = self._seen()
        self.critical_since = now  # the next stop waits a full window, so the kernel can reclaim what this one frees
        if procs is None:
            return self.say(f"memory pressure critical for {self.critical_seconds}s+ and the process table "
                            "cannot be read (ps): no process can be named or stopped")
        target = victim(procs, self.heavy_mb)
        if target is None:
            return self.say(f"memory pressure critical for {self.critical_seconds}s+ and no agent-started heavy "
                            "process to stop (the owner's apps are never touched)")
        tree = [p for p in descendants(procs, target.pid) if not _PROTECTED.search(procs[p].command)]
        if not self.dry_run:
            self.stop(tree)
        verb = "would stop" if self.dry_run else "stopped"
        return self.say(f"pressure watchdog {verb} pid {target.pid} ({target.rss_mb} MB, {target.age}s old, "
                        f"{len(tree)} process(es)): memory pressure critical for {self.critical_seconds}s+; "
                        f"command: {target.command[:120]}")


def watchdog(*, ticks=None, sleep=time.sleep, **kwargs):
    dog = Watchdog(**kwargs)
    print(f"mac_guard watchdog pid {os.getpid()}: critical {dog.critical_seconds}s -> stop the newest agent-started "
          f"heavy process; any test over {dog.stray_mb} MB{' (dry run)' if dog.dry_run else ''}", file=sys.stderr,
          flush=True)
    n = 0
    while ticks is None or n < ticks:
        dog.tick()
        n += 1
        sleep(WATCH_TICK)


# ---- command line --------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("admit", help="exit 0 to go ahead, 75 (reason on stderr) to queue and retry")
    gate.add_argument("--kind", default="launch", help="what wants to start, for the refusal line")
    guard = sub.add_parser("guard-tests", help="watch one pytest run")
    guard.add_argument("pid", type=int)
    guard.add_argument("--deadline-seconds", type=float, default=None,
                       help=f"stop the guarded process group after this long (default {GUARD_DEADLINE_SECONDS}s)")
    dog = sub.add_parser("watchdog", help="the 5 s pressure loop")
    dog.add_argument("--dry-run", action="store_true", help="log what would be stopped, stop nothing")
    sub.add_parser("status", help="pressure, load, free memory and the gate's answer")
    args = parser.parse_args(argv)
    if args.command == "admit":
        reason = admit(args.kind, say=lambda text: print(text, file=sys.stderr))
        return REFUSED_EXIT if reason else 0
    if args.command == "guard-tests":
        guard_tests(args.pid, deadline_seconds=args.deadline_seconds)
        return 0
    if args.command == "watchdog":
        watchdog(dry_run=args.dry_run)
        return 0
    probes = headroom.Probes()
    print(f"pressure={pressure_level()} load={probes.load():.1f} cores={probes.cores()} "
          f"free={headroom.free_percent(probes.memory())}% gate={admission_refusal(probes) or 'open'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
