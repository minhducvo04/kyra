import shutil
import subprocess

import pytest

from companion import team_workspace as tw


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _init_repo(root, ignore_text=None, ignore_message='ignore'):
    origin = root / 'origin.git'
    git(root, '-c', 'init.templateDir=', 'init', '-q', '--bare', '-b', 'master', str(origin))
    main = root / 'main'
    git(root, '-c', 'init.templateDir=', 'clone', '-q', str(origin), str(main))
    git(main, 'config', 'user.email', 'test@example.com')
    git(main, 'config', 'user.name', 'Test')
    (main / 'a.txt').write_text('one\n')
    git(main, 'add', 'a.txt')
    git(main, 'commit', '-qm', 'first')
    git(main, 'push', '-q', 'origin', 'master')
    if ignore_text is not None:
        (main / '.gitignore').write_text(ignore_text)
        git(main, 'add', '.gitignore')
        git(main, 'commit', '-qm', ignore_message)
        git(main, 'push', '-q', 'origin', 'master')
    return main


def _copy_repo(seed_main, tmp_path):
    dest = tmp_path / 'bundle'
    cloned = subprocess.run(['cp', '-cR', str(seed_main.parent), str(dest)], capture_output=True)
    if cloned.returncode:
        shutil.copytree(seed_main.parent, dest)
    main = dest / 'main'
    git(main, 'remote', 'set-url', 'origin', str(dest / 'origin.git'))
    return main


@pytest.fixture(scope='module')
def _plain_seed(tmp_path_factory):
    return _init_repo(tmp_path_factory.mktemp('plain'))


@pytest.fixture(scope='module')
def _named_cache_seed(tmp_path_factory):
    return _init_repo(
        tmp_path_factory.mktemp('named'),
        ignore_text='__pycache__\n.pytest_history.jsonl\n',
        ignore_message='ignore by name',
    )


@pytest.fixture
def repo(_plain_seed, tmp_path):
    return _copy_repo(_plain_seed, tmp_path)


@pytest.fixture
def named_cache_repo(_named_cache_seed, tmp_path):
    return _copy_repo(_named_cache_seed, tmp_path)


def push_change(repo, name, text):
    (repo / name).write_text(text)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', f'change {name}')
    git(repo, 'push', '-q', 'origin', 'master')


def test_create_reuse_and_release(repo):
    path = tw.task_worktree('run1', root=repo)
    assert path == repo.resolve() / '.claude' / 'worktrees' / 'task-run1'
    assert git(path, 'rev-parse', '--abbrev-ref', 'HEAD') == 'team/run1'
    assert git(path, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'origin/master')
    assert tw.task_worktree('run1', root=repo) == path
    assert tw.release('run1', root=repo) is True
    assert not path.exists()
    assert git(repo, 'branch', '--list', 'team/run1')  # branch and its commits are kept
    assert tw.release('run1', root=repo) is False
    assert tw.task_worktree('run1', root=repo) == path  # recreated on the kept branch


def test_called_from_a_worktree_uses_main_checkout(repo):
    first = tw.task_worktree('outer', root=repo)
    second = tw.task_worktree('inner', root=first)
    assert second.parent == first.parent


def test_release_refuses_uncommitted_work(repo):
    path = tw.task_worktree('dirty', root=repo)
    (path / 'new.txt').write_text('work\n')
    with pytest.raises(tw.WorkspaceError, match='uncommitted'):
        tw.release('dirty', root=repo)
    assert (path / 'new.txt').exists()


def test_release_refuses_ignored_content(repo):
    (repo / '.gitignore').write_text('private/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-qm', 'ignore private')
    git(repo, 'push', '-q', 'origin', 'master')
    path = tw.task_worktree('ignored', root=repo)
    (path / 'private').mkdir()
    (path / 'private' / 'draft.txt').write_text('keep me\n')
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('ignored', root=repo)
    assert (path / 'private' / 'draft.txt').read_text() == 'keep me\n'


def test_release_allows_regenerable_caches(repo):
    (repo / '.gitignore').write_text('__pycache__/\n.pytest_cache/\n')
    git(repo, 'add', '.gitignore')
    git(repo, 'commit', '-qm', 'ignore caches')
    git(repo, 'push', '-q', 'origin', 'master')
    path = tw.task_worktree('caches', root=repo)
    (path / 'pkg' / '__pycache__').mkdir(parents=True)
    (path / 'pkg' / '__pycache__' / 'a.pyc').write_bytes(b'x')
    (path / '.pytest_cache').mkdir()
    assert tw.release('caches', root=repo) is True


def test_release_allows_root_history_file(named_cache_repo):
    path = tw.task_worktree('hist', root=named_cache_repo)
    (path / '.pytest_history.jsonl').write_text('{}\n')
    assert tw.release('hist', root=named_cache_repo) is True


def test_release_refuses_history_named_directory(named_cache_repo):
    path = tw.task_worktree('histdir', root=named_cache_repo)
    (path / '.pytest_history.jsonl').mkdir()
    (path / '.pytest_history.jsonl' / 'draft.txt').write_text('keep me\n')
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('histdir', root=named_cache_repo)
    assert (path / '.pytest_history.jsonl' / 'draft.txt').exists()


def test_release_refuses_history_file_below_root(named_cache_repo):
    path = tw.task_worktree('histsub', root=named_cache_repo)
    (path / 'sub').mkdir()
    (path / 'sub' / '.pytest_history.jsonl').write_text('keep me\n')
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('histsub', root=named_cache_repo)


def test_release_refuses_plain_file_named_like_a_cache(named_cache_repo):
    path = tw.task_worktree('fakecache', root=named_cache_repo)
    (path / 'pkg').mkdir()
    (path / 'pkg' / '__pycache__').write_text('keep me\n')
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('fakecache', root=named_cache_repo)
    assert (path / 'pkg' / '__pycache__').read_text() == 'keep me\n'


def test_release_refuses_symlinked_cache(named_cache_repo, tmp_path):
    path = tw.task_worktree('linkcache', root=named_cache_repo)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (path / '__pycache__').symlink_to(outside)
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('linkcache', root=named_cache_repo)


def test_release_refuses_symlink_inside_cache(named_cache_repo, tmp_path):
    path = tw.task_worktree('linkinside', root=named_cache_repo)
    (path / '__pycache__').mkdir()
    (path / '__pycache__' / 'link').symlink_to(tmp_path)
    with pytest.raises(tw.WorkspaceError, match='ignored'):
        tw.release('linkinside', root=named_cache_repo)


@pytest.mark.parametrize('bad', ['', '../x', 'a/b', '-x', '.hidden', 'x' * 65, None])
def test_rejects_unsafe_run_ids(bad):
    with pytest.raises(tw.WorkspaceError, match='invalid run id'):
        tw.task_worktree(bad)


def test_refuses_unregistered_directory(repo):
    stray = repo / '.claude' / 'worktrees' / 'task-stray'
    stray.mkdir(parents=True)
    with pytest.raises(tw.WorkspaceError, match='not a registered worktree'):
        tw.task_worktree('stray', root=repo)


def test_refresh_fast_forwards(repo):
    path = tw.task_worktree('ff', root=repo)
    push_change(repo, 'b.txt', 'two\n')
    head = tw.refresh(path)
    assert head == git(repo, 'rev-parse', 'HEAD')


def test_refresh_merges_when_diverged(repo):
    path = tw.task_worktree('div', root=repo)
    (path / 'c.txt').write_text('mine\n')
    git(path, 'add', 'c.txt')
    git(path, '-c', 'user.email=t@example.com', '-c', 'user.name=T', 'commit', '-qm', 'mine')
    push_change(repo, 'b.txt', 'two\n')
    git(path, 'config', 'user.email', 't@example.com')
    git(path, 'config', 'user.name', 'T')
    tw.refresh(path)
    assert (path / 'b.txt').exists() and (path / 'c.txt').exists()
    assert len(git(path, 'rev-list', '--parents', '-n1', 'HEAD').split()) == 3


def test_refresh_aborts_on_conflict(repo):
    path = tw.task_worktree('clash', root=repo)
    git(path, 'config', 'user.email', 't@example.com')
    git(path, 'config', 'user.name', 'T')
    (path / 'a.txt').write_text('mine\n')
    git(path, 'commit', '-qam', 'mine')
    before = git(path, 'rev-parse', 'HEAD')
    push_change(repo, 'a.txt', 'theirs\n')
    with pytest.raises(tw.WorkspaceError, match='conflicts'):
        tw.refresh(path)
    assert git(path, 'rev-parse', 'HEAD') == before
    assert git(path, 'status', '--porcelain') == ''


def test_refresh_refuses_dirty(repo):
    path = tw.task_worktree('busy', root=repo)
    (path / 'a.txt').write_text('edit\n')
    with pytest.raises(tw.WorkspaceError, match='uncommitted'):
        tw.refresh(path)


def test_role_line():
    assert tw.role_line(1, 'builder') == (
        'You are a builder in lane 1. You are not the planner; ignore home-chat conventions in memory.')
    assert 'reviewer in lane 2' in tw.role_line(2, 'reviewer')
    assert tw.role_line(0, 'planner').startswith('You are the planner in lane 0.')
    with pytest.raises(tw.WorkspaceError):
        tw.role_line(0, 'boss')
