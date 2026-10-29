"""Per-test-file durations from pytest junit XML, kept as an EMA store the runner pool plans from.

The store is ``data/team/test-durations.json`` (``default_store()``; any path can be passed). Its top level is
exactly the shape ``runner_pool.load_durations`` reads, ``{"tests/test_x.py": seconds}``, holding the EMA over
every recorded run whatever the runner. Per-runner EMAs sit under ``"_runners"`` (``{name: {file: seconds}}``)
and the format tag under ``"_schema"``; ``load_durations`` skips both because their values are not numbers.

Seconds are the sum of the file's testcase times in one run (setup, call and teardown as pytest reports them),
so they approximate the core-seconds the planner divides by a runner's capacity.

A store that exists but does not parse into that shape raises ``CorruptStore``: it is never overwritten
silently. ``rebuild=True`` moves it aside (``<name>.corrupt-<UTC stamp>``) and starts empty. Writes take a
sibling lock file and go through a sibling temp file plus ``os.replace``, so a reader sees the old store or
the new one, never half of one, and two recorders cannot lose each other's update.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = "runner-durations/1"
ALPHA = 0.3
_RUNNER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TEST_MODULE = re.compile(r"^(test_\w+|\w+_test)$")


class CorruptStore(Exception):
    """The duration store exists but is not a valid store; nothing was written."""


class BadJunit(Exception):
    """The junit XML could not be read or holds no testcases."""


def default_store() -> Path:
    from companion.paths import DATA_DIR

    return DATA_DIR / "team" / "test-durations.json"


def _file_of(case: ET.Element) -> str | None:
    """The test file a testcase belongs to: its ``file`` attribute (xunit1) or its dotted classname (xunit2)."""
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
    return None


def parse_junit(path: str | Path) -> tuple[dict[str, float], list[str]]:
    """Seconds per test file from one junit XML, and the classnames that map to no file (never dropped silently)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise BadJunit(f"cannot read junit xml {path}: {exc}") from exc
    totals: dict[str, float] = {}
    unmapped: list[str] = []
    cases = list(root.iter("testcase"))
    if not cases:
        raise BadJunit(f"junit xml {path} holds no testcases")
    for case in cases:
        try:
            seconds = float(case.get("time") or 0.0)
        except ValueError as exc:
            raise BadJunit(f"testcase {case.get('name')!r} has a bad time {case.get('time')!r}") from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise BadJunit(f"testcase {case.get('name')!r} has a bad time {case.get('time')!r}")
        name = _file_of(case)
        if name is None:
            unmapped.append(case.get("classname") or case.get("name") or "?")
            continue
        totals[name] = totals.get(name, 0.0) + seconds
    return totals, unmapped


def _is_seconds(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def load_store(path: str | Path) -> dict:
    """The store as a dict; a missing file is an empty store, anything malformed raises ``CorruptStore``."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"_schema": SCHEMA, "_runners": {}}
    except OSError as exc:
        raise CorruptStore(f"cannot read duration store {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CorruptStore(f"duration store {path} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("_schema") != SCHEMA:
        raise CorruptStore(f"duration store {path} is not a {SCHEMA} object")
    runners = data.get("_runners")
    if not isinstance(runners, dict):
        raise CorruptStore(f"duration store {path} has no _runners object")
    for key, value in data.items():
        if not key.startswith("_") and not _is_seconds(value):
            raise CorruptStore(f"duration store {path}: {key!r} is not a seconds value")
    for name, files in runners.items():
        if not isinstance(files, dict) or not all(_is_seconds(v) for v in files.values()):
            raise CorruptStore(f"duration store {path}: runner {name!r} is not a {{file: seconds}} object")
    return data


def update(store: dict, observed: dict[str, float], runner: str, alpha: float = ALPHA) -> dict:
    """Fold one run into the store: EMA per file overall and per runner; a file seen first takes its observed value."""
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    per_runner = store["_runners"].setdefault(runner, {})
    for name, seconds in observed.items():
        for table in (store, per_runner):
            old = table.get(name)
            table[name] = seconds if old is None else alpha * seconds + (1 - alpha) * old
    return store


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path: Path, data: dict) -> None:
    """Temp file in the same directory, fsync, then ``os.replace``: readers see old or new, never a part."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def record(junit: str | Path, runner: str, store_path: str | Path | None = None, *,
           alpha: float = ALPHA, rebuild: bool = False) -> tuple[dict[str, float], list[str], Path | None]:
    """Parse ``junit`` and fold it into the store for ``runner``.

    Returns (seconds per file recorded, unmapped classnames, where a corrupt store was moved or None).
    Raises ``BadJunit`` or ``CorruptStore`` (unless ``rebuild``) before anything is written.
    """
    if not _RUNNER_NAME.match(runner):
        raise ValueError(f"bad runner name {runner!r}")
    observed, unmapped = parse_junit(junit)
    path = Path(store_path) if store_path is not None else default_store()
    moved = None
    with _locked(path):
        try:
            store = load_store(path)
        except CorruptStore:
            if not rebuild:
                raise
            moved = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
            os.replace(path, moved)
            store = load_store(path)
        atomic_write(path, update(store, observed, runner, alpha))
    return observed, unmapped, moved
