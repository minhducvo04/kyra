"""Audit rows per tenant and the privacy label gate, over the sqlite backend."""
import json

import pytest

from kyra_memory.audit import AuditedBackend, PrivacyRefused, TenantLabel
from kyra_memory.labels import PrivacyClass, Tier
from kyra_memory.sqlite_backend import SqliteBackend

MARK = "zebra-quartz-ledger"
PUBLIC = (Tier.T1, frozenset())
HEALTH = (Tier.T2, frozenset({PrivacyClass.health}))


def rows(b, tenant):
    return [json.loads(x) for x in b.audit_path(tenant).read_text().splitlines()]


@pytest.fixture
def b(tmp_path):
    labels = {"acme": TenantLabel(Tier.T1), "globex": TenantLabel(Tier.T2, frozenset({PrivacyClass.health}))}
    a = AuditedBackend(SqliteBackend(tmp_path / "db", "svc"), tmp_path / "audit", labels, actor="tester")
    for t in labels:
        a.open_tenant(t, create=True)
    yield a
    a.close()


def test_every_call_is_audited_per_tenant_without_body(b, tmp_path):
    rid = b.add("acme", "facts", f"fact {MARK}", source="agent", label=PUBLIC)
    b.search("acme", "facts", MARK)
    b.forget("acme", "facts", rid)
    b.add("globex", "facts", "other", source="agent", label=PUBLIC)
    got = rows(b, "acme")
    assert [r["action"] for r in got] == ["open_tenant", "add", "search", "forget"]
    assert {r["tenant"] for r in got} == {"acme"} and got[0]["actor"] == "tester"
    assert got[3]["row_id"] == rid and all(r["result"] == "ok" for r in got)
    assert len(rows(b, "globex")) == 2
    assert MARK not in "".join(p.read_text() for p in (tmp_path / "audit").iterdir())


def test_read_above_label_is_refused_and_audited(b):
    b.add("acme", "facts", "ok row", source="agent", label=PUBLIC)
    b.add("acme", "facts", "sick note", source="agent", key="h", label=HEALTH)
    with pytest.raises(PrivacyRefused):
        b.current("acme", "facts")
    assert rows(b, "acme")[-1]["result"] == "refused"
    b.add("globex", "facts", "sick note", source="agent", label=HEALTH)
    assert [r.body for r in b.current("globex", "facts")] == ["sick note"]


def test_audit_path_refuses_missing_or_bad_tenant(tmp_path):
    import pytest

    from kyra_memory.audit import AuditedBackend
    from kyra_memory.tenancy import TenantError
    b = AuditedBackend.__new__(AuditedBackend)
    b.audit_dir = tmp_path
    for bad in (None, "../x", "a/b", ""):
        with pytest.raises(TenantError):
            b.audit_path(bad)
    assert not (tmp_path / "None.jsonl").exists()
    assert list(tmp_path.iterdir()) == []
