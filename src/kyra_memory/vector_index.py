"""Verified SQLite vector generations. The source and previous index remain intact.

The encoder is a parameter: `encoder(texts, model_name, *, query=False) -> vectors`. The host may also install a
module-wide default with `set_default_encoder`; with neither, the functions refuse rather than guess a model.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from kyra_memory import store as _store
from kyra_memory.store import TABLES, MemoryStore
from kyra_memory.tenancy import tenant_root

_default_encoder = None


def set_default_encoder(encoder) -> None:
    global _default_encoder
    _default_encoder = encoder


def _resolve(encoder):
    encoder = encoder or _default_encoder
    if encoder is None:
        raise ValueError("no encoder: pass encoder= or call set_default_encoder")
    return encoder


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def reindex(root, name, *, encoder=None, scope=None, allow_restricted=False, tenant=None):
    import numpy as np
    encode = _resolve(encoder)
    store = MemoryStore(root, scope or _store.DEFAULT_SCOPE, read_only=True,
                        allow_restricted=allow_restricted, tenant=tenant)
    root = tenant_root(root, tenant)
    try:
        store._db.execute("BEGIN")
        rows = [row for table in TABLES for row in store.all_rows(table)]
    finally:
        store.close()
    vectors = np.asarray(encode([r.hay for r in rows], name), dtype="float32")
    if vectors.ndim != 2 or len(vectors) != len(rows) or not np.isfinite(vectors).all():
        raise ValueError("embedding count or values failed verification")
    fd, temporary = tempfile.mkstemp(prefix=".vectors-", suffix=".sqlite3", dir=root)
    os.close(fd)
    temporary = Path(temporary)
    target = root / "vectors.sqlite3"
    try:
        with sqlite3.connect(temporary) as db:
            db.execute("CREATE TABLE meta (model TEXT, count INTEGER)")
            db.execute("INSERT INTO meta VALUES (?, ?)", (name, len(rows)))
            db.execute("CREATE TABLE vectors (id TEXT PRIMARY KEY, hash TEXT, vector TEXT)")
            db.executemany("INSERT INTO vectors VALUES (?, ?, ?)",
                           [(f"{r.table}:{r.id}", digest(r.hay), json.dumps(v.tolist()))
                            for r, v in zip(rows, vectors, strict=True)])
        with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as db:
            if db.execute("SELECT count(*) FROM vectors").fetchone()[0] != len(rows):
                raise ValueError("index count failed verification")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("index integrity failed verification")
        with temporary.open("rb") as f:
            os.fsync(f.fileno())
        if target.exists():
            # A hard link retains the prior generation even after the atomic replacement.
            backup = root / f"vectors.previous-{temporary.stem}.sqlite3"
            os.link(target, backup)
        os.replace(temporary, target)
        # Only prune after the verified generation is live. Hard links retain each
        # generation's original modification time, rather than backup creation time.
        previous = sorted((p for p in root.glob("vectors.previous-*.sqlite3")
                           if not p.is_symlink() and p.is_file()),
                          key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        for old in previous[2:]:
            old.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return len(rows)


class IndexedEmbedding:
    def __init__(self, root, name, *, encoder=None, tenant=None):
        path = tenant_root(root, tenant) / "vectors.sqlite3"
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            if db.execute("SELECT model FROM meta").fetchone()[0] != name:
                raise ValueError("index model differs from active model")
            self.vectors = {h: json.loads(v) for h, v in db.execute("SELECT hash, vector FROM vectors")}
        self.name = name
        self._encoder = encoder

    def encode_query(self, texts):
        return _resolve(self._encoder)(texts, self.name, query=True)

    def __call__(self, texts):
        # New/edited notes are embedded locally until the next nightly generation.
        return [self.vectors[digest(t)] if digest(t) in self.vectors
                else _resolve(self._encoder)([t], self.name)[0] for t in texts]
