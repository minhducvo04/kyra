"""Vector search harness: per-tenant brute force versus one shared index.

    python -m kyra_memory.bench.vectors --tenants 1,10,100 --per-tenant 1000,10000 --dim 384 --out /tmp/vec

Per-tenant numpy brute force is measured against a 50 ms p95 budget.

Both sides use numpy brute force over synthetic unit vectors. No ANN library
(faiss, hnswlib or annoy) is installed in this environment, so no ANN number is
reported. The shared side is one matrix for all tenants plus a tenant filter:
scores for other tenants are masked out before the top 10 pick, so the result
stays exact. Filtering after the top 10 pick would drop valid hits and is not used.

This harness measures speed and isolation, not recall quality. Brute force is
exact, so recall is 1.0 by construction and says nothing about an ANN index.
Isolation is checked on every query: each returned row must belong to the
queried tenant, and the shared top 10 set must match the per-tenant set.
Synthetic vectors only, built in memory. No live store is opened.
"""
import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from kyra_memory.bench.load import _commit, _machine, _pct, wait_quiet

TOP_K = 10
BUDGET_MS = 50.0
ANN_NOTE = (
    "No ANN library (faiss, hnswlib, annoy) is installed, so the shared side is "
    "numpy brute force plus a tenant filter. An ANN index would trade exact "
    "results for speed and would need its own recall check before use."
)


def _summary_ms(values_s: list[float]) -> dict:
    ms = [v * 1000 for v in values_s]
    return {"n": len(ms), "p50_ms": round(_pct(ms, 0.50), 4), "p95_ms": round(_pct(ms, 0.95), 4),
            "max_ms": round(max(ms, default=0.0), 4),
            "mean_ms": round(statistics.fmean(ms), 4) if ms else 0.0}


def _unit(raw: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    return (raw / np.maximum(norms, 1e-12)).astype(np.float32)


def _topk(scores: np.ndarray, k: int) -> np.ndarray:
    k = min(k, scores.shape[0])
    part = np.argpartition(scores, -k)[-k:]
    return part[np.argsort(scores[part])[::-1]]


def _median_summary(run_summaries: list[dict]) -> dict:
    return {"n": sum(s["n"] for s in run_summaries), "runs": len(run_summaries),
            "p50_ms": round(statistics.median(s["p50_ms"] for s in run_summaries), 4),
            "p95_ms": round(statistics.median(s["p95_ms"] for s in run_summaries), 4),
            "max_ms": round(max(s["max_ms"] for s in run_summaries), 4),
            "mean_ms": round(statistics.median(s["mean_ms"] for s in run_summaries), 4),
            "run_p95_ms": [s["p95_ms"] for s in run_summaries]}


def run_point(tenants_n: int, per_tenant: int, *, dim: int, queries: int, seed: int, runs: int = 3) -> dict:
    rng = np.random.default_rng(seed)
    total = tenants_n * per_tenant
    raw = rng.standard_normal((total, dim), dtype=np.float32)
    query_raw = rng.standard_normal((queries, dim), dtype=np.float32)
    query_tenants = rng.integers(0, tenants_n, size=queries)

    start = time.perf_counter()
    per_tenant_mats = [_unit(raw[t * per_tenant:(t + 1) * per_tenant]) for t in range(tenants_n)]
    per_tenant_build_s = time.perf_counter() - start

    start = time.perf_counter()
    shared_mat = _unit(raw)
    tenant_ids = np.repeat(np.arange(tenants_n), per_tenant)
    shared_build_s = time.perf_counter() - start

    queries_unit = _unit(query_raw)
    isolation_ok = True
    sets_match = True
    # warm up BLAS once so the first timed query is not a load cost
    _ = per_tenant_mats[0] @ queries_unit[0]
    _ = shared_mat @ queries_unit[0]
    per_runs: list[dict] = []
    shared_runs: list[dict] = []
    for _run in range(runs):
        per_lat: list[float] = []
        shared_lat: list[float] = []
        for qi in range(queries):
            tenant = int(query_tenants[qi])
            q = queries_unit[qi]

            def per_query(tenant: int = tenant, q: np.ndarray = q) -> np.ndarray:
                return _topk(per_tenant_mats[tenant] @ q, TOP_K)

            def shared_query(tenant: int = tenant, q: np.ndarray = q) -> np.ndarray:
                scores = shared_mat @ q
                scores[tenant_ids != tenant] = -np.inf
                return _topk(scores, min(TOP_K, per_tenant))

            # alternate which side runs first so a load spike hits both sides over a run
            sides = [("per", per_query), ("shared", shared_query)] if qi % 2 == 0 else [
                ("shared", shared_query), ("per", per_query)]
            found: dict[str, np.ndarray] = {}
            for name, fn in sides:
                t0 = time.perf_counter()
                found[name] = fn()
                (per_lat if name == "per" else shared_lat).append(time.perf_counter() - t0)
            per_idx, shared_idx = found["per"], found["shared"]
            if not bool(np.all(tenant_ids[shared_idx] == tenant)):
                isolation_ok = False
            if set((shared_idx - tenant * per_tenant).tolist()) != set(per_idx.tolist()):
                sets_match = False
        per_runs.append(_summary_ms(per_lat))
        shared_runs.append(_summary_ms(shared_lat))
    per_summary = _median_summary(per_runs)
    shared_summary = _median_summary(shared_runs)
    bytes_per_vec = dim * 4
    return {
        "tenants": tenants_n, "vectors_per_tenant": per_tenant, "vectors_total": total,
        "dim": dim, "queries": queries, "runs": runs, "top_k": TOP_K,
        "per_tenant_build_s": round(per_tenant_build_s, 4),
        "shared_build_s": round(shared_build_s, 4),
        "per_tenant_query": per_summary,
        "shared_query": shared_summary,
        "per_tenant_meets_50ms_p95": per_summary["p95_ms"] < BUDGET_MS,
        "shared_meets_50ms_p95": shared_summary["p95_ms"] < BUDGET_MS,
        "isolation_ok": isolation_ok, "topk_sets_match": sets_match,
        "memory_bytes_per_tenant": per_tenant * bytes_per_vec,
        "memory_bytes_shared_total": total * bytes_per_vec + int(tenant_ids.nbytes),
    }


def _crossing(points: list[dict]) -> dict:
    """First measured per-tenant size at 50 ms p95 or over, else a linear estimate from the largest point."""
    singles = sorted((p for p in points if p["tenants"] == 1), key=lambda p: p["vectors_per_tenant"])
    over = next((p for p in singles if p["per_tenant_query"]["p95_ms"] >= BUDGET_MS), None)
    if over is not None:
        return {"kind": "measured", "vectors_per_tenant": over["vectors_per_tenant"],
                "p95_ms": over["per_tenant_query"]["p95_ms"]}
    if not singles:
        return {"kind": "not measured", "vectors_per_tenant": None, "p95_ms": None}
    largest = singles[-1]
    p95 = largest["per_tenant_query"]["p95_ms"]
    estimate = int(largest["vectors_per_tenant"] * BUDGET_MS / p95) if p95 > 0 else None
    return {"kind": "linear estimate from the largest measured point, not a measurement",
            "vectors_per_tenant": estimate, "p95_ms": p95,
            "based_on_vectors_per_tenant": largest["vectors_per_tenant"]}


def run_vectors(out: Path, *, tenant_counts=(1, 10, 100), per_tenant_counts=(1000, 10000),
                probe_counts=(), dim: int = 384, queries: int = 200, probe_queries: int = 50,
                runs: int = 3, seed: int = 1) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    quiet = wait_quiet()
    points = [run_point(t, n, dim=dim, queries=queries, seed=seed + t * 1000 + n, runs=runs)
              for t in tenant_counts for n in per_tenant_counts]
    probes = [run_point(1, n, dim=dim, queries=probe_queries, seed=seed + n, runs=runs) for n in probe_counts]
    result = {
        "kind": "vector search: per-tenant brute force versus one shared index with a tenant filter",
        "fork": "F3 proceeded on guess: per-tenant numpy brute force first, 50 ms p95 budget, index only past that size",
        "measures": "speed and isolation, not recall quality; brute force is exact by construction",
        "impl": "numpy float32 matrix times unit query vector, argpartition top 10; shared side masks other tenants before the top 10 pick",
        "ann_note": ANN_NOTE, "budget_ms_p95": BUDGET_MS, "top_k": TOP_K, "dim": dim,
        "data": "synthetic unit vectors (seeded normal, row normalised), built in memory, no live store opened",
        "date": datetime.now(UTC).date().isoformat(), "commit": _commit(), "machine": _machine(),
        "quiet_wait": quiet, "seed": seed, "queries_per_point": queries, "runs_per_point": runs,
        "query_summary": "median of per-run p50 and p95 over the runs; run_p95_ms lists each run",
        "points": points, "probe_points": probes,
        "brute_force_50ms_crossing_per_tenant": _crossing(points + probes),
    }
    (out / "vectors.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _int_list(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x.strip())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kyra_memory.bench.vectors", description=__doc__.splitlines()[0])
    p.add_argument("--tenants", default="1,10,100", help="comma list of tenant counts")
    p.add_argument("--per-tenant", default="1000,10000", help="comma list of vectors per tenant")
    p.add_argument("--probe-per-tenant", default="", help="extra sizes run at 1 tenant only, to find the 50 ms crossing")
    p.add_argument("--dim", type=int, default=384)
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--probe-queries", type=int, default=50)
    p.add_argument("--runs", type=int, default=3, help="query runs per point; reported p95 is the median run p95")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, required=True, help="directory for vectors.json")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_vectors(args.out, tenant_counts=_int_list(args.tenants),
                         per_tenant_counts=_int_list(args.per_tenant),
                         probe_counts=_int_list(args.probe_per_tenant), dim=args.dim,
                         queries=args.queries, probe_queries=args.probe_queries, runs=args.runs, seed=args.seed)
    for point in result["points"] + result["probe_points"]:
        print(f"tenants={point['tenants']} per_tenant={point['vectors_per_tenant']} "
              f"per-tenant p95={point['per_tenant_query']['p95_ms']}ms "
              f"shared p95={point['shared_query']['p95_ms']}ms isolation_ok={point['isolation_ok']}")
    print(f"crossing: {result['brute_force_50ms_crossing_per_tenant']}")
    print(f"wrote {args.out / 'vectors.json'}")
    return int(any(not p["isolation_ok"] or not p["topk_sets_match"]
                   for p in result["points"] + result["probe_points"]))


if __name__ == "__main__":
    raise SystemExit(main())
