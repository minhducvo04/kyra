"""The landing train's non-browser test phase through the runner pool (node RP6), behind ``KYRA_RUNNER_POOL=1``.

Off by default: with the flag unset ``scripts/train.py`` runs exactly as before (preflight, guards, ruff, the
impacted tests deferred to the landing's full suite). With it set, ``train.checks`` also runs the whole non-browser
test set before landing:

- the privacy guards stay in the train's own guards step on this Mac (unchanged);
- the other real-data tests (``runner_pool.REAL_DATA_TESTS``) run as one local pytest step on this Mac, never
  through a transport, so a real-data file never leaves this Mac and its skip rules are pytest's own;
- every other non-browser file is planned with ``runner_pool.shard`` (registry ``data/team/runners.json``, or
  ``deploy/runners.example.json``'s local runner only when the registry is absent), probed with
  ``runner_probe.make_probe`` (RP3), executed with ``runner_exec.execute`` (RP5) and its per-file seconds folded
  into ``data/team/test-durations.json`` (RP2). A corrupt duration store is moved aside and rebuilt with a warning
  line: it never stops a train.

Full set: ``split`` proves guards + local + pool equal every non-browser test file, each once; ``execute`` proves
every pool file has exactly one accepted outcome. Any gap, a fail, or a transport rule breach fails the step, so the
train lands nothing. When no runner can take the set at planning time (this Mac busy, every remote down) the train
falls back to today's path and logs why. The browser phase is not touched: it stays workflow_land's.

Transports: ``LocalTransport`` runs pytest here with a junit report; ``RemoteTestTransport`` is the thin adapter to
``scripts/remote_test.py --runner NAME --ref SHA --junit PATH --files ...`` (RP4). Until RP4 offers all three in
``--help``, remote runners are left out of the plan with a log line (feature detected, never assumed). A shard
with no readable junit is "error" for every file: exit 0 alone is never a pass.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from companion import runner_durations, runner_exec, runner_pool
from companion.runner_exec import Attempt, FileResult, ShardResult
from companion.runner_pool import Load, Runner

FLAG = "KYRA_RUNNER_POOL"
DEFAULT_WORKERS = 4  # pytest-xdist workers for this Mac's shard (KYRA_RUNNER_POOL_WORKERS)
DEFAULT_DEADLINE = 3600  # seconds per attempt, the gate's own timeout (KYRA_RUNNER_POOL_DEADLINE)
KILL_GRACE = 10  # seconds between SIGTERM and SIGKILL to a killed attempt's process group
_REPO = Path(__file__).resolve().parents[2]


class PoolError(RuntimeError):
    """The pool phase cannot be trusted (bad registry, a test file outside the path grammar); the train fails."""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get(FLAG) == "1"


def shard_env(*envs: Mapping[str, str]) -> dict[str, str]:
    """The env a shard's tests run under: ``envs`` merged (default os.environ), minus GIT_*, every
    KYRA_RUNNER_POOL* key (the flag, _WORKERS, _DEADLINE) and KYRA_RUNNER_STATE. These are the train's switches,
    never the tests': RP7 (2026-10-16) failed 4 tests in tests/test_train.py on the mac shard because they saw
    KYRA_RUNNER_POOL=1, and rp7c failed one because the queue runner exports KYRA_RUNNER_STATE=free into the train."""
    merged: dict[str, str] = {}
    for env in envs or (os.environ,):
        merged.update(env)
    return {k: v for k, v in merged.items()
            if not k.startswith(("GIT_", FLAG)) and k != "KYRA_RUNNER_STATE"}


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


# ---- the test set ----------------------------------------------------------------------------------------

def test_files(worktree: Path) -> list[str]:
    """Every non-browser test file pytest would collect under tests/ (the deploy split's --ignore-glob)."""
    found = sorted(p.relative_to(worktree).as_posix() for p in (Path(worktree) / "tests").rglob("test_*.py")
                   if "__pycache__" not in p.parts)
    return [p for p in found if not re.fullmatch(r"tests/test_[^/]*_browser\.py", p)]


def split(files: Iterable[str], guards: Iterable[str]) -> tuple[list[str], list[str]]:
    """(local real-data files, pool files). Post-condition: guards + local + pool is the whole set, each once."""
    files = list(files)
    guards = set(guards)
    local = sorted(p for p in files if p not in guards and runner_pool.needs_real_data(runner_pool.Job(p)))
    pool = sorted(p for p in files if p not in guards and p not in local)
    covered = [p for p in files if p in guards] + local + pool
    if sorted(covered) != sorted(files) or len(set(covered)) != len(covered):
        raise PoolError("the guards, local and pool sets do not cover the test set exactly once")
    return local, pool


# ---- registry --------------------------------------------------------------------------------------------

def load_runners(main: Path, repo: Path, log: Callable[[str], None]) -> list[Runner]:
    """data/team/runners.json, or the example's local runner when absent. A malformed registry is a PoolError."""
    try:
        runners = runner_pool.load_registry(Path(main) / "data/team/runners.json")
        if runners is None:
            example = runner_pool.load_registry(Path(repo) / "deploy/runners.example.json") or []
            runners = [r for r in example if r.reach == "local"]
            log("runner-pool: no data/team/runners.json; this Mac only (the example registry's local runner)")
    except (OSError, ValueError) as exc:  # json errors are ValueErrors; a directory or no permission is OSError
        raise PoolError(f"runner registry refused: {exc}") from exc
    return [r for r in runners if r.enabled]


def remote_test_features(script: Path, python: str = sys.executable) -> set[str]:
    """Which RP4 options ``remote_test.py --help`` offers ({"--runner", "--files", "--junit"} subset)."""
    try:
        done = subprocess.run([python, str(script), "--help"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {flag for flag in ("--runner", "--files", "--junit") if flag in (done.stdout or "")}


# ---- results ---------------------------------------------------------------------------------------------

def junit_results(path: Path, paths: Iterable[str]) -> dict[str, FileResult]:
    """Per-file outcome and seconds for ``paths`` from a junit report. A file with a failure is "fail", with an
    error "error", otherwise "pass" (skipped cases are pytest's normal skips; real-data files never come here). A
    file the report does not name is "error": nothing missing reads as a pass. An unreadable report raises."""
    root = ET.parse(path).getroot()
    seen: dict[str, tuple[str, float]] = {}
    rank = {"pass": 0, "fail": 1, "error": 2}
    for case in root.iter("testcase"):
        name = runner_durations._file_of(case)
        if name is None:
            continue
        outcome = ("error" if case.find("error") is not None else
                   "fail" if case.find("failure") is not None else "pass")
        try:
            seconds = max(0.0, float(case.get("time") or 0.0))
        except ValueError:
            seconds = 0.0
        old, total = seen.get(name, ("pass", 0.0))
        seen[name] = (max(old, outcome, key=rank.get), total + seconds)
    out = {}
    for p in paths:
        outcome, seconds = seen.get(p, ("error", 0.0))
        if p in runner_pool.REAL_DATA_TESTS and any(
                runner_durations._file_of(c) == p and c.find("skipped") is not None for c in root.iter("testcase")):
            outcome = "skip"  # RP5 refuses it: a real-data file that skipped is never a pass
        out[p] = FileResult(outcome, seconds)
    return out


def all_error(paths: Iterable[str]) -> dict[str, FileResult]:
    """No per-file evidence (junit missing or unreadable): every file is an error, never a pass (review o-rev81)."""
    return {p: FileResult("error") for p in paths}


# ---- transports ------------------------------------------------------------------------------------------

class _Procs:
    """Live processes by attempt id, so ``kill`` can stop one from another thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}

    def add(self, attempt: Attempt, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[attempt.id] = proc

    def pop(self, attempt: Attempt) -> subprocess.Popen | None:
        with self._lock:
            return self._procs.pop(attempt.id, None)

    def stop(self, attempt: Attempt, sig: int, *, group: bool = False, grace: float = KILL_GRACE) -> None:
        """Signal the attempt; with ``group`` the whole process group (the child started in its own session), then
        SIGKILL to the group after ``grace`` so nothing a hung test started outlives the deadline (review o-rev81)."""
        proc = self.pop(attempt)
        if proc is None:
            return
        if group:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        elif proc.poll() is None:
            proc.send_signal(sig)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
        if group:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class LocalTransport:
    """pytest on this Mac in the train worktree, with xdist when present and a junit report per attempt. Each
    attempt runs in its own session, so a kill reaches every process its tests started."""

    def __init__(self, worktree: Path, *, python: str = sys.executable, workers: int | None = None,
                 env: Mapping[str, str] | None = None, popen=subprocess.Popen, log: Callable[[str], None] = print):
        self.worktree, self.python, self.popen, self.log = Path(worktree), python, popen, log
        self.workers = workers or _int_env("KYRA_RUNNER_POOL_WORKERS", DEFAULT_WORKERS)
        self.env = dict(env) if env is not None else shard_env()
        self.procs = _Procs()
        self.budgets: dict[str, int] = {}  # runner name -> worker_budget at plan time (run_pool sets it)

    def argv(self, junit: Path, paths: Iterable[str], workers: int | None = None) -> list[str]:
        workers = workers or self.workers
        xdist = ["-n", str(workers)] if importlib.util.find_spec("xdist") is not None else []
        return [self.python, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider", *xdist,
                f"--junitxml={junit}", *paths]

    def run(self, attempt: Attempt, env: Mapping[str, str]) -> ShardResult:
        with tempfile.TemporaryDirectory(prefix="kyra-pool-") as folder:
            junit = Path(folder) / "junit.xml"
            workers = self.budgets.get(attempt.runner.name)  # run_pool's plan budget; absent: self.workers
            proc = self.popen(self.argv(junit, attempt.paths, workers), cwd=self.worktree, env=shard_env(self.env, env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                              start_new_session=True)
            self.procs.add(attempt, proc)
            output, _ = proc.communicate()
            self.procs.pop(attempt)
            _log_failures(self.log, attempt, output)
            try:
                files = junit_results(junit, attempt.paths)
            except (OSError, ET.ParseError):
                files = all_error(attempt.paths)
        return ShardResult(proc.returncode, files)

    def kill(self, attempt: Attempt) -> None:
        self.procs.stop(attempt, signal.SIGTERM, group=True)


def planned_workers(runner: Runner, load: Load | None) -> int:
    """xdist workers for a runner's shard: one per whole free core the plan budgeted it (``capacity`` is free cores x
    per-core speed), at least 1. ``load`` is the probe reading the plan was made with, never a fresh one."""
    return max(1, math.floor(runner_pool.free_cores(runner, load)))  # floor: never oversubscribe


def worker_budget(runner: Runner, load: Load | None, headroom_probes=None) -> int:
    """The xdist workers a runner's shard gets, and the figure the plan credits it with: ``planned_workers``, and
    for this Mac (reach local) capped by the owner headroom rule, ``headroom.worker_cap`` (projected load at most
    0.9 x cores) and one worker when memory is short (``headroom.memory_refusal``: under 10% free or pressure)."""
    workers = planned_workers(runner, load)
    if runner.reach != "local":
        return workers
    from companion import headroom
    probes = headroom_probes if headroom_probes is not None else headroom.Probes()
    if headroom.memory_refusal(probes):
        return 1
    return headroom.worker_cap(probes, workers)


# ---- measured shard speed (rp7e, 2026-10-16) -------------------------------------------------------------
# rp7e planned 78 s for both shards and the Mac took 442 s: xdist does not turn N workers into N cores of test
# time. Each accepted shard's measured wall seconds go into the duration store under "_shards" (EMA per runner):
# seconds_per_file, and efficiency = the shard's planned core-seconds / (wall x workers). The next plan multiplies
# the runner's speed by that efficiency, so its predicted ends are the measured ones.
# rp7f (2026-10-16): the efficiency first used the shard's junit seconds. Those are the runner's own seconds (the
# store's per-file EMAs come from whichever runner ran the file), so dividing them by the runner's speed again
# counted a slow runner twice (m1: 1 / 0.6 too slow). Planned core-seconds are the plan's own figures, the same
# durations / speed the next plan divides, so efficiency = unscaled predicted end / measured wall, exactly.
SHARDS = "_shards"
MIN_SHARD_SECONDS = 10.0  # a shorter shard is mostly startup: its efficiency would mislead the next plan


def record_shard(store: Path, runner: str, planned_seconds: float, files: int, wall: float, workers: int,
                 log: Callable[[str], None] = print) -> None:
    """``planned_seconds``: the shard's core-seconds as the plan counts them (durations / the runner's registry
    speed, before efficiency), so ``planned_seconds / workers`` is the unscaled predicted end."""
    if wall < MIN_SHARD_SECONDS or files <= 0 or workers <= 0 or planned_seconds <= 0:
        return
    observed = {"efficiency": min(2.0, max(0.05, planned_seconds / (wall * workers))),
                "seconds_per_file": wall / files}
    with runner_durations._locked(Path(store)):
        try:
            data = runner_durations.load_store(store)
        except runner_durations.CorruptStore as exc:  # the duration recorder moves a corrupt store aside
            log(f"runner-pool: shard speed not recorded for {runner}: {exc}")
            return
        shards = data.get(SHARDS) if isinstance(data.get(SHARDS), dict) else {}
        old = shards.get(runner) if isinstance(shards.get(runner), dict) else {}
        alpha = runner_durations.ALPHA
        shards[runner] = {k: v if not isinstance(old.get(k), (int, float)) else alpha * v + (1 - alpha) * old[k]
                          for k, v in observed.items()} | {"workers": workers}
        data[SHARDS] = shards
        runner_durations.atomic_write(Path(store), data)


def shard_efficiency(store: Path) -> dict[str, float]:
    """runner name -> measured efficiency from the store; a missing or unreadable store is {} (efficiency 1)."""
    try:
        shards = json.loads(Path(store).read_text(encoding="utf-8")).get(SHARDS, {})
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(shards, dict):
        return {}
    return {name: float(entry["efficiency"]) for name, entry in shards.items()
            if isinstance(entry, dict) and isinstance(entry.get("efficiency"), (int, float))
            and not isinstance(entry.get("efficiency"), bool) and 0 < entry["efficiency"] <= 2.0}


@dataclass
class PoolPlan:
    shards: dict[str, list[str]]
    budgets: dict[str, int]  # runner name -> xdist workers: what the plan credits and what the shard runs
    runners: list[Runner]  # speed x measured efficiency
    probe: Callable[[Runner], Load | None]  # the plan's reading: free cores == budget


def plan_pool(files: list[str], runners: list[Runner], loads: dict[str, Load | None], store: Path,
              headroom_probes=None) -> PoolPlan:
    """The plan and the worker budgets from one reading per runner, so plan and run agree. Raises NoRunner."""
    budgets = {r.name: worker_budget(r, loads[r.name], headroom_probes) for r in runners}
    efficiency = shard_efficiency(store)
    planned_runners = [dataclasses.replace(r, speed=r.speed * efficiency.get(r.name, 1.0)) for r in runners]

    def probe(runner: Runner) -> Load | None:
        load = loads.get(runner.name)
        if load is None:
            return None
        return Load(runner.cores - budgets[runner.name], load.busy)  # free cores = the budget, never more

    shards = runner_pool.shard([runner_pool.Job(p) for p in files], planned_runners,
                               runner_pool.load_durations(store), probe)
    return PoolPlan(shards, budgets, planned_runners, probe)


class _Measured:
    """Times each attempt for ``record_shard``; the measurement is written only when execute accepts the shard
    (the duration recorder runs then, on the main thread, so one locked write holds both). ``durations`` are the
    plan's (read before this run's recorder updates them) and ``speeds`` the registry's (before efficiency)."""

    def __init__(self, inner, budgets: dict[str, int], durations: Mapping[str, float],
                 speeds: Mapping[str, float], clock=time.monotonic):
        self.inner, self.budgets, self.clock = inner, budgets, clock
        self.durations, self.speeds = dict(durations), dict(speeds)
        self.default = statistics.median(self.durations.values()) if self.durations else 1.0  # as shard() does
        self.last: dict[str, tuple[float, int, float, int]] = {}

    def planned_seconds(self, attempt: Attempt) -> float:
        speed = self.speeds.get(attempt.runner.name, 1.0)
        return sum(self.durations.get(p, self.default) for p in attempt.paths) / speed

    def run(self, attempt: Attempt, env: Mapping[str, str]) -> ShardResult:
        self.last.pop(attempt.runner.name, None)
        started = self.clock()
        result = self.inner.run(attempt, env)
        files = result.files or {}
        if result.exit_code in (0, 1) and files and all(r.outcome != "error" for r in files.values()):
            self.last[attempt.runner.name] = (self.planned_seconds(attempt), len(files),
                                              self.clock() - started, self.budgets.get(attempt.runner.name, 1))
        return result

    def kill(self, attempt: Attempt) -> None:
        self.inner.kill(attempt)


class RemoteTestTransport:
    """Thin adapter to ``remote_test.py --runner NAME --ref SHA --files ...`` (RP4). SIGINT on kill, so
    remote_test's own cleanup (runner lock release) runs. Exit 73/75/124 pass through as runner failures."""

    def __init__(self, script: Path, ref: str, *, python: str = sys.executable, features: set[str] | None = None,
                 popen=subprocess.Popen, log: Callable[[str], None] = print):
        self.script, self.ref, self.python, self.popen, self.log = Path(script), ref, python, popen, log
        self.features = remote_test_features(self.script, python) if features is None else set(features)
        self.procs = _Procs()
        self.workers: dict[str, int] = {}  # runner name -> planned_workers at plan time; absent is 1

    @property
    def available(self) -> bool:
        """--junit is required: without per-file evidence a dropped file could read as a pass (review o-rev81)."""
        return {"--runner", "--files", "--junit"} <= self.features

    def run(self, attempt: Attempt, env: Mapping[str, str]) -> ShardResult:
        if not self.available:
            return ShardResult(runner_exec.EXIT_UNREACHABLE)
        with tempfile.TemporaryDirectory(prefix="kyra-pool-") as folder:
            junit = Path(folder) / "junit.xml"
            argv = [self.python, str(self.script), "--runner", attempt.runner.name, "--ref", self.ref,
                    "--junit", str(junit), "--workers", str(self.workers.get(attempt.runner.name, 1)),
                    "--files", *attempt.paths]
            proc = self.popen(argv, env=shard_env(os.environ, env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace")
            self.procs.add(attempt, proc)
            output, _ = proc.communicate()
            self.procs.pop(attempt)
            code = proc.returncode
            if code in runner_exec.RUNNER_FAILURES:
                return ShardResult(code)
            _log_failures(self.log, attempt, output)
            try:
                files = junit_results(junit, attempt.paths)
            except (OSError, ET.ParseError):
                files = all_error(attempt.paths)
        return ShardResult(code, files)

    def kill(self, attempt: Attempt) -> None:
        self.procs.stop(attempt, signal.SIGINT)


class RoutedTransport:
    """One transport for ``execute``: local runners to the local one, ssh runners to the remote one."""

    def __init__(self, local, remote):
        self.local, self.remote = local, remote

    def _for(self, attempt: Attempt):
        return self.local if attempt.runner.reach == "local" else self.remote

    def run(self, attempt: Attempt, env: Mapping[str, str]) -> ShardResult:
        return self._for(attempt).run(attempt, env)

    def kill(self, attempt: Attempt) -> None:
        self._for(attempt).kill(attempt)


def _log_failures(log: Callable[[str], None], attempt: Attempt, output: str) -> None:
    for line in (output or "").splitlines():
        if line.startswith(("FAILED ", "ERROR ")) or re.search(r"\d+ (passed|failed|errors?)\b", line):
            log(f"runner-pool: {attempt.runner.name}: {line.strip()}")


# ---- durations (RP2, rebuild semantics) ------------------------------------------------------------------

def recorder(store: Path, log: Callable[[str], None],
             measured: _Measured | None = None) -> Callable[[dict[str, float], str], None]:
    """Fold per-file seconds into the store. A corrupt store is moved aside and rebuilt with a warning (RP2's
    ``--rebuild``), so it never stops a train; zero seconds (no junit came back) are not recorded. With
    ``measured``, the accepted shard's wall time goes in too (``record_shard``)."""
    store = Path(store)

    def record(observed: dict[str, float], runner: str) -> None:
        shard = measured.last.pop(runner, None) if measured is not None else None
        observed = {p: s for p, s in observed.items() if s > 0}
        if not observed:
            return
        with runner_durations._locked(store):
            try:
                data = runner_durations.load_store(store)
            except runner_durations.CorruptStore as exc:
                moved = store.with_name(f"{store.name}.corrupt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
                os.replace(store, moved)
                log(f"runner-pool: WARNING duration store corrupt ({exc}); moved to {moved.name}, rebuilt empty")
                data = runner_durations.load_store(store)
            runner_durations.atomic_write(store, runner_durations.update(data, observed, runner))
        if shard is not None:
            record_shard(store, runner, *shard, log=log)
    return record


# ---- the phase -------------------------------------------------------------------------------------------

def run_pool(worktree: Path, head: str, main: Path, files: list[str], log: Callable[[str], None], *,
             runners: list[Runner] | None = None, transport=None, probe=None, store: Path | None = None,
             deadline: float | None = None, remote_script: Path | None = None, headroom_probes=None) -> bool:
    """Run ``files`` through the pool. True when every file passed on the full set, or when no runner could
    take the set at planning time (today's path: the landing's full suite covers it, logged). False otherwise."""
    worktree, main = Path(worktree), Path(main)
    if not files:
        log("runner-pool: no pool files")
        return True
    runners = load_runners(main, _REPO, log) if runners is None else runners
    remote = None
    if transport is None:
        remote = RemoteTestTransport(remote_script or _REPO / "scripts/remote_test.py", head, log=log)
        if not remote.available and any(r.reach != "local" for r in runners):
            log("runner-pool: remote_test.py has no --runner/--files/--junit yet (RP4); remote runners left out")
            runners = [r for r in runners if r.reach == "local"]
        transport = RoutedTransport(LocalTransport(worktree, log=log), remote)
    if probe is None:
        from companion.runner_probe import make_probe
        probe = make_probe()
    store = Path(store) if store is not None else main / "data/team/test-durations.json"
    durations = runner_pool.load_durations(store)
    jobs = [runner_pool.Job(p) for p in files]
    loads = {r.name: probe(r) for r in runners}  # one reading per runner: the plan and the worker counts share it
    try:
        pool = plan_pool(files, runners, loads, store, headroom_probes)
    except runner_pool.NoRunner as exc:
        log(f"runner-pool: {exc}; today's path, the landing's full suite covers these files")
        return True
    plan = pool.shards
    for part in (transport, getattr(transport, "local", None), getattr(transport, "remote", None)):
        if isinstance(part, LocalTransport):
            part.budgets = dict(pool.budgets)  # -n is the plan's budget (rp7e: the plan said 15, the run used 4)
        elif isinstance(part, RemoteTestTransport):
            part.workers = dict(pool.budgets)
    ends = runner_pool.predicted_end(plan, pool.runners, durations, pool.probe)
    log("runner-pool: plan " + ", ".join(f"{name} {len(paths)} files, {pool.budgets[name]} workers "
                                         f"(predicted {ends[name]:.0f}s)" for name, paths in sorted(plan.items())))
    measured = _Measured(transport, pool.budgets, durations, {r.name: r.speed for r in runners})
    started = time.monotonic()
    try:
        report = runner_exec.execute(
            plan, jobs, pool.runners, measured, durations=durations, probe=pool.probe, env={},
            deadline=deadline or _int_env("KYRA_RUNNER_POOL_DEADLINE", DEFAULT_DEADLINE),
            recorder=recorder(store, log, measured), log=log, repo_root=worktree)
    except (runner_pool.NoRunner, runner_exec.AccountingError, runner_exec.TransportRefused) as exc:
        log(f"runner-pool: refused: {type(exc).__name__}: {exc}")
        return False
    per_runner: dict[str, int] = {}
    for _, ran_on in report.files.values():
        per_runner[ran_on] = per_runner.get(ran_on, 0) + 1
    for name, count in sorted(per_runner.items()):
        log(f"runner-pool: {name} ran {count} files")
    if sorted(report.files) != sorted(files):  # the full set, again at this level (RP5 already checks)
        log("runner-pool: refused: the report does not hold every pool file")
        return False
    bad = sorted(p for p, (outcome, _) in report.files.items() if outcome != "pass")
    for path in bad:
        log(f"runner-pool: FAILED {path} ({report.files[path][0]} on {report.files[path][1]})")
    for path, seen in sorted(report.flaky.items()):
        log(f"runner-pool: FAILED {path} in a discarded attempt ({', '.join(f'{o} {a}' for o, a in seen)})")
    for line in report.bad_exits:
        log(f"runner-pool: nonzero exit {line}")
    log(f"runner-pool: {report.verdict} on the full set of {len(files)} files in "
        f"{time.monotonic() - started:.0f}s (failed runners: {', '.join(report.failed_runners) or 'none'})")
    return report.verdict == "pass"
