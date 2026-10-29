"""Live load probe for the runner pool (node RP3): ``make_probe()`` returns the ``probe(runner) -> Load | None`` that
``runner_pool.shard`` takes.

Local runner (this Mac). ``load1`` is the 1-minute load average plus a reserve of 10% of the cores, so the pool never
plans past 0.9 x cores (the owner headroom rule), and the free part is scaled by pmset's CPU_Speed_Limit when the Mac is
thermally throttled. ``busy`` (no share) when the browser-phase lock or the gpu gate lock is live, the Mac draws from
battery, travel mode is on (only when ``companion.headroom`` exists: feature-detected, never required), memory
pressure is not normal or less than 10% is free. A memory reading that cannot be read counts as busy (fail closed).

Remote runner (``reach: ssh``). One ssh call with a short timeout runs ``uptime`` and looks for the runner lock
``<user-home>/kyra-runner/.lock`` that scripts/remote_test.py takes; ``busy`` while the lock exists. A timeout, a non-zero exit,
an ssh error or output without a load average is offline: None. Host and key come from the registry entry; for the
runner named ``m1`` they come from remote_test.py's own settings (KYRA_M1_HOST / KYRA_M1_KEY or data/config/m1.json)
when those are configured, so the M1's address is kept in one place. Host and key never appear in a returned value,
a log line or an exception: transport failures are caught and logged by runner name and failure kind only.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from companion import browser_lock, gate_lock
from companion.runner_pool import Load, Runner

log = logging.getLogger(__name__)

RESERVE = 0.1  # share of cores the pool leaves free on this Mac
MIN_FREE_PERCENT = 10
SSH_TIMEOUT = 8  # seconds for the whole remote probe; ssh's own ConnectTimeout is shorter
REMOTE_LOCK = "<user-home>/kyra-runner/.lock"  # left unquoted so ~ expands on the runner
REMOTE_COMMAND = f"uptime; if [ -e {REMOTE_LOCK} ]; then echo KYRA_LOCK=held; else echo KYRA_LOCK=free; fi"
_LOAD = re.compile(r"load averages?:\s*([0-9]+(?:[.,][0-9]+)?)")
_ROOT = Path(__file__).resolve().parents[2]


def _run(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _gpu_lock_live() -> bool:
    """Read-only check of the gpu gate lock (and the old single lock, which excludes every kind); never clears it."""
    try:
        path = gate_lock.lock_path(_ROOT, "gpu")
    except gate_lock.GateError:
        return False
    for other in gate_lock.conflicting(path):
        info = gate_lock.read(other)
        if info is not None and (info == {} or gate_lock.live(info)):  # unreadable: assume held
            return True
    return False


def _travel_on() -> bool:
    try:
        from companion import headroom  # lb-hr1; absent on older trees
    except ImportError:
        return False
    data = headroom.main_data_dir()
    return bool(data is not None and headroom.travel_state(data).get("on"))


@dataclass
class LocalSources:
    """Everything that reads this Mac, so tests can feed it states."""
    load: Callable[[], float] = field(default=lambda: os.getloadavg()[0])
    cores: Callable[[], int] = field(default=lambda: os.cpu_count() or 1)
    pressure: Callable[[], str | None] = field(default=lambda: _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]))
    memory: Callable[[], str | None] = field(default=lambda: _run(["memory_pressure", "-Q"]))
    thermal: Callable[[], str | None] = field(default=lambda: _run(["pmset", "-g", "therm"]))
    battery: Callable[[], str | None] = field(default=lambda: _run(["pmset", "-g", "batt"]))
    browser_lock: Callable[[], bool] = field(default=lambda: browser_lock.is_live())
    gpu_lock: Callable[[], bool] = field(default=lambda: _gpu_lock_live())
    travel: Callable[[], bool] = field(default=lambda: _travel_on())


def speed_limit(therm: str | None) -> int:
    """pmset's CPU_Speed_Limit in percent; 100 when no limit is recorded."""
    match = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm or "")
    return min(100, int(match[1])) if match else 100


def on_battery(batt: str | None) -> bool:
    return "'battery power'" in (batt or "").lower()


def free_percent(memory: str | None) -> int | None:
    match = re.search(r"free percentage:\s*(\d+)%", memory or "")
    return int(match[1]) if match else None


def local_reason(src: LocalSources) -> str | None:
    """Why this Mac takes no share now, or None."""
    if src.browser_lock():
        return "browser-phase lock held"
    if src.gpu_lock():
        return "gpu lock held"
    if src.travel():
        return "travel mode on"
    if on_battery(src.battery()):
        return "on battery"
    level = (src.pressure() or "").strip()
    if level != "1":
        return f"memory pressure {level or 'unreadable'}"
    free = free_percent(src.memory())
    if free is None or free < MIN_FREE_PERCENT:
        return f"free memory {'unreadable' if free is None else f'{free}%'}"
    return None


def local_load(src: LocalSources) -> Load:
    cores = src.cores()
    reason = local_reason(src)
    raw = max(0.0, src.load())
    limit = speed_limit(src.thermal())
    used = raw + RESERVE * cores
    load1 = cores - max(0.0, cores - used) * limit / 100  # throttled: the free part shrinks with the limit
    log.info("runner probe local: load %.2f, speed limit %d%%, %s", raw, limit, reason or "free")
    return Load(load1=round(load1, 3), busy=reason is not None)


def parse_remote(stdout: str) -> Load | None:
    """``uptime`` plus the KYRA_LOCK line; None when either is missing."""
    match = _LOAD.search(stdout or "")
    lock = re.search(r"^KYRA_LOCK=(held|free)\s*$", stdout or "", re.M)
    if not match or not lock:
        return None
    return Load(load1=float(match[1].replace(",", ".")), busy=lock[1] == "held")


def _remote_test_settings() -> tuple[str, str]:
    """scripts/remote_test.py's own M1 settings, loaded from that file (not copied); ('', '') when unavailable."""
    try:
        spec = importlib.util.spec_from_file_location("_kyra_remote_test", _ROOT / "scripts" / "remote_test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.runner_settings()
    except Exception:  # noqa: BLE001 - a missing or broken script means "not configured", never a leak
        return "", ""


def ssh_target(runner: Runner, m1_settings: Callable[[], tuple[str, str]] = _remote_test_settings) -> tuple[str, str]:
    if runner.name == "m1":
        host, key = m1_settings()
        if host:
            return host, key
    return runner.host, str(Path(runner.key).expanduser()) if runner.key else ""


def ssh_argv(host: str, key: str, command: str = REMOTE_COMMAND) -> list[str]:
    connect = max(1, SSH_TIMEOUT - 3)
    keyed = ["-i", key] if key else []
    return ["ssh", "-n", *keyed, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect}", host, command]


def _ssh(argv: list[str], timeout: float) -> tuple[int, str]:
    done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout or ""


def remote_load(runner: Runner, *, ssh: Callable[[list[str], float], tuple[int, str]] = _ssh,
                m1_settings: Callable[[], tuple[str, str]] = _remote_test_settings,
                timeout: float = SSH_TIMEOUT) -> Load | None:
    try:
        host, key = ssh_target(runner, m1_settings)
    except Exception as exc:  # noqa: BLE001 - settings text may name the host; only the type is kept
        log.info("runner probe %s: offline (settings %s)", runner.name, type(exc).__name__)
        return None
    if not host:
        log.info("runner probe %s: offline (no host configured)", runner.name)
        return None
    try:
        code, out = ssh(ssh_argv(host, key), timeout)
    except subprocess.TimeoutExpired:
        log.info("runner probe %s: offline (timeout after %ss)", runner.name, timeout)
        return None
    except Exception as exc:  # noqa: BLE001 - the text may name the host; only the type is kept
        log.info("runner probe %s: offline (%s)", runner.name, type(exc).__name__)
        return None
    if code != 0:
        log.info("runner probe %s: offline (ssh exit %s)", runner.name, code)
        return None
    load = parse_remote(out)
    log.info("runner probe %s: %s", runner.name,
             "offline (unreadable reply)" if load is None else f"load {load.load1:.2f}, {'busy' if load.busy else 'free'}")
    return load


def make_probe(*, local: LocalSources | None = None, ssh: Callable[[list[str], float], tuple[int, str]] = _ssh,
               m1_settings: Callable[[], tuple[str, str]] = _remote_test_settings,
               timeout: float = SSH_TIMEOUT) -> Callable[[Runner], Load | None]:
    """The probe ``runner_pool.shard`` takes: Load for a reachable runner, None when it is offline."""
    local = local or LocalSources()

    def probe(runner: Runner) -> Load | None:
        if runner.reach == "local":
            try:
                return local_load(local)
            except Exception as exc:  # noqa: BLE001 - a failing source is busy, never free and never a crash
                log.info("runner probe %s: busy (source failed: %s)", runner.name, type(exc).__name__)
                return Load(load1=float(runner.cores), busy=True)
        return remote_load(runner, ssh=ssh, m1_settings=m1_settings, timeout=timeout)

    return probe
