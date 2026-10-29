"""Opt-in retrieval distance and held discard proposals; never delete a row.

Tiering is a writer maintenance pass. Retrieval reads indexed stored tiers.
Deep search checks time every 64 rows; an in-flight batch may overrun its deadline.
"""
import math
import time
from dataclasses import replace
from datetime import timedelta

from kyra_memory.activation import _time, activation
from kyra_memory.review import _neighbors
from kyra_memory.store import TABLES


def tier_of(activation, *, protected=False) -> str:
    return 'near' if protected or activation >= .5 else 'far' if activation >= .3 else 'deep'


def _budget(budget_ms):
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms < 0:
        raise ValueError('budget_ms must be a finite nonnegative number')
    return budget_ms


def _states(rows, now):
    keyed = {(r.table, r.id): r for r in rows}
    neighbors = _neighbors(keyed, {key: r.extra for key, r in keyed.items()})
    for key, row in keyed.items():
        item = {**row.extra, 'kind': row.table, 'created': row.knowledge_time,
                'links': max(len(neighbors[key]), row.extra.get('links', 0)),
                'superseded': row.status == 'superseded'}
        value = activation(item, now)
        protected = row.table in ('rules', 'pinned') or any(
            row.extra.get(flag) for flag in ('owner_important', 'important', 'fade_exempt'))
        yield row, value, tier_of(value, protected=protected), item['links'], protected


def allowed_tiers(budget_ms):
    budget = _budget(budget_ms)
    return ('near',) if budget < 50 else ('near', 'far') if budget < 200 else ('near', 'far', 'deep')


def budgeted_candidates(rows, budget_ms, now=None):
    """Filter materialized tiers without recomputing any row's activation."""
    allowed = allowed_tiers(budget_ms)
    return [r for r in rows if r.extra.get('retrieval_tier', 'near') in allowed]


def retier(store, now):
    """Atomically materialize tiers; owner recalls cannot race a stale downgrade."""
    counts = dict.fromkeys(('near', 'far', 'deep'), 0)
    with store.transaction() as db:
        metadata, weights = store.recall_metadata(), store.weights()
        rows = [replace(r, extra={**r.extra, **metadata.get((t, r.id), {}),
                                  'weight': weights.get(r.id, 1.0) if t == 'facts' else 1.0})
                for t in TABLES for r in store.current(t)]
        for row, _, tier, _, protected in _states(rows, now):
            assert not protected or tier == 'near', 'protected memory must remain near'
            db.execute(f'UPDATE {row.table} SET retrieval_tier = ? WHERE id = ?', (tier, row.id))
            counts[tier] += 1
    return counts


def map_items(rows, now):
    """Metadata only for the MAP page; no quiet narration enters a context pack."""
    return [{'table': r.table, 'id': r.id, 'activation': value, 'tier': tier,
             'quiet': value < .3 and not protected, 'fade_exempt': bool(r.extra.get('fade_exempt'))}
            for r, value, tier, _, protected in _states(rows, now)]


def discard_candidates(rows, now):
    """Return proposals with a 30-day hold. Expiry is never deletion permission."""
    now = _time(now)
    return [{'table': r.table, 'id': r.id, 'state': 'proposed',
             'hold_until': (now + timedelta(days=30)).isoformat()}
            for r, _, tier, links, protected in _states(rows, now)
            if tier == 'deep' and not protected and r.status == 'active' and r.table in ('facts', 'faults')
            and not links and not r.extra.get('refs')
            and not any(u.get('who') == 'owner' for u in r.extra.get('usage', ()))
            and now - _time(r.knowledge_time) > timedelta(days=180)]


def deep_search(store, query, budget_ms):
    """Explicit local recall of far/deep rows, matching literal labels or text.

    The caller owns the store's scope and any outbound release filtering.
    No store writes, index builds or unbounded current()/metadata reads.
    """
    deadline = time.perf_counter() + _budget(budget_ms) / 1000
    needle, hits = query.strip().lower(), []
    if not needle:
        return hits
    for table, tier in ((t, tier) for tier in ('deep', 'far') for t in TABLES):
        after = 0
        while time.perf_counter() < deadline:
            batch = store.current_batch(table, after=after, limit=64, tiers=(tier,), metadata=False)
            if not batch:
                break
            for row in batch:
                if time.perf_counter() >= deadline:
                    break
                if needle in row.hay:
                    hits.append(row)
            after = batch[-1].id
        if time.perf_counter() >= deadline:
            break
    return hits
