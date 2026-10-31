"""Builder checks for final-report schema, crash recovery and cancellation."""
from copy import deepcopy

import pytest

from companion import team_chat
from companion.team_delegation import Coordinator
from tests.test_team_testloop_regressions import planned


def cards(c, room):
    return [m for m in c.store.get_room(room)['messages'] if m.get('kind') == 'report']


def finished(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Built and reviewed\nNOT_FOR_REPORT')
    c.advance(room, 'parent')
    return c, p, room


def rebuild(c):
    return Coordinator(c.store, c.lead, roster=c.roster, submit=c.submit,
                       child_status=c.child_status, child_stop=c.child_stop)


def test_report_post_concurrent_deduplicated_namespaced_and_local_only(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Report fixture')['id']
    store.post_step(room, 'ready', 'Ready', event_key='one')
    def post(_):
        return team_chat.TeamStore(tmp_path).post_report(room, 'Ready\n- Verified\nAction: review.', event_key='one')
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.map(post, range(2))
    assert a == b and len(store.get_room(room)['messages']) == 2
    private = store.create_room('Private', local_only=True)['id']
    with pytest.raises(ValueError):
        store.post_report(private, 'Ready', event_key='one')


@pytest.mark.parametrize('change', [dict(text=''), dict(text='x'*4001), dict(text='a\n'*30+'a'),
                                   dict(text='tab\tvalue'), dict(text='carriage\rreturn'),
                                   dict(author='codex'), dict(extra='secret'), dict(run_id='bad')])
def test_report_strict_persisted_schema(tmp_path, change):
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Report fixture')['id']
    store.post_report(room, 'Ready', event_key='one', parent_request_id='parent')
    data = deepcopy(store._read())
    data['rooms'][room]['messages'][-1].update(change)
    with pytest.raises(ValueError):
        team_chat._fixed_schema(data)


def test_report_append_crash_recovers_without_resend(tmp_path, monkeypatch):
    c, p, room = finished(tmp_path)
    assert p.submits[-1][1] == 'reviewer'
    assert 'NOT_FOR_REPORT' not in p.submits[-1][2]
    ident = next(k for k, v in p.states.items() if v['state'] == 'queued')
    p.states[ident].update(state='completed', text='Ready\n- Two parts verified\nReview before merging.', verdict='pass')
    original = c.store.post_report
    def crash(*a, **kw):
        original(*a, **kw)
        raise OSError('After append')
    monkeypatch.setattr(c.store, 'post_report', crash)
    with pytest.raises(OSError):
        c.advance(room, 'parent')
    assert len(cards(c, room)) == 1
    monkeypatch.setattr(c.store, 'post_report', original)
    before = len(p.submits)
    c = rebuild(c)
    for _ in range(3):
        assert c.advance(room, 'parent')['reason'] == 'ready_for_merge'
    assert len(cards(c, room)) == 1 and len(p.submits) == before
    assert c.pending() == []


def test_uncertain_report_send_is_not_replayed(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Done')
    def lost(*a, **kw):
        p.submit(*a, **kw)
        raise OSError('Lost receipt')
    c.submit = lost
    c.advance(room, 'parent')
    before = len(p.submits)
    c = rebuild(c)
    for _ in range(3):
        assert c.advance(room, 'parent')['reason'] == 'ready_for_merge'
    assert len(p.submits) == before == 4 and not cards(c, room)


def test_stop_cancels_report_and_drops_late_result(tmp_path):
    c, p, room = finished(tmp_path)
    ident = next(k for k, v in p.states.items() if v['state'] == 'queued')
    c.stop(room, 'parent')
    assert (room, 'reviewer', ident) in p.stops
    p.states[ident].update(state='completed', text='Late report', verdict='pass')
    c.advance(room, 'parent')
    assert not cards(c, room)
    assert c.pending() == []


def test_report_busy_retries_definite_no_send_only(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Done')
    c.submit = lambda *a, **kw: dict(id=None, state='blocked', reason='native_room_busy')
    assert c.advance(room, 'parent')['report']['state'] == 'ready'
    c.submit = p.submit
    row = c.advance(room, 'parent')
    assert row['report']['state'] == 'queued' and len(p.submits) == 4


def test_stop_during_report_submit_cancels_late_identity(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Done')
    p.on_submit = lambda role: c.stop(room, 'parent')
    c.advance(room, 'parent')
    c.advance(room, 'parent')
    assert p.stops[-1][1] == 'reviewer'
    assert not cards(c, room) and c.pending() == []


def test_report_capacity_preserves_completion_reserve(tmp_path):
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Full history')['id']
    with store.edit() as data:
        for _ in range(500):
            team_chat._message(data['rooms'][room], 'user', 'Fixture history')
    store.post_report(room, 'Existing work completed', event_key='completion')
    assert len(store.get_room(room)['messages']) == 501
    with pytest.raises(ValueError, match='full'):
        store.submit(room, 'New work', 'new')


def test_stop_in_gap_before_report_creation_prevents_dispatch(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Done')
    c._advance(room, 'parent')  # persisted completion, before the report phase
    c.stop(room, 'parent')
    c.advance(room, 'parent')
    assert not [s for s in p.submits if s[1] == 'reviewer']
    assert not cards(c, room) and c.pending() == []


@pytest.mark.parametrize('verdict', ['pass', 'changes_requested'])
def test_legacy_mixed_plan_reuses_review_and_recovers_attention_card(tmp_path, monkeypatch, verdict):
    from tests.test_team_all_routes import setup
    from tests.test_team_delegation import answer
    store, room, p, c = setup(tmp_path)
    row = c.create(room, 'Bounded request\nExcluded later detail', 'planned', consent=True, plan_consent=True)
    with c._edit(room, 'planned') as saved:
        saved.pop('retry_policy', None)  # Existing runs keep their original recovery contract.
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    row = c.advance(room, 'planned')
    p.states[row['children']['planner']['id']].update(state='completed', plan=[
        dict(provider='cursor', title='Fixture title', instruction='Instruction not for reporter')])
    row = c.advance(room, 'planned')
    p.states[row['children']['cursor']['id']].update(state='completed', text='First result\nExcluded later output')
    row = c.advance(room, 'planned')
    prompt = p.submits[-1][2]
    assert all(t in prompt for t in ('Bounded request', 'Fixture title', 'First result'))
    assert all(t not in prompt for t in ('Excluded', 'Instruction not for reporter'))
    p.states[row['children']['reviewer']['id']].update(
        state='completed' if verdict == 'pass' else 'blocked', text='Reviewed\n- Follow up', verdict=verdict)
    original = store.post_report
    monkeypatch.setattr(store, 'post_report', lambda *a, **kw: (_ for _ in ()).throw(OSError('Disk unavailable')))
    with pytest.raises(OSError):
        c.advance(room, 'planned')
    assert c.pending()  # even an attention card must recover after a publication failure
    monkeypatch.setattr(store, 'post_report', original)
    c = rebuild(c)
    row = c.advance(room, 'planned')
    assert row['state'] == ('completed' if verdict == 'pass' else 'blocked')
    assert len(cards(c, room)) == 1
    assert len([s for s in p.submits if s[1] == 'reviewer']) == 1
    assert c.pending() == []


def test_upgrade_does_not_dispatch_reports_for_historical_completed_parents(tmp_path):
    c, p, room = planned(tmp_path)
    row = c.advance(room, 'parent')
    for key in ('worker:1', 'worker:2'):
        p.states[row['children'][key]['id']].update(state='completed', text='Done')
    c._advance(room, 'parent')
    with c._edit(room, 'parent') as saved:
        saved.pop('report_version', None)  # record made before the report-card feature
    c = rebuild(c)
    for _ in range(3):
        c.advance(room, 'parent')
    assert not [s for s in p.submits if s[1] == 'reviewer']
    assert not cards(c, room) and c.pending() == []
