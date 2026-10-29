"""Protection reviews use synthetic stores and never touch memory bodies."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from kyra_memory.activation import activation
from kyra_memory.assembler import assemble_pack
from kyra_memory.review import CORE_SET, apply, due, review
from kyra_memory.store import TABLES, MemoryStore

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def stamp(days=0):
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path, 'default', create=True, clock=lambda: stamp())
    yield store
    store.close()


def add(store, table='facts', age=400, **flags):
    store._clock = lambda: stamp(age)
    rid = store.add(table, 'synthetic quartz marker', source='synthetic', label=(1, []), **flags)
    store._clock = lambda: stamp()
    return rid


def recall(store, table, rid, days, who='owner'):
    with store.transaction() as db:
        db.execute('INSERT INTO usage (team, tbl, item_id, at, who) VALUES (?, ?, ?, ?, ?)',
                   ('synthetic', table, rid, stamp(days), who))


def link_from_core(store, target):
    with store.transaction() as db:
        pid = store.add_proposal_in(db, {'table': 'facts', 'body': 'synthetic linked marker',
            'source': 'synthetic', 'label': (1, []), 'refs': [target], 'at': stamp(400)}, 'synthetic')
    _, rid = store.accept(pid)
    with store.transaction() as db:
        db.execute('UPDATE facts SET owner_important = 1 WHERE id = ?', (rid,))
    return rid


def rows(store):
    return {t: store.all_rows(t) for t in TABLES}


def actions(proposals):
    return [(p['target'], p['action']) for p in proposals]


def test_core_and_yearly_evidence_are_proposals_only(memory):
    assert CORE_SET == {'rules', 'pinned', 'owner_important', 'fade_exempt'}
    rule = add(memory, 'rules')
    pinned = add(memory, 'pinned')
    important = add(memory, owner_important=True)
    exempt = add(memory, fade_exempt=True)
    fresh = add(memory, age=20, owner_important=True)
    ordinary = add(memory)
    recall(memory, 'rules', rule, 10)
    recall(memory, 'facts', important, 2, who='agent')
    linked = link_from_core(memory, ['facts', exempt])
    before = rows(memory)
    proposals = review(memory, now=NOW, mode='yearly')
    confirms = {tuple(p['target']) for p in proposals if p['action'] == 'confirm still core'}
    candidates = {tuple(p['target']) for p in proposals if p['action'].startswith('candidate')}
    assert confirms == {('rules', rule), ('pinned', pinned), ('facts', important),
                        ('facts', exempt), ('facts', linked), ('facts', fresh)}
    assert candidates == {('pinned', pinned), ('facts', important)}
    assert ('facts', ordinary) not in confirms
    evidence = next(p['evidence'] for p in proposals if p['target'] == ['rules', rule])
    assert evidence['last_owner_recall'] == stamp(10)
    assert evidence['age_days'] == 400
    assert evidence['links'] == []
    assert rows(memory) == before
    assert all(p['state'] == 'proposed' for p in memory.proposals())
    assert memory.get_meta('last_review:yearly') == stamp()
    assert all(p['action'] != 'candidate for downgrade: owner confirmation required'
               for p in review(memory, now=NOW, mode='monthly'))


def test_monthly_crossings_and_promotion(memory):
    rising = add(memory, age=3650)
    falling = add(memory, age=370)
    weighted = add(memory, age=3650)
    linked = add(memory, age=3650)
    core = add(memory, age=3650, owner_important=True)
    assert review(memory, now=NOW - timedelta(days=30), mode='monthly') == []
    recall(memory, 'facts', rising, 2)
    recall(memory, 'facts', rising, 1)
    memory.set_weights({weighted: 1.2, linked: 1.2})
    link_from_core(memory, ['facts', linked])
    before = rows(memory)
    proposals = review(memory, now=NOW, mode='monthly')
    assert actions(proposals) == [(['facts', rising], 'promote to owner_important'),
        (['facts', falling], 'note: faded'), (['facts', weighted], 'note: faded'),
        (['facts', linked], 'promote to owner_important')]
    assert all(p['target'] != ['facts', core] for p in proposals)
    assert rows(memory) == before
    promote = proposals[0]
    with pytest.raises(ValueError, match='review.apply'):
        memory.accept(promote['id'])
    apply(memory, promote['id'], owner_confirmed=False)
    row = memory.get('facts', rising)
    assert row.extra['owner_important'] == 1
    assert activation({**row.extra, 'created': row.knowledge_time}, NOW) == 1.0
    assert row.body == before['facts'][0].body
    with pytest.raises(ValueError):
        apply(memory, promote['id'], owner_confirmed=True)
    with pytest.raises(ValueError):
        apply(memory, proposals[1]['id'], owner_confirmed=True)
    assert review(memory, now=NOW, mode='monthly') == []
    memory.set_weights({weighted: 1.0})
    assert actions(review(memory, now=NOW + timedelta(days=1), mode='monthly')) == [
        (['facts', weighted], 'note: faded')]


@pytest.mark.parametrize('table,flags', [('facts', {'owner_important': True, 'fade_exempt': True}),
                                        ('rules', {}), ('pinned', {})])
def test_downgrade_requires_owner_confirmation(memory, table, flags):
    rid = add(memory, table, **flags)
    proposals = review(memory, now=NOW, mode='yearly')
    candidate = next(p for p in proposals if p['action'].startswith('candidate'))
    before = rows(memory)
    with pytest.raises(PermissionError):
        apply(memory, candidate['id'], owner_confirmed=False)
    assert rows(memory) == before
    assert len(memory.proposals()) == 2
    new_table, new_id = apply(memory, candidate['id'], owner_confirmed=True)
    row = memory.get(new_table, new_id)
    assert new_table == 'facts'
    assert not row.extra['owner_important'] and not row.extra['fade_exempt']
    assert row.body == before[table][0].body
    assert activation({**row.extra, 'created': row.knowledge_time}, NOW) < 1.0
    if table != 'facts':
        assert memory.get(table, rid).status == 'superseded'
    with pytest.raises(ValueError):
        apply(memory, proposals[0]['id'], owner_confirmed=True)


def test_cadence_and_disabled_pack_have_no_review_writes(memory, tmp_path):
    add(memory, 'rules')
    assert due(memory, NOW) == due(memory, NOW, 'off') == []
    assert due(memory, NOW, 'both') == ['monthly', 'yearly']
    assert due(memory, NOW, 'monthly') == ['monthly']
    assert due(memory, NOW, 'yearly') == ['yearly']
    before = list(memory._db.iterdump())
    baseline = assemble_pack(tmp_path, 'synthetic', 'quartz', record=False, scope='default')
    off = assemble_pack(tmp_path, 'synthetic', 'quartz', record=False, scope='default', review_cadence='off')
    assert (baseline.text, baseline.items) == (off.text, off.items)
    assemble_pack(tmp_path, 'synthetic', 'quartz', record=False, activation=True, review_cadence='off', scope='default')
    assemble_pack(tmp_path, 'synthetic', 'quartz', record=False, review_cadence='both', scope='default')
    assert memory.proposals() == []
    assert list(memory._db.iterdump()) == before
    review(memory, now=NOW, mode='monthly')
    review(memory, now=NOW, mode='yearly')
    assert due(memory, NOW + timedelta(days=29), 'both') == []
    assert due(memory, NOW + timedelta(days=30), 'both') == ['monthly']
    assert due(memory, NOW + timedelta(days=365), 'both') == ['monthly', 'yearly']
    with pytest.raises(ValueError):
        due(memory, NOW, 'daily')
    with pytest.raises(ValueError):
        review(memory, now=NOW, mode='daily')


def test_enabled_pack_runs_due_reviews_once(memory, tmp_path):
    add(memory, 'rules')
    before = rows(memory)
    pack = assemble_pack(tmp_path, 'synthetic', 'quartz', record=False,
                         activation=True, review_cadence='both', scope='default')
    assert pack.items == (('rules', 1),)
    assert memory.proposals()
    assert all(p['origin'] == 'memory-review' for p in memory.proposals())
    assert memory.get_meta('last_review:monthly') and memory.get_meta('last_review:yearly')
    proposals = memory.proposals()
    assemble_pack(tmp_path, 'synthetic', 'quartz', record=False, activation=True, review_cadence='both', scope='default')
    assert memory.proposals() == proposals
    assert rows(memory) == before
    assert json.loads(proposals[0]['quote'])['target'] == ['rules', 1]
