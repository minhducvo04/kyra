"""Inspector: each watchdog tick checks every live run against its expected duration, in code, no LLM.

The owner, 2026-10-15: two Muse runs died at once (a Keychain error, then a wrong model id) yet sat 90 minutes as
dispatched/heartbeat holding file locks, and escalated, verify_fail and timeout runs sat 2 to 9 hours unnoticed.
node_worker's heartbeat says "waiting for result", not "the worker is alive", so the inspector looks at the worker
itself: its pid in the courier ledger, the ledger's failed row (as-gx's exit supervisor), its courier log, its result
file and commits in its worktree.

Only the newest run of a node is inspected, and never a node whose completion row is done (its other runs are
history). Milestones per run, against ``expected_min`` (stored when the record is minted, else by brief kind):

    dead       the ledger closed the worker as failed, its pid is gone, or at 20% elapsed nothing shows life
               (no live process, no log written in the last 10 min, no result): close the run (timeout, marked
               inspector_closed so the dispatcher's loop tick leaves it to the inspector), count a failure on the
               channel (two in six hours pause that channel in model-budget.json, never over a pause the inspector
               did not write), requeue on a different eligible channel
    behind     at 50% no commit or partial result, or at 100% no result, but alive: one inbox row with evidence
    overdue    at 150%: one "inspector" request row the Board routes to an Opus fixer

File claims: an expired claim is released as timeout; an unexpired one only on proof of death (a failed ledger row
or a known pid that is gone, locks reason "dead"). Without proof the requeue waits for the claim to expire, so no
successor is minted over a live worker's files. A requeue blocked for good (no other channel, tiers or requeues
spent) closes an inspector-closed run to escalated and posts one fixer row.

A loop timeout older than 15 minutes with no successor is requeued the same way. Board policy (2026-10-16):
escalated runs are never requeued (they wait for the Board); a verify_fail older than 15 minutes gets one row asking
for the fix-back to its owning agent. A held run (``hold`` on the record or the completion row, or a completion row
dispatched ``by: board``, a hand build) is never touched. Requeues per node are capped; past the cap the node goes to
the fixer. A dead worker's run that the node worker then crashed into escalated ("node worker failed: ..."), with no
result, no live pid and a worker log and worktree quiet for 20 minutes, is requeued the same way (night 2026-10-17: MS3
sat dispatched for hours, an escalated run being final); a run waiting on a human or the Board is never touched. An error on one run is logged and the tick goes on. This code never spawns an agent: the dispatcher's
loop tick starts the queued successor.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from companion import board_inbox
from companion.feature_team import contract, dispatch, runs
from companion.feature_team.completion import COMPLETED_STATUSES
from companion.feature_team.node_worker import LAUNCH_FAILURE, RECEIPT_TIMEOUT

EXPECTED_MIN_BY_KIND = {'plan': 30, 'small_code': 30, 'loop_code': 50, 'review': 20, 'docs': 30, 'check': 20,
                        'verify': 20}
DEFAULT_EXPECTED_MIN = 50
LIFE, PROGRESS, DUE, OVERDUE = 0.2, 0.5, 1.0, 1.5
LOG_FRESH_SECONDS = 10 * 60
STALE_SECONDS = 15 * 60
QUIET_SECONDS = 20 * 60  # a crashed-and-escalated run is requeued only after its log and worktree were quiet this long
CRASH_ESCALATION = 'node worker failed:'  # node_worker's detail when the worker process, not the worker, broke
WAITING_ON_HUMAN = ('delivery_status', 'needs_input', 'question', 'handoff')  # completion-row keys: leave the run
CHANNEL_FAIL_LIMIT = 2
CHANNEL_FAIL_WINDOW = 6 * 3600
CHANNEL_PAUSE_SECONDS = 12 * 3600
NODE_REQUEUE_LIMIT = 2
PAUSED_SUFFIXES = (' paused', ' times recently')  # pick_channel reasons that lift by themselves
LAUNCH_RECEIPT_SECONDS = 2 * RECEIPT_TIMEOUT  # node_worker's own start-receipt wait, plus the same again for setup
STARVED_TICKS = 3  # dispatcher.lock skips in a row before one inbox row (15 min at the 5 min watchdog tick)
LIVE_STATES = ('dispatched', 'started', 'heartbeat')
FIXER = 'claude:opus (claude-opus-5-5)'
_LEDGER_ID = re.compile(r'\bledger_id=([A-Za-z0-9][A-Za-z0-9._-]*)')
_LOG = re.compile(r'\(log ([^)]+)\)|\blog=(\S+)')
_PID = re.compile(r'\bpid[= ](\d+)\b')


def expected_minutes(brief, override=None):
    """The minutes a run should take: an explicit value, else by the brief's DAG kind, else 50."""
    if type(override) in (int, float) and override > 0:
        return float(override)
    return float(EXPECTED_MIN_BY_KIND.get((brief or {}).get('kind'), DEFAULT_EXPECTED_MIN))


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # EPERM: the pid exists under another user; alive, as node_worker._alive reads it (fail closed)
    except OSError:
        return False
    try:
        stat = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True  # the pid exists (kill 0 worked); unknown zombie state reads as alive, the safe side
    return stat[:1] != 'Z'


def branch_commits(rec):
    """Commits on the run's worktree since its base, or None when the worktree is not a readable checkout."""
    worktree, base = rec['brief'].get('worktree'), rec['brief'].get('base')
    if not worktree or not base or not Path(worktree).is_dir():
        return None
    try:
        done = subprocess.run(['git', '-C', worktree, 'rev-list', '--count', f'{base}..HEAD'],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None  # a loaded Mac: unknown, never a reason to stop the tick
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() else None


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _jsonl(path):
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _epoch(value):
    if type(value) in (int, float):
        return float(value)
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp.timestamp()  # naive stamps (the ledger's local time) read as local time


def hand_built(row):
    """A completion row the Board set to dispatched for a hand build: exactly ``"by": "board"`` (lane 1's mark on
    R3, R6, R14, F1, FL, V0 and runner-pool RP1 to RP6, 2026-10-16; the receipt is the dispatcher's own
    "QUEUED: durable run <id>" or absent, so it says nothing). Its loop runs are held, so the inspector never requeues
    a node an Opus subagent is already building. A word match is not enough: a node named board-ui is not the Board
    (review o-rev85)."""
    return isinstance(row, dict) and row.get('status') == 'dispatched' and row.get('by') == 'board'


BUDGET_LOCK_WAIT = 10.0  # seconds the inspector waits for model-budget.lock before it leaves the pause for a tick


@contextmanager
def try_locked(path, seconds):
    """An exclusive flock on ``path`` with a bounded wait: yields True with the lock held, or False after
    ``seconds`` (the watchdog never freezes behind another writer)."""
    import fcntl
    import time

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    with path.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
                continue
            yield True
            return


def _usable(rec):
    return all(isinstance(rec.get(k), t) for k, t in (('created', (int, float)), ('feature', str), ('node', str),
                                                      ('brief', dict), ('history', list), ('attempts', dict)))


def budget_lock(path):
    """The one lock file around every read-modify-write of model-budget.json (budget_mode.update takes it too)."""
    return Path(path).with_name('model-budget.lock')


def proof_dead(ev):
    """Proof the worker is gone: the exit supervisor's failed ledger row, a known pid that is no longer alive, or
    node_worker's launch-failure verdict (itself given only on a gone pid). Only proof (or the lease's own expiry)
    frees a run's file claims early; 'no life seen' or an empty log alone is not proof."""
    return bool(ev['exited'] or (ev['pid'] and not ev['alive']) or ev.get('launch_failed') or ev.get('silent_launch'))


def note_lock_skip(data, now):
    """The watchdog skipped the inspector because the dispatcher held its lock: count the streak in
    inspector-state.json and post one inbox row when it reaches STARVED_TICKS (review o-rev85b). Returns the streak."""
    path = Path(data) / 'team/inspector-state.json'
    state = _read_json(path, {})
    state = state if isinstance(state, dict) else {}
    state['lock_skips'] = int(state.get('lock_skips') or 0) + 1
    if state['lock_skips'] == STARVED_TICKS:
        board_inbox.append({'t': now, 'from': 'inspector', 'kind': 'inspector', 'to': 'board',
                            'text': f'inspector starved: skipped {STARVED_TICKS} watchdog ticks in a row because '
                                    'the dispatcher held dispatcher.lock; dead runs it closed wait meanwhile',
                            'needs': 'check for a long or hung dispatcher tick',
                            'routing_id': f'inspector:starved:{int(now)}'},
                           path=Path(data) / 'team/board-inbox.jsonl', once=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True) + '\n')
    temporary.replace(path)
    return state['lock_skips']


def current_attempt(rec):
    """The history of the current attempt: from the newest dispatched transition on (the whole history when the
    run was never dispatched)."""
    history = rec.get('history', [])
    starts = [i for i, h in enumerate(history) if h.get('state') == 'dispatched']
    return history[starts[-1]:] if starts else history


def started_at(rec):
    """Start of the current attempt: the newest dispatched transition, else the record's creation."""
    times = [h['t'] for h in rec.get('history', []) if h.get('state') == 'dispatched']
    return max(times) if times else rec['created']


class Inspector:
    def __init__(self, data, *, store, now, alive=pid_alive, commits=branch_commits, dry_run=False,
                 line=None, emit=None):
        self.data = Path(data)
        self.store, self.now, self.dry_run = store, now, dry_run
        self.alive, self.commits = alive, commits
        self.line = line or (lambda *a, **k: None)
        self.emit = emit or (lambda event: None)
        self.state_path = self.data / 'team/inspector-state.json'
        self.state = _read_json(self.state_path, {})
        self.state_reset = not isinstance(self.state, dict) or not all(
            isinstance(self.state.get(k, {}), dict) for k in ('channel_fails', 'requeued', 'node_requeues', 'posted'))
        if self.state_reset:
            self.state = {}  # a broken state file must not stop the inspector every tick; run() posts one row
        for key in ('channel_fails', 'requeued', 'node_requeues', 'posted'):
            self.state.setdefault(key, {})
        self.ledger = _jsonl(self.data / 'context/packages.jsonl')
        self.summary = dict(checked=0, dead=0, behind=0, overdue=0, requeued=0, stale=0, errors=0,
                            detect_minutes=[])

    # ---- evidence -----------------------------------------------------------------------------------------------

    def evidence(self, rec):
        """What the CURRENT attempt shows: node_worker's correction attempt reuses the record, and a successor
        inherits its predecessor's partial, so only history from the newest dispatched transition counts, and only
        this attempt's own result (review o-rev85b). An older attempt's partial is progress, never a result."""
        attempt = current_attempt(rec)
        ledger_id = next((m[1] for h in reversed(attempt)
                          if (m := _LEDGER_ID.search(str(h.get('detail', ''))))), None)
        rows = [r for r in self.ledger if ledger_id and r.get('id') == ledger_id]
        sent = next((r for r in rows if 'status' not in r), {})
        failed = next((r for r in rows if r.get('status') == 'failed'), None)
        pid = rec.get('pid') or sent.get('pid')
        if pid is None and (m := _PID.search(str(sent.get('receipt', '')))):
            pid = int(m[1])
        logs = []
        for text in [str(sent.get('receipt', ''))] + [str(h.get('detail', '')) for h in attempt]:
            for m in _LOG.finditer(text):
                logs.append(Path(m[1] or m[2]))
        log_bytes, log_mtime, log_path = 0, None, None
        for log in logs:
            try:
                info = log.stat()
            except OSError:
                continue
            log_bytes = max(log_bytes, info.st_size)
            if info.st_size:
                log_mtime = max(log_mtime or 0, info.st_mtime)
                log_path = str(log)
        result = bool(any(h.get('state') == 'result' for h in attempt)
                      or (rec['brief'].get('result') and Path(rec['brief']['result']).exists()))
        alive = bool(pid) and self.alive(int(pid))
        died_at = None
        if failed:
            died_at = _epoch(failed.get('passed_at'))
        elif pid and not alive:
            died_at = log_mtime
        ev = dict(ledger_id=ledger_id, pid=pid, alive=alive, exited=failed is not None,
                  exit_text=(failed or {}).get('last_text', '')[-200:], logs=[str(p) for p in logs],
                  log_bytes=log_bytes, log_age=None if log_mtime is None else self.now - log_mtime,
                  log_mtime=log_mtime, log_path=log_path,
                  result=result, partial=bool(rec.get('partial')), died_at=died_at,
                  launch_failed=self.launch_failed(rec, logs, alive))
        ev['silent_launch'] = self.silent_launch(rec, attempt, ev)
        return ev

    def silent_launch(self, rec, attempt, ev):
        """Why this attempt is a launch that died before its start receipt, else None: still dispatched with no
        started transition LAUNCH_RECEIPT_SECONDS after dispatch, no live pid (the node worker's, which launch_node
        writes to team/loop/<run_id>.worker.pid, is known and gone, and no courier pid is alive) and an empty or
        missing worker log, and no courier ledger row for its worktree since dispatch. A live or unknown pid keeps its
        lease; a node worker that wrote to its log (it logs a line before every send) is left to the 20% rule."""
        if rec['state'] != 'dispatched' or any(h.get('state') == 'started' for h in attempt) or ev['alive']:
            return None
        waited = self.now - started_at(rec)
        if waited < LAUNCH_RECEIPT_SECONDS:
            return None
        worktree = rec['brief'].get('worktree')
        if any(row.get('target') == worktree and (_epoch(row.get('sent_at')) or 0) >= started_at(rec) - 1
               for row in self.ledger):
            return None  # a courier was sent since dispatch (detached, its pid maybe unknown): no proof it died
        loop = self.data / 'team/loop'
        try:
            launcher = int((loop / f'{rec["run_id"]}.worker.pid').read_text().strip())
        except (OSError, ValueError):
            launcher = None
        if launcher is None or self.alive(launcher):
            return None  # no known pid is no proof: an UNCERTAIN send may have a live worker (review o-rev85 fixback)
        try:
            if (loop / f'{rec["run_id"]}.worker.log').stat().st_size:
                return None
        except FileNotFoundError:
            pass  # a missing log is as silent as an empty one
        except OSError:
            return None  # unreadable is not empty: fail closed, the lease stays
        return (f'{LAUNCH_FAILURE}: no start receipt {int(waited)} s after dispatch, node worker pid '
                f'{launcher} not running, worker log empty')

    def launch_failed(self, rec, logs, alive):
        """node_worker's launch-failure verdict on this attempt, which it gives only once the worker's recorded pid
        is gone. An empty log alone is never proof: a live CLI that buffers its output has one too (review o-rev85c);
        an exited one is caught by its pid or its failed ledger row."""
        from companion.feature_team.node_worker import is_launch_failure
        return bool(is_launch_failure(rec) and not alive)

    def classify(self, rec, ev):
        """(verdict, why): dead, overdue, behind or ok. Pure given the evidence."""
        expected = expected_minutes(rec['brief'], rec.get('expected_min')) * 60
        elapsed = self.now - started_at(rec)
        frac = elapsed / expected
        clock = f'{int(elapsed // 60)} of {int(expected // 60)} min ({int(frac * 100)}%)'
        if ev.get('silent_launch'):
            return 'dead', ev['silent_launch']
        if ev['exited'] and not ev['result']:
            return 'dead', f'worker exited without a result ({clock}): {ev["exit_text"][-120:] or "no text"}'
        if ev['pid'] and not ev['alive'] and not ev['result']:
            return 'dead', f'worker pid {ev["pid"]} is gone and wrote no result ({clock})'
        log_fresh = ev['log_age'] is not None and ev['log_age'] < LOG_FRESH_SECONDS
        # No life can be judged once a worker was launched (a ledger id) or while the run still waits for its start
        # receipt; a started record with no ledger id at all predates the courier ledger and stays with check_workers.
        judged = ev['ledger_id'] is not None or rec['state'] == 'dispatched'
        if judged and frac >= LIFE and not ev['alive'] and not log_fresh and not ev['result']:
            return 'dead', f'no life at {int(LIFE * 100)}%: no live process, no log written, no result ({clock})'
        if frac >= OVERDUE and not ev['result']:
            return 'overdue', f'overdue: {clock}, no result'
        if frac >= DUE and not ev['result']:
            return 'behind', f'past expected time: {clock}, no result yet'
        if frac >= PROGRESS and not ev['result'] and not ev.get('partial') and not ev.get('commits'):
            return 'behind', f'half time with no commit or partial result: {clock}'
        return 'ok', clock

    # ---- the tick -----------------------------------------------------------------------------------------------

    def run(self, records):
        latest = {}
        for run_id in sorted(set(records) - set(usable := {k: r for k, r in records.items() if _usable(r)})):
            self.summary['errors'] += 1  # a malformed record skips only itself
            self.line('inspector', run_id, 'none', 'record without created, feature, node or brief; skipped')
        records = usable
        for rec in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
            latest[rec['feature'], rec['node']] = rec
        successors = {r.get('retry_of') for r in records.values() if r.get('retry_of')}
        for (feature, node), rec in sorted(latest.items()):
            try:
                self.visit(rec, self.completion(feature).get(node) or {}, successors)
            except Exception as exc:  # noqa: BLE001  one run's error must not stop the other runs or the tick
                self.summary['errors'] += 1
                self.line('inspector', rec['run_id'], 'none',
                          f'inspector error: {type(exc).__name__}: {exc}'[:300], record=rec)
        if self.state_reset:
            self.post(None, 'state-reset', 'inspector', 'inspector-state.json was not a valid state object; the '
                      'inspector started a fresh one (requeue counts and channel failures were lost)',
                      str(self.state_path), routing=f'inspector:state-reset:{int(self.now)}')
        self.metric()
        self.save()
        return self.summary

    def visit(self, rec, row, successors):
        if row.get('status') in COMPLETED_STATUSES:
            return  # finished: every run of this node is history
        if rec.get('hold') or row.get('hold') or hand_built(row):
            self.line('inspector', rec['run_id'], result='held (hold, or built by hand); untouched', record=rec)
            return
        if rec['state'] in LIVE_STATES:
            return self.inspect_live(rec)
        if rec['run_id'] in successors or rec['run_id'] in self.state['requeued']:
            return
        from companion.feature_team.node_worker import is_launch_failure
        if rec['state'] == 'timeout' and not rec.get('inspector_closed') and is_launch_failure(rec):
            # node_worker saw the worker end at once with an empty log: dead at start, a failure on its channel
            why = rec['history'][-1]['detail']
            ev = self.evidence(rec)
            self.summary['dead'] += 1
            self.summary['detect_minutes'].append(round(max(0.0, self.now - rec['updated']) / 60, 1))
            if not self.dry_run:
                rec['inspector_closed'] = dict(t=self.now, proof=proof_dead(ev), why=why[:300])
                runs._write_json(self.store.path(rec['run_id']), rec)
                self.channel_failed(rec['brief']['channel'], rec['run_id'], why)
            return self.requeue(rec, why, ev=ev)
        age = self.now - rec['updated']
        if rec['state'] == 'timeout' and (rec.get('inspector_closed') or age >= STALE_SECONDS):
            if not rec.get('inspector_closed'):
                self.summary['stale'] += 1
            return self.requeue(rec, (rec.get('inspector_closed') or {}).get('why')
                                or f'timeout for {int(age // 60)} min, never requeued')
        if rec['state'] == 'verify_fail' and age >= STALE_SECONDS:
            # Board policy: a failed verify goes back to the owning agent through the fix-back, never a requeue
            self.summary['stale'] += 1
            self.post(rec, 'verify_fail', 'inspector', f'{rec["run_id"]} ({rec["feature"]}:{rec["node"]}) has been '
                      f'verify_fail for {int(age // 60)} min with no fix-back; route the fix-back to its owner',
                      f'channel {rec["brief"]["channel"]}; verify output {rec["verify"].get("output")}',
                      extra=dict(to='board', needs='fix-back to the owning agent with the verify output'),
                      routing=f'inspector:{rec["run_id"]}:verify_fail')
        if rec['state'] == 'escalated':
            self.requeue_crashed(rec, row)
        # any other escalated run is final; it waits for the Board (the dispatcher posts its own loop-escalated row)

    def requeue_crashed(self, rec, row):
        """Requeue an escalated run whose worker died silently and whose node worker crashed on the closed record.
        Only when the node still reads dispatched, nothing waits on a human or the Board, no result exists, no
        worker pid is alive and the worker log and worktree have been quiet for QUIET_SECONDS. The requeue (cap, other
        channel, the one fixer row on the last death) is requeue()'s own."""
        detail = str((rec.get('history') or [{}])[-1].get('detail', ''))
        if (not detail.startswith(CRASH_ESCALATION) or row.get('status') != 'dispatched'
                or any(row.get(k) for k in WAITING_ON_HUMAN)):
            return None
        ev = self.evidence(rec)
        if ev['result'] or ev['alive'] or not (rec.get('inspector_closed') or ev['pid']):
            return None  # no worker was ever launched (worktree add, contract refusal): that escalation is final
        stamps = [ev['log_mtime']] if ev['log_mtime'] else []
        try:
            stamps.append(Path(rec['brief']['worktree']).stat().st_mtime)
        except (KeyError, TypeError, OSError):
            pass
        quiet = self.now - max(stamps, default=rec['updated'])
        if quiet < QUIET_SECONDS:
            return None
        self.summary['dead'] += 1
        why = f'worker dead and run escalated by a node-worker crash, quiet {int(quiet // 60)} min: {detail[:120]}'
        return self.requeue(rec, why, evidence=self.evidence_text(rec, ev), ev=ev)

    def inspect_live(self, rec):
        self.summary['checked'] += 1
        ev = self.evidence(rec)
        ev['commits'] = None if ev['result'] else self.commits(rec)
        verdict, why = self.classify(rec, ev)
        if verdict == 'ok':
            self.line('inspector', rec['run_id'], result=f'on time: {why}', record=rec)
            return
        evidence = self.evidence_text(rec, ev)
        if verdict == 'behind':
            self.summary['behind'] += 1
            self.post(rec, f'behind:{why.split(":")[0]}', 'inspector_behind', f'{rec["run_id"]} is behind: {why}',
                      evidence)
            return
        if verdict == 'overdue':
            self.summary['overdue'] += 1
            self.fixer(rec, why, evidence, 'overdue')
            return
        self.summary['dead'] += 1
        self.summary['detect_minutes'].append(round((self.now - (ev['died_at'] or started_at(rec))) / 60, 1))
        if self.dry_run:
            self.line('inspector', rec['run_id'], 'close', f'would close dead run: {why}', record=rec)
            return self.requeue(rec, f'dead: {why}', evidence=evidence, ev=ev)
        rec = self.store.transition(rec['run_id'], 'timeout', worker='watchdog',
                                    detail=f'inspector: dead: {why}'[:contract.DETAIL_MAX])
        # The mark tells the dispatcher's loop tick to leave this timeout alone: the inspector owns its requeue, so a
        # dead channel is never retried behind the inspector's back (review o-rev85).
        rec['inspector_closed'] = dict(t=self.now, proof=proof_dead(ev), why=f'dead: {why}'[:300])
        runs._write_json(self.store.path(rec['run_id']), rec)
        self.channel_failed(rec['brief']['channel'], rec['run_id'], why)
        if ev.get('silent_launch'):  # the log a human opens first says why, instead of nothing
            with (self.data / 'team/loop' / f'{rec["run_id"]}.worker.log').open('a') as log:
                log.write(f'inspector {rec["run_id"]}: closed: {why}\n')
        self.line('inspector', rec['run_id'], 'close', f'dead run closed: {why}', record=rec)
        self.requeue(rec, f'dead: {why}', evidence=evidence, ev=ev)

    def evidence_text(self, rec, ev):
        parts = [f'state {rec["state"]}', f'channel {rec["brief"]["channel"]}',
                 f'ledger {ev["ledger_id"] or "none"}', f'pid {ev["pid"] or "unknown"} '
                 f'{"alive" if ev["alive"] else "not alive"}', f'log {ev["log_bytes"]} B'
                 + ('' if ev['log_age'] is None else f', written {int(ev["log_age"] // 60)} min ago'),
                 *([f'log {ev["log_path"]}, {ev["log_bytes"]} bytes, last write '
                    f'{datetime.fromtimestamp(ev["log_mtime"], UTC).isoformat()}'] if ev.get('log_path') else []),
                 f'result {"yes" if ev["result"] else "no"}', f'commits {ev.get("commits")}']
        waits = [e for e in self.lock_events() if e.get('run_id') == rec['run_id'] and e.get('kind') == 'lock_wait']
        if waits:
            parts.append(f'lock waits {len(waits)}')
        return '; '.join(parts)

    # ---- actions ------------------------------------------------------------------------------------------------

    def locks(self):
        from companion.feature_team.locks import FileLocks
        return FileLocks(self.data / 'team/loop/locks.sqlite3', workspace=dispatch.REPO, caller_role='dispatcher',
                         clock=lambda: self.now)

    def lock_events(self):
        if not (self.data / 'team/loop/locks.sqlite3').exists():
            return []
        try:
            return self.locks().events() if not self.dry_run else []
        except Exception:  # noqa: BLE001  evidence only; a broken lock store must not stop the tick
            return []

    def usable_records(self):
        return {k: r for k, r in self.store.load_all().items() if _usable(r)}

    def claims(self):
        """Held claims, read-only (no schema write, so a dry run reads them too)."""
        import sqlite3
        from contextlib import closing

        from companion.feature_team.locks import FileLocks
        database = self.data / 'team/loop/locks.sqlite3'
        if not database.exists():
            return []
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            return FileLocks._active(db)

    def unexpired_claim_minutes(self, rec):
        return max([(c['deadline'] - self.now) / 60 for c in self.claims()
                    if c['run_id'] == rec['run_id'] and c['deadline'] > self.now], default=0)

    def free_claims(self, rec, proof):
        """Free a closed run's file claims before a successor is minted over the same files. An expired claim is
        released as timeout; an unexpired one only on proof of death (reason dead). Returns the minutes the run
        still holds an unexpired claim without proof (the requeue waits for it), else 0."""
        if not (self.data / 'team/loop/locks.sqlite3').exists():
            return 0
        leases = self.locks()
        held = 0
        for claim in leases.snapshot():
            if claim['run_id'] != rec['run_id']:
                continue
            if claim['deadline'] <= self.now:
                leases.release(rec['run_id'], claim['owner'], reason='timeout')
            elif proof:
                leases.release(rec['run_id'], claim['owner'], reason='dead')
            else:
                held = max(held, (claim['deadline'] - self.now) / 60)
        return held

    def channel_failed(self, channel, run_id, why):
        fails = [f for f in self.state['channel_fails'].get(channel, []) if self.now - f['t'] < CHANNEL_FAIL_WINDOW]
        if not any(f['run_id'] == run_id for f in fails):
            fails.append(dict(t=self.now, run_id=run_id, why=why[:200]))
        self.state['channel_fails'][channel] = fails
        if len(fails) >= CHANNEL_FAIL_LIMIT:
            self.pause_channel(channel, fails)

    def failures(self, channel):
        return len([f for f in self.state['channel_fails'].get(channel, []) if self.now - f['t'] < CHANNEL_FAIL_WINDOW])

    def pause_channel(self, channel, fails):
        """Pause one channel in model-budget.json, under the lock budget_mode.py takes for the same file. Only an
        entry the inspector wrote (``by: inspector``) is ever changed: a human's pause, timed or not, stays exactly
        as written (review o-rev85)."""
        path = self.data / 'team/model-budget.json'
        until = datetime.fromtimestamp(self.now + CHANNEL_PAUSE_SECONDS, UTC).isoformat()
        reason = (f'inspector: {len(fails)} dead runs in {CHANNEL_FAIL_WINDOW // 3600} h ('
                  + ', '.join(f['run_id'] for f in fails[-3:]) + f'); last: {fails[-1]["why"][:120]}')
        path.parent.mkdir(parents=True, exist_ok=True)
        with try_locked(budget_lock(path), BUDGET_LOCK_WAIT) as held:
            if not held:
                self.line('inspector', channel, 'pause', 'model-budget.lock busy; channel not paused this tick')
                return
            try:
                document = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            except (OSError, ValueError):
                document = None  # a human mid-edit or a typo: write nothing, as budget_mode.update refuses
            if not isinstance(document, dict):
                self.line('inspector', channel, 'pause', 'model-budget.json unreadable; nothing written, '
                          'channel not paused')
                return
            entry = document.get(channel)
            if entry is not None and not (isinstance(entry, dict) and entry.get('by') == 'inspector'):
                self.line('inspector', channel, 'pause', 'a pause the inspector did not write; left as it is')
                return
            entry = entry or {}
            if entry.get('paused_until') and (_epoch(entry['paused_until']) or 0) > self.now:
                return  # already paused by the inspector
            document[channel] = dict(entry, paused_until=until, reason=reason, by='inspector')
            temporary = path.with_suffix('.inspector.tmp')
            temporary.write_text(json.dumps(document, indent=2) + '\n')
            temporary.replace(path)
        self.post(None, f'pause:{channel}:{until}', 'inspector_pause', f'channel {channel} paused until {until}: {reason}',
                  reason, routing=f'pause:{channel}:{fails[-1]["run_id"]}')

    def pick_channel(self, rec):
        """A different eligible engineer channel from the feature's team: a loop channel kind, allowed, not
        paused, under the failure limit; another provider first, then an idle one, then team order."""
        failed = rec['brief']['channel']
        records = self.usable_records()
        tried, cursor, seen = {failed}, rec, set()
        while cursor and cursor.get('retry_of') and cursor['retry_of'] not in seen:  # the node's whole retry chain
            seen.add(cursor['retry_of'])
            cursor = records.get(cursor['retry_of'])
            if cursor:
                tried.add(cursor['brief']['channel'])
        team = _read_json(self.data / 'team/features' / rec['feature'] / 'team.json', [])
        modes, skipped = {}, {}
        pauses = dispatch.budget_pauses(self.data / 'team/model-budget.json', self.now, modes)
        busy = {r['brief']['channel'] for r in records.values() if r['state'] in LIVE_STATES}
        options, why_not = [], []
        hint = rec.get('owner_hint') or dispatch._dag_hint(self.data, rec['feature'], rec['node'])
        for index, worker in enumerate(team if isinstance(team, list) else []):
            channel = worker.get('channel') if isinstance(worker, dict) else None
            if not isinstance(channel, str) or dispatch.role(worker.get('role')) != 'engineer':
                continue
            if channel in tried:
                if channel != failed:
                    why_not.append(f'{channel} already failed this node')
                continue
            if refusal := contract.channel_refusal(channel):
                why_not.append(f'{channel} cannot serve this node: {refusal}')
                continue
            if not dispatch.hint_allows(hint, channel):
                why_not.append(f'{channel} does not match owner_hint {hint!r}')
                continue
            if channel.partition(':')[0] not in dispatch.LOOP_CHANNELS or not dispatch.allowed(channel):
                why_not.append(f'{channel} not a loop channel')
                continue
            if not dispatch.budget_table([dict(channel=channel, role='engineer')], pauses, skipped, modes):
                why_not.append(f'{channel} paused')
                continue
            if self.failures(channel) >= CHANNEL_FAIL_LIMIT:
                why_not.append(f'{channel} failed {self.failures(channel)} times recently')
                continue
            from companion.feature_team import picker
            model = worker.get('model') or picker.MODEL_IDS.get(worker.get('name'),
                                                                 str(worker.get('name', '')).lower().replace(' ', '-'))
            rank = (channel.partition(':')[0] == failed.partition(':')[0], channel in busy, index)
            options.append((rank, channel, model))
        if not options:
            return None, why_not or [f'no other engineer channel on {rec["feature"]}\'s team']
        _, channel, model = min(options)
        return (channel, model), []

    def requeue(self, rec, why, evidence=None, ev=None):
        """Mint one queued successor on a different channel (the dispatcher's loop tick starts it), or say why not.
        Blocked for good (no other channel, tiers or requeues spent) on a run the inspector closed: the run is closed
        to escalated, so nothing retries the dead channel, and one fixer row goes to the Board. A run that still holds
        an unexpired file claim without proof of death waits for the claim; nothing is minted over its files."""
        key = f'{rec["feature"]}:{rec["node"]}'
        records = self.usable_records()
        if rec['run_id'] in self.state['requeued'] or any(r.get('retry_of') == rec['run_id'] for r in records.values()):
            return None
        evidence = evidence or f'state {rec["state"]}; channel {rec["brief"]["channel"]}; last: ' + str(
            (rec.get('history') or [{}])[-1].get('detail', ''))[:160]
        ev = ev or self.evidence(rec)
        proof = proof_dead(ev) or bool((rec.get('inspector_closed') or {}).get('proof'))
        blockers, transient = [], []
        if self.state['node_requeues'].get(key, 0) >= NODE_REQUEUE_LIMIT:
            blockers.append(f'node already requeued {self.state["node_requeues"][key]} times by the inspector')
        tier = next((t for t in runs.TIERS[runs.TIERS.index(rec['tier']):]
                     if rec['attempts'][t] < runs.ATTEMPTS_PER_TIER), None)
        if tier is None:
            blockers.append('every tier is exhausted')
        latest = {}
        for r in sorted(records.values(), key=lambda r: (r['created'], r['run_id'])):
            latest[r['feature'], r['node']] = r
        for item in rec['brief'].get('inputs', []):
            parent = latest.get((rec['feature'], item['node']))
            done = (self.completion(rec['feature']).get(item['node']) or {}).get('status') in COMPLETED_STATUSES
            if not done and (not parent or parent['state'] not in runs.VERIFIED):
                transient.append(f'input {item["node"]} is not verified')
        choice = None
        if not blockers and not transient:
            choice, reasons = self.pick_channel(rec)
            if choice is None and any(r.endswith(PAUSED_SUFFIXES) for r in reasons):
                # a channel that is only paused (a 12 h inspector pause, a budget stop, the failure window) comes
                # back by itself: the run waits, closed, instead of a final escalation
                transient.extend(reasons)
            else:
                blockers.extend(reasons if choice is None else [])
        if transient and not blockers:
            self.line('inspector', rec['run_id'], 'none', 'requeue waits: ' + '; '.join(transient), record=rec)
            if any(r.endswith(PAUSED_SUFFIXES) for r in transient):
                self.fixer(rec, f'requeue waits for a paused channel ({why}): ' + '; '.join(transient), evidence,
                           'waiting-on-pause')
            return None
        if blockers:
            text = f'cannot requeue ({why}): ' + '; '.join(blockers + transient)
            if rec.get('inspector_closed') and not self.dry_run:
                held = 0 if proof else self.unexpired_claim_minutes(rec)
                if held:
                    # never proven dead and still inside its claim: stay timeout + inspector_closed (the dispatcher
                    # skips it), escalate on the first tick after the claim expires (review of b9cdd04f)
                    self.line('inspector', rec['run_id'], 'none', f'requeue blocked; stays closed until its file '
                              f'claim expires ({int(held)} min left, no proof the worker died)', record=rec)
                elif rec['state'] == 'timeout':
                    self.free_claims(rec, proof)
                    self.store.transition(rec['run_id'], 'escalated', worker='watchdog',
                                          detail=f'inspector: {text}'[:contract.DETAIL_MAX])
            return self.fixer(rec, text, evidence, 'cannot-requeue')
        channel, model = choice
        held = (0 if proof else self.unexpired_claim_minutes(rec)) if self.dry_run else self.free_claims(rec, proof)
        if self.dry_run and not held:
            self.line('inspector', rec['run_id'], 'requeue', f'would requeue on {channel}: {why}', record=rec)
            return None
        if held:
            self.line('inspector', rec['run_id'], 'none', f'requeue waits for its unexpired file claim '
                      f'({int(held)} min left, no proof the worker died)', record=rec)
            return None
        new_id = f'{rec["feature"]}-{rec["node"]}-{uuid.uuid4().hex[:12]}'
        brief = dict(rec['brief'], run_id=new_id, channel=channel, model=model,
                     deadline=self.now + runs.DEFAULT_DEADLINE_S,
                     result=str(self.data / 'team/loop/results' / f'{new_id}.json'))
        contract.validate_brief(brief)
        detail = f'inspector requeue of {rec["run_id"]} from {rec["brief"]["channel"]} to {channel}: {why}'
        successor = dict(rec, run_id=new_id, brief=brief, state='queued', tier=tier, created=self.now,
                         updated=self.now, deadline=brief['deadline'], result=None,
                         partial=rec.get('partial') or rec.get('result'), retry_of=rec['run_id'],
                         expected_min=expected_minutes(brief, rec.get('expected_min')),
                         history=[dict(t=self.now, state='queued', detail=detail[:contract.DETAIL_MAX])])
        for field in ('silent_since', 'hold', 'pid', 'inspector_closed'):
            successor.pop(field, None)
        self.store._commit(successor, worker='watchdog', channel=channel, path=successor['partial'],
                           detail=detail[:contract.DETAIL_MAX])
        self.state['requeued'][rec['run_id']] = new_id
        self.state['node_requeues'][key] = self.state['node_requeues'].get(key, 0) + 1
        self.summary['requeued'] += 1
        self.line('inspector', rec['run_id'], 'requeue', detail, record=rec)
        self.post(rec, f'requeue:{new_id}', 'inspector_requeue', detail, evidence)
        return new_id

    def fixer(self, rec, why, evidence, key):
        """One request row the Board or home chat routes to an Opus fixer, with the evidence. Nothing is spawned."""
        self.post(rec, f'fixer:{why[:60]}', 'inspector',
                  f'{rec["run_id"]} ({rec["feature"]}:{rec["node"]}) needs a fixer: {why}', evidence,
                  extra=dict(to='board', needs=f'route to {FIXER} with this evidence', fixer=FIXER),
                  routing=f'inspector:{rec["run_id"]}:{key}')

    def post(self, rec, key, kind, text, evidence, *, extra=None, routing=None):
        routing = routing or f'inspector:{rec["run_id"] if rec else "loop"}:{key}'
        if routing in self.state['posted']:
            return
        if self.dry_run:
            self.line('inspector', rec['run_id'] if rec else key, kind, f'would post: {text}', record=rec)
            return
        row = {'t': self.now, 'from': 'inspector', 'kind': kind, 'text': text[:600], 'evidence': evidence[:600],
               'routing_id': routing, **(extra or {})}
        if rec:
            row.update(run_id=rec['run_id'], feature=rec['feature'], node=f'{rec["feature"]}:{rec["node"]}')
        board_inbox.append(row, path=self.data / 'team/board-inbox.jsonl', once=True)
        self.state['posted'][routing] = self.now

    # ---- bookkeeping --------------------------------------------------------------------------------------------

    def completion(self, feature):
        rows = _read_json(self.data / 'team/features' / feature / 'completion.json', {}).get('nodes', {})
        return rows if isinstance(rows, dict) else {}

    def metric(self):
        """One loop-level event per tick: runs checked, dead caught, minutes from death to detection."""
        s = self.summary
        detail = dict(inspector=True, checked=s['checked'], dead=s['dead'], behind=s['behind'],
                      overdue=s['overdue'], requeued=s['requeued'], stale=s['stale'], errors=s['errors'],
                      detect_max_minutes=max(s['detect_minutes'], default=0))  # E0 detail values are scalars
        if not self.dry_run:
            self.emit(dict(t=self.now, run_id=None, feature=None, node=None, worker='watchdog', channel=None,
                           kind='watchdog', sha=None, path=None,
                           detail=json.dumps(detail, separators=(',', ':'))[:contract.DETAIL_MAX]))

    def save(self):
        if self.dry_run:
            return
        self.state['lock_skips'] = 0  # the inspector ran: a starvation streak ends
        cutoff = self.now - 7 * 24 * 3600
        self.state['posted'] = {k: t for k, t in self.state['posted'].items() if t >= cutoff}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=1, sort_keys=True) + '\n')
        temporary.replace(self.state_path)
