#!/usr/bin/env python3
"""Loop metrics (M0): the eleven design-item metrics of LOOP-BUILDING plus the inspector's, measured per window.

  loop_metrics.py --since <iso> [--until <iso>] [--data-dir DIR] [--tokens FILE] [--before FILE] [--write]

Every line is `metric | value | target | status`, status in pass, fail or pending. A metric whose inputs
are missing is pending, never pass. Without --write nothing is written anywhere (the data dir is only
read; the SQLite index is not touched). With --write the report goes to <data>/team/loop/metrics/<date>.json
and one row per metric (kind "metric") is appended to <data>/team/board-inbox.jsonl.

Inputs, all under the data dir except --tokens (K0's scripts/claude_tokens.py output file):
  team/loop/events.jsonl and team/loop-events.jsonl   F0 events (E0 store, and R0's seam before E0 lands)
  team/loop/runs/*.json                               R0 run records (deadline, state, result path)
Events in the window are those with t in [since, until); an event whose time is unknown is skipped.

Measurements (the detail keys are F0 detail JSON objects; rows carry counts, never message text):
  stuck_past_timeout            run records (else events with a detail deadline) live past their deadline at
                                the window end, only the newest run of a node that is not done; Board-held runs
                                are reported in the row's `held` field, not stuck. Target 0.
  detection_time                per `timeout` event: its time minus the earlier of the run's deadline and
                                (last activity + 20 min of silence). Maximum seconds; target under 300.
  lock_waits_and_steals         `lock_wait` events with detail wait_seconds over 600, plus stale locks
                                (`lock_acquire` with an expired detail deadline and no release or steal).
                                Steals (`lock_steal`) are counted in the line and reported, not failed.
  open_questions_per_node       open_questions in each run's result file, or `question` events when there is
                                no result file, divided by node runs. Target under 0.1.
  median_slot_refill            median refill wait of `refill` events: from max(slot free, work available) to
                                filled (refill_waits). Empty-queue time before work appeared is reported as
                                idle_empty_queue_seconds in the detail, not counted. Target under 300.
  idle_worker_minutes_pct       `idle` events (detail seconds = idle worker-seconds while ready work exists)
                                over idle plus busy worker-seconds (run start to its first terminal event).
                                Target under 10 percent.
  search_calls_per_fresh_worker mean detail search_calls over runs that report it. Target 0.
  cli_answerable_questions      the eight status questions answered by the E0 query code on a scratch copy of
                                the event file; answered means the command succeeds. Target 8 of 8.
  claude_tokens_per_landed_node K0 file per_landed_node_new against the before file (--before, default
                                <data>/team/loop/metrics/claude-tokens-before.json, same K0 shape). Target at
                                most 50 percent of the before value; pending without a before file.
  fresh_context_share           percent of runs whose dispatched or started event has detail fresh=true,
                                among runs that carry the flag. Target 100.
  peak_context_percent          maximum detail percent of `context` events (long-lived chats). Target under 70.
  inspector_detection           the inspector's per-tick `watchdog` events (detail inspector=true): runs checked,
                                dead workers caught and minutes from death to detection. Maximum minutes; target
                                under 10 (two watchdog ticks). Pending without an inspector tick.
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from companion.feature_team.completion import COMPLETED_STATUSES  # noqa: E402
from companion.feature_team.events import EventStore, metadata, timestamp  # noqa: E402
from companion.feature_team.inspector import hand_built  # noqa: E402

SILENT_SECONDS = 20 * 60
DETECTION_TARGET_SECONDS = 300
LOCK_WAIT_LIMIT_SECONDS = 600
QUESTIONS_TARGET = 0.1
REFILL_TARGET_SECONDS = 300
TICK_SPAN_SECONDS = 10  # work signals logged by the same dispatcher tick land within this of its tick event
MAX_EMPTY_STRETCH_SECONDS = 180  # a flagged tick vouches for this long (ticks run every ~2 min); a longer gap is wait
REFILL_LOOKBACK_SECONDS = 600  # finds the dispatcher tick at or before a slot's free time (ticks run every ~2 min)
IDLE_TARGET_PERCENT = 10
TOKENS_SHARE = 0.5
CONTEXT_TARGET_PERCENT = 70
INSPECTOR_TARGET_MINUTES = 10
CLI_QUESTIONS = 8
BEFORE_FILE = 'team/loop/metrics/claude-tokens-before.json'
START = {'dispatched', 'started'}
CLOSED = {'verify_pass', 'landed', 'escalated', 'cancelled', 'done', 'failed', 'blocked'}
TERMINAL = CLOSED | {'result', 'verify_fail', 'timeout'}
STUCK_NAMED = 5  # stuck runs named in the detail, most overdue first
LIVE_STATES = {'queued', 'dispatched', 'started', 'heartbeat', 'result', 'verify_fail', 'fixback', 'timeout'}
ACTIVITY = {'dispatched', 'started', 'heartbeat', 'result'}

TARGETS = {
    'stuck_past_timeout': '0 stuck',
    'detection_time': f'under {DETECTION_TARGET_SECONDS} s',
    'lock_waits_and_steals': f'no wait over {LOCK_WAIT_LIMIT_SECONDS // 60} min, 0 stale locks',
    'open_questions_per_node': f'under {QUESTIONS_TARGET}',
    'median_slot_refill': f'under {REFILL_TARGET_SECONDS} s',
    'idle_worker_minutes_pct': f'under {IDLE_TARGET_PERCENT} percent',
    'search_calls_per_fresh_worker': '0',
    'cli_answerable_questions': f'{CLI_QUESTIONS} of {CLI_QUESTIONS}',
    'claude_tokens_per_landed_node': 'at most 50 percent of before',
    'fresh_context_share': '100 percent',
    'peak_context_percent': f'under {CONTEXT_TARGET_PERCENT} percent',
    'inspector_detection': f'dead worker caught under {INSPECTOR_TARGET_MINUTES} min',
}


def _row(metric, value, status, detail=''):
    return dict(metric=metric, value=value, target=TARGETS[metric], status=status, detail=detail)


def _pending(metric, why):
    return _row(metric, None, 'pending', why)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_jsonl(path):
    """Complete JSON object lines; a bad line is counted, not fatal (metrics must never hide a loop)."""
    rows, bad = [], 0
    if path.is_file():
        for text in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not text.strip():
                continue
            try:
                row = json.loads(text)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def load_events(data, since, until):
    """Events in [since, until) from both event files, time-ordered; unknown times are skipped."""
    events, bad = [], 0
    for relative in ('team/loop/events.jsonl', 'team/loop-events.jsonl'):
        rows, skipped = read_jsonl(Path(data) / relative)
        bad += skipped
        for row in rows:
            detail = row.get('detail')
            meta = metadata(detail) if isinstance(detail, str) else {}
            t = row.get('t')
            if meta.get('time_missing') or not isinstance(t, (int, float)) or isinstance(t, bool):
                continue
            if t < since or (until is not None and t >= until):
                continue
            events.append(dict(row, _meta=meta, _kind=meta.get('legacy_kind') or row.get('kind')))
    events.sort(key=lambda e: e['t'])
    return events, bad


def load_records(data):
    folder = Path(data) / 'team/loop/runs'
    records = {}
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        if path.name.startswith('.tmp-'):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('run_id') == path.stem:
            records[path.stem] = record
    return records


def group_runs(events):
    runs = {}
    for event in events:
        if event.get('run_id'):
            runs.setdefault(event['run_id'], []).append(event)
    return runs


def _completion(data, feature, cache):
    if feature not in cache:
        try:
            rows = json.loads((Path(data) / 'team/features' / feature / 'completion.json').read_text()).get('nodes')
        except (OSError, ValueError, AttributeError, TypeError):
            rows = None
        cache[feature] = rows if isinstance(rows, dict) else {}
    return cache[feature]


def stuck(events, records, end, data=None):
    """Live runs past their deadline at `end`. Records win over events for the same run_id.

    Like the inspector, only the newest record of a node (feature, node; ordered by created then run_id) is judged:
    an older run, or one a same-node successor names in retry_of, is superseded history. Only records created
    before `end` supersede; a record without created is always judged. A node whose completion row is done
    is history too. A run held by the Board (hold on the record or the completion row, or a row dispatched
    by: board) that would otherwise be stuck is counted in `held`, not stuck.
    A run with no known deadline cannot be judged: it is left out and counted in the detail; with no
    judgeable run at all, or only held ones, the metric is pending. The detail names up to STUCK_NAMED stuck runs
    (id, feature/node, channel, state, minutes past the deadline) so someone can act on them."""
    judged, unjudged, superseded, done, held, examined, cache = {}, 0, 0, 0, 0, 0, {}
    named, runs = {}, group_runs(events)

    def channel(run_id, record=None):
        known = [e.get('channel') for e in runs.get(run_id, ()) if e.get('channel')]
        return (record or {}).get('channel') or (known[-1] if known else '?')

    def key(record):
        feature, node = record.get('feature'), record.get('node')
        return (feature, node) if isinstance(feature, str) and isinstance(node, str) else None

    # Only records created before `end` can supersede. A record without `created` is never superseded and never
    # supersedes (fail safe: an unknown time must not hide a stuck run either way).
    before = {run_id: r for run_id, r in records.items() if (c := _number(r.get('created'))) is not None and c < end}
    retried = {r['retry_of'] for r in before.values()
               if r.get('retry_of') in records and key(r) is not None and key(r) == key(records[r['retry_of']])}
    latest = {}
    for run_id, record in sorted(before.items(), key=lambda kv: (kv[1]['created'], kv[0])):
        if run_id not in retried:
            latest[key(record) or run_id] = run_id
    newest = set(latest.values()) | {run_id for run_id, r in records.items() if _number(r.get('created')) is None}
    for run_id, record in records.items():
        deadline = _number(record.get('deadline'))
        created = _number(record.get('created'))
        if deadline is None:
            unjudged += 1
            continue
        if created is not None and created >= end:
            continue
        examined += 1
        overdue = record.get('state') in LIVE_STATES and end > deadline
        if run_id not in newest:
            superseded += overdue
            continue
        feature, node = record.get('feature'), record.get('node')
        row = _completion(data, feature, cache).get(node) if data and isinstance(feature, str) else None
        row = row if isinstance(row, dict) else {}
        if row.get('status') in COMPLETED_STATUSES:
            done += overdue
            continue
        if overdue and (record.get('hold') or row.get('hold') or hand_built(row)):
            held += 1
            continue
        judged[run_id] = overdue
        if overdue:
            named[run_id] = (end - deadline, f'{feature}/{node}', channel(run_id, record), record.get('state'))
    for run_id, rows in runs.items():
        if run_id in judged or run_id in records:
            continue
        deadline = max((d for d in (_number(e['_meta'].get('deadline')) for e in rows) if d is not None), default=None)
        if deadline is None:
            unjudged += any(e['_kind'] in START for e in rows)
            continue
        started = any(e['_kind'] in START for e in rows)
        judged[run_id] = bool(started and rows[-1]['_kind'] not in CLOSED and end > deadline)
        if judged[run_id]:
            named[run_id] = (end - deadline, f"{rows[-1].get('feature')}/{rows[-1].get('node')}", channel(run_id),
                             rows[-1]['_kind'])
        examined += 1
    left_out = f'{superseded} superseded, {done} on done nodes, {held} held (Board) not counted'
    if not examined or (not judged and held):
        return dict(_pending('stuck_past_timeout', f'no run with a known deadline ({unjudged} without one); '
                             + left_out), held=held)
    count = sum(judged.values())
    worst = sorted(named.items(), key=lambda kv: -kv[1][0])[:STUCK_NAMED]
    stuck_runs = ''.join(f'; stuck: {run_id} {where} {chan} {state} {round(late / 60)} min past deadline'
                         for run_id, (late, where, chan, state) in worst)
    return dict(_row('stuck_past_timeout', count, 'pass' if count == 0 else 'fail',
                     f'{examined} runs checked, {unjudged} without a deadline not judged; {left_out}{stuck_runs}'),
                held=held)


def detection(events, records):
    lags = []
    for run_id, rows in group_runs(events).items():
        deadline = _number((records.get(run_id) or {}).get('deadline'))
        last_activity = None
        for event in rows:
            deadline = deadline if deadline is not None else _number(event['_meta'].get('deadline'))
            if event['_kind'] == 'timeout':
                triggers = [d for d in (deadline,
                                        None if last_activity is None else last_activity + SILENT_SECONDS)
                            if d is not None]
                if triggers:
                    lags.append(max(0.0, event['t'] - min(triggers)))
            elif event['_kind'] in ACTIVITY:
                last_activity = event['t']
    if not lags:
        return _pending('detection_time', 'no timeout event with a known deadline or last activity')
    worst = max(lags)
    return _row('detection_time', round(worst, 1), 'pass' if worst < DETECTION_TARGET_SECONDS else 'fail',
                f'{len(lags)} timeouts, median {round(statistics.median(lags), 1)} s')


def locks(events, end):
    waits = [e for e in events if e['_kind'] == 'lock_wait']
    steals = [e for e in events if e['_kind'] == 'lock_steal']
    acquires = [e for e in events if e['_kind'] == 'lock_acquire']
    if not (waits or steals or acquires):
        return _pending('lock_waits_and_steals', 'no lock events')
    long_waits = sum(1 for e in waits if (_number(e['_meta'].get('wait_seconds')) or 0) > LOCK_WAIT_LIMIT_SECONDS)
    closed = {e['run_id'] for e in events if e['_kind'] in ('lock_release', 'lock_steal')}
    stale = sum(1 for e in acquires if e['run_id'] not in closed
                and (_number(e['_meta'].get('deadline')) or float('inf')) < end)
    problems = long_waits + stale
    return _row('lock_waits_and_steals', problems, 'pass' if problems == 0 else 'fail',
                f'{len(waits)} waits ({long_waits} over {LOCK_WAIT_LIMIT_SECONDS} s), '
                f'{len(steals)} steals, {stale} stale locks')


def _result_questions(record):
    path = record.get('result')
    if not isinstance(path, str):
        return None
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    questions = document.get('open_questions') if isinstance(document, dict) else None
    return len(questions) if isinstance(questions, list) else None


def open_questions(events, records, since, end):
    runs = group_runs(events)
    node_runs = {rid for rid, rows in runs.items() if any(e['_kind'] in START | {'result'} for e in rows)}
    for rid, record in records.items():
        updated = _number(record.get('updated'))
        if record.get('state') != 'queued' and updated is not None and since <= updated < end:
            node_runs.add(rid)
    if not node_runs:
        return _pending('open_questions_per_node', 'no node runs in the window')
    total = 0
    for rid in node_runs:
        from_events = sum(1 for e in runs.get(rid, []) if e['_kind'] == 'question')
        from_result = _result_questions(records.get(rid, {}))
        total += from_result if from_result is not None else from_events
    value = round(total / len(node_runs), 4)
    return _row('open_questions_per_node', value, 'pass' if value < QUESTIONS_TARGET else 'fail',
                f'{total} questions over {len(node_runs)} node runs')


WORK_EVENTS = {'queued', 'lock_wait', 'dispatch_refused', 'escalated',
               'result', 'verify_pass', 'verify_fail', 'fixback', 'timeout'}


def _work_signals(context, autopilot_rows, queue_mtime):
    """Times at which work existed, ready or blocked: a queued node, a lock wait, a refused dispatch, an escalation, a
    slot idle while ready work existed (Q0 emits idle seconds only then), any autopilot refusal whatever its reason
    (low power, GPU busy, a broken channel, a held gate, a memory hold), a feature started, or a feature-queue.json
    change (both the tick that logged it and the file's mtime)."""
    times = [e['t'] for e in context if e['_kind'] in WORK_EVENTS
             or (e['_kind'] == 'idle' and (_number(e['_meta'].get('seconds')) or 0) > 0)]
    for row in autopilot_rows:
        t, kind = _number(row.get('t')), row.get('event') or row.get('kind')
        if kind == 'queue_changed':
            times.append(_number(row.get('mtime')) or t)
        elif kind not in {'feature_started', 'refused'}:
            continue
        if t is not None:
            times.append(t)
    if queue_mtime is not None:
        times.append(queue_mtime)
    return sorted(times)


def refill_waits(events, data=None, stretches=None, since=None):
    """Per Q0 `refill` event, the seconds from max(slot free, work available) to slot filled.

    Q0's refill_seconds runs from slot free, so a slot that sat free while every queue was empty looked like a slow
    refill (window 4: 38 min of empty ticks until the owner queued a feature, filled 32 s later). Time is split at the
    dispatcher ticks, starting with the last tick at or before slot free. A tick's stretch is empty-queue time only
    when the dispatcher flagged that tick `empty_queue: true` and no work signal falls within TICK_SPAN_SECONDS of
    it; it ends at the next tick, at the first work signal or MAX_EMPTY_STRETCH_SECONDS after the tick (a stopped
    dispatcher never reads as an empty queue). Everything else counts as refill wait: unflagged
    ticks (all ticks before the flag existed), stopped ticks, time with no tick, and ticks near blocked work, so a
    doubt always counts the wait. With `since` (a window start), a slot freed before it counts from `since`: time
    before the window belongs to no window (window 7 read 61176 s for slots refilled 40 s after it opened).
    Returns (waits, idle_empty_queue_seconds, empty_ticks, unflagged_ticks)."""
    refills = [(e['t'], s) for e in events if e['_kind'] == 'refill'
               for s in [_number(e['_meta'].get('refill_seconds'))] if s is not None]
    if not refills:
        return [], 0.0, 0, 0
    context, autopilot_rows, queue_mtime = events, [], None
    if data is not None:
        lo, hi = min(t - s for t, s in refills) - REFILL_LOOKBACK_SECONDS, max(t for t, _ in refills) + 1
        context, _ = load_events(data, lo, hi)
        rows, _ = read_jsonl(Path(data) / 'team/autopilot.jsonl')
        autopilot_rows = [r for r in rows if lo <= (_number(r.get('t')) or 0) < hi]
        queue = Path(data) / 'team/feature-queue.json'
        queue_mtime = queue.stat().st_mtime if queue.is_file() else None
    ticks = sorted((e['t'], e['_meta'].get('empty_queue') is True and e['_meta'].get('status') != 'stopped',
                    'empty_queue' in e['_meta']) for e in context if e['_kind'] == 'tick')
    signals = _work_signals(context, autopilot_rows, queue_mtime)
    waits, idle_empty, empty_count, unflagged = [], 0.0, 0, 0
    for filled, seconds in refills:
        seconds = seconds if since is None else min(seconds, max(0.0, filled - since))
        free = filled - seconds
        before = [tick for tick in ticks if tick[0] <= free]
        inside = before[-1:] + [tick for tick in ticks if free < tick[0] < filled]
        empty_here = 0.0
        for i, (t, flagged_empty, has_flag) in enumerate(inside):
            unflagged += not has_flag
            if not flagged_empty or any(t <= x < t + TICK_SPAN_SECONDS for x in signals):
                continue
            stop = min([inside[i + 1][0] if i + 1 < len(inside) else filled, t + MAX_EMPTY_STRETCH_SECONDS]
                       + [x for x in signals if x > t])
            empty_here += max(0.0, min(stop, filled) - max(t, free))
            if stretches is not None:
                stretches.append((t, max(0.0, min(stop, filled) - max(t, free))))
            empty_count += 1
        idle_empty += empty_here
        waits.append(seconds - empty_here if empty_here else seconds)
    return waits, idle_empty, empty_count, unflagged


def idle_escalated_seconds(events, data=None, since=None):
    """The flagged-empty time spent while a run sat escalated (reported apart from idle_empty_queue_seconds), so a window waiting on the Board shows.
    A flagged-empty tick's stretch counts when a run's last event at that tick is `escalated` (from its escalation to
    its next event of another kind) or an autopilot `skipped ... escalated run` row is logged within TICK_SPAN_SECONDS
    of it. Those rows stay out of the work signals: the wait is not counted, only shown."""
    stretches = []
    refill_waits(events, data, stretches, since)
    if not stretches:
        return 0.0
    hi = max(t for t, _ in stretches) + TICK_SPAN_SECONDS
    context, rows = events, []
    if data is not None:
        context, _ = load_events(data, 0, hi)
        rows, _ = read_jsonl(Path(data) / 'team/autopilot.jsonl')
    skipped = [_number(r.get('t')) or 0 for r in rows
               if (r.get('event') or r.get('kind')) == 'skipped' and 'escalated' in str(r.get('reason'))]
    runs = group_runs(context)

    def escalated(tick):
        if any(tick <= x < tick + TICK_SPAN_SECONDS for x in skipped):
            return True
        for rows_of_run in runs.values():
            last = [e for e in rows_of_run if e['t'] <= tick and e['_kind'] != 'context']
            if last and last[-1]['_kind'] == 'escalated':
                return True
        return False
    return sum(seconds for tick, seconds in stretches if escalated(tick))


def refill(events, data=None):
    seconds, idle_empty, empty_ticks, unflagged = refill_waits(events, data)
    if not seconds:
        return _pending('median_slot_refill', 'no refill events with refill_seconds')
    median, escalated = statistics.median(seconds), idle_escalated_seconds(events, data)
    idle_empty -= escalated  # shown on its own: waiting on the Board, not an empty queue
    return _row('median_slot_refill', round(median, 1), 'pass' if median < REFILL_TARGET_SECONDS else 'fail',
                f'{len(seconds)} refills; idle_empty_queue_seconds {round(idle_empty, 1)} over {empty_ticks} empty ticks; '
                f'{unflagged} ticks without the empty_queue flag counted as wait; '
                f'idle_escalated_seconds {round(escalated, 1)}')


def idle(events, end):
    idle_events = [e for e in events if e['_kind'] == 'idle']
    if not idle_events and not any(e['_kind'] == 'tick' for e in events):
        return _pending('idle_worker_minutes_pct', 'no tick or idle events, so idleness was not measured')
    idle_seconds = sum(_number(e['_meta'].get('seconds')) or 0 for e in idle_events)
    busy = 0.0
    for rows in group_runs(events).values():
        start = next((e['t'] for e in rows if e['_kind'] in START), None)
        if start is None:
            continue
        stop = next((e['t'] for e in rows if e['t'] >= start and e['_kind'] in TERMINAL), end)
        busy += max(0.0, stop - start)
    if idle_seconds + busy == 0:
        return _pending('idle_worker_minutes_pct', 'no worker time recorded')
    percent = 100.0 * idle_seconds / (idle_seconds + busy)
    return _row('idle_worker_minutes_pct', round(percent, 2), 'pass' if percent < IDLE_TARGET_PERCENT else 'fail',
                f'{round(idle_seconds / 60, 1)} idle and {round(busy / 60, 1)} busy worker-minutes')


def searches(events):
    per_run = {}
    for event in events:
        value = _number(event['_meta'].get('search_calls'))
        if value is not None and event.get('run_id'):
            per_run[event['run_id']] = value
    if not per_run:
        return _pending('search_calls_per_fresh_worker', 'no events carry search_calls')
    mean = sum(per_run.values()) / len(per_run)
    return _row('search_calls_per_fresh_worker', round(mean, 3), 'pass' if mean == 0 else 'fail',
                f'{len(per_run)} workers, most searches {max(per_run.values())}')


def cli_questions(events, since, end):
    """Ask the eight status questions of the E0 query code on a scratch copy; the data dir stays untouched."""
    if not events:
        return _pending('cli_answerable_questions', 'no events to query')
    feature = next((e['feature'] for e in events if e.get('feature')), None)
    node = next((e['node'] for e in events if e.get('node')), None)
    worker = next((e['worker'] for e in events if e.get('worker')), None)
    with tempfile.TemporaryDirectory(prefix='loop-metrics-') as scratch:
        folder = Path(scratch) / 'team/loop'
        folder.mkdir(parents=True)
        with (folder / 'events.jsonl').open('w', encoding='utf-8') as out:
            for event in events:
                out.write(json.dumps({k: event[k] for k in event if not k.startswith('_')}, sort_keys=True) + '\n')
        store = EventStore(scratch)
        window = dict(since=since, until=end, now=end)
        questions = {
            'what is running': lambda: store.status(now=end),
            'what is stuck': lambda: store.status(now=end, stuck=True),
            'what did worker X do': lambda: store.query(worker=worker, **window),
            'which nodes of feature Y are done': lambda: store.query(feature=feature, kind='verify_pass', **window),
            'what failed since the window start': lambda: store.query(kind='verify_fail', **window),
            'where is the result of node Z': lambda: store.query(node=node, kind='result', **window),
            'how long did node Z wait for a lock': lambda: store.query(node=node, kind='lock_wait', **window),
            'how many refills': lambda: store.query(kind='refill', **window),
        }
        answered, failed = 0, []
        for name, ask in questions.items():
            try:
                ask()
                answered += 1
            except Exception as exc:  # any failure of the query path counts as unanswerable
                failed.append(f'{name}: {type(exc).__name__}')
    return _row('cli_answerable_questions', f'{answered} of {len(questions)}',
                'pass' if answered == CLI_QUESTIONS else 'fail',
                'all answered' if not failed else '; '.join(failed))


def _per_node(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, None
    if not isinstance(document, dict):
        return None, None
    return _number(document.get('per_landed_node_new')), document


def tokens(path, before_path=None):
    """Window value from K0's file against the before file: pass at or under half of the before value."""
    metric = 'claude_tokens_per_landed_node'
    if path is None:
        return _pending(metric, 'no K0 file given (scripts/claude_tokens.py --write)')
    if before_path is None or not Path(before_path).is_file():
        return _pending(metric, 'no before file (claude-tokens-before.json), so the relative target cannot be judged')
    before, _ = _per_node(before_path)
    if before is None or before <= 0:
        return _pending(metric, 'before file has no usable per_landed_node_new')
    value, document = _per_node(path)
    if value is None:
        return _pending(metric, 'K0 file unreadable or has no per_landed_node_new (no landed node)')
    limit = before * TOKENS_SHARE
    row = _row(metric, value, 'pass' if value <= limit else 'fail',
               f'{len(document.get("landed_nodes") or [])} landed nodes, before {before}, '
               f'{round(100 * value / before, 1)} percent of before, limit {round(limit)}')
    row['target'] = f'at most {round(100 * TOKENS_SHARE)} percent of before ({before})'
    return row


def fresh(events):
    flags = {}
    for event in events:
        value = event['_meta'].get('fresh')
        if event['_kind'] in START and isinstance(value, bool) and event.get('run_id'):
            flags.setdefault(event['run_id'], value)
    if not flags:
        return _pending('fresh_context_share', 'no dispatched or started event carries fresh')
    percent = 100.0 * sum(flags.values()) / len(flags)
    return _row('fresh_context_share', round(percent, 1), 'pass' if percent == 100 else 'fail',
                f'{sum(flags.values())} of {len(flags)} runs fresh')


def peak_context(events):
    by_day = {}
    for event in events:
        value = _number(event['_meta'].get('percent'))
        if event['_kind'] == 'context' and value is not None:
            day = datetime.fromtimestamp(event['t']).date().isoformat()
            by_day[day] = max(by_day.get(day, 0), value)
    if not by_day:
        return _pending('peak_context_percent', 'no context events with percent')
    peak = max(by_day.values())
    return _row('peak_context_percent', peak, 'pass' if peak < CONTEXT_TARGET_PERCENT else 'fail',
                'per day: ' + ', '.join(f'{d} {v}' for d, v in sorted(by_day.items())))


def inspector(events):
    ticks = [e['_meta'] for e in events if e['_kind'] == 'watchdog' and e['_meta'].get('inspector') is True]
    if not ticks:
        return _pending('inspector_detection', 'no inspector tick event')
    checked = sum(_number(t.get('checked')) or 0 for t in ticks)
    dead = sum(_number(t.get('dead')) or 0 for t in ticks)
    worst = max((_number(t.get('detect_max_minutes')) or 0 for t in ticks), default=0)
    return _row('inspector_detection', worst, 'pass' if worst < INSPECTOR_TARGET_MINUTES else 'fail',
                f'{len(ticks)} ticks, {checked} runs checked, {dead} dead caught')


def compute(data, since, until=None, tokens_path=None, now=None, before_path=None):
    """All metric rows, in the order of TARGETS. since/until are epoch seconds."""
    now = datetime.now().timestamp() if now is None else now
    end = until if until is not None else now
    events, bad = load_events(data, since, until)
    records = load_records(data)
    rows = [stuck(events, records, end, data), detection(events, records), locks(events, end),
            open_questions(events, records, since, end), refill(events, data), idle(events, end), searches(events),
            cli_questions(events, since, end), tokens(tokens_path, before_path if before_path is not None else Path(data) / BEFORE_FILE), fresh(events), peak_context(events),
            inspector(events)]
    assert [r['metric'] for r in rows] == list(TARGETS)
    if bad:
        for row in rows:
            row['detail'] = (row['detail'] + f'; {bad} unreadable event lines skipped').lstrip('; ')
    return rows


def format_value(row):
    return 'pending' if row['value'] is None else str(row['value'])


def format_line(row):
    return f"{row['metric']} | {format_value(row)} | {row['target']} | {row['status']}"


def write_report(data, rows, since, end, now):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion import board_inbox

    day = datetime.fromtimestamp(end).date().isoformat()
    folder = Path(data) / 'team/loop/metrics'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'{day}.json'
    report = dict(window=dict(since=since, until=end), written=now, metrics=rows)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')
    os.replace(temp, path)
    inbox = Path(data) / 'team/board-inbox.jsonl'
    for row in rows:
        board_inbox.append({'t': now, 'from': 'loop_metrics', 'kind': 'metric', 'metric': row['metric'],
                            'value': row['value'], 'target': row['target'], 'status': row['status'],
                            'text': format_line(row)}, path=inbox)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--since', required=True, help='ISO time or epoch seconds; naive means local time')
    parser.add_argument('--until', help='exclusive window end; default now')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('KYRA_DATA_DIR', ROOT / 'data')))
    parser.add_argument('--tokens', type=Path, help="K0's claude_tokens.py output file")
    parser.add_argument('--before', type=Path, help="K0 file of the before run; default <data>/" + BEFORE_FILE)
    parser.add_argument('--write', action='store_true', help='write the report file and the Board inbox rows')
    args = parser.parse_args(argv)
    try:
        since = timestamp(args.since)
        until = timestamp(args.until)
    except ValueError:
        print('loop_metrics: --since/--until need an ISO time or epoch seconds', file=sys.stderr)
        return 2
    if since is None or (until is not None and until <= since):
        print('loop_metrics: the window must have since before until', file=sys.stderr)
        return 2
    now = datetime.now().timestamp()
    rows = compute(args.data_dir, since, until, args.tokens, now, args.before)
    for row in rows:
        print(format_line(row) + (f"   [{row['detail']}]" if row['detail'] else ''))
    if args.write:
        print('report: ' + str(write_report(args.data_dir, rows, since, until if until is not None else now, now)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
