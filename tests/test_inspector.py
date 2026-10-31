"""Inspector: real R0/E0/L0 stores on tmp data; only the clock, the process probe and the commit probe are faked."""

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from companion.feature_team import dispatch, events, inspector, locks, runs, watchdog

NOW = 1_800_000_000.0
BRIEF = json.loads((Path(__file__).parent / 'data/loop/brief-ok.json').read_text())
TEAM = [dict(name='Muse Wonder Full', role='Engineer', channel='muse:0'),
        dict(name='Muse Wonder Full 2', role='Engineer', channel='muse:1'),
        dict(name='Claude Opus', role='Engineer', channel='claude:opus'),
        dict(name='Codex Astra', role='Engineer', channel='codex-exec:1', model='codex-astra'),
        dict(name='Reviewer', role='Reviewer', channel='claude:review')]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {}})
    return root


def store(data, now=NOW):
    return runs.Runs(data / 'team/loop', emit=events.EventStore(data).append, clock=lambda: now)


def launch(data, rid='demo-N1-1', node='N1', *, channel='muse:0', kind='loop_code', started=NOW - 60, pid=4242,
           ledger=True, log_text='', state='started', **fields):
    """A run dispatched at ``started`` with a courier ledger row (pid, log) like lane_send writes it."""
    s = store(data, started)
    model = 'claude-opus-5-5' if channel.startswith('claude') else 'muse-wonder-full-1.0'
    s.create(dict(BRIEF, run_id=rid, node=node, inputs=[], channel=channel, model=model, kind=kind,
                  worktree=str(data.parent / 'wt' / rid), result=str(data / 'team/loop/results' / f'{rid}.json'),
                  deadline=started + 5400))
    s.transition(rid, 'dispatched', deadline=started + 5400)
    ledger_id = f'muse-{rid}'
    if state != 'dispatched':
        s.transition(rid, 'started', detail=f'ledger_id={ledger_id}' if ledger else 'started')
    if ledger:
        log = data.parent / 'logs' / f'{rid}.log'
        write(log, log_text)
        row = dict(id=ledger_id, channel=channel.partition(':')[0], target='wt', sent_at='2027-01-15T08:00:00',
                   receipt=f'muse started pid {pid} in wt (log {log}) ledger_id={ledger_id}')
        if pid:
            row['pid'] = pid
        (data / 'context').mkdir(parents=True, exist_ok=True)
        with (data / 'context/packages.jsonl').open('a') as f:
            f.write(json.dumps(row) + '\n')
    if fields:
        rec = s.load(rid)
        rec.update(fields)
        runs._write_json(s.path(rid), rec)
    return s


def inspect(data, now=NOW, alive=(), commits=0, cls=None):
    cls = cls or inspector.Inspector
    s = store(data, now)
    return cls(data, store=s, now=now, alive=lambda pid: pid in alive, commits=lambda rec: commits,
               emit=events.EventStore(data).append).run(s.load_all())


def records(data):
    return store(data).load_all()


def successors(data, rid):
    return [r for r in records(data).values() if r.get('retry_of') == rid]


def inbox(data):
    return lines(data / 'team/board-inbox.jsonl')


# ---- expected duration ---------------------------------------------------------------------------------------------

def test_expected_minutes_are_stored_when_a_record_is_minted(data):
    s = store(data)
    for rid, kind, override, want in [('demo-A-1', 'plan', None, 30), ('demo-B-1', 'small_code', None, 30),
                                      ('demo-C-1', 'loop_code', None, 50), ('demo-D-1', 'review', None, 20),
                                      ('demo-E-1', None, None, 50), ('demo-F-1', 'plan', 12, 12)]:
        brief = dict(BRIEF, run_id=rid, inputs=[])
        brief.pop('kind', None)
        if kind:
            brief['kind'] = kind
        assert s.create(brief, expected_min=override)['expected_min'] == want


# ---- dead workers --------------------------------------------------------------------------------------------------

def test_dead_at_start_the_muse_case_is_caught_in_one_tick_and_requeued_on_another_provider(data):
    launch(data, started=NOW - 60, pid=4242, log_text='')            # 1 of 50 min: far under the 20% mark
    summary = inspect(data, alive=())
    old = records(data)['demo-N1-1']
    assert old['state'] == 'timeout' and old['history'][-1]['detail'].startswith('inspector: dead: worker pid 4242')
    (new,) = successors(data, 'demo-N1-1')
    assert new['state'] == 'queued' and new['brief']['channel'] == 'claude:opus'
    assert new['brief']['model'] == 'claude-opus-5-5' and new['tier'] == 'T1' and new['attempts'] == old['attempts']
    assert summary['dead'] == 1 and summary['requeued'] == 1 and summary['detect_minutes'] == [1.0]
    metric = [e for e in lines(data / 'team/loop/events.jsonl') if e['kind'] == 'watchdog']
    assert json.loads(metric[-1]['detail'])['dead'] == 1 and json.loads(metric[-1]['detail'])['checked'] == 1
    assert [r['kind'] for r in inbox(data)] == ['inspector_requeue']


def test_the_exit_supervisors_failed_row_closes_the_run_even_when_a_pid_looks_alive(data):
    launch(data, started=NOW - 300, pid=4242)
    passed = datetime.fromtimestamp(NOW - 240).isoformat(timespec='seconds')
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id='muse-demo-N1-1', status='failed', exit_code=1, passed_at=passed,
                                last_text='model muse-wonder-full-1.0 does not exist')) + '\n')
    summary = inspect(data, alive={4242})
    rec = records(data)['demo-N1-1']
    assert rec['state'] == 'timeout' and 'does not exist' in rec['history'][-1]['detail']
    assert summary['detect_minutes'] == [4.0]


@pytest.mark.parametrize('minutes, dead', [(9, False), (11, True)])
def test_no_life_by_twenty_percent_is_dead(data, minutes, dead):
    launch(data, started=NOW - minutes * 60, pid=None)                 # launched, no pid, empty log
    inspect(data)
    assert (records(data)['demo-N1-1']['state'] == 'timeout') is dead


def test_a_fresh_log_is_life_without_a_pid(data):
    launch(data, started=NOW - 20 * 60, pid=None, log_text='working\n')
    os.utime(data.parent / 'logs/demo-N1-1.log', (NOW - 120, NOW - 120))
    inspect(data)
    assert records(data)['demo-N1-1']['state'] == 'started'
    inspect(data, now=NOW + 11 * 60)                                  # the log went quiet for 13 min: no life
    assert records(data)['demo-N1-1']['state'] == 'timeout'


# ---- milestones on a live worker -----------------------------------------------------------------------------------

@pytest.mark.parametrize('minutes, commits, kind', [
    (15, 0, None),                       # 30%: alive is enough
    (28, 0, 'inspector_behind'),         # 56%: no commit and no partial result
    (28, 2, None),                       # 56% with commits
    (53, 2, 'inspector_behind'),         # 106%: past expected time, no result
    (76, 2, 'inspector'),                # 152%: overdue, the fixer request
])
def test_milestones(data, minutes, commits, kind):
    launch(data, started=NOW - minutes * 60, pid=4242)
    inspect(data, alive={4242}, commits=commits)
    assert records(data)['demo-N1-1']['state'] == 'started'           # an alive run is never closed
    assert [r['kind'] for r in inbox(data)] == ([kind] if kind else [])
    if kind == 'inspector':
        row = inbox(data)[0]
        assert row['to'] == 'board' and 'claude-opus-5-5' in row['fixer'] and 'pid 4242 alive' in row['evidence']
    inspect(data, now=NOW + 300, alive={4242}, commits=commits)
    assert len(inbox(data)) == (1 if kind else 0)                    # one row per milestone, not one per tick


# ---- requeue rules -------------------------------------------------------------------------------------------------

def test_never_the_same_channel_twice_in_a_row_and_a_channel_pauses_after_two_dead_runs(data):
    write(data / 'team/features/demo/team.json', TEAM[:2])           # only the two Muse slots
    launch(data, 'demo-N1-1', 'N1', channel='muse:0')
    inspect(data)
    (first,) = successors(data, 'demo-N1-1')
    assert first['brief']['channel'] == 'muse:1'
    # the successor starts on muse:1 and dies too: every channel on the node's chain has killed it, so no third
    # try on either; the run closes to escalated with one fixer row
    s = store(data, NOW + 60)
    s.transition(first['run_id'], 'dispatched', deadline=NOW + 5400)
    s.transition(first['run_id'], 'started', detail=f'ledger_id=muse-{first["run_id"]}')
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id=f'muse-{first["run_id"]}', pid=5151)) + '\n')
    inspect(data, now=NOW + 120)
    assert successors(data, first['run_id']) == []
    assert records(data)[first['run_id']]['state'] == 'escalated'
    assert any('muse:0 already failed this node' in r['text'] for r in inbox(data))
    # a second node dies on muse:0: two dead runs on one channel pause it in model-budget.json
    launch(data, 'demo-N2-1', 'N2', channel='muse:0', started=NOW + 100, pid=6161)
    inspect(data, now=NOW + 180)
    budget = json.loads((data / 'team/model-budget.json').read_text())
    assert budget['muse:0']['by'] == 'inspector' and 'demo-N1-1' in budget['muse:0']['reason']
    pauses = dispatch.budget_pauses(data / 'team/model-budget.json', NOW + 200)
    eligible = dispatch.budget_table([dict(channel='muse:0', role='engineer'), dict(channel='muse:1', role='engineer')],
                                     pauses, {})
    assert [e['channel'] for e in eligible] == ['muse:1']
    assert any(r['kind'] == 'inspector_pause' for r in inbox(data))


def test_no_double_requeue_across_ticks(data):
    launch(data)
    inspect(data)
    inspect(data, now=NOW + 300)
    inspect(data, now=NOW + 20 * 60)          # the old run is now a 20-minute-old timeout: it has a successor
    assert len(successors(data, 'demo-N1-1')) == 1
    assert len([r for r in records(data).values() if r['state'] == 'queued']) == 1


def test_no_other_eligible_channel_goes_to_the_fixer_with_the_reason(data):
    write(data / 'team/features/demo/team.json', TEAM[:1])
    launch(data)
    inspect(data)
    assert successors(data, 'demo-N1-1') == []
    (row,) = inbox(data)
    assert row['kind'] == 'inspector' and 'cannot requeue' in row['text'] and 'no other engineer channel' in row['text']
    assert records(data)['demo-N1-1']['state'] == 'escalated'        # closed: nothing retries the dead channel


@pytest.mark.parametrize('where', ['record', 'completion'])
def test_held_runs_are_untouched(data, where):
    launch(data, started=NOW - 40 * 60, hold=True if where == 'record' else None)
    if where == 'completion':
        write(data / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'dispatched', 'hold': 'the owner'}}})
    before = records(data)
    inspect(data)
    assert records(data) == before and inbox(data) == []


def board_row(rid):
    """The shape lane 1 writes when the Board takes a node to build by hand (auto-system R6, 2026-10-16)."""
    return {'sha': None, 'result_path': None, 'by': 'board', 'status': 'dispatched',
            't': '2027-01-15T08:00:00+00:00', 'receipt': f'QUEUED: durable run {rid}', 'result': None}


def hand_built_case(data):
    """A dead Muse run and a two-hour-old loop timeout, both on nodes the Board is building by hand."""
    launch(data, 'demo-N1-1', 'N1', started=NOW - 60)                 # dead at start: would be closed and requeued
    s = launch(data, 'demo-N2-1', 'N2', started=NOW - 3 * 3600, pid=4242)
    s.clock = lambda: NOW - 2 * 3600
    s.transition('demo-N2-1', 'timeout', detail='result deadline expired')
    write(data / 'team/features/demo/completion.json',
          {'nodes': {'N1': board_row('demo-N1-1'), 'N2': board_row('demo-N2-1')}})


def test_a_node_the_board_builds_by_hand_is_held(data):
    hand_built_case(data)
    before = records(data)
    summary = inspect(data)
    assert records(data) == before and inbox(data) == [] and summary['checked'] == 0 and summary['stale'] == 0
    # the rule is the completion row, not the run: an ordinary dispatched row is still inspected
    row = dict(board_row('demo-N1-1'), by='Claude Opus', receipt='STARTED: claude ledger_id=x')
    assert not inspector.hand_built(row) and not inspector.hand_built(dict(row, receipt='dispatched by board'))
    assert not inspector.hand_built(dict(board_row('demo-N1-1'), status='done'))
    assert inspector.hand_built(dict(board_row('demo-N1-1'), receipt=None))   # RP1 to RP6, F1, FL, V0 carry none


def test_mutation_without_the_hand_built_hold_the_board_nodes_are_requeued(data):
    """With hand_built forced false the same case requeues both nodes, so the test above rests on that rule."""
    hand_built_case(data)
    original = inspector.hand_built
    inspector.hand_built = lambda row: False
    try:
        summary = inspect(data)
    finally:
        inspector.hand_built = original
    assert summary['requeued'] == 2 and successors(data, 'demo-N1-1') and successors(data, 'demo-N2-1')


def test_runs_of_a_finished_node_are_history(data):
    """Board correction: a node whose completion row is done is finished; its escalated or timed-out runs are
    superseded attempts (loop-building T0, home-team H2/H8 on 2026-10-15), never flagged or requeued."""
    s = launch(data, 'demo-N1-1', started=NOW - 9 * 3600)
    s.clock = lambda: NOW - 8 * 3600
    s.transition('demo-N1-1', 'escalated', detail='attempts exhausted')
    launch(data, 'demo-N1-2', started=NOW - 7 * 3600)
    write(data / 'team/features/demo/completion.json',
          {'nodes': {'N1': {'status': 'done', 'sha': 'abc1234', 'by': 'Claude Opus'}}})
    before = records(data)
    summary = inspect(data)
    assert records(data) == before and inbox(data) == [] and summary['checked'] == 0 and summary['stale'] == 0


@pytest.mark.parametrize('state', ['escalated', 'verify_fail', 'timeout'])
def test_stale_runs_after_fifteen_minutes_follow_board_policy(data, state):
    """A loop timeout is requeued on another channel; an escalated run waits for the Board; a verify_fail gets one
    row asking for the fix-back to its owner, never a requeue (Board policy, 2026-10-16)."""
    s = launch(data, started=NOW - 3600, pid=4242)
    s.clock = lambda: NOW - 20 * 60
    if state == 'verify_fail':
        s.transition('demo-N1-1', 'result', path=None, result=dict(
            run_id='demo-N1-1', feature='demo', node='N1', worker='w', sha='abc1234', branch='b', status='done',
            artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[], open_questions=[], tokens=None,
            started=NOW - 3600, finished=NOW - 1300))
    s.transition('demo-N1-1', state, detail='why')
    inspect(data, now=NOW - 10 * 60, alive={4242})                    # ten minutes: still the loop's own business
    assert successors(data, 'demo-N1-1') == []
    inspect(data, alive={4242})
    inspect(data, now=NOW + 300, alive={4242})
    assert records(data)['demo-N1-1']['state'] == state
    if state == 'timeout':
        (new,) = successors(data, 'demo-N1-1')
        assert new['brief']['channel'] == 'claude:opus' and new['state'] == 'queued'
        return
    assert successors(data, 'demo-N1-1') == []
    rows = inbox(data)
    assert ([r['kind'] for r in rows], 'fix-back' in (rows or [{}])[0].get('text', '')) == (
        (['inspector'], True) if state == 'verify_fail' else ([], False))


def test_a_late_heartbeat_does_not_postpone_the_stale_timeout_requeue(data):
    """FIX-FIRST finding 2: an ignored heartbeat at inspection time must not reset `updated`,
    the clock the 15-minute stale-timeout threshold ages from."""
    s = launch(data, started=NOW - 3600, pid=4242)
    s.clock = lambda: NOW - 3600
    s.transition('demo-N1-1', 'timeout', detail='result deadline expired')
    s.clock = lambda: NOW
    s.transition('demo-N1-1', 'heartbeat', worker='node_worker', detail='still running')
    assert records(data)['demo-N1-1']['updated'] == NOW - 3600
    summary = inspect(data, alive={4242})
    assert summary['errors'] == 0
    (new,) = successors(data, 'demo-N1-1')
    assert new['state'] == 'queued' and new['brief']['channel'] == 'claude:opus'


def test_a_stale_run_with_every_tier_spent_gets_one_row_naming_why(data):
    s = launch(data, started=NOW - 3600, pid=4242)
    rec = s.load('demo-N1-1')
    rec['attempts'] = {t: runs.ATTEMPTS_PER_TIER for t in runs.TIERS}
    runs._write_json(s.path('demo-N1-1'), rec)
    s.clock = lambda: NOW - 30 * 60
    s.transition('demo-N1-1', 'timeout', detail='result deadline expired')
    inspect(data)
    inspect(data, now=NOW + 300)
    assert successors(data, 'demo-N1-1') == []
    (row,) = inbox(data)
    assert 'every tier is exhausted' in row['text']


# ---- locks ---------------------------------------------------------------------------------------------------------

def leases(data, role='dispatcher', now=NOW):
    return locks.FileLocks(data / 'team/loop/locks.sqlite3', workspace=dispatch.REPO, caller_role=role,
                           clock=lambda: now)


def test_a_dead_runs_file_claims_are_freed_at_once_with_reason_dead(data):
    launch(data, started=NOW - 60)
    assert leases(data, now=NOW - 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 5000)
    inspect(data)
    assert leases(data).snapshot() == []
    release = [e for e in leases(data).events() if e['kind'] == 'lock_release']
    assert [e['detail']['reason'] for e in release] == ['dead'] and release[0]['run_id'] == 'demo-N1-1'


def test_only_the_dispatcher_role_can_release_as_dead_and_timeout_still_waits(data):
    assert leases(data).claim('r1', 'dispatcher', ['src/companion/demo.py'], NOW + 5000)
    with pytest.raises(PermissionError):
        leases(data, role='worker').release('r1', 'dispatcher', reason='dead')
    assert leases(data).release('r1', 'dispatcher', reason='timeout') is False   # before its deadline
    assert leases(data).release('r1', 'dispatcher', reason='dead') is True
    with pytest.raises(ValueError):
        leases(data).release('r1', 'dispatcher', reason='fake')


# ---- mutation check ------------------------------------------------------------------------------------------------

DEAD_PID_RULE = "        if ev['pid'] and not ev['alive'] and not ev['result']:\n"


def test_mutation_without_the_dead_pid_rule_the_muse_case_is_missed(data):
    """Remove the dead-at-start rule from a copy of the module: the Muse case must then go unnoticed this tick,
    so the dead-at-start test above depends on that rule and not on a later one."""
    source = Path(inspector.__file__).read_text()
    assert source.count(DEAD_PID_RULE) == 1, 'the rule this mutation targets moved; update the mutation'
    spec = importlib.util.spec_from_loader('inspector_mutant', loader=None)
    mutant = importlib.util.module_from_spec(spec)
    sys.modules['inspector_mutant'] = mutant
    try:
        exec(compile(source.replace(DEAD_PID_RULE, "        if False:\n"), 'inspector_mutant', 'exec'), mutant.__dict__)
        launch(data, started=NOW - 60, pid=4242)
        summary = inspect(data, cls=mutant.Inspector)
    finally:
        sys.modules.pop('inspector_mutant', None)
    assert summary['dead'] == 0 and records(data)['demo-N1-1']['state'] == 'started'


# ---- the watchdog tick and the metric ------------------------------------------------------------------------------

def test_the_watchdog_tick_runs_the_inspector_and_nudges_the_dispatcher(data):
    events.EventStore(data).append(dict(t=NOW, run_id=None, feature=None, node=None, worker='dispatcher',
                                        channel=None, kind='tick', sha=None, path=None, detail='{}'))
    launch(data)
    calls = []
    log = watchdog.tick(data, dispatcher=lambda dry: calls.append(dry) or {}, git=lambda args: True,
                        clock=lambda: NOW, probes=dict(alive=lambda pid: False, commits=lambda rec: 0))
    assert any(r['check'] == 'inspector' and r['action'] == 'requeue' for r in log)
    assert calls == [False] and len(successors(data, 'demo-N1-1')) == 1


def test_loop_metrics_reports_the_inspector_line(data):
    spec = importlib.util.spec_from_file_location('loop_metrics_t', Path(__file__).parents[1] / 'scripts/loop_metrics.py')
    lm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lm)
    assert lm.compute(data, NOW - 10, NOW + 10, now=NOW + 10)[-1]['status'] == 'pending'
    launch(data)
    inspect(data)
    row = lm.compute(data, NOW - 10, NOW + 10, now=NOW + 10)[-1]
    assert row['metric'] == 'inspector_detection' and row['value'] == 1.0 and row['status'] == 'pass'
    assert '1 runs checked, 1 dead caught' in row['detail']


def test_inspector_requeue_keeps_a_sonnet_hint_off_opus(data):
    """N1 is hinted Sonnet and dies at start on claude:sonnet; the requeue must not land on claude:opus or fable."""
    write(data / 'team/features/demo/team.json', [
        dict(name='Claude Opus', role='Engineer', channel='claude:opus'),
        dict(name='Claude Fable', role='Engineer', channel='claude:fable'),
        dict(name='Claude Sonnet', role='Engineer', channel='claude:sonnet')])
    write(data / 'team/feature-teams/demo.dag.json', {'nodes': [dict(
        id='N1', title='Synthetic', inputs=[], intern_friendly=False, tier='T1', files=[], acceptance='check',
        owner_hint='Engineer: Sonnet subagent')]})
    launch(data, channel='claude:sonnet', started=1_800_000_000.0 - 60, pid=4242, log_text='')
    inspect(data, alive=())
    channels = [r['brief']['channel'] for r in successors(data, 'demo-N1-1')]
    assert not any(c.startswith(('claude:opus', 'claude:fable')) for c in channels), channels
