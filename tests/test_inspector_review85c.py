"""Review o-rev85c probes for the inspector fix-backs fadf30b5 + 7b8f3efe (lens: correctness and locks).

A probe that fails names a defect; a pin passes and holds a behaviour the review relied on. Synthetic fixtures only."""

import json
from pathlib import Path

import pytest

from companion.feature_team import contract, dispatch, events, inspector, node_worker, watchdog
from tests.test_inspector import NOW, TEAM, inbox, launch, leases, store, successors, write
from tests.test_node_worker import case  # noqa: F401  the node_worker fixture (scratch store, fake clock)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {}})
    return root


def inspect(data, now=NOW, alive=(), commits=0):
    s = store(data, now)
    return inspector.Inspector(data, store=s, now=now, alive=lambda pid: pid in alive, commits=lambda rec: commits,
                               emit=events.EventStore(data).append).run(s.load_all())


# ---- Q3 the launch-failure rule: "ends" must mean the process ended, not the clock alone -----------------------------

def test_probe_a_live_worker_that_wrote_an_invalid_result_with_an_empty_log_is_not_a_launch_failure(case, tmp_path,  # noqa: F811
                                                                                                    monkeypatch):
    """A CLI that buffers its output (cursor-agent --output-format text prints only at exit) writes its result file,
    invalid, at 2 min and is still running: its pid is alive and its log is empty. The invalid-result branch calls it a
    launch failure on the clock and the empty log alone, never asking whether the process ended, so a live worker loses
    its one correction, counts a failure on its channel (two pause the channel 12 h) and the inspector requeues the node
    elsewhere while it still runs."""
    store_, brief, clock, _, _, kwargs = case
    log = tmp_path / 'worker.log'
    log.write_text('')
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)          # the worker process is alive throughout
    calls, due = [], {}

    def send(prepared, message, *, receipt_timeout):
        calls.append(message)
        due.update(t=clock() + 120, path=Path(prepared['result']), run_id=prepared['run_id'])
        return f'STARTED: cursor pid=4242 ledger_id=fixture-{len(calls)} log={log}'

    def worker_writes_at_two_minutes():                                    # it works 2 min, then writes its result
        if due and clock() >= due['t'] and not due['path'].exists():
            due['path'].parent.mkdir(parents=True, exist_ok=True)
            due['path'].write_text(json.dumps({'run_id': due['run_id']}))
            due.clear()

    clock.on_sleep = worker_writes_at_two_minutes
    out = node_worker.run_node(brief['run_id'], send=send, **kwargs)
    assert not node_worker.is_launch_failure(out), 'a live worker was recorded as a launch failure'
    assert len(calls) == 2                                                 # it kept its one correction


def test_pin_a_live_pid_at_five_minutes_with_an_empty_log_is_not_a_launch_failure(case, tmp_path, monkeypatch):  # noqa: F811
    """The brief's case: the pid is alive past 5 min with an empty log (a buffering CLI), then exits at 6 min with no
    result: a failed result, never a launch failure (the pid branch checks the process, and the 5 min window)."""
    store_, brief, clock, _, _, kwargs = case
    log = tmp_path / 'worker.log'
    log.write_text('')
    start = {}
    monkeypatch.setattr(node_worker, '_alive', lambda pid: clock() < start['t'] + 360)

    def send(prepared, message, *, receipt_timeout):
        start['t'] = clock()
        return f'STARTED: cursor pid=4242 ledger_id=fixture-1 log={log}'

    out = node_worker.run_node(brief['run_id'], send=send, **kwargs)
    assert not node_worker.is_launch_failure(out)
    assert out['history'][-1]['t'] >= start['t'] + 360                   # it ended only once the pid was gone


def test_pin_a_pid_gone_at_one_minute_with_an_empty_log_is_a_launch_failure(case, tmp_path, monkeypatch):  # noqa: F811
    store_, brief, clock, _, _, kwargs = case
    log = tmp_path / 'worker.log'
    log.write_text('')
    start = {}
    monkeypatch.setattr(node_worker, '_alive', lambda pid: clock() < start['t'] + 60)

    def send(prepared, message, *, receipt_timeout):
        start['t'] = clock()
        return f'STARTED: cursor pid=4242 ledger_id=fixture-1 log={log}'

    out = node_worker.run_node(brief['run_id'], send=send, **kwargs)
    assert out['state'] == 'timeout' and node_worker.is_launch_failure(out)


# ---- Q4 pick_channel: every channel on the node's chain, across a dispatcher retry -----------------------------------

def test_pin_a_dispatcher_retry_in_the_chain_still_excludes_every_channel_that_failed_the_node(data):
    """muse:0 dies -> inspector requeue on claude:opus; that run times out and the dispatcher's fresh retry (retry_of)
    runs claude:opus again and dies: the next requeue goes to neither muse:0 nor claude:opus."""
    launch(data, started=NOW - 60, pid=4242)
    inspect(data)
    (b,) = successors(data, 'demo-N1-1')
    assert b['brief']['channel'] == 'claude:opus'
    s = store(data, NOW + 10)
    s.transition(b['run_id'], 'dispatched', deadline=NOW + 5400)
    s.transition(b['run_id'], 'timeout', detail='result deadline expired')
    launch(data, 'demo-N1-c', 'N1', channel='claude:opus', started=NOW + 20, pid=7777, retry_of=b['run_id'])
    inspect(data, now=NOW + 120)
    (d,) = successors(data, 'demo-N1-c')
    assert d['brief']['channel'] not in {'muse:0', 'claude:opus'}


def test_a_correction_successor_never_gets_a_bare_grok_channel(data):
    """2026-10-16 STORY-REHEARSAL: claude:opus-2 wrote an invalid result, its correction timed out, and the inspector
    requeued it on the team's bare 'grok' row, which the send contract refuses (no grok:<folder>), so the successor
    escalated at once. The pick skips that row and the chosen channel passes the send contract."""
    write(data / 'team/features/demo/team.json', [
        dict(name='Claude Opus', role='Engineer', channel='claude:opus'),
        dict(name='Grok', role='Engineer', channel='grok'),
        dict(name='Muse Wonder Full', role='Engineer', channel='muse:0'),
        dict(name='Claude Opus 2', role='Engineer', channel='claude:opus-2')])
    s = launch(data, started=NOW - 20 * 60, pid=4242, channel='claude:opus-2')
    s.clock = lambda: NOW - 16 * 60
    s.transition('demo-N1-1', 'timeout', detail='invalid result; one correction dispatch follows')
    inspect(data)
    (b,) = successors(data, 'demo-N1-1')
    assert b['brief']['channel'] == 'muse:0'
    assert contract.channel_refusal(b['brief']['channel']) is None


def _only_seat(data, channel):
    write(data / 'team/features/demo/team.json', [dict(name='Claude Opus', role='Engineer', channel='claude:opus'),
                                                  dict(name='Grok', role='Engineer', channel=channel)])


def test_a_team_with_only_a_bare_grok_seat_gets_the_cannot_requeue_row_and_no_run(data):
    """2026-10-16 AUDIT / EXPORT-SCAN: the only free seat was a bare 'grok' (unservable), the requeue minted a run
    that could never start. Now: no successor, one cannot-requeue fixer row."""
    _only_seat(data, 'grok')
    launch(data, started=NOW - 60, pid=4242, channel='claude:opus')
    inspect(data)
    assert successors(data, 'demo-N1-1') == []
    assert any('cannot requeue' in str(row) for row in inbox(data))


def test_a_valid_grok_folder_seat_is_picked(data):
    _only_seat(data, 'grok:story')
    launch(data, started=NOW - 60, pid=4242, channel='claude:opus')
    inspect(data)
    (b,) = successors(data, 'demo-N1-1')
    assert b['brief']['channel'] == 'grok:story'


# ---- Q2 escalated runs keep an unexpired lease in the dispatcher -----------------------------------------------------

def test_pin_the_dispatcher_releases_an_escalated_run_only_at_its_lease_deadline(data, monkeypatch):
    from companion.feature_team import autopilot
    s = launch(data, started=NOW - 60, pid=4242)
    assert leases(data, now=NOW - 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 600)
    s.transition('demo-N1-1', 'escalated', detail='attempts exhausted on this tier')
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW, dry_run=False)
    assert [c['run_id'] for c in leases(data).snapshot()] == ['demo-N1-1']
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW + 601, dry_run=False)
    assert leases(data, now=NOW + 601).snapshot() == []


# ---- Q5 the dry run is read-only: byte and mtime snapshot of the whole data tree ----------------------------------

def _snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob('*')) if p.is_file()}


def test_pin_the_inspector_dry_run_writes_no_file_lock_or_budget(data, tmp_path):
    """Every write path primed: a dead run with an unexpired claim and a gone pid (lease release, close, requeue), a
    second dead run on the same channel (would pause muse:0 in model-budget.json), a launch-failure timeout, a stale
    timeout, a budget file, inspector state and a lock store. The dry run must leave every byte and mtime as it was."""
    write(data / 'team/model-budget.json', {'claude': {'paused_until': None, 'reason': 'manual'}})
    write(data / 'team/inspector-state.json', {'lock_skips': 2, 'channel_fails': {
        'muse:0': [dict(t=NOW - 60, run_id='demo-N9-1', why='earlier death')]}})
    launch(data, 'demo-N1-1', 'N1', started=NOW - 60, pid=4242)
    assert leases(data, now=NOW - 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    launch(data, 'demo-N2-1', 'N2', started=NOW - 60, pid=4343)
    s = launch(data, 'demo-N3-1', 'N3', started=NOW - 300, pid=4444)
    s.transition('demo-N3-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker exited in 40 s with an '
                                                 'empty log (x.log) and no result')
    s = launch(data, 'demo-N4-1', 'N4', started=NOW - 90 * 60, pid=4545)
    s.clock = lambda: NOW - 40 * 60
    s.transition('demo-N4-1', 'timeout', detail='result deadline expired')
    events.EventStore(data).append(dict(t=NOW, run_id=None, feature=None, node=None, worker='dispatcher',
                                        channel=None, kind='tick', sha=None, path=None, detail='{}'))
    before = _snapshot(tmp_path)
    said = []
    st = store(data)
    summary = inspector.Inspector(data, store=st, now=NOW, alive=lambda pid: False, commits=lambda rec: 0,
                                  dry_run=True, line=lambda *a, **k: said.append(' '.join(map(str, a)))
                                  ).run(st.load_all())
    watchdog.tick(data, dispatcher=lambda dry: {}, git=lambda args: True, clock=lambda: NOW, dry_run=True,
                  probes=dict(alive=lambda pid: False, commits=lambda rec: 0))
    after = _snapshot(tmp_path)
    assert summary['dead'] >= 3 and any('would requeue' in x or 'would close' in x for x in said)  # paths were hit
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed == [], f'the dry run wrote: {changed}'
    assert [c['run_id'] for c in leases(data).snapshot()] == ['demo-N1-1'] and not inbox(data)
