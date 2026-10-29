"""Read activity without sending model turns or mistaking a receipt for acceptance.

Native log formats are best-effort observations. Unsupported apps, stale active
records and missing sessions remain unknown. This grants no execution permission.
"""
import json
import time
from pathlib import Path

from companion import app_tasks


def _discover():
    return (app_tasks.discover_codex(Path.home() / '.codex/sessions')
            + app_tasks.discover_claude(Path.home() / '.claude/sessions'))


def _codex_activity(path):
    # Bound reads; no prompt/answer content is returned or logged.
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 1_048_576))
        if size > 1_048_576:
            stream.readline()
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    state, pending = 'unknown', None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get('payload')
        if not isinstance(payload, dict):
            continue
        kind = payload.get('type')
        if record.get('type') == 'event_msg':
            if kind == 'task_started':
                state, pending = 'working', None
            elif kind == 'task_complete':
                state, pending = 'completed', None
            elif kind in {'turn_aborted', 'task_failed'}:
                state, pending = 'blocked', None
        elif record.get('type') == 'response_item':
            if kind == 'function_call' and payload.get('name', '').endswith('request_user_input'):
                state, pending = 'needs_input', payload.get('call_id')
            elif kind == 'function_call_output' and pending and payload.get('call_id') == pending:
                state, pending = 'working', None
            elif state == 'unknown' and (kind in {'reasoning', 'function_call', 'function_call_output',
                                                                'custom_tool_call', 'custom_tool_call_output'}
                                          or (kind == 'message' and payload.get('role') == 'assistant')):
                # Long active turns can outgrow the bounded tail and lose task_started.
                # Fresh native work can establish activity, but never undo a terminal event.
                state = 'working'
    return state


def observe_activity(chats, repo, *, now=None, discover=_discover, stale_after=600):
    """Observe exact app/id/workspace pins; never choose by title or recency."""
    now = time.time() if now is None else now
    tasks = {(t.app, t.id, str(Path(t.cwd).resolve())): t for t in discover()}
    rows = []
    for chat in chats:
        task = tasks.get((chat['app'], chat['id'], str(Path(chat.get('cwd', repo)).resolve())))
        row = {'app': chat['app'], 'id': chat['id'], 'state': 'unknown',
               'observed_at': now, 'reason': 'No supported matching live session'}
        if task and task.source:
            try:
                if task.app == 'codex':
                    state = _codex_activity(task.source)
                    updated = task.updated_at
                elif task.app == 'claude':
                    meta = json.loads(Path(task.source).read_text())
                    state = {'idle': 'idle', 'busy': 'working', 'working': 'working',
                             'thinking': 'thinking', 'waiting_for_permission': 'needs_input',
                             'needs_input': 'needs_input', 'needs_setup': 'needs_setup',
                             'blocked': 'blocked'}.get(meta.get('status'), 'unknown')
                    updated = float(meta.get('statusUpdatedAt', task.updated_at))
                    if updated > 100_000_000_000:
                        updated /= 1000
                else:
                    state, updated = 'unknown', 0
                if state in {'working', 'thinking'} and not 0 <= now - updated <= stale_after:
                    state = 'unknown'
                row.update(state=state, reason='Native activity observation; not completion evidence')
            except (OSError, ValueError, TypeError, AttributeError):
                row['reason'] = 'Native activity source unavailable or invalid'
        rows.append(row)
    return rows
