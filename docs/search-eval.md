# Search: what was measured (2026-09-07)

Held-out set: `tests/data/search_testset.jsonl` - 26 handwritten queries over the real corpus,
each with the source(s) that must come back. Written before any tuning, never generated, never
used to pick a configuration. 4 are exact-token queries, 3 are against `data/private_docs/`.
Corpus at the time: 127 sources, 596 chunks.

Metrics: **Recall@5** (did an expected source come back at all) and **MRR** (how high). A case
marked `sensitive` is searched with the private gate open, so this measures retrieval rather
than re-measuring the gate - the gate has its own tests.

## Results

All rows on the same index, same day, same code (title boost included).

| Configuration | Recall@5 | MRR | s/query | misses |
|---|---|---|---|---|
| lexical only (FTS5 BM25) | 88.5% | 0.708 | 0.00 | 3 |
| vector only (BGE, Chroma) | 69.2% | 0.558 | 0.14 | 8 |
| hybrid (RRF, k=60) | 80.8% | 0.683 | 0.01 | 5 |
| hybrid + `cross-encoder/ms-marco-MiniLM-L-6-v2` | 84.6% | 0.686 | 0.14 | 4 |
| hybrid + `BAAI/bge-reranker-base` | 84.6% | 0.665 | 0.41 | 4 |
| **hybrid + Qwen2.5-14B (the local chat model)** | **92.3%** | **0.716** | 3.13 | 2 |

Recall by depth, no reranker:

| k | lexical | vector | hybrid |
|---|---|---|---|
| 5 | 88.5% | 69.2% | 80.8% |
| 10 | 96.2% | 84.6% | 92.3% |
| 20 | 96.2% | 88.5% | **100%** |
| 40 | 96.2% | 96.2% | **100%** |

## What these numbers actually support

**Read the noise floor first.** 26 queries means one query is 3.8 points. The gap between the
best row and lexical-only is a single query. So the *ordering* of the top rows is suggestive,
not established - the same lesson round 4 of the router fine-tune recorded when a 93.1% vs
88.9% gap turned out to be seed noise. Three findings are bigger than one query and can be
relied on:

1. **Vector-only retrieval is not viable here** (69.2%, an 8-query gap). It misses every exact
   token - `RECENCY_WEIGHT`, `write18`, `ICPC` - which is a large share of how Duc actually
   searches his own material. This is the whole argument for hybrid, and it is not marginal.
2. **Hybrid's candidate pool is structurally better than BM25's**, not just differently
   ordered: hybrid reaches 100% recall by rank 20 while lexical plateaus at 96.2% even at rank
   40. Lexical can never find "the resume I tailored for the hedge fund new grad role" (the
   Northwind resume never says "hedge fund") at any depth. Depth cannot fix a document that is
   not in the list.
3. **Retrieval and ranking fail differently.** Hybrid's *recall* beats BM25 while its top-5
   *ordering* is worse. That is the textbook case for reranking, and it is why RRF weights were
   left alone: tuning fusion weights against 26 queries would be fitting the test set, and the
   diagnosis says the pool is already right.

## The reranker result was the opposite of the prior

The plan argued a cross-encoder was the right tool and that using the 14B chat model would be
"slow, non-deterministic and worse". **Measurement disagreed**: both cross-encoders scored
84.6%, the local chat model 92.3% - the only configuration above 90%, and best on MRR too.
Duc's original instinct ("run it through our local model") was right and the code comments
were corrected to say so.

The explanation is about what the queries are, not about model size. These are personal,
intent-shaped questions - *"which decisions are still waiting on me"*, *"resume for the
Orion early career role"*, *"what degree do I have"*. A cross-encoder trained on MS MARCO
web relevance scores passage-query term overlap; it has no notion of whose files these are or
what "waiting on me" means. The chat model reads the intent. All three of those queries are
fixed by the LLM reranker and missed by both cross-encoders.

The cost is real: 3.13 s/query against 0.01 s for raw hybrid, plus a 14B model in memory. So
retrieval stays instant by default and reranking is opt-in (`--rerank llm`). The web panel
should show fused results immediately and refine them - the model is already loaded whenever
an answer is being synthesized, so reranking is nearly free on that path.

Both cross-encoders are kept behind the `Reranker` interface: `ms-marco-MiniLM` is the fast
option (0.14 s, 90 MB) and `bge-reranker-base` is the control that shows the gap is not about
reranker quality.

## Remaining misses (best configuration)

- *"the resume I tailored for the hedge fund new grad role"* - needs to know Northwind is a hedge
  fund. Nothing in the corpus says so. A real gap; the honest fix is a fact in memory notes,
  not a retrieval change.
- *"ICPC"* - `data/private_docs/linkedin-profile-updates.md` genuinely discusses ICPC and
  outranks the resumes the test set expects. Arguably the test case is too narrow, but it was
  written before the results and is left alone: loosening a held-out expectation to match what
  the system returned is how a test set stops meaning anything.

## Changes made on reasoning, not on scores

- **FTS5 tokenizer** `porter unicode61 tokenchars '_'`. The default splits on `_`, so
  `RECENCY_WEIGHT` indexed as two words and matched the prose "recency weighting" instead of
  the identifier. `-` still splits, so "one page" finds "one-page". Guarded by
  `SCHEMA_VERSION`: an index built under different tokenization is dropped and rebuilt rather
  than left to answer with the old rules.
- **BM25 title weight 3.0.** A word in a document's title is a stronger signal than the same
  word in a page of prose; 3.0 is the conventional field boost. Chosen a priori - measured
  once afterwards (MRR 0.665 -> 0.708 on lexical, recall unchanged), not searched over.

## What this set cannot yet answer

26 queries is enough to reject vector-only and to establish the recall-versus-ordering split.
It is not enough to rank the top three configurations, tune fusion weights, or justify a
reranker fine-tune. Growing it - especially with queries whose answers live in the stores,
digests and conversation log, which are thinly covered - is the prerequisite for any finer
decision.
