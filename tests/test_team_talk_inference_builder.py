"""TEAM-TALK capsule boundary checks; no model, tokenizer or runtime stores."""
import copy
import json

import pytest

from companion import local_lead_inference as lead


def capsule(**extra):
    value = dict(request='Inspect the selected project.', sources=[], history=[],
                 project='demo', room='room', request_id='request')
    value.update(extra)
    value['capsule_sha256'] = lead._worker().digest(value)
    return value


class RecordingTokenizer:
    def __init__(self):
        self.messages = []

    def apply_chat_template(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        return list(range(sum(len(m['content']) for m in messages)))


def test_existing_capsule_stays_compatible_without_context_defaults():
    worker = lead._worker()
    cap = capsule()
    worker.validate_capsule(cap)
    tokenizer = RecordingTokenizer()
    worker.pack(cap, tokenizer)
    data = json.loads(tokenizer.messages[-1][1]['content'])
    assert 'capability_context' not in data and 'usage_context' not in data


@pytest.mark.parametrize('field', ['capability_context', 'usage_context'])
def test_context_is_preserved_in_rendered_input_and_not_source_authority(field):
    worker = lead._worker()
    text = 'native-code-inspection: available; recommendation only'
    cap = capsule(**{field: text})
    worker.validate_capsule(cap)
    tokenizer = RecordingTokenizer()
    tokens, sources, history = worker.pack(cap, tokenizer)
    data = json.loads(tokenizer.messages[-1][1]['content'])
    assert data[field] == text
    assert sources == [] and history == [] and len(tokens) <= 6000
    assert data['sources'] == []
    with pytest.raises(ValueError, match='output_invalid'):
        worker.validate_answer(json.dumps(dict(answer='Inspect it.', sources=[field], missing=False,
                                              route='worker_task')), [])


@pytest.mark.parametrize('field,limit', [('capability_context', 8192), ('usage_context', 2048)])
def test_context_bound_counts_utf8_bytes(field, limit):
    worker = lead._worker()
    worker.validate_capsule(capsule(**{field: 'é' * (limit // 2)}))
    with pytest.raises(ValueError, match='input_too_large'):
        worker.validate_capsule(capsule(**{field: 'é' * (limit // 2) + 'a'}))
    worker.validate_capsule(capsule(**{field: ''}))


@pytest.mark.parametrize('field', ['capability_context', 'usage_context'])
@pytest.mark.parametrize('value', [None, {}, [], 3, True])
def test_context_wrong_type_rejected(field, value):
    with pytest.raises(ValueError, match='output_invalid'):
        lead._worker().validate_capsule(capsule(**{field: value}))


def test_unknown_context_field_rejected():
    with pytest.raises(ValueError, match='output_invalid'):
        lead._worker().validate_capsule(capsule(authority_context='grant'))


def test_required_context_cannot_be_dropped_to_fit_tokens():
    worker = lead._worker()
    cap = capsule(capability_context='x' * 6000)
    worker.validate_capsule(cap)
    with pytest.raises(ValueError, match='input_too_large'):
        worker.pack(cap, RecordingTokenizer())


def test_source_omission_does_not_remove_context():
    worker = lead._worker()
    cap = capsule(capability_context='inspection available', usage_context='usage unknown',
                  sources=[dict(id='s1',kind='note',ref='s1',sha256='a'*64,text='x'*6000)])
    worker.validate_capsule(cap)
    tokenizer = RecordingTokenizer()
    _, sources, _ = worker.pack(cap, tokenizer)
    assert sources == []
    # The rejected source candidate also retains required context on every render.
    for messages in tokenizer.messages:
        data = json.loads(messages[1]['content'])
        assert data['capability_context'] == cap['capability_context']
        assert data['usage_context'] == cap['usage_context']


@pytest.mark.parametrize('field', ['capability_context', 'usage_context'])
def test_context_is_bound_to_capsule_and_effective_input_digests(field):
    worker = lead._worker()
    cap = capsule(**{field: 'native inspection available'})
    worker.validate_capsule(cap)
    result = {k: cap[k] for k in ('project', 'room', 'request_id', 'capsule_sha256')}
    result.update(attempt_id='0'*32, text=json.dumps(dict(answer='I can propose inspection.', sources=[],
                  missing=False, route='worker_task')), included_source_ids=[], included_history_ids=[],
                  omitted_counts=dict(sources=0,history=0), truncated=False,
                  effective_input_sha256=worker.digest({k:v for k,v in cap.items() if k!='capsule_sha256'}),
                  model={'id':'synthetic'},metrics=dict(prompt_tokens=200,generation_tokens=50,finish_reason='stop'))
    worker.validate_result(result,cap,'0'*32,'synthetic')
    changed = copy.deepcopy(cap)
    changed[field] = 'unavailable'
    with pytest.raises(ValueError, match='output_invalid'):
        worker.validate_capsule(changed)
    changed['capsule_sha256']=worker.digest({k:v for k,v in changed.items() if k!='capsule_sha256'})
    result['capsule_sha256']=changed['capsule_sha256']
    with pytest.raises(ValueError, match='output_invalid'):
        worker.validate_result(result,changed,'0'*32,'synthetic')


def test_optional_context_does_not_expand_total_capsule_budget():
    worker = lead._worker()
    cap = capsule(capability_context='x' * 8192, usage_context='y' * 2048,
                  sources=[dict(id=str(i), kind='note', ref=str(i), sha256='a'*64,
                                text='z' * 16384) for i in range(8)])
    with pytest.raises(ValueError, match='input_too_large'):
        worker.validate_capsule(cap)
