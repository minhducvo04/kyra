"""Capture boundaries independent of the future training/export pipeline."""
import json

import pytest

from companion import task_learning


def packet():
    return {
        'schema_version': 1, 'status': 'candidate', 'example_id': 'idle-stop-01',
        'task_family': 'idle-stop', 'target': 'coding', 'kind': 'repair',
        'origin': 'synthetic', 'privacy_tier': 'T1', 'privacy_classes': [],
        'source': {'task_id': 'fixture-task', 'revision': 'a' * 40,
                   'artifact_paths': ['src/companion/workflow_control.py'],
                   'check_summary': 'Fictional fixture, independently exercised by capture tests.',
                   'check_status': 'locally_verified', 'author_provider': 'fixture',
                   'author_model': 'unknown', 'author_effort': 'unknown',
                   'target_author': 'deterministic'},
        'review': {'dataset_review': 'pending', 'local_training_reuse': 'pending',
                   'holdout_check': 'pending'},
        'messages': [{'role': 'user', 'content': 'Stop polling once every worker is idle.'},
                     {'role': 'assistant', 'content': 'Clear the next scheduled check.'}],
    }


def test_capture_preserves_candidate_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(task_learning, 'DATA_DIR', tmp_path)
    value = packet()
    saved = task_learning.save_candidate(value)
    assert json.loads(saved.read_text()) == value
    with pytest.raises(FileExistsError):
        task_learning.save_candidate(value)
    assert json.loads(saved.read_text()) == value


def test_error_does_not_echo_payload():
    value = packet()
    value['messages'][0]['content'] = 'secret@example.com'
    with pytest.raises(ValueError) as exc:
        task_learning.validate_packet(value)
    assert 'secret@example.com' not in str(exc.value)


def test_candidate_cannot_claim_training_approval():
    value = packet()
    value['review']['dataset_review'] = 'approved'
    with pytest.raises(ValueError):
        task_learning.validate_packet(value)


def test_windows_home_path_is_not_hidden_by_json_escaping():
    value = packet()
    value['messages'][0]['content'] = r'C:\Users\example\Documents\note.txt'
    with pytest.raises(ValueError):
        task_learning.validate_packet(value)


def test_ordinary_coding_token_variables_are_allowed():
    value = packet()
    value['messages'][-1]['content'] = 'max_tokens = 1024\ntokenizer = load_tokenizer()'
    assert task_learning.validate_packet(value)['messages'] == value['messages']
