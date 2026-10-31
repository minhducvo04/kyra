"""P3a builder tests for the Team thread store and API (a private design note, 2026-09-30).

Acceptance: a job thread shows only that job's Claude and Codex lines in order; a room with no Team work shows no
threads and no errors; the bell count goes down when a notification is read and stays down after a reload; a double
click on Reply stores one message. Temporary Team root only; no native, provider or network calls.
"""
import sqlite3
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import team_http
from companion.team_chat import TeamStore
from companion.team_threads import Store, job_threads, thread_run_ids


@pytest.fixture
def store(tmp_path):
    return TeamStore(tmp_path / 'team')


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(team_http.router)
    app.dependency_overrides[team_http.get_store] = lambda: store
    return TestClient(app)


def agent(store, room_id, author, text, run_id):
    ident = uuid.uuid4().hex
    with store.admission() as data:
        data['rooms'][room_id]['messages'].append(
            dict(id=ident, author=author, text=text, created_at=time.time(), run_id=run_id, kind='result'))
    return ident


def two_jobs(store):
    room = store.create_room('Work')['id']
    run_a, run_b, run_other = (uuid.uuid4().hex for _ in range(3))
    store.post_step(room, 'assigned', 'Claude planned 1 package', event_key='a1', parent_request_id='job-a')
    store.post_step(room, 'dispatched', 'Codex handoff (worker:1)', event_key='a2', parent_request_id='job-a',
                    run_id=run_a)
    store.post_step(room, 'assigned', 'Other job', event_key='b1', parent_request_id='job-b', run_id=run_other)
    codex = agent(store, room, 'codex', 'Built it.', run_a)
    agent(store, room, 'claude', 'Other result.', run_other)
    store.post_step(room, 'returned', 'worker:1 done: Built it.', event_key='a3', parent_request_id='job-a',
                    run_id=run_a)
    claude = agent(store, room, 'claude', 'Single run reply.', run_b)
    return room, codex, claude


def test_job_thread_has_only_that_jobs_lines_in_order(store, client):
    room, _, _ = two_jobs(store)
    listed = client.get(f'/api/team/rooms/{room}/threads').json()['threads']
    assert {t['id'] for t in listed} >= {'job:job-a', 'job:job-b'}
    lines = client.get('/api/team/threads/job:job-a').json()['lines']
    assert [line['text'] for line in lines] == [
        'Claude planned 1 package', 'Codex handoff (worker:1)', 'Built it.', 'worker:1 done: Built it.']
    assert [line['created_at'] for line in lines] == sorted(line['created_at'] for line in lines)
    assert client.get('/api/team/threads/job:missing').status_code == 404


def test_room_without_team_work_has_no_threads(store, client):
    room = store.create_room('Quiet')['id']
    response = client.get(f'/api/team/rooms/{room}/threads')
    assert response.status_code == 200 and response.json() == {'threads': []}
    assert client.get('/api/team/notifications').json() == {'unread': 0, 'items': []}
    assert not (store.root / 'threads.sqlite3').exists()  # reads never create the store
    assert client.get('/api/team/rooms/nope/threads').status_code == 404


def test_double_reply_stores_one_message_and_lists_the_thread(store, client):
    room, codex, _ = two_jobs(store)
    body = {'text': 'Why this file?', 'request_id': 'reply-1'}
    first = client.post(f'/api/team/rooms/{room}/messages/{codex}/reply', json=body)
    second = client.post(f'/api/team/rooms/{room}/messages/{codex}/reply', json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and first.json()['message']['state'] == 'waiting'
    thread_id = first.json()['thread_id']
    view = client.get(f'/api/team/threads/{thread_id}').json()
    assert len(view['lines']) == 1 and view['thread']['agent'] == 'codex' and view['anchor']['id'] == codex
    assert client.post(f'/api/team/rooms/{room}/messages/{codex}/reply',
                       json={'text': 'Other', 'request_id': 'reply-1'}).status_code == 409
    again = client.post(f'/api/team/rooms/{room}/messages/{codex}/reply', json={'text': 'And?', 'request_id': 'r2'})
    assert again.json()['thread_id'] == thread_id  # same anchor continues the thread
    assert thread_id in {t['id'] for t in client.get(f'/api/team/rooms/{room}/threads').json()['threads']}
    assert oct((store.root / 'threads.sqlite3').stat().st_mode & 0o777) == '0o600'
    stopped = client.post(f'/api/team/threads/{thread_id}/stop', json={})
    assert stopped.status_code == 200
    states = {line['state'] for line in client.get(f'/api/team/threads/{thread_id}').json()['lines']}
    assert states == {'stopped'}


def test_owner_threads_on_own_message_go_to_the_planner_and_steps_and_local_rooms_refuse(store, client):
    room = store.create_room('Local', local_only=True)['id']
    assert client.post(f'/api/team/rooms/{room}/messages/x/reply',
                       json={'text': 'hi', 'request_id': 'r1'}).status_code == 403
    shared, _, _ = two_jobs(store)
    user = uuid.uuid4().hex
    with store.admission() as data:
        data['rooms'][shared]['messages'].append(dict(id=user, author='user', text='Build the bell', created_at=time.time()))
    opened = client.post(f'/api/team/rooms/{shared}/messages/{user}/reply', json={'text': 'Status?', 'request_id': 'r1'})
    assert opened.status_code == 200 and opened.json()['message']['responder'] == 'claude'
    step = next(m['id'] for m in store.get_room(shared)['messages'] if m.get('kind') == 'step')
    assert client.post(f'/api/team/rooms/{shared}/messages/{step}/reply',
                       json={'text': 'hi', 'request_id': 'r2'}).status_code == 409


def test_a_mention_picks_the_responder_and_members_without_a_reply_mode_are_refused(store, client):
    room, codex, _ = two_jobs(store)
    url = f'/api/team/rooms/{room}/messages/{codex}/reply'
    plain = client.post(url, json={'text': 'Why this file?', 'request_id': 'm1'}).json()
    asked = client.post(url, json={'text': '@Claude is that the plan? cc@codex', 'request_id': 'm2'}).json()
    assert (plain['message']['responder'], asked['message']['responder']) == ('codex', 'claude')
    assert {row['request_id']: row['agent'] for row in Store(store.root).replies('waiting')} == {'m1': 'codex', 'm2': 'claude'}
    refused = client.post(url, json={'text': '@cursor can you look?', 'request_id': 'm3'})
    assert refused.status_code == 409  # the route keeps error text generic; the page names the reason itself
    with pytest.raises(ValueError, match='Cursor cannot answer in threads yet'):
        Store(store.root).reply(store.get_room(room), codex, '@cursor can you look?', 'm3')
    listed = next(t for t in client.get(f'/api/team/rooms/{room}/threads').json()['threads'] if t['kind'] == 'reply')
    assert (listed['anchor_message_id'], listed['count']) == (codex, 2)


def test_thread_lines_carry_the_role_and_thread_runs_are_named(store):
    room, codex, _ = two_jobs(store)
    threads = Store(store.root)
    opened = threads.reply(store.get_room(room), codex, 'Why?', 'q1')
    with threads.transaction() as db:
        db.execute("INSERT INTO thread_messages (id, thread_id, author, text, created_at, state) "
                   "VALUES ('a1', ?, 'codex', 'Because.', ?, 'answered')", (opened['thread_id'], time.time() + 1))
    lines = threads.thread(opened['thread_id'], [store.get_room(room)])['lines']
    assert [(line['author'], line['role']) for line in lines] == [('user', 'CEO'), ('codex', 'builder')]
    run_id = uuid.uuid4().hex
    with store.admission() as data:
        data['rooms'][room]['messages'].append(dict(id=uuid.uuid4().hex, author='user', text='x', created_at=time.time(),
                                                    request_id='reply.abc', run_id=run_id))
        answer = dict(id=uuid.uuid4().hex, author='claude', text='y', created_at=time.time(), run_id=run_id, kind='result')
        data['rooms'][room]['messages'].append(answer)
    assert thread_run_ids(store.get_room(room)) == {run_id}
    with pytest.raises(ValueError, match='thread this message belongs to'):
        threads.reply(store.get_room(room), answer['id'], 'More?', 'q2')


def test_an_older_store_gains_the_responder_column(store):
    store.root.mkdir(parents=True)
    db = sqlite3.connect(store.root / 'threads.sqlite3')
    db.execute('CREATE TABLE thread_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, author TEXT NOT NULL, '
               'text TEXT NOT NULL, created_at REAL NOT NULL, state TEXT, run_id TEXT, request_id TEXT UNIQUE, '
               'label_tier TEXT, label_classes TEXT)')
    db.commit()
    db.close()
    room, codex, _ = two_jobs(store)
    assert Store(store.root).reply(store.get_room(room), codex, '@claude why?', 'o1')['message']['responder'] == 'claude'


def test_bell_count_drops_when_read_and_stays_down_after_reload(store, client):
    threads = Store(store.root)
    first = threads.notify('reply:abc', 'reply')
    threads.notify('reply:def', 'needs_owner')
    assert client.get('/api/team/notifications').json()['unread'] == 2
    assert client.post('/api/team/notifications/read', json={'ids': [first]}).json()['unread'] == 1
    assert Store(store.root).notifications()['unread'] == 1  # a fresh reader, as after a page reload
    assert client.post('/api/team/notifications/read', json={'all': True}).json()['unread'] == 0
    items = client.get('/api/team/notifications').json()['items']
    assert len(items) == 2 and all(item['read_at'] for item in items)


def test_symlinked_store_is_refused(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / 'threads.sqlite3').symlink_to(store.root / 'elsewhere')
    with pytest.raises(ValueError):
        Store(store.root)


def test_an_older_store_is_readable_before_any_new_write(store):
    store.root.mkdir(parents=True)
    db = sqlite3.connect(store.root / 'threads.sqlite3')
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, room_id TEXT NOT NULL, kind TEXT NOT NULL, agent TEXT, "
               "anchor_message_id TEXT, created_at REAL NOT NULL, closed_at REAL)")
    db.execute('CREATE TABLE thread_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, author TEXT NOT NULL, '
               'text TEXT NOT NULL, created_at REAL NOT NULL, state TEXT, run_id TEXT, request_id TEXT UNIQUE, '
               'label_tier TEXT, label_classes TEXT)')
    db.execute("INSERT INTO threads VALUES ('reply:old', 'room', 'reply', 'codex', 'm', 1.0, NULL)")
    db.execute("INSERT INTO thread_messages VALUES ('x', 'reply:old', 'user', 'Old reply', 1.0, 'waiting', NULL, "
               "'old-1', NULL, NULL)")
    db.commit()
    db.close()
    threads = Store(store.root)
    assert [(row['request_id'], row['agent']) for row in threads.replies('waiting')] == [('old-1', 'codex')]
    assert threads.thread('reply:old', [])['lines'][0]['responder'] is None


def test_thread_lines_keep_details_ids_for_chi_tiet(store):
    """V3 fix: job-thread lines keep the room message's details_id; member reply lines point at their own log entry
    (team_replies logs a reply's full text under the line id); the owner's own lines have none."""
    room, codex, _ = two_jobs(store)
    value = store.get_room(room)  # the store gains details_id with V1; the thread view must carry it through
    next(m for m in value['messages'] if m['id'] == codex)['details_id'] = codex
    lines = job_threads(value)['job:job-a']
    assert [line.get('details_id') for line in lines if line['text'] == 'Built it.'] == [codex]
    assert all('details_id' not in line for line in lines if line['text'] != 'Built it.')
    threads = Store(store.root)
    opened = threads.reply(store.get_room(room), codex, 'Why?', 'q1')
    with threads.transaction() as db:
        db.execute("INSERT INTO thread_messages (id, thread_id, author, text, created_at, state) "
                   "VALUES ('a1', ?, 'codex', 'Because.', ?, 'answered')", (opened['thread_id'], time.time() + 1))
    view = threads.thread(opened['thread_id'], [value])
    assert [(line['author'], line.get('details_id')) for line in view['lines']] == [('user', None), ('codex', 'a1')]
    assert view['anchor']['details_id'] == codex and view['thread']['room_id'] == room


def test_old_run_id_opens_the_job_thread_the_server_filed_it_under(tmp_path):
    """2026-10-11: the page asks for run:<id>, but a run record carrying its job id files the lines under job:."""
    line = dict(id='m1', author='claude', kind='result', text='Old result.', created_at=1.0, run_id='run-old')
    room = dict(room=dict(id='r1', local_only=False), runs=[dict(id='run-old', parent_request_id='job-old')], messages=[line])
    assert 'job:job-old' in job_threads(room)
    got = Store(tmp_path).thread('run:run-old', [room])
    assert got['thread']['id'] == 'job:job-old' and [x['text'] for x in got['lines']] == ['Old result.']
    with pytest.raises(LookupError):
        Store(tmp_path).thread('run:nope', [room])


def test_missing_thread_says_so_plainly_not_chat_not_found(client):
    body = client.get('/api/team/threads/job:nope')
    assert body.status_code == 404 and 'archived' in body.text
