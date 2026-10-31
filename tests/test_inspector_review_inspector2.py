"""Independent review (inspector2) of session/2026-10-15-inspector at 7b8f3efe. Lens: safety of live state.

Drop into tests/. Probes named test_probe_* that fail mark a defect; test_pin_* pass and pin behaviour."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from companion.feature_team import autopilot, dispatch, inspector, node_worker, watchdog
from tests.test_inspector import (
    NOW,
    TEAM,
    inspect,
    launch,
    leases,
    records,
    store,
    successors,
    write,
)
from tests.test_node_worker import case, write_result  # noqa: F401  (fixture)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    write(root / 'team/features/demo/completion.json', {'nodes': {}})
    return root


# ---- Q1: a live CLI is never a launch failure -----------------------------------------------------------------------

def test_probe_a_live_worker_with_an_empty_log_and_an_invalid_result_is_not_a_launch_failure(case, tmp_path,  # noqa: F811
                                                                                              monkeypatch):
    """A CLI that buffers its stdout (the log stays 0 bytes until it exits) writes an invalid result early and is
    still running. Only an EXITED worker with an empty log is a launch failure; this one must get its correction."""
    store_, brief, _, _, _, kwargs = case
    log = tmp_path / 'worker.log'
    log.write_text('')
    monkeypatch.setattr(node_worker, '_alive', lambda pid: True)       # the worker is alive the whole time
    calls = []

    def send(prepared, message, *, receipt_timeout):
        calls.append(message)
        write_result(prepared, {'run_id': prepared['run_id']})         # invalid result, written at once
        return f'STARTED: claude pid=4242 ledger_id=fixture-{len(calls)} log={log}'

    out = node_worker.run_node(brief['run_id'], send=send, **kwargs)
    assert not node_worker.is_launch_failure(out), (
        f'a live worker (pid alive) was classified a launch failure: {out["history"][-1]["detail"][:160]}')


def test_pin_a_live_launch_failure_close_keeps_its_unexpired_claim_and_mints_nothing(data, monkeypatch):
    s = launch(data, started=NOW - 180, pid=4242)
    assert leases(data, now=NOW - 180).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 30
    s.transition('demo-N1-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker ended in 150 s with an '
                                                'empty log (x.log); its result was not valid (x)')
    inspect(data, alive=(4242,))
    assert successors(data, 'demo-N1-1') == []
    assert records(data)['demo-N1-1']['inspector_closed']['proof'] is False
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW + 60, dry_run=False)
    assert [c['run_id'] for c in leases(data, now=NOW + 60).snapshot()] == ['demo-N1-1']


def test_pin_a_live_launch_failure_with_a_blocked_requeue_stays_closed_and_keeps_its_claim(data, monkeypatch):
    write(data / 'team/features/demo/team.json', TEAM[:1])              # no other channel: blocked
    s = launch(data, started=NOW - 180, pid=4242)
    assert leases(data, now=NOW - 180).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 30
    s.transition('demo-N1-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker ended in 150 s')
    inspect(data, alive=(4242,))
    inspect(data, now=NOW + 60, alive=(4242,))
    assert records(data)['demo-N1-1']['state'] == 'timeout'
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW + 120, dry_run=False)
    assert [c['run_id'] for c in leases(data, now=NOW + 120).snapshot()] == ['demo-N1-1']


def test_pin_a_dead_attempt_0_never_proves_a_live_attempt_1_dead(data):
    """Attempt 0's pid is gone; attempt 1 (same record) has its own ledger row whose pid is alive."""
    s = launch(data, started=NOW - 20 * 60, pid=4242)
    assert leases(data, now=NOW - 20 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 10 * 60
    s.transition('demo-N1-1', 'timeout', detail='invalid result; one correction dispatch follows')
    s.transition('demo-N1-1', 'dispatched', detail='fresh channel dispatch', deadline=NOW + 3000)
    s.transition('demo-N1-1', 'started', detail='ledger_id=muse-second')
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(dict(id='muse-second', channel='muse', pid=5151, receipt='muse started pid 5151')) + '\n')
    inspect(data, alive=(5151,))
    assert records(data)['demo-N1-1']['state'] == 'started' and successors(data, 'demo-N1-1') == []
    assert [c['run_id'] for c in leases(data).snapshot()] == ['demo-N1-1']


# ---- Q3: the whole retry chain -----------------------------------------------------------------------------------------

def test_pin_a_requeue_skips_every_channel_on_a_three_link_retry_chain(data):
    launch(data, 'demo-N1-a', started=NOW - 600, pid=1, channel='muse:0')
    store(data, NOW - 590).transition('demo-N1-a', 'timeout', detail='x')
    launch(data, 'demo-N1-b', started=NOW - 500, pid=2, channel='codex-exec:1', retry_of='demo-N1-a')
    store(data, NOW - 490).transition('demo-N1-b', 'timeout', detail='x')
    launch(data, 'demo-N1-c', started=NOW - 60, pid=3, channel='muse:1', retry_of='demo-N1-b')
    inspect(data)                                                       # pid 3 gone: dead on muse:1
    assert [r['brief']['channel'] for r in successors(data, 'demo-N1-c')] == ['claude:opus']


# ---- Q2: human pauses ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text', ['["muse:0"]', '', '{"muse:0": {"reason": "the owner"}'])
def test_pin_a_non_object_or_broken_budget_file_is_never_written(data, text):
    write(data / 'team/model-budget.json', text)
    insp = inspector.Inspector(data, store=store(data), now=NOW, alive=lambda p: False, commits=lambda r: 0)
    insp.pause_channel('muse:0', [dict(t=NOW, run_id='a', why='x'), dict(t=NOW, run_id='b', why='y')])
    assert (data / 'team/model-budget.json').read_text() == text


def test_pin_human_entries_keep_their_values_when_the_inspector_pauses_another_channel(data):
    human = {'muse:0': {'reason': 'the owner: off'}, 'claude': 'stop', 'muse:1': {'paused_until': '2020-01-01T00:00:00+00:00',
                                                                           'reason': 'old human pause'}}
    write(data / 'team/model-budget.json', json.dumps(human))
    insp = inspector.Inspector(data, store=store(data), now=NOW, alive=lambda p: False, commits=lambda r: 0)
    for channel in ('muse:0', 'muse:1', 'claude', 'codex-exec:1'):
        insp.pause_channel(channel, [dict(t=NOW, run_id='a', why='x'), dict(t=NOW, run_id='b', why='y')])
    after = json.loads((data / 'team/model-budget.json').read_text())
    assert {k: after[k] for k in human} == human and after['codex-exec:1']['by'] == 'inspector'


# ---- Q4: the tick stays alive --------------------------------------------------------------------------------------

def test_pin_a_hung_ps_reads_alive_within_its_timeout(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get('timeout'))
    monkeypatch.setattr(inspector.subprocess, 'run', hang)
    assert inspector.pid_alive(os.getpid()) is True                       # exists; unknown state = alive


def test_pin_a_starved_inspector_whose_inbox_append_fails_still_finishes_the_tick(data, monkeypatch):
    lock = data / 'team/dispatcher.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, '-c', 'import fcntl, sys\nf = open(sys.argv[1], "a")\n'
                               'fcntl.flock(f, fcntl.LOCK_EX); print("held", flush=True); sys.stdin.readline()',
                               str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        write(data / 'team/inspector-state.json', {'lock_skips': inspector.STARVED_TICKS - 1})

        def boom(*a, **k):
            raise OSError('inbox disk full')
        monkeypatch.setattr(inspector.board_inbox, 'append', boom)
        tick = watchdog.Tick(data, dispatcher=lambda dry: {}, git=lambda args: True, clock=lambda: NOW)
        assert tick.check_inspector() is None                           # caught; the other checks still run
    finally:
        holder.stdin.write('\n')
        holder.stdin.flush()
        holder.wait()


# ---- Q5: the dry run writes nothing --------------------------------------------------------------------------------

def tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


def test_probe_the_inspector_dry_run_writes_nothing(data):
    write(data / 'team/model-budget.json', '{"muse:1": {"reason": "the owner"}}')
    write(data / 'team/inspector-state.json', {'channel_fails': {'muse:0': [dict(t=NOW - 60, run_id='old', why='x')]},
                                               'requeued': {}, 'node_requeues': {}, 'posted': {}})
    launch(data, 'demo-N1-1', 'N1', started=NOW - 60, pid=4242)                      # dead: close + requeue
    s = launch(data, 'demo-N2-1', 'N2', started=NOW - 180, pid=4343)                 # launch failure
    assert leases(data, now=NOW - 180).claim('demo-N2-1', 'dispatcher', ['src/companion/n2.py'], NOW + 3000)
    s.clock = lambda: NOW - 30
    s.transition('demo-N2-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker ended in 150 s')
    launch(data, 'demo-N3-1', 'N3', started=NOW - 100 * 60, pid=4444)                # overdue, alive
    before = tree(data)
    st = store(data)
    inspector.Inspector(data, store=st, now=NOW, alive=lambda p: p == 4444, commits=lambda r: 0,
                        dry_run=True).run(st.load_all())
    after = tree(data)
    assert after == before, f'dry run changed: {sorted(set(after.items()) ^ set(before.items()))}'


def test_probe_the_watchdog_dry_run_tick_writes_nothing(data):
    launch(data, 'demo-N1-1', 'N1', started=NOW - 60, pid=4242)
    assert leases(data, now=NOW - 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    before = tree(data)
    watchdog.tick(data, dispatcher=lambda dry: {}, git=lambda args: True, clock=lambda: NOW, dry_run=True,
                  probes=dict(alive=lambda pid: False, commits=lambda rec: 0))
    after = tree(data)
    assert after == before, f'dry run changed: {sorted(set(after.items()) ^ set(before.items()))}'
