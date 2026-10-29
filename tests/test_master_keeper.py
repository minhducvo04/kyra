"""Master incidents use fake suites; state, queue controls and notifications are real."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import landing_runner as lr
import master_keeper as mk

BAD = 'tests/test_example.py::test_example'
A, B = 'a' * 40, 'b' * 40


def setup(tmp_path):
    cfg = lr.Config(main=tmp_path, procs=lambda: [], load=lambda: 1)
    return cfg, [], []


def test_red_confirm_hold_worker_once_and_green(tmp_path):
    cfg, notices, workers = setup(tmp_path)
    calls = []

    def suite(sha, tests):
        calls.append((sha, tests))
        return mk.Outcome('red', [BAD], f'FAILED {BAD} - AssertionError: expected green')

    kwargs = dict(check=suite, notify=lambda *x: notices.append(x),
                  dispatch=lambda *x: workers.append(x) or 'STARTED', commits=lambda *x: 'example fix')
    mk.tick(cfg, A, now=100, **kwargs)
    mk.tick(cfg, A, now=200, **kwargs)
    health = json.loads((cfg.landing / 'master-health.json').read_text())
    assert health['commit'] == A and health['failing_tests'] == [BAD] and health['first_seen'] == 100
    assert calls == [(A, []), (A, [BAD])]
    assert len(workers) == 1 and len(notices) == 1
    assert 'AssertionError' in Path(workers[0][1]).read_text()
    assert (cfg.landing / 'queue-hold').read_text().startswith('master-keeper:')
    kwargs['check'] = lambda sha, tests: mk.Outcome('green')
    mk.tick(cfg, B, now=400, **kwargs)
    assert not (cfg.landing / 'master-health.json').exists()
    assert not (cfg.landing / 'queue-hold').exists()
    assert '300s' in notices[-1][1] and len(notices) == 2


@pytest.mark.parametrize('refused', [True, False])
def test_repair_dispatch_reports_refusal_and_allows_oversize_brief(tmp_path, monkeypatch, refused):
    import subprocess
    cfg, notices, _ = setup(tmp_path)
    sent = []
    receipt = ('refusing an oversize brief (27 lines, limit 80; 8 paths, limit 6)'
               if refused else 'STARTED: codex-exec pid=123')
    monkeypatch.setattr(mk.nightly, 'git', lambda *args: '')

    def run(argv, **kwargs):
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 1 if refused else 0,
                                           '' if refused else receipt, receipt if refused else '')

    monkeypatch.setattr(mk.subprocess, 'run', run)
    for now in (100, 200):
        mk.tick(cfg, A, now=now, check=lambda *args: mk.Outcome('red', [BAD]),
                notify=lambda *args: notices.append(args), commits=lambda *args: '')
    incident = mk.read(cfg.landing / 'master-health.json')
    assert incident['worker']['status'] == ('failed' if refused else 'started')
    assert incident['worker']['receipt'] == receipt
    alerts = [text for key, text in notices if key.startswith('dispatch-failed:')]
    assert len(alerts) == int(refused)
    if refused:
        assert receipt in alerts[0]
    assert len(sent) == 1
    assert sent[0][sent[0].index('--oversize-reason') + 1] == 'master repair brief'
    assert (cfg.landing / 'queue-hold').exists()


def test_train_failure_on_unchanged_green_master_is_confirmed(tmp_path):
    cfg, notices, workers = setup(tmp_path)
    calls = []
    def check(sha, tests):
        calls.append((sha, tests))
        return mk.Outcome('red', [BAD], f'FAILED {BAD}') if tests else mk.Outcome('green')
    kwargs = dict(check=check, notify=lambda *x: notices.append(x),
                  dispatch=lambda *x: workers.append(x), commits=lambda *x: '')
    mk.tick(cfg, A, now=100, **kwargs)
    logs = tmp_path / 'data/train-logs'
    logs.mkdir()
    (logs / 'example.log').write_text(f'FAILED {BAD} - AssertionError\n')
    mk.tick(cfg, A, now=200, **kwargs)
    mk.tick(cfg, A, now=300, **kwargs)
    assert calls == [(A, []), (A, [BAD])]
    assert len(workers) == len(notices) == 1


def test_branch_only_failure_and_unavailable_runner_do_not_blame_master(tmp_path):
    cfg, notices, workers = setup(tmp_path)
    outcomes = iter([mk.Outcome('blocked', reason='M1 unavailable'),
                     mk.Outcome('red', [BAD]), mk.Outcome('green')])
    kwargs = dict(check=lambda *x: next(outcomes), notify=lambda *x: notices.append(x),
                  dispatch=lambda *x: workers.append(x), commits=lambda *x: '')
    mk.tick(cfg, A, now=100, **kwargs)
    mk.tick(cfg, A, now=200, **kwargs)
    assert not (cfg.landing / 'master-health.json').exists()
    assert not (cfg.landing / 'queue-hold').exists()
    assert workers == notices == []


def test_master_hold_admits_only_fix_and_preserves_owner_hold(tmp_path):
    cfg, _, _ = setup(tmp_path)
    ran = []
    cfg.process = lambda cfg, entry: ran.append(entry.name)
    cfg.queue.write_text('other system branch\nmaster-fix-example system fix\n')
    mk.set_hold(cfg, 'incident')
    lr.hold(cfg)  # must not wait 90 minutes or expire the incident hold
    assert lr.cycle(cfg) == 'ran:master-fix-example'
    assert lr.cycle(cfg) == 'held'
    assert ran == ['master-fix-example']
    (cfg.landing / 'queue-hold').write_text('owner push')
    mk.clear_hold(cfg, 'incident')
    assert (cfg.landing / 'queue-hold').read_text() == 'owner push'


def test_stall_once_until_progress_then_new_stall(tmp_path):
    cfg, notices, _ = setup(tmp_path)
    cfg.queue.write_text('other system branch\n')
    state = {}
    def notify(*x):
        notices.append(x)
    for now in (100, 2799, 2800, 3000):
        mk.check_stall(cfg, state, now, 'M1 unreachable; load 7.1 > 6', notify)
    assert len(notices) == 1 and 'M1 unreachable; load 7.1 > 6' in notices[0][1]
    (cfg.landing / 'queue.log').write_text('00:00:00 other started\n')
    mk.check_stall(cfg, state, 3100, 'busy', notify)
    mk.check_stall(cfg, state, 5800, 'busy', notify)
    assert len(notices) == 2


def test_refused_line_retained_reclassified_and_retried_after_green(tmp_path, monkeypatch):
    cfg, notices, workers = setup(tmp_path)
    line = 'example system branch'
    cfg.queue.write_text(line + '\n')
    log = cfg.landing / 'example-run.log'
    log.write_text(f'FAILED {BAD} - AssertionError\n')
    mk.save(cfg.landing / 'example-master-pending.json', {'line': line})
    kwargs = dict(check=lambda *x: mk.Outcome('red', [BAD]), notify=lambda *x: notices.append(x),
                  dispatch=lambda *x: workers.append(x), commits=lambda *x: '')
    state = mk.tick(cfg, A, now=100, **kwargs)
    assert state['classifications'][str(log)] == 'master red; retained for retry'
    assert cfg.queue.read_text().strip() == line
    monkeypatch.setattr(lr, 'master_repair_allowed', lambda cfg: True)
    assert lr.cycle(cfg) == 'held'
    kwargs['check'] = lambda *x: mk.Outcome('green')
    mk.tick(cfg, B, now=200, **kwargs)
    assert cfg.queue.read_text().strip() == line
    ran = []
    cfg.process = lambda cfg, entry: ran.append(entry.name)
    assert lr.cycle(cfg) == 'ran:example'
    assert ran == ['example']


def test_branch_failure_released_after_plain_master_pass(tmp_path):
    cfg, _, _ = setup(tmp_path)
    cfg.queue.write_text('example system branch\n')
    log = cfg.landing / 'example-run.log'
    log.write_text(f'FAILED {BAD}\n')
    mk.save(cfg.landing / 'example-master-pending.json', {'line': 'example system branch'})
    state = mk.tick(cfg, A, now=100, check=lambda *x: mk.Outcome('green'), notify=lambda *x: None)
    assert state['classifications'][str(log)] == 'branch failure'
    assert not cfg.queue.read_text().strip()
    assert not (cfg.landing / 'queue-hold').exists()


@pytest.mark.parametrize('classified', [False, True])
def test_branch_failure_quarantined_while_healthy_line_continues(tmp_path, monkeypatch, classified):
    import hashlib
    import subprocess
    cfg, _, _ = setup(tmp_path)
    monkeypatch.setattr(lr, 'git', lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, A, ''))
    line = 'example system branch'
    cfg.queue.write_text(line + '\nhealthy system other\n')
    log = cfg.landing / 'example-run.log'
    log.write_text(f'FAILED {BAD}\n')
    marker = cfg.landing / 'example-master-pending.json'
    mk.save(marker, {'line': line, 'base': A})
    if classified:
        mk.save(cfg.landing / 'master-keeper.json', {
            'classifications': {str(log): 'branch failure'},
            'logs': {str(log): hashlib.sha256(log.read_bytes()).hexdigest()},
            'classification_inputs': {str(log): {'line': line, 'base': A}}})
    else:
        mk.tick(cfg, A, now=100, check=lambda *x: mk.Outcome('green'), notify=lambda *x: None)
    ran = []
    cfg.process = lambda cfg, entry: ran.append(entry.name) or 'landed'
    assert lr.cycle(cfg) == 'ran:healthy'
    quarantine = (cfg.landing / 'quarantine.txt').read_text()
    assert line in quarantine and 'branch failure' in quarantine and BAD in quarantine
    fixback = (cfg.landing / 'example-fixback.md').read_text()
    assert BAD in fixback and str(log) in fixback
    assert not marker.exists() and cfg.queue.read_text() == ''
    assert ran == ['healthy']
    # Re-adding the identical inputs cannot create another train or delay its neighbor.
    cfg.queue.write_text(line + '\nnext system other\n')
    assert lr.cycle(cfg) == 'ran:next'
    assert ran == ['healthy', 'next']
    assert (cfg.landing / 'quarantine.txt').read_text() == quarantine
    cfg.queue.write_text('example system fixed-branch\n')
    assert lr.cycle(cfg) == 'ran:example'
    assert ran[-1] == 'example'
    # Identical failure text from changed inputs needs a fresh master diagnosis.
    changed = 'example system fixed-branch'
    cfg.queue.write_text(changed + '\n')
    mk.save(marker, {'line': changed, 'base': B, 'log': str(log)})
    assert lr.cycle(cfg) == 'delayed'
    checks = []
    mk.tick(cfg, B, now=200, check=lambda *args: checks.append(args) or mk.Outcome('green'),
            notify=lambda *args: None)
    assert checks == [(B, [])]  # New master gets the full suite before any train confirmation.
    assert cfg.queue.read_text() == ''
    assert changed in (cfg.landing / 'quarantine.txt').read_text()


def git_repo(tmp_path, path='example.py', lines=1):
    import subprocess
    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.name', 'Test Builder')
    git('config', 'user.email', 'builder@example.invalid')
    (tmp_path / 'base.txt').write_text('base\n')
    git('add', 'base.txt')
    git('commit', '-qm', 'baseline')
    parent = git('rev-parse', 'HEAD')
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed\n' * lines)
    git('add', path)
    git('commit', '-qm', 'small change')
    return git, parent, git('rev-parse', 'HEAD')


def test_revert_requires_small_recent_nonprotected_commit_and_green_parent(tmp_path, monkeypatch):
    import time
    git, parent, head = git_repo(tmp_path)
    guarded = []
    monkeypatch.setattr(mk, 'private_check', lambda cfg, work: guarded.append(work))
    cfg, _, _ = setup(tmp_path)
    incident = {'id': head[:12], 'commit': head, 'failing_tests': [BAD]}
    calls = []
    def check(sha, tests):
        calls.append((sha, tests))
        return mk.Outcome('green')
    mk.advance_tracks(cfg, incident, parent, time.time(), check)
    assert calls == [(parent, [BAD])]
    assert len(guarded) == 1
    candidate = incident['revert']
    assert candidate['status'] == 'queued'
    assert candidate['name'].startswith('master-fix-revert-')
    assert git('show', '-s', '--format=%P', candidate['sha']) == head
    assert git('rev-parse', candidate['sha'] + '^{tree}') == git('rev-parse', parent + '^{tree}')
    assert candidate['line'] in cfg.queue.read_text()


def test_revert_never_chosen_for_protected_large_or_unattributed_commit(tmp_path):
    import time
    for label, path, lines in [('protected', 'AGENTS.md', 1), ('large', 'example.py', 201),
                               ('multiple', 'example.py', 1)]:
        repo = tmp_path / label
        repo.mkdir()
        git, parent, head = git_repo(repo, path, lines)
        cfg, _, _ = setup(repo)
        incident = {'id': head[:12], 'commit': head, 'failing_tests': [BAD]}
        mk.advance_tracks(cfg, incident, A if label == 'multiple' else parent, time.time(),
                          lambda *x: (_ for _ in ()).throw(AssertionError('must not run tests')))
        assert not cfg.queue.exists()
        assert incident['revert']['status'] != 'queued'
        if label == 'protected':
            assert 'git revert --no-edit ' + head in incident['revert']['status']


def test_red_parent_does_not_queue_revert(tmp_path):
    import time
    _, parent, head = git_repo(tmp_path)
    cfg, _, _ = setup(tmp_path)
    incident = {'id': head[:12], 'commit': head, 'failing_tests': [BAD]}
    mk.advance_tracks(cfg, incident, parent, time.time(), lambda *x: mk.Outcome('red', [BAD]))
    assert 'parent also red' in incident['revert']['status']
    assert not cfg.queue.exists()


def test_fix_revert_race_cancels_other_line_and_worker(tmp_path, monkeypatch):
    import subprocess

    import pytest
    for winner in ('fix', 'revert'):
        repo = tmp_path / winner
        repo.mkdir()
        cfg, _, _ = setup(repo)
        incident = {'id': 'incident', 'commit': A, 'worker':
                    {'worktree': '/tmp/owned-worker', 'receipt': 'STARTED: codex-exec pid=123'}}
        names = ['master-fix-incident', 'master-fix-revert-incident']
        cfg.queue.write_text('\n'.join(n + ' system branch' for n in [*names, 'other']) + '\n')
        mk.save(cfg.landing / 'master-health.json', incident)
        stopped = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mk.nightly, 'git', lambda *x: B)
            patch.setattr(mk.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0,
                          '123 codex exec -C /tmp/owned-worker\n456 codex exec -C /tmp/unrelated\n', ''))
            patch.setattr(mk.os, 'getpgid', lambda pid: pid)
            patch.setattr(mk.os, 'killpg', lambda pid, sig, stopped=stopped: stopped.append(pid))
            mk.track_landed(cfg, names[0 if winner == 'fix' else 1])
        assert stopped == [123]
        assert cfg.queue.read_text().strip() == 'other system branch'
        assert mk.read(cfg.landing / 'master-health.json')['winner'] in names
        # A late completed worker cannot put its cancelled line back into execution.
        lr.append_line_once(cfg, names[0] + ' system branch')
        (cfg.landing / 'master-health.json').unlink()
        cfg.process = lambda *x: None
        lr.cycle(cfg)
        assert names[0] not in cfg.queue.read_text()


def test_escalate_thirty_minutes_once_with_track_state(tmp_path):
    cfg, notices, _ = setup(tmp_path)
    kwargs = dict(check=lambda *x: mk.Outcome('red', [BAD]), notify=lambda *x: notices.append(x),
                  dispatch=lambda *x: {'status': 'started'}, commits=lambda *x: '',
                  tracks=lambda cfg, incident, *x: incident.update(revert={'status': 'parent probe running'}))
    for now in (100, 1899, 1900, 2000):
        mk.tick(cfg, A, now=now, **kwargs)
    assert len(notices) == 2
    assert 'parent probe running' in notices[-1][1] and 'started' in notices[-1][1]


def test_runner_missing_and_stale_alert_once_then_recover(tmp_path):
    import os
    cfg, notices, _ = setup(tmp_path)
    cfg.queue.write_text('example system branch\n')
    state = {}
    for now in (100, 200):
        mk.check_runner(cfg, state, now, lambda *x: notices.append(x))
    assert len(notices) == 1 and 'absent' in notices[0][1]
    cfg.procs = lambda: ['/venv/python scripts/landing_runner.py']
    log = cfg.landing / 'queue.log'
    log.write_text('heartbeat\n')
    os.utime(log, (200, 200))
    mk.check_runner(cfg, state, 300, lambda *x: notices.append(x))
    for now in (1400, 1500):
        mk.check_runner(cfg, state, now, lambda *x: notices.append(x))
    assert len(notices) == 2 and 'stale' in notices[-1][1]


def test_permission_diagnosis_requires_python_route_error_and_shell_success(monkeypatch):
    import subprocess

    import remote_test
    monkeypatch.setattr(remote_test, 'runner_settings', lambda: ('synthetic-host', '/tmp/synthetic-key'))
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'KYRA_LOCK=free\n', '') if argv[0] == '/bin/sh' else \
            subprocess.CompletedProcess(argv, 255, '', 'No route to host')
    assert mk.reachability_probe(run) == mk.PERMISSION_CAUSE
    assert commands[1][4:] == commands[0]
    assert mk.reachability_probe(lambda argv, **kw: subprocess.CompletedProcess(argv, 255, '', 'No route to host')) != mk.PERMISSION_CAUSE


def test_background_suite_once_and_blocked_retry(tmp_path, monkeypatch):
    import subprocess
    cfg, _, _ = setup(tmp_path)
    launched = []
    def popen(argv, **kw):
        launched.append(argv)
        return type('Child', (), {'pid': 123})()
    monkeypatch.setattr(mk.subprocess, 'Popen', popen)
    assert mk.background_check(cfg, A, []).status == 'pending'
    job_path = Path(launched[0][launched[0].index('--suite') + 1])
    monkeypatch.setattr(mk.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess(a, 0, str(job_path), ''))
    assert mk.background_check(cfg, A, [], 'new-log').status == 'pending'
    assert len(launched) == 1  # log movement cannot start a second full suite
    mk.save(job_path, {'result': {'status': 'green'}})
    assert mk.background_check(cfg, A, []).status == 'green'
    assert len(launched) == 1
    mk.save(job_path, {'result': {'status': 'blocked', 'reason': 'load'}})
    assert mk.background_check(cfg, A, []).status == 'blocked'
    assert mk.background_check(cfg, A, []).status == 'pending'
    assert len(launched) == 2


def test_suite_reuses_gate_and_selected_guards_stay_local(tmp_path, monkeypatch):
    import subprocess

    import deploy_local
    import remote_test
    import train
    cfg, _, _ = setup(tmp_path)
    calls = []
    def gate(run, repo, sha):
        calls.append(('gate', sha))
        for argv in ([cfg.python, '-m', 'ruff', 'check', 'src'],
                     [cfg.python, str(repo / 'scripts/remote_test.py'), '--ref', sha, '--', '--ignore-glob=tests/test_*_browser.py'],
                     [cfg.python, '-m', 'pytest', '-n', 'auto', 'tests/test_example.py'],
                     [cfg.python, '-m', 'pytest', '-p', 'no:xdist', 'tests/test_page_browser.py']):
            run(argv, cwd=repo)
    def guards(run, repo, sha):
        calls.append(('guards', sha))
        run([cfg.python, '-m', 'pytest', *deploy_local.GUARDS], cwd=repo)
    executed = []
    def run(argv, **kw):
        executed.append(argv)
        return subprocess.CompletedProcess(argv, 0, '1 passed', '')
    monkeypatch.setattr(train, 'where_local', lambda **kw: {'KYRA_BROWSER_LOCAL': '1', 'KYRA_LOCAL_WORKERS': '1'})
    monkeypatch.setattr(remote_test, 'runner_state', lambda *a: ('free', ''))
    monkeypatch.setattr(remote_test, 'm1_browser_runner', lambda run, sha: run)
    monkeypatch.setattr(deploy_local, '_run_suite', gate)
    monkeypatch.setattr(deploy_local, '_run_private_guards', guards)
    monkeypatch.setattr(deploy_local, 'run', run)
    assert mk.suite(cfg, A, []).status == 'green'
    assert calls == [('gate', A), ('guards', A)]
    local = next(argv for argv in executed if '-n' in argv)
    assert local[local.index('-n') + 1] == '1'
    calls.clear()
    executed.clear()
    node = 'tests/test_no_personal_files.py::test_fixture'
    assert mk.suite(cfg, A, [node]).status == 'green'
    assert len(executed) == 1 and node in executed[0] and '-m' in executed[0]
    assert 'remote_test.py' not in ' '.join(executed[0])
    monkeypatch.setattr(deploy_local, 'run', lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 5, 'no tests ran', ''))
    empty_guard = mk.suite(cfg, A, [node])
    assert empty_guard.status == 'blocked' and 'pytest exited 5' in empty_guard.reason
    assert not empty_guard.failing_tests
    monkeypatch.setattr(remote_test, 'runner_state', lambda *a: ('unreachable', 'No route to host'))
    monkeypatch.setattr(train, 'where_local', lambda **kw: (_ for _ in ()).throw(train.TrainError('load refused', train.EXIT_REFUSED)))
    assert mk.suite(cfg, B, []).status == 'blocked'


def test_browser_retest_skips_empty_phase_before_placement(tmp_path, monkeypatch):
    import subprocess

    import deploy_local
    import remote_test
    cfg, _, _ = setup(tmp_path)
    tests = tmp_path / 'tests'
    tests.mkdir()
    node = 'tests/test_example_browser.py::test_example'
    (tests / 'test_example_browser.py').write_text('def test_example():\n    assert True\n')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/remote_test.py').touch()
    monkeypatch.setattr(remote_test, 'runner_state', lambda *a: ('free', ''))
    monkeypatch.setattr(remote_test, 'runner_settings', lambda: ('synthetic-host', '/tmp/synthetic-key'))
    monkeypatch.setattr(deploy_local, '_run_suite',
                        lambda run, repo, sha: deploy_local._pytest_phases(run, sys.executable, repo, sha))
    phases, executed = [], []
    adapter = remote_test.m1_browser_runner

    def placement(run, sha):
        placed = adapter(run, sha)

        def record(argv, **kwargs):
            phases.append(argv)
            return placed(argv, **kwargs)

        return record

    monkeypatch.setattr(remote_test, 'm1_browser_runner', placement)
    deselect = False

    def run(argv, **kwargs):
        executed.append(argv)
        # Execute the remote payload locally against a synthetic test, without SSH.
        payload = argv[argv.index('--') + 1:]
        if deselect:
            payload += ['--deselect=' + node]
        return subprocess.run([sys.executable, '-m', 'pytest', *payload], cwd=tmp_path,
                              capture_output=True, text=True,
                              env={'PATH': '/usr/bin:/bin', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'})

    monkeypatch.setattr(deploy_local, 'run', run)
    assert mk.suite(cfg, A, [node]).status == 'green'
    assert len(phases) == len(executed) == 1
    assert phases[0][1:5] == ['-m', 'pytest', '-p', 'no:xdist']
    assert node in executed[0] and '--ignore-glob=tests/test_*_browser.py' not in executed[0]
    deselect = True
    outcome = mk.suite(cfg, A, [node])
    assert outcome.status == 'blocked' and 'pytest exited 5' in outcome.reason


def test_stale_train_selection_cannot_starve_full_master_suite(tmp_path, monkeypatch):
    import subprocess

    import deploy_local
    import remote_test
    cfg, _, _ = setup(tmp_path)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_present.py').write_text('def test_present():\n    assert True\n')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/remote_test.py').touch()
    # A branch-only failure was persisted by an older keeper without a commit field.
    stale = 'tests/test_removed.py::test_removed'
    train_log = cfg.landing / 'example-run.log'
    train_log.write_text(f'FAILED {stale}\n')
    mk.save(cfg.landing / 'master-keeper.json', {'watch_started': 100, 'confirm': {
        'tests': [stale], 'log': str(train_log), 'digest': 'old', 'output': ''}})
    monkeypatch.setattr(remote_test, 'runner_state', lambda *a: ('free', ''))
    monkeypatch.setattr(remote_test, 'runner_settings', lambda: ('synthetic-host', '/tmp/synthetic-key'))
    monkeypatch.setattr(deploy_local, '_run_suite',
                        lambda run, repo, sha: deploy_local._pytest_phases(run, sys.executable, repo, sha))
    guards = []
    monkeypatch.setattr(deploy_local, '_run_private_guards', lambda *args: guards.append(args[2]))
    executed = []

    def run(argv, **kwargs):
        # Execute the remote payload locally: real pytest, selection and worker count.
        payload = argv[argv.index('--') + 1:]
        result = subprocess.run([sys.executable, '-m', 'pytest', '-n', '2', '-q', *payload],
                                cwd=kwargs['cwd'], capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin'})
        executed.append(result)
        return result

    monkeypatch.setattr(deploy_local, 'run', run)
    state = mk.tick(cfg, A, check=lambda sha, tests: mk.suite(cfg, sha, tests))
    assert state.get('checked') == state.get('last_green') == A, [r.stdout for r in executed]
    assert len(executed) == 1 and executed[0].returncode == 0
    assert '1 passed' in executed[0].stdout and guards == [A]
    assert 'confirm' not in state
    assert state['classifications'][str(train_log)] == 'branch failure'
    assert A in (cfg.landing / 'master-keeper.log').read_text()


@pytest.mark.parametrize('confirmation', [False, True])
def test_exit5_logs_and_posts_once_without_marking_master_checked(tmp_path, monkeypatch, confirmation):
    cfg, _, _ = setup(tmp_path)
    if confirmation:
        mk.save(cfg.landing / 'master-keeper.json', {
            'checked': A, 'last_green': A, 'confirm': {'commit': A, 'tests': [BAD]}})
    outcome = mk.Outcome('blocked', reason=f'the suite failed on {A[:8]}: pytest exited 5, not deployed')
    monkeypatch.setattr(mk, 'background_check', lambda *args: outcome)
    for now in (100, 200):
        state = mk.tick(cfg, A, now=now)
    lines = (cfg.landing / 'master-keeper.log').read_text().splitlines()
    assert len(lines) == 1 and A in lines[0] and 'no tests collected' in lines[0]
    rows = [json.loads(line) for line in cfg.inbox.read_text().splitlines()]
    assert len(rows) == 1 and A in rows[0]['text'] and 'pytest exited 5' in rows[0]['text']
    assert state.get('checked') == (A if confirmation else None)
    assert state.get('last_green') == (A if confirmation else None)
    assert not (cfg.landing / 'master-health.json').exists()
    assert not (cfg.landing / 'queue-hold').exists()


def test_new_master_waits_before_old_repair_can_start(tmp_path, monkeypatch):
    import subprocess
    cfg, _, _ = setup(tmp_path)
    cfg.queue.write_text('master-fix-example system branch\n')
    mk.save(cfg.landing / 'master-health.json', {'commit': A})
    mk.set_hold(cfg, 'example')
    monkeypatch.setattr(lr, 'git', lambda *a: subprocess.CompletedProcess(a, 0, B, ''))
    assert lr.cycle(cfg) == 'held'
    assert 'master-fix-example' in cfg.queue.read_text()
    mk.save(cfg.landing / 'master-health.json', {'commit': A, 'tested_commit': B})
    cfg.process = lambda *x: None
    assert lr.cycle(cfg) == 'ran:master-fix-example'


def test_refusal_is_checked_on_current_master_when_master_moves(tmp_path):
    cfg, notices, _ = setup(tmp_path)
    cfg.queue.write_text('example system branch\n')
    (cfg.landing / 'example-run.log').write_text(f'FAILED {BAD}\n')
    mk.save(cfg.landing / 'example-master-pending.json', {'line': 'example system branch', 'base': A})
    calls = []
    def check(sha, tests):
        calls.append((sha, tests))
        return mk.Outcome('red', [BAD]) if sha == A else mk.Outcome('green')
    kwargs = dict(check=check, notify=lambda *x: notices.append(x), dispatch=lambda *x: 'STARTED',
                  commits=lambda *x: '')
    mk.tick(cfg, B, now=100, **kwargs)
    assert calls == [(B, [])]  # Full green master also resolves the branch's failure evidence.
    assert not (cfg.landing / 'master-health.json').exists()
    assert cfg.queue.read_text() == ''
    assert 'example system branch' in (cfg.landing / 'quarantine.txt').read_text()


def test_suite_lock_does_not_block_landing_or_second_keeper(tmp_path):
    import subprocess
    import time
    cfg, _, _ = setup(tmp_path)
    ready = tmp_path / 'ready'
    script = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import master_keeper as mk
def suite(*args):
    Path(sys.argv[2]).touch()
    time.sleep(30)
    return mk.Outcome('green')
mk.suite = suite
raise SystemExit(mk.main(sys.argv[3:]))
"""
    args = ['--repo', str(tmp_path), '--landing', str(cfg.landing),
            '--suite', str(tmp_path / 'job.json'), '--sha', A]
    child = subprocess.Popen([sys.executable, '-c', script, str(Path(mk.__file__).parent), str(ready), *args])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        lock = lr.acquire_lock(cfg.landing)
        assert lock is not None, 'keeper suite holds runner.lock'
        lock.close()
        second = subprocess.run([sys.executable, '-c', script, str(Path(mk.__file__).parent),
                                 str(tmp_path / 'second'), *args], timeout=3)
        assert second.returncode == 0
        assert not (tmp_path / 'second').exists()
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_moved_master_waits_for_active_suite_then_checks_latest(tmp_path, monkeypatch):
    import subprocess
    cfg, _, _ = setup(tmp_path)
    launched = []
    monkeypatch.setattr(mk.subprocess, 'Popen', lambda argv, **kw:
                        launched.append(argv) or type('Child', (), {'pid': 123})())
    assert mk.background_check(cfg, A, []).status == 'pending'
    path = Path(launched[0][launched[0].index('--suite') + 1])
    monkeypatch.setattr(mk.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, str(path), ''))
    assert mk.background_check(cfg, B, []).status == 'pending'
    assert len(launched) == 1
    mk.save(path, {'result': {'status': 'red', 'failing_tests': [BAD]}})
    newest = 'c' * 40
    assert mk.background_check(cfg, newest, []).status == 'pending'
    assert len(launched) == 2
    assert launched[-1][launched[-1].index('--sha') + 1] == newest
    assert not (cfg.landing / 'queue-hold').exists()

    # A stale full-suite confirmation must not postpone checking current master.
    mk.save(cfg.landing / 'master-keeper.json', {
        'checked': A, 'confirm': {'commit': A, 'tests': [BAD], 'output': 'old failure'}})
    checked = []
    state = mk.tick(cfg, newest, check=lambda sha, tests:
                    checked.append((sha, tests)) or mk.Outcome('green'), notify=lambda *x: None)
    assert checked == [(newest, [])]
    assert state['last_green'] == newest
    assert not (cfg.landing / 'queue-hold').exists()
