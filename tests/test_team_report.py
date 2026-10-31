"""Claude acceptance for the planner's final report card (phase 3 package 4, 2026-09-29), written against Codex's
contract in a private design note while Codex builds it.

When a planner-led request finishes (ready_for_merge, or ending with a question for the owner), the planner writes one short
report that Kyra posts once as a kind "report" message from Claude. The web shows it as a card. An all-coding plan gets
a separate reviewer report task after its children settle, carrying the request's first line, the task titles, and
each child's key, state and first summary line, never full outputs. The parent keeps its outcome, and nothing merges.
Stop and direct tags produce no report. A mixed plan reuses its existing reviewer summary as the card.
"""
import pytest

from companion.team_chat import TeamStore
from tests.test_team_testloop import TWO, coding_children, planned

REPORT = 'Both parts are built and reviewed.\n- worker:1 built part 1.\n- worker:2 built part 2.\nNothing needs the owner.'


def reports(coordinator, room):
    return [message for message in coordinator.store.get_room(room)['messages'] if message.get('kind') == 'report']


def reviewer_submits(providers):
    return [submit for submit in providers.submits if submit[1] == 'reviewer']


def settle(coordinator, room, times=3):
    for _ in range(times):
        coordinator.advance(room, 'parent-1')


def finish_children(providers, ids):
    for key, ident in ids.items():
        providers.states[ident].update(state='completed', text=f'{key} built its part.\nFULL-OUTPUT-SECOND-LINE')


def test_a_finished_coding_plan_gets_one_report_task_with_bounded_input(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    finish_children(providers, coding_children(coordinator, room))
    settle(coordinator, room)
    tasks = reviewer_submits(providers)
    assert len(tasks) == 1
    text = tasks[0][2]
    assert 'worker:1 built its part.' in text and 'worker:2 built its part.' in text
    assert 'Part 1' in text and 'Part 2' in text  # the plan's task titles
    assert 'FULL-OUTPUT-SECOND-LINE' not in text  # first summary lines only
    row = coordinator.status(room, 'parent-1')
    assert row['state'] == 'completed' and 'ready_for_merge' in row['reason']  # the report never changes the outcome


def test_the_planner_report_is_posted_once_as_a_claude_report_card(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    finish_children(providers, coding_children(coordinator, room))
    settle(coordinator, room)
    ident = coordinator.status(room, 'parent-1')['report']['id']  # kept apart from the coding children
    providers.states[ident].update(state='completed', text=REPORT, verdict='pass')
    settle(coordinator, room)
    cards = reports(coordinator, room)
    assert len(cards) == 1 and cards[0]['author'] == 'claude' and cards[0]['text'] == REPORT
    settle(coordinator, room)
    assert len(reports(coordinator, room)) == 1
    before = len(providers.submits)
    settle(coordinator, room)
    assert len(providers.submits) == before  # the report dispatches nothing more and merges nothing


def test_stop_produces_no_report(tmp_path):
    room, providers, coordinator, _ = planned(tmp_path, TWO)
    ids = coding_children(coordinator, room)
    coordinator.stop(room, 'parent-1')
    for ident in ids.values():
        providers.states[ident].update(state='stopped')
    settle(coordinator, room)
    assert reviewer_submits(providers) == [] and reports(coordinator, room) == []


def test_the_store_posts_a_report_once_and_refuses_malformed_or_local_only_reports(tmp_path):
    store = TeamStore(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    first = store.post_report(room, REPORT, event_key='parent-1:report')
    again = store.post_report(room, REPORT, event_key='parent-1:report')
    assert first['id'] == again['id'] and first['kind'] == 'report' and first['author'] == 'claude'
    assert len([m for m in store.get_room(room)['messages'] if m.get('kind') == 'report']) == 1
    for index, text in enumerate(('', 'x' * 4001, '\n'.join(['line'] * 31), 'bad \x07 bell')):
        with pytest.raises(ValueError):
            store.post_report(room, text, event_key=f'bad-{index}')
    local = store.create_room('Local fixture', local_only=True)['id']
    with pytest.raises(ValueError):
        store.post_report(local, REPORT, event_key='local-1')
