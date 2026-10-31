"""A worker that died silently and left its run escalated by a node-worker crash is requeued by the inspector.

Night 2026-10-17 (memory-scale MS3): the inspector closed a dead Grok run, the still-running node worker then crashed on
the closed record ("timeout -> heartbeat is not allowed"), the run went to escalated, and the node sat dispatched for
hours because an escalated run is final. Real stores on tmp data; only the clock and the process probe are faked."""

import json
import os

import pytest

from companion.feature_team import inspector, runs
from tests.test_inspector import NOW, TEAM, inbox, inspect, launch, records, successors, write

CRASH = 'node worker failed: ValueError: state: timeout -> heartbeat is not allowed'
RID = 'demo-N1-1'


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    return root


def completion(data, **row):
    write(data / 'team/features/demo/completion.json',
          {'nodes': {'N1': dict(status='dispatched', by='Muse Wonder Full', t='2027-01-15T08:00:00', **row)}})


def crashed(data, *, quiet_min=30, pid=4242, detail=CRASH, row=None, closed=True, **fields):
    """A run whose worker died ``quiet_min`` minutes ago and whose node worker then escalated it."""
    s = launch(data, pid=pid, started=NOW - 3600, **fields)
    s.transition(RID, 'timeout', worker='watchdog', detail='inspector: dead: worker exited without a result')
    s.transition(RID, 'escalated', worker='node_worker', detail=detail)
    if closed:  # as the inspector's own dead-run close leaves the record
        rec = s.load(RID)
        rec['inspector_closed'] = dict(t=NOW - 3000, proof=True, why='dead')
        runs._write_json(s.path(RID), rec)
    log = data.parent / 'logs' / f'{RID}.log'
    log.write_text('a few hundred bytes of output')
    os.utime(log, (NOW - quiet_min * 60,) * 2)
    completion(data, **(row or {}))
    return s


def test_dead_pid_and_stale_log_requeues_once_on_another_channel(data):
    crashed(data)
    inspect(data)
    (new,) = successors(data, RID)
    assert new['state'] == 'queued' and new['brief']['channel'] != 'muse:0'
    inspect(data, now=NOW + 300)
    inspect(data, now=NOW + 600)
    assert len(successors(data, RID)) == 1                      # the next ticks find a successor and do nothing


def test_a_late_heartbeat_after_the_crash_escalation_still_requeues(data):
    """FIX-FIRST finding 1: the ignored late heartbeat must not bury the 'node worker failed:'
    closure detail requeue_crashed reads from history[-1]; otherwise the dead run strands."""
    s = crashed(data)
    s.clock = lambda: NOW
    s.transition(RID, 'heartbeat', worker='node_worker', detail='waiting for result')
    rec = records(data)[RID]
    assert rec['state'] == 'escalated' and rec['history'][-1]['detail'].startswith('node worker failed:')
    summary = inspect(data)
    assert summary['errors'] == 0
    (new,) = successors(data, RID)
    assert new['state'] == 'queued' and new['brief']['channel'] != 'muse:0'


def test_a_live_worker_is_never_requeued(data):
    crashed(data)
    inspect(data, alive=(4242,))
    assert successors(data, RID) == [] and inbox(data) == []


def test_a_fresh_log_is_never_requeued(data):
    crashed(data, quiet_min=5)
    inspect(data)
    assert successors(data, RID) == []
    inspect(data, now=NOW + 16 * 60)                            # twenty quiet minutes in all: now it is dead
    assert len(successors(data, RID)) == 1


def test_third_death_escalates_once_with_the_evidence_and_does_not_loop(data):
    crashed(data)
    state = data / 'team/inspector-state.json'
    write(state, {'node_requeues': {'demo:N1': inspector.NODE_REQUEUE_LIMIT}})
    for tick in range(3):
        inspect(data, now=NOW + 300 * tick)
    assert successors(data, RID) == []
    (row,) = inbox(data)
    assert row['kind'] == 'inspector' and row['to'] == 'board'
    assert f'{RID}.log' in row['evidence'] and 'bytes' in row['evidence'] and 'last write' in row['evidence']
    assert json.loads(state.read_text())['node_requeues']['demo:N1'] == inspector.NODE_REQUEUE_LIMIT
    assert records(data)[RID]['state'] == 'escalated'


@pytest.mark.parametrize('row', [dict(delivery_status='handoff_pending'), dict(needs_input='which licence?'),
                                 dict(hold=True)])
def test_a_run_waiting_on_a_human_or_the_board_is_untouched(data, row):
    crashed(data, row=row)
    inspect(data)
    assert successors(data, RID) == [] and inbox(data) == []


def test_a_run_with_a_result_file_is_untouched(data):
    crashed(data)
    result = data / 'team/loop/results' / f'{RID}.json'
    write(result, '{}')
    inspect(data)
    assert successors(data, RID) == []


def test_a_worker_verdict_is_not_a_crash(data):
    crashed(data, detail='result says blocked: parked until the Board re-plans')
    inspect(data)
    assert successors(data, RID) == []


def test_a_finished_node_is_history(data):
    crashed(data)
    write(data / 'team/features/demo/completion.json', {'nodes': {'N1': dict(status='done')}})
    inspect(data)
    assert successors(data, RID) == []


def test_an_escalation_with_no_launched_worker_is_final(data):
    crashed(data, pid=None, closed=False, detail='node worker failed: CalledProcessError: git worktree add')
    inspect(data)
    assert successors(data, RID) == [] and inbox(data) == []


def test_past_the_cap_on_an_inspector_closed_run_posts_one_row_and_no_error(data):
    crashed(data)
    write(data / 'team/inspector-state.json', {'node_requeues': {'demo:N1': inspector.NODE_REQUEUE_LIMIT}})
    summary = inspect(data)
    assert summary['errors'] == 0 and len(inbox(data)) == 1
