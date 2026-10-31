"""Mac local-worker tests: admission, physical-memory ceiling and process-group cleanup.

The harness runs this outside the test's existing Seatbelt wall. No pytest hook or
worktree conftest is trusted to enforce these limits. Limits cannot be raised by
worker environment variables. Physical footprint includes compressed memory.
"""
from __future__ import annotations

import ctypes
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from companion import headroom

MEMORY_BYTES = 6 * 1024**3
SWAP_BYTES = 20 * 1024**3
REFUSED = 75
TICK = 0.1


class _Usage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_ubyte * 16)] + [(name, ctypes.c_uint64) for name in (
        "user", "system", "package_wakeups", "interrupt_wakeups", "pageins", "wired", "resident",
        "footprint", "start", "exit")]


def usage(pid: int) -> _Usage:
    usage = _Usage()
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    if lib.proc_pid_rusage(pid, 0, ctypes.byref(usage)) != 0:
        raise OSError(ctypes.get_errno(), f"cannot measure pid {pid}")
    return usage


def footprint(pid: int) -> int:
    measured = usage(pid)
    return max(measured.resident, measured.footprint)


def _read(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=2, check=True)
    return result.stdout


def swap_bytes() -> int:
    match = re.search(r"used\s*=\s*([\d.]+)([MGT])", _read(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
    if not match:
        raise OSError("cannot measure system swap")
    return int(float(match[1]) * 1024 ** {"M": 2, "G": 3, "T": 4}[match[2]])


def process_tree(root: int) -> dict[int, tuple[int, str]]:
    rows = {}
    for line in _read(["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat="]).splitlines():
        pid, parent, group, state = line.split(None, 3)
        rows[int(pid)] = (int(parent), int(group), state)
    selected, todo = {}, [root]
    while todo:
        pid = todo.pop()
        if pid in rows and pid not in selected:
            _, group, state = rows[pid]
            selected[pid] = (group, state)
            todo.extend(p for p, (parent, _, _) in rows.items() if parent == pid)
    return selected


def admission_refusal() -> str | None:
    probes = headroom.Probes()
    try:
        reason = (headroom.travel_refusal(headroom.main_data_dir(), probes)
                  or headroom.memory_refusal(probes))
        if reason:
            return reason
        limit = min(10, headroom.load_limit(probes))
        if probes.load() > limit:
            return f"load average {probes.load():.1f} is above {limit:g}"
        if swap_bytes() > SWAP_BYTES:
            return "system swap exceeds 20 GiB"
        footprint(os.getpid())
        process_tree(os.getpid())  # refuse before launch if process accounting is hidden
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"resource accounting unavailable: {exc}"
    return None


def stop(proc, members: dict[int, tuple[int, str]], identities: dict[int, int] | None = None) -> None:
    """Kill owned groups, including reparented sessions, without signalling recycled process IDs."""
    groups = {proc.pid}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        proc.poll()  # reap an exited leader before escalating; do not signal its zombie PID
        live = {}
        for pid, row in members.items():
            try:
                if identities is None or usage(pid).start == identities.get(pid):
                    live[pid] = row
            except OSError:
                pass
        groups.update(group for group, _ in live.values())
        groups.discard(os.getpgrp())
        # A leader can exit before the first sample. Its PGID still owns ordinary children.
        try:
            active_groups = {int(row.split()[0]) for row in _read(["/bin/ps", "-axo", "pgid=,stat="]).splitlines()
                             if not row.split()[1].startswith("Z")}
        except (OSError, ValueError, subprocess.SubprocessError):
            # Losing ps must never prevent stopping a live owned process or a known child group.
            active_groups = {group for group, _ in live.values()}
            if proc.poll() is None:
                active_groups.add(proc.pid)
        for group in groups & active_groups:
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    proc.wait(timeout=5)


def wait(proc, *, timeout: float | None = None, memory_bytes: int = MEMORY_BYTES) -> tuple[int, str]:
    """Return exit code and refusal. Test callers may lower the ceiling; production cannot raise it."""
    started, members, identities = time.monotonic(), {}, {}
    memory_bytes = min(memory_bytes, MEMORY_BYTES)
    try:
        while proc.poll() is None:
            current = process_tree(proc.pid)
            # Keep reparented descendants, but only while their kernel start identity still matches.
            for pid, row in list(members.items()):
                try:
                    if usage(pid).start == identities.get(pid):
                        current.setdefault(pid, row)
                except OSError:
                    pass
            members = current
            total = 0
            for pid, (_, state) in current.items():
                if state.startswith("Z"):
                    continue
                try:
                    identities.setdefault(pid, usage(pid).start)
                    total += footprint(pid)
                except OSError:
                    # A child may exit between ps and the accounting call. Any live unreadable child refuses.
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        continue
                    raise
            reason = ""
            if total > memory_bytes:
                reason = f"process tree uses {total // 1024**2} MiB above {memory_bytes // 1024**2} MiB ceiling"
            elif swap_bytes() > SWAP_BYTES:
                reason = "system swap exceeds 20 GiB"
            elif timeout is not None and time.monotonic() - started >= timeout:
                reason = f"test run exceeded {timeout:g} s"
            if reason:
                stop(proc, current, identities)
                return REFUSED, f"refused: {reason}; process group stopped"
            time.sleep(TICK)
        # A successful controller must not leave children running after its exit.
        stop(proc, members, identities)
        return proc.returncode, ""
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stop(proc, members, identities)
        return REFUSED, f"refused: resource accounting failed: {exc}; process group stopped"
    except BaseException:
        stop(proc, members, identities)
        raise


def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    if reason := admission_refusal():
        return REFUSED, f"refused: Mac admission: {reason}"
    # A file drains output without an unbounded in-memory PIPE buffer or pipe deadlock.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        code, reason = wait(proc, timeout=timeout)
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 6000))
        text = output.read().decode(errors="replace")
    return code, f"{reason}\n{text}" if reason else text
