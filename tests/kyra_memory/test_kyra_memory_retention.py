"""MS5: per-tenant quotas, TTL retention and real erasure (fork F4, proceeded on guess). Synthetic data only."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kyra_memory import inbox
from kyra_memory.audit import AuditedBackend, TenantLabel
from kyra_memory.labels import Tier
from kyra_memory.policy import QuotaExceededError, TenantPolicy
from kyra_memory.sqlite_backend import PURGE_KEY, SqliteBackend, _allow_scratch_attach
from kyra_memory.store import MemoryStore, _deny_attach, _text_bytes

PUBLIC = (Tier.T1, frozenset())
MARKER = "synthetic-marker-umber-foxglove-48151623"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now.isoformat(timespec="microseconds")

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def backend(tmp_path):
    b = SqliteBackend(tmp_path, "svc", clock=Clock())
    yield b
    b.close()


def _files_bytes(db: Path) -> bytes:
    out = db.read_bytes()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            out += side.read_bytes()
    return out


def test_noisy_neighbour_max_rows_is_refused_and_counted(backend):
    backend.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=2))
    backend.open_tenant("globex", create=True)
    backend.add("acme", "facts", "a1", source="agent", label=PUBLIC)
    backend.add("acme", "facts", "a2", source="agent", label=PUBLIC)
    with pytest.raises(QuotaExceededError) as err:
        backend.add("acme", "facts", "a3", source="agent", label=PUBLIC)
    assert (err.value.tenant, err.value.kind) == ("acme", "max_rows")
    assert backend.quota_refusals("acme") == {"max_rows": 1}
    assert backend.stats("acme")["quota_refusals"] == 1
    backend.add("globex", "facts", "g1", source="agent", label=PUBLIC)  # the other tenant keeps writing
    assert backend.counts("globex")["facts"] == 1
    assert backend.quota_refusals("globex") == {}


def test_max_bytes_refuses_before_writing(backend):
    backend.open_tenant("acme", create=True)
    backend._store("acme")._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # no reclaimable WAL in the bound
    backend.set_policy("acme", TenantPolicy(max_bytes=int(backend.stats("acme")["bytes"])))
    with pytest.raises(QuotaExceededError) as err:
        backend.add("acme", "facts", "too big", source="agent", label=PUBLIC)
    assert err.value.kind == "max_bytes"
    assert backend.counts("acme")["facts"] == 0


def test_writes_per_minute_window_slides(tmp_path):
    now = [1000.0]
    b = SqliteBackend(tmp_path, "svc", clock=Clock(), time_fn=lambda: now[0])
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(writes_per_minute=2))
        b.add("acme", "facts", "w1", source="agent", label=PUBLIC)
        b.add("acme", "facts", "w2", source="agent", label=PUBLIC)
        with pytest.raises(QuotaExceededError) as err:
            b.add("acme", "facts", "w3", source="agent", label=PUBLIC)
        assert err.value.kind == "writes_per_minute"
        now[0] += 61.0
        b.add("acme", "facts", "w3", source="agent", label=PUBLIC)
        assert b.counts("acme")["facts"] == 3
    finally:
        b.close()


def test_ttl_expires_only_the_tables_named(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 3600.0}))
        b.add("acme", "facts", "old fact", source="agent", label=PUBLIC)
        b.add("acme", "rules", "old rule", source="owner", label=PUBLIC)
        clock.advance(hours=2)
        b.add("acme", "facts", "fresh fact", source="agent", label=PUBLIC)
        assert b.expire("acme") == {"facts": 1}
        assert [r.body for r in b.current("acme", "facts")] == ["fresh fact"]
        assert [r.body for r in b.current("acme", "rules")] == ["old rule"]  # no TTL named for rules
    finally:
        b.close()


def test_forget_removes_the_markers_bytes_from_db_and_wal(backend, tmp_path):
    backend.open_tenant("acme", create=True)
    first = backend.add("acme", "facts", f"fact {MARKER}", source="agent", key="k", label=PUBLIC)
    db = tmp_path / "tenants" / "acme" / "svc.sqlite3"
    assert MARKER.encode() in _files_bytes(db)  # proven present before the erase
    second = backend.add("acme", "facts", "corrected, no marker", source="agent", key="k", label=PUBLIC)
    assert backend.forget("acme", "facts", second) == 2  # the whole chain, hard-deleted (secure_delete + WAL truncation)
    assert backend.history("acme", "facts", first) == []
    assert MARKER.encode() not in _files_bytes(db)


def test_delete_tenant_erases_its_bytes_and_leaves_the_other(backend, tmp_path):
    backend.open_tenant("acme", create=True)
    backend.open_tenant("globex", create=True)
    backend.add("acme", "facts", f"acme {MARKER}", source="agent", label=PUBLIC)
    backend.add("globex", "facts", "globex fact", source="agent", label=PUBLIC)
    backend.delete_tenant("acme")
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and MARKER.encode() in p.read_bytes()]
    assert leftovers == []
    assert backend.counts("globex")["facts"] == 1


def test_owner_scope_still_keeps_supersede_history(tmp_path):
    store = MemoryStore(tmp_path, "owner", create=True)  # tenant=None: never hard-deleted by the backend
    try:
        store.add("facts", "first version", source="agent", key="k", label=PUBLIC)
        new = store.add("facts", "second version", source="agent", key="k", label=PUBLIC)
        assert [r.body for r in store.history("facts", new)] == ["first version", "second version"]
        assert len(store.all_rows("facts")) == 2
    finally:
        store.close()


def test_policy_rejects_a_ttl_for_an_unknown_table():
    with pytest.raises(ValueError):
        TenantPolicy(ttl_seconds={"nope": 60.0})


def _hits(folder: Path, marker: str = MARKER) -> dict[str, bool]:
    return {p.name: marker.encode() in p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_expire_purges_marker_bytes_and_side_rows(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        fid = b.add("acme", "facts", f"old {MARKER}", source="agent", key="k", label=PUBLIC)
        b.add("acme", "facts", f"old v2 {MARKER}", source="agent", key="k", label=PUBLIC)
        st = b._store("acme")
        with st.transaction() as db:
            db.execute("INSERT INTO usage (team, tbl, item_id, at) VALUES ('t', 'facts', ?, ?)", (fid, clock()))
        st.set_weights({fid: 1.1})
        assert any(_hits(tmp_path / "tenants" / "acme").values())  # present before the erase
        clock.advance(hours=1)
        assert b.expire("acme") == {"facts": 2}
        assert b.search("acme", "facts", MARKER) == []
        assert st._db.execute("SELECT count(*) FROM usage").fetchone()[0] == 0
        assert st._db.execute("SELECT count(*) FROM fact_weights").fetchone()[0] == 0
        hits = _hits(tmp_path / "tenants" / "acme")  # main file, -wal and -shm alike
        assert not any(hits.values()), hits
    finally:
        b.close()
    hits = _hits(tmp_path / "tenants" / "acme")
    assert not any(hits.values()), hits


def test_expire_with_an_open_reader_raises_then_a_retry_purges(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        b.add("acme", "facts", f"old {MARKER}", source="agent", label=PUBLIC)
        b._store("acme")._db.execute("PRAGMA busy_timeout=300")
        reader = MemoryStore(tmp_path, "svc", read_only=True, tenant="acme")
        try:
            reader._db.execute("BEGIN")
            reader.all_rows("facts")  # holds a read snapshot
            clock.advance(hours=1)
            raised = None
            try:
                b.expire("acme")
            except Exception as e:  # noqa: BLE001 - any loud failure is right; silence is the defect
                raised = e
            hits = _hits(tmp_path / "tenants" / "acme")
            assert raised is not None or not any(hits.values()), f"expire returned silently; hits {hits}"
        finally:
            reader.close()
        assert b.expire("acme") == {}  # the rows already went; the retry must still purge the WAL
        hits = _hits(tmp_path / "tenants" / "acme")
        assert not any(hits.values()), hits
    finally:
        b.close()


def test_expire_purges_pending_proposals_of_that_table(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        inbox.append(tmp_path, "builder", {"kind": "add", "table": "facts", "body": f"proposed {MARKER}",
                                           "source": "agent", "status": "proposed", "at": clock(),
                                           "label": [1, []]}, "acme")
        b.merge("acme")
        assert len(b._store("acme").proposals()) == 1
        clock.advance(days=30)
        b.expire("acme")
        assert b._store("acme").proposals() == []
    finally:
        b.close()


def test_policy_is_keyed_by_tenant_for_rows_and_ttl(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=2, ttl_seconds={"facts": 60.0}))
        b.open_tenant("globex", create=True)  # no bounds of its own
        for i in range(4):  # globex writes past acme's bound; acme's policy must not apply to it
            b.add("globex", "facts", f"g{i}", source="agent", label=PUBLIC)
        b.add("acme", "facts", "a1", source="agent", label=PUBLIC)
        assert b.counts("globex")["facts"] == 4
        clock.advance(hours=1)
        b.expire("acme")
        b.expire("globex")  # globex has no TTL either
        assert b.counts("acme")["facts"] == 0 and b.counts("globex")["facts"] == 4
    finally:
        b.close()


def test_ttl_cutoff_uses_knowledge_time_not_a_backdated_event_time(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 3600.0}))
        ancient = (T0 - timedelta(days=3650)).isoformat(timespec="microseconds")
        b.add("acme", "facts", "learned now, happened long ago", source="agent", label=PUBLIC,
              event_time=ancient)
        clock.advance(minutes=30)  # inside the TTL by knowledge_time; far outside it by event_time
        assert b.expire("acme") == {}
        assert b.counts("acme")["facts"] == 1
        clock.advance(hours=1)
        assert b.expire("acme") == {"facts": 1}
    finally:
        b.close()


def test_max_rows_also_bounds_the_inbox_merge_path(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=2))
        for i in range(5):
            inbox.append(tmp_path, "builder", {"kind": "add", "table": "facts", "body": f"m{i}",
                                               "source": "agent", "at": clock(), "label": [1, []]}, "acme")
        with pytest.raises(QuotaExceededError):
            b.merge("acme")
        assert sum(b.counts("acme").values()) <= 2
        b.set_policy("acme", TenantPolicy(max_rows=10))  # the offset never moved, so the retry replays the batch
        b.merge("acme")
        assert sum(b.counts("acme").values()) == 5
    finally:
        b.close()


def test_proposals_count_toward_max_rows(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=1))
        inbox.append(tmp_path, "builder", {"kind": "add", "table": "facts", "body": "p1", "source": "agent",
                                           "status": "proposed", "at": clock(), "label": [1, []]}, "acme")
        b.merge("acme")
        assert len(b._store("acme").proposals()) == 1
        with pytest.raises(QuotaExceededError):  # the pending proposal already spends the one row
            b.add("acme", "facts", "over", source="agent", label=PUBLIC)
    finally:
        b.close()


def test_max_bytes_cannot_be_overshot_by_one_big_write(backend):
    backend.open_tenant("acme", create=True)
    base = int(backend.stats("acme")["bytes"])
    backend.set_policy("acme", TenantPolicy(max_bytes=base + 4096))
    with pytest.raises(QuotaExceededError):
        backend.add("acme", "facts", "x" * 2_000_000, source="agent", label=PUBLIC)
    assert backend.stats("acme")["bytes"] <= base + 4096 + 65536


def test_rate_window_does_not_grow_without_a_rate_policy(backend):
    backend.open_tenant("acme", create=True)
    for i in range(50):
        backend.add("acme", "facts", f"r{i}", source="agent", label=PUBLIC)
    assert len(backend._writes.get("acme", ())) == 0


def test_quota_check_does_not_inflate_the_queries_metric(backend):
    backend.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=1000))
    before = backend.stats("acme")["queries"]
    backend.add("acme", "facts", "q", source="agent", label=PUBLIC)
    assert backend.stats("acme")["queries"] == before


def test_authorizer_still_denies_a_file_attach_after_expire_vacuum(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        b.add("acme", "facts", "a", source="agent", label=PUBLIC)
        clock.advance(hours=1)
        b.expire("acme")
        other = tmp_path / "other.sqlite3"
        sqlite3.connect(other).close()
        with pytest.raises(sqlite3.DatabaseError):
            b._store("acme")._db.execute(f"ATTACH '{other}' AS o")
    finally:
        b.close()


def test_vacuum_is_denied_by_the_store_authorizer(tmp_path):
    db = sqlite3.connect(tmp_path / "x.sqlite3", isolation_level=None)
    try:
        db.execute("CREATE TABLE t (x)")
        db.set_authorizer(_deny_attach)
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("VACUUM")
    finally:
        db.close()


@pytest.mark.parametrize("kw", [
    {"ttl_seconds": {"facts": float("nan")}}, {"ttl_seconds": {"facts": float("inf")}},
    {"ttl_seconds": {"facts": 1e30}}, {"ttl_seconds": {"facts": "60"}}, {"ttl_seconds": {"facts": True}},
    {"max_rows": True}, {"max_rows": 2.5}, {"max_rows": "5"}, {"max_bytes": 1.5},
    {"writes_per_minute": False},
])
def test_policy_refuses_unusable_values(kw):
    with pytest.raises((ValueError, TypeError)):
        TenantPolicy(**kw)


# ---- fix-back 2 (REVIEW-MS5-B) -------------------------------------------------

def _inbox_add(root, clock, body, **extra):
    inbox.append(root, "builder", {"kind": "add", "table": "facts", "body": body, "source": "agent",
                                   "at": clock(), "label": [1, []], **extra}, "acme")


@pytest.mark.parametrize("kind", ["used", "metric"])
def test_merged_used_and_metric_records_are_bounded_by_max_bytes(tmp_path, kind):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        fid = b.add("acme", "facts", "f", source="agent", label=PUBLIC)
        bound = int(b.stats("acme")["bytes"]) + 64 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        for i in range(40):  # about 1.5 MB if unbounded; small enough to stay in the page cache until COMMIT
            rec = ({"kind": "used", "items": [["facts", fid]] * 500} if kind == "used" else
                   {"kind": "metric", "task": f"t{i}-" + "x" * 10_000, "build_ms": 1.0, "memory_calls": 1, "chars": 1})
            inbox.append(tmp_path, "builder", {**rec, "at": clock()}, "acme")
        with pytest.raises(QuotaExceededError):
            b.merge("acme")
        assert b.stats("acme")["bytes"] <= bound + 65536
    finally:
        b.close()


def test_merge_batch_longer_than_the_rate_bound_lands_then_blocks_until_the_window_drains(tmp_path):
    clock, now = Clock(), [1000.0]
    b = SqliteBackend(tmp_path, "svc", clock=clock, time_fn=lambda: now[0])
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(writes_per_minute=3))
        for i in range(5):
            _inbox_add(tmp_path, clock, f"m{i}")
        b.merge("acme")
        assert b.counts("acme")["facts"] == 5  # one batch may overshoot; it lands whole (no wedge)
        with pytest.raises(QuotaExceededError) as err:
            b.add("acme", "facts", "d1", source="agent", label=PUBLIC)  # the batch spent five writes of three
        assert err.value.kind == "writes_per_minute" and err.value.observed == 5
        _inbox_add(tmp_path, clock, "m5")
        with pytest.raises(QuotaExceededError):
            b.merge("acme")  # a second batch inside the window is refused too
        now[0] += 61.0
        b.add("acme", "facts", "d1", source="agent", label=PUBLIC)  # drained: room again
        assert b.counts("acme")["facts"] == 6
    finally:
        b.close()


def test_a_rolled_back_merge_does_not_spend_the_rate_window(tmp_path):
    clock, now = Clock(), [1000.0]
    b = SqliteBackend(tmp_path, "svc", clock=clock, time_fn=lambda: now[0])
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(writes_per_minute=2, max_rows=3))
        b.add("acme", "facts", "d1", source="agent", label=PUBLIC)
        for i in range(5):
            _inbox_add(tmp_path, clock, f"m{i}")
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")  # refused on max_rows at the third row, rolled back whole
        assert err.value.kind == "max_rows" and b.counts("acme")["facts"] == 1
        b.add("acme", "facts", "d2", source="agent", label=PUBLIC)  # one committed write so far, so this fits
    finally:
        b.close()


@pytest.mark.parametrize("at", ["2099-01-01T00:00:00+00:00", (T0 - timedelta(days=3650)).isoformat()])
def test_pending_proposal_expires_by_store_time_not_its_at(tmp_path, at):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 3600.0}))
        _inbox_add(tmp_path, clock, "proposed body", status="proposed", at=at)
        b.merge("acme")
        clock.advance(minutes=1)  # learned a minute ago: inside the TTL whatever `at` claims
        b.expire("acme")
        assert len(b._store("acme").proposals()) == 1
        clock.advance(hours=2)
        b.expire("acme")
        assert b._store("acme").proposals() == []
    finally:
        b.close()


def test_merging_a_proposal_is_refused_at_max_rows(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=1))
        b.add("acme", "facts", "d1", source="agent", label=PUBLIC)
        _inbox_add(tmp_path, clock, "p1", status="proposed")
        with pytest.raises(QuotaExceededError):
            b.merge("acme")
        assert b._store("acme").proposals() == []
    finally:
        b.close()


@pytest.mark.parametrize("write", ["set_weights", "record_outcome"])
def test_weights_and_outcomes_are_rate_bound(tmp_path, write):
    b = SqliteBackend(tmp_path, "svc", clock=Clock(), time_fn=lambda: 1000.0)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(writes_per_minute=1))
        fid = b.add("acme", "facts", "d1", source="agent", label=PUBLIC)
        with pytest.raises(QuotaExceededError) as err:
            b.set_weights("acme", {fid: 1.2}) if write == "set_weights" else b.record_outcome("acme", fid, win=True)
        assert err.value.kind == "writes_per_minute"
    finally:
        b.close()


def test_scratch_attach_is_denied_again_after_expire(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        b.expire("acme")
        with pytest.raises(sqlite3.DatabaseError):
            b._store("acme")._db.execute("ATTACH '' AS scratch")
    finally:
        b.close()


def test_scratch_attach_authorizer_allows_only_the_empty_filename(tmp_path):
    assert _allow_scratch_attach(sqlite3.SQLITE_ATTACH, "") == sqlite3.SQLITE_OK
    for target in (str(tmp_path / "other.sqlite3"), ":memory:", "file:x"):
        assert _allow_scratch_attach(sqlite3.SQLITE_ATTACH, target) == sqlite3.SQLITE_DENY
    assert _allow_scratch_attach(sqlite3.SQLITE_SELECT) == sqlite3.SQLITE_OK


def test_committed_writes_are_not_counted_twice_toward_max_bytes(backend):
    backend.open_tenant("acme", create=True)
    backend.set_policy("acme", TenantPolicy(max_bytes=int(backend.stats("acme")["bytes"]) + 384 * 1024))
    for i in range(10):  # about 290 KB on disk; counting committed growth again would pass 384 KB
        backend.add("acme", "facts", f"{i}-" + "y" * 15_000, source="agent", label=PUBLIC)
    assert backend.counts("acme")["facts"] == 10


# ---- fix-back 3 (REVIEW-MS5-C) -------------------------------------------------

def test_a_rolled_back_merge_is_not_recorded_at_the_next_commit(tmp_path):
    clock, now = Clock(), [1000.0]
    b = SqliteBackend(tmp_path, "svc", clock=clock, time_fn=lambda: now[0])
    try:
        b.open_tenant("acme", create=True,
                      policy=TenantPolicy(writes_per_minute=3, max_rows=3, ttl_seconds={"recipes": 3600.0}))
        b.add("acme", "facts", "d1", source="agent", label=PUBLIC)
        for i in range(5):
            _inbox_add(tmp_path, clock, f"m{i}")
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")  # two records pass their checks, the third hits max_rows: rolled back whole
        assert err.value.kind == "max_rows"
        b.expire("acme")  # an unrelated commit must not record the rolled-back records
        b.add("acme", "facts", "d2", source="agent", label=PUBLIC)
        b.add("acme", "facts", "d3", source="agent", label=PUBLIC)  # 3 of 3: d1, d2, d3
        assert b.counts("acme")["facts"] == 3
    finally:
        b.close()


def test_expire_works_on_a_store_whose_proposals_table_predates_learned_at(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    b.open_tenant("acme", create=True)
    b.add("acme", "facts", "kept", source="agent", label=PUBLIC)
    _inbox_add(tmp_path, clock, "legacy proposal", status="proposed")
    b.merge("acme")
    b.close()
    db = sqlite3.connect(tmp_path / "tenants" / "acme" / "svc.sqlite3")
    db.execute("ALTER TABLE proposals DROP COLUMN learned_at")  # the schema an older store has
    db.commit()
    db.close()
    clock.advance(days=1)
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", policy=TenantPolicy(ttl_seconds={"facts": 3600.0 * 48, "recipes": 3600.0}))
        b.expire("acme")  # no "no such column: learned_at"; the facts TTL pass ran with it
        assert b.counts("acme")["facts"] == 1
        assert [p["learned_at"] for p in b._store("acme").proposals()] == [None]
        b.set_policy("acme", TenantPolicy(ttl_seconds={"facts": 3600.0}))
        b.expire("acme")  # a legacy row falls back to its `at`, a day old
        assert b._store("acme").proposals() == [] and b.counts("acme")["facts"] == 0
    finally:
        b.close()


def test_a_batch_refilling_pages_freed_by_forget_is_refused_at_the_bound(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        for i in [b.add("acme", "facts", f"old {i} " + "o" * 10_000, source="agent", label=PUBLIC)
                  for i in range(150)]:
            b.forget("acme", "facts", i)  # pages go to the freelist; the file keeps its size
        bound = int(b.stats("acme")["bytes"]) + 64 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        for i in range(150):
            _inbox_add(tmp_path, clock, f"new {i} " + "n" * 10_000)
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")
        assert err.value.kind == "max_bytes" and b.counts("acme")["facts"] == 0
        assert b.stats("acme")["bytes"] <= bound + 65536
        b.add("acme", "facts", "small", source="agent", label=PUBLIC)  # a write that fits still lands
    finally:
        b.close()


def test_expire_vacuums_the_freed_pages_away(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        for i in range(40):
            b.add("acme", "facts", f"old {i} " + "o" * 10_000, source="agent", label=PUBLIC)
        clock.advance(hours=1)
        assert b.expire("acme") == {"facts": 40}
        assert b._store("acme")._db.execute("PRAGMA freelist_count").fetchone()[0] == 0
    finally:
        b.close()


@pytest.mark.parametrize("kind", ["source", "proposal_quote", "proposal_refs"])
def test_forget_then_small_bodies_with_large_caller_fields_are_refused_at_the_bound(tmp_path, kind):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        for i in [b.add("acme", "facts", f"old {i} " + "o" * 10_000, source="agent", label=PUBLIC)
                  for i in range(150)]:
            b.forget("acme", "facts", i)  # released pages: the batch below does not raise page_count
        bound = int(b.stats("acme")["bytes"]) + 64 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        big = {"source": {"source": "s" * 10_000},
               "proposal_quote": {"status": "proposed", "quote": "q" * 10_000},
               "proposal_refs": {"status": "proposed", "refs": ["r" * 10_000]}}[kind]
        for i in range(150):
            _inbox_add(tmp_path, clock, f"b{i}", **big)  # a tiny body; the bytes ride in another field
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")
        assert err.value.kind == "max_bytes"
        assert b.counts("acme")["facts"] == 0 and b._store("acme").proposals() == []
        assert b.stats("acme")["bytes"] <= bound + 65536
    finally:
        b.close()


@pytest.mark.parametrize("n,size", [(60, 10_000), (100, 6_000)])
def test_forget_then_outcomes_with_large_package_ids_are_refused_at_the_bound(tmp_path, n, size):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        fid = b.add("acme", "facts", "keep me", source="agent", label=PUBLIC)
        for i in [b.add("acme", "facts", f"old {i} " + "o" * 10_000, source="agent", label=PUBLIC)
                  for i in range(150)]:
            b.forget("acme", "facts", i)
        bound = int(b.stats("acme")["bytes"]) + 64 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        for i in range(n):  # applied_outcomes stores the caller's package_id as text
            inbox.append(tmp_path, "builder", {"kind": "outcome", "package_id": f"{i}-" + "p" * size, "fact_id": fid,
                                               "outcome": "helped", "at": clock()}, "acme")
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")
        assert err.value.kind == "max_bytes"
        assert b.stats("acme")["bytes"] <= bound + 65536
        db = b._store("acme")._db
        assert db.execute("SELECT wins FROM facts WHERE id = ?", (fid,)).fetchone()[0] == 0  # rolled back whole
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'applied_outcomes'").fetchone()
    finally:
        b.close()


def _forgotten(tmp_path):
    b = SqliteBackend(tmp_path, "svc", clock=Clock())
    b.open_tenant("acme", create=True)
    fid = b.add("acme", "facts", "keep me", source="agent", label=PUBLIC)
    for i in [b.add("acme", "facts", f"old {i} " + "o" * 10_000, source="agent", label=PUBLIC) for i in range(150)]:
        b.forget("acme", "facts", i)
    bound = int(b.stats("acme")["bytes"]) + 64 * 1024
    b.set_policy("acme", TenantPolicy(max_bytes=bound))
    return b, fid, bound


@pytest.mark.parametrize("kind", ["text_values", "many_numeric"])
def test_set_weights_after_forget_stays_within_the_bound(tmp_path, kind):
    b, fid, bound = _forgotten(tmp_path)
    try:
        if kind == "text_values":  # the column is REAL, but SQLite would store text as given
            with pytest.raises(ValueError):
                b.set_weights("acme", {i: "w" * 10_000 for i in range(100)})
        else:
            with pytest.raises(QuotaExceededError) as err:
                b.set_weights("acme", {i: 1.0 for i in range(60_000)})
            assert err.value.kind == "max_bytes"
        assert b.stats("acme")["bytes"] <= bound + 65536
        b.set_weights("acme", {fid: 1.2})  # a normal reweight still fits
        assert b._store("acme").weights() == {fid: 1.2}
    finally:
        b.close()


def test_set_weights_on_a_fresh_store_is_bounded(tmp_path):
    b = SqliteBackend(tmp_path, "svc", clock=Clock())
    try:
        b.open_tenant("acme", create=True)
        bound = int(b.stats("acme")["bytes"]) + 64 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        with pytest.raises(QuotaExceededError):
            b.set_weights("acme", {i: 1.0 for i in range(200_000)})
        assert b.stats("acme")["bytes"] <= bound + 65536
    finally:
        b.close()


def test_one_huge_used_record_on_a_fresh_store_is_bounded(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        fid = b.add("acme", "facts", "keep", source="agent", label=PUBLIC)
        b._store("acme")._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        bound = int(b.stats("acme")["bytes"]) + 2 * 1024 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        inbox.append(tmp_path, "builder", {"kind": "used", "items": [["facts", fid]] * 150_000, "at": clock()}, "acme")
        with pytest.raises(QuotaExceededError):
            b.merge("acme")  # one usage row per item: ~10x the raw line once stored
        assert b.stats("acme")["bytes"] <= bound + 65536
        inbox.append(tmp_path, "builder", {"kind": "used", "items": [["facts", fid]] * 10, "at": clock()}, "acme")
        b.set_policy("acme", TenantPolicy(max_bytes=bound * 20))
        b.merge("acme")  # once the bound has room the same inbox replays
        assert b._store("acme")._db.execute("SELECT count(*) FROM usage").fetchone()[0] == 150_010
    finally:
        b.close()


@pytest.mark.parametrize("team,big", [("a" + "b" * 40, True), ("a", False)])
def test_a_used_record_is_charged_at_least_what_it_stores(tmp_path, team, big):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        fid = b.add("acme", "facts", "keep", source="agent", label=PUBLIC)
        store = b._store("acme")
        charged, check = [], store._quota_check
        store._quota_check = lambda db, *, incoming_bytes, new_row: (charged.append(incoming_bytes),
                                                                      check(db, incoming_bytes=incoming_bytes, new_row=new_row))

        def size():
            return store._db.execute("PRAGMA page_count").fetchone()[0] * store._db.execute("PRAGMA page_size").fetchone()[0]

        before = size()
        items = [["facts", (2**62 + k) if big else fid] for k in range(50_000)]  # 8-byte ids: the widest integer
        inbox.append(tmp_path, team, {"kind": "used", "items": items, "at": "2026-01-01T00:00+00:00"}, "acme")
        b.merge("acme")
        assert sum(charged) >= size() - before, (sum(charged), size() - before)
    finally:
        b.close()


def _wal(b):
    return Path(str(b._store("acme").path) + "-wal")


def test_a_large_batch_under_the_bound_does_not_leave_a_doubled_wal(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        bound = int(b.stats("acme")["bytes"]) + 20 * 1024 * 1024
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        for i in range(8000):  # about 9 MB of rows: the WAL keeps a full copy after the autocheckpoint
            _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
        b.merge("acme")
        assert b.counts("acme")["facts"] == 8000
        assert b.stats("acme")["bytes"] <= bound + 65536
        b.add("acme", "facts", "small", source="agent", label=PUBLIC)
        assert b.stats("acme")["bytes"] <= bound + 65536
    finally:
        b.close()


def test_a_wal_a_reader_keeps_is_counted_so_the_next_write_is_refused(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_bytes=10**9))
        b._store("acme")._db.execute("PRAGMA busy_timeout=300")
        reader = MemoryStore(tmp_path, "svc", read_only=True, tenant="acme")
        try:
            reader._db.execute("BEGIN")
            reader.all_rows("facts")  # a snapshot from before the batch: its frames stay in the WAL
            for i in range(3000):
                _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
            b.merge("acme")
            db_only = (tmp_path / "tenants" / "acme" / "svc.sqlite3").stat().st_size
            assert _wal(b).stat().st_size > 1_000_000
            b.set_policy("acme", TenantPolicy(max_bytes=db_only + 65536))  # fits the file, not file + WAL
            with pytest.raises(QuotaExceededError) as err:
                b.add("acme", "facts", "small", source="agent", label=PUBLIC)  # the truncate is busy
            assert err.value.kind == "max_bytes" and err.value.observed > db_only + 1_000_000
        finally:
            reader.close()
    finally:
        b.close()


def test_a_number_too_large_for_a_float_is_a_bad_line_not_a_wedge(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        poison = inbox.tenant_root(tmp_path, "acme") / "inbox" / "builder.jsonl"
        poison.parent.mkdir(parents=True, exist_ok=True)
        poison.write_text('{"kind": "used", "items": [["facts", 1e400]], "at": "2026-01-01T00:00+00:00"}\n')
        _inbox_add(tmp_path, clock, "after the poison line")
        inbox.append(tmp_path, "reviewer", {"kind": "add", "table": "facts", "body": "a later inbox", "source": "agent",
                                            "at": clock(), "label": [1, []]}, "acme")
        report = b.merge("acme")
        assert report.bad_lines == 1
        assert sorted(r.body for r in b.current("acme", "facts")) == ["a later inbox", "after the poison line"]
        assert b.merge("acme").bad_lines == 0  # the offset moved past it
    finally:
        b.close()


# ---- fix-back 4 (REVIEW-MS5-D) -------------------------------------------------

def _wal_size(store):
    p = Path(str(store.path) + "-wal")
    return p.stat().st_size if p.exists() else 0


def test_a_stale_fully_checkpointed_wal_does_not_refuse_writes_far_under_the_bound(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        for i in range(8000):
            _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
        b.merge("acme")
        store = b._store("acme")
        db_only = store.path.stat().st_size
        assert _wal_size(store) > 0.8 * db_only  # the autocheckpoint copied it all; the file kept its size
        bound = int(b.stats("acme")["bytes"]) + 100_000
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        b.add("acme", "facts", "next " + "y" * 200_000, source="agent", label=PUBLIC)  # real data is ~half the bound
        assert b.stats("acme")["bytes"] <= bound and b.quota_refusals("acme") == {}
    finally:
        b.close()


def _doubled_wal_with_a_blocking_reader(tmp_path, clock):
    """All frames backfilled into the db, the WAL file kept, and a reader whose snapshot uses the WAL."""
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    b.open_tenant("acme", create=True)
    store = b._store("acme")
    store._db.execute("PRAGMA wal_autocheckpoint=0")  # setup only: hold the frames until the reader is in
    for i in range(2000):
        _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
    b.merge("acme")
    reader = MemoryStore(tmp_path, "svc", read_only=True, tenant="acme")
    reader._db.execute("BEGIN")
    reader.all_rows("facts")  # snapshot at the batch's last frame
    busy, log, done = store._db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    assert busy == 0 and log == done > 0
    store._db.execute("PRAGMA wal_autocheckpoint=1000")
    return b, store, reader


def test_a_doubled_wal_refuses_while_a_reader_holds_then_resumes_after_it_closes(tmp_path):
    clock = Clock()
    b, store, reader = _doubled_wal_with_a_blocking_reader(tmp_path, clock)
    try:
        bound = store.path.stat().st_size + 200_000  # fits the data, not data + the stale WAL
        assert b.stats("acme")["bytes"] > bound
        b.set_policy("acme", TenantPolicy(max_bytes=bound))
        started = time.monotonic()
        with pytest.raises(QuotaExceededError):
            b.add("acme", "facts", "while the reader holds", source="agent", label=PUBLIC)
        assert time.monotonic() - started < 5  # the busy truncate does not wait out the 30 s busy timeout
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000  # restored
        reader.close()
        b.add("acme", "facts", "after the reader closed", source="agent", label=PUBLIC)  # the pre-write settle
        assert b.stats("acme")["bytes"] <= bound
    finally:
        reader.close()
        b.close()


def test_a_settle_while_another_thread_holds_the_transaction_never_raises_a_sqlite_error(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        for i in range(3000):
            _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
        b.merge("acme")
        store = b._store("acme")
        assert _wal_size(store) > 0
        b.set_policy("acme", TenantPolicy(max_bytes=store.path.stat().st_size + 100_000))  # over only by the WAL
        inside, release, result = threading.Event(), threading.Event(), []

        def holder():
            with store.transaction():
                inside.set()
                release.wait(1)

        def writer():
            try:
                b.add("acme", "facts", "from another thread", source="agent", label=PUBLIC)
                result.append("landed")
            except QuotaExceededError:
                result.append("refused")
            except Exception as e:  # noqa: BLE001 - a raw sqlite error is the defect
                result.append(f"{type(e).__name__}: {e}")

        h = threading.Thread(target=holder)
        h.start()
        inside.wait(3)
        w = threading.Thread(target=writer)
        w.start()
        h.join(10)
        w.join(10)
        assert result == ["landed"], result  # it waited for the lock, truncated the stale WAL, then fit
    finally:
        b.close()


@pytest.mark.parametrize("ch", ["é", "€", "\U0001d11e"])
def test_text_bytes_counts_utf8_bytes_not_characters(ch):
    s = ch * 1000
    assert _text_bytes([s, 7, None]) == len(s.encode("utf-8")) > len(s)


class _ContendedLock:
    """The store RLock, plus a flag set when a second thread has to wait for it."""

    def __init__(self, lock):
        self._lock = lock
        self.contended = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.acquire(False):
            return True
        self.contended.set()
        if timeout < 0:
            return self._lock.acquire(blocking)
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def test_a_settle_waits_for_another_threads_transaction_instead_of_failing(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    release = threading.Event()
    threads = []
    try:
        b.open_tenant("acme", create=True)
        for i in range(3000):
            _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
        b.merge("acme")
        store = b._store("acme")
        b.set_policy("acme", TenantPolicy(max_bytes=10**9))
        inside = threading.Event()
        probe = _ContendedLock(store._lock)
        store._lock = probe
        settled = []

        def holder():
            with store.transaction():
                inside.set()
                release.wait(5)

        def settle():
            try:
                settled.append(b._settle_wal("acme", store, force=True))
            except Exception as exc:  # noqa: BLE001 - a thread failure has to show up on the assert
                settled.append(exc)

        h = threading.Thread(target=holder)
        s = threading.Thread(target=settle)
        threads.extend((h, s))
        h.start()
        assert inside.wait(3)
        s.start()
        assert probe.contended.wait(3)  # settle is blocked on the open transaction
        release.set()
        s.join(10)
        h.join(10)
        assert settled == [True]  # took the store lock, then truncated
        assert _wal_size(store) == 0
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        b.close()


class _FailingCheckpoint:
    """The store's connection, except that a WAL checkpoint raises as a locked database would."""

    def __init__(self, db):
        self._real = db

    def execute(self, sql, *args):
        if "wal_checkpoint" in sql:
            raise sqlite3.OperationalError("database table is locked")
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_failed_settle_after_commit_never_fails_the_landed_write(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        for i in range(3000):
            _inbox_add(tmp_path, clock, f"w{i}-" + "x" * 1000)
        b.merge("acme")
        store = b._store("acme")
        b.set_policy("acme", TenantPolicy(max_bytes=int(b.stats("acme")["bytes"]) - 1000))  # over only by the WAL
        store._db = _FailingCheckpoint(store._db)
        with pytest.raises(QuotaExceededError):  # the settle failed, so the WAL stays counted
            b.add("acme", "facts", "refused", source="agent", label=PUBLIC)
        with store._lock:  # as transaction() calls it, right after a COMMIT, still over the bound
            b._after_commit("acme", store, 1)  # returns quietly: the write already landed
    finally:
        store._db = store._db._real
        b.close()


# ---- fix-back 5: one backend shared by threads ---------------------------------

_THREADS_SCRIPT = r"""
import sys, threading
from datetime import UTC, datetime
from pathlib import Path
from kyra_memory import inbox
from kyra_memory.labels import Tier
from kyra_memory.policy import QuotaExceededError, TenantPolicy
from kyra_memory.sqlite_backend import SqliteBackend
root, mode = Path(sys.argv[1]), sys.argv[2]
at = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
b = SqliteBackend(root, "svc")
b.open_tenant("acme", create=True)
b.set_policy("acme", TenantPolicy(max_bytes=int(b.stats("acme")["bytes"]) + 6 * 1024 * 1024))  # some writes refused
errors, landed, merged, lock = [], [], [], threading.Lock()
def writer(k):
    for i in range(400):
        body = f"w{k}-{i}-" + "y" * 4000
        try:
            b.add("acme", "facts", body, source="agent", label=(Tier.T1, frozenset()))
            with lock:
                landed.append(body)
        except QuotaExceededError:
            pass
        except Exception as e:
            errors.append(repr(e))
def merger():
    for r in range(10):
        for i in range(200):
            inbox.append(root, f"m{r}", {"kind": "add", "table": "facts", "body": f"m{r}-{i}-" + "z" * 2000,
                                         "source": "agent", "at": at, "label": [1, []]}, "acme")
        try:
            merged.append(b.merge("acme").applied.get("add", 0))
        except QuotaExceededError as e:
            merged.append(e.report.applied.get("add", 0))  # inboxes that committed before the refusal
        except Exception as e:
            errors.append(repr(e))
def reader():
    for _ in range(3000):
        try:
            b.counts("acme")
        except Exception as e:
            errors.append(repr(e))
threads = [threading.Thread(target=writer, args=(k,)) for k in range(3)]
threads.append(threading.Thread(target=merger if mode == "merge" else reader))
for t in threads:
    t.start()
for t in threads:
    t.join()
rows = [r.body for r in b._store("acme").all_rows("facts")]
assert not errors, errors[:3]
assert len(rows) == len(set(rows)) == len(landed) + sum(merged), (len(rows), len(set(rows)), len(landed), sum(merged))
assert set(landed) <= set(rows)
print("ok")
"""


@pytest.mark.parametrize("mode", ["merge", "reader"])
def test_writers_and_a_merger_or_reader_on_one_backend_finish_without_errors_or_duplicates(tmp_path, mode):
    src = Path(__file__).resolve().parents[2] / "src"
    out = subprocess.run([sys.executable, "-c", _THREADS_SCRIPT, str(tmp_path), mode], capture_output=True, text=True,
                         timeout=120, env={**os.environ, "PYTHONPATH": str(src)})  # a deadlock fails at the timeout
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]


def test_merge_report_counts_rows_landed_across_the_retry(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True)
        settle, b._settle_wal = b._settle_wal, lambda *a, **k: False  # setup only: leave a stale, copied WAL
        for i in range(4000):
            _inbox_add(tmp_path, clock, f"seed{i}-" + "x" * 1000)
        b.merge("acme")
        b._settle_wal = settle
        b.set_policy("acme", TenantPolicy(max_bytes=int(b.stats("acme")["bytes"]) + 150 * 1024))
        forced = []
        b._settle_wal = lambda *a, **k: (forced.append(k.get("force", False)), settle(*a, **k))[1]
        for team, n in (("t1", 50), ("t2", 200)):  # t1 fits beside the stale WAL and commits; t2 is refused once
            for i in range(n):
                inbox.append(tmp_path, team, {"kind": "add", "table": "facts", "body": f"{team}-{i}-" + "x" * 1000,
                                              "source": "agent", "at": clock(), "label": [1, []]}, "acme")
        report = b.merge("acme")
        assert True in forced  # the refusal-retry path ran
        assert b.counts("acme")["facts"] == 4250
        assert report.applied == {"add": 250}
    finally:
        b.close()


_TWO_MERGERS_SCRIPT = r"""
import sys, threading
from datetime import UTC, datetime
from pathlib import Path
from kyra_memory import inbox
from kyra_memory.labels import Tier
from kyra_memory.policy import QuotaExceededError, TenantPolicy
from kyra_memory.sqlite_backend import SqliteBackend
root = Path(sys.argv[1])
at = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
b = SqliteBackend(root, "svc")
b.open_tenant("acme", create=True)
b.set_policy("acme", TenantPolicy(max_bytes=int(b.stats("acme")["bytes"]) + 6 * 1024 * 1024))
errors, landed, applied, lock = [], [], [], threading.Lock()
def writer(k):
    for i in range(400):
        body = f"w{k}-{i}-" + "y" * 4000
        try:
            b.add("acme", "facts", body, source="agent", label=(Tier.T1, frozenset()))
            with lock:
                landed.append(body)
        except QuotaExceededError:
            pass
        except Exception as e:
            errors.append(repr(e))
def feeder():
    for r in range(10):
        for i in range(200):
            inbox.append(root, f"m{r % 3}", {"kind": "add", "table": "facts", "body": f"m{r}-{i}-" + "z" * 2000,
                                             "source": "agent", "at": at, "label": [1, []]}, "acme")
        threading.Event().wait(0)  # one turn for the mergers, with no timed pause
def merger():
    for _ in range(15):
        try:
            report = b.merge("acme")
        except QuotaExceededError as e:
            report = e.report  # what committed before the refusal
        except Exception as e:
            errors.append(repr(e))
            continue
        with lock:
            applied.append(report.applied.get("add", 0))
threads = [threading.Thread(target=writer, args=(k,)) for k in range(3)]
threads += [threading.Thread(target=feeder), threading.Thread(target=merger), threading.Thread(target=merger)]
for t in threads:
    t.start()
for t in threads:
    t.join()
b.set_policy("acme", TenantPolicy())
applied.append(b.merge("acme").applied.get("add", 0))  # the rest, unbounded
rows = [r.body for r in b._store("acme").all_rows("facts")]
merged = [r for r in rows if r.startswith("m")]
assert not errors, errors[:3]
assert len(rows) == len(set(rows)), ("duplicates", len(rows) - len(set(rows)))
assert len(merged) == 2000 and sum(applied) == 2000, (len(merged), sum(applied))
assert set(landed) <= set(rows)
print("ok")
"""


def test_two_concurrent_mergers_apply_each_inbox_line_exactly_once(tmp_path):
    src = Path(__file__).resolve().parents[2] / "src"
    out = subprocess.run([sys.executable, "-c", _TWO_MERGERS_SCRIPT, str(tmp_path)], capture_output=True, text=True,
                         timeout=30, env={**os.environ, "PYTHONPATH": str(src)})
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]


def test_a_refused_merge_carries_the_report_of_the_inboxes_that_committed(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_rows=3))
        for team, n in (("a1", 2), ("a2", 5)):  # a1 commits, a2 hits max_rows and rolls back
            for i in range(n):
                inbox.append(tmp_path, team, {"kind": "add", "table": "facts", "body": f"{team}-{i}", "source": "agent",
                                              "at": clock(), "label": [1, []]}, "acme")
        with pytest.raises(QuotaExceededError) as err:
            b.merge("acme")
        assert err.value.kind == "max_rows"
        assert err.value.report.applied == {"add": 2} and b.counts("acme")["facts"] == 2
    finally:
        b.close()


# ---- MS5b: lock races on the purge and close paths (REVIEW-MS5-E note A) -------
# Each race runs in a subprocess with a timeout: without its lock the scenario hangs or crashes the process.

def _run_isolated(name: str, root: Path, timeout: float = 30) -> None:
    here = Path(__file__).resolve()
    code = (f"import sys; sys.path[:0] = [{str(here.parent)!r}, {str(here.parents[2] / 'src')!r}]; "
            f"from {here.stem} import {name}; {name}(__import__('pathlib').Path(sys.argv[1])); print('ok')")
    out = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), (out.returncode, out.stderr[-1500:])


def _marker_files(store, needle: bytes) -> list[str]:
    return [suffix or "db" for suffix in ("", "-wal")
            if Path(str(store.path) + suffix).exists() and needle in Path(str(store.path) + suffix).read_bytes()]


def _release_on(started, release):
    # The writer commits once checkpoint or vacuum has been entered, so that call can wait for it.
    if started.wait(5):
        release.set()


def _writer_opens_a_transaction(store, inside, release, blocked=None, reached=None):
    owns_probe = False
    if blocked is not None and not store._lock.acquire(blocking=False):
        # The purge still holds the lock, so this transaction cannot start until the purge drops it.
        blocked.set()
        if reached is not None:
            reached.set()
    elif blocked is not None:
        owns_probe = True
    try:
        with store.transaction() as db:
            store.add_in(db, "facts", "concurrent " + "w" * 3000, source="agent", label=PUBLIC)
            inside.set()
            if reached is not None:
                reached.set()
            release.wait(2)
    finally:
        if owns_probe:
            store._lock.release()


def _arm_writer(inside, release, blocked, reached, started):
    """Unblock the writer as soon as it has either entered or found the purge lock still held.

    Waiting out a fixed timeout here always burned the whole timeout: the purge holds the store
    lock from the delete through the checkpoint, so the writer cannot enter during that stretch.
    When the writer does get in, it stays until ``started`` so the checkpoint or vacuum waits for it.
    """
    reached.wait(3)
    if inside.is_set() and not blocked.is_set():
        threading.Thread(target=_release_on, args=(started, release), daemon=True).start()
        return
    release.set()


def _race_forget_checkpoint(root):
    b = SqliteBackend(root, "svc")
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(max_bytes=10**9))
        store = b._store("acme")
        for i in range(50):
            b.add("acme", "facts", f"filler {i} " + "f" * 500, source="agent", label=PUBLIC)
        fid = b.add("acme", "facts", "FORGETMARKER-" * 40, source="agent", label=PUBLIC)
        inside, release = threading.Event(), threading.Event()
        blocked, reached, checkpoint_started = threading.Event(), threading.Event(), threading.Event()
        t = threading.Thread(target=_writer_opens_a_transaction,
                             args=(store, inside, release, blocked, reached))
        real = b._hard_delete
        real_checkpoint = b._purge_checkpoint

        def checkpoint(*a, **k):
            checkpoint_started.set()
            return real_checkpoint(*a, **k)

        def then_a_writer_starts(*a, **k):  # a writer's transaction opens between the delete and the checkpoint
            real(*a, **k)
            t.start()
            _arm_writer(inside, release, blocked, reached, checkpoint_started)

        b._hard_delete = then_a_writer_starts
        b._purge_checkpoint = checkpoint
        assert b.forget("acme", "facts", fid) == 1  # waits for the writer, then truncates
        t.join(5)
        assert _marker_files(store, b"FORGETMARKER-FORGETMARKER") == []
        assert sum(r.body.startswith("concurrent") for r in store.all_rows("facts")) == 1
    finally:
        b.close()


def _race_expire_vacuum(root):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    b = SqliteBackend(root, "svc", clock=lambda: t0.isoformat())
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"faults": 60}))
        store = b._store("acme")
        b.add("acme", "faults", "EXPIREMARKER-" * 40, source="agent", label=PUBLIC)
        inside, release = threading.Event(), threading.Event()
        blocked, reached, vacuum_started = threading.Event(), threading.Event(), threading.Event()
        t = threading.Thread(target=_writer_opens_a_transaction,
                             args=(store, inside, release, blocked, reached))
        real = b._vacuum

        def a_writer_first(s, *rest):  # a writer's transaction is open when expire reaches its VACUUM
            t.start()
            _arm_writer(inside, release, blocked, reached, vacuum_started)
            vacuum_started.set()
            return real(s, *rest)

        b._vacuum = a_writer_first
        assert b.expire("acme", now=(t0 + timedelta(seconds=120)).isoformat()) == {"faults": 1}
        t.join(5)
        assert _marker_files(store, b"EXPIREMARKER-EXPIREMARKER") == []
        assert sum(r.body.startswith("concurrent") for r in store.all_rows("facts")) == 1
    finally:
        b.close()


def test_forget_checkpoint_racing_a_writer_transaction_still_purges(tmp_path):
    _run_isolated("_race_forget_checkpoint", tmp_path)


def test_expire_vacuum_racing_a_writer_transaction_still_purges(tmp_path):
    _run_isolated("_race_expire_vacuum", tmp_path)


class _RecordingClose:
    """The store's connection, except that close() only records when it ran; the test closes the real one."""

    def __init__(self, db):
        self._real, self.closed_at = db, None

    def close(self):
        self.closed_at = time.monotonic()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_close_waits_for_another_threads_open_transaction(tmp_path):
    # An unlocked close() on a connection another thread is using crashes the process (REVIEW-MS5-E M16);
    # this checks the ordering instead, so a missing lock fails an assert rather than segfaulting.
    store = MemoryStore(tmp_path, "svc", create=True, tenant="acme")
    real = store._db
    recorder = store._db = _RecordingClose(real)
    inside, release = threading.Event(), threading.Event()
    released = []
    probe = _ContendedLock(store._lock)
    store._lock = probe
    threads = []

    def holder():
        with store.transaction():
            inside.set()
            release.wait(5)
            released.append(time.monotonic())

    t = threading.Thread(target=holder)
    threads.append(t)
    t.start()
    try:
        assert inside.wait(3)
        closer = threading.Thread(target=store.close)
        threads.append(closer)
        closer.start()
        assert probe.contended.wait(3)  # close is blocked on the open transaction
        release.set()
        closer.join(5)
        t.join(5)
        assert released and recorder.closed_at is not None and recorder.closed_at >= released[0]
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
        real.close()


@pytest.mark.parametrize("purge", ["forget", "expire"])
def test_a_purge_a_reader_blocks_fails_fast_and_finishes_on_the_next_write(tmp_path, purge):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"faults": 60.0}))
        store = b._store("acme")
        fid = b.add("acme", "faults", "HELDMARKER-" * 40, source="agent", label=PUBLIC)
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the marker is in the db file itself
        reader = MemoryStore(tmp_path, "svc", read_only=True, tenant="acme")
        try:
            reader._db.execute("BEGIN")
            reader.all_rows("facts")  # a snapshot the purge's checkpoint cannot pass
            waited = []
            checkpoint_started = threading.Event()
            real_checkpoint = b._purge_checkpoint

            def checkpoint(*a, **k):
                checkpoint_started.set()
                return real_checkpoint(*a, **k)

            b._purge_checkpoint = checkpoint

            def writer():
                if not checkpoint_started.wait(5):
                    return
                started_write = time.monotonic()
                b.add("acme", "facts", "during the purge", source="agent", label=PUBLIC)
                waited.append(time.monotonic() - started_write)

            t = threading.Thread(target=writer)
            t.start()
            started = time.monotonic()
            clock.advance(hours=1)
            with pytest.raises(RuntimeError):
                b.forget("acme", "faults", fid) if purge == "forget" else b.expire("acme")
            assert time.monotonic() - started < 5  # not the 30 s busy timeout
            t.join(10)
            assert waited and waited[0] < 5
            assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
            assert store._purge_pending
        finally:
            reader.close()
        b.add("acme", "facts", "the next write", source="agent", label=PUBLIC)  # its settle finishes the purge
        assert not store._purge_pending
        assert _marker_files(store, b"HELDMARKER-HELDMARKER") == []
        assert b.counts("acme")["faults"] == 0
    finally:
        b.close()


# ---- MS5b fix-back: a pending purge survives a close or a crash ------------------

PURGE_MARK = b"ZQXMARKER7731"


def _marker_on_disk(path) -> list[str]:
    return [suffix or "db" for suffix in ("", "-wal", "-shm")
            if Path(str(path) + suffix).exists() and PURGE_MARK in Path(str(path) + suffix).read_bytes()]


def _marked_backend(root, clock):
    b = SqliteBackend(root, "svc", clock=clock)
    b.open_tenant("acme", create=True)
    for i in range(20):
        b.add("acme", "facts", f"filler {i} " + "f" * 500, source="agent", label=PUBLIC)
    rid = b.add("acme", "facts", "secret " + PURGE_MARK.decode() + " body", source="agent", label=PUBLIC)
    b._store("acme")._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the marker is in the db file
    return b, rid


def _held_reader(path, ro):
    r = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if ro else sqlite3.connect(str(path))
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM facts").fetchone()
    return r


@pytest.mark.parametrize("ro", [False, True])
def test_a_pending_purge_survives_a_close_and_finishes_on_reopen(tmp_path, ro):
    clock = Clock()
    b, rid = _marked_backend(tmp_path, clock)
    path = b._store("acme").path
    reader = _held_reader(path, ro)
    with pytest.raises(RuntimeError, match="gone from queries"):
        b.forget("acme", "facts", rid)
    b.close()  # the reader is still open, so this close cannot checkpoint
    reader.close()  # a read-only reader cannot checkpoint either
    b2 = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b2.open_tenant("acme")  # the saved pending flag: the purge finishes here
        assert _marker_on_disk(path) == [] and not b2._store("acme")._purge_pending
        assert b2._store("acme").get_meta("purge_pending") is None
        assert b2.history("acme", "facts", rid) == []
        assert b2.forget("acme", "facts", rid) == 1  # this reopened instance finished it
    finally:
        b2.close()


_CRASH_WHILE_PENDING = r"""
import os, sys
from kyra_memory.sqlite_backend import SqliteBackend
b = SqliteBackend(sys.argv[1], "svc")
b.open_tenant("acme")
try:
    b.forget("acme", "facts", int(sys.argv[2]))
    print("forget finished")
except RuntimeError:
    print("pending")
sys.stdout.flush()
os._exit(0)  # no close, no checkpoint
"""


@pytest.mark.parametrize("ro", [False, True])
def test_a_pending_purge_survives_a_process_crash_and_finishes_on_restart(tmp_path, ro):
    clock = Clock()
    b, rid = _marked_backend(tmp_path, clock)
    path = b._store("acme").path
    b.close()
    reader = _held_reader(path, ro)  # in this process; the crashing backend runs in another one
    try:
        src = Path(__file__).resolve().parents[2] / "src"
        out = subprocess.run([sys.executable, "-c", _CRASH_WHILE_PENDING, str(tmp_path), str(rid)], capture_output=True,
                             text=True, timeout=30, env={**os.environ, "PYTHONPATH": str(src)})
        assert out.stdout.strip() == "pending", out.stderr[-1500:]
    finally:
        reader.close()
    b2 = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b2.open_tenant("acme")
        assert b2.history("acme", "facts", rid) == []
        assert _marker_on_disk(path) == [] and not b2._store("acme")._purge_pending
    finally:
        b2.close()


def test_a_quota_refused_write_still_finishes_a_pending_purge(tmp_path):
    clock = Clock()
    b, rid = _marked_backend(tmp_path, clock)
    try:
        store = b._store("acme")
        reader = _held_reader(store.path, ro=False)
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)
        reader.close()
        b.set_policy("acme", TenantPolicy(max_rows=1))
        with pytest.raises(QuotaExceededError):
            b.add("acme", "facts", "refused", source="agent", label=PUBLIC)
        assert _marker_on_disk(store.path) == [] and not store._purge_pending
    finally:
        b.close()


def test_a_retried_forget_reports_the_finished_purge_and_the_audit_logs_it(tmp_path):
    from kyra_memory.audit import AuditedBackend, TenantLabel
    clock = Clock()
    inner, rid = _marked_backend(tmp_path, clock)
    b = AuditedBackend(inner, tmp_path / "audit", {"acme": TenantLabel(Tier.T1)})
    try:
        reader = _held_reader(inner._store("acme").path, ro=True)
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)
        reader.close()
        assert b.forget("acme", "facts", rid) == 1  # the retry finishes the purge: its real outcome, not 0
        assert b.forget("acme", "facts", rid) == 0  # nothing left
        rows = [json.loads(line) for line in b.audit_path("acme").read_text().splitlines()]
        assert [(r["action"], r["result"]) for r in rows if r["action"] in ("forget", "purge_completed")] == [
            ("forget", "error:RuntimeError"), ("forget", "ok"), ("purge_completed", "ok"), ("forget", "ok")]
        done = [r for r in rows if r["action"] == "purge_completed"][0]
        assert (done["table"], done["row_id"]) == ("facts", rid)
        assert _marker_on_disk(inner._store("acme").path) == []
    finally:
        b.close()


# ---- F1 (REVIEW-MS5B): a purged id is never handed out again ----------------------

def test_retry_never_deletes_a_row_added_after_the_blocked_forget(tmp_path):
    b = SqliteBackend(tmp_path, "svc", clock=Clock())
    try:
        b.open_tenant("acme", create=True)
        for i in range(10):
            b.add("acme", "facts", f"filler {i} " + "f" * 300, source="agent", label=PUBLIC)
        rid = b.add("acme", "facts", "the row to forget", source="agent", label=PUBLIC)  # the highest id
        store = b._store("acme")
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store._db.execute("PRAGMA busy_timeout=200")  # keeps the blocked checkpoint short on any code
        reader = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM facts").fetchone()
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)  # the rows are gone; the WAL truncate is blocked
        reader.close()
        new = b.add("acme", "facts", "a new row the caller wants kept", source="agent", label=PUBLIC)
        assert new > rid
        b.forget("acme", "facts", rid)  # the caller's retry of the blocked forget
        assert "a new row the caller wants kept" in [r.body for r in b.current("acme", "facts")]
    finally:
        b.close()


def test_an_expired_proposal_id_is_never_reused(tmp_path):
    clock = Clock()
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b.open_tenant("acme", create=True, policy=TenantPolicy(ttl_seconds={"facts": 60.0}))
        for body in ("p1", "p2"):
            inbox.append(tmp_path, "builder", {"kind": "add", "table": "facts", "body": body, "source": "agent",
                                               "status": "proposed", "at": clock(), "label": [1, []]}, "acme")
        b.merge("acme")
        old = max(p["id"] for p in b._store("acme").proposals())
        clock.advance(hours=1)
        b.expire("acme")
        inbox.append(tmp_path, "builder", {"kind": "add", "table": "facts", "body": "p3", "source": "agent",
                                           "status": "proposed", "at": clock(), "label": [1, []]}, "acme")
        b.merge("acme")
        assert [p["id"] for p in b._store("acme").proposals()] == [old + 1]
    finally:
        b.close()


# ---- REVIEW-MS5B probes (F2, F3; M4, M10, M14 mutation killers) ---------------------

F_MARK = "QQPROBEMARK5591"


def _f_on_disk(path, mark=F_MARK) -> list[str]:
    return [s or "db" for s in ("", "-wal", "-shm")
            if Path(str(path) + s).exists() and mark.encode() in Path(str(path) + s).read_bytes()]


def _f_reader(path, ro=True):
    r = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if ro else sqlite3.connect(str(path))
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM facts").fetchone()
    return r


def _f_seeded(root, clock, mark=F_MARK):
    b = SqliteBackend(root, "svc", clock=clock)
    b.open_tenant("acme", create=True)
    for i in range(10):
        b.add("acme", "facts", f"filler {i} " + "f" * 300, source="agent", label=PUBLIC)
    rid = b.add("acme", "facts", f"secret {mark} body", source="agent", label=PUBLIC)
    b._store("acme")._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return b, rid


@pytest.mark.parametrize("op", ["forget", "expire"])
def test_f_blocked_purge_raises_well_under_a_second_with_no_content(tmp_path, op):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    try:
        b.set_policy("acme", TenantPolicy(ttl_seconds={"facts": 60.0}))
        r = _f_reader(b._store("acme").path)
        clock.advance(hours=1)
        t = time.monotonic()
        with pytest.raises(RuntimeError) as e:
            b.forget("acme", "facts", rid) if op == "forget" else b.expire("acme")
        took = time.monotonic() - t
        r.close()
        assert took < 1.0, took
        assert F_MARK not in str(e.value) and "secret" not in str(e.value)
        meta = b._store("acme").get_meta(PURGE_KEY)  # expire saves its pending entry too (M14)
        assert meta and F_MARK not in meta and "secret" not in meta
    finally:
        b.close()


def test_retry_after_an_intervening_write_reports_the_finished_count(tmp_path):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    try:
        r = _f_reader(b._store("acme").path)
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)
        r.close()
        b.add("acme", "rules", "unrelated write", source="agent", label=PUBLIC)  # finishes the purge
        assert b.forget("acme", "facts", rid) == 1
    finally:
        b.close()


def test_size_read_before_the_lock_never_clears_a_newer_blocked_purge(tmp_path):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    store = b._store("acme")
    try:
        rid2 = b.add("acme", "facts", "second SECONDMARK8813 body", source="agent", label=PUBLIC)
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        r = _f_reader(store.path)
        with pytest.raises(RuntimeError):
            b.forget("acme", "facts", rid)
        r.close()
        store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # e.g. another process's settle: WAL now 0 bytes
        assert store._purge_pending
        real = b._finish_purge
        held = []

        def another_thread_forgets_first(s, own=()):
            # this thread read size == 0 outside the lock; before it takes the lock, a forget runs and is blocked
            if not held:
                held.append(_f_reader(store.path))
                with pytest.raises(RuntimeError):
                    b.forget("acme", "facts", rid2)
            return real(s, own)

        b._finish_purge = another_thread_forgets_first
        b._settle_wal("acme", store)
        b._finish_purge = real
        on_disk = _f_on_disk(store.path, "SECONDMARK8813")
        held[0].close()
        assert not on_disk or store._purge_pending, ("pending cleared while text is on disk", on_disk)
        done = b.completed_purges("acme")
        assert all(e.get("row_id") != rid2 for e in done) or not on_disk, done
    finally:
        b.close()


def test_a_clean_forget_logs_no_purge_completed_row(tmp_path):
    clock = Clock()
    inner, rid = _f_seeded(tmp_path, clock)
    b = AuditedBackend(inner, tmp_path / "audit", {"acme": TenantLabel(Tier.T1)})
    try:
        assert b.forget("acme", "facts", rid) == 1
        rows = [json.loads(x) for x in b.audit_path("acme").read_text().splitlines()]
        assert [r["action"] for r in rows if r["action"] in ("forget", "purge_completed")] == ["forget"], rows
    finally:
        b.close()


_RAW_DELETE_THEN_CRASH = r"""
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA secure_delete=ON")
db.execute("DELETE FROM facts WHERE id = ?", (int(sys.argv[2]),))
print("deleted", flush=True)
os._exit(0)
"""


def test_a_non_empty_wal_without_meta_is_purged_on_open(tmp_path):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    path = b._store("acme").path
    b.close()
    r = _f_reader(path)  # a read-only snapshot: the child's commit stays in the -wal
    out = subprocess.run([sys.executable, "-I", "-c", _RAW_DELETE_THEN_CRASH, str(path), str(rid)],
                         capture_output=True, text=True, timeout=60, env=dict(os.environ))
    r.close()
    assert "deleted" in out.stdout, out.stderr[-500:]
    assert "db" in _f_on_disk(path) and Path(str(path) + "-wal").stat().st_size > 0
    b2 = SqliteBackend(tmp_path, "svc", clock=clock)
    try:
        b2.open_tenant("acme")
        assert _f_on_disk(path) == []
    finally:
        b2.close()


def test_an_expire_whose_vacuum_fails_is_finished_by_the_next_write(tmp_path):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    store = b._store("acme")
    try:
        b.set_policy("acme", TenantPolicy(ttl_seconds={"facts": 60.0}))
        clock.advance(hours=1)

        def locked(*_a):  # VACUUM past busy_timeout while another connection holds the write lock
            raise sqlite3.OperationalError("database is locked")

        b._vacuum = locked
        with pytest.raises(sqlite3.OperationalError):
            b.expire("acme")
        del b._vacuum
        assert store._purge_pending and store.get_meta(PURGE_KEY)
        b.add("acme", "rules", "the next write", source="agent", label=PUBLIC)
        assert not store._purge_pending and store.get_meta(PURGE_KEY) is None
        assert _f_on_disk(store.path) == []
    finally:
        b.close()


# ---- REVIEW-MS5B-2: cross-instance metadata and purge lock boundaries ----

@pytest.mark.parametrize("op", ["forget", "expire"])
def test_purge_holds_the_lock_from_delete_through_checkpoint(tmp_path, op):
    clock = Clock()
    b, rid = _f_seeded(tmp_path, clock)
    try:
        store = b._store("acme")
        b.set_policy("acme", TenantPolicy(ttl_seconds={"facts": 60.0}))
        clock.advance(hours=1)
        hook = "_purge_checkpoint" if op == "forget" else "_vacuum"
        real = getattr(b, hook)
        acquired = []

        def contender():
            got = store._lock.acquire(blocking=False)
            acquired.append(got)
            if got:
                store._lock.release()

        def at_checkpoint(*args, **kwargs):
            t = threading.Thread(target=contender)
            t.start()
            t.join(5)
            assert not t.is_alive()
            return real(*args, **kwargs)

        # Check the boundary immediately after the delete, before the caller can reacquire a lock.
        if op == "forget":
            delete = b._hard_delete

            def after_delete(*args, **kwargs):
                delete(*args, **kwargs)
                t = threading.Thread(target=contender)
                t.start()
                t.join(5)
                assert not t.is_alive()

            b._hard_delete = after_delete
        setattr(b, hook, at_checkpoint)
        if op == "forget":
            assert b.forget("acme", "facts", rid) == 1
        else:
            assert b.expire("acme")["facts"] > 0
        assert acquired and not any(acquired)
        assert b.completed_purges("acme") == []
        assert not b._finished_forgets.get("acme")
        assert _f_on_disk(store.path) == []
        # A real write after the purge remains available and cannot create a delayed completion.
        b.add("acme", "rules", "after purge", source="agent", label=PUBLIC)
        assert b.completed_purges("acme") == []
    finally:
        b.close()


def test_hwm_rises_on_each_higher_purge(tmp_path):
    b = SqliteBackend(tmp_path, "svc", clock=Clock())
    try:
        b.open_tenant("acme", create=True)
        for i in range(3):
            b.add("acme", "facts", f"row {i}", source="agent", label=PUBLIC)
        b.forget("acme", "facts", 3)
        four = b.add("acme", "facts", "row four", source="agent", label=PUBLIC)
        assert four == 4
        b.forget("acme", "facts", four)
        assert b.add("acme", "facts", "row five", source="agent", label=PUBLIC) == 5
    finally:
        b.close()


def test_a_second_instance_purge_after_a_clean_truncate_is_never_cleared(tmp_path):
    clock = Clock()
    a, rid = _f_seeded(tmp_path, clock)
    rid2 = a.add("acme", "facts", "second SECONDMARK7731 body", source="agent", label=PUBLIC)
    store = a._store("acme")
    store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    b = SqliteBackend(tmp_path, "svc", clock=clock)
    b.open_tenant("acme")
    held = []
    try:
        r = _f_reader(store.path)
        with pytest.raises(RuntimeError):
            a.forget("acme", "facts", rid)
        r.close()
        assert store._purge_pending
        real = SqliteBackend._checkpoint_now

        def then_b_forgets(db):
            out = real(db)
            if not held:  # right after A's clean truncate, B's forget commits and a new reader blocks B's
                held.append(_f_reader(store.path))
                with pytest.raises(RuntimeError):
                    b.forget("acme", "facts", rid2)
            return out

        a._checkpoint_now = then_b_forgets
        a._settle_wal("acme", store)
        del a._checkpoint_now
        on_disk = _f_on_disk(store.path, "SECONDMARK7731")
        meta = store.get_meta(PURGE_KEY) or ""
        done = a.completed_purges("acme")
        held[0].close()
        assert on_disk, "setup: B's purge should still be blocked"
        problems = []
        if str(rid2) not in meta:
            problems.append(("B's pending entry cleared while its text is on disk", on_disk, meta))
        if any(e.get("row_id") == rid2 for e in done):
            problems.append(("A audits B's purge as completed while its text is on disk", done))
        assert not problems, problems
        assert store._purge_pending
        a._settle_wal("acme", store)
        assert store.get_meta(PURGE_KEY) is None and not store._purge_pending
        assert _f_on_disk(store.path, "SECONDMARK7731") == []
        assert {e["row_id"] for e in a.completed_purges("acme")} == {rid, rid2}
    finally:
        for h in held:
            h.close()
        b.close()
        a.close()
