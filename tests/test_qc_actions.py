"""QC recovery adapters: requeue through the landing runner, and observe-only registration."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import landing_runner as lr  # noqa: E402
from companion.feature_team import qc_actions as qc  # noqa: E402

# qc-requeue is a child process. It reads the machine process list (`ps`) and git's inherited
# GIT_* variables. Another suite test can leave a scripts/train.py process or a GIT_DIR behind.
_GIT_ENV = (
    'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
    'GIT_PREFIX', 'GIT_CEILING_DIRECTORIES',
)


@pytest.fixture
def isolated_requeue(tmp_path, monkeypatch):
    """Empty process list and a throwaway git environment for this test and its requeue child."""
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    ps = bindir / 'ps'
    ps.write_text('#!/bin/sh\nexit 0\n')
    ps.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
    for name in _GIT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def process_list_available():
    try:
        subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, check=False)
    except OSError:
        return False
    return True

SCRATCH_REGISTRY = '''import json, os, sys
from pathlib import Path

def parse(argv):
    flags, opt = [], {}
    i = 0
    while i < len(argv):
        item = argv[i]
        if item.startswith('--') and i + 1 < len(argv) and not argv[i + 1].startswith('--'):
            opt[item[2:]] = argv[i + 1]
            i += 2
        else:
            flags.append(item[2:] if item.startswith('--') else item)
            i += 1
    return flags, opt

def load(path):
    return json.loads(Path(path).read_text())

def store(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, sort_keys=True))
    os.replace(tmp, path)

def main(argv):
    log = os.environ.get('QC_REGISTRY_LOG')
    if log:
        with open(log, 'a') as handle:
            handle.write(json.dumps(argv) + chr(10))
    if argv == ['capabilities']:
        print(json.dumps({'observe_only': True, 'atomic_replace': True, 'starts_process': False}))
        return 0
    flags, opt = parse(argv)
    data = load(opt['registry'])
    row = data['jobs'][opt['job']]
    action = flags[0]
    if action == 'read':
        print(json.dumps(row, sort_keys=True))
        return 0
    if 'no-start' not in flags:
        print(json.dumps({'error': 'would start a process'}))
        return 2
    if action == 'replace':
        if 'observe-only' not in flags or opt.get('ledger') != row['ledger_id']:
            print(json.dumps({'error': 'refused'}))
            return 2
        snapshot = {key: row[key] for key in row if key != '_before'}
        updated = dict(snapshot)
        updated['cwd'] = opt['cwd']
        updated['observe_only'] = True
        updated['restart'] = False
        updated['version'] = int(snapshot['version']) + 1
        updated['_before'] = snapshot
        data['jobs'][opt['job']] = updated
        store(opt['registry'], data)
        print(json.dumps({key: updated[key] for key in updated if key != '_before'}, sort_keys=True))
        return 0
    if action == 'restore':
        if str(row.get('version')) != str(opt.get('expect')) or not isinstance(row.get('_before'), dict):
            print(json.dumps({'error': 'version'}))
            return 3
        data['jobs'][opt['job']] = row['_before']
        store(opt['registry'], data)
        print(json.dumps(row['_before'], sort_keys=True))
        return 0
    return 2

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
'''

WEAK_REGISTRY = '''import json, sys
print(json.dumps({'observe_only': True, 'atomic_replace': False, 'starts_process': True}))
if sys.argv[1:] != ['capabilities']:
    raise SystemExit('replace must not run')
'''


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def landing_repo(tmp_path):
    origin, main = tmp_path / 'origin.git', tmp_path / 'main'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master', str(origin)], check=True)
    subprocess.run(['git', 'clone', '-q', str(origin), str(main)], check=True, capture_output=True)
    git(main, 'config', 'user.name', 'Probe')
    git(main, 'config', 'user.email', 'probe@example.com')
    (main / 'f.txt').write_text('base\n')
    git(main, 'add', 'f.txt')
    git(main, 'commit', '-qm', 'base')
    git(main, 'push', '-q', 'origin', 'HEAD:master')
    git(main, 'checkout', '-qb', 'feat')
    (main / 'f.txt').write_text('reviewed\n')
    git(main, 'commit', '-qam', 'reviewed')
    head = git(main, 'rev-parse', 'HEAD')
    git(main, 'checkout', '-q', 'master')
    landing = tmp_path / 'landing'
    landing.mkdir()
    return main, landing, f'x system feat@{head}', head


def requeue_hit(line, failure, incident='inc1', subject='x'):
    return {'pattern': 'LANDING_TRANSIENT_STALL', 'subject': subject, 'episode': incident,
            'action_kind': 'requeue_transient', 'suggested_fix': 'touch should-not-run',
            'preconditions': {'queue_line': line, 'failure_text': failure, 'incident_id': incident}}


def test_apply_requeues_then_rolls_back_one_scratch_line(tmp_path, isolated_requeue):
    main, landing, line, head = landing_repo(tmp_path)
    other = f'y system feat@{head}'
    (landing / 'queue.txt').write_text(other + '\n')
    (landing / 'queue-retry.json').write_text(json.dumps({line: {'n': 2, 'after': 50}}))
    config = {'landing': landing, 'main': main, 'qc_dir': tmp_path / 'qc', 'python': sys.executable}
    marker = tmp_path / 'should-not-run'
    failure = 'refused: 1-minute load is 14.2\n'
    hit = requeue_hit(line, failure)
    hit['suggested_fix'] = f'touch {marker}'
    direct = lr.qc_requeue(lr.Config(main=main, landing=landing, procs=lambda: [], load=lambda: 1.0),
                           line, failure, 'direct1', expected_name='x')
    assert direct['outcome'] == 'applied' and direct['train_launched'] is False
    assert direct['after']['retry']['n'] == 2 and direct['before']['retry']['after'] == 50
    assert (landing / 'queue.txt').read_text().splitlines() == [other, line]
    assert not marker.exists()
    assert lr.qc_requeue(lr.Config(main=main, landing=landing, procs=lambda: [], load=lambda: 1.0),
                         line, failure, 'direct1', expected_name='x')['outcome'] == 'already_satisfied'
    rolled = lr.qc_rollback_requeue(lr.Config(main=main, landing=landing, procs=lambda: [], load=lambda: 1.0), 'direct1')
    assert rolled['outcome'] == 'applied' and rolled['reason'] == 'rolled_back'
    assert (landing / 'queue.txt').read_text().splitlines() == [other]
    assert json.loads((landing / 'queue-retry.json').read_text())[line] == {'n': 2, 'after': 50}
    outcome = qc.apply(hit, config)
    if process_list_available():
        assert outcome['outcome'] == 'applied' and outcome['train_launched'] is False, outcome
        assert (landing / 'queue.txt').read_text().splitlines() == [other, line]
        assert qc.rollback(outcome, config)['reason'] == 'rolled_back'
    else:
        assert outcome['reason'] == 'process_list_unavailable', outcome
        assert (landing / 'queue.txt').read_text().splitlines() == [other]
    assert not marker.exists()


def test_apply_refuses_mixed_failure_hold_and_changed_identity(tmp_path):
    main, landing, line, head = landing_repo(tmp_path)
    config = {'landing': landing, 'main': main, 'qc_dir': tmp_path / 'qc', 'python': sys.executable}
    mixed = ('FAILED tests/test_correct_repeaters.py::test_load_allows_the_browser_phase - load\n'
             'FAILED tests/test_memory.py::test_recall - assert 1 == 2\n'
             'refused: merged-tree guards failed\n')
    assert qc.apply(requeue_hit(line, mixed, 'mix'), config)['reason'] == 'not_transient'
    (landing / 'queue-hold').write_text('hold\n')
    assert qc.apply(requeue_hit(line, 'refused: runner held\n', 'hold'), config)['reason'] == 'hold'
    (landing / 'queue-hold').unlink()
    assert qc.apply(requeue_hit(line, 'refused: no headroom\n', 'name', subject='other'), config)['reason'] == 'changed_identity'
    assert not (landing / 'queue.txt').exists() or 'x system' not in (landing / 'queue.txt').read_text()


def test_apply_keeps_an_exhausted_budget(tmp_path, isolated_requeue):
    main, landing, line, head = landing_repo(tmp_path)
    (landing / 'queue.log').write_text(
        '12:00:00 ALERT: x refused 7 times for a busy Mac; gave up and dropped the line\n')
    config = {'landing': landing, 'main': main, 'python': sys.executable}
    if process_list_available():
        outcome = qc.apply(requeue_hit(line, 'refused: no headroom\n', 'gave'), config)
        assert outcome['outcome'] == 'blocked' and outcome['reason'] == 'budget_exhausted', outcome
    else:
        direct = lr.qc_requeue(lr.Config(main=main, landing=landing, procs=lambda: [], load=lambda: 1.0),
                               line, 'refused: no headroom\n', 'gave', expected_name='x')
        assert direct['outcome'] == 'blocked' and direct['reason'] == 'budget_exhausted'
    assert not (landing / 'queue.txt').exists() or (landing / 'queue.txt').read_text().strip() == ''


def test_grok_clause_waits_for_dispatch_and_other_patterns_stay_with_the_board(tmp_path):
    source = Path(qc.__file__).read_text()
    assert 'lane_send' not in source
    grok = qc.apply({'pattern': 'GROK_COMMAND_REFUSAL', 'episode': 'g1', 'subject': 'pkg'}, {})
    assert grok['outcome'] == 'waiting_board' and grok['reason'] == 'next_dispatch'
    stale = qc.apply({'pattern': 'LANDING_STALE_PIN', 'episode': 's1', 'action_kind': None}, {})
    assert stale['outcome'] == 'waiting_board' and stale['reason'] == 'board_owned'
    clash = qc.apply({'pattern': 'WRONG_FOLDER_RESTART', 'action_kind': 'requeue_transient', 'episode': 'c1'}, {})
    assert clash['reason'] == 'action_mismatch'


def registry_file(path, cwd, **extra):
    row = {'job_id': 'job1', 'cwd': cwd, 'ledger_id': 'ledger1', 'observe_only': False,
           'restart': True, 'version': 1, 'worker_pid': None}
    row.update(extra)
    path.write_text(json.dumps({'jobs': {'job1': row}}, sort_keys=True))


def scratch_command(tmp_path, body):
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / 'registry_cli.py'
    script.write_text(body)
    return [sys.executable, str(script)]


def registry_hit(cwd):
    return {'pattern': 'WRONG_FOLDER_RESTART', 'subject': 'job1', 'episode': 'reg1',
            'action_kind': 'reregister_observe_only', 'suggested_fix': 'restart the worker',
            'preconditions': {'job_id': 'job1', 'ledger_id': 'ledger1', 'claimed_worktree': cwd,
                              'incident_id': 'reg1'}}


def test_missing_or_unproven_registry_does_not_invent_a_repair(tmp_path):
    claimed = tmp_path / 'right'
    claimed.mkdir()
    registry = tmp_path / 'registry.json'
    registry_file(registry, str(tmp_path / 'wrong'))
    original = registry.read_text()
    config = {'qc_dir': tmp_path / 'qc', 'registry_path': registry}
    missing = qc.apply(registry_hit(str(claimed)), config)
    assert missing['outcome'] == 'blocked' and missing['reason'] == 'registry_interface_unavailable'
    assert missing['acceptance'] == 'unmet'
    assert registry.read_text() == original
    weak = tmp_path / 'weak'
    config['registry_command'] = scratch_command(weak, WEAK_REGISTRY)
    blocked = qc.apply(registry_hit(str(claimed)), config)
    assert blocked['reason'] == 'registry_cannot_guarantee_observe_only'
    assert registry.read_text() == original


def test_proven_scratch_registry_reregisters_and_rolls_back(tmp_path, monkeypatch):
    claimed = tmp_path / 'right'
    wrong = tmp_path / 'wrong'
    claimed.mkdir()
    wrong.mkdir()
    registry = tmp_path / 'registry.json'
    registry_file(registry, str(wrong))
    log = tmp_path / 'registry-log.txt'
    monkeypatch.setenv('QC_REGISTRY_LOG', str(log))
    config = {'qc_dir': tmp_path / 'qc', 'registry_path': registry,
              'registry_command': scratch_command(tmp_path, SCRATCH_REGISTRY)}
    before = json.loads(registry.read_text())['jobs']['job1']
    outcome = qc.apply(registry_hit(str(claimed)), config)
    assert outcome['outcome'] == 'applied' and outcome['acceptance'] == 'met'
    assert outcome['before']['cwd'] == str(wrong) and outcome['after']['cwd'] == str(claimed)
    after = json.loads(registry.read_text())['jobs']['job1']
    assert after['observe_only'] is True and after['restart'] is False and after['version'] == 2
    assert qc.apply(registry_hit(str(claimed)), config)['outcome'] == 'already_satisfied'
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [item[0] for item in calls].count('replace') == 1
    rolled = qc.rollback(outcome, config)
    assert rolled['outcome'] == 'applied'
    restored = json.loads(registry.read_text())['jobs']['job1']
    assert restored['cwd'] == before['cwd'] and restored['restart'] is True and restored['observe_only'] is False
    assert restored['version'] == 1


def test_registry_refuses_a_live_worker_or_a_folder_that_already_matches(tmp_path):
    claimed = tmp_path / 'right'
    claimed.mkdir()
    registry = tmp_path / 'registry.json'
    registry_file(registry, str(tmp_path / 'wrong'), worker_pid=4321)
    config = {'qc_dir': tmp_path / 'qc', 'registry_path': registry,
              'registry_command': scratch_command(tmp_path, SCRATCH_REGISTRY)}
    live = qc.apply(registry_hit(str(claimed)), config)
    assert live['reason'] == 'live_worker'
    assert json.loads(registry.read_text())['jobs']['job1']['restart'] is True
    registry_file(registry, str(claimed))
    same = qc.apply(registry_hit(str(claimed)), config)
    assert same['reason'] == 'not_a_proven_mismatch'
    assert json.loads(registry.read_text())['jobs']['job1']['cwd'] == str(claimed)
    assert json.loads(registry.read_text())['jobs']['job1']['restart'] is True
