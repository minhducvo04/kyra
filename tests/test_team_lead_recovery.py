"""Claude acceptance for local-answer recovery at startup and after a busy flight (2026-09-28).

Contract: a private design note, accepted by Home, and Home's invariant: retry only while a saved local obligation
exists; never settle a running or stopping answer before the flight that holds it is free; permanent setup or storage
errors never loop. Startup keeps the owner, personal-tenant and inline-worker gates and calls the module-level
`team_http.get_lead(store)` seam, which these tests replace with a fake service factory. The real startup hook,
TeamStore, LeadService and `wake_lead` worker thread run against scratch stores and a fake adapter whose flight can be
held busy, as a child left behind by an earlier process would hold it. No model, native transport or network.
"""
import importlib
import threading
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_team_chat_acceptance import authorize
from tests.test_team_lead import PROJECT, FakeBackend, Sources, inference, lead

WORKER = 'kyra-local-lead'
GATES = {'owner': ('KYRA_OWNER_MACHINE', 'false'), 'tenant': ('KYRA_TENANT', 'other'),
         'inline': ('KYRA_INLINE_WORKER', 'false')}


class Flights(FakeBackend):
    """The fake adapter with a flight that an earlier, still-running child can hold: while `busy` every attempt is
    refused with LeadBusyError, and `flight_error` fails the flight itself. Every attempt is recorded in order."""

    def __init__(self):
        super().__init__()
        self.attempts, self.flight_error = [], None

    @contextmanager
    def flight(self):
        self.attempts.append('error' if self.flight_error else 'busy' if self.busy else 'flight')
        if self.flight_error is not None:
            raise self.flight_error
        with super().flight() as flight:
            yield flight


# wake_lead parks on Event.wait(2) while a flight is busy. The tests assert the retries, not that gap.
_RETRY_PARK_S = 2
_REAL_EVENT_WAIT = threading.Event.wait
_REAL_EVENT_SET = threading.Event.set


class _RetryClock:
    """One lead-worker retry park, off the wall clock.

    Advancing ends one park the way the two second timeout would. Between advances the worker stays parked, so a test
    can stop the question or break storage while it is idle. Event.set wakes the same park, as Event.wait would.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._now = 0.0
        self._parked = False

    def advance(self, seconds=_RETRY_PARK_S):
        with self._cond:
            self._now += seconds
            self._cond.notify_all()

    def lead_is_idle(self):
        """True when no lead worker is inside a store write.

        Replacing the store fails on Windows if a reader still has the file open. Tests observe the saved answer only
        while the worker is parked on this clock or has already exited, which is after any replace.
        """
        alive = any(thread.name == WORKER and thread.is_alive() for thread in threading.enumerate())
        if not alive:
            return True
        with self._cond:
            return self._parked

    def wait(self, event, timeout):
        if event.is_set():
            return True
        with self._cond:
            deadline = self._now + timeout
            self._parked = True
            try:
                while self._now < deadline and not event.is_set():
                    self._cond.wait()
                return event.is_set()
            finally:
                self._parked = False


_retry_clock = _RetryClock()


def _event_wait(self, timeout=None):
    if timeout == _RETRY_PARK_S and threading.current_thread().name == WORKER:
        return _retry_clock.wait(self, timeout)
    return _REAL_EVENT_WAIT(self, timeout)


def _event_set(self):
    # Set the flag while holding the clock lock so a parked worker cannot miss the wake.
    with _retry_clock._cond:
        _REAL_EVENT_SET(self)
        _retry_clock._cond.notify_all()


def until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _retry_clock.lead_is_idle() and predicate():
            return True
        _retry_clock.advance()
        time.sleep(0.001)  # yield so the lead worker can run the iteration just released
    return _retry_clock.lead_is_idle() and predicate()


@pytest.fixture
def boot(tmp_path, monkeypatch):
    from companion import team_http, workflow_control
    from companion.settings import get_settings
    team = importlib.import_module('companion.team_chat')
    data = tmp_path / 'data'
    monkeypatch.setattr(workflow_control, 'DATA_DIR', data)
    monkeypatch.setattr(team, 'DATA_DIR', data)
    for name, value in (('KYRA_OWNER_MACHINE', 'true'), ('KYRA_TENANT', 'personal'), ('KYRA_INLINE_WORKER', 'true')):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    before = set(threading.enumerate())
    state = SimpleNamespace(data=data, backend=Flights(), factory=[], native=[], factory_error=None, native_error=None)

    def get_lead(store):  # the module-level seam startup calls directly
        state.factory.append(store)
        if state.factory_error is not None:
            raise state.factory_error
        return state.service(store)

    def wake_service(service):  # native work is observed, never stepped
        state.native.append(service)
        if state.native_error is not None:
            raise state.native_error

    def home():
        authorize(data)
        state.store = team.TeamStore()
        return state.store

    def start():
        app = FastAPI()
        team_http.install(app)
        with TestClient(app, base_url='http://127.0.0.1:8420', client=('127.0.0.1', 4321)):
            pass

    def workers():
        return [thread for thread in set(threading.enumerate()) - before if thread.name == WORKER and thread.is_alive()]

    def worker_gone(timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = workers()
            if not alive:
                return True
            _retry_clock.advance()  # release a worker parked on the retry clock, then collect it
            remaining = max(0.0, deadline - time.monotonic())
            for thread in alive:
                thread.join(min(0.01, remaining))
        return not workers()

    def gate(name):
        monkeypatch.setenv(*GATES[name])
        get_settings.cache_clear()

    state.service = lambda store: lead().LeadService(store, state.backend, project=PROJECT, source_reader=Sources())
    state.home, state.start, state.workers, state.worker_gone, state.gate = home, start, workers, worker_gone, gate
    monkeypatch.setattr(team_http, 'get_lead', get_lead)
    monkeypatch.setattr(team_http, 'wake_service', wake_service)
    monkeypatch.setattr(threading.Event, 'wait', _event_wait)
    monkeypatch.setattr(threading.Event, 'set', _event_set)
    yield state
    state.backend.busy, state.backend.flight_error = False, None  # let a retrying worker finish before cleanup
    alive = not worker_gone(10.0)
    get_settings.cache_clear()
    assert not alive, 'a local answer worker outlived its test'


def ask(state, question='What is the launch day?', request_id='ask-1', room=None):
    """A private question saved by an earlier process: one queued answer."""
    room = room or state.store.create_room('Fixture room')['id']
    state.store.queue_answer(room, question, request_id, binding=PROJECT)
    return room


def left_running(state, room, request_id, *, stop):
    """What a process that died mid-answer leaves behind: a running record, with Stop possibly requested."""
    with state.store.edit() as data:
        answer = next(item for item in data['rooms'][room]['messages']
                      if item.get('kind') == 'answer' and item['request_id'] == request_id)
        answer.update(status='running', started_at=time.time(), attempt_id=uuid.uuid4().hex, stop_requested=stop)


def settled(record, stop):
    assert record['status'] == ('stopped' if stop else 'failed')
    assert record['text'] == '' and record['sources'] == [] and record['route'] is None


def listing(root):
    return sorted((str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
                  for path in root.rglob('*') if path.is_file()) if root.exists() else None


def test_startup_answers_a_saved_question_once_without_native_work(boot):
    store = boot.home()
    room = ask(boot)
    boot.start()
    assert until(lambda: store.get_answer(room, 'ask-1')['status'] == 'answered'), 'startup never resumed the question'
    assert boot.worker_gone(), 'the worker kept polling after the queue was empty'
    assert [item.root for item in boot.factory] == [store.root]
    assert [capsule['request_id'] for capsule in boot.backend.requests] == ['ask-1']
    assert store.get_answer(room, 'ask-1')['text'] == 'The fictional launch is on Friday.'
    assert boot.native == [] and store.get_room(room)['runs'] == []
    saved = store.path.read_bytes()
    boot.start()  # a later restart finds nothing pending: nothing is built, run, answered twice or written
    assert boot.worker_gone() and len(boot.factory) == 1 and len(boot.backend.requests) == 1
    assert store.path.read_bytes() == saved


@pytest.mark.parametrize('gate', sorted(GATES))
def test_startup_outside_the_owner_personal_inline_gates_leaves_saved_work_alone(boot, gate):
    store = boot.home()
    room = ask(boot)
    saved = store.path.read_bytes()
    boot.gate(gate)
    boot.start()
    assert boot.factory == [] and boot.native == [] and boot.backend.attempts == [] and not boot.workers()
    assert store.get_answer(room, 'ask-1')['status'] == 'queued' and store.path.read_bytes() == saved


@pytest.mark.parametrize('saved', ['missing', 'settled'])
def test_idle_startup_builds_nothing_starts_nothing_and_writes_nothing(boot, saved):
    if saved == 'settled':  # an answered and a stopped answer are history, not obligations
        store = boot.home()
        service = boot.service(store)
        first, second = (store.create_room(f'Fixture room {index}')['id'] for index in range(2))
        service.submit(first, 'What is the launch day?', 'ask-1')
        service.run_once()
        service.submit(second, 'And the venue?', 'ask-2')
        service.stop(second, 'ask-2')
        assert [store.get_answer(room, key)['status'] for room, key in ((first, 'ask-1'), (second, 'ask-2'))] == [
            'answered', 'stopped']
    files = listing(boot.data)
    attempts = list(boot.backend.attempts)
    boot.start()
    assert boot.factory == [] and boot.native == [] and not boot.workers()
    assert boot.backend.attempts == attempts
    assert listing(boot.data) == files  # no store, lock or other file is created or touched


@pytest.mark.parametrize('stop', [False, True], ids=['running', 'stopping'])
def test_startup_settles_an_abandoned_answer_once_when_the_flight_is_free(boot, stop):
    store = boot.home()
    room = ask(boot, 'What was I asking before the restart?', 'ask-old')
    left_running(boot, room, 'ask-old', stop=stop)
    boot.start()
    assert until(lambda: store.get_answer(room, 'ask-old')['status'] != 'running'), 'startup never recovered it'
    settled(store.get_answer(room, 'ask-old'), stop)
    assert boot.worker_gone() and boot.backend.requests == [] and boot.native == []  # never replayed


@pytest.mark.parametrize('stop', [False, True], ids=['running', 'stopping'])
def test_an_abandoned_answer_waits_for_the_held_flight_then_settles_once_and_the_queue_drains(boot, stop):
    store = boot.home()
    abandoned = ask(boot, 'What was I asking before the restart?', 'ask-old')
    left_running(boot, abandoned, 'ask-old', stop=stop)
    waiting = ask(boot, 'What is the launch day?', 'ask-new')
    boot.backend.busy = True  # the dead process's child still holds the flight
    boot.start()
    assert until(lambda: boot.backend.attempts.count('busy') >= 2), 'the worker gave up on a busy flight'
    old = store.get_answer(abandoned, 'ask-old')
    assert (old['status'], old['stop_requested']) == ('running', stop)  # nothing settles while the flight is held
    assert store.get_answer(waiting, 'ask-new')['status'] == 'queued' and boot.backend.requests == []
    boot.backend.busy = False  # the old child exits and its flight is free
    assert until(lambda: store.get_answer(waiting, 'ask-new')['status'] == 'answered'), 'no progress after release'
    settled(store.get_answer(abandoned, 'ask-old'), stop)
    assert [capsule['request_id'] for capsule in boot.backend.requests] == ['ask-new']  # the abandoned one is not replayed
    first = boot.backend.attempts.index('flight')
    assert first >= 2 and set(boot.backend.attempts[:first]) == {'busy'}
    assert boot.worker_gone() and len(boot.factory) == 1 and boot.native == []


def test_an_ordinary_wake_keeps_retrying_a_busy_flight_until_it_is_free(boot):
    from companion import team_http
    store = boot.home()
    service = boot.service(store)
    room = store.create_room('Fixture room')['id']
    boot.backend.busy = True
    service.submit(room, 'What is the launch day?', 'ask-1')
    team_http.wake_lead(service)
    assert until(lambda: boot.backend.attempts.count('busy') >= 2), 'the worker gave up on a busy flight'
    assert store.get_answer(room, 'ask-1')['status'] == 'queued' and boot.backend.requests == []
    boot.backend.busy = False
    # No second submit, wake or startup: the waiting worker itself makes progress once the flight is free.
    assert until(lambda: store.get_answer(room, 'ask-1')['status'] == 'answered'), 'no progress after release'
    assert boot.worker_gone() and boot.factory == [] and boot.native == []


def test_stopping_the_last_queued_question_ends_the_retries(boot):
    from companion import team_http
    store = boot.home()
    service = boot.service(store)
    room = store.create_room('Fixture room')['id']
    boot.backend.busy = True
    service.submit(room, 'What is the launch day?', 'ask-1')
    team_http.wake_lead(service)
    assert until(lambda: 'busy' in boot.backend.attempts)
    assert service.stop(room, 'ask-1')['status'] == 'stopped'
    assert boot.worker_gone(), 'the worker kept waiting with no obligation left'
    boot.backend.busy = False
    assert boot.backend.requests == [] and store.get_answer(room, 'ask-1')['status'] == 'stopped'


def test_a_permanent_flight_failure_is_tried_once_and_never_retried(boot):
    from companion import team_http
    store = boot.home()
    service = boot.service(store)
    room = store.create_room('Fixture room')['id']
    service.submit(room, 'What is the launch day?', 'ask-1')
    boot.backend.flight_error = inference().LeadInferenceError('sandbox_unavailable')
    team_http.wake_lead(service)
    assert boot.worker_gone(), 'a permanent failure kept the worker looping'
    assert boot.backend.attempts == ['error'] and boot.backend.requests == []


def test_storage_that_breaks_while_waiting_ends_the_worker_without_rewriting_it(boot):
    from companion import team_http
    store = boot.home()
    service = boot.service(store)
    room = store.create_room('Fixture room')['id']
    boot.backend.busy = True
    service.submit(room, 'What is the launch day?', 'ask-1')
    team_http.wake_lead(service)
    assert until(lambda: 'busy' in boot.backend.attempts)
    store.path.write_text('{not json', encoding='utf-8')  # the saved state becomes unreadable
    boot.backend.busy = False
    assert boot.worker_gone(), 'unreadable storage kept the worker looping'
    assert boot.backend.requests == [] and store.path.read_text(encoding='utf-8') == '{not json'


def test_startup_with_unreadable_team_state_builds_nothing_and_rewrites_nothing(boot):
    store = boot.home()
    ask(boot)
    store.path.write_text('{not json', encoding='utf-8')
    boot.start()  # startup itself survives
    assert boot.factory == [] and boot.native == [] and boot.backend.attempts == [] and not boot.workers()
    assert store.path.read_text(encoding='utf-8') == '{not json'


def test_a_failing_lead_factory_at_startup_is_tried_once_and_native_work_still_resumes(boot):
    store = boot.home()
    room = ask(boot)
    store.submit(store.create_room('Team room')['id'], 'Add a fixture field and test it.', 'native-1')
    boot.factory_error = OSError('fixture: local setup unavailable')
    boot.start()  # startup itself survives
    assert len(boot.factory) == 1, 'startup never attempted local recovery, or retried a permanent failure'
    assert len(boot.native) == 1 and boot.backend.attempts == [] and not boot.workers()
    assert store.get_answer(room, 'ask-1')['status'] == 'queued'


@pytest.mark.parametrize('error', [ValueError, RuntimeError])
def test_a_native_resume_failure_does_not_block_local_recovery(boot, error):
    store = boot.home()
    room = ask(boot)
    store.submit(store.create_room('Team room')['id'], 'Add a fixture field and test it.', 'native-1')
    boot.native_error = error('fixture: native resume failed')
    boot.start()  # startup itself survives
    assert len(boot.native) == 1
    assert until(lambda: store.get_answer(room, 'ask-1')['status'] == 'answered'), 'local recovery was blocked'
    assert boot.worker_gone() and len(boot.factory) == 1


def test_native_only_work_keeps_its_own_path_and_builds_no_lead(boot):
    store = boot.home()
    store.submit(store.create_room('Team room')['id'], 'Add a fixture field and test it.', 'native-1')
    boot.start()
    assert len(boot.native) == 1 and boot.factory == [] and boot.backend.attempts == [] and not boot.workers()


@pytest.mark.parametrize('gate', sorted(GATES))
def test_a_wake_outside_the_owner_personal_inline_gates_starts_nothing(boot, gate):
    from companion import team_http
    store = boot.home()
    service = boot.service(store)
    room = store.create_room('Fixture room')['id']
    service.submit(room, 'What is the launch day?', 'ask-1')
    boot.gate(gate)
    team_http.wake_lead(service)
    # A started worker is alive at this point or has already flown; neither may happen.
    assert not boot.workers() and boot.backend.attempts == [] and boot.backend.requests == []
    assert store.get_answer(room, 'ask-1')['status'] == 'queued'
