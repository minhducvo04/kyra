"""Gate nodes go to the owner, never to a worker; channel holds key by channel, never by author name alone.

2026-10-17: memory-scale PG-YES ("Board asks the owner") became ready, its hint named no model family, so either dispatch
path could hand it to any seat, and nothing ever asked the owner. Six Muse slots share one model, so a hold looked up by
author name could mark the wrong slot busy.
"""

import json

from companion.feature_team import dispatch
from companion.feature_team.checkpoints import Checkpoints
from companion.feature_team.dag import Dag

NODE = dict(inputs=[], intern_friendly=False, tier='T1', acceptance='pytest tests/test_demo.py', kind='build')
GATE = dict(NODE, id='G', kind='gate', title='the owner says yes to a local Postgres', files=[],
            acceptance='the owner answers yes or no in the home chat.', owner_hint='Board asks the owner')
TABLE = [dict(channel='muse:0', role='engineer'), dict(channel='muse:1', role='engineer')]


def _feature(nodes, state=None, team=None):
    return dict(name='demo', dag=Dag(nodes), state=state or {n['id']: {'status': 'queued'} for n in nodes},
                team=team or [dict(name='Muse Wonder Full', role='Engineer', channel='muse:0'),
                              dict(name='Muse Wonder Full 2', role='Engineer', channel='muse:1')],
                queues=dict(engineer=[dict(node=n['id'], priority=i, t=0) for i, n in enumerate(nodes)], intern=[]))


def _data(tmp_path, nodes):
    folder = tmp_path / 'team/feature-teams'
    folder.mkdir(parents=True)
    (folder / 'demo.dag.json').write_text(json.dumps({'nodes': nodes}))
    return tmp_path


def test_select_never_hands_a_ready_gate_node_to_a_worker():
    got = dispatch.select(TABLE, [], [_feature([GATE, dict(NODE, id='N1')])], {})
    assert [i['node'] for i in got] == ['N1']
    assert dispatch.select(TABLE, [], [_feature([GATE])], {}) == []


def test_ready_gate_raises_one_question_and_one_board_line(tmp_path):
    data = _data(tmp_path, [GATE, dict(NODE, id='N1', inputs=['G'])])
    feature = _feature([GATE, dict(NODE, id='N1', inputs=['G'])], state={})
    assert dispatch.raise_gates(data, [feature], clock=lambda: 1_800_000_000) == [('demo', 'G')]
    assert dispatch.raise_gates(data, [feature], clock=lambda: 1_800_000_100) == []  # the next tick asks nothing
    (question,) = Checkpoints(data / 'team/loop', clock=lambda: 1_800_000_200).open_questions()
    assert (question['node'], question['what'], question['default']) == ('G', GATE['title'], 'wait')
    assert question['why'] == GATE['acceptance'] and not question['reversible']
    lines = (data / 'team/board-inbox.jsonl').read_text().splitlines()
    assert [json.loads(line)['kind'] for line in lines] == ['gate_question']
    assert len(Checkpoints(data / 'team/loop', clock=lambda: 1_800_000_200).card_lines()) == 1


def test_gate_waiting_on_inputs_or_done_is_not_raised(tmp_path):
    data = _data(tmp_path, [dict(NODE, id='N0'), dict(GATE, inputs=['N0'])])
    waiting = _feature([dict(NODE, id='N0'), dict(GATE, inputs=['N0'])], state={})
    assert dispatch.raise_gates(data, [waiting], clock=lambda: 1_800_000_000) == []
    closed = _feature([GATE], state={'G': {'status': 'done'}})
    assert dispatch.raise_gates(data, [closed], clock=lambda: 1_800_000_000) == []


def test_hold_keys_by_the_row_channel_not_a_shared_author_name():
    """Two roster rows share one name; the dispatched row on muse:1 holds muse:1, so muse:0 takes the next node."""
    team = [dict(name='Muse Wonder Full', role='Engineer', channel='muse:1'),
            dict(name='Muse Wonder Full', role='Engineer', channel='muse:0')]
    nodes = [dict(NODE, id='N1'), dict(NODE, id='N2')]
    state = {'N1': {'status': 'dispatched', 'by': 'Muse Wonder Full', 'channel': 'muse:1'}, 'N2': {'status': 'queued'}}
    got = dispatch.select(TABLE, [], [_feature(nodes, state=state, team=team)], {})
    assert [(i['node'], i['channel']) for i in got] == [('N2', 'muse:0')]
