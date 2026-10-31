"""Feature-queue planning inside the scheduled dispatcher's existing delivery lock.

No second dispatcher or transport: prepare supplies its plan and observes receipts.
Absent feature-queue.json retains the dispatcher's bounded legacy behavior.
"""

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from companion import board_inbox, gate_lock
from companion.feature_team import contract, dispatch, events, picker, repos, runs
from companion.feature_team.completion import COMPLETED_STATUSES
from companion.feature_team.dag import Dag, live_path, load

ROOT = Path(__file__).resolve().parents[3]


HOSTED_KINDS = frozenset({'claude', 'codex', 'codex-exec', 'cursor', 'grok', 'muse'})
LANDING_LOCAL_KINDS = frozenset()  # hosted kinds a train's landing gate still holds; muse left 2026-10-17 (the owner's yes;
# muse is a hosted model and back-to-back trains starved it)
FABLE_CHANNEL = 'claude:fable'


def fable_closed(data, now):
    """The reason the Fable window is closed, or None while it is open: lane_send.fable_window over
    data/team/config.json, the gate check_claude_worker applies at send time, so no plan picks a paused Fable."""
    from companion.feature_team import node_worker

    try:  # fail closed, as lane_send.check_claude_worker and muse.enabled do: an unreadable config.json or a
        # lane_send that will not load pauses Fable and never crashes the tick (or the watchdog's silent-node ALERT)
        if node_worker._RULES_LANE is None:
            node_worker._RULES_LANE = node_worker._load_lane_send()
        is_open, why = node_worker._RULES_LANE.fable_window(dispatch.read(data / 'team/config.json', {}),
                                                            data / 'team', now)
    except Exception as exc:
        return f'config.json unreadable: {type(exc).__name__}'
    return None if is_open else why


def capacity_refusals(data, channel=None):
    """Reuse existing power/heavy-job probes, RAM probe and GPU/gate reservations. A hosted channel (``channel``
    of a kind in HOSTED_KINDS) uses no local CPU or GPU, so low_power's heavy-job and load checks are skipped for
    it (2026-10-15: builders' pytest kept the loop from ever dispatching); the gate, RAM and reservation checks,
    the budget pauses and the Fable window still apply. Nor do the gpu gate and local-models.lock apply to it
    (2026-10-16: a local 7B replay holding gate-gpu.lock refused a claude:opus run), nor the gate-landing locks a train holds, except for muse (owner,
    2026-10-16); the single gate.lock still refuses every channel."""
    def memory_free_percent():
        # Public build stand-in for the private home router's probe (same code): unknown is not permission.
        try:
            output = subprocess.run(['/usr/bin/memory_pressure'], capture_output=True, text=True,
                                    timeout=5, check=True).stdout
            match = re.search(r'System-wide memory free percentage:\s*(\d+)%', output)
            return int(match[1]) if match else None
        except (OSError, subprocess.SubprocessError):
            return None

    kind = channel.partition(':')[0] if channel is not None else None
    hosted = kind in HOSTED_KINDS
    reasons = []
    for path in gate_lock.conflicting(data / 'team/gate.lock'):  # the single lock conflicts with every kind
        if hosted and path.name == 'gate-gpu.lock':
            continue
        # owner, 2026-10-16: a train's gate-landing locks do not hold back a cloud channel; local:*
        # still waits (and any kind in LANDING_LOCAL_KINDS). The single gate.lock still refuses every channel.
        if hosted and kind not in LANDING_LOCAL_KINDS and path.name.startswith('gate-landing'):
            continue
        info = gate_lock.read(path)
        if info is not None and gate_lock.live(info):
            reasons.append('landing gate held' if path.name != 'gate-gpu.lock' else 'gpu gate held')
            break
    # Do not use holder(): even a dry-run must leave stale lock files untouched.
    free = memory_free_percent()
    if free is None or free < 30:
        reasons.append('RAM unavailable' if free is None else f'RAM free {free}% below 30%')
    mode = dispatch.read(data / 'team/teams.json', {}).get('rules', {}).get('work_mode', {})
    if mode.get('current') == 'low_ram':
        reasons.append('RAM policy is low_ram')
    if hosted:
        return reasons
    try:
        with (data / 'team/local-models.lock').open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except FileNotFoundError:
        pass
    except OSError:
        reasons.append('GPU/local-model reservation busy or unreadable')
    try:
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/low_power.py'), 'status', '--json'],
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'KYRA_DATA_DIR': str(data)})
        state = json.loads(done.stdout)
        if done.returncode or state.get('would_run') is not True:
            reasons.append('low_power refused: ' + json.dumps({
                key: state.get(key) for key in ('battery', 'on_ac', 'thermal', 'heavy_jobs', 'browser_phase')}))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        reasons.append('low_power readiness unavailable')
    return reasons


def ready(feature):
    state = feature['state']
    return [node for node, spec in feature['dag']._nodes.items()
            if state.get(node, {}).get('status') in (None, 'queued')
            and state.get(node, {}).get('delivery_status') not in {'handoff_pending', 'handoff_printed'}
            and node not in feature.get('blocked', set())
            and node in feature.get('resume_ready', feature['dag']._nodes)
            and all(dep in feature.get('verified_inputs', set()) for dep in spec['inputs'])]


def resumed(data, features, now):
    """Project R0 readiness and reservations; X0 proof is required for every input.

    Read completion from this data root: X0's current verified() helper reads the
    global CompletionMap even when given data_dir, so cannot be used on scratch.
    """
    store = runs.Runs(data / 'team/loop')
    records = store.load_all()
    for feature in features:
        state = feature['state']
        verified, waiting = set(), {}
        for node, row in state.items():
            if dispatch.gate_closed(feature['dag']._nodes.get(node), row):
                verified.add(node)  # closed by done --gate: nothing to verify or land
                continue
            by = str(row.get('by') or '')
            rec = records.get(by.removeprefix('verify:')) if by.startswith('verify:') else None
            if (row.get('status') != 'done' or not row.get('sha') or rec is None
                    or (rec['feature'], rec['node']) != (feature['name'], node)
                    or rec['state'] not in runs.VERIFIED or rec.get('sha') != row['sha']):
                continue
            try:
                if not Path(rec['verify']['output']).read_text().rstrip().endswith(f"VERIFY PASS {row['sha']}"):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            # Verified in its worktree is not landed: dependents wait until the outputs are on master.
            missing = repos.unlanded(data, feature['name'], rec.get('artifacts') or [])
            if missing:
                waiting[node] = f"waiting for {node} to land: {', '.join(missing)} not on master yet"
            else:
                verified.add(node)
        view = store.resume(feature['name'], feature['dag']._nodes, state, now=now)
        feature['resume_ready'] = set(view['ready'])
        feature['resume_escalated'] = set(view['escalated'])  # the skip reason the tick records for the feature
        feature['verified_inputs'] = verified
        feature['waiting_inputs'] = waiting
        feature['released'] = {}
        authors = {w['name']: w['channel'] for w in feature['team']}
        for row in state.values():
            if row.get('status') not in {'done', 'failed'}:
                continue
            by = str(row.get('by') or '')
            rec = records.get(by.removeprefix('verify:')) if by.startswith('verify:') else None
            channel = rec['brief']['channel'] if rec else (row.get('channel') or authors.get(by))
            try:
                stamp = events.timestamp(row.get('t'))
            except ValueError:
                stamp = None
            if channel and stamp is not None and 0 < stamp <= now:
                feature['released'][channel] = max(feature['released'].get(channel, 0), stamp)
        # A dispatched completion row stops holding its author's channel once the node's latest run ended after it
        feature['stale_holds'] = dispatch.stale_holds(records, feature['name'], state, runs_dir=store.runs_dir)
        # Recovery belongs to the run/watchdog owner, never blindly resend a timeout.
        for run_id in view['running'] + view['redispatch']:
            rec = records[run_id]
            # A node done in completion.json is not reopened by a leftover live record (medic 2026-10-17: a
            # 2026-10-16 timeout record of the done auto-system:R14 held muse:0 busy for a day)
            if state.get(rec['node'], {}).get('status') == 'done':
                continue
            state[rec['node']] = {**state.get(rec['node'], {}), 'status': 'dispatched',
                                  'by': rec['brief']['channel'], 'channel': rec['brief']['channel']}
    return features


def queued(features):
    """Project DAG readiness, including nodes not yet written to a role queue."""
    for feature in features:
        # A3's printed Claude handoff remains reserved until the Board reconciles it. A completed
        # row never regresses: its stale handoff flag is ignored (2026-10-20: done MS4 read as
        # dispatched, so MS11 never became ready and its author's seat read busy everywhere).
        for row in feature['state'].values():
            if row.get('status') in COMPLETED_STATUSES:
                continue
            if row.get('delivery_status') in {'handoff_pending', 'handoff_printed'}:
                row['status'] = 'dispatched'
        for role in ('engineer', 'intern'):
            # Persisted role queues must not bypass resume or verified-input gates.
            available = ready(feature)
            existing = feature['queues'][role] = [row for row in feature['queues'][role]
                                                  if row['node'] in available]
            known = {row['node'] for row in existing}
            for i, node in enumerate(ready(feature)):
                target = 'intern' if feature['dag'].node(node)['intern_friendly'] else 'engineer'
                if target == role and node not in known:
                    existing.append(dict(node=node, priority=i, t=0))
    return features


def roster_table(table, features):
    table = list(table)
    known = {row['channel'] for row in table}
    for feature in features:
        for worker in feature['team']:
            if worker['channel'] not in known:
                family = ('claude-' + worker['name'].split()[-1].lower()
                          if worker['name'].startswith('Claude ') else worker['channel'].partition(':')[0])
                table.append(dict(channel=worker['channel'], role=worker['role'], family=family))
                known.add(worker['channel'])
    return table


def select(table, lanes, features, pending, after=None):
    """One slot per feature per round; queue order breaks ties on the first round."""
    reservations = dict(pending)
    plan = []
    names = [f['name'] for f in features]
    if after in names:
        offset = names.index(after) + 1
        features = features[offset:] + features[:offset]
    # Existing select owns role, channel, busy, dependency, M1 and idle checks.
    # Hide other queues while retaining all their busy workers and reservations.
    while True:
        count = len(plan)
        for feature in features:
            visible = [f if f is feature else {**f, 'queues': {'engineer': [], 'intern': []}}
                       for f in features]
            if batch := dispatch.select(table, lanes, visible, reservations):
                item = batch[0]
                worker = next(w for w in feature['team'] if w['name'] == item['by'])
                item['effort'] = worker.get('effort', 'medium')
                reservations[f"{item['feature']}:{item['node']}"] = item
                plan.append(item)
        if len(plan) == count:
            break
    return plan


class SlotTiming:
    """Durable per-channel clocks, under the dispatcher's existing tick lock.

    Unknown release times start at the first idle observation. Ready intervals
    are sampled each tick and at receipts; no time before observation is invented.
    """

    def __init__(self, data, dry_run):
        self.path = data / 'team/autopilot-state.json'
        self.state = dispatch.read(self.path, {'after': None, 'slots': {}})
        self.events = events.EventStore(data)
        self.dry_run = dry_run

    def save(self):
        if not self.dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            dispatch.save(self.path, self.state)

    def emit(self, now, channel, kind, detail, item=None):
        if self.dry_run:
            return
        item = item or {}
        self.events.append(dict(t=now, run_id=item.get('run_id'), feature=item.get('feature'),
                                node=item.get('node'), worker=item.get('by', 'dispatcher'),
                                channel=channel, kind=kind, sha=None, path=None,
                                detail=json.dumps(detail, separators=(',', ':'))))

    def flush(self, now):
        for channel, slot in self.state['slots'].items():
            since = slot.get('idle_since')
            if since is not None:
                if now > since:
                    self.emit(now, channel, 'idle', {'seconds': now - since})
                slot['idle_since'] = now

    def observe(self, table, lanes, features, pending, excluded, now):
        self.flush(now)
        busy = set(excluded) | {row['channel'] for row in pending.values()}
        for feature in features:
            authors = {w['name']: w['channel'] for w in feature['team']}
            busy.update(dispatch.holder_channel(row, authors) for node, row in feature['state'].items()
                        if row.get('status') == 'dispatched' and node not in feature.get('stale_holds', ()))
        states = dispatch.lane_states(lanes)
        free = {row['channel'] for row in table
                if dispatch.role(row['role']) in {'engineer', 'intern'}
                and dispatch.allowed(row['channel']) and row['channel'] not in busy
                and dispatch.observed_state(states, row['channel']) == 'idle'}
        has_ready = any(f"{f['name']}:{node}" not in pending for f in features for node in ready(f))
        self.state['ready_since'] = (self.state.get('ready_since') or now) if has_ready else None
        released = {}
        for feature in features:
            for channel, stamp in feature.get('released', {}).items():
                released[channel] = max(released.get(channel, 0), stamp)
        slots = self.state['slots']
        for channel in list(slots):
            if channel not in free:
                del slots[channel]
        for channel in sorted(free):
            if channel not in slots:
                stamp = released.get(channel, now)
                if stamp < self.state.get('busy', {}).get(channel, 0):
                    stamp = now  # An old completion cannot time a newer release.
                slots[channel] = {'free_since': stamp, 'idle_since': (
                    max(stamp, self.state['ready_since']) if has_ready else None)}
            else:
                slots[channel]['idle_since'] = now if has_ready else None
        self.state['busy'] = {channel: now for channel in busy}
        self.save()

    def receipt(self, item, started, now):
        self.flush(now)
        if started:
            slot = self.state['slots'].pop(item['channel'], None)
            if slot is not None:
                # Record measured zero too, without emitting a zero on every replan.
                if slot.get('idle_since') is not None:
                    self.emit(now, item['channel'], 'idle', {'seconds': 0})
                identity = {**item, 'run_id': item.get('run_id') or contract.new_run_id(
                    item['feature'], item['node'], clock=lambda: now)}
                self.emit(now, item['channel'], 'refill',
                          {'refill_seconds': max(0, now - slot['free_since'])}, identity)
        # Advance on an attempt, including a refusal, so one failing feature cannot starve another.
        self.state['after'] = item['feature']
        self.save()


PLAN_INPUT_CHARS = 24000  # each queue-entry input is inlined into the PLAN node up to this many characters


def _plan_input(item, data):
    """A queue entry's plan input: a data/ path or an absolute path, read here and inlined (the planner's channel
    cannot reach data/); anything outside the data dir is refused."""
    path = Path(str(item))
    if path.is_absolute():
        resolved = path.resolve()
    elif path.parts and path.parts[0] == 'data':
        resolved = (data / Path(*path.parts[1:])).resolve()
    else:
        resolved = (data / path).resolve()
    if not resolved.is_relative_to(Path(data).resolve()):
        raise ValueError(f'inputs: plan input must stay under the data dir: {item}')
    return resolved.read_text(encoding='utf-8')[:PLAN_INPUT_CHARS]


def planner_channel(entry):
    """The channel a dag-less feature's planner would use (its picker roster row), or None when unknown, so the
    capacity check can skip low_power for a hosted planner before the feature exists."""
    if entry.get('dag') is not None:
        return None
    row = picker.ROSTER.get(entry.get('planner') or 'Claude Opus')
    return row[1] if row else None


def plan_document(entry, data=None):
    """The one-node DAG of a dag-less feature, from the queue entry as it is now: the planner as owner hint, the
    worktree-rooted output files (the harvest publishes them), the note and the entry's inputs inlined."""
    inputs = entry.get('inputs') or []
    if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
        raise ValueError('inputs: a queue entry lists plan inputs as path strings')
    if inputs and data is None:
        raise ValueError('inputs: plan inputs need the data dir to be inlined')
    inlined = ''.join(
        f'\nInput {item} L{n}: {clean[0]}'
        for item in inputs
        for n, clean in ((n, contract.acceptance_items(line))
                         for n, line in enumerate(_plan_input(item, data).splitlines(), 1))
        if clean)  # one acceptance line per input line: the contract takes single printable lines
    return {'nodes': [dict(
        id='PLAN', title='Write the plan', inputs=[], intern_friendly=False, tier='T1',
        owner_hint=entry.get('planner') or 'Claude Opus', kind='plan',
        files=[f"NEW PLAN-{entry['name']}.md", f"NEW {entry['name']}-proposed.dag.json"],
        acceptance=("Write the plan from the Board's feature note "
                    f"(feature {entry['name']}; the note and any inputs are inlined in the brief, data/ is not reachable). "
                    f"Feature note: {' '.join(contract.acceptance_items(entry.get('note', '')))}. "
                    "Follow referenced source notes for the owner's words; if absent, record that gap "
                    "instead of inventing a request. Preserve their approval requirements. "
                    f"Write PLAN-{entry['name']}.md and {entry['name']}-proposed.dag.json at the root of your worktree "
                    "and list both as the result's artifacts; the harvest publishes them for the Board."
                    + inlined))]}


def candidate(data, entry):
    name = entry['name']
    canonical = live_path(data / 'team/feature-teams', name)
    if entry.get('dag') is None:
        document = dispatch.read(canonical, plan_document(entry, data))
        dag = Dag(document['nodes'])
    else:
        source = Path(entry['dag'])
        if not source.is_absolute():
            source = data / (source.relative_to('data') if source.parts[0] == 'data' else source)
        if not source.resolve().is_relative_to(data.resolve()):
            raise ValueError('queued DAG must stay inside the data directory')
        dag = load(source)
        document = json.loads(source.read_text())
    # The picker is the policy authority. Its existing API persists, so preview in scratch only.
    with tempfile.TemporaryDirectory(prefix='kyra-autopilot-pick-') as scratch:
        team = picker.pick_team(name, ['plan'] if entry.get('dag') is None else [],
                                rollback_path=data / 'team/rollback.json', model_fit_path=ROOT / 'docs/model-fit.md',
                                effort_policy_path=data / 'team/effort-policy.json', out_dir=Path(scratch),
                                max_engineers=5, n_engineers=5,
                                task_kind='plan' if entry.get('dag') is None else 'build')
    if entry.get('dag') is None:
        # The queue entry may name the planner ("planner": a picker roster name, e.g. "Claude Fable" for the site
        # plan per site-spec-2026-10-15); the default stays Claude Opus. The channel gates (fable window, budget)
        # still apply at send time.
        planner = entry.get('planner') or 'Claude Opus'
        if not isinstance(planner, str) or planner not in picker.ROSTER:
            raise ValueError(f'planner {planner!r} is not a picker roster name')
        team = [w for w in team if w['name'] == planner]
        if not team:
            raise ValueError(f'{planner} planner unavailable in picker roster')
    return dict(name=name, dag=dag, team=team, state={}, queues={'engineer': [], 'intern': []}), document


def initialize(data, feature, document):
    """Validate with CLI init in scratch; publish team.json last as the start marker."""
    name = feature['name']
    dag_path = live_path(data / 'team/feature-teams', name)
    folder = data / 'team/features' / name
    for path in (dag_path, folder / 'team.json'):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('autopilot paths must not contain symlinks')
    with tempfile.TemporaryDirectory(prefix='kyra-autopilot-init-') as scratch:
        staged = Path(scratch)
        staged_dag = staged / dag_path.relative_to(data)
        staged_folder = staged / folder.relative_to(data)
        staged_dag.parent.mkdir(parents=True)
        staged_folder.mkdir(parents=True)
        dispatch.save(staged_dag, document)
        dispatch.save(staged_folder / 'team.json', feature['team'])
        subprocess.run([sys.executable, str(ROOT / 'scripts/feature_team.py'), 'init', name],
                       check=True, capture_output=True, text=True, timeout=30,
                       env={**os.environ, 'KYRA_DATA_DIR': str(staged)})
        completion = json.loads((staged_folder / 'completion.json').read_text())
        for node, row in completion.get('nodes', completion).items():
            if row.get('status') == 'done':
                raise ValueError(f'staged completion row {node}: only verify may write done')
    with dispatch.locked(folder / 'dispatch.lock'):
        if any((folder / p).exists() for p in ('team.json', 'completion.json')):
            raise ValueError('feature already initialized; existing state preserved')
        dag_path.parent.mkdir(parents=True, exist_ok=True)
        if not dag_path.exists():
            dispatch.save(dag_path, document)
        elif json.loads(dag_path.read_text()) != document:
            raise ValueError('queued DAG conflicts with canonical feature DAG')
        dispatch.save(folder / 'completion.json', completion)
        dispatch.save(folder / 'team.json', feature['team'])


def prepare(data, features, table, lanes, journal, sender, plan_next, limit, *, clock, dry_run, excluded):
    """One integration call; keep the dispatcher's journal, receipt and claim path."""
    path = data / 'team/feature-queue.json'
    if not path.exists():
        return features, table, plan_next, sender, limit, {}
    result = dict(starts=[], slots=[], refusals=[], skipped=[], next_feature=None)
    seen = set()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None

    def record(event, **fields):
        row = dict(event=event, **fields)
        key = json.dumps(row, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        if event == 'refused':
            result['refusals'].append(fields)
        if event == 'skipped':
            result['skipped'].append(fields)
        if not dry_run:
            data.joinpath('team').mkdir(parents=True, exist_ok=True)
            with (data / 'team/autopilot.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(t=clock(), **row)) + '\n')
            if event == 'feature_started':
                board_inbox.append(dict(t=clock(), **row, kind=event, **{
                    'from': 'autopilot', 'text': f"Feature {fields['feature']} initialized"}),
                    path=data / 'team/board-inbox.jsonl')

    try:
        entries = json.loads(path.read_text())['features']
        if (not isinstance(entries, list)
                or any(not isinstance(e, dict) or not isinstance(e.get('name'), str)
                       or not re.fullmatch(r'[A-Za-z0-9_-]+', e['name']) for e in entries)
                or len({e['name'] for e in entries}) != len(entries)):
            raise ValueError('invalid or duplicate feature queue names')
    except (OSError, ValueError, KeyError, TypeError):
        record('refused', reason='invalid feature-queue.json')
        return features, table, lambda excluded: [], sender, 0, result
    order = {e['name']: i for i, e in enumerate(entries)}
    timing = SlotTiming(data, dry_run)
    # The queue's change time, once per change: M0/FL time a refill from when work became available, and a feature
    # The owner queues is new work even before a slot takes it (window 4 counted 38 empty-queue minutes as a refill).
    # Stat before the read (an edit after it gets its own row next tick); the caller's dispatcher.lock serializes
    # the compare, the row and the state write, and the state is saved at once so a later failure cannot repeat it.
    if mtime is not None and timing.state.get('queue_mtime') != mtime:
        record('queue_changed', mtime=mtime)
        timing.state['queue_mtime'] = mtime
        timing.save()
    routed_channels = set(excluded)

    def current():
        rows = resumed(data, dispatch.snapshot(data), clock())
        names = {f['name'] for f in rows}
        incomplete = []
        for entry in entries:
            folder = data / 'team/features' / entry['name']
            if entry['name'] not in names and (folder / 'completion.json').exists():
                reason = 'existing feature state lacks a readable DAG or team.json; Board must reconcile'
                record('refused', feature=entry['name'], reason=reason)
                incomplete.append(reason)
        # A dag-less feature's PLAN node is regenerated from the current plan_document and queue entry while
        # none of its nodes has started (files, inputs, kind, planner): the site and 3D DAGs were written at init
        # under the old plan_document with a data/ files list the channel cannot write, and the entry's inputs
        # never reached the brief (the renderer takes an existing feature's node from the DAG on disk). A feature
        # with a dispatched or done node keeps its DAG. A refresh that fails (an unreadable input, a DAG that
        # cannot be read) holds that feature back this tick instead of dispatching the stale node into the preflight.
        held = set()
        for entry in entries:
            row = next((f for f in rows if f['name'] == entry['name']), None)
            if entry.get('dag') is not None or row is None:
                continue
            if any(r.get('status') not in (None, 'queued') for r in row['state'].values()):
                continue
            dag_path = live_path(data / "team/feature-teams", entry["name"])
            try:
                document = plan_document(entry, data)
                stored = json.loads(dag_path.read_text())
            except (OSError, ValueError, KeyError, TypeError) as exc:
                record('refused', feature=entry['name'], reason=f'plan refresh refused: {type(exc).__name__}: {exc}')
                held.add(entry['name'])
                continue
            if stored == document or [n.get('id') for n in stored.get('nodes', [])] != ['PLAN']:
                continue  # unchanged, or a DAG the Board gave the feature some other way: left as written
            row['dag'] = Dag(document['nodes'])
            if not dry_run:
                dispatch.save(dag_path, document)
            record('plan_refreshed', feature=entry['name'], node='PLAN')
        rows = [f for f in rows if f['name'] not in held]
        # A queue entry's planner applies to a feature none of whose nodes has started: its one-worker team is
        # rebuilt from the entry before the first dispatch (the site was initialised under Opus before the
        # entry named Fable). A feature with a dispatched or done node keeps its team.
        for entry in entries:
            planner = entry.get('planner')
            row = next((f for f in rows if f['name'] == entry['name']), None)
            if not planner or entry.get('dag') is not None or row is None:
                continue
            if any(r.get('status') not in (None, 'queued') for r in row['state'].values()):
                continue
            if [w['name'] for w in row['team']] == [planner]:
                continue
            try:
                fresh, _ = candidate(data, entry)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                record('refused', feature=entry['name'], reason=f'planner change refused: {type(exc).__name__}: {exc}')
                continue
            row['team'] = fresh['team']
            if not dry_run:
                dispatch.save(data / 'team/features' / entry['name'] / 'team.json', fresh['team'])
            record('planner_set', feature=entry['name'], planner=planner)
        # A dag-less feature whose plan was promoted (the live DAG is no longer the lone PLAN node) still carries
        # the one-planner team from its PLAN start; it gets the picker's build roster, planner kept first.
        # FL window 6 (2026-10-16): public-release and example-site sat on claude:opus alone while
        # runner-pool:RP7's pending handoff held it, and 11 ready nodes waited beside idle seats.
        for entry in entries:
            row = next((f for f in rows if f['name'] == entry['name']), None)
            if entry.get('dag') is not None or row is None or len(row['team']) != 1:
                continue
            if 'PLAN' in row['dag']._nodes or row['state'].get('PLAN', {}).get('status') != 'done':
                continue
            dag_path = live_path(data / 'team/feature-teams', entry['name'])
            try:
                fresh, _ = candidate(data, {**entry, 'dag': str(dag_path)})
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                record('refused', feature=entry['name'], reason=f'build roster refused: {type(exc).__name__}: {exc}')
                continue
            planner = row['team'][0]
            # The owner's rule: one Fable worker, for plan synthesis and the hardest design nodes only, never routine
            # building (review 2026-10-16: the widened roster ranked claude:fable first and planned PKG-MAP on it).
            # The extra Opus seats the dispatch table names take its place, in the Board's curated team shape.
            build = [w for w in fresh['team'] if w['channel'] != FABLE_CHANNEL]
            effort = next((w['effort'] for w in fresh['team'] if w['channel'] == 'claude:opus'), 'medium')
            build += [dict(name=f"Claude Opus {r['channel'].rsplit('-', 1)[1]}", role='Engineer',
                           channel=r['channel'], model=picker.MODEL_IDS['Claude Opus'], effort=effort,
                           rank_reason='extra Opus seat in the dispatch table')
                      for r in table if re.fullmatch(r'claude:opus-\d+', r.get('channel', ''))
                      and dispatch.role(r.get('role')) == 'engineer']
            team = [planner, *(w for w in build if w['name'] != planner['name']
                               and w['channel'] != planner['channel'])]
            if len(team) == 1:
                continue
            row['team'] = team
            if not dry_run:
                dispatch.save(data / 'team/features' / entry['name'] / 'team.json', team)
            record('team_widened', feature=entry['name'], workers=[w['name'] for w in team])
        if dry_run and projected is not None:
            rows.append(projected)
        rows.sort(key=lambda f: (f['name'] in result['starts'], order.get(f['name'], len(order))))
        _, stops = dispatch.containment(data, rows)
        for reason in stops:
            record('refused', reason=reason)
        return queued(rows), stops + incomplete

    def eligible(rows, excluded):
        modes, skipped = {}, {}
        pauses = dispatch.budget_pauses(data / 'team/model-budget.json', clock(), modes)
        ranked = picker.rank([r for r in roster_table(table, rows) if r['channel'] not in excluded],
                             rows, data / 'team/rollback.json')  # R14: rank, then budget_table last
        available = dispatch.budget_table(ranked, pauses, skipped, modes)
        if any(r['channel'] == FABLE_CHANNEL or r.get('family') == 'claude-fable' for r in available) and (
                closed := fable_closed(data, clock())):
            available = [r for r in available if r['channel'] != FABLE_CHANNEL and r.get('family') != 'claude-fable']
            skipped['claude-fable'] = f'fable is paused; {closed}'
        for family, reason in skipped.items():
            record('refused', reason=reason, family=family)
        return available

    projected = None
    features, stops = current()
    # A feature passed over because its node sits behind an escalated run says so in the event stream, next to
    # the Board's way back; the run's own escalated row alone did not explain why the queue moved on.
    for feature in features:
        for node in sorted(feature.get('resume_escalated', ())):
            record('skipped', feature=feature['name'], node=node,
                   reason=f"escalated run; feature_team.py requeue {feature['name']} {node}")
        for node, spec in feature['dag']._nodes.items():
            if feature['state'].get(node, {}).get('status') in (None, 'queued'):
                for dep in spec['inputs']:
                    if dep in feature.get('waiting_inputs', {}):
                        record('skipped', feature=feature['name'], node=node, reason=feature['waiting_inputs'][dep])
    # Reserve running work first. Unassignable ready nodes prevent starting another feature.
    initial = [] if stops else select(eligible(features, excluded), lanes, features, journal,
                                     timing.state['after'])
    reserved = {**journal, **{f"{i['feature']}:{i['node']}": i for i in initial}}
    # a node whose owner_hint no team channel can serve has a Board row (dispatch.report_hints): it must not hold
    # back the next feature
    stuck = dispatch.unservable_hints(roster_table(table, features), features)
    unassigned = [f"{f['name']}:{node}" for f in features for node in ready(f)
                  if f"{f['name']}:{node}" not in reserved and f"{f['name']}:{node}" not in stuck]
    has_ready = any(ready(f) for f in features)
    if not stops and not unassigned:
        entry = next((e for e in entries if not any(
            (data / 'team/features' / e['name'] / name).exists()
            for name in ('team.json', 'completion.json'))), None)
        if entry is not None:
            result['next_feature'] = entry['name']
            try:
                reasons = capacity_refusals(data, planner_channel(entry))
                for reason in reasons:
                    record('refused', feature=entry['name'], reason=reason)
                if not reasons:
                    new, document = candidate(data, entry)
                    proposed = queued([*features, new])
                    choices = select(eligible(proposed, excluded), lanes, proposed, reserved)
                    if not choices:
                        record('refused', feature=entry['name'], reason='no eligible free worker slot')
                    else:
                        if dry_run:
                            projected = new
                        else:
                            initialize(data, new, document)
                        result['starts'].append(entry['name'])
                        record('feature_started', feature=entry['name'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
                record('refused', feature=entry['name'], reason=f'feature init refused: {type(exc).__name__}: {exc}')

    def plan(excluded):
        rows, stops = current()
        timing.observe(roster_table(table, rows), lanes, rows, journal, routed_channels, clock())
        return [] if stops else select(eligible(rows, excluded), lanes, rows, journal, timing.state['after'])

    def observed_sender(item):
        try:
            reply = sender(item)
        except Exception:
            timing.receipt(item, False, clock())
            record('refused', feature=item['feature'], node=item['node'], reason='uncertain delivery')
            raise
        started, _ = dispatch.delivery_receipt(reply)
        # A loop-channel send answers 'QUEUED: durable run <id>': the record exists and the node is dispatched
        # (dispatch.tick treats durable as started), so the slot clock must see it as filled too, or no refill
        # event is ever emitted (none was, 2026-10-15).
        started = started or (isinstance(reply, str) and reply.startswith('QUEUED: durable run '))
        timing.receipt(item, started, clock())
        if started:
            record('slot_filled', **item)
        else:
            record('refused', feature=item['feature'], node=item['node'], reason=reply.strip())
        return reply

    def explain(rows, slots):
        """One refusal per ready node this tick leaves unassigned, naming each team channel's reason, and one
        dispatch_refused event when that reason changes (FL window 6: 11 ready nodes, no event of any kind).
        A planned slot whose channel the send-time capacity check refuses holds nothing: FL window 7 planned
        STORY-TRAIN on claude:haiku for 2311 s, every send came back 'landing gate held', and the other ready
        nodes read 'claude:haiku busy with public-release:STORY-TRAIN'. That channel is named by its refusal."""
        refused = {}
        for i in slots:
            if i['channel'] not in refused:
                refused[i['channel']] = capacity_refusals(data, i['channel'])
        slots = [i for i in slots if not refused[i['channel']]]
        taken = {f"{i['feature']}:{i['node']}" for i in slots}
        available = {r['channel'] for r in eligible(rows, excluded)}
        states = dispatch.lane_states(lanes)
        holders = {i['channel']: f"{i['feature']}:{i['node']}" for i in slots}
        for f in rows:
            authors = {w['name']: w['channel'] for w in f['team']}
            for node, r in f['state'].items():
                if r.get('status') == 'dispatched' and node not in f.get('stale_holds', ()):
                    holders.setdefault(dispatch.holder_channel(r, authors), f"{f['name']}:{node}")
        awaiting = set()  # a legacy send with no start proof: held until dispatch.UNCERTAIN_TTL, not busy
        for key, r in journal.items():
            if holders.setdefault(r.get('channel'), key) == key and ('by' in r or 'receipt' in r):
                awaiting.add(r.get('channel'))

        rows_by_role = {(r['channel'], dispatch.role(r['role'])) for r in roster_table(table, rows)}

        def why(channel, worker_role, hint=None):
            if (channel, dispatch.role(worker_role)) not in rows_by_role:
                return f'{channel} has no {dispatch.role(worker_role)} row in the dispatch table'
            if not dispatch.hint_allows(hint, channel):
                return f'{channel} does not match owner_hint {dispatch.hint_family(hint)}'
            if channel in excluded:
                return f'{channel} excluded this tick (fix-back or hygiene send)'
            if channel not in available:
                return f'{channel} paused or not allowed (budget or channel gate)'
            if channel in awaiting:
                return (f'{channel} awaiting delivery receipt for {holders[channel]} '
                        f'(held up to {dispatch.UNCERTAIN_TTL // 60} min)')
            if channel in holders:
                return f'{channel} busy with {holders[channel]}'
            if refused.get(channel):
                return f'{channel} refused this tick: ' + '; '.join(refused[channel])
            if (state := dispatch.observed_state(states, channel)) != 'idle':
                return f'{channel} lane not idle ({state})'
            return f'{channel} free but the node is not selectable (M1 node, unfinished input or 3-slot cap)'

        last = timing.state.get('refused', {})
        now = {}
        for f in rows:
            for node in ready(f):
                key = f"{f['name']}:{node}"
                if key in taken:
                    continue
                friendly = f['dag'].node(node)['intern_friendly']
                workers = [w for w in f['team'] if dispatch.role(w['role']) == 'engineer'
                           or (dispatch.role(w['role']) == 'intern' and friendly)]
                # A node the pending journal holds (a legacy send with no start proof, or a loop run record)
                # is named too: the watchdog alerts on every ready node with neither a slot nor a reason, and
                # an uncertain row waits out dispatch.UNCERTAIN_TTL before the node returns to the queue.
                if (held := journal.get(key)) is not None:
                    channel = held.get('channel') or 'no live channel'
                    reason = (f'awaiting delivery receipt from {channel}' if 'by' in held or 'receipt' in held
                              else f'held by its loop run record ({channel})')
                elif workers:
                    hint = f['dag'].node(node).get('owner_hint')
                    reason = '; '.join(why(w['channel'], w['role'], hint) for w in workers)
                else:
                    reason = ('no team.json worker can take it (team: '
                              + (', '.join(w['channel'] for w in f['team']) or 'empty') + ')')
                now[key] = reason
                record('refused', feature=f['name'], node=node, reason=reason)
                if not dry_run and last.get(key) != reason:
                    events.EventStore(data).append(contract.validate_event(dict(
                        t=clock(), run_id=contract.new_run_id(f['name'], node, clock=clock), feature=f['name'],
                        node=node, worker='dispatcher', channel=None, kind='dispatch_refused', sha=None,
                        path=None, detail=' '.join(reason.split())[:contract.DETAIL_MAX])))
        if now != last:
            timing.state['refused'] = now
            timing.save()

    features, _ = current()
    table = roster_table(table, features)
    result['slots'] = plan(excluded)
    if has_ready and not result['slots']:
        record('refused', reason='ready nodes have no eligible free worker slot')
    explain(features, result['slots'])
    return features, table, plan, observed_sender, len(table), result
