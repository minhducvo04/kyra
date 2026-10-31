"""Claude acceptance for server step messages (phase 3 package 3, 2026-09-29), written against Codex's contract in
a private design note while Codex builds it.

The owner's mandatory rule: every flow step shows as one short line in the Kyra Team chat. The server posts kind "step"
messages (author kyra, one line of at most 320 characters) through TeamStore.post_step, once per event key: assigned,
dispatched, returned, asked, ready, stopped. Stop is posted only after the children confirm. Local-only rooms and
malformed steps are refused. Coordinator fakes come from test_team_delegation via test_team_testloop.
"""
import pytest

from companion.team_chat import TeamStore
from tests.test_team_testloop import TWO, coding_children, planned


def lines(coordinator, room):
    return [(message['step'], message['text']) for message in coordinator.store.get_room(room)['messages']
            if message.get('kind') == 'step']


def advance(coordinator, room, times=3):
    for _ in range(times):
        coordinator.advance(room, 'parent-1')


def test_a_two_package_plan_posts_each_step_once_and_in_order(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    for key in ('worker:1', 'worker:2'):
        providers.states[ids[key]].update(state='completed', text=f'{key} built it.\nPrivate detail line.')
    advance(coordinator, room)
    steps = [step for step, _ in lines(coordinator, room)]
    assert steps[0] == 'assigned' and steps[-1] == 'ready'
    assert sorted(steps) == sorted(['assigned', 'dispatched', 'dispatched', 'returned', 'returned', 'ready'])
    last_dispatch = max(i for i, step in enumerate(steps) if step == 'dispatched')
    first_return = min(i for i, step in enumerate(steps) if step == 'returned')
    assert last_dispatch < first_return
    texts = [text for _, text in lines(coordinator, room)]
    assert all('\n' not in text and 0 < len(text) <= 320 for text in texts)
    assert not any('Private detail line' in text for text in texts)  # only the first summary line is shown
    advance(coordinator, room)
    assert len(lines(coordinator, room)) == len(steps)  # extra advances post nothing new


def test_a_stuck_child_posts_an_asked_line_that_names_it(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    providers.states[ids['worker:2']].update(state='needs_input', reason='Choose the fixture behavior.',
                                           requires_owner_decision=True)
    advance(coordinator, room)
    asked = [text for step, text in lines(coordinator, room) if step == 'asked']
    assert asked and all('worker:2' in text or 'Part 2' in text for text in asked)
    advance(coordinator, room)
    assert [text for step, text in lines(coordinator, room) if step == 'asked'] == asked


def test_stop_posts_one_stopped_line_only_after_the_children_confirm(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    coordinator.stop(room, 'parent-1')
    assert 'stopped' not in [step for step, _ in lines(coordinator, room)]
    for ident in ids.values():
        providers.states[ident].update(state='stopped')
    advance(coordinator, room)
    assert [step for step, _ in lines(coordinator, room)].count('stopped') == 1


def test_the_store_posts_a_step_once_and_refuses_malformed_or_local_only_steps(tmp_path):
    store = TeamStore(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    first = store.post_step(room, 'dispatched', 'Claude → Codex (worker:1): Build part 1', event_key='parent-1:worker:1')
    again = store.post_step(room, 'dispatched', 'Claude → Codex (worker:1): Build part 1', event_key='parent-1:worker:1')
    assert first['id'] == again['id']
    assert len([m for m in store.get_room(room)['messages'] if m.get('kind') == 'step']) == 1
    for index, (step, text) in enumerate((('celebrated', 'An unknown step.'), ('ready', 'Two\nlines.'),
                                          ('ready', 'x' * 321), ('ready', ''))):
        with pytest.raises(ValueError):
            store.post_step(room, step, text, event_key=f'bad-{index}')
    local = store.create_room('Local fixture', local_only=True)['id']
    with pytest.raises(ValueError):
        store.post_step(local, 'ready', 'Ready.', event_key='local-1')
