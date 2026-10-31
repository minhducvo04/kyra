"""The Board append boundary is validated and serialized across real processes."""
import json
import multiprocessing
import os

import pytest

from companion import board_inbox


def _writer(path, start, prefix):
    start.wait(10)
    for i in range(60):
        board_inbox.append({'t': i, 'from': prefix, 'kind': 'result',
                            'text': f'{prefix}:{i}\n' + 'λ' * 12000}, path=path)
    board_inbox.append({'t': 0, 'from': prefix, 'kind': 'result', 'summary': 'once',
                        'routing_id': 'shared'}, path=path, once=True)


def test_two_processes_write_whole_lines_and_dedupe(tmp_path):
    path = tmp_path / 'team/board-inbox.jsonl'
    ctx = multiprocessing.get_context('spawn')
    start = ctx.Event()
    children = [ctx.Process(target=_writer, args=(path, start, str(i))) for i in range(2)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(30)
        if child.is_alive():
            child.terminate()
            child.join()
        assert child.exitcode == 0
    lines = path.read_bytes().splitlines()
    assert len(lines) == 121
    rows = [json.loads(line) for line in lines]
    assert len({r['text'] for r in rows if 'text' in r}) == 120
    assert sum(r.get('routing_id') == 'shared' for r in rows) == 1


@pytest.mark.parametrize(('field', 'value'), [
    ('t', 'yesterday'), ('t', True), ('t', float('nan')), ('t', None),
    ('from', ''), ('kind', None), ('text', []), ('node', 3), ('extra', object()),
])
def test_invalid_row_names_field_and_leaves_file_unchanged(tmp_path, field, value):
    path = tmp_path / 'inbox.jsonl'
    valid = {'t': '2027-01-15T00:00:00Z', 'from': 'fixture', 'kind': 'result', 'text': 'ok'}
    board_inbox.append(valid, path=path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=field):
        board_inbox.append(dict(valid, **{field: value}), path=path)
    assert path.read_bytes() == before


def test_write_failure_rolls_back_partial_line(tmp_path, monkeypatch):
    path = tmp_path / 'inbox.jsonl'
    row = {'t': 0, 'from': 'fixture', 'kind': 'result', 'text': 'ok'}
    board_inbox.append(row, path=path)
    before = path.read_bytes()
    real_write = os.write

    def broken(fd, payload):
        real_write(fd, payload[:5])
        raise OSError('disk failure')

    monkeypatch.setattr(board_inbox.os, 'write', broken)
    with pytest.raises(OSError, match='disk failure'):
        board_inbox.append(row, path=path)
    assert path.read_bytes() == before


# Metadata from every audited producer, after adding the required envelope.
# Unknown JSON metadata is retained, so receipt extensions stay readable.
PRODUCER_ROWS = [
    ('request_rows.request', 'request', dict(node='N1', reason='missing_input', needs='input', evidence='log:1', tried='read')),
    ('request_rows.report', 'report', dict(node='N1', step='tests', minutes=2, why='fixture')),
    ('lane_questions', 'question', dict(to='board')),
    ('local_lane', 'result', dict(result_path='draft.md', sha='abcdef12')),
    ('autopilot', 'feature_started', dict(event='feature_started', feature='demo', plan='plan.md')),
    ('watchdog', 'request', dict(node='demo:N1', reason='unknown', needs='check', evidence='log', tried='wait')),
    ('dispatch.escalate', 'escalated', dict(feature='demo', node='N1', run_id='run-1', routing_id='escalate-1')),
    ('dispatch.hygiene', 'routed', dict(lane='fixture', routing_id='hygiene-1')),
    ('dispatch.receipt', 'dispatched', dict(status='started', ledger_id='send-1', pid=123, channel='local')),
    ('dispatch.budget', 'model_budget', {}),
    ('fixback', 'routed', dict(feature=None, node=None, routing_id='fixback-1')),
    ('ladder', 'routed', dict(feature='demo', node='N1')),
    ('budget_mode', 'budget_mode', {}),
    ('context_hygiene.alert', 'hygiene', dict(lane='fixture', tokens=160000, action='compact')),
    ('context_hygiene.recovered', 'hygiene_recovered', dict(lane='fixture', tokens=100)),
    ('feature_team.fixback_row', 'verify', dict(verdict='failed', feature='demo', node='N1', run_id='run-1',
                                            evidence='log', error='failed', rule='retry', routing_id='verify-1')),
    ('feature_team.orphan', 'orphan', dict(row_hash='fixture-hash')),
    ('memory_hygiene', 'hygiene', dict(source='memory_hygiene', at='2027-01-15T00:00:00Z',
                                    passes=True, changed=False, entries=2, duplicates=0, stale=0,
                                    before={'lines': 2}, after={'lines': 2}, protected_entries=1)),
    ('lane_send', 'notice', dict(to='board')),
    ('lane_watch.nudge', 'nudge', dict(to='board')),
    ('lane_watch.post', 'result', dict(lane='fixture', shas=['abcdef12'], turn_id='turn-1', routing_id='turn-1')),
    ('loop_fl', 'fl', dict(verdict='pass', lines={'board_idle': 'pass'}, path='report.json')),
    ('loop_metrics', 'metric', dict(metric='stuck', value=None, target=0, status='pending')),
    ('nightly_suite', 'nightly_suite', dict(created_at='2027-01-15T00:00:00Z', team_delivery='sent',
                                         status='failed', sha='abcdef12', merge_sha='1234abcd',
                                         author_lane='fixture', branch='fix', failing_tests=['test_fixture'])),
    ('or_bench', 'result', dict(node='OR-BENCH', model='fixture', cost_usd=0.01, path='report.md')),
]


@pytest.mark.parametrize(('sender', 'kind', 'metadata'), PRODUCER_ROWS, ids=[r[0] for r in PRODUCER_ROWS])
def test_each_producer_shape_preserves_optional_metadata(tmp_path, sender, kind, metadata):
    row = dict(t='2027-01-15T00:00:00Z', kind=kind, summary='fixture', **{'from': sender}, **metadata)
    path = tmp_path / 'inbox.jsonl'
    assert board_inbox.append(row, path=path) == row
    assert json.loads(path.read_text()) == row


def test_existing_reader_parses_four_historical_shapes(tmp_path):
    from companion.feature_team.fixback import rows

    historical = [
        {'t': 1, 'from': 'fixture', 'kind': 'request', 'node': 'N1', 'reason': 'missing_input',
         'needs': 'input', 'evidence': 'log', 'tried': 'read'},
        {'t': 2, 'from': 'fixture', 'kind': 'report', 'node': 'N1', 'step': 'tests', 'minutes': 2, 'why': 'fixture'},
        {'t': 3, 'from': 'fixture', 'to': 'board', 'text': 'queued'},
        {'t': '2027-01-15T00:00:00Z', 'from': 'fixture', 'kind': 'result', 'node': 'N1', 'path': 'report.md'},
    ]
    path = tmp_path / 'board-inbox.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in historical))
    assert rows(path) == historical
    new = {'t': 4, 'from': 'fixture', 'kind': 'result', 'text': 'new'}
    board_inbox.append(new, path=path)
    assert rows(path) == historical + [new]


def test_incomplete_existing_tail_is_refused_without_changing_it(tmp_path):
    path = tmp_path / 'inbox.jsonl'
    path.write_bytes(b'{"t":')
    with pytest.raises(ValueError, match='incomplete line'):
        board_inbox.append({'t': 0, 'from': 'fixture', 'kind': 'result', 'text': 'ok'}, path=path)
    assert path.read_bytes() == b'{"t":'
