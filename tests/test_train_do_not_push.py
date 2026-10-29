"""No network or pushes: fake Git ancestry responses exercise both train boundaries."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import push_check  # noqa: E402

_spec = importlib.util.spec_from_file_location('train_dnp', SCRIPTS / 'train.py')
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)


@pytest.mark.parametrize('ref', ['session/test-branch', '1234567'])
def test_tainted_branch_or_tip_refused(tmp_path, monkeypatch, ref):
    path = tmp_path / 'data/team/do-not-push.txt'
    path.parent.mkdir(parents=True)
    path.write_text('# private list\nabcdef0 # blocked\n')
    calls = []
    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(train, 'run', runner)
    with pytest.raises(train.TrainError, match=f'abcdef0 is an ancestor of {ref}') as error:
        train.check_push_history(tmp_path, [ref], tmp_path)
    assert error.value.code == 1
    assert calls[0][-4:] == ['merge-base', '--is-ancestor', 'abcdef0', ref]


def test_clean_passes_and_missing_logs_skip(tmp_path):
    path = tmp_path / 'list'
    path.write_text('abcdef0\n')
    push_check.check(tmp_path, ['clean'], path,
                     runner=lambda argv: subprocess.CompletedProcess(argv, 1))
    lines = []
    push_check.check(tmp_path, ['clean'], tmp_path / 'missing', out=lines.append,
                     runner=lambda argv: pytest.fail('Git called without list'))
    assert lines == ['do-not-push list missing; ancestry check skipped']


def test_create_refuses_before_worktree_or_merge(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(train, 'git', lambda *args, **kw: calls.append(args))
    def refuse(*args):
        raise train.TrainError('tainted branch', 1)
    monkeypatch.setattr(train, 'check_push_history', refuse)
    with pytest.raises(train.TrainError):
        train.create(tmp_path, 'sample', 'origin/master', ['branch'])
    assert not any('merge' in call or 'worktree' in call for call in calls)


def test_tainted_merged_tip_never_calls_land(tmp_path, monkeypatch):
    monkeypatch.setattr(train, 'create', lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(train, 'git', lambda repo, *args: 'session/train' if args[0] == 'branch' else '1234567')
    monkeypatch.setattr(train, 'changed_files', lambda *args: [])
    monkeypatch.setattr(train, 'land', lambda *args: pytest.fail('tainted tip landed'))
    path = tmp_path / 'data/team/do-not-push.txt'
    path.parent.mkdir(parents=True)
    path.write_text('abcdef0\n')
    monkeypatch.setattr(train, 'run', lambda argv: subprocess.CompletedProcess(argv, 0))
    with pytest.raises(train.TrainError, match='abcdef0 is an ancestor of session/train'):
        train.train('sample', 'base', ['branch'], main=tmp_path,
                    checks_fn=lambda *args: [])
