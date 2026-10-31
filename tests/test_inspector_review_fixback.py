"""Independent review of the inspector fix-back (b9cdd04f), ported by the author: each probe failed on b9cdd04f."""

import json
import subprocess
import sys
import threading

import pytest

from companion.feature_team import autopilot, dispatch, inspector
from tests.test_inspector import NOW, TEAM, inspect, launch, leases, records, store, successors, write


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {}})
    return root


# Q1: a no-proof close whose requeue is blocked goes to escalated; the dispatcher then frees the claim as 'result'.

def test_probe_no_proof_close_with_blocked_requeue_keeps_the_live_lease(data, monkeypatch):
    """A run still waiting for its start receipt (state dispatched, no ledger id, no pid: the UNCERTAIN worker the
    dispatcher protects with 'previous lease still held') shows no life at 30% elapsed. The inspector closes it with
    proof False. With no other channel the requeue is blocked, so the inspector moves it to escalated, and the
    dispatcher's escalated branch releases every claim with reason 'result', before the lease's deadline."""
    write(data / 'team/features/demo/team.json', TEAM[:1])
    launch(data, started=NOW - 15 * 60, pid=None, ledger=False, state='dispatched')
    assert leases(data, now=NOW - 15 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    inspect(data)
    rec = records(data)['demo-N1-1']
    assert rec['inspector_closed']['proof'] is False
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW + 60, dry_run=False)
    assert [c['run_id'] for c in leases(data, now=NOW + 60).snapshot()] == ['demo-N1-1'], (
        f'unexpired lease of a worker never proven dead was released (state {records(data)["demo-N1-1"]["state"]})')


# Q3: the second requeue may go back to the channel that already killed this node.

def test_probe_second_requeue_never_returns_to_the_channel_that_killed_the_node(data):
    write(data / 'team/features/demo/team.json', [TEAM[0], TEAM[3]])   # muse:0 and codex-exec:1
    launch(data, started=NOW - 60, pid=4242)                           # pid gone: dead on muse:0
    inspect(data)
    (first,) = successors(data, 'demo-N1-1')
    assert first['brief']['channel'] == 'codex-exec:1'
    rid, s = first['run_id'], store(data, NOW + 60)
    s.transition(rid, 'dispatched', deadline=NOW + 5400)
    s.transition(rid, 'started', detail=f'ledger_id=cx-{rid}')
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id=f'cx-{rid}', channel='codex-exec', pid=5151, receipt='codex started pid 5151')) + '\n')
    inspect(data, now=NOW + 120)                                       # pid 5151 gone: dead on codex-exec:1
    second = successors(data, rid)
    assert [r['brief']['channel'] for r in second] != ['muse:0'], 'requeued back onto muse:0, where this node died'


# Q2: an unreadable model-budget.json (a human mid-edit) must never be replaced.

def test_probe_an_unreadable_model_budget_is_never_overwritten(data):
    broken = '{"muse:0": {"reason": "owner: muse:0 off"},\n  "claude": {"reason": "Claude stop"\n'
    write(data / 'team/model-budget.json', broken)
    write(data / 'team/features/demo/team.json', TEAM[:2])
    launch(data, 'demo-N1-1', 'N1', channel='muse:0', started=NOW - 60, pid=4242)
    launch(data, 'demo-N2-1', 'N2', channel='muse:0', started=NOW - 60, pid=4343)
    inspect(data)                                                      # two deaths on muse:0 -> pause_channel
    assert (data / 'team/model-budget.json').read_text() == broken, 'the human file was replaced'


# Q6: the inspector's pause waits on the same lock budget_mode holds (pin).

def test_pin_the_inspector_pause_waits_for_the_budget_lock(data):
    lock = data / 'team/model-budget.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, '-c', 'import fcntl, sys, time\n'
                               'f = open(sys.argv[1], "a"); fcntl.flock(f, fcntl.LOCK_EX); print("held", flush=True)\n'
                               'sys.stdin.readline()', str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        insp = inspector.Inspector(data, store=store(data), now=NOW, alive=lambda p: False, commits=lambda r: 0)
        fails = [dict(t=NOW, run_id='a', why='x'), dict(t=NOW, run_id='b', why='y')]
        worker = threading.Thread(target=lambda: insp.pause_channel('muse:0', fails), daemon=True)
        worker.start()
        worker.join(timeout=1.5)
        blocked = worker.is_alive()
        assert not (data / 'team/model-budget.json').exists()
    finally:
        holder.stdin.write('\n')
        holder.stdin.flush()
        holder.wait()
    worker.join(timeout=10)
    assert blocked and not worker.is_alive()
    assert json.loads((data / 'team/model-budget.json').read_text())['muse:0']['by'] == 'inspector'


# Q5: exact rule (pin).

@pytest.mark.parametrize('row, want', [
    ({'status': 'dispatched', 'by': 'board'}, True),
    ({'status': 'dispatched', 'by': 'Board'}, False),
    ({'status': 'dispatched', 'by': 'board '}, False),
    ({'status': 'queued', 'by': 'board'}, False),
    ({'status': 'dispatched', 'by': 'x', 'receipt': 'board'}, False),
    ('board', False),
])
def test_pin_hand_built_is_exact(row, want):
    assert inspector.hand_built(row) is want


# ---- the author's follow-ups ---------------------------------------------------------------------------------------

def test_a_blocked_no_proof_close_escalates_once_its_claim_has_expired(data):
    write(data / 'team/features/demo/team.json', TEAM[:1])
    launch(data, started=NOW - 15 * 60, pid=None, ledger=False, state='dispatched')
    assert leases(data, now=NOW - 15 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 600)
    inspect(data)
    assert records(data)['demo-N1-1']['state'] == 'timeout'
    inspect(data, now=NOW + 300)
    assert records(data)['demo-N1-1']['state'] == 'timeout'           # claim still unexpired: no escalation
    inspect(data, now=NOW + 700)
    assert records(data)['demo-N1-1']['state'] == 'escalated'
    assert leases(data, now=NOW + 700).snapshot() == []                # released as timeout, at its deadline


def test_the_dispatcher_releases_an_escalated_runs_claim_only_after_its_deadline(data, monkeypatch):
    s = launch(data, started=NOW - 60, pid=4242)
    assert leases(data, now=NOW - 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 600)
    s.transition('demo-N1-1', 'escalated', detail='node worker failed')
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    table = [dict(channel='muse:0', role='engineer')]
    dispatch.loop_tick(data, table, [], clock=lambda: NOW + 60, dry_run=False)
    assert [c['run_id'] for c in leases(data, now=NOW + 60).snapshot()] == ['demo-N1-1']
    dispatch.loop_tick(data, table, [], clock=lambda: NOW + 700, dry_run=False)
    assert leases(data, now=NOW + 700).snapshot() == []
    reasons = [e['detail']['reason'] for e in leases(data).events() if e['kind'] == 'lock_release']
    assert 'result' not in reasons


def test_an_unreadable_budget_file_is_logged_not_replaced(data):
    write(data / 'team/model-budget.json', '{"muse:0": ')
    lines = []
    insp = inspector.Inspector(data, store=store(data), now=NOW, alive=lambda p: False, commits=lambda r: 0,
                               line=lambda *a, **k: lines.append(a))
    insp.pause_channel('muse:0', [dict(t=NOW, run_id='a', why='x'), dict(t=NOW, run_id='b', why='y')])
    assert (data / 'team/model-budget.json').read_text() == '{"muse:0": '
    assert any('unreadable' in str(a) for a in lines)


def test_a_record_without_created_skips_only_itself(data):
    launch(data, started=NOW - 60, pid=4242)
    write(data / 'team/loop/runs/demo-N9-1.json', {'run_id': 'demo-N9-1', 'state': 'started'})
    summary = inspect(data)
    assert summary['errors'] == 0 and summary['dead'] == 1           # R0's reader skips the stub, as a torn file
    assert records(data)['demo-N1-1']['state'] == 'timeout' and 'demo-N9-1' not in records(data)
    from companion.feature_team.inspector import Inspector
    lines = []
    s = store(data)
    Inspector(data, store=s, now=NOW, alive=lambda p: False, commits=lambda r: 0,
              line=lambda *a, **k: lines.append(a)).run({**s.load_all(), 'demo-N9-1': {'run_id': 'demo-N9-1'}})
    assert any('record without created' in str(a) for a in lines)    # and the inspector skips it if handed one
