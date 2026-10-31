"""Nightly uses only remote full runs and prepares, never lands, a revert."""
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import nightly_suite as nightly  # noqa: E402


def test_bisect_order_and_unknown_stops():
    calls = []
    def check(sha):
        calls.append(sha)
        return sha < 'c'
    assert nightly.first_red(['a', 'b', 'c', 'd', 'e'], check) == 'c'
    assert calls == ['c', 'b']
    with pytest.raises(nightly.Blocked):
        nightly.first_red(['a'], lambda _: None)


def test_remote_full_suite_and_infrastructure(monkeypatch, tmp_path):
    monkeypatch.setenv('KYRA_M1_HOST', 'runner.example.test')
    monkeypatch.setenv('KYRA_M1_KEY', '/fixture/key')
    monkeypatch.setenv('KYRA_BROWSER_TESTS', 'tests/test_partial_browser.py')
    calls = []
    def runner(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=1, stdout='FAILED tests/test_example.py::test_rule\npytest exited 1', stderr='')
    result = nightly.remote(tmp_path, tmp_path, 'a' * 40, runner=runner)
    assert result == (False, ['tests/test_example.py::test_rule'])
    argv, kw = calls[0]
    assert 'low_power.py' in ' '.join(argv)
    assert argv[-2:] == ['--workers', '2']
    assert 'KYRA_BROWSER_TESTS' not in kw['env']
    assert kw['env']['KYRA_TEST_HOST'] == 'runner.example.test'
    for code in (2, 73, 75, 76, 124):
        assert nightly.remote(tmp_path, tmp_path, 'a' * 40, runner=lambda *a, code=code, **k:
                              SimpleNamespace(returncode=code, stdout='', stderr=''))[0] is None


def test_missing_runner_no_process(monkeypatch, tmp_path):
    monkeypatch.delenv('KYRA_M1_HOST', raising=False)
    monkeypatch.delenv('KYRA_M1_KEY', raising=False)
    assert nightly.remote(tmp_path, tmp_path, 'a' * 40, runner=lambda *a, **k:
                          pytest.fail('must not start')) == (None, [])


def test_green_inbox_and_team(tmp_path):
    sent = []
    row = nightly.publish(tmp_path, 'room', {'status': 'green', 'sha': 'a' * 40},
                          team_post=lambda *a: sent.append(a))
    assert json.loads((tmp_path / 'team/board-inbox.jsonl').read_text()) == row
    assert row['kind'] == 'nightly_suite' and row['created_at']
    assert len(sent) == 1 and sent[0][0] == 'room'


def test_plist_only_writes_uninstalled_file(tmp_path):
    path = nightly.write_plist(tmp_path, tmp_path / 'data', 'room')
    value = plistlib.loads(path.read_bytes())
    assert path == tmp_path / 'data/team/com.kyra.nightly-suite.plist'
    assert value['StartCalendarInterval'] == {'Hour': 2, 'Minute': 30}
    assert value['ProgramArguments'][-2:] == ['--room', 'room']
    assert not value.get('RunAtLoad')


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init', '-b', 'master')
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'user.email', 'fixture@example.test')
    (path / 'file').write_text('base')
    git(path, 'add', 'file')
    git(path, 'commit', '-m', 'base')
    base = git(path, 'rev-parse', 'HEAD')
    merges = []
    for i in range(3):
        git(path, 'checkout', '-b', f'feature{i}')
        (path / f'file{i}').write_text(str(i))
        git(path, 'add', f'file{i}')
        git(path, 'commit', '-m', 'change\n\nAuthor-Lane: codex lane 1')
        git(path, 'checkout', 'master')
        git(path, 'merge', '--no-ff', f'feature{i}', '-m', f'merge {i}\n\nAuthor-Lane: codex lane 1')
        merges.append(git(path, 'rev-parse', 'HEAD'))
    return path, base, merges


def test_red_real_git_revert_inbox_no_push(repo, tmp_path):
    path, base, merges = repo
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        assert not any(a in ('push', '--force', '-f') for a in argv)
        return subprocess.run(argv, **kw)
    seen = []
    def remote(*args):
        sha = args[2]
        seen.append(sha)
        return (sha in (base, merges[0]), [] if sha in (base, merges[0]) else ['tests/test_example.py::test_rule'])
    row = nightly.run(path, tmp_path / 'data', 'room', ref=merges[-1], runner=runner,
                      test=remote, team_post=lambda *a: None)
    assert row['status'] == 'red' and row['merge_sha'] == merges[1]
    assert row['author_lane'] == 'codex lane 1'
    assert row['branch'].endswith('-revert-' + merges[1][:12])
    assert row['branch'].startswith('session/')
    assert row['failing_tests'] == ['tests/test_example.py::test_rule']
    assert row['lesson']['package'] == 'nightly-' + merges[1][:12]
    assert row['candidate_check']['past_case_sha'] == merges[1]
    assert row['lesson_status'] == 'unavailable in public export'  # export stand-in writes no lessons file
    assert git(path, 'rev-parse', 'HEAD') == merges[-1]
    assert git(path, 'show', row['branch'] + ':file0') == '0'
    assert git(path, 'show', row['branch'] + ':file2') == '2'
    assert git(path, 'diff-tree', '--no-commit-id', '--name-only', '-r', row['branch']) == 'file1'
    assert any('revert' in cmd and '-m' in cmd for cmd in calls)


def test_infrastructure_does_not_revert(repo, tmp_path):
    path, _, merges = repo
    row = nightly.run(path, tmp_path, 'room', ref=merges[-1], test=lambda *a: (None, []),
                      team_post=lambda *a: None)
    assert row['status'] == 'blocked' and 'branch' not in row
    assert 'revert' not in git(path, 'branch', '--list')


def test_green_run_does_not_create_branches(repo, tmp_path):
    path, _, merges = repo
    before = git(path, 'branch', '--list')
    row = nightly.run(path, tmp_path, 'room', ref=merges[-1], test=lambda *a: (True, []),
                      team_post=lambda *a: None)
    assert row['status'] == 'green'
    assert git(path, 'branch', '--list') == before


def test_red_baseline_is_not_blame(repo, tmp_path):
    path, _, merges = repo
    row = nightly.run(path, tmp_path, 'room', ref=merges[-1], test=lambda *a: (False, ['tests/test_a.py::test_a']),
                      team_post=lambda *a: None)
    assert row['status'] == 'blocked' and 'branch' not in row


def test_team_messages_fit_real_step_contract(tmp_path, monkeypatch):
    from companion import team_chat
    messages = []
    class Store:
        def __init__(self, **kw):
            pass

        def post_step(self, room, step, text, **kw):
            team_chat.validate_step(dict(id='a' * 32, author='kyra', kind='step', text=text,
                                         step=step, created_at=1.0))
            messages.append(text)
    monkeypatch.setattr(team_chat, 'TeamStore', Store)
    text = 'Nightly red: ' + 'tests/test_example.py::test_rule ' * 50
    nightly.post_team(tmp_path, 'room', text, 'event')
    assert ''.join(messages) == text.strip()


def test_team_failure_keeps_board_record(tmp_path):
    def fail(*args):
        raise LookupError('room unavailable')
    row = nightly.publish(tmp_path, 'room', {'status': 'green', 'sha': 'a' * 40}, team_post=fail)
    assert row['team_delivery'].startswith('failed')
    assert (tmp_path / 'team/board-inbox.jsonl').exists()
