"""P3b builder tests for Team reply delivery (a private design note, 2026-09-30).

Acceptance: a reply waits while its agent is busy and goes out only after; it is delivered as an ordinary reply run
through the transport (the visible chat in the app); the short answer lands in the same thread and rings the bell;
a reply with family or health details is not sent and says why; Stop on a waiting reply means it never reaches the
agent. Real TeamStore and TeamService over a temp root with the recording fake transport; no native chat is used.
"""
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import team_chat, team_http, team_replies
from companion.input_labels import InputLabeller
from companion.team_threads import Store
from tests.test_team_chat_acceptance import bound, respond
from tests.test_team_native_modes import RevisionTransport

LABELS = InputLabeller(third_party_names=lambda: ['Example Person'])
ANSWER = 'Yes, the queue drains first.\n```mermaid\nflowchart LR\n  A[reply] --> B[Codex]\n```'


@pytest.fixture
def world(tmp_path):
    transport = RevisionTransport()
    store, service = bound(team_chat, tmp_path, transport)
    return store, service, transport, Store(store.root)


def agent_message(store, room_id, author, text):
    ident = uuid.uuid4().hex
    with store.admission() as data:
        data['rooms'][room_id]['messages'].append(
            dict(id=ident, author=author, text=text, created_at=time.time(), run_id=None, kind='result'))
    return ident


def reply(store, threads, author='codex', text='Does the pump wait for you?', request_id='r-1'):
    room_id = store.create_room('Reply room')['id']
    anchor = agent_message(store, room_id, author, 'The pump runs from the wake loop.')
    return room_id, threads.reply(store.get_room(room_id), anchor, text, request_id)


def lines(threads, thread_id):
    return threads.thread(thread_id, [])['lines']


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    parked = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        parked.wait(0.001)
    return predicate()


def test_a_reply_waits_for_busy_codex_then_arrives_in_its_chat_and_the_answer_rings_the_bell(world):
    store, service, transport, threads = world
    other = store.create_room('Other room')['id']
    busy = store.submit(other, 'Explain the lease.', 'busy-1', execution_mode='reply_codex')['run_id']
    service.step(busy)
    room_id, opened = reply(store, threads)

    assert team_replies.pump(service, threads, labeller=LABELS) == {'waiting': 1}
    assert [line['state'] for line in lines(threads, opened['thread_id'])] == ['waiting']
    assert store.get_room(room_id)['runs'] == []

    respond(transport, 'reply_codex', run_id=busy, summary='Done.')
    service.step(busy)
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    service.step(run_id)
    send = transport.sends[-1]
    assert (send['role'], send['run_id']) == ('codex', run_id)
    assignment = send['assignment_path'].read_text()
    assert 'Does the pump wait for you?' in assignment and 'The pump runs from the wake loop.' in assignment
    assert '2 lines, 240 characters' in assignment and 'summary holds' in assignment

    respond(transport, 'reply_codex', run_id=run_id, summary=ANSWER)
    service.step(run_id)
    team_replies.pump(service, threads, labeller=LABELS)
    team_replies.pump(service, threads, labeller=LABELS)  # a second pass adds nothing
    thread = lines(threads, opened['thread_id'])
    assert [(line['author'], line['state']) for line in thread] == [('user', 'answered'), ('codex', 'answered')]
    assert thread[1]['text'] == 'Yes, the queue drains first.…'
    from companion import team_log
    assert team_log.read(store.root, room_id, thread[1]['id'])['details'] == ANSWER
    bell = threads.notifications()
    assert bell['unread'] == 1 and bell['items'][0]['reason'] == 'reply'
    assert bell['items'][0]['thread_id'] == opened['thread_id']


@pytest.mark.parametrize('text,why', [('Can you ask my mom about dinner?', 'third_party'),
                                      ('I could not sleep, the headache is back.', 'health'),
                                      ('Ask Example Person about it.', 'third_party')])
def test_a_private_reply_is_never_sent_and_says_why(world, text, why):
    store, service, transport, threads = world
    room_id, opened = reply(store, threads, author='claude', text=text)
    assert team_replies.pump(service, threads, labeller=LABELS) == {'refused': 1}
    assert store.get_room(room_id)['runs'] == [] and transport.sends == []
    thread = lines(threads, opened['thread_id'])
    assert [(line['author'], line['state']) for line in thread] == [('user', 'failed'), ('kyra', 'failed')]
    assert why in thread[1]['text'] and 'not sent' in thread[1]['text']
    assert threads.notifications()['items'][0]['reason'] == 'needs_owner'


def test_stop_on_a_waiting_reply_means_it_never_reaches_the_agent(world):
    store, service, transport, threads = world
    room_id, opened = reply(store, threads, author='claude', text='Short answer please.')
    team_replies.stop(service, threads, opened['thread_id'])
    assert team_replies.pump(service, threads, labeller=LABELS) == {}
    assert store.get_room(room_id)['runs'] == [] and transport.sends == []
    assert lines(threads, opened['thread_id'])[0]['state'] == 'stopped'


def test_stop_after_the_claim_stops_the_run_before_it_is_sent(world):
    store, service, transport, threads = world
    room_id, opened = reply(store, threads, author='claude', text='Short answer please.')
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    team_replies.stop(service, threads, opened['thread_id'])
    service.step(run_id)
    assert transport.sends == []
    assert store.get_room(room_id)['runs'][0]['status'] == 'stopped'
    team_replies.pump(service, threads, labeller=LABELS)
    assert [line['state'] for line in lines(threads, opened['thread_id'])] == ['stopped']


def test_the_wake_loop_delivers_a_waiting_reply_and_collects_the_answer(world, monkeypatch):
    store, service, transport, threads = world
    room_id, opened = reply(store, threads, author='claude', text='Short answer please.')
    sent = threading.Event()
    deliver = transport.send

    def send(**kwargs):
        result = deliver(**kwargs)
        sent.set()
        return result

    transport.send = send
    team_http.wake_service(service)
    key = str(store.root.resolve())
    assert sent.wait(timeout=10)
    respond(transport, 'reply_claude', summary='Short answer.')
    team_http.wake_service(service)
    assert wait_until(lambda: key not in team_http._workers)
    assert key not in team_http._workers
    thread = lines(threads, opened['thread_id'])
    assert [(line['author'], line['state'], line['text']) for line in thread][1] == ('claude', 'answered', 'Short answer.')
    assert threads.notifications()['unread'] == 1


def test_the_reply_route_wakes_the_worker_and_stop_goes_through_team_replies(world, monkeypatch):
    store, service, transport, threads = world
    wakes = []
    monkeypatch.setattr(team_http, 'wake_service', wakes.append)
    monkeypatch.setattr(team_http, 'get_settings', lambda: SimpleNamespace(inline_worker=True))
    app = FastAPI()
    app.include_router(team_http.router)
    app.dependency_overrides[team_http.get_store] = lambda: store
    app.dependency_overrides[team_http.get_service] = lambda: service
    client = TestClient(app)
    room_id = store.create_room('Route room')['id']
    anchor = agent_message(store, room_id, 'claude', 'Ready.')
    opened = client.post(f'/api/team/rooms/{room_id}/messages/{anchor}/reply',
                         json={'text': 'Go ahead.', 'request_id': 'route-1'}).json()
    assert wakes == [service]
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    assert client.post(f'/api/team/threads/{opened["thread_id"]}/stop', json={}).status_code == 200
    service.step(run_id)
    assert transport.sends == [] and store.get_room(room_id)['runs'][0]['status'] == 'stopped'


def test_the_prompt_carries_handbook_role_project_parent_and_the_thread_but_never_a_held_reply(world, monkeypatch, tmp_path):
    store, service, transport, threads = world
    book = tmp_path / 'handbook.md'
    book.write_text('# Kyra Team handbook\nSlack, not email.')
    monkeypatch.setattr(team_replies, 'HANDBOOK', book)
    room_id = store.create_room('Bell redesign')['id']
    anchor = agent_message(store, room_id, 'codex', 'Built the bell list.')
    room = store.get_room(room_id)
    first = threads.reply(room, anchor, 'Which file?', 't-1')
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    run_id = lines(threads, first['thread_id'])[0]['run_id']
    service.step(run_id)
    respond(transport, 'reply_codex', run_id=run_id, summary='web/team-threads.js.')
    service.step(run_id)
    team_replies.pump(service, threads, labeller=LABELS)
    threads.reply(room, anchor, 'Tell my mom it shipped.', 't-2')  # held as private, must not ride along
    team_replies.pump(service, threads, labeller=LABELS)
    threads.reply(store.get_room(room_id), anchor, '@claude does this match the plan?', 't-3')
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    row = lines(threads, first['thread_id'])[-1]
    prompt = next(m['text'] for m in store.get_room(room_id)['messages'] if m.get('request_id') == 'reply.' + row['id'])
    assert 'Slack, not email.' in prompt and "You are Claude, the team's planner." in prompt
    assert 'Project: Bell redesign' in prompt and 'Codex (builder): Built the bell list.' in prompt
    assert 'the owner (CEO): Which file?' in prompt and 'Codex (builder): web/team-threads.js.' in prompt
    assert 'mom' not in prompt and 'at most 2 lines, 240 characters' in prompt and 'only the planner turns it into work' in prompt
    run = next(r for r in store.get_room(room_id)['runs'] if r['id'] == row['run_id'])
    assert run['execution_mode'] == 'reply_claude'


def test_a_thread_never_starts_a_build_and_a_missing_handbook_is_named(world, monkeypatch, tmp_path):
    store, service, transport, threads = world
    monkeypatch.setattr(team_replies, 'HANDBOOK', tmp_path / 'absent.md')
    room_id, opened = reply(store, threads, author='claude', text='Can you build it now?')
    with threads.transaction() as db:  # a responder without a reply mode, as if written by an older page
        db.execute("UPDATE thread_messages SET responder = 'cursor'")
    assert team_replies.pump(service, threads, labeller=LABELS) == {'failed': 1}
    assert store.get_room(room_id)['runs'] == [] and transport.sends == []
    assert 'Cursor cannot answer in threads' in lines(threads, opened['thread_id'])[1]['text']
    assert 'docs/team-handbook.md is missing' in team_replies.handbook()
    modes = {mode for mode in (r.get('execution_mode') for room in store.list_rooms()
                               for r in store.get_room(room['id'])['runs'])}
    assert modes <= {'reply_claude', 'reply_codex'}


def test_a_restricted_parent_or_history_holds_a_harmless_reply(world):
    store, service, transport, threads = world
    room_id = store.create_room('Plain project')['id']
    parent = agent_message(store, room_id, 'user', 'My mom has a headache again.')  # fictional, restricted
    opened = threads.reply(store.get_room(room_id), parent, 'Can you clarify?', 'p-1')
    assert team_replies.pump(service, threads, labeller=LABELS) == {'refused': 1}
    assert store.get_room(room_id)['runs'] == [] and transport.sends == []
    held = lines(threads, opened['thread_id'])
    assert [(line['author'], line['state']) for line in held] == [('user', 'failed'), ('kyra', 'failed')]
    assert 'health' in held[1]['text'] and 'third_party' in held[1]['text'] and 'not sent' in held[1]['text']

    room_id = store.create_room('History project')['id']
    parent = agent_message(store, room_id, 'codex', 'Built it.')
    opened = threads.reply(store.get_room(room_id), parent, 'Why?', 'p-2')
    with threads.transaction() as db:  # an earlier answer that carries restricted content
        db.execute("INSERT INTO thread_messages (id, thread_id, author, text, created_at, state) VALUES "
                   "('h1', ?, 'codex', 'Ask Example Person about the sleep problem.', 0, 'answered')", (opened['thread_id'],))
    assert team_replies.pump(service, threads, labeller=LABELS) == {'refused': 1}
    assert transport.sends == []


def test_the_whole_eligible_thread_rides_along_including_answers_after_the_reply_was_queued(world):
    store, service, transport, threads = world
    room_id = store.create_room('Long thread')['id']
    parent = agent_message(store, room_id, 'codex', 'Built it.')
    opened = threads.reply(store.get_room(room_id), parent, 'Next step?', 'l-1')
    later = time.time() + 60
    with threads.transaction() as db:
        for i, marker in enumerate(('FIRST-REQUIREMENT', 'SECOND-REQUIREMENT')):
            db.execute("INSERT INTO thread_messages (id, thread_id, author, text, created_at, state) VALUES "
                       "(?, ?, 'codex', ?, ?, 'answered')", (f'l{i}', opened['thread_id'], marker + ' ' + 'x' * 3000, later + i))
    assert team_replies.pump(service, threads, labeller=LABELS) == {'sent': 1}
    row = lines(threads, opened['thread_id'])[0]
    prompt = next(m['text'] for m in store.get_room(room_id)['messages'] if m.get('request_id') == 'reply.' + row['id'])
    assert 'FIRST-REQUIREMENT' in prompt and 'SECOND-REQUIREMENT' in prompt and 'omitted' not in prompt


def test_only_a_thread_past_the_assignment_limit_drops_its_oldest_lines_and_says_so():
    earlier = [dict(author='codex', text=f'LINE-{i} ' + 'x' * 3900) for i in range(6)]
    prompt = team_replies.outgoing('codex', 'P', dict(author='codex', text='Parent'), earlier, 'Why?')
    assert len(prompt) <= team_chat.MAX_TEXT and 'LINE-5' in prompt and 'LINE-0' not in prompt
    assert 'earlier thread lines omitted to fit the assignment limit' in prompt
