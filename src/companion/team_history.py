"""Read-only per-agent history for the Team page: short lines, never full bodies."""
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

AGENTS = ('kyra', 'claude', 'codex', 'cursor', 'antigravity')
APP_NAMES = {'cursor': 'Cursor', 'antigravity': 'Antigravity'}
LIMIT = 50
SUMMARY_CHARS = 140
_DATA_PATH = re.compile(r'\S*\bdata/\S*')


def summary(text):
    """First non-empty line, private data paths hidden, at most 140 characters."""
    line = next((row.strip() for row in str(text or '').splitlines() if row.strip()), '')
    line = _DATA_PATH.sub('[private path]', line)
    return line if len(line) <= SUMMARY_CHARS else line[:SUMMARY_CHARS - 1].rstrip() + '…'


def _entry(at, where, text, *, model=None, start=None, end=None):
    row = {'at': datetime.fromtimestamp(at, UTC).isoformat(timespec='seconds'),
           'where': where, 'summary': summary(text), '_at': at}
    if model:
        row['model'] = str(model)[:80]
    if start is not None and end is not None and end >= start:
        row['took_seconds'] = round(end - start)
    return row


def _native(state, agent):
    rows = []
    for room in state['rooms'].values():
        title = room['room']['title']
        runs = {run['id']: run for run in room['runs']}
        last_seen = {}
        for message in room['messages']:
            kind, author = message.get('kind'), message.get('author')
            run_id = message.get('run_id')
            start = last_seen.get(run_id, runs[run_id].get('created_at') if run_id in runs else None)
            if run_id:
                last_seen[run_id] = message['created_at']
            if author != agent:
                continue
            if kind == 'answer':
                if message.get('status') in {'queued', 'running'}:
                    continue
                text = message['text'] or f"Local answer {message.get('status', 'ended')}."
                rows.append(_entry(message.get('finished_at') or message['created_at'], f'{title} · local answer',
                                   text, model=message.get('model'), start=message.get('started_at'),
                                   end=message.get('finished_at')))
            elif kind == 'step':
                rows.append(_entry(message['created_at'], f"{title} · {message.get('step', 'step')}", message['text']))
            elif kind == 'report':
                rows.append(_entry(message['created_at'], f'{title} · report', message['text']))
            elif kind == 'result':
                rows.append(_entry(message['created_at'], f"{title} · {message.get('stage', 'reply')}",
                                   message['text'], start=start, end=message['created_at']))
    return rows


def _apps(state, root, agent):
    path = Path(root) / 'app-runs.sqlite3'
    if not path.exists():
        return []
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)
    try:
        found = db.execute('SELECT room_id, state, reason, text, created_at, updated_at FROM runs '
                           'WHERE provider=? ORDER BY updated_at DESC LIMIT ?', (agent, LIMIT)).fetchall()
    finally:
        db.close()
    rows = []
    for room_id, run_state, reason, text, created, updated in found:
        room = state['rooms'].get(room_id)
        title = room['room']['title'] if room else 'Removed chat'
        done = run_state not in {'queued', 'running'}
        line = text if text.strip() else f"{APP_NAMES[agent]}: {run_state}{f' ({reason})' if reason else ''}."
        rows.append(_entry(updated, f'{title} · {APP_NAMES[agent]}', line,
                           start=created if done else None, end=updated if done else None))
    return rows


def history(state, root, agent):
    """Newest first, at most 50 entries, built from saved Team stores without writing."""
    if agent not in AGENTS:
        raise ValueError('Unknown agent')
    rows = _apps(state, root, agent) if agent in APP_NAMES else _native(state, agent)
    rows.sort(key=lambda row: row['_at'], reverse=True)
    for row in rows:
        del row['_at']
    return rows[:LIMIT]
