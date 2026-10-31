"""Bounded role queue dispatcher. Selection is pure; tick injects transport and clock."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from companion import board_inbox
from companion.feature_team import contract
from companion.feature_team.completion import COMPLETED_STATUSES, CompletionMap
from companion.feature_team.dag import live_path, load

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / 'scripts'
SILENT_SECONDS = 20 * 60
LOOP_CHANNELS = {'codex-exec', 'cursor', 'claude', 'grok', 'muse'}
# A gate, approval or decision node is the owner's or the Board's call: never handed to a worker, raised as a question.
GATE_KINDS = {'gate', 'approval', 'decision'}
GATE_QUESTION_TTL = 24 * 3600  # seconds before an unanswered gate question parks (it stays on the card)
# A node rq_brief refuses parks instead of being re-picked every tick (home-team V0: 306 refusals in 8 h).
PARK_BASE_S = 60  # first park; doubles per try
PARK_CAP_S = 30 * 60
PARK_MAX_TRIES = 3  # after the third refused try the node stays parked until a requeue


def gate_closed(spec, row):
    """True for a node closed by ``feature_team.py done --gate``: a gate, approval or decision node with no files,
    status done, no sha and a result note. Such a node has nothing to land, so its dependents never wait for master;
    every other node keeps the outputs-on-master rule, data/ paths included (2026-10-18: MS4 waited forever on
    PG-YES's private note)."""
    return (isinstance(spec, dict) and spec.get('kind') in GATE_KINDS and not spec.get('files')
            and isinstance(row, dict) and row.get('status') == 'done' and row.get('sha') is None
            and bool(row.get('result')))


def script(name):
    spec = importlib.util.spec_from_file_location(f'_dispatcher_{name}', SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harvest_results(data, repo, python):
    """H0 owns verification; this dispatcher owns the subsequent slot selection.

    H0 binds completion/queues to the process data root. Isolate that binding in
    a child process, and replace its legacy refill callback with queue unlocks.
    No worker transport runs in the harvest process.
    """
    command = [python, '-c',
               'import json, sys; from pathlib import Path; '
               'from companion.feature_team.dispatch import script; '
               'h = script("feature_team"); '
               'h.tick = lambda feature, **kw: h.feature_state(feature)[1].unlocked(h.feature_state(feature)[0]); '
               'print(json.dumps(h.harvest_tick(Path(sys.argv[1]), Path(sys.argv[2]), sys.executable)))',
               str(data), str(repo)]
    done = subprocess.run(command, check=True, capture_output=True, text=True,
                          env={**os.environ, 'KYRA_DATA_DIR': str(data), 'PYTHONPATH': str(SCRIPTS.parent / 'src')})
    return json.loads(done.stdout)


def run_identity(item, features, data, clock):
    from companion.feature_team import contract, picker, runs

    feature = next(f for f in features if f['name'] == item['feature'])
    worker = next(w for w in feature['team'] if w['name'] == item['by'])
    node = feature['dag'].node(item['node'])
    policy = read(data / 'team/effort-policy.json', {'default': worker.get('effort', 'medium')})
    return dict(item, run_id=contract.new_run_id(item['feature'], item['node'], clock=clock),
                model=worker.get('model') or picker.MODEL_IDS.get(worker['name'], worker['name'].lower().replace(' ', '-')),
                effort=picker.policy_effort(worker['name'], worker['role'], policy,
                                            task_kind='plan' if node.get('kind') == 'plan' else 'build'),
                deadline=clock() + runs.DEFAULT_DEADLINE_S, expected_min=node.get('expected_min'))


def _dag_hint(data, feature, node):
    """The node's owner_hint from the live DAG under ``data``, or None."""
    try:
        return load(live_path(Path(data) / 'team/feature-teams', feature)).node(node).get('owner_hint')
    except (OSError, ValueError, KeyError):
        return None


def mint_run(item, data, clock):
    """Persist B0's brief and R0's claim before starting the public W0 runner."""
    from companion.feature_team.locks import FileLocks

    renderer = script('rq_brief')
    try:
        rendered = renderer.render_brief(item, data_dir=data,
                                         features=data / 'team/feature-teams',
                                         runs=data / 'team/features')
    except SystemExit as exc:
        # The CLI renderer fails closed; a refused node must not exit the tick.
        reason = str(exc)
        if 'the owner said' in reason:
            reason += '; requires run.owner_said, DAG.owner_said or a feature-queue note'
        raise ValueError(reason) from None
    renderer.write_brief(rendered, data / 'team/briefs')
    store = loop_store(data, clock)
    rec = store.create(rendered['brief'], expected_min=item.get('expected_min'))
    hint = _dag_hint(data, item['feature'], item['node'])
    for field, value, detail in (('owner_said', item.get('owner_said'), 'original request retained for worker rendering'),
                                 ('owner_hint', hint, 'owner_hint retained for requeues')):
        if value is not None:
            rec[field] = value
            store._commit(rec, worker='dispatcher', channel=item['channel'], path=None, detail=detail)
    leases = FileLocks(data / 'team/loop/locks.sqlite3', workspace=REPO,
                       caller_role='dispatcher', clock=clock)
    return start_run(store, rec, leases, data)


def start_run(store, rec, leases, data):
    files = [f.removeprefix('NEW ') for f in rec['brief']['files']]
    if files and not leases.claim(rec['run_id'], 'dispatcher', files, rec['deadline']):
        return False
    store.transition(rec['run_id'], 'dispatched', deadline=rec['deadline'], tier=rec['tier'],
                     detail=json.dumps(dict(fresh=True)))
    launch_node(rec['run_id'], root=data)
    return True


def loop_store(data, clock=time.time):
    """Keep lifecycle events on R0's emit seam, including M0 measurements."""
    from companion.feature_team import runs
    from companion.feature_team.events import EventStore, metadata

    events = EventStore(data)

    def emit(row):
        if row['kind'] in {'dispatched', 'started', 'timeout'}:
            detail = metadata(row['detail'])
            detail['deadline'] = store.load(row['run_id'])['deadline']
            if row['kind'] in {'dispatched', 'started'}:
                detail['fresh'] = True
            row = dict(row, detail=json.dumps(detail, separators=(',', ':')))
        events.append(row)

    store = runs.Runs(data / 'team/loop', emit=emit, clock=clock)
    return store


def run_fresh_node(run_id, root):
    """W0 adapter: end after one attempt, attach recovery context.

    W0 otherwise corrects invalid results by sending twice on the same run_id.
    Intercept its timeout transition so the next tick owns every fresh retry.
    """
    from companion.feature_team import node_worker

    root = Path(root)
    store = loop_store(root)
    acceptance = store.load(run_id)['brief']['acceptance']
    transition = store.transition

    class AttemptEnded(Exception):
        pass

    def once(run_id, state, **kwargs):
        rec = transition(run_id, state, **kwargs)
        if state == 'timeout':
            raise AttemptEnded
        return rec

    store.transition = once

    sends = []  # per send: its receipt, REFUSED (nothing launched) or UNKNOWN (an error after a possible launch)

    def send(brief, message, **kwargs):
        # before the courier starts (detached): a node worker killed after this line has a non-empty log, so the
        # inspector never reads it as a launch that died before sending (review of c80f11a2)
        print(f'node-worker {run_id}: sending on {brief["channel"]}', flush=True)
        try:
            receipt = _send(brief, message, **kwargs)
        except (node_worker.ChannelUnavailable, SystemExit):
            sends.append(REFUSED)  # refused at the send boundary, or the courier's CLI already exited
            raise
        except BaseException:
            sends.append(UNKNOWN)
            raise
        sends.append(receipt)
        return receipt

    def _send(brief, message, **kwargs):
        rec = store.load(run_id)
        # W0 rebuilds paths from the DAG; retain B0's executable acceptance list.
        brief['acceptance'] = acceptance
        rec['brief'] = brief
        store._commit(rec, worker='dispatcher', channel=brief['channel'], path=None,
                      detail='acceptance retained from rendered brief')
        rendered = script('rq_brief').render_brief(
            dict(brief, owner_said=rec.get('owner_said')), data_dir=root,
            features=root / 'team/feature-teams',
            runs=root / 'team/features')
        message = rendered['text']
        if (node := unverified_input(root, brief, store.load_all())) is not None:
            raise node_worker.ChannelUnavailable(f"input {node} is not verified")
        # Recheck at the send boundary; a budget can change while a worker starts.
        modes, skipped = {}, {}
        pauses = budget_pauses(root / 'team/model-budget.json', time.time(), modes)
        if not budget_table([dict(channel=brief['channel'], role='engineer')], pauses, skipped, modes):
            raise node_worker.ChannelUnavailable('model budget refused fresh dispatch')
        from companion.feature_team.autopilot import capacity_refusals
        if capacity_refusals(root, brief['channel']):
            raise node_worker.ChannelUnavailable('low_power/capacity refused fresh dispatch')
        if rec.get('partial'):
            message += f"\nPrevious attempt partial result: {rec['partial']}\nRead it before continuing.\n"
        if rec['verify'].get('output'):
            message += f"\nPrevious verification output: {rec['verify']['output']}\n"
        return node_worker.send_channel(brief, message, root=root, **kwargs)

    try:
        # node_worker.plan_worktree decides the worktree: a retry gets its node's worktree brought to the current
        # base when nothing unharvested is in it, else a fresh suffixed one; never a stale or foreign checkout
        # looked up at call time (tests replace node_worker._worktree; run_node's default is bound at import)
        # the feature's own repo (companion.feature_team.repos): the site's nodes build in the site repo
        from companion.feature_team import repos
        profile = repos.for_feature(root, store.load(run_id)['feature'])
        out = node_worker.run_node(run_id, root=root, repo=REPO if profile.is_kyra else profile.path, store=store,
                                   send=send,
                                   make_worktree=lambda command: node_worker._worktree(command))
    except AttemptEnded:
        out = store.load(run_id)
    except BaseException as exc:
        print(f'node-worker {run_id}: crashed: {type(exc).__name__}: {exc}'[:500], flush=True)
        raise
    end_attempt(out, root, sends)
    return out


REFUSED, UNKNOWN = 'refused before launch', 'send error after a possible launch'


def unverified_input(root, brief, records):
    """The first input of ``brief`` whose latest loop run is not verified, or None. A node closed by done --gate
    has no run to verify and counts as landed (gate_closed)."""
    from companion.feature_team import runs

    latest = {}
    for parent in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
        latest[parent['feature'], parent['node']] = parent
    rows = read(Path(root) / 'team/features' / brief['feature'] / 'completion.json', {})
    rows = rows.get('nodes', rows) if isinstance(rows, dict) else {}
    dag = read(live_path(Path(root) / 'team/feature-teams', brief['feature']), {})
    specs = {n.get('id'): n for n in dag.get('nodes', []) if isinstance(n, dict)} if isinstance(dag, dict) else {}
    for item in brief['inputs']:
        if gate_closed(specs.get(item['node']), rows.get(item['node'])):
            continue
        parent = latest.get((brief['feature'], item['node']))
        if not parent or parent['state'] not in runs.VERIFIED:
            return item['node']
    return None


def worker_gone(sends):
    """True only on proof that no worker of this run's sends still runs: every send was refused before launch or
    names a pid that is gone (each one: on the correction path courier A may live while B died). A missing receipt,
    a receipt with no pid (cursor) or a send error after a possible launch proves nothing: that claim waits for the
    inspector or its deadline, so a live worker's files are never handed to another."""
    from companion.feature_team import node_worker

    for sent in sends:
        if sent == REFUSED:
            continue
        pid = node_worker._receipt_field(sent, 'pid') if sent != UNKNOWN else None
        if not pid or node_worker._alive(pid):
            return False
    return True


def end_attempt(rec, root, sends):
    """The node worker's last words (2026-10-16, public-release HISTORY-SCAN and STORY-REHEARSAL: an invalid result
    ended the attempt, the process exited with an empty log and the claim held, and the next ticks refused the retry
    for 'previous lease still held'). One line goes to the worker log (stdout under launch_node); a timeout or
    escalated attempt whose worker is proven gone frees its file claim (reason dead), so the next tick retries now."""
    from companion.feature_team.locks import FileLocks

    detail = str(((rec.get('history') or [{}])[-1]).get('detail', ''))
    line = f"node-worker {rec['run_id']}: attempt ended {rec['state']}: {' '.join(detail.split())[:200]}"
    if rec['state'] in {'timeout', 'escalated'}:
        if worker_gone(sends):
            leases = FileLocks(Path(root) / 'team/loop/locks.sqlite3', workspace=REPO, caller_role='dispatcher')
            freed = [leases.release(rec['run_id'], claim['owner'], reason='dead')
                     for claim in leases.snapshot() if claim['run_id'] == rec['run_id']]
            line += '; file claim released (no worker running)' if any(freed) else '; no file claim held'
        else:
            line += '; file claim kept (the worker may still be running)'
    print(line, flush=True)


def proven_dead(rec, data, now):
    """Proof that no worker of this closed run still runs, to the inspector's standard. A timeout: closed by the
    inspector with proof, or node_worker's launch failure. An escalated run: proof_dead of its evidence, its node
    worker's pid known and gone, and every courier named in its history gone. Any other state is never dead here."""
    from companion.feature_team import inspector, node_worker

    if rec['state'] == 'timeout' and (node_worker.is_launch_failure(rec)
                                      or (rec.get('inspector_closed') or {}).get('proof')):
        return True
    # a timeout after an invalid result (the worker wrote a result; FL11 2026-10-16) gets the escalated standard
    invalid = rec['state'] == 'timeout' and str(((rec.get('history') or [{}])[-1]).get('detail', '')).startswith(
        'invalid result')
    if rec['state'] != 'escalated' and not invalid:
        return False
    def alive(pid):
        return inspector.pid_alive(pid)  # read at call time, so a test can replace it
    probe = inspector.Inspector(data, store=None, now=now, alive=alive)
    if not inspector.proof_dead(probe.evidence(rec)):
        return False
    try:
        launcher = int((Path(data) / 'team/loop' / f"{rec['run_id']}.worker.pid").read_text().strip())
    except FileNotFoundError:
        launcher = None
    except (OSError, ValueError):
        return False  # unreadable is not gone: fail closed
    if launcher is not None and alive(launcher):
        return False
    ids = {m[1] for h in rec.get('history', []) for m in inspector._LEDGER_ID.finditer(str(h.get('detail', '')))}
    exited = {row.get('id') for row in probe.ledger if row.get('status') == 'failed'}
    for row in probe.ledger:
        if row.get('id') in ids - exited and 'status' not in row:
            pid = row.get('pid') or ((m := inspector._PID.search(str(row.get('receipt', '')))) and m[1])
            if not pid or alive(int(pid)):
                return False  # a courier with no known pid, or a live one, proves nothing
    return True


def launch_node(run_id, *, root):
    """Reserve before spawning. A lost launch receipt is never retried on this id. The node worker's pid goes to
    team/loop/<run_id>.worker.pid for the inspector; a launch that cannot start writes its error to the worker log
    and closes the run as a launch failure at once (the inspector owns its requeue)."""
    from companion.feature_team.node_worker import LAUNCH_FAILURE

    command = [sys.executable, '-c',
               'import sys; from companion.feature_team.dispatch import run_fresh_node; '
               'run_fresh_node(sys.argv[1], sys.argv[2])', run_id, str(root)]
    log = root / 'team/loop' / f'{run_id}.worker.log'
    with log.open('a') as output:
        try:
            child = subprocess.Popen(command, cwd=REPO, stdin=subprocess.DEVNULL, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True,
                                     env={**os.environ, 'KYRA_DATA_DIR': str(root), 'PYTHONPATH': str(REPO / 'src')})
        except OSError as exc:
            why = f'{LAUNCH_FAILURE}: the node worker could not start: {type(exc).__name__}: {exc}'[:300]
            output.write(f'dispatcher {run_id}: launch failed: {why}\n')
            output.flush()
            loop_store(root).transition(run_id, 'timeout', worker='dispatcher', detail=why)
            raise
    (root / 'team/loop' / f'{run_id}.worker.pid').write_text(f'{child.pid}\n')


LANDED_CLAIM_KINDS = ('result', 'landed')
_CLAIM_SHA = re.compile(r'[0-9a-f]{7,40}')
_CLAIM_BRANCH = re.compile(r'[A-Za-z0-9._/][A-Za-z0-9._/-]{0,199}')
_OPEN_STATES = ('queued', 'dispatched', 'started', 'heartbeat', 'result', 'verify_pass', 'fixback')


def landing_claim_rows(data, records=None, *, features=None):
    """Board-inbox rows that may close a node as a landing claim: kind result or landed with a plain ``feature``,
    a ``node`` and a 7 to 40 hex ``sha`` (the newest row per node wins), the node not complete, no run of the node
    open or already carrying a commit starting with that sha. Returns {(feature, node): (sha, branch)}; branch is
    None unless it is a plain ref name. Read-only; the harvest checks the DAG, the landing and the node's files."""
    from companion.feature_team.completion import COMPLETED_STATUSES

    inbox = Path(data) / 'team/board-inbox.jsonl'
    if not inbox.is_file() or inbox.is_symlink():
        return {}
    claims = {}
    for line in inbox.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get('kind') not in LANDED_CLAIM_KINDS:
            continue
        feature, node, sha, branch = row.get('feature'), row.get('node'), row.get('sha'), row.get('branch')
        if not all(isinstance(v, str) for v in (feature, node, sha)) or not _CLAIM_SHA.fullmatch(sha.lower()):
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]+', feature) or not re.fullmatch(r'[A-Za-z0-9_.-]+', node):
            continue
        if features is not None and feature not in features:
            continue
        ok_branch = isinstance(branch, str) and _CLAIM_BRANCH.fullmatch(branch) and '..' not in branch
        claims[feature, node] = (sha.lower(), branch if ok_branch else None)
    if not claims:
        return {}
    records = loop_store(data).load_all() if records is None else records
    pending = {}
    for (feature, node), (sha, branch) in claims.items():
        rows = read(Path(data) / 'team/features' / feature / 'completion.json', {}).get('nodes', {})
        if rows.get(node, {}).get('status') in COMPLETED_STATUSES:
            continue
        history = [r for r in records.values() if r['feature'] == feature and r['node'] == node]
        carried = [c for r in history for c in (r.get('sha'), r['brief'].get('base')) if isinstance(c, str)]
        if any(r['state'] in _OPEN_STATES for r in history) or any(c.startswith(sha) for c in carried):
            continue
        pending[feature, node] = (sha, branch)
    return pending


LIVE_RUN_STATES = {'queued', 'dispatched', 'started', 'heartbeat', 'result'}  # a run that holds its channel
WAITING_RUN_STATES = {'queued', 'timeout', 'verify_fail', 'fixback'}  # a run that still needs a slot
FIXBACK_LOG_CHARS = 1500  # the tail of the verify output a fix-back brief carries


def verify_fail_followup(rec):
    """What the loop does with a verify_fail run: ('escalate', reason) when it already was a fix-back or its result
    says blocked, else ('fixback', text) with the failed verify detail and the tail of the verify output.

    2026-10-16 FL: STORY-BATCH (result blocked) and STORY-TRAIN (verify failed) sat in verify_fail over three hours."""
    why = (rec.get('history') or [{}])[-1].get('detail') or 'verify failed'
    if rec['brief'].get('fixback'):
        return 'escalate', f'fix-back already failed: {why}'
    try:
        result = json.loads(Path(rec.get('result') or rec['brief']['result']).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        result = {}
    if isinstance(result, dict) and result.get('status') == 'blocked':
        questions = '; '.join(str(q) for q in result.get('open_questions') or [])
        return 'escalate', f'result says blocked: {questions or why}'[:300]
    kind = rec['brief']['channel'].partition(':')[0]
    # a landing or done claim (local:operator) or a channel the shared eligibility rule refuses (a retired muse slot):
    # no fresh-context fix-back is possible, so it escalates rather than park every tick (the inspector's rule)
    if kind not in LOOP_CHANNELS or not allowed(rec['brief']['channel']):
        return 'escalate', f'verify failed on channel {rec["brief"]["channel"]}, which the loop cannot serve: {why}'[:300]
    output = (rec.get('verify') or {}).get('output')
    try:
        tail = Path(output).read_text(encoding='utf-8', errors='replace')[-FIXBACK_LOG_CHARS:] if output else ''
    except OSError:
        tail = ''
    text = (f"Fix-back of {rec['run_id']}: {why}. Verify output {output or 'not recorded'}"
            + (f': {tail}' if tail else '') + '. Reproduce first, make the smallest fix, keep the files.')
    return 'fixback', ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())  # one brief line


def queue_empty(data, latest):
    """True only when this tick found no work at all: no run waiting for a slot, no open node (status unset or
    queued with every input done, blocked or not), no failed node with an unfinished dependent, and no queue entry that has no feature yet. M0/FL count only
    ticks flagged True as empty-queue time; anything unreadable answers False, so a doubt counts the wait."""
    try:
        if any(rec['state'] in WAITING_RUN_STATES for rec in latest):
            return False
        features = snapshot(data)
        for feature in features:
            state = feature['state']
            for node, spec in feature['dag']._nodes.items():
                if (state.get(node, {}).get('status') in (None, 'queued')
                        and all(state.get(dep, {}).get('status') == 'done' for dep in spec['inputs'])):
                    return False
                # a failed input's fixback goes out the legacy route, which logs no loop work signal
                if (state.get(node, {}).get('status') != 'done'
                        and any(state.get(dep, {}).get('status') == 'failed' for dep in spec['inputs'])):
                    return False
        path = data / 'team/feature-queue.json'
        if path.exists():
            names = {f['name'] for f in features}
            if any(entry['name'] not in names for entry in json.loads(path.read_text())['features']):
                return False
        return True
    except Exception:
        return False


def loop_tick(data, table, lanes, *, clock, dry_run, stopped=False):
    """Advance durable runs only; Q0 owns filling new slots and H0 owns harvesting.

    A successor is published atomically with its inherited attempt counts. The
    parent link makes crash recovery independent of completion.json and chat.
    """
    from companion.feature_team import contract, runs
    from companion.feature_team.autopilot import capacity_refusals
    from companion.feature_team.fixback import append_once
    from companion.feature_team.locks import FileLocks

    store = loop_store(data, clock)
    records = store.load_all()
    report = dict(silent=[], timed_out=[], launched=[], escalated=[], refusals=[])
    # a run in state result, or a landed hand-run result row waiting to become a claim (review o-rev58: an idle
    # loop never reached the harvest, so the row was never read)
    if not dry_run and not stopped and (any(rec['state'] == 'result' for rec in records.values())
                                        or landing_claim_rows(data, records)):
        report['harvest'] = harvest_results(data, REPO, sys.executable)
        records = store.load_all()
    latest = {}
    for rec in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
        latest[rec['feature'], rec['node']] = rec
    # An escalated record keeps its node reserved (the legacy path must not resend a run the loop gave up on)
    # until the Board requeues it: a completion row set back to queued releases the key, so the next tick mints
    # a fresh record (2026-10-15: the site PLAN stayed reserved after requeue and the 3D plan started first).
    def requeued(feature, node):
        rows = read(data / 'team/features' / feature / 'completion.json', {}).get('nodes', {})
        return isinstance(rows, dict) and (rows.get(node) or {}).get('status') == 'queued'
    # One per-tick view of busy channels: seeded from the runs live at tick start, and every launch or successor this
    # tick creates (a queued retry, a correction, a fix-back) joins it, so no later pick in the same tick can take
    # that channel (review o-rev-vf2: two fix-backs, or a timeout retry and a fix-back, both went to muse:0).
    busy = {}  # channel -> the (feature, node) keys holding it

    def hold(rec):
        busy.setdefault(rec['brief']['channel'], set()).add((rec['feature'], rec['node']))

    def channel_taken(rec):
        return bool(busy.get(rec['brief']['channel'], set()) - {(rec['feature'], rec['node'])})

    for r in latest.values():
        if r['state'] in LIVE_RUN_STATES - {'queued'}:  # a queued run has no worker yet: it contends, it does not hold
            hold(r)
    reservations = {f'{feature}:{node}': dict(
        channel=r['brief']['channel'] if r['state'] in LIVE_RUN_STATES else None)
        for (feature, node), r in latest.items() if not (r['state'] == 'escalated' and requeued(feature, node))}

    def event(kind, rec=None, **detail):
        row = dict(t=clock(), run_id=rec['run_id'] if rec else None,
                   feature=rec['feature'] if rec else None, node=rec['node'] if rec else None,
                   worker='dispatcher', channel=rec['brief']['channel'] if rec else None,
                   kind=kind, sha=None, path=None, detail=json.dumps(detail, separators=(',', ':')))
        if not dry_run:
            store.emit(contract.validate_event(row))

    event('tick', status='stopped' if stopped else 'running', empty_queue=queue_empty(data, latest.values()))
    if not records:
        return report, reservations
    leases = None if dry_run else FileLocks(data / 'team/loop/locks.sqlite3', workspace=REPO,
                                           caller_role='dispatcher', clock=clock)
    if leases:
        leases.expire()
    def release(rec, reason):
        if leases:
            for claim in leases.snapshot():
                if claim['run_id'] == rec['run_id']:
                    leases.release(rec['run_id'], claim['owner'], reason=reason)

    def escalate(rec, reason):
        report['escalated'].append(rec['run_id'])
        if dry_run:
            return
        if rec['state'] != 'escalated':
            rec = store.transition(rec['run_id'], 'escalated', detail=reason)
        append_once(data / 'team/board-inbox.jsonl', 'loop-escalated:' + rec['run_id'],
                    dict(t=clock(), kind='escalated', feature=rec['feature'], node=rec['node'],
                         run_id=rec['run_id'], **{'from': 'dispatcher', 'text': reason}))

    for rec in latest.values():
        run_id = rec['run_id']
        if rec['state'] in runs.VERIFIED:
            release(rec, 'result')
            continue
        if rec['state'] == 'escalated':
            # deadline-respecting: an escalated run's unexpired claim may belong to a worker never proven dead
            # (review of the inspector fix-back b9cdd04f); expire() frees it at its deadline
            release(rec, 'timeout')
            escalate(rec, (rec.get('history') or [{'detail': 'escalated'}])[-1]['detail'])
            continue
        if rec['state'] in {'dispatched', 'started', 'heartbeat'}:
            activity = max((h['t'] for h in rec.get('history', [])
                            if h['state'] in {'dispatched', 'started', 'heartbeat'}),
                           default=rec.get('updated', rec['created']))
            for lane in lanes:
                if lane.get('channel', lane.get('lane')) in {rec['brief']['channel'], lane_id(rec['brief']['channel'])}:
                    stamp = lane.get('t')
                    if type(stamp) in (int, float) and stamp <= clock():
                        activity = max(activity, stamp)
            if clock() - activity >= SILENT_SECONDS and rec.get('silent_since') != activity:
                report['silent'].append(run_id)
                if not dry_run:
                    rec['silent_since'] = activity
                    runs._write_json(store.path(run_id), rec)
                event('context', rec, status='silent', seconds=clock() - activity)
        if rec['state'] in {'queued', 'dispatched', 'started', 'heartbeat'} and clock() >= rec['deadline']:
            report['timed_out'].append(run_id)
            partial = rec.get('result') or rec.get('partial')
            if not partial and rec['brief'].get('result') and Path(rec['brief']['result']).exists():
                partial = rec['brief']['result']
            if dry_run:
                rec = dict(rec, state='timeout', partial=partial)
            else:
                rec = store.transition(run_id, 'timeout', path=partial,
                                       detail=json.dumps(dict(deadline=rec['deadline'])))
            release(rec, 'timeout')
        if rec['state'] == 'verify_fail':
            # never silent: one fix-back on the same channel, or escalated when that cannot help (a Board hold or a
            # hand build keeps the node for the Board, as the inspector does)
            from companion.feature_team.inspector import hand_built
            rows = read(data / 'team/features' / rec['feature'] / 'completion.json', {}).get('nodes', {})
            row = (rows.get(rec['node']) if isinstance(rows, dict) else None) or {}
            if not (rec.get('hold') or row.get('hold') or hand_built(row)):
                action, why = verify_fail_followup(rec)
                release(rec, 'result')
                if action == 'escalate':
                    escalate(rec, why)
                    continue
                if dry_run:
                    rec = dict(rec, state='fixback', fixback_note=why)
                else:
                    rec = store.transition(run_id, 'fixback', detail=why[:contract.DETAIL_MAX])
                    rec['fixback_note'] = why
                    runs._write_json(store.path(run_id), rec)
        if rec['state'] in {'result', 'verify_fail'}:
            release(rec, 'result')
            continue
        if rec['state'] == 'fixback':
            release(rec, 'result')
            last = (rec.get('history') or [{}])[-1]
            note = rec.get('fixback_note') or (  # a crash between the transition and the note: the detail holds it
                last.get('detail') if last.get('state') == 'fixback'
                and str(last.get('detail', '')).startswith('Fix-back of ') else None)
            if not note:
                continue  # a fix-back the loop did not start (no verify output to send) is not resent from here
            rec = dict(rec, fixback_note=note)
        if rec['state'] not in {'timeout', 'queued', 'fixback'}:
            continue
        # the completion table may already mark the node done (reconcile, a hand build) while this record still says
        # timeout or queued; refusing it every tick blocked nothing real (2026-10-16: R14 on a paused channel)
        rows = read(data / 'team/features' / rec['feature'] / 'completion.json', {}).get('nodes', {})
        if isinstance(rows, dict) and (rows.get(rec['node']) or {}).get('status') in COMPLETED_STATUSES:
            print(f"loop: skip {run_id}: {rec['state']} record, node {rec['node']} is "
                  f"{rows[rec['node']]['status']} in completion.json", file=sys.stderr)
            continue
        from companion.feature_team.node_worker import is_launch_failure
        if rec['state'] == 'timeout' and (rec.get('inspector_closed') or is_launch_failure(rec)):
            # the inspector closed a dead worker and owns its requeue (another channel, or escalated when blocked);
            # a retry here would resend on the dead channel (review o-rev85)
            report['refusals'].append(dict(run_id=run_id, reasons=['inspector closed a dead worker; it owns the requeue']))
            continue
        reservations[f"{rec['feature']}:{rec['node']}"]['channel'] = rec['brief']['channel']
        tier = next((t for t in runs.TIERS[runs.TIERS.index(rec['tier']):]
                     if rec['attempts'][t] < runs.ATTEMPTS_PER_TIER), None)
        if tier is None:
            escalate(rec, 'All three tiers exhausted (two attempts per tier)')
            continue
        reasons = ['safety containment stopped dispatch'] if stopped else []
        if rec['brief']['channel'].partition(':')[0] not in LOOP_CHANNELS:
            reasons.append('loop cannot serve this channel kind in a fresh context')
        if channel_taken(rec):  # every launch path (queued, retry, correction, fix-back) waits for its channel
            reasons.append('channel has a live run')
        for item in rec['brief']['inputs']:
            parent = latest.get((rec['feature'], item['node']))
            if not parent or parent['state'] not in runs.VERIFIED:
                reasons.append(f"input {item['node']} is {parent['state'] if parent else 'missing'}, not verified")
        modes, skipped = {}, {}
        pauses = budget_pauses(data / 'team/model-budget.json', clock(), modes)
        entry = next((r for r in table if r['channel'] == rec['brief']['channel']),
                     dict(channel=rec['brief']['channel'], role='engineer'))
        if refusal := contract.channel_refusal(entry['channel']):
            reasons.append(f'channel cannot serve this node: {refusal}')
        elif not allowed(entry['channel']) or not budget_table([entry], pauses, skipped, modes):
            reasons.append('model budget or channel refused dispatch')
        reasons.extend(capacity_refusals(data, rec['brief']['channel']))
        if reasons:
            report['refusals'].append(dict(run_id=run_id, reasons=reasons))
            event('context', rec, status='dispatch_refused')
            continue
        if dry_run:
            continue
        # This node's own older runs proven dead hand their file claims to this run. An unproven one (escalated with
        # no proof, a live state) keeps them to its deadline and this run waits here, re-checked each tick, instead
        # of starting the ten-minute claim wait (2026-10-16 FL10: requeued HISTORY-SCAN escalated again on its own
        # escalated grok run's claim). That also keeps one live worker per node. Other nodes' claims go to
        # start_run as before.
        own = {old['run_id']: old for old in records.values() if old['run_id'] != run_id
               and (old['feature'], old['node']) == (rec['feature'], rec['node'])}
        waiting = []
        for claim in leases.snapshot():
            old = own.get(claim['run_id'])
            if old is None:
                continue
            if proven_dead(old, data, clock()):
                leases.release(old['run_id'], claim['owner'], reason='dead')
            else:
                waiting.append(claim)
        if waiting:
            report['refusals'].append(dict(run_id=run_id, reasons=["this node's older run still holds its files"]))
            event('lock_wait', rec, previous_lease=True, older_runs=len(waiting),
                  lease_deadline=max(c['deadline'] for c in waiting), wait_seconds=max(0.0, clock() - rec['updated']))
            continue
        # An early W0 receipt timeout may still hold an unexpired lease. Wait for
        # expiry rather than stealing it or sending another worker over its files.
        # A worker proven gone (an invalid result's first worker, FL11) frees it now, re-checked each tick.
        held = [c for c in leases.snapshot() if c['run_id'] == run_id]
        if held and proven_dead(rec, data, clock()):
            for c in held:
                leases.release(run_id, c['owner'], reason='dead')
            held = []
        if held:
            report['refusals'].append(dict(run_id=run_id, reasons=['previous lease still held',
                                                                   'its worker is not proven gone']))
            # one lock_wait event per node per tick, so a node waiting on a (maybe dead) run's lease is visible to
            # every watcher (3d-inner-demo G1, 2026-10-16: held to its deadline with no event)
            event('lock_wait', rec, previous_lease=True, lease_deadline=max(c['deadline'] for c in held),
                  wait_seconds=max(0.0, clock() - rec['updated']))
            continue
        if rec['state'] != 'queued':
            new_id = f"{rec['feature']}-{rec['node']}-{uuid.uuid4().hex[:12]}"
            brief = dict(rec['brief'], run_id=new_id, deadline=clock() + runs.DEFAULT_DEADLINE_S,
                         result=str(data / 'team/loop/results' / f'{new_id}.json'))
            if rec['state'] == 'fixback' and rec.get('fixback_note'):
                brief['fixback'] = rec['fixback_note']  # the failed verify output goes to the same channel
            # R0.create resets attempts. Seed the new queued record in one commit
            # so a crash can never leave a successor with a reset retry budget.
            successor = dict(rec, run_id=new_id, brief=brief, state='queued', tier=tier,
                             created=clock(), updated=clock(), deadline=brief['deadline'],
                             result=None, partial=rec.get('partial') or rec.get('result'),
                             retry_of=run_id, history=[dict(t=clock(), state='queued', detail='fresh retry')])
            successor.pop('silent_since', None)
            successor.pop('fixback_note', None)  # the successor carries it in brief.fixback
            store._commit(successor, worker='dispatcher', channel=brief['channel'], path=successor['partial'],
                          detail='fresh retry')
            rec, run_id = successor, new_id
        hold(rec)  # a queued successor holds its channel even when start_run waits below
        try:
            if not start_run(store, dict(rec, tier=tier), leases, data):
                event('lock_wait', rec, wait_seconds=0)
                continue
        except TimeoutError:
            escalate(rec, 'File lock wait exceeded ten minutes')
            continue
        except OSError:
            if is_launch_failure(store.load(run_id)):
                # launch_node could not start the node worker and closed the run as a launch failure; the
                # inspector owns its requeue, and nothing was launched
                event('context', rec, status='launch_failed')
                continue
            # an error after the node worker may have started: the record stays dispatched and is never
            # relaunched on this id; the inspector or its deadline closes it
            event('context', rec, status='launch_uncertain')
        report['launched'].append(run_id)
        reservations[f"{rec['feature']}:{rec['node']}"]['channel'] = rec['brief']['channel']
    return report, reservations


class RootCompletion(CompletionMap):
    """Use the existing atomic writer with a caller-selected data root."""

    def __init__(self, folder):
        self.path = folder / "completion.json"
        self._read()


def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def role(value):
    return str(value).lower()


def allowed(channel):
    kind, _, target = channel.partition(":")
    if kind == "muse":
        from companion.feature_team.muse import SLOTS
        return channel in SLOTS
    if kind == "local":
        from companion.local_lane import CLASSES
        return target in CLASSES
    return (kind in {"codex", "codex-exec", "cursor", "claude", "grok"} and bool(target) and target.lower() != "owner"
            and contract.channel_refusal(channel) is None)


def lane_id(channel):
    kind, _, target = channel.partition(":")
    if kind == 'codex-exec':
        kind = 'codex'
    return f"{kind}-{Path(target).name}" if target else kind


LIVE_STATES = ("working", "busy")


def lane_states(rows):
    """{lane or channel: state} from lane-watch rows. A grok run's worktree is GROK_HOME/<target>--<run>, so the
    watch names its lane grok-<target>--<run>; the channel grok:<target> is judged from its exact row and every
    run row together: a live (working) row wins, otherwise the newest by the row's timestamp (review o-rev51,
    2026-10-15: rows sorted by name made a finished run hide a live one, and the folder's own row masked every
    run row)."""
    states = {}
    grouped = {}
    for row in rows:
        lane, state = row.get('channel', row.get('lane')), row.get('state')
        states[lane] = state
        if isinstance(lane, str) and lane.startswith('grok-'):
            grouped.setdefault(lane.split('--', 1)[0], []).append((row.get('t') or 0, state))
    for key, candidates in grouped.items():
        live = [state for _, state in candidates if state in LIVE_STATES]
        states[key] = live[0] if live else max(candidates, key=lambda c: c[0])[1]
    return states


def observed_state(states, channel):
    """The lane-watch state for a channel. One-shot channels (local interns, Claude CLI runs) have no lane the
    watcher can observe, so "not busy" is their idle evidence; every other kind needs a fresh idle row.
    A loop channel (LOOP_CHANNELS: a fresh worker per run, its busy-ness held by its run record) with no matching
    row is idle too: night 2026-10-17, grok:memory-scale had never run and the cursor rows are named
    cursor-cursor-w2, so those seats read "lane not idle (None)" for hours with no worker anywhere."""
    # Only beside a fresh watch: _tick passes no rows at all when lane-watch.json is stale, and stale observations
    # are not permission (a hand-started grok or cursor worker has no run record; review of 5fd2e893).
    loop = bool(states) and channel.partition(":")[0] in LOOP_CHANNELS
    fallback = "idle" if loop or channel.startswith(("local:", "claude:", "muse:")) else None
    if channel in states:
        return states[channel]
    if channel.startswith("grok:"):
        # A map built by lane_states already folded the lane's rows; a plain {lane: state} map has no timestamps,
        # so a live run row wins and otherwise the lane's own row, then the last run row.
        lane = lane_id(channel)
        own = [lane] if lane in states else []
        runs = [name for name in states if isinstance(name, str) and name.startswith(lane + "--")]
        live = [states[name] for name in own + runs if states[name] in LIVE_STATES]
        if live:
            return live[0]
        if own:
            return states[lane]
        if runs:
            return states[runs[-1]]
        return fallback
    return states.get(lane_id(channel), fallback)


def budget_pauses(path, now, modes=None):
    """Invalid or unavailable budget configuration never blocks dispatch."""
    try:
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError('expected object')
        paused = {}
        for family, entry in document.items():
            if not isinstance(entry, dict):
                raise ValueError('expected family object')
            if modes is not None:
                modes[family] = entry.get('mode', 'heavy')
            if 'mode' in entry and 'paused_until' not in entry:
                continue
            until = entry.get('paused_until')
            if until is not None:
                stamp = datetime.fromisoformat(until.replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError('pause date needs timezone')
                if stamp.timestamp() <= now:
                    continue
            paused[family] = str(entry.get('reason') or 'model budget paused')
        return paused
    except (OSError, ValueError, TypeError, AttributeError):
        if modes is not None:
            modes.clear()
        print('Warning: model-budget missing or invalid; no pauses applied', file=sys.stderr)
        return {}


def budget_table(table, pauses, skipped, modes=None):
    modes = modes or {}
    eligible = []
    for entry in table:
        family = entry.get('family', entry['channel'].partition(':')[0])
        # The provider-wide key covers each Claude model family.
        provider = family.split('-', 1)[0]
        # A channel-level pause (the inspector's, after repeated dead runs on one channel) beats the family's.
        key = entry['channel'] if entry['channel'] in pauses else provider if provider in pauses else family
        if key in pauses:
            skipped[key] = pauses[key]
        elif modes.get(provider, modes.get(family)) == 'eco' and (
                role(entry.get('role')) not in {'review', 'reviewer', 'board'}
                or (family == 'claude-fable' and role(entry.get('role')) != 'board')):
            skipped[provider] = 'eco mode reserved for review or Board work'
        else:
            eligible.append(entry)
    # Soft preference only: keep Fable available after other Claude builders.
    positions = [i for i, row in enumerate(eligible)
                 if row.get('family', row['channel'].partition(':')[0]).startswith('claude')
                 and role(row.get('role')) in {'intern', 'engineer'}]
    preferred = sorted((eligible[i] for i in positions),
                       key=lambda row: row.get('family') == 'claude-fable')
    for i, row in zip(positions, preferred, strict=True):
        eligible[i] = row
    return eligible


def containment(data, features):
    """Latest verdict per check controls failures; only named safety checks stop globally."""
    from companion.feature_team.fixback import refused, rows

    failed = {f['name']: {n for n, r in f['state'].items() if r.get('status') == 'failed'}
              for f in features}
    latest = {}
    sources = [(None, data / 'team/board-inbox.jsonl')]
    sources += [(f['name'], data / 'team/features' / f['name'] / 'metrics.jsonl') for f in features]
    def stamp(row):
        value = row.get('t', 0)
        try:
            return float(value)
        except (ValueError, TypeError):
            try:
                return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
            except ValueError:
                return 0
    # A loop run's verify failure blocks its node only while that run is still verify_fail: once the Board escalates
    # it (then requeues the node) or reverifies it, the row is history. No pass row is ever written for a loop node,
    # so without this a requeued node stayed blocked forever (site PLAN rehearsal, 2026-10-15).
    from companion.feature_team import runs as run_store
    records = run_store.Runs(data / 'team/loop').load_all() if (data / 'team/loop').is_dir() else {}
    for name, path in sources:
        for row in rows(path):
            if row.get('event', row.get('kind')) not in {'verify', 'gate'}:
                continue
            if (row.get('from') == 'harvest' and row.get('run_id') in records
                    and records[row['run_id']]['state'] != 'verify_fail'):
                continue
            verdict = str(row.get('verdict', row.get('status', ''))).lower()
            if not refused(row) and verdict not in {'pass', 'passed', 'success'}:
                continue
            feature = row.get('feature', name)
            if feature is None:
                matches = [f['name'] for f in features if row.get('node') in f['dag']._nodes]
                feature = matches[0] if len(matches) == 1 else None
            key = (feature, row.get('node'), row.get('event', row.get('kind')),
                   row.get('test', row.get('test_name', '')), row.get('rule', ''))
            if key not in latest or stamp(row) >= stamp(latest[key]):
                latest[key] = row
    stops = []
    guard_tests = {'test_no_third_party_pii', 'test_no_personal_files'}
    for (name, node, _, test, rule), row in latest.items():
        if not refused(row):
            continue
        category = row.get('failure_kind', row.get('category', rule))
        safety = category if category in {'guard', 'privacy'} else None
        if any(Path(str(test).split('::')[0]).stem == t for t in guard_tests):
            safety = 'privacy'
        if safety:
            stops.append(f'{safety} failure: {name or "unknown"}:{node or "unknown"}')
        if name in failed:
            failed[name].add(node)
    blocked = {}
    for f in features:
        nodes = set(failed[f['name']]) & set(f['dag']._nodes)
        pending = list(nodes)
        while pending:
            for child in f['dag'].dependents(pending.pop()):
                if child not in nodes:
                    nodes.add(child)
                    pending.append(child)
        # A parked node (refused brief, snapshot's ``parked``) is blocked for readiness too: autopilot.ready
        # excludes ``blocked``, so a node parked for good no longer counts as ready-but-unassigned and cannot
        # freeze admission of the next feature. No dependent propagation: an unverified input already holds them.
        nodes |= set(f.get('parked', set())) & set(f['dag']._nodes)
        f['blocked'] = nodes
        if nodes:
            blocked[f['name']] = sorted(nodes)
    return blocked, sorted(set(stops))


def stale_holds(records, feature, state, *, runs_dir=None):
    """Nodes whose completion row says dispatched while the node's latest loop run ended (escalated, verify_pass,
    landed) AFTER the row was written: such a row holds no channel (FL9, 2026-10-16: two runs escalated on grok kept
    claude:opus and claude:opus-4 "busy" through rows dispatched by Claude Opus). Everything else keeps its hold
    (fail closed): no loop run, a live latest run, a row with a missing or bad ``t`` or one written after the run
    ended (a Board requeue resent on a legacy channel that mints no record, review o-rev-bh P3), and every row when
    any record in ``runs_dir`` failed to load (the unreadable one may be the newest, P4). The row itself stays
    dispatched, so the node is never resent by this."""
    from companion.feature_team import events, runs

    if runs_dir is not None and Path(runs_dir).is_dir():
        if len([p for p in Path(runs_dir).glob('*.json') if not p.name.startswith('.tmp-')]) != len(records):
            return set()
    latest = {}
    for rec in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
        if rec['feature'] == feature:
            latest[rec['node']] = rec
    stale = set()
    for node, row in state.items():
        if not isinstance(row, dict) or row.get('status') != 'dispatched' or node not in latest:
            continue
        rec = latest[node]
        try:
            written = events.timestamp(row.get('t'))
        except ValueError:
            written = None
        if rec['state'] not in runs.LIVE and written is not None and rec.get('updated', 0) >= written:
            stale.add(node)
    return stale


HINT_FAMILIES = ('opus', 'fable', 'sonnet', 'haiku', 'codex', 'grok', 'cursor', 'muse')


def hint_family(hint):
    """The first model family a node's free-text owner_hint names, or None (Board, lane 1, intern queue: no rule)."""
    text = str(hint or '').lower()
    found = [(m.start(), name) for name in HINT_FAMILIES if (m := re.search(rf'\b{name}\b', text))]
    return min(found)[1] if found else None


def hint_allows(hint, channel):
    """True when the hint names no family, or the channel is in it: claude:<family>*, codex:*, codex-exec:*, ..."""
    family = hint_family(hint)
    if family is None:
        return True
    kind, _, target = channel.lower().partition(':')
    if family in {'opus', 'fable', 'sonnet', 'haiku'}:
        return kind == 'claude' and target.startswith(family)
    return kind.split('-')[0] == family


LAST_QUEUE_RANK = 10**6


def holder_channel(row, authors):
    """The channel a dispatched completion row holds: its own ``channel`` when the dispatcher wrote one, else the
    roster channel of its author name (rows written before 2026-10-17). Holds key by channel, never by name alone:
    six Muse slots share a model, and a name shared by two roster rows would map to only one of their channels."""
    return row.get("channel") or authors.get(row.get("by"), row.get("by"))


def select(table, watch, features, pending):
    """Choose at most three idle, unassigned lanes and dependency-ready nodes.

    A node with an owner_hint only takes a channel that matches it; with none free it stays queued (one log line)."""
    states = lane_states(watch)
    busy = set()
    for feature in features:
        authors = {w["name"]: w["channel"] for w in feature["team"]}
        # autopilot.resumed marks rows whose node's latest loop run is no longer live: they hold no channel
        busy.update(holder_channel(row, authors) for node, row in feature["state"].items()
                    if row["status"] == "dispatched" and node not in feature.get("stale_holds", ()))
    busy.update(row["channel"] for row in pending.values())
    chosen, used, hinted, stale = [], set(), {}, {}
    for entry in table:
        channel, worker_role = entry["channel"], role(entry["role"])
        if (not allowed(channel) or channel in busy or worker_role not in {"engineer", "intern"}
                or observed_state(states, channel) != "idle"):
            continue
        if channel.startswith("local:") and worker_role != "intern":
            continue
        candidates = []
        for feature in features:
            worker = next((w for w in feature["team"]
                           if w["channel"] == channel and role(w["role"]) == worker_role), None)
            if not worker:
                continue
            queues = ("engineer", "intern") if worker_role == "engineer" else ("intern",)
            for rank, queue in enumerate(queues):
                for row in feature["queues"][queue]:
                    if row.get("kind") == "spot_check":
                        continue  # Board review row, not another execution of this node.
                    try:
                        node = feature["dag"].node(row["node"])
                    except KeyError:
                        # A stale queue row (its node left the DAG) must not abort selection for every seat.
                        stale[f"{feature['name']}:{row.get('node')}"] = STALE_REASON
                        continue
                    if node.get("kind") in GATE_KINDS:
                        continue  # the owner's or the Board's decision: raise_gates asks it, no worker takes it
                    if channel.startswith("local:") and node.get("class", node.get("kind")) != channel.split(":", 1)[1]:
                        continue
                    key = f"{feature['name']}:{row['node']}"
                    if (row["node"] in feature.get("blocked", set())
                            or row["node"] in feature.get("parked", set())
                            or key in pending or key in used or node.get("m1") or node.get("m1_command")
                            or (queue == "intern" and not node["intern_friendly"])
                            or feature["state"].get(row["node"], {}).get("status") not in (None, "queued")
                            or not all(feature["state"].get(dep, {}).get("status") == "done"
                                       for dep in node["inputs"])):
                        continue
                    if not hint_allows(node.get("owner_hint"), channel):
                        hinted[key] = node["owner_hint"]
                        continue
                    # The owner's feature priority first (night 2026-10-17: a free seat went by DAG index and time, so
                    # a lower feature could take the seat a ready memory-scale node needed); unranked rows sort last
                    candidates.append(((feature.get("queue_rank", LAST_QUEUE_RANK), rank,
                                        node.get("kind") not in entry.get("prefers", []),
                                        row["priority"], row["t"], key),
                                       {"feature": feature["name"], "node": row["node"],
                                        "channel": channel, "by": worker["name"]}))
        if candidates:
            _, item = min(candidates, key=lambda pair: pair[0])
            chosen.append(item)
            used.add(f"{item['feature']}:{item['node']}")
            busy.add(channel)
        if len(chosen) == 3:
            break
    waiting = {key: hint for key, hint in hinted.items()
               if key not in used and not any(f"{c['feature']}:{c['node']}" == key for c in chosen)}
    if (collector := _WAITS) is not None:
        collector['waits'].update(waiting)  # tick() prints a line only when a node starts or stops waiting
        collector.setdefault('stale', {}).update(stale)  # tick() logs each quarantined row once
    else:
        for key, hint in sorted(waiting.items()):
            print(f"dispatch: {key} {WAIT_TEXT} {hint!r}", flush=True)
        for key, why in sorted(stale.items()):
            print(f"dispatch: {key} quarantined: {why}", flush=True)
    return chosen


def raise_gates(data, features, clock=time.time):
    """Ask the owner once per ready gate node: one checkpoint question (title as the question, acceptance as the why,
    the DAG guess if any) and one Board inbox line. Deduplicated on (feature, node) through a fixed run id, so a
    later tick or a re-promoted DAG never asks twice. Returns the (feature, node) pairs newly raised.
    2026-10-17: PG-YES "Board asks the owner" became ready, matched no hint family, and nothing ever asked the owner."""
    from companion.feature_team.checkpoints import Checkpoints, file_sha256

    data = Path(data)
    store = Checkpoints(data / 'team/loop', clock=clock)
    asked = {(row['feature'], row['node']) for row in store._load() if row['run_id'].startswith('gate-')}
    raised = []
    for feature in features:
        for node_id, node in feature['dag']._nodes.items():
            if (node.get('kind') not in GATE_KINDS or (feature['name'], node_id) in asked
                    or feature['state'].get(node_id, {}).get('status') in COMPLETED_STATUSES
                    or not all(feature['state'].get(dep, {}).get('status') == 'done' for dep in node['inputs'])):
                continue
            dag_path = live_path(data / 'team/feature-teams', feature['name'])
            one_line = lambda text: ' '.join(str(text or '').split())  # noqa: E731
            store.log(feature=feature['name'], node=node_id, run_id=f"gate-{feature['name']}-{node_id}",
                      what=one_line(node.get('title')) or node_id,
                      why=one_line(node.get('acceptance')) or 'gate node in the DAG',
                      guess=one_line(node.get('guess')) or 'wait for the yes',
                      artifact=str(dag_path), sha256=file_sha256(dag_path), reversible=False,
                      expiry=clock() + GATE_QUESTION_TTL, worker='dispatcher')
            board_inbox.append({'t': datetime.fromtimestamp(clock(), UTC).isoformat(), 'from': 'dispatcher',
                                'kind': 'gate_question', 'feature': feature['name'], 'node': node_id,
                                'routing_id': f"gate-question:{feature['name']}:{node_id}",
                                'text': f"{feature['name']}:{node_id} is ready and needs the owner's answer: "
                                        f"{one_line(node.get('title'))[:200]} (checkpoint question in "
                                        "data/team/loop/questions.jsonl; no worker takes a gate node)"},
                               path=data / 'team/board-inbox.jsonl', once=True)
            raised.append((feature['name'], node_id))
    return raised


WAIT_TEXT = 'waits for a free channel matching owner_hint'
_WAITS = None  # {'waits': {key: hint}, 'ran': bool} while a tick runs, so select() stays pure and quiet


def unservable_hints(table, features):
    """{feature:node: hint} for dependency-ready queued nodes whose hint names a family that no allowed table channel
    on the feature's team can take with a usable role (engineer; intern only for an intern_friendly node)."""
    stuck = {}
    for feature in features:
        for node_id, node in feature["dag"]._nodes.items():
            hint = node.get("owner_hint")
            if (hint_family(hint) is None or node_id in feature.get("blocked", set())
                    or feature["state"].get(node_id, {}).get("status") not in (None, "queued")
                    or not all(feature["state"].get(dep, {}).get("status") == "done" for dep in node["inputs"])):
                continue
            for entry in table:
                channel, worker_role = entry["channel"], role(entry["role"])
                if (allowed(channel) and worker_role in {"engineer", "intern"} and hint_allows(hint, channel)
                        and (worker_role == "engineer" or node.get("intern_friendly"))
                        and not (channel.startswith("local:") and worker_role != "intern")
                        and any(w["channel"] == channel and role(w["role"]) == worker_role for w in feature["team"])):
                    break
            else:
                stuck[f"{feature['name']}:{node_id}"] = hint
    return stuck


def report_hints(data, stuck, waits, now):
    """One Board row per unservable node and hint; one log line when a node starts or stops waiting."""
    for key, hint in sorted(stuck.items()):
        board_inbox.append({"t": datetime.fromtimestamp(now, UTC).isoformat(), "from": "dispatcher",
                            "kind": "owner_hint_unservable", "feature": key.partition(":")[0],
                            "node": key.partition(":")[2], "routing_id": f"owner-hint:{key}:{hint}",
                            "text": f"{key} cannot start: no allowed engineer channel on the team matches owner_hint "
                                    f"{hint!r}; change the hint or mark the node intern_friendly"},
                           path=data / "team/board-inbox.jsonl", once=True)
    path = data / "team/hint-waits.json"
    old = read(path, {})
    for key, hint in sorted(waits.items()):
        if old.get(key) != hint:
            print(f"dispatch: {key} {WAIT_TEXT} {hint!r}", flush=True)
    for key in sorted(set(old) - set(waits)):
        print(f"dispatch: {key} no longer waits for owner_hint {old[key]!r}", flush=True)
    if old != waits:
        save(path, waits)


STALE_REASON = 'queue row names a node missing from the DAG'


def stale_rows(features):
    """{feature:node: reason} for persisted queue rows naming a node the DAG no longer has. Scanned from the
    queues snapshot() read off disk, before any lane, readiness or autopilot.queued() filtering, so a busy
    lane or a loop roster cannot hide a stale row from quarantine (review 2026-10-20)."""
    stale = {}
    for feature in features:
        for queue in feature.get('queues', {}).values():
            for row in queue:
                if row.get('kind') == 'spot_check':
                    continue  # Board review row, not an execution of this node (select's rule)
                if row.get('node') not in feature['dag']._nodes:
                    stale[f"{feature['name']}:{row.get('node')}"] = STALE_REASON
    return stale


def report_stale(data, stale, scanned_features):
    """One log line per quarantined queue row, when it (re)appears; team/stale-queue.json holds what was said.
    Only ``scanned_features`` had their persisted queues and DAG inspected. Keep records for omitted features
    (e.g. missing team or DAG metadata); retire scanned records only when the row is gone or reconciled."""
    path = data / "team/stale-queue.json"
    old = read(path, {})
    stale = {**{key: why for key, why in old.items() if key.partition(':')[0] not in scanned_features},
             **stale}
    for key, why in sorted(stale.items()):
        if old.get(key) != why:
            print(f"dispatch: {key} quarantined: {why}", flush=True)
    if old != stale:
        save(path, stale)


@contextmanager
def locked(path):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save(path, document):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def _parks_path(data):
    return Path(data) / 'team/dispatch-parks.json'


def load_parks(data):
    """{feature:node: park} from team/dispatch-parks.json; anything unreadable parks nothing."""
    try:
        doc = read(_parks_path(data), {})
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _completion_row(data, feature, node):
    rows = read(Path(data) / 'team/features' / feature / 'completion.json', {})
    rows = rows.get('nodes', {}) if isinstance(rows, dict) else {}
    row = rows.get(node) if isinstance(rows, dict) else None
    return row if isinstance(row, dict) else {}


def _park_cleared(park, row):
    """A requeue (completion.mark back to queued, stamping a new ``t``) clears the park and its try count."""
    return row.get('status') == 'queued' and row.get('t') != park.get('row_t')


def parked_nodes(data, feature, state, now=None):
    """The feature's nodes parked after a refused brief: recorded parked, not requeue-cleared, and (when
    ``now`` is given) still inside their backoff. A park with no ``parked_until`` waits for a requeue."""
    parked = set()
    for park in load_parks(data).values():
        if not isinstance(park, dict) or park.get('feature') != feature or not park.get('parked', True):
            continue
        node = park.get('node')
        row = state.get(node) if isinstance(state, dict) else None
        if _park_cleared(park, row if isinstance(row, dict) else {}):
            continue
        until = park.get('parked_until')
        if now is not None and isinstance(until, (int, float)) and now >= until:
            continue
        parked.add(node)
    return parked


def park_refusal(data, feature, node, reason, now):
    """Park a node whose brief rq_brief refused: record the reason, double the backoff (1 min, cap 30 min),
    give up after PARK_MAX_TRIES tries, and leave one Board row per (node, reason). Returns the park."""
    data = Path(data)
    parks = load_parks(data)
    key = f'{feature}:{node}'
    row = _completion_row(data, feature, node)
    old = parks.get(key)
    attempts = (old['attempts'] + 1 if isinstance(old, dict) and isinstance(old.get('attempts'), int)
                and not _park_cleared(old, row) else 1)
    delay = min(PARK_BASE_S * 2 ** (attempts - 1), PARK_CAP_S)
    parks[key] = dict(feature=feature, node=node, reason=str(reason), attempts=attempts, parked=True,
                      parked_at=now, parked_until=None if attempts >= PARK_MAX_TRIES else now + delay,
                      row_t=row.get('t'))
    save(_parks_path(data), parks)
    board_inbox.append({'t': datetime.fromtimestamp(now, UTC).isoformat(), 'from': 'dispatcher',
                        'kind': 'dispatch_parked', 'feature': feature, 'node': node,
                        'routing_id': f'dispatch-park:{key}:'
                                      f'{hashlib.sha256(str(reason).encode()).hexdigest()[:12]}',
                        'text': f'{key} parked after a refused brief: {reason}; it is not re-picked while '
                                f'parked (try {attempts} of {PARK_MAX_TRIES}); fix the brief and requeue '
                                'the node to clear the park'},
                       path=data / 'team/board-inbox.jsonl', once=True)
    return parks[key]


def clear_park(data, feature, node):
    """Drop a node's park once a brief renders again, so a later refusal starts its tries from one."""
    parks = load_parks(data)
    if parks.pop(f'{feature}:{node}', None) is not None:
        save(_parks_path(data), parks)


def expire_parks(data, now):
    """Tick start, under the tick lock: a requeued node's park is deleted; an out-of-backoff park stops
    holding its node (its try count stays, so the next refusal doubles or gives up)."""
    parks = load_parks(data)
    changed = False
    for key, park in list(parks.items()):
        if not isinstance(park, dict):
            del parks[key]
            changed = True
        elif _park_cleared(park, _completion_row(data, park.get('feature'), park.get('node'))):
            del parks[key]
            changed = True
        elif (park.get('parked', True) and isinstance(park.get('parked_until'), (int, float))
                and now >= park['parked_until']):
            park['parked'] = False
            changed = True
    if changed:
        save(_parks_path(data), parks)


def queue_ranks(data):
    """{feature: index in data/team/feature-queue.json} (the owner's priority order); unreadable means no ranks."""
    try:
        entries = json.loads((data / "team/feature-queue.json").read_text()).get("features", [])
        return {e["name"]: i for i, e in enumerate(entries) if isinstance(e, dict) and isinstance(e.get("name"), str)}
    except (OSError, ValueError, AttributeError):
        return {}


def snapshot(data, now=None):
    features = []
    ranks = queue_ranks(data)
    for folder in sorted((data / "team/features").glob("*")):
        if not folder.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]+", folder.name):
            continue
        dag_path = live_path(data / "team/feature-teams", folder.name)
        if not (folder / "team.json").exists() or not dag_path.exists():
            continue
        queues = {}
        for queue in ("engineer", "intern"):
            path = folder / f"queue-{queue}.jsonl"
            queues[queue] = [json.loads(line) for line in path.read_text().splitlines()
                             if line.strip()] if path.exists() else []
        state = RootCompletion(folder)._read()
        features.append(dict(name=folder.name, team=read(folder / "team.json", []),
                             dag=load(dag_path), state=state, queues=queues,
                             parked=parked_nodes(data, folder.name, state, now),
                             queue_rank=ranks.get(folder.name, len(ranks))))
    return features


UNCERTAIN_TTL = 1800  # seconds a legacy pending row may hold its channel with no start proof and no run record


def expire_uncertain(journal, path, data, now, dry_run, clock=time.time):
    """A legacy journal row (no durable run record) whose receipt never became STARTED holds its channel with no
    deadline: T0 times out run records only, so one UNCERTAIN send parked a lane for good (home-team:H6B held
    codex:0). Stamp such a row when first seen and drop it after UNCERTAIN_TTL; the node then goes back
    to the queue. The bounded resend is the trade for a lane that otherwise never returns."""
    expired, changed = [], False
    for key, row in list(journal.items()):
        if delivery_receipt(str(row.get("receipt", "")))[0]:
            continue
        if row.get("run_id") and loop_store(data, clock).path(row["run_id"]).exists():
            continue
        seen = row.get("seen")
        if not isinstance(seen, (int, float)) or isinstance(seen, bool):
            row["seen"] = now
            changed = True
        elif now - seen >= UNCERTAIN_TTL:
            expired.append(key)
            del journal[key]
            changed = True
    if changed and not dry_run:
        save(path, journal)
    return expired


def delivery_receipt(reply):
    """Interpret the courier's shared start proof, retaining uncertain receipts."""
    started = re.search(r"^(?:(?:codex|cursor|grok) )?STARTED(?:$|[ :])[^\n]*",
                        reply, re.I | re.M)
    ledger = re.search(r"\b(?:ledger_id=|ledger id: )([\w-]+)", reply)
    receipt = {"receipt": started.group(0) if started else reply.strip()}
    if ledger:
        receipt["ledger_id"] = ledger.group(1)
    return bool(started), receipt


def hygiene_channel(data, lane, table):
    """Resolve observed lane aliases; ambiguous worker basenames go to the Board."""
    channels = {r['channel'] for r in table if lane_id(r['channel']) == lane}
    match = re.fullmatch(r'(codex|claude)-(\d+)', lane)
    if match:
        kind, index = match.groups()
        if kind == 'codex':
            # The sender resolves numbered lanes to their codex-exec worktrees.
            # App thread ids in config are observation metadata, not send targets.
            return f'codex:{index}'
        lanes = read(data / 'team/config.json', {}).get('lanes', [])
        if int(index) < len(lanes):
            participant = lanes[int(index)].get('participants', {}).get(kind, {})
            if participant.get('id'):
                return f"{kind}:{participant['id']}"
    return next(iter(channels)) if len(channels) == 1 else None


def hygiene_board(data, key, row, reason, clock):
    from companion.feature_team.fixback import append_once

    text = (f"{row['lane']}: write HANDOFF.md, then clear. {reason}. "
            "Board: ask the lane owner to act and verify context shrinks; "
            "receipt alone does not prove cleanup.")
    # A separate card leaves the planner's dated morning report free to be generated.
    card = data / 'team/MORNING-CONTEXT.md'
    marker = f'<!-- hygiene:{key} -->'
    card.parent.mkdir(parents=True, exist_ok=True)
    body = card.read_text() if card.exists() else '# Context cleaning for the morning Board\n\n'
    if marker not in body:
        card.write_text(body + f'- {text} {marker}\n')
    append_once(data / 'team/board-inbox.jsonl', key, {
        't': clock(), 'from': 'dispatcher-hygiene', 'kind': 'routed',
        'lane': row['lane'], 'text': text + ' See data/team/MORNING-CONTEXT.md'})


def route_hygiene(data, table, sender, clock=time.time, dry_run=False, limit=3):
    """Consume each over-limit episode once under the dispatcher's global lock."""
    from companion.feature_team.fixback import rows

    active = {}
    for row in rows(data / 'team/board-inbox.jsonl'):
        lane = row.get('lane')
        if not isinstance(lane, str):
            continue
        if row.get('kind') == 'hygiene_recovered':
            active.pop(lane, None)
        elif row.get('kind') == 'hygiene':
            active.setdefault(lane, row)  # Cooldown reminders are the same episode.
    path = data / 'team/dispatcher-hygiene.json'
    journal = read(path, {})
    attempts, channels = 0, set()
    result = dict(sent=0, board=0, deferred=0)
    for lane, row in active.items():
        key = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        target = hygiene_channel(data, lane, table)
        old = journal.get(key, {})
        if old.get('status') == 'done':
            continue
        if target:
            channels.add(target)
            channels.update(r['channel'] for r in table if lane_id(r['channel']) == lane)
        reason = 'Claude cleanup requires the Board' if lane.startswith('claude-') else 'lane owner unavailable or ambiguous'
        sendable = target and allowed(target) and target.partition(':')[0] in {'codex', 'cursor'}
        if old.get('status') == 'sending':
            sendable = False
            reason = 'uncertain delivery; reconcile before retry'
        if sendable:
            modes = {}
            pauses = budget_pauses(data / 'team/model-budget.json', clock(), modes)
            family = next((r.get('family', target.partition(':')[0]) for r in table
                           if r['channel'] == target or lane_id(r['channel']) == lane), target.partition(':')[0])
            provider = family.split('-', 1)[0]
            if (family in pauses or provider in pauses or modes.get(provider, modes.get(family)) == 'eco'
                    or attempts >= limit):
                result['deferred'] += 1
                continue
        if dry_run:
            continue
        record = {**old, 'lane': lane, 'channel': target, 'status': 'sending'}
        if sendable:
            journal[key] = record
            save(path, journal)  # Reserve before transport; a crash must not duplicate a send.
            attempts += 1
            try:
                reply = sender(dict(channel=target, message=(
                    'Context hygiene: write HANDOFF.md, then clear. Preserve current work and '
                    'record the handoff path and before/after context size for the Board. '
                    'Do not delete transcript history.')))
            except Exception:
                reply = 'UNCERTAIN'
            started, receipt = delivery_receipt(reply)
            record.update(receipt)
            if started:
                journal[key] = {**record, 'status': 'done', 'outcome': 'started'}
                save(path, journal)
                result['sent'] += 1
                continue
            reason = ('NOT STARTED' if 'NOT STARTED' in reply.upper() and 'QUEUED' not in reply.upper()
                      else 'uncertain delivery; reconcile before retry')
            journal[key] = record
            save(path, journal)
        hygiene_board(data, key, row, reason, clock)
        journal[key] = {**record, 'status': 'done', 'outcome': 'board'}
        save(path, journal)
        result['board'] += 1
    return attempts, channels, result


def tick(data, *, sender, clock=time.time, dry_run=False):
    global _WAITS
    mine = _WAITS = {'waits': {}, 'stale': {}, 'ran': False}
    try:
        result = _tick(data, sender=sender, clock=clock, dry_run=dry_run)
        if not dry_run:
            if mine['ran']:
                report_hints(Path(data), mine.get('stuck', {}), mine['waits'], clock())
            if mine.get('stale_checked'):
                # scanned from the persisted queues at tick start: reported even on a stopped or busy tick
                report_stale(Path(data), mine['stale'], mine['stale_features'])
        return result
    finally:
        _WAITS = None


def _tick(data, *, sender, clock=time.time, dry_run=False):
    data = Path(data)
    team_dir = data / "team"
    result = dict(planned=0, sent=0, not_started=0, uncertain=0, stop_reasons=[])
    # ponytail: one global tick lock; feature locks also serialize the existing E5 dispatcher.
    with nullcontext() if dry_run else locked(team_dir / "dispatcher.lock"):
        from companion.feature_team import jev_hooks
        if not dry_run:
            jev_hooks.settle(data)
            expire_parks(data, clock())
        features = snapshot(data, now=clock())
        if (collector := _WAITS) is not None:
            # Quarantine from the persisted queues before lanes/readiness filter anything: a busy tick must
            # retain the record, and a loop roster's queued() projection must not hide the row (review).
            collector['stale'].update(stale_rows(features))
            collector['stale_features'] = {feature['name'] for feature in features}
            collector['stale_checked'] = True
        table_path = team_dir / "dispatch-table.json"
        defaults = {(role(w["role"]), w["channel"]) for f in features for w in f["team"]
                    if role(w["role"]) in {"engineer", "intern"} and allowed(w["channel"])}
        table = read(table_path, [dict(role=r, channel=c, prefers=[]) for r, c in sorted(defaults, key=lambda pair: pair[1])])
        from companion.feature_team.muse import dispatch_table
        table = dispatch_table(table, data)
        if not dry_run and defaults and not table_path.exists():
            save(table_path, table)
        journal_path = team_dir / "dispatcher-pending.json"
        journal = read(journal_path, {})
        expired = expire_uncertain(journal, journal_path, data, clock(), dry_run, clock)
        if expired:
            result["expired"] = expired
        watch = read(team_dir / "lane-watch.json", {})
        lanes = watch.get("lanes", [])
        if "observed_at" in watch and clock() - watch["observed_at"] > 600:
            lanes = []  # Stale observations are not permission to start work.
        skipped = {}
        blocked, stops = containment(data, features)
        loop, reservations = loop_tick(data, table, lanes, clock=clock, dry_run=dry_run, stopped=bool(stops))
        features = snapshot(data, now=clock())  # Harvest may have changed completion and author rows.
        blocked, stops = containment(data, features)
        if not dry_run:
            result["gates_raised"] = raise_gates(data, features, clock)
        pending = {**journal, **reservations}
        if stops:
            result.update(blocked_nodes=blocked, stop_reasons=stops, loop=loop)
            return result

        def plan_next(excluded):
            from companion.feature_team import picker
            from companion.feature_team.autopilot import queued, resumed

            modes = {}
            pauses = budget_pauses(team_dir / 'model-budget.json', clock(), modes)
            current = snapshot(data, now=clock())
            # R14: the picker's rollback rank orders the lanes, then budget_table filters (and keeps Fable
            # after the other Claude builders) as the last step before the pure select.
            eligible = budget_table(picker.rank([row for row in table if row['channel'] not in excluded],
                                                current, data / 'team/rollback.json'), pauses, skipped, modes)
            queued(resumed(data, [f for f in current if any(
                w['channel'].partition(':')[0] in LOOP_CHANNELS for w in f['team'])], clock()))
            _, stop = containment(data, current)
            return [] if stop else select(eligible, lanes, current, pending)

        from companion.feature_team.fixback import route
        from companion.feature_team.ladder import tick as ladder_tick

        routed, routed_channels = route(data, features, table, sender, clock, dry_run)
        hygiene_attempts, hygiene_channels, hygiene = route_hygiene(
            data, table, sender, clock, dry_run, limit=3 - routed)
        routed += hygiene_attempts
        routed_channels |= hygiene_channels
        ladder = ladder_tick(data, features, table, sender, clock, dry_run)
        from companion.feature_team.autopilot import prepare

        legacy_sender = sender

        def node_sender(item):
            from companion.feature_team import contract
            from companion.feature_team.autopilot import capacity_refusals

            kind = item['channel'].partition(':')[0]
            if kind in LOOP_CHANNELS:
                reasons = capacity_refusals(data, item['channel'])
                if reasons:
                    return 'NOT STARTED: ' + '; '.join(reasons)
                # Refuse an incompatible integrated event schema before R0.create
                # can leave a half-published record (LX and E0 land separately).
                from companion.feature_team import events
                try:
                    events.validate(dict(t=clock(), run_id=item['run_id'], feature=item['feature'],
                                         node=item['node'], worker='dispatcher', channel=item['channel'],
                                         kind='queued', sha=None, path=None, detail='record created'))
                except ValueError as exc:
                    reason = f'loop event schema refuses channel: {exc}'
                    loop['refusals'].append(dict(run_id=item['run_id'], reasons=[reason]))
                    return 'NOT STARTED: ' + reason
                try:
                    launched = mint_run(item, data, clock)
                except (ValueError, OSError) as exc:
                    if loop_store(data, clock).path(item['run_id']).exists():
                        raise
                    reason = f'brief refused: {exc}'
                    loop['refusals'].append(dict(run_id=item['run_id'], reasons=[reason]))
                    events.EventStore(data).append(dict(
                        t=clock(), run_id=item['run_id'], feature=item['feature'], node=item['node'],
                        worker='dispatcher', channel=item['channel'], kind='dispatch_refused',
                        sha=None, path=None, detail=' '.join(reason.split())[:contract.DETAIL_MAX]))
                    if isinstance(exc, ValueError):  # rq_brief refused the brief; an OSError may be transient
                        park_refusal(data, item['feature'], item['node'], reason, clock())
                    return 'NOT STARTED: ' + reason
                clear_park(data, item['feature'], item['node'])
                if launched:
                    loop['launched'].append(item['run_id'])
                return 'QUEUED: durable run ' + item['run_id']
            # E0 on this integration base predates LX's new observation kinds.
            # Keep the contract-valid refusal in the dispatcher's audit stream.
            row = contract.validate_event(dict(
                t=clock(), run_id=contract.new_run_id(item['feature'], item['node'], clock=clock),
                feature=item['feature'], node=item['node'],
                worker='dispatcher', channel=item['channel'], kind='dispatch_refused',
                sha=None, path=None, detail=f'loop cannot serve channel kind {kind}; using legacy sender'))
            from companion.feature_team.fixback import rows
            path = team_dir / 'dispatcher-events.jsonl'
            if not any(all(old.get(k) == row[k] for k in ('feature', 'node', 'channel', 'kind'))
                       for old in rows(path)):
                with path.open('a') as stream:
                    stream.write(json.dumps(row) + '\n')
            return legacy_sender(item)

        features, table, plan_next, sender, limit, autopilot = prepare(
            data, features, table, lanes, pending, node_sender, plan_next, 3 - routed,
            clock=clock, dry_run=dry_run, excluded=routed_channels)
        raw_plan = plan_next
        identities = {}
        from companion.feature_team.autopilot import roster_table
        if (waits := _WAITS) is not None:
            waits['stuck'] = unservable_hints(roster_table(table, features), features)
            waits['ran'] = True

        def plan_next(excluded):
            # B0 warns on result-only inputs. Completion's legacy done marker
            # cannot override an explicit unverified R0 record.
            from companion.feature_team import runs
            pending.clear()
            pending.update(journal)
            pending.update(reservations)
            records = runs.Runs(data / 'team/loop').load_all()
            latest = {}
            for rec in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
                latest[rec['feature'], rec['node']] = rec
            result = []
            for item in raw_plan(excluded):
                node = next(f['dag'].node(item['node']) for f in features if f['name'] == item['feature'])
                if any((rec := latest.get((item['feature'], dep))) and rec['state'] not in runs.VERIFIED
                       for dep in node['inputs']):
                    continue
                key = (item['feature'], item['node'], item['channel'])
                if item['channel'].partition(':')[0] in LOOP_CHANNELS:
                    if key not in identities:
                        identities[key] = run_identity(item, features, data, clock)
                    item = identities[key]
                result.append(item)
            return result

        plan = plan_next(routed_channels)
        result.update(planned=len(plan), blocked_nodes=blocked, ladder=ladder, hygiene=hygiene, loop=loop)
        if autopilot:
            result['autopilot'] = autopilot
        if dry_run:
            result['would_mint'] = [{k: item[k] for k in ('feature', 'node', 'run_id', 'channel', 'model', 'effort')}
                                    for item in plan if item.get('run_id')]
            return result
        attempted = set(routed_channels)
        for _ in range(limit):
            plan = plan_next(attempted)
            if not plan:
                break
            item = plan[0]
            attempted.add(item['channel'])
            folder = team_dir / "features" / item["feature"]
            completion = RootCompletion(folder)
            with locked(folder / "dispatch.lock"), completion._locked():
                state = completion._read()
                node = next(f["dag"].node(item["node"]) for f in features if f["name"] == item["feature"])
                authors = next({w["name"]: w["channel"] for w in f["team"]}
                               for f in features if f["name"] == item["feature"])
                loop_runs = loop_store(data, clock)  # not "loop": that name is the tick's loop report
                stale = stale_holds(loop_runs.load_all(), item["feature"], state, runs_dir=loop_runs.runs_dir)
                if (state.get(item["node"], {}).get("status") not in (None, "queued")
                        or not all(state.get(dep, {}).get("status") == "done" for dep in node["inputs"])
                        or any(holder_channel(row, authors) == item["channel"] and row["status"] == "dispatched"
                               and n not in stale for n, row in state.items())):
                    continue
                key = f"{item['feature']}:{item['node']}"
                journal[key] = item
                save(journal_path, journal)  # Crash/timeout after delivery must not resend blindly.
                try:
                    reply = sender(item)
                except Exception:
                    reply = "UNCERTAIN"
                started, receipt = delivery_receipt(reply)
                durable = item.get('run_id') and loop_store(data, clock).path(item['run_id']).exists()
                if started or durable:
                    jev_hooks.assignment(data, item, node, table)
                    stamp = datetime.fromtimestamp(clock(), UTC).isoformat()
                    state[item["node"]] = {**state.get(item["node"], {}), "status": "dispatched",
                                           "by": item["by"], "channel": item["channel"], "t": stamp, **receipt}
                    completion._write(state)
                    board_inbox.append({"t": stamp, "from": "dispatcher", "kind": "dispatched",
                                        "text": f"{key} dispatched to {item['channel']}",
                                        **receipt}, path=team_dir / "board-inbox.jsonl")
                    journal.pop(key)
                    if durable:
                        reservations[key] = dict(channel=item['channel'])
                        result.setdefault('created', []).append(item['run_id'])
                    else:
                        result["sent"] += 1
                elif "NOT STARTED" in reply.upper() and "QUEUED" not in reply.upper():
                    journal.pop(key)
                    result["not_started"] += 1
                else:
                    journal[key] = {**item, **receipt}
                    result["uncertain"] += 1
                save(journal_path, journal)
        if skipped:
            stamp = datetime.fromtimestamp(clock(), UTC).isoformat()
            board_inbox.append({'t': stamp, 'from': 'dispatcher', 'kind': 'model_budget',
                                'text': '; '.join(f'Skipped {family}: {reason}'
                                                  for family, reason in sorted(skipped.items()))},
                               path=team_dir / 'board-inbox.jsonl')
        return result


def send_local(item, brief, data):
    from companion.local_lane import run
    path = run(item['channel'].split(':', 1)[1], brief, root=data,
               feature=item['feature'], node=item['node'], queue_locked=True)
    return f'STARTED: local draft {path}'
