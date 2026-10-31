"""Synthetic omission/retry journeys; no private run content or live providers."""
import pytest

from companion.team_delegation import Coordinator
from tests.test_team_all_builders_d1 import planned, task


def fixture(tmp_path):
    work = task('codex', mode='reply')
    work['acceptance'] = ['Required attachment exists', 'Attachment was checked']
    store, room, providers, c, calls = planned(tmp_path, [work])
    row = c.advance(room, 'post')
    return store, room, providers, c, calls, row['children']['task:1']['id']


def test_missing_acceptance_stays_open_and_gets_owned_repair(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Done.', acceptance={'Attachment was checked': 'Checked metadata.'})
    row = c.advance(room, 'post')
    assert row['state'] != 'completed' and row['progress']['done'] == 0
    items = row['open_items']
    assert len(items) == 1 and items[0]['text'] == 'Required attachment exists'
    assert items[0]['owner'] == 'codex' and items[0]['state'] == 'queued'
    assert items[0]['due_at'] > items[0]['created_at']
    assert 'Required attachment exists' in providers.submits[-1][2]


def test_review_followup_is_queued_once_across_restart(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', verdict='pass', text='Pass.',
        acceptance={'Required attachment exists': 'fixture output', 'Attachment was checked': 'metadata'},
        follow_ups=['Wire the fixture integration'])
    row = c.advance(room, 'post')
    assert row['state'] != 'completed'
    c = Coordinator(c.store, c.lead, roster=providers.roster, submit=c.submit,
                    child_status=providers.child_status, child_stop=providers.child_stop)
    row = c.advance(room, 'post')
    assert [i['text'] for i in row['open_items'] if i['state'] != 'completed'] == ['Wire the fixture integration']


def test_overdue_item_nudges_its_owner_once(tmp_path, monkeypatch):
    store, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Done.')
    row = c.advance(room, 'post')
    due = max(i['due_at'] for i in row['open_items'])
    monkeypatch.setattr('companion.team_delegation.time.time', lambda: due + 1)
    for _ in range(3):
        c.advance(room, 'post')
    nudges = [m['text'] for m in store.get_room(room)['messages'] if 'overdue' in m['text'].lower()]
    assert len(nudges) == 1 and 'Codex' in nudges[0] and 'the owner' not in nudges[0]


def test_omission_then_blocked_retry_journey(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Draft ready.', unmet_acceptance=['Required attachment exists'])
    row = c.advance(room, 'post')
    repair = row['children']['repair:task:1']
    original = c._read(room, 'post')['plan'][0]
    providers.states[repair['id']].update(state='completed', planner_kind='task', plan=[original])
    row = c.advance(room, 'post')
    retried = row['children']['task:1']
    assert retried['id'] != ident
    assert providers.submits[-1][2] != original['instruction']
    assert 'Required attachment exists' in providers.submits[-1][2]
    providers.states[retried['id']].update(state='needs_input', reason='Fixture unavailable')
    row = c.advance(room, 'post')
    assert row['state'] == 'blocked' and row['progress']['done'] == 0
    sent = len(providers.submits)
    c.advance(room, 'post')
    assert len(providers.submits) == sent  # repair budget is bounded; no skipped completion


def test_real_owner_decision_does_not_retry(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='needs_input', reason='Choose fixture format A or B.', requires_owner_decision=True)
    row = c.advance(room, 'post')
    assert row['state'] == 'needs_input'
    assert 'repair:task:1' not in row['children']


def test_result_contract_validates_bounded_evidence():
    from companion.team_chat import validate_result
    value = dict(run_id='r', stage='review', attempt=0, status='completed', summary='Checked.',
                 revision='a'*64, verdict='pass', acceptance={'Output exists': 'fixture proof'}, follow_ups=['Add fixture check'])
    assert validate_result(value, run_id='r', stage='review', attempt=0)['follow_ups']
    value['acceptance'] = {'Output exists': {'execute': 'command'}}
    with pytest.raises(ValueError):
        validate_result(value, run_id='r', stage='review', attempt=0)


def test_app_metadata_survives_and_unflagged_input_is_not_owner_decision(tmp_path):
    import json

    from companion.team_connected import ConnectedStore, _app_reply
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a'*32, provider='cursor', request_id='fixture', text='Inspect.', config_hash='pin')
    store.begin(row['id'])
    packet = _app_reply(json.dumps(dict(run_id=row['id'], summary='Fixture missing', status='needs_input',
                                      unmet_acceptance=['Output exists'], extra='tolerated')), row)
    assert packet['unmet_acceptance'] == ['Output exists']
    store.finish(row['id'], state=packet['status'], text=packet['summary'], reason='', session_id=None,
                 surface='fixture', completion=packet)
    saved = store.get(row['id'])
    assert saved['state'] == 'blocked' and not saved['requires_owner_decision']
    assert saved['completion']['unmet_acceptance'] == ['Output exists']


def test_followup_heading_is_queued(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('codex', mode='reply')])
    ident = c.advance(room, 'post')['children']['task:1']['id']
    providers.states[ident].update(state='completed', text='Checked.\nFollow-ups:\n- Wire fixture')
    assert c.advance(room, 'post')['open_items'][0]['text'] == 'Wire fixture'


def test_satisfied_acceptance_can_finish(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Done.', acceptance={
        'Required attachment exists': 'scratch output', 'Attachment was checked': 'scratch check'})
    row = c.advance(room, 'post')
    assert row['progress']['done'] == 1 and not row['open_items']


def test_stop_suppresses_overdue_and_recovery(tmp_path, monkeypatch):
    store, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Done.')
    row = c.advance(room, 'post')
    due = max(i['due_at'] for i in row['open_items'])
    c.stop(room, 'post')
    for child in providers.states.values():
        if child['state'] == 'queued':
            child['state'] = 'stopped'
    sent = len(providers.submits)
    monkeypatch.setattr('companion.team_delegation.time.time', lambda: due + 1)
    c.advance(room, 'post')
    assert len(providers.submits) == sent
    assert not any('overdue' in m['text'].lower() for m in store.get_room(room)['messages'])
    assert not c.pending()


def test_successful_repair_closes_open_items(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    providers.states[ident].update(state='completed', text='Draft ready.')
    row = c.advance(room, 'post')
    original = c._read(room, 'post')['plan'][0]
    providers.states[row['children']['repair:task:1']['id']].update(state='completed', planner_kind='task', plan=[original])
    row = c.advance(room, 'post')
    providers.states[row['children']['task:1']['id']].update(state='completed', text='Verified.', acceptance={
        'Required attachment exists': 'scratch output', 'Attachment was checked': 'scratch check'})
    row = c.advance(room, 'post')
    assert row['progress']['done'] == 1
    assert all(i['state'] == 'completed' for i in row['open_items'])


def test_final_report_review_followup_returns_to_owned_work(tmp_path):
    _, room, providers, c, _, ident = fixture(tmp_path)
    # The report review is a terminal native child after the worker steps.
    with c._edit(room, 'post') as row:
        row['children']['task:1'].update(state='completed', text='Built.', acceptance={
            'Required attachment exists': 'output', 'Attachment was checked': 'checked'})
        row['children']['reviewer'] = dict(id='report-review', state='queued', reason='', request_id='review',
            text='Review finished.', verdict='pass', follow_ups=['Verify fixture link'])
        row['targets'].append('reviewer')
        row['workflow'] = 'reviewing'
        providers.states['report-review'] = {**row['children']['reviewer'], 'state': 'completed'}
    row = c.status(room, 'post')
    assert row['state'] != 'completed'
    assert any(i['text'] == 'Verify fixture link' and i['owner'] == 'codex' for i in row['open_items'])


@pytest.mark.usefixtures('legacy_lane_workspaces')
def test_native_standalone_reply_keeps_followup_owned_without_building(tmp_path):
    from companion import team_chat
    from tests.test_team_chat_acceptance import FakeTransport, bound, respond
    transport = FakeTransport()
    store, service = bound(team_chat, tmp_path, transport)
    room = store.create_room('Synthetic native journey')['id']
    ident = store.submit(room, 'Check the fixture.', 'native', execution_mode='reply_codex')['run_id']
    service.step(ident)
    respond(transport, 'reply_codex', summary='More detail needed.', follow_ups=['Explain fixture format'])
    service.step(ident)
    run = store.get_room(room)['runs'][0]
    assert run['status'] != 'completed' and run['attempt'] == 1
    assert run['open_items'][0]['owner'] == 'codex'
    assert run['open_items'][0]['due_at'] > run['created_at']
    assert 'Explain fixture format' in transport.sends[-1]['assignment_path'].read_text()
    respond(transport, 'reply_codex', summary='Fixture format explained.', acceptance={'Explain fixture format': 'Description in this reply.'})
    service.step(ident)
    run = store.get_room(room)['runs'][0]
    assert run['status'] == 'completed' and all(i['state'] == 'completed' for i in run['open_items'])
    assert all(s['stage'] == 'reply_codex' for s in transport.sends)


@pytest.mark.usefixtures('legacy_lane_workspaces')
def test_native_blocker_retry_is_bounded_and_overdue_once(tmp_path, monkeypatch):
    from companion import team_chat
    from tests.test_team_chat_acceptance import FakeTransport, bound, respond
    transport = FakeTransport()
    store, service = bound(team_chat, tmp_path, transport)
    room = store.create_room('Synthetic blocker')['id']
    ident = store.submit(room, 'Inspect fixture.', 'native', execution_mode='reply_codex')['run_id']
    service.step(ident)
    for _ in range(3):
        respond(transport, 'reply_codex', status='needs_input', summary='Fixture unavailable.')
        service.step(ident)
    run = store.get_room(room)['runs'][0]
    assert len(transport.sends) == 3 and run['status'] != 'completed'
    assert not run['requires_owner_decision']
    assert 'Fixture unavailable.' in transport.sends[-1]['assignment_path'].read_text()
    due = run['open_items'][0]['due_at']
    monkeypatch.setattr(team_chat.time, 'time', lambda: due + 1)
    service.step(ident)
    service.step(ident)
    nudges = [m for m in store.get_room(room)['messages'] if 'overdue' in m['text']]
    assert len(nudges) == 1 and 'Codex' in nudges[0]['text']
    assert ident not in store.active_runs()


def test_local_followups_never_enter_shared_queue(tmp_path):
    from tests.test_team_delegation_routing import setup
    _, room, _, providers, c = setup(tmp_path, {'local': ''})
    c.create(room, 'Inspect local fixture.', 'post', mode='local', consent=True)
    ident = c.advance(room, 'post')['children']['local']['id']
    providers.states[ident].update(state='completed', text='Follow-ups:\n- LOCAL-ONLY-FIXTURE', follow_ups=['LOCAL-ONLY-FIXTURE'])
    row = c.advance(room, 'post')
    assert not row['open_items']
    assert all('LOCAL-ONLY-FIXTURE' not in send[2] for send in providers.submits)


def test_blocked_transport_followup_requires_evidence_on_retry(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('codex', mode='reply')])
    ident = c.advance(room, 'post')['children']['task:1']['id']
    providers.states[ident].update(state='needs_input', reason='Unmet work', unmet_acceptance=['Required fixture'])
    row = c.advance(room, 'post')
    original = c._read(room, 'post')['plan'][0]
    providers.states[row['children']['repair:task:1']['id']].update(state='completed', planner_kind='task', plan=[original])
    row = c.advance(room, 'post')
    providers.states[row['children']['task:1']['id']].update(state='completed', text='Done.')
    row = c.advance(room, 'post')
    assert row['state'] == 'blocked'
    assert any(i['text'] == 'Required fixture' and i['state'] == 'queued' for i in row['open_items'])
