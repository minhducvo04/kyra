"""Long-term memory for the companion, backed by a local vector store."""
import math
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from companion.paths import DATA_DIR


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


def bge_embedding_function():
    """A real retrieval-tuned embedding model, for callers who want better
    recall than Chroma's built-in default.

    Chroma's `DefaultEmbeddingFunction` is *already* all-MiniLM-L6-v2 (ONNX)
    with a 256-token cap that silently truncates anything longer - not an
    upgrade path, just what you get for free. `BAAI/bge-small-en-v1.5` is a
    genuinely stronger small retrieval model (higher MTEB retrieval score,
    512-token context) at a similar size (~130MB), and normalizing its
    embeddings makes cosine similarity behave the way Chroma expects.
    Downloads once from HF on first use, then runs fully local/offline.
    """
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    return SentenceTransformerEmbeddingFunction(model_name="BAAI/bge-small-en-v1.5", normalize_embeddings=True)


class ChromaMemoryStore(MemoryStore):
    """v1 backend: local Chroma collection. Retrieval blends semantic
    similarity with recency - for casual chat, "what we just talked about"
    often matters as much as pure topical similarity (see
    docs/agentic-roadmap.md, job #3).
    """

    # Recency half-life: a memory from `RECENCY_HALFLIFE_HOURS` ago carries
    # half the recency weight of a brand-new one. 24h means "today" stays
    # noticeably boosted without permanently burying older, still-relevant
    # memories - similarity still does most of the work past a day or two.
    RECENCY_HALFLIFE_HOURS = 24.0
    RECENCY_WEIGHT = 0.3  # final_score = (1 - RECENCY_WEIGHT)*similarity + RECENCY_WEIGHT*recency

    def __init__(
        self,
        path: str | None = None,
        collection_name: str = "kyra_memory",
        embedding_function=None,
    ):
        import chromadb

        # Default to the real retrieval model, not Chroma's built-in
        # default (which is unlabeled MiniLM anyway - see bge_embedding_
        # function's docstring) - callers get better recall without
        # having to remember to opt in.
        embedding_function = embedding_function or bge_embedding_function()
        # Default under DATA_DIR, not a cwd-relative "data/memory_db" - the
        # old relative default silently created a fresh empty store whenever
        # the process was launched from any directory but the project root.
        self._client = chromadb.PersistentClient(path=str(path or (DATA_DIR / "memory_db")))
        self._collection = self._client.get_or_create_collection(
            collection_name, embedding_function=embedding_function
        )

    def add(self, text: str, metadata: dict | None = None) -> None:
        """Store one exchange. The id is random, not a counter, because
        Chroma *ignores* an add whose id already exists - no exception,
        no warning, the record is simply gone and count() does not move.
        Ids used to be `str(self._next_id)` seeded from count() at
        construction, so a second store over the same collection (the web
        app and a CLI session, a store built while another was writing) or
        a deleted row handed out an id already in use and silently lost
        the memory. Nothing reads these ids as numbers - retrieve() ranks
        by the `timestamp` metadata - so uuid4 costs nothing, and the
        existing numeric ids stay valid alongside it.
        """
        metadata = dict(metadata or {})
        metadata.setdefault("timestamp", time.time())
        self._collection.add(
            documents=[text],
            metadatas=[metadata],
            ids=[uuid.uuid4().hex],
        )

    def retrieve(self, query: str, k: int = 5) -> list[MemoryRecord]:
        count = self._collection.count()
        if count == 0:
            return []
        # Overfetch so re-ranking by recency has something to work with -
        # a pure top-k similarity query would never let a slightly-less-
        # similar-but-recent memory win.
        n = min(max(k * 4, k), count)
        results = self._collection.query(query_texts=[query], n_results=n)

        now = time.time()
        records = []
        for text, meta, distance in zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0], strict=True
        ):
            similarity = 1 - distance
            ts = (meta or {}).get("timestamp")
            if ts is None:
                recency = 0.0  # older memories predating this field just don't get the boost
            else:
                age_hours = max(0.0, (now - ts) / 3600)
                recency = math.exp(-age_hours / self.RECENCY_HALFLIFE_HOURS)
            score = (1 - self.RECENCY_WEIGHT) * similarity + self.RECENCY_WEIGHT * recency
            records.append(MemoryRecord(text=text, metadata=meta, score=score))

        records.sort(key=lambda r: r.score, reverse=True)
        return records[:k]
