"""Review o-rev80 probes for auto-system R14 (picker.rank in the dispatcher), ported after the fix-back.

Order on every path: picker.rank, then dispatch.budget_table as the last step before select.
Synthetic tmp data only; no real sends, launches or network.
"""

import json

import pytest

from companion.feature_team import autopilot, dispatch, picker

PAUSE = {'claude': {'paused_until': '2030-01-01T00:00:00Z', 'reason': 'Claude limit warning'}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def row(key, b, f):
    return {'key': key, 'bounce_per_landed': b, 'fix_first_per_landed': f}


@pytest.fixture(autouse=True)
def no_capacity_or_launch(monkeypatch):
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])


def world(tmp_path, team, table, nodes=1, intern=()):
    """One feature, `nodes` engineer nodes (and intern-friendly ones in `intern`), every lane idle."""
    data = tmp_path / 'data'
    folder = data / 'team/features/demo'
    write(folder / 'team.json', team)
    write(folder / 'completion.json', {'nodes': {}})
    ids = [f'N{i}' for i in range(nodes)]
    dag = [dict(id=n, title='Synthetic work', inputs=[], intern_friendly=n in intern, tier='T1',
                acceptance='check', files=[], kind='code') for n in ids]
    write(data / 'team/feature-teams/demo.dag.json', {'owner_said': 'Synthetic.', 'nodes': dag})
    (folder / 'queue-engineer.jsonl').write_text(''.join(
        json.dumps({'node': n, 'priority': i, 't': i}) + '\n' for i, n in enumerate(ids) if n not in intern))
    (folder / 'queue-intern.jsonl').write_text(''.join(
        json.dumps({'node': n, 'priority': i, 't': i}) + '\n' for i, n in enumerate(ids) if n in intern))
    write(data / 'team/dispatch-table.json', table)
    write(data / 'team/lane-watch.json', {'observed_at': 100, 'lanes': [
        {'channel': r['channel'], 'state': 'idle'} for r in table]})
    write(data / 'team/effort-policy.json', {'default': 'high'})
    return data, folder


def opus_first_world(tmp_path):
    """Opus on a Claude lane ranked best by rollback; Codex worst and listed second."""
    team = [dict(name='Claude Opus', role='Engineer', channel='claude:opus', effort='high'),
            dict(name='Cursor', role='Engineer', channel='codex:2', effort='high')]
    table = [dict(role='engineer', channel='codex:2', family='codex'),
             dict(role='engineer', channel='claude:opus', family='claude-opus')]
    data, folder = world(tmp_path, team, table)
    write(data / 'team/rollback.json', {'rows': [row('Claude Opus', 0.0, 0.0), row('Cursor', 0.5, 0.5)]})
    return data, folder


def spy(monkeypatch):
    seen, real = [], picker.rank

    def rank(table, features, path=picker.ROLLBACK):
        seen.append([r['channel'] for r in table])
        return real(table, features, path)

    monkeypatch.setattr(picker, 'rank', rank)
    return seen


def launches(monkeypatch):
    out = []
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: out.append(
        dispatch.loop_store(root).load(run_id)['brief']['channel']))
    return out


# 1. The owner's 2026-10-12 rule: the pause filters before rank, on each sending path.

def test_control_unpaused_opus_is_ranked_first_and_launched(tmp_path, monkeypatch):
    """Control for the pause probes: without a pause, rank really puts claude:opus first."""
    data, _ = opus_first_world(tmp_path)
    seen, launched, sent = spy(monkeypatch), launches(monkeypatch), []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert seen and seen[0] == ['codex:2', 'claude:opus']
    assert launched == ['claude:opus'] and sent == []


def test_paused_claude_never_reaches_a_send_on_plan_next(tmp_path, monkeypatch):
    """rank sees the paused row and puts it first; budget_table, the last step, still removes it."""
    data, _ = opus_first_world(tmp_path)
    write(data / 'team/model-budget.json', PAUSE)
    seen, launched, sent = spy(monkeypatch), launches(monkeypatch), []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert seen
    assert launched == [] and sent == ['codex:2']


def test_paused_claude_never_sent_on_the_autopilot_path(tmp_path, monkeypatch):
    data, _ = opus_first_world(tmp_path)
    write(data / 'team/model-budget.json', PAUSE)
    write(data / 'team/feature-queue.json', {'features': [
        {'name': 'demo', 'dag': 'data/team/feature-teams/demo.dag.json'}]})
    launched, sent = launches(monkeypatch), []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert launched == [] and sent == ['codex:2']


def test_paused_claude_blocks_every_claude_family_and_rank_adds_nothing():
    table = [dict(role='engineer', channel=f'claude:{m}', family=f'claude-{m}') for m in ('fable', 'opus', 'sonnet')]
    table.append(dict(role='engineer', channel='claude:opus-2'))  # no family: provider from the channel
    skipped = {}
    eligible = dispatch.budget_table(table, {'claude': 'limit'}, skipped)
    assert eligible == [] and picker.rank(eligible, []) == []
    # The send-boundary and retry checks build a family-less entry; the provider key still refuses it.
    assert dispatch.budget_table([dict(channel='claude:opus', role='engineer')], {'claude': 'limit'}, {}) == []


def test_rank_returns_exactly_the_rows_it_was_given(tmp_path):
    table = [dict(role='engineer', channel=c) for c in ('codex:2', 'claude:opus', 'cursor:/x')]
    write(tmp_path / 'rb.json', {'rows': [row('Claude Opus', 0, 0), row('Codex', 1, 1)]})
    for subset in (table, table[:1], table[::2], []):
        ranked = picker.rank(subset, [], tmp_path / 'rb.json')
        assert sorted(map(id, ranked)) == sorted(map(id, subset))


def test_paused_claude_retry_of_a_timed_out_run_is_refused(tmp_path, monkeypatch):
    from companion.feature_team import contract, runs
    from companion.feature_team.events import EventStore

    data, _ = opus_first_world(tmp_path)
    now = [1_800_000_000.0]
    store = runs.Runs(data / 'team/loop', emit=EventStore(data).append, clock=lambda: now[0])
    brief = contract.brief_from_node(
        dict(id='N0', title='Synthetic work', files=['fixture.py'], inputs=[], acceptance='check'),
        run_id='demo-N0-first', feature='demo', channel='claude:opus', model='claude-opus-5-5',
        effort='high', base='master', worktree=str(tmp_path / 'w'), inputs={},
        deadline=now[0] + 5400, result=str(data / 'first.json'), rules='AGENTS.md')
    store.create(brief)
    store.transition('demo-N0-first', 'dispatched', deadline=brief['deadline'])
    store.transition('demo-N0-first', 'started')
    write(data / 'team/model-budget.json', PAUSE)
    launched = launches(monkeypatch)
    now[0] = brief['deadline']
    result = dispatch.tick(data, sender=lambda _: pytest.fail('legacy send'), clock=lambda: now[0])
    assert launched == []
    assert any('model budget' in ' '.join(r['reasons']) for r in result['loop']['refusals'])


# 2. Channel keys (the inspector's muse:0 style pauses).

def test_channel_key_pause_is_filtered_before_rank_once_budget_table_honours_it(tmp_path, monkeypatch):
    """rank only reorders budget_table's output, so a channel key holds once budget_table honours it. The inspector
    (49ca27b3 and its fix-backs) made the real budget_table honour channel keys, so this now runs on the real one."""
    team = [dict(name='Claude Opus', role='Engineer', channel='codex:1', effort='high'),
            dict(name='Cursor', role='Engineer', channel='codex:2', effort='high')]
    table = [dict(role='engineer', channel='codex:2'), dict(role='engineer', channel='codex:1')]
    data, _ = world(tmp_path, team, table)
    write(data / 'team/rollback.json', {'rows': [row('Claude Opus', 0, 0), row('Cursor', 1, 1)]})
    write(data / 'team/model-budget.json', {'codex:1': {'paused_until': '2030-01-01T00:00:00Z', 'reason': 'dead'}})
    assert dispatch.budget_table(table, {'codex:1': 'dead'}, {}) == [table[0]]  # the channel key is honoured
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert sent == ['codex:2']


# 3. Behaviour: starvation, Fable soft preference, local class rule, autopilot.

def test_median_lane_still_gets_work_when_ranked_lanes_are_taken(tmp_path):
    team = [dict(name='Claude Opus', role='Engineer', channel='codex:1', effort='high'),
            dict(name='Nobody', role='Engineer', channel='codex:2', effort='high'),
            dict(name='Cursor', role='Engineer', channel='codex:3', effort='high')]
    table = [dict(role='engineer', channel=f'codex:{i}') for i in (3, 2, 1)]
    data, _ = world(tmp_path, team, table, nodes=2)
    write(data / 'team/rollback.json', {'rows': [row('Claude Opus', 0, 0), row('Cursor', 1, 1),
                                                 row('Grok', 0.5, 0.5)]})
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert sent == ['codex:1', 'codex:2']  # best, then the no-data lane at the median, worst stays idle


def test_local_class_rule_survives_a_first_ranked_local_channel(tmp_path):
    from companion.local_lane import CLASSES
    if not CLASSES:
        pytest.skip('local:* channels need the private local task catalogue, not shipped in the public export')
    team = [dict(name='Claude Opus', role='Intern', channel='local:file_triage', effort='low'),
            dict(name='Claude Opus', role='Engineer', channel='local:log_reading', effort='low'),
            dict(name='Cursor', role='Intern', channel='local:log_reading', effort='low')]
    table = [dict(role='intern', channel='local:log_reading'), dict(role='engineer', channel='local:log_reading'),
             dict(role='intern', channel='local:file_triage')]
    data, _ = world(tmp_path, team, table, nodes=1, intern=('N0',))
    dag = data / 'team/feature-teams/demo.dag.json'
    doc = json.loads(dag.read_text())
    doc['nodes'][0]['class'] = 'log_reading'
    write(dag, doc)
    write(data / 'team/rollback.json', {'rows': [row('Claude Opus', 0, 0), row('Cursor', 1, 1)]})
    features = dispatch.snapshot(data)
    ranked = picker.rank(table, features, data / 'team/rollback.json')
    # rank keys by channel (last worker on a channel wins): the wrong-class local:file_triage ranks first.
    assert ranked[0]['channel'] == 'local:file_triage'
    plan = dispatch.select(ranked, [], features, {})
    assert [(p['channel'], p['by']) for p in plan] == [('local:log_reading', 'Cursor')]


def test_rank_keeps_fable_after_the_other_claude_builders(tmp_path):
    """budget_table's soft preference: Fable after other Claude builders survives because rank runs first."""
    team = [dict(name='Claude Fable', role='Engineer', channel='claude:fable'),
            dict(name='Claude Opus', role='Engineer', channel='claude:opus')]
    table = [dict(role='engineer', channel='claude:fable', family='claude-fable'),
             dict(role='engineer', channel='claude:opus', family='claude-opus')]
    assert [r['channel'] for r in dispatch.budget_table(table, {}, {})] == ['claude:opus', 'claude:fable']
    write(tmp_path / 'rb.json', {'rows': [row('Claude Fable', 0.0, 0.0), row('Claude Opus', 0.1, 0.0)]})
    assert [r['channel'] for r in picker.rank(table, [{'team': team}], tmp_path / 'rb.json')] == [
        'claude:fable', 'claude:opus']  # rank alone would put Fable first
    ranked = dispatch.budget_table(picker.rank(table, [{'team': team}], tmp_path / 'rb.json'), {}, {})
    assert [r['channel'] for r in ranked] == ['claude:opus', 'claude:fable']


def test_autopilot_path_orders_lanes_by_rank(tmp_path):
    """With data/team/feature-queue.json present, prepare() replaces plan_next with its own plan()."""
    team = [dict(name='Cursor', role='Engineer', channel='codex:1', effort='high'),
            dict(name='Claude Opus', role='Engineer', channel='codex:3', effort='high')]
    table = [dict(role='engineer', channel='codex:1'), dict(role='engineer', channel='codex:3')]
    data, _ = world(tmp_path, team, table)
    write(data / 'team/rollback.json', {'rows': [row('Claude Opus', 0, 0), row('Cursor', 1, 1)]})
    write(data / 'team/feature-queue.json', {'features': [
        {'name': 'demo', 'dag': 'data/team/feature-teams/demo.dag.json'}]})
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert sent == ['codex:3']


# 6. The author's replay test can fail: with rank a no-op it must go red.

def test_author_replay_fails_when_rank_is_a_no_op(tmp_path, monkeypatch):
    from tests import test_feature_team_picker as author

    monkeypatch.setattr(picker, 'rank', lambda table, features, path=None: list(table))
    with pytest.raises(AssertionError):
        author.test_dispatch_replay_routes_three_kinds_by_picker_rank_without_model_calls(tmp_path, monkeypatch)


# 4. rollback.json is read-only input: missing or malformed must fall back to table order.

@pytest.mark.parametrize('content', [None, 'not json', '[]', '{"rows": ["x"]}', '{"rows": 3}',
                                     '{"rows": [{"key": "Cursor", "bounce_per_landed": "n/a",'
                                     ' "fix_first_per_landed": 0}]}', 'dir'])
def test_missing_or_malformed_rollback_falls_back_to_table_order(tmp_path, content):
    team = [dict(name='Cursor', role='Engineer', channel='codex:1', effort='high'),
            dict(name='Claude Opus', role='Engineer', channel='codex:3', effort='high')]
    table = [dict(role='engineer', channel='codex:1'), dict(role='engineer', channel='codex:3')]
    data, _ = world(tmp_path, team, table, nodes=2)
    path = data / 'team/rollback.json'
    if content == 'dir':
        path.mkdir()
    elif content is not None:
        path.write_text(content)
    sent = []
    dispatch.tick(data, sender=lambda item: sent.append(item['channel']) or 'STARTED', clock=lambda: 100)
    assert sent == ['codex:1', 'codex:3']
