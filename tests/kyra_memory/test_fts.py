"""FTS5 / Postgres FTS for the load search path; LIKE remains the fallback."""
import sqlite3

from kyra_memory.fts import (
    POSTGRES_IMPL,
    SQLITE_IMPL,
    ensure_sqlite,
    impl_label,
    like_where,
    match_query,
    postgres_clause,
    postgres_ddl,
    sqlite_ready,
    sqlite_select,
)
from kyra_memory.labels import Tier
from kyra_memory.store import MemoryStore

PUBLIC = (Tier.T1, frozenset())


def test_match_query_quotes_tokens():
    assert match_query("invoice needs") == '"invoice" "needs"'
    assert match_query("") == ""
    assert match_query('say "hi"') == '"say" "hi"'


def test_sqlite_fts_finds_a_token_and_skips_a_miss(tmp_path):
    store = MemoryStore(tmp_path, "bench", create=True)
    store.add("facts", "the boiler needs a spare key", source="bench", label=PUBLIC)
    assert sqlite_ready(store._db, "facts")
    assert [r.body for r in store.search("facts", "boiler")] == ["the boiler needs a spare key"]
    assert store.search("facts", "ladder") == []
    store.close()


def test_sqlite_falls_back_to_like_without_fts():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT, topic TEXT, key TEXT, status TEXT)")
    db.execute("INSERT INTO facts VALUES (1, 'the boiler needs a spare key', '', '', 'active')")
    assert sqlite_ready(db, "facts") is False
    sql, args = sqlite_select("facts", "boiler")
    assert sql is not None
    where = like_where("LIKE", "?")
    like = "%boiler%"
    rows = db.execute(f"SELECT body FROM facts WHERE {where}", (like,) * 3).fetchall()
    assert [r[0] for r in rows] == ["the boiler needs a spare key"]
    db.close()


def test_ensure_sqlite_rebuilds_existing_rows():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT, topic TEXT, key TEXT, status TEXT)")
    db.execute("INSERT INTO facts VALUES (1, 'the invoice is late', 'billing', 'invoice', 'active')")
    for table in ("rules", "pinned", "faults", "recipes"):
        db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, body TEXT, topic TEXT, key TEXT, status TEXT)")
    assert ensure_sqlite(db) is True
    sql, args = sqlite_select("facts", "invoice")
    bodies = [r["body"] if isinstance(r, sqlite3.Row) else r[1]
              for r in db.execute(sql, args)]
    assert bodies == ["the invoice is late"]
    db.close()


def test_two_sqlite_tenants_do_not_share_hits(tmp_path):
    a = MemoryStore(tmp_path, "bench", create=True, tenant="acme")
    b = MemoryStore(tmp_path, "bench", create=True, tenant="globex")
    a.add("facts", "acme boiler schedule", source="bench", label=PUBLIC)
    b.add("facts", "globex invoice stack", source="bench", label=PUBLIC)
    assert [r.body for r in a.search("facts", "boiler")] == ["acme boiler schedule"]
    assert a.search("facts", "invoice") == []
    assert [r.body for r in b.search("facts", "invoice")] == ["globex invoice stack"]
    assert b.search("facts", "boiler") == []
    a.close()
    b.close()


def test_search_impl_names_fts():
    assert "FTS" in impl_label("sqlite")
    assert impl_label("sqlite") == SQLITE_IMPL
    assert "FTS" in impl_label("postgres")
    assert impl_label("postgres") == POSTGRES_IMPL


def test_load_json_search_impl_names_fts(tmp_path, monkeypatch):
    from kyra_memory.bench import load

    monkeypatch.setattr(load, "wait_quiet", lambda timeout_s=180.0: {
        "load_1m": 0.0, "threshold": load.QUIET_LOAD, "waited_s": 0.0, "quiet": True})
    monkeypatch.setattr(load, "isolation_tests", lambda repo: {"summary": "not run", "passed": None, "method": "test"})
    monkeypatch.setattr(load, "run_point", lambda *a, **k: {
        "writes_per_s": 1.0, "search_s": {"p95": 0.01}, "topk_pack_s": {"p95": 0.02}})
    result = load.run_load(tmp_path, backend_name="sqlite", tenant_counts=(1,), runs=1, repo=tmp_path)
    assert "FTS" in result["search_impl"]


def test_postgres_clause_and_ddl_name_fts():
    where, args = postgres_clause("invoice")
    assert where is not None
    assert "fts @@" in where
    assert args == ("invoice",)
    assert postgres_clause("  ") == (None, None)
    ddl = postgres_ddl()
    assert "to_tsvector" in ddl
    assert "USING GIN (fts)" in ddl
