"""Placement by needs: assign test files to runners by their declared markers.

``plan`` is the file-level entry point on top of ``companion.test_markers``
(slice 1, PLAN.md section 1): it reads each file's markers without importing
it (a static AST scan for ``pytestmark`` and ``@pytest.mark.*`` decorators,
plus the seed map via ``seed_markers_for``), then places every file on a
runner whose ``Capabilities`` cover those markers. Runners come only from
the registry the caller passes (``{runner_id: Capabilities}``), loaded
solely through ``companion.runner_bootstrap.load_registry(registry_dir)``
so TS1's registry entry shape is the one format; this module does not read
registry files itself and names no machine, host or OS beyond what a
capability check in ``test_markers`` already does.

With no durations and no workers, placement is least-files-first. With
durations (or workers), files go longest-first onto the eligible runner
with the least predicted finish time: assigned seconds divided by the
workers that runner will actually use (``workers``, as computed by the
caller after any override), falling back to the runner's ``cores`` when
the registry's capabilities carry that field (1 otherwise). Balancing by
seconds per worker, not by file count, is what keeps a 2-worker runner
from receiving as many files as a 26-worker one and finishing last by
far. A file no runner can take raises ``Unplaceable`` naming the file,
the markers it needs and what each runner is missing; nothing is skipped.

``timing_sensitive`` is a scheduling need, not a capability (PLAN.md
section 1: "low-load / serial placement"): the probe's ``timing_safe``
flag reflects load at probe time, so on a busy machine treating it as a
capability would make every timing-sensitive file unplaceable and refuse
the whole run. Such files are placed on any runner that covers their
other markers, preferring one whose entry says ``timing_safe`` when an
eligible runner has it; the runner executes them serially (see
``scripts/test_fasttrack.py``, which gives them a final ``workers=1``
batch per runner). ``missing_capabilities`` in ``test_markers`` still
reports ``timing_sensitive`` as missing on a non-``timing_safe`` runner;
placement here deliberately ignores that one marker via
``capability_markers``.

``plan`` also takes an optional per-runner ``skip`` map (test files that
can never run on that runner, as the old runner pool registry keeps):
a skip-listed file is placed on another runner covering its markers, or
raises ``Unplaceable`` when none does.
"""
from __future__ import annotations

import ast
import math
import statistics
from collections.abc import Iterable, Mapping
from pathlib import Path

from companion.test_markers import (
    Capabilities,
    Unplaceable,
    missing_capabilities,
    seed_markers_for,
)

__all__ = [
    "DEFAULT_DURATION_SECONDS",
    "TIMING_SENSITIVE",
    "Unplaceable",
    "capability_markers",
    "declared_markers",
    "markers_for_file",
    "plan",
    "predicted_seconds",
]

#: The one marker that is a scheduling need (serial / low-load), not a capability.
TIMING_SENSITIVE = "timing_sensitive"

#: Predicted seconds for a file when no measured duration exists at all.
DEFAULT_DURATION_SECONDS = 2.0


def capability_markers(markers: Iterable[str]) -> tuple[str, ...]:
    """The markers that are real capabilities: all of them except ``timing_sensitive``."""
    return tuple(m for m in markers if m != TIMING_SENSITIVE)


def _mark_name(node: ast.expr) -> str | None:
    """The marker name in ``pytest.mark.<name>`` / ``mark.<name>`` (called or not), else None."""
    while isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return None
    base = node.value
    if isinstance(base, ast.Name) and base.id == "mark":
        return node.attr
    if isinstance(base, ast.Attribute) and base.attr == "mark":
        return node.attr
    return None


def _collect(expr: ast.expr, found: set[str]) -> None:
    name = _mark_name(expr)
    if name is not None:
        found.add(name)
    elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        for item in expr.elts:
            _collect(item, found)


def declared_markers(source: str) -> tuple[str, ...]:
    """Every ``pytest.mark.*`` name a source text declares, statically.

    Covers module/class ``pytestmark`` (single mark or list) and
    ``@pytest.mark.*`` decorators on functions and classes. The names are
    raw (``parametrize`` included); ``seed_markers_for`` filters them to the
    portability set and refuses unknown ``needs_*``/``timing_*`` names.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                _collect(decorator, found)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            _collect(node.value, found)
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and node.target.id == "pytestmark" and node.value is not None):
            _collect(node.value, found)
    return tuple(sorted(found))


def _seed_key(path: str, root: Path | None) -> str:
    """The repo-relative key ``seed_markers_for`` looks up, from the path as given."""
    given = str(path).replace("\\", "/").removeprefix("./")
    full = Path(given)
    if not full.is_absolute():
        return given
    for base in (root, Path.cwd()):
        if base is not None:
            try:
                return full.relative_to(base).as_posix()
            except ValueError:
                continue
    index = given.find("/tests/")
    return given[index + 1:] if index >= 0 else given


def markers_for_file(path: str | Path, *, root: str | Path | None = None) -> tuple[str, ...]:
    """A file's portability markers: what it declares plus its seed-map entry."""
    base = Path(root) if root is not None else None
    full = Path(path) if base is None or Path(path).is_absolute() else base / path
    try:
        source = full.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read test file {path}: {exc.strerror or exc}") from exc
    try:
        declared = declared_markers(source)
    except SyntaxError as exc:
        raise ValueError(f"cannot parse test file {path}: {exc.msg} (line {exc.lineno})") from exc
    return seed_markers_for(_seed_key(str(path), base), declared)


def _cores(caps: Capabilities) -> float:
    cores = getattr(caps, "cores", 1)
    return float(cores) if isinstance(cores, (int, float)) and not isinstance(cores, bool) \
        and math.isfinite(cores) and cores > 0 else 1.0


def _worker_counts(registry: Mapping[str, Capabilities],
                   workers: Mapping[str, float] | None) -> dict[str, float]:
    """Workers each runner will actually use: the caller's ``workers`` map
    (already after any override), else the legacy ``cores`` fallback."""
    counts = {}
    for name, caps in registry.items():
        if workers is not None and name in workers:
            value = workers[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 1:
                raise ValueError(f"workers for {name} must be a number >= 1, got {value!r}")
            counts[name] = float(value)
        else:
            counts[name] = _cores(caps)
    return counts


def _validated_durations(durations: Mapping[str, float]) -> dict[str, float]:
    clean = {}
    for path, seconds in durations.items():
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"duration for {path} must be a non-negative number, got {seconds!r}")
        clean[str(path)] = float(seconds)
    return clean


def _costs(files: list[str], durations: Mapping[str, float],
           root: str | Path | None = None) -> dict[str, float]:
    """Predicted seconds per file: its measured duration when known, else
    the median known duration, else ``DEFAULT_DURATION_SECONDS``.

    A duration keyed repo-relative (``tests/test_x.py``, as the runner
    pool stores it) also matches the same file passed by absolute path,
    via the seed-map key for that path.
    """
    known = _validated_durations(durations)
    default = float(statistics.median(known.values())) if known else DEFAULT_DURATION_SECONDS
    base = Path(root) if root is not None else None
    costs = {}
    for path in files:
        if path in known:
            costs[path] = known[path]
        else:
            costs[path] = known.get(_seed_key(path, base), default)
    return costs


def predicted_seconds(placed: Mapping[str, Iterable[str]],
                      durations: Mapping[str, float] | None = None, *,
                      workers: Mapping[str, float] | None = None,
                      root: str | Path | None = None) -> dict[str, float]:
    """Predicted finish seconds per runner: assigned seconds / workers.

    The same cost model ``plan`` balances by, so a caller can print the
    prediction a plan was made under. A runner missing from ``workers``
    counts as 1 worker.
    """
    files = [str(p) for group in placed.values() for p in group]
    cost = _costs(files, durations or {}, root=root)
    counts = workers or {}
    predicted = {}
    for name in sorted(placed):
        value = counts.get(name, 1)
        divisor = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value) and value >= 1 else 1.0
        predicted[name] = sum(cost[str(p)] for p in placed[name]) / divisor
    return predicted


def _eligible_runners(markers: tuple[str, ...],
                      registry: Mapping[str, Capabilities],
                      skipped: Iterable[str] = ()) -> tuple[str, ...]:
    """Runners covering a file's capability markers, in name order.

    ``timing_sensitive`` never makes a runner ineligible; when the file is
    timing-sensitive and an eligible runner says ``timing_safe``, only
    those runners are returned (the scheduling preference), otherwise
    every runner covering the other markers is returned. A runner in
    ``skipped`` (the file is on that runner's skip list) is never
    eligible, whatever its capabilities.
    """
    required = capability_markers(markers)
    held = set(skipped)
    eligible = tuple(name for name in sorted(registry)
                     if name not in held
                     and not missing_capabilities(required, registry[name]))
    if TIMING_SENSITIVE in markers:
        calm = tuple(name for name in eligible if registry[name].timing_safe)
        if calm:
            return calm
    return eligible


def _stuck_message(path: str, markers: tuple[str, ...], registry: Mapping[str, Capabilities],
                   skipped: Iterable[str] = ()) -> str:
    needs = ", ".join(markers) or "no markers"
    if not registry:
        return f"{path} needs {needs} (registry has no runners)"
    required = capability_markers(markers)
    held = set(skipped)
    parts = []
    for name in sorted(registry):
        missing = missing_capabilities(required, registry[name])
        if name in held and not missing:
            parts.append(f"{name} skip-listed for this file")
        elif name in held:
            parts.append(f"{name} missing {', '.join(missing)}, skip-listed for this file")
        else:
            parts.append(f"{name} missing {', '.join(missing)}")
    detail = "; ".join(parts)
    return f"{path} needs {needs} (no runner covers it: {detail})"


def _skipped_runners(path: str, skip: Mapping[str, Iterable[str]],
                     root: str | Path | None = None) -> tuple[str, ...]:
    """Runners whose skip list holds ``path``, in name order.

    A skip entry is a repo-relative ``tests/...`` path (the runner pool's
    validated shape); it matches the file as given or under its
    repo-relative seed key, so an absolute ``test_files`` entry is held
    the same way.
    """
    key = _seed_key(path, Path(root) if root is not None else None)
    held = []
    for name in sorted(skip):
        entries = {str(e).replace("\\", "/").removeprefix("./") for e in skip[name] or ()}
        if path in entries or key in entries:
            held.append(name)
    return tuple(held)


def plan(test_files: Iterable[str | Path], registry: Mapping[str, Capabilities],
         durations: Mapping[str, float] | None = None, *,
         root: str | Path | None = None,
         workers: Mapping[str, float] | None = None,
         skip: Mapping[str, Iterable[str]] | None = None) -> dict[str, list[str]]:
    """Assign every test file to one runner whose capabilities cover its markers.

    ``timing_sensitive`` is not a capability here: such a file goes to any
    runner covering its other markers, preferring a ``timing_safe`` one
    when an eligible runner says so (see ``_eligible_runners``).

    ``skip`` maps a runner name to test files that can never run there
    (a per-runner skip list, as the old runner pool registry keeps):
    such a file is placed on another runner covering its markers, and
    raises ``Unplaceable`` when no other runner covers it. Skip entries
    for runners outside ``registry`` are ignored.

    ``durations`` maps a file path (as given in ``test_files``, or its
    repo-relative ``tests/...`` key) to predicted seconds; files missing
    from it get the median known duration (``DEFAULT_DURATION_SECONDS``
    when the map is empty). Placement balances predicted seconds per
    worker: ``workers`` maps each runner to the workers it will actually
    use (after any override); a runner missing from it falls back to its
    ``cores`` capability, then 1. With neither durations nor workers,
    placement is least-files-first (the legacy behaviour). Raises
    ``Unplaceable`` naming every file no runner can take.
    """
    files = [str(f) for f in test_files]
    if len(set(files)) != len(files):
        dupes = sorted({f for f in files if files.count(f) > 1})
        raise ValueError("duplicate test files: " + ", ".join(dupes))
    markers = {f: markers_for_file(f, root=root) for f in files}
    skipped = {f: _skipped_runners(f, skip or {}, root=root) for f in files}
    eligible = {f: _eligible_runners(markers[f], registry, skipped[f]) for f in files}
    stuck = [_stuck_message(f, markers[f], registry, skipped[f])
             for f in files if not eligible[f]]
    if stuck:
        raise Unplaceable("no runner can take: " + "; ".join(stuck))
    if durations is None and workers is None:
        result = _place_by_count(files, eligible)
    else:
        result = _place_by_duration(files, eligible, registry,
                                     {str(k): v for k, v in (durations or {}).items()},
                                     workers=workers, root=root)
    placed = [p for paths in result.values() for p in paths]
    if sorted(placed) != sorted(files):  # post-condition: each file exactly once
        raise AssertionError("plan lost or duplicated a file")
    return {name: sorted(result[name]) for name in sorted(result) if result[name]}


def _place_by_count(files: list[str], eligible: Mapping[str, tuple[str, ...]],
                    ) -> dict[str, list[str]]:
    """Least-files-first placement over each file's eligible runners."""
    counts: dict[str, int] = {}
    result: dict[str, list[str]] = {}
    for path in sorted(files):
        best = min(eligible[path], key=lambda name: (counts.get(name, 0), name))
        result.setdefault(best, []).append(path)
        counts[best] = counts.get(best, 0) + 1
    return result


def _place_by_duration(files: list[str], eligible: Mapping[str, tuple[str, ...]],
                       registry: Mapping[str, Capabilities],
                       durations: Mapping[str, float], *,
                       workers: Mapping[str, float] | None = None,
                       root: str | Path | None = None) -> dict[str, list[str]]:
    """Longest first (LPT) onto the runner finishing soonest: predicted
    seconds per worker, so equal files split in proportion to workers."""
    cost = _costs(files, durations, root=root)
    counts = _worker_counts(registry, workers)
    assigned: dict[str, float] = dict.fromkeys(registry, 0.0)
    result: dict[str, list[str]] = {}
    for path in sorted(files, key=lambda f: (-cost[f], f)):  # longest first (LPT)
        best = min(eligible[path],
                   key=lambda name: ((assigned[name] + cost[path]) / counts[name], name))
        result.setdefault(best, []).append(path)
        assigned[best] += cost[path]
    return result
