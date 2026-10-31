"""A discovered native chat cannot claim dispatch readiness without current authority."""
from companion import team_transport
from companion.app_tasks import AppTask
from companion.paths import write_json
from companion.workflow_control import WorkflowStore


def test_native_readiness_requires_matching_unexpired_controller(tmp_path, monkeypatch):
    repo = str(tmp_path)
    participants = {p: {'id': p + '-fixture', 'cwd': repo} for p in ('claude', 'codex')}
    root = tmp_path / 'team'
    root.mkdir()
    config = dict(version=2, workspace=repo, scope='Fictional arithmetic only', participants=participants,
                  controller={'task_id': 'fixture', 'version': 1})
    write_json(root / 'config.json', config)
    tasks = [AppTask(p, pin['id'], p, repo, True, 1) for p, pin in participants.items()]
    monkeypatch.setattr(team_transport, 'discover', lambda: tasks)
    authority = WorkflowStore(tmp_path / 'workflow' / 'control.json')
    transport = team_transport.VisibleTeamTransport(root, controller=authority)
    assert not transport.status()['ready']
    import time
    envelope = dict(task_ids=['fixture'], repo=repo, actions=['use_claude', 'use_codex'],
                    chats=[dict(app=p, **pin) for p, pin in participants.items()], max_unknown_checks=2,
                    max_checks=10, interval_seconds=30, expires_at=time.time()+600, max_repair_attempts=1)
    authority.configure(envelope)
    assert transport.status()['ready']
    authority.revoke()
    assert not transport.status()['ready']
    authority.configure(envelope)  # New version cannot silently renew an old chat binding.
    assert not transport.status()['ready']
