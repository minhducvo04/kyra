#!/usr/bin/env python3
"""Fast-track runner: place test files on registry runners and run them all at once.

    python scripts/test_fasttrack.py --registry DIR --ref SHA [--files TEST ...]
        [--dry-run] [--junit-dir DIR] [--health-dir DIR] [--max-workers RUNNER=N ...]
        [--durations PATH]

One placement function drives everything: ``companion.test_placement.plan``
over the runners from ``companion.runner_bootstrap.load_registry`` (the one
registry format; nothing here names a machine). With no ``--files`` the plan
covers every tracked test file (``git ls-files tests/``). The raw registry
entries are also read for the probe facts ``load_registry`` does not carry
(cores, RAM, tools, packages, an ssh target when one is recorded); placement
itself still goes only through ``load_registry``.

Fix-back A2-FIX1 constraints, held in code:

1. Batching: Windows cmd.exe caps a command line at 8191 chars and
   ``scripts/remote_test.py`` takes its file list only on argv (no stdin
   mode), so no call carries more than ``MAX_FILES_PER_CALL`` (350) files.
   A runner's batches run in sequence; their junit counts are summed and
   their per-file results merged into one health record per runner.
2. Workers: per runner, ``max(1, min(cores, ram_gb // 3))`` from the
   registry facts (an 8 GB, 8-core machine gets 2), passed to remote_test.py
   as ``--workers`` (local pytest gets the xdist spelling ``-n``).
   ``--max-workers RUNNER=N`` (repeatable) overrides the computed value.
3. Browser files (``test_*_browser.py``, or a source that imports
   playwright or takes a ``page``/``browser`` fixture, mirroring
   ``companion.runner_pool.Job.browser`` detection) go only to a local
   runner whose facts list a browser tool (playwright importable, a
   chromium-family binary on PATH, a ``browsers`` list, or ``browser`` in
   ``test_kinds``); never to an ssh runner. No such runner: exit 2.
4. Reachability: before any test run, each ssh runner in the plan is probed
   through the injected runner (``ssh -o BatchMode=yes -o ConnectTimeout=8
   <registry target> true`` when the entry records a target/host, else one
   one-file remote_test.py call whose results are discarded). An
   unreachable runner is reported on stderr and every file is re-planned
   onto the remaining runners; files that then fit nowhere exit 2.
   ``--dry-run`` probes nothing and runs nothing.
5. Timing: ``timing_sensitive`` is a scheduling need, not a capability
   (PLAN.md section 1, "low-load / serial placement"). Placement puts
   such a file on any runner covering its other markers, preferring a
   ``timing_safe`` one, and ``build_commands`` gives each runner's
   timing-sensitive files a separate final batch with ``workers=1``
   (remote ``--workers 1``; local pytest without ``-n``), after that
   runner's parallel batches, still capped at ``MAX_FILES_PER_CALL``
   files per call. A busy probe (no ``timing_safe`` runner) therefore
   never makes those files unplaceable.

Fix-back A2-FIX2 constraints, held in code (first real Mac run,
2026-10-30: an 8-worker local batch died at the per-process memory cap
applied to its whole tree, two local batches ran at once, and the
killed runner still tabled 0 passed / 0 failed):

6. Batch cap: the outer ``run_capped`` cap for a local batch is the
   runner's total budget, ``workers`` x the per-process cap from
   ``resource_cap.caps_for`` (about 0.6 x RAM; see
   ``resource_cap.batch_caps_for``). Each pytest process keeps its own
   per-process cap through ``companion.pytest_caps``.
7. Local batches run one after another, never in parallel: local
   runners share this machine and its ledger allows one heavy job, so
   every local batch takes one shared lock. Remote runners still run
   in parallel with everything.
8. A batch killed by a cap counts as failed (one failure per killed
   batch) with the cap reason named on stderr; the run exits 1.

Fix-back A2-FIX3 (three-runner dry run: the 2-worker runner got as many
files as the 26-worker one, so it would finish last by far):

9. Placement balances predicted seconds per worker (the workers each
   runner will actually use, after ``--max-workers``), using measured
   per-file durations: ``--durations PATH`` when given, else the runner
   pool's durations file (``<data root>/team/test-durations.json`` via
   ``companion.runner_pool.load_durations``), else
   ``runner_pool.seed_durations`` of the train logs
   (``<data root>/train-logs``), else a default of 2 seconds per file.
   The dry run prints each runner's predicted minutes.

Fix-back FX2 (first real three-machine run: the PC shard never ran
because ``remote_test.py`` refused the whole call over files on the old
registry's hand-kept skip list for that runner):

10. The old registry's per-runner ``skip`` lists are read through
    ``companion.runner_pool.load_registry`` at its default path (the
    same file ``remote_test.py`` enforces) and passed to placement:
    a skip-listed file cannot run on that runner and moves to another
    runner covering its markers (``Unplaceable``, exit 2, when none
    does). The dry run names every requested file held this way under
    "held by the old skip list", so the hand-kept list can shrink to
    zero as markers replace it.

Each runner's commands then run through the injected subprocess runner,
in parallel across runners except that local batches never overlap (item 7
above): a remote runner (registry entry ``reach`` is not
``local``) goes through the existing ``scripts/remote_test.py --runner
<runner_id> --ref SHA --workers N --files <batch> --junit <file>``; a runner
whose entry says it is this machine (``reach == "local"``) runs pytest
locally in this checkout instead. Resource caps (RC1, after the 2026-10-30
171 GB hog that no watchdog could see): every real local pytest batch
runs through ``companion.resource_cap.run_capped`` with the batch caps
(``resource_cap.batch_caps_for``) of that runner's probed facts (the
total memory budget from RAM and workers, a wall-clock deadline), and
only that capped local pytest gets ``KYRA_MAC_GATE=off`` in
its environment. Remote runs carry ``--memory-mb``/``--seconds`` for
remote_test.py only when that script accepts them. Each runner's merged junit results are
recorded with ``companion.master_health.record`` under ``--health-dir``
(that module's data-root/health-dir argument; the default data root when
omitted), red or green, since red is valid health evidence.

Prints one table ``runner | files | passed | failed | skipped | minutes``
(counts are junit testcases summed over the runner's batches; minutes are
the summed junit seconds). Exit 0 only when every runner is green (every
batch exit code 0, no failed or errored case, a parseable junit report
recorded); 1 when any runner is red or left no usable report; 2 when
planning refuses (unplaceable files are printed, a bad registry, bad
``--max-workers``, no test files, or files left over after unreachable
runners are dropped). Standard library only.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from companion import master_health, resource_cap, runner_pool  # noqa: E402
from companion.runner_bootstrap import load_registry  # noqa: E402
from companion.test_markers import Unplaceable, missing_capabilities  # noqa: E402
from companion.test_placement import (  # noqa: E402
    capability_markers,
    markers_for_file,
    plan,
    predicted_seconds,
)

REMOTE_TEST = ROOT / "scripts" / "remote_test.py"

# Windows cmd.exe allows at most 8191 characters per command line and
# remote_test.py accepts its file list only on argv: batch the calls.
MAX_FILES_PER_CALL = 350

# remote_test.py exit codes that mean "the runner cannot take work now"
# (its EXIT_BUSY / EXIT_UNREACHABLE / EXIT_TIMEOUT), for the fallback probe.
_REMOTE_UNREACHABLE_CODES = frozenset({73, 75, 124})

_TARGET_KEYS = ("target", "ssh_target", "host", "ssh_host", "address")
_OUTCOME_RANK = {"skip": 0, "pass": 1, "fail": 2, "error": 3}


class RunnerResult:
    """One runner's outcome row. A plain class, not a dataclass: this script
    is loaded by path in tests, where dataclass annotation lookup needs the
    module registered in ``sys.modules`` first."""

    def __init__(self, runner, files):
        self.runner = runner
        self.files = list(files)
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.seconds = 0.0
        self.returncode = None
        self.error = None
        self.cap_reason = None

    @property
    def green(self) -> bool:
        return self.error is None and self.returncode == 0 and self.failed == 0


def _fact(facts, key):
    """One fact from a mapping (registry JSON) or an attribute object (a Runner)."""
    if facts is None:
        return None
    if isinstance(facts, Mapping):
        return facts.get(key)
    return getattr(facts, key, None)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def load_registry_facts(registry_dir) -> dict:
    """Raw per-runner facts from the registry entries, keyed like ``load_registry``.

    ``load_registry`` returns only the placement ``Capabilities``; cores,
    RAM, tools, packages and any recorded ssh target live in each entry's
    ``facts`` (bootstrap shape) or at its top level. Facts win over
    capabilities win over top-level keys. A malformed entry raises
    ``ValueError`` naming the file, mirroring ``load_registry``.
    """
    folder = Path(registry_dir)
    facts: dict = {}
    if not folder.is_dir():
        return facts
    for path in sorted(folder.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"bad registry entry {path.name}: {exc}") from exc
        merged: dict = {}
        if isinstance(entry, Mapping):
            merged.update({k: v for k, v in entry.items() if k not in ("facts", "capabilities")})
            caps = entry.get("capabilities")
            if isinstance(caps, Mapping):
                merged.update(caps)
            probed = entry.get("facts")
            if isinstance(probed, Mapping):
                merged.update(probed)
        facts[path.stem] = merged
    return facts


def workers_for(facts=None, override=None) -> int:
    """Pytest workers for one runner: ``max(1, min(cores, ram_gb // 3))``.

    Missing or unusable facts fail closed to 1. An override (from
    ``--max-workers RUNNER=N``) replaces the computed value, clamped to >= 1.
    """
    if override is not None:
        return max(1, int(override))
    cores = _number(_fact(facts, "cores"))
    ram_gb = _number(_fact(facts, "ram_gb"))
    by_cores = int(cores) if cores is not None and cores > 0 else 1
    by_ram = int(ram_gb // 3) if ram_gb is not None and ram_gb > 0 else 0
    return max(1, min(by_cores, by_ram))


def caps_for_runner(facts=None, override=None) -> dict:
    """Per-process resource caps for one runner: ``resource_cap.caps_for``
    of its facts, with the workers value this runner will actually run with."""
    return resource_cap.caps_for(facts or {}, workers=workers_for(facts, override))


def batch_caps_for_runner(facts=None, override=None) -> dict:
    """Batch (whole-tree) resource caps for one runner: the total budget,
    ``workers`` x the per-process cap (``resource_cap.batch_caps_for`` of
    its facts, with the workers value this runner will actually run with).
    This is the cap ``run_capped`` applies to a local batch's whole tree
    (controller plus xdist workers); the per-process cap stays inside
    each pytest process via ``companion.pytest_caps``."""
    return resource_cap.batch_caps_for(facts or {}, workers=workers_for(facts, override))


def local_caps_map(placed, registry, facts_map=None, max_workers=None) -> dict:
    """``{runner: batch caps}`` for the local runners in a plan (remote caps
    travel on the remote_test.py command line instead, when it accepts them)."""
    facts_map = facts_map or {}
    overrides = max_workers or {}
    return {runner: batch_caps_for_runner(facts_map.get(runner, {}), overrides.get(runner))
            for runner in placed if registry[runner].reach == "local"}


_REMOTE_ACCEPTS_CAPS = None


def remote_accepts_caps() -> bool:
    """remote_test.py takes ``--memory-mb``/``--seconds`` (read from its source,
    so the caps are passed only when the script on disk accepts them)."""
    global _REMOTE_ACCEPTS_CAPS
    if _REMOTE_ACCEPTS_CAPS is None:
        try:
            source = REMOTE_TEST.read_text(encoding="utf-8")
        except OSError:
            source = ""
        _REMOTE_ACCEPTS_CAPS = "--memory-mb" in source and "--seconds" in source
    return _REMOTE_ACCEPTS_CAPS


def parse_max_workers(values, registry) -> dict:
    """``RUNNER=N`` overrides as ``{runner: N}``; the last spelling of a runner wins."""
    overrides = {}
    for value in values or []:
        name, sep, number = str(value).partition("=")
        if not sep or not name:
            raise ValueError(f"--max-workers expects RUNNER=N, got {value!r}")
        if name not in registry:
            raise ValueError(f"--max-workers: unknown runner {name!r}")
        try:
            count = int(number)
        except ValueError:
            raise ValueError(f"--max-workers: {name}={number!r} is not an integer") from None
        if count < 1:
            raise ValueError(f"--max-workers: {name}={count} must be at least 1")
        overrides[name] = count
    return overrides


def workers_map(registry, facts_map=None, max_workers=None) -> dict:
    """``{runner: workers}`` each runner will actually use, after overrides."""
    facts_map = facts_map or {}
    overrides = max_workers or {}
    return {runner: workers_for(facts_map.get(runner, {}), overrides.get(runner))
            for runner in registry}


def default_durations_file() -> Path:
    """The runner pool's durations file: ``<data root>/team/test-durations.json``."""
    return master_health.default_data_root() / "team" / "test-durations.json"


def default_train_log_dir() -> Path:
    """The train logs ``runner_pool.seed_durations`` reads: ``<data root>/train-logs``."""
    return master_health.default_data_root() / "train-logs"


def resolve_durations(path=None) -> dict:
    """Measured per-file durations for placement (A2-FIX3, item 9).

    An explicit ``--durations PATH`` must exist and is read with
    ``runner_pool.load_durations``. Otherwise the runner pool's durations
    file is read the same way, falling back to
    ``runner_pool.seed_durations`` of the train logs when that file holds
    nothing. An empty result is fine: placement then predicts the default
    of 2 seconds per file (``test_placement.DEFAULT_DURATION_SECONDS``).
    """
    if path is not None:
        if not Path(path).is_file():
            raise ValueError(f"durations file not found: {path}")
        return runner_pool.load_durations(path)
    durations = runner_pool.load_durations(default_durations_file())
    if not durations:
        try:
            durations = runner_pool.seed_durations(default_train_log_dir())
        except OSError:
            durations = {}
    return durations


def _old_registry_data_dir() -> Path:
    """The data dir ``remote_test.py`` reads its registry from:
    ``KYRA_DATA_DIR``, else the main checkout's ``data/`` (from a
    worktree too), mirroring ``remote_test._data_dir`` so the skip
    lists honoured here are the ones it enforces there."""
    data_dir = os.environ.get("KYRA_DATA_DIR")
    if data_dir is None:
        try:
            done = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=5, check=True)
            common = Path(done.stdout.strip())
            if not common.is_absolute():
                common = ROOT / common
            data_dir = common.resolve().parent / "data"
        except (OSError, subprocess.SubprocessError):
            data_dir = ROOT / "data"
    return Path(data_dir)


def default_old_registry_path() -> Path:
    """The old runner pool registry's default path
    (``runner_pool.registry_path`` of the data dir above)."""
    return runner_pool.registry_path(_old_registry_data_dir())


def load_skip_lists(path=None) -> dict:
    """Per-runner skip lists from the old registry: ``{runner: (paths, ...)}``.

    Read through ``companion.runner_pool.load_registry`` at its default
    path (``path`` overrides it, for tests). A missing registry means no
    runner skips anything; a malformed one raises ``ValueError``, since
    ``remote_test.py`` would refuse on it too. Only non-empty lists are
    returned.
    """
    runners = runner_pool.load_registry(default_old_registry_path() if path is None else path)
    if not runners:
        return {}
    return {runner.name: tuple(runner.skip) for runner in runners if runner.skip}


def _skip_key(path) -> str:
    """The repo-relative ``tests/...`` key a skip entry names, from the path as given."""
    given = str(path).replace("\\", "/").removeprefix("./")
    index = given.find("/tests/")
    return given[index + 1:] if index >= 0 and not given.startswith("tests/") else given


def is_skipped_on(path, runner, skip_lists=None) -> bool:
    """The file is on ``runner``'s old skip list: it cannot run there."""
    entries = (skip_lists or {}).get(runner) or ()
    return str(path) in entries or _skip_key(path) in entries


def held_by_skip(files, registry, skip_lists=None) -> list:
    """``[(file, [runner, ...]), ...]`` for requested files held off a
    plan runner by the old skip list, in file then runner order."""
    held = []
    for path in sorted(str(f) for f in files):
        runners = [name for name in sorted(registry) if is_skipped_on(path, name, skip_lists)]
        if runners:
            held.append((path, runners))
    return held


def is_browser_file(path) -> bool:
    """Browser test detection, mirroring the runner pool's ``Job.browser`` inputs.

    A ``test_*_browser.py`` name, a playwright import, or a ``page`` /
    ``browser`` fixture argument (or ``usefixtures``/``getfixturevalue`` of
    one) in the source, read statically like ``test_shards._browser_source``.
    An unreadable file is not a browser file here: ``plan`` already failed
    loudly on it before this runs.
    """
    path = str(path)
    if Path(path).name.endswith("_browser.py"):
        return True
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "playwright" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "playwright":
                return True
        elif isinstance(node, ast.arg) and node.arg in {"page", "browser"}:
            return True
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name in {"usefixtures", "getfixturevalue"} and any(
                    isinstance(arg, ast.Constant) and arg.value in ("page", "browser")
                    for arg in node.args):
                return True
    return False


def has_browser_tool(facts) -> bool:
    """The runner's facts list a browser tool: playwright importable, a
    chromium-family binary on PATH, a ``browsers`` list, or ``browser`` in
    its ``test_kinds`` (the runner pool's browser capability spellings)."""
    browsers = _fact(facts, "browsers")
    if isinstance(browsers, (list, tuple)) and browsers:
        return True
    kinds = _fact(facts, "test_kinds")
    if isinstance(kinds, (list, tuple)) and "browser" in kinds:
        return True
    tools = _fact(facts, "tools")
    if isinstance(tools, (list, tuple)):
        names = {str(tool).lower() for tool in tools}
        if any("chrom" in name or name == "playwright" for name in names):
            return True
    packages = _fact(facts, "packages")
    if isinstance(packages, Mapping) and packages.get("playwright"):
        return True
    return bool(_fact(facts, "playwright") or _fact(facts, "chromium") or _fact(facts, "browser"))


def browser_eligible(runner, registry, facts_map=None) -> bool:
    """Browser files may go here: this machine (``reach == "local"``) with a browser tool."""
    return registry[runner].reach == "local" and has_browser_tool((facts_map or {}).get(runner, {}))


def apply_browser_rule(placed, registry, facts_map=None, workers=None, skip=None):
    """Move browser files onto local runners that list a browser tool.

    Placement (``plan``) knows only the portability markers; this
    post-condition re-homes every browser file that landed elsewhere onto
    the least-loaded eligible runner that also covers the file's markers
    and does not skip-list it (load counted per worker when ``workers``
    is given).
    A browser file with nowhere to go raises ``Unplaceable``; nothing is
    dropped and every file still lands exactly once.
    """
    eligible = [name for name in sorted(registry) if browser_eligible(name, registry, facts_map)]
    result = {runner: list(files) for runner, files in placed.items()}
    moved: list[str] = []
    for runner in sorted(result):
        if runner in eligible:
            continue
        keep = []
        for path in result[runner]:
            (moved if is_browser_file(path) else keep).append(path)
        result[runner] = keep
    counts = workers or {}

    def _load(name):
        held = len(result.get(name, []))
        return held / counts[name] if counts.get(name) else held

    stuck = []
    for path in moved:
        # timing_sensitive is a scheduling need, not a capability: it must
        # not make an otherwise eligible browser runner ineligible here.
        markers = capability_markers(markers_for_file(path))
        choices = [name for name in eligible
                   if not is_skipped_on(path, name, skip)
                   and not missing_capabilities(markers, registry[name])]
        if not choices:
            stuck.append(path)
            continue
        best = min(choices, key=lambda name: (_load(name), name))
        result.setdefault(best, []).append(path)
    if stuck:
        raise Unplaceable(
            "no local runner with a browser tool can take browser files: " + ", ".join(stuck))
    result = {runner: sorted(files) for runner, files in sorted(result.items()) if files}
    before = sorted(path for group in placed.values() for path in group)
    after = sorted(path for group in result.values() for path in group)
    if before != after:  # post-condition: each file exactly once
        raise AssertionError("browser rule lost or duplicated a file")
    return result


def default_test_files(run=subprocess.run) -> list[str]:
    """Tracked test files: the ``test_*.py`` entries of ``git ls-files tests/``."""
    done = run(["git", "ls-files", "tests/"], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError("git ls-files tests/ failed; pass --files explicitly")
    return [line for line in done.stdout.splitlines()
            if line.endswith(".py") and Path(line).name.startswith("test_")]


def _batches(files, size=MAX_FILES_PER_CALL):
    files = list(files)
    return [files[start:start + size] for start in range(0, len(files), size)] or [[]]


def is_timing_sensitive_file(path) -> bool:
    """The file declares (or is seeded) ``timing_sensitive``: serial placement."""
    return "timing_sensitive" in markers_for_file(path)


def split_timing_files(files):
    """Split one runner's files into ``(parallel, serial)``: serial is timing-sensitive."""
    parallel, serial = [], []
    for path in files:
        (serial if is_timing_sensitive_file(path) else parallel).append(path)
    return parallel, serial


def build_commands(placed, registry, *, ref, junit_dir, python=sys.executable,
                   facts=None, max_workers=None):
    """Batched commands per runner: at most ``MAX_FILES_PER_CALL`` files each.

    Returns ``{runner: [command, ...]}`` in run order. Remote runners go
    through remote_test.py with ``--workers``; the local runner's pytest gets
    ``-n`` when it may use more than one worker. A single-batch runner keeps
    the plain ``<runner>.xml`` junit name; extra batches get ``-<n>``.

    Timing-sensitive files form a separate final batch per runner with
    ``workers=1`` (remote ``--workers 1``; local pytest without ``-n``),
    after that runner's parallel batches and still capped at
    ``MAX_FILES_PER_CALL`` files per call.
    """
    junit_dir = Path(junit_dir)
    facts = facts or {}
    overrides = max_workers or {}
    commands = {}
    for runner in sorted(placed):
        workers = workers_for(facts.get(runner, {}), overrides.get(runner))
        parallel, serial = split_timing_files(placed[runner])
        groups = [(chunk, workers) for chunk in _batches(parallel)] if parallel else []
        groups += [(chunk, 1) for chunk in _batches(serial)] if serial else []
        chunks = groups or [([], workers)]
        runner_commands = []
        for index, (chunk, chunk_workers) in enumerate(chunks):
            junit = junit_dir / (f"{runner}.xml" if len(chunks) == 1 else f"{runner}-{index + 1}.xml")
            if registry[runner].reach == "local":
                command = master_health.pytest_argv(junit, chunk, python)
                if chunk_workers > 1:
                    command[3:3] = ["-n", str(chunk_workers)]
                runner_commands.append(command)
            else:
                command = [python, str(REMOTE_TEST), "--runner", runner, "--ref", ref,
                           "--workers", str(chunk_workers)]
                if remote_accepts_caps():
                    # A tree cap like the local one: the batch budget for
                    # this chunk's workers, never the per-process share.
                    caps = batch_caps_for_runner(facts.get(runner, {}), chunk_workers)
                    command += ["--memory-mb", str(caps["memory_mb"]), "--seconds", str(caps["seconds"])]
                runner_commands.append([*command, "--files", *chunk, "--junit", str(junit)])
        commands[runner] = runner_commands
    return commands


def _junit_path_of(command) -> Path:
    for i, token in enumerate(command):
        if token == "--junit":
            return Path(command[i + 1])
        if token.startswith("--junitxml="):
            return Path(token.split("=", 1)[1])
    raise ValueError(f"command carries no junit target: {command!r}")


def _junit_counts(xml_text: str) -> tuple[int, int, int, float]:
    """Testcase-level (passed, failed, skipped, seconds); an error counts as failed."""
    passed = failed = skipped = 0
    seconds = 0.0
    for case in ET.fromstring(xml_text).iter("testcase"):
        seconds += float(case.get("time") or 0.0)
        if case.find("failure") is not None or case.find("error") is not None:
            failed += 1
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1
    return passed, failed, skipped, seconds


def _exit_code(result) -> int:
    return int(result) if isinstance(result, int) else int(result.returncode)


def _default_run(argv):
    return subprocess.run(argv, check=False).returncode


def _merge_results(merged: dict, batch: dict) -> None:
    """Fold one batch's per-file results in: seconds sum, the worst outcome wins."""
    for name, entry in batch.items():
        old = merged.get(name)
        if old is None:
            merged[name] = {"outcome": entry["outcome"], "seconds": float(entry["seconds"])}
        else:
            old["seconds"] = float(old["seconds"]) + float(entry["seconds"])
            if _OUTCOME_RANK[str(entry["outcome"])] > _OUTCOME_RANK[str(old["outcome"])]:
                old["outcome"] = entry["outcome"]


def _run_runner(runner, files, commands, *, ref, health_dir, run, caps=None, lock=None) -> RunnerResult:
    """One runner's batches in sequence; counts summed, one health record.

    With ``caps`` (a local runner's batch caps: the total budget for the
    whole tree) the batch runs through ``resource_cap.run_capped``
    instead of the injected runner, and that capped local pytest is the
    only process here that gets ``KYRA_MAC_GATE=off`` (in its own
    environment, never ours). A batch killed by a cap counts as failed
    with the cap reason as the runner's error, never as a silent 0/0/0.

    ``lock`` (shared by every local runner) is held while a batch
    executes, so local batches run one after another, never in parallel:
    local runners share this machine, whose ledger allows one heavy job.
    """
    result = RunnerResult(runner, files)
    merged: dict = {}
    codes = []
    for command in commands:
        junit = _junit_path_of(command)
        try:
            with lock if lock is not None else nullcontext():
                if caps is not None:
                    code, reason = resource_cap.run_capped(
                        command, memory_mb=caps["memory_mb"], seconds=caps["seconds"],
                        env={**os.environ, "KYRA_MAC_GATE": "off"})
                    codes.append(code)
                    if reason != "ok":
                        result.cap_reason = reason
                        if reason.startswith(("memory cap", "time cap")):
                            result.error = result.error or f"resource cap: {reason}"
                            result.failed += 1  # a killed batch is a failed batch
                else:
                    codes.append(_exit_code(run(command)))
        except OSError as exc:
            result.error = f"could not start: {exc}"
            return result
        try:
            xml_text = junit.read_text(encoding="utf-8")
            _merge_results(merged, master_health.parse_junit(xml_text))
            passed, failed, skipped, seconds = _junit_counts(xml_text)
            result.passed += passed
            result.failed += failed
            result.skipped += skipped
            result.seconds += seconds
        except OSError as exc:
            result.error = result.error or f"left no readable junit report at {junit}: {exc}"
        except ValueError as exc:
            result.error = result.error or str(exc)
    result.returncode = next((code for code in codes if code != 0), 0) if codes else None
    if result.error is None:
        try:
            master_health.record(health_dir, ref, runner, merged)
        except ValueError as exc:
            result.error = str(exc)
    return result


def run_plan(placed, commands, *, ref, health_dir=None, run=None, caps=None,
             local_runners=None) -> list[RunnerResult]:
    """Run every runner in parallel (its own batches in sequence); results by runner id.

    ``caps`` is ``{runner: batch_caps_for(...)}`` for local runners; those
    batches run capped (see ``_run_runner``) instead of through ``run``.

    Local runners never run batches at the same time: they share this
    machine and its ledger allows one heavy job, so all of them execute
    under one shared lock (their batches run one after another; junit
    parsing and health recording need no lock). Remote runners run in
    parallel with everything, as before. ``local_runners`` names the
    local set; by default it is the capped runners (caps only ever
    belong to local runners, see ``local_caps_map``).
    """
    run = _default_run if run is None else run
    caps = caps or {}
    local = set(caps) if local_runners is None else set(local_runners)
    local_lock = threading.Lock()
    runners = sorted(placed)
    with ThreadPoolExecutor(max_workers=max(1, len(runners))) as pool:
        futures = {runner: pool.submit(_run_runner, runner, placed[runner], commands[runner],
                                       ref=ref, health_dir=health_dir, run=run, caps=caps.get(runner),
                                       lock=local_lock if runner in local else None)
                   for runner in runners}
        return [futures[runner].result() for runner in runners]


def probe_command(runner, facts, *, ref, files=(), junit=None, python=sys.executable):
    """The reachability probe for one ssh runner, or ``None`` when none can be built.

    An entry that records an ssh target (target/host, see ``_TARGET_KEYS``)
    gets the cheap ssh ``true``; otherwise one one-file remote_test.py call
    (the runner's first placed file, results discarded) so no host is ever
    invented here: remote_test.py resolves the runner from its own registry.
    The target itself is never printed anywhere.
    """
    for key in _TARGET_KEYS:
        target = _fact(facts, key)
        if isinstance(target, str) and target.strip():
            return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", target.strip(), "true"]
    if files and junit is not None:
        return [python, str(REMOTE_TEST), "--runner", runner, "--ref", ref,
                "--workers", "1", "--files", str(files[0]), "--junit", str(junit)]
    return None


def _probe_reachable(runner, facts, *, ref, files, junit, run, python) -> bool:
    command = probe_command(runner, facts, ref=ref, files=files, junit=junit, python=python)
    if command is None:
        return True  # nothing probeable is recorded; the real call will tell
    try:
        code = _exit_code(run(command))
    except OSError:
        return False
    if command[0] == "ssh":
        return code == 0
    return code not in _REMOTE_UNREACHABLE_CODES


def check_reachable(placed, registry, facts_map=None, *, ref, junit_dir, run=None,
                    python=sys.executable, err=None, durations=None, workers=None, skip=None):
    """Probe the plan's ssh runners; re-plan an unreachable runner's files away.

    Returns ``(placed, registry)`` narrowed to reachable runners. Each
    unreachable runner is reported on ``err`` and every file is re-planned
    (placement plus the browser rule) onto what remains; a runner that only
    receives files after a re-plan is probed before anything runs too.
    Raises ``Unplaceable`` when the remaining runners cannot take a file.
    """
    err = sys.stderr if err is None else err
    run = _default_run if run is None else run
    facts_map = facts_map or {}
    junit_dir = Path(junit_dir)
    probed_ok: set[str] = set()
    dropped: set[str] = set()
    while True:
        to_probe = [runner for runner in sorted(placed)
                    if registry[runner].reach != "local" and runner not in probed_ok]
        unreachable = [runner for runner in to_probe
                       if not _probe_reachable(runner, facts_map.get(runner, {}), ref=ref,
                                               files=placed[runner],
                                               junit=junit_dir / f"{runner}-probe.xml",
                                               run=run, python=python)]
        probed_ok.update(runner for runner in to_probe if runner not in unreachable)
        if not unreachable:
            return placed, registry
        for runner in unreachable:
            print(f"{runner}: unreachable; re-planning its {len(placed[runner])} files "
                  f"onto the other runners", file=err)
        dropped.update(unreachable)
        all_files = sorted(path for group in placed.values() for path in group)
        registry = {name: caps for name, caps in registry.items() if name not in dropped}
        placed = apply_browser_rule(
            plan(all_files, registry, durations, workers=workers, skip=skip),
            registry, facts_map, workers=workers, skip=skip)


def format_table(results) -> str:
    lines = ["runner | files | passed | failed | skipped | minutes"]
    for r in results:
        lines.append(f"{r.runner} | {len(r.files)} | {r.passed} | {r.failed} | "
                     f"{r.skipped} | {r.seconds / 60:.2f}")
    return "\n".join(lines)


def main(argv=None, *, run=None, out=None, err=None, python=sys.executable,
         skip_lists=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    run_fn = _default_run if run is None else run
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", required=True, help="registry directory (runner_bootstrap entries)")
    parser.add_argument("--ref", required=True, help="commit sha to test and record health for")
    parser.add_argument("--files", nargs="+", default=None,
                        help="test files (default: every tracked test file)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and commands, run nothing")
    parser.add_argument("--junit-dir", default=None, help="keep per-runner junit files here")
    parser.add_argument("--health-dir", default=None,
                        help="master-health data root or directory (default: the default data root)")
    parser.add_argument("--max-workers", action="append", default=[], metavar="RUNNER=N",
                        help="override one runner's computed workers (repeatable)")
    parser.add_argument("--durations", default=None, metavar="PATH",
                        help="JSON file of {test file: seconds} (default: the runner pool's "
                             "durations file, else the train-log seed, else 2 s per file)")
    args = parser.parse_args(argv)

    try:
        registry = load_registry(args.registry)
        facts_map = load_registry_facts(args.registry)
        overrides = parse_max_workers(args.max_workers, registry)
        files = args.files or default_test_files()
        if not files:
            raise ValueError("no test files to run")
        durations = resolve_durations(args.durations)
        if skip_lists is None:
            skip_lists = load_skip_lists()
        worker_counts = workers_map(registry, facts_map, overrides)
        placed = apply_browser_rule(
            plan(files, registry, durations, workers=worker_counts, skip=skip_lists),
            registry, facts_map, workers=worker_counts, skip=skip_lists)
    except Unplaceable as exc:
        print(f"unplaceable: {exc}", file=err)
        print(f"unplaceable: {exc}", file=out)
        return 2
    except (ValueError, OSError) as exc:
        print(f"test_fasttrack: {exc}", file=err)
        return 2

    if args.dry_run:
        junit_dir = Path(args.junit_dir) if args.junit_dir else \
            Path(tempfile.gettempdir()) / "kyra-test-fasttrack-junit"
        commands = build_commands(placed, registry, ref=args.ref, junit_dir=junit_dir,
                                  python=python, facts=facts_map, max_workers=overrides)
        predicted = predicted_seconds(placed, durations, workers=worker_counts)
        for runner in sorted(placed):
            print(f"{runner}:", file=out)
            print(f"  predicted minutes: {predicted.get(runner, 0.0) / 60:.2f} "
                  f"({len(placed[runner])} files, {worker_counts.get(runner, 1)} workers)", file=out)
            for path in placed[runner]:
                print(f"  {path}", file=out)
            for command in commands[runner]:
                print(f"  $ {shlex.join(command)}", file=out)
        held = held_by_skip(files, registry, skip_lists)
        if held:
            print("held by the old skip list:", file=out)
            for path, runners in held:
                print(f"  {path} (not on {', '.join(runners)})", file=out)
        return 0

    def _execute(junit_dir: Path) -> int:
        junit_dir.mkdir(parents=True, exist_ok=True)
        try:
            final_placed, active = check_reachable(
                placed, registry, facts_map, ref=args.ref, junit_dir=junit_dir,
                run=run_fn, python=python, err=err, durations=durations,
                workers=worker_counts, skip=skip_lists)
        except Unplaceable as exc:
            print(f"unplaceable: {exc}", file=err)
            print(f"unplaceable: {exc}", file=out)
            return 2
        commands = build_commands(final_placed, active, ref=args.ref, junit_dir=junit_dir,
                                  python=python, facts=facts_map, max_workers=overrides)
        # Real execution only: an injected `run` replaces execution entirely
        # (the tests' fake), so caps (and the KYRA_MAC_GATE=off that only a
        # capped local pytest may carry) apply on the real path.
        caps_map = local_caps_map(final_placed, active, facts_map, overrides) if run is None else None
        local = {runner for runner in final_placed if active[runner].reach == "local"}
        results = run_plan(final_placed, commands, ref=args.ref,
                           health_dir=args.health_dir, run=run_fn, caps=caps_map,
                           local_runners=local)
        for r in results:
            if r.error:
                print(f"{r.runner}: {r.error}", file=err)
            elif r.returncode != 0:
                print(f"{r.runner}: exited {r.returncode}", file=err)
        print(format_table(results), file=out)
        return 0 if all(r.green for r in results) else 1

    if args.junit_dir:
        return _execute(Path(args.junit_dir))
    with tempfile.TemporaryDirectory(prefix="kyra-test-fasttrack-") as folder:
        return _execute(Path(folder))


if __name__ == "__main__":
    raise SystemExit(main())
