"""D1 contracts with scratch stores and a fake guarded submission seam."""
import pytest

from companion.errors import ApiError
from companion.team_delegation import Coordinator
from tests.test_team_delegation_routing import setup


def planned(tmp_path, tasks):
    store, room, _, providers, coordinator = setup(tmp_path, dict.fromkeys(
        ('planner', 'worker', 'reviewer', 'codex', 'claude', 'cursor', 'antigravity'), ''))
    calls = []
    def submit(*args, **kwargs):
        calls.append((args[1], kwargs))
        return providers.submit(*args, **kwargs)
    coordinator.submit = submit
    coordinator.create(room, 'Build the fixture.', 'post', consent=True)
    planner = coordinator.advance(room, 'post')['children']['planner']
    providers.states[planner['id']].update(state='completed', planner_kind='plan', plan=tasks)
    return store, room, providers, coordinator, calls


def task(provider, order=1, mode='code'):
    return dict(provider=provider, title='Fixture', instruction='Build the fixture.', order=order, mode=mode)


@pytest.mark.parametrize('builder', ['codex', 'claude', 'cursor', 'antigravity'])
def test_each_builder_gets_cross_model_review_and_progress(tmp_path, builder):
    _, room, providers, coordinator, calls = planned(tmp_path, [task(builder)])
    row = coordinator.advance(room, 'post')
    child = row['children']['task:1']
    assert child['state'] == 'queued'
    assert calls[-1] == ('worker', dict(parent_request_id='post', builder=builder,
                                       reviewer='claude' if builder == 'codex' else 'codex'))
    providers.states[child['id']].update(state='running', stage='review')
    row = coordinator.status(room, 'post')
    step = row['plan'][0]
    assert step['state'] == 'review' and step['assignee'] == builder
    assert step['started_at'] and step['elapsed'] >= 0
    assert row['progress']['running'][0]['key'] == 'task:1'
    providers.states[child['id']].update(state='completed', text='Built and reviewed.')
    if builder in {'cursor', 'antigravity'}:
        providers.states[child['id']].update(workspace='/fixture/task', revision='a'*40, files=['fixture.py'],
                                             session_id='fixture-session', surface='fixture-app')
        row = coordinator.advance(room, 'post')
        assert row['progress']['done'] == 0
        assert row['children']['task:1']['session'] == dict(session_id='fixture-session', surface='fixture-app')
        assert calls[-1][0] == 'reviewer'
        assert calls[-1][1]['revision'] == 'a'*40 and calls[-1][1]['workspace_run_id'] == child['id']
        assert '/fixture/task' not in str(row)
        child = row['children']['task:1']
        providers.states[child['review_id']].update(state='completed', verdict='pass', text='Reviewed.')
    row = coordinator.advance(room, 'post')
    assert row['progress'] == dict(done=1, total=1, percent=100, running=[])
    assert row['plan'][0]['finished_at'] >= step['started_at']


def test_blocked_plan_repairs_once_then_waits_until_resume(tmp_path):
    store, room, providers, c, calls = planned(tmp_path, [task('cursor'), task('codex', 2)])
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='blocked', reason='sign_in_required')
    row = c.advance(room, 'post')
    assert row['children']['task:2']['id'] is None
    assert row['plan'][1]['state'] == 'waiting' and row['plan'][1]['waiting_on'] == 'task:1'
    repair = row['children']['repair:task:1']
    assert calls[-1][0] == 'planner'
    providers.states[repair['id']].update(state='completed', planner_kind='task', plan=[task('claude')])
    row = c.advance(room, 'post')
    retried = row['children']['task:1']
    assert retried['request_id'] != child['request_id'] and row['plan'][0]['assignee'] == 'claude'
    providers.states[retried['id']].update(state='blocked', reason='fixture_unavailable')
    row = c.advance(room, 'post')
    assert row['state'] == 'blocked' and row['who_must_act'] == 'Claude'
    assert row['children']['task:2']['id'] is None and not row['children']['task:1'].get('skipped')
    for _ in range(3):
        c.advance(room, 'post')
    lines = [m['text'] for m in store.get_room(room)['messages'] if m['text'].startswith('Blocked:')]
    assert len(lines) == 1 and 'Claude must' in lines[0]
    recovered = Coordinator(store, c.lead, roster=providers.roster, submit=c.submit,
                            child_status=providers.child_status, child_stop=providers.child_stop)
    recovered.resume(room, 'post')
    row = recovered.advance(room, 'post')
    assert row['children']['task:1']['request_id'] != retried['request_id']
    providers.states[row['children']['task:1']['id']].update(state='completed', text='Fixed.')
    row = recovered.advance(room, 'post')
    assert row['progress']['done'] == 1 and row['children']['task:2']['state'] == 'queued'


def test_resume_refuses_active_and_uncertain_children(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('codex')])
    child = c.advance(room, 'post')['children']['task:1']
    with pytest.raises(ApiError) as error:
        c.resume(room, 'post')
    assert error.value.status_code == 409
    providers.states[child['id']].update(state='uncertain', reason='send_unknown')
    c.status(room, 'post')
    with pytest.raises(ApiError):
        c.resume(room, 'post')


def test_local_failure_reason_never_enters_repair_prompt(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('kyra', mode='reply')])
    providers.roster_value['local'] = ''
    with c._edit(room, 'post') as row:
        row['roster']['local'] = ''
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='blocked', reason='PRIVATE-LOCAL-HISTORY', text='PRIVATE-OUTPUT')
    c.advance(room, 'post')
    prompt = providers.submits[-1][2]
    assert providers.submits[-1][1] == 'planner'
    assert 'PRIVATE' not in prompt and 'Local step unavailable' in prompt


def test_app_review_failure_returns_to_app_builder_not_native_worker(tmp_path):
    _, room, providers, c, calls = planned(tmp_path, [task('cursor'), task('codex', 2)])
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='completed', workspace='/fixture/task', revision='a'*40, files=[])
    review = c.advance(room, 'post')['children']['task:1']
    providers.states[review['review_id']].update(state='needs_input', verdict='changes_requested', reason='Fix the fixture.')
    row = c.advance(room, 'post')
    repair = row['children']['repair:task:1']
    assert row['children']['task:2']['id'] is None
    providers.states[repair['id']].update(state='completed', planner_kind='task', plan=[task('cursor')])
    c.advance(room, 'post')
    assert calls[-1] == ('worker', dict(parent_request_id='post', builder='cursor', reviewer='codex'))


def test_resume_http_and_room_progress(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from companion import team_http
    _, room, providers, c, _ = planned(tmp_path, [task('codex'), task('claude', 2)])
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='blocked', reason='Owner choice', requires_owner_decision=True)
    c.advance(room, 'post')
    app = FastAPI()
    app.include_router(team_http.router)
    app.dependency_overrides[team_http.get_delegation] = lambda: c
    monkeypatch.setattr(team_http, 'get_delegation', lambda *a: c)
    monkeypatch.setattr(team_http, 'get_lead', lambda *a: None)
    monkeypatch.setattr(team_http, 'get_service', lambda *a: None)
    wakes = []
    monkeypatch.setattr(team_http, 'wake_delegation', lambda value: wakes.append(value))
    response = TestClient(app).post(f'/api/team/rooms/{room}/delegations/post/resume', json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['room']['delegations'][0]['progress']['total'] == 2
    assert body['delegation']['plan'][1]['waiting_on'] == 'task:1'
    assert wakes == [c]


def test_independent_members_start_together_and_build_is_not_done_before_review(tmp_path):
    store, room, providers, c, _ = planned(tmp_path, [task('cursor'), task('codex'), task('claude')])
    row = c.advance(room, 'post')
    assert all(key in row['children'] for key in ('task:1', 'task:2', 'task:3'))
    providers.states[row['children']['task:1']['id']].update(state='completed', text='Built.',
                                                           workspace='/fixture/task', revision='b'*40, files=[])
    row = c.status(room, 'post')
    assert row['progress']['done'] == 0 and row['plan'][0]['state'] == 'review'
    assert not any(m['text'].startswith('task:1 done') for m in store.get_room(room)['messages'])


def test_resume_old_skipped_plan_retries_under_current_rules(tmp_path):
    _, room, _, c, _ = planned(tmp_path, [task('claude'), task('codex', 2)])
    c.advance(room, 'post')
    with c._edit(room, 'post') as row:
        row['children']['task:1'].update(state='blocked', reason='independent_code_review_not_supported', skipped=True)
        row.update(state='completed', reason='Skipped after one retry: task:1', workflow='incomplete')
    assert c.status(room, 'post')['children']['task:1']['state'] == 'skipped'
    c.resume(room, 'post')
    row = c.advance(room, 'post')
    assert row['children']['task:1']['state'] == 'queued'
    assert row['children']['task:2']['id'] is None


def test_failed_planner_repair_does_not_poison_resumed_plan(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('codex')])
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='blocked', reason='Offline')
    repair = c.advance(room, 'post')['children']['repair:task:1']
    providers.states[repair['id']].update(state='failed', reason='No repair')
    c.advance(room, 'post')
    c.resume(room, 'post')
    row = c.advance(room, 'post')
    providers.states[row['children']['task:1']['id']].update(state='completed', text='Fixed and reviewed.')
    row = c.advance(room, 'post')
    assert row['reason'] == 'ready_for_merge' and row['progress']['percent'] == 100


@pytest.mark.parametrize('verdict', ['changes_requested', 'needs_input', None])
def test_app_review_never_accepts_completed_without_pass(tmp_path, verdict):
    _, room, providers, c, _ = planned(tmp_path, [task('cursor'), task('codex', 2)])
    child = c.advance(room, 'post')['children']['task:1']
    providers.states[child['id']].update(state='completed', workspace='/fixture/task', revision='a'*40, files=[])
    review = c.advance(room, 'post')['children']['task:1']
    providers.states[review['review_id']].update(state='completed', text='Review result')
    if verdict:
        providers.states[review['review_id']]['verdict'] = verdict
    row = c.advance(room, 'post')
    assert row['progress']['done'] == 0 and row['children']['task:2']['id'] is None


@pytest.mark.parametrize('builder', ['codex', 'claude', 'cursor', 'antigravity'])
def test_http_dispatch_keywords_match_builder_seam(tmp_path, monkeypatch, builder):
    from types import SimpleNamespace

    from companion import team_connected, team_http
    from tests.test_team_delegation import Store
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    calls = []
    monkeypatch.setattr(store, 'submit', lambda *a, **kw: calls.append(kw) or dict(run_id='a'*32))
    monkeypatch.setattr(team_connected, 'submit', lambda *a, **kw: calls.append(kw) or dict(id='b'*32, state='queued'))
    monkeypatch.setattr(team_http, 'wake_service', lambda *a: None)
    service = SimpleNamespace(transport=SimpleNamespace(status=lambda: dict(ready=True)))
    coordinator = team_http.get_delegation(store, None, service)
    coordinator.submit(room, 'worker', 'Build.', 'child', parent_request_id='parent',
                       builder=builder, reviewer='claude' if builder == 'codex' else 'codex')
    assert calls[0]['execution_mode'] == ('code' if builder in {'cursor', 'antigravity'} else 'worker')
    assert calls[0]['reviewer'] == ('claude' if builder == 'codex' else 'codex')
    coordinator.submit(room, 'reviewer', 'Review.', 'review', parent_request_id='parent', builder='cursor',
                       reviewer='codex', workspace='/fixture/task', workspace_run_id='b'*32,
                       revision='a'*40, files=[])
    assert calls[1]['execution_mode'] == 'code_review'
    assert calls[1]['workspace_run_id'] == 'b'*32


def test_children_carry_checklist_fields_and_plan_mirrors_them(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('codex'), task('claude', 2), task('cursor', 3)])
    row = c.advance(room, 'post')
    first = row['children']['task:1']
    providers.states[first['id']].update(state='completed', text='Built and reviewed.')
    row = c.advance(room, 'post')
    second = row['children']['task:2']
    providers.states[second['id']].update(state='blocked', reason='Permission required', requires_owner_decision=True)
    row = c.advance(room, 'post')
    assert row['children']['task:1']['state'] == 'done'
    assert row['children']['task:1']['finished_at'] >= first['started_at']
    assert row['children']['task:2']['blocked']['who_must_act'] == 'the owner'
    assert row['children']['task:3']['state'] == 'waiting'
    assert row['children']['task:3']['waiting_on'] == 'task:2'
    for step in row['plan']:
        assert all(row['children'][step['key']][k] == v for k, v in step.items())


def test_same_full_plan_repair_retries_only_blocked_step_once(tmp_path):
    tasks = [task('codex'), task('claude', 2), task('cursor', 3)]
    _, room, providers, c, calls = planned(tmp_path, tasks)
    first = c.advance(room, 'post')['children']['task:1']
    providers.states[first['id']].update(state='completed', text='Built and reviewed.')
    blocked = c.advance(room, 'post')['children']['task:2']
    providers.states[blocked['id']].update(state='blocked', reason='Offline')
    repair = c.advance(room, 'post')['children']['repair:task:2']
    providers.states[repair['id']].update(state='completed', planner_kind='plan', plan=tasks)
    row = c.advance(room, 'post')
    retry = row['children']['task:2']
    assert retry['id'] != blocked['id'] and retry['state'] == 'queued'
    assert row['children']['task:1']['id'] == first['id']
    providers.states[retry['id']].update(state='blocked', reason='Offline')
    row = c.advance(room, 'post')
    assert row['state'] == 'blocked'
    assert sum(role == 'planner' for role, _ in calls) == 2
    assert len([role for role, _ in calls if role == 'worker']) == 3


def test_app_review_preserves_builder_identity_and_result(tmp_path):
    _, room, providers, c, _ = planned(tmp_path, [task('cursor')])
    built = c.advance(room, 'post')['children']['task:1']
    providers.states[built['id']].update(state='completed', text='CURSOR-RESULT-MARKER',
                                        workspace='/fixture/task', revision='a'*40, files=[],
                                        session_id='cursor-session', surface='fixture')
    reviewing = c.advance(room, 'post')['children']['task:1']
    assert reviewing['id'] == built['id'] and reviewing['text'] == 'CURSOR-RESULT-MARKER'
    assert reviewing['state'] == 'review' and reviewing['review_id'] != built['id']
    providers.states[reviewing['review_id']].update(state='completed', verdict='pass', text='Reviewed.')
    done = c.advance(room, 'post')['children']['task:1']
    assert done['state'] == 'done' and done['id'] == built['id']
    assert done['text'] == 'CURSOR-RESULT-MARKER'
    assert done['session'] == dict(session_id='cursor-session', surface='fixture')


def test_each_new_block_posts_once_even_after_restart_and_resume(tmp_path):
    store, room, providers, c, _ = planned(tmp_path, [task('codex'), task('claude', 2)])
    for attempt in range(2):
        child = c.advance(room, 'post')['children']['task:1']
        providers.states[child['id']].update(state='blocked', reason='Choose an option', requires_owner_decision=True)
        for _ in range(3):
            c.advance(room, 'post')
        lines = [m for m in store.get_room(room)['messages'] if m['text'].startswith('Blocked:')]
        assert len(lines) == attempt + 1
        assert 'report' not in c.status(room, 'post')
        c = Coordinator(store, c.lead, roster=providers.roster, submit=c.submit,
                        child_status=providers.child_status, child_stop=providers.child_stop)
        c.status(room, 'post')
        assert len([m for m in store.get_room(room)['messages'] if m['text'].startswith('Blocked:')]) == attempt + 1
        c.resume(room, 'post')
    assert len({m['id'] for m in lines}) == 2
    assert not any(s[1] == 'planner' for s in providers.submits[1:])


@pytest.mark.parametrize('builder', ['codex', 'cursor'])
def test_missing_builder_submit_keywords_block_without_sending_and_allow_resume(tmp_path, monkeypatch, builder):
    from types import SimpleNamespace

    from companion import team_connected, team_http
    store, room, providers, c, _ = planned(tmp_path, [task(builder)])
    def native_old(room_id, text, request_id, share_with_team=True, execution_mode='plan', *,
                   parent_request_id=None, author='user'):
        pytest.fail('Unsupported native submit was called')
    def app_old(root, room, *, provider, text, request_id, message_author='user'):
        pytest.fail('Unsupported app submit was called')
    monkeypatch.setattr(store, 'submit', native_old)
    monkeypatch.setattr(team_connected, 'submit', app_old)
    http = team_http.get_delegation(store, None, SimpleNamespace(transport=SimpleNamespace(status=lambda: dict(ready=True))))
    original = c.submit
    c.submit = lambda *a, **kw: http.submit(*a, **kw) if a[1] == 'worker' else original(*a, **kw)
    row = c.advance(room, 'post')
    assert row['children']['task:1']['state'] == 'blocked'
    assert row['children']['task:1']['reason'] == 'builder_seam_missing'
    repair = c.advance(room, 'post')['children']['repair:task:1']
    providers.states[repair['id']].update(state='needs_input', reason='Install the builder support.')
    assert c.advance(room, 'post')['state'] == 'blocked'
    c.submit = original
    c.resume(room, 'post')
    assert c.advance(room, 'post')['children']['task:1']['state'] == 'queued'


def test_type_error_inside_submit_stays_uncertain_and_never_retries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from companion import team_http
    store, room, _, c, _ = planned(tmp_path, [task('codex')])
    sends = []
    def native_send(*a, **kw):
        sends.append(kw)
        raise TypeError("Runtime failure after dispatch")
    monkeypatch.setattr(store, 'submit', native_send)
    c.submit = team_http.get_delegation(store, None, SimpleNamespace(transport=SimpleNamespace(status=lambda: dict(ready=True)))).submit
    assert c.advance(room, 'post')['state'] == 'uncertain'
    with pytest.raises(ApiError):
        c.resume(room, 'post')
    c.advance(room, 'post')
    assert len(sends) == 1


@pytest.mark.parametrize(('state', 'verdict'), [('completed', 'changes_requested'), ('needs_input', 'changes_requested'),
                                               ('completed', 'pass')])
def test_http_code_review_uses_saved_verdict_and_returns_failed_review_to_app(tmp_path, monkeypatch, state, verdict):
    from types import SimpleNamespace

    from companion import team_http
    store, room, providers, c, _ = planned(tmp_path, [task('cursor'), task('codex', 2)])
    built = c.advance(room, 'post')['children']['task:1']
    providers.states[built['id']].update(state='completed', workspace='/fixture/task', revision='a'*40, files=[])
    review = c.advance(room, 'post')['children']['task:1']
    http = team_http.get_delegation(store, None, SimpleNamespace())
    monkeypatch.setattr(store, 'get_room', lambda _: dict(runs=[dict(id=review['review_id'], status=state,
        execution_mode='code_review', stage='review', verdict=verdict, parent_request_id='post', reason='Review result')], messages=[]))
    actual = http.child_status(room, 'reviewer', review['review_id'])
    assert actual['verdict'] == verdict
    providers.states[review['review_id']].update(actual)
    row = c.advance(room, 'post')
    if verdict == 'pass':
        assert row['children']['task:1']['state'] == 'done'
    else:
        assert row['progress']['done'] == 0 and row['children']['task:2']['id'] is None
        repair = row['children']['repair:task:1']
        providers.states[repair['id']].update(state='completed', planner_kind='task', plan=[task('cursor')])
        c.advance(room, 'post')
        assert providers.submits[-1][1] == 'worker'


@pytest.mark.parametrize(('reason', 'owner', 'action'), [
    ('sign_in_required', 'the owner', 'sign in to Cursor'),
    ('build_failed', 'Cursor', 'fix the step'),
])
def test_dispatch_block_has_times_and_plain_action_for_the_right_owner(tmp_path, reason, owner, action):
    _, room, _, c, _ = planned(tmp_path, [task('cursor')])
    with c._edit(room, 'post') as row:
        row['roster']['cursor'] = reason
    child = c.advance(room, 'post')['children']['task:1']
    assert child['started_at'] and child['finished_at'] >= child['started_at']
    assert child['blocked']['who_must_act'] == owner
    assert action in child['blocked']['what']
    assert '_' not in child['blocked']['reason']


@pytest.mark.parametrize('reason', ['permission_required', 'permission_required:RunCommand'])
def test_named_permission_block_keeps_owner_and_readable_tool(tmp_path, reason):
    _, room, _, coordinator, _ = planned(tmp_path, [task('antigravity')])
    with coordinator._edit(room, 'post') as row:
        row['roster']['antigravity'] = reason
    child = coordinator.advance(room, 'post')['children']['task:1']
    assert child['blocked']['who_must_act'] == 'the owner'
    assert 'Antigravity asked to run a command' in child['blocked']['reason']
    assert ('(RunCommand)' in child['blocked']['reason']) == (':' in reason)
    assert 'permission_required' not in child['blocked']['reason']
    assert 'resume' in child['blocked']['what']
