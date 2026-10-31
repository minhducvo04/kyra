"""Self-capping pytest plugin: every pytest session kills itself at a hard
memory or wall-clock cap, even where no outside watchdog can see it.

Owner's order 2026-10-30, after two same-day Mac freezes (171 GB and
~400 GB) from a synthetic hog run inside an agent wall with
``KYRA_MAC_GATE=off``: a cap that lives outside the process is not a cap
for a process nobody outside can see. So the cap lives inside: at
``pytest_configure`` (in the controller and in every xdist worker) a
daemon thread starts; every 0.5 s it reads this process tree's footprint
with ``companion.resource_cap``'s tree reader (psutil USS / macOS
``proc_pid_rusage``, never ``ps``) and kills the session's process group
when the tree is over the memory cap or past the session deadline. On
Linux, ``RLIMIT_AS`` is also set (the platform that enforces it; macOS
does not, so nothing relies on it there).

Caps: ``KYRA_TEST_SESSION_MEMORY_MB`` / ``KYRA_TEST_SESSION_SECONDS`` when set to a
positive number, else ``resource_cap.caps_for`` from the RAM and cores
``companion.capability_probe`` probes. The memory figure is the whole
session's budget, divided by the xdist worker count: a worker enforces
its share on its own tree, the controller enforces the total on the
whole tree (its tree includes the workers). No variable turns this off:
zero, negative or unparsable values fall back to the computed caps, and
``KYRA_MAC_GATE`` is never read here. At start one line is printed:
``test caps: <MB> MB, <s> s`` (the limit this process enforces).

Killing: ``pytest_configure`` tries ``os.setsid()`` so the session owns
its process group where it did not already (a no-op failure when pytest
is already the group leader, e.g. launched from a shell). On a breach
the watchdog SIGTERMs the tree's descendants, then SIGKILLs them, the
process group (only when this process leads it, so a parent that shares
the group is never hit) and finally itself.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

from companion import resource_cap

try:
    import resource
except ImportError:  # non-unix runner: the monitor still applies
    resource = None

POLL_SECONDS = 0.5
_STARTED = threading.Lock()
_ACTIVE = False

__all__ = ["resolve_caps", "pytest_configure", "POLL_SECONDS"]


def _env_positive(name: str):
    """A positive float from the environment, or None (never "off")."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _worker_count(config) -> int:
    """How many pytest processes share this session's memory budget."""
    raw = os.environ.get("PYTEST_XDIST_WORKER_COUNT")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    numprocesses = getattr(getattr(config, "option", None), "numprocesses", None)
    if isinstance(numprocesses, int) and numprocesses > 0:
        return numprocesses
    if isinstance(numprocesses, str) and numprocesses not in ("", "0"):
        return max(1, os.cpu_count() or 1)  # "auto"/"logical": fail closed
    return 1


def resolve_caps(config) -> dict:
    """This process's caps: env overrides, else caps_for(probed facts).

    ``memory_mb`` is the per-process share (total budget divided by the
    worker count); ``limit_mb`` is what this process enforces on the tree
    it can see (the total for an xdist controller, the share otherwise).
    """
    env_mb = _env_positive("KYRA_TEST_SESSION_MEMORY_MB")
    env_seconds = _env_positive("KYRA_TEST_SESSION_SECONDS")
    workers = _worker_count(config)
    is_worker = hasattr(config, "workerinput")

    if env_mb is not None and env_seconds is not None:
        total_mb, seconds = int(env_mb), int(env_seconds)
    else:
        facts = {}
        if env_mb is None:
            try:
                from companion import capability_probe

                facts = capability_probe.probe(".")
            except Exception:  # noqa: BLE001 - caps_for({}) fails closed
                facts = {}
        caps = resource_cap.caps_for(facts, workers=workers, seconds=env_seconds)
        seconds = caps["seconds"]
        total_mb = int(env_mb) if env_mb is not None else caps["memory_mb"] * workers

    per_process_mb = max(1, total_mb // workers)
    limit_mb = total_mb if (not is_worker and workers > 1) else per_process_mb
    return {"memory_mb": per_process_mb, "total_memory_mb": total_mb, "limit_mb": limit_mb,
            "seconds": max(1, seconds), "workers": workers, "is_worker": is_worker}


def _set_rlimit_as(limit_bytes: int) -> None:
    """RLIMIT_AS where the OS enforces it (Linux), mirroring resource_cap."""
    if resource is None or not sys.platform.startswith("linux"):
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (OSError, ValueError, AttributeError):
        pass


def _die(reason: str, reader) -> None:
    """Kill the session: descendants, own process group when led, then self."""
    try:
        os.write(2, f"test caps: killed: {reason}\n".encode())
    except OSError:
        pass
    me = os.getpid()
    try:
        pids = resource_cap._tree_pids(me)
    except Exception:  # noqa: BLE001 - dying anyway; cached pids still help
        pids = list(getattr(reader, "pids", [me]))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            if pid != me:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if sig is signal.SIGTERM:
            time.sleep(0.3)
    try:
        if os.getpgrp() == me:  # we lead the group: it is ours to kill
            os.killpg(me, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        pass
    try:
        os.kill(me, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    os._exit(137)


def _watch(limit_bytes: int, deadline: float) -> None:
    reader = resource_cap._TreeReader(os.getpid())
    while True:
        time.sleep(POLL_SECONDS)
        if time.monotonic() >= deadline:
            _die("time cap", reader)
        footprint = reader.footprint()
        if footprint is not None and footprint > limit_bytes:
            _die("memory cap", reader)


def _announce(config, line: str) -> None:
    reporter = None
    try:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
    except Exception:  # noqa: BLE001 - fall back to stderr below
        reporter = None
    if reporter is not None:
        reporter.write_line(line)
    else:
        print(line, file=sys.stderr, flush=True)


def pytest_configure(config):
    global _ACTIVE
    with _STARTED:
        if _ACTIVE:
            return
        _ACTIVE = True

    caps = resolve_caps(config)
    config._kyra_test_caps = caps
    limit_bytes = caps["limit_mb"] * 1024 * 1024
    _set_rlimit_as(limit_bytes)
    try:
        os.setsid()  # own the process group where we do not already lead it
    except (OSError, AttributeError):
        pass
    _announce(config, f"test caps: {caps['limit_mb']} MB, {caps['seconds']} s")
    deadline = time.monotonic() + caps["seconds"]
    threading.Thread(target=_watch, args=(limit_bytes, deadline),
                     name="pytest-caps-watch", daemon=True).start()
