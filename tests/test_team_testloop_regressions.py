"""Builder checks for coding-package identity, terminal roll-up and room isolation."""
import pytest

from companion.team_delegation import Coordinator
from tests.test_team_delegation import Lead, Providers, Store, answer


class CodingProviders(Providers):
    def __init__(self):
        super().__init__({'planner': '', 'worker': '', 'reviewer': ''})
        self.parents = []

    def submit(self, room, provider, text, request, *, parent_request_id=None):
        self.parents.append(parent_request_id)
        return super().submit(room, provider, text, request)

    def child_stop(self, room, provider, ident):
        super().child_stop(room, provider, ident)
        self.states[ident]['state'] = 'stopped'


def planned(tmp_path, count=2):
    store = Store(tmp_path)
    lead, providers = Lead(store), CodingProviders()
    coordinator = Coordinator(store, lead, roster=providers.roster, submit=providers.submit,
                              child_status=providers.child_status, child_stop=providers.child_stop)
    room = store.create_room('Parallel packages')['id']
    row = coordinator.create(room, 'Implement independent packages', 'parent', consent=True, plan_consent=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    row = coordinator.advance(room, 'parent')
    providers.states[row['children']['planner']['id']].update(state='completed', plan=[
        dict(provider='codex', title=f'Package {i}', instruction=f'Build package {i}') for i in range(count)])
    return coordinator, providers, room


def test_repeated_codex_packages_dispatch_once_and_roll_up(tmp_path):
    coordinator, providers, room = planned(tmp_path, 3)
    row = coordinator.advance(room, 'parent')
    assert set(row['children']) == {'planner', 'worker:1', 'worker:2', 'worker:3'}
    assert providers.parents == [None, 'parent', 'parent', 'parent']
    assert [call[1] for call in providers.submits] == ['planner', 'worker', 'worker', 'worker']
    coordinator.advance(room, 'parent')
    assert len(providers.submits) == 4
    for key in ('worker:1', 'worker:2', 'worker:3'):
        providers.states[row['children'][key]['id']].update(state='completed', text='Reviewed package')
    final = coordinator.advance(room, 'parent')
    assert (final['state'], final['reason']) == ('completed', 'ready_for_merge')
    assert [s[1] for s in providers.submits] == ['planner', 'worker', 'worker', 'worker', 'reviewer']


def test_needs_input_names_child_and_parent_stop_still_stops_siblings(tmp_path):
    coordinator, providers, room = planned(tmp_path)
    row = coordinator.advance(room, 'parent')
    providers.states[row['children']['worker:1']['id']].update(state='needs_input', reason='Choose product scope', requires_owner_decision=True)
    row = coordinator.advance(room, 'parent')
    assert row['state'] == 'needs_input' and 'worker:1' in row['reason'] and 'Package 0' in row['reason']
    coordinator.stop(room, 'parent')
    coordinator.stop(room, 'parent')
    assert [ident for _, _, ident in providers.stops] == [row['children']['worker:2']['id']]
    assert len(providers.submits) == 3


def test_room_parent_is_bounded_and_duplicate_bound(tmp_path):
    from companion.team_chat import TeamStore
    store = TeamStore(tmp_path)
    room = store.create_room('Siblings')['id']
    for i in range(3):
        store.submit(room, f'Task {i}', f'child{i}', execution_mode='worker', parent_request_id='parent')
    for parent in ('parent', 'other', None):
        with pytest.raises(ValueError, match='already has work'):
            store.submit(room, 'Another task', 'extra', execution_mode='worker', parent_request_id=parent)
    with pytest.raises(ValueError):
        store.submit(room, 'Task 0', 'child0', execution_mode='worker', parent_request_id='other')
    assert len(store.get_room(room)['runs']) == 3


def test_busy_room_retries_only_after_definite_no_send(tmp_path):
    coordinator, providers, room = planned(tmp_path)
    original = coordinator.submit
    coordinator.submit = lambda *args, **kw: dict(id=None, state='blocked', reason='native_room_busy')
    waiting = coordinator.advance(room, 'parent')
    assert waiting['state'] == 'running' and set(waiting['children']) == {'planner'}
    coordinator.submit = original
    resumed = coordinator.advance(room, 'parent')
    assert len(resumed['children']) == 3 and len(providers.submits) == 3


def test_uncertain_coding_child_never_dispatches_remaining_packages(tmp_path):
    coordinator, providers, room = planned(tmp_path, 3)
    original = coordinator.submit
    def uncertain(*args, **kw):
        value = original(*args, **kw)
        return {**value, 'state': 'uncertain'}
    coordinator.submit = uncertain
    row = coordinator.advance(room, 'parent')
    for _ in range(3):
        row = coordinator.advance(room, 'parent')
    assert row['state'] == 'uncertain'
    assert len(providers.submits) == 2


def test_coding_refusal_during_dispatch_sends_no_later_packages(tmp_path):
    coordinator, providers, room = planned(tmp_path, 3)
    original = coordinator.submit
    def refused(*args, **kw):
        value = original(*args, **kw)
        return {**value, 'state': 'needs_input', 'reason': 'scope requires owner', 'requires_owner_decision': True}
    coordinator.submit = refused
    row = coordinator.advance(room, 'parent')
    assert row['state'] == 'needs_input'
    assert [s[1] for s in providers.submits] == ['planner', 'worker', 'reviewer']


def test_unresolved_sibling_still_fences_room(tmp_path):
    from companion.team_chat import TeamStore
    store = TeamStore(tmp_path)
    room = store.create_room('Unresolved sibling')['id']
    ident = store.submit(room, 'Task', 'child', execution_mode='worker', parent_request_id='parent')['run_id']
    with store.edit() as data:
        run = next(r for r in data['rooms'][room]['runs'] if r['id'] == ident)
        run.update(status='uncertain', phase='terminal')
    with pytest.raises(ValueError, match='already has work'):
        store.submit(room, 'Other task', 'second', execution_mode='worker', parent_request_id='parent')


def test_stop_during_coding_submit_stops_late_identity_and_sends_no_sibling(tmp_path):
    coordinator, providers, room = planned(tmp_path)
    providers.on_submit = lambda role: coordinator.stop(room, 'parent')
    row = coordinator.advance(room, 'parent')
    row = coordinator.status(room, 'parent')
    assert row['state'] == 'stopped' and len(providers.submits) == 2
    assert len(providers.stops) == 1 and providers.stops[0][1] == 'worker'


def test_uncertain_coding_child_remains_visible_while_sibling_finishes(tmp_path):
    coordinator, providers, room = planned(tmp_path)
    row = coordinator.advance(room, 'parent')
    providers.states[row['children']['worker:1']['id']].update(state='uncertain', reason='delivery unknown')
    assert coordinator.advance(room, 'parent')['state'] == 'uncertain'
    assert len(coordinator.pending()) == 1
    providers.states[row['children']['worker:2']['id']].update(state='completed', text='Reviewed')
    settled = coordinator.advance(room, 'parent')
    assert settled['state'] == 'uncertain'
    assert settled['children']['worker:2']['state'] == 'completed'
    assert coordinator.pending() == [] and len(providers.submits) == 3
