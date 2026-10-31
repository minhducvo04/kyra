"""Place one landing phase and reuse the runner pool's failover and JUnit accounting."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from companion import runner_exec, runner_pool


def execute(paths, runners, loads, transport, *, preferred, root, log, durations=None, deadline=3600, recorder=None):
    """Run each file once; a transport loss reassigns work, a missing receipt refuses.

    Small/untimed phases use one remote. Known file durations may justify splitting:
    the estimated saving must exceed 30 seconds for another archive/collection.
    This is a planning estimate, never a measured speedup claim.
    """
    durations = durations or {}
    jobs = [runner_pool.Job(p) for p in paths]
    # A remote train may use this Mac only for files that must read local
    # private state. Keep the restriction in the runner passed to failover too.
    ordinary = {j.path for j in jobs if not runner_pool.needs_real_data(j)}
    runners = [replace(r, skip=tuple(sorted(set(r.skip) | ordinary)))
               if r.reach == 'local' and r.name != preferred else r for r in runners]
    available = [r for r in runners if runner_pool.free_cores(r, loads.get(r.name)) > 0]
    remotes = [r for r in available if r.reach == 'ssh']
    local = [r for r in available if r.reach == 'local']
    selected_local = next((r for r in local if r.name == preferred), None)
    local_preferred = [j for j in jobs if selected_local and runner_pool.can_run(selected_local, j)]
    remote_jobs = [j for j in jobs if j not in local_preferred
                   and any(runner_pool.can_run(r, j) for r in remotes)]
    local_jobs = [j for j in jobs if j not in remote_jobs and j not in local_preferred]
    def probe(r):
        return loads.get(r.name)

    plan = runner_pool.shard(local_jobs, local, durations, probe) if local_jobs else {}
    if local_preferred:
        plan[selected_local.name] = [j.path for j in local_preferred]
    if remote_jobs:
        single = next((r for r in remotes if r.name == preferred
                       and all(runner_pool.can_run(r, j) for j in remote_jobs)), None)
        split = runner_pool.shard(remote_jobs, remotes, durations, probe)
        if single is not None:
            one = {single.name: [j.path for j in remote_jobs]}
            known = all(j.path in durations for j in remote_jobs)
            saving = (max(runner_pool.predicted_end(one, remotes, durations, probe).values())
                      - max(runner_pool.predicted_end(split, remotes, durations, probe).values()))
            if not known or saving <= 30:
                split = one
        plan.update(split)
    if not plan:
        raise runner_pool.NoRunner('no free capable machine for this phase')
    log('train phase placement: ' + ', '.join(f'{name}={len(files)} files' for name, files in plan.items()))
    return runner_exec.execute(plan, jobs, available, transport,
                               durations=durations, probe=probe, env={}, deadline=deadline,
                               repo_root=Path(root), log=log, recorder=recorder)


def phase_files(argv, root):
    """Translate the gate's known non-browser selectors; refuse unknown selection flags."""
    from companion.train_pool import test_files
    args = iter(argv[argv.index('--') + 1:] if '--' in argv else [])
    paths = []
    for arg in args:
        if arg in ('-o', '-p'):
            expected = 'addopts=' if arg == '-o' else 'no:cacheprovider'
            if next(args, None) != expected:
                raise ValueError('unsupported train phase pytest option')
        elif arg in ('--ignore-glob=tests/test_*_browser.py', '-q'):
            continue
        elif arg.startswith('tests/'):
            # Run the whole file for a node selector; junit accounting is file-based.
            paths.append(arg.split('::')[0])
        else:
            raise ValueError('unsupported train phase pytest option')
    guards = {'tests/test_no_third_party_pii.py', 'tests/test_no_personal_files.py',
              'tests/test_no_target_companies.py'}
    return sorted(set(paths or test_files(Path(root))) - guards)


def phase_local_sources(local_phase):
    """Observe locks while allowing only this gate's already admitted phase."""
    import os

    from companion import browser_lock, gate_lock, runner_probe

    def gpu_busy():
        if not getattr(local_phase, 'users', 0):
            return runner_probe._gpu_lock_live()
        path = gate_lock.lock_path(Path(__file__).resolve().parents[2], 'gpu')
        for other in gate_lock.conflicting(path):
            info = gate_lock.read(other)
            if info is not None and (not info or gate_lock.live(info)):
                if info.get('pid') != os.getpid():
                    return True
        return False

    def browser_busy():
        row = browser_lock.read_lock()
        if row is None:
            return False
        pid = row['pid']
        own = pid == os.getpid() or (os.environ.get('KYRA_BROWSER_GATE_PID') == str(pid)
                                     and browser_lock._ancestor(pid))
        return not own

    return runner_probe.LocalSources(gpu_lock=gpu_busy, browser_lock=browser_busy)


# This always-run local-data smoke uses a serial, supervised transport.
SMOKE_FILES = frozenset({'tests/test_correct_repeaters.py'})


def smoke_refusal(data, sources=None, local_refusal=None):
    """Keep the canonical Mac admission and all smoke resource limits."""
    import math
    import os
    import subprocess
    import sys

    from companion import headroom, mac_guard, runner_probe
    from companion import local_test_supervisor as supervisor

    if sys.platform != 'darwin':
        return 'real-data smoke requires this Mac'
    probes = headroom.Probes()
    try:
        reason = (mac_guard.admit('real-data smoke') or headroom.travel_refusal(data, probes) or headroom.memory_refusal(probes)
                  or runner_probe.local_reason(sources or phase_local_sources(None))
                  or (local_refusal() if local_refusal else None))
        if reason:
            return reason
        load, cores = probes.load(), probes.cores()
        if not math.isfinite(load) or cores < 1 or load > headroom.load_limit(probes):
            return f'unsafe smoke load {load:g} on {cores} cores'
        if supervisor.swap_bytes() > supervisor.SWAP_BYTES:
            return 'system swap exceeds 20 GiB'
        supervisor.footprint(os.getpid())
        if not supervisor.process_tree(os.getpid()):
            return 'process accounting unavailable'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f'resource accounting unavailable: {exc}'
    return None



def wait_smoke_admission(admission, log, *, clock=None, sleep=None, timeout=120):
    """Let transient preflight load settle; expiry remains a refusal, never extra capacity."""
    import time

    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + timeout
    reason = admission()
    if reason:
        log(f'train smoke waiting up to {timeout}s for admission: {reason}')
    while reason and clock() < deadline:
        sleep(min(5, max(0, deadline - clock())))
        reason = admission()
    return reason


def run_smoke(local, attempt, env, data):
    """One serial smoke across all trains, nice'd and watched by the unchanged memory supervisor."""
    import fcntl
    import runpy
    import subprocess
    import tempfile
    import xml.etree.ElementTree as ET

    from companion import local_test_supervisor as supervisor
    from companion import train_pool

    if (attempt.runner.reach != 'local' or not attempt.runner.may_see_real_data
            or not attempt.paths or not set(attempt.paths) <= SMOKE_FILES):
        raise train_pool.PoolError('invalid real-data smoke attempt')
    admission = local.smoke_admission
    lockpath = Path(data) / 'team/realdata-smoke.lock'
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise train_pool.PoolError('another real-data smoke is running') from None
        if reason := admission():
            raise train_pool.PoolError(f'real-data smoke refused: {reason}')
        with tempfile.TemporaryDirectory(prefix='kyra-smoke-') as folder, tempfile.TemporaryFile() as output:
            junit = Path(folder) / 'junit.xml'
            argv = ['/usr/bin/nice', '-n', '10', local.python, '-m', 'pytest', '-o', 'addopts=',
                    '-p', 'no:xdist', '-p', 'no:cacheprovider', '-q', f'--junitxml={junit}', *attempt.paths]
            child_env = train_pool.shard_env(local.env, env)
            child_env['PYTEST_ADDOPTS'] = ''
            # This file contains a preflight-only load check. Execute it, rather than
            # accepting its intentional non-preflight skip as a real-data receipt.
            child_env['KYRA_PREFLIGHT'] = '1'
            state = 'unreachable'
            if local.smoke_local_only:
                child_env['KYRA_WHERE'] = 'local'
            else:
                child_env.pop('KYRA_WHERE', None)
                remote = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/remote_test.py'))
                state, _ = remote['runner_state'](data)
            if state not in ('free', 'held', 'unreachable'):
                raise train_pool.PoolError('invalid fresh smoke runner state')
            child_env['KYRA_RUNNER_STATE'] = state
            if reason := admission():
                raise train_pool.PoolError(f'real-data smoke refused: {reason}')
            proc = local.popen(argv, cwd=local.worktree, env=child_env, stdin=subprocess.DEVNULL,
                               stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            local.procs.add(attempt, proc)
            try:
                code, reason = supervisor.wait(proc, timeout=120)
            finally:
                local.procs.pop(attempt)
            output.seek(0, 2)
            output.seek(max(0, output.tell() - 6000))
            text = output.read().decode(errors='replace')
            train_pool._log_failures(local.log, attempt, text)
            if reason:
                local.log(reason)
            try:
                files = train_pool.junit_results(junit, attempt.paths)
            except (OSError, ET.ParseError):
                files = train_pool.all_error(attempt.paths)
            return runner_exec.ShardResult(code, files)


@contextmanager
def local_training_data(work, data, paths):
    """Expose only handwritten labels to their local check, then remove our links/directories."""
    from companion import train_pool

    if 'tests/test_router_train_real.py' not in paths:
        yield
        return
    source = Path(data).resolve() / 'decisions/labels'
    if not source.is_dir():
        raise train_pool.PoolError('local handwritten training labels are unavailable')
    target = Path(work) / 'data/decisions/labels'
    # An existing store belongs to its checkout; never replace or remove it.
    if target.resolve() == source:
        yield
        return
    made = []
    linked = False
    try:
        for directory in (Path(work) / 'data', Path(work) / 'data/decisions'):
            if directory.is_symlink():
                raise train_pool.PoolError('unexpected private-data parent symlink')
            if not directory.exists():
                directory.mkdir()
                made.append(directory)
        if target.exists() or target.is_symlink():
            raise train_pool.PoolError('checkout already has a different training-label store')
        target.symlink_to(source, target_is_directory=True)
        linked = True
        yield
    finally:
        if linked:
            target.unlink()
        for directory in reversed(made):
            directory.rmdir()


def gate_phase(argv, *, work, data, ref, preferred, local_phase, local_refusal, remote_script, log, local_only=False):
    """Execute inside the existing landing gate; transport failures never trigger an unguarded fallback."""
    import json
    import re
    import subprocess

    from companion import runner_probe, train_pool

    paths = phase_files(argv, work)
    if not paths:
        raise runner_pool.NoRunner('no test files selected for the configured phase')
    machines = runner_pool.load_registry(runner_pool.registry_path(data))
    if not machines:
        raise runner_pool.NoRunner('machines config is missing or empty')
    # The Windows adapter remains a separate prerequisite. Never send POSIX snippets to it.
    if 'def remote_command_ps(' not in Path(remote_script).read_text(encoding='utf-8'):
        machines = [r for r in machines if r.os != 'windows']
    machines = [r for r in machines if 'system' in r.test_kinds
                and (not local_only or r.reach == 'local')]
    probe = runner_probe.make_probe(local=phase_local_sources(local_phase),
                                    m1_settings=lambda: ('', ''))
    loads = {r.name: probe(r) for r in machines}
    try:
        from companion import mac_guard
    except ImportError:
        mac_guard = None
    if (mac_guard is None or mac_guard.admit('configured landing phase') is not None
            or local_refusal() is not None):
        loads.update({r.name: None for r in machines if r.reach == 'local'})

    def smoke_admission():
        return smoke_refusal(data, phase_local_sources(local_phase), local_refusal)

    smoke_runners = set()
    if set(paths) & SMOKE_FILES:
        reason = smoke_admission()
        if reason and any(r.reach == 'local' and r.may_see_real_data and r.enabled for r in machines):
            reason = wait_smoke_admission(smoke_admission, log)
            if reason is None:
                # Admission changed while waiting. Re-probe actual local capacity; never invent a slot.
                loads.update({r.name: probe(r) for r in machines if r.reach == 'local'})
        restricted = []
        for machine in machines:
            if machine.reach == 'local' and machine.may_see_real_data and machine.enabled:
                if reason is None:
                    smoke_runners.add(machine.name)
                    log(f'train smoke placement: {machine.name}, serial supervised, 6 GiB ceiling, 120s timeout')
                else:
                    machine = replace(machine, skip=tuple(sorted(set(machine.skip) | SMOKE_FILES)))
                    log(f'train smoke refused: {reason}')
            restricted.append(machine)
        machines = restricted

    class AdmittedLocal(train_pool.LocalTransport):
        def run(self, attempt, env):
            smoke = None
            if attempt.runner.name in smoke_runners and set(attempt.paths) & SMOKE_FILES:
                smoke_paths = tuple(p for p in attempt.paths if p in SMOKE_FILES)
                smoke = run_smoke(self, replace(attempt, paths=smoke_paths), env, data)
                remaining = tuple(p for p in attempt.paths if p not in SMOKE_FILES)
                if not remaining:
                    return smoke
                if smoke.exit_code:
                    return runner_exec.ShardResult(smoke.exit_code,
                        dict(smoke.files) | train_pool.all_error(remaining))
                attempt = replace(attempt, paths=remaining)
            # Rechecks admission after planning and starts pytest with its normal supervisor.
            if mac_guard is None or mac_guard.admit('configured landing shard') is not None:
                raise train_pool.PoolError('Mac stability admission refused')
            with local_phase(), local_training_data(work, data, attempt.paths):
                result = super().run(attempt, env)
            return runner_exec.ShardResult(result.exit_code, dict(smoke.files) | dict(result.files)) if smoke else result

    local = AdmittedLocal(Path(work), log=log)
    local.smoke_admission = smoke_admission
    local.smoke_local_only = local_only
    remote = train_pool.RemoteTestTransport(Path(remote_script), ref, log=log)
    store = Path(data) / 'team/test-durations.json'
    planned = train_pool.plan_pool(paths, machines, loads, store)
    budgets = planned.budgets
    local.budgets = remote.workers = budgets
    durations = runner_pool.load_durations(store)
    transport = train_pool._Measured(train_pool.RoutedTransport(local, remote), budgets, durations,
                                     {r.name: r.speed for r in machines})
    attempts = {}

    def timed_log(line):
        match = re.fullmatch(r'runner-pool: ([a-z0-9-]+) (\S+) \d+ files ([\d.]+)s exit \S+ '
                            r'(?:accepted|discarded(?:, shard re-placed)?|deadline, shard re-placed)', line)
        if match:
            name, attempt, seconds = match.groups()
            # A deadline and its late completion are the same attempt.
            attempts[attempt] = (name, float(seconds))
        log(line)

    try:
        report = execute(paths, planned.runners, {r.name: planned.probe(r) for r in machines},
                         transport, preferred=preferred, root=work, log=timed_log,
                         durations=durations, recorder=train_pool.recorder(store, log, transport))
    finally:
        usage = {}
        for name, seconds in attempts.values():
            usage[name] = usage.get(name, 0) + seconds
        log('KYRA_MACHINE_USAGE=' + json.dumps(usage, sort_keys=True))
    code = 0 if report.verdict == 'pass' else 1
    return subprocess.CompletedProcess(argv, code, f'configured train phase: {report.verdict}, {len(report.files)} files\n', '')


def usage_lines(text, *, landed):
    """Per-attempt machine totals, including failed work; landing is supplied by ancestry verification."""
    import json
    import math
    import re

    totals = {}
    for line in text.splitlines():
        if not line.startswith('KYRA_MACHINE_USAGE='):
            continue
        try:
            row = json.loads(line.split('=', 1)[1])
        except ValueError:
            continue
        if not isinstance(row, dict) or any(
                not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name)
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                for name, seconds in row.items()):
            continue
        for name, seconds in row.items():
            totals[name] = totals.get(name, 0) + seconds
    return [f'machine-stats: {name} trains=1 minutes={seconds / 60:.2f} landed={int(landed)}'
            for name, seconds in sorted(totals.items())]
