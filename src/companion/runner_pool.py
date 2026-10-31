"""Runner pool: a machine registry, capability matching and duration-balanced test shards.

Pure planning, no transport. The registry is a JSON file (``data/team/runners.json``, gitignored;
``deploy/runners.example.json`` shows the shape with fake hosts). Each runner is a machine that can
run pytest: this Mac (``reach: local``), the M1 and later the Windows PC (``reach: ssh``).

Privacy (AGENTS.md rules 1, 2 and 6, held in code per rule 10): only a ``reach: local`` runner with no
``host`` or ``key`` may set ``may_see_real_data``; the loader refuses anything else, and ``can_run``
checks the flag and the reach again. Which tests need real data is owned here and fails closed:
``REAL_DATA_TESTS`` applies whatever the caller's ``Job.real_data`` says, and
``tests/test_runner_pool.py`` scans ``tests/`` so a new reader of the repo's real ``data/`` must be
listed (or exempted with a reason) before the suite passes. ``shard`` refuses duplicate paths, so an
unflagged twin cannot carry a real-data job's file to a remote runner.

TODO for the transport node (RP4/RP5), from review o-rev69: never send ``data/`` or ``.env`` to a remote
runner, never forward ``KYRA_DATA_DIR`` and refuse a remote run whose env names the real data directory;
when a runner fails, kill its process or discard its results so a reassigned shard is never counted twice.

``shard`` splits jobs (one test file each) across the online capable runners so their predicted end
times match: longest file first, each to the runner where it would finish earliest, weighted by the
runner's free cores and speed. A busy or offline runner gets no share; ``reassign`` moves a failed
runner's shard to the rest. Every job lands in exactly one shard, or ``NoRunner`` is raised: nothing
is skipped silently. A new registry entry (say ``windows-wsl``) gets work with no code change.
An optional ``skip`` list names test files that never go to that runner. ``can_run`` returns False
when the job path is in that list, and ``shard`` places the file on another runner or raises
``NoRunner``. Registries that omit ``skip`` are unchanged.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REACHES = ("local", "ssh")
_FIELDS = {"name", "os", "cores", "ram_gb", "gpu_vram_gb", "browsers", "reach", "host", "key",
           "may_see_real_data", "enabled", "speed", "skip", "max_parallel_trains", "test_kinds"}

# Test files that read the repo's real data/ (or are the privacy guards); placed only where
# may_see_real_data is true, whatever the caller flags. tests/test_runner_pool.py keeps this complete.
# test_correct_repeaters, test_intern_promotion and test_m1_host_source left on 2026-10-30 (owner): they use synthetic
# inputs only, and tests/test_hermetic_runner_tests.py fails if they open data/ or a non-temp KYRA_DATA_DIR.
REAL_DATA_TESTS = frozenset({
    "tests/test_no_target_companies.py",  # REPO_ROOT / "data", skips where absent
    "tests/test_no_third_party_pii.py",  # privacy guard (remote_test LOCAL_ONLY)
    "tests/test_no_personal_files.py",  # privacy guard (remote_test LOCAL_ONLY)
    "tests/test_home_router_holdout2.py",  # ROOT / "data" holdout files
    "tests/test_home_router_holdout3.py",  # KYRA_DATA_DIR default ROOT / "data"
    "tests/test_router_train_real.py",  # repo data/decisions/labels
})


def needs_real_data(job: Job) -> bool:
    """True when the job's flag or its file says it reads real data (fail closed)."""
    return job.real_data or job.path.split("::")[0] in REAL_DATA_TESTS


class NoRunner(ValueError):
    """A job has no online runner that can take it; the caller must refuse, never skip."""


@dataclass(frozen=True)
class Runner:
    name: str
    os: str
    cores: int
    ram_gb: float
    reach: str = "local"
    gpu_vram_gb: float = 0
    browsers: tuple[str, ...] = ()
    may_see_real_data: bool = False
    enabled: bool = True
    speed: float = 1.0  # relative per-core speed; 1.0 = this Mac
    host: str = field(default="", repr=False)  # ssh target and key path never appear in repr or logs
    key: str = field(default="", repr=False)
    skip: tuple[str, ...] = ()  # test files that never go to this runner
    max_parallel_trains: int = 1
    test_kinds: tuple[str, ...] = ("system",)


@dataclass(frozen=True)
class Job:
    path: str
    real_data: bool = False
    browser: bool = False
    gpu_vram_gb: float = 0
    cpu: int = 1


@dataclass(frozen=True)
class Load:
    """Live state from a probe. ``busy`` means the runner's lock is held: it gets no share."""
    load1: float = 0.0
    busy: bool = False


def _valid_skip_path(path: object) -> bool:
    """Repo-relative test file under tests/, forward slashes, ending in .py."""
    if not isinstance(path, str) or "\\" in path or not path.endswith(".py"):
        return False
    parts = path.split("/")
    return len(parts) >= 2 and parts[0] == "tests" and all(part not in ("", ".", "..") for part in parts)


def parse_registry(document: object) -> list[Runner]:
    """Validate a registry document ``{"runners": [...]}``; any bad entry raises ValueError."""
    if not isinstance(document, dict) or not isinstance(document.get("runners"), list):
        raise ValueError("registry must be an object with a runners list")
    runners, names = [], set()
    for index, item in enumerate(document["runners"]):
        if not isinstance(item, dict):
            raise ValueError(f"runner {index}: expected an object")
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError(f"runner {index}: name must be lowercase letters, digits and dashes")
        if name in names:
            raise ValueError(f"duplicate runner: {name}")
        names.add(name)
        unknown = set(item) - _FIELDS
        if unknown:
            raise ValueError(f"runner {name}: unknown fields {sorted(unknown)}")
        reach = item.get("reach", "local")
        if reach not in REACHES:
            raise ValueError(f"runner {name}: reach must be local or ssh")
        if item.get("may_see_real_data", False) is not False and (item["may_see_real_data"] is not True or reach != "local"):
            raise ValueError(f"runner {name}: may_see_real_data is only allowed on a local runner")
        if reach == "local" and (item.get("host") or item.get("key")):
            raise ValueError(f"runner {name}: a local runner takes no host or key")
        if reach == "ssh" and not (isinstance(item.get("host"), str) and item["host"].strip()):
            raise ValueError(f"runner {name}: an ssh runner needs a host")
        cores = item.get("cores")
        if not isinstance(cores, int) or isinstance(cores, bool) or cores < 1:
            raise ValueError(f"runner {name}: cores must be a positive integer")
        for key in ("ram_gb", "gpu_vram_gb", "speed"):
            value = item.get(key, 1.0 if key == "speed" else 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"runner {name}: {key} must be a non-negative number")
        if item.get("speed", 1.0) <= 0:
            raise ValueError(f"runner {name}: speed must be positive")
        browsers = item.get("browsers", [])
        if not isinstance(browsers, list) or not all(isinstance(b, str) for b in browsers):
            raise ValueError(f"runner {name}: browsers must be a list of names")
        if not isinstance(item.get("enabled", True), bool):
            raise ValueError(f"runner {name}: enabled must be true or false")
        skip = item.get("skip", [])
        if not isinstance(skip, list) or not all(_valid_skip_path(entry) for entry in skip):
            raise ValueError(f"runner {name}: skip must be a list of tests/ paths ending in .py")
        parallel = item.get("max_parallel_trains", 1)
        if type(parallel) is not int or parallel < 1:
            raise ValueError(f"runner {name}: max_parallel_trains must be a positive integer")
        kinds = item.get("test_kinds", ["system", *(["browser"] if browsers else [])])
        if (not isinstance(kinds, list) or not kinds
                or any(k not in ("system", "browser", "apple") for k in kinds)):
            raise ValueError(f"runner {name}: test_kinds must list system, browser or apple")
        if "apple" in kinds and item.get("os") != "macos":
            raise ValueError(f"runner {name}: apple tests require macos")
        runners.append(Runner(
            name=name, os=str(item.get("os", "")), cores=cores, ram_gb=float(item.get("ram_gb", 0)),
            reach=reach, gpu_vram_gb=float(item.get("gpu_vram_gb", 0)), browsers=tuple(browsers),
            may_see_real_data=item.get("may_see_real_data", False), enabled=item.get("enabled", True),
            speed=float(item.get("speed", 1.0)), host=str(item.get("host", "")), key=str(item.get("key", "")),
            skip=tuple(skip), max_parallel_trains=parallel, test_kinds=tuple(kinds)))
    return runners



def registry_path(data: str | Path) -> Path:
    """One machines config for placement and execution; accept the legacy path until migrated."""
    data = Path(data)
    configured = data / 'config/runners.json'
    return configured if configured.exists() else data / 'team/runners.json'

def load_registry(path: str | Path) -> list[Runner] | None:
    """Read the registry file; None when it does not exist (the caller keeps today's path)."""
    path = Path(path)
    if not path.exists():
        return None
    return parse_registry(json.loads(path.read_text(encoding="utf-8")))


def can_run(runner: Runner, job: Job) -> bool:
    """Capability match. Real data needs a local runner that may see it, checked on both fields.

    A job whose path is in ``runner.skip`` is refused, so ``shard`` places it on another runner or
    raises ``NoRunner``.
    """
    if not runner.enabled:
        return False
    if job.path in runner.skip:
        return False
    if needs_real_data(job) and not (runner.may_see_real_data and runner.reach == "local"):
        return False
    if job.browser and not runner.browsers:
        return False
    if job.gpu_vram_gb and runner.gpu_vram_gb < job.gpu_vram_gb:
        return False
    return runner.cores >= job.cpu


def free_cores(runner: Runner, load: Load | None) -> float:
    """Cores the runner can give now; offline, busy or disabled is 0."""
    if load is None or load.busy or not runner.enabled:
        return 0.0
    return max(0.0, runner.cores - max(0.0, load.load1))


def capacity(runner: Runner, load: Load | None) -> float:
    """Work per second the runner can give now: free cores times speed; offline or busy is 0."""
    return free_cores(runner, load) * runner.speed


def load_durations(path: str | Path) -> dict[str, float]:
    """Per-file core-seconds from a JSON object ``{"tests/test_x.py": 12.3}``; a missing or bad file is empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: float(v) for k, v in data.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}


_TIMED_GROUP = re.compile(r"passed in ([0-9.]+) seconds: (.+)$")


def seed_durations(log_dir: str | Path) -> dict[str, float]:
    """Per-file seconds from train logs: lines ``... passed in N seconds: file file ...`` split N evenly.

    Train logs only time whole groups (the preflight), so this is a coarse seed; the latest log wins.
    """
    seeded: dict[str, float] = {}
    for log in sorted(Path(log_dir).glob("*.log"), key=lambda p: p.stat().st_mtime):
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _TIMED_GROUP.search(line)
            if not match:
                continue
            files = [f.split("::")[0] for f in match.group(2).split() if f.startswith("tests/")]
            for f in set(files):
                seeded[f] = float(match.group(1)) / len(set(files))
    return seeded


def shard(jobs: Iterable[Job], runners: list[Runner], durations: dict[str, float],
          probe: Callable[[Runner], Load | None], *, start: dict[str, float] | None = None) -> dict[str, list[str]]:
    """Assign every job to one online capable runner so predicted end times are close.

    ``probe(runner)`` returns the live Load or None when offline. ``start`` gives seconds of work a
    runner already holds (used by ``reassign``). Unknown files get the median known duration.
    Raises NoRunner if any job has no online runner that can take it.
    """
    jobs = list(jobs)
    paths = [j.path for j in jobs]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate job paths: " + ", ".join(sorted({p for p in paths if paths.count(p) > 1})))
    caps = {r.name: capacity(r, probe(r)) for r in runners}
    live = [r for r in runners if caps[r.name] > 0]
    default = statistics.median(durations.values()) if durations else 1.0
    cost = {j.path: durations.get(j.path, default) for j in jobs}
    end = {r.name: (start or {}).get(r.name, 0.0) for r in live}
    plan: dict[str, list[str]] = {r.name: [] for r in live}
    # Most constrained first (fewest eligible runners), then longest first: LPT on related machines.
    eligible = {j.path: [r for r in live if can_run(r, j)] for j in jobs}
    for job in sorted(jobs, key=lambda j: (len(eligible[j.path]), -cost[j.path], j.path)):
        if not eligible[job.path]:
            raise NoRunner(f"no online runner can take {job.path}")
        best = min(eligible[job.path], key=lambda r: (end[r.name] + cost[job.path] / caps[r.name], r.name))
        end[best.name] += cost[job.path] / caps[best.name]
        plan[best.name].append(job.path)
    placed = [p for paths in plan.values() for p in paths]
    if sorted(placed) != sorted(j.path for j in jobs):  # post-condition: each job exactly once
        raise AssertionError("shard lost or duplicated a job")
    return {name: paths for name, paths in plan.items() if paths}


def predicted_end(plan: dict[str, list[str]], runners: list[Runner], durations: dict[str, float],
                  probe: Callable[[Runner], Load | None]) -> dict[str, float]:
    """Predicted seconds until each runner finishes its shard: core-seconds over free capacity (inf if none)."""
    default = statistics.median(durations.values()) if durations else 1.0
    by_name = {r.name: r for r in runners}
    ends = {}
    for name, paths in plan.items():
        cap = capacity(by_name[name], probe(by_name[name]))
        ends[name] = sum(durations.get(p, default) for p in paths) / cap if cap else float("inf")
    return ends


def reassign(plan: dict[str, list[str]], failed: str, jobs: Iterable[Job], runners: list[Runner],
             durations: dict[str, float], probe: Callable[[Runner], Load | None]) -> dict[str, list[str]]:
    """Move the failed runner's whole shard to the remaining runners; other shards keep their files."""
    jobs = {j.path: j for j in jobs}
    rest = [r for r in runners if r.name != failed]
    kept = {name: list(paths) for name, paths in plan.items() if name != failed}
    held = predicted_end(kept, rest, durations, probe)
    moved = shard([jobs[p] for p in plan.get(failed, [])], rest, durations, probe, start=held)
    for name, paths in moved.items():
        kept.setdefault(name, []).extend(paths)
    return kept
