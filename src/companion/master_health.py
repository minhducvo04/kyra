"""Per-runner master health files: record one clean-master run, read the verdict.

PLAN.md section 4: every runner writes one JSON file per master sha under
``<data root>/master-health/``, holding pass/fail/skip/error and seconds per
test file. ``status`` folds the files for one sha across the expected runners
into green/red/missing. A runner with no readable file for the sha is
missing, never green; an empty or corrupt file is missing too (no evidence
is not a pass, mirroring ``train_pool``). A file is red on ``fail`` or
``error``; ``pass`` and ``skip`` are not red, so a file that legitimately
skips on a runner whose capabilities it does not need (see
``test_markers``) does not block that runner. ``table_status`` widens the
same files into one row per registry runner (plus any runner that has a
health file), marking a runner whose newest evidence is for a different
sha ``stale``.

Universal (no machine, host or OS names here): runner ids and the data root
come from the caller, the environment or ``companion.paths``. Standard
library only; every external call (the pytest run) is injected.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "master-health/1"
OUTCOMES = ("pass", "fail", "skip", "error")
RED_OUTCOMES = ("fail", "error")
_RANK = {"skip": 0, "pass": 1, "fail": 2, "error": 3}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TEST_MODULE = re.compile(r"^(test_\w+|\w+_test)$")
_HEALTH_NAME = "master-health"

# Injected pytest runner: takes the argv and the junit path to write, returns an exit code.
Runner = Callable[[list[str], Path], int]


def default_data_root() -> Path:
    """The data root: ``KYRA_DATA_DIR`` when set, else ``companion.paths.DATA_DIR``."""
    env = os.environ.get("KYRA_DATA_DIR")
    if env:
        return Path(env)
    from companion.paths import DATA_DIR

    return Path(DATA_DIR)


def health_dir(path: str | Path | None = None) -> Path:
    """The health directory for a data root (``<data root>/master-health``).

    Passing the health directory itself (named ``master-health``) returns it
    unchanged, so ``record``/``status`` accept either form.
    """
    base = Path(path) if path is not None else default_data_root()
    return base if base.name == _HEALTH_NAME else base / _HEALTH_NAME


def health_file(path: str | Path | None, sha: str, runner_id: str) -> Path:
    """The file one runner's results for one sha live in."""
    return health_dir(path) / f"{sha}--{runner_id}.json"


def _check_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f"bad {kind} {value!r}: expected [A-Za-z0-9][A-Za-z0-9._-]* (no path separators)")
    return value


def validate_results(results: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, float | str]]:
    """Normalised ``{test_file: {"outcome": ..., "seconds": float}}``, or ValueError naming the bad entry."""
    if not isinstance(results, Mapping) or not results:
        raise ValueError("results must be a non-empty {test_file: {outcome, seconds}} mapping")
    out: dict[str, dict[str, float | str]] = {}
    for name, entry in results.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"bad test file name {name!r}")
        if not isinstance(entry, Mapping):
            raise ValueError(f"result for {name!r} is not an object: {entry!r}")
        outcome = entry.get("outcome")
        if outcome not in OUTCOMES:
            raise ValueError(f"result for {name!r} has bad outcome {outcome!r}; expected one of {OUTCOMES}")
        seconds = entry.get("seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
                or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"result for {name!r} has bad seconds {seconds!r}")
        out[name.replace("\\", "/")] = {"outcome": str(outcome), "seconds": float(seconds)}
    return out


def record(path: str | Path | None, sha: str, runner_id: str,
           results: Mapping[str, Mapping[str, object]]) -> Path:
    """Write one runner's results for ``sha`` atomically; return the file written.

    ``path`` is the data root (or the ``master-health`` directory itself, or
    ``None`` for the default data root). Validation runs before anything is
    written, and the write goes through a sibling temp file plus
    ``os.replace``, so a failure leaves neither a partial new file nor a
    clobbered old one.
    """
    _check_id("sha", sha)
    _check_id("runner id", runner_id)
    clean = validate_results(results)
    target = health_file(path, sha, runner_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "sha": sha, "runner": runner_id, "results": clean}
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return target


def _load_entry(path: Path, sha: str, runner_id: str) -> dict | None:
    """One validated health file as ``{"results", "recorded_at"}``, or ``None`` when it counts as missing.

    ``recorded_at`` is the file's own ``recorded_at`` string when it carries
    one, else its mtime as an ISO UTC string: ``record`` writes no timestamp,
    so the mtime is the only record time a file from this module has.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA \
            or data.get("sha") != sha or data.get("runner") != runner_id:
        return None
    try:
        results = validate_results(data.get("results"))  # type: ignore[arg-type]
    except ValueError:
        return None
    recorded_at = data.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at:
        try:
            recorded_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        except OSError:
            recorded_at = None
    return {"results": results, "recorded_at": recorded_at}


def _load(path: Path, sha: str, runner_id: str) -> dict[str, dict[str, float | str]] | None:
    """One runner's validated results, or ``None`` when the file counts as missing."""
    entry = _load_entry(path, sha, runner_id)
    return entry["results"] if entry is not None else None


def status(dir: str | Path | None, sha: str, runners: Iterable[str]) -> dict:  # noqa: A002 - brief's name
    """``{"green": bool, "red_files": {...}, "missing": [...]}`` for one sha.

    ``red_files`` maps each red test file to ``{"outcome", "seconds",
    "runner"}`` for its worst result (error over fail) across runners.
    Green needs at least one expected runner, none missing and no red file.
    """
    _check_id("sha", sha)
    expected = list(dict.fromkeys(runners))
    missing: list[str] = []
    red: dict[str, dict[str, float | str]] = {}
    for runner_id in expected:
        _check_id("runner id", runner_id)
        results = _load(health_file(dir, sha, runner_id), sha, runner_id)
        if results is None:
            missing.append(runner_id)
            continue
        for name, entry in sorted(results.items()):
            if entry["outcome"] not in RED_OUTCOMES:
                continue
            old = red.get(name)
            if old is None or _RANK[str(entry["outcome"])] > _RANK[str(old["outcome"])]:
                red[name] = {"outcome": entry["outcome"], "seconds": entry["seconds"], "runner": runner_id}
    return {"green": bool(expected) and not missing and not red, "red_files": red, "missing": missing}


def discover_runners(path: str | Path | None, sha: str) -> list[str]:
    """Runner ids that have a health file for ``sha`` in the directory, sorted."""
    _check_id("sha", sha)
    folder = health_dir(path)
    found = []
    for entry in sorted(folder.glob(f"{sha}--*.json")) if folder.is_dir() else []:
        runner_id = entry.name[len(sha) + 2:-len(".json")]
        if _ID.match(runner_id):
            found.append(runner_id)
    return found


# Injected git runner: takes the argv, returns stdout or ``None`` on failure.
GitRun = Callable[[list[str]], str | None]


def _git_stdout(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def default_sha(run: GitRun | None = None) -> str:
    """The sha a table covers when none is named: ``git rev-parse origin/master``."""
    out = (run if run is not None else _git_stdout)(["git", "rev-parse", "origin/master"])
    sha = out.strip() if out else ""
    if not sha:
        raise ValueError("cannot resolve origin/master (git rev-parse failed); pass --sha")
    return _check_id("sha", sha)


def health_files_by_runner(path: str | Path | None) -> dict[str, list[tuple[str, Path]]]:
    """Every health file in the directory as ``{runner: [(sha, file), ...]}``, sorted by name.

    A file counts by its ``<sha>--<runner>.json`` name even when its content
    later proves unreadable: the runner still gets a (missing) table row.
    """
    folder = health_dir(path)
    found: dict[str, list[tuple[str, Path]]] = {}
    for entry in sorted(folder.glob("*--*.json")) if folder.is_dir() else []:
        sha, sep, runner_id = entry.name[:-len(".json")].partition("--")
        if sep and _ID.match(sha) and _ID.match(runner_id):
            found.setdefault(runner_id, []).append((sha, entry))
    return found


TABLE_STATES = ("green", "red", "missing", "stale")


def _os_of(entry: object) -> str | None:
    """The OS name from a registry entry: ``Capabilities``, a mapping or a plain string."""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, Mapping):
        caps = entry.get("capabilities")
        value = caps.get("os") if isinstance(caps, Mapping) else entry.get("os")
    else:
        value = getattr(entry, "os", None)
    return str(value) if value else None


def _counts(results: Mapping[str, Mapping[str, object]]) -> tuple[int, int, int]:
    """(passed, failed, skipped) file counts; ``error`` folds into failed so the three sum to the total."""
    passed = sum(1 for r in results.values() if r["outcome"] == "pass")
    failed = sum(1 for r in results.values() if r["outcome"] in RED_OUTCOMES)
    skipped = sum(1 for r in results.values() if r["outcome"] == "skip")
    return passed, failed, skipped


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def table_status(path: str | Path | None, sha: str, registry: Mapping[str, object]) -> dict:
    """One row per registry runner plus any runner that has a health file, for ``sha``.

    ``registry`` is ``companion.runner_bootstrap.load_registry`` output
    (``{runner_id: Capabilities}``); the ``os`` column comes from it and is
    ``None`` for a runner known only from a health file. A row is ``green``
    or ``red`` from its validated file for ``sha``; with no such file but a
    validated file for a different sha it is ``stale`` (the newest such file
    fills the row); otherwise ``missing``. Returns ``{"sha", "green",
    "verdict", "rows"}`` where rows are sorted by runner and carry
    ``runner``, ``os``, ``sha``, ``passed``, ``failed``, ``skipped``,
    ``state``, ``recorded_at`` and ``registered``. Green (and the verdict)
    consider registered runners only: green needs at least one and every
    one of them green; a missing or stale registered runner outranks a red
    one, mirroring the CLI exit codes 0 green, 1 red, 2 missing.
    """
    _check_id("sha", sha)
    files = health_files_by_runner(path)
    rows = []
    for runner_id in sorted(set(registry) | set(files)):
        _check_id("runner id", runner_id)
        row: dict[str, object] = {
            "runner": runner_id, "os": _os_of(registry.get(runner_id)), "sha": None,
            "passed": None, "failed": None, "skipped": None, "state": "missing",
            "recorded_at": None, "registered": runner_id in registry,
        }
        current = _load_entry(health_file(path, sha, runner_id), sha, runner_id)
        if current is not None:
            row["sha"] = sha
            row["passed"], row["failed"], row["skipped"] = _counts(current["results"])
            row["state"] = "red" if row["failed"] else "green"
            row["recorded_at"] = current["recorded_at"]
        else:
            stale = [(s, f) for s, f in files.get(runner_id, []) if s != sha]
            for stale_sha, stale_file in sorted(stale, key=lambda sf: (_mtime(sf[1]), sf[1].name), reverse=True):
                entry = _load_entry(stale_file, stale_sha, runner_id)
                if entry is None:
                    continue
                row["sha"] = stale_sha
                row["passed"], row["failed"], row["skipped"] = _counts(entry["results"])
                row["state"] = "stale"
                row["recorded_at"] = entry["recorded_at"]
                break
        rows.append(row)
    registered = [r["state"] for r in rows if r["registered"]]
    green = bool(registered) and all(state == "green" for state in registered)
    verdict = ("green" if green else
               "missing" if not registered or any(s in ("missing", "stale") for s in registered) else "red")
    return {"sha": sha, "green": green, "verdict": verdict, "rows": rows}


_TABLE_COLUMNS = ("runner", "os", "sha", "passed", "failed", "skipped", "state", "recorded_at")
_TABLE_HEADS = ("runner", "os", "sha", "passed", "failed", "skipped", "state", "recorded at")


def format_table(report: Mapping[str, object]) -> str:
    """``table_status`` output as aligned ``|``-separated text; the last line is the verdict.

    Unknown values render as ``-``. The last line is exactly ``GREEN``
    only when every registered runner is green, else ``RED`` or ``MISSING``.
    """
    rows = report["rows"]  # type: ignore[assignment]
    cells = [[("-" if row[key] is None else str(row[key])) for key in _TABLE_COLUMNS]  # type: ignore[index]
             for row in rows]  # type: ignore[union-attr]
    widths = [max([len(head), *(len(line[i]) for line in cells)]) for i, head in enumerate(_TABLE_HEADS)]

    def render(values: Sequence[str]) -> str:
        return " | ".join(value.ljust(widths[i]) for i, value in enumerate(values)).rstrip()

    lines = [render(_TABLE_HEADS), *(render(line) for line in cells), str(report["verdict"]).upper()]
    return "\n".join(lines)


def _file_of(case: ET.Element) -> str:
    """The test file a testcase belongs to: its ``file`` attribute, else its dotted classname.

    Mirrors ``runner_durations._file_of`` (re-derived here so this module
    stays importable where that module's POSIX file locking is not). A case
    that maps to no file keeps its classname/name as the key, so a collection
    error is never dropped silently.
    """
    path = case.get("file")
    if path:
        return path.replace("\\", "/").split("::")[0]
    dotted = case.get("classname") or ""
    if not dotted:  # a collection error is reported as classname="" name="tests.test_x"
        dotted = case.get("name") or ""
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        if _TEST_MODULE.match(part):
            return "/".join(parts[: i + 1]) + ".py"
    return dotted or case.get("name") or "?"


def parse_junit(xml_text: str | bytes) -> dict[str, dict[str, float | str]]:
    """A pytest ``--junitxml`` report (as text) into per-file results.

    Seconds sum the file's testcase times. The outcome is the worst case in
    the file: error over fail over pass over skip, so a file of only skipped
    cases is ``skip`` and a mixed pass/skip file is ``pass``. Malformed XML,
    a bad time or a report with no testcases raises ``ValueError``.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"cannot parse junit xml: {exc}") from exc
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("junit xml holds no testcases")
    out: dict[str, dict[str, float | str]] = {}
    for case in cases:
        try:
            seconds = float(case.get("time") or 0.0)
        except ValueError as exc:
            raise ValueError(f"testcase {case.get('name')!r} has a bad time {case.get('time')!r}") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"testcase {case.get('name')!r} has a bad time {case.get('time')!r}")
        outcome = ("error" if case.find("error") is not None else
                   "fail" if case.find("failure") is not None else
                   "skip" if case.find("skipped") is not None else "pass")
        name = _file_of(case)
        old = out.get(name)
        if old is None:
            out[name] = {"outcome": outcome, "seconds": seconds}
        else:
            old["seconds"] = float(old["seconds"]) + seconds
            if _RANK[outcome] > _RANK[str(old["outcome"])]:
                old["outcome"] = outcome
    return out


def pytest_argv(junit: Path, tests: Iterable[str], python: str = sys.executable) -> list[str]:
    """The pytest command a health run executes (same shape as ``train_pool.LocalTransport.argv``)."""
    return [python, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}", *tests]


def _subprocess_runner(argv: list[str], junit: Path) -> int:
    return subprocess.run(argv, check=False).returncode


def run_and_record(path: str | Path | None, sha: str, runner_id: str, tests: Iterable[str], *,
                   runner: Runner | None = None, python: str = sys.executable) -> tuple[Path, dict, int]:
    """Run pytest through ``runner``, parse its junit report and record it.

    Returns (file written, results, runner exit code). A red run (exit 1)
    still records: red is valid health evidence. A run that leaves no
    readable junit report raises ``RuntimeError`` and records nothing.
    """
    test_list = list(tests) or ["tests"]
    run = runner if runner is not None else _subprocess_runner
    with tempfile.TemporaryDirectory(prefix="kyra-master-health-") as folder:
        junit = Path(folder) / "junit.xml"
        code = run(pytest_argv(junit, test_list, python), junit)
        try:
            xml_text = junit.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"health run for {sha} on {runner_id} left no junit report: {exc}") from exc
        results = parse_junit(xml_text)
    return record(path, sha, runner_id, results), results, code
