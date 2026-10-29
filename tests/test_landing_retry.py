"""Bounded landing retries, including a real serial pytest rerun."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from landing_retry import retry_runner


@pytest.mark.parametrize('first', ['FAILED', 'ERROR'])
def test_flake_passes_records_and_deduplicates_inbox(tmp_path, first):
    test = tmp_path / 'tests/test_example.py'
    test.parent.mkdir()
    test.write_text('def test_ok():\n    assert True\n')
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if '-v' not in argv:
            return subprocess.CompletedProcess(argv, 1 if first == 'FAILED' else 2,
                f'{first} tests/test_example.py::test_ok - transient\n1 {"failed" if first == "FAILED" else "error"}\n', '')
        return subprocess.run(argv, capture_output=True, text=True, **kwargs)

    wrapped = retry_runner(runner, tmp_path, 'train-example')
    for _ in range(2):
        assert wrapped([sys.executable, '-m', 'pytest'], cwd=tmp_path).returncode == 0
    rows = [json.loads(line) for line in (tmp_path / 'data/team/landing/flaky.jsonl').read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]['test_id'] == 'tests/test_example.py::test_ok'
    assert rows[0]['first_outcome'] == first and rows[0]['second_outcome'] == 'PASSED'
    assert rows[0]['train'] == 'train-example' and rows[0]['date']
    assert len((tmp_path / 'data/team/board-inbox.jsonl').read_text().splitlines()) == 1
    assert calls[1][1]['cwd'] == tmp_path
    assert calls[1][0][-1] == 'tests/test_example.py::test_ok'
    assert '-n' not in calls[1][0] and 'no:xdist' in calls[1][0]


@pytest.mark.parametrize('node', ['tests/test_example.py::test_ok',
    'tests/test_no_third_party_pii.py::test_ok', 'tests/test_no_personal_files.py::test_ok',
    'tests/test_worker_sandbox.py::test_ok', 'tests/test_workflow_land.py::test_ok'])
def test_repeat_refuses_and_sensitive_never_retries(tmp_path, node):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, f'FAILED {node} - broken\n1 failed\n', '')
    assert retry_runner(runner, tmp_path, 'train')([sys.executable, '-m', 'pytest'], cwd=tmp_path).returncode == 1
    assert len(calls) == (2 if 'test_example' in node else 1)
    assert not (tmp_path / 'data/team/landing/flaky.jsonl').exists()


def test_remote_collection_error_retries_exact_file_serially(tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 2 if len(calls) == 1 else 0,
            'ERROR tests/test_example.py - import failed\n1 error\n' if len(calls) == 1 else
            'PASSED tests/test_example.py::test_ok\n1 passed\n', '')
    argv = [sys.executable, '/tree/scripts/remote_test.py', '--ref', 'a' * 40,
            '--workers', '2', '--', '--ignore-glob=tests/test_*_browser.py']
    assert retry_runner(runner, tmp_path, 'train')(argv, cwd=tmp_path).returncode == 0
    assert calls[1][:6] == [*argv[:5], '1']
    assert calls[1][-1] == 'tests/test_example.py'
    assert '--ignore-glob=tests/test_*_browser.py' not in calls[1]


def test_skipped_retry_does_not_clear_failure(tmp_path):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0,
            'FAILED tests/test_example.py::test_ok - transient\n1 failed\n' if len(calls) == 1 else
            'SKIPPED tests/test_example.py::test_ok\n1 skipped\n', '')
    assert retry_runner(runner, tmp_path, 'train')(
        [sys.executable, '-m', 'pytest'], cwd=tmp_path).returncode == 1
    assert len(calls) == 2
    assert not (tmp_path / 'data/team/landing/flaky.jsonl').exists()


@pytest.mark.parametrize('explicit_env', [False, True])
def test_serial_retry_scrubs_parent_pytest_environment(tmp_path, monkeypatch, explicit_env):
    import os

    from deploy_local import run

    polluted = {'PYTEST_ADDOPTS': '--invalid-parent-option',
                'PYTEST_CURRENT_TEST': 'parent test', 'PYTEST_XDIST_WORKER': 'gw0',
                'PYTEST_XDIST_WORKER_COUNT': '2', 'PYTEST_XDIST_TESTRUNUID': 'parent',
                'KYRA_RETRY_CONTROL': 'retained'}
    for key, value in polluted.items():
        monkeypatch.setenv(key, value)
    test = tmp_path / 'tests/test_example.py'
    test.parent.mkdir()
    test.write_text('import os\ndef test_ok():\n'
                    '    assert "PYTEST_ADDOPTS" not in os.environ and "PYTEST_XDIST_WORKER" not in os.environ\n'
                    '    assert os.environ["KYRA_RETRY_CONTROL"] == "retained"\n')
    calls = []
    def runner(argv, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1,
                'FAILED tests/test_example.py::test_ok - transient\n1 failed\n', '')
        return run(argv, **kwargs)
    kwargs = {'cwd': tmp_path}
    if explicit_env:
        kwargs['env'] = dict(os.environ)
    assert retry_runner(runner, tmp_path, 'train')(
        [sys.executable, '-m', 'pytest'], **kwargs).returncode == 0
    assert not any(key.startswith('PYTEST_') for key in calls[1]['env'])
    assert os.environ['PYTEST_XDIST_WORKER'] == 'gw0'
    if explicit_env:
        assert kwargs['env']['PYTEST_XDIST_WORKER'] == 'gw0'


@pytest.mark.parametrize('filename', ['test_impact.py', 'test_train.py',
    'test_landing_runner.py', 'test_remote_test.py'])
def test_sensitive_selector_files_never_retry(tmp_path, filename):
    node = f'tests/{filename}::test_ok'
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0,
            f'FAILED {node} - transient\n1 failed\n' if len(calls) == 1 else f'PASSED {node}\n', '')
    assert retry_runner(runner, tmp_path, 'train')([sys.executable, '-m', 'pytest']).returncode == 1
    assert len(calls) == 1


def test_retry_module_and_tests_are_sensitive(tmp_path):
    from landing_retry import sensitive

    assert sensitive('scripts/landing_retry.py')
    node = 'tests/test_landing_retry.py::test_ok'
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0,
            f'FAILED {node} - transient\n1 failed\n' if len(calls) == 1 else f'PASSED {node}\n', '')
    assert retry_runner(runner, tmp_path, 'train')([sys.executable, '-m', 'pytest']).returncode == 1
    assert len(calls) == 1
