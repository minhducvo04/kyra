"""QC desk inputs: read configured local sources into one event stream.

``collect(config, cursor) -> (events, next_cursor, health)`` is the frozen
reader. It does not publish, repair, or run anything found in a log. A log
line is evidence text, never an argument vector.

Each event carries ``source``, ``file_id``, ``line`` (one-based), ``offset``,
``record_hash``, ``time`` (or None), ``feature``, ``node``, ``run_id``,
``kind`` and ``facts``. Multiline JSON also names the field in ``facts``.

The cursor resumes by file identity, byte offset and the hash of the last
complete record. A trailing partial line stays unread. A replaced or
truncated file is read from the start so a rotated record is not dropped.
Landing lines that contain only ``HH:MM:SS`` keep that clock in ``facts``
and store the tick's first-seen time; the clock is not turned into a date.
Reads stop at 10 MiB per call. Missing, unreadable and malformed sources
stay visible on ``health`` and are never reported as a healthy team.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

READ_BUDGET = 10 * 1024 * 1024

# Mirrors scripts/landing_runner.py's transient refusal wording. This reader
# only classifies text; it does not import the runner (that module is owned
# by another lane) and it never launches a train.
_TRANSIENT_RE = re.compile(
    r"headroom refused|no headroom|runner held|another heavy job|"
    r"1-minute load (is )?[\d.]+|origin/master moved while the suite ran",
    re.I,
)
_GUARDS_FAILED_RE = re.compile(r"merged-tree guards failed", re.I)
_FAILED_TEST_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
_LOAD_GUARD_TEST = "tests/test_correct_repeaters.py::test_load_allows_the_browser_phase"
_CLOCK_RE = re.compile(r"^(?P<clock>\d{2}:\d{2}:\d{2})\s+(?P<rest>.*)$")
_PIN_RE = re.compile(r"reviewed pin|has no branch@|differs from the reviewed", re.I)
_STALE_RE = re.compile(r"stale train", re.I)
_REFUSAL_RE = re.compile(r"PermissionCancelled|command refusal|refused the command", re.I)
_KEY_RE_CACHE: dict[str, re.Pattern[str]] = {}

GLOBAL_SOURCES = frozenset({
    "landing_log",
    "landing_queue",
    "landing_retry",
    "train_log",
    "board_inbox",
    "qc_decision",
    "qc_outcome",
    "qc_incident",
})

_REQUIRED = (
    "ledger",
    "runs",
    "features",
    "dag",
    "review",
    "landing_log",
    "landing_queue",
    "landing_retry",
    "board_inbox",
    "inspector_log",
    "registry",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _data_root(config: dict) -> Path:
    explicit = config.get("data_root")
    if explicit:
        return Path(explicit)
    env = os.environ.get("KYRA_DATA_DIR")
    if env:
        return Path(env)
    return _repo_root() / "data"


def _ledger_path(config: dict, root: Path) -> Path:
    explicit = config.get("ledger")
    if explicit:
        return Path(explicit)
    env = os.environ.get("KYRA_CONTEXT_LEDGER")
    if env:
        return Path(env)
    return root / "context" / "packages.jsonl"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(text: str) -> str:
    return _hash(text.encode("utf-8"))


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _allowed(path: Path, roots: list[Path], files: list[Path] | None = None) -> bool:
    """True for a configured file itself, or a path that stays inside a configured root.

    A ledger or registry file may sit outside the data root when the caller
    names it. That does not make its parent directory readable.
    """
    try:
        real = path.resolve()
    except OSError:
        return False
    for file in files or []:
        try:
            if real == Path(file).resolve():
                return True
        except OSError:
            continue
    for root in roots:
        try:
            real.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _line_offset(text: str, line: int) -> int:
    parts = text.splitlines(keepends=True)
    return sum(len(part.encode("utf-8")) for part in parts[: max(0, line - 1)])


def _shallow_key_line(text: str, key: str) -> int:
    pattern = _KEY_RE_CACHE.get(key)
    if pattern is None:
        pattern = re.compile(r'^(\s*)"' + re.escape(key) + r'"\s*:')
        _KEY_RE_CACHE[key] = pattern
    best: tuple[int, int] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        match = pattern.match(line)
        if match is None:
            continue
        indent = len(match.group(1))
        if best is None or indent < best[0]:
            best = (indent, number)
    return best[1] if best else 1


def _parse_time(value):
    """Epoch seconds for a real timestamp. A clock-only string stays None."""
    if type(value) in (int, float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        return None
    if "T" not in text and not re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        return None
    try:
        stamp = text.replace("Z", "+00:00")
        return datetime_timestamp(stamp)
    except (ValueError, OverflowError, OSError):
        return None


def datetime_timestamp(text: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(text).timestamp()


def _refusal_marker(text: str) -> str | None:
    if not text:
        return None
    if "PermissionCancelled" in text:
        return "PermissionCancelled"
    if _REFUSAL_RE.search(text):
        return "command_refusal"
    return None


def _refusal_class(text: str) -> str | None:
    if not text:
        return None
    if _PIN_RE.search(text):
        return "pin"
    if _STALE_RE.search(text):
        return "stale_train"
    if "NOT fast-forwarded" in text:
        return "fast_forward"
    failed = set(_FAILED_TEST_RE.findall(text)) if _GUARDS_FAILED_RE.search(text) else set()
    if failed:
        return "transient" if failed == {_LOAD_GUARD_TEST} else "guard"
    if _TRANSIENT_RE.search(text):
        return "transient"
    if re.search(r"\bREFUSED\b|precheck refused|refused", text, re.I):
        return "other"
    return None


def _excerpt(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit]


def _event(source, file_id, line, offset, record_hash, when, feature, node, run_id, kind, facts):
    return {
        "source": source,
        "file_id": file_id,
        "line": int(line),
        "offset": int(offset),
        "record_hash": record_hash,
        "time": when,
        "feature": feature,
        "node": node,
        "run_id": run_id,
        "kind": kind,
        "facts": facts,
    }


class _Tick:
    def __init__(self, root: Path, config: dict, cursor: dict, roots: list[Path], files: list[Path]):
        self.root = root
        self.config = config
        self.cursor = cursor
        self.roots = roots
        self.files = list(files)
        self.now = float(config.get("now") if config.get("now") is not None else time.time())
        self.budget = int(config.get("read_budget") or READ_BUDGET)
        self.bytes_read = 0
        self.bounded = False
        self.events: list[dict] = []
        self.sources: dict[str, dict] = {}
        self.file_state = dict(cursor.get("files") or {})
        self.snapshots = dict(cursor.get("snapshots") or {})
        self.first_seen = {key: dict(value) for key, value in (cursor.get("first_seen") or {}).items()}
        self.queue_names: list[str] = []

    def room(self) -> int:
        return max(0, self.budget - self.bytes_read)

    def note(self, name: str, status: str, path: Path | None, events: int, detail: str = ""):
        self.sources[name] = {
            "status": status,
            "path": None if path is None else _display(path, self.root),
            "events": events,
            "detail": detail,
        }

    def add(self, event: dict, source_name: str):
        self.events.append(event)
        row = self.sources.get(source_name)
        if row is not None:
            row["events"] = row.get("events", 0) + 1

    def take(self, amount: int):
        self.bytes_read += amount
        if self.bytes_read >= self.budget:
            self.bounded = True


def _blank_state(identity: tuple[int, int] | None, offset: int, line: int, previous: dict | None) -> dict:
    state = {
        "dev": None if identity is None else identity[0],
        "ino": None if identity is None else identity[1],
        "offset": offset,
        "line": line,
        "record_hash": None if not previous else previous.get("record_hash"),
        "last_start": None if not previous else previous.get("last_start"),
        "last_len": None if not previous else previous.get("last_len"),
    }
    return state


def _pull_lines(tick: _Tick, path: Path, file_id: str):
    """Complete lines from ``path``, honoring identity, offset, hash and the tick budget.

    Returns (records, status, detail). Each record is
    ``{line, offset, raw, record_hash}`` for a non-blank line that was consumed.
    """
    previous = dict(tick.file_state.get(file_id) or {})
    try:
        if not _allowed(path, tick.roots, tick.files):
            return [], "unreadable", "path is outside the configured roots"
        st = path.stat()
    except FileNotFoundError:
        return [], "missing", "not found"
    except OSError as exc:
        return [], "unreadable", exc.__class__.__name__

    if tick.room() <= 0:
        tick.file_state[file_id] = previous
        return [], "deferred", "tick budget exhausted"

    identity = (st.st_dev, st.st_ino)
    rotated = False
    if previous.get("dev") is not None:
        if (previous.get("dev"), previous.get("ino")) != identity or st.st_size < int(previous.get("offset") or 0):
            rotated = True
        elif previous.get("last_start") is not None and previous.get("record_hash"):
            try:
                with path.open("rb") as handle:
                    handle.seek(int(previous["last_start"]))
                    prior = handle.read(int(previous.get("last_len") or 0))
            except OSError as exc:
                return [], "unreadable", exc.__class__.__name__
            if _hash(prior) != previous["record_hash"]:
                rotated = True

    offset = 0 if rotated or not previous else int(previous.get("offset") or 0)
    line_no = 0 if rotated or not previous else int(previous.get("line") or 0)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read(tick.room())
    except OSError as exc:
        return [], "unreadable", exc.__class__.__name__
    tick.take(len(blob))
    reached_end = offset + len(blob) >= st.st_size

    if not blob:
        tick.file_state[file_id] = _blank_state(identity, offset, line_no, None if rotated else previous)
        tick.file_state[file_id]["record_hash"] = None if rotated else previous.get("record_hash")
        tick.file_state[file_id]["last_start"] = None if rotated else previous.get("last_start")
        tick.file_state[file_id]["last_len"] = None if rotated else previous.get("last_len")
        return [], "ok", "rotated" if rotated else ""

    held_partial = False
    oversized = False
    if blob.endswith(b"\n"):
        consumed = blob
    else:
        cut = blob.rfind(b"\n")
        if cut == -1:
            if not reached_end:
                oversized = True
                consumed = blob
            else:
                held_partial = True
                consumed = b""
        else:
            consumed = blob[: cut + 1]
            held_partial = reached_end

    records = []
    cursor_at = offset
    last = None
    for part in consumed.splitlines(keepends=True):
        line_no += 1
        start = cursor_at
        body = part[:-2] if part.endswith(b"\r\n") else part[:-1] if part.endswith(b"\n") else part
        cursor_at += len(part)
        if not body.strip():
            continue
        digest = _hash(body)
        last = (digest, start, len(body))
        if oversized and not part.endswith(b"\n"):
            records.append({
                "line": line_no, "offset": start, "raw": body, "record_hash": digest, "broken": True,
            })
        else:
            records.append({
                "line": line_no, "offset": start, "raw": body, "record_hash": digest, "broken": False,
            })

    new_offset = offset + len(consumed)
    state = _blank_state(identity, new_offset, line_no, None)
    if last is not None:
        state["record_hash"], state["last_start"], state["last_len"] = last
    elif not rotated:
        state["record_hash"] = previous.get("record_hash")
        state["last_start"] = previous.get("last_start")
        state["last_len"] = previous.get("last_len")
    if held_partial and not consumed:
        # Identity is current, but the only bytes are an unfinished line.
        state["offset"] = offset
        state["line"] = 0 if rotated else int(previous.get("line") or 0)
    tick.file_state[file_id] = state

    if oversized:
        return records, "malformed", "record exceeds tick budget"
    if not reached_end:
        return records, "partial", "read budget reached; resume at next offset"
    if held_partial:
        return records, "ok", "partial trailing line held"
    return records, "ok", "rotated" if rotated else ""


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _load_groups(tick: _Tick) -> dict[str, list[str]]:
    if isinstance(tick.config.get("groups"), dict):
        return {
            str(name): [str(item) for item in members]
            for name, members in tick.config["groups"].items()
            if isinstance(members, list)
        }
    path = tick.root / "team" / "qc" / "groups.json"
    if not path.exists():
        return {}
    if not _allowed(path, tick.roots, tick.files):
        tick.note("qc_groups", "unreadable", path, 0, "path is outside the configured roots")
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        tick.note("qc_groups", "malformed", path, 0, exc.__class__.__name__)
        return {}
    if not isinstance(document, dict):
        tick.note("qc_groups", "malformed", path, 0, "groups file must be an object")
        return {}
    groups = {}
    for name, members in document.items():
        if isinstance(members, list) and all(isinstance(item, str) for item in members):
            groups[str(name)] = list(members)
    tick.note("qc_groups", "ok", path, 0, "")
    return groups


def _finish_status(current: str, status: str) -> str:
    rank = {"ok": 0, "absent": 0, "partial": 1, "deferred": 2, "malformed": 3, "unreadable": 4, "missing": 5}
    if current is None:
        return status
    return status if rank.get(status, 0) >= rank.get(current, 0) else current


def _read_ledger(tick: _Tick, path: Path):
    file_id = "context/packages.jsonl"
    records, status, detail = _pull_lines(tick, path, file_id)
    emitted = 0
    bad = False
    for record in records:
        text = _decode(record["raw"])
        if record.get("broken"):
            bad = True
            tick.events.append(_event(
                "ledger", file_id, record["line"], record["offset"], record["record_hash"],
                None, None, None, None, "malformed",
                {"field": None, "reason": "record exceeds tick budget"},
            ))
            emitted += 1
            continue
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            bad = True
            tick.events.append(_event(
                "ledger", file_id, record["line"], record["offset"], record["record_hash"],
                None, None, None, None, "malformed",
                {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        feature = row.get("feature") if isinstance(row.get("feature"), str) else None
        if feature is None and isinstance(row.get("team"), str) and row.get("team"):
            feature = row["team"]
        node = row.get("node") if isinstance(row.get("node"), str) else None
        run_id = row.get("run_id") if isinstance(row.get("run_id"), str) else None
        if run_id is None and isinstance(row.get("id"), str):
            run_id = row["id"]
        status_text = row.get("status") if isinstance(row.get("status"), str) else None
        if status_text == "failed":
            kind = "ledger_failed"
            field = "status"
        elif "sent_at" in row:
            kind = "ledger_send"
            field = "sent_at"
        elif "passed_at" in row:
            kind = "ledger_close"
            field = "passed_at"
        else:
            kind = "ledger_row"
            field = "id"
        when = _parse_time(row.get("sent_at"))
        if when is None:
            when = _parse_time(row.get("passed_at"))
        if when is None:
            when = _parse_time(row.get("t"))
        excerpt_source = row.get("last_text") if isinstance(row.get("last_text"), str) else ""
        marker = _refusal_marker(excerpt_source) or _refusal_marker(str(row.get("reason_code") or ""))
        folder = row.get("target") if isinstance(row.get("target"), str) else None
        if folder is None and isinstance(row.get("folder"), str):
            folder = row["folder"]
        log_path = row.get("log") if isinstance(row.get("log"), str) else None
        log_facts = _linked_log(tick, log_path)
        facts = {
            "field": field,
            "lane_id": run_id,
            "team": row.get("team") if isinstance(row.get("team"), str) else None,
            "model": row.get("model") if isinstance(row.get("model"), str) else None,
            "channel": row.get("channel") if isinstance(row.get("channel"), str) else None,
            "folder": folder,
            "status": status_text or ("sent" if kind == "ledger_send" else "closed" if kind == "ledger_close" else None),
            "reason_code": row.get("reason_code") if isinstance(row.get("reason_code"), str) else None,
            "exit_code": row.get("exit_code") if type(row.get("exit_code")) is int else None,
            "pid": row.get("pid") if type(row.get("pid")) is int else None,
            "refusal": marker,
            "excerpt": _excerpt(excerpt_source) if excerpt_source else "",
            **log_facts,
        }
        tick.events.append(_event(
            "ledger", file_id, record["line"], record["offset"], record["record_hash"],
            when, feature, node, run_id, kind, facts,
        ))
        emitted += 1
    final = "malformed" if bad else status
    if bad and status == "ok":
        detail = "malformed ledger line"
    tick.note("ledger", final, path, emitted, detail)


def _linked_log(tick: _Tick, log_path: str | None) -> dict:
    """Metadata for a ledger-linked worker log. The body is not copied.

    Hits are marker names and line numbers only, and only when the resolved
    file stays inside a configured root. Symlinks that leave those roots are
    refused.
    """
    if not log_path:
        return {"log": None, "log_status": "absent", "log_hits": []}
    path = Path(log_path)
    if not path.is_absolute():
        path = tick.root / path
    if path.is_symlink() or not _allowed(path, tick.roots, tick.files):
        return {"log": None, "log_status": "outside_root", "log_hits": []}
    if not path.is_file():
        return {"log": path.name, "log_status": "missing", "log_hits": []}
    if tick.room() <= 0:
        return {"log": _display(path, tick.root), "log_status": "deferred", "log_hits": []}
    try:
        blob = path.read_bytes()[: tick.room()]
    except OSError:
        return {"log": path.name, "log_status": "unreadable", "log_hits": []}
    tick.take(len(blob))
    hits = []
    for number, raw in enumerate(blob.splitlines(), 1):
        text = _decode(raw)
        marker = _refusal_marker(text)
        if marker:
            hits.append({"line": number, "marker": marker})
    status = "truncated" if path.stat().st_size > len(blob) else "ok"
    return {"log": _display(path, tick.root), "log_status": status, "log_hits": hits}


def _json_file_changed(tick: _Tick, path: Path, file_id: str, text: str) -> bool:
    try:
        st = path.stat()
    except OSError:
        return True
    digest = _digest(text)
    snapshot = tick.snapshots.get(file_id) or {}
    changed = not (
        snapshot.get("dev") == st.st_dev
        and snapshot.get("ino") == st.st_ino
        and snapshot.get("digest") == digest
        and snapshot.get("size") == st.st_size
    )
    tick.snapshots[file_id] = {
        "dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "digest": digest,
    }
    return changed


def _read_text(tick: _Tick, path: Path):
    if tick.room() <= 0:
        return None, "deferred", "tick budget exhausted"
    if not _allowed(path, tick.roots, tick.files):
        return None, "unreadable", "path is outside the configured roots"
    try:
        if not path.exists():
            return None, "missing", "not found"
        blob = path.read_bytes()[: tick.room()]
    except OSError as exc:
        return None, "unreadable", exc.__class__.__name__
    tick.take(len(blob))
    try:
        size = path.stat().st_size
    except OSError:
        size = len(blob)
    if size > len(blob):
        return _decode(blob), "partial", "read budget reached before end of file"
    return _decode(blob), "ok", ""


def _read_runs(tick: _Tick, directory: Path):
    if not directory.exists():
        tick.note("runs", "missing", directory, 0, "not found")
        return
    if not directory.is_dir():
        tick.note("runs", "malformed", directory, 0, "runs path is not a directory")
        return
    if not _allowed(directory, tick.roots, tick.files):
        tick.note("runs", "unreadable", directory, 0, "path is outside the configured roots")
        return
    emitted = 0
    status, detail = "ok", ""
    paths = sorted(item for item in directory.glob("*.json") if not item.name.startswith("."))
    if not paths:
        tick.note("runs", "ok", directory, 0, "")
        return
    for path in paths:
        if tick.room() <= 0:
            status, detail = "deferred", "tick budget exhausted"
            break
        file_id = _display(path, tick.root)
        text, one_status, one_detail = _read_text(tick, path)
        if one_status in {"deferred", "partial", "unreadable", "missing"}:
            status = _finish_status(status, one_status)
            detail = one_detail or detail
            if one_status != "partial":
                continue
        if text is None:
            continue
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            status = "malformed"
            detail = f"invalid JSON object at {file_id}"
            tick.events.append(_event(
                "run", file_id, 1, 0, _hash(text.encode("utf-8")), None, None, None, path.stem,
                "malformed", {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        if not _json_file_changed(tick, path, file_id, text) and one_status == "ok":
            continue
        feature = row.get("feature") if isinstance(row.get("feature"), str) else None
        node = row.get("node") if isinstance(row.get("node"), str) else None
        run_id = row.get("run_id") if isinstance(row.get("run_id"), str) else path.stem
        field_line = _shallow_key_line(text, "state")
        history = row.get("history") if isinstance(row.get("history"), list) else []
        states = [item.get("state") for item in history if isinstance(item, dict) and isinstance(item.get("state"), str)]
        attempts = row.get("attempts") if isinstance(row.get("attempts"), dict) else {}
        attempt_counts = {
            str(key): value for key, value in attempts.items()
            if isinstance(key, str) and type(value) is int
        }
        brief = row.get("brief") if isinstance(row.get("brief"), dict) else {}
        facts = {
            "field": "state",
            "state": row.get("state") if isinstance(row.get("state"), str) else None,
            "hold": row.get("hold") if isinstance(row.get("hold"), str) else None,
            "attempts": attempt_counts,
            "tier": row.get("tier") if isinstance(row.get("tier"), str) else None,
            "worktree": row.get("worktree") if isinstance(row.get("worktree"), str) else brief.get("worktree"),
            "owner": brief.get("worker") if isinstance(brief.get("worker"), str) else None,
            "history_states": states,
            "sha": row.get("sha") if isinstance(row.get("sha"), str) else None,
        }
        when = _parse_time(row.get("updated"))
        if when is None:
            when = _parse_time(row.get("created"))
        tick.events.append(_event(
            "run", file_id, field_line, _line_offset(text, field_line), _hash(_canonical(row)),
            when, feature, node, run_id, "run", facts,
        ))
        emitted += 1
        if one_status not in {"ok", "partial"}:
            status = _finish_status(status, one_status)
            detail = one_detail or detail
    tick.note("runs", status, directory, emitted, detail)


def _completion_nodes(document: dict) -> dict:
    rows = document.get("nodes", document) if isinstance(document, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict) and key != "nodes"}


def _read_features(tick: _Tick, directory: Path, dag_dir: Path):
    if not directory.exists():
        tick.note("features", "missing", directory, 0, "not found")
        tick.note("dag", "missing", dag_dir, 0, "no feature directory")
        return
    if not _allowed(directory, tick.roots, tick.files):
        tick.note("features", "unreadable", directory, 0, "path is outside the configured roots")
        tick.note("dag", "unreadable", dag_dir, 0, "features unreadable")
        return
    emitted = 0
    status, detail = "ok", ""
    features = sorted(item for item in directory.iterdir() if item.is_dir() and not item.name.startswith("."))
    seen_features: list[str] = []
    for feature_dir in features:
        path = feature_dir / "completion.json"
        file_id = _display(path, tick.root)
        if not path.exists():
            continue
        if tick.room() <= 0:
            status, detail = "deferred", "tick budget exhausted"
            break
        text, one_status, one_detail = _read_text(tick, path)
        if text is None:
            status = _finish_status(status, one_status)
            detail = one_detail or detail
            continue
        try:
            document = json.loads(text)
            nodes = _completion_nodes(document)
            if not isinstance(document, dict):
                raise ValueError
        except ValueError:
            status = "malformed"
            detail = f"invalid completion map at {file_id}"
            tick.events.append(_event(
                "completion", file_id, 1, 0, _hash(text.encode("utf-8")), None, feature_dir.name, None, None,
                "malformed", {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        seen_features.append(feature_dir.name)
        if not _json_file_changed(tick, path, file_id, text) and one_status == "ok":
            if one_status != "ok":
                status = _finish_status(status, one_status)
                detail = one_detail or detail
            continue
        for node, row in nodes.items():
            field = f"nodes.{node}"
            field_line = _shallow_key_line(text, node)
            when = _parse_time(row.get("t") or row.get("updated"))
            facts = {
                "field": field,
                "status": row.get("status") if isinstance(row.get("status"), str) else None,
                "hold": row.get("hold") if isinstance(row.get("hold"), str) else None,
                "sha": row.get("sha") if isinstance(row.get("sha"), str) else None,
                "by": row.get("by") if isinstance(row.get("by"), str) else None,
                "owner": row.get("owner") if isinstance(row.get("owner"), str) else None,
            }
            run_id = row.get("run_id") if isinstance(row.get("run_id"), str) else None
            tick.events.append(_event(
                "completion", file_id, field_line, _line_offset(text, field_line),
                _hash(_canonical({"node": node, **row})),
                when, feature_dir.name, node, run_id, "completion", facts,
            ))
            emitted += 1
        if one_status != "ok":
            status = _finish_status(status, one_status)
            detail = one_detail or detail
    tick.note("features", status, directory, emitted, detail)
    _read_dags(tick, dag_dir, seen_features)


def _read_dags(tick: _Tick, dag_dir: Path, features: list[str]):
    if not features:
        # No completion maps yet: an absent DAG folder is not an unobserved team.
        tick.note("dag", "ok", dag_dir, 0, "" if dag_dir.exists() else "no feature completions")
        return
    emitted = 0
    missing = []
    status, detail = "ok", ""
    for feature in features:
        if not dag_dir.exists() or not _allowed(dag_dir, tick.roots, tick.files):
            missing.append(feature)
            continue
        path = _dag_path(dag_dir, feature)
        if path is None or not path.exists():
            missing.append(feature)
            continue
        if tick.room() <= 0:
            status, detail = "deferred", "tick budget exhausted"
            break
        file_id = _display(path, tick.root)
        text, one_status, one_detail = _read_text(tick, path)
        if text is None:
            status = _finish_status(status, one_status)
            detail = one_detail or detail
            missing.append(feature)
            continue
        try:
            from companion.feature_team.dag import load
            graph = load(path)
            nodes = [graph.node(node_id) for node_id in graph._nodes]
        except (OSError, ValueError, TypeError, KeyError):
            status = "malformed"
            detail = f"invalid DAG at {file_id}"
            tick.events.append(_event(
                "dag", file_id, 1, 0, _hash(text.encode("utf-8")), None, feature, None, None,
                "malformed", {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        if not _json_file_changed(tick, path, file_id, text) and one_status == "ok":
            continue
        for node in nodes:
            node_id = node.get("id")
            field_line = _shallow_key_line(text, "id") if len(nodes) == 1 else _id_line(text, str(node_id))
            facts = {
                "field": "id",
                "inputs": [item for item in node.get("inputs") or [] if isinstance(item, str)],
                "owner": node.get("owner") if isinstance(node.get("owner"), str) else None,
                "has_acceptance": bool(node.get("acceptance")),
            }
            tick.events.append(_event(
                "dag", file_id, field_line, _line_offset(text, field_line),
                _hash(_canonical({"id": node_id, "inputs": facts["inputs"]})),
                None, feature, str(node_id), None, "dag_node", facts,
            ))
            emitted += 1
        if one_status != "ok":
            status = _finish_status(status, one_status)
            detail = one_detail or detail
    if missing:
        status = _finish_status(status, "partial")
        detail = "missing dag for " + ", ".join(missing)
    tick.note("dag", status, dag_dir, emitted, detail)


def _id_line(text: str, node_id: str) -> int:
    pattern = re.compile(r'"id"\s*:\s*"' + re.escape(node_id) + r'"')
    for number, line in enumerate(text.splitlines(), 1):
        if pattern.search(line):
            return number
    return 1


def _dag_path(folder: Path, feature: str) -> Path | None:
    try:
        from companion.feature_team.dag import live_path
        return live_path(folder, feature)
    except (OSError, ValueError, TypeError):
        return None


def _read_review(tick: _Tick, path: Path):
    if not path.exists():
        tick.note("review", "absent", path, 0, "no review history")
        return
    file_id = _display(path, tick.root)
    records, status, detail = _pull_lines(tick, path, file_id)
    if status == "missing":
        tick.note("review", "absent", path, 0, "no review history")
        return
    emitted = 0
    bad = False
    for record in records:
        text = _decode(record["raw"])
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            bad = True
            tick.events.append(_event(
                "review", file_id, record["line"], record["offset"], record["record_hash"],
                None, None, None, None, "malformed", {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        feature = row.get("feature") if isinstance(row.get("feature"), str) else None
        node = row.get("node") if isinstance(row.get("node"), str) else None
        kind = "review_mark" if row.get("kind") == "mark" else "review_verified"
        facts = {
            "field": "kind",
            "state": row.get("status") if row.get("kind") == "mark" else row.get("kind"),
            "sha": row.get("sha") if isinstance(row.get("sha"), str) else None,
            "by": row.get("by") if isinstance(row.get("by"), str) else None,
        }
        tick.events.append(_event(
            "review", file_id, record["line"], record["offset"], record["record_hash"],
            _parse_time(row.get("t")), feature, node,
            row.get("run_id") if isinstance(row.get("run_id"), str) else None,
            kind, facts,
        ))
        emitted += 1
    final = "malformed" if bad else ("ok" if status == "ok" else status)
    tick.note("review", final, path, emitted, "malformed review line" if bad else detail)


def _landing_kind(rest: str) -> str:
    if "NOT fast-forwarded" in rest:
        return "landing_fast_forward"
    if "fast-forwarded" in rest:
        return "landing_fast_forward"
    if "transient refusal" in rest or "retry " in rest and " in " in rest:
        return "landing_retry_note"
    if re.search(r"refused \d+ times", rest):
        return "landing_retry_cap"
    if rest.startswith("ALERT:") or " ALERT:" in rest:
        return "landing_alert"
    if "LANDED:" in rest:
        return "landing_landed"
    if "REFUSED:" in rest or "precheck refused" in rest:
        return "landing_refused"
    return "landing_note"


def _landing_name(rest: str) -> str | None:
    if rest.startswith("main checkout") or rest.startswith("ALERT:") or rest.startswith("queue "):
        return None
    if rest.startswith("placement:"):
        parts = rest.split()
        return parts[1] if len(parts) > 1 else None
    token = rest.split(" ", 1)[0]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", token):
        return token
    return None


def _first_seen(tick: _Tick, file_id: str, record_hash: str) -> float:
    bucket = tick.first_seen.setdefault(file_id, {})
    if record_hash not in bucket:
        bucket[record_hash] = tick.now
    return float(bucket[record_hash])


def _read_landing_log(tick: _Tick, path: Path):
    file_id = "team/landing/queue.log"
    records, status, detail = _pull_lines(tick, path, file_id)
    emitted = 0
    for record in records:
        text = _decode(record["raw"]).strip()
        match = _CLOCK_RE.match(text)
        clock = match.group("clock") if match else None
        rest = match.group("rest") if match else text
        when = _first_seen(tick, file_id, record["record_hash"]) if clock else _parse_time(text[:40])
        name = _landing_name(rest)
        if name:
            tick.queue_names.append(name)
        facts = {
            "field": "line",
            "clock": clock,
            "time_basis": "first_seen" if clock else None,
            "name": name,
            "text": _excerpt(rest, 240),
            "refusal_class": _refusal_class(rest),
            "fast_forward_ok": False if "NOT fast-forwarded" in rest else True if "fast-forwarded" in rest else None,
        }
        tick.events.append(_event(
            "landing_log", file_id, record["line"], record["offset"], record["record_hash"],
            when, None, None, name, _landing_kind(rest), facts,
        ))
        emitted += 1
    tick.note("landing_log", status, path, emitted, detail)


def _read_queue(tick: _Tick, path: Path):
    file_id = "team/landing/queue.txt"
    records, status, detail = _pull_lines(tick, path, file_id)
    emitted = 0
    bad = False
    for record in records:
        text = _decode(record["raw"]).strip()
        if not text or text.startswith("#"):
            continue
        entry, reason = _parse_queue_line(text)
        if entry is None:
            bad = True
            tick.events.append(_event(
                "landing_queue", file_id, record["line"], record["offset"], record["record_hash"],
                None, None, None, None, "malformed",
                {"field": "line", "reason": reason, "text": _excerpt(text, 80)},
            ))
            emitted += 1
            continue
        tick.queue_names.append(entry["name"])
        facts = {"field": "line", **entry}
        tick.events.append(_event(
            "landing_queue", file_id, record["line"], record["offset"], record["record_hash"],
            None, None, None, entry["name"], "landing_queue", facts,
        ))
        emitted += 1
    final = "malformed" if bad else status
    tick.note("landing_queue", final, path, emitted, "malformed queue line" if bad else detail)


def _parse_queue_line(text: str):
    tokens = text.split()
    if len(tokens) < 3 or tokens[1] not in ("system", "web"):
        return None, "bad queue line"
    name, kind, rest = tokens[0], tokens[1], tokens[2:]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return None, "bad train name"
    envs = []
    while rest and re.match(r"^[A-Z_][A-Z0-9_]*=", rest[0]):
        envs.append(rest.pop(0))
    if not rest:
        return None, "queue line has no branches"
    priority = 99
    for env in envs:
        if env.startswith("PRIORITY="):
            if not env[9:].isdigit():
                return None, "bad PRIORITY"
            priority = int(env[9:])
    return {
        "name": name,
        "train_kind": kind,
        "priority": priority,
        "branches": rest,
        "envs": [env.split("=", 1)[0] for env in envs],
    }, ""


def _read_retry(tick: _Tick, path: Path):
    file_id = "team/landing/queue-retry.json"
    if not path.exists():
        tick.note("landing_retry", "absent", path, 0, "no retry state")
        return
    text, status, detail = _read_text(tick, path)
    if text is None:
        mapped = "absent" if status == "missing" else status
        tick.note("landing_retry", mapped, path, 0, detail)
        return
    try:
        document = json.loads(text)
        if not isinstance(document, dict):
            raise ValueError
    except ValueError:
        tick.note("landing_retry", "malformed", path, 0, "invalid retry state")
        tick.events.append(_event(
            "landing_retry", file_id, 1, 0, _hash(text.encode("utf-8")), None, None, None, None,
            "malformed", {"field": None, "reason": "invalid_json"},
        ))
        return
    if not _json_file_changed(tick, path, file_id, text) and status == "ok":
        tick.note("landing_retry", "ok", path, 0, "")
        return
    emitted = 0
    for key, row in document.items():
        if not isinstance(row, dict):
            continue
        field_line = _json_key_line(text, str(key))
        after = row.get("after") if type(row.get("after")) in (int, float) else None
        count = row.get("n") if type(row.get("n")) is int else None
        facts = {
            "field": "after",
            "n": count,
            "after": after,
            "pending": bool(after is not None and after > tick.now),
            "queue_line": _excerpt(str(key), 160),
        }
        name = str(key).split(" ", 1)[0]
        tick.events.append(_event(
            "landing_retry", file_id, field_line, _line_offset(text, field_line),
            _hash(_canonical({key: row})), after, None, None, name if name else None,
            "landing_retry", facts,
        ))
        emitted += 1
    tick.note("landing_retry", "malformed" if status == "malformed" else status, path, emitted, detail)


def _json_key_line(text: str, key: str) -> int:
    needle = json.dumps(key)
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return 1


def _read_train_logs(tick: _Tick, landing: Path):
    if not landing.exists():
        tick.note("train_log", "missing", landing, 0, "landing directory missing")
        return
    names = []
    for name in tick.queue_names:
        if name and name not in names and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            names.append(name)
    emitted = 0
    status, detail = "ok", ""
    seen = False
    for name in names:
        for suffix in ("-precheck.log", "-run.log"):
            path = landing / f"{name}{suffix}"
            if not path.exists():
                continue
            seen = True
            file_id = _display(path, tick.root)
            records, one_status, one_detail = _pull_lines(tick, path, file_id)
            if one_status not in {"ok", "partial"}:
                status = _finish_status(status, one_status)
                detail = one_detail or detail
            for record in records:
                text = _decode(record["raw"]).strip()
                if not re.search(r"refused|failed|LANDED|fast-forward|headroom|reviewed pin|runner held", text, re.I):
                    continue
                facts = {
                    "field": "line",
                    "name": name,
                    "text": _excerpt(text, 240),
                    "refusal_class": _refusal_class(text),
                    "log": path.name,
                }
                tick.events.append(_event(
                    "train_log", file_id, record["line"], record["offset"], record["record_hash"],
                    None, None, None, name, "train_log", facts,
                ))
                emitted += 1
            if one_status == "partial":
                status = "partial"
                detail = one_detail
    if not seen and status == "ok":
        detail = "no referenced train log"
    tick.note("train_log", status, landing, emitted, detail)


def _read_jsonl_records(tick: _Tick, path: Path, source: str, file_id: str, interpret):
    if not path.exists():
        return "absent", "not found", 0
    records, status, detail = _pull_lines(tick, path, file_id)
    emitted = 0
    bad = False
    for record in records:
        text = _decode(record["raw"])
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            bad = True
            tick.events.append(_event(
                source, file_id, record["line"], record["offset"], record["record_hash"],
                None, None, None, None, "malformed", {"field": None, "reason": "invalid_json"},
            ))
            emitted += 1
            continue
        event = interpret(row, record)
        if event is not None:
            tick.events.append(event)
            emitted += 1
    final = "malformed" if bad else status
    extra = "malformed line" if bad else detail
    return final, extra, emitted


def _read_board(tick: _Tick, path: Path):
    file_id = "team/board-inbox.jsonl"

    def interpret(row, record):
        feature = row.get("feature") if isinstance(row.get("feature"), str) else None
        text = row.get("text") if isinstance(row.get("text"), str) else ""
        facts = {
            "field": "text",
            "from": row.get("from") if isinstance(row.get("from"), str) else None,
            "to": row.get("to") if isinstance(row.get("to"), str) else None,
            "routing_id": row.get("routing_id") if isinstance(row.get("routing_id"), str) else None,
            "pattern": row.get("pattern") if isinstance(row.get("pattern"), str) else None,
            "text": _excerpt(text, 300),
        }
        kind = row.get("kind") if isinstance(row.get("kind"), str) and row.get("kind") else "inbox"
        return _event(
            "board_inbox", file_id, record["line"], record["offset"], record["record_hash"],
            _parse_time(row.get("t")), feature,
            row.get("node") if isinstance(row.get("node"), str) else None,
            row.get("run_id") if isinstance(row.get("run_id"), str) else None,
            kind, facts,
        )

    status, detail, emitted = _read_jsonl_records(tick, path, "board_inbox", file_id, interpret)
    if status == "absent":
        status = "missing"
    tick.note("board_inbox", status, path, emitted, detail)


def _read_qc_jsonl(tick: _Tick, path: Path, source: str):
    file_id = _display(path, tick.root)

    def interpret(row, record):
        facts = {
            "field": "routing_id" if "routing_id" in row else "id",
            "routing_id": row.get("routing_id") or row.get("incident_id") or row.get("id"),
            "owner": row.get("owner") if isinstance(row.get("owner"), str) else None,
            "pattern": row.get("pattern") if isinstance(row.get("pattern"), str) else None,
            "outcome": row.get("outcome") if isinstance(row.get("outcome"), str) else None,
            "text": _excerpt(row.get("text") or row.get("note") or "", 240),
        }
        return _event(
            source, file_id, record["line"], record["offset"], record["record_hash"],
            _parse_time(row.get("t")),
            row.get("feature") if isinstance(row.get("feature"), str) else None,
            row.get("node") if isinstance(row.get("node"), str) else None,
            row.get("run_id") if isinstance(row.get("run_id"), str) else None,
            source, facts,
        )

    if not path.exists():
        tick.note(source, "absent", path, 0, "not found")
        return
    status, detail, emitted = _read_jsonl_records(tick, path, source, file_id, interpret)
    tick.note(source, status, path, emitted, detail)


def _read_inspector(tick: _Tick, path: Path | None):
    if path is None:
        tick.note("inspector_log", "unconfigured", None, 0, "scratchpad inspector log is not configured")
        return
    path = Path(path)
    if not path.exists():
        tick.note("inspector_log", "missing", path, 0, "not found")
        return
    file_id = _display(path, tick.root)
    records, status, detail = _pull_lines(tick, path, "inspector_log")
    emitted = 0
    for record in records:
        text = _decode(record["raw"]).strip()
        row = None
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                row = loaded
        except ValueError:
            row = None
        if row is None:
            facts = {
                "field": "line",
                "text": _excerpt(text, 240),
                "gave_up": "gave up" in text.lower() or "after four restarts" in text.lower()
                or "after 4 restarts" in text.lower(),
                "restarts": _restart_count(text),
            }
            feature = None
            run_id = None
            kind = "inspector_log"
        else:
            feature = row.get("feature") if isinstance(row.get("feature"), str) else None
            if feature is None and isinstance(row.get("team"), str):
                feature = row["team"]
            run_id = row.get("ledger_id") or row.get("job") or row.get("id")
            run_id = run_id if isinstance(run_id, str) else None
            facts = {
                "field": "restart_count" if "restart_count" in row else "line",
                "restarts": row.get("restart_count") if type(row.get("restart_count")) is int else _restart_count(text),
                "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else None,
                "folder": row.get("folder") if isinstance(row.get("folder"), str) else None,
                "gave_up": bool(row.get("gave_up")),
                "observe_only": bool(row.get("observe_only")),
                "text": _excerpt(row.get("text") or "", 240),
            }
            kind = "inspector_log"
        tick.events.append(_event(
            "inspector", file_id, record["line"], record["offset"], record["record_hash"],
            _parse_time((row or {}).get("t")), feature,
            (row or {}).get("node") if isinstance((row or {}).get("node"), str) else None,
            run_id, kind, facts,
        ))
        emitted += 1
    tick.note("inspector_log", status, path, emitted, detail)


def _restart_count(text: str) -> int | None:
    match = re.search(r"(\d+)\s+restarts", text, re.I)
    return int(match.group(1)) if match else None


def _read_registry(tick: _Tick, path: Path | None):
    if path is None:
        tick.note("registry", "unconfigured", None, 0, "job registry path is not configured")
        return
    path = Path(path)
    if not path.exists():
        tick.note("registry", "missing", path, 0, "not found")
        return
    text, status, detail = _read_text(tick, path)
    if text is None:
        tick.note("registry", status, path, 0, detail)
        return
    file_id = _display(path, tick.root)
    jobs = _registry_jobs(text)
    if jobs is None:
        tick.note("registry", "malformed", path, 0, "registry is not a job list")
        tick.events.append(_event(
            "registry", file_id, 1, 0, _hash(text.encode("utf-8")), None, None, None, None,
            "malformed", {"field": None, "reason": "invalid_json"},
        ))
        return
    if not _json_file_changed(tick, path, file_id, text) and status == "ok":
        tick.note("registry", "ok", path, 0, "")
        return
    emitted = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id") or job.get("job") or job.get("ledger_id")
        job_id = job_id if isinstance(job_id, str) else None
        cwd = job.get("cwd") if isinstance(job.get("cwd"), str) else None
        worktree = job.get("worktree") or job.get("folder") or job.get("claimed_worktree")
        worktree = worktree if isinstance(worktree, str) else None
        mismatch = False
        if cwd and worktree:
            mismatch = os.path.normpath(cwd) != os.path.normpath(worktree)
        field_line = _json_key_line(text, job_id) if job_id else 1
        feature = job.get("feature") if isinstance(job.get("feature"), str) else None
        facts = {
            "field": "id" if job_id else "jobs",
            "cwd": cwd,
            "worktree": worktree,
            "folder_mismatch": mismatch,
            "ledger_id": job.get("ledger_id") if isinstance(job.get("ledger_id"), str) else job_id,
            "observe_only": bool(job.get("observe_only")),
            "restart": bool(job.get("restart")) if "restart" in job else None,
        }
        tick.events.append(_event(
            "registry", file_id, field_line, _line_offset(text, field_line),
            _hash(_canonical(job)), _parse_time(job.get("t")), feature,
            job.get("node") if isinstance(job.get("node"), str) else None,
            facts["ledger_id"], "registry_job", facts,
        ))
        emitted += 1
    tick.note("registry", status, path, emitted, detail)


def _registry_jobs(text: str):
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict) and isinstance(document.get("jobs"), list):
        return document["jobs"]
    if isinstance(document, list):
        return document
    if document is not None:
        return None
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        jobs.append(row)
    return jobs


def _scope_of(config: dict, groups: dict[str, list[str]]):
    scope = config.get("scope") or "all"
    if scope == "all":
        return "all", None, None
    if isinstance(scope, str) and scope.startswith("feature:"):
        return "feature", scope.split(":", 1)[1], None
    if isinstance(scope, str) and scope.startswith("group:"):
        name = scope.split(":", 1)[1]
        if name not in groups:
            return "group", name, f"unknown group {name}"
        return "group", name, None
    if isinstance(scope, dict) and isinstance(scope.get("feature"), str):
        return "feature", scope["feature"], None
    if isinstance(scope, dict) and isinstance(scope.get("group"), str):
        name = scope["group"]
        if name not in groups:
            return "group", name, f"unknown group {name}"
        return "group", name, None
    return "all", None, f"unrecognized scope {scope!r}"


def _select(events: list[dict], mode: str, scope_id: str | None, groups: dict[str, list[str]]):
    if mode == "all":
        unassigned = [
            {"source": event["source"], "file_id": event["file_id"], "line": event["line"], "run_id": event["run_id"]}
            for event in events
            if event["source"] not in GLOBAL_SOURCES and not event.get("feature")
        ]
        return events, unassigned, 0
    allowed = {scope_id} if mode == "feature" else set(groups.get(scope_id) or [])
    chosen = []
    unassigned = []
    out = 0
    for event in events:
        if event["source"] in GLOBAL_SOURCES:
            chosen.append(event)
            continue
        feature = event.get("feature")
        if not feature:
            unassigned.append({
                "source": event["source"], "file_id": event["file_id"],
                "line": event["line"], "run_id": event["run_id"],
            })
            continue
        if feature in allowed:
            chosen.append(event)
        else:
            out += 1
    return chosen, unassigned, out


def collect(config, cursor):
    """Read one tick of configured QC sources.

    ``config`` is a plain dict. ``data_root`` selects the rehearsal root
    (otherwise ``KYRA_DATA_DIR``, otherwise the checkout ``data/``).
    ``ledger`` overrides ``KYRA_CONTEXT_LEDGER``. ``scope`` is ``"all"``,
    ``{"feature": ID}`` or ``{"group": NAME}``. Group membership is
    ``config["groups"]`` or ``team/qc/groups.json``, matched as exact feature
    ids. ``cursor`` is the previous ``next_cursor`` or None.

    The returned cursor is what this call finished reading. The caller stores
    it after durable processing; this function does not write it.
    """
    config = dict(config or {})
    cursor = dict(cursor or {})
    root = _data_root(config)
    ledger = _ledger_path(config, root)
    roots = [root]
    for extra in config.get("log_roots") or []:
        roots.append(Path(extra))
    explicit = [ledger]
    for key in ("inspector_log", "registry"):
        if config.get(key):
            explicit.append(Path(config[key]))
    tick = _Tick(root, config, cursor, roots, explicit)
    groups = _load_groups(tick)
    mode, scope_id, scope_error = _scope_of(config, groups)

    landing = root / "team" / "landing"
    inspector = config.get("inspector_log")
    registry = config.get("registry")
    if inspector is None and "inspector_log" not in config:
        configured = root / "team" / "qc" / "inspector.log"
        inspector = configured if configured.exists() else None
    if registry is None and "registry" not in config:
        configured = root / "team" / "qc" / "registry.json"
        registry = configured if configured.exists() else None

    _read_ledger(tick, ledger)
    _read_runs(tick, root / "team" / "loop" / "runs")
    _read_features(tick, root / "team" / "features", root / "team" / "feature-teams")
    _read_review(tick, root / "team" / "loop" / "ready-to-board.jsonl")
    _read_queue(tick, landing / "queue.txt")
    _read_landing_log(tick, landing / "queue.log")
    _read_retry(tick, landing / "queue-retry.json")
    _read_train_logs(tick, landing)
    _read_inspector(tick, Path(inspector) if inspector else None)
    _read_registry(tick, Path(registry) if registry else None)
    _read_board(tick, root / "team" / "board-inbox.jsonl")
    qc = root / "team" / "qc"
    _read_qc_jsonl(tick, qc / "decisions.jsonl", "qc_decision")
    _read_qc_jsonl(tick, qc / "outcomes.jsonl", "qc_outcome")
    _read_qc_jsonl(tick, qc / "incidents.jsonl", "qc_incident")

    selected, unassigned, out_of_scope = _select(tick.events, mode, scope_id, groups)
    if scope_error:
        selected = [event for event in selected if event["source"] in GLOBAL_SOURCES]
    scratch = "ok"
    inspector_status = tick.sources.get("inspector_log", {}).get("status")
    registry_status = tick.sources.get("registry", {}).get("status")
    if inspector_status == "unconfigured" or registry_status == "unconfigured":
        scratch = "unmapped"
    elif inspector_status not in {"ok", "absent"} or registry_status not in {"ok", "absent"}:
        scratch = "incomplete"
    complete = scratch == "ok" and not tick.bounded and not scope_error
    for name in _REQUIRED:
        row = tick.sources.get(name)
        if row is None or row["status"] not in {"ok", "absent"}:
            complete = False
            break
    health = {
        "complete": complete,
        "bounded": tick.bounded,
        "bytes_read": tick.bytes_read,
        "read_budget": tick.budget,
        "scope": mode,
        "scope_id": scope_id,
        "scope_error": scope_error,
        "scratchpad": scratch,
        "sources": tick.sources,
        "unassigned": unassigned,
        "out_of_scope": out_of_scope,
    }
    next_cursor = {
        "version": 1,
        "files": tick.file_state,
        "snapshots": tick.snapshots,
        "first_seen": tick.first_seen,
    }
    return selected, next_cursor, health


def _config_from_args(args) -> dict:
    scope: object = "all"
    if args.scope == "feature":
        scope = {"feature": args.feature or ""}
    elif args.scope == "group":
        scope = {"group": args.group or ""}
    config = {"scope": scope}
    if args.data:
        config["data_root"] = args.data
    if args.inspector_log:
        config["inspector_log"] = args.inspector_log
    if args.registry:
        config["registry"] = args.registry
    return config


def main(argv: list[str] | None = None) -> int:
    """One fixture collection. Prints events, cursor and health as JSON. Writes nothing."""
    parser = argparse.ArgumentParser(description="Collect one QC tick from configured local sources")
    parser.add_argument("--once", action="store_true", help="read one tick and exit")
    parser.add_argument("--scope", choices=("all", "feature", "group"), default="all")
    parser.add_argument("--feature", default=None)
    parser.add_argument("--group", default=None)
    parser.add_argument("--data", default=None, help="data root (else KYRA_DATA_DIR, else checkout data/)")
    parser.add_argument("--cursor", default=None, help="path of a previous cursor JSON")
    parser.add_argument("--inspector-log", default=None)
    parser.add_argument("--registry", default=None)
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("--once is required")
    cursor = None
    if args.cursor:
        cursor = json.loads(Path(args.cursor).read_text(encoding="utf-8"))
    events, next_cursor, health = collect(_config_from_args(args), cursor)
    json.dump({"events": events, "cursor": next_cursor, "health": health}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if health.get("scope_error"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
