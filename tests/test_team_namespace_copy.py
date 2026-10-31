"""Controller identity survives archive checkouts and symlinked temp roots."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from companion.team_chat import TeamStateError, TeamStore
from companion.workflow_control import WorkflowStore


def _serial_child_env(**overrides):
    env = {**os.environ, **overrides}
    # This child runs serially even when its parent is an xdist worker.
    env.pop('PYTEST_XDIST_WORKER', None)
    env.pop('PYTEST_XDIST_TESTRUNUID', None)
    return env


def test_serial_child_env_drops_parent_worker_identity(monkeypatch):
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'gw0')
    monkeypatch.setenv('PYTEST_XDIST_TESTRUNUID', 'parent-run')
    env = _serial_child_env(TMPDIR='/tmp')
    assert 'PYTEST_XDIST_WORKER' not in env
    assert 'PYTEST_XDIST_TESTRUNUID' not in env
    assert env['TMPDIR'] == '/tmp'
    assert os.environ['PYTEST_XDIST_WORKER'] == 'gw0'
    assert os.environ['PYTEST_XDIST_TESTRUNUID'] == 'parent-run'


def test_stored_alias_names_the_same_controller(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    store = TeamStore(real / 'team', controller=WorkflowStore(real / 'control.json'))
    data = store._read()
    data['controller_path'] = str(alias / 'control.json')
    store.check_namespace(data)
    data['controller_path'] = str(real / 'another-control.json')
    with pytest.raises(TeamStateError, match='namespace'):
        store.check_namespace(data)


def test_live_dispatch_from_non_git_copy_with_symlink_tmpdir(tmp_path):
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / 'copy'
    copy.mkdir()
    shutil.copytree(root / 'src', copy / 'src')
    (copy / 'tests').mkdir()
    for name in ['__init__.py', 'conftest.py', 'test_team_delegation.py']:
        shutil.copy2(root / 'tests' / name, copy / 'tests' / name)
    real = tmp_path / 'temp-real'
    real.mkdir()
    alias = tmp_path / 'temp-alias'
    alias.symlink_to(real, target_is_directory=True)
    result = subprocess.run([sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q',
                             'tests/test_team_delegation.py::test_a_live_other_process_keeps_its_dispatch'],
                            cwd=copy, env=_serial_child_env(TMPDIR=str(alias),
                                                  PYTHONPATH=str(copy / 'src')),
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


def test_graph_import_preserves_namespace_in_non_git_copy(tmp_path):
    if importlib.util.find_spec('companion.graph') is None:
        pytest.skip('companion.graph is not shipped in the public export')
    root = Path(__file__).resolve().parents[1]
    copy = tmp_path / 'copy'
    shutil.copytree(root / 'src', copy / 'src')
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    probe = """
import json
import os
import subprocess
import sys
from companion.paths import DATA_DIR
from companion.workflow_control import WorkflowStore
before = (str(DATA_DIR), str(WorkflowStore().path), os.environ['KYRA_DATA_DIR'])
from companion.graph.paths import live_data_dirs, Refused
assert before == (str(DATA_DIR), str(WorkflowStore().path), os.environ['KYRA_DATA_DIR'])
try:
    live_data_dirs()
except Refused:
    pass
else:
    raise AssertionError('no-git guard did not fail closed')
child = subprocess.check_output([sys.executable, '-c',
    'import json; from companion.workflow_control import WorkflowStore; '
    'print(json.dumps(str(WorkflowStore().path)))'], text=True)
assert json.loads(child) == before[1]
print('namespace unchanged; child agrees; guard refused')
"""
    result = subprocess.run(
        [sys.executable, '-c', probe], cwd=copy,
        env={**os.environ, 'KYRA_DATA_DIR': str(alias), 'TMPDIR': str(alias),
             'PYTHONPATH': str(copy / 'src')},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'namespace unchanged; child agrees; guard refused' in result.stdout
