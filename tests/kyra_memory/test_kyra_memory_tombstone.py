"""Deletion stays effective for late writers; only explicit creation reopens a tenant."""
import json
from contextlib import ExitStack

import pytest

from kyra_memory import inbox, sqlite_backend
from kyra_memory.audit import AuditedBackend, TenantLabel
from kyra_memory.labels import Tier
from kyra_memory.sqlite_backend import SqliteBackend

PUBLIC = (Tier.T0, frozenset())


@pytest.fixture
def backends(tmp_path):
    with ExitStack() as stack:
        first = SqliteBackend(tmp_path, "svc")
        second = SqliteBackend(tmp_path, "svc")
        stack.callback(first.close)
        stack.callback(second.close)
        first.open_tenant("acme", create=True)
        first.add("acme", "facts", "synthetic original body", source="agent", label=PUBLIC)
        second.open_tenant("acme")
        yield first, second


@pytest.mark.parametrize("operation", [
    "add", "merge", "accept", "weights", "outcome", "forget", "expire", "store_add", "prune", "relabel", "hot",
])
def test_late_writes_on_second_open_backend_refused(tmp_path, backends, operation):
    first, second = backends
    store = second._store("acme")
    writes = {
        "add": lambda: second.add("acme", "facts", "synthetic late body", source="agent", label=PUBLIC),
        "merge": lambda: second.merge("acme"),
        "accept": lambda: second.accept("acme", 1),
        "weights": lambda: second.set_weights("acme", {1: 0.5}),
        "outcome": lambda: second.record_outcome("acme", 1, win=True),
        "forget": lambda: second.forget("acme", "facts", 1),
        "expire": lambda: second.expire("acme"),
        "store_add": lambda: store.add("facts", "synthetic late body", source="agent", label=PUBLIC),
        "prune": lambda: store.prune(),
        "relabel": lambda: store.relabel("facts", "agent", PUBLIC),
        "hot": lambda: inbox.refresh_hot(store, tmp_path, teams=["builders"]),
    }
    first.delete_tenant("acme")
    with pytest.raises(FileNotFoundError):
        writes[operation]()
    assert not (tmp_path / "tenants" / "acme").exists()


def test_late_inbox_append_and_open_refused(tmp_path, backends):
    first, second = backends
    first.delete_tenant("acme")
    with pytest.raises(FileNotFoundError):
        inbox.append_add(tmp_path, "builders", "facts", "synthetic late body", source="agent", tenant="acme")
    for backend in (first, second):
        with pytest.raises(FileNotFoundError):
            backend.open_tenant("acme")
    assert not (tmp_path / "tenants" / "acme").exists()
    marker = json.loads((tmp_path / "tenants" / ".deleted" / "acme").read_text())
    assert set(marker) == {"tenant", "time"}
    assert marker["tenant"] == "acme"
    assert marker["time"]


def test_explicit_recreate_uses_fresh_store_and_keeps_audit(tmp_path, backends):
    first, second = backends
    stale = second._store("acme")
    audited = AuditedBackend(first, tmp_path / "audit", {"acme": TenantLabel(Tier.T0)})
    audited.delete_tenant("acme")
    audit = audited.audit_path("acme").read_bytes()
    assert b'delete_tenant' in audit
    second.open_tenant("acme", create=True)
    assert not (tmp_path / "tenants" / ".deleted" / "acme").exists()
    with pytest.raises(FileNotFoundError):
        stale.add("facts", "stale body", source="agent", label=PUBLIC)
    second.add("acme", "facts", "fresh body", source="agent", label=PUBLIC)
    inbox.append_add(tmp_path, "builders", "facts", "fresh inbox", source="agent", tenant="acme", label=PUBLIC)
    second.merge("acme")
    first.open_tenant("acme")
    assert [r.body for r in first.current("acme", "facts")] == ["fresh body", "fresh inbox"]
    assert audited.audit_path("acme").read_bytes() == audit


def test_recreate_finishes_interrupted_delete(tmp_path, backends, monkeypatch):
    first, second = backends
    folder = tmp_path / "tenants" / "acme"
    marker = tmp_path / "tenants" / ".deleted" / "acme"
    inbox.append_add(tmp_path, "builders", "facts", "synthetic original inbox", source="agent", tenant="acme")

    def interrupted_erase(path):
        raise OSError("simulated interrupted erase")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_backend.shutil, "rmtree", interrupted_erase)
        with pytest.raises(OSError, match="simulated interrupted erase"):
            first.delete_tenant("acme")
        assert marker.exists() and folder.exists()
        with pytest.raises(OSError, match="simulated interrupted erase"):
            second.open_tenant("acme", create=True)
        assert marker.exists()

    second.open_tenant("acme", create=True)
    assert not marker.exists()
    assert second.current("acme", "facts") == []
    assert not inbox.inbox_path(tmp_path, "builders", "acme").exists()
    for path in folder.rglob("*"):
        if path.is_file():
            assert b"synthetic original" not in path.read_bytes()
    second.add("acme", "facts", "fresh body", source="agent", label=PUBLIC)
    assert [r.body for r in second.current("acme", "facts")] == ["fresh body"]


@pytest.mark.parametrize("during_write", [False, True])
def test_inbox_append_racing_delete_leaves_no_file(tmp_path, backends, monkeypatch, during_write):
    first, _ = backends
    path = inbox.inbox_path(tmp_path, "builders", "acme")
    inbox.append_add(tmp_path, "builders", "facts", "valid inbox", source="agent", tenant="acme")
    assert path.exists()

    if during_write:
        original_write = inbox.os.write

        def interrupted_erase(folder):
            raise OSError("simulated interrupted erase")

        def racing_write(fd, line):
            result = original_write(fd, line)
            with pytest.raises(OSError, match="simulated interrupted erase"):
                first.delete_tenant("acme")
            return result

        monkeypatch.setattr(sqlite_backend.shutil, "rmtree", interrupted_erase)
        monkeypatch.setattr(inbox.os, "write", racing_write)
    else:
        original_check = inbox.check_not_deleted

        def racing_check(root, tenant):
            original_check(root, tenant)
            first.delete_tenant(tenant)

        monkeypatch.setattr(inbox, "check_not_deleted", racing_check)

    with pytest.raises(FileNotFoundError):
        inbox.append_add(tmp_path, "builders", "facts", "synthetic late body", source="agent", tenant="acme")
    assert not path.exists()
    if not during_write:
        assert not path.parent.parent.exists()


def test_inbox_append_requires_existing_tenant_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        inbox.append_add(tmp_path, "builders", "facts", "synthetic body", source="agent", tenant="acme")
    folder = tmp_path / "tenants" / "acme"
    assert not folder.exists()
    folder.mkdir(parents=True)
    inbox.append_add(tmp_path, "builders", "facts", "synthetic body", source="agent", tenant="acme")
    assert b"synthetic body" in inbox.inbox_path(tmp_path, "builders", "acme").read_bytes()
