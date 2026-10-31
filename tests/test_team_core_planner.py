"""P1 planner contracts and bounded shared context, using scratch stores."""
import json
from pathlib import Path

import pytest

from companion import team_chat as team
from tests.test_team_chat_acceptance import FakeTransport, bound, respond


def envelope(**fields):
    return dict(run_id='a'*32, stage='plan', attempt=0, status='completed', summary='Decision', **fields)


def test_three_planner_kinds_and_ordered_five_targets():
    for kind, tasks in [('answer', []), ('task', [dict(provider='claude', title='Reply', instruction='Say hello', order=1, mode='reply')]),
                        ('plan', [dict(provider=p, title='Count', instruction=str(i), order=i, mode='reply')
                                  for i, p in enumerate(('kyra', 'claude', 'codex', 'cursor', 'antigravity'), 1)])]:
        value = envelope(kind=kind, tasks=tasks)
        assert team.validate_result(value, run_id='a'*32, stage='plan', attempt=0, execution_mode='plan_only') == value
    for tasks in [[dict(provider='claude', title='x', instruction='x', order=True, mode='reply')],
                  [dict(provider='claude', title='x', instruction='x', order=1, mode='invented')]]:
        with pytest.raises(ValueError):
            team.validate_result(envelope(kind='task', tasks=tasks), run_id='a'*32, stage='plan', attempt=0, execution_mode='plan_only')


def test_planner_answer_finishes_without_a_build_or_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(team, 'team_handbook', lambda: 'Fixture handbook', raising=False)
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run = store.submit(room, 'What is 2+2?', 'question', execution_mode='plan_only')['run_id']
    service.step(run)
    respond(transport, 'plan', summary='4', kind='answer', tasks=[])
    service.step(run)
    row = store.get_room(room)['runs'][0]
    assert row['status'] == 'completed' and row['planner_kind'] == 'answer'
    assert row['tasks'] == [] and row['revision'] is None
    assert transport.stages() == [('claude', 'plan')]


def test_assignment_includes_shared_history_real_authors_and_handbook(tmp_path, monkeypatch):
    monkeypatch.setattr(team, 'team_handbook', lambda: 'Fixture handbook: answer in the thread.', raising=False)
    store, service = bound(team, tmp_path, FakeTransport())
    room = store.create_room('Fixture')['id']
    with store.edit() as data:
        value = data['rooms'][room]
        team._message(value, 'user', 'PRIVATE', shared=False)
        for i in range(15):
            team._message(value, 'user' if i % 2 else 'claude', f'shared-{i}', shared=True)
    store.submit(room, 'Next question', 'next', execution_mode='plan_only')
    value = store.get_room(room)
    text = service._assignment(value, value['runs'][0], Path('/tmp/result.json'))
    history = json.loads(text.split('Room instruction/context (data):\n')[1].split('\nTask instructions')[0])
    assert len(history) == 12 and history[-1] == {'author': 'user', 'text': 'Next question'}
    assert {'author': 'claude', 'text': 'shared-14'} in history
    assert 'PRIVATE' not in text and 'shared-0"' not in text
    assert 'Fixture handbook: answer in the thread.' in text


def test_text_reply_never_reads_workspace_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(team, 'team_handbook', lambda: 'Fixture handbook', raising=False)
    transport = FakeTransport()
    def forbidden():
        pytest.fail('Text replies must not fingerprint source')
    transport.revision = forbidden
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run = store.submit(room, 'Hello', 'hi', execution_mode='reply_codex')['run_id']
    service.step(run)
    respond(transport, 'reply_codex', summary='Hello')
    service.step(run)
    assert store.get_room(room)['runs'][0]['status'] == 'completed'


def test_system_stage_context_references_shared_input_without_impersonating_user(tmp_path):
    store, _ = bound(team, tmp_path, FakeTransport())
    room = store.create_room('Fixture')['id']
    with store.edit() as data:
        team._message(data['rooms'][room], 'user', 'Original instruction', shared=True)
        input_id = data['rooms'][room]['messages'][-1]['id']
    result = store.submit(room, 'Planner stage instruction', 'stage1', execution_mode='plan_only',
                          author='system', input_message_id=input_id, parent_request_id='parent')
    assert store.submit(room, 'Planner stage instruction', 'stage1', execution_mode='plan_only',
                        author='system', input_message_id=input_id, parent_request_id='parent') == result
    value = store.get_room(room)
    assert [m['author'] for m in value['messages']] == ['user', 'system']
    assert value['runs'][0]['input_message_id'] == input_id
    with pytest.raises(ValueError):
        store.submit(room, 'Planner stage instruction', 'stage1', execution_mode='plan_only',
                     author='user', input_message_id=input_id, parent_request_id='parent')


def test_private_input_cannot_be_selected_for_shared_assignment(tmp_path):
    store, _ = bound(team, tmp_path, FakeTransport())
    room = store.create_room('Fixture')['id']
    with store.edit() as data:
        team._message(data['rooms'][room], 'user', 'Private', shared=False)
        input_id = data['rooms'][room]['messages'][-1]['id']
    with pytest.raises(ValueError):
        store.submit(room, 'Stage', 'stage', execution_mode='plan_only', author='system', input_message_id=input_id)


def test_ordered_plan_persists_equal_groups_and_all_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(team, 'team_handbook', lambda: 'Fixture handbook')
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run_id = store.submit(room, 'Ask the team in order', 'ordered', execution_mode='plan_only')['run_id']
    service.step(run_id)
    tasks = [dict(provider=p, title=p, instruction=f'Say {i}', order=(i+1)//2)
             for i, p in enumerate(('kyra', 'claude', 'codex', 'cursor', 'antigravity'), 1)]
    respond(transport, 'plan', summary='Ordered work', kind='plan', tasks=tasks)
    service.step(run_id)
    saved = team.TeamStore(store.root, controller=service.controller).get_room(room)['runs'][0]
    assert saved['status'] == 'completed'
    assert [t['order'] for t in saved['tasks']] == [1, 1, 2, 2, 3]
    assert [t['assignee'] for t in saved['tasks']] == ['kyra', 'claude', 'codex', 'cursor', 'antigravity']
    assert saved['acknowledged_at'] >= saved['created_at']
    assert transport.stages() == [('claude', 'plan')]


def test_task_workspace_is_returned_for_separate_dispatch_authorization(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    bound_path = tmp_path / 'bound'
    monkeypatch.setitem(sys.modules, 'companion.team_workspace', SimpleNamespace(
        task_worktree=lambda run_id, **kwargs: tmp_path / 'different'))
    assert team._task_workspace('a'*32, {'workspace': str(bound_path)}) == tmp_path / 'different'


def test_missing_handbook_fails_closed_and_mtime_reloads(tmp_path, monkeypatch):
    from companion import team_connected
    monkeypatch.delattr(team_connected, 'team_handbook', raising=False)
    path = tmp_path / 'handbook.md'
    monkeypatch.setattr(team, '_HANDBOOK_PATH', path)
    with pytest.raises(FileNotFoundError):
        team.team_handbook()
    path.write_text('First handbook')
    assert team.team_handbook() == 'First handbook'
    path.write_text('Updated handbook')
    import os
    stamp = path.stat().st_mtime_ns + 1_000_000
    os.utime(path, ns=(stamp, stamp))
    assert team.team_handbook() == 'Updated handbook'


def test_later_assignment_sees_previous_shared_agent_result(tmp_path, monkeypatch):
    monkeypatch.setattr(team, 'team_handbook', lambda: 'Fixture handbook')
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    first = store.submit(room, 'First turn', 'first', execution_mode='reply_codex')['run_id']
    service.step(first)
    respond(transport, 'reply_codex', summary='Earlier agent finding')
    service.step(first)
    store.submit(room, 'Use the finding', 'second', execution_mode='plan_only')
    value = store.get_room(room)
    text = service._assignment(value, value['runs'][-1], tmp_path / 'result.json')
    assert '{"author": "codex", "text": "Earlier agent finding"}' in text


def test_reply_threads_do_not_enter_room_context(tmp_path):
    store, service = bound(team, tmp_path, FakeTransport())
    room_id = store.create_room('Fixture')['id']
    thread_run = store.submit(room_id, 'THREAD QUESTION', 'reply.fixture', execution_mode='reply_codex')['run_id']
    store.stop(room_id)
    with store.edit() as data:
        team._message(data['rooms'][room_id], 'codex', 'THREAD ANSWER', run_id=thread_run, kind='result', shared=True)
    store.submit(room_id, 'Room question', 'room-question', execution_mode='plan_only')
    room = store.get_room(room_id)
    text = service._assignment(room, room['runs'][-1], tmp_path / 'result.json')
    assert 'Room question' in text
    assert 'THREAD QUESTION' not in text and 'THREAD ANSWER' not in text


def test_system_local_steps_keep_their_author_and_idempotence(tmp_path):
    from tests.test_team_lead import PROJECT
    store, _ = bound(team, tmp_path, FakeTransport())
    room = store.create_room('Fixture')['id']
    args = dict(binding=PROJECT, author='system')
    first = store.queue_answer(room, 'Derived local task', 'local-step', **args)
    assert store.queue_answer(room, 'Derived local task', 'local-step', **args) == first
    assert store.get_room(room)['messages'][0]['author'] == 'system'
    for author in ('user', 'codex'):
        with pytest.raises(ValueError):
            store.queue_answer(room, 'Derived local task', 'local-step', binding=PROJECT, author=author)


@pytest.mark.parametrize('owner', [True, False])
def test_only_explicit_owner_questions_are_exposed(tmp_path, owner):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run = store.submit(room, 'Plan this', 'plan', execution_mode='plan_only')['run_id']
    service.step(run)
    respond(transport, 'plan', status='needs_input', summary='Choose the intended audience' if owner else 'Tool unavailable',
            requires_owner_decision=owner)
    service.step(run)
    saved = store.get_room(room)['runs'][0]
    assert saved['status'] == 'needs_input'
    assert saved.get('requires_owner_decision') is owner


def test_owner_flag_cannot_mark_success_or_use_nonboolean_values():
    for flag in (True, 'true', 1):
        with pytest.raises(ValueError):
            team.validate_result(envelope(kind='answer', tasks=[], requires_owner_decision=flag),
                                 run_id='a'*32, stage='plan', attempt=0, execution_mode='plan_only')


@pytest.mark.parametrize('interview', [True, False])
@pytest.mark.parametrize('kind', ['answer', 'task', 'plan'])
def test_modern_planner_decision_after_interview_or_direct(tmp_path, interview, kind):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    if interview:
        turn = store.submit(room, 'Interview request', 'interview', execution_mode='interview_only')['run_id']
        service.step(turn)
        respond(transport, 'plan', summary='One sentence answer. Start?', ready=True)
        service.step(turn)
        assert store.get_room(room)['runs'][-1]['interview_ready'] is True
    # Coordinator owns consent; this exercises its native planner boundary after consent or interview=false.
    turn = store.submit(room, 'Approved: answer the question', 'plan', execution_mode='plan_only')['run_id']
    service.step(turn)
    tasks = [] if kind == 'answer' else [dict(provider='claude', title='First', instruction='Reply first', order=1)]
    if kind == 'plan':
        tasks.append(dict(provider='codex', title='Next', instruction='Use the first reply', order=2))
    respond(transport, 'plan', run_id=turn, summary='Decision', kind=kind, tasks=tasks)
    service.step(turn)
    result = store.get_room(room)['runs'][-1]
    assert result['status'] == 'completed' and result['planner_kind'] == kind
    assert [task['order'] for task in result['tasks']] == [task['order'] for task in tasks]
    assert all(stage == 'plan' for _, stage in transport.stages())


def test_task_tree_must_be_authorized_before_native_dispatch(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace


    calls = []
    def task_worktree(run_id, **kwargs):
        calls.append(run_id)
        return tmp_path / 'unauthorized-task-tree'

    monkeypatch.setitem(sys.modules, 'companion.team_workspace', SimpleNamespace(
        task_worktree=task_worktree, role_line=lambda lane, role: f'Assigned {role}'))
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room = store.create_room('Fixture')['id']
    run_id = store.submit(room, 'Build the fixture', 'build', execution_mode='worker')['run_id']
    service.step(run_id)
    assert transport.sends == []
    assert store.get_room(room)['runs'][0]['status'] == 'needs_input'
    assert calls == [run_id]
