"""P4 journey acceptance for the Team flow fix (a private plan), using the integrated P1/P2/P6 contracts.

What the owner wants from the Team chat, through the most public seams that exist today:
1. One "Kyra & team" message asking to count 1 to 5 goes to the Claude planner; its ordered plan runs Kyra 1, Claude 2,
   Codex 3, Cursor 4, Antigravity 5, each step started only after the previous one finished.
2. "@all hi" goes directly to connected members: one reply each, one composer post and no planner run.
3. A plain question is answered by the planner itself: no build run and no review run.
4. A thread reply to one member reaches only that member, carries the thread history and the Team handbook, and the
   answer lands in the same thread.

Seams. Journeys 1 and 3 drive `team_delegation.Coordinator(store, lead, roster=, submit=, child_status=, child_stop=)`
exactly as the composer does (`create(room, text, request_id, mode='auto', consent=True, plan_consent=True, interview=False)`, then
`advance` until settled), with fakes at the provider seam. Journey 2 drives the real `team_http.get_delegation` wiring
over a real TeamStore/TeamService (fake native transport), a real LeadService (fake local model) and Cursor/Antigravity
runs finished by a fake adapter. Journey 4 drives `team_threads.Store.reply` + `team_replies.pump` + `TeamService.step`.
No network, model, app or native chat runs.

Provider fake contracts matching the real HTTP adapter and native result schema:
- PLANNER. The planner is the child submitted with role 'planner'. Its completed status is the existing child dict
  plus `planner_kind`: 'answer' (`text` is the answer, posted as is), 'task' or 'plan'. A plan is `plan`: a list of
  {provider, title, instruction, order, mode} with provider in kyra|claude|codex|cursor|antigravity; equal `order` values
  may run in parallel, a higher order starts only after every lower one finished.
- MEMBERS. Kyra's step may be submitted as role 'local' or 'kyra'; Codex's text step as 'codex' or 'worker'. A member
  posts its own reply when it finishes (as the real app adapters and native runs do), so the fake appends to
  `posted` at completion. A later step's prompt contains the earlier steps' outputs.
- ROOM. The owner's composer post is the only `author == 'user'` row in the room projection (TeamStore messages plus
  `team_connected.project_room`); stage prompts are stored with author 'system' or not shown at all.
- HANDBOOK. `docs/team-handbook.md` (P3/P5) is included in a thread reply's assignment.
"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import team_chat
from companion.team_threads import Store as Threads
from tests.test_team_chat_acceptance import bound, respond
from tests.test_team_lead import PROJECT, FakeBackend, Sources, model_json, worker_result
from tests.test_team_native_modes import RevisionTransport

ROOT = Path(__file__).resolve().parent.parent
MEMBERS = ('kyra', 'claude', 'codex', 'cursor', 'antigravity')
ROLE_TO_MEMBER = {'local': 'kyra', 'kyra': 'kyra', 'claude': 'claude', 'codex': 'codex', 'worker': 'codex',
                  'cursor': 'cursor', 'antigravity': 'antigravity'}
SETTLED = {'completed', 'blocked', 'failed', 'needs_input', 'stopped', 'uncertain', 'cancelled'}
COUNT = 'count 1 to 5 in order: Kyra 1, Claude 2, Codex 3, Cursor 4, Antigravity 5'
QUESTION = 'what does lane 0 do?'
LANE_ANSWER = 'Lane 0 is the Codex builder lane for team_chat.py.'
NAMES = {'kyra': 'Kyra', 'claude': 'Claude', 'codex': 'Codex', 'cursor': 'Cursor', 'antigravity': 'Antigravity'}


# Journeys 1 and 3: the Coordinator with a fake planner and fake members

class Room:
    """A real TeamStore for the room; a local triage answer exists only if the test gives one."""

    def __init__(self, root):
        self._team = team_chat.TeamStore(root)
        self.answers = {}

    def __getattr__(self, name):
        return getattr(self._team, name)

    def get_answer(self, room_id, request_id):
        if (room_id, request_id) not in self.answers:
            raise LookupError('Answer not found')
        return dict(self.answers[(room_id, request_id)])


class Triage:
    """Kyra's local triage. In "Kyra & team" it must not be consulted; if it is, it never answers."""

    def __init__(self):
        self.calls = []

    def submit(self, room_id, text, request_id):
        self.calls.append(text)
        return {'request_id': request_id, 'status': 'queued'}

    def stop(self, room_id, request_id):
        return None


class Team:
    """Planner and members at the provider seam. A child finishes on its first status read and posts its reply."""

    def __init__(self, planner):
        self.planner = planner
        self.submits, self.posted, self.children = [], [], {}

    def roster(self):
        return {name: '' for name in ('local', 'claude', 'codex', 'cursor', 'antigravity',
                                      'planner', 'reviewer', 'worker')}

    def submit(self, room_id, role, text, request_id, **kwargs):
        running = sorted(child['member'] for child in self.children.values()
                         if child['state'] == 'running' and child['member'] != 'planner')
        ident = f'child-{len(self.submits) + 1}'
        member = 'planner' if role == 'planner' else ROLE_TO_MEMBER.get(role, role)
        self.submits.append(SimpleNamespace(role=role, member=member, text=text, running=running))
        self.children[ident] = dict(member=member, role=role, state='running', prompt=text)
        return {'id': ident, 'state': 'queued', 'reason': ''}

    def child_status(self, room_id, role, ident):
        child = self.children[ident]
        if child['state'] == 'running':
            child['state'] = 'completed'
            if child['member'] == 'planner':
                child['result'] = self.planner(child['prompt'])
            else:
                child['result'] = {'text': f"{NAMES.get(child['member'], child['member'])} says "
                                           f"{MEMBERS.index(child['member']) + 1 if child['member'] in MEMBERS else 0}"}
                self.posted.append((child['member'], child['result']['text']))
        return {'id': ident, 'state': child['state'], 'reason': '', 'text': '', **child.get('result', {})}

    def child_stop(self, room_id, role, ident):
        self.children[ident]['state'] = 'stopped'

    def members(self):
        return [s.member for s in self.submits if s.member not in {'planner', 'reviewer'}]

    def roles(self):
        return [s.role for s in self.submits]


def counting_plan(_prompt):
    steps = [dict(provider=m, title=f'Say {i}', instruction=f'Say {i} after the previous member.', order=i, mode='reply')
             for i, m in enumerate(MEMBERS, start=1)]
    return {'planner_kind': 'plan', 'text': 'Five steps, one after another.', 'plan': steps}


def direct_answer(_prompt):
    return {'planner_kind': 'answer', 'text': LANE_ANSWER, 'plan': []}


@pytest.fixture
def coordinated(tmp_path):
    from companion.team_delegation import Coordinator

    def make(planner):
        store = Room(tmp_path / 'team')
        room_id = store.create_room('Acceptance room')['id']
        triage, team = Triage(), Team(planner)
        coordinator = Coordinator(store, triage, roster=team.roster, submit=team.submit,
                                  child_status=team.child_status, child_stop=team.child_stop)
        return SimpleNamespace(store=store, room=room_id, triage=triage, team=team, coordinator=coordinator)
    return make


def compose(world, text, request_id='composer-1', rounds=80):
    """What the "Kyra & team" composer sends, then the background worker's advances until the request settles."""
    result = world.coordinator.create(world.room, text, request_id, mode='auto', consent=True, plan_consent=True, interview=False)
    for _ in range(rounds):
        result = world.coordinator.advance(world.room, request_id)
        if result['state'] in SETTLED and not world.coordinator.pending():
            break
    return result


def test_one_message_counts_one_to_five_in_order_through_the_planner(coordinated):
    world = coordinated(counting_plan)
    result = compose(world, COUNT)

    assert world.triage.calls == [], 'a "Kyra & team" message went to local triage instead of the Claude planner'
    assert world.team.roles()[:1] == ['planner'], f'the planner was not asked first: {world.team.roles()}'
    assert world.team.members() == list(MEMBERS), f'steps ran as {world.team.members()}'
    started_early = [(s.member, s.running) for s in world.team.submits if s.member in MEMBERS and s.running]
    assert started_early == [], f'a step started before the previous one finished: {started_early}'
    assert [member for member, _ in world.team.posted] == list(MEMBERS)
    assert [text for _, text in world.team.posted] == [f'{NAMES[m]} says {i}' for i, m in enumerate(MEMBERS, 1)]
    prompts = {s.member: s.text for s in world.team.submits}
    for earlier, later in zip(MEMBERS, MEMBERS[1:], strict=False):
        if earlier == 'kyra':
            assert 'Kyra step done' in prompts[later]
            assert dict(world.team.posted)[earlier] not in prompts[later]  # Local context is private.
        else:
            assert dict(world.team.posted)[earlier] in prompts[later], f'{later} did not receive {earlier}\'s output'
    assert result['state'] == 'completed', result


def test_a_plain_question_is_answered_by_the_planner_with_no_build_or_review(coordinated):
    world = coordinated(direct_answer)
    result = compose(world, QUESTION)

    assert world.triage.calls == [], 'a "Kyra & team" question went to local triage instead of the Claude planner'
    assert world.team.roles() == ['planner'], f'a question started more than the planner: {world.team.roles()}'
    assert result['state'] == 'completed', result
    room = world.store.get_room(world.room)
    assert room['runs'] == [], 'a question created a build or review run'
    assert not [m for m in room['messages'] if m.get('step') == 'asked'], 'a plain question asked the owner for input'


# Journey 2: "@all hi" through the real HTTP delegation wiring

class ReadyTransport(RevisionTransport):
    """The recording native transport, reporting ready like a connected visible chat."""

    def status(self):
        return {'ready': True}


class SyncThread:
    """team_connected starts the app adapter in a thread; here it runs inline so the test stays deterministic."""

    def __init__(self, *, target, args=(), daemon=None, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    from companion import team_connected, team_http
    from companion.team_lead import LeadService

    transport = ReadyTransport()
    store, service = bound(team_chat, tmp_path, transport)
    backend = FakeBackend(respond=lambda capsule: worker_result(
        capsule, model_json(answer='Hi from Kyra.', sources=(), route='answer')))
    lead = LeadService(store, backend, project=PROJECT, source_reader=Sources())

    def finish_app(connected, row):
        connected.begin(row['id'])
        connected.finish(row['id'], state='completed', text=f"Hi from {team_connected.PROVIDERS[row['provider']]}.",
                         reason='', session_id=None, surface='')

    apps = {'apps': [dict(id=p, workspace=str(tmp_path / 'checkout')) for p in team_connected.PROVIDERS]}
    monkeypatch.setattr(team_connected, 'app_status', lambda **kwargs: apps)
    monkeypatch.setattr(team_connected, '_config', lambda provider: ({'workspace': str(tmp_path), 'model': 'fixture'},
                                                                      'fixture-pin'))
    monkeypatch.setattr(team_connected, '_work', finish_app)
    monkeypatch.setattr(team_connected, 'threading', SimpleNamespace(Thread=SyncThread, Lock=threading.Lock))
    monkeypatch.setattr(team_http, 'wake_service', lambda *args, **kwargs: None)
    monkeypatch.setattr(team_http, 'wake_lead', lambda *args, **kwargs: None)
    coordinator = team_http.get_delegation(store, lead, service)
    return SimpleNamespace(store=store, service=service, transport=transport, lead=lead, coordinator=coordinator,
                           room=store.create_room('Acceptance room')['id'], connected=team_connected)


def play_native(world):
    """Play Claude and Codex: answer every delivered native run once, then let the service consume it."""
    for run_id in world.store.active_runs():
        world.service.step(run_id)
        sends = [s for s in world.transport.sends if s['run_id'] == run_id]
        if sends and not sends[-1]['result_path'].exists():
            assert sends[-1]['stage'] in {'reply_claude', 'reply_codex'}
            who = 'Claude' if sends[-1]['role'] == 'claude' else 'Codex'
            respond(world.transport, sends[-1]['stage'], run_id=run_id, summary=f'Hi from {who}.')
            world.service.step(run_id)


def room_projection(world):
    from companion.team_http import collapse_delegated_requests, room_view
    raw = world.store.get_room(world.room)
    view = world.connected.project_room(room_view(raw), world.store.root)
    view['delegations'] = world.coordinator.for_room(world.room)
    collapse_delegated_requests(view)
    return view['messages']


def test_at_all_hi_gets_one_reply_from_each_member_and_one_post_from_owner(wired):
    world = wired
    text = '@all hi'
    world.coordinator.create(world.room, text, 'composer-all', mode='auto', consent=True, interview=True)
    for _ in range(40):
        result = world.coordinator.advance(world.room, 'composer-all')
        world.lead.run_once()
        play_native(world)
        if result['state'] in SETTLED and not world.store.active_runs():
            break

    assert {run['execution_mode'] for run in world.store.get_room(world.room)['runs']} == {'reply_claude', 'reply_codex'}
    messages = room_projection(world)
    replies = {}
    for m in messages:
        if m['author'] in MEMBERS and m.get('kind') not in {'step', 'report'} and m.get('text'):
            replies.setdefault(m['author'], []).append(m['text'])
    assert {name: len(texts) for name, texts in replies.items()} == {name: 1 for name in MEMBERS}, replies
    from_owner = [m['text'] for m in messages if m['author'] == 'user']
    assert from_owner == [text], f'the owner\'s post should be stored once and nothing else as the owner; stored {from_owner}'


# Journey 4: a thread reply to one member

def handbook_line():
    path = ROOT / 'docs' / 'team-handbook.md'
    assert path.exists(), 'docs/team-handbook.md is missing'
    talk = path.read_text(encoding='utf-8').split('## How we talk', 1)[1]
    return next(line[2:].strip() for line in talk.splitlines() if line.startswith('- '))


def test_a_thread_reply_reaches_one_member_with_history_and_handbook_and_lands_in_the_thread(tmp_path):
    from companion import team_replies
    from companion.input_labels import InputLabeller

    labels = InputLabeller(third_party_names=lambda: [])
    transport = RevisionTransport()
    store, service = bound(team_chat, tmp_path, transport)
    threads = Threads(store.root)
    room_id = store.create_room('Thread room')['id']
    with store.admission() as data:  # a finished Codex answer in the room, the message the owner replies to
        data['rooms'][room_id]['messages'].append(dict(id='a' * 32, author='codex', text='The pump drains the queue.',
                                                       created_at=1.0, run_id=None, kind='result'))

    def round_trip(text, request_id, answer):
        opened = threads.reply(store.get_room(room_id), 'a' * 32, text, request_id)
        assert team_replies.pump(service, threads, labeller=labels) == {'sent': 1}
        line = next(row for row in threads.thread(opened['thread_id'], [])['lines'] if row['request_id'] == request_id)
        service.step(line['run_id'])
        send = transport.sends[-1]
        assert send['run_id'] == line['run_id']
        respond(transport, send['stage'], run_id=line['run_id'], summary=answer)
        service.step(line['run_id'])
        team_replies.pump(service, threads, labeller=labels)
        return opened['thread_id'], send

    thread_id, _ = round_trip('Does it wait for Claude?', 'reply-1', 'No, it waits only for me.')
    sent_before = len(transport.sends)
    again, send = round_trip('Then who drains it overnight?', 'reply-2', 'I do, from the wake loop.')

    assert again == thread_id
    assert [s['role'] for s in transport.sends[sent_before:]] == ['codex'], 'the reply reached another member'
    prompt = send['assignment_path'].read_text(encoding='utf-8')
    assert 'Then who drains it overnight?' in prompt
    for earlier in ('Does it wait for Claude?', 'No, it waits only for me.'):
        assert earlier in prompt, f'the thread history is missing from the prompt: {earlier!r}'
    assert handbook_line() in prompt, 'the Team handbook is missing from the prompt'
    lines = threads.thread(thread_id, [])['lines']
    assert [(row['author'], row['text']) for row in lines][-1] == ('codex', 'I do, from the wake loop.')
    assert store.get_room(room_id)['runs'][-1]['status'] == 'completed'
