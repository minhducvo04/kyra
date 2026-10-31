"""Hard resource caps for a child process tree, enforced by the parent.

    python -m companion.resource_cap --memory-mb N --seconds S -- CMD...

Incident 2026-10-30: a synthetic memory hog grew to 171 GB inside a
walled agent because /bin/ps is denied in the wall, so the mac guard saw
no processes and never stopped it. A watchdog that cannot see the process
is not a cap. This module is the cap that works anyway: the parent starts
the child in a new session, reads the child tree's own memory footprint
(no ``ps``: psutil ``memory_full_info().uss``, falling back to ``rss`` and,
on macOS, to ``proc_pid_rusage`` phys_footprint via ctypes), and kills the
whole process group when the tree goes over ``memory_mb`` or ``seconds``.

``RLIMIT_AS``/``RLIMIT_DATA`` are also set in a preexec where the OS
enforces them (Linux). macOS does not enforce them, so nothing here
relies on them: the parent-side monitor is the guarantee. When the
footprint cannot be read at all, the deadline still applies and the
reason says "footprint unreadable"; after exit, the children's peak RSS
from ``getrusage(RUSAGE_CHILDREN)`` is the conservative memory check.

Reason strings are "memory cap", "time cap" and "ok" (with a
"(footprint unreadable)" suffix when no footprint could ever be read).
"""
from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping

try:
    import resource
except ImportError:  # non-unix runner: the monitor still applies
    resource = None

try:
    import psutil
except ImportError:
    psutil = None

DEFAULT_SECONDS = 1800
DEFAULT_MEMORY_MB = 1024
POLL_SECONDS = 0.2
KILL_GRACE_SECONDS = 2.0

__all__ = ["caps_for", "batch_caps_for", "run_capped", "watch_process", "tree_footprint_bytes", "main",
           "DEFAULT_SECONDS", "DEFAULT_MEMORY_MB"]


def _fact(facts, *keys):
    if facts is None:
        return None
    for key in keys:
        value = facts.get(key) if isinstance(facts, Mapping) else getattr(facts, key, None)
        if value is not None:
            return value
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def caps_for(facts=None, *, workers=None, memory_mb=None, seconds=None) -> dict:
    """Caps for one runner from its probed facts (``ram_gb``, ``cores``).

    Workers are ``max(1, min(cores, ram_gb // 3))`` (an 8 GB, 8-core
    machine gets 2); memory per pytest process is
    ``ram_gb * 1024 * 0.6 / workers`` MB. Missing facts fail closed to one
    worker and ``DEFAULT_MEMORY_MB``. Every value is overridable: the
    keyword arguments win, then ``workers``/``memory_mb``/``seconds``
    recorded in the facts themselves, then the computed defaults.

    ``memory_mb`` here is the per-process share. A cap applied to a whole
    process tree (controller plus workers, as ``run_capped`` sees it) is
    the batch budget instead: see ``batch_caps_for``.
    """
    ram_gb = _number(_fact(facts, "ram_gb", "memory_gb", "mem_gb"))
    cores = _number(_fact(facts, "cores", "cpu_count", "cpus"))

    by_cores = int(cores) if cores is not None and cores > 0 else 1
    by_ram = int(ram_gb // 3) if ram_gb is not None and ram_gb > 0 else 0
    computed_workers = max(1, min(by_cores, by_ram))

    if workers is None:
        workers = _number(_fact(facts, "workers"))
    worker_count = max(1, int(workers)) if workers is not None else computed_workers

    if memory_mb is None:
        memory_mb = _number(_fact(facts, "memory_mb"))
    if memory_mb is not None:
        memory = int(memory_mb)
    elif ram_gb is not None and ram_gb > 0:
        memory = int(ram_gb * 1024 * 0.6 / worker_count)
    else:
        memory = DEFAULT_MEMORY_MB

    if seconds is None:
        seconds = _number(_fact(facts, "seconds"))
    limit_seconds = int(seconds) if seconds is not None else DEFAULT_SECONDS

    return {"memory_mb": max(1, memory), "seconds": max(1, limit_seconds), "workers": worker_count}


def batch_caps_for(facts=None, *, workers=None, memory_mb=None, seconds=None) -> dict:
    """Caps for one whole batch: a process tree, not one pytest process.

    ``run_capped`` measures the summed footprint of the controller plus
    its pytest workers, so the batch cap is the runner's total budget:
    ``workers`` times the per-process ``memory_mb`` from ``caps_for``
    (about 0.6 x RAM when derived from ``ram_gb``). Applying the
    per-process share to the whole tree killed an 8-worker local batch
    at 2,764 MB on a 36 GB machine (2026-10-30, A2/RC1). Each pytest
    process keeps its own per-process cap through
    ``companion.pytest_caps``. ``seconds`` and ``workers`` pass through.
    """
    caps = caps_for(facts, workers=workers, memory_mb=memory_mb, seconds=seconds)
    return {"memory_mb": caps["memory_mb"] * caps["workers"],
            "seconds": caps["seconds"], "workers": caps["workers"]}


# ---- footprint reading (never ps) ---------------------------------------------------------------

_LIBPROC = None


def _macos_footprint_bytes(pid: int):
    """``phys_footprint`` for one pid from ``proc_pid_rusage``, or None.

    rusage_info starts with a 16-byte uuid then uint64 fields; resident
    size is field 7 and phys_footprint field 8 (offset 72) in every
    version, so a flavor-0 call into a large buffer is enough.
    """
    global _LIBPROC
    if sys.platform != "darwin":
        return None
    try:
        if _LIBPROC is None:
            _LIBPROC = ctypes.CDLL("libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(4096)
        if _LIBPROC.proc_pid_rusage(int(pid), 0, buffer) != 0:
            return None
        return int(ctypes.c_uint64.from_buffer(buffer, 72).value)
    except (OSError, ValueError, AttributeError):
        return None


def _pid_footprint_bytes(pid: int):
    if psutil is not None:
        try:
            info = psutil.Process(pid).memory_full_info()
            return int(getattr(info, "uss", None) or info.rss)
        except (psutil.Error, OSError, AttributeError, ValueError):
            pass
    return _macos_footprint_bytes(pid)


def _tree_pids(root_pid: int) -> list[int]:
    if psutil is None:
        return [root_pid]
    try:
        root = psutil.Process(root_pid)
        return [root_pid] + [child.pid for child in root.children(recursive=True)]
    except (psutil.Error, OSError, ValueError):
        return [root_pid]


def _sum_footprint(pids):
    total = 0
    read_any = False
    for pid in pids:
        value = _pid_footprint_bytes(pid)
        if value is not None:
            total += value
            read_any = True
    return total if read_any else None


def tree_footprint_bytes(root_pid: int):
    """Summed footprint of a process tree in bytes, or None when unreadable."""
    return _sum_footprint(_tree_pids(root_pid))


class _TreeReader:
    """Polling footprint reader for one tree. Summing known pids is fast;
    discovering descendants scans every process on the machine (seconds
    under heavy load), so the pid list refreshes at most once a second
    and starts as just the root (a newborn tree has no children yet)."""

    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self.pids = [root_pid]
        self._refreshed = time.monotonic()

    def footprint(self):
        now = time.monotonic()
        if now - self._refreshed >= 1.0:
            self.pids = _tree_pids(self.root_pid)
            self._refreshed = now
        return _sum_footprint(self.pids)


# ---- the monitor --------------------------------------------------------------------------------

class _WatchState:
    def __init__(self):
        self.reason = None
        self.readable = False


def _kill_process_group(proc) -> None:
    """SIGTERM the child's whole group, then SIGKILL after the grace period."""
    def _signal_group(sig):
        try:
            os.killpg(proc.pid, sig)  # new session: the group id is the child's pid
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    if proc.poll() is not None:
        return
    _signal_group(signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        _signal_group(signal.SIGKILL)


def _watch(proc, limit_bytes, deadline, state: _WatchState) -> None:
    # Probe the mechanism on ourselves first: a child that exits before the
    # first poll must not be misreported as "footprint unreadable" when
    # footprint reading works fine.
    if _pid_footprint_bytes(os.getpid()) is not None:
        state.readable = True
    reader = _TreeReader(proc.pid)
    while proc.poll() is None:
        if deadline is not None and time.monotonic() >= deadline:
            state.reason = "time cap"
            _kill_process_group(proc)
            return
        if limit_bytes is not None:
            footprint = reader.footprint()
            if footprint is not None:
                state.readable = True
                if footprint > limit_bytes and proc.poll() is None:
                    state.reason = "memory cap"
                    _kill_process_group(proc)
                    return
        time.sleep(POLL_SECONDS)


def watch_process(proc, memory_mb=None, seconds=None):
    """Start the cap monitor for an already-started ``proc`` (same monitor
    ``run_capped`` uses). Returns ``(thread, state)``; the thread ends when
    the process exits, and ``state.reason`` names the cap it enforced."""
    limit_bytes = int(memory_mb * 1024 * 1024) if memory_mb else None
    deadline = time.monotonic() + seconds if seconds else None
    state = _WatchState()
    thread = threading.Thread(target=_watch, args=(proc, limit_bytes, deadline, state),
                              name="resource-cap-watch", daemon=True)
    thread.start()
    return thread, state


# ---- running a command under the caps -----------------------------------------------------------

def _linux_preexec(limit_bytes, caller_preexec):
    def _preexec():
        if caller_preexec is not None:
            caller_preexec()
        if resource is None or not limit_bytes:
            return
        for name in ("RLIMIT_AS", "RLIMIT_DATA"):
            limit = getattr(resource, name, None)
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, (limit_bytes, limit_bytes))
            except (OSError, ValueError):
                pass

    return _preexec


def _children_peak_bytes():
    """Peak RSS of terminated children; ru_maxrss is bytes on macOS, KiB on Linux."""
    if resource is None:
        return None
    try:
        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (OSError, ValueError):
        return None
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def run_capped(argv, memory_mb=None, seconds=None, **popen_kwargs):
    """Run ``argv`` under hard caps; return ``(returncode, reason)``.

    The child starts in a new session. On Linux a preexec sets
    ``RLIMIT_AS`` and ``RLIMIT_DATA`` to the memory cap (macOS does not
    enforce them, so they are not set there and never relied on). A
    parent-side monitor thread reads the tree footprint every 0.2 s and
    kills the whole process group (SIGTERM, then SIGKILL after 2 s) over
    ``memory_mb`` or ``seconds``. When no footprint could ever be read,
    the deadline still applies, the children's peak RSS is checked after
    exit, and the reason carries "(footprint unreadable)". A ``None``
    cap means that dimension is not enforced. Extra keyword arguments go
    to ``subprocess.Popen`` (``start_new_session`` is forced on).
    """
    limit_bytes = int(memory_mb * 1024 * 1024) if memory_mb else None
    popen_kwargs.pop("start_new_session", None)
    caller_preexec = popen_kwargs.pop("preexec_fn", None)
    if sys.platform.startswith("linux"):
        popen_kwargs["preexec_fn"] = _linux_preexec(limit_bytes, caller_preexec)
    elif caller_preexec is not None:
        popen_kwargs["preexec_fn"] = caller_preexec

    proc = subprocess.Popen(list(argv), start_new_session=True, **popen_kwargs)
    thread, state = watch_process(proc, memory_mb=memory_mb, seconds=seconds)
    try:
        if popen_kwargs.get("stdout") is subprocess.PIPE or popen_kwargs.get("stderr") is subprocess.PIPE:
            proc.communicate()  # drain the pipes the caller asked for; output is not returned
        else:
            proc.wait()
    finally:
        thread.join(timeout=KILL_GRACE_SECONDS + 5)
    returncode = int(proc.returncode)

    reason = state.reason
    if not state.readable:
        if reason is None:
            peak = _children_peak_bytes()
            reason = "memory cap" if (limit_bytes and peak is not None and peak > limit_bytes) else "ok"
        return returncode, f"{reason} (footprint unreadable)"
    return returncode, reason or "ok"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" in args:
        cut = args.index("--")
        own, command = args[:cut], args[cut + 1:]
    else:
        own, command = args, []
    parser = argparse.ArgumentParser(
        prog="python -m companion.resource_cap",
        description="Run a command under hard memory and time caps: "
                    "--memory-mb N --seconds S -- CMD...")
    parser.add_argument("--memory-mb", type=float, default=None, help="tree memory cap in MB")
    parser.add_argument("--seconds", type=float, default=None, help="wall-clock cap in seconds")
    parsed = parser.parse_args(own)
    if not command:
        parser.error("no command given after --")
    try:
        returncode, reason = run_capped(command, memory_mb=parsed.memory_mb, seconds=parsed.seconds)
    except OSError as exc:
        print(f"resource_cap: could not start {command[0]!r}: {exc}", file=sys.stderr)
        return 127
    if reason != "ok":
        print(f"resource_cap: {reason}", file=sys.stderr)
    return returncode if returncode >= 0 else 128 + (-returncode)


if __name__ == "__main__":
    raise SystemExit(main())
