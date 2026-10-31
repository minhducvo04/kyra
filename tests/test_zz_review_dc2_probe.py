"""Review o-rev-dc2 probes on dead-claims 9fe0dd3f (synthetic fixtures only)."""

import json

import pytest

from companion.feature_team import autopilot, dispatch, inspector
from tests.test_inspector import NOW, TEAM, launch, leases, write
from tests.test_loop_dead_claims import FILES, holders, requeued_run, tick


@pytest.fixture
def data(tmp_path, monkeypatch):
    """The same fixture as test_loop_dead_claims.data."""
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'queued'}}})
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    return root


def test_pb_the_wait_ends_at_the_older_claims_deadline_without_escalating(data, monkeypatch):
    # item 4: a hung live worker never proven dead blocks its requeued node only until its claim's fixed deadline
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: True)
    launch(data, 'demo-N1-1', 'N1', started=NOW - 600).transition('demo-N1-1', 'escalated', detail='node worker failed')
    assert leases(data, now=NOW - 600).claim('demo-N1-1', 'dispatcher', FILES, NOW + 3000)
    requeued_run(data)
    for now in (NOW + 60, NOW + 1300, NOW + 2999):
        report = tick(data, now)
        assert report['launched'] == [] and report['escalated'] == [], report
    waits = [e for e in leases(data, now=NOW + 2999).events() if e['kind'] == 'lock_wait']
    assert waits == []  # the locks module's ten-minute wait was never entered
    report = tick(data, NOW + 3000)
    assert 'demo-N1-2' in report['launched'] and report['escalated'] == [], report
    assert holders(data, NOW + 3000) == ['demo-N1-2']


@pytest.mark.parametrize('first_pid', [4242, None], ids=['live-courier', 'courier-without-pid'])
def test_pc_a_correction_runs_first_courier_still_running_keeps_the_claim(data, monkeypatch, first_pid):
    # worker_gone's correction-path case: courier A (invalid result, maybe a buffering CLI still running, or a cursor
    # receipt with no pid) lives while correction courier B is gone. Only B is in the current attempt's evidence.
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: pid == 4242)
    s = launch(data, 'demo-N1-1', 'N1', started=NOW - 600, pid=first_pid)
    assert leases(data, now=NOW - 600).claim('demo-N1-1', 'dispatcher', FILES, NOW + 3000)
    s.transition('demo-N1-1', 'timeout', detail='invalid result; one correction dispatch follows')
    s.transition('demo-N1-1', 'dispatched', detail='fresh channel dispatch', deadline=NOW + 4800)
    s.transition('demo-N1-1', 'started', detail='ledger_id=muse-demo-N1-1-b')
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id='muse-demo-N1-1-b', channel='muse', target='wt', sent_at='2027-01-15T08:00:00',
                                pid=5151, receipt='muse started pid 5151 in wt ledger_id=muse-demo-N1-1-b')) + '\n')
    write(data / 'team/loop/demo-N1-1.worker.pid', '6161\n')
    s.transition('demo-N1-1', 'escalated', detail='result validation failed after one correction')
    probe = inspector.Inspector(data, store=None, now=NOW, alive=lambda pid: pid == 4242)
    assert inspector.proof_dead(probe.evidence(s.load('demo-N1-1')))  # B, the current attempt's courier, is gone
    requeued_run(data)
    report = tick(data)
    assert holders(data) == ['demo-N1-1'] and 'demo-N1-2' not in report['launched'], report
    assert dispatch.proven_dead(s.load('demo-N1-1'), data, NOW) is False


def test_pd_an_uncertain_launch_escalated_on_tier_exhaustion_keeps_its_claim(data, monkeypatch):
    # The dispatcher's tier-exhaustion escalate() of a 'start receipt timeout; no confirmed ledger id' record: the
    # detached courier may be live with no ledger id or pid in the history (worker_gone's UNKNOWN). Every known pid
    # is gone, so only proof_dead(evidence) of an attempt with no courier evidence keeps the claim.
    monkeypatch.setattr(inspector, 'pid_alive', lambda pid: False)
    s = launch(data, 'demo-N1-1', 'N1', started=NOW - 600, state='dispatched', ledger=False)
    assert leases(data, now=NOW - 600).claim('demo-N1-1', 'dispatcher', FILES, NOW + 3000)
    write(data / 'team/loop/demo-N1-1.worker.pid', '6161\n')
    s.transition('demo-N1-1', 'timeout', detail='start receipt timeout; no confirmed ledger id')
    s.transition('demo-N1-1', 'escalated', detail='All three tiers exhausted (two attempts per tier)')
    requeued_run(data)
    report = tick(data)
    assert holders(data) == ['demo-N1-1'] and 'demo-N1-2' not in report['launched'], report
