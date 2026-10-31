"""Scratch Git/store journeys; no native sends or production state."""
import os
import re
import subprocess
import time
from types import SimpleNamespace

import pytest

from companion import team_chat, team_connected
from companion.team_transport import workspace_revision
from tests.test_team_all_builders_d1 import planned, task
from tests.test_team_chat_acceptance import FakeTransport, bound, respond


def _git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def _reuse_checkout(link, target):
    """Point the per-test checkout at the shared repo. Seat tests only read it."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture(scope='module')
def seat_repo(tmp_path_factory):
    """One checkout and two task worktrees, created once. Tests do not commit."""
    checkout = tmp_path_factory.mktemp('seat-repo') / 'checkout'
    checkout.mkdir()
    _git(checkout, 'init', '-q', '-b', 'master')
    _git(checkout, 'config', 'user.email', 'test@example.com')
    _git(checkout, 'config', 'user.name', 'Test')
    (checkout / 'README.md').write_text('fixture\n', encoding='utf-8')
    _git(checkout, 'add', 'README.md')
    _git(checkout, 'commit', '-qm', 'first')
    paths = []
    for name in ('a', 'b'):
        path = checkout / '.claude' / 'worktrees' / f'task-{name}'
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(checkout, 'worktree', 'add', '-q', '-b', f'team/{name}', str(path), 'HEAD')
        paths.append(path)
    return checkout, tuple(paths)


@pytest.fixture
def team(tmp_path, monkeypatch, seat_repo):
    """Fresh store and transport. Worktrees are reused and only read."""
    checkout, paths = seat_repo
    _reuse_checkout(tmp_path / 'checkout', checkout)
    assigned, pool = {}, list(paths)

    def workspace(run_id, _binding):
        if run_id not in assigned:
            assigned[run_id] = pool.pop(0)
        return assigned[run_id]

    monkeypatch.setattr(team_chat, '_task_workspace', workspace)
    monkeypatch.setattr(team_connected, 'team_handbook', lambda: 'Synthetic handbook for the seat wait fixture.')
    transport = FakeTransport()
    transport.codex_home = tmp_path / 'codex-home'
    transport.codex_home.mkdir()
    transport.revision_for = lambda binding: workspace_revision(binding['workspace'])
    store, service = bound(team_chat, tmp_path, transport)
    return SimpleNamespace(store=store, service=service, transport=transport)


def worker(t, name, builder):
    room = t.store.create_room(name)['id']
    ident = t.store.submit(room, 'Build fixture', name, execution_mode='worker', builder=builder,
                           reviewer='claude' if builder == 'codex' else 'codex')['run_id']
    t.service.step(ident)
    return room, ident


def saved(t, ident):
    return t.store.find_run(t.store._read(), ident)[1]


def finish_build(t, ident):
    respond(t.transport, 'build', ident, summary='Built fixture.',
            revision=workspace_revision(saved(t, ident)['workspace']))
    t.service.step(ident)


def test_crossed_builders_release_finished_seats(team):
    _, first = worker(team, 'First', 'codex')
    _, second = worker(team, 'Second', 'claude')
    assert len(team.transport.sends) == 2
    finish_build(team, first)
    assert saved(team, first)['status'] == 'queued'
    assert not any(c['run_id'] == first for c in team.store.controller.claims())
    finish_build(team, second)
    team.service.step(first)
    for ident in (first, second):
        assert saved(team, ident)['stage'] == 'review'
        assert saved(team, ident)['phase'] == 'waiting'
        respond(team.transport, 'review', ident, summary='Checked fixture.', verdict='pass',
                revision=saved(team, ident)['revision'])
        team.service.step(ident)
        assert saved(team, ident)['status'] == 'completed'
    assert not team.store.controller.claims()


@pytest.mark.parametrize('kind', ['seat', 'codex'])
def test_wait_reason_rollover_and_one_line_cadence(team, monkeypatch, kind):
    now = [time.time()]
    monkeypatch.setattr(team_chat.time, 'time', lambda: now[0])
    _, holder = worker(team, 'Holder', 'codex')
    if kind == 'seat':
        room, ident = worker(team, 'Waiting', 'codex')
        with team.store.edit() as data:
            holder_room, _ = team.store.find_run(data, holder)
            holder_room['room']['title'] = 'Holder\nproject'
        target = f'seat held by run {holder} in Holder project'
    else:
        ident = holder
        room = team.store.find_run(team.store._read(), ident)[0]['room']['id']
        target = 'Codex chat fixture-code'
    pattern = re.compile(r'waiting on (Codex chat .+|seat held by run [a-f0-9]{32} in .+) · (\d+) min')
    for seconds, minutes in ((59, 0), (1, 1), (240, 5)):
        now[0] += seconds
        team.service.step(ident)
        reason = saved(team, ident)['reason']
        assert pattern.fullmatch(reason)
        assert reason == f'waiting on {target} · {minutes} min'
    def lines():
        return [m for m in team.store.get_room(room)['messages'] if m.get('kind') == 'step'
                and m['text'].startswith('waiting on ')]
    assert not lines()
    now[0] += 1
    team.service.step(ident)
    original = lines()
    assert len(original) == 1
    now[0] += 899
    team.service.step(ident)
    assert lines() == original
    now[0] += 1
    # A new service instance must retain the cadence and update the same message.
    team_chat.TeamService(team.store, team.transport, controller=team.store.controller).step(ident)
    assert len(lines()) == 1 and lines()[0]['id'] == original[0]['id']
    assert lines()[0]['text'] == f'waiting on {target} · 20 min'
    assert len([s for s in team.transport.sends if s['run_id'] == ident]) == (0 if kind == 'seat' else 1)


def test_stopped_ordered_child_cannot_leave_parent_running(tmp_path):
    store, room, providers, coordinator, calls = planned(tmp_path, [task('codex'), task('claude', 2)])
    row = coordinator.advance(room, 'post')
    providers.states[row['children']['task:1']['id']].update(state='stopped', reason='Stopped by user')
    row = coordinator.advance(room, 'post')
    assert row['state'] == 'needs_input'
    assert 'task:1' in row['reason'] and 'new request' in row['reason'].lower()
    assert any('new request' in m['text'].lower() for m in store.get_room(room)['messages'])
    before = len(calls)
    coordinator.advance(room, 'post')
    assert len(calls) == before


@pytest.mark.parametrize('crash_before_notice', [False, True])
def test_forced_cycle_finishes_younger_and_names_holder(team, monkeypatch, crash_before_notice):
    # Simulate the persisted pre-fix state: both replies consumed, both old
    # build seats retained. Recovery must choose age, not which run polls.
    room_a, first = worker(team, 'Older', 'codex')
    room_b, second = worker(team, 'Younger', 'claude')
    with team.store.edit() as data:
        _, older = team.store.find_run(data, first)
        _, younger = team.store.find_run(data, second)
        younger['created_at'] = older['created_at'] + 1
    monkeypatch.setattr(team.service, '_release_finished_stage', lambda _: None)
    finish_build(team, first)
    original_post = team.store.post_step
    if crash_before_notice:
        def interrupted(*args, **kwargs):
            raise OSError('simulated notice interruption')
        monkeypatch.setattr(team.store, 'post_step', interrupted)
        with pytest.raises(OSError, match='notice interruption'):
            finish_build(team, second)
        monkeypatch.setattr(team.store, 'post_step', original_post)
        team_chat.TeamService(team.store, team.transport, controller=team.store.controller).step(second)
    else:
        finish_build(team, second)
    assert saved(team, second)['status'] == 'needs_input'
    assert saved(team, first)['status'] == 'queued'
    lines = [m for m in team.store.get_room(room_b)['messages']
             if m.get('kind') == 'step' and 'Seat wait cycle' in m['text']]
    assert len(lines) == 1 and first in lines[0]['text'] and 'Older' in lines[0]['text']
    assert not any(c['run_id'] == second for c in team.store.controller.claims())
    team.service.step(second)
    assert len([m for m in team.store.get_room(room_b)['messages']
                if m.get('kind') == 'step' and 'Seat wait cycle' in m['text']]) == 1


def test_released_seat_successor_crash_still_requires_exact_reconciliation(team, monkeypatch):
    _, first = worker(team, 'First', 'codex')
    _, second = worker(team, 'Second', 'claude')
    old = saved(team, first)['claim']
    finish_build(team, first)
    assert saved(team, first)['claim_released'] is True
    finish_build(team, second)
    sends = len(team.transport.sends)
    real = team_chat.write_json
    def failing(path, value, **kwargs):
        if path == team.store.path and team.store.find_run(value, first)[1]['phase'] == 'dispatching':
            raise OSError('fixture successor save failed')
        return real(path, value, **kwargs)
    monkeypatch.setattr(team_chat, 'write_json', failing)
    with pytest.raises(OSError, match='successor save failed'):
        team.service.step(first)
    monkeypatch.setattr(team_chat, 'write_json', real)
    assert saved(team, first)['status'] == 'uncertain'
    current = next(c for c in team.store.controller.claims() if c['run_id'] == first)
    assert current['id'] != old['id'] and current['stage'] == 'review'
    with pytest.raises(PermissionError):
        team.service.reconcile(first, worker_ended=True)
    assert current in team.store.controller.claims()
    team.service.reconcile(first, worker_ended=True, expected_claim=current)
    assert saved(team, first)['status'] == 'stopped'
    assert len(team.transport.sends) == sends
