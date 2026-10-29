"""SQLite backend: one MemoryStore file per tenant (fork F1/F2). It adds no schema of its own.

Per-tenant quotas and retention (MS5, fork F4 proceeded on guess) live here,
not in MemoryStore: tenant stores are service data, so they get quota
refusals, TTL expiry and real erasure (hard DELETE, secure_delete, VACUUM,
WAL truncation). The owner's own scope (tenant=None) is only ever opened
through MemoryStore directly, where supersede history stays the default
because that history is the owner's audit trail and powers ``as_of``. This
backend refuses a missing tenant id, so the hard-delete paths below can
never reach the owner's store. See kyra_memory.policy for the policy shape.
"""
import json
import shutil
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from kyra_memory import inbox
from kyra_memory._io import write_json
from kyra_memory.policy import RATE_WINDOW_SECONDS, QuotaExceededError, TenantPolicy
from kyra_memory.purge import purge_copies, purge_rows
from kyra_memory.store import TABLES, MemoryStore, Row, _deny_attach, now_iso, raise_hwm
from kyra_memory.tenancy import check_not_deleted, check_tenant, tenant_root, tombstone_path

PURGE_KEY = "purge_pending"  # meta row: the forget/expire purges whose on-disk removal a reader blocked


def _allow_scratch_attach(action, arg1=None, *_args):
    """VACUUM's internal scratch attach (empty filename) passes; a file ATTACH is still denied."""
    if action == sqlite3.SQLITE_ATTACH:
        return sqlite3.SQLITE_OK if arg1 == "" else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class _TenantStore(MemoryStore):
    """Guard all transactions, including store writers that have no quota hook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        stat = self.path.stat()
        self._file_identity = (stat.st_dev, stat.st_ino)
        self._closed = False

    def check_live(self) -> None:
        check_not_deleted(self.root, self.tenant)
        stat = self.path.stat()
        if self._closed or (stat.st_dev, stat.st_ino) != self._file_identity:
            raise FileNotFoundError(f"tenant {self.tenant!r} store was closed or replaced; reopen it")

    @contextmanager
    def transaction(self):
        if hasattr(self, "_file_identity"):  # initial schema creation precedes the identity snapshot
            self.check_live()
        with super().transaction() as db:
            yield db

    def close(self) -> None:
        self._closed = True
        super().close()


class SqliteBackend:
    def __init__(self, root: Path | str, scope: str, *, clock=None, default_policy: TenantPolicy | None = None,
                 policies: dict[str, TenantPolicy] | None = None, time_fn=time.monotonic):
        self.root = Path(root)
        self.scope = scope
        self._clock = clock
        self._time_fn = time_fn
        self._default_policy = default_policy or TenantPolicy()
        self._policies: dict[str, TenantPolicy] = dict(policies or {})
        self._open: dict[str, MemoryStore] = {}
        self._writes: dict[str, deque[tuple[float, int]]] = {}  # (commit time, records committed)
        self._refusals: dict[str, dict[str, int]] = {}
        self._completed: dict[str, list[dict]] = {}  # pending purges finished later, for the audit log
        self._finished_forgets: dict[str, dict[tuple, int]] = {}  # (table, row_id) -> rows, until the retry asks

    @staticmethod
    def _named(tenant: str) -> str:
        if check_tenant(tenant) is None:
            raise ValueError("a backend call needs a tenant id")
        return tenant

    def _store(self, tenant: str) -> MemoryStore:
        check_not_deleted(self.root, self._named(tenant))
        if self._named(tenant) not in self._open:
            raise KeyError(f"tenant {tenant!r} is not open")
        self._open[tenant].check_live()
        return self._open[tenant]

    def set_policy(self, tenant: str, policy: TenantPolicy) -> None:
        self._policies[self._named(tenant)] = policy

    def policy(self, tenant: str) -> TenantPolicy:
        return self._policies.get(self._named(tenant), self._default_policy)

    def open_tenant(self, tenant: str, *, create: bool = False, policy: TenantPolicy | None = None) -> None:
        marker = tombstone_path(self.root, self._named(tenant))
        if not create:
            check_not_deleted(self.root, tenant)
        if marker.exists() and create:
            old = self._open.pop(tenant, None)
            if old is not None:
                old.close()
            self._writes.pop(tenant, None)
            self._erase_tenant_folder(tenant)
            marker.unlink()
        if policy is not None:
            self.set_policy(tenant, policy)
        if self._named(tenant) in self._open:
            self._open[tenant].check_live()
            return
        extra = {"clock": self._clock} if self._clock else {}
        store = _TenantStore(self.root, self.scope, create=create, tenant=tenant, **extra)
        # Every write through this store instance is quota-checked: backend.add, the inbox
        # merger (add and proposal records), accept, set_weights and record_outcome.
        store._quota_check = lambda db, *, incoming_bytes, new_row: self._check_quota(
            tenant, store, db, incoming_bytes=incoming_bytes, new_row=new_row)
        store._quota_record = lambda n: self._after_commit(tenant, store, n)
        # A forget or expire whose WAL checkpoint a reader blocked: saved in meta inside the purge's own
        # transaction, so a close or a crash keeps it; a non-empty -wal at open is treated the same way.
        wal = Path(str(store.path) + "-wal")
        store._purge_pending = bool(store.get_meta(PURGE_KEY)) or (wal.exists() and wal.stat().st_size > 0)
        self._open[tenant] = store  # MemoryStore sets PRAGMA secure_delete=ON (MS3)
        if store._purge_pending:
            self._settle_wal(tenant, store)  # finishes now unless a reader still blocks it (then quietly not)

    def completed_purges(self, tenant: str) -> list[dict]:
        """Purges that a reader blocked and a later settle finished, since the last call (for the audit log)."""
        return self._completed.pop(tenant, [])

    # ---- quotas --------------------------------------------------------

    @staticmethod
    def _bytes(store: MemoryStore) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(store.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def _refuse(self, tenant: str, kind: str, limit: float, observed: float) -> None:
        counted = self._refusals.setdefault(tenant, {})
        counted[kind] = counted.get(kind, 0) + 1
        raise QuotaExceededError(tenant, kind, limit, observed)

    def _check_quota(self, tenant: str, store: MemoryStore, db=None, *, incoming_bytes: int = 0,
                     new_row: bool = True) -> None:
        """Refuse before the write. ``db`` is the caller's live transaction when there is one,
        so a merge batch counts its own uncommitted rows; counts run on the raw connection,
        so the check never inflates the store's query metric."""
        store.check_live()
        pol = self.policy(tenant)
        if pol.writes_per_minute is not None:
            now = self._time_fn()
            window = self._writes.setdefault(tenant, deque())
            while window and now - window[0][0] >= RATE_WINDOW_SECONDS:
                window.popleft()
            # Committed records only: one batch may overshoot and still lands (no wedge), then the
            # window refuses the tenant until it drains below the bound.
            used = sum(n for _t, n in window)
            if used >= pol.writes_per_minute:
                self._refuse(tenant, "writes_per_minute", pol.writes_per_minute, used)
        if pol.max_rows is not None and new_row:
            conn = db if db is not None else store._db
            rows = sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES)
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
                rows += conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
            if rows >= pol.max_rows:
                self._refuse(tenant, "max_rows", pol.max_rows, rows)
        if pol.max_bytes is not None:
            # On-disk bytes, plus what this transaction added so far (a merge batch's earlier records sit
            # in the page cache, not on disk, until COMMIT), plus the incoming body. The batch's share is
            # the larger of its page growth and the bytes its passed checks admitted: a batch that fills
            # pages released by forget does not raise page_count. A batch page already spilled to the WAL
            # counts twice, which only refuses earlier.
            growth = 0
            if db is not None:
                logical = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
                if store._tx_base_bytes is None:
                    store._tx_base_bytes = logical
                growth = max(0, logical - store._tx_base_bytes, store._tx_bytes)
            size = self._bytes(store) + growth + incoming_bytes
            if size > pol.max_bytes:
                self._refuse(tenant, "max_bytes", pol.max_bytes, size)
            if db is not None:
                store._tx_bytes += incoming_bytes

    def _record_write(self, tenant: str, n: int = 1) -> None:
        if self.policy(tenant).writes_per_minute is not None:  # no rate policy, no window to keep
            self._writes.setdefault(tenant, deque()).append((self._time_fn(), n))

    def _after_commit(self, tenant: str, store: MemoryStore, n: int) -> None:
        self._record_write(tenant, n)
        self._settle_wal(tenant, store)  # transaction() still holds store._lock here (an RLock)

    def _settle_wal(self, tenant: str, store: MemoryStore, *, force: bool = False) -> bool:
        """Truncate the WAL when the tenant is over max_bytes (or ``force``) and a WAL exists; True if it shrank.

        After a large commit the autocheckpoint copies the batch into the database, but the
        -wal file keeps its size, so the batch would sit on disk twice. Runs after every
        hooked commit, and before a max_bytes refusal is final (``force``, then the write is
        checked once more). It never raises and never waits: busy_timeout is 0 for the
        checkpoint, so a reader that blocks the truncate only leaves the WAL in place, where
        _check_quota counts it and the write is refused rather than overshooting; the next
        call after the reader closes truncates it. An in-process reader that stays open keeps
        the tenant refused for as long as it is open (WAL semantics). A purge that a reader
        blocked (``_purge_pending``) is finished here too, with or without a byte bound."""
        pol = self.policy(tenant)
        wal = Path(str(store.path) + "-wal")
        with store._lock:  # the shared connection, and the WAL size and pending flag read under the same lock
            size = wal.stat().st_size if wal.exists() else 0
            pending = store._purge_pending
            if pending:  # _finish_purge checks for a clean truncate itself before it clears anything
                self._finish_purge(store)
                return (wal.stat().st_size if wal.exists() else 0) < size
            if not size or pol.max_bytes is None or not (force or self._bytes(store) > pol.max_bytes):
                return False
            try:
                self._checkpoint_now(store._db)
            except sqlite3.OperationalError:  # a failed settle never fails a write, landed or not
                return False
            return (wal.stat().st_size if wal.exists() else 0) < size

    @staticmethod
    def _add_pending(store: MemoryStore, db, entry: dict) -> None:
        """Save a purge as pending inside its own transaction; cleared only after a clean truncate. The flag is
        set here too, so a purge that fails later for any reason (a busy checkpoint, a locked VACUUM) is finished
        by the next write, not only by a reopen."""
        store._purge_pending = True
        found = db.execute("SELECT v FROM meta WHERE k = ?", (PURGE_KEY,)).fetchone()
        entries = json.loads(found[0]) if found else []
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (PURGE_KEY, json.dumps([*entries, entry])))

    def _finish_purge(self, store: MemoryStore, own: list[dict] = ()) -> list[dict]:
        """Truncate the WAL; only when that is clean, drop the saved entries and report the ones from earlier
        calls (not ``own``) as completed. Returns them. The caller holds store._lock. Never raises; when the
        truncate is busy or fails, the purge stays pending."""
        db = store._db
        try:
            found = db.execute("SELECT v FROM meta WHERE k = ?", (PURGE_KEY,)).fetchone()
            busy, log, done = self._checkpoint_now(db)
            if busy or log != done:  # a reader still holds old pages: nothing is cleared
                store._purge_pending = True
                return []
            if found:
                timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
                db.execute("PRAGMA busy_timeout=0")
                try:
                    # Another instance may have committed a purge since our checkpoint snapshot.
                    if not db.execute("DELETE FROM meta WHERE k = ? AND v = ?", (PURGE_KEY, found[0])).rowcount:
                        store._purge_pending = True
                        return []
                finally:
                    db.execute(f"PRAGMA busy_timeout={int(timeout)}")
                self._checkpoint_now(db)  # best effort: the clear left a meta page (no purged text) in the WAL
        except sqlite3.OperationalError:
            store._purge_pending = True
            return []
        store._purge_pending = False
        earlier = json.loads(found[0]) if found else []
        for entry in own:
            if entry in earlier:
                earlier.remove(entry)
        if earlier:
            self._completed.setdefault(store.tenant, []).extend(earlier)
            for e in earlier:  # a retried forget of this row reports this count (same backend instance)
                if e.get("op") == "forget":
                    self._finished_forgets.setdefault(store.tenant, {})[(e.get("table"), e.get("row_id"))] = e["rows"]
        return earlier

    @staticmethod
    def _checkpoint_now(db) -> tuple[int, int, int]:
        """PRAGMA wal_checkpoint(TRUNCATE) with busy_timeout 0, restored after: a reader makes it
        report busy at once instead of holding the store lock (and every writer) for 30 s."""
        timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute("PRAGMA busy_timeout=0")
        try:
            return tuple(db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        finally:
            db.execute(f"PRAGMA busy_timeout={int(timeout)}")

    def _purge_checkpoint(self, store: MemoryStore, what: str, own: list[dict] = ()) -> list[dict]:
        """The purge's WAL truncate (the caller holds store._lock). When a reader blocks it, the purge
        stays pending (in meta) and this raises at once; the next settle (any write on this tenant, or
        reopening it) or a retry finishes it once no reader blocks it. Returns earlier purges it finished."""
        busy, log, done = self._checkpoint_now(store._db)
        if busy or log != done:
            store._purge_pending = True
            raise RuntimeError(f"{what} rows are gone from queries now; their text is removed from disk at the next "
                               "settle (any write on this tenant, or reopening it) once no reader blocks the WAL")
        return self._finish_purge(store, own)

    def quota_refusals(self, tenant: str) -> dict[str, int]:
        """Refused-write counts for this tenant, by bound (in memory, per backend instance)."""
        return dict(self._refusals.get(self._named(tenant), {}))

    def _write(self, tenant: str, op):
        """Run one backend write. A max_bytes refusal rolled the write back whole; when a stale
        WAL can be truncated it is, and the write is checked once more (its refusal uncounted).
        This is also how a tenant recovers once a reader that blocked the truncate has closed."""
        store = self._store(tenant)
        if store._purge_pending:  # even a write that is refused below finishes a blocked purge first
            self._settle_wal(tenant, store)
        try:
            return op(store)
        except QuotaExceededError as err:
            if err.kind != "max_bytes" or not self._settle_wal(tenant, store, force=True):
                raise
            counted = self._refusals[tenant]
            counted["max_bytes"] -= 1
            if not counted["max_bytes"]:
                del counted["max_bytes"]
        return op(store)

    def add(self, tenant: str, table: str, body: str, *, source: str, **fields) -> int:
        return self._write(tenant, lambda s: s.add(table, body, source=source, **fields))  # quota-checked in add_in

    def merge(self, tenant: str) -> inbox.MergeReport:
        """Fold the tenant's inboxes in. Rows and bytes are checked per record inside each
        inbox's transaction; an over-quota batch raises QuotaExceededError, rolls back whole and
        leaves the inbox offset unmoved, so a retry after expiry or a policy lift replays it.
        For writes_per_minute every record is one write, counted after its batch commits: a
        batch longer than the rate bound still lands once the window has room, then the window
        refuses the tenant until those records age out."""
        report = inbox.MergeReport()  # shared by the retry: each inbox counts once, when it commits
        return self._write(tenant, lambda s: inbox.merge(s, self.root, report))

    def accept(self, tenant: str, proposal_id: int) -> tuple[str, int]:
        return self._write(tenant, lambda s: s.accept(proposal_id))  # quota-checked in add_in

    def set_weights(self, tenant: str, weights: dict[int, float]) -> None:
        self._write(tenant, lambda s: s.set_weights(weights))

    def record_outcome(self, tenant: str, fact_id: int, *, win: bool) -> None:
        self._write(tenant, lambda s: s.record_outcome(fact_id, win=win))

    # ---- reads ----------------------------------------------------------

    def current(self, tenant: str, table: str) -> list[Row]:
        return self._store(tenant).current(table)

    def search(self, tenant: str, table: str, text: str) -> list[Row]:
        return self._store(tenant).search(table, text)

    def history(self, tenant: str, table: str, row_id: int) -> list[Row]:
        return self._store(tenant).history(table, row_id)

    # ---- erasure and retention -----------------------------------------

    def _vacuum(self, store: MemoryStore, own: list[dict] = ()) -> None:
        """Rebuild the file without deleted pages and truncate the WAL, so erased bytes are gone.

        VACUUM internally ATTACHes a scratch database (empty filename). The
        authorizer is narrowed to allow only that attach for the vacuum,
        then the deny-all authorizer is restored. Raises at once if the final
        WAL checkpoint could not finish (a reader is open), since the text
        may still be in the -wal or the old db pages; see _purge_checkpoint.
        """
        with store._lock:
            store._db.set_authorizer(_allow_scratch_attach)
            try:
                store._db.execute("VACUUM")
            finally:
                store._db.set_authorizer(_deny_attach)
            self._purge_checkpoint(store, "expired", own)

    @staticmethod
    def _hard_delete_in(db, table: str, ids: list[int]) -> None:
        """The one side-row purge (MS3), inside the caller's transaction. Supersedes is
        nulled first because expiry may take a chain's middle; forget deletes whole
        chains, where the null is harmless. Ids are chunked under SQLite's variable limit.
        The high-water ids are raised first, so no new row ever gets a purged id back."""
        raise_hwm(db, table, ids)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
            raise_hwm(db, "proposals", [db.execute("SELECT coalesce(max(id), 0) FROM proposals").fetchone()[0]])
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" * len(chunk))
            db.execute(f"UPDATE {table} SET supersedes = NULL WHERE supersedes IN ({marks})", chunk)
            db.execute(f"DELETE FROM {table} WHERE id IN ({marks})", chunk)
            db.execute(f"DELETE FROM usage WHERE tbl = ? AND item_id IN ({marks})", (table, *chunk))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
                db.execute(f"DELETE FROM proposals WHERE tbl = ? AND fact_id IN ({marks})", (table, *chunk))
            if table == "facts" and db.execute("SELECT 1 FROM sqlite_master WHERE name = 'fact_weights'").fetchone():
                db.execute(f"DELETE FROM fact_weights WHERE fact_id IN ({marks})", chunk)

    def _hard_delete(self, store: MemoryStore, table: str, ids: list[int], pending: dict | None = None) -> None:
        with store.transaction() as db:
            purge_rows(self.root, store.tenant, db, table, ids)
            self._hard_delete_in(db, table, ids)
            if pending is not None:
                self._add_pending(store, db, pending)

    def forget(self, tenant: str, table: str, row_id: int) -> int:
        """Hard purge (fork F4): the chain's rows and every side row that names them, zeroed in the file and the WAL.

        Raises at once if the WAL checkpoint could not finish (a reader is open), since the text may still be in the
        -wal or the db's old pages; the purge is then pending and finishes on the next write or a retry.
        Persistent vectors, inbox bodies and hot entries are scrubbed before the source rows."""
        if table not in TABLES:
            raise ValueError(f"unknown table: {table!r}")
        store = self._store(tenant)
        with store._lock:
            ids = [r.id for r in store.history(table, row_id)]
            entry = {"op": "forget", "table": table, "row_id": row_id, "rows": len(ids)}
            if ids:
                self._hard_delete(store, table, ids, pending=entry)
            self._purge_checkpoint(store, "forgotten", [entry] if ids else [])
            if not ids:  # a retry: report the rows of this row's earlier purge, whichever call finished it
                return self._finished_forgets.get(tenant, {}).pop((table, row_id), 0)
            return len(ids)

    def expire(self, tenant: str, *, now: str | None = None) -> dict[str, int]:
        """Hard-delete rows older than their table's TTL (by knowledge_time, never event_time),
        plus that table's still-proposed proposals past the same cutoff, then VACUUM and
        truncate the WAL on every call, so a retry after a reader blocked the first call's
        checkpoint still purges. Raises at once if that checkpoint could not finish (then pending,
        like forget).

        Persistent vectors, inbox bodies and hot entries are scrubbed before the source rows."""
        store = self._store(tenant)
        pol = self.policy(tenant)
        moment = datetime.fromisoformat(now or (self._clock() if self._clock else now_iso()))
        expired: dict[str, int] = {}
        own: list[dict] = []
        with store._lock:
            for table, ttl in sorted(pol.ttl_seconds.items()):
                cutoff = moment - timedelta(seconds=ttl)
                ids = [r.id for r in store.all_rows(table) if datetime.fromisoformat(r.knowledge_time) <= cutoff]
                with store.transaction() as db:
                    if ids:
                        purge_rows(self.root, tenant, db, table, ids)
                        self._hard_delete_in(db, table, ids)
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
                        store._ensure_proposals(db)  # a table from before learned_at gets the column first
                        pending = []
                        for pid, at in db.execute(  # store-clock merge time; `at` is caller-set (pre-column rows fall back)
                                "SELECT id, COALESCE(learned_at, at) FROM proposals WHERE tbl = ? AND state = 'proposed'",
                                (table,)):
                            at_dt = datetime.fromisoformat(at)
                            cmp_cutoff = cutoff.replace(tzinfo=None) if at_dt.tzinfo is None and cutoff.tzinfo else cutoff
                            if at_dt <= cmp_cutoff:
                                pending.append(pid)
                        if pending:
                            bodies = [db.execute("SELECT body FROM proposals WHERE id = ?", (pid,)).fetchone()[0]
                                      for pid in pending]
                            purge_copies(self.root, tenant, table, [], bodies)
                        raise_hwm(db, "proposals", pending)
                        for start in range(0, len(pending), 400):
                            chunk = pending[start:start + 400]
                            db.execute(f"DELETE FROM proposals WHERE id IN ({', '.join('?' * len(chunk))})", chunk)
                        if pending and not ids:
                            own.append({"op": "expire", "table": table, "rows": 0})
                            self._add_pending(store, db, own[-1])
                    if ids:
                        own.append({"op": "expire", "table": table, "rows": len(ids)})
                        self._add_pending(store, db, own[-1])
                if ids:
                    expired[table] = len(ids)
            self._vacuum(store, own)  # every call: a retry with nothing left to delete must still purge the WAL
        return expired

    def delete_tenant(self, tenant: str) -> None:
        """Tombstone before erasing the directory. Keep the separate, ids-only audit record."""
        write_json(tombstone_path(self.root, self._named(tenant)), {"tenant": tenant, "time": now_iso()})
        store = self._open.pop(self._named(tenant), None)
        self._writes.pop(tenant, None)
        if store is not None:
            store.close()
        self._erase_tenant_folder(tenant)

    def _erase_tenant_folder(self, tenant: str) -> None:
        """Finish erasure before a deletion marker can be cleared, including after a crash."""
        folder = tenant_root(self.root, tenant)
        if folder.is_symlink() or (self.root / "tenants").is_symlink():
            raise ValueError("a symlinked tenant folder is refused; nothing was deleted")
        if folder.exists():
            shutil.rmtree(folder)
        if folder.exists():
            raise OSError(f"tenant folder {folder} is still there after delete")

    # ---- reporting ------------------------------------------------------

    def counts(self, tenant: str) -> dict[str, int]:
        return self._store(tenant).counts()

    def stats(self, tenant: str) -> dict[str, float]:
        store = self._store(tenant)
        return {"bytes": self._bytes(store), "queries": store.queries,
                "congestion_events": store.congestion_events,
                "quota_refusals": sum(self._refusals.get(tenant, {}).values())}

    def close(self) -> None:
        for store in self._open.values():
            store.close()
        self._open.clear()
