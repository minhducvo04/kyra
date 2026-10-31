#!/usr/bin/env python3
"""The Mac's last-resort memory kill switch (owner order, 2026-10-30, after three runaways in one day: a 24 GB web UI
model, then a 171 GB and a 400 GB synthetic memory hog started by an agent in a sandbox; each time the owner had to
restart this Mac).

companion.mac_guard reads RSS through `ps`, skips processes by name and is blind where a sandbox denies `ps`. This
script is none of those: it runs as the owner under launchd (scripts/mac_hard_kill.plist, outside every sandbox), uses
only the standard library and libproc, and every TICK (2 s) reads each process's physical footprint
(proc_pid_rusage ri_phys_footprint, the number Activity Monitor's "Memory" column shows: it counts compressed and GPU
memory, which RSS does not). A process owned by this user is killed (SIGTERM, then SIGKILL after GRACE 3 s) when it is

  * over HARD_BYTES (12 GB), or
  * over GROW_FLOOR_BYTES (4 GB) and growing faster than GROW_BYTES (1 GB) per GROW_WINDOW (10 s).

Exempt: only WindowServer, kernel_task and processes another user owns. Python, Claude, browsers and editors are not
exempt on purpose: any of them at 12 GB on this Mac is the runaway. Each kill writes one line with the pid, command
(first 120 chars), footprint and parent chain to data/team/mac-hard-kill.log, and one line to
data/team/inbox.md.

    python scripts/mac_hard_kill.py            # the loop launchd runs
    python scripts/mac_hard_kill.py --once --dry-run   # one look, kills nothing, prints what it would kill

Install (the owner runs it, from the main checkout once this file is on its master; one line):
    sed "s#__KYRA_ROOT__#$PWD#g" scripts/mac_hard_kill.plist > <user-home>/Library/LaunchAgents/com.kyra.mac-hard-kill.plist && launchctl bootstrap gui/$(id -u) <user-home>/Library/LaunchAgents/com.kyra.mac-hard-kill.plist
Check:     launchctl print gui/$(id -u)/com.kyra.mac-hard-kill | grep -E "state|pid"
Uninstall: launchctl bootout gui/$(id -u)/com.kyra.mac-hard-kill && rm <user-home>/Library/LaunchAgents/com.kyra.mac-hard-kill.plist
"""  # noqa: E501
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

GB = 1024**3
HARD_BYTES = 12 * GB
GROW_FLOOR_BYTES = 4 * GB
GROW_BYTES = 1 * GB
GROW_WINDOW = 10.0
TICK = 2.0
GRACE = 3.0
EXEMPT_NAMES = frozenset({"WindowServer", "kernel_task"})
ROOT = Path(__file__).resolve().parents[1]

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
_PROC_PIDTBSDINFO = 3
_RUSAGE_INFO_V0 = 0
_SZOMB = 5  # <sys/proc.h> p_stat of a zombie


class _BsdInfo(ctypes.Structure):  # <sys/proc_info.h> struct proc_bsdinfo
    _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32), ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32), ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32), ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32), ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32), ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32), ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64)]


class _RusageV0(ctypes.Structure):  # <sys/resource.h> struct rusage_info_v0
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16), ("ri_user_time", ctypes.c_uint64),
                ("ri_system_time", ctypes.c_uint64), ("ri_pkg_idle_wkups", ctypes.c_uint64),
                ("ri_interrupt_wkups", ctypes.c_uint64), ("ri_pageins", ctypes.c_uint64),
                ("ri_wired_size", ctypes.c_uint64), ("ri_resident_size", ctypes.c_uint64),
                ("ri_phys_footprint", ctypes.c_uint64), ("ri_proc_start_abstime", ctypes.c_uint64),
                ("ri_proc_exit_abstime", ctypes.c_uint64)]


def all_pids():
    """Every pid on the machine (libproc proc_listallpids; no `ps`, so no sandbox rule can blind it)."""
    size = 4096
    while True:
        buf = (ctypes.c_int * size)()
        count = _libc.proc_listallpids(buf, ctypes.sizeof(buf))
        if count <= 0:
            return []
        if count < size:
            return [p for p in buf[:count] if p > 0]
        size *= 2


def info(pid):
    """(uid, ppid, name, start seconds) or None when the process is gone or unreadable."""
    bsd = _BsdInfo()
    if _libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(bsd),
                          ctypes.sizeof(bsd)) != ctypes.sizeof(bsd):
        return None
    name = (bsd.pbi_name or bsd.pbi_comm).decode(errors="replace")
    return bsd.pbi_uid, bsd.pbi_ppid, name, bsd.pbi_start_tvsec


def running(pid):
    """True while the process exists and is not a zombie (a killed child stays a zombie until its parent reaps it;
    libproc then refuses its bsdinfo and reports a zero footprint)."""
    bsd = _BsdInfo()
    if _libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0), ctypes.byref(bsd),
                          ctypes.sizeof(bsd)) == ctypes.sizeof(bsd):
        return bsd.pbi_status != _SZOMB
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return bool(footprint(pid))


def footprint(pid):
    """Physical footprint in bytes (compressed and GPU memory included), or None when unreadable."""
    usage = _RusageV0()
    if _libc.proc_pid_rusage(pid, _RUSAGE_INFO_V0, ctypes.byref(usage)) != 0:
        return None
    return int(usage.ri_phys_footprint)


def command(pid):
    """The full command line (first 120 chars) for the log, falling back to the process name."""
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if not out.strip():
        found = info(pid)
        out = found[2] if found else "?"
    return out.strip()[:120]


def exempt(uid, name, *, me=None):
    """Only WindowServer, kernel_task and other users' processes are never killed."""
    return uid != (os.getuid() if me is None else me) or name in EXEMPT_NAMES


def parent_chain(pid, *, read=info, depth=12):
    links = []
    while pid > 0 and len(links) < depth:
        found = read(pid)
        if found is None:
            links.append(f"{pid} ?")
            break
        links.append(f"{pid} {found[2]}")
        if pid == 1:
            break
        pid = found[1]
    return " <- ".join(links)


def stop(pid, *, grace=GRACE, kill=os.kill, sleep=time.sleep, alive=running):
    """SIGTERM, then SIGKILL if it is still there after ``grace`` seconds. Returns the signal that ended it."""
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "not permitted"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        sleep(0.1)
        if not alive(pid):
            return "SIGTERM"
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    return "SIGKILL"


def gb(n):
    return f"{n / GB:.1f} GB"


class Killer:
    """One look per tick(); every input is injectable so tests can scope it to their own hog."""

    def __init__(self, *, pids=all_pids, read=info, measure=footprint, clock=time.monotonic, kill=stop,
                 log=None, hard=HARD_BYTES, floor=GROW_FLOOR_BYTES, grow=GROW_BYTES, window=GROW_WINDOW,
                 dry_run=False, me=None):
        self.pids, self.read, self.measure, self.clock, self.kill = pids, read, measure, clock, kill
        self.log = log or (lambda line: None)
        self.hard, self.floor, self.grow, self.window, self.dry_run = hard, floor, grow, window, dry_run
        self.me, self.self_pid = me, os.getpid()
        self.history = {}  # (pid, start) -> deque[(t, bytes)]

    def verdict(self, key, size, now):
        """Why this process must die, or None. Growth is judged over the last ``window`` seconds of samples."""
        samples = self.history.setdefault(key, deque())
        samples.append((now, size))
        while samples and now - samples[0][0] > self.window:
            samples.popleft()
        if size > self.hard:
            return f"footprint {gb(size)} is over the {gb(self.hard)} ceiling"
        then, before = samples[0]
        span = now - then
        if size > self.floor and span >= 1.0 and (size - before) * self.window / span > self.grow:
            return (f"footprint {gb(size)} is over {gb(self.floor)} and grew {gb(size - before)} in {span:.0f}s "
                    f"(limit {gb(self.grow)} per {self.window:.0f}s)")
        return None

    def tick(self):
        now, seen, lines = self.clock(), set(), []
        for pid in self.pids():
            if pid == self.self_pid:
                continue
            found = self.read(pid)
            if found is None:
                continue
            uid, _ppid, name, start = found
            if exempt(uid, name, me=self.me):
                continue
            size = self.measure(pid)
            if size is None:
                continue
            key = (pid, start)
            seen.add(key)
            reason = self.verdict(key, size, now)
            if reason is None:
                continue
            chain, cmd = parent_chain(pid, read=self.read), command(pid)
            how = "dry run, not killed" if self.dry_run else self.kill(pid)
            self.history.pop(key, None)
            line = f"killed pid {pid} ({how}): {reason}; command: {cmd}; parents: {chain}"
            lines.append(self.log(line) or line)
        for key in set(self.history) - seen:
            del self.history[key]
        return lines


def logger(data):
    """Write each kill to data/team/mac-hard-kill.log and one line to the inbox; always to stderr too."""
    log, inbox = Path(data) / "team/mac-hard-kill.log", Path(data) / "team/inbox.md"

    def write(line):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {line}", file=sys.stderr, flush=True)
        for path, text in ((log, f"{stamp} {line}"), (inbox, f"- {stamp[:16]} mac-hard-kill: {line}")):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as out:
                    out.write(text + "\n")
            except OSError:
                pass
        return line
    return write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", default=str(ROOT / "data"), help="the data/ folder for the log and inbox lines")
    parser.add_argument("--once", action="store_true", help="one look, then exit")
    parser.add_argument("--dry-run", action="store_true", help="log what would be killed, kill nothing")
    args = parser.parse_args(argv)
    log = (lambda line: print(line, file=sys.stderr, flush=True)) if args.dry_run else logger(args.data)
    killer = Killer(log=log, dry_run=args.dry_run)
    while True:
        try:
            for line in killer.tick():
                if args.once:
                    print(line)
        except Exception as error:  # the switch must never die on one odd process
            print(f"mac-hard-kill tick failed: {error!r}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
