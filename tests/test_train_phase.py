"""Use the existing shard executor for real failure/accounting semantics."""
from pathlib import Path

import pytest

from companion import runner_exec as ex
from companion import runner_pool as rp
from companion import train_phase

pytestmark = pytest.mark.timing_sensitive



def pool():
    return [rp.Runner('pc', 'windows', 32, 96, reach='ssh', host='example.invalid'),
            rp.Runner('m1', 'macos', 8, 16, reach='ssh', host='example.invalid')]


class Transport:
    def __init__(self, dead=(), omit=False):
        self.dead, self.omit, self.ran, self.killed = set(dead), omit, [], []

    def run(self, attempt, env):
        self.ran.append((attempt.runner.name, attempt.paths))
        if attempt.runner.name in self.dead:
            return ex.ShardResult(75)
        return ex.ShardResult(0, {} if self.omit else {p: ex.FileResult('pass', 1) for p in attempt.paths})

    def kill(self, attempt):
        self.killed.append(attempt.id)


def test_selected_train_runs_elsewhere_after_machine_goes_down():
    runners = pool()
    transport = Transport(dead={'pc'})
    report = train_phase.execute(['tests/test_example.py'], runners, {r.name: rp.Load() for r in runners},
                                 transport, preferred='pc', root=Path('/tmp'), log=lambda s: None)
    assert report.verdict == 'pass'
    assert report.files == {'tests/test_example.py': ('pass', 'm1')}
    assert [name for name, paths in transport.ran] == ['pc', 'm1']
    assert transport.killed == ['pc#1']


def test_missing_result_is_never_success():
    runners = pool()
    with pytest.raises(ex.AccountingError):
        train_phase.execute(['tests/test_example.py'], runners, {r.name: rp.Load() for r in runners},
                            Transport(omit=True), preferred='pc', root=Path('/tmp'), log=lambda s: None)


def test_large_known_work_splits_and_all_files_are_accounted_once():
    runners = pool()
    paths = [f'tests/test_example_{i}.py' for i in range(12)]
    transport = Transport()
    report = train_phase.execute(paths, runners, {r.name: rp.Load() for r in runners}, transport,
                                 preferred='pc', durations=dict.fromkeys(paths, 1000),
                                 root=Path('/tmp'), log=lambda s: None)
    assert report.verdict == 'pass' and sorted(report.files) == sorted(paths)
    assert {name for name, paths in transport.ran} == {'pc', 'm1'}


def test_no_free_machine_refuses_instead_of_claiming_coverage():
    with pytest.raises(rp.NoRunner):
        train_phase.execute(['tests/test_example.py'], pool(), {}, Transport(), preferred='pc',
                            root=Path('/tmp'), log=lambda s: None)


def test_gate_selectors_preserve_requested_files_and_exclude_separate_guards(tmp_path):
    root = tmp_path
    (root / 'tests').mkdir()
    for name in ('test_a.py', 'test_a_browser.py', 'test_no_personal_files.py'):
        (root / 'tests' / name).write_text('')
    assert train_phase.phase_files(['python', 'remote_test.py', '--',
                                    '--ignore-glob=tests/test_*_browser.py'], root) == ['tests/test_a.py']
    assert train_phase.phase_files(['python', 'remote_test.py', '--', '-o', 'addopts=', '-p',
                                    'no:cacheprovider', 'tests/test_a.py::test_one'], root) == ['tests/test_a.py']
    with pytest.raises(ValueError, match='unsupported'):
        train_phase.phase_files(['python', 'remote_test.py', '--', '-k', 'one'], root)


def test_gate_wrapper_passes_selected_runner_and_never_masks_phase_failure(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import remote_test as rt
    monkeypatch.setenv('KYRA_GATE', '1')
    monkeypatch.setenv('KYRA_TRAIN_RUNNER', 'pc')
    seen = []

    def phase(argv, **kwargs):
        seen.append((argv, kwargs['preferred']))
        raise rp.NoRunner('all machines unavailable')

    monkeypatch.setattr(train_phase, 'gate_phase', phase)
    wrapped = rt.m1_browser_runner(lambda *a, **kw: pytest.fail('uncontrolled local fallback'), 'abc')
    with pytest.raises(Exception, match='all machines unavailable'):
        wrapped(['python', 'remote_test.py', '--', '--ignore-glob=tests/test_*_browser.py'], cwd=tmp_path)
    assert seen[0][1] == 'pc'


def test_machine_usage_survives_train_summary_and_reports_failed_train(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import train

    logfile = tmp_path / 'train.log'
    logfile.write_text('KYRA_MACHINE_USAGE={"pc": 120, "m1": 60}\nrefused: tests failed\n')
    summary = '\n'.join(train.summary_lines(logfile))
    assert train_phase.usage_lines(summary, landed=False) == [
        'machine-stats: m1 trains=1 minutes=1.00 landed=0',
        'machine-stats: pc trains=1 minutes=2.00 landed=0',
    ]
    assert train_phase.usage_lines('KYRA_MACHINE_USAGE={"pc": -1}', landed=True) == []


def test_gate_phase_connects_config_probe_failover_and_receipts(tmp_path, monkeypatch):
    import json

    from companion import runner_probe, train_pool

    data = tmp_path / 'custom-state'
    config = data / 'config/runners.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'runners': [
        {'name': r.name, 'os': r.os, 'cores': r.cores, 'reach': r.reach, 'host': r.host}
        for r in pool()
    ]}))
    script = tmp_path / 'remote_test.py'
    script.write_text('def remote_command_ps(): pass\n')
    transport = Transport(dead={'pc'})
    monkeypatch.setattr(runner_probe, 'make_probe', lambda **kw: lambda r: rp.Load())
    monkeypatch.setattr(train_pool, 'RemoteTestTransport', lambda *a, **kw: transport)
    logs = []
    result = train_phase.gate_phase(['python', 'remote_test.py', '--', 'tests/test_example.py'],
                                   work=tmp_path, data=data, ref='abc', preferred='pc',
                                   local_phase=lambda: pytest.fail('local must not run'),
                                   local_refusal=lambda: None, remote_script=script, log=logs.append)
    assert result.returncode == 0
    assert [name for name, paths in transport.ran] == ['pc', 'm1']
    assert set(json.loads(logs[-1].split('=', 1)[1])) == {'pc', 'm1'}
    timing = json.loads((data / 'team/test-durations.json').read_text())
    assert timing['tests/test_example.py'] == 1
    assert timing['_runners'] == {'m1': {'tests/test_example.py': 1}}


def test_selected_local_priority_runs_here_even_when_m1_is_available():
    runners = [rp.Runner('mac', 'macos', 18, 36), pool()[1]]
    transport = Transport()
    report = train_phase.execute(['tests/test_example.py'], runners,
                                 {r.name: rp.Load() for r in runners}, transport,
                                 preferred='mac', root=Path('/tmp'), log=lambda s: None)
    assert report.files == {'tests/test_example.py': ('pass', 'mac')}
    assert [name for name, paths in transport.ran] == ['mac']


def test_configured_local_wrapper_uses_receipts_without_remote_fallback(monkeypatch, tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import remote_test as rt
    monkeypatch.setenv('KYRA_GATE', '1')
    monkeypatch.setenv('KYRA_TRAIN_RUNNER', 'mac')
    monkeypatch.setenv('KYRA_BROWSER_LOCAL', '1')
    seen = []
    def phase(argv, **kwargs):
        seen.append((kwargs['preferred'], kwargs['local_only']))
        raise rp.NoRunner('admission refused')
    monkeypatch.setattr(train_phase, 'gate_phase', phase)
    wrapped = rt.m1_browser_runner(lambda *a, **kw: pytest.fail('legacy fallback'), 'abc')
    with pytest.raises(Exception, match='admission refused'):
        wrapped(['python', 'remote_test.py', '--', 'tests/test_example.py'], cwd=tmp_path)
    assert seen == [('mac', True)]


@pytest.mark.parametrize('own', [True, False])
def test_phase_probe_shares_only_its_own_live_locks(monkeypatch, tmp_path, own):
    import os
    from types import SimpleNamespace

    from companion import browser_lock, gate_lock, runner_probe
    owner = os.getpid() if own else os.getpid() + 100000
    monkeypatch.setattr(gate_lock, 'lock_path', lambda *a: tmp_path / 'gpu.lock')
    monkeypatch.setattr(gate_lock, 'conflicting', lambda p: [p])
    monkeypatch.setattr(gate_lock, 'read', lambda p: {'pid': owner})
    monkeypatch.setattr(gate_lock, 'live', lambda row: True)
    monkeypatch.setattr(browser_lock, 'read_lock', lambda: {'pid': owner})
    monkeypatch.setattr(browser_lock, '_ancestor', lambda pid: False)
    src = train_phase.phase_local_sources(SimpleNamespace(users=1))
    assert src.gpu_lock() is (not own)
    assert src.browser_lock() is (not own)
    # Ownership cannot waive other admission conditions.
    src.travel = lambda: True
    assert runner_probe.local_reason(src) is not None
    # The same pid without an admitted LocalPhase cannot waive a GPU lock.
    if own:
        assert train_phase.phase_local_sources(SimpleNamespace(users=0)).gpu_lock()


@pytest.mark.parametrize('own', [True, False])
def test_gate_phase_joins_own_browser_lock_but_refuses_foreign_lock(tmp_path, monkeypatch, own):
    import json
    import os
    from contextlib import nullcontext

    from companion import browser_lock, gate_lock, mac_guard, runner_probe, train_pool

    config = tmp_path / 'state/config/runners.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'runners': [
        {'name': 'mac', 'os': 'macos', 'cores': 18, 'reach': 'local'},
    ]}))
    script = tmp_path / 'remote_test.py'
    script.write_text('')
    owner = os.getpid() if own else os.getpid() + 100000
    monkeypatch.setattr(gate_lock, 'lock_path', lambda *a: tmp_path / 'gpu.lock')
    monkeypatch.setattr(gate_lock, 'conflicting', lambda p: [p])
    monkeypatch.setattr(gate_lock, 'read', lambda p: {'pid': owner})
    monkeypatch.setattr(gate_lock, 'live', lambda row: True)
    monkeypatch.setattr(browser_lock, 'read_lock', lambda: {'pid': owner})
    monkeypatch.setattr(browser_lock, '_ancestor', lambda pid: False)
    monkeypatch.setattr(mac_guard, 'admit', lambda reason: None)
    real_probe = runner_probe.make_probe
    def probe(**kwargs):
        src = kwargs.get('local') or runner_probe.LocalSources()
        src.pressure, src.memory = lambda: '1', lambda: 'free percentage: 90%'
        src.load, src.cores, src.travel = lambda: 0, lambda: 18, lambda: False
        src.thermal, src.battery = lambda: '', lambda: 'AC Power'
        return real_probe(local=src, m1_settings=lambda: ('', ''))
    monkeypatch.setattr(runner_probe, 'make_probe', probe)
    monkeypatch.setattr(train_pool, 'worker_budget', lambda *a: 2)
    ran = []
    def run(self, attempt, env):
        ran.append(attempt.runner.name)
        return ex.ShardResult(0, {p: ex.FileResult('pass', 1) for p in attempt.paths})
    monkeypatch.setattr(train_pool.LocalTransport, 'run', run)
    class ActivePhase:
        users = 1
        def __call__(self):
            return nullcontext()
    def execute():
        return train_phase.gate_phase(['python', 'remote_test.py', '--', 'tests/test_example.py'],
                                      work=tmp_path, data=tmp_path / 'state', ref='abc', preferred='mac',
                                      local_phase=ActivePhase(), local_refusal=lambda: None,
                                      remote_script=script, log=lambda line: None)
    if own:
        assert execute().returncode == 0
        assert ran == ['mac']
    else:
        with pytest.raises(rp.NoRunner):
            execute()
        assert ran == []


def test_remote_loss_never_moves_ordinary_tests_to_owner_mac():
    runners = [pool()[0], rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True)]
    transport = Transport(dead={'pc'})
    with pytest.raises(rp.NoRunner, match='no online runner'):
        train_phase.execute(['tests/test_example.py'], runners,
                            {r.name: rp.Load() for r in runners}, transport,
                            preferred='pc', root=Path('/tmp'), log=lambda s: None)
    assert [name for name, _ in transport.ran] == ['pc']


def test_remote_phase_keeps_required_private_checks_local():
    runners = [pool()[0], rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True)]
    transport = Transport()
    path = 'tests/test_no_target_companies.py'  # in runner_pool.REAL_DATA_TESTS (master dropped test_correct_repeaters)
    report = train_phase.execute([path, 'tests/test_example.py'], runners,
                                 {r.name: rp.Load() for r in runners}, transport,
                                 preferred='pc', root=Path('/tmp'), log=lambda s: None)
    assert report.files[path] == ('pass', 'mac')
    assert report.files['tests/test_example.py'] == ('pass', 'pc')



def test_smoke_admission_preserves_memory_swap_accounting_and_severe_load(monkeypatch):
    import sys
    from types import SimpleNamespace

    from companion import headroom, mac_guard
    from companion import local_test_supervisor as supervisor

    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: None)
    from companion import runner_probe

    sources = runner_probe.LocalSources(load=lambda: 14, cores=lambda: 18,
        pressure=lambda: '1', memory=lambda: 'free percentage: 90%',
        thermal=lambda: '', battery=lambda: 'AC Power',
        browser_lock=lambda: False, gpu_lock=lambda: False, travel=lambda: False)
    monkeypatch.setattr(train_phase, 'phase_local_sources', lambda phase: sources)
    probes = SimpleNamespace(load=lambda: 14, cores=lambda: 18)
    monkeypatch.setattr(headroom, 'Probes', lambda: probes)
    monkeypatch.setattr(headroom, 'travel_refusal', lambda *a: None)
    monkeypatch.setattr(headroom, 'memory_refusal', lambda *a: None)
    monkeypatch.setattr(supervisor, 'swap_bytes', lambda: 0)
    monkeypatch.setattr(supervisor, 'footprint', lambda pid: 10)
    monkeypatch.setattr(supervisor, 'process_tree', lambda pid: {pid: (pid, 'R')})
    assert train_phase.smoke_refusal(None) is None
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: 'load average 14 is above 10')
    assert train_phase.smoke_refusal(None, sources, lambda: None) == 'load average 14 is above 10'
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: None)
    for attr, value, why in [('gpu_lock', True, 'gpu lock'), ('browser_lock', True, 'browser-phase'),
                             ('battery', "Now drawing from 'Battery Power'", 'battery')]:
        previous = getattr(sources, attr)
        setattr(sources, attr, lambda value=value: value)
        assert why in train_phase.smoke_refusal(None)
        setattr(sources, attr, previous)
    assert train_phase.smoke_refusal(None, sources, lambda: 'another heavy job') == 'another heavy job'
    probes.load = lambda: 17
    assert 'unsafe smoke load' in train_phase.smoke_refusal(None)
    probes.load = lambda: 14
    monkeypatch.setattr(headroom, 'memory_refusal', lambda *a: 'memory pressure is warn')
    assert train_phase.smoke_refusal(None) == 'memory pressure is warn'
    monkeypatch.setattr(headroom, 'memory_refusal', lambda *a: None)
    monkeypatch.setattr(supervisor, 'swap_bytes', lambda: supervisor.SWAP_BYTES + 1)
    assert 'swap' in train_phase.smoke_refusal(None)
    monkeypatch.setattr(supervisor, 'swap_bytes', lambda: 0)
    monkeypatch.setattr(supervisor, 'process_tree', lambda pid: {})
    assert 'accounting' in train_phase.smoke_refusal(None)


@pytest.mark.parametrize('where,state', [('auto', 'free'), ('auto', 'held'), ('auto', 'unreachable'), ('local', 'unreachable')])
def test_smoke_transport_is_serial_exclusive_and_supervised(tmp_path, monkeypatch, where, state):
    import runpy
    from types import SimpleNamespace

    from companion import local_test_supervisor as supervisor
    from companion import train_pool

    path = 'tests/test_correct_repeaters.py'
    attempt = ex.Attempt('mac#1', rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True), (path,))
    monkeypatch.setattr(train_phase, 'smoke_refusal', lambda *args: None)
    calls = []
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        junit = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--junitxml=')))
        junit.write_text('<testsuite><testcase classname="tests.test_correct_repeaters" name="test_one" time="1"/></testsuite>')
        return SimpleNamespace(pid=999999)
    probes = []
    def runner_state(data):
        probes.append(data)
        return state, 'synthetic fresh probe'
    monkeypatch.setattr(runpy, 'run_path', lambda path: {'runner_state': runner_state})
    local = train_pool.LocalTransport(tmp_path, popen=popen,
        env={'PYTEST_ADDOPTS': '-n 8', 'KYRA_WHERE': where, 'KYRA_RUNNER_STATE': 'stale'})
    local.smoke_admission = lambda: train_phase.smoke_refusal(tmp_path)
    local.smoke_local_only = where == 'local'
    def wait(proc, **kwargs):
        assert kwargs == {'timeout': 120}
        # The same canonical lock excludes another train until supervisor cleanup returns.
        with pytest.raises(train_pool.PoolError, match='another real-data smoke'):
            train_phase.run_smoke(local, attempt, {}, tmp_path)
        return 0, ''
    monkeypatch.setattr(supervisor, 'wait', wait)
    result = train_phase.run_smoke(local, attempt, {}, tmp_path)
    assert result.exit_code == 0 and result.files[path].outcome == 'pass'
    argv, kwargs = calls[0]
    assert argv[:3] == ['/usr/bin/nice', '-n', '10']
    assert '-n' not in argv[3:] and 'no:xdist' in argv
    assert kwargs['env']['PYTEST_ADDOPTS'] == '' and kwargs['start_new_session']
    assert kwargs['env']['KYRA_PREFLIGHT'] == '1'
    assert kwargs['env']['KYRA_RUNNER_STATE'] == state
    assert probes == ([] if where == 'local' else [tmp_path])
    if where != 'local':
        assert 'KYRA_WHERE' not in kwargs['env']
    assert local.procs.pop(attempt) is None
    monkeypatch.setattr(train_phase, 'smoke_refusal', lambda *args: 'memory pressure is warn')
    with pytest.raises(train_pool.PoolError, match='warn'):
        train_phase.run_smoke(local, attempt, {}, tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize('other', ['tests/test_example.py', 'tests/test_intern_promotion.py'])
def test_healthy_local_phase_keeps_ordinary_and_other_realdata_capacity(tmp_path, monkeypatch, other):
    from contextlib import nullcontext

    from companion import mac_guard, runner_probe, train_pool

    machine = rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True)
    monkeypatch.setattr(rp, 'load_registry', lambda path: [machine])
    monkeypatch.setattr(runner_probe, 'make_probe', lambda **kw: lambda r: rp.Load())
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: None)
    monkeypatch.setattr(train_phase, 'smoke_refusal', lambda *args: None)
    ran = []
    def smoke(local, attempt, env, data):
        ran.append(('smoke', attempt.paths))
        return ex.ShardResult(0, {p: ex.FileResult('pass', 1) for p in attempt.paths})
    def ordinary(local, attempt, env):
        ran.append(('ordinary', attempt.paths))
        return ex.ShardResult(0, {p: ex.FileResult('pass', 1) for p in attempt.paths})
    monkeypatch.setattr(train_phase, 'run_smoke', smoke)
    monkeypatch.setattr(train_pool.LocalTransport, 'run', ordinary)
    script = tmp_path / 'remote_test.py'
    script.write_text('def remote_command_ps(): pass\n')
    path = 'tests/test_correct_repeaters.py'
    result = train_phase.gate_phase(['python', 'remote_test.py', '--', path, other],
        work=tmp_path, data=tmp_path, ref='abc', preferred='mac', local_only=True,
        local_phase=nullcontext, local_refusal=lambda: None, remote_script=script, log=lambda s: None)
    assert result.returncode == 0
    assert ran == [('smoke', (path,)), ('ordinary', (other,))]


def test_gate_local_only_smoke_never_probes_remote(tmp_path, monkeypatch):
    import runpy
    from contextlib import nullcontext
    from types import SimpleNamespace

    from companion import local_test_supervisor as supervisor
    from companion import mac_guard, runner_probe, train_pool

    monkeypatch.delenv('KYRA_WHERE', raising=False)
    machine = rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True)
    monkeypatch.setattr(rp, 'load_registry', lambda path: [machine])
    monkeypatch.setattr(runner_probe, 'make_probe', lambda **kw: lambda r: rp.Load())
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: None)
    monkeypatch.setattr(train_phase, 'smoke_refusal', lambda *args: None)
    monkeypatch.setattr(runpy, 'run_path', lambda path: pytest.fail('local-only remote probe'))
    seen = []
    def popen(argv, **kwargs):
        seen.append(kwargs['env'])
        junit = Path(next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--junitxml=')))
        junit.write_text('<testsuite><testcase classname="tests.test_correct_repeaters" name="test_one" time="1"/></testsuite>')
        return SimpleNamespace(pid=999999)
    original = train_pool.LocalTransport.__init__
    def init(self, *args, **kwargs):
        original(self, *args, popen=popen, **kwargs)
    monkeypatch.setattr(train_pool.LocalTransport, '__init__', init)
    monkeypatch.setattr(supervisor, 'wait', lambda *args, **kwargs: (0, ''))
    script = tmp_path / 'remote_test.py'
    script.write_text('def remote_command_ps(): pass\n')
    result = train_phase.gate_phase(['python', 'remote_test.py', '--', 'tests/test_correct_repeaters.py'],
        work=tmp_path, data=tmp_path, ref='abc', preferred='mac', local_only=True,
        local_phase=nullcontext, local_refusal=lambda: None, remote_script=script, log=lambda s: None)
    assert result.returncode == 0
    assert len(seen) == 1
    assert seen[0]['KYRA_WHERE'] == 'local'
    assert seen[0]['KYRA_RUNNER_STATE'] == 'unreachable'
    assert seen[0]['KYRA_PREFLIGHT'] == '1'


@pytest.mark.parametrize('settles', [True, False])
def test_smoke_wait_observes_admission_and_keeps_timeout_refusal(settles):
    now = [0]
    checks = []
    def admission():
        checks.append(now[0])
        return None if settles and now[0] >= 5 else 'load average 10.7 is above 10'
    def sleep(seconds):
        now[0] += seconds
    result = train_phase.wait_smoke_admission(admission, lambda message: None,
        clock=lambda: now[0], sleep=sleep, timeout=12)
    assert result == (None if settles else 'load average 10.7 is above 10')
    assert checks == ([0, 5] if settles else [0, 5, 10, 12])


def test_smoke_wait_reprobes_real_capacity_after_transient_load(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from companion import mac_guard, runner_probe

    machine = rp.Runner('mac', 'macos', 18, 36, may_see_real_data=True)
    monkeypatch.setattr(rp, 'load_registry', lambda path: [machine])
    settled = [False]
    probes = []
    def probe(runner):
        probes.append(settled[0])
        return rp.Load(5 if settled[0] else 18)
    monkeypatch.setattr(runner_probe, 'make_probe', lambda **kw: probe)
    monkeypatch.setattr(mac_guard, 'admit', lambda *a: None if settled[0] else 'load')
    monkeypatch.setattr(train_phase, 'smoke_refusal', lambda *a: None if settled[0] else 'load')
    def wait(admission, log):
        assert admission() == 'load'
        settled[0] = True
        return admission()
    monkeypatch.setattr(train_phase, 'wait_smoke_admission', wait)
    monkeypatch.setattr(train_phase, 'run_smoke', lambda local, attempt, env, data:
        ex.ShardResult(0, {p: ex.FileResult('pass', 1) for p in attempt.paths}))
    script = tmp_path / 'remote_test.py'
    script.write_text('def remote_command_ps(): pass\n')
    result = train_phase.gate_phase(['python', 'remote_test.py', '--', 'tests/test_correct_repeaters.py'],
        work=tmp_path, data=tmp_path, ref='abc', preferred='mac', local_only=True,
        local_phase=nullcontext, local_refusal=lambda: None, remote_script=script, log=lambda s: None)
    assert result.returncode == 0
    assert probes == [False, True]


def test_local_training_labels_visible_only_during_local_check(tmp_path):
    from companion.train_pool import PoolError

    data, work = tmp_path / 'private', tmp_path / 'checkout'
    source = data / 'decisions/labels'
    source.mkdir(parents=True)
    work.mkdir()
    (source / 'synthetic.jsonl').write_text('synthetic local input')
    paths = ['tests/test_router_train_real.py']
    with pytest.raises(RuntimeError, match='child failed'):
        with train_phase.local_training_data(work, data, paths):
            assert (work / 'data/decisions/labels/synthetic.jsonl').read_text() == 'synthetic local input'
            raise RuntimeError('child failed')
    assert not (work / 'data').exists()
    assert (source / 'synthetic.jsonl').read_text() == 'synthetic local input'
    with train_phase.local_training_data(work, data, ['tests/test_example.py']):
        assert not (work / 'data').exists()
    target = work / 'data/decisions/labels'
    target.mkdir(parents=True)
    (target / 'keep').write_text('existing')
    with pytest.raises(PoolError, match='different training-label store'):
        with train_phase.local_training_data(work, data, paths):
            pytest.fail('different store was accepted')
    assert (target / 'keep').read_text() == 'existing'
    with train_phase.local_training_data(data, data, paths):
        assert source.is_dir()
