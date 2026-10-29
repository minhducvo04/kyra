"""Load harness: concurrent writers and recall latency per tenant count.

    python -m kyra_memory.bench.load --backend sqlite --tenants 1,10,100 --runs 3 --out /tmp/load

Each point builds fresh synthetic tenants through the Backend interface, writes with
several threads per tenant, then times at least 1000 search queries and top-k pack
builds. Search is SQLite FTS5 or Postgres tsvector/GIN, with LIKE/ILIKE as fallback,
named in the output. Postgres (`--backend postgres`) runs the same workload through PgBackend.
The JSON also carries the pytest summary line of tests/kyra_memory, run in an isolated copy so
the harness never opens a live store.

Honest labels: the "pack" step is a sort-and-join stand-in (sort hits by term count,
join their lines), not kyra_memory.assembler.assemble, and its timing starts before the
search, so it includes search time. Write timing starts before tenant creation, so it
includes open_tenant. Recall queries run sequentially (no concurrent readers) against
stores just written by this process, so they are warm-cache numbers. Each point's stores
are deleted after measuring; only the JSON stays under --out.
"""
import argparse
import json
import os
import platform
import random
import re

try:
    import resource
except ImportError:  # Windows has no rlimit
    resource = None
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from kyra_memory.fts import impl_label
from kyra_memory.sqlite_backend import SqliteBackend
from kyra_memory.synth import LABEL, OBJECTS, SUBJECTS, VERBS

SCOPE = "bench"
TOP_K = 10
QUIET_LOAD = 16.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": round(_pct(values, 0.50), 6), "p95": round(_pct(values, 0.95), 6),
            "max": round(max(values, default=0.0), 6), "mean": round(statistics.fmean(values), 6) if values else 0.0}


def _machine() -> dict:
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    mem = sysctl("hw.memsize")
    return {"model": sysctl("hw.model") or platform.machine(), "memory_bytes": int(mem) if mem.isdigit() else None,
            "platform": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()}


def _commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def wait_quiet(timeout_s: float = 180.0) -> dict:
    """Wait until the 1 minute load average is under QUIET_LOAD, or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    waited = 0.0
    while True:
        load = os.getloadavg()[0]
        if load < QUIET_LOAD or time.monotonic() >= deadline:
            return {"load_1m": round(load, 2), "threshold": QUIET_LOAD, "waited_s": round(waited, 1),
                    "quiet": load < QUIET_LOAD}
        time.sleep(5.0)
        waited += 5.0


def isolation_tests(repo: Path) -> dict:
    """Run tests/kyra_memory in an isolated copy (the boundary test method) and parse its summary line."""
    src_pkg, test_dir = repo / "src" / "kyra_memory", repo / "tests" / "kyra_memory"
    if not test_dir.is_dir():
        return {"summary": "not run: tests/kyra_memory not found", "passed": None, "method": "isolated copy"}
    with tempfile.TemporaryDirectory(prefix="kyra-memory-iso-") as tmp:
        root = Path(tmp)
        shutil.copytree(src_pkg, root / "src" / "kyra_memory", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(test_dir, root / "tests" / "kyra_memory", ignore=shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "KYRA_MEMORY_ISOLATED_RUN": "1"}
        done = subprocess.run(
            [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(root),
             "-o", f"pythonpath={root / 'src'}", str(root / "tests" / "kyra_memory")],
            cwd=root, env=env, capture_output=True, text=True, timeout=600)
    line = next((ln.strip() for ln in reversed(done.stdout.splitlines()) if "passed" in ln or "failed" in ln), "")
    found = re.search(r"(\d+) passed", line)
    passed = int(found.group(1)) if found else None
    return {"summary": line or f"exit {done.returncode}", "passed": passed if "passed" in line else None,
            "exit": done.returncode, "method": "isolated copy of src/kyra_memory and tests/kyra_memory"}


def _write_batch(backend, tenant: str, worker: int, count: int, rng_seed: int) -> list[float]:
    rng = random.Random(rng_seed)
    lat = []
    for i in range(count):
        subject, verb = rng.choice(SUBJECTS), rng.choice(VERBS)
        body = f"user-{worker:02d} {verb} {rng.choice(OBJECTS)} for the {subject}"
        start = time.perf_counter()
        backend.add(tenant, "facts", body, source="bench:load", topic=verb,
                    key=f"w{worker}|{i}|{subject}|{verb}", label=LABEL)
        lat.append(time.perf_counter() - start)
    return lat


def open_backend(backend_name: str, root: Path):
    """The backend for one point. Postgres reads KYRA_MEMORY_PG_DSN; it does not take a DSN string."""
    if backend_name == "sqlite":
        return SqliteBackend(root, SCOPE)
    if backend_name == "postgres":
        from kyra_memory.pg_backend import PgBackend
        return PgBackend("KYRA_MEMORY_PG_DSN", schema=SCOPE)
    raise ValueError(f"unknown backend {backend_name!r}; only sqlite and postgres exist")


def fresh_postgres() -> None:
    """Drop and reinstall the bench schema so the next point starts empty."""
    from psycopg import sql

    from kyra_memory.pg_backend import _connect, install

    with _connect("KYRA_MEMORY_PG_ADMIN_DSN", autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(SCOPE)))
    install("KYRA_MEMORY_PG_ADMIN_DSN", SCOPE)


def _postgres_database_bytes() -> int:
    from kyra_memory.pg_backend import _connect

    with _connect("KYRA_MEMORY_PG_ADMIN_DSN", autocommit=True) as conn:
        return int(conn.execute("SELECT pg_database_size(current_database())").fetchone()[0])


def run_point(root: Path, tenants_n: int, *, writers: int, rows_per_tenant: int, queries: int, seed: int,
              backend_name: str = "sqlite") -> dict:
    # one open store per tenant holds db, wal and shm files: the default 256 fd soft limit stops near 80 tenants
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 8192:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, hard), hard))
    root.mkdir(parents=True, exist_ok=True)
    backend = open_backend(backend_name, root)
    tenants = [f"tenant-{t:04d}" for t in range(1, tenants_n + 1)]
    per_writer = max(1, rows_per_tenant // writers)
    write_lat: list[float] = []
    # write timing starts here, so it includes tenant creation (open_tenant below)
    start = time.perf_counter()
    try:
        for tenant in tenants:
            backend.open_tenant(tenant, create=True)
        with ThreadPoolExecutor(max_workers=min(32, writers * tenants_n)) as pool:
            futs = [pool.submit(_write_batch, backend, tenant, w, per_writer, seed + ti * 100 + w)
                    for ti, tenant in enumerate(tenants) for w in range(writers)]
            for fut in futs:
                write_lat += fut.result()
        write_s = time.perf_counter() - start
        # recall: distribute at least `queries` searches, each also built into a top-k pack
        per_tenant_q = max(1, -(-queries // tenants_n))
        rng = random.Random(seed)
        search_lat: list[float] = []
        pack_lat: list[float] = []
        hits = 0
        pack_chars = 0
        terms = list(SUBJECTS) + list(VERBS)
        for tenant in tenants:
            for _ in range(per_tenant_q):
                term = rng.choice(terms)
                t0 = time.perf_counter()
                rows = backend.search(tenant, "facts", term)
                t1 = time.perf_counter()
                # sort-and-join stand-in, not assembler.assemble; pack_lat starts at t0, so it includes search
                ranked = sorted(rows, key=lambda r: r.hay.count(term), reverse=True)[:TOP_K]
                pack = "\n".join(r.line for r in ranked)
                t2 = time.perf_counter()
                hits += len(rows)
                pack_chars += len(pack)
                search_lat.append(t1 - t0)
                pack_lat.append(t2 - t0)
        congestion = sum(backend.stats(t)["congestion_events"] for t in tenants)
        waits: list[float] = []
        for store in getattr(backend, "_open", {}).values():  # sqlite records writer-queue waits; postgres does not
            waits += list(getattr(store, "write_waits", []))
        if backend_name == "postgres":
            # column size, not file size: tables are shared, so there is no per-tenant directory
            sizes = [backend.stats(t)["bytes"] for t in tenants]
            database_bytes = _postgres_database_bytes()
        else:
            sizes = []
            database_bytes = None
    finally:
        backend.close()
    if backend_name == "sqlite":
        # after close the WAL is checkpointed, so the tenant dir holds the whole store size
        sizes = [sum(p.stat().st_size for p in (root / "tenants" / t).rglob("*") if p.is_file()) for t in tenants]
    rows_done = per_writer * writers * tenants_n
    point = {
        "tenants": tenants_n, "writers_per_tenant": writers, "rows_per_tenant": per_writer * writers,
        "writes": rows_done, "write_seconds": round(write_s, 4),
        "writes_per_s": round(rows_done / write_s, 2) if write_s else 0.0,
        "writes_per_s_per_tenant": round(rows_done / write_s / tenants_n, 2) if write_s else 0.0,
        "write_latency_s": _summary(write_lat),
        "writer_lock_waits_s": _summary(waits), "writer_lock_wait_total_s": round(sum(waits), 4),
        "congestion_events_over_1s": int(congestion),
        "queries": len(search_lat), "search_hits": hits, "pack_chars_total": pack_chars,
        "search_s": _summary(search_lat), "topk_pack_s": _summary(pack_lat),
        "store_bytes_per_tenant_mean": round(statistics.fmean(sizes), 1) if sizes else 0.0,
        "store_bytes_total": int(sum(sizes)),
        "store_bytes_kind": ("pg_column_size" if backend_name == "postgres"
                             else "tenant directory bytes after checkpoint"),
    }
    if database_bytes is not None:
        point["database_bytes"] = database_bytes
        point["writer_lock_note"] = "PgBackend serializes callers on one connection and does not record wait times"
    return point


def run_load(out: Path, *, backend_name: str = "sqlite", tenant_counts=(1, 10, 100), runs: int = 3,
             writers: int = 4, rows_per_tenant: int = 200, queries: int = 1000, seed: int = 1,
             repo: Path | None = None) -> dict:
    if backend_name not in ("sqlite", "postgres"):
        raise ValueError(f"unknown backend {backend_name!r}; only sqlite and postgres exist")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    quiet = wait_quiet()
    points = []
    for n in tenant_counts:
        run_rows = []
        for r in range(runs):
            point_root = out / f"run{r + 1}-t{n}"
            try:
                if backend_name == "postgres":
                    fresh_postgres()
                point = run_point(point_root, n, writers=writers, rows_per_tenant=rows_per_tenant,
                                  queries=queries, seed=seed + r, backend_name=backend_name)
            finally:
                # stores are measured inside run_point; keep only the JSON under --out
                shutil.rmtree(point_root, ignore_errors=True)
            run_rows.append(point)
        wps = [row["writes_per_s"] for row in run_rows]
        points.append({"tenants": n, "runs": run_rows,
                       "writes_per_s_median": round(statistics.median(wps), 2),
                       "writes_per_s_spread_pct": round(100 * (max(wps) - min(wps)) / statistics.median(wps), 1)
                       if wps and statistics.median(wps) else 0.0,
                       "search_p95_s_median": round(statistics.median(
                           [row["search_s"]["p95"] for row in run_rows]), 6),
                       "topk_pack_p95_s_median": round(statistics.median(
                           [row["topk_pack_s"]["p95"] for row in run_rows]), 6)})
    result = {
        "backend": backend_name,
        "search_impl": impl_label(backend_name),
        "pack_impl": "sort-and-join stand-in, not kyra_memory.assembler.assemble; topk_pack timing includes search",
        "write_timing": "includes tenant creation (open_tenant)",
        "recall_timing": "sequential queries, no concurrent readers, warm cache (stores just written)",
        "stores_kept": False,
        "date": datetime.now(UTC).date().isoformat(), "commit": _commit(), "machine": _machine(),
        "quiet_wait": quiet, "runs_per_point": runs, "seed": seed, "top_k": TOP_K,
        "isolation_tests": isolation_tests(repo or Path(__file__).resolve().parents[3]), "points": points,
    }
    (out / f"load-{backend_name}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kyra_memory.bench.load", description=__doc__.splitlines()[0])
    p.add_argument("--backend", default="sqlite")
    p.add_argument("--tenants", default="1,10,100", help="comma list of tenant counts")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--writers", type=int, default=4, help="concurrent writers per tenant")
    p.add_argument("--rows-per-tenant", type=int, default=200)
    p.add_argument("--queries", type=int, default=1000, help="minimum recall queries per point")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", type=Path, required=True,
                 help="directory for load-<backend>.json; each point's stores are removed after measuring")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    counts = tuple(int(x) for x in args.tenants.split(",") if x.strip())
    result = run_load(args.out, backend_name=args.backend, tenant_counts=counts, runs=args.runs,
                      writers=args.writers, rows_per_tenant=args.rows_per_tenant, queries=args.queries,
                      seed=args.seed)
    for point in result["points"]:
        print(f"tenants={point['tenants']} writes/s median={point['writes_per_s_median']} "
              f"search p95={point['search_p95_s_median']}s pack p95={point['topk_pack_p95_s_median']}s")
    print(f"wrote {args.out / f'load-{args.backend}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
