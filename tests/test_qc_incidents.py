"""QC incident ownership: one suggestion per episode, shared store, Board deadlines."""
import json
import multiprocessing
import traceback
from pathlib import Path

import pytest

from companion import board_inbox
from companion.feature_team import qc_incidents
from companion.feature_team.qc_incidents import QCIncidentError

OPENED = 1_700_000_000


def _hit(**overrides):
    hit = {
        'pattern': 'START_UNCONFIRMED',
        'subject': {
            'task': 'train-9',
            'group': 'alpha',
            'line': 12,
            'model_summary': 'grok run',
            'retry': 4,
        },
        'evidence_refs': [{'path': 'team/landing/queue.log', 'line': 3, 'field': 'reason'}],
        'suggested_fix': 'Reconcile the original receipt, worker and hold.',
        'owner': 'dispatch-owner',
        'action_kind': 'waiting_board',
        'preconditions': ['unconfirmed for two ticks'],
        'episode': 'retry-4',
    }
    hit.update(overrides)
    return hit


def _config(root):
    return {'data_root': root}


def _rows(root):
    path = Path(root) / 'team' / 'board-inbox.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def _suggestions(root):
    return [row for row in _rows(root) if row.get('kind') == 'qc_suggestion']


def _scope_worker(root, scope, started, sink):
    started.wait(10)
    try:
        incident_id = qc_incidents.publish(_hit(), config=_config(root), now=OPENED, scope=scope)
    except Exception:
        Path(sink).write_text(traceback.format_exc(), encoding='utf-8')
        raise
    Path(sink).write_text(incident_id, encoding='utf-8')


def test_publish_writes_one_suggestion_through_the_board_inbox(tmp_path):
    incident_id = qc_incidents.publish(_hit(), config=_config(tmp_path), now=OPENED)
    rows = _suggestions(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row['routing_id'] == incident_id
    assert row['from'] == 'qc-desk' and row['kind'] == 'qc_suggestion' and row['to'] == 'board'
    assert row['t'] == OPENED
    assert row['pattern'] == 'START_UNCONFIRMED'
    assert row['owner'] == 'dispatch-owner'
    assert row['suggested_fix'] == 'Reconcile the original receipt, worker and hold.'
    assert isinstance(row['evidence'], str) and 'team/landing/queue.log:3 reason' in row['evidence']
    assert row['evidence_refs'] == [{'path': 'team/landing/queue.log', 'line': 3, 'field': 'reason'}]
    assert 'Owner: dispatch-owner' in row['text']
    saved = qc_incidents.read_incident(incident_id, config=_config(tmp_path))
    assert saved['episode'] == 1 and saved['state'] == 'open' and saved['inbox_acked'] is True


def test_repeated_ticks_and_unstable_fields_stay_one_incident(tmp_path):
    config = _config(tmp_path)
    first = qc_incidents.publish(_hit(), config=config, now=OPENED, scope='feature:alpha')
    second = qc_incidents.publish(
        _hit(subject={'task': 'train-9', 'group': 'beta', 'retry_number': 9, 'tick_time': OPENED + 30,
                      'model_summary': 'other model', 'line': 99}),
        config=config, now=OPENED + 30, scope='group:west')
    third = qc_incidents.publish(
        _hit(evidence_refs=[{'path': 'team/landing/queue.log', 'line': 3},
                            {'path': 'team/landing/queue.log', 'line': 4}]),
        config=config, now=OPENED + 60)
    assert first == second == third
    assert len(_suggestions(tmp_path)) == 1
    saved = qc_incidents.read_incident(first, config=config)
    assert saved['count'] == 3
    assert saved['scopes'] == ['feature:alpha', 'group:west']
    assert {ref['line'] for ref in saved['evidence_refs']} == {3, 4}
    view = qc_incidents.status_view(config=config, now=OPENED + 60)
    assert view['open'] == [first]
    assert view['overdue'] == [] and view['escalated'] == []


def test_two_processes_overlapping_scopes_publish_one_suggestion(tmp_path):
    """Real scratch inbox, two processes, two scopes, one suggestion."""
    ctx = multiprocessing.get_context('spawn')
    started = ctx.Event()
    sinks = [tmp_path / 'scope-a.txt', tmp_path / 'scope-b.txt']
    children = [
        ctx.Process(target=_scope_worker, args=(str(tmp_path), 'feature:alpha', started, str(sinks[0]))),
        ctx.Process(target=_scope_worker, args=(str(tmp_path), 'group:beta', started, str(sinks[1]))),
    ]
    for child in children:
        child.start()
    started.set()
    for child, sink in zip(children, sinks, strict=True):
        child.join(30)
        if child.is_alive():
            child.terminate()
            child.join()
        detail = sink.read_text(encoding='utf-8') if sink.exists() else ''
        assert child.exitcode == 0, detail
    ids = {sink.read_text(encoding='utf-8') for sink in sinks}
    assert len(ids) == 1
    incident_id = ids.pop()
    assert len(_suggestions(tmp_path)) == 1
    assert _suggestions(tmp_path)[0]['routing_id'] == incident_id
    saved = qc_incidents.read_incident(incident_id, config=_config(tmp_path))
    assert set(saved['scopes']) == {'feature:alpha', 'group:beta'}
    assert saved['count'] == 2


def test_crash_after_append_replays_the_same_routing_id(tmp_path, monkeypatch):
    config = _config(tmp_path)
    real = qc_incidents.board_inbox.append

    def crash_after(row, *, path=None, once=False):
        real(row, path=path, once=once)
        raise RuntimeError('crash before local acknowledgment')

    monkeypatch.setattr(qc_incidents.board_inbox, 'append', crash_after)
    with pytest.raises(RuntimeError, match='crash before local acknowledgment'):
        qc_incidents.publish(_hit(), config=config, now=OPENED)
    stored = json.loads((tmp_path / 'team' / 'qc' / 'incidents.json').read_text(encoding='utf-8'))
    pending = list(stored['incidents'].values())
    assert len(pending) == 1 and pending[0]['inbox_acked'] is False
    with pytest.raises(QCIncidentError, match='durable processing'):
        qc_incidents.advance_cursor({'file_id': 'queue.log', 'offset': 10}, config=config, now=OPENED + 1)
    monkeypatch.setattr(qc_incidents.board_inbox, 'append', real)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED + 2)
    again = qc_incidents.publish(_hit(), config=config, now=OPENED + 3)
    assert again == incident_id == pending[0]['incident_id']
    assert len(_suggestions(tmp_path)) == 1
    assert _suggestions(tmp_path)[0]['routing_id'] == incident_id
    cursor = qc_incidents.advance_cursor({'file_id': 'queue.log', 'offset': 10}, config=config, now=OPENED + 4)
    assert cursor['cursor'] == {'file_id': 'queue.log', 'offset': 10}


def test_closure_keeps_the_episode_until_new_evidence(tmp_path):
    config = _config(tmp_path)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED)
    closed = qc_incidents.record_decision(incident_id, 'dispatch-owner', 'close', config=config, now=OPENED + 5)
    assert closed['incident_id'] == incident_id and closed['owner'] == 'dispatch-owner'
    replay = qc_incidents.publish(_hit(), config=config, now=OPENED + 6)
    assert replay == incident_id
    assert len(_suggestions(tmp_path)) == 1
    opened = qc_incidents.publish(
        _hit(evidence_refs=[{'path': 'team/landing/queue.log', 'line': 8}]),
        config=config, now=OPENED + 7)
    assert opened != incident_id
    assert qc_incidents.read_incident(opened, config=config)['episode'] == 2
    assert qc_incidents.read_incident(incident_id, config=config)['closure'] == 'board'
    assert [row['routing_id'] for row in _suggestions(tmp_path)] == [incident_id, opened]
    decisions = (tmp_path / 'team' / 'qc' / 'decisions.jsonl').read_text(encoding='utf-8')
    assert incident_id in decisions and 'dispatch-owner' in decisions


def test_verified_recovery_then_new_evidence_opens_the_next_episode(tmp_path):
    config = _config(tmp_path)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED)
    qc_incidents.record_recovery(
        incident_id,
        {'evidence_refs': [{'path': 'team/loop/runs/train-9.json', 'line': 2}]},
        config=config, now=OPENED + 5)
    assert qc_incidents.publish(_hit(), config=config, now=OPENED + 6) == incident_id
    opened = qc_incidents.publish(
        _hit(evidence_refs=[{'path': 'team/landing/queue.log', 'line': 9}]),
        config=config, now=OPENED + 7)
    assert opened != incident_id
    assert qc_incidents.read_incident(incident_id, config=config)['closure'] == 'recovered'
    assert len(_suggestions(tmp_path)) == 2


def test_inbox_row_is_not_acknowledgment_and_owner_must_match(tmp_path):
    config = _config(tmp_path)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED)
    board_inbox.append({
        't': OPENED + 1, 'from': 'board', 'kind': 'note', 'to': 'qc-desk',
        'text': 'saw the suggestion', 'routing_id': 'board-saw-it',
    }, path=tmp_path / 'team' / 'board-inbox.jsonl', once=True)
    with pytest.raises(QCIncidentError, match='owner'):
        qc_incidents.record_decision(incident_id, 'someone-else', 'acknowledge', config=config, now=OPENED + 2)
    assert not (tmp_path / 'team' / 'qc' / 'decisions.jsonl').exists()
    view = qc_incidents.status_view(config=config, now=OPENED + 20 * 60)
    assert incident_id in view['overdue']
    assert qc_incidents.read_incident(incident_id, config=config)['acknowledged_at'] is None
    disk = json.loads((tmp_path / 'team' / 'qc' / 'status.json').read_text(encoding='utf-8'))
    assert incident_id in disk['overdue']


def test_deadlines_mark_overdue_then_escalate_without_a_second_suggestion(tmp_path):
    config = _config(tmp_path)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED)
    early = qc_incidents.status_view(config=config, now=OPENED + 20 * 60 - 1)
    assert early['overdue'] == [] and early['escalated'] == []
    overdue = qc_incidents.status_view(config=config, now=OPENED + 20 * 60)
    assert overdue['overdue'] == [incident_id] and overdue['escalated'] == []
    assert not any(row.get('kind') == 'escalated' for row in _rows(tmp_path))
    escalated = qc_incidents.status_view(config=config, now=OPENED + 60 * 60)
    assert escalated['escalated'] == [incident_id] and incident_id in escalated['overdue']
    qc_incidents.status_view(config=config, now=OPENED + 60 * 60 + 30)
    kinds = [row['kind'] for row in _rows(tmp_path)]
    assert kinds.count('qc_suggestion') == 1
    assert kinds.count('escalated') == 1
    escalation = next(row for row in _rows(tmp_path) if row['kind'] == 'escalated')
    assert escalation['routing_id'] == f'qc-escalated:{incident_id}'
    assert escalation['from'] == 'qc-desk' and incident_id in escalation['text']
    assert escalation['routing_id'] != incident_id


def test_acknowledgment_stops_overdue_and_escalation(tmp_path):
    config = _config(tmp_path)
    incident_id = qc_incidents.publish(_hit(), config=config, now=OPENED)
    qc_incidents.record_decision(incident_id, 'dispatch-owner', 'acknowledge', config=config, now=OPENED + 30)
    view = qc_incidents.status_view(config=config, now=OPENED + 60 * 60)
    assert view['overdue'] == [] and view['escalated'] == []
    assert view['incidents'][0]['acknowledged'] is True
    assert not any(row.get('kind') == 'escalated' for row in _rows(tmp_path))
    decision = json.loads((tmp_path / 'team' / 'qc' / 'decisions.jsonl').read_text(encoding='utf-8'))
    assert decision['incident_id'] == incident_id and decision['owner'] == 'dispatch-owner'
    assert decision['decision'] == 'acknowledge'


def test_unreadable_store_is_not_treated_as_empty(tmp_path):
    path = tmp_path / 'team' / 'qc' / 'incidents.json'
    path.parent.mkdir(parents=True)
    path.write_text('{', encoding='utf-8')
    with pytest.raises(QCIncidentError, match='unreadable'):
        qc_incidents.publish(_hit(), config=_config(tmp_path), now=OPENED)
    assert path.read_text(encoding='utf-8') == '{'
    assert _suggestions(tmp_path) == []
