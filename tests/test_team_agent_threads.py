"""T1 builder tests: a thread per agent row; a follow-up goes to that same member with its work in context.

Real TeamStore and TeamService over a temp root with the recording fake transport (Claude, Codex). Cursor and
Antigravity go through a fake `team_connected.submit` (with and without session resume) and a fake run store.
The delegation row is written in the shape team_delegation saves. No native chat, app or network is used.
"""
import inspect
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import team_chat, team_connected, team_http, team_log, team_replies
from companion.input_labels import InputLabeller
from companion.team_threads import Store, agent_thread_id, child_member
from tests.test_team_chat_acceptance import bound, respond
from tests.test_team_native_modes import RevisionTransport

LABELS = InputLabeller(third_party_names=lambda: ['Example Person'])
REQUEST = 'req-1'


@pytest.fixture
def world(tmp_path, monkeypatch):
    transport = RevisionTransport()
    store, service = bound(team_chat, tmp_path, transport)
    book = tmp_path / 'handbook.md'
    book.write_text('# Kyra Team handbook\nSlack, not email.')
    monkeypatch.setattr(team_replies, 'HANDBOOK', book)
    room = store.create_room('Find opportunities')['id']
    return SimpleNamespace(store=store, service=service, transport=transport, threads=Store(store.root), room=room)


def delegation(world, children, *, prompts=None, roles=None, plan=None, **extra):
    body = dict(request_id=REQUEST, room_id=world.room, text='Find opportunities', mode='auto', state='running',
                reason='', targets=list(children), children=children, prompts=prompts or {}, roles=roles or {},
                plan=plan or [], **extra)
    db = sqlite3.connect(world.store.root / 'delegations.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS delegations (room_id TEXT NOT NULL, request_id TEXT NOT NULL, '
               'body TEXT NOT NULL, PRIMARY KEY(room_id, request_id))')
    db.execute('INSERT OR REPLACE INTO delegations VALUES (?, ?, ?)', (world.room, REQUEST, json.dumps(body)))
    db.commit()
    db.close()


def done(text, **extra):
    return dict(id='x' * 32, state='completed', reason='', request_id='c', text=text, **extra)


def lines(world, key):
    return world.threads.thread(agent_thread_id(world.room, REQUEST, key), [])['lines']


def prompt_of(world, line):
    return next(m['text'] for m in world.store.get_room(world.room)['messages']
                if m.get('request_id') == 'reply.' + line['id'])


class Apps:
    """Fake connected-app layer: records submits, answers from a run table."""

    def __init__(self, resume=True):
        self.calls, self.runs, self.stopped, self.during = [], {}, [], None
        self.resume = resume

    def submit(self, root, room, *, provider, text, request_id, message_author='user', session_id=None,
               read_only=False):
        self.calls.append(dict(provider=provider, text=text, request_id=request_id, session_id=session_id,
                               read_only=read_only))
        ident = f'app{len(self.calls)}'
        self.runs[ident] = dict(id=ident, provider=provider, state='running', text='', reason='')
        if self.during:
            self.during()  # e.g. The owner presses Stop while the app run is being started
        return self.runs[ident]

    def install(self, monkeypatch):
        if self.resume:
            monkeypatch.setattr(team_connected, 'submit', self.submit)
        else:
            def submit(root, room, *, provider, text, request_id, message_author='user'):
                return self.submit(root, room, provider=provider, text=text, request_id=request_id)
            monkeypatch.setattr(team_connected, 'submit', submit)
        apps = self

        class Runs:
            def __init__(self, root):
                pass

            def get(self, ident):
                return dict(apps.runs[ident])

            def for_room(self, room_id):
                return [dict(r) for r in apps.runs.values()]

            def stop(self, room_id, ident):
                apps.stopped.append(ident)
                apps.runs[ident].update(state='stopped')
        monkeypatch.setattr(team_connected, 'ConnectedStore', Runs)


def test_thread_ids_are_fixed_per_child_and_the_room_map_counts_and_unread(world):
    delegation(world, {'step-1': done('Built the job watcher.')}, roles={'step-1': 'worker'})
    key = agent_thread_id(world.room, REQUEST, 'step-1')
    assert key == agent_thread_id(world.room, REQUEST, 'step-1') != agent_thread_id(world.room, REQUEST, 'step-2')
    assert world.threads.agent_threads(world.room) == {}
    opened = world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-1', 'codex', 'Why?', 'a-1')
    assert opened['thread_id'] == key and opened['message']['responder'] == 'codex'
    again = world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-1', 'codex', 'Why?', 'a-1')
    assert again == opened  # a double click stores one reply
    world.threads.notify(key, 'reply')
    assert world.threads.agent_threads(world.room) == {f'{REQUEST}/step-1': dict(thread_id=key, count=1, unread=1)}


def test_local_rooms_and_members_without_a_follow_up_are_refused(world):
    local = world.store.create_room('Private', local_only=True)['id']
    with pytest.raises(PermissionError):
        world.threads.agent_reply(world.store.get_room(local), REQUEST, 'k', 'codex', 'hi', 'b-1')
    with pytest.raises(ValueError, match='Kyra'):
        world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'k', 'local', 'hi', 'b-2')


def test_a_codex_step_follow_up_goes_to_codex_with_its_task_result_and_thread(world):
    delegation(world, {'step-1': done('Built the job watcher; 94 tests pass.')},
               prompts={'step-1': 'Build the job watcher in src/jobs.py.'}, roles={'step-1': 'worker'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-1', 'codex', 'Which file?', 'c-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    line = lines(world, 'step-1')[0]
    run = next(r for r in world.store.get_room(world.room)['runs'] if r['id'] == line['run_id'])
    assert run['execution_mode'] == 'reply_codex'
    prompt = prompt_of(world, line)
    assert 'Slack, not email.' in prompt and "You are Codex, the team's builder" in prompt
    assert 'Build the job watcher in src/jobs.py.' in prompt and '94 tests pass' in prompt
    assert 'Which file?' in prompt and 'chat' in prompt and 'details' in prompt
    assert 'only the planner turns it into work' in prompt
    world.service.step(line['run_id'])
    respond(world.transport, 'reply_codex', run_id=line['run_id'], summary='src/jobs.py.')
    world.service.step(line['run_id'])
    team_replies.pump(world.service, world.threads, labeller=LABELS)
    assert [(x['author'], x['state']) for x in lines(world, 'step-1')] == [('user', 'answered'), ('codex', 'answered')]


def test_a_planner_follow_up_goes_to_claude(world):
    delegation(world, {'planner': done('Plan: 3 steps.')}, prompts={'planner': 'Plan the sample task.'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'planner', 'claude', 'Why 3?', 'd-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    line = lines(world, 'planner')[0]
    run = next(r for r in world.store.get_room(world.room)['runs'] if r['id'] == line['run_id'])
    assert run['execution_mode'] == 'reply_claude'
    assert 'Plan the sample task.' in prompt_of(world, line) and 'Plan: 3 steps.' in prompt_of(world, line)


def test_a_cursor_follow_up_resumes_its_session_and_the_answer_lands_in_the_thread(world, monkeypatch):
    apps = Apps(resume=True)
    apps.install(monkeypatch)
    delegation(world, {'step-2': done('Wrote the scraper.', session=dict(session_id='s-42', surface='cursor_cli_acp'))},
               prompts={'step-2': 'Write the scraper.'}, plan=[dict(provider='cursor', title='Scraper', instruction='x')],
               roles={'step-2': 'cursor'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-2', 'cursor', 'Rate limits?', 'e-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    call = apps.calls[0]
    assert (call['provider'], call['session_id'], call['read_only']) == ('cursor', 's-42', True)
    assert call['request_id'].startswith('reply.') and 'Wrote the scraper.' in call['text']
    assert world.store.get_room(world.room)['runs'] == []  # no Team build run
    apps.runs['app1'].update(state='completed', text='Yes: one request per second.', read_only_enforced=False)
    team_replies.pump(world.service, world.threads, labeller=LABELS)
    thread = lines(world, 'step-2')
    logged = team_log.read(world.store.root, world.room, thread[1]['id'])['details']
    assert 'read-only was not enforced' in logged
    assert [(x['author'], x['state']) for x in thread] == [('user', 'answered'), ('cursor', 'answered')]
    assert thread[1]['text'] == 'Yes: one request per second.' and thread[1]['role'] == 'builder'


def test_an_antigravity_follow_up_without_resume_gets_its_result_and_thread_as_context(world, monkeypatch):
    apps = Apps(resume=False)
    apps.install(monkeypatch)
    assert 'session_id' not in inspect.signature(team_connected.submit).parameters
    delegation(world, {'step-3': done('Researched 7 lock cases.', session=dict(session_id='g-1', surface='antigravity_cli'))},
               prompts={'step-3': 'Research lock cases.'}, roles={'step-3': 'antigravity'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-3', 'antigravity', 'Which is worst?', 'f-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    call = apps.calls[0]
    assert call['session_id'] is None and call['read_only'] is False and 'Researched 7 lock cases.' in call['text'] and 'Which is worst?' in call['text']
    apps.runs['app1'].update(state='blocked', reason='permission_required')
    team_replies.pump(world.service, world.threads, labeller=LABELS)
    last = lines(world, 'step-3')[-1]
    assert last['author'] == 'kyra' and 'permission_required' in last['text']


def test_restricted_task_or_result_context_holds_the_follow_up(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    delegation(world, {'step-1': done('Asked Example Person about the headache.')}, roles={'step-1': 'cursor'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-1', 'cursor', 'And then?', 'g-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'refused': 1}
    assert apps.calls == [] and world.store.get_room(world.room)['runs'] == []
    assert 'not sent' in lines(world, 'step-1')[-1]['text']


def test_a_missing_child_or_a_member_mismatch_is_never_sent(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    delegation(world, {'step-1': done('Built it.')}, roles={'step-1': 'worker'})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'gone', 'codex', 'Hi?', 'h-1')
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-1', 'cursor', 'Hi?', 'h-2')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'failed': 2}
    assert apps.calls == [] and world.store.get_room(world.room)['runs'] == []


def test_routes_get_a_thread_by_child_key_post_a_reply_and_list_the_room_map(world, monkeypatch):
    from companion import team_agent_threads

    monkeypatch.setattr(team_http, 'get_settings', lambda: SimpleNamespace(inline_worker=False))
    delegation(world, {'step-1': done('Built it.')}, roles={'step-1': 'worker'})
    app = FastAPI()
    app.include_router(team_agent_threads.router)
    app.dependency_overrides[team_http.get_store] = lambda: world.store
    client = TestClient(app)
    base = f'/api/team/rooms/{world.room}/delegations/{REQUEST}/children/step-1'
    empty = client.get(base + '/thread').json()
    assert empty == dict(thread_id=agent_thread_id(world.room, REQUEST, 'step-1'), member='codex', lines=[])
    posted = client.post(base + '/reply', json={'text': 'Why?', 'request_id': 'r-1'})
    assert posted.status_code == 200 and posted.json()['message']['responder'] == 'codex'
    assert [x['text'] for x in client.get(base + '/thread').json()['lines']] == ['Why?']
    listed = client.get(f'/api/team/rooms/{world.room}/agent-threads').json()
    assert listed['threads'][f'{REQUEST}/step-1']['count'] == 1
    assert client.get(f'/api/team/rooms/{world.room}/delegations/{REQUEST}/children/nope/thread').status_code == 404


def test_the_member_is_the_assignee_then_the_plan_provider_then_the_role():
    plan = [dict(provider='antigravity', title='a', instruction='x'), dict(provider='codex', title='b', instruction='y')]
    row = dict(children={'t1': {}, 't2': {'assignee': 'claude'}, 'planner': {}, 'local': {}},
               task_targets=['t1', 't2'], plan=plan, roles={'t1': 'worker'})
    assert [child_member(row, key) for key in ('t1', 't2', 'planner', 'local')] == ['antigravity', 'claude', 'claude', None]
    keyed = dict(children={'s': {}}, plan=[dict(key='s', provider='cursor', title='c', instruction='z')])
    assert child_member(keyed, 's') == 'cursor'


def test_stop_while_the_app_run_starts_stops_that_run_and_the_line_keeps_its_run_id(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    delegation(world, {'step-2': done('Wrote the scraper.')}, roles={'step-2': 'cursor'})
    opened = world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-2', 'cursor', 'Why?', 's-1')
    apps.during = lambda: team_replies.stop(world.service, world.threads, opened['thread_id'])
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'stopped': 1}
    assert apps.stopped == ['app1']
    line = lines(world, 'step-2')[0]
    assert (line['state'], line['run_id']) == ('stopped', 'app:app1')


def test_earlier_answers_ride_along_in_full_from_the_log(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    delegation(world, {'step-2': done('Wrote the scraper.')}, roles={'step-2': 'cursor'})
    thread_id = world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-2', 'cursor', 'Why?', 'u-1')['thread_id']
    with world.threads.transaction() as db:
        db.execute("UPDATE thread_messages SET state = 'answered'")
        db.execute("INSERT INTO thread_messages (id, thread_id, author, text, created_at, state) VALUES "
                   "('ans1', ?, 'cursor', 'Short line.', 1e12, 'answered')", (thread_id,))
    team_log.append(world.store.root, world.room, message_id='ans1', author='cursor', kind='reply',
                    details='Short line. DETAIL-RATE-LIMIT is 1 per second.', created_at=1e12)
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'step-2', 'cursor', 'Which limit?', 'u-2')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    assert 'DETAIL-RATE-LIMIT is 1 per second.' in apps.calls[0]['text']


@pytest.mark.parametrize('member,role', [('cursor', 'cursor'), ('codex', 'worker')])
def test_the_follow_up_carries_the_full_logged_result_under_the_child_id(world, monkeypatch, member, role):
    apps = Apps()
    apps.install(monkeypatch)
    child = dict(done('Cursor done.'), id='c' * 32)  # the row keeps only the short chat line
    delegation(world, {'task:3': child}, roles={'task:3': role})
    team_log.append(world.store.root, world.room, message_id='c' * 32, author=member, kind='app_result',
                    details='Cursor done. FULL-RESULT-MARKER: changed src/jobs.py.', created_at=1.0)
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'task:3', member, 'Which file?', 'v-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    prompt = apps.calls[0]['text'] if member == 'cursor' else prompt_of(world, lines(world, 'task:3')[0])
    assert 'FULL-RESULT-MARKER' in prompt


def test_an_app_follow_up_carries_its_build_result_logged_under_the_app_run_not_the_review(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    child = dict(done('CODEX-REVIEW reviewed at the worktree revision.'), id='r' * 32, request_id='child-req-3',
                 assignee='cursor')
    apps.runs['b' * 32] = dict(id='b' * 32, provider='cursor', request_id='child-req-3', state='completed',
                               text='Cursor done.', reason='')
    apps.runs['o' * 32] = dict(id='o' * 32, provider='cursor', request_id='other', state='completed', text='', reason='')
    team_log.append(world.store.root, world.room, message_id='b' * 32, author='cursor', kind='app_result',
                    details='Cursor done. BUILD-MARKER: changed src/scraper.py.', created_at=1.0)
    team_log.append(world.store.root, world.room, message_id='o' * 32, author='cursor', kind='app_result',
                    details='OTHER-STEP-MARKER', created_at=1.0)
    delegation(world, {'task:3': child})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'task:3', 'cursor', 'Which file?', 'w-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    prompt = apps.calls[0]['text']
    assert 'BUILD-MARKER' in prompt and 'CODEX-REVIEW' in prompt and 'OTHER-STEP-MARKER' not in prompt


def test_a_reviewed_step_carries_the_build_result_kept_in_build_results(world, monkeypatch):
    apps = Apps()
    apps.install(monkeypatch)
    review = dict(done('CODEX-REVIEW passed.'), id='r' * 32, request_id='review-req', assignee='cursor', stage='review')
    build = dict(done('Cursor done.'), id='b' * 32, request_id='build-req', assignee='cursor')
    team_log.append(world.store.root, world.room, message_id='b' * 32, author='cursor', kind='app_result',
                    details='Cursor done. BUILD-RESULT-MARKER.', created_at=1.0)
    delegation(world, {'task:3': review}, build_results={'task:3': build})
    world.threads.agent_reply(world.store.get_room(world.room), REQUEST, 'task:3', 'cursor', 'Which file?', 'x-1')
    assert team_replies.pump(world.service, world.threads, labeller=LABELS) == {'sent': 1}
    prompt = apps.calls[0]['text']
    assert 'BUILD-RESULT-MARKER' in prompt and 'CODEX-REVIEW passed.' in prompt
