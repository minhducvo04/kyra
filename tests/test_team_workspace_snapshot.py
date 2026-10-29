"""The app workspace snapshot follows master's tracked files only (2026-10-04)."""
import subprocess

import pytest

from companion.team_workspace import WorkspaceError, refresh_snapshot


def _repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def run(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    run('init', '-q', '-b', 'master')
    run('config', 'user.email', 't@example.com')
    run('config', 'user.name', 't')
    for name in ('README.md', 'src/a.py', 'scripts/s.py', 'tests/t.py', 'apple/x.swift'):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text('one\n')
    (repo / '.gitignore').write_text('data/\n.env\n')
    (repo / 'data').mkdir()
    (repo / 'data' / 'private.txt').write_text('secret\n')
    (repo / '.env').write_text('KEY=x\n')
    run('add', '.')
    run('commit', '-qm', 'one')
    return repo, run


def test_snapshot_copies_tracked_paths_only_and_follows_new_commits(tmp_path):
    repo, run = _repo(tmp_path)
    ws = tmp_path / 'ws'
    ws.mkdir()
    (ws / 'src').mkdir()
    (ws / 'src' / 'stale.py').write_text('old\n')
    first = refresh_snapshot(ws, root=repo)
    assert (ws / 'src' / 'a.py').read_text() == 'one\n'
    assert not (ws / 'src' / 'stale.py').exists()
    assert not (ws / 'data').exists() and not (ws / '.env').exists()
    (repo / 'src' / 'a.py').write_text('two\n')
    run('commit', '-qam', 'two')
    second = refresh_snapshot(ws, root=repo)
    assert second != first and (ws / 'src' / 'a.py').read_text() == 'two\n'
    assert refresh_snapshot(ws, root=repo) == second


def test_snapshot_refuses_a_git_checkout(tmp_path):
    repo, _ = _repo(tmp_path)
    with pytest.raises(WorkspaceError):
        refresh_snapshot(repo, root=repo)
