"""Erase tenant-derived copies before deleting their source rows.

Inbox add records have no assigned row id, so match their table and stripped
body (including duplicate submissions). Blank matching lines in place: offsets
and concurrent O_APPEND writers stay valid. Call under the store transaction.
This covers persistent files, not embeddings already loaded by another process;
reindex/hot refresh must not run concurrently with erasure.
"""
import json
import os
import sqlite3

from kyra_memory._io import write_json
from kyra_memory.store import TABLES, _deny_attach
from kyra_memory.tenancy import check_tenant, tenant_root


def purge_copies(root, tenant, table, ids, bodies):
    """Raise on cleanup failure, leaving the source rows available for a retry."""
    if check_tenant(tenant) is None or table not in TABLES:
        raise ValueError("purge needs a tenant and a known table")
    root = tenant_root(root, tenant)
    ids, bodies = set(ids), set(bodies)
    paths = [root / "vectors.sqlite3", *root.glob("vectors.previous-*.sqlite3")]
    for path in paths:
        if path.is_symlink():
            raise ValueError("refusing a symlinked vector index")
        if not path.exists():
            continue
        db = sqlite3.connect(path, timeout=0)
        try:
            db.set_authorizer(_deny_attach)
            db.execute("PRAGMA secure_delete=ON")
            with db:
                db.executemany("DELETE FROM vectors WHERE id = ?", [(f"{table}:{i}",) for i in ids])
                db.execute("UPDATE meta SET count = (SELECT count(*) FROM vectors)")
            busy, logged, done = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy or logged != done:
                raise RuntimeError("vector purge blocked by a reader; retry after it closes")
        finally:
            db.close()
    for path in (root / "inbox").glob("*.jsonl"):
        if path.is_symlink():
            raise ValueError("refusing a symlinked inbox")
        with path.open("r+b") as stream:
            # Snapshot length: never chase a busy appender or rewrite its partial tail.
            remaining = os.fstat(stream.fileno()).st_size
            while remaining:
                start = stream.tell()
                raw = stream.readline(remaining)
                remaining -= len(raw)
                if not raw.endswith(b"\n"):
                    break
                try:
                    rec = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if (isinstance(rec, dict) and rec.get("kind") == "add" and rec.get("table") == table
                        and isinstance(rec.get("body"), str) and rec["body"].strip() in bodies):
                    stream.seek(start)
                    stream.write(b" " * (len(raw) - 1) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    for path in (root / "teams").glob("*.hot.json"):
        if path.is_symlink():
            raise ValueError("refusing a symlinked hot file")
        data = json.loads(path.read_text(encoding="utf-8"))
        kept = [r for r in data["items"] if not (r["table"] == table and r["id"] in ids)]
        if len(kept) != len(data["items"]):
            write_json(path, {**data, "items": kept})


def purge_rows(root, tenant, db, table, ids):
    """Collect source and accepted-proposal bodies while they still exist."""
    bodies = set()
    proposals = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'proposals'").fetchone()
    for row_id in ids:
        row = db.execute(f"SELECT body FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row:
            bodies.add(row[0])
        if proposals:
            bodies.update(r[0] for r in db.execute("SELECT body FROM proposals WHERE tbl = ? AND fact_id = ?",
                                                  (table, row_id)))
    purge_copies(root, tenant, table, ids, bodies)
