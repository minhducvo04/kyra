"""Append-only JSONL log of every routed turn - which backend/path was
chosen and why, latency, errors, whether the turn got interrupted.

Deliberately a flat file, not SQL, for now: migrating a flat, single-table
JSONL to SQLite later is a small, well-defined script (same shape as
scripts/migrate_memory_embeddings.py - read every line, insert a row), so
starting simple here is a safely reversible choice, not a permanent one.
Keep every record carrying the same fields (use None, don't omit keys) so
that future migration stays that easy - schema drift is what makes a log
like this annoying to migrate, not the format itself.
"""
import json
import time

from companion.paths import DATA_DIR

LOG_PATH = DATA_DIR / "router.log"


def log_turn(**fields) -> None:
    record = {"ts": time.time(), **fields}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def read_recent(n: int = 20) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    lines = [line for line in LOG_PATH.read_text().splitlines() if line.strip()]
    return [json.loads(line) for line in lines[-n:]]
