"""Landing runner: queue parsing, priority order, single-instance lock, refusing while a train runs."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import landing_runner as lr  # noqa: E402

# Module snapshot of the temp origin. Read-only tests share it. Other tests copy it.
_GIT_ROOTS = []
_SNAPSHOTS = {}
_SHAS = {}


@pytest.fixture(autouse=True)
def no_real_repo(tmp_path, monkeypatch):
    """Safety: a test (or a mutation of the runner) must never run git against the real repo or a real remote.
    cwd moves into tmp_path, git cannot walk up out of it, and any git call whose -C or cwd is outside tmp_path fails.
    The module snapshot directory is also allowed, because tests that only read the checkout share it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))
    real_popen = subprocess.Popen
    root = tmp_path.resolve()

    def inside(path):
        resolved = Path(path).resolve()
        if resolved == root or root in resolved.parents:
            return True
        return any(resolved == base or base in resolved.parents for base in _GIT_ROOTS)

    def guarded(cmd, *a, cwd=None, **kw):
        argv = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        if argv and Path(argv[0]).name == 'git':
            where = argv[2] if argv[1:2] == ['-C'] else (cwd or os.getcwd())
            assert inside(where), f'git would run outside tmp_path: {argv[:4]} cwd={cwd}'
        return real_popen(cmd, *a, cwd=cwd, **kw)
    monkeypatch.setattr(subprocess, 'Popen', guarded)


def test_guard_refuses_git_outside_tmp_path():
    with pytest.raises(AssertionError, match='outside tmp_path'):
        subprocess.run(['git', '-C', str(Path(lr.__file__).resolve().parents[1]), 'status'])
    with pytest.raises(AssertionError, match='outside tmp_path'):
        subprocess.run(['git', 'push'], cwd=str(Path(lr.__file__).resolve().parents[1]))

TRAIN = '/opt/homebrew/Python.app/Contents/MacOS/Python scripts/train.py demo --base origin/master b'


def cfg_for(tmp_path, procs=(), ran=None):
    return lr.Config(main=tmp_path, landing=tmp_path / 'landing', procs=lambda: list(procs), load=lambda: 3.0,
                     process=lambda cfg, entry: (ran if ran is not None else []).append(entry.name))


def test_parse_line_tokens():
    e = lr.parse_line('demo system PRIORITY=1 WHERE=local KEY=v br/a br/b')
    assert (e.name, e.kind, e.priority) == ('demo', 'system', 1)
    assert e.envs == ['PRIORITY=1', 'WHERE=local', 'KEY=v'] and e.branches == ['br/a', 'br/b']
    assert lr.parse_line('# comment') is None and lr.parse_line('   ') is None
    for bad in ('demo', 'demo other br', 'demo system PRIORITY=1', 'demo system PRIORITY=x br'):
        try:
            lr.parse_line(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_priority_order_then_file_order(tmp_path):
    q = tmp_path / 'queue.txt'
    q.write_text('# head\nc system br/c\nb system PRIORITY=2 br/b\na system PRIORITY=1 br/a\nbroken\nd system br/d\n')
    entries, bad = lr.read_queue(q)
    assert [e.name for e in entries] == ['a', 'b', 'c', 'd'] and bad == ['broken']


def test_single_instance_lock(tmp_path):
    first = lr.acquire_lock(tmp_path)
    assert first is not None
    assert lr.acquire_lock(tmp_path) is None
    first.close()
    assert lr.acquire_lock(tmp_path) is not None


@pytest.mark.parametrize('fails', [False, True])
def test_cycle_holds_train_lock_only_during_work(tmp_path, fails):
    cfg = cfg_for(tmp_path)
    cfg.queue.write_text('a system br/a\n')
    ran = []

    def process(cfg, entry):
        assert lr.acquire_lock(cfg.landing) is None
        ran.append(entry.name)
        if fails:
            raise RuntimeError('train failed')

    cfg.process = process
    assert lr.cycle(cfg) == 'ran:a'
    assert ran == ['a']
    with lr.acquire_lock(cfg.landing):
        assert lr.cycle(cfg) == 'idle'
    with lr.acquire_lock(cfg.landing):
        pass


def test_cycle_waits_once_for_shared_lock(tmp_path):
    ran = []
    cfg = cfg_for(tmp_path, ran=ran)
    line = 'a system br/a\n'
    cfg.queue.write_text(line)
    state = {}
    with lr.acquire_lock(cfg.landing):
        for _ in range(2):
            assert lr.cycle(cfg, state) == 'busy'
        assert ran == []
        assert cfg.queue.read_text() == line
        assert len((cfg.landing / 'queue.log').read_text().splitlines()) == 1
    assert lr.cycle(cfg, state) == 'ran:a'
    assert ran == ['a']


def test_main_has_separate_single_instance_guard(tmp_path, monkeypatch, capsys):
    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(lr, 'Config', lambda **kwargs: cfg)
    real_cycle = lr.cycle

    def cycle(cfg, state):
        # The idle daemon leaves the train lock available to Forge.
        with lr.acquire_lock(cfg.landing):
            assert lr.main(['--once']) == 0
            assert 'another landing runner holds the lock; exiting' in capsys.readouterr().out
            return real_cycle(cfg, state)

    monkeypatch.setattr(lr, 'cycle', cycle)
    assert lr.main(['--once']) == 0
    # A clean exit also releases the instance guard.
    assert lr.main(['--once']) == 0


def test_train_detection_ignores_shells_that_mention_it(tmp_path):
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', procs=lambda: [
        '/bin/zsh -c eval "for i in 1; do pgrep scripts/train.py; done"', 'python scripts/landing_runner.py'])
    assert not lr.train_running(cfg)
    cfg.procs = lambda: [TRAIN]
    assert lr.train_running(cfg)
    cfg.procs = lambda: ['sh /tmp/x/queue-runner5e.sh']
    assert lr.old_runner_alive(cfg)


def test_cycle_refuses_while_another_train_runs(tmp_path):
    ran = []
    cfg = cfg_for(tmp_path, procs=[TRAIN], ran=ran)
    cfg.queue.write_text('a system br/a\n')
    assert lr.cycle(cfg) == 'busy' and ran == []
    assert cfg.queue.read_text() == 'a system br/a\n'  # not consumed
    cfg.procs = lambda: []
    assert lr.cycle(cfg) == 'ran:a' and ran == ['a'] and cfg.queue.read_text() == ''
    assert lr.cycle(cfg) == 'idle'


def test_cycle_leaves_forge_lines_and_runs_legacy_work(tmp_path):
    ran = []
    cfg = cfg_for(tmp_path, ran=ran)
    forge = 'forge-owned system PRIORITY=0 br@' + 'a' * 40
    malformed = 'forge-incomplete'
    cfg.queue.write_text(f'{forge}\n{malformed}\nlegacy system br/legacy\n')
    retry = {forge: {'n': 2, 'after': 0}}
    lr.save_retry_state(cfg, retry)
    assert lr.cycle(cfg) == 'ran:legacy'
    assert ran == ['legacy']
    assert cfg.queue.read_text() == f'{forge}\n{malformed}\n'
    assert lr.retry_state(cfg) == retry
    assert 'forge-' not in (cfg.landing / 'queue-done.txt').read_text()
    for _ in range(2):
        assert lr.cycle(cfg) == 'idle'
    assert not (cfg.landing / 'queue.log').exists()
    assert not cfg.inbox.exists()


def test_forge_only_queue_does_not_raise_legacy_idle_alarm(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, procs=[TRAIN])
    cfg.queue.write_text('forge-owned system br/forge\n')
    monkeypatch.setattr(lr.time, 'time', lambda: 1000)
    assert lr.cycle(cfg, {'idle_since': 1}) == 'idle'
    assert not cfg.inbox.exists()


def test_cycle_waits_for_legacy_runner_and_continues_after_a_failure(tmp_path):
    ran = []
    cfg = cfg_for(tmp_path, procs=['sh /x/queue-runner5e.sh'], ran=ran)
    cfg.queue.write_text('a system PRIORITY=1 br/a\nb system br/b\n')
    assert lr.cycle(cfg) == 'old-runner' and ran == []
    cfg.procs = lambda: []

    def boom(cfg, entry):
        if entry.name == 'a':
            raise RuntimeError('refused')
        ran.append(entry.name)
    cfg.process = boom
    assert lr.cycle(cfg) == 'ran:a' and lr.cycle(cfg) == 'ran:b' and ran == ['b']
    assert 'a ERROR' in (cfg.landing / 'queue.log').read_text()


def test_placement_rules(tmp_path):
    cfg = cfg_for(tmp_path)
    e = lr.parse_line('x system PRIORITY=2 br')
    assert lr.placement(cfg, e) == (['--priority', '2', '--where', 'local'], [], 'local')
    assert lr.placement(cfg, lr.parse_line('x web br'))[2] == 'm1'
    assert lr.placement(cfg, lr.parse_line('x web WHERE=local br'))[2] == 'local'
    cfg.load = lambda: 13.0
    assert lr.placement(cfg, e)[2] == 'm1'


def test_launchd_template_is_a_keepalive_job():
    import plistlib
    plist = plistlib.loads((Path(__file__).resolve().parents[1] / 'deploy/com.kyra.landing-runner.plist').read_bytes())
    assert plist['Label'] == 'com.kyra.landing-runner' and plist['KeepAlive'] is True
    assert plist['ProgramArguments'][-1] == '__KYRA_ROOT__/scripts/landing_runner.py'


FAKE_TRAIN = '''import json, os, sys
with open(os.environ["LR_PROBE_OUT"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(0 if "--precheck-only" in sys.argv else 1)
'''


def sh(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def _git(env, main, *args):
    subprocess.run(['git', '-C', str(main), *args], check=True, capture_output=True, text=True, env=env)


def _rewrite_origin(main, origin):
    """Point a copied checkout at its copied bare origin without another git process."""
    config = main / '.git' / 'config'
    url = origin.resolve().as_posix()
    lines = config.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith('url ='):
            indent = line[:len(line) - len(line.lstrip())]
            ending = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
            lines[i] = f'{indent}url = {url}{ending}'
            config.write_text(''.join(lines))
            return
    raise AssertionError('copied git config has no origin url')


def _build_pair(base, texts):
    """One bare origin and a checkout: base, then feat commits. Returns the two feat shas."""
    base.mkdir(parents=True)
    origin, main = base / 'origin.git', base / 'main'
    env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(base.parent)}
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master', str(origin)], check=True, env=env)
    subprocess.run(['git', 'clone', '-q', str(origin), str(main)], check=True, capture_output=True, env=env)
    _git(env, main, 'config', 'user.name', 'Probe')
    _git(env, main, 'config', 'user.email', 'probe@example.com')
    (main / 'f.txt').write_text('base\n')
    _git(env, main, 'add', 'f.txt')
    _git(env, main, 'commit', '-qm', 'base')
    _git(env, main, 'push', '-q', 'origin', 'HEAD:master')
    _git(env, main, 'checkout', '-qb', 'feat')
    shas = []
    for text in texts:
        (main / 'f.txt').write_text(text)
        _git(env, main, 'commit', '-qam', text[0])
        shas.append(_git_out(env, main, 'rev-parse', 'HEAD'))
    _git(env, main, 'checkout', '-q', 'master')
    (main / 'scripts').mkdir()
    return tuple(shas)


def _git_out(env, main, *args):
    done = subprocess.run(['git', '-C', str(main), *args], check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _apply_stale(main):
    """Move origin/master forward and leave the main checkout one landing behind."""
    env = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(main.parent)}
    (main / 'g.txt').write_text('other\n')
    _git(env, main, 'add', 'g.txt')
    _git(env, main, 'commit', '-qm', 'other landing')
    _git(env, main, 'push', '-q', 'origin', 'HEAD:master')
    _git(env, main, 'reset', '-q', '--hard', 'HEAD~1')


def _copy_snapshot(snapshot, tmp_path):
    origin = tmp_path / 'origin.git'
    main = tmp_path / 'main'
    shutil.copytree(snapshot / 'origin.git', origin)
    shutil.copytree(snapshot / 'main', main)
    _rewrite_origin(main, origin)
    return main


def _runner_unknown(cfg):
    # These tests assert pins, retries and landings. The ssh probe's connect timeout is wall clock.
    return 'unknown'


def _bind_checkout(tmp_path, monkeypatch, main, train_text):
    if train_text is not None:
        scripts = main / 'scripts'
        scripts.mkdir(exist_ok=True)
        (scripts / 'train.py').write_text(train_text)
    monkeypatch.setattr(lr, 'runner_state', _runner_unknown)
    out = tmp_path / 'argv.jsonl'
    monkeypatch.setenv('LR_PROBE_OUT', str(out))
    cfg = lr.Config(main=main, landing=tmp_path / 'landing', procs=lambda: [], load=lambda: 20.0)
    return cfg, main, out


@pytest.fixture(scope='module', autouse=True)
def _landing_git_snapshots(tmp_path_factory):
    """Build the temp origin once per worker. Tests that only read it share the checkout."""
    root = tmp_path_factory.mktemp('landing-git')
    resolved = root.resolve()
    repo_root = Path(__file__).resolve().parents[1]
    if resolved == repo_root or repo_root in resolved.parents or resolved in repo_root.parents:
        raise AssertionError(f'landing snapshot must stay outside the real repo: {resolved}')
    _GIT_ROOTS.append(resolved)
    reviewed = root / 'reviewed'
    _SNAPSHOTS['reviewed'] = reviewed
    _SHAS['reviewed'] = _build_pair(reviewed, ('A reviewed\n', 'B never reviewed\n'))
    landing = root / 'landing'
    _SNAPSHOTS['landing'] = landing
    _SHAS['landing'] = _build_pair(landing, ('A\n', 'B\n'))
    stale = root / 'landing-stale'
    stale.mkdir()
    shutil.copytree(landing / 'origin.git', stale / 'origin.git')
    shutil.copytree(landing / 'main', stale / 'main')
    _rewrite_origin(stale / 'main', stale / 'origin.git')
    _apply_stale(stale / 'main')
    _SNAPSHOTS['landing-stale'] = stale
    yield
    _SNAPSHOTS.clear()
    _SHAS.clear()
    _GIT_ROOTS.clear()


def _open_shared(kind, tmp_path, monkeypatch):
    main = _SNAPSHOTS[kind] / 'main'
    cfg, main, out = _bind_checkout(tmp_path, monkeypatch, main, None)
    a, b = _SHAS[kind]
    return cfg, main, a, b, out


def repo(tmp_path, monkeypatch):
    """Copy of the shared origin. feat has A (reviewed) then B (pushed after the review)."""
    main = _copy_snapshot(_SNAPSHOTS['reviewed'], tmp_path)
    cfg, main, out = _bind_checkout(tmp_path, monkeypatch, main, FAKE_TRAIN)
    a, b = _SHAS['reviewed']
    return cfg, main, a, b, out


def train_calls(out):
    return [json.loads(r) for r in out.read_text().splitlines()] if out.exists() else []


def test_unpinned_line_never_reaches_train(tmp_path, monkeypatch):
    """Judge 1: any agent that appends 'x system feat' lands whatever feat holds now (B, never reviewed)."""
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    cfg.queue.write_text('x system feat\n')
    lr.cycle(cfg)
    calls = train_calls(out)
    rode = [sh(main, 'show', f'{arg}:f.txt') for arg in (calls[-1] if calls else []) if arg == 'feat']
    assert calls == [], f'an unpinned queue line reached train.py, riding {rode}'
    assert 'ALERT' in (cfg.landing / 'queue.log').read_text()


def test_branch_moved_after_review_is_refused(tmp_path, monkeypatch):
    """Judge 1: line pins the reviewed sha A; the branch head is B -> refuse with an ALERT, no train."""
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    cfg.queue.write_text(f'x system feat@{a}\n')
    lr.cycle(cfg)
    assert train_calls(out) == []
    assert 'ALERT' in (cfg.landing / 'queue.log').read_text()


def test_pinned_line_with_matching_head_runs(tmp_path, monkeypatch):
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    cfg.queue.write_text(f'x system feat@{b}\n')
    lr.cycle(cfg)
    calls = train_calls(out)
    assert calls, 'a correctly pinned line did not run'
    rode = [arg for arg in calls[-1] if not arg.startswith('-') and arg not in ('x', 'origin/master')]
    assert all(sh(main, 'merge-base', '--is-ancestor', b, r) == '' for r in rode)


def test_queue_env_tokens_cannot_weaken_the_gate(tmp_path):
    """KEY=VALUE tokens reach train.py and the gate's pytest (deploy_local runs pytest with the inherited env)."""
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', procs=lambda: [], load=lambda: 20.0)
    for token in ('PYTEST_ADDOPTS=--co', 'KYRA_LAND_NO_BROWSER=1', 'PYTHONPATH=/tmp/x'):
        _, envs, _ = lr.placement(cfg, lr.parse_line(f'x system {token} br'))
        assert envs == [], f'{token} would reach the merge gate'


def test_full_landing_pass_never_pushes_or_force_merges(tmp_path, monkeypatch):
    """Judge 2: every git call the runner itself makes in a full landing pass; no push, no force, no non-ff merge."""
    calls = []

    def fake_git(cfg, *args, check=False):
        calls.append(args)
        out = ('a' * 40 + '\n') if args[0] in ('rev-parse', 'commit-tree', 'merge-tree') else ''
        rc = 0 if args[0] != 'merge-base' else 1
        if args[:2] == ('merge-base', '--is-ancestor') and args[2] == 'deadbeef':
            rc = 0
        return subprocess.CompletedProcess(args, rc, out, '')

    def fake_run(cmd, **kw):
        if kw.get('stdout') is not None and hasattr(kw['stdout'], 'write'):
            kw['stdout'].write('landed aaaa..deadbeef on origin/master\n')
        calls.append(tuple(cmd))
        return subprocess.CompletedProcess(cmd, 0, 'free\n', '')

    monkeypatch.setattr(lr, 'git', fake_git)
    monkeypatch.setattr(lr.subprocess, 'run', fake_run)
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', procs=lambda: [], load=lambda: 20.0)
    lr.process_entry(cfg, lr.parse_line('x system br@' + 'a' * 40))
    assert 'LANDED' in (cfg.landing / 'queue.log').read_text()
    flat = [' '.join(c).replace('git -C ' + str(tmp_path) + ' ', '') for c in calls]
    assert not [c for c in flat if c.startswith('push') or '--force' in c or '--no-verify' in c], flat
    assert all('--ff-only' in c for c in flat if c.startswith('merge ')), flat


def test_idle_alarm_when_work_waits(tmp_path, monkeypatch):
    """Judge 4 parity: v5e writes an ALERT after 300 s idle with work queued; a stuck 'busy' must not be silent."""
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', load=lambda: 3.0,
                    procs=lambda: ['/opt/x/Python scripts/train.py stuck --base origin/master b'])
    cfg.queue.write_text('a system br/a\n')
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    state = {}
    for _ in range(20):
        lr.cycle(cfg, state)
        now[0] += 20
    assert 'ALERT' in (cfg.landing / 'queue.log').read_text()
    assert 'idle' in (tmp_path / 'data/team/board-inbox.jsonl').read_text()


def test_bad_bytes_in_queue_do_not_crash_the_loop(tmp_path):
    """Judge 5: a torn or non-UTF-8 queue file is skipped loudly, not a launchd crash loop."""
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', procs=lambda: [], load=lambda: 3.0,
                    process=lambda c, e: None)
    cfg.queue.write_bytes(b'a system br/a\n\xff\xfe torn\n')
    lr.cycle(cfg)


def test_train_name_cannot_escape_the_landing_folder():
    for bad in ('../../x system br', '--resume=other system br'):
        try:
            lr.parse_line(bad)
        except ValueError:
            continue
        raise AssertionError(f'accepted train name in: {bad}')


def test_runner_source_has_no_push_call():
    src = (Path(lr.__file__)).read_text()
    assert not re.search(r"""['"]push['"]|git push""", src.split('"""', 2)[2])


LANDING_TRAIN = '''import json, os, subprocess, sys
with open(os.environ["LR_PROBE_OUT"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "--precheck-only" in sys.argv:
    sys.exit(0)
refs = [a for a in sys.argv[2:] if not a.startswith("-") and a != "origin/master"]
g = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
old = g("rev-parse", "origin/master")
g("push", "-q", "origin", refs[-1] + ":refs/heads/master")
new = g("rev-parse", refs[-1])
print(f"landed {old[:9]}..{new[:9]} on origin/master")
'''


def repo2(tmp_path, monkeypatch, stale=False):
    """Copy of the shared origin; feat = base + A + B. stale=True uses the snapshot whose origin/master moved on."""
    key = 'landing-stale' if stale else 'landing'
    main = _copy_snapshot(_SNAPSHOTS[key], tmp_path)
    cfg, main, out = _bind_checkout(tmp_path, monkeypatch, main, LANDING_TRAIN)
    a, b = _SHAS['landing']
    return cfg, main, a, b, out


def calls(out):
    return [json.loads(r) for r in out.read_text().splitlines()] if out.exists() else []


def rode_refs(call):
    return [a for a in call if not a.startswith('-') and a not in ('x', 'origin/master')]


def test_short_or_near_miss_pin_never_reaches_train(tmp_path, monkeypatch):
    cfg, main, a, b, out = repo2(tmp_path, monkeypatch)
    near = b[:-1] + ('0' if b[-1] != '0' else '1')
    for pin in (b[:12], near, b.upper()):
        cfg.queue.write_text(f'x system feat@{pin}\n')
        lr.cycle(cfg)
        assert calls(out) == [], f'pin {pin} reached train.py'
    assert (cfg.landing / 'queue.log').read_text().count('ALERT') == 3


def test_push_between_check_and_start_does_not_ride(tmp_path, monkeypatch):
    """TOCTOU: the branch moves right after the pin check; the train must still ride the pinned sha."""
    cfg, main, a, b, out = repo2(tmp_path, monkeypatch)
    check = lr.check_pins

    def check_then_push(cfg, entry):
        pins = check(cfg, entry)
        sh(main, 'branch', '-f', 'feat', a)  # someone moves feat after the check
        return pins
    monkeypatch.setattr(lr, 'check_pins', check_then_push)
    cfg.queue.write_text(f'x system feat@{b}\n')
    lr.cycle(cfg)
    ridden = rode_refs(calls(out)[-1])
    assert ridden and 'feat' not in ridden
    assert all(sh(main, 'show', f'{r}:f.txt') == 'B' for r in ridden)


def test_stale_branch_boards_the_pinned_sha_with_master(tmp_path, monkeypatch):
    cfg, main, a, b, out = repo2(tmp_path, monkeypatch, stale=True)
    cfg.queue.write_text(f'x system feat@{b}\n')
    lr.cycle(cfg)
    ridden = rode_refs(calls(out)[0])
    om = sh(main, 'rev-parse', 'origin/master')
    assert ridden and all(sh(main, 'merge-base', '--is-ancestor', om, r) == '' for r in ridden)


def test_landing_fast_forwards_the_main_checkout(tmp_path, monkeypatch):
    cfg, main, a, b, out = repo2(tmp_path, monkeypatch)
    cfg.queue.write_text(f'x system feat@{b}\n')
    lr.cycle(cfg)
    assert 'x LANDED' in (cfg.landing / 'queue.log').read_text()
    assert sh(main, 'rev-parse', 'HEAD') == sh(main, 'rev-parse', 'origin/master') == b


def test_pin_already_on_master_is_skipped(tmp_path, monkeypatch):
    cfg, main, a, b, out = repo2(tmp_path, monkeypatch)
    base = sh(main, 'rev-parse', 'master')
    sh(main, 'branch', 'old', base)
    cfg.queue.write_text(f'x system old@{base}\n')
    lr.cycle(cfg)
    assert calls(out) == [] and 'already on origin/master' in (cfg.landing / 'queue.log').read_text()


def test_idle_alert_is_one_inbox_row_per_stretch(tmp_path, monkeypatch):
    cfg = lr.Config(main=tmp_path, landing=tmp_path / 'landing', load=lambda: 3.0,
                    procs=lambda: ['/opt/x/Python scripts/train.py stuck --base origin/master b'])
    cfg.queue.write_text('a system br@' + 'a' * 40 + '\n')
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    state = {}
    for _ in range(40):
        lr.cycle(cfg, state)
        now[0] += 20
    rows = (tmp_path / 'data/team/board-inbox.jsonl').read_text().splitlines()
    assert len(rows) == 1 and (cfg.landing / 'queue.log').read_text().count('ALERT') == 1


def test_refused_second_instance_keeps_the_holders_pid(tmp_path):
    first = lr.acquire_lock(tmp_path)
    assert lr.acquire_lock(tmp_path) is None
    assert (tmp_path / 'runner.lock').read_text() == f'{os.getpid()}\n'
    first.close()


REFUSING_TRAIN = '''import os, sys
if "--precheck-only" in sys.argv:
    print(os.environ["LR_REFUSAL"])
    print("refused: merged-tree guards failed; see failure output and suspect history")
    sys.exit(3)
sys.exit(1)
'''
LOAD_FAIL = ('FAILED tests/test_correct_repeaters.py::test_load_allows_the_browser_phase - '
             'AssertionError: 1-minute load is 14.2')
SUITE_FAIL = 'FAILED tests/test_memory.py::test_recall - AssertionError: assert 1 == 2'


def refusing(tmp_path, monkeypatch, refusal):
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    (main / 'scripts/train.py').write_text(REFUSING_TRAIN)
    monkeypatch.setenv('LR_REFUSAL', refusal)
    line = f'x system feat@{b}'
    cfg.queue.write_text(line + '\n')
    return cfg, line


def test_load_refusal_keeps_the_line_with_a_delay(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, LOAD_FAIL)
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == now[0] + 300


def test_real_test_failure_waits_for_master_diagnosis(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, SUITE_FAIL)
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert (cfg.landing / 'x-master-pending.json').exists()
    assert not (cfg.landing / 'queue-retry.json').exists()


@pytest.mark.parametrize('refuse', [refusing, lambda *args: refusing_in_the_train(*args)])
def test_red_batch_splits_before_retaining_single_lines(tmp_path, monkeypatch, refuse):
    cfg, line = refuse(tmp_path, monkeypatch, SUITE_FAIL)
    other = line.replace('x system', 'y system')
    cfg.queue.write_text(line + '\n' + other + '\n')
    monkeypatch.setattr(lr, 'needs_full', lambda *args: False)
    assert lr.cycle(cfg) == 'ran:x'
    assert (cfg.landing / 'x-master-pending.json').exists()
    assert (cfg.landing / 'y-master-pending.json').exists()
    assert not list(cfg.landing.glob('batch-*-master-pending.json'))
    assert 'red batch; splitting' in (cfg.landing / 'queue.log').read_text()
    assert lr.cycle(cfg) == 'delayed'


@pytest.mark.parametrize('legacy', [False, True])
def test_same_red_batch_does_not_rerun_until_inputs_change(tmp_path, monkeypatch, legacy):
    import hashlib
    cfg = cfg_for(tmp_path)
    base = ['a' * 40]
    monkeypatch.setattr(lr, 'git', lambda *args: subprocess.CompletedProcess(args, 0, base[0], ''))
    entries = [lr.parse_line(f'{name} system branch@' + 'b' * 40) for name in ('x', 'y')]
    batches = []

    def process(cfg, entry):
        if entry.batch:
            batches.append(entry.line)
            return 'failed'
        return 'landed'

    cfg.process = process
    if legacy:
        identity = '\n'.join(entry.line for entry in entries)
        name = 'batch-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        marker = cfg.landing / (name + '-master-pending.json')
        marker.write_text(json.dumps({'line': identity, 'base': base[0]}))
        cfg.queue.write_text(identity + '\n')
        lr.reconcile_pending(cfg)
        assert not marker.exists()
    for _ in range(2):
        assert [result for _, result in lr.process_batch(cfg, entries)] == ['landed', 'landed']
    expected = 0 if legacy else 1
    assert len(batches) == expected
    base[0] = 'c' * 40
    lr.process_batch(cfg, entries)
    assert len(batches) == expected + 1
    entries[1] = lr.parse_line('y system branch@' + 'd' * 40)
    lr.process_batch(cfg, entries)
    assert len(batches) == expected + 2


def test_stale_pending_files_cleaned_on_next_cycle(tmp_path):
    ran = []
    cfg = cfg_for(tmp_path, ran=ran)
    stale = cfg.landing / 'gone-master-pending.json'
    stale.write_text(json.dumps({'line': 'gone system old'}))
    assert lr.cycle(cfg) == 'idle'
    assert not stale.exists()
    changed = cfg.landing / 'changed-master-pending.json'
    changed.write_text(json.dumps({'line': 'changed system old'}))
    cfg.queue.write_text('changed system new\n')
    assert lr.cycle(cfg) == 'ran:changed'
    assert ran == ['changed'] and not changed.exists()


def test_retry_delay_is_honored_and_backs_off(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, 'precheck: headroom refused (exit 75)')
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    lr.cycle(cfg)
    now[0] += 299
    assert lr.cycle(cfg) == 'delayed'
    assert (cfg.landing / 'queue.log').read_text().count('precheck starting') == 1
    now[0] += 2
    lr.cycle(cfg)
    assert (cfg.landing / 'queue.log').read_text().count('precheck starting') == 2
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == now[0] + 600


def test_third_retry_writes_one_inbox_row(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, LOAD_FAIL)
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    inbox = tmp_path / 'main/data/team/board-inbox.jsonl'
    for i in range(5):
        lr.cycle(cfg)
        if i < 2:
            assert not inbox.exists()
        now[0] += 3600
    assert len(inbox.read_text().splitlines()) == 1
    assert cfg.queue.read_text().strip() == line


def test_unreachable_runner_logs_the_first_ssh_line(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    out = type('R', (), {'stdout': 'unreachable\nssh exit 255: No route to host\n', 'stderr': ''})()
    monkeypatch.setattr(lr.subprocess, 'run', lambda *a, **k: out)
    assert lr.runner_state(cfg) == 'unreachable'
    assert 'runner unreachable: ssh exit 255: No route to host' in (cfg.landing / 'queue.log').read_text()


REFUSING_RUN = '''import json, os, sys
with open(os.environ["LR_PROBE_OUT"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if "--precheck-only" in sys.argv:
    sys.exit(0)
print(os.environ["LR_REFUSAL"])
sys.exit(1)
'''


def refusing_in_the_train(tmp_path, monkeypatch, refusal):
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    (main / 'scripts/train.py').write_text(REFUSING_RUN)
    monkeypatch.setenv('LR_REFUSAL', refusal)
    line = f'x system feat@{b}'
    cfg.queue.write_text(line + '\n')
    return cfg, line


def test_train_load_refusal_after_precheck_keeps_the_line(tmp_path, monkeypatch):
    """2026-10-17 04:32: '--where local refused: 1-minute load 12.5 is 12 or more' came from the train itself and the
    runner dropped the line, because only precheck refusals were retried."""
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch,
                                      '--where local refused: 1-minute load 12.5 is 12 or more')
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == now[0] + 300


def test_train_real_refusal_after_precheck_still_drops_the_line(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, 'refused: merged-tree tests failed: 2 failed')
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == ''


def test_transient_refusal_with_an_existing_train_worktree_resumes_it(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, '--where local refused: 1-minute load 12.5 is 12 or more')
    sh(cfg.main, 'worktree', 'add', '-q', str(cfg.main / '.claude/worktrees/x'), line.split('@')[1])
    (cfg.main / 'data/train-logs').mkdir(parents=True)
    (cfg.main / 'data/train-logs/x.state.json').write_text(json.dumps({'branches': [line.split('@')[1]]}))
    lr.cycle(cfg)
    argvs = [json.loads(row) for row in (tmp_path / 'argv.jsonl').read_text().splitlines()]
    assert argvs and all(a[:2] == ['--resume', 'x'] for a in argvs), argvs  # precheck and run, never a fresh launch
    assert any('--precheck-only' in a for a in argvs) and any('--precheck-only' not in a for a in argvs)


def test_existing_train_worktree_of_another_train_is_not_resumed(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, '--where local refused: 1-minute load 12.5 is 12 or more')
    sh(cfg.main, 'worktree', 'add', '-q', '--detach', str(cfg.main / '.claude/worktrees/x'), 'master')
    state = cfg.main / 'data/train-logs'
    state.mkdir(parents=True)
    (state / 'x.state.json').write_text(json.dumps({'branches': [sh(cfg.main, 'rev-parse', 'master')]}))  # not from the pin
    lr.cycle(cfg)
    assert not (tmp_path / 'argv.jsonl').exists()
    assert cfg.queue.read_text().strip() == ''
    assert 'stale train' in (cfg.landing / 'queue.log').read_text()


def test_a_preflight_refused_train_worktree_at_base_is_resumed(tmp_path, monkeypatch):
    """create() makes the worktree and runs preflight before any merge: HEAD is at the base and the state rides the pin."""
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, '--where local refused: 1-minute load 12.5 is 12 or more')
    sh(cfg.main, 'worktree', 'add', '-q', '--detach', str(cfg.main / '.claude/worktrees/x'), 'master')
    state = cfg.main / 'data/train-logs'
    state.mkdir(parents=True)
    (state / 'x.state.json').write_text(json.dumps({'branches': [line.split('@')[1]]}))
    lr.cycle(cfg)
    assert json.loads((tmp_path / 'argv.jsonl').read_text().splitlines()[0])[:2] == ['--resume', 'x']
    assert cfg.queue.read_text().strip() == line


def test_a_pin_missing_from_the_saved_train_is_stale_and_no_state_is_stale(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, '--where local refused: 1-minute load 12.5 is 12 or more')
    b = line.split('@')[1]
    sh(cfg.main, 'checkout', '-q', '-b', 'feat2', 'master')
    (cfg.main / 'g.txt').write_text('second\n')
    sh(cfg.main, 'add', 'g.txt')
    sh(cfg.main, 'commit', '-qm', 'second branch')
    c = sh(cfg.main, 'rev-parse', 'HEAD')
    sh(cfg.main, 'checkout', '-q', 'master')
    sh(cfg.main, 'worktree', 'add', '-q', '--detach', str(cfg.main / '.claude/worktrees/x'), b)
    cfg.queue.write_text(f'x system feat@{b} feat2@{c}\n')
    assert lr.stale_train(cfg, 'x', [('feat', b), ('feat2', c)], [b, c])  # no state file at all
    state = cfg.main / 'data/train-logs'
    state.mkdir(parents=True)
    (state / 'x.state.json').write_text(json.dumps({'branches': [b]}))  # feat2 was never in this train
    assert lr.stale_train(cfg, 'x', [('feat', b), ('feat2', c)], [b, c])
    assert not lr.stale_train(cfg, 'x', [('feat', b), ('feat2', c)], [b])  # feat2 left out at boarding: fine


def test_seventh_transient_refusal_drops_the_line_with_one_inbox_row(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, LOAD_FAIL)
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    inbox = tmp_path / 'main/data/team/board-inbox.jsonl'
    for i in range(6):
        lr.cycle(cfg)
        assert cfg.queue.read_text().strip() == line, i
        now[0] += 3600
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == ''
    assert not (cfg.landing / 'queue-retry.json').exists() or line not in json.loads((cfg.landing / 'queue-retry.json').read_text())
    rows = [json.loads(row) for row in inbox.read_text().splitlines()]
    assert len([r for r in rows if 'gave up' in r['text']]) == 1


def test_real_suite_failure_in_the_train_is_never_retried(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, 'refused: merged-tree tests failed: 2 failed')
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == ''
    assert not (cfg.landing / 'queue-retry.json').exists()


LOAD_ONLY = ('FAILED tests/test_correct_repeaters.py::test_load_allows_the_browser_phase - AssertionError: '
             '1-minute load is 13.1 (--where local limit: under 12.0)')
GUARDS_FAILED = 'refused: merged-tree guards failed; see failure output and suspect history'


def test_train_guard_failure_with_only_the_load_guard_keeps_the_line(tmp_path, monkeypatch):
    """Lane 1: the load text sat only in the guard failure output, so the line was dropped."""
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, LOAD_ONLY + '\n' + GUARDS_FAILED)
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == now[0] + 300


def test_train_guard_failure_load_guard_plus_another_is_real(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, LOAD_ONLY + '\n' + SUITE_FAIL + '\n' + GUARDS_FAILED)
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert (cfg.landing / 'x-master-pending.json').exists()
    assert not (cfg.landing / 'queue-retry.json').exists()


def test_train_guard_failure_of_another_guard_alone_is_real(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch, SUITE_FAIL + '\n' + GUARDS_FAILED)
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert (cfg.landing / 'x-master-pending.json').exists()


def test_precheck_guard_failure_load_guard_plus_another_is_real(tmp_path, monkeypatch):
    cfg, line = refusing(tmp_path, monkeypatch, LOAD_ONLY + '\n' + SUITE_FAIL)
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line
    assert (cfg.landing / 'x-master-pending.json').exists()


MOVING_TRAIN = '''import json, os, subprocess, sys
from pathlib import Path
main, other = Path(os.environ["LR_MAIN"]), Path(os.environ["LR_OTHER"])
def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout.strip()
args, wt = sys.argv[1:], main / ".claude/worktrees/x"
with open(os.environ["LR_PROBE_OUT"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if "--precheck-only" in args:
    sys.exit(0)
if "--resume" not in args:
    pin = args[-1]
    g(main, "worktree", "add", "-q", "--detach", str(wt), pin)
    (main / "data/train-logs").mkdir(parents=True, exist_ok=True)
    (main / "data/train-logs/x.state.json").write_text(json.dumps({"branches": [pin]}))
    g(other, "pull", "-q", "origin", "master")  # the owner pushes to master while the suite runs
    (other / "g.txt").write_text("the owner\\n")
    g(other, "add", "g.txt"); g(other, "commit", "-qm", "the owner push"); g(other, "push", "-q", "origin", "HEAD:master")
    print("refused: origin/master moved while the suite ran, so run the landing again")
    sys.exit(1)
g(main, "fetch", "-q", "origin", "master")
before = g(main, "rev-parse", "origin/master")
if subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", "origin/master", "HEAD"]).returncode:
    g(wt, "-c", "user.name=P", "-c", "user.email=p@example.com", "merge", "--no-edit", "origin/master")
assert (wt / "g.txt").exists(), "resume did not re-merge the moved origin/master"
Path(os.environ["LR_CHECKS"]).write_text("re-ran checks on " + g(wt, "rev-parse", "HEAD"))
g(wt, "push", "-q", "origin", "HEAD:master")
print(f"landed {before[:8]}..{g(wt, 'rev-parse', 'HEAD')} on origin/master")
'''


def moving_origin(tmp_path, monkeypatch):
    """Temp bare origin (never the real one): the fake train's suite phase pushes to it, as the owner push did."""
    cfg, main, a, b, out = repo(tmp_path, monkeypatch)
    other = tmp_path / 'other'
    shutil.copytree(main, other)
    for k, v in (('user.name', 'the owner'), ('user.email', 'the owner@example.com')):
        sh(other, 'config', k, v)
    (main / 'scripts/train.py').write_text(MOVING_TRAIN)
    monkeypatch.setenv('LR_MAIN', str(main))
    monkeypatch.setenv('LR_OTHER', str(other))
    monkeypatch.setenv('LR_CHECKS', str(tmp_path / 'checks.txt'))
    line = f'x system feat@{b}'
    cfg.queue.write_text(line + '\n')
    return cfg, line, b, tmp_path / 'checks.txt'


def test_origin_moved_while_the_suite_ran_keeps_the_line_resumes_remerges_and_lands(tmp_path, monkeypatch):
    """2026-10-17 20:51 workernode2: the owner pushed to master mid-run; the line was dropped and lane 1 requeued by hand."""
    cfg, line, b, checks = moving_origin(tmp_path, monkeypatch)
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == line and not checks.exists()
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == now[0] + 300
    assert sh(tmp_path / 'origin.git', 'log', '-1', '--format=%s', 'master') == 'the owner push'  # nothing of ours pushed
    now[0] += 301
    lr.cycle(cfg)
    runs = [c for c in train_calls(tmp_path / 'argv.jsonl') if '--precheck-only' not in c]
    assert runs[-1][:2] == ['--resume', 'x']  # resumed, not a fresh launch
    assert checks.read_text().startswith('re-ran checks on ')
    assert lr.on_master(cfg, b) and 'LANDED' in (cfg.landing / 'queue.log').read_text()
    assert cfg.queue.read_text().strip() == ''


def test_origin_moved_refusal_is_capped_like_other_transients(tmp_path, monkeypatch):
    cfg, line = refusing_in_the_train(tmp_path, monkeypatch,
                                      'refused: origin/master moved while the suite ran, so run the landing again')
    now = [1_000_000.0]
    monkeypatch.setattr(lr.time, 'time', lambda: now[0])
    for i in range(lr.RETRY_CAP):
        lr.cycle(cfg)
        assert cfg.queue.read_text().strip() == line, i
        now[0] += 3600
    lr.cycle(cfg)
    assert cfg.queue.read_text().strip() == ''


def qc_repo(tmp_path, monkeypatch, isolated=False):
    """Read-only QC checks share the snapshot. A test that writes into the checkout passes isolated=True."""
    if isolated:
        cfg, main, reviewed, head, out = repo(tmp_path, monkeypatch)
    else:
        cfg, main, reviewed, head, out = _open_shared('reviewed', tmp_path, monkeypatch)
    return cfg, main, reviewed, head, out, f'x system feat@{head}'


def test_qc_requeue_restores_one_line_and_keeps_retry_count(tmp_path, monkeypatch):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch)
    other = f'y system feat@{head}'
    cfg.queue.write_text(other + '\n')
    (cfg.landing / 'queue-retry.json').write_text(json.dumps({
        line: {'n': 2, 'after': 50}, other: {'n': 1, 'after': 9_999_999_999}}))
    failure = 'refused: 1-minute load is 14.2\n'
    outcome = lr.qc_requeue(cfg, line, failure, 'inc1', expected_name='x')
    assert outcome['outcome'] == 'applied' and outcome['train_launched'] is False
    assert outcome['before']['retry'] == {'n': 2, 'after': 50}
    assert outcome['after']['retry'] == {'n': 2, 'after': 0}
    assert cfg.queue.read_text().splitlines() == [other, line]
    state = json.loads((cfg.landing / 'queue-retry.json').read_text())
    assert state[other] == {'n': 1, 'after': 9_999_999_999}
    assert train_calls(out) == []
    assert lr.qc_requeue(cfg, line, failure, 'inc1', expected_name='x')['outcome'] == 'already_satisfied'
    assert cfg.queue.read_text().splitlines() == [other, line]
    rolled = lr.qc_rollback_requeue(cfg, 'inc1')
    assert rolled['outcome'] == 'applied'
    assert cfg.queue.read_text().splitlines() == [other]
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line] == {'n': 2, 'after': 50}


def test_qc_requeue_refuses_mixed_failure_hold_and_changed_identity(tmp_path, monkeypatch):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch)
    mixed = LOAD_ONLY + '\n' + SUITE_FAIL + '\n' + GUARDS_FAILED
    refused = lr.qc_requeue(cfg, line, mixed, 'incmix', expected_name='x')
    assert refused['outcome'] == 'blocked' and refused['reason'] == 'not_transient'
    (cfg.landing / 'queue-hold').write_text('protected push\n')
    held = lr.qc_requeue(cfg, line, 'refused: 1-minute load is 14.2\n', 'inchold', expected_name='x')
    assert held['reason'] == 'hold'
    (cfg.landing / 'queue-hold').unlink()
    moved = lr.qc_requeue(cfg, f'x system feat@{reviewed}', 'refused: runner held\n', 'incpin', expected_name='x')
    assert moved['reason'] == 'changed_pin'
    renamed = lr.qc_requeue(cfg, line, 'refused: no headroom\n', 'incname', expected_name='other')
    assert renamed['reason'] == 'changed_identity'
    assert not cfg.queue.exists() or cfg.queue.read_text().strip() == ''
    assert train_calls(out) == []


def test_qc_requeue_preserves_a_pending_retry_and_an_exhausted_budget(tmp_path, monkeypatch):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch)
    cfg.queue.write_text(line + '\n')
    (cfg.landing / 'queue-retry.json').write_text(json.dumps({line: {'n': 2, 'after': 9_999_999_999}}))
    pending = lr.qc_requeue(cfg, line, 'refused: another heavy job\n', 'incpend', expected_name='x')
    assert pending['outcome'] == 'already_satisfied' and pending['reason'] == 'retry_scheduled'
    assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['after'] == 9_999_999_999
    cfg.queue.write_text('')
    (cfg.landing / 'queue-retry.json').write_text(json.dumps({line: {'n': lr.RETRY_CAP + 1, 'after': 0}}))
    capped = lr.qc_requeue(cfg, line, 'refused: another heavy job\n', 'inccap', expected_name='x')
    assert capped['reason'] == 'budget_exhausted'
    assert cfg.queue.read_text().strip() == ''
    (cfg.landing / 'queue-retry.json').unlink()
    (cfg.landing / 'queue.log').write_text(
        '12:00:00 ALERT: x refused 7 times for a busy Mac; gave up and dropped the line (requeue it when the Mac is quiet)\n')
    gave_up = lr.qc_requeue(cfg, line, 'refused: another heavy job\n', 'incgave', expected_name='x')
    assert gave_up['reason'] == 'budget_exhausted'
    assert cfg.queue.read_text().strip() == ''


def test_qc_requeue_refuses_an_unauthorized_pin_and_a_live_train(tmp_path, monkeypatch):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch, isolated=True)
    denied = main / 'data' / 'team'
    denied.mkdir(parents=True)
    (denied / 'do-not-push.txt').write_text(head + '\n')
    blocked = lr.qc_requeue(cfg, line, 'refused: no headroom\n', 'incpush', expected_name='x')
    assert blocked['reason'] == 'push_not_authorized'
    assert not cfg.queue.exists() or line not in cfg.queue.read_text()
    (denied / 'do-not-push.txt').unlink()
    cfg.procs = lambda: [TRAIN]
    running = lr.qc_requeue(cfg, line, 'refused: no headroom\n', 'inctrain', expected_name='x')
    assert running['reason'] == 'active_train'
    assert train_calls(out) == []


def test_qc_rollback_refuses_a_changed_line(tmp_path, monkeypatch):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch)
    assert lr.qc_requeue(cfg, line, 'refused: no headroom\n', 'incedit', expected_name='x')['outcome'] == 'applied'
    cfg.queue.write_text(line + ' extra\n')
    rolled = lr.qc_rollback_requeue(cfg, 'incedit')
    assert rolled['outcome'] == 'blocked' and rolled['reason'] == 'changed'
    assert cfg.queue.read_text().strip() == line + ' extra'


@pytest.fixture(params=['', TRAIN], ids=['idle', 'active-train'])
def cli_process_list(tmp_path, monkeypatch, request):
    """The CLI child cannot inherit cfg.procs; give it a private ps via PATH instead."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    ps = bin_dir / 'ps'
    output = f'12345 {request.param}\n' if request.param else ''
    ps.write_text(f'#!{sys.executable}\nimport sys\n'
                  "assert sys.argv[1:] == ['-axo', 'pid=,command=']\n"
                  f'sys.stdout.write({output!r})\n')
    ps.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    return request.param


def test_qc_requeue_cli_uses_a_fixed_argv_and_does_not_start_a_train(tmp_path, monkeypatch, cli_process_list):
    cfg, main, reviewed, head, out, line = qc_repo(tmp_path, monkeypatch)
    line_path, failure_path = tmp_path / 'line.txt', tmp_path / 'failure.txt'
    line_path.write_text(line + '\n')
    failure_path.write_text('refused: 1-minute load is 13.0\n')
    (cfg.landing / 'queue-retry.json').write_text(json.dumps({line: {'n': 1, 'after': 1}}))
    completed = subprocess.run(
        [sys.executable, str(Path(lr.__file__)), '--qc-action', 'qc-requeue', '--landing', str(cfg.landing),
         '--main', str(main), '--line-file', str(line_path), '--failure-file', str(failure_path),
         '--incident', 'cli1', '--name', 'x'],
        capture_output=True, text=True, check=False)
    assert completed.stdout, (completed.returncode, completed.stderr)
    payload = json.loads(completed.stdout)
    assert payload['train_launched'] is False
    assert train_calls(out) == []
    if not cli_process_list:
        assert completed.returncode == 0 and payload['outcome'] == 'applied', payload
        assert cfg.queue.read_text().strip() == line
        assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line]['n'] == 1
    else:
        assert completed.returncode == 2, payload
        assert payload['outcome'] == 'blocked' and payload['reason'] == 'active_train'
        assert not cfg.queue.exists() or cfg.queue.read_text().strip() == ''
        assert json.loads((cfg.landing / 'queue-retry.json').read_text())[line] == {'n': 1, 'after': 1}
