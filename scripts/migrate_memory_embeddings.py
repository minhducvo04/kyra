"""One-time migration: re-embed existing memories under the new BGE
embedding function (see memory.py::bge_embedding_function).

Why this needs a migration, not just a config flip: switching a Chroma
collection's embedding function doesn't retroactively re-embed what's
already stored - old documents keep their old vectors, so similarity
between an old memory and a new BGE-embedded query becomes meaningless
even though both happen to be 384-dimensional. This reads out every
document with its original text/metadata (preserving timestamps and
roles), recreates the collection under the new embedding function, and
re-adds everything so it gets embedded fresh.

Safe to run more than once (idempotent: re-embeds under the same
function). Back up data/memory_db first if you want extra insurance -
scripts/migrate_memory_embeddings.py already refuses to touch anything
if the collection is empty.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import chromadb

from companion.memory import bge_embedding_function

DB_PATH = "data/memory_db"
COLLECTION = "kyra_memory"


def main() -> None:
    client = chromadb.PersistentClient(path=DB_PATH)

    old = client.get_or_create_collection(COLLECTION)
    n = old.count()
    if n == 0:
        print("No memories to migrate - nothing to do.")
        return

    print(f"Reading {n} existing memories...")
    dump = old.get(limit=n)
    ids, docs, metas = dump["ids"], dump["documents"], dump["metadatas"]

    print("Recreating collection under BAAI/bge-small-en-v1.5 (downloads once, ~130MB)...")
    client.delete_collection(COLLECTION)
    new = client.get_or_create_collection(COLLECTION, embedding_function=bge_embedding_function())

    print("Re-embedding and re-adding...")
    new.add(ids=ids, documents=docs, metadatas=metas)

    print(f"Done. {new.count()} memories now under the new embedding function.")


if __name__ == "__main__":
    main()
