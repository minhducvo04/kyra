"""Claude acceptance for server-side Team delegation (2026-09-29): independent tests written against Home's frozen API
while a second builder lane built src/companion/team_delegation.py in parallel, so this is acceptance evidence, not a red-first proof.

Contract: a private design note (the
coordinator reconciliation and the second builder's clarification). Behavior through the public seam only:
Coordinator(store, lead, *, roster, submit, child_status, child_stop). A refusal (ApiError 403 consent or local-only,
422 unknown tag, 409 changed duplicate) leaves no record and calls nothing; the local lead and every child get the
owner's exact typed text, tags included, never room history; the roster is snapshotted at
consent time and a blocked target keeps its exact reason with no fallback; a duplicate never dispatches again and a
changed one is refused; Stop before the dispatch claim sends nothing, and Stop during a submit stops the returned child
and ends the fan-out without claiming the prompt was unsent; a real owner-process death parks work as uncertain and it
is never replayed, a live other process keeps its dispatch, and a waiting local answer resumes only before dispatch.
Fakes stand in for the local model and every provider; the restart cases use real child processes. No model, vendor or
native call runs. State names are pinned only where the contract names them.
"""
import fcntl
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion.errors import ApiError
from companion.team_chat import TeamStore

ROOT = Path(__file__).resolve().parent.parent
READY = {'cursor': '', 'antigravity': '', 'claude': 'reply_publication_denied', 'codex': 'not_bound'}
CHILD_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
HISTORY = 'EARLIER-PRIVATE-HISTORY must never be forwarded'
RESULT_KEYS = {'request_id', 'room_id', 'text', 'mode', 'state', 'reason', 'targets', 'children', 'lead_request_id'}


class Store:
    """A real TeamStore for rooms; local answers come from the test, not a model."""
    def __init__(self, root):
        self._team = TeamStore(root)
        self.answers = {}

    def __getattr__(self, name):
        return getattr(self._team, name)

    def get_answer(self, room_id, request_id):
        if (room_id, request_id) not in self.answers:
            raise LookupError('Answer not found')
        return dict(self.answers[(room_id, request_id)])


class Lead:
    """Like LeadService.submit: the question is queued in the store, and a model answers later."""
    def __init__(self, store):
        self.store, self.calls = store, []

    def submit(self, room_id, text, request_id):
        self.calls.append((room_id, text, request_id))
        self.store.answers.setdefault((room_id, request_id), {'kind': 'answer', 'request_id': request_id,
                                                              'status': 'queued', 'route': None, 'missing': None})
        return {'request_id': request_id, 'status': 'queued'}

    def stop(self, room_id, request_id):
        return None


class Providers:
    def __init__(self, roster):
        self.roster_value = dict(roster)
        self.submits, self.stops, self.states = [], [], {}
        self.on_submit = self.on_stop = None

    def roster(self):
        return dict(self.roster_value)

    def submit(self, room_id, provider, text, child_request_id, **kwargs):
        self.submits.append((room_id, provider, text, child_request_id))
        child = {'id': f'run-{provider}-{len(self.submits)}', 'state': 'queued', 'reason': ''}
        self.states[child['id']] = dict(child)
        if self.on_submit:
            self.on_submit(provider)
        return dict(child)

    def child_status(self, room_id, provider, ident):
        return dict(self.states[ident])

    def child_stop(self, room_id, provider, ident):
        if self.on_stop:
            self.on_stop(provider)
        self.stops.append((room_id, provider, ident))


def answer(request_id, route):
    return {'kind': 'answer', 'request_id': request_id, 'status': 'answered', 'route': route, 'missing': False,
            'text': 'Local routing decision (fixture).', 'sources': []}


def refused(status, call, *args, **kwargs):
    with pytest.raises(ApiError) as caught:
        call(*args, **kwargs)
    assert caught.value.status_code == status


def sent(world):
    return sorted(provider for _, provider, _, _ in world.fake.submits)


@pytest.fixture
def world(tmp_path):
    module = importlib.import_module('companion.team_delegation')
    store = Store(tmp_path / 'team')
    shared = store.create_room('Shared fixture')['id']
    private = store.create_room('Local fixture', local_only=True)['id']
    store.submit(shared, HISTORY, 'h-0', share_with_team=False)
    lead, fake = Lead(store), Providers(READY)

    def coordinator():
        return module.Coordinator(store, lead, roster=fake.roster, submit=fake.submit,
                                  child_status=fake.child_status, child_stop=fake.child_stop)
    return SimpleNamespace(store=store, lead=lead, fake=fake, shared=shared, private=private, new=coordinator,
                           root=tmp_path / 'team', tmp=tmp_path)


def test_refusals_leave_no_record_and_call_nothing(world):
    coordinator = world.new()
    cases = [(world.shared, '@cursor Say hi.', 'r-no-consent', False, 403),
             (world.private, '@cursor Say hi.', 'r-local-only', True, 403),
             (world.shared, '@curser Say hi.', 'r-unknown-tag', True, 422),
             (world.shared, '@cursor @nobody Say hi.', 'r-one-unknown-tag', True, 422)]
    for room, text, request_id, consent, status in cases:
        refused(status, coordinator.create, room, text, request_id, consent=consent, mode='cursor')
        with pytest.raises(LookupError):
            coordinator.status(room, request_id)
    assert world.lead.calls == [] and world.fake.submits == [] and coordinator.pending() == []


def test_consent_is_never_upgraded_in_place(world):
    coordinator = world.new()
    refused(403, coordinator.create, world.shared, '@cursor Say hi.', 'r-1')
    coordinator.create(world.shared, '@cursor Say hi.', 'r-1', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-1')
    assert sent(world) == ['cursor']


def test_children_get_only_the_typed_instruction(world):
    coordinator = world.new()
    typed = '@cursor @antigravity Summarise the fixture module in one line.'
    coordinator.create(world.shared, typed, 'r-typed', consent=True, mode='cursor')
    result = coordinator.status(world.shared, 'r-typed')
    assert RESULT_KEYS <= set(result) and set(result['targets']) == {'cursor', 'antigravity'}
    coordinator.advance(world.shared, 'r-typed')
    assert sent(world) == ['antigravity', 'cursor']
    for room, _, text, _ in world.fake.submits:
        assert room == world.shared and text == typed and 'EARLIER-PRIVATE' not in text


def legacy_wait(coordinator, room, text, request):
    coordinator.create(room, text, request, consent=True, mode='cursor')
    with coordinator._edit(room, request) as row:
        row.update(state='waiting_lead', targets=[], lead_request_id=request + '-lead')
        row.pop('retry_policy', None)


def test_legacy_waiting_row_resumes_local_answer_without_dispatch(world):
    coordinator = world.new()
    typed = 'Tell all available teammates to say hi.'
    legacy_wait(coordinator, world.shared, typed, 'r-auto')
    coordinator.advance(world.shared, 'r-auto')
    assert len(world.lead.calls) == 1
    room, text, lead_id = world.lead.calls[0]
    assert (room, text) == (world.shared, typed) and lead_id != 'r-auto'
    assert coordinator.status(world.shared, 'r-auto')['lead_request_id'] == lead_id
    assert 'r-auto' in [row['request_id'] for row in coordinator.pending()]
    coordinator.advance(world.shared, 'r-auto')
    assert world.fake.submits == []
    world.store.answers[(world.shared, lead_id)] = answer(lead_id, 'answer')
    coordinator.advance(world.shared, 'r-auto')
    assert world.fake.submits == [] and coordinator.status(world.shared, 'r-auto')['children'] == {}


def test_legacy_app_all_uses_the_consent_time_snapshot(world):
    world.fake.roster_value = dict(READY, antigravity='not_configured')
    coordinator = world.new()
    typed = 'Ask every connected app to report its version.'
    legacy_wait(coordinator, world.shared, typed, 'r-app-all')
    coordinator.advance(world.shared, 'r-app-all')
    world.fake.roster_value = dict(READY)  # Antigravity is configured after consent; it was not a candidate then.
    lead_id = world.lead.calls[0][2]
    world.store.answers[(world.shared, lead_id)] = answer(lead_id, 'app_all')
    coordinator.advance(world.shared, 'r-app-all')
    assert [(provider, text) for _, provider, text, _ in world.fake.submits] == [('cursor', typed)]


def test_at_all_records_every_teammate_and_native_blockers_keep_their_exact_reason(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@all Say hi.', 'r-all', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-all')
    result = coordinator.status(world.shared, 'r-all')
    assert set(result['targets']) == {'cursor', 'antigravity', 'claude', 'codex', 'local'}
    assert sent(world) == ['antigravity', 'cursor']
    blocked = {'claude': READY['claude'], 'codex': READY['codex']}
    assert {provider: result['children'][provider]['reason'] for provider in blocked} == blocked


def test_an_explicit_blocked_teammate_is_not_replaced(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@claude Plan the fixture change.', 'r-claude', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-claude')
    assert world.fake.submits == [] and world.lead.calls == []
    assert coordinator.status(world.shared, 'r-claude')['children']['claude']['reason'] == READY['claude']


def test_duplicate_returns_the_original_and_a_changed_request_is_refused(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor Say hi.', 'r-dup', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-dup')
    first = coordinator.status(world.shared, 'r-dup')['children']['cursor']['id']
    coordinator.create(world.shared, '@cursor Say hi.', 'r-dup', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-dup')
    for changed in ('@cursor Say hello instead.', '@antigravity Say hi.'):
        refused(409, coordinator.create, world.shared, changed, 'r-dup', consent=True, mode='cursor')
    assert len(world.fake.submits) == 1 and coordinator.status(world.shared, 'r-dup')['children']['cursor']['id'] == first


def test_child_request_ids_are_valid_distinct_and_recorded(world):
    coordinator = world.new()
    for request_id in ('r-a', 'r-b'):
        coordinator.create(world.shared, '@cursor @antigravity Say hi.', request_id, consent=True, mode='cursor')
        coordinator.advance(world.shared, request_id)
    ids = [child for _, _, _, child in world.fake.submits]
    assert len(ids) == len(set(ids)) == 4 and all(CHILD_ID.fullmatch(ident) for ident in ids)
    recorded = {c['request_id'] for c in coordinator.status(world.shared, 'r-a')['children'].values()}
    assert recorded <= set(ids) and len(recorded) == 2


def test_stop_before_the_dispatch_claim_sends_nothing(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-stop', consent=True, mode='cursor')
    coordinator.stop(world.shared, 'r-stop')
    coordinator.advance(world.shared, 'r-stop')
    assert world.fake.submits == [] and world.fake.stops == []
    assert 'r-stop' not in [row['request_id'] for row in coordinator.pending()]


def test_stop_during_a_submit_stops_that_child_and_ends_the_fan_out(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-race', consent=True, mode='cursor')
    # Stop arrives while the first callback runs; the database must not be held across the callback.
    world.fake.on_submit = lambda provider: coordinator.stop(world.shared, 'r-race')
    coordinator.advance(world.shared, 'r-race')
    (_, provider, _, _), = world.fake.submits
    assert [(p, ident) for _, p, ident in world.fake.stops] == [(provider, f'run-{provider}-1')]


def test_stop_records_the_parent_before_stopping_children(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-order', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-order')
    before = coordinator.status(world.shared, 'r-order')
    seen = []
    world.fake.on_stop = lambda provider: seen.append(coordinator.status(world.shared, 'r-order'))
    coordinator.stop(world.shared, 'r-order')
    assert len(seen) == 2 and all((s['state'], s['reason']) != (before['state'], before['reason']) for s in seen)


def test_status_mirrors_children_and_never_invents_acknowledgement(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-ack', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-ack')
    result = coordinator.status(world.shared, 'r-ack')
    assert {child['state'] for child in result['children'].values()} == {'queued'} and result['state'] == 'running'
    for child in result['children'].values():
        world.fake.states[child['id']]['state'] = 'completed'
    assert coordinator.status(world.shared, 'r-ack')['state'] == 'completed'



def test_a_stopping_child_stays_tracked_until_it_reports_stopped(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor Say hi.', 'r-stopping', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-stopping')
    child_id = coordinator.status(world.shared, 'r-stopping')['children']['cursor']['id']
    world.fake.states[child_id]['state'] = 'running'
    # Callback compatibility: a provider callback may report "stopping" until its process confirms the stop.
    # (Today's app store keeps running with stop_requested; this is not a proven live failure.)
    world.fake.on_stop = lambda provider: world.fake.states[child_id].update(state='stopping')
    coordinator.stop(world.shared, 'r-stopping')
    for _ in range(2):
        coordinator.advance(world.shared, 'r-stopping')
        polled = coordinator.status(world.shared, 'r-stopping')
        assert polled['children']['cursor']['state'] == 'stopping'
        assert polled['state'] not in {'uncertain', 'completed', 'stopped'}
        assert 'r-stopping' in [row['request_id'] for row in coordinator.pending()]
    world.fake.states[child_id]['state'] = 'stopped'
    final = coordinator.status(world.shared, 'r-stopping')
    assert final['children']['cursor']['state'] == 'stopped' and final['state'] == 'stopped'
    assert 'r-stopping' not in [row['request_id'] for row in coordinator.pending()]
    assert len(world.fake.submits) == 1


def test_a_child_stopped_during_its_submit_is_tracked_through_stopping(world):
    coordinator = world.new()
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-race-stop', consent=True, mode='cursor')
    world.fake.on_submit = lambda provider: coordinator.stop(world.shared, 'r-race-stop')
    world.fake.on_stop = lambda provider: world.fake.states[f'run-{provider}-1'].update(state='stopping')
    coordinator.advance(world.shared, 'r-race-stop')
    (_, provider, _, _), = world.fake.submits
    polled = coordinator.status(world.shared, 'r-race-stop')
    assert polled['children'][provider]['state'] == 'stopping' and polled['state'] != 'uncertain'
    assert 'r-race-stop' in [row['request_id'] for row in coordinator.pending()]
    world.fake.states[f'run-{provider}-1']['state'] = 'stopped'
    assert coordinator.status(world.shared, 'r-race-stop')['children'][provider]['state'] == 'stopped'
    assert len(world.fake.submits) == 1


OWNER = r'''
import fcntl, json, os, sys
src, root, room, text, request_id, behavior, log, release, roster = sys.argv[1:10]
sys.path.insert(0, src)
from companion.team_chat import TeamStore
from companion.team_delegation import Coordinator
roster = json.loads(roster)
class Store:
    def __init__(self, root):
        self.team, self.answers = TeamStore(root), {}
    def __getattr__(self, name):
        return getattr(self.team, name)
    def get_answer(self, room_id, request_id):
        if (room_id, request_id) not in self.answers:
            raise LookupError('Answer not found')
        return dict(self.answers[(room_id, request_id)])
store = Store(root)
class Lead:
    def submit(self, room_id, text, request_id):
        store.answers.setdefault((room_id, request_id), {'kind': 'answer', 'request_id': request_id,
                                                         'status': 'queued', 'route': None, 'missing': None})
        return {'request_id': request_id, 'status': 'queued'}
    def stop(self, room_id, request_id):
        return None
def submit(room_id, provider, text, child_request_id):
    with open(log, 'a') as handle:
        handle.write(json.dumps([provider, child_request_id]) + '\n')
    if behavior == 'die':
        os._exit(17)
    if behavior == 'hold':
        # Parent holds this flock until it has observed the live dispatch.
        fd = os.open(release, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.close(fd)
    return {'id': 'run-' + provider, 'state': 'queued', 'reason': ''}
coordinator = Coordinator(store, Lead(), roster=lambda: dict(roster), submit=submit,
                          child_status=lambda room_id, provider, ident: {'id': ident, 'state': 'queued', 'reason': ''},
                          child_stop=lambda room_id, provider, ident: None)
coordinator.create(room, text, request_id, consent=True, mode='cursor')
if behavior == 'create_only':
    with coordinator._edit(room, request_id) as row:
        row.update(state='waiting_lead', targets=[], lead_request_id=request_id + '-lead')
        row.pop('retry_policy', None)
if behavior != 'create_only':
    coordinator.advance(room, request_id)
'''


def owner(world, text, request_id, behavior):
    log, release = world.tmp / f'{request_id}.log', world.tmp / f'{request_id}.release'
    hold_fd = None
    if behavior == 'hold':
        hold_fd = os.open(release, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600)
        fcntl.flock(hold_fd, fcntl.LOCK_EX)
    process = subprocess.Popen([sys.executable, '-c', OWNER, str(ROOT / 'src'), str(world.root), world.shared, text,
                                request_id, behavior, str(log), str(release), json.dumps(READY)], env=dict(os.environ))
    process.hold_fd = hold_fd
    return process, log, release


def logged(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    parked = threading.Event()
    while time.monotonic() < end and not predicate():
        parked.wait(0.001)
    return predicate()


def test_owner_death_mid_dispatch_parks_uncertain_and_is_never_replayed(world):
    process, log, _ = owner(world, '@cursor @antigravity Say hi.', 'r-crash', 'die')
    assert process.wait(timeout=60) == 17 and [p for p, _ in logged(log)] == ['cursor']
    coordinator = world.new()  # a different, living process; the owner is dead
    coordinator.pending()
    coordinator.advance(world.shared, 'r-crash')
    result = coordinator.status(world.shared, 'r-crash')
    assert world.fake.submits == []
    assert 'uncertain' in {result['state']} | {child['state'] for child in result['children'].values()}
    assert 'r-crash' not in [row['request_id'] for row in coordinator.pending()]
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-crash', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-crash')
    assert world.fake.submits == []  # the same parent is never re-dispatched
    coordinator.create(world.shared, '@cursor @antigravity Say hi.', 'r-crash-rerun', consent=True, mode='cursor')
    coordinator.advance(world.shared, 'r-crash-rerun')
    assert sent(world) == ['antigravity', 'cursor']
    assert not {c for *_, c in world.fake.submits} & {c for _, c in logged(log)}


# Runner parallel suites can give the child a mismatched Team controller namespace.
@pytest.mark.skipif(
    bool(os.environ.get('PYTEST_XDIST_WORKER')),
    reason='not isolated under xdist on the runner since 2026-10-12; backlog root-cause package',
)
def test_a_live_other_process_keeps_its_dispatch(world):
    process, log, _release = owner(world, '@cursor @antigravity Say hi.', 'r-live', 'hold')
    try:
        assert wait_for(lambda: len(logged(log)) == 1 or process.poll() is not None, timeout=60), (
            'the owner never reached its first submit')
        assert len(logged(log)) == 1, 'the owner never reached its first submit'
        coordinator = world.new()
        coordinator.pending()
        coordinator.advance(world.shared, 'r-live')
        assert world.fake.submits == [] and coordinator.status(world.shared, 'r-live')['state'] != 'uncertain'
    finally:
        if process.hold_fd is not None:
            fcntl.flock(process.hold_fd, fcntl.LOCK_UN)
            os.close(process.hold_fd)
            process.hold_fd = None
        process.wait(timeout=60)
    assert process.returncode == 0 and sorted(p for p, _ in logged(log)) == ['antigravity', 'cursor']


def test_waiting_local_answer_resumes_after_owner_death_before_any_dispatch(world):
    typed = 'Tell all available teammates to say hi.'
    process, log, _ = owner(world, typed, 'r-wait', 'create_only')
    assert process.wait(timeout=60) == 0 and logged(log) == []
    coordinator = world.new()
    lead_id = coordinator.status(world.shared, 'r-wait')['lead_request_id']
    assert lead_id and 'r-wait' in [row['request_id'] for row in coordinator.pending()]
    world.store.answers[(world.shared, lead_id)] = answer(lead_id, 'app_cursor')
    coordinator.advance(world.shared, 'r-wait')
    assert [(provider, text) for _, provider, text, _ in world.fake.submits] == [('cursor', typed)]
