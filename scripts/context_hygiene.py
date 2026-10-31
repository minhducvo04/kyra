#!/usr/bin/env python3
"""Lane context from token counts only: never file size, never message text.

Claude lanes: the last main-thread turn's input + cache tokens (context_handoff.read_context) of the
lane's CURRENT transcript. A Claude Desktop lane is resolved through its desktop session metadata
(the session whose CLI ids include the configured id, else the one titled "Kyra Team lane N"), then the
newest transcript among that session's current and prior CLI ids; the configured id is the fallback.
Codex lanes: the last ``token_count`` record of the thread's newest rollout. Claude CLI workers
(lane-send ``claude -p`` logs) are covered while their transcript is active.

The long-lived chat thresholds (warn at WARN_PERCENT, handoff and clear at CLEAR_PERCENT of the
context window) live here so scripts/context_handoff.py and these lane alerts share one rule; each
alert this tick writes is also one F0 ``context`` event in the loop event store.

Episodes: a breach opens an episode (``episode`` on every alert row); reminders repeat at most once per
COOLDOWN_SECONDS inside it. Dropping below the threshold writes ``hygiene_recovered`` and closes it, so
the next breach is a new episode the dispatcher acts on again.
"""
import argparse

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BYTES_PER_TOKEN = 7  # context_handoff's last-resort proxy only; this module never uses size.
THRESHOLD_TOKENS = 150_000
COOLDOWN_SECONDS = 6 * 60 * 60
WARN_PERCENT = 50   # design item 10: one warning line
CLEAR_PERCENT = 60  # design item 10: write the handoff, then clear
CONTEXT_WINDOW = 1_000_000  # tokens; the desktop session tool reports the real window when available
CODEX_WINDOW = 258_400  # used only when a token_count record carries no model_context_window
CLI_ACTIVE_SECONDS = 60 * 60  # a claude -p worker counts while its transcript changed this recently
LANE_TITLE = 'Kyra Team lane {}'
_CLI_LOG = re.compile(r'claude-([0-9a-f-]{36})\.log')

# The one context-management table: channel kind -> (how usage is read, what happens over the threshold).
# "fresh per task" couriers are proved by tests/test_lane_send.py::test_every_courier_starts_a_fresh_session_per_task;
# the local loops by tests/test_local_context_budget.py.
CHANNELS = {
    'claude': ('desktop lane: last main-thread turn tokens of its current transcript',
               'Board card; after each finished package the lane writes HANDOFF-<lane>.md and clears itself'),
    'codex': ('app thread: last token_count record of its newest rollout',
              'courier handoff + /compact (codex exec workers are fresh per task)'),
    'claude-cli': ('claude -p worker: last-turn tokens while active', 'fresh per task; Board card if one overflows'),
    'codex-exec': ('none needed', 'fresh per task (new codex exec thread per send)'),
    'cursor': ('none needed', 'fresh per task (cursor-agent -p per send)'),
    'grok': ('none needed', 'fresh per task (grok -p per send)'),
    'muse': ('none needed', 'fresh per task (muse exec per send)'),
    'local': ('estimated prompt tokens per loop (3 chars a token)',
              'chat history and the local tool loop keep whole exchanges inside the model window (newest never '
              'dropped, bounded if it overflows) and fold the rest into one fixed note; tool output is bounded '
              '(companion.context_budget); full_bench and the local lead worker '
              'have their own budgets; the home router and worker_loop are one-shot'),
}


def channel_kind(lane):
    return 'claude-cli' if lane.startswith('claude-cli-') else lane.rsplit('-', 1)[0]


def status_lines(root, sessions=None, *, desktop=None, logs=None, threshold_tokens=THRESHOLD_TOKENS, now=None):
    """One line per channel kind: its policy and, for measured lanes, today's percent of the model's window."""
    sessions = Path(sessions) if sessions is not None else Path.home() / '.codex/sessions'
    measured = {}
    for lane, tokens, window, _, how in lane_contexts(Path(root).absolute(), sessions, desktop=desktop, logs=logs,
                                                      now=now):
        kind = channel_kind(lane)
        flag = ' OVER' if tokens > threshold_tokens else ''
        measured.setdefault(kind, []).append(f'{lane} {100 * tokens / window:.1f}%{flag} ({how})')
    return [f"{kind}: {', '.join(measured.get(kind, [])) or 'no live lane'} | read: {read} | action: {action}"
            for kind, (read, action) in CHANNELS.items()]


def level(percent, warn=WARN_PERCENT, clear=CLEAR_PERCENT):
    """'ok', 'warn' or 'handoff' for one context percent."""
    return 'handoff' if percent >= clear else 'warn' if percent >= warn else 'ok'


def emit_context(data_dir, worker, *, percent, tokens, window, source, status=None, path=None, now=None):
    """Append one F0 loop-level ``context`` event (counts and paths only, never transcript text)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from companion.feature_team.events import EventStore
    detail = {'percent': round(percent, 1), 'tokens': int(tokens), 'window': int(window),
              'status': status or level(percent), 'source': source}
    if path is not None:
        detail['handoff_path'] = str(path)
    return EventStore(data_dir).append(dict(
        t=time.time() if now is None else now, run_id=None, feature=None, node=None, worker=worker,
        channel=None, kind='context', sha=None, path=None if path is None else str(path),
        detail=json.dumps(detail, separators=(',', ':'), sort_keys=True)))


def warning_line(lane):
    """Stop-hook text for the dispatcher; lane identifies the recipient at the caller."""
    return 'context over 150k tokens: write HANDOFF.md, then clear'


# ---- readers: token fields only ------------------------------------------------------------------------------

def claude_tokens(transcript):
    """(tokens, window, source) from the last main-thread turn's usage, or None when no turn carries usage."""
    try:
        from scripts import context_handoff
    except ImportError:  # run as a script: scripts/ is sys.path[0]
        import context_handoff
    context = context_handoff.read_context(transcript=transcript)
    if context['source'] != 'transcript':
        return None  # the size proxy is not a measurement
    return context['tokens'], context['window'], 'transcript'


def codex_tokens(rollout):
    """(tokens, window, source) from the rollout's last token_count record; other rows are never parsed."""
    found = None
    with Path(rollout).open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if '"token_count"' not in line:
                continue
            try:
                payload = json.loads(line).get('payload') or {}
            except ValueError:
                continue
            info = payload.get('info') if payload.get('type') == 'token_count' else None
            last = (info or {}).get('last_token_usage') or {}
            tokens = last.get('total_tokens') or (last.get('input_tokens') or 0) + (last.get('output_tokens') or 0)
            if tokens:
                found = (int(tokens), int(info.get('model_context_window') or CODEX_WINDOW), 'rollout')
    return found


# ---- which file holds each lane's current context -------------------------------------------------------------

def _newest(paths):
    best, best_mtime = None, None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if best is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def claude_transcript(cli_id, projects=None):
    """Newest transcript file for one CLI session id across project folders."""
    projects = projects or Path.home() / '.claude/projects'
    return _newest(projects.glob(f'*/{cli_id}.jsonl'))


def desktop_sessions(base=None):
    """Desktop session metadata (ids, title, archive flag, activity time); no chat text is stored there."""
    base = base or Path.home() / 'Library/Application Support/Claude/claude-code-sessions'
    for path in sorted(base.glob('*/*/local_*.json')) if base.is_dir() else ():
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and not meta.get('isArchived'):
            yield {k: meta.get(k) for k in ('sessionId', 'cliSessionId', 'priorCliSessionIds', 'title',
                                            'lastActivityAt')}


def resolve_claude_lane(index, config_id, sessions, projects=None):
    """(transcript, how) for Claude lane ``index``: the desktop session's newest transcript, else config."""
    def cli_ids(meta):
        return [i for i in [meta.get('cliSessionId'), *(meta.get('priorCliSessionIds') or [])] if i]

    by_id = [m for m in sessions if config_id and config_id in cli_ids(m)]
    by_title = [m for m in sessions if m.get('title') == LANE_TITLE.format(index)]
    for group in (by_id, by_title):
        for meta in sorted(group, key=lambda m: m.get('lastActivityAt') or 0, reverse=True):
            path = _newest(filter(None, (claude_transcript(i, projects) for i in cli_ids(meta))))
            if path is not None:
                return path, f"desktop:{meta.get('sessionId')}"
    path = claude_transcript(config_id, projects) if config_id else None
    return path, 'config'


def lane_contexts(root, sessions, *, desktop=None, logs=None, now=None):
    """Yield (lane, tokens, window, source, how) per lane with a readable token count; text is never kept."""
    now = time.time() if now is None else now
    config = json.loads((root / 'data/team/config.json').read_text())
    meta = list(desktop_sessions(desktop))
    for index, lane in enumerate(config['lanes']):
        for role, participant in lane.get('participants', {}).items():
            session_id = participant.get('id')
            if role == 'claude':
                path, how = resolve_claude_lane(index, session_id, meta)
                if how == 'config':
                    print(f'context hygiene: claude-{index} uses the config id (desktop session not found)',
                          file=sys.stderr)
                reading = claude_tokens(path) if path else None
            elif role == 'codex' and session_id:
                path, how = _newest(sessions.rglob(f'*{session_id}*.jsonl')), 'config'
                reading = codex_tokens(path) if path else None
            else:
                continue
            if reading:
                yield f'{role}-{index}', *reading, how
    # Claude CLI workers: lane-send names each log after the worker's own session id.
    logs = Path(logs) if logs is not None else Path('/tmp/lane-send')
    for log in sorted(logs.glob('claude-*.log')) if logs.is_dir() else ():
        match = _CLI_LOG.fullmatch(log.name)
        path = claude_transcript(match.group(1)) if match else None
        if path is None or now - path.stat().st_mtime > CLI_ACTIVE_SECONDS:
            continue
        reading = claude_tokens(path)
        if reading:
            yield f'claude-cli-{match.group(1)[:8]}', *reading, 'cli-log'


def tick(root, *, dry_run=False, threshold_tokens=THRESHOLD_TOKENS, sessions=None, desktop=None, logs=None,
         now=None):
    """Post at most one alert per lane episode per six hours; dry runs never write."""
    if threshold_tokens <= 0:
        raise ValueError('threshold_tokens must be positive')
    root = Path(root).absolute()
    sessions = Path(sessions) if sessions is not None else Path.home() / '.codex/sessions'
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, UTC).isoformat()
    over, small = {}, {}
    for lane, tokens, window, source, how in lane_contexts(root, sessions, desktop=desktop, logs=logs, now=now):
        (over if tokens > threshold_tokens else small)[lane] = (tokens, window, source, how)
    team = root / 'data/team'
    state_path = team / 'context-hygiene-state.json'
    if not over and not state_path.exists():
        return []

    def plan(state):
        """Rows to append and the next state; a legacy numeric entry is a size-proxy episode to close."""
        state, rows = dict(state), []
        for lane, entry in list(state.items()):
            if not isinstance(entry, dict):
                rows.append({'t': stamp, 'from': 'lane-watch', 'kind': 'hygiene_recovered', 'lane': lane,
                             'tokens': 0, 'text': 'Size-proxy episode closed; token counts replace it.'})
                state.pop(lane)
        for lane, (tokens, window, source, how) in small.items():
            if lane in state:
                state.pop(lane)
                rows.append({'t': stamp, 'from': 'lane-watch', 'kind': 'hygiene_recovered', 'lane': lane,
                             'tokens': tokens, 'percent': round(100 * tokens / window, 1), 'source': source, 'id_source': how,
                             'text': 'Context is now below the hygiene threshold.'})
        alerts = []
        for lane, (tokens, window, source, how) in over.items():
            entry = state.get(lane)
            if entry and now - entry['last'] < COOLDOWN_SECONDS:
                continue
            episode = entry['episode'] if entry else f'{lane}@{int(now)}'
            state[lane] = {'episode': episode, 'last': now}
            percent = round(100 * tokens / window, 1)
            alerts.append({'t': stamp, 'from': 'lane-watch', 'kind': 'hygiene', 'lane': lane,
                           'tokens': tokens, 'percent': percent, 'window': window, 'source': source, 'id_source': how,
                           'episode': episode, 'policy': CHANNELS.get(channel_kind(lane), ('', ''))[1],
                           'text': f'{lane}: context {tokens} tokens ({percent}% of {window}) exceeds threshold.',
                           'action': 'clear' if tokens > 2 * threshold_tokens else 'compact'})
        return rows + alerts, alerts, state

    if dry_run:
        state = json.loads(state_path.read_text() or '{}') if state_path.exists() else {}
        return plan(state)[1]
    # Lock the state through inbox append and state update to serialize concurrent ticks.
    with state_path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        rows, alerts, state = plan(json.loads(stream.read() or '{}'))
        if rows:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
            from companion import board_inbox
            for row in rows:
                board_inbox.append(row, path=team / 'board-inbox.jsonl')
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(state))
        for row in alerts:
            emit_context(root / 'data', row['lane'], percent=row['percent'], tokens=row['tokens'],
                         window=row['window'], source=row['source'], now=now)
        return alerts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--threshold-tokens', type=int, default=THRESHOLD_TOKENS)
    parser.add_argument('--status', action='store_true', help='print one line per channel and write nothing')
    args = parser.parse_args()
    if args.status:
        print('\n'.join(status_lines(args.root, threshold_tokens=args.threshold_tokens)))
        return
    for row in tick(args.root, dry_run=args.dry_run, threshold_tokens=args.threshold_tokens):
        print(json.dumps(row))


if __name__ == '__main__':
    main()
