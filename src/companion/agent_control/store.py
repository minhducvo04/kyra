"""The append-only, hash-chained event store (AC0 sections 4 and 8).

One JSON line per event under ``<data dir>/agent_control/events/<UTC
date>.jsonl``, the date taken from the event's own ``ts``. Each file is its
own chain: the first line's ``prev_hash`` is the genesis value, every later
line links to the previous line's ``hash`` in the same file, so a backfill
into an older date file cannot break a newer file's chain. Every append
holds one flock, validates the event, skips an event id already stored
(ingest is idempotent), then writes, flushes and fsyncs.

Identity is settled *before* redaction: ``event_id`` is computed from the
original ``source_ref`` and the unredacted row is validated first. The row
on disk keeps that ``event_id`` and a separately redacted display copy of
``source_ref`` (and of every other free-text field), so a secret-shaped
ref can neither leak nor collapse two distinct refs into one stored key;
dedup keys on ``(source, event_id)``.

A small sidecar, ``<data dir>/agent_control/events.head``, records each
date file's latest chain head (line count and last hash). It is rewritten
atomically (temp file, fsync, ``os.replace``) under the same lock on every
append, so ``verify_chain`` detects a deleted last line, a torn last line
and a deleted date file, none of which a bare chain recompute can see.
Writers refuse inconsistent history or a missing/invalid head; repair is
separate from ingest. A batch scans history once under its writer lock.
Verification never creates store paths: it shares an existing lock or checks
a stable snapshot when no lock exists, and fails closed on contention or change.

Legacy reading tolerates damage row by row: a torn last line and a row whose
hash does not recompute are reported (log, warning, and ``verify_chain``)
and skipped, never raised, so one bad row cannot hide the whole store.
The strict descriptor-based read returns events only after the complete captured
snapshot passes that same integrity scan; it never reopens a pathname to decode.
Nothing here opens a source file, a ``.env`` file or a network connection.
"""

import json
import logging
import os
import re
import stat
import warnings
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion.agent_control import events as event_model
from companion.agent_control.redact import scrub

log = logging.getLogger(__name__)


def _diagnostic(exc):
    """Only allowlisted field names and fixed reason codes reach diagnostics."""
    field = str(exc).partition(":")[0]
    allowed = (*event_model.FIELDS, "event", "store",
               *(f"attrs.{key}" for key in event_model.ATTRS_KEYS))
    if field not in allowed:
        field = "event"
    return f"{field}: invalid_value"


@dataclass(frozen=True)
class ChainReport:
    """The first chain problem found, or ``ok`` with the lines checked."""

    ok: bool
    checked: int = 0
    file: str | None = None
    line: int | None = None
    reason: str | None = None
    expected: str | None = None
    actual: str | None = None

    def __bool__(self):
        return self.ok


@dataclass(frozen=True)
class SnapshotRead:
    """Events and integrity from one capture; failures never expose partial rows."""

    events: tuple[event_model.Event, ...]
    chain: ChainReport


@dataclass(frozen=True)
class _CapturedSnapshot:
    files: tuple[tuple[str, bytes], ...]
    head: bytes | None


class _SnapshotChanged(Exception):
    """Retry capture without including filesystem values in diagnostics."""


def _file_state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _state_at(parent_fd, name, *, directory=False):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise OSError("unsafe snapshot entry")
    return _file_state(info)


def _open_at(stack, parent_fd, name, state, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise _SnapshotChanged from None
    stack.callback(os.close, fd)
    if _file_state(os.fstat(fd)) != state:
        raise _SnapshotChanged
    return fd


def _read_at(stack, parent_fd, name, state):
    """Read at most the observed size, then reject growth, shrinkage or replacement."""
    fd = _open_at(stack, parent_fd, name, state)
    remaining = state[3]
    parts = []
    while remaining:
        part = os.read(fd, min(remaining, 1024 * 1024))
        if not part:
            raise _SnapshotChanged
        parts.append(part)
        remaining -= len(part)
    if os.read(fd, 1) or _file_state(os.fstat(fd)) != state:
        raise _SnapshotChanged
    return b"".join(parts)


def _data_root(data_dir):
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.environ.get("KYRA_DATA_DIR") or "data")


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class EventStore:
    """``EventStore(data_dir)``; the stream lives in ``<data_dir>/agent_control/events/``."""

    def __init__(self, data_dir=None):
        self.root = _data_root(data_dir) / "agent_control"
        self.events_dir = self.root / "events"
        self.head_path = self.root / "events.head"
        self._lock_path = self.root / "events.lock"
        self.append_errors = []
        self.last_append_errors = self.append_errors

    # -- paths and locking -------------------------------------------------
    def _file_for(self, ts):
        day = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
        return self.events_dir / f"{day}.jsonl"

    def _files(self):
        if not self.events_dir.is_dir():
            return []
        return sorted(self.events_dir.glob("*.jsonl"))

    @contextmanager
    def _locked(self):
        import fcntl

        self.events_dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # -- the chain-head sidecar ---------------------------------------------
    def _load_head(self):
        """The sidecar dict, or None when it is absent or unreadable."""
        try:
            body = self.head_path.read_bytes()
        except FileNotFoundError:
            return None
        return self._decode_head(body)

    @staticmethod
    def _decode_head(body):
        if body is None:
            return None
        try:
            head = json.loads(body.decode("utf-8"))
        except ValueError:
            return None
        if not isinstance(head, dict) or not isinstance(head.get("files"), dict):
            return None
        for name, entry in head["files"].items():
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", name) or not isinstance(entry, dict):
                return None
            count, digest = entry.get("count"), entry.get("hash")
            if type(count) is not int or count < 0 or not isinstance(digest, str) \
                    or not re.fullmatch(r"[0-9a-f]{64}", digest):
                return None
        return head

    def _write_head(self, head):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / f".events.head.tmp-{os.getpid()}"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(head, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.head_path)
        _fsync_dir(self.root)

    def _update_head(self, head, path, count, last_hash):
        """Advance only a head already checked under the writer lock."""
        head["files"][path.name] = {"count": count, "hash": last_hash}
        head["file"] = path.name
        head["hash"] = last_hash
        head["count"] += 1
        self._write_head(head)

    # -- reading -----------------------------------------------------------
    def _rows(self, path, *, warn_torn=True, include_invalid=False):
        """Tolerant reads skip damage; integrity scans retain its physical line."""
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            return []
        return self._decode_rows(body, path.name, warn_torn=warn_torn,
                                 include_invalid=include_invalid)

    @staticmethod
    def _decode_rows(body, name, *, warn_torn=True, include_invalid=False):
        lines = body.decode("utf-8").splitlines()
        rows = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                rows.append((number, json.loads(line)))
            except ValueError:
                if include_invalid:
                    rows.append((number, None))
                elif number == len(lines):
                    if warn_torn:
                        message = f"agent_control: torn last line skipped in {name}:{number}"
                        log.warning(message)
                        warnings.warn(message, RuntimeWarning, stacklevel=2)
                else:
                    log.warning("agent_control: unparseable line skipped in %s:%s", name, number)
        return rows

    def read_events(self):
        found = []
        for path in self._files():
            for number, row in self._rows(path):
                if not isinstance(row, dict):
                    log.warning("agent_control: non-object row skipped in %s:%s", path.name, number)
                    continue
                try:
                    found.append(event_model.Event.from_dict(row, chained=True))
                except (ValueError, TypeError, KeyError) as exc:
                    message = (f"agent_control: invalid row skipped in {path.name}:{number}: "
                               f"{_diagnostic(exc)}")
                    log.warning(message)
                    warnings.warn(message, RuntimeWarning, stacklevel=2)
        return found

    iter_events = read_events

    def __len__(self):
        return sum(len(self._rows(path, warn_torn=False)) for path in self._files())

    # -- appending ---------------------------------------------------------
    def _prepare(self, event):
        row = event.to_dict() if isinstance(event, event_model.Event) else dict(event)
        if not row.get("event_id"):
            row["event_id"] = event_model.make_event_id(row.get("source", ""), row.get("source_ref", ""))
        event_model.validate_event(row)  # before redaction, against the original ref
        cleaned = scrub(row)
        for name in ("run_id", "feature", "node"):
            if cleaned[name] != row[name]:
                raise ValueError(f"{name}: secret-shaped id refused")
        return cleaned

    def _append_locked(self, row, keys, head):
        key = (row["source"], row["event_id"])
        if key in keys:
            return False
        path = self._file_for(row["ts"])
        entry = head["files"].get(path.name, {"count": 0, "hash": event_model.GENESIS})
        row["prev_hash"] = entry["hash"]
        row["hash"] = event_model.compute_hash(row)
        event_model.validate_event(row, chained=True)
        new_file = not path.is_file()
        prefix = ""
        if not new_file and path.stat().st_size > 0:
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                prefix = "" if handle.read() == b"\n" else "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(prefix + event_model.canonical_json(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if new_file:
            _fsync_dir(self.events_dir)
        self._update_head(head, path, entry["count"] + 1, row["hash"])
        keys.add(key)
        return True

    def append(self, event):
        """Append one event; refuse damaged history; return False for a replay."""
        row = self._prepare(event)
        with self._locked():
            report, keys, head = self._scan()
            if not report.ok:
                raise ValueError("store: integrity check failed; repair required")
            return self._append_locked(row, keys, head)

    def append_many(self, batch):
        """Skip invalid inputs, dedupe under one lock, and refuse damaged history."""
        self.append_errors = []
        self.last_append_errors = self.append_errors
        appended = 0
        with self._locked():
            report, keys, head = self._scan()
            if not report.ok:
                raise ValueError("store: integrity check failed; repair required")
            for event in batch:
                try:
                    appended += int(self._append_locked(self._prepare(event), keys, head))
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    message = f"agent_control: invalid event skipped: {_diagnostic(exc)}"
                    self.append_errors.append(message)
                    log.warning(message)
                    warnings.warn(message, RuntimeWarning, stacklevel=2)
        return appended

    def ingest(self, sources=None):
        """Append every registered adapter's events; return how many were new."""
        registered = event_model.adapters()
        appended = 0
        for source, iter_events in registered.items():
            if sources is not None and source not in sources:
                continue
            appended += self.append_many(iter_events())
        return appended

    # -- verification ------------------------------------------------------
    def read_verified_snapshot(self, *, root_fd: int) -> SnapshotRead:
        """Read only through a borrowed authorized agent_control directory fd.

        The caller owns anchored traversal to root_fd and keeps it open until
        return. Its inode, rather than self.root or its current pathname, is the
        capability. Two attempts bound lock contention and capture races. The
        captured bytes supply both events and verification; failure has no rows.
        """
        import fcntl

        reason = "store changed during verification"
        for _ in range(2):
            try:
                with ExitStack() as stack:
                    if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                        raise OSError("unsafe snapshot root")
                    lock = _state_at(root_fd, "events.lock")
                    if lock is not None:
                        fd = _open_at(stack, root_fd, "events.lock", lock)
                        try:
                            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        except BlockingIOError:
                            reason = "store busy during verification"
                            continue
                        stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
                    reason = "store changed during verification"
                    if _state_at(root_fd, "events.lock") != lock:
                        continue
                    captured = self._capture_snapshot(root_fd)
                    if _state_at(root_fd, "events.lock") != lock:
                        continue
                # Parsing is entirely detached from the namespace and lock now.
                entries = tuple(
                    (name, self._decode_rows(body, name, warn_torn=False, include_invalid=True))
                    for name, body in captured.files
                )
                report, _, _ = self._scan_entries(
                    entries, self._decode_head(captured.head), captured.head is not None,
                )
                if not report.ok:
                    return SnapshotRead((), ChainReport(False, reason="store integrity check failed"))
                events = tuple(event_model.Event.from_dict(row, chained=True)
                               for _, rows in entries for _, row in rows)
                return SnapshotRead(events, report)
            except _SnapshotChanged:
                reason = "store changed during verification"
            except (OSError, UnicodeError):
                return SnapshotRead((), ChainReport(
                    False, reason="store unavailable during verification",
                ))
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                return SnapshotRead((), ChainReport(False, reason="store integrity check failed"))
        return SnapshotRead((), ChainReport(False, reason=reason))

    @staticmethod
    def _capture_snapshot(root_fd):
        """Capture immutable bytes and reject changed bindings/metadata/file sets."""
        with ExitStack() as stack:
            root_state = _file_state(os.fstat(root_fd))
            events_state = _state_at(root_fd, "events", directory=True)
            events_fd = None
            names = []
            if events_state is not None:
                events_fd = _open_at(stack, root_fd, "events", events_state, directory=True)
                names = sorted(name for name in os.listdir(events_fd) if name.endswith(".jsonl"))
            head_state = _state_at(root_fd, "events.head")
            head = None if head_state is None else _read_at(
                stack, root_fd, "events.head", head_state,
            )
            files, states = [], []
            for name in names:
                state = _state_at(events_fd, name)
                if state is None:
                    raise _SnapshotChanged
                files.append((name, _read_at(stack, events_fd, name, state)))
                states.append((name, state))
            if events_fd is not None:
                current = sorted(name for name in os.listdir(events_fd) if name.endswith(".jsonl"))
                if current != names or _file_state(os.fstat(events_fd)) != events_state:
                    raise _SnapshotChanged
                if any(_state_at(events_fd, name) != state for name, state in states):
                    raise _SnapshotChanged
            if (_state_at(root_fd, "events.head") != head_state
                    or _state_at(root_fd, "events", directory=True) != events_state
                    or _file_state(os.fstat(root_fd)) != root_state):
                raise _SnapshotChanged
            return _CapturedSnapshot(tuple(files), head)

    def verify_chain(self):
        """Read and verify history, retrying once on a busy or changing store."""
        import fcntl

        reason = "store changed during verification"
        for _ in range(2):
            try:
                try:
                    fd = os.open(self._lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    before = self._verification_state()
                    if self._verification_lock_identity() is not None:
                        continue
                    report = self._scan()[0]
                    after = self._verification_state()
                    if self._verification_lock_identity() is None and before == after:
                        return report
                    reason = "store changed during verification"
                    continue
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        return ChainReport(False, reason="store unavailable during verification")
                    identity = (info.st_dev, info.st_ino)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        reason = "store busy during verification"
                        continue
                    try:
                        reason = "store changed during verification"
                        if self._verification_lock_identity() != identity:
                            continue
                        report = self._scan()[0]
                        if self._verification_lock_identity() == identity:
                            return report
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            except (OSError, UnicodeError):
                return ChainReport(False, reason="store unavailable during verification")
        return ChainReport(False, reason=reason)

    def _verification_lock_identity(self):
        """Missing is distinct from an unsafe or unreadable existing lock."""
        try:
            info = self._lock_path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise OSError("unsafe verification lock")
        return info.st_dev, info.st_ino

    def _verification_state(self):
        """Observe a lock-free scan; cooperating writers retain their lock path."""
        state = []

        def record(path, *, directory=False):
            try:
                info = path.lstat()
            except FileNotFoundError:
                state.append((path, None))
                return False
            expected = stat.S_ISDIR if directory else stat.S_ISREG
            if not expected(info.st_mode):
                raise OSError("unsafe verification path")
            state.append((path, info.st_dev, info.st_ino, info.st_mode, info.st_size,
                          info.st_mtime_ns, info.st_ctime_ns))
            return True

        record(self.root, directory=True)
        record(self.events_dir, directory=True)
        head_present = record(self.head_path)
        for path in self._files():
            record(path)
        head_bytes = self.head_path.read_bytes() if head_present else None
        return state, head_bytes

    def _scan(self):
        """Shared scan; callers hold a lock or validate an unchanged snapshot."""
        files = self._files()
        head = self._load_head()
        # Keep legacy acquisition under its existing writer/verification protocol.

        def rows(path):
            yield from self._rows(path, warn_torn=False, include_invalid=True)

        entries = [(path.name, rows(path)) for path in files]
        return self._scan_entries(entries, head, head is not None or self.head_path.exists())

    @staticmethod
    def _scan_entries(entries, head, head_present):
        """The single integrity loop for legacy and descriptor-captured reads."""
        checked = 0
        keys = set()
        if head is None:
            if entries or head_present:
                return (ChainReport(False, file=entries[0][0] if entries else None,
                                    reason="head record missing or invalid"), keys, None)
            head = {"files": {}}
        head_files = head["files"]
        for name, rows in entries:
            previous = event_model.GENESIS
            count = 0
            last_line = None
            for number, row in rows:
                checked += 1
                try:
                    event_model.validate_event(row, chained=True)
                except (ValueError, TypeError, KeyError) as exc:
                    return (ChainReport(False, checked, name, number,
                                        _diagnostic(exc)), keys, head)
                if row["prev_hash"] != previous:
                    return (ChainReport(False, checked, name, number,
                                        "prev_hash mismatch (removed or reordered line)",
                                        previous, row["prev_hash"]), keys, head)
                keys.add((row["source"], row["event_id"]))
                previous = row["hash"]
                count += 1
                last_line = number
            entry = head_files.get(name)
            if entry is None:
                return (ChainReport(False, checked, name, last_line,
                                    "no head record for file", None, previous), keys, head)
            if entry["count"] != count or entry["hash"] != previous:
                line = count + 1 if count < entry["count"] else last_line
                return (ChainReport(False, checked, name, line,
                                    "head mismatch (deleted or torn last line)",
                                    entry["hash"], previous), keys, head)
        if head_files:
            present = {name for name, _ in entries}
            for name in sorted(head_files):
                if name not in present:
                    return (ChainReport(False, checked, name, None,
                                        "date file deleted (head record present but file missing)",
                                        head_files[name]["hash"], None), keys, head)
        head["count"] = checked
        return ChainReport(True, checked), keys, head


def __getattr__(name):
    """Lazy fcntl so callers can patch ``store.fcntl`` without a module-level import."""
    if name == "fcntl":
        import fcntl
        return fcntl
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
