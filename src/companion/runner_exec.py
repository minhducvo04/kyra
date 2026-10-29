"""Runner pool executor: run a shard plan through an injected transport, with re-placement and full-set accounting.

``runner_pool.shard`` plans which runner takes which test file; this module runs that plan. The transport is
injected (``Transport``): the real ssh/rsync one belongs to ``scripts/remote_test.py`` (RP4); tests use fakes.

Rules held in code (AGENTS.md rules 1, 2, 6 and 10; the RP1 review o-rev69 TODO):

- A file runs only where ``runner_pool.can_run`` allows: real-data tests only on the local runner that may see
  real data. Checked again right before every dispatch, not only at planning.
- Paths are an allowlist (review o-rev77): only a relative ``tests/(<dir>/)*test_<name>.py`` with an optional
  restricted ``::node`` id, case folded, resolving inside ``<repo>/tests`` with no symlink escape, for every runner.
  So no data/ or .env path, option (``--basetemp=``, ``-c``) or home path reaches a transport. The env given to a
  non-local runner is an allowlist of names (``REMOTE_ENV_NAMES``: never ``KYRA_DATA_DIR``, never a key), and a
  value may not name the real data dir of this worktree or the main checkout (resolved, case folded), a data or
  .env segment, ``~`` or ``%``. A breach raises ``TransportRefused`` before anything is sent.
- A runner that ends with 73 (busy), 75 (offline or no transport), 124 (timed out), raises, or passes its
  deadline is out for the rest of the run: its attempt is killed, its partial results are discarded, and its
  whole shard is re-placed with ``runner_pool.reassign`` on the remaining runners. Results that arrive later
  from a dead attempt are discarded too, so no file is counted twice.
- Full-set accounting: the report lists every planned file exactly once with pass, fail or error. An accepted
  shard that reports a file it was not given, or misses one, or a final union that differs from the plan,
  raises ``AccountingError``; a run is never a pass with a file missing. Skip rule (Board, review o-rev82): on a
  remote runner skip and xfail count as pass, as pytest counts them, and a REAL_DATA_TESTS or guard file that
  shows up there is "error" (they are never dispatched remotely). Locally, a skipped REAL_DATA_TESTS file is
  reported as "skip", which is refused here, so it never reads as a pass. A fail or error seen in a discarded result, or an
  accepted shard's nonzero exit, makes the verdict "fail" (``Report.flaky``, ``Report.bad_exits``).

Each finished attempt prints one timing line (runner, files, seconds, exit, accepted or discarded). Per-file
seconds from accepted shards go to ``recorder`` (``store_recorder`` wraps RP2's duration store).
"""

from __future__ import annotations

import functools
import math
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from companion import runner_durations, runner_pool
from companion.runner_pool import Job, Load, Runner

EXIT_BUSY = 73
EXIT_UNREACHABLE = 75
EXIT_TIMEOUT = 124
RUNNER_FAILURES = frozenset({EXIT_BUSY, EXIT_UNREACHABLE, EXIT_TIMEOUT})
OUTCOMES = ("pass", "fail", "error")
_REPO = Path(__file__).resolve().parents[2]


class TransportRefused(RuntimeError):
    """A dispatch would break a transport rule (data/ or .env path, real-data env, real data off the Mac)."""


class AccountingError(RuntimeError):
    """The executed files do not match the planned set exactly once; the run is a refusal, never a pass."""


@dataclass(frozen=True)
class FileResult:
    outcome: str  # pass | fail | error
    seconds: float = 0.0


@dataclass(frozen=True)
class ShardResult:
    """What a transport returns for one attempt: the exit code and the per-file results it has (maybe partial)."""
    exit_code: int
    files: Mapping[str, FileResult] = field(default_factory=dict)


@dataclass(frozen=True)
class Attempt:
    id: str  # "<runner>#<n>", unique per run
    runner: Runner
    paths: tuple[str, ...]


class Transport(Protocol):
    def run(self, attempt: Attempt, env: Mapping[str, str]) -> ShardResult: ...
    def kill(self, attempt: Attempt) -> None: ...


@dataclass
class Report:
    verdict: str  # pass | fail
    files: dict[str, tuple[str, str]]  # path -> (outcome, runner that ran it)
    timings: list[str]
    failed_runners: list[str]
    discarded: list[str]  # attempt ids whose results were thrown away
    flaky: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # path -> [(fail|error, attempt id)]
    bad_exits: list[str] = field(default_factory=list)  # accepted attempts that exited nonzero


# ---- transport rules (allowlists, review o-rev77) --------------------------------------------------------

# A test path is a relative tests/(<segment>/)*test_<name>.py, optionally ::<node id> parts made of word
# characters, dashes and brackets (no "/", "." or ".." after ::). ASCII only, so look-alike letters fail.
# Matched on the case-folded path, because this Mac's volume is case-insensitive.
_TEST_PATH = re.compile(r"tests/(?:[a-z0-9_]+/)*test_[a-z0-9_]+\.py(?:::[a-z0-9_\-\[\]]+)*", re.ASCII)
# The only env names a remote runner may receive; every other name is refused (never KYRA_DATA_DIR, never a key).
REMOTE_ENV_NAMES = frozenset({"PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "TZ", "LANG",
                              "LC_ALL", "CI"})
_SEGMENTS = re.compile(r"[/\\=:\s,;]+")
_CHUNKS = re.compile(r"[=:\s,;]+")


@functools.cache
def _main_checkout() -> Path:
    """The main checkout (the git common dir's parent): from a worktree this is where the owner real data/ lives."""
    try:
        out = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        return Path(out).resolve().parent
    except (OSError, subprocess.SubprocessError):
        for parent in (_REPO, *_REPO.parents):
            if (parent / ".git").is_dir():
                return parent
        return _REPO


def real_data_dirs(extra: Path | None = None) -> list[Path]:
    """The real data/ of this worktree and of the main checkout (plus ``extra``), resolved."""
    dirs = [_REPO / "data", _main_checkout() / "data"] + ([extra] if extra is not None else [])
    return [d.resolve() for d in dirs]


def _under(path: Path, roots: Iterable[Path]) -> bool:
    p = str(path).casefold()
    return any(p == str(r).casefold() or p.startswith(str(r).casefold().rstrip("/") + "/") for r in roots)


def _allowed_path(path: str, repo_root: Path) -> bool:
    """Allowlist: the test path grammar (case folded), then the file must resolve inside <repo>/tests."""
    if not isinstance(path, str) or not path.isascii() or not _TEST_PATH.fullmatch(path.casefold()):
        return False
    tests = (repo_root / "tests").resolve()
    target = (repo_root / path.split("::")[0]).resolve()  # follows any symlink on the way
    return _under(target, [tests]) and str(target).casefold() != str(tests).casefold()


def _allowed_env(name: str, value: str, real_dirs: list[Path]) -> bool:
    """Allowlisted name; the value holds no ~, %, data or .env* segment, and no path into a real data dir."""
    if name not in REMOTE_ENV_NAMES or not isinstance(value, str):
        return False
    if "~" in value or "%" in value:
        return False
    if any(t == "data" or t.startswith(".env") for t in _SEGMENTS.split(value.casefold())):
        return False
    for chunk in _CHUNKS.split(value):
        if chunk and _under(Path(chunk).resolve() if chunk.startswith("/") else (_REPO / chunk).resolve(),
                            real_dirs):
            return False
    return True


def check_dispatch(attempt: Attempt, env: Mapping[str, str], jobs: Mapping[str, Job], *,
                   real_data_dir: Path | None = None, repo_root: Path = _REPO) -> None:
    """Raise TransportRefused unless the attempt is safe to hand to the transport.

    Paths (every runner, the Mac too): only allowlisted test paths that stay under <repo>/tests, and only where
    ``runner_pool.can_run`` allows. Env (non-local runners): only ``REMOTE_ENV_NAMES``, with values that name no
    real data dir (this worktree's or the main checkout's, resolved and case folded) and no .env.
    """
    for path in attempt.paths:
        if not _allowed_path(path, repo_root):
            raise TransportRefused(f"{attempt.id}: {path!r} is not an allowed test path")
        if not runner_pool.can_run(attempt.runner, jobs[path]):
            raise TransportRefused(f"{attempt.id}: {path} may not run on {attempt.runner.name}")
    if attempt.runner.reach == "local":
        return
    real_dirs = real_data_dirs(real_data_dir)
    for name, value in env.items():
        if not _allowed_env(name, value, real_dirs):
            raise TransportRefused(f"{attempt.id}: env {name} may not go to a remote runner")


# ---- accounting ------------------------------------------------------------------------------------------

def _accepts(attempt: Attempt, result: ShardResult) -> bool:
    """A finished attempt counts only if it did not end as a runner failure (its whole shard is re-placed)."""
    return result.exit_code not in RUNNER_FAILURES


def _is_late(attempt: Attempt, dead: set[str]) -> bool:
    """An attempt whose runner was already failed (deadline, kill, re-placement): its results are discarded."""
    return attempt.runner.name in dead


def _check_shard(attempt: Attempt, result: ShardResult) -> None:
    got, want = set(result.files), set(attempt.paths)
    if got != want:
        raise AccountingError(f"{attempt.id}: reported {sorted(got - want)} extra, {sorted(want - got)} missing")
    bad = sorted(p for p, r in result.files.items() if r.outcome not in OUTCOMES)
    if bad:
        raise AccountingError(f"{attempt.id}: unknown outcome for {bad}")
    bad = sorted(p for p, r in result.files.items() if not _is_seconds(r.seconds))
    if bad:  # a NaN, inf or negative would poison RP2's duration store
        raise AccountingError(f"{attempt.id}: bad seconds for {bad}")


def _is_seconds(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _failures(attempt: Attempt, result: ShardResult | None) -> list[tuple[str, str, str]]:
    """Fail/error outcomes inside a discarded result: kept so a re-run pass cannot hide them."""
    files = result.files if result is not None else {}
    return [(p, r.outcome, attempt.id) for p, r in files.items()
            if p in attempt.paths and getattr(r, "outcome", None) in ("fail", "error")]


def account(planned: Iterable[str], accepted: list[tuple[Attempt, ShardResult]]) -> dict[str, tuple[str, str]]:
    """Every planned file exactly once across the accepted shards, or AccountingError."""
    planned = list(planned)
    out: dict[str, tuple[str, str]] = {}
    for attempt, result in accepted:
        for path, res in result.files.items():
            if path in out:
                raise AccountingError(f"{path} counted twice ({out[path][1]} and {attempt.runner.name})")
            out[path] = (res.outcome, attempt.runner.name)
    if len(set(planned)) != len(planned) or set(out) != set(planned):
        raise AccountingError(f"executed set differs from plan: missing {sorted(set(planned) - set(out))}, "
                              f"extra {sorted(set(out) - set(planned))}")
    return out


# ---- durations -------------------------------------------------------------------------------------------

def store_recorder(store_path: str | Path | None = None) -> Callable[[dict[str, float], str], None]:
    """A recorder that folds per-file seconds into RP2's EMA store (default ``data/team/test-durations.json``)."""
    def record(observed: dict[str, float], runner: str) -> None:
        if not runner_durations._RUNNER_NAME.match(runner):
            raise ValueError(f"bad runner name {runner!r}")
        path = Path(store_path) if store_path is not None else runner_durations.default_store()
        with runner_durations._locked(path):
            store = runner_durations.load_store(path)
            runner_durations.atomic_write(path, runner_durations.update(store, observed, runner))
    return record


# ---- the executor ----------------------------------------------------------------------------------------

def execute(plan: dict[str, list[str]], jobs: Iterable[Job], runners: list[Runner], transport: Transport, *,
            durations: dict[str, float], probe: Callable[[Runner], Load | None],
            env: Mapping[str, str] | None = None, deadline: float | None = None,
            recorder: Callable[[dict[str, float], str], None] | None = None,
            log: Callable[[str], None] = print, real_data_dir: Path | None = None,
            repo_root: Path = _REPO) -> Report:
    """Run ``plan`` (runner name -> files, from ``runner_pool.shard``) and return the full-set report.

    One attempt per runner at a time (a runner's lock allows one run); runners work in parallel. ``deadline``
    is seconds per attempt; past it the attempt is killed and treated as 124. The verdict is "pass" only if
    every file passed, no accepted shard exited nonzero and no discarded result held a fail or error. Raises ``NoRunner`` when a
    failed shard has nowhere to go, ``TransportRefused`` on a rule breach, ``AccountingError`` on a gap.
    """
    jobs = {j.path: j for j in jobs}
    planned = [p for paths in plan.values() for p in paths]
    if sorted(planned) != sorted(jobs):
        raise AccountingError("plan does not hold every job exactly once")
    env = dict(env or {})
    by_name = {r.name: r for r in runners}
    if set(plan) - set(by_name):
        raise ValueError(f"plan names unknown runners {sorted(set(plan) - set(by_name))}")
    plan = {name: list(paths) for name, paths in plan.items()}
    queues: dict[str, list[tuple[str, ...]]] = {name: [tuple(paths)] for name, paths in plan.items() if paths}
    dead: set[str] = set()
    live: dict[Future, tuple[Attempt, float]] = {}
    busy: set[str] = set()
    accepted: list[tuple[Attempt, ShardResult]] = []
    timings: list[str] = []
    discarded: list[str] = []
    flaky: dict[str, list[tuple[str, str]]] = {}
    bad_exits: list[str] = []
    counter = 0

    def keep_failures(attempt: Attempt, result: ShardResult | None) -> None:
        for path, outcome, attempt_id in _failures(attempt, result):
            flaky.setdefault(path, []).append((outcome, attempt_id))

    def outcome_of(fut: Future) -> ShardResult | None:
        try:
            return fut.result()
        except Exception:
            return None

    def line(attempt: Attempt, started: float, code: object, state: str) -> None:
        text = (f"runner-pool: {attempt.runner.name} {attempt.id} {len(attempt.paths)} files "
                f"{time.monotonic() - started:.1f}s exit {code} {state}")
        timings.append(text)
        log(text)

    def fail_runner(attempt: Attempt) -> None:
        nonlocal plan
        name = attempt.runner.name
        dead.add(name)
        queues.pop(name, None)
        try:
            transport.kill(attempt)
        except Exception as exc:  # the attempt is already dead to us; its results are discarded either way
            log(f"runner-pool: kill {attempt.id} failed: {exc}")
        rest = [r for r in runners if r.name not in dead]
        before = {n: list(p) for n, p in plan.items() if n != name}
        done_here = {p for a, _ in accepted if a.runner.name == name for p in a.paths}
        unfinished = [p for p in plan.get(name, []) if p not in done_here]  # accepted files stay counted
        plan = runner_pool.reassign({**plan, name: unfinished}, name, jobs.values(), rest, durations, probe)
        for target, paths in plan.items():
            moved = tuple(p for p in paths if p not in before.get(target, []))
            if moved:
                queues.setdefault(target, []).append(moved)

    pool = ThreadPoolExecutor(max_workers=max(1, len(runners)))
    try:
        while True:
            for name in list(queues):
                if name in busy or name in dead or not queues[name]:
                    continue
                counter += 1
                attempt = Attempt(f"{name}#{counter}", by_name[name], queues[name].pop(0))
                check_dispatch(attempt, env, jobs, real_data_dir=real_data_dir, repo_root=repo_root)
                busy.add(name)
                live[pool.submit(transport.run, attempt, dict(env))] = (attempt, time.monotonic())
            active = [f for f, (a, _) in live.items() if a.runner.name not in dead]
            if not active:
                break
            done, _ = wait(list(live), timeout=0.05, return_when=FIRST_COMPLETED)
            for fut in done:
                attempt, started = live.pop(fut)
                name = attempt.runner.name
                if _is_late(attempt, dead):  # result from a runner already failed: never counted
                    if attempt.id not in discarded:
                        discarded.append(attempt.id)
                    keep_failures(attempt, outcome_of(fut))
                    line(attempt, started, "late", "discarded")
                    continue
                busy.discard(name)
                try:
                    result = fut.result()
                except Exception as exc:
                    result = ShardResult(exit_code=EXIT_UNREACHABLE)
                    log(f"runner-pool: {attempt.id} transport error: {exc}")
                if not _accepts(attempt, result):
                    discarded.append(attempt.id)
                    keep_failures(attempt, result)
                    line(attempt, started, result.exit_code, "discarded, shard re-placed")
                    fail_runner(attempt)
                    continue
                _check_shard(attempt, result)
                accepted.append((attempt, result))
                if result.exit_code != 0:
                    bad_exits.append(f"{attempt.id} exit {result.exit_code}")
                line(attempt, started, result.exit_code, "accepted")
                if recorder is not None:
                    try:  # a duration store problem never stops a run (RP2)
                        recorder({p: r.seconds for p, r in result.files.items()}, name)
                    except Exception as exc:
                        log(f"runner-pool: durations not recorded for {attempt.id}: {exc}")
            if deadline is not None:
                now = time.monotonic()
                for attempt, started in list(live.values()):
                    if attempt.runner.name not in dead and now - started > deadline:
                        discarded.append(attempt.id)
                        line(attempt, started, EXIT_TIMEOUT, "deadline, shard re-placed")
                        busy.discard(attempt.runner.name)
                        fail_runner(attempt)
    finally:
        for fut, (attempt, _) in live.items():
            if not fut.done() and attempt.runner.name not in dead:
                try:
                    transport.kill(attempt)
                except Exception as exc:
                    log(f"runner-pool: kill {attempt.id} failed: {exc}")
        pool.shutdown(wait=False, cancel_futures=True)

    files = account(sorted(jobs), accepted)
    clean = all(outcome == "pass" for outcome, _ in files.values()) and not flaky and not bad_exits
    return Report("pass" if clean else "fail", files, timings, sorted(dead), discarded, flaky, bad_exits)
