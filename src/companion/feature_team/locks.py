"""Atomic per-file leases, independent of the landing gate's gate_lock.

T0/Q0 supply ONE shared database (data/team/loop/locks.sqlite3) and the
repository root for logical paths, even when workers use separate worktrees.
claim() returns False while waiting; the dispatcher retries on later ticks.
After ten minutes it raises TimeoutError, so the caller must fail/requeue with
a fresh run_id. T0 calls expire() each tick and release(..., reason="result")
on results; the inspector calls release(..., reason="dead") for a run whose worker died. Q0 calls next_ready() with eligible nodes in feature queue order.

Roles and owner identities come from trusted dispatcher code, never request
payloads. These are advisory leases, not OS write permissions: callers must
stop workers at their deadline before redispatch. A stolen run_id is fenced
from reuse and cannot release its successor's claim.

SQLite serializes check/claim/release and commits the audit event in the same
transaction. events() supplies ordered rows for E0 ingestion; this module does
not append separately to E0 because that could lose or duplicate steal events.
"""

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

MAX_WAIT_SECONDS = 600


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("time must be a finite number of Unix seconds")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_id and owner must be nonempty strings")
    return value


class FileLocks:
    def __init__(self, database, *, workspace, caller_role="worker", clock=time.time):
        if caller_role not in {"worker", "dispatcher"}:
            raise PermissionError("invalid lock caller role")
        self.database = Path(database)
        self.workspace = Path(workspace).resolve()
        self.role = caller_role
        self.clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS claims ("
                       "run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, deadline REAL NOT NULL, "
                       "files TEXT NOT NULL, state TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS waits ("
                       "run_id TEXT PRIMARY KEY, started REAL NOT NULL, timed_out INTEGER NOT NULL DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS scheduler (id INTEGER PRIMARY KEY CHECK(id=1), feature TEXT)")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _dispatcher(self):
        if self.role != "dispatcher":
            raise PermissionError("only the dispatcher can steal, expire or schedule")

    def _files(self, paths):
        if not isinstance(paths, (list, tuple, set)) or not paths:
            raise ValueError("claim requires a nonempty set of file paths")
        result = set()
        for raw in paths:
            if not isinstance(raw, (str, Path)) or not str(raw).strip():
                raise ValueError("invalid file path")
            path = (self.workspace / raw).resolve()
            relative = path.relative_to(self.workspace)
            if relative == Path(".") or path.is_dir():
                raise ValueError("claim paths must name files inside the workspace")
            # Conservatively collide case aliases on the default macOS filesystem.
            result.add(relative.as_posix().casefold())
        return sorted(result)

    @staticmethod
    def _event(db, kind, now, run_id, owner, files, **detail):
        row = dict(t=now, run_id=run_id, worker=owner, kind=kind,
                   path=files, detail=detail)
        db.execute("INSERT INTO events(payload) VALUES (?)", (json.dumps(row, allow_nan=False),))

    @staticmethod
    def _active(db):
        return [dict(row) | {"files": json.loads(row["files"])}
                for row in db.execute("SELECT * FROM claims WHERE state='held' ORDER BY run_id")]

    def claim(self, run_id, owner, paths, deadline, *, steal_expired=False):
        """Atomically acquire the entire set or nothing. Deadline is never extended.

        False means wait, not permission to edit. A repeated successful claim is
        idempotent only with exactly the same owner, paths and node deadline.
        """
        _text(run_id)
        _text(owner)
        _number(deadline)
        files = self._files(paths)
        if steal_expired:
            self._dispatcher()
        timed_out = False
        acquired = False
        with self._transaction() as db:
            now = _number(self.clock())
            if deadline <= now:
                raise ValueError("node deadline must be in the future")
            previous = db.execute("SELECT * FROM claims WHERE run_id=?", (run_id,)).fetchone()
            if previous:
                if (previous["state"] != "held" or previous["owner"] != owner
                        or previous["deadline"] != deadline or json.loads(previous["files"]) != files):
                    raise ValueError("run_id is closed or claim identity changed")
                return True
            waiting = db.execute("SELECT * FROM waits WHERE run_id=?", (run_id,)).fetchone()
            if waiting and (waiting["timed_out"] or now - waiting["started"] >= MAX_WAIT_SECONDS):
                timed_out = True
                if not waiting["timed_out"]:
                    db.execute("UPDATE waits SET timed_out=1 WHERE run_id=?", (run_id,))
                    self._event(db, "lock_wait_timeout", now, run_id, owner, files,
                                wait_seconds=now - waiting["started"])
            else:
                conflicts = [row for row in self._active(db) if set(files).intersection(row["files"])]
                # Safety gate: a dispatcher may NEVER steal an unexpired claim.
                live_conflict = any(row["deadline"] > now for row in conflicts)
                if conflicts and (not steal_expired or live_conflict):
                    if waiting is None:
                        db.execute("INSERT INTO waits(run_id, started) VALUES (?, ?)", (run_id, now))
                        self._event(db, "lock_wait", now, run_id, owner, files,
                                    holders=[row["run_id"] for row in conflicts], wait_seconds=0)
                else:
                    for row in conflicts:
                        db.execute("UPDATE claims SET state='stolen' WHERE run_id=?", (row["run_id"],))
                        self._event(db, "lock_steal", now, run_id, owner, row["files"],
                                    previous_run_id=row["run_id"], previous_owner=row["owner"],
                                    deadline=row["deadline"])
                    db.execute("INSERT INTO claims VALUES (?, ?, ?, ?, 'held')",
                               (run_id, owner, deadline, json.dumps(files)))
                    db.execute("DELETE FROM waits WHERE run_id=?", (run_id,))
                    self._event(db, "lock_claim", now, run_id, owner, files, deadline=deadline,
                                wait_seconds=now - waiting["started"] if waiting else 0)
                    acquired = True
        if timed_out:
            raise TimeoutError("file lock wait reached ten minutes; use a fresh run_id")
        return acquired

    def release(self, run_id, owner, *, reason):
        """Release only this run's complete claim; late results cannot unlock a successor.

        ``dead`` is the inspector's close of a run whose worker is gone: dispatcher role only, and unlike
        ``timeout`` it frees the claim before its deadline (a dead worker writes nothing more)."""
        _text(run_id)
        _text(owner)
        if reason not in {"result", "timeout", "dead"}:
            raise ValueError("release reason must be result, timeout or dead")
        if reason in {"timeout", "dead"}:
            self._dispatcher()
        with self._transaction() as db:
            now = _number(self.clock())
            row = db.execute("SELECT * FROM claims WHERE run_id=? AND state='held'", (run_id,)).fetchone()
            if row is None:
                return False
            if row["owner"] != owner:
                raise PermissionError("claim owner does not match")
            if reason == "timeout" and row["deadline"] > now:
                return False
            db.execute("UPDATE claims SET state=? WHERE run_id=?", (reason, run_id))
            self._event(db, "lock_release", now, run_id, owner, json.loads(row["files"]), reason=reason)
            return True

    def expire(self):
        """Dispatcher tick cleanup, including expired claims nobody is waiting for."""
        self._dispatcher()
        with self._transaction() as db:
            now = _number(self.clock())
            expired = [row for row in self._active(db) if row["deadline"] <= now]
            for row in expired:
                db.execute("UPDATE claims SET state='timeout' WHERE run_id=?", (row["run_id"],))
                self._event(db, "lock_release", now, row["run_id"], row["owner"], row["files"], reason="timeout")
            return [row["run_id"] for row in expired]

    def snapshot(self):
        """All unreleased claims, INCLUDING expired ones, for honest stale metrics."""
        with self._transaction() as db:
            return self._active(db)

    def stale(self):
        now = _number(self.clock())
        return [row for row in self.snapshot() if row["deadline"] <= now]

    def events(self, *, after=0):
        """Durable, ordered audit rows; E0 consumers checkpoint the returned id."""
        with self._transaction() as db:
            return [json.loads(row["payload"]) | {"id": row["id"]}
                    for row in db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (after,))]

    def next_ready(self, ready):
        """Return (feature, node), rotating across ticks; None when no work is ready.

        ready maps running feature names to eligible node IDs in queue order.
        The caller removes/reserves each selected node before calling again.
        Selection is not dispatch: T0 must claim the files before sending work.
        """
        self._dispatcher()
        names = list(ready)
        if not names:
            return None
        with self._transaction() as db:
            cursor = db.execute("SELECT feature FROM scheduler WHERE id=1").fetchone()
            offset = names.index(cursor[0]) + 1 if cursor and cursor[0] in names else 0
            for feature in names[offset:] + names[:offset]:
                if ready[feature]:
                    db.execute("INSERT INTO scheduler VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET feature=excluded.feature",
                               (feature,))
                    return feature, ready[feature][0]
            return None
