"""Tier, bounded recall and proposal contracts on synthetic rows."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kyra_memory import tiers
from kyra_memory.assembler import assemble
from kyra_memory.labels import Tier
from kyra_memory.store import TABLES, MemoryStore, Row

NOW = datetime(2026, 10, 27, tzinfo=UTC)


def row(rid, days=36500, table='facts', **extra):
    stamp = (NOW - timedelta(days=days)).isoformat()
    return Row(table, rid, f'synthetic marker {rid}', '', None, 'synthetic', stamp, stamp,
               (Tier.T1, frozenset()), 'active', None, None, None, extra)


@pytest.mark.parametrize('value,expected', [(0, 'deep'), (.2, 'deep'), (.2999, 'deep'), (.3, 'far'), (.4999, 'far'), (.5, 'near'), (1, 'near')])
def test_thresholds(value, expected):
    assert tiers.tier_of(value) == expected
    assert tiers.tier_of(value, protected=True) == 'near'


def test_protected_and_budget_gates():
    rows = [row(1, .1), row(2, 100), row(3)]
    protected = [row(4, table='rules'), row(5, table='pinned'), row(6, owner_important=True),
                 row(7, fade_exempt=True), row(8, important=True)]
    rows += protected
    rows = [replace(r, extra={**r.extra, 'retrieval_tier': tier})
            for r, _, tier, _, _ in tiers._states(rows, NOW)]
    for budget, ids in [(0, [1]), (49, [1]), (50, [1, 2]), (199, [1, 2]), (200, [1, 2, 3])]:
        assert [r.id for r in tiers.budgeted_candidates(rows, budget, NOW)] == ids + [4, 5, 6, 7, 8]
    groups = {t: [r for r in rows if r.table == t] for t in TABLES}
    off = assemble(groups, 'marker', budget_ms=0)
    assert ('facts', 3) in off.items
    assert ('facts', 3) not in assemble(groups, 'marker', budget_ms=0, activation=True, now=NOW).items
    assert ('facts', 3) not in assemble(groups, 'marker', hot=[rows[2]], budget_ms=0, activation=True, now=NOW).items


def test_discard_is_only_a_held_proposal():
    rows = [row(1), row(2, table='faults'), row(3, table='recipes'), row(4, table='rules'),
            row(5, table='pinned'), row(6, fade_exempt=True), row(7, owner_important=True),
            row(8, usage=[{'who': 'owner', 'at': row(8).knowledge_time}]), row(9, refs=[('facts', 10)]),
            row(10), row(11, refs=['synthetic-file']), row(12, 180), row(13, links=1),
            row(14, weight=1.2)]
    proposals = tiers.discard_candidates(rows, NOW)
    assert [(p['table'], p['id']) for p in proposals] == [('facts', 1), ('faults', 2)]
    assert all(p['state'] == 'proposed' and p['hold_until'] == (NOW + timedelta(days=30)).isoformat()
               for p in proposals)
    assert all(r.status == 'active' for r in rows)


def test_map_fields_and_links():
    rows = [row(1), row(2, fade_exempt=True), row(3, refs=[('facts', 1)])]
    fields = tiers.map_items(rows, NOW)
    assert fields[0]['quiet'] and fields[0]['tier'] == 'deep'
    assert fields[1]['quiet'] is False and fields[1]['tier'] == 'near'
    assert 'body' not in fields[0]
    assert tiers.discard_candidates(rows, NOW) == []


@pytest.fixture
def thousand_store(tmp_path):
    store = MemoryStore(tmp_path, 'default', create=True, clock=lambda: row(1).knowledge_time)
    with store.transaction() as db:
        for i in range(1000):
            store.add_in(db, 'facts', f'synthetic buried-label-{i:04}', source='synthetic', label=(1, []))
    tiers.retier(store, NOW)
    yield store
    store.close()


def test_deep_search_finds_buried_exact_label(thousand_store):
    hits = tiers.deep_search(thousand_store, 'buried-label-0999', 5000)
    assert [r.body for r in hits] == ['synthetic buried-label-0999']
    assert thousand_store.counts()['facts'] == 1000


def test_deep_search_stops_between_batches(thousand_store, monkeypatch):
    ticks = [0.0]
    original = thousand_store.current_batch
    batches = []

    def batch(*args, **kwargs):
        result = original(*args, **kwargs)
        if result:
            batches.append(len(result))
            ticks[0] += .011
        return result

    monkeypatch.setattr(thousand_store, 'current_batch', batch)
    monkeypatch.setattr(tiers.time, 'perf_counter', lambda: ticks[0])
    assert tiers.deep_search(thousand_store, 'buried-label-0999', 10) == []
    assert batches == [64]
    batches.clear()
    assert tiers.deep_search(thousand_store, 'buried-label-0999', 0) == []
    assert batches == []


def test_recall_and_weight_metadata_stay_near(thousand_store):
    with thousand_store.transaction() as db:
        db.execute("INSERT INTO usage (team, tbl, item_id, at, who) VALUES ('owner', 'facts', 1000, ?, 'owner')",
                   (datetime.now(UTC).isoformat(),))
    thousand_store.set_weights({999: 1.2})
    tiers.retier(thousand_store, datetime.now(UTC))
    assert tiers.deep_search(thousand_store, 'buried-label-0999', 5000) == []
    assert tiers.deep_search(thousand_store, 'buried-label-0998', 5000) == []
    assert tiers.deep_search(thousand_store, 'buried-label-0997', 5000)


def test_batch_links_include_both_ends_across_batches(thousand_store):
    with thousand_store.transaction() as db:
        pid = thousand_store.add_proposal_in(db, {
            'table': 'facts', 'body': 'synthetic linked source', 'source': 'synthetic', 'label': (1, []),
            'at': NOW.isoformat(), 'refs': [('facts', 1), 'facts:2', ('facts', 999999)]}, 'builders')
    _, rid = thousand_store.accept(pid)
    first = thousand_store.current_batch('facts', limit=64)
    last = thousand_store.current_batch('facts', after=1000)
    assert first[0].extra['links'] == first[1].extra['links'] == 1
    assert last[0].id == rid and last[0].extra['links'] == 2


def test_fixback_ten_year_retier_and_owner_recall(tmp_path):
    store = MemoryStore(tmp_path, 'default', create=True, clock=lambda: row(1, 3650).knowledge_time)
    try:
        rid = store.add('facts', 'synthetic ten year marker', source='synthetic', label=(1, []))
        protected = store.add('facts', 'synthetic protected marker', source='synthetic', label=(1, []),
                              owner_important=True)
        tiers.retier(store, NOW)
        assert store.get('facts', rid).extra['retrieval_tier'] == 'deep'
        assert store.get('facts', protected).extra['retrieval_tier'] == 'near'
        assert tiers.discard_candidates(store.current_batch('facts'), NOW)[0]['id'] == rid
        with store.transaction() as db:
            db.execute("INSERT INTO usage (team, tbl, item_id, at, who) VALUES ('owner', 'facts', ?, ?, 'owner')",
                       (rid, NOW.isoformat()))
        assert store.get('facts', rid).extra['retrieval_tier'] == 'near'
        tiers.retier(store, NOW)
        assert store.get('facts', rid).extra['retrieval_tier'] == 'near'
    finally:
        store.close()


def test_fixback_quick_does_not_score_cold_rows(tmp_path, monkeypatch):
    import importlib
    import time

    from kyra_memory import assembler

    act = importlib.import_module('kyra_memory.activation')
    store = MemoryStore(tmp_path, 'default', create=True, clock=lambda: row(1, 3650).knowledge_time)
    try:
        with store.transaction() as db:
            for i in range(998):
                store.add_in(db, 'facts', f'synthetic buried-label-{i:04}', source='synthetic', label=(1, []))
        store._clock = lambda: NOW.isoformat()
        near = store.add('facts', 'synthetic near marker', source='synthetic', label=(1, []))
        store._clock = lambda: row(1, 100).knowledge_time
        far = store.add('facts', 'synthetic far marker', source='synthetic', label=(1, []))
        tiers.retier(store, NOW)
        assert store.get('facts', far).extra['retrieval_tier'] == 'far'
        calls = []
        original_activation, original_components = act.activation, act.components

        def counted(item, now):
            calls.append(item['created'])
            assert item['created'] == NOW.isoformat()
            return original_activation(item, now)

        def components(item, now):
            assert item['created'] == NOW.isoformat()
            return original_components(item, now)

        monkeypatch.setattr(tiers, 'activation', counted)
        monkeypatch.setattr(act, 'activation', counted)
        monkeypatch.setattr(assembler, 'components', components)
        started = time.perf_counter()
        pack = assembler.assemble_pack(tmp_path, 'owner', 'marker', record=False,
                                       activation=True, budget_ms=30, scope='default')
        elapsed = (time.perf_counter() - started) * 1000
        assert pack.items == (('facts', near),)
        assert elapsed < 30
        assert len(calls) <= 1
        started = time.perf_counter()
        hits = tiers.deep_search(store, 'buried-label-0997', 2000)
        deep_ms = (time.perf_counter() - started) * 1000
        assert [r.body for r in hits] == ['synthetic buried-label-0997']
        assert deep_ms < 2000
        monkeypatch.undo()
        normal = assembler.assemble_pack(tmp_path, 'owner', 'marker', record=False,
                                         activation=True, budget_ms=100, scope='default')
        assert set(normal.items) == {('facts', near), ('facts', far)}
        with store.transaction() as db:
            db.execute("INSERT INTO usage (team, tbl, item_id, at, who) VALUES ('owner', 'facts', 1, ?, 'owner')",
                       (NOW.isoformat(),))
        recalled = assembler.assemble_pack(tmp_path, 'owner', 'buried-label-0000', record=False,
                                           activation=True, budget_ms=30, scope='default')
        assert ('facts', 1) in recalled.items
        print(f'fixback quick_ms={elapsed:.3f} deep_ms={deep_ms:.3f} rows={sum(store.counts().values())}')
    finally:
        store.close()


def test_nightly_retier_respects_policy(tmp_path):
    import json

    from kyra_memory.review import nightly

    store = MemoryStore(tmp_path, 'default', create=True, clock=lambda: row(1, 3650).knowledge_time)
    try:
        rid = store.add('facts', 'synthetic nightly marker', source='synthetic', label=(1, []))
        assert nightly(tmp_path, 'default', now=NOW) == {}
        assert store.get('facts', rid).extra['retrieval_tier'] == 'near'
        (tmp_path / 'teams').mkdir()
        (tmp_path / 'teams' / 'owner.policy.json').write_text(json.dumps({'activation': True, 'review_cadence': 'both'}))
        assert nightly(tmp_path, 'default', now=NOW) == {'near': 0, 'far': 0, 'deep': 1}
        assert store.get('facts', rid).extra['retrieval_tier'] == 'deep'
        assert store.get_meta('last_review:monthly') and store.get_meta('last_review:yearly')
        with store.transaction() as db:
            db.execute('UPDATE facts SET fade_exempt = 1 WHERE id = ?', (rid,))
        assert store.get('facts', rid).extra['retrieval_tier'] == 'near'
    finally:
        store.close()
