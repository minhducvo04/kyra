"""Task worktree dispatch and file/revision boundaries for all builders."""
import json
import shutil
from pathlib import Path

import pytest

from companion import team_chat, team_connected, team_workspace
from companion.team_cursor import AdapterResult

# One prepared checkout is copied per test. Gitdir files store absolute paths, so each copy is retargeted.
_TASK_IDENT = 'b' * 32


def _retarget_gitdir(path, marker, dest):
    """Point one absolute gitdir path at dest. Git records the path with forward slashes."""
    original = path.read_bytes()
    needle = marker.encode()
    start = original.rfind(needle)
    if start < 0:
        raise RuntimeError('checkout copy still points at the shared repo')
    head = original[:start]
    label = b'gitdir: '
    prefix = label if head.startswith(label) else b''
    if not head[len(prefix):]:
        raise RuntimeError('checkout copy still points at the shared repo')
    updated = prefix + dest.resolve().as_posix().encode() + original[start:]
    # Replacing the worktree gitdir file in place is denied on Windows, so write a new file.
    path.chmod(0o644)
    path.unlink()
    path.write_bytes(updated)


def _copy_task_checkout(template, dest):
    """Copy the shared checkout. Tests write only in the copy. The bare origin is not written after setup."""
    shutil.copytree(template, dest, symlinks=True)
    ident = 'task-' + _TASK_IDENT
    _retarget_gitdir(dest / '.claude' / 'worktrees' / ident / '.git', '/.git/worktrees/' + ident, dest)
    _retarget_gitdir(dest / '.git' / 'worktrees' / ident / 'gitdir', '/.claude/worktrees/' + ident + '/.git', dest)


@pytest.fixture(scope='module')
def native_repo_template(tmp_path_factory):
    from tests.test_team_workspace import git

    root = tmp_path_factory.mktemp('native-origin')
    origin, repo = root / 'origin.git', root / 'repo'
    git(root, 'init', '-q', '--bare', '-b', 'master', str(origin))
    git(root, 'clone', '-q', str(origin), str(repo))
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'base.txt').write_text('base')
    git(repo, 'add', 'base.txt')
    git(repo, 'commit', '-qm', 'fixture')
    git(repo, 'push', '-q', 'origin', 'master')
    team_workspace.task_worktree(_TASK_IDENT, root=repo)
    return repo


@pytest.fixture(scope='module')
def shared_app_clone(native_repo_template, tmp_path_factory):
    from tests.test_team_workspace import git

    root = tmp_path_factory.mktemp('app-clone')
    target = root / 'clone'
    git(root, 'clone', str(native_repo_template), str(target))
    return target


def test_files_confined_to_existing_worktree_files(tmp_path):
    root = tmp_path / 'task'
    root.mkdir()
    (root / 'result.txt').write_text('proof')
    (root / 'outside').symlink_to(tmp_path)
    assert team_workspace.validate_files(['result.txt'], root) == ['result.txt']
    for paths in (['../escape'], ['/etc/passwd'], ['outside/file'], ['missing'], ['.'], ['result.txt'] * 2):
        with pytest.raises(ValueError):
            team_workspace.validate_files(paths, root)


def test_native_files_validated(tmp_path):
    (tmp_path / 'proof.txt').write_text('proof')
    body = dict(run_id='x', stage='build', attempt=0, status='completed', summary='built',
                revision='a' * 64, files=['proof.txt'])
    assert team_chat.validate_result(body, run_id='x', stage='build', attempt=0,
                                     workspace=tmp_path)['files'] == ['proof.txt']
    body['files'] = ['../escape']
    with pytest.raises(ValueError):
        team_chat.validate_result(body, run_id='x', stage='build', attempt=0, workspace=tmp_path)


def app_row(tmp_path, provider='cursor', **kwargs):
    store = team_connected.ConnectedStore(tmp_path / 'store')
    row, _ = store.claim(room_id='a' * 32, provider=provider, request_id='build', text='Create proof',
                         config_hash='pin', **kwargs)
    return store, row


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_app_build_uses_task_path_and_keeps_outputs(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    root.mkdir()
    calls = []
    monkeypatch.setattr(team_workspace, 'task_worktree', lambda ident, **kw: calls.append(ident) or root)
    monkeypatch.setattr(team_connected, '_config', lambda provider: ({'workspace': str(tmp_path), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (object(), None))
    from companion import team_transport
    monkeypatch.setattr(team_transport, 'workspace_revision', lambda path: 'b' * 64)
    class Adapter:
        def run(self, request, **kwargs):
            assert Path(request.workspace) == root
            assert kwargs['authorize'](request)
            assert 'work in this directory only' in request.prompt
            (root / 'proof.txt').write_text('Built here')
            return AdapterResult('cursor', request.request_id, 'completed', json.dumps(dict(
                run_id=request.request_id, stage='build', attempt=0, status='completed',
                summary='Built the output', files=['proof.txt'], revision='b' * 64)), 'session', 'surface')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    store, row = app_row(tmp_path, execution_mode='code', reviewer='claude')
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == 'completed'
    assert calls == [row['id']]
    assert result['workspace'] == str(root)
    assert result['revision'] == 'b' * 64
    assert result['files'] == ['proof.txt']
    assert result['reviewer'] == 'claude'
    assert (result['session_id'], result['surface']) == ('session', 'surface')
    assert root.exists()


@pytest.mark.parametrize('provider,reason,action', [
    ('antigravity', 'permission_required', 'proceed-in-sandbox'),
    ('antigravity', 'permission_required:RunCommand', 'proceed-in-sandbox'),
    ('cursor', 'subscription_evidence_stale_or_mismatched', 'sign in'),
    ('cursor', 'permission_required', 'cli-config.json'),
])
@pytest.mark.parametrize('mode', ['reply', 'code'])
@pytest.mark.usefixtures("isolated_app_dispatch")
def test_retry_is_once_after_probe_and_keeps_session(tmp_path, monkeypatch, provider, reason, action, mode):
    events = []
    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(tmp_path), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (events.append('probe') or object(), None))
    class Adapter:
        def run(self, request, **kwargs):
            events.append('run')
            if events.count('run') == 2:
                assert request.session_id == 'session'
            return AdapterResult(provider, request.request_id, 'blocked', session_id='session', surface='surface', reason=reason)
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    task = tmp_path / 'task'
    task.mkdir()
    monkeypatch.setattr(team_workspace, 'task_worktree', lambda *a, **kw: task)
    store, row = app_row(tmp_path, provider, execution_mode=mode, reviewer='codex' if mode == 'code' else None)
    team_connected._work(store, row)
    result = store.get(row['id'])
    attempts = 1 if provider == 'cursor' and reason == 'permission_required' else 2
    assert events == (['probe', 'run'] * attempts)
    assert result['state'] == 'blocked'
    assert provider.lower() in result['text'].lower() and action in result['text']
    if provider == 'antigravity':
        assert 'run /config' in result['text']
        assert 'Terminal Sandbox on' in result['text']
        assert 'does not approve commands for you' in result['text']
        assert 'trust' not in result['text']
        assert 'pending request' not in result['text']
    if provider == 'cursor' and reason == 'permission_required':
        assert 'sandbox enabled' in result['text']
        assert 'sign in' not in result['text']
        assert 'pending' not in result['text']
    assert (result['session_id'], result['surface']) == ('session', 'surface')
    team_connected._work(store, row)
    assert events.count('run') == attempts


def test_code_never_accepts_plain_reply(tmp_path):
    row = {'id': 'test', 'provider': 'cursor', 'execution_mode': 'code', 'workspace': str(tmp_path)}
    assert team_connected._app_reply('Done', row) is None


@pytest.fixture
def native_task(native_repo_template, tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from companion.team_transport import workspace_revision
    from companion.workflow_control import ACTIONS, WorkflowStore
    from tests.test_team_chat_acceptance import FakeTransport

    ident = _TASK_IDENT
    repo = tmp_path / 'repo'
    _copy_task_checkout(native_repo_template, repo)
    task = repo / '.claude' / 'worktrees' / ('task-' + ident)
    pins = {role: {'id': f'fixture-{role}', 'cwd': str(repo)} for role in ('codex', 'claude')}
    controller = WorkflowStore(tmp_path / 'control.json')
    controller.configure(dict(task_ids=['fixture'], repo=str(repo),
                              actions=sorted(ACTIONS), chats=[{'app': role, **pin} for role, pin in pins.items()],
                              max_unknown_checks=2, max_checks=50, interval_seconds=60,
                              expires_at=time.time() + 3600, max_repair_attempts=2))
    root = tmp_path / 'store'
    root.mkdir()
    config = dict(version=2, workspace=str(repo), scope='Fixture scope', participants=pins,
                  controller=dict(task_id='fixture', version=controller.status()['version']))
    (root / 'config.json').write_text(json.dumps(config))
    store = team_chat.TeamStore(root, controller=controller)
    transport = FakeTransport()
    transport.revision_for = lambda binding: workspace_revision(binding['workspace'])
    service = team_chat.TeamService(store, transport, controller=controller)
    room = store.create_room('Fixture')['id']
    # Predict the one run ID solely to grant this real task path before admission.
    with monkeypatch.context() as patch:
        ids = iter([ident, 'c' * 32])
        patch.setattr(team_chat.uuid, 'uuid4', lambda: SimpleNamespace(hex=next(ids)))
        store.submit(room, 'Build proof', 'work', execution_mode='worker')
    return store, service, transport, room, ident, task, repo


def test_real_task_build_review_and_outputs_survive_completion(native_task):
    from companion.team_transport import workspace_revision
    from tests.test_team_chat_acceptance import respond
    from tests.test_team_workspace import git

    store, service, transport, room, ident, task, repo = native_task
    service.step(ident)
    assert transport.stages() == [('codex', 'build')]
    assignment = transport.sends[-1]['assignment_path'].read_text()
    assert str(task) in assignment and 'work in this directory only' in assignment
    assert service.controller.claims()[0]['repo'] == str(task)
    assert git(task, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'origin/master')
    (task / 'proof.txt').write_text('Output from a task worktree')
    revision = workspace_revision(task)
    respond(transport, 'build', summary='Built', revision=revision, files=['proof.txt'])
    service.step(ident)
    assert transport.stages()[-1] == ('claude', 'review')
    assert 'proof.txt' in transport.sends[-1]['assignment_path'].read_text()
    respond(transport, 'review', summary='Reviewed', revision=revision, verdict='pass')
    service.step(ident)
    assert store.get_room(room)['runs'][0]['status'] == 'completed'
    assert (task / 'proof.txt').read_text() == 'Output from a task worktree'
    assert not (repo / 'proof.txt').exists()


def test_real_revision_change_blocks_review_completion(native_task):
    from companion.team_transport import workspace_revision
    from tests.test_team_chat_acceptance import respond

    store, service, transport, room, ident, task, _ = native_task
    service.step(ident)
    revision = workspace_revision(task)
    respond(transport, 'build', summary='Built', revision=revision)
    service.step(ident)
    (task / 'changed.txt').write_text('Changed after review started')
    respond(transport, 'review', summary='Reviewed', revision=revision, verdict='pass')
    service.step(ident)
    assert store.get_room(room)['runs'][0]['status'] == 'needs_input'
    assert task.exists()


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_app_followup_resumes_and_reports_readonly_limit(tmp_path, monkeypatch):
    from companion.team_cursor import AdapterRequest

    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(tmp_path), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (object(), None))
    class Adapter:
        def run(self, request, **kwargs):
            assert isinstance(request, AdapterRequest) and request.read_only
            assert request.session_id == 'prior'
            assert 'do not build' in request.prompt
            kwargs['on_event']({'type': 'session', 'session_id': 'prior', 'surface': 'fixture'})
            raise RuntimeError('lost connection')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    store, row = app_row(tmp_path, read_only=True, session_id='prior', parent_request_id='parent')
    team_connected._work(store, row)
    saved = store.get(row['id'])
    assert saved['state'] == 'uncertain'
    assert saved['session_id'] == 'prior' and saved['surface'] == 'fixture'
    assert saved['read_only_enforced'] is False
    assert saved['workspace'] is None
    with pytest.raises(ValueError, match='request_changed'):
        store.claim(room_id='a' * 32, provider='cursor', request_id='build', text='Create proof',
                    config_hash='pin', read_only=True, session_id='different', parent_request_id='parent')


def test_external_review_checks_task_identity_before_send(native_task):
    from companion.team_transport import workspace_revision

    store, service, transport, room, ident, task, repo = native_task
    service.stop(room, run_id=ident)
    bad = store.submit(room, 'Review outputs', 'review', execution_mode='code_review',
                       workspace=str(repo), workspace_run_id=ident, revision=workspace_revision(task), files=[])['run_id']
    service.step(bad)
    assert not transport.sends
    assert store.get_room(room)['runs'][-1]['status'] == 'needs_input'


@pytest.mark.parametrize('verdict', ['pass', 'changes_requested', 'needs_input', 'status_needs_input'])
def test_external_review_uses_exact_app_task_and_returns_repairs(native_task, verdict):
    import inspect

    from companion.team_transport import workspace_revision
    from tests.test_team_chat_acceptance import respond

    store, service, transport, room, ident, task, _ = native_task
    if 'builder' not in inspect.signature(service.controller.reserve_dispatch).parameters:
        pytest.skip('Needs the separately owned G1 gate; also run against the G1 overlay')
    service.stop(room, run_id=ident)
    (task / 'proof.txt').write_text('App output')
    revision = workspace_revision(task)
    review = store.submit(room, 'Review app output', 'review', execution_mode='code_review', builder='cursor',
                          reviewer='codex', workspace=str(task), workspace_run_id=ident,
                          revision=revision, files=['proof.txt'])['run_id']
    service.step(review)
    assert transport.stages() == [('codex', 'review')]
    if verdict == 'status_needs_input':
        respond(transport, 'review', summary='Need input', status='needs_input')
    else:
        respond(transport, 'review', summary='Review result', revision=revision, verdict=verdict)
    service.step(review)
    run = store.get_room(room)['runs'][-1]
    assert run['status'] == ('completed' if verdict == 'pass' else 'needs_input')
    expected = 'needs_input' if verdict == 'status_needs_input' else verdict
    assert run['verdict'] == expected
    reopened = team_chat.TeamStore(store.root, controller=store.controller)
    assert reopened.get_room(room)['runs'][-1]['verdict'] == expected
    assert len(transport.sends) == 1
    assert task.exists()


def test_claude_build_is_reviewed_by_codex(native_task):
    import inspect

    from companion.team_transport import workspace_revision
    from tests.test_team_chat_acceptance import respond

    store, service, transport, room, ident, task, _ = native_task
    if 'builder' not in inspect.signature(service.controller.reserve_dispatch).parameters:
        pytest.skip('Needs the separately owned G1 gate; also run against the G1 overlay')
    # Fixture admission was ordinary Codex; use a fresh Claude admission in the same authorized task path.
    with store.edit() as data:
        _, run = store.find_run(data, ident)
        run.update(builder='claude', reviewer='codex')
        run['tasks'][0]['assignee'] = 'claude'
    service.step(ident)
    assert transport.stages() == [('claude', 'build')]
    revision = workspace_revision(task)
    respond(transport, 'build', summary='Built', revision=revision)
    service.step(ident)
    assert transport.stages()[-1] == ('codex', 'review')
    respond(transport, 'review', summary='Reviewed', revision=revision, verdict='pass')
    service.step(ident)
    assert store.get_room(room)['runs'][0]['status'] == 'completed'


def test_real_connected_stdio_build_writes_only_task_worktree(native_task, tmp_path, monkeypatch):
    from companion.team_cursor import CursorAdapter
    from companion.team_transport import workspace_revision
    from tests.test_team_cursor import ACP, evidence, peer

    native, _, _, _, _, _, repo = native_task
    from companion import workflow_control
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    frozen = tmp_path / 'frozen'
    frozen.mkdir()
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='stdio', text='Build', config_hash='pin', execution_mode='code', reviewer='codex')
    real = team_workspace.task_worktree
    monkeypatch.setattr(team_workspace, 'task_worktree', lambda ident, **kw: real(ident, root=repo))
    envelope = dict(run_id=row['id'], stage='build', summary='Created proof', files=['proof.txt'])
    script = 'from pathlib import Path\n' + ACP.replace(
        "    elif method == 'session/prompt':",
        "    elif method == 'session/prompt':\n        Path('proof.txt').write_text('stdio build output')")
    script = script.replace("'text':'fixture answer'", "'text':" + repr(json.dumps(envelope)))
    command = peer(tmp_path, script)
    cfg = dict(workspace=str(frozen), model='fixture-model', command=command)
    monkeypatch.setattr(team_connected, '_config', lambda p: (cfg, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (evidence(), None))
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: CursorAdapter(command))
    team_connected._work(store, row)
    result = store.get(row['id'])
    task = repo / '.claude' / 'worktrees' / ('task-' + row['id'])
    assert result['state'] == 'completed', result['reason']
    assert result['workspace'] == str(task)
    assert result['files'] == ['proof.txt']
    assert result['revision'] == workspace_revision(task)
    assert (task / 'proof.txt').read_text() == 'stdio build output'
    assert not (frozen / 'proof.txt').exists()
    assert result['session_id'] == 'fixture-session'
    assert result['surface'] == 'cursor_cli_acp'


def test_plan_hands_off_lane_claim_before_claiming_task_worktree(native_task):
    from tests.test_team_chat_acceptance import PLAN, respond

    store, service, transport, room, ident, task, _ = native_task
    with store.edit() as data:
        _, run = store.find_run(data, ident)
        run.update(execution_mode='plan', stage='plan', tasks=[])
    service.step(ident)
    respond(transport, 'plan', **PLAN)
    service.step(ident)
    assert transport.stages() == [('claude', 'plan'), ('codex', 'build')]
    assert store.get_room(room)['runs'][0]['workspace'] == str(task)
    assert service.controller.claims()[0]['repo'] == str(task)


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_result_session_survives_fingerprint_failure(tmp_path, monkeypatch):
    from companion import team_transport

    monkeypatch.setattr(team_workspace, 'task_worktree', lambda *a, **kw: tmp_path)
    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(tmp_path), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (object(), None))
    def failed_revision(path):
        raise ValueError('unavailable')
    monkeypatch.setattr(team_transport, 'workspace_revision', failed_revision)
    class Adapter:
        def run(self, request, **kwargs):
            return AdapterResult('cursor', request.request_id, 'completed',
                                 json.dumps(dict(run_id=request.request_id, summary='Built')),
                                 'result-session', 'result-surface')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    store, row = app_row(tmp_path, execution_mode='code', reviewer='codex')
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == 'uncertain'
    assert result['session_id'] == 'result-session' and result['surface'] == 'result-surface'

@pytest.mark.parametrize('provider', ['cursor', 'antigravity'])
@pytest.mark.parametrize('outside', [False, True])
def test_app_controller_claim(native_task, tmp_path, monkeypatch, provider, outside):
    from companion import workflow_control
    from tests.test_team_cursor import evidence

    native, _, _, _, _, task, repo = native_task
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider=provider, request_id='app-gate', text='Build',
                         config_hash='pin', execution_mode='code', reviewer='codex', session_id='prior')
    target = tmp_path if outside else team_workspace.task_worktree(row['id'], root=repo)
    cfg = {'workspace': str(target), 'model': 'fixture'}
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    monkeypatch.setattr(team_connected, '_config', lambda p: (cfg, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (evidence(), None))
    if outside:
        monkeypatch.setattr(team_workspace, 'task_worktree', lambda *a, **kw: target)
    calls = []
    class Adapter:
        def run(self, request, **kwargs):
            claims = native.controller.claims()
            assert len(claims) == 1
            assert claims[0]['repo'] == str(target)
            assert request.execution_mode == 'code'
            assert claims[0]['stage'] == 'build'
            assert claims[0]['participants'] == {provider: {'id': evidence().evidence_id, 'cwd': str(target)}}
            assert kwargs['authorize'](request)
            calls.append(request)
            return AdapterResult(provider, request.request_id, 'completed',
                                 json.dumps(dict(run_id=row['id'], summary='Done')), 'session', 'surface')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == ('blocked' if outside else 'completed'), result['reason']
    assert bool(calls) is not outside
    if outside:
        assert result['reason'] == 'gate_refused:workspace_not_authorized'
        assert result['session_id'] == 'prior'
    assert native.controller.claims() == []


@pytest.mark.parametrize('state', ['blocked', 'failed', 'uncertain'])
def test_app_claim_result_lifecycle(native_task, monkeypatch, state):
    from companion import workflow_control
    from tests.test_team_cursor import evidence

    native, _, _, _, _, task, _ = native_task
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    outcomes = []
    release = native.controller.release_claim
    def record_release(claim, *, outcome):
        outcomes.append(outcome)
        return release(claim, outcome=outcome)
    monkeypatch.setattr(native.controller, 'release_claim', record_release)
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='lifecycle', text='Reply',
                         config_hash='pin', session_id='prior', execution_mode='code', reviewer='codex')
    cfg = {'workspace': str(task), 'model': 'fixture'}
    monkeypatch.setattr(team_connected, '_config', lambda p: (cfg, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (evidence(), None))
    class Adapter:
        def run(self, request, **kwargs):
            assert kwargs['authorize'](request)
            return AdapterResult('cursor', request.request_id, state, session_id='returned',
                                 surface='fixture', reason='fixture_failure', read_only_enforced=True)
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert outcomes == ([] if state == 'uncertain' else ['needs_input'])
    assert result['state'] == state
    assert result['session_id'] == 'returned' and result['surface'] == 'fixture'
    assert result['read_only_enforced'] is True
    assert bool(native.controller.claims()) == (state == 'uncertain')
    assert bool(result['controller_claim']) == (state == 'uncertain')


def test_app_workspace_failure_is_definitive(native_task, monkeypatch):
    native, _, _, _, _, _, repo = native_task
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='workspace-failure', text='Build',
                         config_hash='pin', execution_mode='code', reviewer='codex')
    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(repo)}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (object(), None))
    def fail(ident, *, root, **kwargs):
        assert Path(root) == repo
        raise RuntimeError('fetch failed')
    monkeypatch.setattr(team_workspace, 'task_worktree', fail)
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: pytest.fail('must not dispatch'))
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == 'blocked'
    assert result['reason'] == 'task_workspace_unavailable'
    assert native.controller.claims() == []

@pytest.mark.parametrize('interrupted', [False, True])
def test_app_claim_rechecks_authority_and_retains_uncertain_send(native_task, monkeypatch, interrupted):
    from companion import workflow_control
    from tests.test_team_cursor import evidence

    native, _, _, _, _, task, _ = native_task
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider='cursor', request_id='revoke', text='Build', config_hash='pin', execution_mode='code', reviewer='codex')
    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(task), 'model': 'fixture'}, 'pin'))
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (evidence(), None))
    class Adapter:
        def run(self, request, **kwargs):
            assert kwargs['authorize'](request)
            kwargs['on_event']({'type': 'session', 'session_id': 'new', 'surface': 'fixture'})
            if interrupted:
                raise RuntimeError('lost process')
            native.controller.revoke()
            assert not kwargs['authorize'](request)
            return AdapterResult('cursor', request.request_id, 'blocked', reason='authorization_refused')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == ('uncertain' if interrupted else 'blocked')
    assert result['session_id'] == 'new' and result['surface'] == 'fixture'
    assert bool(native.controller.claims()) == interrupted


def test_app_retry_reacquires_claim_with_fresh_pin(native_task, monkeypatch):
    from dataclasses import replace

    from companion import workflow_control
    from tests.test_team_cursor import evidence

    native, _, _, _, _, task, _ = native_task
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider='antigravity', request_id='retry-pin', text='Build', config_hash='pin', execution_mode='code', reviewer='codex')
    monkeypatch.setattr(team_connected, '_config', lambda p: ({'workspace': str(task), 'model': 'fixture'}, 'pin'))
    probes, attempts = [], []
    def readiness(*args, **kwargs):
        value = replace(evidence(), evidence_id='fresh-' + str(len(probes)))
        probes.append(value)
        return value, None
    monkeypatch.setattr(team_connected, '_readiness', readiness)
    class Adapter:
        def run(self, request, **kwargs):
            claim, = native.controller.claims()
            assert kwargs['authorize'](request)
            assert claim['participants']['antigravity']['id'] == probes[-1].evidence_id
            attempts.append(claim['attempt'])
            return AdapterResult('antigravity', request.request_id, 'blocked', session_id='resumable',
                                 surface='fixture', reason='permission_required')
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    assert attempts == [0, 1]
    assert native.controller.claims() == []
    assert store.get(row['id'])['session_id'] == 'resumable'


@pytest.mark.parametrize('provider', ['cursor', 'antigravity'])
@pytest.mark.parametrize('mode', ['reply', 'code'])
def test_app_separate_clone_reply_without_claim(native_task, monkeypatch, provider, mode, shared_app_clone):
    from companion import workflow_control
    from tests.test_team_cursor import evidence

    native, _, _, _, _, _, _repo = native_task
    target = shared_app_clone
    store = team_connected.ConnectedStore(native.root)
    row, _ = store.claim(room_id='a' * 32, provider=provider, request_id='separate-clone',
                         text='Reply', config_hash='pin', execution_mode=mode,
                         reviewer='codex' if mode == 'code' else None, read_only=mode == 'reply')
    cfg = {'workspace': str(target), 'model': 'fixture'}
    monkeypatch.setattr(workflow_control, 'WorkflowStore', lambda: native.controller)
    monkeypatch.setattr(team_connected, '_config', lambda p: (cfg, 'pin'))
    entitlement = evidence(provider)
    monkeypatch.setattr(team_connected, '_readiness', lambda *a, **kw: (entitlement, None))
    if mode == 'code':
        monkeypatch.setattr(team_workspace, 'task_worktree', lambda *a, **kw: target)
    else:
        monkeypatch.setattr(native.controller, 'reserve_dispatch',
                            lambda **kw: pytest.fail('reply must not reserve a claim'))
    calls = []
    class Adapter:
        def run(self, request, **kwargs):
            assert request.workspace == str(target)
            assert request.read_only
            assert request.execution_mode == 'reply'
            assert kwargs['authorize'](request)
            assert kwargs['entitlement'] is entitlement
            assert native.controller.claims() == []
            calls.append(request)
            return AdapterResult(provider, request.request_id, 'completed',
                                 json.dumps(dict(run_id=row['id'], summary='Done')),
                                 'reply-session', 'fixture', read_only_enforced=True)
    monkeypatch.setattr(team_connected, '_adapter', lambda *a: Adapter())
    team_connected._work(store, row)
    result = store.get(row['id'])
    assert result['state'] == ('completed' if mode == 'reply' else 'blocked'), result['reason']
    assert bool(calls) == (mode == 'reply')
    assert not result['controller_claim']
    if mode == 'reply':
        assert result['session_id'] == 'reply-session'
        assert result['surface'] == 'fixture'
        assert result['read_only_enforced']
    else:
        assert result['reason'] == 'gate_refused:workspace_not_authorized'
