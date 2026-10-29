"""Frozen T12 synthetic labels and never-delete guarantees."""
import ast
import hashlib
import inspect
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kyra_memory import inbox
from kyra_memory.activation import activation, components
from kyra_memory.assembler import _snapshot, assemble, assemble_pack
from kyra_memory.store import TABLES, MemoryStore, ScopeError

NOW = datetime(2026, 10, 26, tzinfo=UTC)
PUBLIC = (1, [])


def stamp(days):
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def store(tmp_path):
    st = MemoryStore(tmp_path, 'default', create=True, clock=lambda: stamp(3650))
    st.add('facts', 'synthetic quartz old body marker', source='synthetic', label=PUBLIC)
    st._clock = lambda: stamp(0.5)
    st.add('facts', 'synthetic quartz new body marker', source='synthetic', label=PUBLIC)
    for table in ('rules', 'pinned'):
        st.add(table, f'synthetic {table} marker', source='owner', label=PUBLIC)
    st._clock = lambda: stamp(3650)
    st.add('facts', 'synthetic important marker', source='owner', label=PUBLIC, owner_important=True)
    yield st
    st.close()


def snapshot(st):
    return {(t, r.id, r.status) for t in TABLES for r in st.all_rows(t)}


def test_guarantee_one_pure_and_unchanged_rows(store, tmp_path):
    before = snapshot(store)
    db_before = store._db.total_changes
    assemble_pack(tmp_path, 'builders', 'quartz', activation=True, record=False, scope='default')
    assert snapshot(store) == before
    assert len(snapshot(store)) == len(before)
    assert store._db.total_changes == db_before
    import kyra_memory.activation as module
    tree = ast.parse(inspect.getsource(module))
    imports = [n.module if isinstance(n, ast.ImportFrom) else n.names[0].name
               for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert set(imports) == {'math', 'datetime'}


def test_guarantee_two_protected_active_at_one(store, tmp_path):
    rows, _ = _snapshot(tmp_path, 'default', activation=True)
    pack = assemble(rows, 'important', activation=True, now=NOW)
    protected = [r for t in TABLES for r in rows[t]
                 if t in ('rules', 'pinned') or r.extra.get('owner_important')]
    assert len(protected) == 3
    for r in protected:
        assert activation({**r.extra, 'kind': r.table, 'created': r.knowledge_time}, NOW) == 1.0
        assert store.get(r.table, r.id).status == 'active'
        assert (r.table, r.id) in pack.items


def test_guarantee_three_no_delete_imports_and_file_hash(store, tmp_path):
    store._db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    assemble_pack(tmp_path, 'builders', 'quartz', record=False, activation=True, scope='default')
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_guarantee_four_log_ids_only_after_policy_filter(store, tmp_path):
    rid = store.add('facts', 'restricted body marker', source='synthetic', label=(2, ['health']))

    class Deny:
        def allows(self, classes):
            return False

    pack = assemble_pack(tmp_path, 'builders', 'quartz', policy=Deny(), record=False, activation=True, scope='default')
    entries = [json.loads(line) for line in (tmp_path / 'default.fade.jsonl').read_text().splitlines()]
    assert entries
    assert all(set(e) == {'id', 'tbl', 'B', 'L', 'floor', 'A', 'why'} and e['A'] < .3 for e in entries)
    assert len(entries) == len({(e['tbl'], e['id']) for e in entries})
    assert ('facts', rid) not in pack.items
    assert ('facts', rid) not in {(e['tbl'], e['id']) for e in entries}
    assert 'body' not in (tmp_path / 'default.fade.jsonl').read_text()


def test_guarantee_five_one_exemption_and_exact_off_order(store, tmp_path):
    rows, _ = _snapshot(tmp_path, 'default', activation=True)
    baseline = assemble(rows, 'quartz')
    # Golden pre-change lexical order: tied facts keep ascending id.
    assert baseline.items == (('rules', 1), ('pinned', 1), ('facts', 1), ('facts', 2))
    # Section headers are a host parameter, so compare body lines only.
    assert [line for line in baseline.text.splitlines() if line.startswith('- ')] == ['- synthetic rules marker', '- synthetic pinned marker', '- synthetic quartz old body marker', '- synthetic quartz new body marker']
    enabled = assemble(rows, 'quartz', activation=True, now=NOW)
    assert enabled.items[-2:] == (('facts', 2), ('facts', 1))
    modified = {**rows, 'facts': [replace(r, extra={**r.extra, 'fade_exempt': True}) if r.id == 1 else r
                                for r in rows['facts']]}
    for before, after in zip(rows['facts'], modified['facts'], strict=True):
        a = activation({**before.extra, 'created': before.knowledge_time}, NOW)
        b = activation({**after.extra, 'created': after.knowledge_time}, NOW)
        assert b == (1.0 if before.id == 1 else a)
    assert assemble(modified, 'quartz', activation=True, now=NOW).items == baseline.items
    assert assemble(modified, 'quartz', activation=False) == baseline
    # A flag persisted for one row survives a new reader.
    with store.transaction() as db:
        db.execute('UPDATE facts SET fade_exempt = 1 WHERE id = 1')
    assert assemble_pack(tmp_path, 'builders', 'quartz', activation=True, record=False, scope='default').items == baseline.items


def test_recall_weights_window_burst_and_creation():
    item = {'created': stamp(3650)}
    original = components(item, NOW)
    exposures = [{'at': stamp(i), 'who': 'agent'} for i in range(100)]
    assert components({**item, 'usage': exposures}, NOW) == original
    owner = {'at': stamp(1), 'who': 'owner'}
    outcome = {**owner, 'who': 'outcome'}
    assert activation({**item, 'usage': [owner]}, NOW) > activation({**item, 'usage': [outcome]}, NOW) > original['A']
    assert components({**item, 'usage': [{'at': item['created'], 'who': 'owner'}]}, NOW) == original
    burst = [{'at': stamp(1 + i / 100), 'who': 'owner'} for i in range(12)]
    assert components({**item, 'usage': burst}, NOW) == components({**item, 'usage': [owner]}, NOW)
    many = [{'at': stamp(i), 'who': 'owner'} for i in range(1, 80)]
    assert components({**item, 'usage': many[::-1]}, NOW) == components({**item, 'usage': many[:50]}, NOW)
    assert components({**item, 'links': 50, 'superseded': True}, NOW)['L'] == .5
    assert components({**item, 'weight': 1.25}, NOW)['floor'] == .6


def test_legacy_read_only_and_writer_migration(tmp_path):
    st = MemoryStore(tmp_path, 'default', create=True, clock=lambda: stamp(3650))
    st.add('facts', 'synthetic quartz', source='synthetic', label=PUBLIC)
    st.close()
    with sqlite3.connect(tmp_path / 'default.sqlite3') as db:
        for t in TABLES:
            db.execute(f'DROP TRIGGER {t}_owner_recall')
            db.execute(f'DROP TRIGGER {t}_protect_retrieval')
            db.execute(f'DROP INDEX {t}_retrieval')
            db.execute(f'ALTER TABLE {t} DROP COLUMN retrieval_tier')
        db.execute('ALTER TABLE usage DROP COLUMN who')
        db.execute("INSERT INTO usage VALUES ('builders', 'facts', 1, ?)", (stamp(.1),))
        for t in TABLES:
            for flag in ('fade_exempt', 'owner_important'):
                db.execute(f'ALTER TABLE {t} DROP COLUMN {flag}')
    before = (tmp_path / 'default.sqlite3').read_bytes()
    assert assemble_pack(tmp_path, 'builders', 'quartz', activation=True, record=False, scope='default').items == (('facts', 1),)
    assert assemble_pack(tmp_path, 'builders', 'quartz', activation=True, record=False,
                         scope='default', budget_ms=30).items == (('facts', 1),)
    assert (tmp_path / 'default.sqlite3').read_bytes() == before
    st = MemoryStore(tmp_path, 'default')
    assert st.recall_metadata()[('facts', 1)]['usage'][0]['who'] == 'agent'
    assert st.get('facts', 1).extra['fade_exempt'] == 0
    assert st.get('facts', 1).extra['retrieval_tier'] == 'near'
    from kyra_memory.tiers import retier

    retier(st, NOW)
    assert st.get('facts', 1).extra['retrieval_tier'] == 'deep'
    assert st.get('facts', 1).label == (PUBLIC[0], frozenset(PUBLIC[1]))
    st.close()


def test_pack_exposure_and_confirmed_outcome(store, tmp_path):
    # Even a team called owner building a pack is exposure, not an owner recall.
    assemble_pack(tmp_path, 'owner', 'quartz', activation=True, scope='default')
    inbox.merge(store, tmp_path)
    assert {r[0] for r in store._db.execute('SELECT who FROM usage')} == {'agent'}
    store.record_outcome(1, win=True)
    store.record_outcome(2, win=False)
    assert list(map(tuple, store._db.execute("SELECT item_id, who FROM usage WHERE who = 'outcome'"))) == [(1, 'outcome')]


def test_degree_filters_forbidden_neighbors(store, tmp_path):
    hidden = store.add('facts', 'synthetic restricted', source='synthetic', label=(2, ['health']))
    rows = {t: store.current(t) for t in TABLES}
    rows['facts'][0] = replace(rows['facts'][0], extra={'refs': [('facts', hidden), ('facts', 2), ('facts', 1)]})

    class Deny:
        def allows(self, classes):
            return False

    logs = []
    assemble(rows, 'quartz', activation=True, policy=Deny(), now=NOW, _fade_log=logs)
    part = next(e for e in logs if e['tbl'] == 'facts' and e['id'] == 1)
    assert part['L'] == components({'created': stamp(3650), 'links': 1}, NOW)['L']


def test_enabled_restricted_scope_still_refused(tmp_path):
    with pytest.raises(ScopeError):
        assemble_pack(tmp_path, 'builders', 'synthetic', scope='health', activation=True)

