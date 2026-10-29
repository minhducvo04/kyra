"""Forgotten FTS tokens leave the temp index, and a higher label does not change a search answer."""
import json

from kyra_memory.audit import AuditedBackend, TenantLabel
from kyra_memory.labels import PrivacyClass, Tier
from kyra_memory.sqlite_backend import SqliteBackend

PUBLIC = (Tier.T1, frozenset())
HIDDEN = (Tier.T2, frozenset({PrivacyClass.health}))
MARKER = "zqxmarker"
FILLER = 400  # enough that a plain FTS delete leaves the token in a data block


def _blocks(db):
    return db.execute(
        "SELECT count(*) FROM temp.facts_fts_data WHERE instr(block, ?) > 0", (MARKER,)
    ).fetchone()[0]


def test_forget_leaves_no_fts_token_in_temp_blocks(tmp_path):
    backend = SqliteBackend(tmp_path, "svc")
    try:
        backend.open_tenant("acme", create=True)
        db = backend._store("acme")._db
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
        assert db.execute("PRAGMA temp.secure_delete").fetchone()[0] == 1
        rid = backend.add("acme", "facts", f"only {MARKER} here", source="agent", label=PUBLIC)
        for i in range(FILLER):
            backend.add("acme", "facts", f"filler fact number {i} about boilers", source="agent", label=PUBLIC)
        assert _blocks(db) > 0
        assert backend.forget("acme", "facts", rid) == 1
        assert _blocks(db) == 0
        assert backend.search("acme", "facts", MARKER) == []
        assert backend.search("acme", "facts", "boilers")
    finally:
        backend.close()


def test_search_for_a_hidden_word_matches_a_miss(tmp_path):
    labels = {"acme": TenantLabel(Tier.T1)}
    backend = AuditedBackend(SqliteBackend(tmp_path / "db", "svc"), tmp_path / "audit", labels, actor="tester")
    try:
        backend.open_tenant("acme", create=True)
        public_id = backend.add("acme", "facts", "public boiler note", source="agent", label=PUBLIC)
        hidden_id = backend.add("acme", "facts", f"hidden {MARKER} note", source="agent", label=HIDDEN)
        hidden_hit = backend.search("acme", "facts", MARKER)
        missing = backend.search("acme", "facts", "qqqabsent")
        assert hidden_hit == missing == []
        assert [r.body for r in backend.search("acme", "facts", "note")] == ["public boiler note"]
        assert backend.history("acme", "facts", hidden_id) == []
        assert [r.body for r in backend.history("acme", "facts", public_id)] == ["public boiler note"]
        text = backend.audit_path("acme").read_text(encoding="utf-8")
        assert MARKER not in text
        rows = [json.loads(line) for line in text.splitlines()]
        searches = [r for r in rows if r["action"] == "search"]
        assert [r["result"] for r in searches] == ["ok", "ok", "ok"]
        assert searches[0]["withheld"] == 1
        assert "withheld" not in searches[1]
        assert searches[2]["withheld"] == 1 and searches[2]["result"] == "ok"
        histories = [r for r in rows if r["action"] == "history"]
        assert histories[0]["result"] == "ok" and histories[0]["withheld"] == 1
        assert histories[1]["result"] == "ok" and "withheld" not in histories[1]
        assert all("body" not in r and "text" not in r for r in rows)
    finally:
        backend.close()
