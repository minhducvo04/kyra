"""Long-term memory for the companion, backed by a local vector store."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class MemoryRecord:
    text: str
    metadata: dict
    score: float = 0.0


class MemoryStore(ABC):
    """Interface: swap the backend without touching anything that uses it."""

    @abstractmethod
    def add(self, text: str, metadata: dict | None = None) -> None: ...

    @abstractmethod
    def retrieve(self, query: str, k: int = 5) -> list[MemoryRecord]: ...


class ChromaMemoryStore(MemoryStore):
    """v1 backend: local Chroma collection, Chroma's built-in embedding model."""

    def __init__(self, path: str = "data/memory_db", collection_name: str = "kyra_memory"):
        import chromadb

        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(collection_name)
        self._next_id = self._collection.count()

    def add(self, text: str, metadata: dict | None = None) -> None:
        self._collection.add(
            documents=[text],
            metadatas=[metadata or {}],
            ids=[str(self._next_id)],
        )
        self._next_id += 1

    def retrieve(self, query: str, k: int = 5) -> list[MemoryRecord]:
        if self._collection.count() == 0:
            return []
        n = min(k, self._collection.count())
        results = self._collection.query(query_texts=[query], n_results=n)
        records = []
        for text, meta, distance in zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0]
        ):
            records.append(MemoryRecord(text=text, metadata=meta, score=1 - distance))
        return records
