"""Claude acceptance for the Team test loop (phase 3 package 2, 2026-09-29), written against Codex's contract in
a private design note while Codex builds it.

A plan's Codex tasks become coding children with stable keys worker:1 to worker:3. They start in the same cycle and
each runs the existing worker build, same-revision review and two-repair loop on its own lane. The parent is ready
for the merge gate only when every child passed, asks the owner naming a stuck child, and stops every child on Stop.
Nothing merges here. Coordinator fakes come from test_team_delegation; the room case uses real lanes from
test_team_lanes.
"""
import importlib

import pytest

from companion.team_delegation import Coordinator
from tests.test_team_delegation import Lead, Providers, Store, answer
from tests.test_team_lanes import bind_lanes
from tests.test_team_lanes import run as run_record

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

TWO = [dict(provider='codex', title=f'Part {i}', instruction=f'Build fixture part {i}.') for i in (1, 2)]


class CodingProviders(Providers):
    """Also records which parent each coding child belongs to."""

    def __init__(self, roster):
        super().__init__(roster)
        self.parents = {}

    def submit(self, room_id, provider, text, child_request_id, **extra):
        child = super().submit(room_id, provider, text, child_request_id)
        self.parents[child['id']] = extra.get('parent_request_id')
        return child


def planned(tmp_path, tasks):
    """Run a team_plan request until its plan is accepted; return the worker-submit count after each advance."""
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    providers = CodingProviders({p: '' for p in ('codex', 'cursor', 'antigravity', 'planner', 'reviewer', 'worker')})
    coordinator = Coordinator(store, Lead(store), roster=providers.roster, submit=providers.submit,
                              child_status=providers.child_status, child_stop=providers.child_stop)
    row = coordinator.create(room, 'Build two small fixture features', 'parent-1', consent=True, plan_consent=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    coordinator.advance(room, 'parent-1')
    planner = coordinator.status(room, 'parent-1')['children']['planner']['id']
    providers.states[planner].update(state='completed', text='Plan ready', plan=tasks)
    counts = []
    for _ in range(3):
        coordinator.advance(room, 'parent-1')
        counts.append(sum(1 for submit in providers.submits if submit[1] == 'worker'))
    return room, providers, coordinator, counts


def coding_children(coordinator, room):
    children = coordinator.status(room, 'parent-1')['children']
    return {key: children[key]['id'] for key in ('worker:1', 'worker:2')}


def test_two_codex_tasks_start_both_coding_children_in_one_cycle(tmp_path):
    room, providers, coordinator, counts = planned(tmp_path, TWO)
    assert next(count for count in counts if count) == 2  # both children appear in the same advance
    ids = coding_children(coordinator, room)
    assert {providers.parents[ident] for ident in ids.values()} == {'parent-1'}


def test_the_parent_is_ready_for_merge_only_when_every_coding_child_passed(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    providers.states[ids['worker:1']].update(state='completed', text='Part 1 built and reviewed.')
    coordinator.advance(room, 'parent-1')
    assert coordinator.status(room, 'parent-1')['state'] != 'completed'
    providers.states[ids['worker:2']].update(state='completed', text='Part 2 built and reviewed.')
    coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert row['state'] == 'completed' and 'ready_for_merge' in row['reason']
    before = len(providers.submits)
    coordinator.advance(room, 'parent-1')
    assert len(providers.submits) == before  # readiness sends nothing more, and nothing merges


def test_a_stuck_coding_child_makes_the_parent_ask_owner_and_name_it(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    providers.states[ids['worker:2']].update(state='needs_input', reason='Choose the requested product scope.', requires_owner_decision=True)
    coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert row['state'] == 'needs_input'
    assert 'worker:2' in row['reason'] or 'Part 2' in row['reason']
    before = len(providers.submits)
    coordinator.advance(room, 'parent-1')
    assert len(providers.submits) == before  # no new children once the parent asks


def test_stop_on_the_parent_stops_every_coding_child_and_resends_nothing(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = set(coding_children(coordinator, room).values())
    coordinator.stop(room, 'parent-1')
    assert ids <= {ident for _, _, ident in providers.stops}
    assert coordinator.status(room, 'parent-1')['state'] != 'stopped'  # not until every child confirms it stopped
    before = len(providers.submits)
    coordinator.stop(room, 'parent-1')
    for ident in ids:
        providers.states[ident].update(state='stopped')
    coordinator.advance(room, 'parent-1')
    assert len(providers.submits) == before
    assert coordinator.status(room, 'parent-1')['state'] == 'stopped'


@pytest.fixture
def team():
    return importlib.import_module('companion.team_chat')


def test_coding_children_of_one_parent_share_a_room_each_on_its_own_lane(team, tmp_path):
    store, service, transport, _, _ = bind_lanes(team, tmp_path, 2)
    room = store.create_room('Fixture')['id']

    def child(index):
        return store.submit(room, f'Build fixture part {index}.', f'child-{index}', share_with_team=True,
                            execution_mode='worker', parent_request_id='parent-1')['run_id']

    kids = [child(1), child(2)]
    for kid in kids:
        service.step(kid)
    assert sorted(send['run_id'] for send in transport.sends) == sorted(kids)
    assert len({run_record(store, room, kid)['lane']['workspace'] for kid in kids}) == 2
    child(3)  # a third sibling is admitted and waits for a lane
    with pytest.raises(ValueError):
        child(4)  # at most three active siblings
    with pytest.raises(ValueError):
        store.submit(room, 'An unrelated request.', 'other', share_with_team=True)
