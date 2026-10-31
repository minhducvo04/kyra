#!/usr/bin/env python3
"""Background landing runner: the queue loop that used to live in a Claude chat (queue-runner5e.sh), as a launchd job.

Queue: data/team/landing/queue.txt, one train per line (lines starting with # are comments):
    <name> <system|web> [KEY=VALUE ...] <branch>@<40-hex sha> [<branch>@<sha> ...]
Only lane 1 writes the queue. Every branch carries the sha that was independently reviewed; the runner refuses (ALERT, no
train) when the branch head differs from the pin or a token has no pin, and rides the pinned sha itself.
PRIORITY=N sets the order (lowest first, no token = 99, file order breaks ties) and becomes `train.py --priority N`.
FULL=1 forces the full landing suite and excludes the entry from a batch.
Eligible ready entries share one merged-diff train: fast entries with fast ones, and entries whose diff needs the full
suite with each other (up to FULL_BATCH_MAX); red batches split in halves, transient refusals retain all lines.
WHERE=local asks for `--where local` whatever the kind. KYRA_RUNNER_POOL passes through; any other KEY=VALUE token is
dropped with a log line (a queue line must never change the merge gate's environment).
Log: data/team/landing/queue.log, lines `HH:MM:SS <name> LANDED: ...` / `REFUSED: ...`.
The forge- namespace belongs to Forge: this loop leaves those lines untouched.

Rules kept from v5e: one train at a time (it never starts while any `scripts/train.py <name>` process runs; train.py
takes its own landing-<area> locks on top), a single runner instance (flock), every branch gets current origin/master
merged in first (clean merges only), every landing fast-forwards the main checkout, system trains get `--where local`
while the 1-minute load is under 12 and companion.mac_guard admits new work (else the M1 runs the suite; 2026-10-29),
a refusal never stops the loop. It never pushes: train.py does, and refuses
protected paths. While a legacy queue-runner<N>.sh shell loop is alive it waits, so two runners never race.

    scripts/landing_runner.py            run forever (launchd, deploy/com.kyra.landing-runner.plist)
    scripts/landing_runner.py --once     one pass: land one train or a ready batch (bisect failures), then exit
"""
import argparse
import datetime

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

LOAD_LIMIT = 12.0
# A train is a Python process whose argv is "scripts/train.py <name>"; anchored so a shell that only mentions it never counts.
TRAIN_RE = re.compile(r'^/\S*[Pp]ython\S* (\S+ )*scripts/train\.py [A-Za-z0-9]')
OLD_RUNNER_RE = re.compile(r'^(/bin/)?(ba|z)?sh \S*queue-runner\d\w*\.sh')
NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
PIN_RE = re.compile(r'^(?P<branch>[^@\s]+)@(?P<sha>[0-9a-f]{40})$')
ALLOWED_ENV = ('PRIORITY', 'WHERE', 'KYRA_RUNNER_POOL', 'FULL')
IDLE_ALERT_S = 300
FULL_BATCH_MAX = 6  # entries per full-suite batch; a red batch still splits in halves
ENV_RE = re.compile(r'^[A-Z_][A-Z0-9_]*=')
# Precheck refusals that say "the Mac is busy", not "the branch is bad": the line stays queued and retries later.
# "origin/master moved while the suite ran" is the same kind: nothing was pushed, and the resume re-merges the new
# origin/master and re-runs the checks before landing (train.py remerge_master).
TRANSIENT_RE = re.compile(r'headroom refused|no headroom|runner held|another heavy job|1-minute load (is )?[\d.]+'
                          r'|origin/master moved while the suite ran|no online runner can take', re.I)
# Capacity is not a branch failure: no runner can take the work right now. The line stays queued as
# capacity-pending, spends no retry budget and does not blame the author (A0.3 refusal-loop fix).
CAPACITY_RE = re.compile(r'no online runner can take|no free capable machine', re.I)
GUARDS_FAILED_RE = re.compile(r'merged-tree guards failed', re.I)
FAILED_TEST_RE = re.compile(r'^(?:FAILED|ERROR) (\S+)', re.M)
LOAD_GUARD_TEST = 'tests/test_correct_repeaters.py::test_load_allows_the_browser_phase'
RETRY_FIRST_S, RETRY_MAX_S, RETRY_ALERT_AT, RETRY_CAP = 300, 1800, 3, 6
LANDED_RE = re.compile(r'landed [0-9a-f]+\.\.([0-9a-f]+) on origin/master')


def mac_busy_now():
    """companion.mac_guard's admission (2026-10-29 panic): a train's suite runs on this Mac only while it admits new
    work (memory pressure normal, 10% free, load at or under 10); otherwise the M1 runs it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion import mac_guard
    return mac_guard.admit('landing train on this Mac')


def main_checkout():
    here = Path(__file__).resolve().parents[1]
    out = subprocess.run(['git', '-C', str(here), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                         capture_output=True, text=True)
    return Path(out.stdout.strip()).parent if out.returncode == 0 and out.stdout.strip() else here


@dataclass
class Config:
    main: Path = field(default_factory=main_checkout)
    landing: Path = None
    poll: int = 20
    hold_max: int = 5400
    procs: callable = None  # () -> list of command lines; injectable for tests
    load: callable = None   # () -> 1-minute load
    process: callable = None  # (cfg, entry) -> None; injectable for tests
    forge_authority: object = None  # explicit run_queued compatibility; never used by this polling loop
    mac_busy: callable = None  # () -> companion.mac_guard's refusal or None; injectable for tests

    def __post_init__(self):
        self.landing = Path(self.landing or self.main / 'data/team/landing')
        self.landing.mkdir(parents=True, exist_ok=True)
        self.procs = self.procs or all_commands
        self.load = self.load or load_1min
        self.process = self.process or process_entry
        self.mac_busy = self.mac_busy or mac_busy_now

    @property
    def queue(self): return self.landing / 'queue.txt'

    @property
    def parked(self): return self.landing / 'queue-parked.txt'

    @property
    def inbox(self): return self.main / 'data/team/board-inbox.jsonl'

    @property
    def python(self):
        venv = self.main / '.venv/bin/python'
        return str(venv) if venv.exists() else sys.executable


@dataclass
class Entry:
    name: str
    kind: str
    envs: list
    branches: list
    line: str
    priority: int = 99
    index: int = 0
    batch: bool = False


def parse_line(line):
    """None for a blank or comment line; ValueError for a malformed one."""
    text = line.strip()
    if not text or text.startswith('#'):
        return None
    tokens = text.split()
    if len(tokens) < 3 or tokens[1] not in ('system', 'web'):
        raise ValueError(f'bad queue line: {text[:80]}')
    name, kind, rest = tokens[0], tokens[1], tokens[2:]
    if not NAME_RE.match(name):
        raise ValueError(f'bad train name: {text[:80]}')
    envs = []
    while rest and ENV_RE.match(rest[0]):
        envs.append(rest.pop(0))
    if not rest:
        raise ValueError(f'queue line has no branches: {text[:80]}')
    priority = 99
    for env in envs:
        if env.startswith('PRIORITY='):
            if not env[9:].isdigit():
                raise ValueError(f'bad PRIORITY: {text[:80]}')
            priority = int(env[9:])
    return Entry(name, kind, envs, rest, text, priority)


def read_queue(path, *, include_forge=True):
    """(entries in run order, malformed lines); the legacy loop excludes Forge ownership."""
    entries, bad = [], []
    try:
        lines = Path(path).read_text(errors='replace').splitlines()
    except FileNotFoundError:
        return [], []
    for i, line in enumerate(lines):
        if not include_forge and line.lstrip().startswith('forge-'):
            continue
        try:
            entry = parse_line(line)
        except ValueError:
            bad.append(line)
            continue
        if entry:
            entry.index = i
            entries.append(entry)
    return sorted(entries, key=lambda e: (e.priority, e.index)), bad


@contextmanager
def queue_locked(cfg):
    """The same queue lock for both append and removal."""
    with open(cfg.landing / 'queue.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_queue(cfg, lines):
    """Replace queue.txt durably; caller holds queue_locked."""
    tmp = cfg.landing / 'queue.tmp'
    with tmp.open('w') as stream:
        stream.write(''.join(item + '\n' for item in lines))
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(cfg.queue)
    fd = os.open(cfg.landing, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_line_once(cfg, line):
    """Append an exact train identity once, using the runner's parser/lock/atomic replacement.

    The Forge journal additionally survives queue removal; this file alone is not a delivery ledger.
    """
    entry = parse_line(line)
    if entry is None:
        raise ValueError('empty queue entry')
    with queue_locked(cfg):
        entries, bad = read_queue(cfg.queue)
        if bad:
            raise ValueError('malformed queue; reconcile before appending')
        matches = [e for e in entries if e.name == entry.name]
        if matches:
            if len(matches) != 1 or matches[0].line != entry.line:
                raise ValueError('train identity conflicts with queue')
            return False
        lines = cfg.queue.read_text().splitlines() if cfg.queue.exists() else []
        write_queue(cfg, [*lines, entry.line])
        return True


def remove_line(cfg, line):
    """Drop the first equal line under the shared queue lock."""
    with queue_locked(cfg):
        try:
            lines = cfg.queue.read_text(errors='replace').splitlines()
        except FileNotFoundError:
            return
        if line in lines:
            lines.remove(line)
        write_queue(cfg, lines)


def retry_state(cfg):
    try:
        data = json.loads((cfg.landing / 'queue-retry.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_retry_state(cfg, data):
    tmp = cfg.landing / 'queue-retry.tmp'
    tmp.write_text(json.dumps(data))
    tmp.replace(cfg.landing / 'queue-retry.json')


def note_retry(cfg, entry):
    """Back off a transiently refused line: 5 min, doubling to 30. The 3rd retry puts one row in the Board inbox.
    Returns False once the line has been refused RETRY_CAP times (about 2 h): the caller drops it (one inbox row)."""
    data = retry_state(cfg)
    n = data.get(entry.line, {}).get('n', 0) + 1
    if n > RETRY_CAP:
        alert(cfg, f'{entry.name} refused {n} times for a busy Mac; gave up and dropped the line (requeue it when the '
                   f'Mac is quiet)', f'retrycap:{entry.line}')
        return False
    delay = min(RETRY_FIRST_S * 2 ** (n - 1), RETRY_MAX_S)
    data[entry.line] = {'n': n, 'after': time.time() + delay}
    save_retry_state(cfg, data)
    log(cfg, f'{entry.name} transient refusal, kept in queue: retry {n} in {delay // 60} min')
    if n == RETRY_ALERT_AT:
        alert(cfg, f'{entry.name} refused {n} times for a busy Mac (load or headroom); still queued, retrying every '
                   f'{RETRY_MAX_S // 60} min or less', f'retry{n}:{entry.line}')
    return True


def clear_retry(cfg, line):
    data = retry_state(cfg)
    if data.pop(line, None) is not None:
        save_retry_state(cfg, data)


def is_capacity(text):
    """True when the refusal only says no runner can take the work now (capacity, not a branch failure)."""
    return bool(CAPACITY_RE.search(text or ''))


def pinned_key(entry):
    """The pinned shas of an entry (raw token when it has no pin): the identity a refusal dedupes on."""
    parts = []
    for token in entry.branches:
        m = PIN_RE.match(token)
        parts.append(m['sha'] if m else token)
    return ','.join(parts)


def refusal_state(cfg):
    try:
        data = json.loads((cfg.landing / 'queue-refusals.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def refusal_key(entry, reason_class):
    return f'{entry.name}|{pinned_key(entry)}|{reason_class}'


def refusal_seen(cfg, entry, reason_class):
    return refusal_key(entry, reason_class) in refusal_state(cfg)


def record_refusal(cfg, entry, reason_class, reason=''):
    """Remember one refusal keyed by (name, pinned sha, reason class). Returns True the first time only."""
    data = refusal_state(cfg)
    key = refusal_key(entry, reason_class)
    if key in data:
        return False
    data[key] = {'name': entry.name, 'pins': pinned_key(entry), 'class': reason_class,
                 'reason': ' '.join(str(reason).split())[:200], 'line': entry.line}
    tmp = cfg.landing / 'queue-refusals.tmp'
    tmp.write_text(json.dumps(data))
    tmp.replace(cfg.landing / 'queue-refusals.json')
    return True


def parked_lines(cfg):
    try:
        return [line.strip() for line in cfg.parked.read_text(errors='replace').splitlines()
                if line.strip() and not line.startswith('#')]
    except FileNotFoundError:
        return []


def is_parked(cfg, entry):
    """True when an entry with the same name and pinned shas is already parked: not retried until its sha changes."""
    for line in parked_lines(cfg):
        try:
            other = parse_line(line)
        except ValueError:
            continue
        if other and other.name == entry.name and pinned_key(other) == pinned_key(entry):
            return True
    return False


def park_entry(cfg, entry, reason, reason_class=None):
    """Move the queue line to queue-parked.txt once (one-line reason) and out of the queue. Idempotent.

    A repeat with an unchanged pinned sha adds no second parked row and no second log line.
    """
    reason = ' '.join(str(reason).split())[:200] or 'refused'
    record_refusal(cfg, entry, reason_class or reason.split(':', 1)[0], reason)
    with queue_locked(cfg):
        already = is_parked(cfg, entry)
        if not already:
            with cfg.parked.open('a') as stream:
                stream.write(f'# parked: {reason}\n{entry.line}\n')
                stream.flush()
                os.fsync(stream.fileno())
        try:
            lines = cfg.queue.read_text(errors='replace').splitlines()
        except FileNotFoundError:
            lines = []
        if entry.line in lines:
            lines.remove(entry.line)
            write_queue(cfg, lines)
    clear_retry(cfg, entry.line)
    if not already:
        log(cfg, f'{entry.name} PARKED: {reason}')
    return 'parked'


def rename_stale_entry(cfg, entry):
    """Rename a stale-train entry once to <base>-n<k> (smallest free k), in the queue and on the entry itself."""
    base = re.sub(r'-n\d+$', '', entry.name)
    k = 1
    while (cfg.main / '.claude' / 'worktrees' / f'{base}-n{k}').exists():
        k += 1
    new_name = f'{base}-n{k}'
    new_line = new_name + entry.line[len(entry.name):]
    with queue_locked(cfg):
        try:
            lines = cfg.queue.read_text(errors='replace').splitlines()
        except FileNotFoundError:
            lines = []
        if entry.line in lines:
            lines[lines.index(entry.line)] = new_line
            write_queue(cfg, lines)
    clear_retry(cfg, entry.line)
    log(cfg, f'{entry.name}: stale train of the same name; renamed once to {new_name} and processing it')
    entry.name, entry.line = new_name, new_line
    return entry


def log(cfg, msg):
    with open(cfg.landing / 'queue.log', 'a') as f:
        f.write(f'{time.strftime("%H:%M:%S")} {msg}\n')


def alert(cfg, msg, key=None):
    """An ALERT row in queue.log and, once per key, in the Board inbox (the place the coordinator reads)."""
    log(cfg, f'ALERT: {msg}')
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from companion import board_inbox
        rid = 'landing-runner:' + hashlib.sha256((key or msg).encode()).hexdigest()[:16]
        board_inbox.append({'t': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
                            'from': 'landing-runner', 'to': 'board', 'kind': 'alert', 'text': msg[:300],
                            'routing_id': rid}, path=cfg.inbox, once=True)
    except Exception as exc:  # the log row above stands; an inbox failure must not stop the loop
        log(cfg, f'Board inbox write failed: {type(exc).__name__}: {str(exc)[:120]}')


def all_commands():
    out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout
    rows = (re.match(r'\s*(\d+) (.*)', row) for row in out.splitlines())
    return [m.group(2) for m in rows if m and int(m.group(1)) != os.getpid()]


def load_1min():
    return os.getloadavg()[0]


def train_running(cfg):
    return any(TRAIN_RE.match(c) for c in cfg.procs())


def old_runner_alive(cfg):
    return any(OLD_RUNNER_RE.match(c) for c in cfg.procs())


def acquire_lock(landing, filename="runner.lock"):
    """Nonblocking lock; keep the returned file open, or wait when it returns None."""
    f = open(Path(landing) / filename, 'a')  # append: a refused second instance must not wipe the holder's pid
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.truncate(0)
    f.write(f'{os.getpid()}\n')
    f.flush()
    return f


def git(cfg, *args, check=False):
    from pkgmap_merge import GIT_CONFIG  # package-map entries added side by side merge cleanly when boarding
    return subprocess.run(['git', '-C', str(cfg.main), *GIT_CONFIG, *args], capture_output=True, text=True, check=check)


def stale_train(cfg, name, pins, boarded):
    """True when the existing train worktree is not this line's train: no saved state, a saved ref that descends from
    none of the current pins, or a boarded pin (the pin itself or the first parent of its boarding merge) that no saved
    ref contains. A pin that boarding left out is never required (review of a11de9b6 and 2996f031)."""
    try:
        saved = json.loads((cfg.main / 'data' / 'train-logs' / f'{name}.state.json').read_text()).get('branches') or []
    except (OSError, ValueError):
        return True
    shas = {sha for _, sha in pins}

    def ancestor(a, b):
        return git(cfg, 'merge-base', '--is-ancestor', a, b).returncode == 0

    kept = []
    for ref in boarded:
        sha = git(cfg, 'rev-parse', '--verify', '-q', ref + '^{commit}').stdout.strip()
        kept.append(sha if sha in shas else git(cfg, 'rev-parse', '--verify', '-q', sha + '^1').stdout.strip())
    return (not saved or any(not any(ancestor(sha, ref) for sha in shas) for ref in saved)
            or any(not any(ancestor(pin, ref) for ref in saved) for pin in kept))


def on_master(cfg, ref):
    return git(cfg, 'merge-base', '--is-ancestor', ref, 'origin/master').returncode == 0


def board(cfg, entry, pins):
    """Merge current origin/master into each pinned (branch, sha) (clean merges only); return the refs to ride."""
    git(cfg, 'fetch', '-q', 'origin')
    om = git(cfg, 'rev-parse', 'origin/master').stdout.strip()
    today = datetime.date.today().isoformat()
    kept = []
    for k, (src, tip) in enumerate(pins, 1):
        if git(cfg, 'merge-base', '--is-ancestor', om, tip).returncode == 0:
            kept.append(tip)
            continue
        tree = git(cfg, 'merge-tree', '--write-tree', tip, om)
        if tree.returncode != 0:
            log(cfg, f'{entry.name}: {src} does not merge cleanly with origin/master; left out')
            continue
        msg = (f'Merge origin/master into {src} for boarding {entry.name} (no conflict)\n\n'
               'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>')
        made = git(cfg, 'commit-tree', tree.stdout.splitlines()[0], '-p', tip, '-p', om, '-m', msg)
        commit = made.stdout.strip()
        boarded = f'session/{today}-{entry.name}-q{k}'
        if made.returncode != 0 or not commit or git(cfg, 'branch', '-f', boarded, commit).returncode != 0:
            log(cfg, f'{entry.name}: boarding merge for {src} failed; left out')
            continue
        kept.append(boarded)
    return kept



def smart_batch(cfg, entries):
    """Related reviewed pins share a bounded train; unrelated changes keep their own outcome."""
    import yaml

    from companion.train_placement import related_groups

    first = entries[0]
    if 'FULL=1' in first.envs:
        return [first]
    changes = {}
    for entry in entries:
        if 'FULL=1' in entry.envs:
            continue
        pins = check_pins(cfg, entry, quiet=True)
        if not pins:
            if entry == first:
                return [first]  # process_entry owns the refusal and its evidence
            continue
        paths = set()
        for _, sha in pins:
            diff = git(cfg, 'diff', '--name-only', '--no-renames', f'origin/master...{sha}')
            if diff.returncode:
                return [first]
            paths.update(diff.stdout.splitlines())
        changes[entry.name] = paths
    document = yaml.safe_load((cfg.main / 'docs/architecture/packages.yaml').read_text())
    group = related_groups(changes, document['packages'])[0][:FULL_BATCH_MAX]
    return [entry for entry in entries if entry.name in group]


def configured_machine(cfg, entry):
    """None for legacy configuration; a registry name or unavailable for the new config."""
    path = cfg.main / 'data/config/runners.json'
    if not path.exists():
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    import remote_test
    from companion import runner_pool, runner_probe, train_placement
    machines = runner_pool.load_registry(path)
    # A configured native Windows host is not proof that this revision can run it.
    if not hasattr(remote_test, 'remote_command_ps'):
        machines = [r for r in machines if r.os != 'windows']
    if 'WHERE=local' in entry.envs:
        machines = [r for r in machines if r.reach == 'local']
    probe = getattr(cfg, 'machine_probe', None) or runner_probe.make_probe(m1_settings=lambda: ('', ''))
    loads = {r.name: probe(r) for r in machines}
    busy_probe = getattr(cfg, 'mac_busy', None)
    if busy_probe is None:
        try:
            from companion import mac_guard
            busy = mac_guard.admit('landing train on this Mac')
        except ImportError:
            busy = 'Mac stability admission is not installed'
    else:
        busy = busy_probe()
    # The browser is a separate macOS phase; the placement names the non-browser work.
    request = train_placement.Request(entry.name, entry.priority)
    selected = train_placement.plan([request], machines, loads, mac_admitted=not busy)
    name = selected.get(entry.name)
    if name is None:
        return 'unavailable'
    return name

def placement(cfg, entry):
    """(train.py extra args, env assignments, place) from the queue line's tokens."""
    envs, kind, args = list(entry.envs), entry.kind, []
    if entry.batch:
        args.append('--allow-mixed')
    if 'WHERE=local' in envs:
        kind = 'system'
    if entry.priority != 99 or any(e.startswith('PRIORITY=') for e in envs):
        args += ['--priority', str(entry.priority)]
    pool = 'KYRA_RUNNER_POOL=1' in envs
    dropped = [e.split('=', 1)[0] for e in envs if e.split('=', 1)[0] not in ALLOWED_ENV]
    if dropped:
        log(cfg, f'{entry.name}: queue env token(s) dropped, not allowed: {" ".join(dropped)}')
    force_full = 'FULL=1' in envs
    envs = [e for e in envs if e.split('=', 1)[0] == 'KYRA_RUNNER_POOL']
    if force_full:
        envs.append('KYRA_LAND_FULL=1')
    configured = configured_machine(cfg, entry)
    if configured is not None:
        if configured != 'unavailable':
            from companion import runner_pool
            machine = next(r for r in runner_pool.load_registry(cfg.main / 'data/config/runners.json')
                           if r.name == configured)
            if machine.reach == 'local':
                args += ['--where', 'local']
            envs = [e for e in envs if not e.startswith('KYRA_RUNNER_POOL=')]
            envs.append('KYRA_TRAIN_RUNNER=' + configured)
        return args, envs, configured
    place = 'pool' if pool else 'm1'
    if kind == 'system' and not pool and not (cfg.landing / 'no-where').exists() and cfg.load() < LOAD_LIMIT:
        if busy := cfg.mac_busy():
            log(cfg, f'{entry.name}: tests go to the M1, not this Mac: {busy}')
        else:
            args += ['--where', 'local']
            place = 'local'
    return args, envs, place


def runner_state(cfg):
    """free | held | unreachable | unknown. The ssh detail of an unreachable runner is logged (first line, host hidden)."""
    code = ('import sys; sys.path[:0]=["scripts","src"]; import remote_test as rt; s, d = rt.runner_state(); '
            'print(s); print(rt.safe_log(d))')
    out = subprocess.run([cfg.python, '-c', code], cwd=cfg.main, capture_output=True, text=True)
    state, _, detail = out.stdout.strip().partition('\n')
    if state == 'unreachable':
        log(cfg, f'runner unreachable: {detail.strip()[:160]}')
    return state or 'unknown'


def landed(cfg, run_log, branches):
    git(cfg, 'fetch', '-q', 'origin')
    try:
        hits = LANDED_RE.findall(Path(run_log).read_text())
    except FileNotFoundError:
        hits = []
    if hits and on_master(cfg, hits[-1]):
        return True
    return bool(branches) and all(on_master(cfg, b) for b in branches)


def ff_main(cfg):
    git(cfg, 'fetch', '-q', 'origin')
    out = git(cfg, 'merge', '--ff-only', 'origin/master')
    if out.returncode == 0:
        log(cfg, f'main checkout fast-forwarded to {git(cfg, "rev-parse", "--short", "HEAD").stdout.strip()}')
    else:
        tail = (out.stdout + out.stderr).strip().splitlines()[-1:] or ['']
        log(cfg, f'main checkout NOT fast-forwarded (left as is): {tail[0][:160]}')


def is_transient(text):
    """True when a refusal only says the Mac is busy. A guard-failure refusal is transient only if the load guard is
    the one failing test; any other failing test (alone or beside it) is a real refusal."""
    failed = {m.group(1) for m in FAILED_TEST_RE.finditer(text)} if GUARDS_FAILED_RE.search(text) else set()
    if failed:
        return failed == {LOAD_GUARD_TEST}
    return bool(TRANSIENT_RE.search(text))


def last_matching(path, pattern):
    try:
        rows = [r for r in Path(path).read_text().splitlines() if r.strip() and re.search(pattern, r, re.I)]
    except FileNotFoundError:
        rows = []
    return (rows[-1] if rows else '')[:200]


def check_pins(cfg, entry, *, quiet=False):
    """[(branch, sha)] when every token is branch@sha and the branch head still equals the pin; else None (ALERT).

    quiet=True is the QC requeue check: a mismatch is a blocked repair, not a second runner alert.
    """
    pins = []
    for token in entry.branches:
        m = PIN_RE.match(token)
        if not m:
            first = quiet or entry.name.startswith('forge-') or record_refusal(cfg, entry, 'no-pin', f'{token[:80]} has no pin')
            if not quiet and first:
                alert(cfg, f'{entry.name} refused: {token[:80]} has no branch@<40-hex sha> pin; no train started', entry.line)
            return None
        head = git(cfg, 'rev-parse', '--verify', '-q', m['branch'] + '^{commit}').stdout.strip()
        if head != m['sha']:
            first = quiet or entry.name.startswith('forge-') or record_refusal(
                cfg, entry, 'pin-mismatch', f'{m["branch"]} head differs from the reviewed pin')
            if not quiet and first:
                alert(cfg, f'{entry.name} refused: {m["branch"]} head {head[:12] or "missing"} differs from the reviewed '
                           f'pin {m["sha"][:12]}; no train started', entry.line)
            return None
        pins.append((m['branch'], m['sha']))
    return pins


def process_entry(cfg, entry):
    name = entry.name
    if not entry.batch and not name.startswith('forge-') and is_parked(cfg, entry):
        return park_entry(cfg, entry, 'already parked: unchanged pinned sha', 'parked')
    pins = check_pins(cfg, entry)
    if pins is None:
        if entry.batch:
            return 'failed'
        if name.startswith('forge-'):
            return
        reason_class = 'pin-mismatch' if all(PIN_RE.match(t) for t in entry.branches) else 'no-pin'
        reason = ('pin-mismatch: head differs from the reviewed pin' if reason_class == 'pin-mismatch'
                  else 'no-pin: branch token has no branch@sha pin')
        return park_entry(cfg, entry, reason, reason_class)
    if all(on_master(cfg, sha) for _, sha in pins):
        log(cfg, f'{name} already on origin/master; skipped')
        return 'landed'
    # Forge pins have already been checked against the current base and evidence under admission.
    # Never silently manufacture a new boarding candidate carrying the old candidate's review.
    branches = [sha for _, sha in pins] if name.startswith('forge-') else board(cfg, entry, pins)
    if name.startswith('master-fix-') and not master_repair_allowed(cfg):
        log(cfg, f'{name} waits for keeper to check the moved master')
        return 'master-pending'
    if not branches:
        log(cfg, f'{name}: no branches left after boarding merges; skipped')
        return
    if len(branches) != len(pins):
        log(cfg, f'{name}: boarding omitted a pin; refuse the whole candidate before splitting')
        return 'failed'
    (cfg.landing / f'{name}-branches.txt').write_text(''.join(b + '\n' for b in branches))
    args, envs, place = placement(cfg, entry)
    if place == 'unavailable':
        log(cfg, f'{name}: no free capable machine; retained without consuming refusal retries')
        return 'capacity-pending'
    env = {**os.environ, 'KYRA_LAND_FULL': '0', **dict(e.split('=', 1) for e in envs),
           'KYRA_RUNNER_STATE': runner_state(cfg)}
    base = [cfg.python, 'scripts/train.py', name, '--base', 'origin/master']
    log(cfg, f'{name} precheck starting: ({" ".join(entry.envs)}; {" ".join(branches)}; runner {env["KYRA_RUNNER_STATE"]})')
    worktree = cfg.main / '.claude' / 'worktrees' / name
    resume = worktree.exists()  # a transient refusal after the train made its worktree: a fresh launch is refused as a duplicate
    if resume and stale_train(cfg, name, pins, branches):
        if entry.batch:
            return 'failed'
        if name.startswith('forge-'):
            alert(cfg, f'{name} refused: the existing train worktree rides branches that do not descend from the pinned '
                       f'shas (a stale train of the same name); no train started', entry.line)
            return
        rename_stale_entry(cfg, entry)
        name = entry.name
        resume = False
        (cfg.landing / f'{name}-branches.txt').write_text(''.join(b + '\n' for b in branches))
        base = [cfg.python, 'scripts/train.py', name, '--base', 'origin/master']
    resume_argv = [cfg.python, 'scripts/train.py', '--resume', name] + args
    pre = subprocess.run((resume_argv + ['--precheck-only']) if resume else base + args + ['--precheck-only'] + branches, cwd=cfg.main, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True)
    (cfg.landing / f'{name}-precheck.log').write_text(pre.stdout + pre.stderr)
    if name.startswith('forge-'):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))  # launchd has no PYTHONPATH
        from companion.forge_landing import execution_fact
        execution_fact(cfg, entry, 'precheck', pre.returncode)
    if pre.returncode != 0:
        why = last_matching(cfg.landing / f'{name}-precheck.log', 'refused')
        log(cfg, f'{name} precheck refused: {why}')
        if is_capacity(pre.stdout + pre.stderr):
            log(cfg, f'{name} capacity-pending: no online runner can take it now; kept queued, no retry budget spent')
            return 'capacity-pending'
        if retain_for_master(cfg, entry, pre.stdout + pre.stderr, cfg.landing / f'{name}-precheck.log'):
            return 'master-pending'
        if is_transient(pre.stdout + pre.stderr):
            return 'retry'
        return 'failed'
    log(cfg, f'placement: {name} {place}')
    if place == 'local':
        log(cfg, f'{name} tests on this Mac (--where local, load {cfg.load():.2f})')
    log(cfg, f'{name} started' + (f' ({" ".join(envs)})' if envs else ''))
    run_log = cfg.landing / f'{name}-run.log'
    with open(run_log, 'w') as out:
        argv = resume_argv if resume else base + args + branches
        completed = subprocess.run(argv, cwd=cfg.main, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=subprocess.STDOUT)
    if name.startswith('forge-'):
        execution_fact(cfg, entry, 'run', completed.returncode)
    verified_landed = landed(cfg, run_log, branches)
    from companion.train_phase import usage_lines
    for usage in usage_lines(run_log.read_text(errors='replace'), landed=verified_landed):
        log(cfg, f'{usage} train={name}')
    if verified_landed:
        log(cfg, f'{name} LANDED: {last_matching(run_log, "landed ")[:160]}')
        if name.startswith('master-fix-'):
            from master_keeper import track_landed
            track_landed(cfg, name)
        ff_main(cfg)
        return 'landed'
    else:
        why = last_matching(run_log, "refused|failed")
        log(cfg, f'{name} REFUSED: {why}')
        try:
            tail = run_log.read_text()
        except OSError:
            tail = ''
        if is_capacity(tail):
            log(cfg, f'{name} capacity-pending: no online runner can take it now; kept queued, no retry budget spent')
            return 'capacity-pending'
        if retain_for_master(cfg, entry, tail, run_log):
            return 'master-pending'
        if is_transient(tail if GUARDS_FAILED_RE.search(why) else why):  # the train's own load/headroom/runner-held refusal: keep the line, retry
            return 'retry'
        return 'failed'


def needs_full(cfg, entry):
    """Inspect the pinned tree, not the runner checkout. The merged gate recomputes this.

    An invalid pin or unreadable mapping is never admitted to a fast batch.
    """
    if 'FULL=1' in entry.envs:
        return True
    pins = check_pins(cfg, entry, quiet=True)
    if not pins:
        return True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion.test_impact import landing_selection
    for _, sha in pins:
        diff = git(cfg, 'diff', '--name-only', '--no-renames', f'origin/master...{sha}')
        if diff.returncode:
            return True
        with tempfile.TemporaryDirectory(prefix='landing-selection-') as directory:
            tree = Path(directory) / 'tree'
            if git(cfg, 'worktree', 'add', '--detach', str(tree), sha).returncode:
                return True
            try:
                if landing_selection(diff.stdout.splitlines(), root=tree)['full']:
                    return True
            finally:
                git(cfg, 'worktree', 'remove', '--force', str(tree), check=True)
    return False


def process_batch(cfg, entries):
    """One union train. Bisect real failures; transient refusals keep original lines pending."""
    if len(entries) == 1:  # cycle() already applied the hold (a master-fix line may pass it)
        pending = _load_json(cfg.landing / (entries[0].name + '-master-pending.json')) or {}
        if pending.get('line') == entries[0].line:
            return [(entries[0], 'master-pending')]
        try:
            result = cfg.process(cfg, entries[0])
        except Exception as exc:
            log(cfg, f'{entries[0].name} ERROR: {type(exc).__name__}: {str(exc)[:160]}')
            result = None
        return [(entries[0], result)]
    if (cfg.landing / 'queue-hold').exists():
        log(cfg, 'batch continuation held; keep original queue lines pending')
        return [(entry, 'retry') for entry in entries]
    identity = '\n'.join(e.line for e in entries)
    name = 'batch-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    # Keep explicit placement only when shared; a mixed batch uses the web placement.
    envs = [e for e in entries[0].envs if e in set.intersection(*(set(x.envs) for x in entries))
            and e.split('=', 1)[0] in ('WHERE', 'KYRA_RUNNER_POOL')]
    envs.append(f'PRIORITY={min(e.priority for e in entries)}')
    branches = list(dict.fromkeys(b for e in entries for b in e.branches))
    kind = 'web' if any(e.kind == 'web' for e in entries) else 'system'
    batch = Entry(name, kind, envs, branches, identity, min(e.priority for e in entries), batch=True)
    receipt = cfg.landing / (name + '-batch-failed.json')
    inputs = {'line': identity, 'base': git(cfg, 'rev-parse', 'origin/master').stdout.strip()}
    log(cfg, f'{name}: batch members ' + ' '.join(e.name for e in entries))
    try:
        result = 'failed' if _load_json(receipt) == inputs else cfg.process(cfg, batch)
    except Exception as exc:
        log(cfg, f'{name}: batch interrupted ({type(exc).__name__}); keep members pending')
        result = 'retry'
    if result == 'failed':
        _atomic_json(receipt, inputs)
        log(cfg, f'{name}: red batch; splitting {len(entries)} entries in halves')
        mid = len(entries) // 2
        return process_batch(cfg, entries[:mid]) + process_batch(cfg, entries[mid:])
    return [(entry, result) for entry in entries]


def master_repair_allowed(cfg):
    health = _load_json(cfg.landing / 'master-health.json') or {}
    if not health:
        return True
    current = git(cfg, 'rev-parse', 'origin/master').stdout.strip()
    return current == health.get('tested_commit', health.get('commit'))


def master_hold(cfg):
    marker = cfg.landing / 'queue-hold'
    return marker.exists() and marker.read_text().startswith('master-keeper:')


def retain_for_master(cfg, entry, text, log_path=None):
    """Do not discard a test refusal before the keeper checks the identical tests on master."""
    if entry.batch or not FAILED_TEST_RE.search(text) or is_transient(text):
        return False
    _atomic_json(cfg.landing / (entry.name + '-master-pending.json'),
                 {'line': entry.line, 'base': git(cfg, 'rev-parse', 'origin/master').stdout.strip(),
                  'tests': sorted(set(FAILED_TEST_RE.findall(text))), 'log': str(log_path or '')})
    log(cfg, f'{entry.name} retained for master diagnosis')
    return True


def quarantine_entry(cfg, entry, tests, log_path):
    """Keep refusal evidence before removing the exact queue identity, under the queue lock."""
    with queue_locked(cfg):
        lines = cfg.queue.read_text().splitlines() if cfg.queue.exists() else []
        if entry.line not in [line.strip() for line in lines]:
            return
        receipt = cfg.landing / (entry.name + '-quarantined.json')
        pending = _load_json(cfg.landing / (entry.name + '-master-pending.json')) or _load_json(receipt) or {}
        _atomic_json(receipt, {'line': entry.line, 'base': pending.get('base', ''),
                               'tests': tests, 'log': str(log_path)})
        reason = 'branch failure; failing tests: ' + ', '.join(tests)
        (cfg.landing / (entry.name + '-fixback.md')).write_text(
            f'# Fix back: {entry.name}\n\nReason: {reason}\nLog: {log_path}\n\nQueue line:\n{entry.line}\n')
        quarantine = cfg.landing / 'quarantine.txt'
        saved = quarantine.read_text().splitlines() if quarantine.exists() else []
        if entry.line not in saved:
            with quarantine.open('a') as stream:
                stream.write(f'# {reason}; log: {log_path}\n{entry.line}\n')
                stream.flush()
                os.fsync(stream.fileno())
        write_queue(cfg, [line for line in lines if line.strip() != entry.line])
        (cfg.landing / (entry.name + '-master-pending.json')).unlink(missing_ok=True)
    clear_retry(cfg, entry.line)
    log(cfg, f'{entry.name} quarantined: {reason}; log: {log_path}')


def reconcile_pending(cfg):
    """Retire stale markers and finish saved keeper classifications before selecting work."""
    state = _load_json(cfg.landing / 'master-keeper.json') or {}
    entries, _ = read_queue(cfg.queue, include_forge=False)
    queued = {entry.line: entry for entry in entries}
    base = None
    for receipt in cfg.landing.glob('*-quarantined.json'):
        row = _load_json(receipt) or {}
        if row.get('line') in queued:
            if base is None:
                base = git(cfg, 'rev-parse', 'origin/master').stdout.strip()
            if row.get('base') == base:
                quarantine_entry(cfg, queued[row['line']], row.get('tests', []), row.get('log', ''))
    for marker in cfg.landing.glob('*-master-pending.json'):
        if marker.name.startswith('forge-'):
            continue
        row = _load_json(marker) or {}
        line = row.get('line', '')
        if '\n' in line:  # Old runners parked whole batches; bisect without rerunning them.
            _atomic_json(marker.with_name(marker.name.replace('-master-pending', '-batch-failed')),
                         {'line': line, 'base': row.get('base', '')})
            marker.unlink(missing_ok=True)
            continue
        current = {entry.line: entry for entry in read_queue(cfg.queue, include_forge=False)[0]}
        if line not in current:
            marker.unlink(missing_ok=True)
            continue
        entry = current[line]
        for suffix in ('-run.log', '-precheck.log'):
            path = cfg.landing / (entry.name + suffix)
            if row.get('log') and row['log'] != str(path):
                continue
            if state.get('classifications', {}).get(str(path)) != 'branch failure' or not path.exists():
                continue
            inputs = state.get('classification_inputs', {}).get(str(path))
            if inputs is not None and inputs != {'line': line, 'base': row.get('base')}:
                continue
            text = path.read_text(errors='replace')
            if state.get('logs', {}).get(str(path)) == hashlib.sha256(text.encode()).hexdigest():
                quarantine_entry(cfg, entry, sorted(set(FAILED_TEST_RE.findall(text))), path)
                break


def hold(cfg):
    """queue-hold marker: only for the owner protected push; it lapses after hold_max seconds."""
    marker = cfg.landing / 'queue-hold'
    if not marker.exists() or master_hold(cfg):
        return
    log(cfg, f'queue HOLD on (queue-hold marker; protected push only), max {cfg.hold_max} s')
    t0 = time.time()
    while marker.exists() and time.time() - t0 < cfg.hold_max:
        time.sleep(cfg.poll)
    if marker.exists():
        marker.unlink()
        alert(cfg, f'queue HOLD timed out after {cfg.hold_max} s and was removed; queue resumes')
    else:
        log(cfg, 'queue HOLD released')
    ff_main(cfg)


def idle_check(cfg, state, waiting):
    """ALERT once per stretch when work has waited IDLE_ALERT_S with no train of ours starting (load under the limit)."""
    if not waiting or cfg.load() >= LOAD_LIMIT:
        state['idle_since'] = state['idle_alerted'] = None
        return
    now = time.time()
    state['idle_since'] = state.get('idle_since') or now
    if now - state['idle_since'] >= IDLE_ALERT_S and not state.get('idle_alerted'):
        state['idle_alerted'] = True
        alert(cfg, f'queue idle {IDLE_ALERT_S} s with work waiting and no train started '
                   '(a train or legacy runner still alive?)', f'idle:{int(state["idle_since"])}')


def cycle(cfg, state=None):
    """One pass. Returns idle, old-runner, busy, delayed or ran:<name>."""
    state = state if state is not None else {}
    reconcile_pending(cfg)
    result = _cycle(cfg, state)
    idle_check(cfg, state, result in ('busy', 'old-runner')
               and bool(read_queue(cfg.queue, include_forge=False)[0]))
    return result


def _cycle(cfg, state):
    if old_runner_alive(cfg):
        if not state.get('waited'):
            log(cfg, 'waiting: a legacy queue-runner shell loop is still alive (lane 1 stops it, then this runner starts)')
            state['waited'] = True
        return 'old-runner'
    state['waited'] = False
    entries, bad = read_queue(cfg.queue, include_forge=False)
    for line in bad:
        log(cfg, f'bad queue line dropped: {line[:100]}')
        remove_line(cfg, line)
    if not entries:
        return 'idle'
    marker = cfg.landing / 'queue-hold'
    if marker.exists() or (cfg.landing / 'master-health.json').exists():
        if marker.exists() and not master_hold(cfg):
            return 'held'
        entries = [e for e in entries if e.name.startswith('master-fix-')]
        if not entries or not master_repair_allowed(cfg):
            return 'held'
    cancelled = _load_json(cfg.landing / 'master-cancelled.json') or {}
    for entry in entries[:]:
        if entry.name in cancelled:
            remove_line(cfg, entry.line)
            entries.remove(entry)
    if not entries:
        return 'idle'
    lock = acquire_lock(cfg.landing)
    if lock is None:
        if not state.get('lock_waited'):
            log(cfg, 'waiting: runner.lock is held by another landing train')
            state['lock_waited'] = True
        return 'busy'
    state['lock_waited'] = False
    with lock:
        if train_running(cfg):
            return 'busy'
        retry = retry_state(cfg)
        ready = [e for e in entries if retry.get(e.line, {}).get('after', 0) <= time.time()
                 and not (cfg.landing / (e.name + '-master-pending.json')).exists()]
        if not ready:
            return 'delayed'
        # A capacity refusal is not a branch failure. Give other ready groups a
        # turn before probing the same unavailable group again. Priority stays
        # stable within each set and the queue/retry budget are unchanged.
        pending = state.setdefault('capacity_pending', set())
        pending.intersection_update(e.line for e in ready)
        if all(e.line in pending for e in ready):
            pending.clear()  # a new priority-ordered capacity sweep
        ready.sort(key=lambda e: e.line in pending)
        entry = ready[0]
        selected = [entry]
        if len(ready) > 1:
            try:
                if (cfg.main / 'data/config/runners.json').exists():
                    selected = smart_batch(cfg, ready)
                elif not needs_full(cfg, entry):
                    selected += [e for e in ready[1:] if not needs_full(cfg, e)]
                elif 'FULL=1' not in entry.envs and check_pins(cfg, entry, quiet=True):
                    # A full run costs the same for one branch or several: full entries share it, capped.
                    for e in ready[1:]:
                        if len(selected) >= FULL_BATCH_MAX:
                            break
                        if ('FULL=1' not in e.envs and check_pins(cfg, e, quiet=True)
                                and needs_full(cfg, e)):
                            selected.append(e)
            except (OSError, ValueError, subprocess.SubprocessError):
                selected = [entry]  # unknown eligibility: do not batch
        outcomes = [(entry, None)]
        try:
            outcomes = process_batch(cfg, selected)
        except Exception as exc:  # a broken train must not stop the loop
            log(cfg, f'{entry.name} ERROR: {type(exc).__name__}: {str(exc)[:160]}')
        for member, result in outcomes:
            if result == 'capacity-pending':
                pending.add(member.line)
            else:
                pending.discard(member.line)
            if result in ('master-pending', 'capacity-pending', 'parked'):
                continue
            if result == 'failed':
                park_entry(cfg, member, 'failed: train refused (see queue.log)', 'failed')
                continue
            if result == 'retry' and note_retry(cfg, member):
                continue
            remove_line(cfg, member.line)
            clear_retry(cfg, member.line)
            with open(cfg.landing / 'queue-done.txt', 'a') as f:
                f.write(f'{time.strftime("%H:%M:%S")} {member.line}\n')
            if result == 'landed':
                log(cfg, f'{member.name} LANDED: batch member verified on origin/master')
        return f'ran:{entry.name}'


INCIDENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$')
QC_ACTIONS = ('qc-requeue', 'qc-rollback-requeue')


def _qc_outcome(outcome, incident_id, **fields):
    row = {'outcome': outcome, 'action_kind': 'requeue_transient', 'incident_id': incident_id, 'train_launched': False}
    row.update(fields)
    return row


def _queue_text(cfg):
    try:
        return cfg.queue.read_text(errors='replace')
    except FileNotFoundError:
        return ''


def _queue_lines(cfg):
    text = _queue_text(cfg)
    return text.splitlines() if text else []


def _write_queue_lines(cfg, lines):
    tmp = cfg.landing / 'queue.tmp'
    tmp.write_text(''.join(item + '\n' for item in lines))
    tmp.replace(cfg.queue)


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(path)


def _qc_paths(cfg, incident_id):
    root = cfg.landing / 'qc-requeue'
    return root / f'{incident_id}.intent', root / f'{incident_id}.json'


def _load_json(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _retry_n(record):
    if not record:
        return 0
    n = record.get('n', 0)
    return n if type(n) is int else None


def _retry_after(record):
    if not record:
        return 0
    after = record.get('after', 0)
    return after if type(after) in (int, float) and after == after else None


def _activity(cfg):
    """active_train when a train or legacy runner is visible. An unreadable process list is not 'idle'."""
    try:
        if train_running(cfg) or old_runner_alive(cfg):
            return 'active_train'
    except OSError:
        return 'process_list_unavailable'
    return None


def _gave_up(cfg, name):
    """True when this train already hit the runner cap. Missing log is not exhaustion; an unreadable log is."""
    path = cfg.landing / 'queue.log'
    try:
        text = path.read_text(errors='replace')
    except FileNotFoundError:
        return False
    except OSError:
        return None
    needle = f'{name} refused '
    return any(needle in line and 'gave up and dropped the line' in line for line in text.splitlines())


def _push_block(cfg, shas):
    """None when the existing do-not-push check allows the pins. A missing list is that check's own skip."""
    import push_check
    skipped = []

    def _out(msg):
        skipped.append(msg)

    try:
        push_check.check(cfg.main, list(shas), cfg.main / 'data/team/do-not-push.txt', out=_out)
    except ValueError as exc:
        return str(exc)
    return 'skipped' if skipped else None


def _refs(text, present, retry):
    return {'queue_sha256': _sha256(text), 'line_present': present, 'retry': retry}


def queue_lock(cfg):
    """The queue file lock remove_line already uses. QC never takes the runner lock and never starts a train."""
    from contextlib import contextmanager

    @contextmanager
    def _held():
        with open(cfg.landing / 'queue.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    return _held()


def _parsed_name(item):
    try:
        parsed = parse_line(item)
    except ValueError:
        return None
    return None if parsed is None else parsed.name


def qc_requeue(cfg, line, failure_text, incident_id, *, expected_name=None):
    """Restore one dropped transient queue line under queue.lock, or refuse.

    Keeps the existing retry count, refuses a future retry, a hold, a live train, a moved pin,
    a mixed guard failure, an exhausted cap and a do-not-push hit. One restoration per incident.
    """
    if not isinstance(incident_id, str) or not INCIDENT_RE.fullmatch(incident_id):
        return _qc_outcome('blocked', str(incident_id)[:80], reason='bad_incident_id')
    if not isinstance(line, str):
        return _qc_outcome('blocked', incident_id, reason='bad_queue_line')
    line = line.strip()
    if not line or '\n' in line or '\r' in line:
        return _qc_outcome('blocked', incident_id, reason='bad_queue_line')
    if not isinstance(failure_text, str):
        return _qc_outcome('blocked', incident_id, reason='failure_output_missing')
    intent_path, receipt_path = _qc_paths(cfg, incident_id)
    with queue_lock(cfg):
        receipt = _load_json(receipt_path) if receipt_path.exists() else None
        if receipt and receipt.get('status') in ('applied', 'rolled_back'):
            text = _queue_text(cfg)
            return _qc_outcome('already_satisfied', incident_id, reason='one_restoration',
                               verification='this incident already restored one queue entry',
                               before=_refs(text, line in _queue_lines(cfg), retry_state(cfg).get(line)),
                               after=_refs(text, line in _queue_lines(cfg), retry_state(cfg).get(line)))
        if intent_path.exists() and receipt is None:
            return _qc_outcome('unknown', incident_id, reason='intent_without_receipt',
                               verification='inspect the queue before another restoration')
        try:
            entry = parse_line(line)
        except ValueError:
            return _qc_outcome('blocked', incident_id, reason='bad_queue_line')
        if entry is None:
            return _qc_outcome('blocked', incident_id, reason='bad_queue_line')
        if expected_name is not None and expected_name != entry.name:
            return _qc_outcome('blocked', incident_id, reason='changed_identity')
        if not is_transient(failure_text):
            return _qc_outcome('blocked', incident_id, reason='not_transient')
        if (cfg.landing / 'queue-hold').exists():
            return _qc_outcome('blocked', incident_id, reason='hold')
        activity = _activity(cfg)
        if activity:
            return _qc_outcome('blocked', incident_id, reason=activity)
        pins = check_pins(cfg, entry, quiet=True)
        if not pins:
            return _qc_outcome('blocked', incident_id, reason='changed_pin')
        push = _push_block(cfg, [sha for _, sha in pins])
        if push not in (None, 'skipped'):
            return _qc_outcome('blocked', incident_id, reason='push_not_authorized', verification=push)
        gave_up = _gave_up(cfg, entry.name)
        if gave_up is None:
            return _qc_outcome('blocked', incident_id, reason='queue_log_unreadable')
        if gave_up:
            return _qc_outcome('blocked', incident_id, reason='budget_exhausted')
        data = retry_state(cfg)
        previous = data.get(line)
        if previous is not None and not isinstance(previous, dict):
            return _qc_outcome('blocked', incident_id, reason='unknown_retry_state')
        n, after = _retry_n(previous), _retry_after(previous)
        if n is None or after is None:
            return _qc_outcome('blocked', incident_id, reason='unknown_retry_state')
        if n > RETRY_CAP:
            return _qc_outcome('blocked', incident_id, reason='budget_exhausted')
        lines = _queue_lines(cfg)
        same = [item for item in lines if item.strip() == line or _parsed_name(item) == entry.name]
        exact = [item for item in lines if item.strip() == line]
        before_text = _queue_text(cfg)
        before = _refs(before_text, bool(exact), previous)
        if len(exact) > 1 or len(same) > 1:
            return _qc_outcome('blocked', incident_id, reason='not_exactly_one', before=before, after=before)
        if exact and after > time.time():
            return _qc_outcome('already_satisfied', incident_id, reason='retry_scheduled',
                               verification='recovery already in progress; queue left unchanged',
                               before=before, after=before)
        if exact:
            return _qc_outcome('already_satisfied', incident_id, reason='already_pending',
                               verification='the same pinned entry is already pending',
                               before=before, after=before)
        if same:
            return _qc_outcome('blocked', incident_id, reason='changed_identity', before=before, after=before)
        written = {'n': n, 'after': 0} if previous else None
        _atomic_json(intent_path, {'incident_id': incident_id, 'line': line, 'phase': 'intent'})
        _write_queue_lines(cfg, lines + [line])
        if written:
            data[line] = {'n': n, 'after': 0}
            save_retry_state(cfg, data)
        after_lines = _queue_lines(cfg)
        after_exact = [item for item in after_lines if item.strip() == line]
        after_same = [item for item in after_lines if item.strip() == line or _parsed_name(item) == entry.name]
        if after_exact != [line] or len(after_same) != 1:
            _write_queue_lines(cfg, lines)
            if previous is None:
                data.pop(line, None)
            else:
                data[line] = previous
            save_retry_state(cfg, data)
            intent_path.unlink(missing_ok=True)
            return _qc_outcome('blocked', incident_id, reason='not_exactly_one', before=before,
                               after=_refs(_queue_text(cfg), line in _queue_lines(cfg), retry_state(cfg).get(line)))
        after_text = _queue_text(cfg)
        receipt = {'status': 'applied', 'incident_id': incident_id, 'added_line': line,
                   'before_retry': previous, 'written_retry': retry_state(cfg).get(line),
                   'push_check': 'skipped' if push == 'skipped' else 'clean'}
        _atomic_json(receipt_path, receipt)
        log(cfg, f'{entry.name} QC requeue restored one pending entry for {incident_id} '
                 f'(retry count {n}; train not started)')
        return _qc_outcome('applied', incident_id, reason='restored',
                           verification='one pending entry; retry count kept; train not started; '
                                        f'push check {receipt["push_check"]}',
                           before=before, after=_refs(after_text, True, retry_state(cfg).get(line)))


def qc_rollback_requeue(cfg, incident_id):
    """Remove only this incident's pending line when it and its retry record are still the QC write."""
    if not isinstance(incident_id, str) or not INCIDENT_RE.fullmatch(incident_id):
        return _qc_outcome('blocked', str(incident_id)[:80], reason='bad_incident_id')
    _, receipt_path = _qc_paths(cfg, incident_id)
    with queue_lock(cfg):
        receipt = _load_json(receipt_path) if receipt_path.exists() else None
        if not receipt:
            return _qc_outcome('blocked', incident_id, reason='no_receipt')
        if receipt.get('status') == 'rolled_back':
            text = _queue_text(cfg)
            line = receipt.get('added_line', '')
            return _qc_outcome('already_satisfied', incident_id, reason='already_rolled_back',
                               before=_refs(text, line in _queue_lines(cfg), retry_state(cfg).get(line)),
                               after=_refs(text, line in _queue_lines(cfg), retry_state(cfg).get(line)))
        if receipt.get('status') != 'applied' or not isinstance(receipt.get('added_line'), str):
            return _qc_outcome('unknown', incident_id, reason='receipt_not_applied')
        line = receipt['added_line']
        activity = _activity(cfg)
        if activity:
            return _qc_outcome('blocked', incident_id, reason='claimed' if activity == 'active_train' else activity)
        lines = _queue_lines(cfg)
        if lines.count(line) != 1:
            return _qc_outcome('blocked', incident_id, reason='changed',
                               verification='the QC line is no longer the single unchanged pending entry')
        current = retry_state(cfg).get(line)
        if current != receipt.get('written_retry'):
            return _qc_outcome('blocked', incident_id, reason='claimed',
                               verification='retry state no longer matches the QC write')
        before_text = _queue_text(cfg)
        before = _refs(before_text, True, current)
        kept = []
        removed = False
        for item in lines:
            if not removed and item == line:
                removed = True
                continue
            kept.append(item)
        _write_queue_lines(cfg, kept)
        data = retry_state(cfg)
        previous = receipt.get('before_retry')
        if previous is None:
            data.pop(line, None)
        else:
            data[line] = previous
        save_retry_state(cfg, data)
        receipt['status'] = 'rolled_back'
        _atomic_json(receipt_path, receipt)
        after_text = _queue_text(cfg)
        log(cfg, f'QC rollback removed the unchanged pending entry for {incident_id}')
        return _qc_outcome('applied', incident_id, reason='rolled_back',
                           verification='removed only the QC pending entry; other queue rows kept',
                           before=before, after=_refs(after_text, line in _queue_lines(cfg), retry_state(cfg).get(line)))


def _qc_main(args):
    if not args.landing or not args.main:
        print(json.dumps(_qc_outcome('blocked', args.incident or 'none', reason='landing_and_main_required')))
        return 2
    cfg = Config(main=Path(args.main), landing=args.landing, procs=all_commands, load=load_1min)
    if args.qc_action == 'qc-requeue':
        if not args.line_file or not args.failure_file or not args.incident:
            print(json.dumps(_qc_outcome('blocked', args.incident or 'none', reason='arguments_required')))
            return 2
        try:
            line = Path(args.line_file).read_text(errors='replace')
            failure = Path(args.failure_file).read_text(errors='replace')
        except OSError as exc:
            print(json.dumps(_qc_outcome('blocked', args.incident, reason='unreadable_input', verification=type(exc).__name__)))
            return 2
        outcome = qc_requeue(cfg, line, failure, args.incident, expected_name=args.name)
    else:
        if not args.incident:
            print(json.dumps(_qc_outcome('blocked', 'none', reason='arguments_required')))
            return 2
        outcome = qc_rollback_requeue(cfg, args.incident)
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome['outcome'] in ('applied', 'already_satisfied') else 2


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--once', action='store_true', help='one pass, then exit')
    ap.add_argument('--landing', help='queue folder (default data/team/landing of the main checkout)')
    ap.add_argument('--main', help='checkout root used by qc-requeue to verify pins and do-not-push')
    ap.add_argument('--qc-action', choices=QC_ACTIONS, help='queue repair; does not start a train or the runner loop')
    ap.add_argument('--line-file')
    ap.add_argument('--failure-file')
    ap.add_argument('--incident')
    ap.add_argument('--name', help='train name that must match the queue line')
    args = ap.parse_args(argv)
    if args.qc_action:
        return _qc_main(args)
    cfg = Config(landing=args.landing)
    lock = acquire_lock(cfg.landing, 'runner-instance.lock')
    if lock is None:
        print('another landing runner holds the lock; exiting')
        return 0
    with lock:
        log(cfg, f'landing runner started (pid {os.getpid()}, main {cfg.main}, script {Path(__file__).resolve()})')
        state = {}
        while True:
            hold(cfg)
            result = cycle(cfg, state)
            if args.once:
                print(result)
                return 0
            time.sleep(1 if result.startswith('ran:') else cfg.poll)


if __name__ == '__main__':
    sys.exit(main())
