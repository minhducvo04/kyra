"""Search everything Kyra stores - by meaning and by exact term.

Usage:
    python3 scripts/search.py --reindex                    # build/refresh the index
    python3 scripts/search.py "why did we pick BGE"        # search
    python3 scripts/search.py "ICPC" --kind resume         # filter by kind
    python3 scripts/search.py "interview prep" --private   # include data/private_docs
    python3 scripts/search.py --stats                      # what Kyra stores, and what fell through
    python3 scripts/search.py "..." --mode lexical|vector|hybrid --explain

Index: data/search_index/ (FTS5 + a Chroma collection). Logic lives in
src/companion/search.py; this is the thin CLI, same shape as
watch_boards.py over job_boards.py.
"""
import argparse
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion.logging_setup import configure_logging
from companion.paths import PROJECT_ROOT
from companion.search import (
    KINDS,
    CrossEncoderReranker,
    HybridSearchIndex,
    LlmReranker,
    answer,
    evaluate,
    load_testset,
)


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def print_stats(index: HybridSearchIndex) -> None:
    report = index.inventory()
    print(f"{report['sources']} sources, {report['chunks']} chunks, {_human_bytes(report['bytes'])} on disk\n")
    width = max((len(k) for k in report["by_kind"]), default=4)
    for kind, counts in sorted(report["by_kind"].items(), key=lambda kv: -kv[1]["chunks"]):
        print(f"  {kind:<{width}}  {counts['sources']:>4} sources  {counts['chunks']:>5} chunks")
    if report["orphans"]:
        by_ext = ", ".join(f"{n} {ext}" for ext, n in report["orphans_by_ext"].items())
        print(f"\n{len(report['orphans'])} file(s) held but not searchable ({by_ext}):")
        for path in report["orphans"][:10]:
            print(f"  {path}")
        if len(report["orphans"]) > 10:
            print(f"  ... and {len(report['orphans']) - 10} more")
    if report["stale"]:
        print(f"\n{len(report['stale'])} indexed source(s) whose file is gone (run --reindex):")
        for path in report["stale"]:
            print(f"  {path}")


def print_hits(hits, explain: bool) -> None:
    if not hits:
        print("no matches")
        return
    for i, hit in enumerate(hits, 1):
        chunk = hit.chunk
        flag = " [private]" if chunk.sensitive else ""
        print(f"\n{i:>2}. {chunk.path}{flag}   ({chunk.kind}, chunk {chunk.index})")
        if explain:
            print(f"    score {hit.score:.4f}   lexical rank {hit.lexical_rank}   vector rank {hit.vector_rank}")
        snippet = " ".join(chunk.text.split())
        print(textwrap.fill(snippet[:400] + ("..." if len(snippet) > 400 else ""),
                            width=96, initial_indent="    ", subsequent_indent="    "))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", help="what to search for")
    parser.add_argument("--reindex", action="store_true", help="refresh the index before doing anything else")
    parser.add_argument("--stats", action="store_true", help="what is stored, plus orphans and stale entries")
    parser.add_argument("--kind", action="append", choices=KINDS, help="restrict to a kind (repeatable)")
    parser.add_argument("--private", action="store_true", help="include data/private_docs (excluded by default)")
    parser.add_argument("-k", type=int, default=8, help="how many results (default 8)")
    parser.add_argument("--mode", choices=("hybrid", "lexical", "vector"), default="hybrid")
    parser.add_argument("--explain", action="store_true", help="show the fused score and each retriever's rank")
    parser.add_argument("--eval", action="store_true", help="score every mode against the held-out relevance set")
    parser.add_argument("--answer", action="store_true",
                        help="have the local model write a cited answer over the results")
    parser.add_argument("--rerank", choices=("cross", "llm"), help="rerank the fused candidates (cross-encoder, or the local chat model)")
    args = parser.parse_args()
    configure_logging()

    if not (args.reindex or args.stats or args.query or args.eval):
        parser.error("give a query, or --reindex, or --stats, or --eval")

    reranker = {"cross": CrossEncoderReranker, "llm": LlmReranker}[args.rerank]() if args.rerank else None
    index = HybridSearchIndex()
    if args.reindex:
        stats = index.index()
        print(stats)
        for err in stats.errors:
            print(f"  error: {err}")
    if args.stats:
        print_stats(index)
    if args.eval:
        cases = load_testset(PROJECT_ROOT / "tests" / "data" / "search_testset.jsonl")
        print(f"{len(cases)} held-out queries, Recall@5 / MRR\n")
        print(f"  {'mode':<16} {'Recall@5':>9} {'MRR':>7}")
        results = [evaluate(index, cases, mode=mode) for mode in ("lexical", "vector", "hybrid")]
        if reranker:
            results.append(evaluate(index, cases, mode="hybrid", reranker=reranker))
        for r in results:
            print(f"  {r.mode:<16} {r.recall:>8.1%} {r.mrr:>7.3f}")
        for r in results:
            if r.misses:
                print(f"\n{r.mode} missed {len(r.misses)}:")
                for q in r.misses:
                    print(f"  - {q}")
    if args.query and args.answer:
        result = answer(index, args.query, k=args.k, kinds=args.kind,
                        include_sensitive=args.private, reranker=reranker)
        print(textwrap.fill(result.text, width=96))
        if result.citations:
            print("\nSources:")
            for i, chunk in enumerate(result.citations, 1):
                print(f"  [{i}] {chunk.path}" + ("  [private]" if chunk.sensitive else ""))
        for warning in result.warnings:
            print(f"\n! {warning}")
    elif args.query:
        hits = index.search(args.query, k=args.k, kinds=args.kind,
                            include_sensitive=args.private, mode=args.mode, reranker=reranker)
        print_hits(hits, args.explain)


if __name__ == "__main__":
    main()
