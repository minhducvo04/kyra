"""A run in verify_fail never sits silently: within one loop tick it gets ONE fix-back on the same channel with the
failed verify output, or, when it already was a fix-back or its result says blocked, the node is escalated.

2026-10-16 FL: public-release STORY-BATCH (result blocked) and STORY-TRAIN (verify failed) sat in verify_fail for
over three hours; every watchdog tick logged "none" and nothing routed a fix-back."""

import pytest

from companion.feature_team import autopilot, dispatch, runs
from tests.test_inspector import NOW, TEAM, inbox, launch, records, store, successors, write


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'dispatched'}}})
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    return root


def failed_verify(data, rid='demo-N1-1', status='done', **fields):
    """A worker that returned a result and failed verify, like STORY-TRAIN."""
    s = launch(data, rid, started=NOW - 600, **fields)
    result = dict(run_id=rid, feature='demo', node='N1', worker='w', sha='abc1234', branch='b', status=status,
                  artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[], open_questions=['gate refused'],
                  tokens=None, started=NOW - 600, finished=NOW - 500)
    write(data / 'team/loop/results' / f'{rid}.json', result)
    s.transition(rid, 'result', result=result)
    log = data / 'team/loop/verify' / f'{rid}.log'
    write(log, 'FAILED tests/test_demo.py::test_it - AssertionError: 2 != 3\n')
    why = 'stale result: result status is blocked' if status == 'blocked' else 'verify fail: 1 of 2 command(s) failed'
    s.transition(rid, 'verify_fail', worker='verify', detail=why, path=str(log))
    return s


def tick(data, now=NOW + 60):
    report, _ = dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: now,
                                   dry_run=False)
    return report


def test_a_verify_fail_gets_one_fixback_on_the_same_channel_with_the_verify_output(data):
    failed_verify(data)
    report = tick(data)
    assert records(data)['demo-N1-1']['state'] == 'fixback'
    (new,) = successors(data, 'demo-N1-1')
    assert new['run_id'] in report['launched'] and new['brief']['channel'] == 'muse:0'
    assert 'verify fail: 1 of 2 command(s) failed' in new['brief']['fixback']
    assert 'AssertionError: 2 != 3' in new['brief']['fixback']
    tick(data, NOW + 400)
    tick(data, NOW + 700)
    assert len(successors(data, 'demo-N1-1')) == 1          # one fix-back, one live worker per node
    assert len([r for r in records(data).values() if r['state'] in {'queued', 'dispatched', 'started'}]) == 1


def test_a_blocked_result_escalates_with_the_reason(data):
    failed_verify(data, status='blocked')
    report = tick(data)
    assert report['escalated'] == ['demo-N1-1'] and successors(data, 'demo-N1-1') == []
    assert records(data)['demo-N1-1']['state'] == 'escalated'
    (row,) = [r for r in inbox(data) if r.get('kind') == 'escalated']
    assert 'blocked' in row['text'] and 'gate refused' in row['text']


def test_a_failed_fixback_escalates_instead_of_a_second_fixback(data):
    failed_verify(data)
    tick(data)
    (new,) = successors(data, 'demo-N1-1')
    s = store(data, NOW + 120)
    s.transition(new['run_id'], 'started', detail='started')
    result = dict(run_id=new['run_id'], feature='demo', node='N1', worker='w', sha='abc1234', branch='b',
                  status='done', artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[],
                  open_questions=[], tokens=None, started=NOW + 100, finished=NOW + 150)
    s.transition(new['run_id'], 'result', result=result)
    s.transition(new['run_id'], 'verify_fail', worker='verify', detail='verify fail: still red')
    report = tick(data, NOW + 400)
    assert report['escalated'] == [new['run_id']] and successors(data, new['run_id']) == []
    (row,) = [r for r in inbox(data) if r.get('kind') == 'escalated']
    assert 'fix-back already failed' in row['text'] and 'still red' in row['text']


def test_a_board_held_verify_fail_is_left_alone(data):
    failed_verify(data, hold='board')
    assert tick(data)['escalated'] == [] and records(data)['demo-N1-1']['state'] == 'verify_fail'


LIVE = {'queued', 'dispatched', 'started', 'heartbeat'}


def live_on(data, channel='muse:0'):
    return [r['run_id'] for r in records(data).values() if r['brief']['channel'] == channel and r['state'] in LIVE]


def _failed(data, rid, node):
    s = launch(data, rid, node=node, started=NOW - 600)
    rec = s.load(rid)
    rec['brief']['files'] = [f'NEW src/companion/demo_{node.lower()}.py']  # two nodes, two files: no lease clash
    runs._write_json(s.path(rid), rec)
    result = dict(run_id=rid, feature='demo', node=node, worker='w', sha='abc1234', branch='b', status='done',
                  artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[], open_questions=[],
                  tokens=None, started=NOW - 600, finished=NOW - 500)
    write(data / 'team/loop/results' / f'{rid}.json', result)
    s.transition(rid, 'result', result=result)
    log = data / 'team/loop/verify' / f'{rid}.log'
    write(log, 'FAILED tests/test_demo.py::test_it\n')
    s.transition(rid, 'verify_fail', worker='verify', detail='verify fail: 1 of 2 command(s) failed', path=str(log))


def test_two_parked_verify_fails_on_one_channel_launch_one_fixback_per_tick(data):
    """Review o-rev-vf2 p3: the busy check read the tick-start snapshot, so N2's fix-back missed N1's, launched in
    the same tick, and both ran on muse:0."""
    _failed(data, 'demo-N1-1', 'N1')
    _failed(data, 'demo-N2-1', 'N2')
    report = tick(data)
    assert len(live_on(data)) == 1 and len(report['launched']) == 1
    assert any('channel has a live run' in r['reasons'] for r in report['refusals'])


def test_a_timeout_retry_and_a_fixback_never_share_a_channel_in_one_tick(data):
    """Review o-rev-vf2 p3c: a waiting timeout retry and another node's fix-back both launched on muse:0."""
    s = launch(data, 'demo-N2-1', node='N2', started=NOW - 6000)
    s.transition('demo-N2-1', 'timeout', detail='{}')  # timed out last tick; its retry waited (budget, RAM)
    rec = s.load('demo-N2-1')
    rec['brief']['files'] = ['NEW src/companion/demo_n2.py']
    runs._write_json(s.path('demo-N2-1'), rec)
    _failed(data, 'demo-N1-1', 'N1')
    tick(data)
    assert len(live_on(data)) == 1


def test_a_noteless_fixback_still_gets_its_one_fixback(data):
    """Review o-rev-vf2 p4: a crash between the fixback transition and the note write is not skipped forever."""
    failed_verify(data)
    store(data, NOW + 30).transition('demo-N1-1', 'fixback',
                                     detail='Fix-back of demo-N1-1: verify fail: 1 of 2 command(s) failed.')
    assert 'fixback_note' not in records(data)['demo-N1-1']
    report = tick(data)
    (new,) = successors(data, 'demo-N1-1')
    assert new['run_id'] in report['launched'] and 'Fix-back of demo-N1-1' in new['brief']['fixback']
    assert 'fixback_note' not in new
    tick(data, NOW + 400)
    assert len(successors(data, 'demo-N1-1')) == 1


def test_a_verify_fail_on_a_retired_slot_escalates_instead_of_parking(data):
    failed_verify(data, channel='muse:9')  # not in muse.SLOTS: allowed() refuses it for good
    report = tick(data)
    assert report['escalated'] == ['demo-N1-1'] and successors(data, 'demo-N1-1') == []


def _fail(data, rid, now, why):
    s = store(data, now)
    result = dict(run_id=rid, feature='demo', node='N1', worker='w', sha='abc1234', branch='b', status='done',
                  artifacts=[], proof=[dict(command='x', exit=0, log='l')], tests=[], open_questions=[],
                  tokens=None, started=now - 50, finished=now)
    s.transition(rid, 'result', result=result)
    s.transition(rid, 'verify_fail', worker='verify', detail=why)


def test_a_board_requeue_gets_its_own_one_fixback_then_a_second_fail_escalates(data):
    """FL13 (2026-10-16) read the 'fix-back already failed' escalations of STORY-REHEARSAL, STORY-TRAIN and README
    as a requeue keeping the one-fix-back marker. The marker is the run's brief.fixback, and a requeue mints a fresh
    record without it, so a requeued node gets exactly one fix-back of its own before it escalates."""
    failed_verify(data)
    tick(data)
    (fix,) = successors(data, 'demo-N1-1')
    _fail(data, fix['run_id'], NOW + 150, 'verify fail: still red')
    assert tick(data, NOW + 400)['escalated'] == [fix['run_id']]
    # the Board requeues the node (completion back to queued) and the next fill mints a fresh record
    write(data / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'queued'}}})
    launch(data, 'demo-N1-2', started=NOW + 500)
    _fail(data, 'demo-N1-2', NOW + 600, 'verify fail: 1 of 2 command(s) failed')
    report = tick(data, NOW + 700)
    (second,) = successors(data, 'demo-N1-2')
    assert report['escalated'] == [] and second['run_id'] in report['launched'] and second['brief']['fixback']
    _fail(data, second['run_id'], NOW + 800, 'verify fail: still red')
    assert tick(data, NOW + 900)['escalated'] == [second['run_id']] and successors(data, second['run_id']) == []
