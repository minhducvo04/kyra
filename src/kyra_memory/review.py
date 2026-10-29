"""Opt-in protection reviews: proposals only until explicit apply().

CORE_SET names every never-fade protection. Monthly reviews snapshot activation
and outcome weight; the first pass establishes a baseline, not a crossing.
Cadences are elapsed 30/365 days, chosen by the caller, with no background job.
Review envelopes use the existing fact-proposal path, with their actual target
and evidence in quote; state='proposed' is the store's proposal status column.
Owner-confirmed downgrades clear flags. For rules/pinned they retain the original
as superseded and add an ordinary fact, preserving its body and two clocks.
"""
import json
from datetime import timedelta

from kyra_memory.activation import _time, activation
from kyra_memory.store import TABLES

CORE_SET = frozenset({'rules', 'pinned', 'owner_important', 'fade_exempt'})
_INTERVALS = {'monthly': 30, 'yearly': 365}
_PROMOTE = 'promote to owner_important'
_DOWNGRADE = 'candidate for downgrade: owner confirmation required'


def _core(row):
    return row.table in CORE_SET or any(row.extra.get(flag) for flag in CORE_SET - {'rules', 'pinned'})


def due(store, now, cadence='off') -> list[str]:
    """Return due modes, in monthly/yearly order. Off does not even read the store."""
    if cadence == 'off':
        return []
    if cadence not in {*_INTERVALS, 'both'}:
        raise ValueError('cadence must be monthly, yearly, both or off')
    now = _time(now)
    modes = _INTERVALS if cadence == 'both' else (cadence,)
    return [mode for mode in modes if (last := store.get_meta(f'last_review:{mode}')) is None
            or now - _time(last) >= timedelta(days=_INTERVALS[mode])]


def nightly(root, scope, *, now):
    """Opt-in nightly maintenance: retier first, then any due protection reviews."""
    from kyra_memory.assembler import read_team_policy
    from kyra_memory.store import MemoryStore
    from kyra_memory.tiers import retier

    policy = read_team_policy(root, 'owner')
    if not policy.get('activation', False):
        return {}
    try:
        store = MemoryStore(root, scope)
    except FileNotFoundError:
        return {}
    try:
        counts = retier(store, now)
        for mode in due(store, now, policy.get('review_cadence', 'off')):
            review(store, now=now, mode=mode)
        return counts
    finally:
        store.close()


def _neighbors(rows, metadata):
    links = {key: set() for key in rows}
    for key in rows:
        for ref in metadata.get(key, {}).get('refs', ()):
            if isinstance(ref, str) and ':' in ref:
                table, _, rid = ref.partition(':')
                ref = (table, int(rid)) if rid.isdigit() else ()
            if isinstance(ref, (tuple, list)) and len(ref) == 2:
                target = tuple(ref)
                if target in rows and target != key:
                    links[key].add(target)
                    links[target].add(key)
    return links


def review(store, *, now, mode) -> list[dict]:
    """Atomically propose reviews and save a per-mode checkpoint; no memory row writes."""
    if mode not in _INTERVALS:
        raise ValueError('mode must be monthly or yearly')
    now = _time(now)
    with store.transaction() as db:
        last = store.get_meta(f'last_review:{mode}')
        if last and now < _time(last):
            raise ValueError('review time precedes last review')
        rows = {(t, r.id): r for t in TABLES for r in store.current(t)}
        metadata, weights = store.recall_metadata(), store.weights()
        links = _neighbors(rows, metadata)
        core = {key for key, row in rows.items() if _core(row)}
        prior = json.loads(store.get_meta('review:monthly:snapshot') or '{}')
        snapshot, proposals = {}, []
        for key, row in rows.items():
            usage = [u for u in metadata.get(key, {}).get('usage', ()) if _time(u['at']) <= now]
            recalls = sorted({_time(u['at']) for u in usage if u['who'] == 'owner'})
            age = max(0.0, (now - _time(row.knowledge_time)).total_seconds() / 86400)
            evidence = {'last_owner_recall': recalls[-1].isoformat() if recalls else None,
                        'links': [list(ref) for ref in sorted(links[key])], 'age_days': age}
            actions = []
            if mode == 'yearly' and key in core:
                actions.append('confirm still core')
                silent = not recalls or now - recalls[-1] >= timedelta(days=365)
                if age >= 365 and silent and not links[key]:
                    actions.append(_DOWNGRADE)
            elif mode == 'monthly':
                weight = weights.get(row.id, 1.0) if row.table == 'facts' else 1.0
                score = activation({**row.extra, 'kind': row.table, 'created': row.knowledge_time,
                                    'usage': usage, 'links': len(links[key]), 'weight': weight}, now)
                token = f'{row.table}:{row.id}'
                snapshot[token] = [score, weight]
                old = prior.get(token)
                if key not in core and old and ((old[0] >= .3) != (score >= .3)
                                               or (old[1] > 1.1) != (weight > 1.1)):
                    recent = sum(now - at <= timedelta(days=60) for at in recalls)
                    actions.append(_PROMOTE if recent >= 2 or links[key] & core else 'note: faded')
                    evidence.update(activation_before=old[0], activation_now=score,
                                    weight_before=old[1], weight_now=weight, owner_recalls_60d=recent)
            for action in actions:
                proposal = {'target': list(key), 'action': action, 'evidence': evidence, 'mode': mode}
                pid = store.add_proposal_in(db, {
                    'table': 'facts', 'body': action, 'source': 'memory-review', 'origin': 'memory-review',
                    'label': row.label, 'refs': [list(key)], 'quote': json.dumps(proposal),
                    'at': now.isoformat()}, 'memory-review')
                proposals.append({**proposal, 'id': pid, 'state': 'proposed'})
        if mode == 'monthly':
            db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                       ('review:monthly:snapshot', json.dumps(snapshot)))
        db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (f'last_review:{mode}', now.isoformat()))
    return proposals


def apply(store, proposal_id, *, owner_confirmed: bool):
    """Accept a promotion or explicitly owner-confirmed downgrade; reject notes/replays."""
    with store.transaction() as db:
        proposal = next((p for p in store.proposals() if p['id'] == proposal_id), None)
        if proposal is None or proposal['origin'] != 'memory-review':
            raise ValueError('not a pending memory review proposal')
        payload = json.loads(proposal['quote'])
        action = payload['action']
        if action not in (_PROMOTE, _DOWNGRADE) or proposal['body'] != action:
            raise ValueError('only a promotion or downgrade can be applied')
        if action == _DOWNGRADE and owner_confirmed is not True:
            raise PermissionError('downgrade requires owner confirmation')
        table, rid = payload['target']
        row = store.get(table, rid) if table in TABLES else None
        if row is None or row.status != 'active':
            raise ValueError('review target is no longer active')
        if action == _PROMOTE:
            db.execute(f'UPDATE {table} SET owner_important = 1 WHERE id = ?', (rid,))
        elif table in ('rules', 'pinned'):
            new_id = store.add_in(db, 'facts', row.body, source=row.source, topic=row.topic,
                                  event_time=row.event_time, label=row.label)
            # Do not manufacture freshness, supersede keyed facts, or erase the original.
            db.execute('UPDATE facts SET knowledge_time = ?, key = ? WHERE id = ?',
                       (row.knowledge_time, row.key, new_id))
            db.execute(f"UPDATE {table} SET status = 'superseded', ended_at = ? WHERE id = ?",
                       (store._clock(), rid))
            table, rid = 'facts', new_id
        else:
            db.execute(f'UPDATE {table} SET owner_important = 0, fade_exempt = 0 WHERE id = ?', (rid,))
        # fact_id stays NULL: review acceptance must not replace the target's graph refs.
        db.execute("UPDATE proposals SET state = 'accepted' WHERE id = ?", (proposal_id,))
    return table, rid
