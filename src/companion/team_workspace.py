"""Task worktrees and lane role lines for Team runs.

Each Team run that writes code gets its own worktree at
`.claude/worktrees/task-<run_id>` on branch `team/<run_id>`, cut from origin/master.
Nothing here forces, pushes, deletes a branch or touches `lane-*` worktrees.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

_RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
ROLES = ('planner', 'builder', 'reviewer')
_CACHE_DIRS = {'__pycache__', '.pytest_cache', '.ruff_cache'}  # regenerable; release may delete these
_HISTORY = '.pytest_history.jsonl'  # test_impact's run history, only as a regular file at the root


class WorkspaceError(RuntimeError):
    pass


def _git(cwd, *args):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    if done.returncode:
        raise WorkspaceError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def _main_checkout(root=None):
    """The primary checkout that owns `.claude/worktrees`, even when called from a worktree."""
    common = _git(root or Path(__file__).resolve().parent, 'rev-parse', '--path-format=absolute',
                  '--git-common-dir')
    return Path(common).resolve().parent


def _check_run_id(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise WorkspaceError(f'invalid run id: {run_id!r}')


def _path(run_id, root):
    _check_run_id(run_id)
    return _main_checkout(root) / '.claude' / 'worktrees' / f'task-{run_id}'


def _registered(main, path):
    listing = _git(main, 'worktree', 'list', '--porcelain')
    target = Path(path).resolve()
    return any(line.startswith('worktree ') and Path(line[9:]).resolve() == target
               for line in listing.splitlines())


def _dirty(path):
    return _git(path, 'status', '--porcelain')


def task_worktree(run_id, base='origin/master', root=None):
    """Fetch, then create (or reuse) the run's worktree on branch team/<run_id>."""
    path = _path(run_id, root)
    main = path.parents[2]
    branch = f'team/{run_id}'
    remote = base.split('/', 1)[0] if '/' in base else None
    if remote:
        _git(main, 'fetch', remote)
    if _registered(main, path):
        current = _git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
        if current != branch:
            raise WorkspaceError(f'{path} is on {current}, not {branch}')
        return path
    if path.exists():
        raise WorkspaceError(f'{path} exists but is not a registered worktree')
    has_branch = subprocess.run(['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'],
                                cwd=main).returncode == 0
    if has_branch:
        _git(main, 'worktree', 'add', str(path), branch)
    else:
        _git(main, 'worktree', 'add', '-b', branch, str(path), base)
    return path


def _is(path, kind):
    try:
        return kind(os.lstat(path).st_mode)
    except OSError:
        return False


def _regenerable(root, entry):
    """True only for a real cache directory (no symlinks on the way or inside) or the root history file."""
    rel = Path(entry.rstrip('/'))
    if rel == Path(_HISTORY):
        return _is(root / rel, stat.S_ISREG)
    parts = rel.parts
    hit = next((i for i, part in enumerate(parts) if part in _CACHE_DIRS), None)
    if hit is None or not all(_is(root.joinpath(*parts[:i + 1]), stat.S_ISDIR) for i in range(hit + 1)):
        return False
    cache = root.joinpath(*parts[:hit + 1])
    for folder, dirs, files in os.walk(cache):
        if not all(_is(Path(folder, d), stat.S_ISDIR) for d in dirs):
            return False
        if not all(_is(Path(folder, f), stat.S_ISREG) for f in files):
            return False
    return True


def release(run_id, root=None):
    """Remove the run's worktree. Refuses uncommitted or ignored files; keeps the branch and its commits."""
    path = _path(run_id, root)
    main = path.parents[2]
    if not _registered(main, path):
        return False
    pending = _dirty(path)
    if pending:
        raise WorkspaceError(f'{path} has uncommitted work; commit or discard it first:\n{pending}')
    listing = _git(path, 'status', '--porcelain', '--ignored=matching', '--untracked-files=all')
    ignored = '\n'.join(line for line in listing.splitlines() if not _regenerable(path, line[3:]))
    if ignored:
        raise WorkspaceError(f'{path} holds ignored files that removal would delete; move them first:\n{ignored}')
    _git(main, 'worktree', 'remove', str(path))
    return True


def refresh(path, base='origin/master'):
    """Bring a clean worktree up to date with base: fast-forward, else a merge commit. Never forces."""
    path = Path(path)
    if _dirty(path):
        raise WorkspaceError(f'{path} has uncommitted work; commit it before refreshing')
    if '/' in base:
        _git(path, 'fetch', base.split('/', 1)[0])
    try:
        _git(path, 'merge', '--ff-only', base)
    except WorkspaceError:
        try:
            _git(path, 'merge', '--no-edit', base)
        except WorkspaceError as error:
            subprocess.run(['git', 'merge', '--abort'], cwd=path, capture_output=True)
            raise WorkspaceError(f'{path} conflicts with {base}; resolve by hand') from error
    return _git(path, 'rev-parse', 'HEAD')


def role_line(lane, role):
    """The first line of every lane-chat assignment."""
    if role not in ROLES:
        raise WorkspaceError(f'unknown role: {role!r}')
    if role == 'planner':
        return f'You are the planner in lane {lane}. Decide with stated defaults; ask at most one blocking question.'
    return (f'You are a {role} in lane {lane}. You are not the planner; '
            'ignore home-chat conventions in memory.')


def validate_files(files, workspace):
    """Bound output names to existing regular files inside the assigned worktree."""
    if not isinstance(files, list) or len(files) > 32:
        raise ValueError('invalid_output_files')
    if workspace is None:
        raise ValueError('output_workspace_required')
    root = Path(workspace).resolve(strict=True)
    seen = set()
    for name in files:
        if (not isinstance(name, str) or not name or len(name) > 256 or '\\' in name
                or any(ord(c) < 32 for c in name)):
            raise ValueError('invalid_output_path')
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != name or name in seen:
            raise ValueError('invalid_output_path')
        try:
            target = (root / path).resolve(strict=True)
            target.relative_to(root)
            if not target.is_file():
                raise ValueError('output_not_regular_file')
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError('output_outside_worktree_or_missing') from exc
        seen.add(name)
    return list(files)


SNAPSHOT_PATHS = ('README.md', 'apple', 'scripts', 'src', 'tests')
_SNAPSHOT_MARK = '.kyra-snapshot'


def refresh_snapshot(workspace, ref='master', root=None):
    """Bring the read-only app workspace (Cursor, Antigravity replies) up to `ref`'s tracked files.

    Only SNAPSHOT_PATHS are replaced, from `git archive`, so nothing untracked (data/, .env) can enter it.
    Returns the commit it now holds; a no-op when the marker already names that commit.
    """
    import fcntl
    import shutil
    import tarfile
    import tempfile
    workspace = Path(workspace)
    if not workspace.is_absolute() or not workspace.is_dir() or (workspace / '.git').exists():
        raise WorkspaceError(f'not a snapshot workspace: {workspace}')
    main = _main_checkout(root)
    head = _git(main, 'rev-parse', '--verify', f'{ref}^{{commit}}')
    mark = workspace / _SNAPSHOT_MARK
    # One refresher at a time; the marker is re-read under the lock so a second caller does nothing (review item).
    with open(workspace / '.kyra-snapshot.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mark.is_file() and mark.read_text().strip() == head:
            return head
        # Build beside the paths (same filesystem) and swap each with a rename, so a reader never sees one missing.
        with tempfile.TemporaryDirectory(dir=workspace, prefix='.kyra-snapshot-') as tmp:
            archive = Path(tmp) / 'snapshot.tar'
            _git(main, 'archive', '--format=tar', '-o', str(archive), head, *SNAPSHOT_PATHS)
            with tarfile.open(archive) as tar:
                tar.extractall(Path(tmp) / 'tree', filter='data')
            old = Path(tmp) / 'old'
            old.mkdir()
            for name in SNAPSHOT_PATHS:
                target = workspace / name
                if target.exists() or target.is_symlink():
                    target.rename(old / name)
                (Path(tmp) / 'tree' / name).rename(target)
            shutil.rmtree(old)
        mark.write_text(head + '\n')
    return head
