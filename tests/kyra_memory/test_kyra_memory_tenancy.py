"""Tenant isolation: one tenant's rules, facts and inbox line are invisible to every read API of another tenant."""
import shutil
import sqlite3

import pytest

from kyra_memory import inbox, vector_index
from kyra_memory.assembler import assemble_pack
from kyra_memory.labels import Tier
from kyra_memory.store import MemoryStore, ScopeError
from kyra_memory.tenancy import TenantError

PUBLIC = (Tier.T1, frozenset())
CANARY = "zebra-quartz-ledger"


def fake_encoder(texts, name, *, query=False):
    return [[float(len(t)), 1.0] for t in texts]


@pytest.fixture
def two_tenants(tmp_path):
    a = MemoryStore(tmp_path, "svc", create=True, tenant="acme")
    b = MemoryStore(tmp_path, "svc", create=True, tenant="globex")
    a.add("rules", f"Acme rule {CANARY}", source="owner", label=PUBLIC)
    a.add("facts", f"Acme fact {CANARY}", source="agent", label=PUBLIC)
    b.add("facts", "Globex fact about invoices", source="agent", label=PUBLIC)
    inbox.append_add(tmp_path, "builders", "facts", f"Acme inbox {CANARY}", source="agent", label=PUBLIC,
                     tenant="acme")
    inbox.merge(a, tmp_path)
    inbox.merge(b, tmp_path)
    yield tmp_path, a, b
    a.close()
    b.close()


def test_layout_none_keeps_the_flat_file_and_tenant_gets_its_own(tmp_path):
    MemoryStore(tmp_path, "svc", create=True).close()
    s = MemoryStore(tmp_path, "svc", create=True, tenant="acme")
    assert (tmp_path / "svc.sqlite3").exists()
    assert s.path == tmp_path / "tenants" / "acme" / "svc.sqlite3"
    assert s.get_meta("tenant") == "acme"
    s.close()


def test_merge_reads_only_the_tenants_inbox(two_tenants):
    _, a, b = two_tenants
    assert any(CANARY in r.body for r in a.current("facts"))
    assert b.search("facts", CANARY) == []


def test_every_read_api_on_b_returns_none_of_a(two_tenants):
    root, a, b = two_tenants
    rule_id = a.current("rules")[0].id
    fact_id = a.current("facts")[0].id
    for table in ("rules", "facts"):
        assert not any(CANARY in r.body for r in b.current(table))
        assert b.search(table, CANARY) == []
        assert not any(CANARY in r.body for r in b.as_of(table, "2999-01-01T00:00:00+00:00"))
    assert b.get("rules", rule_id) is None
    assert all(CANARY not in r.body for r in b.history("facts", fact_id))
    inbox.append(root, "builders", {"kind": "used", "items": [["facts", fact_id]]}, "acme")
    inbox.merge(a, root)
    assert b.hot_set("builders") == []
    pack = assemble_pack(root, "builders", "acme rule fact inbox", record=False, scope="svc", tenant="globex")
    assert CANARY not in pack.text and "Globex" in pack.text


def test_vector_index_is_per_tenant(two_tenants):
    root, _, _ = two_tenants
    vector_index.reindex(root, "m", encoder=fake_encoder, scope="svc", tenant="acme")
    vector_index.reindex(root, "m", encoder=fake_encoder, scope="svc", tenant="globex")
    b_vectors = vector_index.IndexedEmbedding(root, "m", encoder=fake_encoder, tenant="globex").vectors
    a_hashes = {vector_index.digest(f"Acme fact {CANARY}")}
    assert not a_hashes & set(b_vectors)
    assert len(b_vectors) == 1
    with pytest.raises(sqlite3.Error):
        vector_index.IndexedEmbedding(root, "m", encoder=fake_encoder, tenant="initech")


def test_a_file_naming_another_tenant_is_refused(tmp_path):
    MemoryStore(tmp_path, "svc", create=True, tenant="acme").close()  # close checkpoints the WAL into the file
    (tmp_path / "tenants" / "globex").mkdir()
    shutil.copy(tmp_path / "tenants" / "acme" / "svc.sqlite3", tmp_path / "tenants" / "globex" / "svc.sqlite3")
    root = tmp_path
    with pytest.raises(ScopeError):
        MemoryStore(root, "svc", tenant="globex", read_only=True)
    with pytest.raises(ScopeError):  # a file that names a tenant never opens as the tenant-less layout
        MemoryStore(root / "tenants" / "globex", "svc", read_only=True)


@pytest.mark.parametrize("bad", ["", "../acme", "a/b", "a.b", ".", "..", "Acme", "a\\b"])
def test_bad_tenant_id_is_refused_before_any_path_exists(tmp_path, bad):
    with pytest.raises(TenantError):
        MemoryStore(tmp_path, "svc", create=True, tenant=bad)
    with pytest.raises(TenantError):
        inbox.append(tmp_path, "builders", {"kind": "used", "items": []}, bad)
    assert list(tmp_path.iterdir()) == []


def test_symlinked_tenant_dir_refused_for_writes(tmp_path):
    import os
    a = MemoryStore(tmp_path, "svc", create=True, tenant="acme")
    os.symlink(tmp_path / "tenants" / "acme", tmp_path / "tenants" / "globex")
    with pytest.raises(TenantError):
        inbox.append_add(tmp_path, "builders", "facts", "written as globex", source="agent", label=PUBLIC,
                         tenant="globex")
    inbox.merge(a, tmp_path)
    assert a.current("facts") == []
    a.close()


def test_symlinked_tenants_folder_refused(tmp_path):
    import os
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", tmp_path / "tenants")
    with pytest.raises(TenantError):
        MemoryStore(tmp_path, "svc", create=True, tenant="acme")
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_kyra_root_inbox_never_merges_tenant_lines(tmp_path):
    k = MemoryStore(tmp_path, "kyra", create=True)
    a = MemoryStore(tmp_path, "kyra", create=True, tenant="acme")
    inbox.append_add(tmp_path, "builders", "facts", f"acme {CANARY}", source="agent", label=PUBLIC, tenant="acme")
    inbox.append_add(tmp_path, "builders", "facts", "kyra own fact", source="agent", label=PUBLIC)
    inbox.merge(k, tmp_path)
    inbox.merge(a, tmp_path)
    assert [r.body for r in k.current("facts")] == ["kyra own fact"]
    assert [r.body for r in a.current("facts")] == [f"acme {CANARY}"]
    k.close()
    a.close()
