"""Claude acceptance for the direct native modes of Team runs (2026-09-29), written against Home's contract while
A second builder lane builds team_chat.py and workflow_control.py; Home owns delegation, HTTP and readiness tests.

Seam: TeamStore.submit(room_id, text, request_id, share_with_team=True, execution_mode=...) with the modes below,
TeamService.step(run_id), TeamService.stop(room_id, run_id=...), validated results echoing run_id, stage and
attempt. reply_claude and reply_codex are real direct-reply stages: one delivery, one correlated summary, nothing
after it, and a Codex reply never accepts code. Legacy plan_only stores one to three {provider, title, instruction} tasks for
supported providers and runs none of them. Codex may repeat (coding packages, 2026-09-29), while
Cursor and Antigravity appear at most once; with no task the planner asks the owner through a
structured needs_input, and a completed plan without tasks is refused. A Codex
text reply skips the workspace fingerprint and never accepts code changes.
review_only judges bounded text reports with a verdict and no revision, and never starts a build or repair. The
legacy worker still needs a same-revision review. Stop of the exact run sends no successor, and a reply that
arrives after Stop is recorded but not applied.
Real TeamStore and TeamService with the fake transport from test_team_chat_acceptance; the tests play the agents.
"""
import contextlib
import importlib
import json

import pytest

from tests.test_team_chat_acceptance import BUILD, OTHER_REV, PASS, REV, FakeTransport, bound, respond

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")

TERMINAL = {'completed', 'needs_input', 'stopped', 'failed', 'uncertain'}
GREETING = 'Say hello and add 17 and 25.'
REPORTS = 'Cursor report: Hello, 17 + 25 = 42.\nAntigravity report: blocked (account_interaction_required).'
PROVIDER_PLAN = {'summary': 'Three targets.', 'tasks': [
    {'provider': 'codex', 'title': 'Build the field', 'instruction': 'Add the fixture field.'},
    {'provider': 'cursor', 'title': 'Explain the field', 'instruction': 'Explain the fixture field.'},
    {'provider': 'antigravity', 'title': 'Check the wording', 'instruction': 'Check the fixture wording.'}]}


@pytest.fixture
def team():
    """Imported per test so an unbuilt module fails each test rather than erroring collection."""
    return importlib.import_module('companion.team_chat')


class RevisionTransport(FakeTransport):
    """The fake transport plus the workspace fingerprint a Codex reply binds before and checks after."""

    def __init__(self):
        super().__init__()
        self.current = REV

    def revision(self):
        return self.current


@pytest.fixture
def world(team, tmp_path):
    transport = RevisionTransport()
    store, service = bound(team, tmp_path, transport)
    return store, service, transport, store.create_room('Fixture room')['id']


def launch(store, service, room_id, mode, text=GREETING, request_id='req-1'):
    run_id = store.submit(room_id, text, request_id, share_with_team=True, execution_mode=mode)['run_id']
    service.step(run_id)
    return run_id


def record(store, room_id, run_id):
    return next(run for run in store.get_room(room_id)['runs'] if run['id'] == run_id)


def said(store, room_id, author):
    return [message['text'] for message in store.get_room(room_id)['messages'] if message['author'] == author]


def answer(transport, stage, body):
    send = transport.sends[-1]
    send['result_path'].parent.mkdir(parents=True, exist_ok=True)
    send['result_path'].write_text(json.dumps(body), encoding='utf-8')


@pytest.mark.parametrize('mode,role,baseline', [('reply_claude', 'claude', None), ('reply_codex', 'codex', REV)])
def test_a_direct_reply_is_one_delivery_one_correlated_summary_and_no_follow_up(world, mode, role, baseline):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, mode)
    assert transport.stages() == [(role, mode)]
    assert said(store, room_id, role) == []
    respond(transport, mode, summary='Hello. 17 + 25 = 42.')
    service.step(run_id)
    service.step(run_id)
    assert transport.stages() == [(role, mode)]
    assert record(store, room_id, run_id)['status'] == 'completed'
    assert any('17 + 25 = 42' in text for text in said(store, room_id, role))
    assert record(store, room_id, run_id).get('revision') in (None, baseline)  # never more than the unchanged baseline


@pytest.mark.parametrize('defect', ['wrong_run', 'wrong_stage', 'wrong_attempt', 'plan_fields', 'no_summary'])
def test_an_uncorrelated_direct_reply_completes_nothing(world, defect):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'reply_claude')
    send = transport.sends[-1]
    body = {'run_id': send['run_id'], 'stage': 'reply_claude', 'attempt': send['attempt'], 'status': 'completed',
            'summary': 'Hello.'}
    if defect == 'wrong_run':
        body['run_id'] = 'another-run'
    elif defect == 'wrong_stage':
        body['stage'] = 'plan'
    elif defect == 'wrong_attempt':
        body['attempt'] += 1
    elif defect == 'plan_fields':
        body['tasks'] = [{'title': 'T', 'instruction': 'Do it.'}]
    else:
        del body['summary']
    answer(transport, 'reply_claude', body)
    service.step(run_id)
    assert transport.stages() == [('claude', 'reply_claude')]
    assert record(store, room_id, run_id)['status'] != 'completed'


def test_a_codex_direct_reply_that_claims_a_revision_is_refused(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'reply_codex')
    respond(transport, 'reply_codex', summary='I also changed a file.', revision=REV)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] != 'completed'
    assert transport.stages() == [('codex', 'reply_codex')]


def test_a_text_reply_does_not_treat_workspace_changes_as_code_acceptance(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'reply_codex')
    transport.current = OTHER_REV  # Codex edited the workspace while replying
    respond(transport, 'reply_codex', summary='Hello.')
    service.step(run_id)
    run = record(store, room_id, run_id)
    assert run['status'] == 'completed' and run.get('revision') is None
    assert transport.stages() == [('codex', 'reply_codex')]


def test_a_codex_text_reply_can_be_sent_without_a_source_baseline(team, tmp_path):
    transport = FakeTransport()
    store, service = bound(team, tmp_path, transport)
    room_id = store.create_room('Fixture room')['id']
    run_id = launch(store, service, room_id, 'reply_codex')
    assert transport.stages() == [('codex', 'reply_codex')]
    assert record(store, room_id, run_id)['revision'] is None


def test_plan_only_stores_one_to_three_unique_provider_tasks_and_runs_none(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan_only')
    assert transport.stages() == [('claude', 'plan')]
    respond(transport, 'plan', **PROVIDER_PLAN)
    service.step(run_id)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] == 'completed'
    assert transport.stages() == [('claude', 'plan')]
    stored = json.dumps(store.get_room(room_id))
    assert all(task['title'] in stored and task['provider'] in stored for task in PROVIDER_PLAN['tasks'])


def test_plan_only_accepts_repeated_codex_coding_tasks(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan_only')
    tasks = [{'provider': 'codex', 'title': f'Part {i}', 'instruction': f'Build part {i}.'} for i in (1, 2)]
    tasks.append({'provider': 'cursor', 'title': 'Explain', 'instruction': 'Explain both parts.'})
    respond(transport, 'plan', summary='Two coding packages and one explanation.', tasks=tasks)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] == 'completed'
    assert [task['assignee'] for task in record(store, room_id, run_id)['tasks']] == ['codex', 'codex', 'cursor']


@pytest.mark.parametrize('tasks', [
    [{'provider': p, 'title': f'T{i}', 'instruction': 'Do it.'} for i, p in enumerate(('codex', 'cursor', 'antigravity', 'codex'))],
    [{'provider': 'cursor', 'title': 'A', 'instruction': 'Do A.'}, {'provider': 'cursor', 'title': 'B', 'instruction': 'Do B.'}],
    [{'provider': 'gemini', 'title': 'T', 'instruction': 'Do it.'}],
    [{'provider': 'codex', 'title': 'T'}],
    [{'title': 'T', 'instruction': 'Do it.'}],
], ids=['four', 'repeated_provider', 'unknown_provider', 'no_instruction', 'no_provider'])
def test_plan_only_refuses_an_invalid_task_list(world, tasks):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan_only')
    respond(transport, 'plan', summary='Invalid plan.', tasks=tasks)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] != 'completed'
    assert transport.stages() == [('claude', 'plan')]


def test_plan_only_with_nothing_to_assign_asks_the_owner_in_a_structured_reply(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan_only')
    respond(transport, 'plan', summary='Which provider should take this?', status='needs_input')
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] == 'needs_input'
    assert 'Which provider should take this?' in said(store, room_id, 'claude')
    assert transport.stages() == [('claude', 'plan')]


@pytest.mark.parametrize('fields', [{}, {'tasks': []}], ids=['no_tasks_field', 'empty_tasks'])
def test_a_completed_plan_only_without_a_task_is_not_accepted(world, fields):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan_only')
    respond(transport, 'plan', summary='Nothing to assign.', **fields)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] == 'needs_input'
    assert transport.stages() == [('claude', 'plan')]


def test_review_only_judges_text_reports_without_accepting_code(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'review_only', text=REPORTS)
    assert transport.stages() == [('claude', 'review')]
    assert '17 + 25 = 42' in transport.sends[-1]['assignment_path'].read_text(encoding='utf-8')
    respond(transport, 'review', summary='The reports are consistent.', verdict='pass')
    service.step(run_id)
    service.step(run_id)
    run = record(store, room_id, run_id)
    assert run['status'] == 'completed' and run.get('revision') is None
    assert transport.stages() == [('claude', 'review')]


@pytest.mark.parametrize('verdict', ['changes_requested', 'needs_input'])
def test_a_report_review_never_starts_a_build_or_repair(world, verdict):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'review_only', text=REPORTS)
    respond(transport, 'review', summary='One report is incomplete.', verdict=verdict)
    service.step(run_id)
    service.step(run_id)
    assert transport.stages() == [('claude', 'review')]
    assert record(store, room_id, run_id)['status'] in TERMINAL


def test_a_report_review_that_claims_a_revision_is_refused(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'review_only', text=REPORTS)
    respond(transport, 'review', summary='Pass.', verdict='pass', revision=REV)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] != 'completed'
    assert transport.stages() == [('claude', 'review')]


def test_report_reviews_are_bounded(world):
    store, service, transport, room_id = world
    assert store.submit(room_id, REPORTS, 'req-ok', share_with_team=True, execution_mode='review_only')['run_id']
    with pytest.raises(ValueError):
        store.submit(room_id, 'x' * 16001, 'req-big', share_with_team=True, execution_mode='review_only')
    assert transport.stages() == []


def test_the_legacy_worker_rejects_a_review_of_another_revision(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'worker', text='Add a fixture field.')
    assert transport.stages() == [('codex', 'build')]
    respond(transport, 'build', **BUILD)
    service.step(run_id)
    assert transport.stages() == [('codex', 'build'), ('claude', 'review')]
    respond(transport, 'review', **{**PASS, 'revision': OTHER_REV})
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] != 'completed'


def test_the_legacy_worker_completes_only_on_a_same_revision_review(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'worker', text='Add a fixture field.')
    respond(transport, 'build', **BUILD)
    service.step(run_id)
    respond(transport, 'review', **PASS)
    service.step(run_id)
    assert record(store, room_id, run_id)['status'] == 'completed'
    assert transport.stages() == [('codex', 'build'), ('claude', 'review')]


@pytest.mark.parametrize('mode,role,baseline', [('reply_codex', 'codex', REV), ('reply_claude', 'claude', None)])
def test_a_reply_after_stop_is_recorded_but_not_applied(world, mode, role, baseline):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, mode)
    service.stop(room_id, run_id=run_id)
    respond(transport, mode, summary='Late hello.')
    service.step(run_id)
    service.step(run_id)
    run = record(store, room_id, run_id)
    assert run['status'] == 'stopped' and run.get('revision') in (None, baseline)
    assert 'Late hello.' in said(store, room_id, role)
    assert transport.stages() == [(role, mode)]


def test_stopping_the_exact_plan_run_sends_no_successor(world):
    store, service, transport, room_id = world
    run_id = launch(store, service, room_id, 'plan', text='Add a fixture field and test it.')
    service.stop(room_id, run_id=run_id)
    respond(transport, 'plan', summary='Two tasks.', tasks=[{'title': 'Add', 'instruction': 'Add it.'}])
    service.step(run_id)
    run = record(store, room_id, run_id)
    assert run['status'] == 'stopped' and run['tasks'] == []
    assert transport.stages() == [('claude', 'plan')]


def test_a_stop_for_another_run_leaves_the_current_run_alone(world):
    store, service, transport, room_id = world
    earlier = launch(store, service, room_id, 'reply_claude')
    respond(transport, 'reply_claude', run_id=earlier, summary='Hello.')
    service.step(earlier)
    current = launch(store, service, room_id, 'reply_claude', text='Add 2 and 3.', request_id='req-2')
    service.stop(room_id, run_id=earlier)
    with contextlib.suppress(LookupError, ValueError):
        service.stop(room_id, run_id='f' * 32)
    respond(transport, 'reply_claude', run_id=current, summary='5.')
    service.step(current)
    assert record(store, room_id, current)['status'] == 'completed'
    assert transport.stages() == [('claude', 'reply_claude'), ('claude', 'reply_claude')]
