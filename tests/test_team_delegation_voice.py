from companion import team_delegation as delegation
from tests.test_team_report import finish_children, reports, reviewer_submits, settle
from tests.test_team_testloop import TWO, coding_children, planned


def test_step_and_report_keep_full_details(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    children = coding_children(coordinator, room)
    finish_children(providers, children)
    full = 'Built the fixture.\n' + 'Detail ' * 400
    for ident in children.values():
        providers.states[ident]['text'] = full
    settle(coordinator, room)
    steps = [m for m in coordinator.store.get_room(room)['messages']
             if m.get('kind') == 'step' and 'done:' in m['text']]
    assert steps and all(len(m['text']) <= 240 for m in steps)
    for message in steps:
        assert delegation.read_details(coordinator.store.root, room, message['id'])['details'] == full
    assert reviewer_submits(providers)
    ident = coordinator.status(room, 'parent-1')['report']['id']
    full_report = 'Ready for review.\n' + 'Evidence line.\n' * 20
    providers.states[ident].update(state='completed', text=full_report, verdict='pass')
    settle(coordinator, room)
    card, = reports(coordinator, room)
    assert len(card['text']) <= 600 and len(card['text'].splitlines()) <= 6
    assert delegation.read_details(coordinator.store.root, room, card['id'])['details'] == full_report


def test_interview_card_caps_question_but_retains_full_transcript(tmp_path):
    from tests.test_team_interview import reply, world
    store, room, fake, coordinator = world.__wrapped__(tmp_path)
    full = 'Question context.\n' * 200
    reply(fake, full)
    coordinator.advance(room, 'parent-1')
    turn = coordinator.status(room, 'parent-1')['interview']['turns'][-1]
    assert len(turn['question']) <= 1200 and len(turn['question'].splitlines()) <= 12
    assert coordinator._read(room, 'parent-1')['interview']['turns'][-1]['question'] == full.strip()


def test_long_needs_input_reason_stays_actionable_and_logged(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ident = coding_children(coordinator, room)['worker:2']
    full = 'Review needs an owner decision. ' + 'x' * 973
    assert len(full) == 1005
    providers.states[ident].update(state='needs_input', reason=full, requires_owner_decision=True)
    coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert row['state'] == 'needs_input'
    assert len(row['children']['worker:2']['reason']) <= 240
    steps = [m for m in coordinator.store.get_room(room)['messages']
             if m.get('kind') == 'step' and 'worker:2 needs the owner:' in m['text']]
    assert len(steps) == 1
    assert delegation.read_details(coordinator.store.root, room, steps[0]['id'])['details'] == full


def test_very_long_reason_is_logged_without_unbounded_delegation_row(tmp_path):
    import json
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ident = coding_children(coordinator, room)['worker:2']
    full = 'Needs input. ' * 100000
    providers.states[ident].update(state='needs_input', reason=full, requires_owner_decision=True)
    coordinator.advance(room, 'parent-1')
    row = coordinator._read(room, 'parent-1')
    assert row['state'] == 'needs_input'
    assert len(json.dumps(row)) < 50000
    steps = [m for m in coordinator.store.get_room(room)['messages']
             if m.get('kind') == 'step' and 'worker:2 needs the owner:' in m['text']]
    assert delegation.read_details(coordinator.store.root, room, steps[0]['id'])['details'] == full


def test_local_step_details_contain_only_state():
    row = dict(request_id='parent-1', targets=['local'], children={'local': dict(
        id='a'*32, state='completed', text='PRIVATE LOCAL CONTENT', reason='')}, state='completed', reason='')
    delegation._queue_steps(row)
    done = next(e for e in row['steps'].values() if 'done:' in e['message']['text'])
    assert done['details'] == 'Kyra step done'
