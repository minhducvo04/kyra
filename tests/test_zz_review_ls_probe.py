"""Review o-rev-ls probes on c80f11a2 (launch-silent + "hosted through"). Synthetic fixtures only; nothing spawned.

RED on c80f11a2 (defects):  test_p1_*, test_p2_*.
GREEN on c80f11a2 but each kills a mutation that survived the builder's tests: test_p3_*, test_p4_*, test_p5_*.
GREEN, refutes the second lens's item 2: test_p6_*.
XFAIL (strict), records the second lens's item 3 for the owner: test_p7_*."""

import json
import pathlib
from pathlib import Path

import pytest

from companion.feature_team import dispatch, node_worker
from tests.test_dispatcher import durable  # noqa: F401  the loop fixture
from tests.test_inspector import NOW, data, inspect, launch, leases, records, store  # noqa: F401
from tests.test_launch_silent import dispatched_only, held, queued


@pytest.fixture(autouse=True)
def no_capacity_refusal(monkeypatch):
    from companion.feature_team import autopilot

    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda _, channel=None: [])


# ---- P1 (RED): an empty worker log must prove that no courier was started ---------------------------------------
# silent_launch frees the lease on "node worker pid dead + worker log empty + no started transition". The courier
# (lane_send, start_new_session=True) outlives a node worker killed during send's receipt wait (up to RECEIPT_TIMEOUT)
# or before the 'started' transition; its pid is on no record the inspector reads in the dispatched state, so a live
# courier loses its lease and a successor is minted over the same files. The node worker must say something in its
# log before any courier can start (or record the courier pid where the inspector reads it).

def test_p1_the_worker_log_is_not_empty_when_a_courier_can_start(durable, monkeypatch, capsys):  # noqa: F811
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    seen = {}

    class Killed(BaseException):  # stands in for SIGKILL/SIGTERM during the courier's receipt wait
        pass

    def send(brief, message, **kwargs):
        seen['log_at_launch'] = capsys.readouterr().out  # stdout is the worker log under launch_node
        raise Killed

    monkeypatch.setattr(node_worker, 'send_channel', send)
    with pytest.raises(Killed):
        dispatch.run_fresh_node(rec['run_id'], root)
    assert seen['log_at_launch'].strip(), 'a courier may be running while the worker log is still empty'


# ---- P2 (RED): fail closed on an unreadable worker log -------------------------------------------------------------

def test_p2_an_unreadable_worker_log_keeps_the_lease(data, monkeypatch):  # noqa: F811
    dispatched_only(data, log='')
    real_stat = pathlib.Path.stat

    def stat(self, *args, **kwargs):
        if self.name.endswith('.worker.log'):
            raise PermissionError(13, 'fixture: permission denied', str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'stat', stat)
    inspect(data, alive=())
    monkeypatch.setattr(pathlib.Path, 'stat', real_stat)
    assert records(data)['demo-N1-1']['state'] == 'dispatched'
    assert len(leases(data).snapshot()) == 1


# ---- P3 (GREEN; kills "unknown pid as dead" and "UNKNOWN counted as refused" in worker_gone) -----------------------

@pytest.mark.parametrize('last', [
    'STARTED: cursor ledger_id=fixture-1 log=/nonexistent/cursor.log',  # cursor receipts carry no pid
    'UNCERTAIN: fixture launch unconfirmed',
    None,
    dispatch.UNKNOWN,
])
def test_p3_an_attempt_end_without_a_dead_pid_keeps_the_claim(durable, last, capsys):  # noqa: F811
    root, store, leases_, rec = queued(durable)
    dispatch.end_attempt(dict(rec, state='timeout'), root, [last])
    assert len(held(leases_, rec['run_id'])) == 1
    assert 'claim kept' in capsys.readouterr().out


# ---- P4 (GREEN; kills "a send error after a possible launch is recorded as REFUSED") -------------------------------

def test_p4_a_send_error_after_a_possible_launch_keeps_the_claim(durable, monkeypatch, capsys):  # noqa: F811
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)

    def send(brief, message, **kwargs):
        raise OSError('fixture: the courier died after Popen')

    monkeypatch.setattr(node_worker, 'send_channel', send)
    out = dispatch.run_fresh_node(rec['run_id'], root)
    assert out['state'] in {'timeout', 'escalated'}
    assert len(held(leases_, rec['run_id'])) == 1
    assert 'claim kept' in capsys.readouterr().out


# ---- P5 (GREEN; kills "ignore ev['alive']" and "drop the started check" in silent_launch) --------------------------

def test_p5a_a_live_courier_pid_on_a_dispatched_record_keeps_the_lease(data):  # noqa: F811
    dispatched_only(data, log='')
    rec = records(data)['demo-N1-1']
    rec['pid'] = 5151
    store(data).path('demo-N1-1').write_text(json.dumps(rec))
    inspect(data, alive=(5151,))  # node worker 4242 gone, courier 5151 alive
    assert records(data)['demo-N1-1']['state'] == 'dispatched' and len(leases(data).snapshot()) == 1


def test_p5b_a_started_run_with_a_dead_node_worker_and_a_pidless_courier_keeps_the_lease(data):  # noqa: F811
    launch(data, state='started', ledger=False, started=NOW - 180)  # a cursor-like courier: no pid on record
    loop = data / 'team/loop'
    (loop / 'demo-N1-1.worker.pid').write_text('4242\n')
    (loop / 'demo-N1-1.worker.log').write_text('')
    assert leases(data, now=NOW - 180).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 5000)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'started' and len(leases(data).snapshot()) == 1


# ---- P6 (GREEN): the second lens's item 2 does not hold under run_fresh_node ----------------------------------------
# run_fresh_node's `once` turns every timeout transition (including run_node's "invalid result; one correction
# dispatch follows") into AttemptEnded, so one node-worker process makes at most one send; a retry is a successor
# with a new run_id. worker_gone's "last send" is therefore the only send.

def test_p6_one_node_worker_process_sends_at_most_once(durable, monkeypatch):  # noqa: F811
    root, store, leases_, rec = queued(durable)
    monkeypatch.setattr(node_worker, '_worktree', lambda _: None)
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)
    log = root / 'cli.log'
    log.write_text('{"type": "assistant"}\n')
    calls = []

    def send(brief, message, **kwargs):
        calls.append(1)
        Path(brief['result']).write_text(json.dumps({'run_id': brief['run_id']}))  # invalid result
        return f'STARTED: claude pid=4242 ledger_id=fixture-{len(calls)} log={log}'

    monkeypatch.setattr(node_worker, 'send_channel', send)
    assert dispatch.run_fresh_node(rec['run_id'], root)['state'] == 'timeout'
    assert len(calls) == 1


# ---- P7 (was a strict xfail; passes since the area-lock brake, 2026-10-16) ------------------------------------------
# With "hosted through", a hosted builder runs while an area train (scripts/train.py: gate-landing-<area>.lock) lands.
# companion.priority now reads the area locks too (the owner's standing headroom rule): only NEW lower-priority workers get
# PYTEST_ADDOPTS "-n 1" while one is held. An area train never runs --pause-lower, so running workers are not paused.

def test_p7_an_area_landing_lock_is_seen_by_priority_admission(tmp_path):
    from companion import priority

    team = tmp_path / 'team'
    team.mkdir()
    (team / 'gate-landing-loop.lock').write_text(json.dumps({'pid': 1, 'priority': 1}))
    assert priority.protected_holder(team, live=lambda info: True) is not None
