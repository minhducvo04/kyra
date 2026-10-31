"""Builder regressions for durable progress messages; no native calls."""
from copy import deepcopy

import pytest

from companion import team_chat
from companion.team_delegation import Coordinator
from tests.test_team_testloop_regressions import planned


def messages(c, room):
    return [m for m in c.store.get_room(room)['messages'] if m.get('kind') == 'step']


def test_steps_survive_failure_between_append_and_outbox_ack(tmp_path, monkeypatch):
    c, providers, room = planned(tmp_path)
    original = c.store.post_step
    crashed = False
    def crash(*args, **kwargs):
        nonlocal crashed
        result = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise OSError('Synthetic crash after message append')
        return result
    monkeypatch.setattr(c.store, 'post_step', crash)
    with pytest.raises(OSError):
        c.advance(room, 'parent')
    assert len(messages(c, room)) == 1
    monkeypatch.setattr(c.store, 'post_step', original)
    rebuilt = Coordinator(c.store, c.lead, roster=c.roster, submit=c.submit,
                          child_status=c.child_status, child_stop=c.child_stop)
    rebuilt.advance(room, 'parent')
    rows = messages(c, room)
    assert [m['step'] for m in rows] == ['assigned', 'dispatched', 'dispatched']
    assert len({m['id'] for m in rows}) == len(rows)
    assert len(providers.submits) == 3


def test_finished_parent_flushes_pending_terminal_steps_after_reconstruction(tmp_path, monkeypatch):
    c, providers, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        providers.states[row['children'][key]['id']].update(state='completed', text='First line\nPrivate second line')
    original = c.store.post_step
    def fail_ready(*args, **kwargs):
        if args[1] == 'ready':
            raise OSError('Posting temporarily unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(c.store, 'post_step', fail_ready)
    with pytest.raises(OSError):
        c.advance(room, 'parent')
    assert c.pending()[0]['request_id'] == 'parent'
    monkeypatch.setattr(c.store, 'post_step', original)
    rebuilt = Coordinator(c.store, c.lead, roster=c.roster, submit=c.submit,
                          child_status=c.child_status, child_stop=c.child_stop)
    assert rebuilt.advance(room, 'parent')['reason'] == 'ready_for_merge'
    assert [m['step'] for m in messages(c, room)] == ['assigned', 'dispatched', 'dispatched', 'returned', 'returned', 'ready']
    assert not any('Private second line' in m['text'] for m in messages(c, room))
    report = rebuilt.status(room, 'parent')['report']
    providers.states[report['id']].update(state='completed', text='Ready for review', verdict='pass')
    rebuilt.advance(room, 'parent')
    assert rebuilt.pending() == []


@pytest.mark.parametrize('change', [dict(step='made_up'), dict(author='codex'), dict(extra='hidden'),
                                   dict(text='two\nlines'), dict(text='x'*321), dict(run_id='not-hex')])
def test_step_schema_refuses_bad_fields(tmp_path, change):
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Fixture')['id']
    store.post_step(room, 'ready', 'Ready', event_key='event', parent_request_id='parent')
    data = store._read()
    bad = deepcopy(data)
    bad['rooms'][room]['messages'][-1].update(change)
    with pytest.raises(ValueError):
        team_chat._fixed_schema(bad)


def test_step_post_is_atomic_deduplicated_and_local_only_is_refused(tmp_path):
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Fixture')['id']
    first = store.post_step(room, 'ready', 'Ready', event_key='one')
    second = team_chat.TeamStore(tmp_path).post_step(room, 'ready', 'Ready', event_key='one')
    assert first == second and len(store.get_room(room)['messages']) == 1
    private = store.create_room('Private', local_only=True)['id']
    with pytest.raises(ValueError):
        store.post_step(private, 'ready', 'Ready', event_key='one')
    assert store.get_room(private)['messages'] == []


def test_completion_step_can_follow_room_input_limit_without_allowing_new_work(tmp_path):
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Full history')['id']
    with store.edit() as data:
        for _ in range(500):
            team_chat._message(data['rooms'][room], 'user', 'Existing fixture history')
    store.post_step(room, 'ready', 'Existing work completed', event_key='completion')
    assert len(store.get_room(room)['messages']) == 501
    with pytest.raises(ValueError, match='full'):
        store.submit(room, 'New work', 'new')


def test_parallel_posters_share_one_durable_message(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Concurrent posts')['id']
    def post(_):
        return team_chat.TeamStore(tmp_path).post_step(room, 'ready', 'Ready', event_key='same-event')
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.map(post, range(2))
    assert one == two and len(store.get_room(room)['messages']) == 1
