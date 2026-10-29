import sqlite3

import numpy as np
import pytest

from companion.memory_v2 import embeddings, vector_index
from companion.memory_v2.store import MemoryStore
from companion.settings import Settings


def test_model_switch(monkeypatch):
    assert Settings(_env_file=None).memory.embedding_model == "gemma2"
    monkeypatch.setenv("KYRA_MEMORY", '{"embedding_model":"bge-small"}')
    assert Settings(_env_file=None).memory.embedding_model == "bge-small"
    monkeypatch.setenv("KYRA_MEMORY", '{"embedding_model":"invalid"}')
    with pytest.raises(ValueError):
        Settings(_env_file=None)
    monkeypatch.setattr(embeddings, "load_model", lambda name: type("Fake", (), {
        "encode": lambda self, texts, **kw: texts})())
    assert embeddings.encode(["hello"], "gemma2", query=True) == ["task: search result | query: hello"]
    assert embeddings.encode(["hello"], "gemma2") == ["title: none | text: hello"]
    assert embeddings.encode(["hello"], "bge-small") == ["hello"]


def test_atomic_reindex_and_count_refusal(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path, "kyra", create=True)
    store.add("facts", "Synthetic note", source="fixture")
    store.close()
    def encoder(texts, name):
        return np.ones((len(texts), 768))
    assert vector_index.reindex(tmp_path, "gemma2", encoder=encoder) == 1
    target = tmp_path / "vectors.sqlite3"
    before = target.read_bytes()
    with pytest.raises(ValueError, match="count"):
        vector_index.reindex(tmp_path, "gemma2", encoder=lambda *a: np.ones((0, 768)))
    assert target.read_bytes() == before
    replace = vector_index.os.replace
    def inspect(source, destination):
        assert target.read_bytes() == before
        with sqlite3.connect(source) as db:
            assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
        replace(source, destination)
    monkeypatch.setattr(vector_index.os, "replace", inspect)
    vector_index.reindex(tmp_path, "bge-small", encoder=lambda texts, name: np.ones((1, 384)))
    assert next(tmp_path.glob("vectors.previous-*")).read_bytes() == before
    with pytest.raises(ValueError, match="model"):
        vector_index.IndexedEmbedding(tmp_path, "gemma2")
    assert vector_index.IndexedEmbedding(tmp_path, "bge-small").name == "bge-small"


def test_three_nights_keep_latest_two_previous_indexes(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path, "kyra", create=True)
    store.add("facts", "Synthetic note", source="fixture")
    store.close()
    target = tmp_path / "vectors.sqlite3"
    generations = []
    original_unlink = type(target).unlink

    def checked_unlink(path, *args, **kwargs):
        if path.name.startswith("vectors.previous-"):
            # Pruning must see the completed newest index, never remove the live path.
            assert path != target
            with sqlite3.connect(target) as db:
                assert db.execute("SELECT vector FROM vectors").fetchone()[0] == "[3.0]"
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "unlink", checked_unlink)
    for night in range(4):  # initial index, followed by three nightly replacements
        vector_index.reindex(tmp_path, "gemma2",
                             encoder=lambda texts, name, value=night: np.full((1, 1), value))
        # Explicit generation times avoid filesystem timestamp-resolution dependence.
        vector_index.os.utime(target, (1000 + night, 1000 + night))
        generations.append(target.read_bytes())
    backups = list(tmp_path.glob("vectors.previous-*"))
    assert len(backups) == 2
    assert {p.read_bytes() for p in backups} == set(generations[1:3])
    assert target.read_bytes() == generations[3]

    before = {p.name: p.read_bytes() for p in backups}
    def refuse_swap(*args):
        raise OSError("synthetic swap failure")
    monkeypatch.setattr(vector_index.os, "replace", refuse_swap)
    with pytest.raises(OSError, match="swap failure"):
        vector_index.reindex(tmp_path, "gemma2", encoder=lambda *args: np.ones((1, 1)))
    assert target.read_bytes() == generations[3]
    assert all((tmp_path / name).read_bytes() == content for name, content in before.items())
