"""A requeued node takes over its own older runs' file claims only on proof they died; otherwise it waits.

2026-10-16 FL10: public-release HISTORY-SCAN and STORY-REHEARSAL were requeued by the Board, waited ten minutes on
the file claims of their OWN escalated grok runs (held to their deadlines), escalated again with "File lock wait
exceeded ten minutes" and needed the Board a second time. Review o-rev-dc (FIX-FIRST on 6cd3d02a): escalated is no
proof of death, so an unproven older run keeps its claim and the requeued run waits for it, without escalating."""

import json
import os

import pytest

from companion.feature_team import autopilot, dispatch, inspector
from tests.test_inspector import BRIEF, NOW, TEAM, launch, leases, records, store, write

FILES = ['src/companion/demo.py', 'tests/test_demo.py']
LIVE_PID = os.getpid()


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'queued'}}})
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    return root


def old_run(data, rid, node='N1', state='escalated'):
    """An earlier run that claimed the node's files at dispatch; its claim deadline is far ahead."""
    s = launch(data, rid, node, started=NOW - 600)
    assert leases(data, now=NOW - 600).claim(rid, 'dispatcher', FILES, NOW + 3000)
    if state != 'started':
        s.transition(rid, state, detail='node worker failed: grok courier error')


def requeued_run(data, rid='demo-N1-2'):
    store(data, NOW).create(dict(BRIEF, run_id=rid, node='N1', inputs=[], channel='muse:0',
                                 result=str(data / 'team/loop/results' / f'{rid}.json'), deadline=NOW + 5400))


def tick(data, now=NOW + 60):
    report, _ = dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: now,
                                   dry_run=False)
    return report


def holders(data, now=NOW + 60):
    return sorted(c['run_id'] for c in leases(data, now=now).snapshot())


def courier_row(data, rid, pid):
    """The courier ledger row and the node worker pid file a live grok run leaves (like launch() in test_inspector)."""
    (data / 'context').mkdir(parents=True, exist_ok=True)
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id=f'grok-{rid}', channel='grok', target='wt', sent_at='2027-01-15T08:00:00', pid=pid,
                                receipt=f'grok started pid {pid} in wt ledger_id=grok-{rid}')) + '\n')
    write(data / 'team/loop' / f'{rid}.worker.pid', str(pid))


def test_a_requeued_node_takes_over_its_own_escalated_runs_claims_once_proven_dead(data, monkeypatch):
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: False)   # the courier pid 4242 is gone
    old_run(data, 'demo-N1-1')
    requeued_run(data)
    report = tick(data)
    assert 'demo-N1-2' in report['launched'], report
    assert holders(data) == ['demo-N1-2']
    waits = [e for e in leases(data, now=NOW + 60).events() if e['kind'] == 'lock_wait']
    assert waits == [], 'the requeued run waited on its own dead escalated run'
    assert records(data)['demo-N1-2']['state'] == 'dispatched'
    release = [e for e in leases(data, now=NOW + 60).events() if e['kind'] == 'lock_release']
    assert [(e['run_id'], e['detail']['reason']) for e in release] == [('demo-N1-1', 'dead')]


def test_an_unproven_escalated_run_keeps_its_claim_and_the_requeue_waits_without_escalating(data, monkeypatch):
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: True)    # the courier may still run
    old_run(data, 'demo-N1-1')
    requeued_run(data)
    for now in (NOW + 60, NOW + 700, NOW + 1300):                  # past the ten-minute claim wait
        report = tick(data, now)
        assert report['launched'] == [] and 'demo-N1-2' not in report['escalated'], report
        assert holders(data, now) == ['demo-N1-1']
        (row,) = [r for r in report['refusals'] if r['run_id'] == 'demo-N1-2']
        assert row['reasons'] == ["this node's older run still holds its files"]
    assert records(data)['demo-N1-2']['state'] == 'queued'
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: False)   # the worker died: the next tick dispatches
    assert 'demo-N1-2' in tick(data, NOW + 1400)['launched']


def test_a_live_older_run_of_the_same_node_still_blocks(data):
    old_run(data, 'demo-N1-1', state='started')
    requeued_run(data)
    report = tick(data)
    assert 'demo-N1-2' not in report['launched']
    assert holders(data) == ['demo-N1-1']


def test_an_escalated_run_of_another_node_still_blocks(data, monkeypatch):
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: False)
    old_run(data, 'demo-N2-1', node='N2')
    requeued_run(data)
    report = tick(data)
    assert 'demo-N1-2' not in report['launched']
    assert holders(data) == ['demo-N2-1']


# ---- review o-rev-dc probes p1 to p3, copied from tests/test_zz_review_dc_probe.py --------------------------------

def test_p1_an_escalated_run_whose_courier_is_alive_keeps_its_claim_on_requeue(data):
    # node_worker escalates 'result validation failed after one correction' while a buffering CLI may still run
    # (launch_failure's docstring, review o-rev85c); end_attempt then logs 'file claim kept (the worker may still be
    # running)'. A Board requeue is no proof of death.
    old_run(data, 'demo-N1-1')
    courier_row(data, 'demo-N1-1', LIVE_PID)
    requeued_run(data)
    report = tick(data)
    assert 'demo-N1-1' in holders(data), 'a live worker lost its file claim on a Board requeue'
    assert 'demo-N1-2' not in report['launched']


@pytest.mark.parametrize('proof', [False, None])
def test_p2_a_timeout_closed_without_proof_keeps_its_claim(data, proof):
    old_run(data, 'demo-N1-1', state='timeout')
    rec = store(data, NOW).load('demo-N1-1')
    if proof is not None:
        rec['inspector_closed'] = dict(t=NOW, proof=proof, why='dead: no life at 20%')
    store(data, NOW)._commit(rec, worker='watchdog', channel='muse:0', path=None, detail='closed')
    requeued_run(data)
    tick(data)
    assert 'demo-N1-1' in holders(data)


def test_p3_a_timeout_closed_with_proof_hands_its_claim_over(data):
    old_run(data, 'demo-N1-1', state='timeout')
    rec = store(data, NOW).load('demo-N1-1')
    rec['inspector_closed'] = dict(t=NOW, proof=True, why='dead: worker pid 4242 is gone')
    store(data, NOW)._commit(rec, worker='watchdog', channel='muse:0', path=None, detail='closed')
    requeued_run(data)
    report = tick(data)
    assert holders(data) == ['demo-N1-2'] and 'demo-N1-2' in report['launched']


# ---- FL11 2026-10-16: an invalid result kept its own run's claim, so the correction waited out the 90-min deadline -

def invalid_result_run(data):
    s = launch(data, 'demo-N1-1', 'N1', started=NOW - 600)
    assert leases(data, now=NOW - 600).claim('demo-N1-1', 'dispatcher', FILES, NOW + 3000)
    s.transition('demo-N1-1', 'timeout', detail='invalid result; one correction dispatch follows',
                 path=str(data / 'team/loop/results/demo-N1-1.json.invalid-1'))


def test_the_correction_after_an_invalid_result_starts_once_the_worker_is_proven_gone(data, monkeypatch):
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: False)
    invalid_result_run(data)
    report = tick(data)
    (successor,) = [r for r in records(data).values() if r.get('retry_of') == 'demo-N1-1']
    assert report['launched'] == [successor['run_id']], report
    assert holders(data) == [successor['run_id']]


def test_the_correction_waits_while_the_first_worker_may_still_run(data, monkeypatch):
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: True)
    invalid_result_run(data)
    report = tick(data)
    assert report['launched'] == [] and holders(data) == ['demo-N1-1']
    (row,) = [r for r in report['refusals'] if r['run_id'] == 'demo-N1-1']
    assert row['reasons'] == ['previous lease still held', 'its worker is not proven gone']
