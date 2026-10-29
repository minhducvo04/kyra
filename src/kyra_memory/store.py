"""One SQLite file per scope, five typed tables, rows superseded instead of overwritten.

Scopes: the host names them (`kyra` is the first). Restricted scopes (health, busy, family, secrets) are
separate files that the default code path never opens: every entry point
opens `<scope>.sqlite3` by name, the file's own meta row must name the scope, a
symlinked file is refused and ATTACH is denied. That structure is the
isolation; there is no per-read check. Deleting a scope is deleting one file.

Tables (every row carries source, event_time, knowledge_time and a privacy
label: Tier + PrivacyClass set):
- rules:   instructions from the owner.
- pinned:  facts the owner stated; the assembler ranks them above facts and drops a
           fact that shares a pinned row's key.
- facts:   agent-derived; wins/losses, confidence, valid_from/valid_to.
- faults:  error signature -> correction, written by code (gate or test failure).
- recipes: task kind -> steps that worked, with ids and positions stripped.

Two clocks: event_time is when the thing happened or became true (caller says);
knowledge_time is when this store learned it (store clock, never the caller).
`as_of` answers "what did we believe at time T" from knowledge_time.

Who writes: only the merger (kyra_memory.inbox) and the owner CLI, through one
lock and BEGIN EXCLUSIVE. Agents append to their team inbox and never touch
this writer. Pack building opens the store read-only (`read_only=True`); WAL
lets it read the last committed snapshot while the merger writes.
"""
import functools
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from kyra_memory.labels import UNKNOWN, PrivacyClass, Tier, combine
from kyra_memory.tenancy import tenant_root

TABLES = ("rules", "pinned", "facts", "faults", "recipes")
RESTRICTED_SCOPES = frozenset({"health", "busy", "family", "secrets"})
SCOPE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SCHEMA_VERSION = 2
CONGESTION_SECONDS = 1.0
DEFAULT_SCOPE = "default"  # the scope the pack builder and indexer open when the caller names none; a host sets its own

log = logging.getLogger("kyra_memory.store")

_COMMON = """
    id INTEGER PRIMARY KEY,
    body TEXT NOT NULL CHECK (length(body) > 0),
    topic TEXT NOT NULL DEFAULT '',
    key TEXT,
    source TEXT NOT NULL CHECK (length(source) > 0),
    event_time TEXT NOT NULL,
    knowledge_time TEXT NOT NULL,
    tier INTEGER NOT NULL,
    classes TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'pruned')),
    supersedes INTEGER,
    superseded_by INTEGER,
    ended_at TEXT,
    fade_exempt INTEGER NOT NULL DEFAULT 0,
    owner_important INTEGER NOT NULL DEFAULT 0
"""
_EXTRA = {
    "rules": "",
    "pinned": "",
    "facts": """,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    valid_from TEXT,
    valid_to TEXT""",
    "faults": """,
    signature TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 1""",
    "recipes": "",
}
_EXTRA_COLS = {"facts": ("wins", "losses", "confidence", "valid_from", "valid_to"), "faults": ("signature", "seen")}


class ScopeError(PermissionError):
    """A store was asked to open a file that is not its own scope's file."""


@dataclass(frozen=True)
class Row:
    table: str
    id: int
    body: str
    topic: str
    key: str | None
    source: str
    event_time: str
    knowledge_time: str
    label: tuple[Tier, frozenset[PrivacyClass]]
    status: str
    supersedes: int | None
    superseded_by: int | None
    ended_at: str | None
    extra: dict = field(default_factory=dict)

    @functools.cached_property
    def hay(self) -> str:
        """Lowercased searchable text, computed once per row object (pack snapshots reuse rows)."""
        return f"{self.topic} {self.key or ''} {self.body}".lower()

    @functools.cached_property
    def line(self) -> str:
        return "- " + (f"[{self.topic}] " if self.topic else "") + " ".join(self.body.split())


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _check_time(value: str) -> str:
    datetime.fromisoformat(value)  # raises ValueError on anything that is not ISO 8601
    return value


def _label(label) -> tuple[Tier, frozenset[PrivacyClass]]:
    tier, classes = label
    return combine([(Tier(tier), frozenset(PrivacyClass(c) for c in classes))])


@functools.lru_cache(maxsize=256)
def _stored_label(tier: int, classes_json: str) -> tuple[Tier, frozenset[PrivacyClass]]:
    """A row's label from its columns; few distinct values, so parsed once each."""
    return _label((tier, json.loads(classes_json)))


def fault_signature(text: str) -> str:
    """Stable key for an error: lowercased, ids, paths and numbers replaced."""
    s = text.lower()
    s = re.sub(r"(?:/[\w.@-]+)+/?", "<path>", s)
    s = re.sub(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b", "<n>", s)  # ids and numbers are one token, so 3fa9c21d ~ 12
    s = re.sub(r"\d+", "<n>", s)
    return re.sub(r"\s+", " ", s).strip()[:300]


def strip_ids(text: str) -> str:
    """Remove what does not transfer to the next task: shas, uuids, line positions, element refs."""
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", text, flags=re.I)
    s = re.sub(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", "<id>", s)
    s = re.sub(r"(\.\w{1,5}):\d+(?::\d+)?", r"\1", s)  # file.py:12 -> file.py
    s = re.sub(r"\bref_\d+\b", "ref", s)
    s = re.sub(r"#\d+\b", "#<n>", s)
    return s.strip()


def require_scope_allowed(scope: str, allow_restricted: bool = False) -> None:
    """A restricted scope (health, busy, family, secrets) opens only when the caller says so, in code."""
    if scope in RESTRICTED_SCOPES and not allow_restricted:
        raise ScopeError(f"scope {scope!r} is restricted; pass allow_restricted=True for a local, owner-approved use")


def scope_path(root: Path | str, scope: str, tenant: str | None = None) -> Path:
    base = tenant_root(root, tenant)  # refuses a bad tenant id before any path is built
    if not isinstance(scope, str) or not SCOPE_RE.fullmatch(scope):
        raise ScopeError(f"invalid scope name: {scope!r}")
    return base / f"{scope}.sqlite3"


def _deny_attach(action, *_args):
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


ROW_OVERHEAD_BYTES = 48  # max_bytes upper bound per stored row beyond its values: record header, cell pointer, rowid, page slack


def _hwm_key(table: str) -> str:
    return f"hwm:{table}"


def raise_hwm(db, table: str, ids) -> None:
    """Record the highest id ever hard-deleted from ``table`` (inside the purge's transaction), so add_in
    never gives a purged id to a new row: a caller retrying forget(id) can then only find nothing."""
    top = max(ids, default=None)
    if top is None:
        return
    found = db.execute("SELECT v FROM meta WHERE k = ?", (_hwm_key(table),)).fetchone()
    if found is None or int(found[0]) < top:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (_hwm_key(table), str(top)))


def _next_id(db, table: str) -> int | None:
    """max(max(id), high-water id) + 1 once a table has had a hard delete; None means SQLite picks as before."""
    found = db.execute("SELECT v FROM meta WHERE k = ?", (_hwm_key(table),)).fetchone()
    if found is None:
        return None
    top = db.execute(f"SELECT max(id) FROM {table}").fetchone()[0] or 0
    return max(top, int(found[0])) + 1


def _text_bytes(values) -> int:
    """Bytes of every text value a write stores (body, source, topic, key, refs, quote, ...), for max_bytes."""
    return sum(len(v.encode("utf-8")) for v in values if isinstance(v, str))


def _row_bytes(values) -> int:
    """Upper bound on one stored row for max_bytes: its text, 8 bytes per number, and the row overhead."""
    return _text_bytes(values) + 8 * sum(1 for v in values if isinstance(v, (int, float))) + ROW_OVERHEAD_BYTES


def _locked(method):
    """Run a read under the store's lock: a statement on the shared connection never interleaves with
    another thread's transaction (it would read that thread's uncommitted rows, or deadlock with it)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class MemoryStore:
    """The store for one scope. `create=True` once; `read_only=True` for pack building."""

    def __init__(self, root: Path | str, scope: str, *, create: bool = False, read_only: bool = False,
                 clock=now_iso, allow_restricted: bool = False, tenant: str | None = None):
        require_scope_allowed(scope, allow_restricted)
        self.scope = scope
        self.tenant = tenant
        self.root = Path(root)
        self.path = scope_path(root, scope, tenant)
        self.read_only = read_only
        self._clock = clock
        self._lock = threading.RLock()  # the shared connection: every statement on it runs under this lock
        self.queries = 0  # read statements run; pack metrics report it
        self._quota_check = None  # SqliteBackend attaches per-tenant quota callbacks; the owner's store has none
        self._quota_record = None
        self._tx_writes = 0  # hooked writes (records) in the open transaction; recorded only once it commits
        self._tx_base_bytes = None  # logical db size when the open transaction's first quota check ran
        self._tx_bytes = 0  # incoming bytes the open transaction's quota checks already passed
        self.write_waits: list[float] = []
        self.congestion_events = 0
        if self.path.is_symlink():
            raise ScopeError(f"{self.path.name} is a symlink; a scope must be its own file")
        exists = self.path.exists()
        if not exists and (read_only or not create):
            raise FileNotFoundError(f"memory v2: no store for scope {scope!r} at {self.path}")
        if read_only:
            self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA secure_delete=ON")  # released pages are zeroed, so a purged row stays purged
            # The FTS index lives in temp. Keep that schema off disk, and zero pages a rebuild frees.
            self._db.execute("PRAGMA temp_store=MEMORY")
            self._db.execute("PRAGMA temp.secure_delete=ON")
        self._db.row_factory = sqlite3.Row
        self._db.set_authorizer(_deny_attach)
        if exists:
            self._check_scope()
        else:
            self._create()
        if not read_only:
            self._migrate_recall_metadata()
            from kyra_memory.fts import ensure_sqlite
            try:
                ensure_sqlite(self._db)
            except sqlite3.Error:
                pass

    def _migrate_recall_metadata(self) -> None:
        """Writer-only, additive migration; old read-only stores remain usable."""
        with self.transaction() as db:
            if 'who' not in {r[1] for r in db.execute('PRAGMA table_info(usage)')}:
                db.execute("ALTER TABLE usage ADD COLUMN who TEXT NOT NULL DEFAULT 'agent'")
            for table in TABLES:
                columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
                for flag in ('fade_exempt', 'owner_important'):
                    if flag not in columns:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0')
                if 'retrieval_tier' not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN retrieval_tier TEXT NOT NULL DEFAULT 'near' "
                               "CHECK (retrieval_tier IN ('near', 'far', 'deep'))")
                db.execute(f'CREATE INDEX IF NOT EXISTS {table}_retrieval ON {table}(status, retrieval_tier, id)')
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_protect_retrieval
                    AFTER UPDATE OF fade_exempt, owner_important ON {table}
                    WHEN NEW.fade_exempt OR NEW.owner_important
                    BEGIN UPDATE {table} SET retrieval_tier = 'near' WHERE id = NEW.id; END""")
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_owner_recall
                    AFTER INSERT ON usage WHEN NEW.who = 'owner' AND NEW.tbl = '{table}'
                    BEGIN UPDATE {table} SET retrieval_tier = 'near' WHERE id = NEW.item_id; END""")
            db.execute('CREATE INDEX IF NOT EXISTS usage_item ON usage(tbl, item_id)')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
                db.execute('CREATE INDEX IF NOT EXISTS proposals_item ON proposals(tbl, fact_id)')
            db.execute("UPDATE meta SET v = ? WHERE k = 'schema_version' AND v = '1'", (str(SCHEMA_VERSION),))

    def _check_scope(self) -> None:
        try:
            found = self._db.execute("SELECT v FROM meta WHERE k = 'scope'").fetchone()
        except sqlite3.DatabaseError as e:
            raise ScopeError(f"{self.path.name} is not a memory v2 store") from e
        if found is None or found[0] != self.scope:
            raise ScopeError(f"{self.path.name} belongs to scope {found[0] if found else None!r}, not {self.scope!r}")
        owner = self._db.execute("SELECT v FROM meta WHERE k = 'tenant'").fetchone()
        if (owner[0] if owner else None) != self.tenant:
            raise ScopeError(f"{self.path.name} belongs to tenant {owner[0] if owner else None!r}, "
                             f"not {self.tenant!r}")

    def _create(self) -> None:
        with self.transaction() as db:
            db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('scope', ?), ('schema_version', ?)", (self.scope, str(SCHEMA_VERSION)))
            if self.tenant is not None:
                db.execute("INSERT INTO meta VALUES ('tenant', ?)", (self.tenant,))
            for t in TABLES:
                db.execute(f"CREATE TABLE {t} ({_COMMON}{_EXTRA[t]})")
                db.execute(f"CREATE INDEX {t}_status ON {t}(status, key)")
            db.execute("CREATE INDEX faults_signature ON faults(status, signature)")
            db.execute("CREATE TABLE usage (team TEXT NOT NULL, tbl TEXT NOT NULL, item_id INTEGER NOT NULL, "
                       "at TEXT NOT NULL)")
            db.execute("CREATE INDEX usage_team ON usage(team)")
            db.execute("CREATE TABLE metrics (team TEXT NOT NULL, task TEXT NOT NULL, at TEXT NOT NULL, "
                       "build_ms REAL NOT NULL, memory_calls INTEGER NOT NULL, chars INTEGER NOT NULL)")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self):
        """The writer queue (merger and owner CLI only): one writer here, BEGIN EXCLUSIVE across processes."""
        if self.read_only:
            raise PermissionError("this store was opened read-only")
        start = time.perf_counter()
        with self._lock:
            self._db.execute("BEGIN EXCLUSIVE")
            self._tx_writes = 0  # reset per transaction, so a rolled-back write never spends a rate window
            self._tx_base_bytes = None
            self._tx_bytes = 0
            waited = time.perf_counter() - start
            self.write_waits.append(waited)
            if waited > CONGESTION_SECONDS:
                self.congestion_events += 1
                log.warning("memory v2 write congestion: %s waited %.2f s for the writer", self.scope, waited)
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            try:
                self._rebuild_forgotten_fts()
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            writes, self._tx_writes = self._tx_writes, 0
            if writes and self._quota_record is not None:
                self._quota_record(writes)  # every committed record counts: a merge batch of n is n writes

    def _rebuild_forgotten_fts(self) -> None:
        """Rebuild each temp FTS index that lost a row in this transaction.

        A delete drops the FTS row through its trigger, but FTS5 keeps the token in a
        segment block until a rebuild. Temp secure_delete then zeros the pages that frees.
        """
        from kyra_memory.fts import sqlite_dirty, sqlite_rebuild
        for table in sqlite_dirty(self._db):
            if table in TABLES:
                sqlite_rebuild(self._db, table)

    def _quota_before(self, db, *, incoming_bytes: int, new_row: bool) -> None:
        if self._quota_check is not None:
            self._quota_check(db, incoming_bytes=incoming_bytes, new_row=new_row)

    def _quota_after(self) -> None:
        self._tx_writes += 1  # transaction() records the count after COMMIT

    # ---- writers -------------------------------------------------------

    def add(
        self,
        table: str,
        body: str,
        *,
        source: str,
        topic: str = "",
        key: str | None = None,
        event_time: str | None = None,
        label=UNKNOWN,
        supersedes: int | None = None,
        confidence: float | None = None,
        signature: str | None = None,
        fade_exempt: bool = False,
        owner_important: bool = False,
    ) -> int:
        """Insert a row. A row with the same key (or fault signature) supersedes the active one."""
        with self.transaction() as db:
            return self.add_in(db, table, body, source=source, topic=topic, key=key, event_time=event_time,
                               label=label, supersedes=supersedes, confidence=confidence, signature=signature,
                               fade_exempt=fade_exempt, owner_important=owner_important)

    def add_in(self, db, table: str, body: str, *, source: str, topic: str = "", key: str | None = None,
               event_time: str | None = None, label=UNKNOWN, supersedes: int | None = None,
               confidence: float | None = None, signature: str | None = None,
               fade_exempt: bool = False, owner_important: bool = False) -> int:
        """add() inside a transaction the caller holds (the merger folds a whole inbox batch in one)."""
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        body = (strip_ids(body) if table == "recipes" else body).strip()
        if not body or not source or not source.strip():
            raise ValueError("body and source are required")
        if table == "recipes" and not topic.strip():
            raise ValueError("a recipe needs its task kind as topic")
        label = _label(label)
        knowledge = self._clock()
        event = _check_time(event_time) if event_time else knowledge
        cols = {"body": body, "topic": topic.strip(), "key": key, "source": source.strip(), "event_time": event,
                "knowledge_time": knowledge, "fade_exempt": int(fade_exempt), "owner_important": int(owner_important)}
        if table == "facts":
            cols.update(valid_from=event, confidence=0.5 if confidence is None else float(confidence))
        if table == "faults":
            cols["signature"] = fault_signature(signature or body)
        old = None
        if supersedes is not None:
            old = db.execute(f"SELECT * FROM {table} WHERE id = ?", (supersedes,)).fetchone()
            if old is None or old["status"] != "active":
                raise ValueError(f"{table} row {supersedes} is not an active row to supersede")
        elif table == "faults":
            old = db.execute("SELECT * FROM faults WHERE status = 'active' AND signature = ?",
                             (cols["signature"],)).fetchone()
            if old is not None and old["body"] == body:
                old_label = _stored_label(old["tier"], old["classes"])
                label = combine([label, old_label])
                if label == old_label:
                    self._quota_before(db, incoming_bytes=0, new_row=False)
                    db.execute("UPDATE faults SET seen = seen + 1 WHERE id = ?", (old["id"],))
                    self._quota_after()
                    return old["id"]
                # Preserve widening in the existing supersede history, including the repeat count.
                cols["seen"] = old["seen"] + 1
        elif key is not None:
            old = db.execute(f"SELECT * FROM {table} WHERE status = 'active' AND key = ?", (key,)).fetchone()
        if old is not None:
            # A correction never narrows what the old row was labelled.
            label = combine([label, _label((old["tier"], json.loads(old["classes"])))])
            cols["supersedes"] = old["id"]
            if cols["key"] is None:
                cols["key"] = old["key"]
        cols["tier"], cols["classes"] = int(label[0]), json.dumps(sorted(c.value for c in label[1]))
        self._quota_before(db, incoming_bytes=_text_bytes(cols.values()), new_row=True)
        next_id = _next_id(db, table)
        if next_id is not None:  # this table had rows hard-deleted: never hand a purged id out again
            cols = {"id": next_id, **cols}
        names = ", ".join(cols)
        new_id = db.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(cols))})",
                            tuple(cols.values())).lastrowid
        self._quota_after()
        if old is not None:
            db.execute(f"UPDATE {table} SET status = 'superseded', superseded_by = ?, ended_at = ? WHERE id = ?",
                       (new_id, knowledge, old["id"]))
            if table == "facts":
                db.execute("UPDATE facts SET valid_to = ? WHERE id = ?", (event, old["id"]))
        return new_id

    # ---- fact weights (reweight pass): a side table the assembler reads, created on first use ----------

    @_locked
    def weights(self, ids=None) -> dict[int, float]:
        """{fact id: stored weight}; empty when no reweight pass has run (the table does not exist yet)."""
        if not self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fact_weights'").fetchone():
            return {}
        out = {}
        where = '' if ids is None else ' WHERE fact_id IN (' + ','.join('?' for _ in ids) + ')'
        for fact_id, raw in self._db.execute("SELECT fact_id, weight FROM fact_weights" + where, () if ids is None else ids):
            try:  # SQLite REAL columns accept text; anything non-numeric or non-finite counts as no weight
                w = float(raw)
            except (TypeError, ValueError):
                w = 1.0
            out[fact_id] = min(1.3, max(0.7, w)) if math.isfinite(w) else 1.0
        return out

    def set_weights(self, weights: dict[int, float]) -> None:
        for fact_id, w in weights.items():  # the column is REAL, but SQLite would keep text or a blob as given
            if type(fact_id) is not int or type(w) not in (int, float):
                raise ValueError("weights map an int fact id to a number")
        with self.transaction() as db:  # one fact_weights row per entry: two 8-byte numbers plus row overhead
            self._quota_before(db, incoming_bytes=len(weights) * _row_bytes((0, 0.0)), new_row=False)
            db.execute("CREATE TABLE IF NOT EXISTS fact_weights (fact_id INTEGER PRIMARY KEY, weight REAL NOT NULL)")
            db.executemany("INSERT OR REPLACE INTO fact_weights VALUES (?, ?)", sorted(weights.items()))
            self._quota_after()

    # ---- proposals (consolidate pass): outside the five tables, so no pack ever reads one -------------

    def _ensure_proposals(self, db) -> None:
        db.execute("CREATE TABLE IF NOT EXISTS proposals (id INTEGER PRIMARY KEY, tbl TEXT NOT NULL, body TEXT NOT NULL, "
                   "topic TEXT NOT NULL DEFAULT '', key TEXT, source TEXT NOT NULL, tier INTEGER NOT NULL, "
                   "classes TEXT NOT NULL, refs TEXT NOT NULL, quote TEXT NOT NULL, confidence REAL NOT NULL, "
                   "origin TEXT NOT NULL, team TEXT NOT NULL, supersedes INTEGER, signature TEXT, at TEXT NOT NULL, "
                   "state TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed', 'accepted')), fact_id INTEGER, "
                   "event_time TEXT)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(proposals)")]
        for col, kind in (("event_time", "TEXT"), ("verdict", "TEXT"), ("verdict_match", "INTEGER"),
                          ("verdict_confidence", "REAL"), ("learned_at", "TEXT")):
            if col not in cols:  # verdict*: a local duplicate judge's annotation only; accept() never reads it.
                # learned_at: store-clock time of the merge; tenant expiry keys on it, never on the caller's `at`
                db.execute(f"ALTER TABLE proposals ADD COLUMN {col} {kind}")
        if {'tbl', 'fact_id'} <= set(cols):  # an ancient table without these columns is migrated above, not indexed
            db.execute('CREATE INDEX IF NOT EXISTS proposals_item ON proposals(tbl, fact_id)')

    def add_proposal_in(self, db, rec: dict, team: str) -> int:
        """Record an inbox add with status=proposed. Nothing reaches an active table until accept()."""
        if rec["table"] not in TABLES or rec["table"] in ("rules", "pinned"):
            raise ValueError("a proposal is a fact, fault or recipe")
        self._ensure_proposals(db)
        tier, classes = _label(rec["label"])
        body = rec["body"].strip()
        if not body or not rec.get("source"):
            raise ValueError("body and source are required")
        v = rec.get("verdict") or {}
        if v and v.get("verdict") not in ("duplicate", "update", "new", "conflict", "unsure", "held"):
            raise ValueError("unknown verdict")
        values = (rec["table"], body, rec.get("topic") or "", rec.get("key"), rec["source"], int(tier),
                  json.dumps(sorted(c.value for c in classes)), json.dumps(rec.get("refs") or []), rec.get("quote") or "",
                  float(rec.get("confidence") if rec.get("confidence") is not None else 0.5), rec.get("origin") or "",
                  team, rec.get("supersedes"), rec.get("signature"), rec["at"], rec.get("event_time"), v.get("verdict"),
                  v.get("match_id"), v.get("confidence"), self._clock())
        self._quota_before(db, incoming_bytes=_text_bytes(values), new_row=True)
        next_id = _next_id(db, "proposals")  # never reuse a purged proposal id (accept takes one)
        new_id = db.execute(
            "INSERT INTO proposals (id, tbl, body, topic, key, source, tier, classes, refs, quote, confidence, origin, "
            "team, supersedes, signature, at, event_time, verdict, verdict_match, verdict_confidence, learned_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (next_id, *values)).lastrowid
        self._quota_after()
        return new_id

    @_locked
    def proposals(self, state: str = "proposed") -> list[dict]:
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone() is None:
            return []
        return [dict(r) for r in self._db.execute("SELECT * FROM proposals WHERE state = ? ORDER BY id", (state,))]

    def accept(self, proposal_id: int) -> tuple[str, int]:
        """Flip a proposal to an active row (it supersedes its listed old row if that is still active)."""
        with self.transaction() as db:
            self._ensure_proposals(db)
            p = db.execute("SELECT * FROM proposals WHERE id = ? AND state = 'proposed'", (proposal_id,)).fetchone()
            if p is None:
                raise ValueError(f"no proposed row {proposal_id}")
            if p["origin"] == "memory-review":
                raise ValueError("protection proposals require review.apply()")
            if p["key"] and db.execute("SELECT 1 FROM pinned WHERE status = 'active' AND key = ?", (p["key"],)).fetchone():
                raise ValueError(f"proposal {proposal_id}: key {p['key']!r} is pinned by the owner")
            old = db.execute(f"SELECT status FROM {p['tbl']} WHERE id = ?", (p["supersedes"],)).fetchone() \
                if p["supersedes"] is not None else None
            new_id = self.add_in(db, p["tbl"], p["body"], source=p["source"], topic=p["topic"], key=p["key"],
                                 event_time=p["event_time"], label=(p["tier"], json.loads(p["classes"])),
                                 supersedes=p["supersedes"] if old and old["status"] == "active" else None,
                                 confidence=p["confidence"], signature=p["signature"])
            db.execute("UPDATE proposals SET state = 'accepted', fact_id = ? WHERE id = ?", (new_id, proposal_id))
        return p["tbl"], new_id

    def record_outcome(self, fact_id: int, *, win: bool) -> None:
        """A fact helped (win) or misled (loss); confidence is the Laplace-smoothed win rate."""
        with self.transaction() as db:
            self.record_outcome_in(db, fact_id, win=win)

    def record_outcome_in(self, db, fact_id: int, *, win: bool, incoming_bytes: int = 0) -> None:
        """incoming_bytes: caller text the same write stores beside it (the merger's applied_outcomes package_id)."""
        at = self._clock()
        recall_bytes = _row_bytes(("outcome", "facts", fact_id, at, "outcome")) + 16 if win else 0
        self._quota_before(db, incoming_bytes=incoming_bytes + recall_bytes, new_row=False)
        col = "wins" if win else "losses"
        cur = db.execute(f"UPDATE facts SET {col} = {col} + 1, "
                         "confidence = (wins + ? + 1.0) / (wins + losses + 3.0) WHERE id = ? AND status = 'active'",
                         (1 if win else 0, fact_id))
        if cur.rowcount != 1:
            raise ValueError(f"fact {fact_id} is not active")
        if win:
            db.execute("INSERT INTO usage (team, tbl, item_id, at, who) VALUES ('outcome', 'facts', ?, ?, 'outcome')",
                       (fact_id, at))
        self._quota_after()

    def prune(self, *, min_losses: int = 3, ratio: float = 3.0) -> list[int]:
        """Retire active facts with losses >= min_losses and losses >= ratio * max(wins, 1). Rows are kept."""
        now = self._clock()
        with self.transaction() as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM facts WHERE status = 'active' AND losses >= ? AND losses >= ? * max(wins, 1) ORDER BY id",
                (min_losses, ratio))]
            for i in ids:
                db.execute("UPDATE facts SET status = 'pruned', ended_at = ?, valid_to = ? WHERE id = ?", (now, now, i))
        return ids

    # ---- readers -------------------------------------------------------

    def _row(self, table: str, r: sqlite3.Row) -> Row:
        return Row(table, r["id"], r["body"], r["topic"], r["key"], r["source"], r["event_time"], r["knowledge_time"],
                   _stored_label(r["tier"], r["classes"]), r["status"], r["supersedes"], r["superseded_by"],
                   r["ended_at"], {c: r[c] for c in (*_EXTRA_COLS.get(table, ()), "fade_exempt", "owner_important", "retrieval_tier")
                                  if c in r.keys()})

    @_locked
    def _select(self, table: str, where: str = "1", args: tuple = ()) -> list[Row]:
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        self.queries += 1
        return [self._row(table, r) for r in self._db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", args)]

    def _tier_where(self, table, tiers):
        if table not in TABLES or not tiers or not set(tiers) <= {'near', 'far', 'deep'}:
            raise ValueError('invalid table or retrieval tiers')
        # Legacy read-only stores default to near until their first writer migration/pass.
        columns = {r[1] for r in self._db.execute(f'PRAGMA table_info({table})')}
        column = 'retrieval_tier' if 'retrieval_tier' in columns else "'near'"
        return f" AND {column} IN ({','.join('?' for _ in tiers)})", tuple(tiers)

    def current(self, table: str, *, tiers=None) -> list[Row]:
        clause, args = self._tier_where(table, tiers) if tiers is not None else ('', ())
        return self._select(table, "status = 'active'" + clause, args)

    @_locked
    def current_batch(self, table: str, *, after: int = 0, limit: int = 64, tiers=None, metadata=True) -> list[Row]:
        """Bounded active rows with recall metadata, for time-budgeted local searches."""
        if table not in TABLES or not 1 <= limit <= 256:
            raise ValueError('invalid table or batch limit')
        clause, args = self._tier_where(table, tiers) if tiers is not None else ('', ())
        self.queries += 1
        rows = [self._row(table, r) for r in self._db.execute(
            f"SELECT * FROM {table} WHERE status = 'active'{clause} AND id > ? ORDER BY id LIMIT ?",
            (*args, after, limit))]
        if not metadata:
            return rows
        if not rows:
            return []
        metadata = self.recall_metadata([(table, r.id) for r in rows])
        weights = self.weights([r.id for r in rows]) if table == 'facts' else {}
        # Count both ends of accepted links, including neighbors outside this batch.
        # Only active endpoints contribute, matching the full snapshot's graph.
        active = ' UNION ALL '.join(f"SELECT '{t}' AS tbl, id FROM {t} WHERE status = 'active'" for t in TABLES)
        links = {r.id: set() for r in rows}
        slots = ','.join('?' for _ in rows)
        self.queries += 1
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
            self.queries += 1
            edges = self._db.execute(f"""
                WITH active AS ({active}), edges AS (
                    SELECT p.tbl AS st, p.fact_id AS sid,
                        CASE j.type WHEN 'array' THEN json_extract(j.value, '$[0]')
                            WHEN 'text' THEN substr(j.value, 1, instr(j.value, ':') - 1) END AS tt,
                        CASE j.type WHEN 'array' THEN json_extract(j.value, '$[1]')
                            WHEN 'text' THEN substr(j.value, instr(j.value, ':') + 1) END AS tid
                    FROM proposals p, json_each(p.refs) j WHERE p.state = 'accepted'
                ) SELECT DISTINCT st, sid, tt, b.id FROM edges
                JOIN active a ON a.tbl = st AND a.id = sid
                JOIN active b ON b.tbl = tt AND b.id = tid
                WHERE (st != tt OR sid != b.id)
                  AND ((st = ? AND sid IN ({slots})) OR (tt = ? AND b.id IN ({slots})))
                """, (table, *links, table, *links))
            for st, sid, tt, tid in edges:
                if st == table and sid in links:
                    links[sid].add((tt, tid))
                if tt == table and tid in links:
                    links[tid].add((st, sid))
        return [replace(r, extra={**r.extra, **metadata.get((table, r.id), {}),
                                  'links': len(links[r.id]), 'weight': weights.get(r.id, 1.0)}) for r in rows]

    def all_rows(self, table: str) -> list[Row]:
        return self._select(table)

    def get(self, table: str, row_id: int) -> Row | None:
        rows = self._select(table, "id = ?", (row_id,))
        return rows[0] if rows else None

    def history(self, table: str, row_id: int) -> list[Row]:
        """The supersede chain containing row_id, oldest first."""
        row = self.get(table, row_id)
        if row is None:
            return []
        while row.supersedes is not None:
            row = self.get(table, row.supersedes)
        chain = [row]
        while chain[-1].superseded_by is not None:
            chain.append(self.get(table, chain[-1].superseded_by))
        return chain

    def as_of(self, table: str, when: str) -> list[Row]:
        """Rows the store believed at knowledge time `when`."""
        when = _check_time(when)
        return self._select(table, "knowledge_time <= ? AND (ended_at IS NULL OR ended_at > ?)", (when, when))

    def search(self, table: str, text: str) -> list[Row]:
        from kyra_memory.fts import ensure_sqlite, like_where, sqlite_ready, sqlite_select
        like = f"%{text}%"
        fallback = like_where("LIKE", "?")
        sql, args = sqlite_select(table, text)
        if sql is not None and not self.read_only:
            with self._lock:
                try:
                    ensure_sqlite(self._db)
                except sqlite3.Error:
                    pass
                if sqlite_ready(self._db, table):
                    try:
                        self.queries += 1
                        return [self._row(table, r) for r in self._db.execute(sql, args)]
                    except sqlite3.Error:
                        pass
        return self._select(table, fallback, (like,) * 3)

    def relabel(self, kind: str, source_prefix: str, label) -> int:
        """Set the label of the active `kind` rows whose source starts with `source_prefix`; returns rows changed."""
        if kind not in TABLES:
            raise ValueError(f"unknown table {kind!r}")
        if not source_prefix:
            raise ValueError("relabel needs a source prefix")
        tier, classes = _label(label)
        with self.transaction() as db:  # substr compare, not LIKE: LIKE ignores case in SQLite
            cur = db.execute(f"UPDATE {kind} SET tier = ?, classes = ? WHERE status = 'active' "
                             "AND substr(source, 1, length(?)) = ? AND (tier != ? OR classes != ?)",
                             (int(tier), json.dumps(sorted(c.value for c in classes)), source_prefix, source_prefix,
                              int(tier), json.dumps(sorted(c.value for c in classes))))
        return cur.rowcount

    def label_counts(self, kind: str, source_prefix: str = "") -> dict[str, int]:
        """{label text: active rows} for `kind`, optionally only rows whose source starts with the prefix."""
        out: dict[str, int] = {}
        for r in self._select(kind, "status = 'active'"):
            if r.source.startswith(source_prefix):
                key = f"T{int(r.label[0])}:{','.join(sorted(c.value for c in r.label[1]))}"
                out[key] = out.get(key, 0) + 1
        return out

    @_locked
    def counts(self) -> dict[str, int]:
        return {t: self._db.execute(f"SELECT count(*) FROM {t} WHERE status = 'active'").fetchone()[0] for t in TABLES}

    @_locked
    def recall_metadata(self, keys=None) -> dict:
        """Read-only metadata; legacy exposure has zero recall weight, never inferred ownership.

        Only accepted proposals have refs attached to a stored row. The assembler
        resolves row references against its permitted candidates; file refs are not degrees.
        """
        out = {}
        groups = {t: [rid for table, rid in keys if table == t] for t in TABLES} if keys is not None else {}
        groups = {t: ids for t, ids in groups.items() if ids}
        args = tuple(value for t, ids in groups.items() for value in (t, *ids))
        clause = ' OR '.join(f"(tbl = ? AND {{column}} IN ({','.join('?' for _ in ids)}))" for ids in groups.values())
        clause = '(' + (clause or '0') + ')'
        usage_where = ' WHERE ' + clause.format(column='item_id') if keys is not None else ''
        refs_where = ' AND ' + clause.format(column='fact_id') if keys is not None else ''
        self.queries += 1
        columns = {r[1] for r in self._db.execute('PRAGMA table_info(usage)')}
        who = 'who' if 'who' in columns else "'agent'"
        self.queries += 1
        for tbl, rid, at, actor in self._db.execute(f'SELECT tbl, item_id, at, {who} FROM usage' + usage_where, args):
            out.setdefault((tbl, rid), {}).setdefault('usage', []).append({'at': at, 'who': actor})
        self.queries += 1
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone():
            self.queries += 1
            for tbl, rid, refs in self._db.execute("SELECT tbl, fact_id, refs FROM proposals "
                                                  "WHERE state = 'accepted' AND fact_id IS NOT NULL" + refs_where, args):
                out.setdefault((tbl, rid), {})['refs'] = json.loads(refs)
        return out

    # ---- usage (pathways) and metrics, written by the merger only -------

    @_locked
    def hot_set(self, team: str, *, k: int = 8, half_life_days: float = 7.0, now: str | None = None) -> list[tuple[Row, float]]:
        """The team's most used active items, by exponentially decayed use count (half-life in days)."""
        half_life = half_life_days * 86400.0
        t_now = datetime.fromisoformat(now or self._clock())
        scores: dict[tuple[str, int], float] = {}
        for tbl, item, at in self._db.execute("SELECT tbl, item_id, at FROM usage WHERE team = ?", (team,)):
            age = max(0.0, (t_now - datetime.fromisoformat(at)).total_seconds())
            scores[(tbl, item)] = scores.get((tbl, item), 0.0) + 0.5 ** (age / half_life)
        out = []
        for (tbl, item), score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
            row = self.get(tbl, item) if tbl in TABLES else None
            if row is not None and row.status == "active":
                out.append((row, score))
            if len(out) >= k:
                break
        return out

    @_locked
    def metrics(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM metrics ORDER BY at")]

    @_locked
    def get_meta(self, key: str) -> str | None:
        found = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return found[0] if found else None
