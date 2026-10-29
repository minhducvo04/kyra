"""Per-tenant full-text search for the load path: SQLite FTS5, Postgres tsvector. LIKE is the fallback.

SQLite FTS5 lives in the connection's temp schema, so the tenant file is unchanged: hard
purge, WAL truncate and max_bytes still see only the five memory tables. The temp schema
is kept in memory, with secure_delete on, before that index is built. Triggers on the
content tables keep the temp index current for the open connection. After a forget or
purge, the affected index is rebuilt so a deleted token does not stay in an FTS5 data
block. Postgres uses a generated tsvector column plus GIN, installed with the schema.
"""
import re
import sqlite3

from kyra_memory.store import TABLES

SQLITE_IMPL = "SQLite FTS5 (LIKE fallback)"
POSTGRES_IMPL = "Postgres FTS (tsvector/GIN; ILIKE fallback)"

_TOKEN = re.compile(r"[\w./-]+", re.UNICODE)
_FTS = "{}_fts"
_TEMP_STORE_MEMORY = 2  # PRAGMA temp_store: 1 is FILE, 2 is MEMORY


def impl_label(backend: str) -> str:
    """The load JSON `search_impl` value for this backend."""
    return POSTGRES_IMPL if backend == "postgres" else SQLITE_IMPL


def match_query(text: str) -> str:
    """FTS5 MATCH string: quoted tokens, AND-ed. Empty when there is nothing to search."""
    tokens = [t.replace('"', "") for t in _TOKEN.findall(text or "") if t.replace('"', "")]
    return " ".join(f'"{t}"' for t in tokens)


def like_where(op: str, placeholder: str) -> str:
    """Fallback predicate: substring match on body, topic and key."""
    return (f"status = 'active' AND (body {op} {placeholder} OR topic {op} {placeholder} "
            f"OR key {op} {placeholder})")


def sqlite_ready(db, table: str) -> bool:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    try:
        db.execute(f"SELECT 1 FROM temp.{_FTS.format(table)} LIMIT 0")
        return True
    except sqlite3.Error:
        return False


def _private_temp(db) -> None:
    """Put the temp schema in memory and zero its released pages before any FTS table exists.

    Changing temp_store drops every temp table, so this skips the assignment when the
    connection is already in memory. A later call must not wipe an index it just built.
    """
    if db.execute("PRAGMA temp_store").fetchone()[0] != _TEMP_STORE_MEMORY:
        db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA temp.secure_delete=ON")


def ensure_sqlite(db) -> bool:
    """Create per-table temp FTS5 indexes and keep them in sync. Returns False when FTS5 is missing."""
    try:
        _private_temp(db)
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._kyra_fts5_probe USING fts5(x)")
        db.execute("DROP TABLE IF EXISTS temp._kyra_fts5_probe")
    except sqlite3.Error:
        return False
    # One row per content table deleted since the last rebuild. The delete trigger fills it.
    db.execute("CREATE TEMP TABLE IF NOT EXISTS _kyra_fts_dirty (tbl TEXT PRIMARY KEY)")
    if sqlite_ready(db, TABLES[0]):
        return True
    for table in TABLES:
        name = _FTS.format(table)
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.{name} USING fts5("
            f"body, topic, key, id UNINDEXED, tokenize = \"unicode61 tokenchars '_-'\")"
        )
        db.execute(f"DELETE FROM temp.{name}")
        db.execute(
            f"INSERT INTO temp.{name}(body, topic, key, id) "
            f"SELECT body, topic, key, id FROM {table} WHERE status = 'active'"
        )
        # A temp trigger's writes name the temp index. Some SQLite builds reject a schema
        # prefix inside a trigger; the bare name still resolves there, temp before main.
        _trigger(
            db,
            f"CREATE TEMP TRIGGER IF NOT EXISTS {name}_ai AFTER INSERT ON {table} BEGIN "
            f"INSERT INTO temp.{name}(body, topic, key, id) "
            f"SELECT new.body, new.topic, new.key, new.id WHERE new.status = 'active'; END",
        )
        _trigger(
            db,
            f"CREATE TEMP TRIGGER IF NOT EXISTS {name}_ad AFTER DELETE ON {table} BEGIN "
            f"DELETE FROM temp.{name} WHERE id = old.id; "
            f"INSERT OR IGNORE INTO temp._kyra_fts_dirty VALUES ('{table}'); END",
        )
        _trigger(
            db,
            f"CREATE TEMP TRIGGER IF NOT EXISTS {name}_au AFTER UPDATE ON {table} BEGIN "
            f"DELETE FROM temp.{name} WHERE id = old.id; "
            f"INSERT INTO temp.{name}(body, topic, key, id) "
            f"SELECT new.body, new.topic, new.key, new.id WHERE new.status = 'active'; END",
        )
    return True


def _trigger(db, sql: str) -> None:
    """Create one temp trigger. Retry with bare table names when a schema prefix is rejected."""
    try:
        db.execute(sql)
    except sqlite3.OperationalError as exc:
        if "qualified table names" not in str(exc):
            raise
        db.execute(sql.replace("temp.", ""))


def sqlite_dirty(db) -> list[str]:
    """Content tables deleted since the last call, then clear that list. Empty when FTS is absent."""
    try:
        found = db.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type = 'table' AND name = '_kyra_fts_dirty'"
        ).fetchone()
        if not found:
            return []
        rows = [r[0] for r in db.execute("SELECT tbl FROM temp._kyra_fts_dirty")]
        if rows:
            db.execute("DELETE FROM temp._kyra_fts_dirty")
        return rows
    except sqlite3.Error:
        return []


def sqlite_rebuild(db, table: str) -> None:
    """Rebuild one temp FTS5 index so a deleted row's tokens leave its data blocks."""
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    if not sqlite_ready(db, table):
        return
    name = _FTS.format(table)
    db.execute(f"INSERT INTO temp.{name}({name}) VALUES ('rebuild')")


def sqlite_select(table: str, text: str) -> tuple[str, tuple] | tuple[None, None]:
    """SELECT of matching active rows, or (None, None) to fall back to LIKE."""
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    query = match_query(text)
    if not query:
        return None, None
    name = _FTS.format(table)
    sql = (f"SELECT f.* FROM {table} AS f WHERE f.status = 'active' AND f.id IN "
           f"(SELECT id FROM temp.{name} WHERE {name} MATCH ?) ORDER BY f.id")
    return sql, (query,)


def postgres_clause(text: str) -> tuple[str, tuple] | tuple[None, None]:
    """WHERE clause using the generated `fts` column, or (None, None) to fall back to ILIKE."""
    if not (text or "").strip():
        return None, None
    return "status = 'active' AND fts @@ plainto_tsquery('simple', %s)", (text,)


def postgres_ddl() -> str:
    """Idempotent ALTER/INDEX statements for each memory table. Run as the installer."""
    parts = []
    for table in TABLES:
        parts.append(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS fts tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('simple', coalesce(body, '') || ' ' || "
            f"coalesce(topic, '') || ' ' || coalesce(key, ''))) STORED"
        )
        parts.append(f"CREATE INDEX IF NOT EXISTS {table}_fts ON {table} USING GIN (fts)")
    return ";\n".join(parts) + ";\n"
