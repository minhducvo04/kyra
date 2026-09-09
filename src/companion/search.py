"""Search across everything Kyra stores - documents, drafts, tracked rows
and the conversation log - by meaning *and* by exact term.

Why hybrid rather than "semantic search". Embeddings are good at "why did
we pick this embedding model" and bad at `RECENCY_WEIGHT`, `CS 169A` or
`qwen1.5b-v4-s13` - the tokens that actually come up when Duc is looking
for something he wrote. BM25 is the reverse. So both indexes are built
over the same chunks and their *ranks* are fused with Reciprocal Rank
Fusion (RRF): a BM25 score and a cosine distance are not on comparable
scales, and any weighted blend of the two needs normalisation constants
that drift the moment the corpus changes; RRF only reads positions, so it
cannot drift. The lexical half is SQLite FTS5, which ships inside
`sqlite3` - no new dependency for the half that usually wins.

The vector half reuses `memory.py::bge_embedding_function` - the same
retrieval model already chosen and justified there - but in its own
collection under `data/search_index/`, not a second collection inside
`memory_db`. The conversation log is re-indexed here like any other
source; ~800K of duplicated vectors is nothing at this size, and one
uniform path beats a special case inside the fusion.

Chunk size is a hard constraint, not a preference: `bge-small-en-v1.5`
has a 512-token window and silently truncates past it, the same trap
`bge_embedding_function`'s docstring records for MiniLM's 256-token cap.
`MAX_CHUNK_WORDS` is set below that with room to spare and is enforced by
a test, not by trusting the splitter.

Sensitivity is a post-condition, not a prompt request. Everything under
`data/private_docs/` is tagged `sensitive=True`, and `search()` refuses to
return it unless the caller explicitly asks. Drafting paths must never
ask: this repo already turned one true-but-private fact into a fabricated
resume entry (CLAUDE.md, 2026-09-04), and a default argument is the only
kind of guarantee that survives a prompt rewrite.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from companion.doc_text import UnsupportedDocumentType, extract_text
from companion.paths import DATA_DIR, PROJECT_ROOT
from companion.tools import Tool

log = logging.getLogger(__name__)

INDEX_DIR = DATA_DIR / "search_index"
COLLECTION = "kyra_search"

# bge-small-en-v1.5 truncates past 512 tokens. ~300 words is ~400 tokens
# for English prose, which leaves headroom for LaTeX and code-ish text
# that tokenizes worse than prose.
MAX_CHUNK_WORDS = 300
CHUNK_OVERLAP_WORDS = 50
RRF_K = 60  # the standard constant; large enough that no single list dominates the top

# Bump when the FTS schema or tokenizer changes: an index built by an older
# version is dropped and rebuilt rather than silently answering with the old
# tokenization. v2 added tokenchars '_' after `RECENCY_WEIGHT` was being split
# into "recency"+"weight" and matching the prose "recency weighting" instead.
SCHEMA_VERSION = 2

# BM25 weights the title column above the body: a word in "Duc Vo Resume Google
# SWE Orion" is a far stronger relevance signal than the same word buried in
# a page of prose. 3.0 is the conventional field boost (Elasticsearch's own
# `title^3` examples), chosen on that reasoning rather than by trying values
# against the held-out set - which would be fitting 26 queries.
BM25_TITLE_WEIGHT = 3.0

# Kinds are a closed set so the CLI and the UI can offer filters without
# discovering them from the data (and so a typo in a filter is an error).
KINDS = (
    "doc", "plan", "memory_note", "private", "resume", "cover_letter",
    "job_description", "digest", "application", "outreach", "reminder",
    "learning", "conversation",
)


@dataclass
class SourceDoc:
    """One indexable thing, before chunking."""
    source_id: str  # stable identity; re-indexing the same source replaces its chunks
    path: str  # what a human should be shown / what they can open
    kind: str
    title: str
    text: str
    sensitive: bool = False
    mtime: float = 0.0
    fmt: str = "text"  # how to split it: markdown | latex | text


@dataclass
class Chunk:
    chunk_id: str
    source_id: str
    path: str
    kind: str
    title: str
    index: int
    text: str
    sensitive: bool
    mtime: float


@dataclass
class Hit:
    chunk: Chunk
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None


@dataclass
class IndexStats:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.added} added, {self.updated} updated, {self.unchanged} unchanged, "
            f"{self.deleted} removed; {self.chunks} chunks written"
        )


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^#{1,6}\s", re.M)
_TEX_BLOCK = re.compile(r"^\s*\\(?:section|subsection|resumeSubheading|resumeProjectHeading)\b", re.M)
_PARAGRAPH = re.compile(r"\n\s*\n")


def _split_keeping_delimiters(text: str, pattern: re.Pattern[str]) -> list[str]:
    starts = [m.start() for m in pattern.finditer(text)]
    if not starts:
        return [text]
    if starts[0] != 0:
        starts.insert(0, 0)
    bounds = starts + [len(text)]
    # strict=False on purpose: bounds[1:] is shorter by one, which is what pairs
    # each start with the next one and the last with the end of the text.
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


def _blocks(text: str, fmt: str) -> list[str]:
    if fmt == "markdown":
        return _split_keeping_delimiters(text, _MD_HEADING)
    if fmt == "latex":
        return _split_keeping_delimiters(text, _TEX_BLOCK)
    return _PARAGRAPH.split(text)


def _window(block: str, max_words: int, overlap: int) -> list[str]:
    words = block.split()
    step = max(1, max_words - overlap)
    out = []
    for i in range(0, len(words), step):
        piece = words[i : i + max_words]
        if not piece:
            break
        out.append(" ".join(piece))
        if i + max_words >= len(words):
            break
    return out


def chunk_text(text: str, fmt: str = "text", max_words: int = MAX_CHUNK_WORDS,
               overlap: int = CHUNK_OVERLAP_WORDS) -> list[str]:
    """Split on the format's own structure (headings, LaTeX entries,
    paragraphs), then pack neighbours together up to `max_words` so a
    heading line doesn't become its own useless chunk, and window anything
    still too big. No chunk exceeds `max_words` - the embedding model would
    silently drop the tail.
    """
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            joined = "\n\n".join(buf).strip()
            if joined:
                chunks.append(joined)
        buf, buf_len = [], 0

    for block in _blocks(text, fmt):
        block = block.strip()
        if not block:
            continue
        n = len(block.split())
        if n > max_words:
            flush()
            chunks.extend(_window(block, max_words, overlap))
            continue
        if buf_len + n > max_words:
            flush()
        buf.append(block)
        buf_len += n
    flush()
    return chunks


def chunks_for(doc: SourceDoc) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"{doc.source_id}#{i}",
            source_id=doc.source_id,
            path=doc.path,
            kind=doc.kind,
            title=doc.title,
            index=i,
            text=text,
            sensitive=doc.sensitive,
            mtime=doc.mtime,
        )
        for i, text in enumerate(chunk_text(doc.text, doc.fmt))
    ]


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------


class Source(ABC):
    """Interface: one place documents come from. Adding a new corpus means
    a new Source, not a change to the index.
    """

    name: str

    @abstractmethod
    def iter_documents(self) -> Iterator[SourceDoc]: ...


def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


_FMT_BY_SUFFIX = {".md": "markdown", ".tex": "latex"}


class FileSource(Source):
    """Files on disk. One class rather than one per format, because
    `doc_text.extract_text` already dispatches on the suffix - the only
    per-format difference left is how to split, which is a property of the
    file, not of the source.
    """

    def __init__(self, root: Path | str, patterns: Sequence[str], kind: str,
                 sensitive: bool = False, name: str | None = None, owns_root: bool = True):
        self.root = Path(root)
        self.patterns = list(patterns)
        self.kind = kind
        self.sensitive = sensitive
        self.name = name or f"{kind}:{self.root.name}"
        # owns_root=False means "this source picks named files out of a
        # directory it does not own" - the project root holds the whole repo,
        # so listing everything there as un-indexed would be noise, not news.
        self.owns_root = owns_root

    def unindexed(self) -> Iterator[Path]:
        """Every file under this source's root, so inventory() can report what
        it holds but never indexes. Recursive only if a pattern asked to be."""
        if not self.owns_root or not self.root.exists():
            return
        walk = self.root.rglob("*") if any("**" in p for p in self.patterns) else self.root.glob("*")
        for path in walk:
            if path.is_file() and not path.name.startswith("."):
                yield path

    def iter_documents(self) -> Iterator[SourceDoc]:
        if not self.root.exists():
            return
        seen: set[Path] = set()
        for pattern in self.patterns:
            for path in sorted(self.root.glob(pattern)):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                try:
                    text = extract_text(path.name, path.read_bytes())
                except UnsupportedDocumentType:
                    continue  # counted as an orphan by inventory(), not an error
                except Exception as exc:  # a corrupt PDF must not stop the whole index
                    log.warning("search: could not read %s: %s", path, exc)
                    continue
                if not text.strip():
                    continue
                yield SourceDoc(
                    source_id=f"file:{rel_path(path)}",
                    path=rel_path(path),
                    kind=self.kind,
                    title=path.stem.replace("_", " "),
                    text=text,
                    sensitive=self.sensitive,
                    mtime=path.stat().st_mtime,
                    fmt=_FMT_BY_SUFFIX.get(path.suffix.lower(), "text"),
                )


class DigestSource(Source):
    """Digest days. Reads the canonical `<date>.json` where one exists so
    each item's summary is indexed - `render_markdown` drops summaries, and
    the summary is the whole "is this worth my time" signal (CLAUDE.md,
    2026-09-07). Days that predate the JSON record fall back to their `.md`.
    """

    name = "digests"

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or (DATA_DIR / "digests"))

    def iter_documents(self) -> Iterator[SourceDoc]:
        if not self.root.exists():
            return
        from companion.digest import load_archive

        covered = set()
        try:
            archive = load_archive(self.root)
        except Exception as exc:
            log.warning("search: could not load digest archive: %s", exc)
            archive = []
        for stem, data in archive:
            covered.add(stem)
            lines = [f"Daily digest for {stem}."]
            for item in data.news:
                lines.append(f"{item.source}: {item.title}\n{item.summary}\n{item.link}")
            for warning in data.warnings:
                lines.append(f"Warning: {warning}")
            path = self.root / f"{stem}.json"
            yield SourceDoc(
                source_id=f"digest:{stem}",
                path=rel_path(path),
                kind="digest",
                title=f"Digest {stem}",
                text="\n\n".join(lines),
                mtime=path.stat().st_mtime if path.exists() else 0.0,
                fmt="text",
            )
        for path in sorted(self.root.glob("*.md")):
            if path.stem in covered:
                continue
            yield SourceDoc(
                source_id=f"digest:{path.stem}",
                path=rel_path(path),
                kind="digest",
                title=f"Digest {path.stem}",
                text=path.read_text(encoding="utf-8", errors="replace"),
                mtime=path.stat().st_mtime,
                fmt="markdown",
            )


class StoreSource(Source):
    """Rows from the relational stores, one document per row. Imports are
    deferred so `import companion.search` stays cheap - the store modules
    pull in the LLM layer.
    """

    name = "stores"

    def iter_documents(self) -> Iterator[SourceDoc]:
        from sqlalchemy import select

        from companion import schema
        from companion.db import engine_for_store
        from companion.job_applications import DB_PATH as APPS_DB
        from companion.learning import DB_PATH as LEARNING_DB
        from companion.outreach import DB_PATH as OUTREACH_DB
        from companion.reminders import DB_PATH as REMINDERS_DB

        specs = (
            ("application", APPS_DB, schema.job_applications,
             lambda r: (f"{r.company} - {r.role}",
                        f"Application to {r.company} for {r.role}. Status: {r.status}. "
                        f"{r.link or ''}\n{r.notes or ''}")),
            ("outreach", OUTREACH_DB, schema.outreach_contacts,
             lambda r: (f"{r.name} ({r.company})",
                        f"Outreach to {r.name}, {r.role or 'unknown role'} at {r.company}. "
                        f"Status: {r.status}. {r.relation or ''}\n{r.note or ''}\n{r.follow_up or ''}")),
            ("reminder", REMINDERS_DB, schema.reminders,
             lambda r: (r.text[:60], f"Reminder: {r.text}. Due {r.due_at or 'unscheduled'}.")),
            ("learning", LEARNING_DB, schema.learning_items,
             lambda r: (r.topic, f"{r.topic}\n{r.summary}\nKey takeaway: {r.key_takeaway}")),
        )

        for kind, db_path, table, render in specs:
            try:
                engine = engine_for_store(db_path)
                with engine.connect() as conn:
                    rows = conn.execute(select(table)).all()
            except Exception as exc:
                log.warning("search: could not read %s rows: %s", kind, exc)
                continue
            for row in rows:
                title, text = render(row)
                if not text.strip():
                    continue
                yield SourceDoc(
                    source_id=f"{kind}:{row.id}",
                    path=f"{rel_path(Path(db_path))}#{table.name}/{row.id}",
                    kind=kind,
                    title=str(title),
                    text=text,
                    mtime=0.0,
                    fmt="text",
                )


class ConversationSource(Source):
    """The Chroma conversation log. Read through Chroma's own client rather
    than `ChromaMemoryStore` so this never constructs the embedding model
    just to list documents.
    """

    name = "conversation"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or (DATA_DIR / "memory_db"))

    def iter_documents(self) -> Iterator[SourceDoc]:
        if not self.path.exists():
            return
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(self.path))
            collection = client.get_collection("kyra_memory")
            payload = collection.get(include=["documents", "metadatas"])
        except Exception as exc:
            log.warning("search: could not read the conversation log: %s", exc)
            return
        for doc_id, text, meta in zip(
            payload["ids"], payload["documents"], payload["metadatas"] or [], strict=False
        ):
            if not (text or "").strip():
                continue
            yield SourceDoc(
                source_id=f"conversation:{doc_id}",
                path=f"{rel_path(self.path)}#{doc_id}",
                kind="conversation",
                title="conversation",
                text=text,
                mtime=float((meta or {}).get("timestamp") or 0.0),
                fmt="text",
            )


def default_sources() -> list[Source]:
    """Everything indexed by default: Duc's data plus the project's own
    decision record. Source code is deliberately absent - ripgrep is better
    at code, and code chunking is a separate problem (see the plan).
    """
    return [
        FileSource(PROJECT_ROOT / "docs", ["*.md", "templates/*.md"], kind="doc"),
        FileSource(PROJECT_ROOT / "docs" / "plans", ["*.md"], kind="plan"),
        FileSource(PROJECT_ROOT, ["CLAUDE.md", "README.md"], kind="doc", owns_root=False),
        FileSource(DATA_DIR / "memory_notes", ["*.md"], kind="memory_note"),
        FileSource(DATA_DIR / "private_docs", ["*.md", "*.txt"], kind="private", sensitive=True),
        FileSource(DATA_DIR / "resumes", ["*.tex"], kind="resume"),
        FileSource(DATA_DIR / "cover_letters", ["*.tex"], kind="cover_letter"),
        FileSource(DATA_DIR / "job_descriptions", ["*.pdf", "*.md"], kind="job_description"),
        FileSource(DATA_DIR / "projects", ["**/*.md"], kind="doc"),
        DigestSource(),
        StoreSource(),
        ConversationSource(),
    ]


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

_TOKEN = re.compile(r"[\w']+", re.UNICODE)


def fts_match_query(query: str) -> str:
    """FTS5 reads quotes, `*`, `NEAR`, `-` and friends as syntax, so a raw
    user query is a syntax error waiting to happen. Reduce it to bare
    tokens, each quoted, OR-ed together - BM25 does the ranking, so OR
    recalls broadly without matching everything equally.
    """
    tokens = [t for t in _TOKEN.findall(query) if t]
    return " OR ".join('"' + t.replace('"', "") + '"' for t in tokens)


def rrf_fuse(ranked_lists: Sequence[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


class SearchIndex(ABC):
    @abstractmethod
    def index(self, sources: Sequence[Source] | None = None) -> IndexStats: ...

    @abstractmethod
    def search(self, query: str, k: int = 10, kinds: Sequence[str] | None = None,
               include_sensitive: bool = False) -> list[Hit]: ...


class HybridSearchIndex(SearchIndex):
    """FTS5 for exact terms, Chroma+BGE for meaning, fused by RRF."""

    def __init__(self, dir_path: Path | str | None = None, embedding_function=None):
        self.dir = Path(dir_path or INDEX_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.dir / "index.db"
        self._embedding_function = embedding_function
        self._collection = None
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    # -- storage -----------------------------------------------------------

    def _create_tables(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        built = self._conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks'").fetchone()
        # An index that exists but carries a different (or absent, hence 0)
        # version was tokenized by other rules and must not answer queries.
        if built and version != SCHEMA_VERSION:
            log.warning("search: index built by schema v%s, rebuilding for v%s", version, SCHEMA_VERSION)
            self._conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS manifest;")
            self._conn.commit()
            shutil.rmtree(self.dir / "vectors", ignore_errors=True)
        self._conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                chunk_id UNINDEXED, source_id UNINDEXED, path UNINDEXED,
                kind UNINDEXED, title, text, idx UNINDEXED,
                sensitive UNINDEXED, mtime UNINDEXED,
                -- porter so "weighting" finds "weight"; tokenchars '_' so a
                -- snake_case identifier stays one token (unicode61 would split
                -- RECENCY_WEIGHT and then match the prose "recency weighting").
                -- '-' is deliberately left splitting, so "one page" still finds
                -- "one-page".
                tokenize = "porter unicode61 tokenchars '_'"
            );
            CREATE TABLE IF NOT EXISTS manifest(
                source_id TEXT PRIMARY KEY, hash TEXT NOT NULL, path TEXT,
                kind TEXT, indexed_at REAL
            );
            """
        )
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _vectors(self):
        if self._collection is None:
            import chromadb

            from companion.memory import bge_embedding_function

            client = chromadb.PersistentClient(path=str(self.dir / "vectors"))
            self._collection = client.get_or_create_collection(
                COLLECTION, embedding_function=self._embedding_function or bge_embedding_function()
            )
        return self._collection

    def last_indexed(self) -> float | None:
        """When the newest source in the index was written, or None for an
        empty index. Search never reindexes on its own, so callers that
        present results to a human need a way to say how old they are."""
        row = self._conn.execute("SELECT MAX(indexed_at) FROM manifest").fetchone()
        return row[0] if row and row[0] else None

    def close(self) -> None:
        self._conn.close()

    # -- indexing ----------------------------------------------------------

    def _drop_source(self, source_id: str) -> None:
        self._conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
        self._conn.execute("DELETE FROM manifest WHERE source_id = ?", (source_id,))
        try:
            self._vectors().delete(where={"source_id": source_id})
        except Exception as exc:  # an empty collection can raise; never fail the reindex over it
            log.debug("search: vector delete for %s: %s", source_id, exc)

    def _write_chunks(self, chunks: Sequence[Chunk]) -> None:
        self._conn.executemany(
            "INSERT INTO chunks(chunk_id, source_id, path, kind, title, text, idx, sensitive, mtime)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (c.chunk_id, c.source_id, c.path, c.kind, c.title, c.text, c.index,
                 int(c.sensitive), c.mtime)
                for c in chunks
            ],
        )
        collection = self._vectors()
        for i in range(0, len(chunks), 128):
            batch = chunks[i : i + 128]
            # upsert, not add: a chunk_id is deterministic (`source_id#i`), so
            # re-indexing an edited document writes ids that are already there.
            # Chroma *ignores* an add whose id exists - no exception, no warning
            # - and `_drop_source`'s vector delete is deliberately swallowed, so
            # if it ever misses, add() would leave the OLD embedding in place
            # while FTS holds the new text: hybrid search keeps working and
            # nothing surfaces that half the index is stale. upsert has no such
            # precondition. (Same failure Chroma has on ids everywhere - see
            # ChromaMemoryStore.add.)
            collection.upsert(
                ids=[c.chunk_id for c in batch],
                documents=[c.text for c in batch],
                metadatas=[
                    {"source_id": c.source_id, "path": c.path, "kind": c.kind,
                     "title": c.title, "sensitive": bool(c.sensitive), "mtime": c.mtime}
                    for c in batch
                ],
            )

    def index(self, sources: Sequence[Source] | None = None) -> IndexStats:
        sources = list(sources if sources is not None else default_sources())
        stats = IndexStats()
        docs: dict[str, SourceDoc] = {}
        for source in sources:
            try:
                for doc in source.iter_documents():
                    if doc.source_id in docs:
                        log.warning("search: duplicate source_id %s, keeping the first", doc.source_id)
                        continue
                    docs[doc.source_id] = doc
            except Exception as exc:
                stats.errors.append(f"{source.name}: {exc}")
                log.warning("search: source %s failed: %s", source.name, exc)

        manifest = {
            row["source_id"]: row["hash"]
            for row in self._conn.execute("SELECT source_id, hash FROM manifest")
        }

        for source_id, doc in docs.items():
            digest = hashlib.sha256(
                f"{doc.kind}\0{doc.path}\0{doc.fmt}\0{doc.text}".encode()
            ).hexdigest()
            if manifest.get(source_id) == digest:
                stats.unchanged += 1
                continue
            existed = source_id in manifest
            self._drop_source(source_id)
            chunks = chunks_for(doc)
            if not chunks:
                continue
            self._write_chunks(chunks)
            self._conn.execute(
                "INSERT INTO manifest(source_id, hash, path, kind, indexed_at) VALUES(?,?,?,?,?)",
                (source_id, digest, doc.path, doc.kind, time.time()),
            )
            stats.chunks += len(chunks)
            if existed:
                stats.updated += 1
            else:
                stats.added += 1

        for stale in set(manifest) - set(docs):
            self._drop_source(stale)
            stats.deleted += 1

        self._conn.commit()
        return stats

    # -- retrieval ---------------------------------------------------------

    def _row_to_chunk(self, row: sqlite3.Row) -> Chunk:
        return Chunk(
            chunk_id=row["chunk_id"], source_id=row["source_id"], path=row["path"],
            kind=row["kind"], title=row["title"], index=row["idx"], text=row["text"],
            sensitive=bool(int(row["sensitive"])), mtime=row["mtime"],
        )

    def _lexical(self, query: str, n: int, kinds, include_sensitive) -> list[str]:
        match = fts_match_query(query)
        if not match:
            return []
        sql = "SELECT chunk_id FROM chunks WHERE chunks MATCH ?"
        params: list[object] = [match]
        if not include_sensitive:
            sql += " AND sensitive = 0"
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        # bm25() takes a weight per column in declaration order:
        # chunk_id, source_id, path, kind, title, text, idx, sensitive, mtime.
        sql += f" ORDER BY bm25(chunks, 0, 0, 0, 0, {BM25_TITLE_WEIGHT}, 1.0, 0, 0, 0) LIMIT ?"
        params.append(n)
        try:
            return [row["chunk_id"] for row in self._conn.execute(sql, params)]
        except sqlite3.OperationalError as exc:
            log.warning("search: FTS query failed for %r: %s", query, exc)
            return []

    def _vector(self, query: str, n: int, kinds, include_sensitive) -> list[str]:
        clauses: list[dict] = []
        if not include_sensitive:
            clauses.append({"sensitive": False})
        if kinds:
            clauses.append({"kind": {"$in": list(kinds)}})
        where = clauses[0] if len(clauses) == 1 else ({"$and": clauses} if clauses else None)
        try:
            collection = self._vectors()
            if collection.count() == 0:
                return []
            result = collection.query(
                query_texts=[query], n_results=min(n, collection.count()), where=where
            )
        except Exception as exc:
            log.warning("search: vector query failed: %s", exc)
            return []
        return list(result["ids"][0])

    def search(self, query: str, k: int = 10, kinds: Sequence[str] | None = None,
               include_sensitive: bool = False, candidates: int = 40,
               mode: str = "hybrid", reranker: Reranker | None = None,
               rerank_pool: int = 30) -> list[Hit]:
        """`mode` exists so the eval harness can measure lexical-only and
        vector-only against hybrid on the same index rather than trusting
        that fusing two lists must be better.

        With a `reranker`, `rerank_pool` fused candidates are scored and cut
        to `k`. Retrieval is tuned for recall and reranking for precision:
        hybrid reaches every answer by rank 20 but orders the top 5 worse
        than BM25 alone, which is the gap the reranker exists to close.
        """
        query = query.strip()
        if not query:
            return []
        if kinds:
            unknown = set(kinds) - set(KINDS)
            if unknown:
                raise ValueError(f"unknown kind(s): {sorted(unknown)} (have: {list(KINDS)})")

        lexical = self._lexical(query, candidates, kinds, include_sensitive) if mode != "vector" else []
        vector = self._vector(query, candidates, kinds, include_sensitive) if mode != "lexical" else []
        lex_rank = {cid: i for i, cid in enumerate(lexical)}
        vec_rank = {cid: i for i, cid in enumerate(vector)}
        scores = rrf_fuse([lst for lst in (lexical, vector) if lst])
        if not scores:
            return []

        want = max(k, rerank_pool) if reranker is not None else k
        ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))[:want]
        placeholders = ",".join("?" * len(ordered))
        rows = {
            row["chunk_id"]: row
            for row in self._conn.execute(
                f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})", ordered
            )
        }
        hits = []
        for chunk_id in ordered:
            row = rows.get(chunk_id)
            if row is None:  # in the vector store but not FTS - a torn write; skip, don't crash
                log.warning("search: %s is in the vector index but not FTS", chunk_id)
                continue
            chunk = self._row_to_chunk(row)
            if chunk.sensitive and not include_sensitive:
                continue  # belt and braces: the filters above should already have excluded it
            hits.append(Hit(chunk=chunk, score=scores[chunk_id],
                            lexical_rank=lex_rank.get(chunk_id), vector_rank=vec_rank.get(chunk_id)))
        if reranker is not None:
            return reranker.rerank(query, hits, k)
        return hits

    # -- housekeeping ------------------------------------------------------

    def inventory(self, sources: Sequence[Source] | None = None) -> dict:
        """What Kyra actually stores, and what fell through the cracks:
        `orphans` are files sitting under an indexed root that no source
        could read (usually an unsupported type), `stale` are indexed
        sources whose origin is gone.
        """
        by_kind = {
            row["kind"]: {"sources": row["n"], "chunks": row["c"]}
            for row in self._conn.execute(
                "SELECT m.kind AS kind, COUNT(DISTINCT m.source_id) AS n,"
                " (SELECT COUNT(*) FROM chunks c WHERE c.kind = m.kind) AS c"
                " FROM manifest m GROUP BY m.kind"
            )
        }
        indexed = {row["source_id"]: row["path"] for row in self._conn.execute(
            "SELECT source_id, path FROM manifest")}

        orphans, stale = [], []
        for source in sources if sources is not None else default_sources():
            if not isinstance(source, FileSource):
                continue
            for path in source.unindexed():
                if f"file:{rel_path(path)}" not in indexed:
                    orphans.append(rel_path(path))
        for source_id, path in indexed.items():
            if source_id.startswith("file:") and not (PROJECT_ROOT / path).exists():
                stale.append(path)

        size = sum(p.stat().st_size for p in self.dir.rglob("*") if p.is_file())
        return {
            "by_kind": by_kind,
            "sources": len(indexed),
            "chunks": self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "bytes": size,
            "orphans": sorted(set(orphans)),
            "orphans_by_ext": dict(Counter(Path(p).suffix.lower() or "(none)" for p in set(orphans)).most_common()),
            "stale": sorted(set(stale)),
        }



# --------------------------------------------------------------------------
# reranking
# --------------------------------------------------------------------------


class Reranker(ABC):
    """Interface: reorder a candidate list against the query. Retrieval is
    about recall (get the right chunk into the pool at all); reranking is
    about precision (put it first). They fail differently and are measured
    separately - see docs/search-eval.md.
    """

    name: str

    @abstractmethod
    def rerank(self, query: str, hits: Sequence[Hit], k: int) -> list[Hit]: ...


class CrossEncoderReranker(Reranker):
    """A cross-encoder scores (query, passage) jointly, so it sees term
    interactions a bi-encoder cannot. Deterministic and fast (~0.9s for 30
    candidates), which makes a regression here a real regression rather than
    sampling noise.

    Measured on the held-out set it did NOT win: 84.6% Recall@5 against the
    local chat model's 92.3% (docs/search-eval.md). The queries here are
    personal and intent-shaped - "which decisions are still waiting on me",
    "resume for the Orion early career role" - and a reranker trained on
    MS MARCO web relevance has no notion of whose files these are. Kept
    behind the interface as the fast option and as the control.
    """

    DEFAULT_MODEL = "BAAI/bge-reranker-base"

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.name = f"cross-encoder:{self.model_name}"
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(self, query: str, hits: Sequence[Hit], k: int) -> list[Hit]:
        if not hits:
            return []
        scores = self._load().predict([(query, h.chunk.text) for h in hits])
        ordered = sorted(zip(hits, scores, strict=True), key=lambda pair: -float(pair[1]))
        return [Hit(chunk=h.chunk, score=float(score), lexical_rank=h.lexical_rank,
                    vector_rank=h.vector_rank) for h, score in ordered[:k]]


class LlmReranker(Reranker):
    """Let the local chat model do the ranking - and on this corpus it is
    the best option measured: 92.3% Recall@5 versus 84.6% for either
    cross-encoder, at 3.6s per query (docs/search-eval.md). It reads the
    query's intent, which is what these queries mostly need.
    It reads a numbered candidate list and returns positions, best first;
    anything it invents or omits is dropped, and the retrieval order fills
    the rest, so a bad reply degrades to "no reranking" instead of losing
    documents.
    """

    PROMPT = (
        "You are ranking search results. Return ONLY the numbers of the passages that answer "
        "the query, best first, comma-separated, at most {k}. No words, no explanation.\n\n"
        "Query: {query}\n\n{passages}"
    )

    def __init__(self, llm=None):
        self.name = "llm:local"
        self._llm = llm

    def _backend(self):
        if self._llm is None:
            from companion.llm import LocalLLM

            self._llm = LocalLLM(max_tokens=64)
        return self._llm

    def rerank(self, query: str, hits: Sequence[Hit], k: int) -> list[Hit]:
        if not hits:
            return []
        passages = "\n\n".join(
            f"[{i + 1}] ({h.chunk.path}) {' '.join(h.chunk.text.split())[:400]}"
            for i, h in enumerate(hits)
        )
        reply = self._backend().respond(
            system="You rank search results. You reply with numbers only.",
            history=[],
            user_input=self.PROMPT.format(k=k, query=query, passages=passages),
        )
        order: list[int] = []
        for token in re.findall(r"\d+", reply):
            idx = int(token) - 1
            if 0 <= idx < len(hits) and idx not in order:
                order.append(idx)
        order.extend(i for i in range(len(hits)) if i not in order)  # never lose a document
        return [hits[i] for i in order[:k]]

# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------


@dataclass
class EvalResult:
    mode: str
    n: int
    recall: float
    mrr: float
    misses: list[str] = field(default_factory=list)


def load_testset(path: Path | str) -> list[dict]:
    """The held-out relevance set: handwritten queries, each with the
    source(s) that must come back. Never generated, never tuned against -
    same discipline as tests/data/router_testset.jsonl.
    """
    import json

    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def evaluate(index: SearchIndex, cases: Sequence[dict], mode: str = "hybrid", k: int = 5,
             reranker: Reranker | None = None) -> EvalResult:
    """Recall@k (did an expected source come back at all) and MRR (how high).
    A case marked `sensitive` is searched with the private gate open, so the
    metric measures retrieval rather than re-measuring the gate.
    """
    found = 0
    reciprocal = 0.0
    misses = []
    for case in cases:
        hits = index.search(case["query"], k=k, include_sensitive=bool(case.get("sensitive")),
                            mode=mode, reranker=reranker)
        paths = [h.chunk.path for h in hits]
        expected = set(case["expect_any"])
        rank = next((i for i, p in enumerate(paths) if p in expected), None)
        if rank is None:
            misses.append(case["query"])
        else:
            found += 1
            reciprocal += 1.0 / (rank + 1)
    n = len(cases)
    label = mode if reranker is None else f"{mode}+rerank"
    return EvalResult(mode=label, n=n, recall=found / n if n else 0.0,
                      mrr=reciprocal / n if n else 0.0, misses=misses)


# --------------------------------------------------------------------------
# answering
# --------------------------------------------------------------------------

ANSWER_SYSTEM = (
    "You answer questions about Duc's own files using ONLY the numbered sources given to you. "
    "Every sentence that states a fact must end with the bracketed number of the source it came "
    "from, like [2]. Never cite a number you were not given. If the sources do not answer the "
    "question, say exactly what is missing instead of guessing. Be brief: a few sentences."
)

ANSWER_PROMPT = "Question: {query}\n\nSources:\n{sources}\n\nAnswer, citing each fact:"

_CITATION = re.compile(r"\[(\d+)\]")


@dataclass
class Answer:
    text: str
    citations: list[Chunk] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def enforce_citations(text: str, n_sources: int) -> tuple[str, list[int], list[str]]:
    """A citation the model invented is worse than no citation: it reads as
    provenance while pointing at nothing. Markers outside the range of what
    was actually retrieved are removed and reported - never silently kept,
    and never silently dropped either.
    """
    invented: set[int] = set()
    used: list[int] = []

    def keep_or_strip(match: re.Match[str]) -> str:
        n = int(match.group(1))
        if 1 <= n <= n_sources:
            if n not in used:
                used.append(n)
            return match.group(0)
        invented.add(n)
        return ""

    cleaned = _CITATION.sub(keep_or_strip, text).strip()
    warnings = []
    if invented:
        warnings.append(
            f"removed {len(invented)} citation(s) to sources that were never retrieved: "
            f"{sorted(invented)}"
        )
    if not used:
        warnings.append("the answer cited nothing - treat every claim in it as unsourced")
    return cleaned, used, warnings


def answer(index: SearchIndex, query: str, k: int = 6, llm=None,
           include_sensitive: bool = False, kinds: Sequence[str] | None = None,
           reranker: Reranker | None = None) -> Answer:
    """Retrieve, then let the local model write the answer over what came
    back. The model never sees anything the retrieval step did not return,
    so `include_sensitive=False` (the default) keeps private notes out of a
    generated answer the same way it keeps them out of a result list.
    """
    hits = index.search(query, k=k, kinds=kinds, include_sensitive=include_sensitive,
                        reranker=reranker)
    if not hits:
        return Answer(text="Nothing in the index matches that.", warnings=["no results to answer from"])

    if llm is None:
        from companion.llm import LocalLLM

        llm = LocalLLM(max_tokens=600)
    sources = "\n\n".join(
        f"[{i}] ({hit.chunk.path}) {' '.join(hit.chunk.text.split())}"
        for i, hit in enumerate(hits, 1)
    )
    reply = llm.respond(
        system=ANSWER_SYSTEM, history=[],
        user_input=ANSWER_PROMPT.format(query=query, sources=sources),
    )
    text, used, warnings = enforce_citations(reply, len(hits))
    return Answer(text=text, citations=[hits[i - 1].chunk for i in used], hits=hits, warnings=warnings)


# --- the chat tool ---------------------------------------------------------

SEARCH_TOOL_SNIPPET_WORDS = 60


class SearchKyraDataTool(Tool):
    """Look something up in everything Kyra stores, from a chat turn.

    The one thing this deliberately cannot do is reach private material.
    The web SEARCH panel can, because a human ticks a box there and watches
    the result; a chat turn has no such gesture and its far end is the
    Anthropic API. So there is no include_sensitive parameter to get wrong -
    the absence is the guarantee, and asking for the `private` kind is
    refused rather than quietly answered with an empty list, which would
    read as "nothing there" instead of "not yours to read".
    """

    name = "search_kyra_data"
    description = (
        "Search everything Kyra stores - project docs and plans, daily digests, resumes and cover "
        "letters, job applications and outreach, reminders, learning items, saved memory notes and "
        "past conversations - and get back the passages that match, each with the file it came from. "
        "Use it when Duc asks what was decided, written, measured or saved about something, or when "
        "answering needs a detail you would otherwise be guessing at. Not for source code: ripgrep is "
        "better at that. Private material is never searched."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for. A question or a phrase both work; exact "
                               "identifiers (CS 169A, qwen1.5b-v4-s13) work especially well.",
            },
            "kind": {
                "type": "string",
                "description": "Optional filter to one kind of material, e.g. "
                               f"{', '.join(k for k in KINDS if k != 'private')}.",
            },
            "k": {"type": "integer", "description": "How many passages to return (1-10, default 5)."},
        },
        "required": ["query"],
    }

    def __init__(self, index: SearchIndex | None = None, index_factory=HybridSearchIndex):
        # Built lazily: default_tool_registry() runs at startup in all three
        # front doors, and opening SQLite + Chroma there would cost every
        # launch whether or not the turn ever searches anything.
        self._index = index
        self._index_factory = index_factory

    @property
    def index(self) -> SearchIndex:
        if self._index is None:
            self._index = self._index_factory()
        return self._index

    def run(self, query: str = "", kind: str | None = None, k: int = 5) -> dict:
        query = query.strip()
        if not query:
            return {"error": "no query given - say what to look for"}
        if kind == "private":
            return {"error": "private material is not searchable from a chat turn - "
                             "open the SEARCH panel and turn on the private toggle"}
        if kind and kind not in KINDS:
            return {"error": f"unknown kind {kind!r} - have: "
                             f"{', '.join(x for x in KINDS if x != 'private')}"}
        k = max(1, min(int(k or 5), 10))

        hits = self.index.search(query, k=k, kinds=[kind] if kind else None, include_sensitive=False)
        results = [
            {
                "path": h.chunk.path,
                "kind": h.chunk.kind,
                "title": h.chunk.title,
                "score": round(h.score, 4),
                "text": " ".join(h.chunk.text.split()[:SEARCH_TOOL_SNIPPET_WORDS]),
            }
            for h in hits
        ]
        last = getattr(self.index, "last_indexed", lambda: None)()
        return {
            "query": query,
            "results": results,
            # Search never reindexes, so a query right after an edit answers from
            # the previous index. Saying when that was is what keeps a stale
            # answer from reading like a current one.
            "index_last_updated": (
                datetime.fromtimestamp(last).astimezone().strftime("%Y-%m-%d %H:%M") if last else "never"
            ),
        }
