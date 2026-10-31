"""Headroom and travel mode for the owner MacBook: the two standing rules, in code (the owner, 2026-10-15).

    "for this mac specifically, you always need to leave at least a bit of room (10 ish % is fine), i don't want my
    work or my Brave browser to be freezed, and should not run any tests when I say I am carrying this outside"

1. Headroom, always on. A local heavy job (a pytest run beyond a focused handful, browser tests, the commit gate's
   impacted run, a train's local phase, local model serving or training) starts only while the 1-minute load is at
   or under 0.9 x the core count (16.2 on 18 cores), the memory pressure level is normal and at least 10% of memory
   is free; otherwise it waits, then refuses. The xdist worker count is capped so the projected load (the current
   load plus one per worker) stays under the line. A focused handful ignores the load but still waits out memory
   pressure warn or critical (2026-10-29). Running tests have hard ceilings too: companion.mac_guard.
2. Travel mode. data/team/travel-mode.json; scripts/travel_mode.py on|off|status. While it is on, no local tests,
   browser runs, local models or training (tests go to the M1, GPU jobs wait); hosted agents continue. Turning it
   on releases this user's caffeinate keep-awake assertions so the Mac can sleep in the bag. It turns on by itself
   on battery with the lid closed, or when pmset reports a CPU speed limit under 100; it turns off by itself only
   when it turned itself on and AC power is back with the lid open; otherwise only the owner command turns it off.

The checks run on this Mac only: main_data_dir() needs the git checkout and its data/ folder, which the M1 runner's
archive has neither of. A probe that cannot be read refuses (an unknown state is not "all clear").
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

LOAD_FACTOR = 0.9
MIN_FREE_PERCENT = 10
TRAVEL_FILE = "team/travel-mode.json"
WAIT_SECONDS = int(os.environ.get("KYRA_ADMISSION_WAIT", "900"))  # a caller with its own timeout sets it lower
REFUSED_EXIT = 75  # what a refused pytest exits with (tests/conftest.py)
POLL_SECONDS = 15
UNREADABLE_WAIT = 30  # no readable memory signal at all (a sandbox blocking sysctl and vm_stat): short wait, then fail
UNREADABLE = "cannot read the free memory percentage"
LIGHT_MAX_FILES = 5  # "a focused handful": up to five explicit test files or node ids, no xdist
LIGHT_WAIT = 60  # a focused run waits this long (or KYRA_ADMISSION_WAIT if lower) for memory pressure to be normal


def _run(command):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


@dataclass
class Probes:
    """Everything that reads the machine, so tests can feed it states."""
    load: callable = field(default=lambda: os.getloadavg()[0])
    cores: callable = field(default=lambda: os.cpu_count() or 1)
    memory: callable = field(default=lambda: _run(["memory_pressure", "-Q"]))
    pressure: callable = field(default=lambda: _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]))
    vmstat: callable = field(default=lambda: _run(["vm_stat"]))
    memsize: callable = field(default=lambda: _run(["sysctl", "-n", "hw.memsize"]))
    battery: callable = field(default=lambda: _run(["pmset", "-g", "batt"]))
    lid: callable = field(default=lambda: _run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"]))
    thermal: callable = field(default=lambda: _run(["pmset", "-g", "therm"]))
    clock: callable = field(default=time.time)


def main_data_dir(start=None):
    """The main checkout's data/ folder (shared by every worktree), or None off this Mac (no git, no data/)."""
    start = Path(start or Path(__file__).resolve().parent)
    out = _run(["git", "-C", str(start), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not out:
        return None
    common = Path(out.strip())
    data = common.parent / "data" if common.name == ".git" else None
    try:
        return data if data is not None and data.is_dir() else None
    except PermissionError:
        # a walled worker (companion.worker_sandbox denies data/): the dispatcher already admitted its launch, and a
        # crash here failed every pytest run before any test (medic 2026-10-17, muse MS5)
        return None


def free_percent(output):
    match = re.search(r"free percentage:\s*(\d+)%", output or "")
    return int(match[1]) if match else None


def vmstat_free_percent(vmstat, memsize):
    """Free plus inactive pages as a percentage of hw.memsize (the fallback when memory_pressure cannot run)."""
    size = re.search(r"page size of (\d+) bytes", vmstat or "")
    pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive):\s*(\d+)", vmstat or "")}
    total = (memsize or "").strip()
    if not size or len(pages) != 2 or not total.isdigit() or int(total) <= 0:
        return None
    return int(100 * sum(pages.values()) * int(size[1]) / int(total))


def memory_refusal(probes):
    level = (probes.pressure() or "").strip()
    if level and level != "1":
        names = {"2": "warn", "4": "critical"}
        return f"memory pressure is {names.get(level, level)} (needs normal)"
    free = free_percent(probes.memory())
    if free is None and not level:
        free = vmstat_free_percent(probes.vmstat(), probes.memsize())
    if not level:
        # A worker context can block the sysctl (exit -15 in the commit gate, 2026-10-16): degrade to the free-RAM
        # percentage and the load check, say so once, and never kill the run for a probe that cannot be read.
        print("headroom: memory pressure level unreadable; using another free-memory measure and load", file=sys.stderr)
    if free is None:
        return UNREADABLE
    if free < MIN_FREE_PERCENT:
        return f"free memory is {free}% (needs {MIN_FREE_PERCENT}%)"
    return None


def load_limit(probes):
    return LOAD_FACTOR * probes.cores()


def headroom_refusal(probes):
    """Why a local heavy job must not start now, or None: load above 0.9 x cores, or memory short."""
    load, limit = probes.load(), load_limit(probes)
    if load > limit:
        return f"load average {load:.1f} is above {limit:.1f} ({LOAD_FACTOR:g} x {probes.cores()} cores)"
    return memory_refusal(probes)


def worker_cap(probes, requested):
    """The xdist workers that keep the projected load (current + one per worker) at or under the line; at least 1."""
    room = math.floor(load_limit(probes) - probes.load())
    return max(1, min(int(requested), room))


# ---- travel mode ------------------------------------------------------------------------------------------------

def travel_path(data):
    return Path(data) / TRAVEL_FILE


def travel_state(data):
    """{"on": bool, "since", "by", "reason"}; a missing file is off, an unreadable one is on (fail closed)."""
    path = travel_path(data)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"on": False}
    except (OSError, ValueError):
        return {"on": True, "by": "unreadable", "reason": f"{path} is unreadable"}
    return state if isinstance(state, dict) and isinstance(state.get("on"), bool) else {
        "on": True, "by": "unreadable", "reason": f"{path} is not a travel-mode object"}


def set_travel(data, on, *, by, reason, clock=time.time):
    path = travel_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"on": bool(on), "since": clock(), "by": by, "reason": reason}
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as out:
        out.write(json.dumps(state) + "\n")
    os.replace(out.name, path)
    return state


def on_ac(battery_output):
    text = (battery_output or "").lower()
    return "'ac power'" in text or "ac attached" in text


def lid_closed(ioreg_output):
    match = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', ioreg_output or "")
    return match is not None and match[1] == "Yes"


def cpu_speed_limit(thermal_output):
    match = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", thermal_output or "")
    return int(match[1]) if match else None


def auto_travel(data, probes):
    """Turn travel mode on by itself (battery with the lid closed, or a CPU speed limit under 100), and off by itself
    only when it turned itself on and AC power is back with the lid open. Returns the state after the check."""
    state = travel_state(data)
    battery, lid, limit = probes.battery(), probes.lid(), cpu_speed_limit(probes.thermal())
    if not state["on"]:
        if battery is not None and not on_ac(battery) and lid_closed(lid):
            return set_travel(data, True, by="auto", reason="on battery with the lid closed", clock=probes.clock)
        if limit is not None and limit < 100:
            return set_travel(data, True, by="auto", reason=f"CPU_Speed_Limit {limit}", clock=probes.clock)
        return state
    if state.get("by") == "auto" and on_ac(battery) and lid is not None and not lid_closed(lid) and (
            limit is None or limit >= 100):
        return set_travel(data, False, by="auto", reason="AC power back with the lid open", clock=probes.clock)
    return state


def travel_refusal(data, probes=None):
    """Why no local heavy job may start (travel mode), or None. Runs the auto check first."""
    if data is None:
        return None
    state = auto_travel(data, probes or Probes())
    if not state["on"]:
        return None
    return (f"travel mode is on ({state.get('reason') or 'no reason given'}, by {state.get('by') or '?'}): no local "
            "tests, browser runs, local models or training; tests go to the M1 (scripts/remote_test.py), hosted "
            "agents continue; scripts/travel_mode.py off turns it off")


def local_heavy_refusal(data, probes=None):
    """The one admission answer for a local heavy job on this Mac: travel mode first, then headroom."""
    probes = probes or Probes()
    return travel_refusal(data, probes) or headroom_refusal(probes)


def release_keep_awake(run=subprocess.run):
    """Stop this user's caffeinate processes so the Mac can sleep in the bag; list other sleep-preventing holders
    (another app's assertion is reported, never touched). Returns (stopped pids, other holder lines)."""
    found = run(["pgrep", "-x", "-u", str(os.getuid()), "caffeinate"], capture_output=True, text=True)
    pids = [int(p) for p in (found.stdout or "").split() if p.isdigit()]
    for pid in pids:
        run(["kill", "-TERM", str(pid)], capture_output=True, text=True)
    listing = run(["pmset", "-g", "assertions"], capture_output=True, text=True)
    holders = [line.strip() for line in (listing.stdout or "").splitlines()
               if re.search(r"PreventUserIdleSystemSleep|PreventSystemSleep", line) and "named:" in line
               and "caffeinate" not in line]
    return pids, holders


# ---- pytest admission ---------------------------------------------------------------------------------------------

def is_light(args, numprocesses):
    """A focused handful: no xdist and up to LIGHT_MAX_FILES explicit test files or node ids (no directories), none of
    them a browser test (Chromium beside Brave is heavy at any count, review o-rev66)."""
    if numprocesses not in (None, 0, "0"):
        return False
    targets = [a for a in args if not a.startswith("-")]
    return 0 < len(targets) <= LIGHT_MAX_FILES and all(
        re.search(r"(^|/)test_[^/]*\.py(::|$)", a) and not re.search(r"_browser\.py(::|$)", a) for a in targets)


def light_refusal(probes):
    """A light run ignores the load but not the memory pressure level: the one-file tests/test_listening_vad.py that grew
    to 5.4 GB on 2026-10-29 started while pressure was already at warn (companion.mac_guard)."""
    level = (probes.pressure() or "").strip()
    if level in ("2", "4"):
        return f"memory pressure is {'warn' if level == '2' else 'critical'} (needs normal), even for a focused run"
    return None


def train_refusal(probes):
    """A train's serial preflight and merged-tree guards (KYRA_PREFLIGHT=1, no xdist) judge memory danger, not load
    (owner, 2026-10-30): companion.mac_guard.memory_danger_refusal. The running-test ceilings still apply."""
    from companion import mac_guard
    return mac_guard.memory_danger_refusal(probes, None)


def admit_pytest(data, args, numprocesses, *, probes=None, sleep=time.sleep, say=None, wait=None):
    """None to go ahead, else the refusal text. Travel mode refuses every local run at once; a heavy run waits up
    to ``wait`` seconds for headroom, then refuses; a light run skips the load wait but waits up to LIGHT_WAIT seconds
    for memory pressure to fall back to normal, then refuses."""
    probes = probes or Probes()
    refused = travel_refusal(data, probes)
    if refused:
        return refused
    light = is_light(args, numprocesses)
    train = os.environ.get("KYRA_PREFLIGHT") == "1" and numprocesses in (None, 0, "0")
    check = train_refusal if train else light_refusal if light else headroom_refusal
    wait = (min(LIGHT_WAIT, WAIT_SECONDS) if light else WAIT_SECONDS) if wait is None else wait
    deadline = probes.clock() + wait
    reason = check(probes)
    told = capped = False
    while reason:
        if reason == UNREADABLE and not capped:
            # Waiting cannot make a blocked probe readable: cap the wait so the caller's own time limit never hits first.
            capped, deadline, wait = True, min(deadline, probes.clock() + UNREADABLE_WAIT), min(wait, UNREADABLE_WAIT)
        if probes.clock() >= deadline:
            return f"no headroom for {wait}s: {reason}; the owner rule keeps 10% of this Mac free"
        if say and not told:
            say(f"waiting for headroom (up to {wait}s): {reason}")
            told = True
        sleep(POLL_SECONDS)
        reason = check(probes)
    return None
