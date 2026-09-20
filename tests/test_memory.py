"""Post-conditions for the conversation memory store.

A dropped write is the worst failure this project can have: nothing
raises, nothing logs, and the memory is simply not there next turn.
Chroma ignores an add whose id already exists, so any id scheme that can
repeat loses records silently. These tests pin the guarantee - a write is
retrievable - not the scheme, so they still hold if the id changes again.
"""
from companion.memory import ChromaMemoryStore
from tests.fakes import HashingEmbedding


def open_store(tmp_path) -> ChromaMemoryStore:
    return ChromaMemoryStore(path=str(tmp_path / "memory_db"), embedding_function=HashingEmbedding())


def texts(records) -> set[str]:
    return {r.text for r in records}


def test_added_memory_is_retrievable(tmp_path):
    store = open_store(tmp_path)
    store.add("Duc prefers push-to-talk over hands-free")
    assert texts(store.retrieve("push to talk", k=5)) == {"Duc prefers push-to-talk over hands-free"}


def test_two_stores_over_one_collection_keep_every_write(tmp_path):
    """The reproduced defect: a second store over the same collection
    seeds its counter from count(), so both instances hand out the same
    next id and one write vanishes with no error anywhere.
    """
    a = open_store(tmp_path)
    a.add("first")
    a.add("second")
    b = open_store(tmp_path)
    b.add("third from B")
    a.add("third from A")

    assert {"first", "second", "third from B", "third from A"} <= texts(a.retrieve("third", k=10))
    assert a._collection.count() == 4


def test_write_after_a_row_is_deleted_is_kept(tmp_path):
    """The other drift: deleting a row moves count() backwards, so the
    next store to open the collection hands out an id that is still in use.
    """
    store = open_store(tmp_path)
    store.add("first")
    store.add("second")
    store._collection.delete(ids=store._collection.get()["ids"][:1])

    reopened = open_store(tmp_path)
    reopened.add("third")

    assert texts(reopened.retrieve("second third", k=10)) == {"second", "third"}
