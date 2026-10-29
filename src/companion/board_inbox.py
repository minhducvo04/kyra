"""One validated, file-locked append boundary for the Board inbox.

New rows require t (ISO datetime or finite epoch), from, kind, and text or
summary. Historical metadata remains optional, including JSON extensions.
Readers may continue consuming old rows without applying the new-write schema.
"""
try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import math
import os
from datetime import datetime
from pathlib import Path

from companion import paths

# Optional identifiers can be null in historical routing receipts.
STRING_FIELDS = frozenset({
    'node', 'sha', 'branch', 'path', 'lane', 'run_id', 'feature', 'to', 'routing_id',
    'reason', 'needs', 'evidence', 'tried', 'step', 'why', 'result_path', 'model',
    'action', 'verdict', 'error', 'rule', 'status', 'metric', 'created_at', 'team_delivery',
})


def validate(row):
    """Return a copy; refuse invalid fields before opening or creating any file."""
    if not isinstance(row, dict):
        raise ValueError('row must be an object')
    row = dict(row)
    t = row.get('t')
    if isinstance(t, str):
        try:
            datetime.fromisoformat(t.replace('Z', '+00:00'))
            if 'T' not in t and ' ' not in t:
                raise ValueError
        except ValueError:
            raise ValueError('t must be an ISO datetime or finite epoch') from None
    elif type(t) not in (int, float) or not math.isfinite(t):
        raise ValueError('t must be an ISO datetime or finite epoch')
    for field in ('from', 'kind'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    for field in ('text', 'summary'):
        if field in row and (not isinstance(row[field], str) or not row[field].strip()):
            raise ValueError(f'{field} must be a nonempty string')
    if not any(field in row for field in ('text', 'summary')):
        raise ValueError('text or summary is required')
    for field, value in row.items():
        if not isinstance(field, str):
            raise ValueError('row field names must be strings')
        if field in STRING_FIELDS and value is not None and not isinstance(value, str):
            raise ValueError(f'{field} must be a string or null')
        try:
            json.dumps(value, allow_nan=False, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            raise ValueError(f'{field} must be finite UTF-8 JSON') from None
    return row


def append(row, *, path=None, once=False):
    """Append one complete row; optional routing_id dedupe shares the file lock.

    Serialization precedes I/O. A short write or ordinary I/O exception rolls
    back to the locked original length. This is not a power-loss transaction.
    """
    row = validate(row)
    if once and (not isinstance(row.get('routing_id'), str) or not row['routing_id']):
        raise ValueError('routing_id is required for once')
    payload = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    path = Path(path) if path is not None else paths.DATA_DIR / 'team/board-inbox.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b', buffering=0) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if once:
            stream.seek(0)
            for line in stream:
                try:
                    old = json.loads(line)
                except ValueError:
                    continue
                if isinstance(old, dict) and old.get('routing_id') == row['routing_id']:
                    return row
        end = stream.seek(0, os.SEEK_END)
        if end:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b'\n':
                raise ValueError('inbox ends with an incomplete line')
        try:
            if os.write(stream.fileno(), payload) != len(payload):
                raise OSError('short Board inbox write')
            os.fsync(stream.fileno())
        except BaseException:
            stream.truncate(end)
            raise
    return row
