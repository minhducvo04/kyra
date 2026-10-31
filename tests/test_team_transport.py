"""Team handoffs use explicit Codex exec effort and preserve Claude's selected model."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion import app_dispatch
from companion.app_tasks import AppTask

pytestmark = pytest.mark.usefixtures("legacy_lane_workspaces")


@pytest.mark.parametrize("version", [1, 2, 3])
def test_ls_team_config_defaults_and_explicit_receptionist(tmp_path, version):
    from companion import team_transport

    pair = {role: dict(id=f"{role}-fixture", cwd=str(tmp_path)) for role in ("claude", "codex")}
    config = dict(version=version, workspace=str(tmp_path), scope="Fixture", participants=pair)
    if version >= 2:
        config["controller"] = dict(task_id="fixture", version=1)
    if version == 3:
        config["lanes"] = [dict(workspace=str(tmp_path), participants=config.pop("participants"))]
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert team_transport.read_config(tmp_path) == config  # Keep legacy binding fingerprints stable.
    config["receptionist_threads"] = ["receptionist-fixture"]
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert team_transport.read_config(tmp_path)["receptionist_threads"] == ["receptionist-fixture"]
    for bad in ("receptionist-fixture", [1], [""]):
        with pytest.raises(ValueError, match="receptionist"):
            team_transport.validate_config(dict(config, receptionist_threads=bad))


def test_ls_configure_defaults_and_preserves_receptionist_threads(tmp_path, monkeypatch):
    from companion import team_chat, team_transport
    from tests.test_team_lanes import bind_lanes

    store, _, _, authority, lanes = bind_lanes(team_chat, tmp_path, 1)
    tasks = [AppTask(role, pin["id"], pin["id"], pin["cwd"], True, 1)
             for lane in lanes for role, pin in lane["participants"].items()]
    monkeypatch.setattr(team_transport, "discover", lambda: tasks)
    config = team_transport.read_config(store.root)
    kwargs = dict(workspace=config["workspace"], scope=config["scope"], lanes=lanes,
                  controller=config["controller"], authority=authority)
    team_transport.configure(store.root, **kwargs)
    assert team_transport.read_config(store.root)["receptionist_threads"] == []
    team_transport.configure(store.root, **kwargs, receptionist_threads=["receptionist-fixture"])
    team_transport.configure(store.root, **kwargs)
    assert team_transport.read_config(store.root)["receptionist_threads"] == ["receptionist-fixture"]


@pytest.fixture
def ledger_routes(tmp_path, monkeypatch):
    import json
    from functools import partial

    from companion import team_transport

    root = tmp_path / 'team'
    root.mkdir()
    task = AppTask('codex', 'fixture-chat', 'Fixture', str(tmp_path), True, 1)
    config = dict(version=2, workspace=str(tmp_path), scope='Fixture task',
                  participants={role: dict(id='fixture-chat', cwd=str(tmp_path)) for role in ('claude', 'codex')},
                  controller=dict(task_id='fixture', version=1))
    (root / 'config.json').write_text(json.dumps(config))
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='queued')
    monkeypatch.setattr(team_transport, 'discover', lambda: [task])
    monkeypatch.setattr(app_dispatch, 'send', partial(app_dispatch.send, runner=runner))
    transport = team_transport.VisibleTeamTransport(root, controller=object(), codex_home=tmp_path / 'codex')
    assignment, result = tmp_path / 'task.md', tmp_path / 'result.json'
    def team_send():
        return transport.send(role='codex', run_id='fixture-run', stage='build', attempt=0,
                              assignment_path=assignment, result_path=result, binding=config)
    def web_send():
        return app_dispatch.send(task, assignment, result, kind='bounded_build',
                                 store_path=tmp_path / 'dispatch' / 'deliveries.json')
    return SimpleNamespace(team=team_send, web=web_send, calls=calls, transport=transport, config=config,
                           assignment=assignment, result=result)


@pytest.mark.parametrize('first,second', [('team', 'web'), ('web', 'team')])
def test_both_routes_share_the_duplicate_guard(ledger_routes, tmp_path, first, second):
    routes = ledger_routes
    receipt = getattr(routes, first)()
    assert receipt['state'] == 'queued'
    with pytest.raises(app_dispatch.DuplicateDelivery):
        getattr(routes, second)()
    assert len(routes.calls) == 1
    rows = app_dispatch.load(tmp_path / 'dispatch' / 'deliveries.json')
    assert [row['id'] for row in rows] == [receipt['id']]


@pytest.mark.parametrize('state', ['queued', 'running', 'acknowledged'])
def test_team_status_observes_the_canonical_receipt(ledger_routes, tmp_path, state):
    import json
    from datetime import UTC, datetime

    routes = ledger_routes
    receipt = routes.web()
    rollout = tmp_path / 'codex' / 'sessions' / 'rollout-fixture-chat.jsonl'
    rollout.parent.mkdir(parents=True)
    header = json.dumps(dict(type='session_meta', payload=dict(id='fixture-chat'))) + '\n'
    rollout.write_text(header + json.dumps(dict(timestamp=datetime.now(UTC).isoformat(), type='event_msg',
                                               payload=dict(type='task_started'))) + '\n')
    receipt.update(rollout=str(rollout), rollout_offset=len(header.encode()), state=state)
    (tmp_path / 'dispatch' / 'deliveries.json').write_text(json.dumps([receipt]))
    run = dict(stage='build', delivery_id=receipt['id'], submitted_config=routes.config,
               assignment_path=str(routes.assignment), result_path=str(routes.result))
    assert routes.transport.delivery_status(run) == 'running'


def test_concurrent_routes_reserve_only_one_delivery(ledger_routes, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    routes = ledger_routes
    barrier = Barrier(2)
    def race(send):
        barrier.wait(timeout=5)
        try:
            return send()['state']
        except app_dispatch.DuplicateDelivery:
            return 'duplicate'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [routes.web, routes.team]))
    assert sorted(results) == ['duplicate', 'queued']
    assert len(routes.calls) == 1
    assert len(app_dispatch.load(tmp_path / 'dispatch' / 'deliveries.json')) == 1


def test_codex_handoff_uses_fresh_exec_with_explicit_model_and_effort(tmp_path, monkeypatch):
    from companion import paths

    monkeypatch.setattr(paths, 'DATA_DIR', tmp_path)
    (tmp_path / 'team').mkdir()
    (tmp_path / 'team' / 'effort-policy.json').write_text(json.dumps({'default': 'medium'}))
    sent = []
    task = AppTask('codex', 'fixture-chat', 'Fixture', str(tmp_path), True, 1)
    assignment = tmp_path / 'assignment.md'
    assignment.write_text('Fixture task')
    def runner(argv, **kwargs):
        sent.append(argv)
        return SimpleNamespace(returncode=0, stdout='Finished fixture')
    receipt = app_dispatch.send(task, assignment, tmp_path / 'result.json', kind='bounded_build',
                                store_path=tmp_path / 'deliveries.json', runner=runner, preserve_model=True)
    assert receipt['state'] == 'queued'
    assert len(sent) == 1
    argv = sent[0]
    assert argv[1] == 'exec'
    assert not {'queue', 'resume', '--thread', task.id}.intersection(argv)
    assert argv[argv.index('-m') + 1] == receipt['model'] == 'gpt-6-astra'
    assert argv[argv.index('-c') + 1] == 'model_reasoning_effort="medium"'
    assert receipt['effort'] == 'medium'
    assert Path(argv[argv.index('-C') + 1]) == tmp_path
    assert receipt['rollout'] is None and receipt['rollout_offset'] == 0


def test_revision_includes_dirty_and_untracked_code_but_not_ignored_state(tmp_path):
    import subprocess

    from companion.team_transport import workspace_revision
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    git('config', 'user.email', 'fixture@example.invalid')
    git('config', 'user.name', 'Fixture')
    (tmp_path / '.gitignore').write_text('runtime/\n')
    source = tmp_path / 'code.txt'
    source.write_text('one')
    git('add', '.')
    git('commit', '-m', 'Fixture')
    original = workspace_revision(tmp_path)
    source.write_text('two')
    dirty = workspace_revision(tmp_path)
    assert dirty != original
    (tmp_path / 'new.txt').write_text('new')
    untracked = workspace_revision(tmp_path)
    assert untracked != dirty
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime' / 'state.json').write_text('{}')
    assert workspace_revision(tmp_path) == untracked


def test_changed_binding_does_not_send_a_later_stage(tmp_path):
    import json

    from companion import team_chat
    from tests.test_team_chat_acceptance import FakeTransport, bound
    transport = FakeTransport()
    store, service = bound(team_chat, tmp_path, transport)
    room = store.create_room('Fixture')
    run_id = store.submit(room['id'], 'Fixture task', 'req')['run_id']
    service.step(run_id)
    run = store.get_room(room['id'])['runs'][0]
    Path(run['result_path']).write_text(json.dumps(dict(run_id=run_id, stage='plan', attempt=0,
        status='completed', summary='Ready', tasks=[dict(title='Fixture', instruction='Fixture task')])) )
    config = tmp_path / 'team' / 'config.json'  # commit-1 seams: the binding is the config pinned at submit
    config.write_text(json.dumps({**json.loads(config.read_text()), 'scope': 'Changed after submit.'}))
    service.step(run_id)
    assert transport.stages() == [('claude', 'plan')]
    messages = store.get_room(room['id'])['messages']
    assert any(message['author'] == 'claude' for message in messages)  # the sent stage's reply is still ingested


def test_preflight_blocked_worker_exits_and_releases_without_retry(tmp_path):
    import threading
    import time

    from companion import team_http
    from companion.team_chat import TeamService, TeamStore
    from tests.test_team_chat_acceptance import authorize
    class Blocked:
        def send(self, **kwargs):
            return {'state': 'blocked'}
    controller = authorize(tmp_path)
    store = TeamStore(tmp_path / 'team', controller=controller)
    room = store.create_room('Fixture')
    store.submit(room['id'], 'Fixture task', 'req')
    before = {thread.ident for thread in threading.enumerate()}
    team_http.wake_service(TeamService(store, Blocked(), controller=controller))
    key = str((tmp_path / 'team').resolve())
    # wake_service runs on a kyra-team thread and removes its own key when the
    # blocked turn releases. Join that thread instead of sleeping between polls.
    deadline = time.monotonic() + 3
    spawned = [thread for thread in threading.enumerate()
               if thread.name == 'kyra-team' and thread.ident not in before]
    for thread in spawned:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(remaining)
    assert key not in team_http._workers
    run = store.get_room(room['id'])['runs'][0]
    assert run['status'] == 'needs_input' and run['role_lease'] is None
    assert controller.claims() == []


def test_room_blocked_by_uncertain_participant_does_not_keep_polling(tmp_path):
    from companion import team_chat
    from tests.test_team_chat_acceptance import FakeTransport, bound
    transport = FakeTransport(states=['ambiguous'])
    store, service = bound(team_chat, tmp_path, transport)
    first = store.create_room('First')
    second = store.create_room('Second')
    a = store.submit(first['id'], 'Fixture first', 'a')['run_id']
    service.step(a)
    b = store.submit(second['id'], 'Fixture second', 'b')['run_id']
    service.step(b)
    assert store.get_room(second['id'])['runs'][0]['status'] == 'needs_input'
    assert store.active_runs() == []
    assert len(transport.sends) == 1


def test_each_dispatch_gets_a_fresh_stage_deadline(tmp_path, monkeypatch):
    import json

    from companion import team_chat
    from tests.test_team_chat_acceptance import FakeTransport, bound
    now = [1000]
    monkeypatch.setattr(team_chat.time, 'time', lambda: now[0])
    store, service = bound(team_chat, tmp_path, FakeTransport())  # the grant is made on the same patched clock
    room = store.create_room('Fixture')
    run_id = store.submit(room['id'], 'Fixture', 'req')['run_id']
    service.step(run_id)
    run = store.get_room(room['id'])['runs'][0]
    assert run['expires_at'] == 1000 + team_chat.STAGE_TIMEOUT_SECONDS
    now[0] += 1900
    Path(run['result_path']).write_text(json.dumps(dict(run_id=run_id, stage='plan', attempt=0,
        status='completed', summary='Ready', tasks=[dict(title='Fixture', instruction='Fixture task')])))
    service.step(run_id)
    run = store.get_room(room['id'])['runs'][0]
    assert run['stage'] == 'build' and run['status'] == 'building'
    assert run['expires_at'] == now[0] + team_chat.STAGE_TIMEOUT_SECONDS


def test_store_refuses_growth_before_existing_history_becomes_unreadable(tmp_path, monkeypatch):
    import pytest

    from companion import team_chat
    store = team_chat.TeamStore(tmp_path)
    room = store.create_room('Fixture')
    original = store.path.read_bytes()
    monkeypatch.setattr(team_chat, 'MAX_STORE_BYTES', len(original) + 10, raising=False)
    with pytest.raises(ValueError):
        store.submit(room['id'], 'Fixture message', 'req', share_with_team=False)
    assert store.path.read_bytes() == original
    assert store.get_room(room['id'])['messages'] == []
