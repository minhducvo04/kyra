"""Tenant erasure reaches persistent derived copies, using real scratch files."""
import json
import sqlite3

import pytest

from kyra_memory import inbox, vector_index
from kyra_memory.labels import Tier
from kyra_memory.policy import TenantPolicy
from kyra_memory.sqlite_backend import SqliteBackend
from kyra_memory.tenancy import tenant_root


@pytest.mark.parametrize("operation", ["forget", "expire"])
def test_purge_removes_vectors_inbox_and_hot(tmp_path, operation):
    backend = SqliteBackend(tmp_path, "svc", clock=lambda: "2026-01-01T00:00:00+00:00")
    try:
        backend.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 1}))
        for tenant in ("acme", "globex"):
            if tenant != "acme":
                backend.open_tenant(tenant, create=True)
            inbox.append_add(tmp_path, "builder", "facts", "synthetic forgotten body", source="agent",
                             label=(Tier.T1, frozenset()), tenant=tenant)
            backend.merge(tenant)
            backend.add(tenant, "rules", "retained control", source="owner", label=(Tier.T1, frozenset()))
            store = backend._store(tenant)
            store._db.execute("INSERT INTO usage (team, tbl, item_id, at) VALUES (?, ?, ?, ?)",
                              ("builder", "facts", 1, "2026-01-01T00:00:00+00:00"))
            inbox.refresh_hot(store, tmp_path, teams=["builder"])
            for _ in range(2):
                vector_index.reindex(tmp_path, "synthetic", encoder=lambda texts, name: [[1., 0.] for t in texts],
                                     scope="svc", tenant=tenant)
        path = inbox.inbox_path(tmp_path, "builder", "acme")
        offset = path.stat().st_size
        inbox.append_add(tmp_path, "builder", "faults", "pending control", source="owner",
                         label=(Tier.T1, frozenset()), tenant="acme")
        if operation == "forget":
            assert backend.forget("acme", "facts", 1) == 1
        else:
            assert backend.expire("acme", now="2026-01-02T00:00:00+00:00") == {"facts": 1}
        root = tenant_root(tmp_path, "acme")
        for index in root.glob("vectors*.sqlite3"):
            with sqlite3.connect(index) as db:
                assert db.execute("SELECT id FROM vectors ORDER BY id").fetchall() == [("rules:1",)]
                assert db.execute("SELECT count FROM meta").fetchone()[0] == 1
        assert b"synthetic forgotten body" not in path.read_bytes()
        assert int(backend._store("acme").get_meta("inbox:builder")) == offset
        hot = json.loads(inbox.hot_path(tmp_path, "builder", "acme").read_text())
        assert hot["items"] == []
        assert backend.merge("acme").applied == {"add": 1}
        assert len(backend.current("acme", "rules")) == 1
        assert len(backend.current("acme", "faults")) == 1
        assert b"synthetic forgotten body" in inbox.inbox_path(tmp_path, "builder", "globex").read_bytes()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            backend._store("acme")._db.execute("ATTACH DATABASE ':memory:' AS forbidden")
    finally:
        backend.close()


def test_expire_proposal_body_and_retry_cleanup_failure(tmp_path):
    backend = SqliteBackend(tmp_path, "svc", clock=lambda: "2026-01-01T00:00:00+00:00")
    try:
        backend.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 1}))
        inbox.append_add(tmp_path, "builder", "facts", "pending synthetic body", source="agent",
                         label=(Tier.T1, frozenset()), status="proposed", tenant="acme")
        backend.merge("acme")
        hot = inbox.hot_path(tmp_path, "builder", "acme")
        hot.parent.mkdir(parents=True, exist_ok=True)
        hot.write_text("broken json")
        with pytest.raises(ValueError):
            backend.expire("acme", now="2026-01-02T00:00:00+00:00")
        assert len(backend._store("acme").proposals()) == 1
        hot.write_text('{"items": []}')
        assert backend.expire("acme", now="2026-01-02T00:00:00+00:00") == {}
        assert backend._store("acme").proposals() == []
        assert b"pending synthetic body" not in inbox.inbox_path(tmp_path, "builder", "acme").read_bytes()
    finally:
        backend.close()
