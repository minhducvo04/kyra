"""loop_tick and the completion table: a timeout or queued record whose node is already done is skipped.

2026-10-16 smoke window: R14's completion row said done (reconcile) while its run record said timeout on a paused
channel, so every tick refused it again, forever. A node that is not done still behaves as before."""

import pytest

from companion.feature_team import autopilot, dispatch
from tests.test_inspector import BRIEF, NOW, TEAM, launch, store, write


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    write(root / 'team/features/demo/team.json', TEAM)
    monkeypatch.setattr(autopilot, 'capacity_refusals', lambda data, channel=None: ['channel paused'])
    return root


def tick(data):
    report, _ = dispatch.loop_tick(data, [dict(channel='muse:0', role='engineer')], [],
                                   clock=lambda: NOW + 60, dry_run=True)
    return report


@pytest.mark.parametrize('state', ['timeout', 'queued'])
def test_a_record_whose_node_is_done_is_skipped_without_a_refusal(data, state, capsys):
    if state == 'timeout':
        launch(data, state='dispatched').transition('demo-N1-1', 'timeout', detail='deadline')
    else:
        store(data).create(dict(BRIEF, run_id='demo-N1-1', node='N1', inputs=[], channel='muse:0',
                                result=str(data / 'team/loop/results/demo-N1-1.json'), deadline=NOW + 5400))
    write(data / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'done', 'by': 'reconcile'}}})
    assert tick(data)['refusals'] == []
    assert 'demo-N1-1' in capsys.readouterr().err


def test_a_timeout_record_whose_node_is_not_done_is_still_refused(data):
    s = launch(data, state='dispatched')
    s.transition('demo-N1-1', 'timeout', detail='deadline')
    write(data / 'team/features/demo/completion.json', {'nodes': {'N1': {'status': 'dispatched'}}})
    (row,) = tick(data)['refusals']
    assert row['run_id'] == 'demo-N1-1' and 'channel paused' in row['reasons']
