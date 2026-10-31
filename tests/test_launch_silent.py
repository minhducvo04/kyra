"""Silent launches (2026-10-16, public-release HISTORY-SCAN and STORY-REHEARSAL on claude:opus).

The node worker ended its attempt (an invalid result, a refused send) and exited without a word in its worker log and
without releasing the run's file claim, so the dispatcher waited out the claim ("previous lease still held") until the
inspector's 15 minute stale check, and a successor refused at dispatch held its claim for its full 90 minutes. A node
worker that died before its start receipt left an empty log and a dispatched record nobody closed until the 20% mark.
Synthetic fixtures only; the launcher and the process probe are faked, nothing is spawned."""

import json
from pathlib import Path

import pytest

from companion.feature_team import dispatch, inspector, node_worker, runs
from tests.test_dispatcher import durable  # noqa: F401  the loop fixture (scratch store, fake clock)
from tests.test_inspector import (
    NOW,
    data,  # noqa: F401  the inspector data fixture
    inspect,
    launch,
    leases,
    records,
    successors,
)

REAL_LAUNCH = dispatch.launch_node  # test_dispatcher's autouse fixture replaces it; one test needs the real one


@pytest.fixture(autouse=True)
def no_real_worker_launch(monkeypatch):
    from companion.feature_team import autopilot

    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])


def queued(durable):  # noqa: F811
    root, _, _, store, _, leases_, _, _ = durable
    rec = store.load('demo-N0-first')
    rec.update(state='queued', deadline=2_000_000_000)
    runs._write_json(store.path(rec['run_id']), rec)
    return root, store, leases_, rec


def held(leases_, run_id):
    return [c for c in leases_.snapshot() if c['run_id'] == run_id]


# ---- part 1: the node worker ends an attempt out loud and frees a claim nobody is using ------------------------------

def test_an_invalid_result_from_an_exited_worker_frees_the_claim_and_logs_a_line(durable, monkeypatch, capsys):  # noqa: F811
    """The incident: the CLI wrote an invalid result and exited; the node worker ended the attempt as timeout and
    exited silently with the claim held, so the next tick refused the fresh retry for 'previous lease still held'."""
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(node_worker, '_alive', lambda pid: False)  # the CLI has exited
    log = root / 'cli.log'
    log.write_text('{"type": "result", "result": "done"}\n')

    def send(brief, message, **kwargs):
        Path(brief['result']).write_text(json.dumps({'run_id': brief['run_id']}))  # invalid: fields missing
        return f'STARTED: claude pid=4242 ledger_id=fixture-1 log={log}'

    monkeypatch.setattr(node_worker, 'send_channel', send)
    out = dispatch.run_fresh_node(rec['run_id'], root)
    assert out['state'] == 'timeout'
    assert held(leases_, rec['run_id']) == []
    release = [e for e in leases_.events() if e['kind'] == 'lock_release']
    assert [e['detail']['reason'] for e in release] == ['dead']
    printed = capsys.readouterr().out  # stdout is the worker log under launch_node
    assert f'node-worker {rec["run_id"]}: attempt ended timeout' in printed and 'claim released' in printed


def test_a_live_worker_keeps_its_claim_when_the_attempt_ends(durable, monkeypatch, capsys):  # noqa: F811
    """Never steal a live lease: a buffering CLI that wrote an early invalid result is still running."""
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)
    log = root / 'cli.log'
    log.write_text('{"type": "assistant"}\n')

    def send(brief, message, **kwargs):
        Path(brief['result']).write_text(json.dumps({'run_id': brief['run_id']}))
        return f'STARTED: claude pid=4242 ledger_id=fixture-1 log={log}'

    monkeypatch.setattr(node_worker, 'send_channel', send)
    assert dispatch.run_fresh_node(rec['run_id'], root)['state'] == 'timeout'
    assert len(held(leases_, rec['run_id'])) == 1
    assert 'claim kept' in capsys.readouterr().out


def test_a_send_refused_before_launch_frees_the_claim(durable, monkeypatch, capsys):  # noqa: F811
    """The successor case: refused at the send boundary (here the capacity recheck), so no worker ever started; the
    escalated record must not hold its files for 90 minutes."""
    from companion.feature_team import autopilot

    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: ['refused for the fixture'])
    monkeypatch.setattr(node_worker, 'send_channel', lambda *a, **k: pytest.fail('nothing may be sent'))
    out = dispatch.run_fresh_node(rec['run_id'], root)
    assert out['state'] == 'escalated'
    assert held(leases_, rec['run_id']) == []
    assert 'claim released' in capsys.readouterr().out


def test_a_launch_that_fails_writes_an_error_line_and_records_a_launch_failure(durable, monkeypatch):  # noqa: F811
    """A fake launcher that cannot start the node worker: the log names the error and the record is closed as a
    launch failure at once (the inspector owns its requeue), instead of an empty log and a dispatched record."""
    root, store, leases_, rec = queued(durable)
    store.transition(rec['run_id'], 'dispatched', deadline=rec['deadline'])

    def popen(*args, **kwargs):
        raise OSError('fixture: exec failed')

    monkeypatch.setattr(dispatch.subprocess, 'Popen', popen)
    with pytest.raises(OSError):
        REAL_LAUNCH(rec['run_id'], root=root)
    text = (root / 'team/loop' / f'{rec["run_id"]}.worker.log').read_text()
    assert 'launch failed' in text and 'fixture: exec failed' in text
    after = store.load(rec['run_id'])
    assert after['state'] == 'timeout' and node_worker.is_launch_failure(after)


def test_a_started_launch_records_the_node_worker_pid(durable, monkeypatch):  # noqa: F811
    root, store, leases_, rec = queued(durable)

    class Child:
        pid = 31337

    monkeypatch.setattr(dispatch.subprocess, 'Popen', lambda *a, **k: Child())
    REAL_LAUNCH(rec['run_id'], root=root)
    assert (root / 'team/loop' / f'{rec["run_id"]}.worker.pid').read_text().strip() == '31337'


# ---- part 2: the inspector closes a launch that never produced a receipt ---------------------------------------------

def dispatched_only(data, *, started=NOW - 180, pid=4242, log=''):  # noqa: F811
    """A run the dispatcher launched (claim held, node worker pid recorded) that never got a start receipt."""
    launch(data, state='dispatched', ledger=False, started=started)
    loop = data / 'team/loop'
    if pid is not None:
        (loop / 'demo-N1-1.worker.pid').write_text(f'{pid}\n')
    if log is not None:
        (loop / 'demo-N1-1.worker.log').write_text(log)
    assert leases(data, now=started).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 5000)


@pytest.mark.parametrize('pid,log', [(4242, ''), (4242, None)])
def test_a_dead_launch_past_the_receipt_wait_is_closed_its_claim_freed_and_requeued(data, pid, log):  # noqa: F811
    dispatched_only(data, pid=pid, log=log)
    inspect(data, alive=())
    rec = records(data)['demo-N1-1']
    assert rec['state'] == 'timeout' and rec['inspector_closed']['proof'] is True
    assert 'launch failure' in rec['history'][-1]['detail']
    assert leases(data).snapshot() == []
    assert [e['detail']['reason'] for e in leases(data).events() if e['kind'] == 'lock_release'] == ['dead']
    assert len(successors(data, 'demo-N1-1')) == 1
    text = (data / 'team/loop/demo-N1-1.worker.log').read_text()
    assert 'inspector' in text and 'launch failure' in text


def test_a_live_node_worker_keeps_its_lease_past_the_receipt_wait(data):  # noqa: F811
    dispatched_only(data)
    inspect(data, alive=(4242,))
    assert records(data)['demo-N1-1']['state'] == 'dispatched'
    assert len(leases(data).snapshot()) == 1 and successors(data, 'demo-N1-1') == []


def test_an_unknown_node_worker_pid_is_no_proof_and_keeps_the_lease(data):  # noqa: F811
    """No pid file (a record from before launch_node wrote one, or an UNCERTAIN send): never proof of death."""
    dispatched_only(data, pid=None, log=None)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'dispatched' and len(leases(data).snapshot()) == 1


def test_a_launch_inside_its_receipt_wait_is_left_alone(data):  # noqa: F811
    dispatched_only(data, started=NOW - 30)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'dispatched' and len(leases(data).snapshot()) == 1


def test_a_node_worker_that_wrote_to_its_log_is_not_a_silent_launch(data):  # noqa: F811
    dispatched_only(data, log='Traceback: something the 20% rule judges\n')
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'dispatched'


def test_the_receipt_wait_covers_the_node_workers_own_receipt_timeout():
    assert inspector.LAUNCH_RECEIPT_SECONDS >= 2 * node_worker.RECEIPT_TIMEOUT


# ---- review FIX-FIRST of c80f11a2 ------------------------------------------------------------------------------------

def ledger_row(data, sent_at):  # noqa: F811
    from datetime import datetime
    rec = records(data)['demo-N1-1']
    row = dict(id='claude-fixture', channel='claude', target=rec['brief']['worktree'],
               sent_at=datetime.fromtimestamp(sent_at).strftime('%Y-%m-%dT%H:%M:%S'))
    (data / 'context').mkdir(parents=True, exist_ok=True)
    (data / 'context/packages.jsonl').write_text(json.dumps(row) + '\n')


def test_a_live_courier_keeps_the_lease_when_the_node_worker_died(data):  # noqa: F811
    """Q1: lane_send starts the courier detached. A node worker SIGKILLed after the launch leaves a live courier;
    its pid on the record must keep the lease (dropping the alive check must turn this red)."""
    dispatched_only(data)
    rec = records(data)['demo-N1-1']
    rec['pid'] = 5555
    runs._write_json(data / 'team/loop/runs/demo-N1-1.json', rec)
    inspect(data, alive=(5555,))
    assert records(data)['demo-N1-1']['state'] == 'dispatched' and len(leases(data).snapshot()) == 1


def test_a_courier_ledger_row_since_dispatch_keeps_the_lease(data):  # noqa: F811
    """Q1: a ledger row for the run's worktree after dispatch means a courier was sent; with its pid unknown that
    is no proof of death, so it is not a silent launch."""
    dispatched_only(data)
    ledger_row(data, NOW - 170)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'dispatched' and len(leases(data).snapshot()) == 1


def test_an_older_ledger_row_for_the_worktree_does_not_count(data):  # noqa: F811
    dispatched_only(data)
    ledger_row(data, NOW - 3600)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'timeout'


def test_the_node_worker_logs_before_it_sends(durable, monkeypatch, capsys):  # noqa: F811
    """Q1: a node worker killed after it called the courier has a non-empty log, so it is never a silent launch."""
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)
    seen, log = [], root / 'cli.log'
    log.write_text('')

    def send(brief, message, **kwargs):
        seen.append(capsys.readouterr().out)
        Path(brief['result']).write_text(json.dumps({'run_id': brief['run_id']}))
        return f'STARTED: claude pid=4242 ledger_id=fixture-1 log={log}'

    monkeypatch.setattr(node_worker, 'send_channel', send)
    dispatch.run_fresh_node(rec['run_id'], root)
    assert seen and f'node-worker {rec["run_id"]}: sending' in seen[0]


def test_worker_gone_needs_every_started_courier_gone(monkeypatch):
    """Q2: on the correction path courier A may still run while B died; the claim is A's too."""
    monkeypatch.setattr(node_worker, '_alive', lambda pid: str(pid) == '1111')
    a, b = 'STARTED: claude pid=1111 ledger_id=a log=x', 'STARTED: claude pid=2222 ledger_id=b log=y'
    assert dispatch.worker_gone([a, b]) is False
    assert dispatch.worker_gone([b]) is True
    assert dispatch.worker_gone([dispatch.REFUSED, b]) is True
    assert dispatch.worker_gone([b, dispatch.UNKNOWN]) is False


def test_eperm_reads_as_alive_in_both_probes(monkeypatch):
    """A pid owned by another user (EPERM) is alive to the inspector as to node_worker: fail closed."""
    import os

    def kill(pid, sig):
        raise PermissionError(1, 'fixture: operation not permitted')

    monkeypatch.setattr(os, 'kill', kill)
    monkeypatch.setattr(inspector.subprocess, 'run', lambda *a, **k: type('Done', (), {'stdout': 'S'})())
    assert node_worker._alive(4242) is True
    assert inspector.pid_alive(4242) is True
