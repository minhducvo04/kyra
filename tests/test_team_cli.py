"""The trusted CLI cannot turn an uncertain delivery into an automatic retry."""
import json
import time

import pytest

from companion.team_chat import TeamStore
from companion.workflow_control import ACTIONS, WorkflowStore


def controller(tmp_path):
    """Commit-1 seams: the CLI's authority comes only from an explicit --controller-path, never from --root."""
    store = WorkflowStore(tmp_path / 'workflow' / 'control.json')
    store.configure({'task_ids': ['task-1'], 'repo': '/tmp/example', 'actions': sorted(ACTIONS),
                     'chats': [{'app': 'claude', 'id': 'claude-1', 'cwd': '/tmp/example'},
                               {'app': 'codex', 'id': 'codex-1', 'cwd': '/tmp/example'}],
                     'max_unknown_checks': 2, 'max_checks': 10, 'interval_seconds': 60,
                     'expires_at': time.time() + 3600, 'max_repair_attempts': 2})
    return store


def test_revision_prints_only_fingerprint(monkeypatch, capsys):
    from scripts import team_chat
    monkeypatch.setattr(team_chat.transport, 'workspace_revision', lambda path: 'a' * 64)
    assert team_chat.main(['revision', '--workspace', '/tmp/example']) == 0
    assert capsys.readouterr().out == 'a' * 64 + '\n'


def test_configure_requires_explicit_bindings_and_refuses_during_run(tmp_path, monkeypatch, capsys):
    from scripts import team_chat
    calls = []
    monkeypatch.setattr(team_chat.transport, 'configure', lambda root, **kwargs: calls.append(kwargs))
    authority = controller(tmp_path)
    args = ['--root', str(tmp_path), '--controller-path', str(authority.path), 'configure',
            '--workspace', '/tmp/example', '--scope', 'Edit example.py',
            '--claude-id', 'claude-1', '--claude-cwd', '/tmp/example',
            '--codex-id', 'codex-1', '--codex-cwd', '/tmp/example',
            '--controller-task-id', 'task-1', '--controller-version', str(authority.status()['version'])]
    assert team_chat.main(args) == 0
    assert calls[0]['participants'] == {'claude': {'id': 'claude-1', 'cwd': '/tmp/example'},
                                       'codex': {'id': 'codex-1', 'cwd': '/tmp/example'}}
    assert calls[0]['controller'] == {'task_id': 'task-1', 'version': authority.status()['version']}
    store = TeamStore(tmp_path, controller=authority)
    room = store.create_room('Example')
    store.submit(room['id'], 'Build example', 'request')
    assert team_chat.main(args) == 1
    assert len(calls) == 1
    assert 'error' in json.loads(capsys.readouterr().err)


def test_reconciliation_requires_worker_ended_and_retains_delivery_record(tmp_path, capsys):
    from scripts import team_chat
    store = TeamStore(tmp_path, controller=WorkflowStore(tmp_path / 'workflow' / 'control.json'))
    room = store.create_room('Example')
    run_id = store.submit(room['id'], 'Build example', 'request')['run_id']
    with store.edit() as data:
        _, run = store.find_run(data, run_id)
        run.update(status='uncertain', phase='terminal', role_lease='codex', write_lease=True,
                   delivery_id='original-delivery', assignment_path='/tmp/old-assignment')
    args = ['--root', str(tmp_path), '--controller-path', str(controller(tmp_path).path), 'reconcile', run_id]
    with pytest.raises(SystemExit) as exc:
        team_chat.main(args)
    assert exc.value.code == 2
    assert store.get_room(room['id'])['runs'][0]['write_lease'] is True
    assert team_chat.main(args + ['--worker-ended']) == 0
    run = store.get_room(room['id'])['runs'][0]
    assert run['status'] == 'stopped' and run['role_lease'] is None and run['write_lease'] is False
    assert run['delivery_id'] == 'original-delivery' and run['attempt'] == 0
    assert run['stop_requested'] is True and run['reconciled_at'] > 0
    assert store.active_runs() == []
    assert team_chat.main(args + ['--worker-ended']) == 1


def test_status_is_read_only_and_never_prints_message_content(tmp_path, capsys):
    from scripts import team_chat
    store = TeamStore(tmp_path)
    room = store.create_room('Example')
    store.submit(room['id'], 'Private message must stay out of status', 'request', False)
    before = store.path.read_bytes()
    assert team_chat.main(['--root', str(tmp_path), 'status']) == 0
    out = capsys.readouterr().out
    assert 'Private message' not in out
    assert json.loads(out)['rooms'][0]['id'] == room['id']
    assert store.path.read_bytes() == before
