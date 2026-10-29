"""A restricted scope (health, busy, family, secrets) opens only with an explicit allow_restricted=True."""
import pytest

from kyra_memory.assembler import assemble_pack
from kyra_memory.store import RESTRICTED_SCOPES, MemoryStore, ScopeError
from kyra_memory.vector_index import reindex

SCOPES = sorted(RESTRICTED_SCOPES)


def _make(root, scope):
    s = MemoryStore(root, scope, create=True, allow_restricted=True)
    s.add("facts", "synthetic restricted fact about the deploy", source="agent", topic="deploy", label=(2, ["health"]))
    s.close()


@pytest.mark.parametrize("scope", SCOPES)
def test_pack_builder_refuses_a_restricted_scope(tmp_path, scope):
    _make(tmp_path, scope)
    with pytest.raises(ScopeError):
        assemble_pack(tmp_path, "builders", "deploy", record=False, scope=scope)


@pytest.mark.parametrize("scope", SCOPES)
def test_reindex_refuses_a_restricted_scope(tmp_path, scope):
    _make(tmp_path, scope)
    with pytest.raises(ScopeError):
        reindex(tmp_path, "m", encoder=lambda texts, name: [[1.0]] * len(texts), scope=scope)
    assert not (tmp_path / "vectors.sqlite3").exists()


@pytest.mark.parametrize("scope", SCOPES)
def test_direct_open_refuses_a_restricted_scope(tmp_path, scope):
    _make(tmp_path, scope)
    for kwargs in ({}, {"read_only": True}, {"create": True}):
        with pytest.raises(ScopeError):
            MemoryStore(tmp_path, scope, **kwargs)


def test_cached_snapshot_does_not_leak_to_a_caller_without_the_flag(tmp_path):
    _make(tmp_path, "health")
    assemble_pack(tmp_path, "builders", "deploy", record=False, scope="health", allow_restricted=True)
    with pytest.raises(ScopeError):
        assemble_pack(tmp_path, "builders", "deploy", record=False, scope="health")


def test_allow_restricted_works_locally(tmp_path):
    _make(tmp_path, "health")
    pack = assemble_pack(tmp_path, "builders", "deploy", record=False, scope="health", allow_restricted=True)
    assert "synthetic restricted fact" in pack.text
    reindex(tmp_path, "m", encoder=lambda texts, name: [[1.0]] * len(texts), scope="health", allow_restricted=True)
    assert (tmp_path / "vectors.sqlite3").exists()
