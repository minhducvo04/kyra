"""All-team fanout and derived plans must keep one durable task per actual worker."""
from companion.team_delegation import Coordinator
from tests.test_team_delegation import Lead, Providers, Store, answer


def setup(tmp_path):
    store=Store(tmp_path/'team')
    room=store.create_room('Fixture')['id']
    lead=Lead(store)
    providers=Providers({p:'' for p in ('local','claude','codex','cursor','antigravity','planner','reviewer','worker')})
    def submit(room, role, text, request, **kwargs):
        if role == 'worker' and kwargs:
            assert kwargs['parent_request_id'] in {'planned', 'code'}
        return providers.submit(room, role, text, request)
    coordinator=Coordinator(store,lead,roster=providers.roster,submit=submit,
                            child_status=providers.child_status,child_stop=providers.child_stop)
    return store,room,providers,coordinator


def test_local_tag_and_all_route_include_every_teammate(tmp_path):
    store,room,p,c=setup(tmp_path)
    c.create(room,'@Kyra Hello','local',mode='local',consent=True)
    c.advance(room,'local')
    assert p.submits[0][1]=='local'
    row=c.create(room,'Ask every teammate to reply','all',mode='all',consent=True)
    store.answers[room,row['lead_request_id']]=answer(row['lead_request_id'],'app_all')
    c.advance(room,'all')
    assert set(c.status(room,'all')['targets'])=={'local','claude','codex','cursor','antigravity'}
    assert sum(x[1] in {'claude','codex'} for x in p.submits)==1
    first=c.status(room,'all')['children']['claude']['id']
    p.states[first].update(state='completed',text='Hello')
    c.advance(room,'all')
    assert sum(x[1]=='codex' for x in p.submits)==1


def test_planner_fanout_uses_shared_consent_and_returns_review(tmp_path):
    store,room,p,c=setup(tmp_path)
    row=c.create(room,'Plan a test','without',consent=True)
    store.answers[room,row['lead_request_id']]=answer(row['lead_request_id'],'team_plan')
    assert 'planner' in c.advance(room,'without')['children']
    row=c.create(room,'Plan checks by all workers','planned',consent=True,plan_consent=True)
    store.answers[room,row['lead_request_id']]=answer(row['lead_request_id'],'team_plan')
    c.advance(room,'planned')
    ident=c.status(room,'planned')['children']['planner']['id']
    plan=[dict(provider=k,title=k,instruction=f'Compute 6*7, {k}') for k in ('codex','cursor','antigravity')]
    p.states[ident].update(state='completed',text='Plan ready',plan=plan)
    c.advance(room,'planned')
    c.advance(room,'planned')
    row=c.status(room,'planned')
    for role in ('codex','cursor','antigravity'):
        child=row['children']['worker:1' if role == 'codex' else role]
        p.states[child['id']].update(state='completed',text=f'{role}: 42')
    c.advance(room,'planned')
    c.advance(room,'planned')
    row=c.status(room,'planned')
    review=next(x for x in p.submits if x[1]=='reviewer')
    assert all(f'{k}: 42' in review[2] for k in ('codex','cursor','antigravity'))
    p.states[row['children']['reviewer']['id']].update(state='completed',text='All checked',verdict='pass')
    assert c.advance(room,'planned')['state']=='completed'
    count=len(p.submits)
    c.advance(room,'planned')
    assert len(p.submits)==count


def test_stop_between_completed_phases_never_dispatches_next_task(tmp_path):
    store, room, p, c = setup(tmp_path)
    for phase in ('planning', 'workers', 'reviewing'):
        c.create(room, '@cursor fixture', phase, consent=True, plan_consent=True)
        with c._edit(room, phase) as row:
            row.update(state='completed', workflow=phase)
        assert c.status(room, phase)['state'] == 'running'
        assert c.stop(room, phase)['state'] == 'stopped'
        assert c.advance(room, phase)['state'] == 'stopped'
    assert p.submits == []


def test_worker_route_preserves_implementation_mode(tmp_path):
    store, room, p, c = setup(tmp_path)
    p.roster_value['worker'] = ''
    row = c.create(room, 'Implement the requested change', 'code', consent=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'worker_task')
    c.advance(room, 'code')
    planner = c.status(room, 'code')['children']['planner']
    p.states[planner['id']].update(state='completed', planner_kind='task', plan=[
        dict(provider='codex', title='Code', instruction='Implement.', order=1, mode='code')])
    c.advance(room, 'code')
    assert p.submits[-1][1] == 'worker'
