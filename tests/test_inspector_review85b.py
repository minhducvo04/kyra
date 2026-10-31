"""Review o-rev85b probes for the inspector fix-back b9cdd04f, ported by the author (3 failed on b9cdd04f).

A probe that fails names a defect; a probe that passes pins a behaviour the review relied on."""

import json

import pytest

from companion.feature_team import events, inspector
from tests.test_inspector import NOW, TEAM, inbox, launch, leases, records, store, successors, write


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


def _ledger(data, ledger_id, pid=None, failed=False):
    log = data.parent / 'logs' / f'{ledger_id}.log'
    write(log, '')
    row = dict(id=ledger_id, channel='muse', target='wt', sent_at='2027-01-15T08:00:00',
               receipt=f'muse started pid {pid} in wt (log {log}) ledger_id={ledger_id}')
    if pid:
        row['pid'] = pid
    with (data / 'context/packages.jsonl').open('a') as f:
        f.write(json.dumps(row) + '\n')
        if failed:
            f.write(json.dumps(dict(id=ledger_id, status='failed', passed_at='2027-01-15T08:05:00',
                                    last_text='Keychain error', exit_code=1)) + '\n')


def _correction_attempt(data, *, started_ledger=None):
    """node_worker's in-record correction: attempt 0 (ledger muse-demo-N1-1, pid 4242) wrote an invalid result, the
    record went timeout with the archive as partial, then a fresh dispatch of attempt 1 on the same run_id."""
    s = launch(data, started=NOW - 40 * 60, pid=4242)               # attempt 0: started ledger_id=muse-demo-N1-1
    s.clock = lambda: NOW - 30 * 60
    s.transition('demo-N1-1', 'timeout', path=str(data / 'team/loop/results/demo-N1-1.json.invalid-1'),
                 detail='invalid result; one correction dispatch follows')
    s.transition('demo-N1-1', 'dispatched', detail='fresh channel dispatch', deadline=NOW - 40 * 60 + 5400)
    if started_ledger:
        s.transition('demo-N1-1', 'started', detail=f'ledger_id={started_ledger}')
    return s


# ---- Q2 proof of death must belong to the current attempt ------------------------------------------------------------

def test_probe_a_previous_attempts_dead_pid_is_not_proof_for_the_correction_attempt(data):
    """Attempt 1's send came back UNCERTAIN (no ledger row, so no pid), node_worker closed it 'start receipt timeout'
    with attempt 1's worker possibly alive and its lease unexpired. evidence() takes the newest ledger_id in the whole
    history, which is attempt 0's; attempt 0's pid is gone, so proof_dead is true and the live lease is freed as dead
    and a successor minted over it: the o-rev85 defect 1 again, through a stale attempt's evidence."""
    s = _correction_attempt(data)
    assert leases(data, now=NOW - 40 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 20 * 60
    s.transition('demo-N1-1', 'timeout', detail='start receipt timeout; no confirmed ledger id')
    inspect(data, alive=())                                          # 4242 (attempt 0) is gone; attempt 1 unknown
    assert [c['run_id'] for c in leases(data).snapshot()] == ['demo-N1-1'], 'lease freed on a stale attempt\'s pid'
    assert successors(data, 'demo-N1-1') == []


def test_probe_a_dead_correction_attempt_is_caught_despite_the_previous_attempts_partial(data):
    """Attempt 1 started (its own ledger row, its exit supervisor wrote failed: the Muse Keychain case). The partial
    on the record is attempt 0's invalid-result archive, not attempt 1's output, yet evidence() counts it as a result,
    so classify never says dead (nor behind nor overdue) and the dead worker sits until the 90 min deadline."""
    _correction_attempt(data, started_ledger='muse-demo-N1-1-b')
    _ledger(data, 'muse-demo-N1-1-b', pid=5151, failed=True)
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'timeout', 'a dead correction attempt was not caught'


def test_probe_a_dead_successor_that_inherited_a_partial_is_caught(data):
    """A fresh retry (dispatcher or inspector) copies the predecessor's partial onto the successor; the successor's own
    worker then dies at start. Its pid is gone and it wrote nothing, yet the inherited partial reads as its result."""
    launch(data, started=NOW - 60, pid=4242, partial=str(data / 'team/loop/results/demo-N1-0.json'),
           retry_of='demo-N1-0')
    inspect(data, alive=())
    assert records(data)['demo-N1-1']['state'] == 'timeout', 'dead successor shielded by an inherited partial'


# ---- Q2 pins: the safe directions ------------------------------------------------------------------------------------

def test_pin_a_ledger_row_of_another_run_is_never_proof(data):
    """Ledger ids are uuid-minted per send; a failed row for another id does not close this run."""
    launch(data, started=NOW - 60, pid=4242)
    _ledger(data, 'muse-other-run', pid=9999, failed=True)
    inspect(data, alive={4242})
    assert records(data)['demo-N1-1']['state'] == 'started'


def test_pin_a_reused_pid_reads_alive_and_frees_nothing_early(data):
    s = launch(data, started=NOW - 40 * 60, pid=4242)
    assert leases(data, now=NOW - 40 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 20 * 60
    s.transition('demo-N1-1', 'timeout', detail='start receipt timeout; no confirmed ledger id')
    inspect(data, alive={4242})
    assert [c['run_id'] for c in leases(data).snapshot()] == ['demo-N1-1']


# ---- Q5 verify_fail: one row, to the Board for the owner, never on a held node ---------------------------------------

def _verify_fail(data, rid='demo-N1-1', node='N1'):
    s = launch(data, rid, node, started=NOW - 60 * 60, pid=4242)
    s.clock = lambda: NOW - 40 * 60
    s.transition(rid, 'result', path=None, result=dict(json.loads(json.dumps(dict(
        run_id=rid, feature='demo', node=node))), **_result_fields(data, rid)))
    s.transition(rid, 'verify_fail', path=str(data / 'verify.txt'))
    return s


def _result_fields(data, rid):
    sample = json.loads((__import__('pathlib').Path(__file__).parent / 'data/loop/result-ok.json').read_text())
    return {k: v for k, v in sample.items() if k not in ('run_id', 'feature', 'node')}


def test_pin_verify_fail_gets_exactly_one_row_across_ticks_and_no_requeue(data):
    _verify_fail(data)
    for minute in (0, 5, 10):
        inspect(data, now=NOW + minute * 60)
    rows = [r for r in inbox(data) if r.get('routing_id') == 'inspector:demo-N1-1:verify_fail']
    assert len(rows) == 1 and rows[0]['to'] == 'board' and 'muse:0' in rows[0]['evidence']
    assert successors(data, 'demo-N1-1') == [] and records(data)['demo-N1-1']['state'] == 'verify_fail'


def test_pin_verify_fail_on_a_board_held_node_is_untouched(data):
    _verify_fail(data)
    write(data / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'dispatched', 'by': 'board'}}})
    inspect(data)
    assert inbox(data) == []


# ---- Q3 the inspector's own half of the model-budget.lock ------------------------------------------------------------

def test_pin_the_inspectors_channel_pause_waits_on_model_budget_lock(data):
    """test_budget_mode_takes_the_same_lock_as_the_inspector only checks budget_lock()'s path on the inspector side;
    with the inspector's `with dispatch.locked(...)` removed every existing test still passes. This pins the wait."""
    import fcntl
    import threading
    write(data / 'team/features/demo/team.json', TEAM[:2])
    launch(data, 'demo-N1-1', 'N1', channel='muse:0', started=NOW - 60, pid=4242)
    launch(data, 'demo-N2-1', 'N2', channel='muse:0', started=NOW - 60, pid=4343)
    holder = (data / 'team/model-budget.lock').open('a')
    fcntl.flock(holder, fcntl.LOCK_EX)
    worker = threading.Thread(target=lambda: inspect(data), daemon=True)
    worker.start()
    worker.join(timeout=2)
    blocked = worker.is_alive()
    holder.close()
    worker.join(timeout=20)
    assert blocked and not worker.is_alive()
    assert json.loads((data / 'team/model-budget.json').read_text())['muse:0']['by'] == 'inspector'


# ---- the author's follow-ups: the risks o-rev85b named, and launch failures ------------------------------------------

def test_starved_inspector_posts_one_row_after_three_skips_in_a_row(data):
    for _ in range(4):
        inspector.note_lock_skip(data, NOW)
    rows = [r for r in inbox(data) if 'inspector starved' in r['text']]
    assert len(rows) == 1 and rows[0]['to'] == 'board'
    assert json.loads((data / 'team/inspector-state.json').read_text())['lock_skips'] == 4
    inspect(data)                                                     # the inspector ran: the streak ends
    assert json.loads((data / 'team/inspector-state.json').read_text())['lock_skips'] == 0


def test_a_paused_alternative_leaves_the_dead_run_closed_not_escalated(data):
    """The only other channel is under the inspector's 12 h pause: the run waits (timeout + inspector_closed, which
    the dispatcher skips) with one fixer row, and is never closed to final escalated."""
    write(data / 'team/features/demo/team.json', TEAM[:2])
    write(data / 'team/model-budget.json', {'muse:1': {'paused_until': '2099-01-01T00:00:00+00:00',
                                                       'reason': 'inspector: 2 dead runs', 'by': 'inspector'}})
    launch(data, started=NOW - 60, pid=4242)
    inspect(data)
    inspect(data, now=NOW + 300)
    rec = records(data)['demo-N1-1']
    assert rec['state'] == 'timeout' and rec['inspector_closed'] and successors(data, 'demo-N1-1') == []
    rows = [r for r in inbox(data) if r['kind'] == 'inspector']
    assert len(rows) == 1 and 'paused' in rows[0]['text']


def test_the_dry_run_reports_a_wait_for_an_unexpired_claim_not_a_requeue(data):
    s = launch(data, started=NOW - 40 * 60, pid=4242)
    assert leases(data, now=NOW - 40 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 20 * 60
    s.transition('demo-N1-1', 'timeout', detail='start receipt timeout; no confirmed ledger id')
    lines = []
    st = store(data)
    inspector.Inspector(data, store=st, now=NOW, alive=lambda p: True, commits=lambda r: 0, dry_run=True,
                        line=lambda *a, **k: lines.append(' '.join(map(str, a)) + ' ' + str(k.get('result', '')))
                        ).run(st.load_all())
    text = '\n'.join(lines)
    assert 'waits for its unexpired file claim' in text and 'would requeue' not in text


def test_a_launch_failure_timeout_is_requeued_on_another_channel_and_counted(data, monkeypatch):
    from companion.feature_team import autopilot, dispatch, node_worker
    s = launch(data, started=NOW - 180, pid=4242)
    s.clock = lambda: NOW - 30
    s.transition('demo-N1-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker ended in 150 s with an '
                                                f'empty log (x.log); its result was not valid (proof: empty)')
    launched = []
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: launched.append(run_id))
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW, dry_run=False)
    assert launched == [] and successors(data, 'demo-N1-1') == []    # the dispatcher leaves it to the inspector
    summary = inspect(data)
    (new,) = successors(data, 'demo-N1-1')
    assert new['brief']['channel'] != 'muse:0' and summary['dead'] == 1
    state = json.loads((data / 'team/inspector-state.json').read_text())
    assert [f['run_id'] for f in state['channel_fails']['muse:0']] == ['demo-N1-1']


def test_a_g1_shaped_dead_worker_frees_its_lease_as_dead(data):
    """3d-inner-demo G1: the worker exited (its recorded pid is gone), its log is 0 bytes, the claim would hold until
    the deadline. The gone pid is the proof; the claim is released as dead and the node requeued."""
    s = launch(data, started=NOW - 20 * 60, pid=4242, log_text='')
    assert leases(data, now=NOW - 20 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 16 * 60
    s.transition('demo-N1-1', 'timeout', detail='invalid result; one correction dispatch follows')
    inspect(data, alive=())
    assert leases(data).snapshot() == [] and len(successors(data, 'demo-N1-1')) == 1
    release = [e for e in leases(data).events() if e['kind'] == 'lock_release']
    assert [e['detail']['reason'] for e in release] == ['dead']


@pytest.mark.parametrize('pid, alive, proof', [(4242, True, False),   # a slow, live CLI with an empty log at 6 min
                                               (None, False, False),  # no pid known: unknown, never proof
                                               (4242, False, True)])  # the recorded pid is gone
def test_an_empty_log_is_proof_only_when_the_process_has_exited(data, pid, alive, proof):
    launch(data, started=NOW - 6 * 60, pid=pid, log_text='')
    rec = records(data)['demo-N1-1']
    ev = inspector.Inspector(data, store=store(data), now=NOW, alive=lambda p: alive,
                             commits=lambda r: 0).evidence(rec)
    assert inspector.proof_dead(ev) is proof


def test_the_dispatcher_emits_a_lock_wait_event_for_a_held_previous_lease(data, monkeypatch):
    from companion.feature_team import autopilot, dispatch
    s = launch(data, started=NOW - 20 * 60, pid=4242)
    assert leases(data, now=NOW - 20 * 60).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 10 * 60
    s.transition('demo-N1-1', 'timeout', detail='result deadline expired')
    monkeypatch.setattr(dispatch, 'launch_node', lambda run_id, *, root: None)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: [])
    report, _ = dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [], clock=lambda: NOW,
                                   dry_run=False)
    waits = [e for e in events.EventStore(data)._read() if e['kind'] == 'lock_wait']
    assert len(waits) == 1 and waits[0]['run_id'] == 'demo-N1-1'
    detail = json.loads(waits[0]['detail'])
    assert detail['lease_deadline'] == NOW + 3000 and detail['previous_lease'] is True


def test_a_broken_inspector_state_file_is_one_visible_row_not_a_stopped_inspector(data):
    write(data / 'team/inspector-state.json', '[1, 2]')
    launch(data, started=NOW - 60, pid=4242)
    summary = inspect(data)
    assert summary['dead'] == 1 and [r for r in inbox(data) if 'not a valid state object' in r['text']]


def test_a_launch_failure_close_with_the_ledger_pid_unknown_releases_the_claim(data):
    """Lane 2's mutant M3 survived: node_worker's launch-failure verdict (given only on a gone pid) is proof by
    itself, so a run whose ledger row carries no pid still has its unexpired claim released as dead and is requeued."""
    from companion.feature_team import node_worker
    s = launch(data, started=NOW - 180, pid=None, log_text='')
    assert leases(data, now=NOW - 180).claim('demo-N1-1', 'dispatcher', ['src/companion/demo.py'], NOW + 3000)
    s.clock = lambda: NOW - 30
    s.transition('demo-N1-1', 'timeout', detail=f'{node_worker.LAUNCH_FAILURE}: the worker ended in 150 s with an '
                                                f'empty log (x.log); its result was not valid (proof: empty)')
    inspect(data, alive=())
    assert leases(data).snapshot() == [] and len(successors(data, 'demo-N1-1')) == 1
    release = [e for e in leases(data).events() if e['kind'] == 'lock_release']
    assert [e['detail']['reason'] for e in release] == ['dead']
