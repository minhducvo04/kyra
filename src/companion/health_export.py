"""Local Apple Health export readers.

summarize() returns shape only. nights() and resting_heart_rate() return private
readings for the local HUD; their output must never enter prompts or logs.

The export itself (Health app > profile picture > Export All Health Data) is private and lives
under ``DATA_DIR/health/``, which is gitignored with the rest of ``data/``.
"""
from __future__ import annotations

import re
import zipfile
from bisect import bisect_right
from contextlib import contextmanager
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from statistics import median
from typing import IO, Any
from xml.etree.ElementTree import iterparse

SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
RESTING_HR = "HKQuantityTypeIdentifierRestingHeartRate"
_ASLEEP = {"HKCategoryValueSleepAnalysis" + suffix for suffix in
           ("Asleep", "AsleepUnspecified", "AsleepCore", "AsleepDeep", "AsleepREM")}


class MissingExport(FileNotFoundError):
    """No local Health export is available."""


HEART_RATE = "HKQuantityTypeIdentifierHeartRate"
_DATE = "%Y-%m-%d %H:%M:%S %z"
_WATCH_HARDWARE = re.compile(r"hardware:(Watch\d+,\d+)")
# Shares of gaps at or under each bound: a live session, a minute, the usual background pace, off the wrist.
_BOUNDS_S = (10, 60, 600, 1800)


def find_export(health_dir: Path) -> Path:
    """The export under ``health_dir``: the zip as the phone sends it, or the folder it unzips to."""
    for candidate in (health_dir / "export.zip", health_dir / "apple_health_export" / "export.xml"):
        if candidate.is_file():
            return candidate
    raise MissingExport("No Health export available")


@contextmanager
def _open_xml(path: Path) -> IO[bytes]:
    path = Path(path)
    if not path.is_file():
        raise MissingExport("No Health export available")
    if path.suffix != ".zip":
        with path.open("rb") as stream:
            yield stream
        return
    with zipfile.ZipFile(path) as archive:
        name = next((n for n in archive.namelist() if n.endswith("/export.xml") or n == "export.xml"), None)
        if name is None:
            raise ValueError("Health archive has no export XML")
        with archive.open(name) as stream:
            yield stream


def _elements(path):
    with _open_xml(path) as stream:
        events = iterparse(stream, events=("start", "end"))
        _, root = next(events)
        for event, elem in events:
            if event == "end" and elem.tag in {"Record", "ExportDate", "Workout", "ActivitySummary", "Correlation"}:
                if elem.tag in {"Record", "ExportDate"}:
                    yield elem.tag, dict(elem.attrib)
                elem.clear()
                root.clear()


def _window(days, now):
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    return now, now.date() - timedelta(days=days - 1)


def _minutes(intervals):
    total = 0.0
    end = None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).total_seconds()
        elif stop > end:
            total += (stop - end).total_seconds()
        end = max(end, stop) if end is not None else stop
    return round(total / 60)


def nights(path, *, days=30, now=None):
    now, first = _window(days, now)
    grouped = {}
    for tag, row in _elements(path):
        if tag != "Record" or row.get("type") != SLEEP:
            continue
        value = row.get("value")
        if value not in _ASLEEP | {"HKCategoryValueSleepAnalysisInBed"}:
            continue
        start = datetime.strptime(row["startDate"], _DATE)
        end = datetime.strptime(row["endDate"], _DATE)
        if start >= end or end > now or end.hour >= 12 or not first <= end.date() <= now.date():
            continue
        spans = grouped.setdefault(end.date().isoformat(), {"asleep": [], "in_bed": []})
        spans["asleep" if value in _ASLEEP else "in_bed"].append((start, end))
    result = []
    for day, spans in sorted(grouped.items()):
        all_spans = spans["asleep"] + spans["in_bed"]
        result.append(dict(date=day, bed=min(a for a, _ in all_spans).isoformat(),
                           wake=max(b for _, b in all_spans).isoformat(),
                           asleep_minutes=_minutes(spans["asleep"]), in_bed_minutes=_minutes(spans["in_bed"])))
    return result


def resting_heart_rate(path, *, days=30, now=None):
    """Daily median of finite positive readings; exact duplicate records count once."""
    now, first = _window(days, now)
    grouped = {}
    for tag, row in _elements(path):
        if tag != "Record" or row.get("type") != RESTING_HR:
            continue
        stamp = datetime.strptime(row["startDate"], _DATE)
        if stamp > now or not first <= stamp.date() <= now.date():
            continue
        if row.get("unit", "count/min") != "count/min":
            continue
        value = float(row["value"])
        if isfinite(value) and value > 0:
            grouped.setdefault(stamp.date().isoformat(), {})[(stamp, row.get("endDate"), value)] = value
    return [dict(date=day, bpm=round(median(readings.values()))) for day, readings in sorted(grouped.items())]


def export_info(path):
    """Export date and Watch hardware identifier, never a person or device name."""
    day, hardware = None, set()
    for tag, row in _elements(path):
        if tag == "ExportDate":
            day = datetime.strptime(row["value"], _DATE).date().isoformat()
        elif tag == "Record":
            match = _WATCH_HARDWARE.search(row.get("device", ""))
            if match:
                hardware.add(match.group(1))
    # Older/synthetic exports have no ExportDate; expose file freshness explicitly.
    return {"export_day": day or datetime.fromtimestamp(Path(path).stat().st_mtime).date().isoformat(),
            "watch": ", ".join(sorted(hardware)) or None,
            "export_day_source": "export" if day else "file_modified"}


def _gap_stats(gaps: list[float]) -> dict[str, Any]:
    if not gaps:
        return {"count": 0}
    ordered = sorted(gaps)
    stats: dict[str, Any] = {
        "count": len(ordered),
        "median_s": round(median(ordered)),
        "p90_s": round(ordered[int(0.9 * (len(ordered) - 1))]),
        "max_s": round(ordered[-1]),
    }
    for bound in _BOUNDS_S:
        stats[f"share_le_{bound}s"] = round(bisect_right(ordered, bound) / len(ordered), 3)
    return stats


def summarize(path: Path) -> dict[str, Any]:
    types: dict[str, dict[str, Any]] = {}
    watch_hardware: set[str] = set()
    heart_times: list[datetime] = []
    workouts: list[tuple[datetime, datetime]] = []

    with _open_xml(path) as stream:
        for _, elem in iterparse(stream, events=("end",)):
            if elem.tag == "Record":
                kind, day = elem.get("type", ""), elem.get("startDate", "")[:10]
                seen = types.setdefault(kind, {"count": 0, "first_day": day, "last_day": day})
                seen["count"] += 1
                seen["first_day"], seen["last_day"] = min(seen["first_day"], day), max(seen["last_day"], day)
                hardware = _WATCH_HARDWARE.search(elem.get("device") or "")
                if hardware:
                    watch_hardware.add(hardware.group(1))
                    if kind == HEART_RATE:
                        heart_times.append(datetime.strptime(elem.get("startDate"), _DATE))
            elif elem.tag == "Workout":
                workouts.append((datetime.strptime(elem.get("startDate"), _DATE),
                                 datetime.strptime(elem.get("endDate"), _DATE)))
            if elem.tag in ("Record", "Workout", "ActivitySummary", "Correlation"):
                elem.clear()  # exports run to gigabytes; keep nothing that has been counted

    def in_workout(a: datetime, b: datetime) -> bool:
        return any(start <= a and b <= end for start, end in workouts)

    heart_times.sort()
    inside: list[float] = []
    outside: list[float] = []
    for a, b in zip(heart_times, heart_times[1:], strict=False):
        (inside if in_workout(a, b) else outside).append((b - a).total_seconds())

    return {
        "types": dict(sorted(types.items())),
        "watch_hardware": sorted(watch_hardware),
        "workouts": len(workouts),
        "heart_rate_gaps": {"in_workout": _gap_stats(inside), "outside_workout": _gap_stats(outside)},
    }
