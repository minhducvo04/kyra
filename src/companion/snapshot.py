"""Local data snapshots: consistent SQLite copies and shared unchanged files."""
import fcntl
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)
EXCLUDED = frozenset({
    "bench_hf", "local_llm_models", "router_ft", "search_index",
    "generated_resumes", "voice_models", "tool_ft",
})
_STAMP = "%Y%m%dT%H%M%S%fZ"


class SnapshotRefused(ValueError):
    """No snapshot was published because copying or rotation was unsafe."""


def _files(directory: Path, excluded=frozenset()):
    for path in sorted(directory.iterdir()):
        if path.name in excluded:
            continue
        if path.is_symlink():
            raise SnapshotRefused(f"Snapshot refuses symbolic links: {path}")
        if path.is_dir():
            yield from _files(path)
        elif path.is_file():
            yield path
        else:
            raise SnapshotRefused(f"Snapshot requires regular files: {path}")


def list_snapshots(root: Path) -> list[Path]:
    """List completed timestamp directories, oldest first; ignore temporary copies."""
    if not root.exists():
        return []
    found = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            stamp = datetime.strptime(path.name, _STAMP)
        except ValueError:
            continue
        if stamp.strftime(_STAMP) == path.name:
            found.append(path)
    return sorted(found)


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as source:
        return source.read(16) == b"SQLite format 3\x00"


def _copy_database(source: Path, target: Path) -> None:
    # Read-only prevents a misspelled path from creating an empty live database.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)


def _same_content(source: Path, previous: Path) -> bool:
    with source.open("rb") as live, previous.open("rb") as saved:
        return hashlib.file_digest(live, "sha256").digest() == hashlib.file_digest(saved, "sha256").digest()


def _copy_file(source, target):
    shutil.copy2(source, target)
    # Live files can be protected with uchg; copies must remain rotatable/editable.
    if hasattr(os, "chflags"):
        os.chflags(target, 0)
    return target


def take_snapshot(data_dir: Path, root: Path, *, now=None, keep=30) -> Path:
    """Publish a complete copy before rotating; refuse an apparent loss of live data."""
    data_dir, root = Path(data_dir).resolve(), Path(root).resolve()
    if not data_dir.is_dir():
        raise SnapshotRefused("Snapshot source data directory is missing")
    if root.is_relative_to(data_dir) or data_dir.is_relative_to(root):
        raise ValueError("Snapshot root and live data must be separate directories")
    if not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be a positive integer")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotRefused("Another snapshot is already running") from exc
        previous = list_snapshots(root)
        target = root / (now or datetime.now(UTC)).astimezone(UTC).strftime(_STAMP)
        if target.exists():
            raise SnapshotRefused("A snapshot with this timestamp already exists")
        if previous and target.name <= previous[-1].name:
            raise SnapshotRefused("Snapshot time must follow the newest snapshot")
        files = list(_files(data_dir, EXCLUDED))
        databases = {path for path in files if _is_sqlite(path)}
        sidecars = {Path(str(db) + suffix) for db in databases for suffix in ("-wal", "-shm", "-journal")}
        with tempfile.TemporaryDirectory(prefix=".partial-", dir=root) as temporary:
            staging = Path(temporary)
            for source in files:
                if source in sidecars:
                    continue
                relative = source.relative_to(data_dir)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source in databases:
                    _copy_database(source, destination)
                    continue
                old = previous[-1] / relative if previous else None
                current = source.stat()
                if old is not None and old.is_file() and not old.is_symlink():
                    saved = old.stat()
                    if ((current.st_size, current.st_mtime_ns) == (saved.st_size, saved.st_mtime_ns)
                            and _same_content(source, old)):
                        os.link(old, destination)
                        continue
                _copy_file(source, destination)
            if previous:
                size = sum(path.stat().st_size for path in _files(staging))
                prior_size = sum(path.stat().st_size for path in _files(previous[-1]))
                if size < prior_size / 2:
                    raise SnapshotRefused("Live data is smaller than half the newest snapshot; keeping every snapshot")
            staging.rename(target)
        for expired in previous[:max(0, len(previous) + 1 - keep)]:
            shutil.rmtree(expired)
        logger.info("Snapshot saved: %s", target)
        return target


def restore_snapshot(snapshot: Path, into: Path) -> Path:
    """Copy a snapshot into a new directory, with independent file contents."""
    snapshot, into = Path(snapshot).resolve(), Path(into).absolute()
    if into.exists() or into.is_symlink():
        raise ValueError("Restore destination must be a new directory; never restore over live data")
    if into.resolve().is_relative_to(snapshot.parent):
        raise ValueError("Restore destination must be outside snapshot storage")
    list(_files(snapshot))  # Refuse symlinks before copytree can follow one.
    into.mkdir(parents=True, mode=0o700)
    try:
        shutil.copytree(snapshot, into, dirs_exist_ok=True, copy_function=_copy_file)
    except BaseException:
        shutil.rmtree(into)
        raise
    return into
