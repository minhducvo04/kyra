"""QC incident ownership. One open episode produces one Board suggestion.

``publish(hit)`` persists the candidate, then appends through
``board_inbox.append(..., once=True)``. The incident id is a hash of pattern,
stable subject (task, train or job) and the store's episode number. Group,
scope, tick time, line number, model summary and retry number are not part of
that hash. A repeated tick updates the same record. Closure or verified
recovery ends the episode; a later hit with new evidence opens the next one.

Board acknowledgment is a row in ``data/team/qc/decisions.jsonl`` that names
the incident and its owner. An inbox row is not acknowledgment. Twenty minutes
without acknowledgment marks the incident overdue in ``status.json``. Sixty
minutes routes that same id on the Board escalation channel (inbox kind
``escalated``) and does not write another suggestion.

The cursor advances only after every candidate has been acknowledged locally.
A crash after the inbox append leaves the candidate unacked, so the next
publish replays the same routing id and the inbox dedupe keeps one row.
Overlapping scopes share this store and its lock.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from companion import board_inbox
from companion.file_lock import exclusive

OVERDUE_SECONDS = 20 * 60
ESCALATE_SECONDS = 60 * 60
_UNSTABLE_SUBJECT_KEYS = frozenset({
    'group', 'group_name', 'scope', 'tick', 'tick_time', 'time', 'now',
    'line', 'line_number', 'model', 'model_summary', 'summary',
    'retry', 'retry_number', 'retries', 'episode',
})


class QCIncidentError(ValueError):
    """The incident store refused a write, or a saved store cannot be trusted."""


def publish(hit, config=None, now=None, scope=None):
    """Persist one candidate and return its incident id. One suggestion per open episode."""
    now = _clock(now)
    pattern, subject, refs, fix, owner, extra = _parse_hit(hit)
    scope_name = _scope(scope if scope is not None else hit.get('scope') or hit.get('group'))
    root = _root(config)
    inbox = _inbox(config, root)
    with _locked(root) as store_path:
        store = _load(store_path)
        chain_key = pattern + '\n' + subject
        chain = store['chains'].setdefault(chain_key, [])
        current = store['incidents'][chain[-1]] if chain else None
        if current and current['state'] == 'open':
            if current.get('inbox_acked'):
                current['count'] += 1
            _merge(current, refs, scope_name, extra, now)
            if not current.get('inbox_acked'):
                _save(store_path, store)
                board_inbox.append(_suggestion_row(current, now), path=inbox, once=True)
                current['inbox_acked'] = True
            _save(store_path, store)
            return current['incident_id']
        if current and not _new_evidence(current['evidence_refs'], refs):
            return current['incident_id']
        episode = (current['episode'] + 1) if current else 1
        incident_id = _incident_id(pattern, subject, episode)
        prior = store['incidents'].get(incident_id)
        if prior and (prior['pattern'], prior['subject'], prior['episode']) != (pattern, subject, episode):
            raise QCIncidentError('incident id collision')
        record = {
            'incident_id': incident_id,
            'pattern': pattern,
            'subject': subject,
            'episode': episode,
            'state': 'open',
            'owner': owner,
            'suggested_fix': fix,
            'evidence_refs': refs,
            'action_kind': extra.get('action_kind'),
            'preconditions': extra.get('preconditions'),
            'detector_episode': extra.get('detector_episode'),
            'scopes': [scope_name] if scope_name else [],
            'count': 1,
            'opened_at': now,
            'updated_at': now,
            'inbox_acked': False,
            'acknowledged_at': None,
            'overdue_at': None,
            'escalated_at': None,
            'closed_at': None,
            'closure': None,
            'recovery': None,
        }
        store['incidents'][incident_id] = record
        if incident_id not in chain:
            chain.append(incident_id)
        _save(store_path, store)
        board_inbox.append(_suggestion_row(record, now), path=inbox, once=True)
        record['inbox_acked'] = True
        _save(store_path, store)
        return incident_id


def record_decision(incident_id, owner, decision, config=None, now=None, note=None):
    """Record a Board decision. Acknowledgment and closure both name the incident and its owner."""
    now = _clock(now)
    if decision not in {'acknowledge', 'close'}:
        raise QCIncidentError('decision must be acknowledge or close')
    if not isinstance(owner, str) or not owner.strip():
        raise QCIncidentError('decision owner must name this incident owner')
    owner = owner.strip()
    if not isinstance(incident_id, str) or not incident_id:
        raise QCIncidentError('unknown incident')
    root = _root(config)
    with _locked(root) as store_path:
        store = _load(store_path)
        record = store['incidents'].get(incident_id)
        if record is None:
            raise QCIncidentError('unknown incident')
        if owner != record['owner']:
            raise QCIncidentError('decision owner must name this incident owner')
        row = {'t': now, 'incident_id': incident_id, 'owner': owner, 'decision': decision}
        if note is not None:
            if not isinstance(note, str) or not note.strip():
                raise QCIncidentError('decision note must be a nonempty string')
            row['note'] = note.strip()
        _append_jsonl(root / 'team' / 'qc' / 'decisions.jsonl', row)
        if record.get('acknowledged_at') is None:
            record['acknowledged_at'] = now
        if decision == 'close' and record['state'] == 'open':
            record['state'] = 'closed'
            record['closed_at'] = now
            record['closure'] = 'board'
            record['updated_at'] = now
        _save(store_path, store)
        return row


def record_recovery(incident_id, verification, config=None, now=None):
    """Close the open episode after verified recovery. The next new evidence starts another episode."""
    now = _clock(now)
    refs = _refs(verification.get('evidence_refs') if isinstance(verification, dict) and 'evidence_refs' in verification
                 else verification)
    root = _root(config)
    with _locked(root) as store_path:
        store = _load(store_path)
        record = store['incidents'].get(incident_id)
        if record is None:
            raise QCIncidentError('unknown incident')
        if record['state'] != 'open':
            raise QCIncidentError('episode is already closed')
        record['state'] = 'closed'
        record['closed_at'] = now
        record['closure'] = 'recovered'
        record['recovery'] = refs
        record['updated_at'] = now
        _save(store_path, store)
        return {'incident_id': incident_id, 'closure': 'recovered', 'recovery': refs, 't': now}


def status_view(config=None, now=None):
    """Apply acknowledgment deadlines and return the status view, also written to status.json."""
    now = _clock(now)
    root = _root(config)
    inbox = _inbox(config, root)
    with _locked(root) as store_path:
        store = _load(store_path)
        for record in store['incidents'].values():
            if not _waiting(record):
                continue
            age = now - record['opened_at']
            if age >= OVERDUE_SECONDS and record.get('overdue_at') is None:
                record['overdue_at'] = now
            if age >= ESCALATE_SECONDS and record.get('escalated_at') is None:
                board_inbox.append(_escalation_row(record, now), path=inbox, once=True)
                record['escalated_at'] = now
        _save(store_path, store)
        view = _view(store, now)
        _atomic_write(root / 'team' / 'qc' / 'status.json',
                      json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
        return view


def advance_cursor(cursor, config=None, now=None):
    """Record a collector cursor only after every candidate inbox append is locally acknowledged."""
    now = _clock(now)
    try:
        json.dumps(cursor, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise QCIncidentError('cursor must be finite JSON') from exc
    root = _root(config)
    with _locked(root) as store_path:
        store = _load(store_path)
        pending = [incident_id for incident_id, record in store['incidents'].items()
                   if not record.get('inbox_acked')]
        if pending:
            raise QCIncidentError('cursor advances only after durable processing')
        store['cursor'] = {'cursor': cursor, 't': now}
        _save(store_path, store)
        return dict(store['cursor'])


def read_incident(incident_id, config=None):
    """Return one saved incident, or None when the id is absent."""
    root = _root(config)
    with _locked(root) as store_path:
        store = _load(store_path)
        record = store['incidents'].get(incident_id)
        return json.loads(json.dumps(record)) if record else None


def _parse_hit(hit):
    if not isinstance(hit, dict):
        raise QCIncidentError('hit must be an object')
    pattern = hit.get('pattern')
    if not isinstance(pattern, str) or not pattern.strip():
        raise QCIncidentError('pattern must be a nonempty string')
    pattern = pattern.strip()
    subject = _subject(hit.get('subject'))
    refs = _refs(hit.get('evidence_refs'))
    fix = hit.get('suggested_fix')
    owner = hit.get('owner')
    if not isinstance(fix, str) or not fix.strip():
        raise QCIncidentError('suggested_fix must be a nonempty string')
    if not isinstance(owner, str) or not owner.strip():
        raise QCIncidentError('owner must be a nonempty string')
    extra = {}
    action = hit.get('action_kind')
    if action is not None:
        if not isinstance(action, str) or not action.strip():
            raise QCIncidentError('action_kind must be a nonempty string')
        extra['action_kind'] = action.strip()
    preconditions = hit.get('preconditions')
    if preconditions is not None:
        if not isinstance(preconditions, list) or not all(isinstance(item, str) and item.strip() for item in preconditions):
            raise QCIncidentError('preconditions must be a list of nonempty strings')
        extra['preconditions'] = [item.strip() for item in preconditions]
    detector_episode = hit.get('episode')
    if isinstance(detector_episode, str) and detector_episode.strip():
        extra['detector_episode'] = detector_episode.strip()
    elif type(detector_episode) is int:
        extra['detector_episode'] = detector_episode
    return pattern, subject, refs, fix.strip(), owner.strip(), extra


def _subject(subject):
    if isinstance(subject, str):
        text = subject.strip()
        if not text:
            raise QCIncidentError('subject must be a stable task, train or job')
        return text
    if not isinstance(subject, dict):
        raise QCIncidentError('subject must be a string or object')
    kept = {}
    for key in sorted(subject):
        if key in _UNSTABLE_SUBJECT_KEYS:
            continue
        value = subject[key]
        if value is None or value == '':
            continue
        if isinstance(value, bool) or type(value) not in (str, int, float):
            raise QCIncidentError('subject values must be plain strings or numbers')
        if type(value) is float and not _finite(value):
            raise QCIncidentError('subject values must be finite')
        kept[key] = value
    if not kept:
        raise QCIncidentError('subject must be a stable task, train or job')
    return json.dumps(kept, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _refs(raw):
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise QCIncidentError('evidence_refs must name a path and a line')
    refs = []
    for item in raw:
        if not isinstance(item, dict):
            raise QCIncidentError('evidence_refs must name a path and a line')
        path = item.get('path')
        line = item.get('line')
        if not isinstance(path, str) or not path.strip():
            raise QCIncidentError('evidence path is required')
        if type(line) is not int or line < 1:
            raise QCIncidentError('evidence line must be a one-based integer')
        ref = {'path': path.strip(), 'line': line}
        field = item.get('field')
        if field is not None:
            if not isinstance(field, str) or not field.strip():
                raise QCIncidentError('evidence field must be a nonempty string')
            ref['field'] = field.strip()
        refs.append(ref)
    return sorted(refs, key=lambda ref: (ref['path'], ref['line'], ref.get('field') or ''))


def _scope(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise QCIncidentError('scope must be a nonempty string')
    return value.strip()


def _incident_id(pattern, subject, episode):
    payload = json.dumps(
        {'episode': episode, 'pattern': pattern, 'subject': subject},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _new_evidence(saved, incoming):
    have = {(ref['path'], ref['line'], ref.get('field')) for ref in saved}
    return any((ref['path'], ref['line'], ref.get('field')) not in have for ref in incoming)


def _merge(record, refs, scope_name, extra, now):
    record['evidence_refs'] = _union(record['evidence_refs'], refs)
    if scope_name and scope_name not in record['scopes']:
        record['scopes'].append(scope_name)
    if extra.get('detector_episode') is not None:
        record['detector_episode'] = extra['detector_episode']
    record['updated_at'] = now


def _union(saved, incoming):
    merged = {(ref['path'], ref['line'], ref.get('field')): ref for ref in saved}
    for ref in incoming:
        merged[(ref['path'], ref['line'], ref.get('field'))] = ref
    return sorted(merged.values(), key=lambda ref: (ref['path'], ref['line'], ref.get('field') or ''))


def _suggestion_text(record):
    return f"{record['pattern']} for {record['subject']}: {record['suggested_fix']} Owner: {record['owner']}."


def _evidence_text(refs):
    parts = []
    for ref in refs:
        part = f"{ref['path']}:{ref['line']}"
        if ref.get('field'):
            part += f" {ref['field']}"
        parts.append(part)
    return '; '.join(parts)


def _suggestion_row(record, now):
    """Inbox fields required for a QC suggestion. ``evidence`` stays a string."""
    return {
        't': now,
        'from': 'qc-desk',
        'kind': 'qc_suggestion',
        'to': 'board',
        'text': _suggestion_text(record),
        'routing_id': record['incident_id'],
        'pattern': record['pattern'],
        'owner': record['owner'],
        'suggested_fix': record['suggested_fix'],
        'evidence_refs': record['evidence_refs'],
        'evidence': _evidence_text(record['evidence_refs']),
    }


def _escalation_row(record, now):
    """The existing Board channel is an inbox row of kind ``escalated``, not a second suggestion."""
    return {
        't': now,
        'from': 'qc-desk',
        'kind': 'escalated',
        'to': 'board',
        'text': (
            f"QC incident {record['incident_id']} has had no Board acknowledgment for 60 minutes. "
            f"Owner: {record['owner']}."
        ),
        'routing_id': f"qc-escalated:{record['incident_id']}",
        'pattern': record['pattern'],
        'owner': record['owner'],
        'incident_id': record['incident_id'],
        'evidence': _evidence_text(record['evidence_refs']),
    }


def _waiting(record):
    return (record['state'] == 'open' and record.get('inbox_acked')
            and record.get('acknowledged_at') is None)


def _view(store, now):
    incidents = []
    overdue = []
    escalated = []
    open_ids = []
    for incident_id in sorted(store['incidents']):
        record = store['incidents'][incident_id]
        waiting = _waiting(record)
        is_overdue = waiting and now - record['opened_at'] >= OVERDUE_SECONDS
        if record['state'] == 'open':
            open_ids.append(incident_id)
        if is_overdue:
            overdue.append(incident_id)
        if record.get('escalated_at') is not None:
            escalated.append(incident_id)
        incidents.append({
            'incident_id': incident_id,
            'pattern': record['pattern'],
            'subject': record['subject'],
            'episode': record['episode'],
            'state': record['state'],
            'owner': record['owner'],
            'count': record['count'],
            'scopes': list(record['scopes']),
            'acknowledged': record.get('acknowledged_at') is not None,
            'overdue': is_overdue,
            'escalated': record.get('escalated_at') is not None,
            'closure': record.get('closure'),
            'evidence_refs': record['evidence_refs'],
            'suggested_fix': record['suggested_fix'],
        })
    return {
        't': now,
        'open': open_ids,
        'overdue': overdue,
        'escalated': escalated,
        'incidents': incidents,
        'cursor': store.get('cursor'),
    }


def _root(config):
    if config and config.get('data_root'):
        return Path(config['data_root'])
    env = os.environ.get('KYRA_DATA_DIR')
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / 'data'


def _inbox(config, root):
    if config and config.get('inbox_path'):
        return Path(config['inbox_path'])
    return root / 'team' / 'board-inbox.jsonl'


def _clock(now):
    if now is None:
        now = time.time()
    if isinstance(now, bool) or type(now) not in (int, float) or not _finite(now):
        raise QCIncidentError('now must be a finite number of seconds')
    return float(now)


def _finite(value):
    return value == value and value not in (float('inf'), float('-inf'))


def _load(path):
    if not path.exists():
        return {'incidents': {}, 'chains': {}, 'cursor': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        raise QCIncidentError(f'incident store is unreadable: {path}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('incidents'), dict) or not isinstance(data.get('chains'), dict):
        raise QCIncidentError('incident store is malformed')
    data.setdefault('cursor', None)
    return data


def _save(path, store):
    _atomic_write(path, json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n')


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _append_jsonl(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    with path.open('a+b', buffering=0) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.write(payload) != len(payload):
            raise QCIncidentError('short decision write')
        os.fsync(handle.fileno())


@contextmanager
def _locked(root):
    qc = Path(root) / 'team' / 'qc'
    qc.mkdir(parents=True, exist_ok=True)
    handle = (qc / 'incidents.lock').open('a+')
    try:
        with exclusive(handle):
            yield qc / 'incidents.json'
    finally:
        handle.close()
