"""Context packs: the whole memory block for one task, built once at assignment.

`assemble_pack(root, team, task)` opens one scope's store read-only, reads the
team's hot file, and returns at most `max_chars` characters in priority order:
rules > pinned > the team's hot set > relevant facts > relevant faults > one
similar recipe. The agent does no memory reads during the task. Usage (which
items went in) and metrics (build ms, memory calls) go to the team inbox; the
merger folds them in later, so building a pack never waits on a write.

Inside a section rows keep a fixed order (rules and pinned by id; the rest by
relevance, then confidence, then id), so the same inputs give the same block.
A row that does not fit is skipped, never cut, and the final length is checked
in code. Relevance is a keyword idea (query words without stopwords, distinct
matches counted); an optional `embed(texts) -> vectors` adds cosine similarity
for wrong-word misses.

A fact whose key matches a pinned row's key is left out: what the owner said wins.
With a release policy (anything with `allows(classes)`), rows the policy does not
allow are left out; the pack carries the combined label of what went in so a
caller can widen its release label.

Isolation is structural: a pack opens only the one scope it is given, and the
section headers are a parameter (`headers`), so a host words its own pack.
"""
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from kyra_memory import inbox
from kyra_memory import store as _store
from kyra_memory.activation import components
from kyra_memory.labels import PrivacyClass, Tier, combine
from kyra_memory.store import TABLES, MemoryStore, Row, require_scope_allowed, scope_path
from kyra_memory.text import query_words

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 2500
EMBED_MIN_SIM = 0.35
DEFAULT_HEADERS = {"rules": "## Rules", "pinned": "## Pinned", "hot": "## Often used",
                   "facts": "## Facts", "faults": "## Known faults", "recipes": "## Recipe"}


@dataclass(frozen=True)
class Pack:
    text: str
    label: tuple[Tier, frozenset[PrivacyClass]]
    items: tuple[tuple[str, int], ...]
    left_out_by_label: int
    build_ms: float = 0.0
    memory_calls: int = 0


def _releasable(row: Row, policy) -> bool:
    if policy is None:
        return True
    tier, classes = row.label
    return tier <= Tier.T1 or (tier == Tier.T2 and policy.allows(classes))


def _scorer(task: str, embed):
    words = sorted(set(query_words(task)))
    qvec = None
    if embed is not None:
        import numpy as np

        qvec = np.asarray(getattr(embed, "encode_query", embed)([task])[0], dtype="float32")

    def score(row: Row) -> float:
        hay = row.hay
        s = float(sum(1 for w in words if w in hay))
        if qvec is not None:
            import numpy as np

            v = np.asarray(embed([hay])[0], dtype="float32")
            sim = float(v @ qvec / ((np.linalg.norm(v) * np.linalg.norm(qvec)) or 1.0))
            s = s + sim if (s > 0 or sim >= EMBED_MIN_SIM) else 0.0
        return s

    return score


def assemble(rows: dict[str, list[Row]], task: str, *, hot: list[Row] = (), max_chars: int = DEFAULT_MAX_CHARS,
             policy=None, embed=None, lessons_first: bool = False,
             weights: dict | None = None, headers: dict | None = None,
             activation: bool = False, now=None, budget_ms: float | None = None, _fade_log: list | None = None) -> Pack:
    """Pure: active rows per table plus the team's hot rows -> a budgeted block."""
    if max_chars < 0:
        raise ValueError("max_chars must be >= 0")
    headers = DEFAULT_HEADERS if headers is None else headers
    rows = {t: list(rows.get(t, [])) for t in TABLES}
    left_out = 0
    for t in TABLES:
        kept = [r for r in rows[t] if _releasable(r, policy)]
        left_out += len(rows[t]) - len(kept)
        rows[t] = kept
    pinned_keys = {r.key for r in rows["pinned"] if r.key}
    rows["facts"] = [r for r in rows["facts"] if not (r.key and r.key in pinned_keys)]
    if activation and budget_ms is not None:
        from kyra_memory.tiers import budgeted_candidates

        candidates = [r for t in TABLES for r in rows[t]]
        allowed = {(r.table, r.id) for r in budgeted_candidates(candidates, budget_ms, now or datetime.now(UTC))}
        rows = {t: [r for r in rows[t] if (t, r.id) in allowed] for t in TABLES}
    live = {(t, r.id): r for t in TABLES for r in rows[t]}
    # A hot file can be older than the store: keep only rows still active and allowed, in their current form.
    hot = [live[(h.table, h.id)] for h in hot if (h.table, h.id) in live and h.table in ("facts", "faults", "recipes")]
    hot_ids = {(r.table, r.id) for r in hot}
    score = _scorer(task, embed)
    factors = {}
    if activation:
        when = now or datetime.now(UTC)
        neighbors = {key: set() for key in live}
        for key, row in live.items():
            for ref in row.extra.get('refs', ()):
                if isinstance(ref, str) and ':' in ref:
                    table, _, rid = ref.partition(':')
                    ref = (table, int(rid)) if rid.isdigit() else ()
                if isinstance(ref, (tuple, list)) and len(ref) == 2:
                    target = tuple(ref)
                    if target in live and target != key:
                        neighbors[key].add(target)
                        neighbors[target].add(key)
        for key, row in live.items():
            item = {**row.extra, 'kind': row.table, 'created': row.knowledge_time,
                    'links': len(neighbors[key]), 'superseded': row.status == 'superseded',
                    'weight': (weights or {}).get(row.id, 1.0) if row.table == 'facts' else 1.0}
            parts = components(item, when)
            factors[key] = parts['A']
            if parts['A'] < 0.3 and _fade_log is not None:
                _fade_log.append({'id': row.id, 'tbl': row.table, **parts, 'why': 'activation_below_0.3'})

    def ranked(table: str) -> list[Row]:
        scored = [(score(r) * (weights or {}).get(r.id, 1.0) if table == "facts" else score(r), r)
                  for r in rows[table] if (table, r.id) not in hot_ids]
        if activation:
            scored = [(s * factors[(table, r.id)], r) for s, r in scored]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda sr: (-sr[0], -float(sr[1].extra.get("confidence", 0.0)), sr[1].id))
        return [r for _, r in scored]

    recipes = [] if any(r.table == "recipes" for r in hot) else ranked("recipes")[:1]
    sections = [("rules", rows["rules"]), ("pinned", rows["pinned"]), ("hot", hot),
                ("facts", ranked("facts")), ("faults", ranked("faults")), ("recipes", recipes)]
    if lessons_first:  # builders: the lesson rows (faults, recipes) come before facts, so a long note cannot crowd them out
        sections = sections[:3] + [sections[4], sections[5], sections[3]]

    parts: list[str] = []
    used = 0
    included: list[Row] = []
    for name, section in sections:
        header_done = False
        for row in section:
            line = row.line
            sep = "\n" if parts else ""
            cost = len(sep) + len(line) + (0 if header_done else len(headers[name]) + 1)
            if used + cost > max_chars:
                continue
            if not header_done:
                parts.append(sep + headers[name])
                sep = "\n"
                header_done = True
            parts.append(sep + line)
            used += cost
            included.append(row)
    text = "".join(parts)
    if len(text) > max_chars:  # the guarantee, independent of the loop's arithmetic
        raise RuntimeError(f"memory v2 assembler produced {len(text)} chars over the {max_chars} budget")
    label = combine(r.label for r in included) if included else (Tier.T0, frozenset())
    return Pack(text, label, tuple((r.table, r.id) for r in included), left_out)


def read_hot(root: Path | str, team: str, tenant: str | None = None) -> list[Row]:
    """The team's hot file as Row stubs (table and id are what count; assemble swaps in the live row)."""
    try:
        data = json.loads(inbox.hot_path(root, team, tenant).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for it in data.get("items", []):
        if it.get("table") in TABLES and isinstance(it.get("id"), int):
            out.append(Row(it["table"], it["id"], "", "", None, "", "", "", (Tier.T0, frozenset()), "active",
                           None, None, None))
    return out


_local = threading.local()


def _snapshot(root: Path | str, scope: str | None = None, allow_restricted: bool = False,
              tenant: str | None = None, activation: bool = False, budget_ms=None) -> tuple[dict[str, list[Row]], int]:
    """Active rows of one scope's store, reused in this thread until another connection commits.

    One read-only connection per thread and store file; `PRAGMA data_version`
    changes when any other connection commits, so an unchanged store costs one
    statement instead of five table reads. A replaced or symlinked file is
    reopened through MemoryStore, which re-checks the scope.
    """
    scope = scope or _store.DEFAULT_SCOPE
    require_scope_allowed(scope, allow_restricted)  # before the cache: a cached restricted store must not leak
    path = scope_path(root, scope, tenant)  # the cache key is the tenant's own file, so tenants never share an entry
    cache = _local.__dict__.setdefault("cache", {})
    st = os.stat(path)  # FileNotFoundError when there is no store for the scope: nothing else is ever opened
    from kyra_memory.tiers import allowed_tiers

    tiers = allowed_tiers(budget_ms) if activation and budget_ms is not None else None
    cache_key = (path, activation, tiers)
    entry = cache.pop(cache_key, None)
    if entry is not None:
        ident, store, version, rows = entry
        if ident == (st.st_dev, st.st_ino) and store._db.execute("PRAGMA data_version").fetchone()[0] == version:
            cache[cache_key] = entry
            return rows, 1
        store.close()
    store = MemoryStore(root, scope, read_only=True, allow_restricted=allow_restricted, tenant=tenant)
    version = store._db.execute("PRAGMA data_version").fetchone()[0]
    rows = {t: store.current(t, tiers=tiers) for t in TABLES}
    if activation:
        metadata = {} if tiers else store.recall_metadata()
        for t, group in (rows.items() if tiers else ()):
            for offset in range(0, len(group), 256):
                metadata.update(store.recall_metadata([(t, r.id) for r in group[offset:offset + 256]]))
        rows = {t: [replace(r, extra={**r.extra, **metadata.get((t, r.id), {})}) for r in group]
                for t, group in rows.items()}
    weights = {} if tiers else store.weights()
    if tiers:
        ids = [r.id for r in rows['facts']]
        for offset in range(0, len(ids), 256):
            weights.update(store.weights(ids[offset:offset + 256]))
    rows['_weights'] = weights
    cache[cache_key] = ((st.st_dev, st.st_ino), store, version, rows)
    return rows, store.queries + 1


def read_team_policy(root, team, tenant=None) -> dict:
    """Optional local defaults; validated names and no symlinked policy files."""
    path = inbox.hot_path(root, team, tenant).with_name(f'{inbox._team(team)}.policy.json')
    if path.parent.is_symlink():
        raise ValueError('team policy directory must not be a symlink')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, encoding='utf-8') as stream:
        values = json.load(stream)
    if not isinstance(values, dict):
        raise ValueError('team policy must be an object')
    if 'activation' in values and type(values['activation']) is not bool:
        raise ValueError('activation must be boolean')
    if values.get('review_cadence', 'off') not in ('off', 'monthly', 'yearly', 'both'):
        raise ValueError('invalid review cadence')
    budget = values.get('budget_ms')
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget < 0):
        raise ValueError('budget_ms must be a finite nonnegative number')
    return values


def assemble_pack(root: Path | str, team: str, task: str, *, task_id: str = "", max_chars: int = DEFAULT_MAX_CHARS,
                  policy=None, embed=None, record: bool = True, lessons_first: bool = False,
                  scope: str | None = None, headers: dict | None = None, allow_restricted: bool = False,
                  tenant: str | None = None, activation: bool | None = None, review_cadence: str | None = None,
                  budget_ms: float | None = None) -> Pack:
    """Build read-only; opt-in activation reviews write proposals after packing."""
    start = time.perf_counter()
    defaults = read_team_policy(root, team, tenant)
    activation = defaults.get('activation', False) if activation is None else activation
    review_cadence = defaults.get('review_cadence', 'off') if review_cadence is None else review_cadence
    budget_ms = defaults.get('budget_ms') if budget_ms is None else budget_ms
    hot = read_hot(root, team, tenant)
    rows, queries = _snapshot(root, scope, allow_restricted, tenant, activation, budget_ms)
    calls = queries + 1  # store statements plus the hot file
    fade_log = []
    try:
        pack = assemble(rows, task, hot=hot, max_chars=max_chars, policy=policy, embed=embed,
                        lessons_first=lessons_first, weights=rows.get("_weights"), headers=headers,
                        activation=activation, budget_ms=budget_ms, _fade_log=fade_log)
    except Exception as exc:  # an unavailable encoder must never fail a turn: rank lexically instead
        if embed is None:
            raise
        log.warning("memory v2 embedding failed (%s: %s); using lexical retrieval for this pack",
                    type(exc).__name__, exc)
        fade_log.clear()
        pack = assemble(rows, task, hot=hot, max_chars=max_chars, policy=policy, embed=None,
                        lessons_first=lessons_first, weights=rows.get("_weights"), headers=headers,
                        activation=activation, budget_ms=budget_ms, _fade_log=fade_log)
    if fade_log:
        path = scope_path(root, scope or _store.DEFAULT_SCOPE, tenant).with_suffix('.fade.jsonl')
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, ''.join(json.dumps(entry) + '\n' for entry in fade_log).encode('utf-8'))
        finally:
            os.close(fd)
    build_ms = (time.perf_counter() - start) * 1000.0
    if record:
        if pack.items:
            inbox.append(root, team, {"kind": "used", "task": task_id, "items": [list(i) for i in pack.items]}, tenant)
        inbox.append(root, team, {"kind": "metric", "task": task_id, "build_ms": round(build_ms, 3),
                                  "memory_calls": calls, "chars": len(pack.text)}, tenant)
    if activation and review_cadence != "off":
        from kyra_memory.review import due, review

        reader = MemoryStore(root, scope or _store.DEFAULT_SCOPE, read_only=True,
                             allow_restricted=allow_restricted, tenant=tenant)
        try:
            modes = due(reader, datetime.now(UTC), review_cadence)
        finally:
            reader.close()
        if modes:
            writer = MemoryStore(root, scope or _store.DEFAULT_SCOPE,
                                 allow_restricted=allow_restricted, tenant=tenant)
            try:
                for mode in modes:
                    review(writer, now=datetime.now(UTC), mode=mode)
            finally:
                writer.close()
    return Pack(pack.text, pack.label, pack.items, pack.left_out_by_label, build_ms, calls)
