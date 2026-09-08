# Semantic search over Kyra's data (2026-09-07)

## Goal

One search front door over everything Kyra holds — Duc's resumes, job descriptions, cover
letters, memory notes, digests, tracked applications and outreach, the conversation log, and
the project's own docs — that answers *"where did I say that?"* and *"what did we decide about
X, and why?"* without knowing which file to open. Semantic first, because the whole point is
finding by meaning; the local model then reranks and, on request, writes a short answer with
citations back to the real sources.

The second half of the ask, in Duc's words: *"help us improve on data storage and management."*
Indexing every source forces one inventory of what Kyra actually stores — which is why
`--stats` (below) reports orphans and stale entries, not just counts.

## What already exists — do not rebuild

| Piece | Where | Reuse as |
|---|---|---|
| Chroma + BGE embeddings, normalized | `memory.py::bge_embedding_function`, `ChromaMemoryStore` | the vector half; same embedding function, a **separate collection** |
| Text extraction: pdf / docx / txt / md / tex | `doc_text.py::extract_text` | ingestion for every file source |
| `MemoryStore` ABC → concrete backend | `memory.py` | the shape `SearchIndex` copies |
| Local model | `llm.py::LocalLLM` (Qwen2.5-14B-Instruct-4bit) | synthesis |
| Cross-encoder support | `sentence-transformers` (already a dependency, in `requirements-web.txt` too) | rerank |
| Job queue + SSE + panel pattern | `jobs.py`, `webapp.py`, `web/index.html` `.jobs-panel` | slice 3's UI |
| Keyword-search precedent | `digest.py::search_archive` | superseded for digests once indexed |
| **SQLite FTS5** | stdlib `sqlite3` (3.53.3, verified present) | the lexical half, **zero new dependencies** |

Measured corpus, 2026-09-07: ~50 Markdown/`.tex` text documents (~46k words), 15 resume
`.tex`, 16 job-description PDFs, 3 cover letters, 9 digest files, 1 library document, 4 SQLite
stores, and an 812K Chroma conversation log. Order **1–3k chunks**. That is not a scale
problem, so no vector database service, no FAISS, no pgvector. It is a *coverage, ranking and
citation* problem.

## Decisions

Duc's four, taken 2026-09-07:

1. **Corpus** — everything under `data/` **plus** `docs/`, `docs/plans/` and `CLAUDE.md`.
   Source code is excluded: ripgrep beats embeddings on code, and code chunking is its own
   problem for the weakest payoff.
2. **Local model** — rerank on every search, synthesize an answer only on request. Staged
   deliberately so retrieval quality stays measurable on its own before synthesis can hide it.
3. **Front door** — CLI first (verifiable), then a web SEARCH panel. Chat tool deferred to
   slice 4 because it needs a router adapter retrain (round 5).
4. **`data/private_docs/`** — indexed and tagged `sensitive`, structurally excluded from
   anything that generates outward-facing material. A default argument and a test, not a
   prompt request. This repo already has the scar: a memory note about Kyra became a
   fabricated resume entry (CLAUDE.md, 2026-09-04).

Taken here, not asked:

5. **Hybrid, not pure vector.** Real queries here are full of exact tokens — `Northwind`,
   `CS 169A`, `8f4f8496ae73`, `qwen1.5b-v4-s13`. Embeddings are bad at those and BM25 is
   excellent; the industry answer is both, fused. FTS5 makes the lexical half free.
6. **Reciprocal Rank Fusion, k=60**, not weighted score blending. BM25 scores and cosine
   distances are not on comparable scales, so blending them needs normalization constants
   that drift; RRF only reads ranks. `score = Σ 1/(60 + rank_i)`.
7. **Rerank is a cross-encoder, not the 14B LLM.** `BAAI/bge-reranker-base` is the
   purpose-built tool, the same family as the embedding model already chosen, runs local,
   is deterministic, and scores 20 candidates in ~1–2s; asking Qwen-14B to score 20
   candidates is slow, non-deterministic and worse. Duc's "run it through our local model"
   is honoured where it belongs — writing the answer. Both are built behind one `Reranker`
   ABC and **measured against the same eval set**; the numbers pick the default, the way
   the router and tool-distillation choices were made.
8. **Own Chroma collection under `data/search_index/`**, not a second collection inside
   `memory_db`. The conversation log is re-embedded into the search index like any other
   source — ~800K of duplicated vectors is irrelevant at this size, and uniformity beats a
   special case in the fusion path.

## Design

### Sources → chunks

`Source` ABC → `iter_documents() -> Iterable[SourceDoc]`, one concrete class per kind:

| Concrete | Covers | Chunk unit |
|---|---|---|
| `MarkdownSource` | `docs/**.md`, `docs/plans/**.md`, `CLAUDE.md`, `data/memory_notes/*.md`, `data/private_docs/*.md` | heading section, then windowed |
| `LatexSource` | `data/resumes/*.tex`, `data/cover_letters/*.tex` | entry block (`\resumeSubheading`), then windowed |
| `PdfSource` | `data/job_descriptions/*.pdf` | page, then windowed |
| `DigestSource` | `data/digests/*.json` (canonical record) | one chunk per item |
| `StoreSource` | `job_applications`, `outreach_contacts`, `reminders`, `learning_items` | one chunk per row |
| `ConversationSource` | `data/memory_db` Chroma collection | one chunk per logged exchange |

Every chunk carries: `chunk_id`, `source_id`, `path`, `kind`, `title`, `chunk_index`,
`mtime`, `sensitive: bool`.

**Chunk size is a hard constraint, not a preference.** `bge-small-en-v1.5` has a 512-token
context and silently truncates past it — the same trap CLAUDE.md already documents for
MiniLM's 256-token cap. Target ~300 words (~400 tokens) with ~50 words of overlap, enforced
in code with a test, not left to the splitter's good behaviour.

### Index and retrieval

`SearchIndex` ABC → `HybridSearchIndex`:

- **Lexical**: one FTS5 virtual table in `data/search_index/index.db`, BM25 ranked.
- **Vector**: a Chroma collection at `data/search_index/vectors`, BGE embeddings, same
  chunk ids.
- **Fuse**: RRF over the two ranked lists → top-N candidates.
- **Filter**: by `kind`, and by `sensitive` — `include_sensitive` defaults to `False`.
- **Rerank**: cross-encoder over the top ~20, returns the final top-k.

FTS5 `MATCH` treats quotes and operators as syntax, so a raw user query breaks it. Every
token is quoted and escaped before it reaches `MATCH` — a real bug class, pinned by a test.

### Freshness

`data/search_index/manifest.json` maps `source_id -> content hash`. Reindex touches only
changed sources; chunks whose source has vanished are deleted. A search against a stale
manifest triggers a cheap `stat` sweep and refreshes what changed, so the CLI and the panel
are never reading a stale index without saying so.

### Synthesis

`answer(query)` → local model reads the reranked top-k and writes a short answer where every
claim carries a `[n]` marker. **Post-condition in code**: every `[n]` must resolve to a chunk
that was actually retrieved; unresolvable markers are stripped and reported as a warning, not
silently dropped. In a container there is no mlx, so synthesis is unavailable there and says
so — rerank still works, since `sentence-transformers` is in `requirements-web.txt`.

### Storage management

`--stats` prints documents and chunks by kind, index size on disk, **orphans** (files present
under an indexed root but never indexed — usually an unsupported type) and **stale** entries
(indexed but the source is gone). That report is the "data management" half of the ask made
concrete: one place that says what Kyra actually stores.

## The eval set comes first

A search engine without a relevance test set is a demo. `tests/data/search_testset.jsonl`:
~25 **handwritten** queries, each with the source(s) that must appear in the top 5 — written
before any tuning, never generated, never used to pick a checkpoint. Same discipline as
`router_testset.jsonl` and the 70-case tool suite.

Metrics: Recall@5 and MRR, reported for **vector-only / FTS-only / hybrid / hybrid+rerank**,
so every added stage has to earn its place with a number.

## Slices

### Slice 1 — index + CLI

- Write `tests/data/search_testset.jsonl` (~25 handwritten queries) **before** tuning anything
  -> verify: committed, and a test asserts every expected path in it exists on disk
- `src/companion/search.py`: `Source` ABC + the six concrete sources, chunker, `SearchIndex`
  ABC → `HybridSearchIndex` (FTS5 + Chroma + RRF), manifest, sensitivity tagging
  -> verify: `pytest` green on chunk-size cap, FTS5 escaping, RRF ordering, incremental
     reindex (change one file, only that source re-embeds), deletion, and the sensitivity
     post-condition (a `private_docs` chunk never returns with the default flag)
- `scripts/search.py`: `--query`, `--kind`, `--reindex`, `--stats`, `--include-sensitive`
  -> verify: real `--reindex` over the real `data/` + `docs/`, then real queries run by hand
- Measure vector-only / FTS-only / hybrid on the eval set
  -> verify: recorded Recall@5 + MRR in `docs/search-eval.md`, hybrid beats both halves or
     the fusion is wrong and gets fixed before anything else is built

### Slice 2 — rerank + synthesis

- `Reranker` ABC → `CrossEncoderReranker` (`bge-reranker-base`) and `LlmReranker` (`LocalLLM`)
  -> verify: both measured on the same eval set; the default is whichever wins, stated with
     its number and its latency, not assumed
- `answer(query)` — local synthesis with the citation post-condition
  -> verify: a real local run answering a real question, every `[n]` resolving to a real
     retrieved chunk; a test with a fabricated marker proves it is stripped and warned about
- `--answer` on the CLI
  -> verify: real end-to-end run, output pasted into the session record

### Slice 3 — web SEARCH panel — DONE 2026-09-07

- `POST /api/search`, `POST /api/search/answer` (SSE, reusing the `/api/chat/stream` shape),
  `POST /api/search/reindex`
  -> verify: real requests against the running server, `ApiError` envelope on bad input
- Third slide-in panel reusing `.jobs-panel` / `.jobs-tab` / `.jobs-btn`, kind filter chips,
  an explicit include-sensitive toggle, per-hit score + kind badge + snippet, and a
  "Synthesize answer" button
  -> verify: real browser interaction and a screenshot, no console errors, sensitive hits
     visibly absent until the toggle is on

### Slice 4 — wiring

- `search_kyra_data` chat tool - **DONE 2026-09-08** - + router adapter **round 5** (a new tool
  means retraining, two seeds, per the standing rule)
- Drafting paths pull background through `search(..., include_sensitive=False)` instead of
  whole documents - **not done, deliberately**; see below
  -> verify: a real draft run proves no `private_docs` content reaches generated material

## Not built, on purpose

- **No source-code indexing.** ripgrep is better at it.
- **No vector database service, no FAISS, no pgvector.** 1–3k chunks.
- **No query rewriting / HyDE.** Measure the base system first; add only if the eval set says
  a query class is failing.
- **No cross-encoder fine-tune.** Off-the-shelf `bge-reranker-base` first; a fine-tune needs a
  labelled set far larger than 25 queries.

## Open for Duc

- Does the conversation log belong in the same result list as documents, or does it deserve
  its own tab? (Indexed either way; this is presentation.)
- Should `--stats`' orphan report be wired into the daily digest as a "housekeeping" line?

---

## What actually shipped (slice 1 + most of slice 2, 2026-09-07)

Built: `src/companion/search.py`, `scripts/search.py`, `tests/test_search.py` (20 tests),
`tests/data/search_testset.jsonl` (26 queries), `docs/search-eval.md`. 229 tests pass, ruff
clean, purely additive.

Deviations from the design above, and why:

- **One `FileSource`, not `MarkdownSource`/`LatexSource`/`PdfSource`.** `doc_text.extract_text`
  already dispatches on the suffix, and the only remaining per-format difference is how to
  split - which is a property of the file, not of the source. Three classes would have been
  three copies of the same glob.
- **The reranker default is the opposite of what this plan argued.** The plan said a
  cross-encoder was the right tool and the 14B chat model would be worse. Measured: 84.6% for
  both cross-encoders, 92.3% for the local chat model. Duc's original instinct was right; the
  numbers and the reasoning are in `docs/search-eval.md`, and the code comments were corrected.
- **Reranking is opt-in, not always-on.** 3.13 s/query against 0.01 s. Slice 3's panel should
  show fused results immediately and refine them.
- **Two constraints added that this plan did not anticipate**, both from real failures:
  `tokenize = "porter unicode61 tokenchars '_'"` (the default splits `RECENCY_WEIGHT` into two
  words and matches the prose "recency weighting"), and a `SCHEMA_VERSION` guard that rebuilds
  an index built under different tokenization rather than letting it answer with the old rules.
- **The orphan report walks each owned root**, rather than only re-globbing the source's own
  patterns - the original version could only ever report unreadable files, when the real
  question is "what does Kyra hold that search cannot see". It currently, correctly, reports
  18 compiled `.pdf` whose `.tex` sources are indexed.

- **Auto-refresh on a stale index was NOT built**, though the Freshness section above promises
  it. Indexing is incremental by content hash, but it only runs when `--reindex` is passed, so
  a search after an edit answers from the previous index without saying so. Deliberate for now
  - a stat sweep plus possible re-embedding inside every query makes search latency
  unpredictable - but the honest fix is either a staleness *warning* on search, or wiring
  `--reindex` into the 05:00 digest run. Not done either way, so treat the index as stale until
  reindexed.

Still open, unchanged: slice 3 (web SEARCH panel) and slice 4 (`search_kyra_data` chat tool +
router round 5, and routing the drafting paths through `search(include_sensitive=False)`).

## Slice 3 as built (2026-09-07)

`POST /api/search`, `POST /api/search/answer` (SSE, same event shape as `/api/chat/stream`),
`POST /api/search/reindex`, plus a third slide-in panel reusing the `.jobs-panel` shell.
`tests/test_search_api.py` (8 tests); 238 pass, ruff clean.

Two things worth knowing:

- **The index is a `cached_property` on `_Runtime` and the local model is fetched inside the
  worker thread**, not at request time. `LazyBackends["local"]` builds a 14B MLX model on first
  access, so evaluating it eagerly made the endpoint untestable and hung the first test run for
  minutes before it was moved.
- **`k` is clamped to 1..50** so a single request cannot ask for the whole index.

Verified against the real index in a real browser, not fixtures: the same query ("what needs my
input") returned 8 hits with **zero** private ones by default and 4 with the toggle on; kind chips
filtered to a single kind; reindex reported `2 added, 5 updated, 122 unchanged, 0 removed; 109
chunks written`; and the answer endpoint, run against the real local model, produced a correct
answer citing `docs/search-eval.md` and `CLAUDE.md` with no invented citations and no private
content in its sources.

Still open: slice 4 (the `search_kyra_data` chat tool) remains deferred - it needs router
adapter round 5, per the standing rule that a new tool means a retrain on two seeds.

## Slice 4 as built (2026-09-08)

`SearchKyraDataTool` in `search.py`, registered by `default_tool_registry()`. Three properties
worth keeping:

- **No `include_sensitive` parameter at all.** The panel can reach private material because a
  human ticks a box and watches the result; a chat turn has no such gesture and its far end is
  the Anthropic API. Asking for the `private` kind is *refused*, not answered with an empty
  list - empty reads as "nothing there" rather than "not yours to read".
- **The index opens on first use.** `default_tool_registry()` runs at startup in all three front
  doors; the registry still builds in 0.55 s and does not import chromadb.
- **Every result carries the index's last-updated date**, because search never reindexes on its
  own. Which is also why `scripts/daily_digest.py` now reindexes as part of the 05:00 run
  (best-effort, `--no-reindex` to skip, never reached by `--dry-run`): the digest is the one job
  that already runs every day, so the morning after a day's work the index is current.

Verified against the real index: correct hits with paths, zero private hits on a query that
returns four of them with the panel toggle on, the refusal path, and then a real Claude
tool-calling turn - which called the tool twice with refined queries, cited the right files, and
correctly said it found no decision about *tuning* k=60 rather than inventing one.

### The second half, and why it is not built

Routing the drafting paths through retrieved passages instead of whole documents is a **quality**
change, not a safety one: `data/private_docs/` never reaches drafting today, because the drafting
paths read the document library and memory notes and private docs are in neither. Against it:
retrieval decides what the model can see, and what is not retrieved cannot be written about. The
2026-09-06 Northwind run cut the whole ICPC block for a role where competitive programming was the
strongest signal on the page - with the *full* document in front of it. A retrieved subset gives
that failure a second, quieter place to happen, and `resume_guard.py` can see an invention but
never an omission. Left for Duc as `needs-your-input.md` item 23, with a recommendation to apply
it to background facts only, never to the resume source.
