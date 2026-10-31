"""The planner interview before any Team build (the owner's 2026-10-02 request; AGENTS.md section 3 step 3).

A team_plan request created with interview=True first sends an interviewer round (one question with a guess, or a
restated intent once ready). The parent then waits in state "interviewing" and sends nothing until the owner answers;
each answer starts the next round with the whole transcript. The planner is dispatched only after the owner's explicit
yes, and the planning -> workers step refuses an interview that was never approved. Fakes stand in for every model.
"""
import json
import sqlite3

import pytest

from companion.errors import ApiError
from companion.team_delegation import Coordinator
from tests.test_team_delegation import Lead, Store, answer
from tests.test_team_testloop import CodingProviders as Providers  # coding children carry parent_request_id

ROSTER = {p: '' for p in ('codex', 'cursor', 'antigravity', 'planner', 'reviewer', 'worker', 'interviewer')}
TEXT = 'Build an error report button for the office app'
PLAN = [dict(provider='codex', title='Error report', instruction='Build the error report button.')]


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    fake = Providers(ROSTER)
    coordinator = Coordinator(store, Lead(store), roster=fake.roster, submit=fake.submit,
                              child_status=fake.child_status, child_stop=fake.child_stop)
    row = coordinator.create(room, TEXT, 'parent-1', consent=True, plan_consent=True, interview=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    coordinator.advance(room, 'parent-1')
    return store, room, fake, coordinator


def roles(fake):
    return [provider for _, provider, _, _ in fake.submits]


def reply(fake, text, ready=False):
    """The latest interviewer round finishes with a question (or, when ready, a restated intent)."""
    ident = next(ident for ident in reversed(list(fake.states)) if ident.startswith('run-interviewer'))
    fake.states[ident].update(state='completed', text=text, ready=ready)


def test_a_planned_request_asks_the_interviewer_first_and_builds_nothing(world):
    _, room, fake, coordinator = world
    assert roles(fake) == ['interviewer']
    assert TEXT in fake.submits[0][2] and 'one question' in fake.submits[0][2].lower()
    reply(fake, 'Who reads the report? My guess: you, in Kyra.')
    for _ in range(3):
        coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert (row['state'], row['reason']) == ('interviewing', 'awaiting_answer')
    assert row['interview']['turns'][-1]['question'].startswith('Who reads the report?')
    assert roles(fake) == ['interviewer']  # waiting on the owner sends nothing


def test_each_answer_starts_the_next_round_with_the_whole_transcript(world):
    _, room, fake, coordinator = world
    reply(fake, 'Who reads the report? My guess: you.')
    coordinator.advance(room, 'parent-1')
    coordinator.answer_interview(room, 'parent-1', 'Me, in my Kyra inbox')
    assert roles(fake) == ['interviewer', 'interviewer']
    prompt = fake.submits[1][2]
    assert 'Who reads the report?' in prompt and 'Me, in my Kyra inbox' in prompt and TEXT in prompt
    assert fake.submits[0][3] != fake.submits[1][3]  # a distinct child request per round


def test_the_planner_starts_only_after_an_explicit_yes(world):
    _, room, fake, coordinator = world
    reply(fake, 'Importance and urgency? My guess: high, today.')
    coordinator.advance(room, 'parent-1')
    coordinator.answer_interview(room, 'parent-1', 'high, this week')
    reply(fake, 'You want an error report button; done when a real report reaches your inbox. Start?', ready=True)
    coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert (row['state'], row['reason']) == ('interviewing', 'awaiting_approval')
    assert 'planner' not in roles(fake)
    coordinator.answer_interview(room, 'parent-1', 'yes', approve=True)
    assert roles(fake)[-1] == 'planner'
    prompt = fake.submits[-1][2]
    assert TEXT in prompt and 'high, this week' in prompt and 'done when a real report' in prompt
    planner = coordinator.status(room, 'parent-1')['children']['planner']['id']
    fake.states[planner].update(state='completed', text='Plan ready', plan=PLAN)
    coordinator.advance(room, 'parent-1')
    assert 'worker' in roles(fake)


def test_owner_can_skip_the_remaining_questions_with_owner_yes(world):
    _, room, fake, coordinator = world
    reply(fake, 'Which inbox? My guess: Kyra.')
    coordinator.advance(room, 'parent-1')
    coordinator.answer_interview(room, 'parent-1', 'Kyra; enough questions, start', approve=True)
    assert roles(fake)[-1] == 'planner'
    assert 'Kyra; enough questions, start' in fake.submits[-1][2]


def test_answers_are_refused_outside_the_interview(world, tmp_path):
    store, room, fake, coordinator = world
    with pytest.raises(ApiError) as caught:  # the round is still running: nothing to answer yet
        coordinator.answer_interview(room, 'parent-1', 'early')
    assert caught.value.status_code == 409
    reply(fake, 'Question? My guess: yes.')
    coordinator.advance(room, 'parent-1')
    for bad in ('', '   ', 'x' * 8001):
        with pytest.raises(ApiError) as caught:
            coordinator.answer_interview(room, 'parent-1', bad)
        assert caught.value.status_code == 422
    assert roles(fake) == ['interviewer']


def test_builders_never_start_from_an_unapproved_interview(world):
    store, room, fake, coordinator = world
    # Force the planning phase with a finished plan but no yes, as a bug or a hand-edited record would.
    path = coordinator.path
    with sqlite3.connect(path) as db:
        body = json.loads(db.execute('SELECT body FROM delegations').fetchone()[0])
        body.update(workflow='planning', state='completed', targets=['planner'])
        body['children'] = {'planner': dict(id='run-planner-x', state='completed', reason='', request_id='p',
                                            plan=PLAN, text='Plan')}
        db.execute('UPDATE delegations SET body=?', (json.dumps(body),))
    fake.states['run-planner-x'] = dict(id='run-planner-x', state='completed', reason='', plan=PLAN, text='Plan')
    coordinator.advance(room, 'parent-1')
    row = coordinator.status(room, 'parent-1')
    assert (row['state'], row['reason']) == ('blocked', 'interview_not_approved')
    assert 'worker' not in roles(fake)


def test_stop_while_waiting_on_owner_sends_nothing_more(world):
    _, room, fake, coordinator = world
    reply(fake, 'Question? My guess: yes.')
    coordinator.advance(room, 'parent-1')
    coordinator.stop(room, 'parent-1')
    assert coordinator.status(room, 'parent-1')['state'] == 'stopped'
    with pytest.raises(ApiError):
        coordinator.answer_interview(room, 'parent-1', 'yes', approve=True)
    assert roles(fake) == ['interviewer']


def test_without_the_interview_flag_the_planner_starts_as_before(tmp_path):
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    fake = Providers(ROSTER)
    coordinator = Coordinator(store, Lead(store), roster=fake.roster, submit=fake.submit,
                              child_status=fake.child_status, child_stop=fake.child_stop)
    row = coordinator.create(room, TEXT, 'parent-1', consent=True, plan_consent=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    coordinator.advance(room, 'parent-1')
    assert roles(fake) == ['planner']


def test_long_requests_and_transcripts_stay_under_the_submit_limit(tmp_path):
    store = Store(tmp_path / 'team')
    room = store.create_room('Fixture')['id']
    fake = Providers(ROSTER)
    coordinator = Coordinator(store, Lead(store), roster=fake.roster, submit=fake.submit,
                              child_status=fake.child_status, child_stop=fake.child_stop)
    row = coordinator.create(room, 'x' * 15900, 'parent-1', consent=True, plan_consent=True, interview=True)
    store.answers[room, row['lead_request_id']] = answer(row['lead_request_id'], 'team_plan')
    coordinator.advance(room, 'parent-1')
    for _ in range(8):
        reply(fake, 'Q' * 7000)
        coordinator.advance(room, 'parent-1')
        coordinator.answer_interview(room, 'parent-1', 'A' * 7000)
    reply(fake, 'Restated. Start?', ready=True)
    coordinator.advance(room, 'parent-1')
    coordinator.answer_interview(room, 'parent-1', 'yes', approve=True)
    assert roles(fake)[-1] == 'planner'
    assert max(len(text) for _, _, text, _ in fake.submits[:-1]) <= 15000  # interviewer rounds
    assert len(fake.submits[-1][2]) <= 16000 and 'The owner approved this interview' in fake.submits[-1][2]
    assert fake.submits[-1][2].startswith('x' * 15000)  # the planner still gets the request itself


def test_the_question_count_is_capped_until_owner_says_yes(world):
    _, room, fake, coordinator = world
    for _ in range(9):
        reply(fake, 'Another question? My guess: no.')
        coordinator.advance(room, 'parent-1')
        try:
            coordinator.answer_interview(room, 'parent-1', 'an answer')
        except ApiError as error:
            assert error.status_code == 409
            break
    else:
        raise AssertionError('the ninth answer should be refused')
    assert sum(r == 'interviewer' for r in roles(fake)) == 9
    coordinator.answer_interview(room, 'parent-1', 'yes, start', approve=True)
    assert roles(fake)[-1] == 'planner'
