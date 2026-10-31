"""Claude acceptance for the local team lead's store and service (2026-09-28): acceptance against baseline a6bf5d4.

Contract: a private design note (literal payloads, errors, model JSON, digests) with a private design note and the frozen
backend seams, signed in a private design note. A private question becomes one private user message plus one bounded
`kind="answer"` record; at most two wait and three are unfinished; Stop is immediate when queued and holds a running
answer active until its worker returns, discarding late output; the flight is held through terminal save; a crashed
running answer fails on the next flight and is never replayed; the worker is offered the exact digested capsule with
only this room's earlier turns and approved unchanged sources; a valid answer keeps only trusted citations of retained
sources, an advisory route and summed omissions; malformed output and adapter failures leave fixed codes and no text;
new native runs share only the selected text; each unfinished answer reserves completion room below the ordinary
ceiling. Fake adapter and scratch stores only: no model, cache, network or provider.
"""
import fcntl
import hashlib
import importlib
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.test_team_capacity_acceptance import pad_value
from tests.test_team_chat_acceptance import FakeTransport, authorize

SOURCE_TEXT = {'pref-ref': 'PREFERENCE: keep replies short.', 'map-ref': 'PROJECT MAP: the fictional recap is done.',
               'note-ref': 'NOTE: the fictional launch is on Friday.'}
CAPSULE_KEYS = {'request', 'sources', 'history', 'project', 'room', 'request_id', 'capsule_sha256'}
TERMINAL = {'answered', 'failed', 'stopped'}


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def digest(value):
    """The wire contract's cross-owner digest encoding."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                     allow_nan=False).encode('utf-8')).hexdigest()


PROJECT = {'project_id': 'kyra-fixture', 'project_sha256': 'b' * 64, 'model_id': 'fixture-model', 'sources': [
    {'id': 'pref', 'kind': 'preference', 'ref': 'pref-ref', 'sha256': sha(SOURCE_TEXT['pref-ref'])},
    {'id': 'map', 'kind': 'project', 'ref': 'map-ref', 'sha256': sha(SOURCE_TEXT['map-ref'])},
    {'id': 'note', 'kind': 'note', 'ref': 'note-ref', 'sha256': sha(SOURCE_TEXT['note-ref'])}]}


def lead():
    return importlib.import_module('companion.team_lead')  # the immutable red base has neither module yet


def inference():
    return importlib.import_module('companion.local_lead_inference')


def model_json(answer='The fictional launch is on Friday.', sources=('pref',), missing=False, route='answer'):
    return json.dumps({'answer': answer, 'sources': list(sources), 'missing': missing, 'route': route})


def worker_result(capsule, text=None, *, keep_sources=None, keep_history=None, model='fixture-model', **changes):
    """A correct adapter result for `capsule`: a retained source prefix, a retained recent-history suffix, exact
    omissions and the effective-input digest, as the wire contract defines them."""
    sources, history = capsule['sources'], capsule['history']
    kept_sources = sources if keep_sources is None else sources[:keep_sources]
    kept_history = history if keep_history is None else history[len(history) - keep_history:]
    body = {key: value for key, value in capsule.items() if key != 'capsule_sha256'}
    omitted = {'sources': len(sources) - len(kept_sources), 'history': len(history) - len(kept_history)}
    result = {'project': capsule['project'], 'room': capsule['room'], 'request_id': capsule['request_id'],
              'capsule_sha256': capsule['capsule_sha256'], 'attempt_id': 'e' * 32,
              'text': model_json() if text is None else text,
              'included_source_ids': [item['id'] for item in kept_sources],
              'included_history_ids': [item['id'] for item in kept_history],
              'omitted_counts': omitted, 'truncated': any(omitted.values()),
              'effective_input_sha256': digest({**body, 'sources': kept_sources, 'history': kept_history}),
              'model': {'id': model}, 'metrics': {'prompt_tokens': 120, 'generation_tokens': 24, 'finish_reason': 'stop'}}
    result.update(changes)
    return result


class Sources:
    """Trusted resolver double: returns configured text and refuses bytes that no longer match the approved digest."""

    def __init__(self):
        self.text, self.reads, self.probe = dict(SOURCE_TEXT), [], None

    def __call__(self, ref, expected_sha256):
        self.reads.append(self.probe() if self.probe else None)
        value = self.text[ref]
        if sha(value) != expected_sha256:
            raise ValueError('source changed since approval')
        return value


class FakeBackend:
    """Adapter double with the frozen public surface: availability() and a nonblocking flight() whose flight has
    generate(capsule, *, cancel_event). `respond(capsule)` builds the result; `error` is raised instead."""

    def __init__(self, respond=None, hold=False, error=None):
        self.busy, self.respond, self.error = False, respond or worker_result, error
        self.hold = threading.Event() if hold else None
        self.running, self.requests, self.cancelled, self.exits, self.probes = threading.Event(), [], [], [], []
        self.on_exit = self.probe = None

    def availability(self):
        return {'available': True, 'reason': ''}

    @contextmanager
    def flight(self):
        if self.busy:
            raise inference().LeadBusyError()
        try:
            yield FakeFlight(self)
        finally:
            if self.on_exit:
                self.exits.append(self.on_exit())


class FakeFlight:
    def __init__(self, backend):
        self.backend = backend

    def generate(self, capsule, *, cancel_event):
        backend = self.backend
        backend.requests.append(json.loads(json.dumps(capsule)))
        backend.probes.append(backend.probe() if backend.probe else None)
        backend.running.set()
        if backend.hold is not None:
            deadline = time.monotonic() + 5
            while not (backend.hold.is_set() or cancel_event.is_set()) and time.monotonic() < deadline:
                time.sleep(0.01)
        backend.cancelled.append(cancel_event.is_set())
        if backend.error is not None:
            raise backend.error
        return backend.respond(capsule)


@pytest.fixture
def home(tmp_path):
    team = importlib.import_module('companion.team_chat')
    controller = authorize(tmp_path)
    store = team.TeamStore(tmp_path / 'team', controller=controller)
    transport = FakeTransport()
    return SimpleNamespace(team=team, store=store, transport=transport, reader=Sources(), root=tmp_path,
                           service=team.TeamService(store, transport, controller=controller))


def service(home, backend, project=PROJECT, store=None):
    return lead().LeadService(store or home.store, backend, project=project, source_reader=home.reader)


def rooms(home, count):
    return [home.store.create_room(f'Fixture room {index}')['id'] for index in range(count)]


def texts(home, room, author='user'):
    return [message['text'] for message in home.store.get_room(room)['messages'] if message['author'] == author]


def answered(home, respond=None, *, question='What is the launch day?', setup=None, error=None):
    room, = rooms(home, 1)
    backend = FakeBackend(respond=respond, error=error)
    lead_service = service(home, backend)
    if setup:
        setup(lead_service, room)
    lead_service.submit(room, question, 'ask-1')
    lead_service.run_once()
    return room, backend, home.store.get_answer(room, 'ask-1')


def assert_failed_without_text(record, notice=None):
    assert record['status'] == 'failed' and not record.get('text') and not record.get('sources')
    assert record.get('route') is None
    if notice is not None:
        assert record['notice'] == notice


def test_a_private_question_queues_one_answer_beside_its_private_message(home):
    room, legacy = rooms(home, 2)
    home.store.submit(legacy, 'A storage-only note.', 'note-1', share_with_team=False)
    queued = service(home, FakeBackend()).submit(room, '  What did we decide?  ', 'ask-1')
    assert set(queued) == {'answer_id', 'reply_to', 'request_id', 'status'}
    assert (queued['request_id'], queued['status']) == ('ask-1', 'queued')
    question, answer = home.store.get_room(room)['messages']
    assert (question['author'], question['text'], question['shared']) == ('user', 'What did we decide?', False)
    assert (answer['author'], answer['kind'], answer['status'], answer['shared']) == ('kyra', 'answer', 'queued', False)
    assert answer['id'] == queued['answer_id'] and answer['reply_to'] == question['id'] == queued['reply_to']
    assert answer['omitted_counts'] == {'sources': 0, 'history': 0} and answer['route'] is None
    assert home.store.get_room(room)['runs'] == []  # a private question never starts native work
    assert home.store.get_answer(room, 'ask-1')['status'] == 'queued'
    assert [message.get('kind') for message in home.store.get_room(legacy)['messages']] == [None]


def test_one_record_per_room_and_request_even_under_a_new_snapshot(home):
    room, second = rooms(home, 2)
    first = service(home, FakeBackend()).submit(room, 'Question one?', 'ask-1')
    again = service(home, FakeBackend(), project={**PROJECT, 'project_sha256': 'c' * 64}).submit(
        room, 'Question one?', 'ask-1')
    assert again == first and len(home.store.get_room(room)['messages']) == 2
    assert home.store.get_answer(room, 'ask-1')['binding']['project_sha256'] == 'b' * 64  # never repinned
    home.store.submit(second, 'Add a fixture field and test it.', 'native-1')
    before = home.store.path.read_bytes()
    with pytest.raises(ValueError):
        service(home, FakeBackend()).submit(room, 'A different question?', 'ask-1')
    with pytest.raises(ValueError):
        service(home, FakeBackend()).submit(second, 'Question one?', 'native-1')  # a native submission's key
    with pytest.raises(ValueError):
        service(home, FakeBackend()).submit('f' * 32, 'Question one?', 'ask-9')  # no such room
    assert home.store.path.read_bytes() == before
    assert service(home, FakeBackend()).submit(second, 'Question one?', 'ask-1')['status'] == 'queued'


@pytest.mark.parametrize('change', [
    pytest.param(lambda b: b['sources'].extend(dict(b['sources'][0], id=f'x{i}') for i in range(6)), id='nine-sources'),
    pytest.param(lambda b: b['sources'][0].update(kind='memory'), id='unknown-kind'),
    pytest.param(lambda b: b['sources'][0].update(sha256='X' * 64), id='bad-digest'),
    pytest.param(lambda b: b['sources'][0].update(ref='r' * 257), id='long-ref'),
    pytest.param(lambda b: b['sources'][0].update(id='i' * 129), id='long-id'),
    pytest.param(lambda b: b.update(model_id='m' * 129), id='long-model')])
def test_invalid_trusted_bindings_are_refused_without_writing(home, change):
    room, = rooms(home, 1)
    binding = json.loads(json.dumps(PROJECT))
    change(binding)
    before = home.store.path.read_bytes()
    with pytest.raises(ValueError):
        home.store.queue_answer(room, 'Question?', 'ask-1', binding=binding)
    assert home.store.path.read_bytes() == before


def test_two_may_wait_and_three_may_be_unfinished_across_rooms(home):
    first, second, third, fourth = rooms(home, 4)
    backend = FakeBackend(hold=True)
    lead_service = service(home, backend)
    lead_service.submit(first, 'First?', 'a1')
    lead_service.submit(second, 'Second?', 'a2')
    assert lead_service.status() == {'available': True, 'active': 0, 'queued': 2, 'reason': ''}
    before = home.store.path.read_bytes()
    with pytest.raises(inference().LeadBusyError):  # none active: still only two may wait
        lead_service.submit(third, 'Third?', 'a3')
    assert home.store.path.read_bytes() == before
    worker = threading.Thread(target=lead_service.run_once)
    worker.start()
    try:
        assert backend.running.wait(5)
        lead_service.submit(third, 'Third?', 'a3')  # one running, one waiting: a second may wait
        assert {key: lead_service.status()[key] for key in ('active', 'queued')} == {'active': 1, 'queued': 2}
        with pytest.raises(inference().LeadBusyError):
            lead_service.submit(fourth, 'Fourth?', 'a4')
    finally:
        backend.hold.set()
        worker.join(10)
    with pytest.raises(inference().LeadBusyError):  # the settled job frees nothing while two still wait
        lead_service.submit(fourth, 'Fourth?', 'a4')


def test_status_is_metadata_only_and_never_creates_the_store(home):
    missing = home.team.TeamStore(home.root / 'absent-team', controller=home.store.controller)
    status = service(home, FakeBackend(), store=missing).status()
    assert set(status) == {'available', 'active', 'queued', 'reason'}
    assert (status['active'], status['queued']) == (0, 0)
    assert not (home.root / 'absent-team').exists()


def test_stopping_a_queued_answer_is_immediate_and_it_never_runs(home):
    room, = rooms(home, 1)
    backend = FakeBackend()
    lead_service = service(home, backend)
    lead_service.submit(room, 'Question?', 'a1')
    stopped = lead_service.stop(room, 'a1')
    assert stopped['status'] == 'stopped' and not stopped.get('text') and not stopped.get('sources')
    lead_service.run_once()
    assert backend.requests == [] and home.store.get_answer(room, 'a1')['status'] == 'stopped'
    assert texts(home, room) == ['Question?']


@pytest.mark.parametrize('late', ['valid answer', 'cancelled error'])
def test_a_running_stop_stays_active_until_the_worker_returns_and_its_output_is_discarded(home, late):
    room, = rooms(home, 1)
    backend = FakeBackend(respond=lambda capsule: worker_result(capsule, model_json(answer='LATE ANSWER TEXT')),
                          hold=True)
    if late == 'cancelled error':
        backend.error = inference().LeadInferenceError('cancelled')
    lead_service = service(home, backend)
    lead_service.submit(room, 'Question?', 'a1')
    worker = threading.Thread(target=lead_service.run_once)
    worker.start()
    try:
        assert backend.running.wait(5)
        during = lead_service.stop(room, 'a1')
        assert (during['status'], during['stop_requested']) == ('running', True)
    finally:
        worker.join(10)
    final = home.store.get_answer(room, 'a1')
    assert backend.cancelled == [True]  # the Stop reached the running worker
    assert final['status'] == 'stopped' and not final.get('sources') and final.get('route') is None
    assert 'LATE ANSWER TEXT' not in json.dumps(final)
    assert texts(home, room) == ['Question?']


def test_the_flight_is_held_until_the_terminal_state_is_saved(home):
    room, = rooms(home, 1)
    backend = FakeBackend()
    lead_service = service(home, backend)
    lead_service.submit(room, 'Question?', 'a1')
    backend.on_exit = lambda: home.store.get_answer(room, 'a1')['status']
    lead_service.run_once()
    assert len(backend.requests) == 1 and backend.exits == ['answered']


def test_a_busy_flight_leaves_the_queue_untouched(home):
    room, = rooms(home, 1)
    backend = FakeBackend()
    lead_service = service(home, backend)
    lead_service.submit(room, 'Question?', 'a1')
    before = home.store.path.read_bytes()
    backend.busy = True
    assert lead_service.run_once() is False
    assert backend.requests == [] and home.store.path.read_bytes() == before


def test_a_crashed_running_answer_fails_on_the_next_flight_and_is_never_replayed(home):
    room, = rooms(home, 1)
    service(home, FakeBackend()).submit(room, 'Question?', 'a1')
    with home.store.edit() as data:  # fixture: a process died while this answer was running
        answer = next(item for item in data['rooms'][room]['messages'] if item.get('kind') == 'answer')
        answer.update(status='running', attempt_id='d' * 32, started_at=time.time())
    orphan = FakeBackend()
    orphan.busy = True  # a surviving child still holds the flight: nothing may be recovered
    service(home, orphan).run_once()
    assert home.store.get_answer(room, 'a1')['status'] == 'running'
    backend = FakeBackend()
    service(home, backend).run_once()
    assert_failed_without_text(home.store.get_answer(room, 'a1'))
    assert backend.requests == []


def test_the_worker_is_offered_the_exact_capsule_of_this_rooms_turns_and_unchanged_sources(home):
    room, other = rooms(home, 2)
    backend = FakeBackend()
    lead_service = service(home, backend)
    for target, text, request in ((other, 'OTHER-ROOM-SECRET question?', 'o1'), (room, 'EARLIER-TURN-ONE?', 'r1'),
                                  (room, 'EARLIER-TURN-TWO?', 'r2')):
        lead_service.submit(target, text, request)
        lead_service.stop(target, request)
    lead_service.submit(room, 'CURRENT-QUESTION?', 'r3')
    lead_service.submit(room, 'LATER-TURN?', 'r4')  # added after the trigger
    home.reader.text['note-ref'] = 'NOTE: changed after approval.'
    lead_service.run_once()
    capsule = backend.requests[0]
    assert set(capsule) == CAPSULE_KEYS
    assert capsule['capsule_sha256'] == digest({key: value for key, value in capsule.items() if key != 'capsule_sha256'})
    assert (capsule['request'], capsule['room'], capsule['request_id'], capsule['project']) == (
        'CURRENT-QUESTION?', room, 'r3', 'kyra-fixture')
    assert [(item['id'], item['kind'], item['ref'], item['sha256'], item['text']) for item in capsule['sources']] == [
        (source['id'], source['kind'], source['ref'], source['sha256'], SOURCE_TEXT[source['ref']])
        for source in PROJECT['sources'][:2]]  # the changed note is excluded, not re-pinned
    assert all(set(item) == {'id', 'role', 'text'} and item['role'] in ('user', 'assistant')
               for item in capsule['history'])
    turns = [item['text'] for item in capsule['history']]
    assert turns.index('EARLIER-TURN-ONE?') < turns.index('EARLIER-TURN-TWO?')  # oldest first
    offered = json.dumps(capsule, ensure_ascii=False)
    for absent in ('OTHER-ROOM-SECRET', 'LATER-TURN', SOURCE_TEXT['note-ref'], 'changed after approval'):
        assert absent not in offered


def test_no_team_lock_is_held_while_sources_are_read_or_the_worker_runs(home):
    room, = rooms(home, 1)

    def team_lock_free():
        with open(home.store.root / '.lock', 'a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
            return True

    backend = FakeBackend()
    backend.probe = home.reader.probe = team_lock_free
    lead_service = service(home, backend)
    lead_service.submit(room, 'Question?', 'a1')
    lead_service.run_once()
    assert home.reader.reads and backend.probes and all(home.reader.reads + backend.probes)


def test_a_valid_answer_keeps_trusted_citations_and_an_advisory_route(home):
    room, backend, record = answered(home, lambda capsule: worker_result(
        capsule, model_json(sources=('map', 'pref'), route='team_plan')))
    assert (record['status'], record['text'], record['route'], record['missing']) == (
        'answered', 'The fictional launch is on Friday.', 'team_plan', False)
    assert record['model'] == 'fixture-model' and record['omitted_counts'] == {'sources': 0, 'history': 0}
    trusted = {source['id']: source for source in PROJECT['sources']}
    assert sorted(citation['id'] for citation in record['sources']) == ['map', 'pref']
    for citation in record['sources']:
        assert (citation['kind'], citation['ref']) == (trusted[citation['id']]['kind'], trusted[citation['id']]['ref'])
    assert home.store.get_room(room)['runs'] == [] and home.transport.sends == []  # a route suggestion starts nothing
    assert not any(message.get('kind') == 'result' for message in home.store.get_room(room)['messages'])


def test_omissions_add_pre_worker_exclusions_and_worker_packing_once(home):
    offered = {}

    def keep_one_of_each(capsule):
        offered.update(capsule)
        return worker_result(capsule, model_json(), keep_sources=1, keep_history=1)

    def earlier_turns(lead_service, room):
        for request in ('h1', 'h2'):
            lead_service.submit(room, f'Earlier question {request}?', request)
            lead_service.stop(room, request)
        home.reader.text['note-ref'] = 'NOTE: changed after approval.'  # excluded before packing

    _, _, record = answered(home, keep_one_of_each, setup=earlier_turns)
    assert [item['id'] for item in offered['sources']] == ['pref', 'map'] and len(offered['history']) >= 2
    assert record['status'] == 'answered'
    assert record['omitted_counts'] == {'sources': 1 + 1, 'history': len(offered['history']) - 1}


@pytest.mark.parametrize('text', [
    pytest.param(model_json(sources=('map',)), id='cites-an-omitted-source'),
    pytest.param(model_json(sources=('note',)), id='cites-an-excluded-source'),
    pytest.param(model_json(sources=('ghost',)), id='cites-an-unknown-source'),
    pytest.param(model_json(sources=('pref', 'pref')), id='duplicate-citation'),
    pytest.param(model_json(answer=''), id='empty-answer'),
    pytest.param(model_json(answer='x' * 4001), id='answer-over-4000'),
    pytest.param(model_json(route='deploy'), id='unknown-route'),
    pytest.param(json.dumps({'answer': 'A', 'sources': [], 'missing': 'false', 'route': 'answer'}), id='missing-not-bool'),
    pytest.param(json.dumps({'answer': 'A', 'sources': [], 'route': 'answer'}), id='missing-key'),
    pytest.param(json.dumps({'answer': 'A', 'sources': [], 'missing': False, 'route': 'answer', 'why': 'x'}),
                 id='extra-key'),
    pytest.param('{"answer": "A", "answer": "B", "sources": [], "missing": false, "route": "answer"}', id='duplicate-key'),
    pytest.param('```json\n' + model_json() + '\n```', id='markdown-fence'),
    pytest.param('The launch is on Friday.', id='prose')])
def test_malformed_model_output_fails_as_output_invalid_without_text(home, text):
    def keep_only_pref(capsule):
        return worker_result(capsule, text, keep_sources=1)

    _, _, record = answered(home, keep_only_pref, setup=lambda _, __: home.reader.text.update(
        {'note-ref': 'NOTE: changed after approval.'}))
    assert_failed_without_text(record, 'output_invalid')


@pytest.mark.parametrize('code', ['timeout', 'cache_unavailable', 'input_too_large', 'worker_failed',
                                  'sandbox_unavailable'])
def test_adapter_failures_fail_the_answer_with_their_fixed_code(home, code):
    room, _, record = answered(home, error=inference().LeadInferenceError(code))
    assert_failed_without_text(record, code)
    assert texts(home, room) == ['What is the launch day?'] and home.transport.sends == []


def test_a_result_from_another_model_is_refused(home):
    _, _, record = answered(home, lambda capsule: worker_result(capsule, model='another-model'))
    assert_failed_without_text(record)


def test_a_new_team_run_shares_history_but_never_private_answers(home):
    room, = rooms(home, 1)
    home.store.submit(room, 'SHARED-EARLIER request for the team.', 'n1')
    home.store.stop(room)
    lead_service = service(home, FakeBackend())
    lead_service.submit(room, 'PRIVATE-EARLIER question?', 'p1')
    lead_service.run_once()  # a completed private answer now sits in the room
    run = home.store.submit(room, 'Add the SELECTED fixture field and test it.', 'n2')['run_id']
    home.service.step(run)
    assignment = [send for send in home.transport.sends if send['run_id'] == run][-1]['assignment_path'].read_text()
    assert 'SELECTED fixture field' in assignment
    assert 'SHARED-EARLIER' in assignment
    for earlier in ('PRIVATE-EARLIER', 'The fictional launch is on Friday.'):
        assert earlier not in assignment


def test_status_ignores_answers_and_archive_waits_for_them_to_settle(home):
    room, = rooms(home, 1)
    lead_service = service(home, FakeBackend())
    lead_service.submit(room, 'Question?', 'a1')
    assert home.store.status_snapshot()['runs'] == []
    assert home.service.archive_preview(room)['eligible'] is False
    lead_service.stop(room, 'a1')
    assert home.service.archive_preview(room)['eligible'] is True


@pytest.mark.parametrize(('headroom', 'accepted'), [(100 * 1024, False), (700 * 1024, True)],
                         ids=['below-one-allowance', 'room-for-allowances'])
def test_each_unfinished_answer_reserves_its_completion_below_the_ordinary_ceiling(home, headroom, accepted):
    room, = rooms(home, 1)
    with home.store.edit() as data:  # fixture setup through the local primitive
        pad_value(data, home.team.ADMISSION_LIMIT_BYTES - headroom)
    before = home.store.path.read_bytes()
    if accepted:
        assert home.store.queue_answer(room, 'Question?', 'a1', binding=PROJECT)['status'] == 'queued'
        return
    with pytest.raises(home.team.TeamCapacityError):
        home.store.queue_answer(room, 'Question?', 'a1', binding=PROJECT)
    assert home.store.path.read_bytes() == before


def test_current_system_step_reaches_inference_without_prior_system_history(home):
    room, = rooms(home, 1)
    with home.store.edit() as data:
        home.team._message(data['rooms'][room], 'system', 'UNRELATED SYSTEM NOTE', shared=False)
    backend = FakeBackend()
    runner = service(home, backend)
    home.store.queue_answer(room, 'CURRENT SYSTEM TASK', 'system-step', binding=PROJECT, author='system')
    runner.run_once()
    assert home.store.get_answer(room, 'system-step')['status'] == 'answered'
    assert len(backend.requests) == 1
    assert backend.requests[0]['request'] == 'CURRENT SYSTEM TASK'
    assert 'UNRELATED SYSTEM NOTE' not in json.dumps(backend.requests)
    assert home.store.get_room(room)['messages'][1]['author'] == 'system'
