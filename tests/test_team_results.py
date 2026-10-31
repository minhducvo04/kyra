"""Terminal envelopes must be correlated; a draft or another room is not evidence."""
import pytest

from companion.team_chat import validate_result


def plan(**extra):
    return dict(run_id='run-1', stage='plan', attempt=0, status='completed',
                summary='Make the small change.', tasks=[{'title':'Change','instruction':'Edit the approved file.'}], **extra)


def test_plan_result_keeps_bounded_tasks():
    value = validate_result(plan(), run_id='run-1', stage='plan', attempt=0)
    assert value['tasks'][0]['title'] == 'Change'


@pytest.mark.parametrize('change', [
    {'run_id':'other-room'}, {'stage':'build'}, {'attempt':1},
    {'status':'acknowledged'}, {'tasks':[]}, {'tasks':[{'title':'x','instruction':'y'}] * 5},
    {'actions':['merge_pr']}, {'tasks':[{'title':'x','instruction':'y','app':'unknown'}]},
])
def test_wrong_identity_partial_or_expanded_authority_is_not_a_terminal_plan(change):
    value = plan()
    value.update(change)
    with pytest.raises(ValueError):
        validate_result(value, run_id='run-1', stage='plan', attempt=0)


def test_review_must_bind_the_actual_build_revision():
    value = dict(run_id='run-1', stage='review', attempt=0, status='completed',
                 summary='Checks passed.', verdict='pass', revision='a'*64)
    assert validate_result(value, run_id='run-1', stage='review', attempt=0,
                           revision='a'*64)['verdict'] == 'pass'
    with pytest.raises(ValueError):
        validate_result(value, run_id='run-1', stage='review', attempt=0, revision='b'*64)


def test_needs_input_is_a_terminal_pause_not_completion():
    value = dict(run_id='run-1', stage='build', attempt=0, status='needs_input',
                 summary='The requirement needs a decision.')
    assert validate_result(value, run_id='run-1', stage='build', attempt=0)['status'] == 'needs_input'


@pytest.mark.parametrize('field,value', [('status', []), ('status', {}), ('summary', []), ('attempt', True)])
def test_untrusted_result_types_fail_as_validation_errors(field, value):
    packet = plan()
    packet[field] = value
    with pytest.raises(ValueError):
        validate_result(packet, run_id='run-1', stage='plan', attempt=0)
