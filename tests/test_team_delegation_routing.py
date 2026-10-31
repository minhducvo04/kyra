"""Planner-first routing and bounded retry regressions; all stores are scratch."""
import os
from types import SimpleNamespace

import pytest

from companion import team_connected, team_http
from companion.team_delegation import Coordinator
from tests.test_team_delegation import Lead, Providers, Store


def setup(tmp_path, roster=None):
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    lead = Lead(store)
    providers = Providers(roster or {'planner': '', 'cursor': '', 'antigravity': ''})
    coordinator = Coordinator(store, lead, roster=providers.roster, submit=lambda *a, **kw: providers.submit(*a),
                              child_status=providers.child_status, child_stop=providers.child_stop)
    return store, room, lead, providers, coordinator


@pytest.mark.parametrize('text', ['Explain this feature.', 'Explain @all mentions.', 'Count in order.'])
def test_shared_auto_goes_to_planner_without_local_triage_or_second_consent(tmp_path, text):
    store, room, lead, providers, coordinator = setup(tmp_path)
    coordinator.create(room, text, 'post', consent=True)
    coordinator.advance(room, 'post')
    assert not lead.calls
    assert [(p, t) for _, p, t, _ in providers.submits] == [('planner', text)]
    child = coordinator.status(room, 'post')['children']['planner']
    providers.states[child['id']].update(state='completed', text='Planner answered.', planner_kind='answer', plan=[])
    assert coordinator.advance(room, 'post')['state'] == 'completed'
    coordinator.create(room, text, 'post', consent=True)
    coordinator.advance(room, 'post')
    assert len(providers.submits) == 1


@pytest.mark.parametrize('state', ['blocked', 'needs_input', 'failed'])
def test_child_retries_once_then_stays_blocked_without_asking_owner(tmp_path, state):
    store, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Inspect the fixture.', 'post', mode='cursor', consent=True)
    first = coordinator.advance(room, 'post')['children']['cursor']
    providers.states[first['id']].update(state=state, reason='temporary_problem')
    coordinator.advance(room, 'post')
    assert len(providers.submits) == 2
    second = coordinator.status(room, 'post')['children']['cursor']
    assert second['request_id'] != first['request_id']
    providers.states[second['id']].update(state=state, reason='still_unavailable')
    result = coordinator.advance(room, 'post')
    assert result['state'] == 'blocked'
    assert result['children']['cursor']['skipped'] is True
    assert 'cursor' in result['reason']
    for _ in range(3):
        coordinator.advance(room, 'post')
    assert len(providers.submits) == 2
    messages = store.get_room(room)['messages']
    assert any('cursor' in m['text'].lower() and 'skipped' in m['text'].lower() for m in messages)
    assert not any('needs the owner' in m['text'] for m in messages)


def test_uncertain_send_is_not_retried(tmp_path):
    _, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Inspect.', 'post', mode='cursor', consent=True)
    child = coordinator.advance(room, 'post')['children']['cursor']
    providers.states[child['id']].update(state='uncertain', reason='process_unconfirmed')
    for _ in range(3):
        result = coordinator.advance(room, 'post')
    assert result['state'] == 'uncertain' and len(providers.submits) == 1


def test_real_owner_decision_is_not_retried_or_skipped(tmp_path):
    _, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Inspect.', 'post', mode='cursor', consent=True)
    child = coordinator.advance(room, 'post')['children']['cursor']
    providers.states[child['id']].update(state='needs_input', reason='Choose the product direction.',
                                        requires_owner_decision=True)
    result = coordinator.advance(room, 'post')
    assert result['state'] == 'needs_input' and len(providers.submits) == 1


def test_handbook_cache_reloads_on_mtime_and_refuses_missing(tmp_path, monkeypatch):
    path = tmp_path / 'docs' / 'team-handbook.md'
    path.parent.mkdir()
    path.write_text('Fixture handbook one')
    monkeypatch.setattr(team_connected, '__file__', str(tmp_path / 'src/companion/team_connected.py'))
    assert team_connected.team_handbook() == 'Fixture handbook one'
    before = path.stat().st_mtime_ns
    path.write_text('Fixture handbook two')
    os.utime(path, ns=(before + 1000000, before + 1000000))
    assert team_connected.team_handbook() == 'Fixture handbook two'
    path.unlink()
    with pytest.raises(FileNotFoundError):
        team_connected.team_handbook()


@pytest.mark.parametrize('provider', ['cursor', 'antigravity'])
def test_connected_prompt_has_role_handbook_and_correlated_output(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(team_connected, 'team_handbook', lambda: 'Synthetic team handbook')
    row = dict(id='a'*32, provider=provider, prompt='Explain the fixture.')
    prompt = team_connected._app_prompt(row)
    assert 'Synthetic team handbook' in prompt
    assert 'You are not the planner' in prompt
    assert row['prompt'] in prompt
    for field in ('run_id', 'stage', 'attempt', 'status', 'summary'):
        assert field in prompt
    assert row['id'] in prompt


def test_connected_result_records_acknowledgement_but_not_on_submission(tmp_path):
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a'*32, provider='cursor', request_id='r', text='Hello', config_hash='pin')
    assert row.get('acknowledged_at') is None
    store.begin(row['id'])
    store.finish(row['id'], state='completed', text='Hello.', reason='', session_id='session', surface='fixture')
    result = store.get(row['id'])
    assert result['acknowledged_at'] > 0
    assert result['state'] == 'completed'


def test_http_wires_planner_to_full_planner_run(tmp_path, monkeypatch):
    submitted = []
    store = SimpleNamespace(root=tmp_path, get_room=lambda _: {'room': {'local_only': False}},
                            submit=lambda *a, **kw: submitted.append((a, kw)) or {'run_id': 'a'*32})
    service = SimpleNamespace(transport=SimpleNamespace(status=lambda: {'ready': True}))
    lead = SimpleNamespace(status=lambda: {'available': False})
    monkeypatch.setattr(team_http, 'wake_service', lambda _: None)
    coordinator = team_http.get_delegation(store, lead, service)
    coordinator.submit('room', 'planner', 'Explain.', 'request')
    assert submitted[0][1]['execution_mode'] == 'plan_only'


def test_ordered_five_member_replies_receive_previous_outputs_and_skip_review(tmp_path):
    store, room, lead, providers, coordinator = setup(tmp_path, dict.fromkeys(
        ('planner', 'local', 'claude', 'codex', 'cursor', 'antigravity'), ''))
    coordinator.create(room, 'Count one to five in order.', 'post', consent=True)
    planner = coordinator.advance(room, 'post')['children']['planner']
    members = ['kyra', 'claude', 'codex', 'cursor', 'antigravity']
    providers.states[planner['id']].update(state='completed', text='Count in order.', planner_kind='plan', plan=[
        dict(provider=p, title=f'Count {i}', instruction=f'Say {i}.', order=i, mode='reply')
        for i, p in enumerate(members, 1)])
    for i in range(1, 6):
        result = coordinator.advance(room, 'post')
        assert [k for k, c in result['children'].items() if c.get('id')] == ['planner'] + [f'task:{j}' for j in range(1, i+1)]
        assert all(result['children'][f'task:{j}']['state'] == 'waiting' for j in range(i+1, 6))
        child = result['children'][f'task:{i}']
        if i > 1:
            assert str(i-1) in providers.submits[-1][2]
            assert 'Previous step results' in providers.submits[-1][2]
        providers.states[child['id']].update(state='completed', text=str(i))
    result = coordinator.advance(room, 'post')
    assert result['state'] == 'completed' and result['reason'] == 'replies_completed'
    assert [p for _, p, _, _ in providers.submits] == ['planner', 'local', 'claude', 'codex', 'cursor', 'antigravity']
    assert lead.calls == []


def test_parallel_group_waits_for_all_results_before_next_order(tmp_path):
    _, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Parallel then sequential.', 'post', consent=True)
    planner = coordinator.advance(room, 'post')['children']['planner']
    providers.states[planner['id']].update(state='completed', planner_kind='plan', plan=[
        dict(provider=p, title=f'Part {i}', instruction=f'Part {i}', order=order, mode='reply')
        for i, (p, order) in enumerate([('cursor', 1), ('antigravity', 1), ('cursor', 2)])])
    result = coordinator.advance(room, 'post')
    assert {k for k, c in result['children'].items() if c.get('id')} == {'planner', 'task:1', 'task:2'}
    providers.states[result['children']['task:1']['id']].update(state='completed', text='First done')
    assert coordinator.advance(room, 'post')['children']['task:3']['id'] is None
    providers.states[result['children']['task:2']['id']].update(state='completed', text='Second done')
    assert coordinator.advance(room, 'post')['children']['task:3']['id'] is not None
    assert 'First done' in providers.submits[-1][2] and 'Second done' in providers.submits[-1][2]


def test_composer_projection_preserves_identical_distinct_posts_once():
    parents = [dict(room_id='a'*32, request_id=f'post-{i}', text='hi', created_at=i, children={
        'planner': dict(id=f'run-{i}', request_id=f'child-{i}')}) for i in (1, 2)]
    view = dict(delegations=parents, messages=[
        dict(id=f'm-{i}', author='user', request_id=f'child-{i}', text='hi', created_at=i) for i in (1, 2)])
    view['messages'].append(dict(id='stage', author='system', text='Derived prompt.', created_at=3))
    team_http.collapse_delegated_requests(view)
    assert [m['text'] for m in view['messages'] if m['author'] == 'user'] == ['hi', 'hi']
    assert view['messages'][-1]['author'] == 'system'


def test_stop_before_retry_prevents_a_new_delivery(tmp_path):
    _, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Inspect.', 'post', mode='cursor', consent=True)
    child = coordinator.advance(room, 'post')['children']['cursor']
    providers.states[child['id']].update(state='blocked', reason='temporary')
    coordinator.status(room, 'post')
    coordinator.stop(room, 'post')
    coordinator.advance(room, 'post')
    assert len(providers.submits) == 1


def test_earlier_failure_must_finish_retry_before_later_order(tmp_path):
    _, room, _, providers, coordinator = setup(tmp_path)
    coordinator.create(room, 'Two steps.', 'post', consent=True)
    planner = coordinator.advance(room, 'post')['children']['planner']
    providers.states[planner['id']].update(state='completed', planner_kind='plan', plan=[
        dict(provider=p, title=p, instruction='Reply.', order=i)
        for i, p in enumerate(['cursor', 'antigravity'], 1)])  # Omitted mode means reply.
    original = coordinator.submit
    def failing(*a, **kw):
        value = original(*a, **kw)
        return {**value, 'state': 'blocked', 'reason': 'offline'} if a[1] == 'cursor' else value
    coordinator.submit = failing
    assert coordinator.advance(room, 'post')['children']['task:2']['id'] is None
    assert coordinator.advance(room, 'post')['children']['task:2']['id'] is None
    row = coordinator.advance(room, 'post')
    repair = row['children']['repair:task:1']
    providers.states[repair['id']].update(state='blocked', reason='No repair available')
    row = coordinator.advance(room, 'post')
    assert row['children']['task:1']['state'] == 'blocked'
    assert row['state'] == 'blocked' and row['children']['task:2']['id'] is None
    recovered = Coordinator(coordinator.store, coordinator.lead, roster=providers.roster,
                            submit=coordinator.submit, child_status=providers.child_status,
                            child_stop=providers.child_stop)
    recovered.advance(room, 'post')
    assert len([s for s in providers.submits if s[1] == 'cursor']) == 1


def test_projection_hides_retry_requests_and_preserves_system_prompts():
    parent = dict(room_id='a'*32, request_id='post', text='Hi', created_at=1,
                  children={'cursor': dict(id='new', request_id='retry')},
                  previous_children={'cursor': dict(id='old', request_id='first')})
    view = dict(delegations=[parent], messages=[
        dict(id='old-request', author='user', text='Hi', created_at=1),
        dict(id='new-request', author='system', text='Hi', created_at=2)])
    team_http.collapse_delegated_requests(view)
    assert [m['text'] for m in view['messages'] if m['author'] == 'user'] == ['Hi']
    assert any(m['id'] == 'new-request' and m['author'] == 'system' for m in view['messages'])


def test_http_derived_native_assignment_uses_system_author(tmp_path, monkeypatch):
    submitted = []
    store = SimpleNamespace(root=tmp_path, submit=lambda *a, **kw: submitted.append(kw) or {'run_id': 'a'*32})
    service = SimpleNamespace(transport=SimpleNamespace(status=lambda: {'ready': True}))
    monkeypatch.setattr(team_http, 'wake_service', lambda _: None)
    coordinator = team_http.get_delegation(store, None, service)
    coordinator.submit('room', 'codex', 'Reply.', 'child', parent_request_id='parent')
    assert submitted == [dict(share_with_team=True, execution_mode='reply_codex',
                              parent_request_id='parent', author='system')]


@pytest.mark.parametrize('reply_kind', ['valid', 'wrong_run', 'plain_text', 'empty', 'owner_question'])
def test_connected_work_accepts_only_correlated_result_and_records_ack(tmp_path, monkeypatch, reply_kind, isolated_app_dispatch):
    import json

    from companion.team_cursor import AdapterResult
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a'*32, provider='cursor', request_id='post', text='Reply.', config_hash='pin')
    monkeypatch.setattr(team_connected, 'team_handbook', lambda: 'Synthetic handbook')
    monkeypatch.setattr(team_connected, '_config', lambda _: (dict(workspace=str(tmp_path), model='fixture'), 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (None, None))
    reply = dict(run_id=row['id'] if reply_kind != 'wrong_run' else 'b'*32, stage='reply_cursor', attempt=0,
                 status='needs_input' if reply_kind == 'owner_question' else 'completed', summary='Fixture reply',
                 requires_owner_decision=reply_kind == 'owner_question')
    # The adapter result is correlated to this run; plain text is a text-only reply (2026-10-02 live @all).
    body = {'plain_text': 'Fixture reply', 'empty': '  '}.get(reply_kind) or json.dumps(reply)
    adapter = SimpleNamespace(run=lambda *a, **kw: AdapterResult('cursor', row['id'], 'completed', body, 'session', 'fixture'))
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: adapter)
    team_connected._work(store, row)
    result = store.get(row['id'])
    valid = reply_kind in {'valid', 'owner_question', 'plain_text'}
    assert result['state'] == (reply['status'] if valid else 'failed')
    assert bool(result['acknowledged_at']) == valid
    assert result['text'] == ('Fixture reply' if valid else '')
