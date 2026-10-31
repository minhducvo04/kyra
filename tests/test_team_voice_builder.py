"""V1 chat/log contract over real stores and correlated native/app envelopes."""
import importlib
import json
from pathlib import Path

import pytest

from companion import team_chat, team_connected, team_replies
from companion.team_threads import Store
from tests.test_team_chat_acceptance import bound, respond
from tests.test_team_native_modes import RevisionTransport
from tests.test_team_replies import LABELS, lines, reply

LONG = ('Built the bounded change. ' + 'More verification details. ' * 95)[:2399] + '.'


def log():
    return importlib.import_module('companion.team_log')


@pytest.fixture
def world(tmp_path):
    transport = RevisionTransport()
    store, service = bound(team_chat, tmp_path, transport)
    return store, service, transport, store.create_room('Voice fixture')['id']


@pytest.mark.parametrize('chat', [None, 'Built and checked.\nReady for review.', '', 42, 'x' * 241, 'a\nb\nc', '\ud800'])
def test_native_result_keeps_full_summary_and_uses_only_valid_chat(world, chat):
    store, service, transport, room_id = world
    run_id = store.submit(room_id, 'Check the change.', 'voice', execution_mode='reply_codex')['run_id']
    service.step(run_id)
    respond(transport, 'reply_codex', summary=LONG, **({'chat': chat} if chat is not None else {}))
    service.step(run_id)
    room = store.get_room(room_id)
    assert room['runs'][0]['status'] == 'completed'
    message = room['messages'][-1]
    assert len(message['text']) <= 240
    assert len(message['text'].splitlines()) <= 2
    if isinstance(chat, str) and chat.startswith('Built'):
        assert message['text'] == chat
    assert log().read(store.root, room_id, message['details_id'])['details'] == LONG
    assert message['details_id'] == message['id']


def test_interview_keeps_its_larger_chat_budget(world):
    store, service, transport, room_id = world
    run_id = store.submit(room_id, 'Review context.', 'voice', execution_mode='interview_only')['run_id']
    service.step(run_id)
    text = 'Useful context ' * 30
    respond(transport, 'plan', summary=text, chat=text, ready=False)
    service.step(run_id)
    assert store.get_room(room_id)['messages'][-1]['text'] == text
    assert len(text) <= 1200


def test_review_result_uses_reply_budget_and_logs_full_summary(world):
    store, service, transport, room_id = world
    run_id = store.submit(room_id, 'Review context.', 'voice', execution_mode='review_only')['run_id']
    service.step(run_id)
    text = 'Useful review context ' * 20
    respond(transport, 'review', summary=text, chat=text, verdict='pass')
    service.step(run_id)
    message = store.get_room(room_id)['messages'][-1]
    assert len(message['text']) <= 240 and len(message['text'].splitlines()) <= 2
    assert log().read(store.root, room_id, message['details_id'])['details'] == text


@pytest.mark.parametrize('mode,extra', [
    ('reply_codex', {'status': 'needs_input'}),
    ('interview_only', {'status': 'needs_input'}),
    ('review_only', {'verdict': 'needs_input'}),
    ('review_only', {'verdict': 'changes_requested'}),
])
def test_long_result_reason_survives_coordinator_child_status(world, mode, extra):
    from types import SimpleNamespace

    from companion import team_http
    from companion.team_delegation import _child

    store, service, transport, room_id = world
    run_id = store.submit(room_id, 'Check the change.', 'voice', execution_mode=mode,
                          parent_request_id='parent')['run_id']
    service.step(run_id)
    chat = 'Please inspect the remaining details. ' * (12 if mode == 'interview_only' else 1)
    respond(transport, team_chat.SINGLE_STAGES[mode], summary=LONG, chat=chat, **extra)
    service.step(run_id)
    coordinator = team_http.get_delegation(store, SimpleNamespace(), service)
    child = _child(coordinator.child_status(room_id, 'reviewer', run_id), 'voice')
    assert child['state'] == 'blocked'  # No explicit owner decision was supplied.
    assert 0 < len(child['reason']) <= 240
    assert len(child['reason'].splitlines()) <= 2
    assert child['reason'].startswith('Built the bounded change.')
    message = store.get_room(room_id)['messages'][-1]
    assert message['text'] == chat
    assert log().read(store.root, room_id, message['details_id'])['details'] == LONG


def test_system_assignment_is_hidden_and_logged_without_losing_dispatch_context(world):
    store, service, transport, room_id = world
    run_id = store.submit(room_id, LONG, 'assignment', author='system', execution_mode='reply_codex')['run_id']
    message = store.get_room(room_id)['messages'][0]
    assert message['hidden'] is True
    assert log().read(store.root, room_id, message['details_id'])['details'] == LONG
    service.step(run_id)
    assert LONG in transport.sends[-1]['assignment_path'].read_text()
    # Old system messages still load, without a migration changing their context.
    with store.admission() as data:
        del data['rooms'][room_id]['messages'][0]['hidden']
    assert store.get_room(room_id)['messages'][0]['author'] == 'system'


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_connected_result_envelope_has_chat_but_log_keeps_summary(tmp_path, monkeypatch):
    from companion.team_cursor import AdapterResult
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='voice', text=LONG,
                         config_hash='pin', message_author='system')
    monkeypatch.setattr(team_connected, '_config', lambda _: ({'workspace': str(tmp_path), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: ({}, None))

    class Adapter:
        def run(self, request, **kwargs):
            body = dict(run_id=row['id'], stage='reply_cursor', attempt=0, status='completed',
                        chat='Checked the change.', summary=LONG)
            return AdapterResult('cursor', row['id'], 'completed', json.dumps(body), 'fixture', 'fixture')

    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    assert store.get(row['id'])['state'] == 'completed'
    view = team_connected.project_room({'id': row['room_id'], 'messages': []}, tmp_path)
    assert not any(m['author'] == 'system' for m in view['messages'])
    message = view['messages'][-1]
    assert message['text'] == 'Checked the change.'
    assert log().read(tmp_path, row['room_id'], message['details_id'])['details'] == LONG
    assert log().read(tmp_path, row['room_id'], row['id'] + '-request')['details'] == LONG


@pytest.mark.parametrize('chat', ['Checked the reply.', '  Checked the reply.  '])
def test_thread_reply_logs_full_details_under_its_message_id(world, chat):
    store, service, transport, _ = world
    threads = Store(store.root)
    room_id, opened = reply(store, threads)
    team_replies.pump(service, threads, labeller=LABELS)
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    service.step(run_id)
    respond(transport, 'reply_codex', summary=LONG, chat=chat)
    service.step(run_id)
    team_replies.pump(service, threads, labeller=LABELS)
    message = lines(threads, opened['thread_id'])[-1]
    assert message['text'] == chat
    assert log().read(store.root, room_id, message['id'])['details'] == LONG


def test_log_reads_newest_and_tolerates_bad_lines_and_read_only_directory(tmp_path, caplog):
    module = log()
    root = tmp_path / 'readonly'
    root.mkdir()
    root.chmod(0o500)
    try:
        module.append(root, 'a' * 32, message_id='b' * 32, author='codex', kind='result', details=LONG, created_at=1)
        assert module.read(root, 'a' * 32, 'b' * 32) is None
        assert 'warning' in caplog.text.lower()
    finally:
        root.chmod(0o700)
    for text in ('first', LONG):
        module.append(tmp_path, 'a' * 32, message_id='b' * 32, author='codex', kind='result', details=text, created_at=1)
    path = tmp_path / 'logs' / ('a' * 32 + '.jsonl')
    with path.open('a') as stream:
        stream.write('bad partial line\n[]\n')
    assert module.read(tmp_path, 'a' * 32, 'b' * 32)['details'] == LONG
    assert module.read(tmp_path, '../escape', 'b' * 32) is None
    module.append(tmp_path, '../escape', message_id='b' * 32, author='codex', kind='result', details=LONG, created_at=1)
    assert not (tmp_path / 'escape.jsonl').exists()


def test_chat_line_limits_first_sentence_and_unicode_clusters():
    voice = importlib.import_module('companion.team_voice')
    assert voice.LIMITS == {'reply': (240, 2), 'interview': (1200, 12), 'report': (600, 6)}
    assert voice.chat_line('Short. Two sentences.') == 'Short. Two sentences.'
    assert voice.chat_line(LONG) == 'Built the bounded change.…'
    assert voice.chat_line('First line\nSecond\nThird') == 'First line…'
    assert voice.chat_line('one two three four', limit=12) == 'one two…'
    for text in ('a\u0306\u0301' * 100, '🦉' * 100, '\ud83e\udd89' * 100):
        shortened = voice.chat_line(text, limit=11)
        assert len(shortened) <= 11
        shortened.encode('utf-8')
        assert shortened.endswith('…')
        if text.startswith('a'):
            assert shortened[:-1] == 'a\u0306\u0301' * 3


def test_log_io_errors_do_not_escape(tmp_path, monkeypatch):
    module = log()
    def deny(*a, **kw):
        raise PermissionError('synthetic private path')
    monkeypatch.setattr(Path, 'open', deny)
    module.append(tmp_path, 'a' * 32, message_id='b' * 32, author='codex', kind='result', details=LONG, created_at=1)
    assert module.read(tmp_path, 'a' * 32, 'b' * 32) is None


def test_followup_privacy_check_includes_details_hidden_by_short_chat(world):
    store, service, transport, _ = world
    threads = Store(store.root)
    room_id, opened = reply(store, threads)
    team_replies.pump(service, threads, labeller=LABELS)
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    service.step(run_id)
    respond(transport, 'reply_codex', summary='Example Person needs a follow-up.', chat='Follow-up ready.')
    service.step(run_id)
    team_replies.pump(service, threads, labeller=LABELS)
    anchor = threads.thread(opened['thread_id'], [])['thread']['anchor_message_id']
    threads.reply(store.get_room(room_id), anchor, 'Please explain more.', 'followup')
    assert team_replies.pump(service, threads, labeller=LABELS) == {'refused': 1}
    assert len(transport.sends) == 1


def test_connected_stream_logs_once_at_finish_not_per_event_or_poll(tmp_path):
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='stream', text='Check it.', config_hash='pin')
    store.begin(row['id'])
    for _ in range(80):
        store.event(row['id'], {'type': 'text', 'text': 'stream delta '})
        view = team_connected.project_room({'id': row['room_id'], 'messages': []}, tmp_path)
    path = tmp_path / 'logs' / (row['room_id'] + '.jsonl')
    assert not path.exists()
    message = view['messages'][-1]
    assert len(message['text']) <= 240
    store.finish(row['id'], state='completed', text=LONG, chat='Checked.', reason='', session_id=None, surface='')
    for _ in range(3):
        team_connected.project_room({'id': row['room_id'], 'messages': []}, tmp_path)
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 1
    assert log().read(tmp_path, row['room_id'], message['details_id'])['details'] == LONG


@pytest.mark.parametrize('text', ['Family 👨\u200d👩\u200d👧 together.', 'Ready ✈\ufe0f.', 'Ready 👩\u200d💻.'])
def test_chat_preserves_joiners_and_variation_selectors(text):
    voice = importlib.import_module('companion.team_voice')
    assert voice.chat_line(text) == text
    assert voice.result_chat('Fallback summary.', text) == text


def test_chat_line_control_only_is_a_nonempty_placeholder():
    voice = importlib.import_module('companion.team_voice')
    assert 0 < len(voice.chat_line('\x00\x01\x7f\t\r\n')) <= 240


def test_missing_native_details_do_not_become_short_thread_details(world, monkeypatch):
    store, service, transport, _ = world
    threads = Store(store.root)
    room_id, opened = reply(store, threads)
    team_replies.pump(service, threads, labeller=LABELS)
    run_id = lines(threads, opened['thread_id'])[0]['run_id']
    service.step(run_id)
    respond(transport, 'reply_codex', summary=LONG, chat='Checked.')
    with monkeypatch.context() as patch:
        patch.setattr(log(), 'append', lambda *a, **kw: None)  # original log write failed
        service.step(run_id)
    original = store.get_room(room_id)['messages'][-1]
    assert original['details_id'] == original['id']
    assert log().read(store.root, room_id, original['details_id']) is None
    team_replies.pump(service, threads, labeller=LABELS)
    message = lines(threads, opened['thread_id'])[-1]
    assert message['text'] == 'Checked.'
    assert log().read(store.root, room_id, message['id']) is None


def test_connected_projection_retries_missing_log_with_original_summary(tmp_path, monkeypatch):
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='retry', text='Check it.', config_hash='pin')
    store.begin(row['id'])
    with monkeypatch.context() as patch:
        patch.setattr(log(), 'append', lambda *a, **kw: None)
        store.finish(row['id'], state='completed', text=LONG, chat='Checked.', reason='', session_id=None, surface='')
    assert log().read(tmp_path, row['room_id'], row['id']) is None
    view = team_connected.project_room({'id': row['room_id'], 'messages': []}, tmp_path)
    assert view['messages'][-1]['text'] == 'Checked.'
    assert log().read(tmp_path, row['room_id'], row['id'])['details'] == LONG


def test_log_concurrent_writers_keep_complete_entries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    module = log()
    def write(number):
        module.append(tmp_path, 'a' * 32, message_id=str(number), author='codex', kind='result',
                      details=LONG, created_at=number)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(24)))
    path = tmp_path / 'logs' / ('a' * 32 + '.jsonl')
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(entries) == 24
    assert {entry['message_id'] for entry in entries} == {str(i) for i in range(24)}
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_connected_reply_does_not_publish_streamed_envelope(tmp_path):
    store = team_connected.ConnectedStore(tmp_path)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='invalid', text='Check it.', config_hash='pin')
    store.begin(row['id'])
    store.event(row['id'], {'type': 'text', 'text': 'Unvalidated streamed envelope'})
    store.finish(row['id'], state='failed', text='', reason='invalid_app_result', session_id=None, surface='')
    message = team_connected.project_room({'id': row['room_id'], 'messages': []}, tmp_path)['messages'][-1]
    assert message['text'] == 'Cursor: failed (invalid_app_result).'


def test_local_step_assignment_is_also_hidden_and_logged(world):
    from tests.test_team_lead import PROJECT
    store, _, _, room_id = world
    store.queue_answer(room_id, LONG, 'local-step', binding=PROJECT, author='system')
    message = store.get_room(room_id)['messages'][0]
    assert message['hidden'] is True
    assert log().read(store.root, room_id, message['details_id'])['details'] == LONG
