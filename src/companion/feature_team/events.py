"""Local append-only loop events; SQLite is a disposable query index.

F0 fields: t, run_id, feature, node, worker, channel, kind, sha, path, detail.
Times are epoch seconds. A replay with no source time records the import time
but time_missing=True excludes it from time queries and age calculations.
Naive legacy times mean machine-local time (lane_send uses time.localtime).
Detail is a printable string (F0, 400 characters maximum). For queryable
measurements use a compact JSON object string, e.g. {"seconds":125} or
{"deadline":1767394800}. Legacy detail is allowlisted metadata, never message text.
EventStore(data_dir).append(event) is the producer API. Replay never modifies its
sources. Each JSONL line / completion node version is imported once, even if the
index is deleted. A malformed source/log refuses with its location, not its text.
"""

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from companion.feature_team import contract

FIELDS = ('t', 'run_id', 'feature', 'node', 'worker', 'channel', 'kind', 'sha', 'path', 'detail')
FILTERS = ('feature', 'node', 'worker', 'kind', 'run_id')
START = {'dispatched', 'started'}
RUN_KINDS = set(contract.RUN_KINDS)
LOOP_KINDS = {'tick', 'idle', 'watchdog', 'context'}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
TERMINAL = {'result', 'done', 'failed', 'blocked', 'verify_pass', 'verify_fail',
            'timeout', 'landed', 'cancelled', 'escalated'}
TEXT_METADATA = {'status', 'source', 'source_id', 'source_sha256', 'source_kind', 'deadline',
                 'started', 'finished', 'branch', 'model', 'effort', 'ledger_id', 'time_basis', 'legacy_kind'}
COUNTS = {'brief_bytes', 'brief_lines', 'file_count', 'pack_bytes', 'tokens', 'cost_usd',
          'seconds', 'wait_seconds', 'refill_seconds', 'count', 'exit_code', 'attempt'}


def timestamp(value):
    """Epoch seconds; a missing timestamp remains unknown, never 'now'."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('invalid timestamp')
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            result = datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            raise ValueError('invalid timestamp') from None
    if not math.isfinite(result):
        raise ValueError('invalid timestamp')
    return result


def time_bound(value, now=None):
    """ISO/epoch, local HH:MM, today, or elapsed 30m/1h/1d; until is exclusive."""
    if value is None:
        return None
    current = datetime.fromtimestamp(timestamp(now) if now is not None else datetime.now().timestamp()).astimezone()
    if value == 'today':
        return current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([smhd])', str(value))
    if match:
        return current.timestamp() - float(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    if re.fullmatch(r'\d{2}:\d{2}', str(value)):
        hour, minute = map(int, value.split(':'))
        return current.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    return timestamp(value)


def _detail(value):
    if not isinstance(value, dict):
        raise ValueError('detail must be a metadata object')
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError('detail keys must be strings')
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)) and math.isfinite(item):
            continue
        if isinstance(item, str) and (key in TEXT_METADATA or key.endswith('_path')):
            if '\n' not in item and '\r' not in item:
                continue
        if key == 'paths' and isinstance(item, list) and all(isinstance(p, str) and '\n' not in p for p in item):
            continue
        raise ValueError(f'detail.{key} must be a count, path or supported metadata field')


def metadata(detail):
    """Parse structured measurements; opaque F0 detail remains display-only."""
    try:
        value = json.loads(detail)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def event_time(event):
    return None if metadata(event['detail']).get('time_missing') else event['t']


def validate(event):
    """F0 event contract, kept standalone until the contract lands on this base."""
    if not isinstance(event, dict) or set(event) != set(FIELDS):
        raise ValueError('event requires exactly: ' + ', '.join(FIELDS))
    if type(event['t']) not in (int, float) or not 0 < event['t'] < 1e11:
        raise ValueError('t must be epoch seconds')
    kind = event['kind']
    if not isinstance(kind, str) or kind not in RUN_KINDS | LOOP_KINDS:
        raise ValueError('kind must be an F0 event kind')
    for key in ('run_id', 'feature', 'node'):
        value = event[key]
        if value is None and kind in LOOP_KINDS:
            continue
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise ValueError(f'{key} must be an identifier')
    for key in ('worker', 'path', 'detail'):
        value = event[key]
        if value is None and key == 'path':
            continue
        if not isinstance(value, str) or not value.strip() or not value.isprintable():
            raise ValueError(f'{key} must be a nonempty printable line')
    if len(event['detail']) > 400:
        raise ValueError('detail must be at most 400 characters')
    if event['sha'] is not None and (not isinstance(event['sha'], str)
                                   or not re.fullmatch(r'[0-9a-f]{7,40}', event['sha'])):
        raise ValueError('sha must be null or 7 to 40 lowercase hex characters')
    channel = event['channel']
    if channel is not None and (not isinstance(channel, str)
                               or not re.fullmatch(r'(?:codex|codex-exec|cursor|claude|muse|local):.+|grok(?::.*)?', channel)):
        raise ValueError('channel must be null or an F0 kind:target channel')
    if event['detail'].startswith('{'):
        try:
            detail = json.loads(event['detail'])
        except ValueError:
            raise ValueError('detail contains invalid JSON metadata') from None
        _detail(detail)
        if detail.get('deadline') is not None:
            deadline = timestamp(detail['deadline'])
            if deadline is None or not 0 < deadline < 1e11:
                raise ValueError('detail.deadline must be a timestamp')
    return event


def _json_rows(path):
    """Read complete object rows; no silent loss of malformed source records."""
    if not path.exists():
        return
    with path.open(encoding='utf-8') as source:
        for line, text in enumerate(source, 1):
            if not text.strip():
                continue
            try:
                row = json.loads(text)
                if not isinstance(row, dict):
                    raise ValueError
            except ValueError:
                raise ValueError(f'invalid JSON object at {path}:{line}') from None
            yield line, row


def _text(row, *keys):
    return next((row[key] for key in keys if isinstance(row.get(key), str) and row[key]), '')


def _legacy(row, path, position, source_kind, feature='', node=''):
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).hexdigest()
    identity = hashlib.sha256(f'{path.resolve()}:{position}:{digest}'.encode()).hexdigest()
    # Keep the pointer short enough for F0 detail; the source root is in replay's report.
    relative = ('team/features/' + path.parent.name + '/completion.json' if source_kind == 'completion'
                else 'context/packages.jsonl' if source_kind == 'ledger' else 'team/board-inbox.jsonl')
    detail = dict(source_path=relative, source_id=identity)
    detail['source_line' if isinstance(position, int) else 'source_node_path'] = position
    for key in COUNTS:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            detail[key] = value
    if row.get('deadline') is not None:
        detail['deadline'] = timestamp(row['deadline'])
    stamp = next((row[k] for k in ('t', 'sent_at', 'timestamp') if row.get(k) is not None), None)
    try:
        t = timestamp(stamp)
        if t is not None and not 0 < t < 1e11:
            raise ValueError
    except (ValueError, OverflowError, OSError):
        t = None
        detail['time_invalid'] = True
    if t is None:
        t = datetime.now().timestamp()
        detail['time_missing'] = True
    elif isinstance(stamp, str) and ('T' in stamp or '-' in stamp):
        if datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo is None:
            detail['time_basis'] = 'local'
    kind = _text(row, 'kind', 'event', 'status') or source_kind
    # Preserve legacy kinds without fabricating F0 dispatch/verification evidence.
    detail['legacy_kind'] = 'sent' if source_kind == 'ledger' else kind
    feature = feature or _text(row, 'feature')
    node = node or _text(row, 'node')
    if not feature and ':' in node:
        feature, node = node.split(':', 1)
    def identifier(value):
        return value if ID.fullmatch(value) else None
    channel = _text(row, 'channel')
    if channel in {'codex', 'cursor', 'claude', 'local'} and _text(row, 'target'):
        channel += ':' + row['target']
    if not re.fullmatch(r'(?:codex|cursor|claude|muse|local):.+|grok(?::.*)?', channel):
        channel = None
    sha = _text(row, 'sha')
    return validate(dict(t=t, run_id=identifier(_text(row, 'run_id', 'ledger_id', 'id')),
                         feature=identifier(feature), node=identifier(node),
                         worker=_text(row, 'worker', 'by', 'from', 'lane', 'target') or 'legacy',
                         channel=channel, kind='context', sha=sha if re.fullmatch(r'[0-9a-f]{7,40}', sha) else None,
                         path=_text(row, 'path', 'result_path') or str(path.resolve()),
                         detail=json.dumps(detail, separators=(',', ':'), sort_keys=True)))


def replay_rows(data):
    """One event per nonblank JSONL row and per node in each completion map."""
    data = Path(data)
    events, counts = [], {}
    for relative, kind in [('team/board-inbox.jsonl', 'inbox'), ('context/packages.jsonl', 'ledger')]:
        path = data / relative
        if not path.is_file():
            raise FileNotFoundError(f'replay source missing: {path}')
        counts[str(path)] = 0
        for line, row in _json_rows(path):
            events.append(_legacy(row, path, line, kind))
            counts[str(path)] += 1
    for path in sorted((data / 'team/features').glob('*/completion.json')):
        try:
            document = json.loads(path.read_text())
            nodes = document['nodes']
            if not isinstance(nodes, dict) or any(not isinstance(row, dict) for row in nodes.values()):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ValueError(f'invalid completion map at {path}') from None
        counts[str(path)] = len(nodes)
        for node, row in nodes.items():
            events.append(_legacy(row, path, node, 'completion', feature=path.parent.name, node=node))
    return events, counts


class EventStore:
    def __init__(self, data_dir=None):
        # Avoid companion.paths/settings: this CLI must never load .env.
        default = Path(__file__).resolve().parents[3] / 'data'
        self.data_dir = Path(data_dir or os.environ.get('KYRA_DATA_DIR', default))
        self.directory = self.data_dir / 'team/loop'
        self.path = self.directory / 'events.jsonl'
        self.index_path = self.directory / 'events.sqlite3'

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / 'events.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        events = []
        for line, row in _json_rows(self.path):
            try:
                validate(row)
            except ValueError:
                raise ValueError(f'invalid event at {self.path}:{line}') from None
            events.append(row)
        return events

    def _append(self, rows):
        if not rows:
            return
        # Refuse a torn tail; never overwrite append-only history to repair it.
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open('rb') as source:
                source.seek(-1, os.SEEK_END)
                if source.read(1) != b'\n':
                    raise ValueError(f'incomplete final line at {self.path}; repair separately')
        with self.path.open('a', encoding='utf-8') as output:
            for row in rows:
                output.write(json.dumps(row, sort_keys=True, allow_nan=False) + '\n')
            output.flush()
            os.fsync(output.fileno())

    def append(self, event):
        """Durably append a validated F0-shaped event, serialized across writers."""
        validate(event)
        with self._locked():
            self._read()
            self._append([event])
        return event

    def _rebuild(self):
        rows = self._read()
        fd, name = tempfile.mkstemp(prefix='.events-', suffix='.sqlite3', dir=self.directory)
        os.close(fd)
        try:
            with closing(sqlite3.connect(name)) as db, db:
                db.execute('CREATE TABLE events (seq INTEGER PRIMARY KEY, t REAL, run_id TEXT, feature TEXT, '
                           'node TEXT, worker TEXT, kind TEXT, legacy_kind TEXT, row TEXT)')
                db.executemany('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
                    (i, event_time(row), *(row[k] for k in ('run_id', 'feature', 'node', 'worker', 'kind')),
                     metadata(row['detail']).get('legacy_kind'), json.dumps(row)) for i, row in enumerate(rows)])
                for column in ('t', 'legacy_kind', *FILTERS):
                    db.execute(f'CREATE INDEX by_{column} ON events ({column})')
                db.execute('CREATE TABLE metadata (signature TEXT)')
                db.execute('INSERT INTO metadata VALUES (?)', (self._signature(),))
            os.replace(name, self.index_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return len(rows)

    def _signature(self):
        if not self.path.exists():
            return 'missing'
        stat = self.path.stat()
        return f'f0-v1:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'

    def _sync(self):
        try:
            with closing(sqlite3.connect(self.index_path)) as db:
                signature = db.execute('SELECT signature FROM metadata').fetchone()
                if signature and signature[0] == self._signature():
                    return
        except sqlite3.DatabaseError:
            pass
        self._rebuild()

    def rebuild(self):
        with self._locked():
            return self._rebuild()

    def replay(self, source_data_dir=None):
        incoming, counts = replay_rows(source_data_dir or self.data_dir)
        with self._locked():
            existing = self._read()
            seen = {metadata(row['detail']).get('source_id') for row in existing}
            pending = [row for row in incoming if metadata(row['detail'])['source_id'] not in seen]
            self._append(pending)
            total = self._rebuild()
            seen.update(metadata(row['detail'])['source_id'] for row in pending)
            return dict(source_rows=len(incoming), appended=len(pending), total=total,
                        matched=sum(metadata(row['detail'])['source_id'] in seen for row in incoming), sources=counts)

    def query(self, *, since=None, until=None, now=None, **filters):
        unknown = filters.keys() - set(FILTERS)
        if unknown:
            raise ValueError('unsupported query filter')
        conditions, values = [], []
        for key, value in filters.items():
            if value is not None:
                if key == 'kind':
                    conditions.append('(kind = ? OR legacy_kind = ?)')
                    values.extend([value, value])
                else:
                    conditions.append(f'{key} = ?')
                    values.append(value)
        for bound, op in [(since, '>='), (until, '<')]:
            if bound is not None:
                conditions.append(f't {op} ?')
                values.append(time_bound(bound, now))
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self._locked():
            self._sync()
            with closing(sqlite3.connect(self.index_path)) as db:
                return [json.loads(row[0]) for row in db.execute(
                    'SELECT row FROM events' + where + ' ORDER BY t IS NULL, t, seq', values)]

    def status(self, *, now=None, stuck=False, **filters):
        """Latest run per node. Terminal runs cannot be revived by late heartbeats.

        Start age survives heartbeats. Missing deadlines/ages stay null; only an
        explicit deadline proves a run overdue. Stuck includes every overdue active
        run, even if a newer attempt has started. Events without node IDs are not
        invented into running nodes. Queued/sent alone never establish a start.
        """
        current = timestamp(now) if now is not None else datetime.now().timestamp()
        runs = {}
        for event in self.query():
            if not event['node']:
                continue
            key = event['feature'], event['node'], event['run_id']
            run = runs.setdefault(key, dict(event=event, start=None, deadline=None, terminal=False))
            detail = metadata(event['detail'])
            kind = detail.get('legacy_kind', event['kind'])
            if kind in START and run['start'] is None:
                run['start'] = event
            if kind in TERMINAL:
                run['terminal'] = True
            if detail.get('deadline'):
                run['deadline'] = timestamp(detail['deadline'])
            run['event'] = event
        latest = {}
        for (feature, node, _), run in runs.items():
            if run['start'] is None:
                continue
            anchor = run['start']
            rank = event_time(anchor)
            rank = float('-inf') if rank is None else rank
            if (feature, node) not in latest or rank >= latest[(feature, node)][0]:
                latest[(feature, node)] = rank, run
        output = []
        candidates = runs.values() if stuck else (run for _, run in latest.values())
        for run in candidates:
            if run['start'] is None or run['terminal']:
                continue
            event = run['event']
            identity = {k: run['start'][k] for k in ('feature', 'node', 'run_id', 'worker', 'channel')}
            if any(v is not None and identity.get(k) != v for k, v in filters.items()):
                continue
            started = event_time(run['start'])
            deadline = timestamp(run['deadline'])
            overdue = max(0, current - deadline) if deadline is not None else None
            if stuck and (overdue is None or overdue <= 0):
                continue
            output.append(dict(**identity, kind=event['kind'], started=started,
                               age_seconds=max(0, current - started) if started is not None else None,
                               deadline=run['deadline'], overdue_seconds=overdue, path=event['path']))
        return sorted(output, key=lambda row: (row['feature'] or '', row['node']))


def append_event(event, data_dir=None):
    """Convenience producer entry point; see EventStore.append."""
    return EventStore(data_dir).append(event)
