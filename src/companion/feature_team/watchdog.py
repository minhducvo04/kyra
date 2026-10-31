"""Five-minute, no-LLM watchdog over R0 runs, L0 leases and E0 events.

A stale worker moves to R0's retryable ``timeout`` state once; only the dispatcher
sends the next attempt. Completion files and leases are never changed here. A
missing/stalled dispatcher is reported, never started by the watchdog. Each
new decision emits one F0 watchdog event; actionable decisions also use the
existing Board inbox shape. Repeated alerts are suppressed until their evidence
changes. Dry runs do not create indexes, locks or files, and only invoke read-only Git commands.

R0 records may specify heartbeat_interval and receipt_timeout (seconds); older
records default to 300 seconds each. Dispatcher tick events may specify
tick_interval in their JSON detail (default 300 seconds).
"""

import json
import math
import re
import sqlite3
import time
from contextlib import closing, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from companion import board_inbox
from companion.feature_team import autopilot, contract, dispatch, events, inspector, locks, runs

HEARTBEAT_INTERVAL = 300
RECEIPT_TIMEOUT = 300
DISPATCHER_TICK_SECONDS = 300
ALERT_SILENT = 'ready but not dispatched, no reason logged'
ALERT_STALLED = 'loop stalled: ready nodes wait while slots idle'
# Night 2026-10-17: every ready node had a refusal reason, so ALERT_SILENT stayed quiet, and check_loop said
# "stuck_loop: none; ok" for five hours with no worker running. Ready nodes beside idle slots (or beside no live run
# at all) for STALL_ALARM_SECONDS is a stuck loop whatever the reasons say (memory: stall alarm 20 min).
STALL_ALARM_SECONDS = 20 * 60
BUSY = frozenset({'dispatched', 'started', 'heartbeat', 'result', 'verify_fail'})
# Critical-path watch (owner, 2026-10-15):
# a train carrying priority 1 to 3 work, a priority 1 to 3 node's run, or a GPU job for long-input with no forward
# event for STALL_SECONDS gets one critical_path_stall event and one inbox row addressed to the home chat.
STALL_SECONDS = 15 * 60
HORIZON_SECONDS = 24 * 3600  # an unfinished state file older than this is not today's critical path
CRITICAL_RANK = 3
LAST_RANK = 99
TRAIN_FINAL = ('landed ',)  # a refusal is a stall, not a finish (train46 sat refused for an hour, 2026-10-15)
BUSY_OR_STUCK = BUSY | {'timeout'}  # a timed-out run nobody redispatched is stuck, not progressing


DISPATCHER_LOCK_WAIT = 2.0  # seconds the inspector waits for dispatcher.lock before it skips one tick


def _seconds(record, name, default):
    value = record.get(name, record.get(name + '_s', default))
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else default


def _stale(record, now):
    state = record['state']
    if state not in {'dispatched', 'started', 'heartbeat'}:
        return None
    stamp = record['updated']
    if state == 'dispatched':
        limit = _seconds(record, 'receipt_timeout', RECEIPT_TIMEOUT)
        check, reason = 'never_started', 'no start receipt'
    else:
        limit = 3 * _seconds(record, 'heartbeat_interval', HEARTBEAT_INTERVAL)
        check, reason = 'silent', 'silent worker'
    if now - stamp > limit:
        return check, f'{reason}: {record["run_id"]}, {int(now - stamp)} s since {state}; limit {limit:g} s'
    return None


def _held(data):
    """Use L0's claim reader without its constructor's schema writes/BEGIN IMMEDIATE."""
    database = data / 'team/loop/locks.sqlite3'
    if not database.exists():
        return []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return locks.FileLocks._active(db)


class Tick:
    def __init__(self, data, *, dispatcher, git, clock=time.time, dry_run=False, probes=None):
        self.data = Path(data).resolve()
        self.probes = probes or {}  # inspector seams: alive(pid) -> bool, commits(record) -> int | None
        self.dispatcher, self.git, self.clock = dispatcher, git, clock
        self.now, self.dry_run = clock(), dry_run
        self.log = []
        self.events = events.EventStore(self.data)
        self.runs = runs.Runs(self.data / 'team/loop', emit=self.events.append, clock=clock)
        self.state_path = self.data / 'team/watchdog-state.json'
        self.state = dispatch.read(self.state_path, {})
        self.nudge = False
        self.stall = None  # set by check_slots (track_stall), read by check_loop
        self.dispatcher_alive = False

    def line(self, check, finding, action='none', result='ok', *, record=None, emit=True):
        row = dict(t=datetime.fromtimestamp(self.now, UTC).isoformat(), check=check,
                   finding=finding, action=action, result=result)
        if record:
            row['run_id'] = record['run_id']
        self.log.append(row)
        detail = f'{check}: {action}: {finding}; {result}'
        encoded = ''.join(c if c.isprintable() else ' ' for c in detail)[:contract.DETAIL_MAX]
        rec = record or {}
        event = contract.validate_event(dict(
            t=self.now, run_id=rec.get('run_id'), feature=rec.get('feature'), node=rec.get('node'),
            worker='watchdog', channel=rec.get('brief', {}).get('channel'), kind='watchdog',
            sha=None, path=str(self.runs.path(rec['run_id'])) if rec.get('run_id') else None, detail=encoded))
        if not self.dry_run:
            path = self.data / 'team/watchdog.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
            if emit:
                self.events.append(event)
        return row

    def report(self, check, finding, needs, evidence, tried, *, record=None, token=None, action='inbox'):
        """One inbox row and watchdog event per incident, including across process restarts."""
        key = f'{check}:{finding}'
        token = token if token is not None else evidence
        if self.state.get(key) == token:
            return self.line(check, finding, 'none', 'already reported; unchanged evidence', record=record, emit=False)
        if self.dry_run:
            return self.line(check, finding, action, f'would post: {needs}', record=record)
        row = dict(t=self.now, **{'from': 'watchdog'}, kind='request', reason='unknown',
                   node=f'{record["feature"]}:{record["node"]}' if record and record.get('feature') else finding,
                   needs=needs, evidence=evidence, tried=tried, text=needs)
        path = self.data / 'team/board-inbox.jsonl'
        board_inbox.append(row, path=path)
        result = self.line(check, finding, action, needs, record=record)
        self.state[key] = token
        dispatch.save(self.state_path, self.state)
        return result

    def run(self):
        # E0.query() builds a SQLite index. Its JSONL reader is validated and read-only.
        self.event_rows = self.events._read()
        self.records = self.runs.load_all()
        self.check_dispatcher()
        self.check_critical_path()  # before check_workers moves a silent run on, so the stall is still visible
        self.check_inspector()  # a dead worker is closed and requeued before the slower stale rules look
        self.check_workers()
        self.check_locks()
        self.check_slots(dispatch.snapshot(self.data))
        self.check_unlanded()
        self.check_loop()
        return self.log

    def check_dispatcher(self):
        ticks = [r for r in self.event_rows if r['kind'] == 'tick' and r['worker'] == 'dispatcher']
        if not ticks:
            self.report('dispatcher', 'WD', 'no dispatcher tick event; dispatcher health unknown',
                        'E0 has no dispatcher tick', 'read E0; no restart attempted', token='missing')
            return
        last = max(ticks, key=events.event_time)
        age = self.now - events.event_time(last)
        interval = _seconds(events.metadata(last['detail']), 'tick_interval', DISPATCHER_TICK_SECONDS)
        if age > 3 * interval:
            self.report('dispatcher', 'WD', f'dispatcher down: last tick {int(age)} s ago',
                        f'last tick {last["t"]}; interval {interval:g} s',
                        'read E0; no restart attempted', token=last['t'])
            return
        self.dispatcher_alive = True
        self.line('dispatcher', f'last tick {int(age)} s ago')

    def check_inspector(self):
        """Expected-duration milestones per live run, dead-worker close and requeue (feature_team.inspector). Its
        record writes hold the dispatcher's lock, so a dispatcher tick never mints a successor beside it."""
        context = nullcontext(True) if self.dry_run else inspector.try_locked(self.data / 'team/dispatcher.lock',
                                                                                DISPATCHER_LOCK_WAIT)
        try:
            with context as held:
                if not held:
                    # a long harvest or a hung dispatcher holds its lock: skip this tick, never freeze the watchdog
                    streak = 0 if self.dry_run else inspector.note_lock_skip(self.data, self.now)
                    self.line('inspector', 'dispatcher.lock busy', 'none',
                              f'skipped this tick: the dispatcher held its lock past {DISPATCHER_LOCK_WAIT:g} s '
                              f'({streak} in a row)')
                    return None
                summary = inspector.Inspector(
                    self.data, store=self.runs, now=self.now, dry_run=self.dry_run, line=self.line,
                    emit=self.events.append, **self.probes).run(self.runs.load_all())
        except Exception as exc:  # noqa: BLE001  the other checks still run
            self.line('inspector', 'error', 'none', f'inspector failed: {type(exc).__name__}: {exc}'[:300])
            return None
        self.records = self.runs.load_all()
        if summary['requeued']:
            self.nudge = True  # check_slots asks the dispatcher for one tick, which starts the queued successor
        return summary

    def check_workers(self):
        if not self.records:
            self.line('silent', 'no run records', result='no run_id evidence; legacy activity is not used')
        for rec in self.records.values():
            finding = _stale(rec, self.now)
            if finding is None:
                self.line('runs', rec['run_id'], result=f'run state {rec["state"]}', record=rec)
                continue
            check, why = finding
            # Re-read before acting: a newer receipt/result or a replacement run wins.
            current = self.runs.load_all()
            latest = current.get(rec['run_id'])
            siblings = [r for r in current.values() if r['run_id'] != rec['run_id']
                        and (r['feature'], r['node']) == (rec['feature'], rec['node'])
                        and r['state'] not in {'timeout', 'escalated', 'fixback'} and _stale(r, self.now) is None]
            if latest != rec or siblings:
                self.line(check, rec['run_id'], result='record moved on or node has a live/finished run', record=rec)
                continue
            if self.dry_run:
                self.line(check, rec['run_id'], 'requeue', f'would requeue through timeout: {why}', record=rec)
            else:
                self.records[rec['run_id']] = self.runs.transition(
                    rec['run_id'], 'timeout', worker='watchdog', detail=why[:contract.DETAIL_MAX])
                self.report(check, rec['run_id'], f'requeued through timeout: {why}', why,
                            'R0 timeout transition; dispatcher owns the next attempt', record=rec,
                            token=rec['updated'], action='requeue')
            self.nudge = True

    def check_locks(self):
        expired = [r for r in _held(self.data) if r['deadline'] <= self.now]
        for claim in expired:
            rec = self.records.get(claim['run_id'], dict(run_id=claim['run_id']))
            self.report('stuck_lock', claim['run_id'],
                        f'Expired held lock: {claim["run_id"]} owner {claim["owner"]}',
                        f'deadline {claim["deadline"]}; files {json.dumps(claim["files"])}',
                        'read L0; release/steal belongs to the dispatcher', record=rec,
                        token=[claim['owner'], claim['deadline'], claim['files']])
        if not expired:
            self.line('stuck_lock', 'none')

    def check_slots(self, features):
        # Read the derived R0 index without index(), which rebuilds it on a missing read.
        # Records override stale index entries, and include records written before a crash.
        self.records = self.runs.load_all()
        try:
            index = dispatch.read(self.runs.index_path, {}).get('features', {})
        except (OSError, ValueError):
            index = {}  # R0 records survive a missing or torn derived index.
        latest = {(f, n): entry['latest'] for f, nodes in index.items() for n, entry in nodes.items()}
        for rec in sorted(self.records.values(), key=lambda r: (r['created'], r['run_id'])):
            latest[rec['feature'], rec['node']] = rec['run_id']
        busy = {r['brief']['channel'] for r in self.records.values() if r['state'] in BUSY}
        rostered = {w['channel'] for f in features for w in f['team']
                    if dispatch.role(w.get('role')) in {'engineer', 'intern'} and dispatch.allowed(w['channel'])}
        lane_rows = dispatch.read(self.data / 'team/lane-watch.json', {}).get('lanes', [])
        channels = {r.get('channel') or next((c for c in rostered if dispatch.lane_id(c) == r.get('lane')), '')
                    for r in lane_rows}
        idle = sorted((channels & rostered) - busy)
        for feature in features:
            # Completion remains a fallback for pre-R0 verified dependencies, never liveness.
            state = {n: r for n, r in feature['state'].items() if r.get('status') == 'done'}
            for (name, node), rid in latest.items():
                if name != feature['name'] or rid not in self.records:
                    continue
                rec = self.records[rid]
                status = ('done' if rec['state'] in runs.VERIFIED else 'queued'
                          if rec['state'] in {'queued', 'timeout', 'fixback'} else 'dispatched')
                state[node] = dict(status=status)
            # Even an older run may still own the node; never schedule over it.
            for rec in self.records.values():
                if rec['feature'] == feature['name'] and rec['state'] in BUSY:
                    state[rec['node']] = dict(status='dispatched')
            feature['state'] = state
        ready = [f'{f["name"]}:{n}' for f in features for n in autopilot.ready(f)]
        queue = dispatch.read(self.data / 'team/feature-queue.json', {}).get('features', [])
        started = {f['name'] for f in features}
        waiting = [e['name'] for e in queue if isinstance(e, dict) and e.get('name') not in started
                   and not (self.data / 'team/features' / str(e.get('name')) / 'completion.json').exists()]
        self.track_stall(ready, idle, busy)
        if ready and idle:
            finding = f'{len(ready)} ready node(s) wait while {len(idle)} slot(s) idle'
        elif not ready and waiting and not busy:
            finding = f'queued feature {waiting[0]} waits'
        elif self.nudge:
            finding = 'requeued run(s) wait for the dispatcher'
        else:
            if not ready:  # nothing ready is nothing silent; ready nodes beside no idle slot were not ticked
                self.clear_alert(ALERT_SILENT)
            return self.line('slots', 'none')
        if not self.dispatcher_alive:
            return self.line('slots', finding, result='dispatcher unavailable; no tick attempted')
        if self.dry_run:
            return self.line('slots', finding, 'dispatcher tick', 'would request one dispatcher tick')
        result = self.dispatcher(False)
        summary = json.dumps(result, sort_keys=True)
        self.report('slots', 'WD', f'dispatcher tick: {finding}', summary,
                    'one dispatcher tick', token=self.now, action='dispatcher tick')
        # FL window 6 (2026-10-16): 11 ready nodes, idle slots, and the tick logged no dispatch and no refusal.
        # A ready node the tick neither sent nor refused by name is an ALERT for the home chat, not an info line.
        pilot = result.get('autopilot') if isinstance(result, dict) else None
        pilot = pilot if isinstance(pilot, dict) else {}
        explained = {f"{r.get('feature')}:{r.get('node')}" for r in pilot.get('slots', []) + pilot.get('refusals', [])
                     if isinstance(r, dict)}
        silent = [key for key in ready if key not in explained]
        # Only the queue autopilot names refusals per node; a legacy (queue-less) tick reports counts alone.
        # `silent` already leaves out the planned slots, so a sent count is not subtracted again (review
        # 2026-10-16: slots=[N0], sent=1 hid an unexplained N1).
        if pilot and silent:
            self.report_alert(ALERT_SILENT, silent, summary)
        elif pilot:
            self.clear_alert(ALERT_SILENT)

    def track_stall(self, ready, idle, busy):
        """Stamp when ready nodes started waiting beside an idle slot or no live run; keep the stamp while that
        lasts and drop it the moment it ends. check_loop reads self.stall."""
        self.stall = None
        if not ready or not (idle or not busy):
            if not self.dry_run and self.state.pop('stall:since', None) is not None:
                dispatch.save(self.state_path, self.state)
            return
        since = self.state.get('stall:since')
        if not isinstance(since, (int, float)) or isinstance(since, bool) or since > self.now:
            since = self.now
            if not self.dry_run:
                self.state['stall:since'] = since
                dispatch.save(self.state_path, self.state)
        if self.now - since >= STALL_ALARM_SECONDS:
            self.stall = dict(minutes=int((self.now - since) // 60), ready=ready, idle=idle, busy=sorted(busy),
                              since=since)

    def clear_alert(self, what):
        """Forget an incident's dedupe once nothing is silent, so the same node set stuck again alerts again."""
        if not self.dry_run and self.state.pop(f'alert:{what}', None) is not None:
            dispatch.save(self.state_path, self.state)

    def report_alert(self, what, nodes, evidence, *, token=None):
        """One ALERT row for the home chat per incident (deduped like report_stall on the node set, or ``token``)."""
        key, token = f'alert:{what}', token if token is not None else ','.join(sorted(nodes))
        if self.state.get(key) == token:
            return self.line('alert', what, 'none', 'already reported; unchanged evidence', emit=False)
        needs = f'{what}: {len(nodes)} ready node(s) ({", ".join(nodes[:12])}); read the dispatcher dry run'
        if self.dry_run:
            return self.line('alert', what, 'home chat', f'would post: {needs}')
        board_inbox.append(dict(t=self.now, **{'from': 'watchdog'}, kind='request', reason='unknown', level='ALERT',
                                to='home-chat', node=nodes[0], needs=needs, evidence=evidence[:2000],
                                tried='one dispatcher tick', text=needs), path=self.data / 'team/board-inbox.jsonl')
        result = self.line('alert', what, 'home chat', needs)
        self.state[key] = token
        dispatch.save(self.state_path, self.state)
        return result

    def check_unlanded(self):
        """Verified work that is not on origin/master is reported to the Board inbox once, with its branch
        (recorded, else a unique branch tip). The watchdog never writes the ready queue: verify_pass is the
        runner's verdict, not a review, and the autopilot lands whatever is marked without checking review
        (review o-rev53, 2026-10-15). A marker already pending or a commit the tick refused is left alone."""
        from companion.feature_team import repos

        def kyra(feature):
            try:
                return repos.for_feature(self.data, feature).is_kyra
            except ValueError:
                return True  # an unlisted repo is reported as before; the dispatcher refuses it anyway

        completions = {}
        for path in sorted((self.data / 'team/features').glob('*/completion.json')):
            if not kyra(path.parent.name):
                continue  # another repo's landing is the Board's: done at verify_pass, nothing to report here
            for node, row in dispatch.read(path, {}).get('nodes', {}).items():
                if row.get('status') == 'done' and row.get('sha'):
                    completions[path.parent.name, node] = row
        candidates = []
        recorded = {(r['feature'], r['node']) for r in self.records.values()}
        for rec in self.records.values():
            if rec['state'] != 'verify_pass' or not rec.get('sha') or not kyra(rec['feature']):
                continue
            row = completions.get((rec['feature'], rec['node']), {})
            branch = row.get('branch') if row.get('sha') == rec['sha'] else None
            candidates.append((rec['run_id'], rec['sha'], branch or rec.get('branch'), rec))
        for (feature, node), row in completions.items():
            if (feature, node) not in recorded:
                candidates.append((f'{feature}:{node}', row['sha'], row.get('branch'), None))
        from companion import landing_queue

        # the one ready-to-land queue (audit row 11): the autopilot's markers, not the old landing-queue rows
        queued = {sha for _, sha, _ in landing_queue.pending(self.data)}
        refused = landing_queue.refused(self.data)
        found = False
        for finding, sha, branch, rec in candidates:
            if sha in queued or sha in refused or any(sha.startswith(q) or q.startswith(sha) for q in queued):
                continue
            if self.git(['merge-base', '--is-ancestor', sha, 'origin/master']) is not False:
                continue
            found = True
            if not branch:
                tips = (self.git(['branch', '--points-at', sha, '--format=%(refname:short)',
                                  '--list', 'session/*']) or '').splitlines()
                if len(tips) == 1:
                    branch = tips[0]
                else:
                    containing = (self.git(['branch', '--contains', sha, '--format=%(refname:short)',
                                            '--list', 'session/*']) or '').splitlines()
                    branches = sorted(set(tips + containing))
                    needs = f'find its branch: {finding} @ {sha}; candidates: {", ".join(branches) or "none"}'
                    self.report('unlanded', finding, needs, needs,
                                'read completion/R0 and git branch tips; no unique branch', record=rec)
                    continue
            needs = (f'{branch} @ {sha} is verified but not on origin/master and has no independent review PASS; '
                     f'a reviewer marks it ready (autopilot.py ready) or the planner round does on its PASS')
            self.report('unlanded', finding, needs, sha,
                        'completion/R0, git ancestry, the ready queue and ticks.log; the watchdog marks nothing',
                        record=rec)
        if not found:
            self.line('unlanded', 'none')

    def check_loop(self):
        """Keep the repeated-failure check, now based on E0 verification events."""
        failures = {}
        for row in self.event_rows:
            if row['kind'] == 'verify_fail':
                key = row['feature'], row['node'], row['detail']
                failures.setdefault(key, []).append(row)
        found = False
        if stall := self.stall:
            found = True
            what = (f"{ALERT_STALLED}: {stall['minutes']} min, {len(stall['ready'])} ready node(s), "
                    f"{len(stall['idle'])} idle slot(s), {len(stall['busy'])} live run(s)")
            self.line('stuck_loop', what, 'home chat', 'stuck')
            # one ALERT per stall, however the ready set shifts while it lasts
            self.report_alert(ALERT_STALLED, stall['ready'], what, token=f"since:{stall['since']}")
        elif f'alert:{ALERT_STALLED}' in self.state:
            self.clear_alert(ALERT_STALLED)  # the same node set stalling again later alerts again
        for (feature, node, signature), history in failures.items():
            if len(history) < 3:
                continue
            found = True
            self.report('stuck_loop', f'{feature}:{node}',
                        f'Failure repeated {len(history)} times: {signature}; review the loop',
                        f'{len(history)} E0 verify_fail events', 'read E0 verification history',
                        record=self.records.get(history[-1]['run_id']), token=signature)
        if not found:
            self.line('stuck_loop', 'none')


    # ---- critical-path watch ---------------------------------------------------------------------------------------
    def _rank(self, feature):
        order = []
        try:
            entries = json.loads((self.data / 'team/feature-queue.json').read_text())['features']
            order = [e['name'] for e in entries if isinstance(e, dict) and isinstance(e.get('name'), str)]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return order.index(feature) + 1 if feature in order else LAST_RANK

    def _sha_features(self):
        """sha -> feature for every completion row and landing-queue row that names one."""
        found = {}
        for path in sorted((self.data / 'team/features').glob('*/completion.json')):
            for row in dispatch.read(path, {}).get('nodes', {}).values():
                if row.get('sha'):
                    found[str(row['sha'])] = path.parent.name
        from companion import landing_queue  # lb-aq: one store; the old file's rows are read through it
        for _number, row, _raw in landing_queue.legacy_rows(self.data / 'team/landing-queue.jsonl'):
            if row and row.get('sha') and row.get('feature'):
                found[str(row['sha'])] = row['feature']
        for rec in self.records.values():
            if rec.get('sha'):
                found[str(rec['sha'])] = rec['feature']
        return found

    def _branch_feature(self, item, features):
        """The feature a train branch entry belongs to: an exact or prefix sha match (a completion row may hold a
        7-character id), else a queued feature's name inside a branch name."""
        for sha, feature in features.items():
            if item == sha or item.startswith(sha) or sha.startswith(item):
                return feature
        for name in self._queue_order():
            if name and name in item:
                return name
        return None

    def _queue_order(self):
        try:
            entries = json.loads((self.data / 'team/feature-queue.json').read_text())['features']
            return [e['name'] for e in entries if isinstance(e, dict) and isinstance(e.get('name'), str)]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def report_stall(self, what, cause, impact, fix, *, token, idle=STALL_SECONDS, record=None):
        """One critical_path_stall event and one inbox row for the home chat per incident (deduped like report)."""
        key = f'critical_path_stall:{what}'
        if self.state.get(key) == token:
            return self.line('critical_path_stall', what, 'none', 'already reported; unchanged evidence',
                             record=record, emit=False)
        needs = f'{what} has no forward event for {int(idle // 60)} min: {cause}; {impact}; default fix: {fix}'
        if self.dry_run:
            return self.line('critical_path_stall', what, 'home chat', f'would post: {needs}', record=record)
        row = dict(t=self.now, **{'from': 'watchdog'}, kind='critical_path_stall', to='home-chat',
                   node=f'{record["feature"]}:{record["node"]}' if record and record.get('feature') else what,
                   blocked=what, cause=cause, eta_impact=impact, fix=fix, needs=needs)
        path = self.data / 'team/board-inbox.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        result = self.line('critical_path_stall', what, 'home chat', needs, record=record)
        self.state[key] = token
        dispatch.save(self.state_path, self.state)
        return result

    def _train_stalls(self):
        """A live train (state file inside the horizon, not superseded by a newer train carrying its branches,
        branches not all on origin/master, no 'landed' log line) carrying priority 1 to 3 work with no new log
        line for STALL_SECONDS; a refusal line is a stall, not a finish. The watchdog never relaunches a train."""
        found = []
        features = self._sha_features()
        trains = []
        for state_path in sorted((self.data / 'train-logs').glob('*.state.json')):
            try:
                branches = [str(b) for b in json.loads(state_path.read_text()).get('branches', [])]
            except (OSError, ValueError, AttributeError):
                continue
            trains.append((state_path, branches, state_path.stat().st_mtime))
        for state_path, branches, state_time in trains:
            name = state_path.name.removesuffix('.state.json')
            if self.now - state_time > HORIZON_SECONDS:
                continue
            if any(set(branches) <= set(other) and other_time > state_time and other is not branches
                   for _, other, other_time in trains):
                continue  # relaunched as a newer train carrying the same branches: history
            carried = sorted({f for f in (self._branch_feature(b, features) for b in branches) if f}, key=self._rank)
            if not carried or self._rank(carried[0]) > CRITICAL_RANK:
                continue
            shas = [b for b in branches if re.fullmatch(r'[0-9a-f]{7,40}', b)]
            if shas and all(self.git(['merge-base', '--is-ancestor', b, 'origin/master']) is not False for b in shas):
                continue  # everything it carries is on master already
            logs = sorted((self.data / 'train-logs').glob(f'{name}-[0-9]*.log'), key=lambda p: p.stat().st_mtime)
            logs = [p for p in logs if '-precheck-' not in p.name]
            last_line, last_time = '', state_time
            if logs:
                lines = [ln for ln in logs[-1].read_text(errors='replace').splitlines() if ln.strip()]
                last_line, last_time = (lines[-1] if lines else ''), max(last_time, logs[-1].stat().st_mtime)
            if any(marker in last_line for marker in TRAIN_FINAL):
                continue
            idle = self.now - last_time
            if idle < STALL_SECONDS:
                continue
            minutes = int(idle // 60)
            rank = self._rank(carried[0])
            what = f'train {name} (carries {", ".join(carried)}, priority {rank})'
            refused = last_line.startswith(('refused:', 'checks failed', 'merge conflict')) or 'nothing landed' in last_line
            cause = (f'refused {minutes} min ago: "{last_line[:160]}"' if refused
                     else f'last log line {minutes} min ago: {last_line[:160] or "no log yet"}')
            impact = f'ETA: {", ".join(carried)} slips {minutes}+ min until this train finishes or is relaunched'
            found.append((what, cause, impact, "none safe (a train is never relaunched by the watchdog); the Board's",
                          f'{last_line[:60]}|{int(last_time)}', idle))
        return found

    def _run_stalls(self):
        """A busy run of a priority 1 to 3 feature with no state transition or event for STALL_SECONDS. The stale
        rules in check_workers keep their own limits; this is the earlier, louder report."""
        found = []
        latest_event = {}
        for row in self.event_rows:
            # The watchdog's own rows (check_workers lines every tick, this report's own event) are not forward
            # progress; counting them kept every run under five minutes old (review o-rev48, 2026-10-15).
            if row.get('run_id') and row.get('worker') != 'watchdog' and row.get('kind') != 'watchdog':
                latest_event[row['run_id']] = max(latest_event.get(row['run_id'], 0), row['t'])
        latest = {}
        for rec in self.records.values():
            key = (rec['feature'], rec['node'])
            if key not in latest or (rec['created'], rec['run_id']) > (latest[key]['created'], latest[key]['run_id']):
                latest[key] = rec
        for rec in latest.values():  # only a node's newest run is the critical item
            rank = self._rank(rec['feature'])
            if rec['state'] not in BUSY_OR_STUCK or rank > CRITICAL_RANK:
                continue
            last = max(rec['updated'], latest_event.get(rec['run_id'], 0))
            idle = self.now - last
            if idle < STALL_SECONDS:
                continue
            minutes = int(idle // 60)
            what = f'run {rec["run_id"]} ({rec["feature"]}:{rec["node"]}, priority {rank})'
            cause = f'state {rec["state"]} for {minutes} min, no event since'
            impact = f'ETA: {rec["feature"]} slips {minutes}+ min while this node waits'
            self.nudge = True  # the default fix: one dispatcher tick, requested through check_slots
            found.append((what, cause, impact, 'one dispatcher tick requested; check_workers keeps its own timeout',
                          f'{rec["state"]}|{int(last)}', rec, idle))
        return found

    def _gpu_stalls(self):
        """A live gpu gate lock (a profile or another local model job) whose lock and newest long-input profile
        have not moved for STALL_SECONDS. The watchdog never kills a job."""
        from companion import gate_lock

        found = []
        for lock in (self.data / 'team/gate-gpu.lock', self.data / 'team/gate.lock'):  # a profile with no --kind holds the old lock
            info = gate_lock.read(lock)
            if not info or not gate_lock.live(info):
                continue
            command = ' '.join(str(c) for c in info.get('command', []))
            rank = info.get('priority') if isinstance(info.get('priority'), int) else None
            if rank is not None and rank > CRITICAL_RANK:
                continue  # a ranked lock says what it is; a rank 4+ job is not critical-path work
            if rank is None and 'long-input' not in command and 'agent_profile' not in command:
                continue  # an unranked lock counts only when its command names the long-input profile
            last = lock.stat().st_mtime
            folders = sorted((self.data / 'team/long-input').glob('profile-*/profile.jsonl'),
                             key=lambda p: p.stat().st_mtime)
            if folders:
                last = max(last, folders[-1].stat().st_mtime)
            idle = self.now - last
            if idle < STALL_SECONDS:
                continue
            minutes = int(idle // 60)
            what = f'gpu job pid {info.get("pid")} ({command[:80] or "unknown command"})'
            cause = f'lock and profile events unchanged for {minutes} min'
            found.append((what, cause, f'ETA: long-input profiles and the L4 verdict slip {minutes}+ min',
                          'none safe (a GPU job is never killed by the watchdog); the Board decides', f'{int(last)}', idle))
        return found

    def check_critical_path(self):
        found = False
        for what, cause, impact, fix, token, idle in self._train_stalls() + self._gpu_stalls():
            found = True
            self.report_stall(what, cause, impact, fix, token=token, idle=idle)
        for what, cause, impact, fix, token, rec, idle in self._run_stalls():
            found = True
            self.report_stall(what, cause, impact, fix, token=token, idle=idle, record=rec)
        if not found:
            self.line('critical_path_stall', 'none')


def tick(data, *, dispatcher, git, clock=time.time, dry_run=False, probes=None):
    """Serialize watchdog writers; dry runs read only and never invoke the dispatcher."""
    context = nullcontext() if dry_run else dispatch.locked(Path(data) / 'team/watchdog.lock')
    with context:
        return Tick(data, dispatcher=dispatcher, git=git, clock=clock, dry_run=dry_run, probes=probes).run()
