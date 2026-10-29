"""Single delivery ledger: locked appends, observations and one-time Team migration.

New rows go to dispatch/deliveries.json. The former team/deliveries.json is
merged once, canonical IDs win, and its bytes are archived after the durable
write. Old-version writers must be stopped before deploying this migration.
"""
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path

from companion.file_lock import exclusive
from companion.paths import DATA_DIR, write_json

DEFAULT_STORE = DATA_DIR / "dispatch" / "deliveries.json"
OPEN_STATES = {"queued", "acknowledged", "running", "ambiguous"}


def path_for(data_root):
    return Path(data_root) / "dispatch" / "deliveries.json"


def has_store(store_path=DEFAULT_STORE):
    """True when the canonical ledger or the legacy file it migrates is on disk."""
    store, legacy = _store_paths(store_path)
    return store.exists() or (legacy is not None and legacy.exists())


def _store_paths(store_path, legacy_store_path=None):
    path = Path(store_path).absolute()
    if path.name == "deliveries.json" and path.parent.name == "team":
        path = path.parent.parent / "dispatch" / path.name
    legacy = Path(legacy_store_path).absolute() if legacy_store_path is not None else (
        path.parent.parent / "team" / path.name
        if path.name == "deliveries.json" and path.parent.name == "dispatch" else None)
    # The old Team reader refused symlinks. Preserve that boundary for both inputs.
    if path.is_symlink() or (legacy is not None and legacy.is_symlink()):
        raise ValueError("Delivery stores must not be symlinks")
    path = path.resolve()
    legacy = legacy.resolve() if legacy is not None else None
    return path, legacy if legacy != path else None


def _read_document(path, *, max_bytes=None):
    if not path.exists():
        return [], "[]"
    with path.open("rb") as stream:
        raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("Delivery store exceeds the observation budget")
    rows = json.loads(raw)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not {"id", "assignment", "state"} <= row.keys()
        or not isinstance(row["id"], str) or not isinstance(row["assignment"], str)
        or not isinstance(row["state"], str)
        or row["state"] not in OPEN_STATES | {"failed", "completed"} for row in rows
    ):
        raise ValueError("Invalid delivery store")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate delivery IDs")
    return rows, raw.decode("utf-8")


def _read_store(path, *, max_bytes=None):
    return _read_document(path, max_bytes=max_bytes)[0]


def _merged_rows(store, legacy, *, max_bytes=None):
    rows = _read_store(store, max_bytes=max_bytes)
    by_id = {row["id"]: row for row in rows}
    for row in _read_store(legacy, max_bytes=max_bytes) if legacy is not None else []:
        current = by_id.get(row["id"])
        if current is None:
            rows.append(row)
            by_id[row["id"]] = row
        elif any(current.get(key) != row.get(key) for key in ("assignment", "app", "task_id", "result")):
            raise ValueError("Conflicting delivery identity in legacy store")
        # Canonical observations supersede the retained migration input, including
        # completed states. Re-reading an old queued row must never revive it.
    return rows


class _Transaction:
    def __init__(self, path, rows):
        self.path, self.rows = path, rows

    def append(self, row):
        """Reserve a delivery durably before the caller attempts transport."""
        if any(existing["id"] == row["id"] for existing in self.rows):
            raise ValueError("Duplicate delivery IDs")
        self.rows.append(row)
        self.save()

    def save(self, rows=None):
        if rows is not None:
            self.rows = rows
        write_json(self.path, self.rows)


@contextmanager
def locked(store_path=DEFAULT_STORE, legacy_store_path=None, *, max_bytes=None):
    """Serialize migration, reservation, transport outcome and later observation."""
    store, legacy = _store_paths(store_path, legacy_store_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for path in (store, legacy):
            if path is None or (path != store and not path.exists()):
                continue
            handle = stack.enter_context(path.with_suffix(path.suffix + ".lock").open("a"))
            stack.enter_context(exclusive(handle))
        rows = _merged_rows(store, legacy, max_bytes=max_bytes)
        if legacy is not None and legacy.exists():
            archive = legacy.with_suffix(".migrated.json")
            if archive.exists() or archive.is_symlink():
                raise ValueError("Legacy delivery writer is still active; inspect before migration")
            # If interrupted before rename, the next merge dedupes these IDs.
            write_json(store, rows)
            legacy.rename(archive)
        yield _Transaction(store, rows)


def read(store_path=DEFAULT_STORE, *, legacy_store_path=None, max_bytes=None):
    """Read canonical rows, first importing any legacy input under the send lock."""
    with locked(store_path, legacy_store_path, max_bytes=max_bytes) as transaction:
        return transaction.rows


def read_snapshot(store_path=DEFAULT_STORE, *, legacy_store_path=None, max_bytes=None):
    """Return (canonical path, rows, original text) for graph evidence line numbers."""
    with locked(store_path, legacy_store_path, max_bytes=max_bytes) as transaction:
        rows, raw = _read_document(transaction.path, max_bytes=max_bytes)
        return transaction.path, rows, raw
