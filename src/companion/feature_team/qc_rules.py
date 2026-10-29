"""QC desk rules. ``detect`` names repeated failures. It does not repair them.

Other lanes own collection, incident publishing and the three repairs. This module
only reads the frozen event dictionaries and returns suggestion hits.

Event fields used here: ``source``, ``file_id``, ``line``, ``offset``, ``record_hash``,
``time``, ``feature``, ``node``, ``run_id``, ``kind``, ``facts``. ``time`` may be an
epoch number, an aware or naive ISO timestamp, or null. A bare ``HH:MM:SS`` clock is
not a timestamp. For a time-only landing line the collector passes ``facts["first_seen"]``
as the persisted absolute time.

``facts`` keys by kind (collectors may set the boolean and omit the prose):

- Grok refusal: ``model``/``worker`` contains grok, ``refusal`` ``command`` or
  ``permission_cancelled``, ``package``, ``attempt``, ``ledger_id``.
- Folder: ``job`` or ``job_id``, ``ledger_id``, ``registry_cwd``, ``claimed_worktree``,
  ``gave_up`` or text "gave up after four restarts". ``api_unsupported`` blocks observe-only.
- Landing refusal: ``entry``/``task``/``train``, ``transient``, ``mixed``, ``text``,
  ``retry_scheduled``, ``retry_after``, ``successful_retry``, ``retry_cap_reached``,
  ``hold``, ``active_train``.
- Pin: ``branch_head`` and ``reviewed_pin``, or ``pin_mismatch`` / ``stale_ancestry``.
- Review: ``rejected``, ``attempt``, optional ``issue``. Duplicate receipts share ``attempt``.
- Main checkout: ``not_fast_forwarded`` / ``fast_forwarded``, or the runner's log phrases.
- Recovery: ``retry_cap_reached``, ``worker_escalated`` with ``escalation_count``,
  or inspector give-up with no ``coordinator_linked`` action.
- Handoff: ``classification`` ``UNCONFIRMED``, ``start_proof`` / ``exit_proof``.
- Source: ``source``, ``problem`` in missing/unreadable/malformed/unmapped, ``teams_unobserved``.
- Unmatched failure: ``failed``, ``error_category``, ``task``, ``attempt``.

Subjects are stable ids (``package:``, ``job:``, ``train:``, ``node:``, ``checkout:main``,
``worker:``, ``dispatch:``, ``source:``, ``unmatched:``). A line number, tick time, group
name or retry number is not part of the subject. Episode stays on the open incident.
After closure, only a new record hash (or a later time when the closed incident recorded
``closed_at`` and no hashes) opens the next episode.

Hits use ``action_kind`` ``add_grok_single_command_clause``, ``reregister_observe_only``,
``requeue_transient`` or ``waiting_board``. Suggested fixes are instructions for the
owner, never a shell command.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime

TICK_S = 600
TWO_TICKS_S = 1200
HOUR_S = 3600
REVIEW_MIN = 3
REVIEW_URGENT = 6
RETRY_CAP = 6

GROK_COMMAND_REFUSAL = 'GROK_COMMAND_REFUSAL'
WRONG_FOLDER_RESTART = 'WRONG_FOLDER_RESTART'
LANDING_TRANSIENT_STALL = 'LANDING_TRANSIENT_STALL'
LANDING_STALE_PIN = 'LANDING_STALE_PIN'
REVIEW_CHURN = 'REVIEW_CHURN'
MAIN_CHECKOUT_STALE = 'MAIN_CHECKOUT_STALE'
RECOVERY_EXHAUSTED = 'RECOVERY_EXHAUSTED'
START_UNCONFIRMED = 'START_UNCONFIRMED'
QC_SOURCE_UNAVAILABLE = 'QC_SOURCE_UNAVAILABLE'
UNMATCHED_FAILURE = 'UNMATCHED_FAILURE'

ADD_GROK_CLAUSE = 'add_grok_single_command_clause'
REREGISTER_OBSERVE_ONLY = 'reregister_observe_only'
REQUEUE_TRANSIENT = 'requeue_transient'
WAITING_BOARD = 'waiting_board'

PATTERN_ORDER = {
    GROK_COMMAND_REFUSAL: 0,
    WRONG_FOLDER_RESTART: 1,
    LANDING_TRANSIENT_STALL: 2,
    LANDING_STALE_PIN: 3,
    REVIEW_CHURN: 4,
    MAIN_CHECKOUT_STALE: 5,
    RECOVERY_EXHAUSTED: 6,
    START_UNCONFIRMED: 7,
    QC_SOURCE_UNAVAILABLE: 8,
    UNMATCHED_FAILURE: 9,
}

# Same busy-Mac signals as scripts/landing_runner.py. A mixed guard failure is not transient
# unless the only failing test is the load guard.
TRANSIENT_RE = re.compile(
    r'headroom refused|no headroom|runner held|another heavy job|1-minute load (is )?[\d.]+'
    r'|origin/master moved while the suite ran', re.I)
GUARDS_FAILED_RE = re.compile(r'merged-tree guards failed', re.I)
FAILED_TEST_RE = re.compile(r'^(?:FAILED|ERROR) (\S+)', re.M)
LOAD_GUARD_TEST = 'tests/test_correct_repeaters.py::test_load_allows_the_browser_phase'
GAVE_UP_RE = re.compile(r'gave up after (?:four|4) restarts', re.I)
PIN_TEXT_RE = re.compile(r'reviewed[- ]pin mismatch|branch-head mismatch|stale train ancestry', re.I)
NOT_FF = 'main checkout NOT fast-forwarded'
YES_FF = 'main checkout fast-forwarded'
CLOSED = {'closed', 'resolved', 'verified'}
SOURCE_PROBLEMS = {'missing', 'unreadable', 'malformed', 'unmapped', 'unavailable'}
BOARD_PRECONDITIONS = ['board decides; no automatic mutation']
RECOVERY_SENTENCE = 'Never reset a retry budget to make a job look new.'


def detect(events, incidents, now):
    """Return suggestion hits for qualifying evidence. Does not write or call a model."""
    rows = [(index, event) for index, event in enumerate(events or []) if isinstance(event, dict)]
    now_ts = _coerce_time(now)
    classified = {index for index, event in rows if _known_domain(event)}
    hits = []
    hits += _detect_grok(rows, incidents)
    hits += _detect_folder(rows, incidents)
    hits += _detect_stall(rows, incidents, now_ts)
    hits += _detect_pin(rows, incidents)
    hits += _detect_review(rows, incidents)
    hits += _detect_main(rows, incidents)
    hits += _detect_unconfirmed(rows, incidents, now_ts)
    hits += _detect_source(rows, incidents, now_ts)
    hits += _detect_recovery(rows, incidents, hits, now_ts)
    hits += _detect_unmatched(rows, incidents, classified)
    hits.sort(key=lambda hit: (PATTERN_ORDER.get(hit['pattern'], 99), hit['subject']))
    return hits


def _facts(event):
    facts = event.get('facts')
    return facts if isinstance(facts, dict) else {}


def _text(event):
    facts = _facts(event)
    parts = []
    for key in ('text', 'message', 'detail', 'output', 'reason'):
        value = facts.get(key)
        if isinstance(value, str):
            parts.append(value)
    return '\n'.join(parts)


def _first(facts, keys):
    for key in keys:
        value = facts.get(key)
        if value not in (None, ''):
            return str(value)
    return None


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
        return int(value.strip())
    return None


def _coerce_time(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        try:
            return float(value.timestamp())
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text or re.fullmatch(r'\d{2}:\d{2}:\d{2}', text):
            return None
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None and math.isfinite(number):
            return float(number)
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        try:
            return float(parsed.timestamp())
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _event_time(event):
    facts = _facts(event)
    for candidate in (event.get('time'), facts.get('first_seen'), facts.get('dispatched_at'), facts.get('since')):
        stamp = _coerce_time(candidate)
        if stamp is not None:
            return stamp
    return None


def _line(event):
    line = event.get('line')
    number = _as_int(line)
    if number is not None and number >= 1:
        return number
    return None


def _path(event):
    facts = _facts(event)
    path = facts.get('path') or event.get('file_id')
    if isinstance(path, str) and path.strip():
        return path.strip()
    return None


def _norm_path(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    normed = os.path.normpath(text)
    if normed != os.sep:
        normed = normed.rstrip(os.sep)
    return normed


def _iter_incidents(incidents):
    if incidents is None:
        return []
    if isinstance(incidents, dict):
        nested = incidents.get('incidents')
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if 'pattern' in incidents and 'subject' in incidents:
            return [incidents]
        return [item for item in incidents.values() if isinstance(item, dict)]
    if isinstance(incidents, (list, tuple)):
        return [item for item in incidents if isinstance(item, dict)]
    return []


def _is_closed(incident):
    if incident.get('open') is False:
        return True
    status = str(incident.get('status') or incident.get('state') or '').lower()
    return status in CLOSED


def _episode_num(value):
    number = _as_int(value)
    return number if number is not None and number >= 1 else 1


def _incident_evidence_ids(incident):
    found = set()
    for item in incident.get('evidence_hashes') or []:
        if item not in (None, ''):
            found.add(str(item))
    for ref in incident.get('evidence_refs') or []:
        if not isinstance(ref, dict):
            continue
        if ref.get('record_hash'):
            found.add(str(ref['record_hash']))
        elif ref.get('path') and ref.get('line'):
            found.add(f"{ref['path']}:{ref['line']}")
    return found


def _evidence(event):
    path = _path(event)
    line = _line(event)
    if path is None or line is None:
        return None
    ref = {'path': path, 'line': line}
    if event.get('record_hash') not in (None, ''):
        ref['record_hash'] = str(event['record_hash'])
    field = _facts(event).get('field')
    if isinstance(field, str) and field.strip():
        ref['field'] = field.strip()
    return ref


def _evidence_id(event, ref):
    if event.get('record_hash') not in (None, ''):
        return str(event['record_hash'])
    if ref is not None:
        return f"{ref['path']}:{ref['line']}"
    return None


def _has_new_evidence(closed_rows, evidence_ids, evidence_times):
    known = set()
    for incident in closed_rows:
        known |= _incident_evidence_ids(incident)
    fresh = {item for item in evidence_ids if item}
    if known and fresh - known:
        return True
    if known and fresh and not (fresh - known):
        return False
    closed_at = [stamp for stamp in (_coerce_time(incident.get('closed_at')) for incident in closed_rows)
                 if stamp is not None]
    if not closed_at or not evidence_times:
        return False
    return max(evidence_times) > max(closed_at)


def _select_episode(incidents, pattern, subject, evidence_ids, evidence_times):
    related = [incident for incident in _iter_incidents(incidents)
               if incident.get('pattern') == pattern and str(incident.get('subject')) == str(subject)]
    if not related:
        return 1
    open_rows = [incident for incident in related if not _is_closed(incident)]
    if open_rows:
        return max(_episode_num(incident.get('episode')) for incident in open_rows)
    if not _has_new_evidence(related, evidence_ids, evidence_times):
        return None
    return max(_episode_num(incident.get('episode')) for incident in related) + 1


def _build(incidents, pattern, subject, events, suggested_fix, owner, action_kind, preconditions):
    if not subject:
        return None
    refs = []
    evidence_ids = []
    evidence_times = []
    seen = set()
    ordered = sorted(events, key=lambda event: (_line(event) or 0, _path(event) or ''))
    for event in ordered:
        ref = _evidence(event)
        if ref is None:
            continue
        key = (ref['path'], ref['line'], ref.get('field'), ref.get('record_hash'))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
        ident = _evidence_id(event, ref)
        if ident:
            evidence_ids.append(ident)
        stamp = _event_time(event)
        if stamp is not None:
            evidence_times.append(stamp)
    if not refs:
        return None
    episode = _select_episode(incidents, pattern, subject, evidence_ids, evidence_times)
    if episode is None:
        return None
    return {
        'pattern': pattern,
        'subject': str(subject),
        'episode': episode,
        'evidence_refs': refs,
        'suggested_fix': suggested_fix,
        'owner': owner,
        'action_kind': action_kind,
        'preconditions': list(preconditions),
    }


def _ordered(indexed):
    if indexed and all(_event_time(event) is not None for _, event in indexed):
        return sorted(indexed, key=lambda pair: (_event_time(pair[1]), pair[0]))
    return list(indexed)


def _group(rows, key_fn):
    groups = {}
    for index, event in rows:
        key = key_fn(event)
        if key is None:
            continue
        groups.setdefault(key, []).append((index, event))
    return groups


def _is_grok(event):
    facts = _facts(event)
    fields = ' '.join(str(facts.get(key) or '') for key in ('model', 'worker', 'channel', 'lane', 'provider'))
    blob = f"{fields} {event.get('kind') or ''}".lower()
    return re.search(r'(^|[^a-z0-9])grok([^a-z0-9]|$)', blob) is not None


def _refusal_label(event):
    raw = _facts(event).get('refusal', _facts(event).get('refusal_kind'))
    label = re.sub(r'[^a-z0-9]+', '_', str(raw or '').lower()).strip('_')
    if label in {'command', 'command_refusal', 'permission_cancelled', 'permissioncancelled'}:
        return label
    text = _text(event)
    if 'PermissionCancelled' in text or re.search(r'permission cancelled', text, re.I):
        return 'permission_cancelled'
    if re.search(r'command refusal|refused the command|explicit command refusal', text, re.I):
        return 'command'
    return None


def _grok_refusal(event):
    return _is_grok(event) and _refusal_label(event) is not None


def _package_key(event):
    facts = _facts(event)
    for key in ('package', 'package_id'):
        if facts.get(key) not in (None, ''):
            return str(facts[key])
    if facts.get('ledger_id') not in (None, ''):
        return str(facts['ledger_id'])
    if event.get('run_id') not in (None, ''):
        return str(event['run_id'])
    return None


def _attempt_id(event):
    facts = _facts(event)
    for key in ('attempt', 'attempt_id'):
        if facts.get(key) not in (None, ''):
            return str(facts[key])
    if event.get('run_id') not in (None, ''):
        return str(event['run_id'])
    return None


def _gave_up(event):
    facts = _facts(event)
    if facts.get('gave_up') is True or facts.get('inspector_gave_up') is True:
        return True
    return GAVE_UP_RE.search(_text(event)) is not None


def _folder_paths(event):
    facts = _facts(event)
    return _norm_path(facts.get('registry_cwd')), _norm_path(facts.get('claimed_worktree'))


def _folder_mismatch(event):
    cwd, claimed = _folder_paths(event)
    return cwd is not None and claimed is not None and cwd != claimed


def _folder_signal(event):
    facts = _facts(event)
    kind = str(event.get('kind') or '')
    if kind in {'folder_check', 'job_registration', 'inspector_restart'}:
        return True
    if _gave_up(event) or _folder_mismatch(event):
        return True
    return facts.get('registry_cwd') not in (None, '') or facts.get('claimed_worktree') not in (None, '')


def _job_subject(event):
    facts = _facts(event)
    job = _first(facts, ('job', 'job_id')) or event.get('run_id')
    if job in (None, ''):
        return None
    ledger = facts.get('ledger_id') or ''
    return f'job:{job}|{ledger}' if ledger else f'job:{job}'


def _failed_test_names(event):
    facts = _facts(event)
    failed = facts.get('failed_tests')
    names = set()
    if isinstance(failed, (list, tuple, set)):
        names = {str(item) for item in failed if str(item)}
    text = _text(event)
    names |= {match.group(1) for match in FAILED_TEST_RE.finditer(text)}
    return names


def _guards_failed(event):
    facts = _facts(event)
    return facts.get('guards_failed') is True or facts.get('mixed') is True or facts.get('mixed_guard') is True \
        or GUARDS_FAILED_RE.search(_text(event)) is not None


def _transient(event):
    facts = _facts(event)
    if facts.get('mixed') is True or facts.get('mixed_guard') is True:
        return False
    if _guards_failed(event):
        names = _failed_test_names(event)
        return bool(names) and names == {LOAD_GUARD_TEST}
    if facts.get('transient') is True:
        return True
    if facts.get('transient') is False:
        return False
    if TRANSIENT_RE.search(_text(event)):
        return True
    reason = str(facts.get('refusal_reason') or '').lower().replace('-', '_')
    return reason in {'load', 'headroom', 'runner_held'}


def _future_retry(event, now_ts):
    facts = _facts(event)
    retry_after = _coerce_time(facts.get('retry_after'))
    if retry_after is not None:
        if now_ts is None:
            return True
        return retry_after > now_ts
    return facts.get('retry_scheduled') is True


def _retry_succeeded(event):
    facts = _facts(event)
    if facts.get('successful_retry') is True or facts.get('retry_succeeded') is True:
        return True
    return 'successful retry' in _text(event).lower() or 'retry succeeded' in _text(event).lower()


def _retry_capped(event):
    facts = _facts(event)
    if facts.get('retry_cap_reached') is True:
        return True
    kind = str(event.get('kind') or '')
    if facts.get('landing_retry') is not True and kind not in {'landing_retry', 'landing_refusal', 'recovery'}:
        return False
    count = _as_int(facts.get('retry_count'))
    if count is None:
        count = _as_int(facts.get('attempts'))
    cap = _as_int(facts.get('retry_cap'))
    if cap is None:
        cap = RETRY_CAP
    return count is not None and count >= cap


def _train_subject(event):
    entry = _first(_facts(event), ('entry', 'task', 'train', 'queue_entry')) or event.get('node')
    if entry in (None, ''):
        return None
    return f'train:{entry}'


def _pin_state(event):
    facts = _facts(event)
    head = facts.get('branch_head', facts.get('head'))
    pin = facts.get('reviewed_pin', facts.get('pin'))
    if isinstance(head, str) and head and isinstance(pin, str) and pin:
        return 'match' if head == pin else 'mismatch'
    if facts.get('stale_ancestry') is True or facts.get('pin_mismatch') is True or facts.get('stale_pin') is True:
        return 'mismatch'
    if facts.get('pin_mismatch') is False or facts.get('pins_match') is True:
        return 'match'
    if PIN_TEXT_RE.search(_text(event)):
        return 'mismatch'
    return None


def _is_rejection(event):
    facts = _facts(event)
    if facts.get('rejected') is True:
        return True
    outcome = str(facts.get('outcome') or '').lower()
    if outcome in {'rejected', 'fix_rejected', 'review_rejected'}:
        return True
    return str(event.get('kind') or '') in {'review_rejection', 'fix_rejection'}


def _review_kind(event):
    kind = str(event.get('kind') or '')
    return kind.startswith('review') or kind in {'fix_rejection', 'fix_review'}


def _issue_id(event):
    value = _facts(event).get('issue', _facts(event).get('issue_id'))
    if value in (None, '', 'unknown'):
        return None
    return str(value)


def _review_subject(event):
    facts = _facts(event)
    feature = event.get('feature') or facts.get('feature')
    node = event.get('node') or facts.get('node')
    if not feature or not node:
        return None
    issue = _issue_id(event)
    subject = f'node:{feature}/{node}'
    return f'{subject}#{issue}' if issue else subject


def _ff_state(event):
    facts = _facts(event)
    if facts.get('not_fast_forwarded') is True:
        return 'failed'
    if facts.get('fast_forwarded') is True:
        return 'ok'
    text = _text(event)
    if NOT_FF in text:
        return 'failed'
    if YES_FF in text:
        return 'ok'
    return None


def _worker_escalated(event):
    facts = _facts(event)
    reason = str(facts.get('reason') or '')
    signaled = facts.get('worker_escalated') is True or reason == 'worker_escalated' \
        or 'worker escalat' in _text(event).lower()
    if not signaled:
        return False
    if facts.get('repeated') is True:
        return True
    count = _as_int(facts.get('escalation_count'))
    if count is None:
        count = _as_int(facts.get('attempts'))
    return count is not None and count >= 2


def _coordinator_linked(event):
    facts = _facts(event)
    return facts.get('coordinator_linked') is True or facts.get('coordinator_action') is True


def _inspector_exhausted(event, now_ts):
    if not _gave_up(event) or _coordinator_linked(event) or now_ts is None:
        return False
    stamp = _event_time(event)
    return stamp is not None and now_ts - stamp >= TICK_S


def _is_unconfirmed(event):
    label = str(_facts(event).get('classification') or '')
    return label.upper() == 'UNCONFIRMED' or str(event.get('kind') or '') == 'unconfirmed'


def _unconfirmed_signal(event):
    if 'classification' in _facts(event):
        return True
    return str(event.get('kind') or '') in {'handoff', 'unconfirmed', 'dispatch'}


def _proof(event):
    facts = _facts(event)
    if facts.get('start_proof') is True or facts.get('exit_proof') is True:
        return True
    if facts.get('started') is True or facts.get('exited') is True:
        return True
    return str(event.get('kind') or '') in {'start', 'exit', 'worker_start', 'worker_exit'}


def _dispatch_subject(event):
    facts = _facts(event)
    feature = event.get('feature') or facts.get('feature')
    node = event.get('node') or facts.get('node')
    if feature and node:
        return f'dispatch:{feature}/{node}'
    if event.get('run_id') not in (None, ''):
        return f'dispatch:{event["run_id"]}'
    return None


def _source_name(event):
    name = _first(_facts(event), ('source', 'source_name'))
    if name:
        return name
    if event.get('file_id'):
        return str(event['file_id'])
    return None


def _source_problem(event):
    facts = _facts(event)
    if facts.get('scratchpad_unmapped') is True or facts.get('unmapped') is True:
        return 'unmapped'
    for key in ('problem', 'status', 'error'):
        value = str(facts.get(key) or '').lower()
        if value in SOURCE_PROBLEMS:
            return value
    if facts.get('available') is False:
        return 'unavailable'
    return None


def _source_ok(event):
    facts = _facts(event)
    if facts.get('available') is True or facts.get('scratchpad_unmapped') is False:
        return True
    status = str(facts.get('status') or facts.get('problem') or '').lower()
    return status in {'ok', 'healthy', 'available'}


def _source_signal(event):
    facts = _facts(event)
    kind = str(event.get('kind') or '')
    if kind in {'source_health', 'qc_source'}:
        return True
    if 'available' in facts or 'problem' in facts or 'scratchpad_unmapped' in facts:
        return True
    return False


def _is_failure(event):
    facts = _facts(event)
    if facts.get('failed') is True:
        return True
    return str(event.get('kind') or '') in {'failure', 'run_failed', 'unmatched_failure'}


def _known_domain(event):
    return any((
        _grok_refusal(event),
        _folder_signal(event),
        _transient(event),
        _pin_state(event) is not None,
        _is_rejection(event) or _review_kind(event),
        _ff_state(event) is not None,
        _retry_succeeded(event),
        _future_retry(event, None),
        _retry_capped(event),
        _worker_escalated(event),
        _unconfirmed_signal(event),
        _source_signal(event),
    ))


def _detect_grok(rows, incidents):
    hits = []
    for package, indexed in _group(rows, lambda event: _package_key(event) if _grok_refusal(event) else None).items():
        events = [event for _, event in indexed]
        attempts = []
        ledgers = []
        for event in events:
            attempt = _attempt_id(event)
            if attempt and attempt not in attempts:
                attempts.append(attempt)
            ledger = _facts(event).get('ledger_id')
            if ledger not in (None, '') and str(ledger) not in ledgers:
                ledgers.append(str(ledger))
        counted = [event for event in events if _attempt_id(event) in attempts] if attempts else events[:1]
        # One evidence line per distinct attempt. Later receipts of that attempt are not new attempts.
        chosen = []
        seen_attempts = set()
        for event in counted:
            attempt = _attempt_id(event) or id(event)
            if attempt in seen_attempts:
                continue
            seen_attempts.add(attempt)
            chosen.append(event)
        fix = 'Add the single-command brief clause for its next dispatch. Lane coordinator owns the brief.'
        if len(seen_attempts) > 1:
            fix += ' Sandbox owner investigates refusals that persist.'
        fix += ' Never treat cancellation as permission to retry a denied command.'
        if ledgers:
            fix += ' Ledger: ' + ', '.join(ledgers) + '.'
        hit = _build(
            incidents, GROK_COMMAND_REFUSAL, f'package:{package}', chosen, fix, 'lane-coordinator',
            ADD_GROK_CLAUSE,
            ['append once to the next-dispatch brief',
             'do not edit a live worker or weaken a permission rule',
             'validate brief size after insertion',
             'a cancellation is not permission to retry the command'])
        if hit:
            hits.append(hit)
    return hits


def _detect_folder(rows, incidents):
    hits = []
    eligible = _group(rows, lambda event: _job_subject(event) if (_folder_mismatch(event) or _gave_up(event)) else None)
    for subject, indexed in eligible.items():
        events = [event for _, event in indexed]
        mismatch = [event for event in events if _folder_mismatch(event)]
        gave_up = [event for event in events if _gave_up(event)]
        api_blocked = any(_facts(event).get('api_unsupported') is True or _facts(event).get('observe_only_api') is False
                          for event in events)
        if mismatch:
            chosen = mismatch
            fix = ('Re-register the job as observe-only for the proven folder mismatch. '
                   'Stop the recovery loop from launching a second worker.')
            if api_blocked:
                action = WAITING_BOARD
                fix += ' Registration API cannot guarantee observe-only. Block the repair and report the missing capability.'
                preconditions = ['registration API cannot guarantee observe-only', *BOARD_PRECONDITIONS]
            else:
                action = REREGISTER_OBSERVE_ONLY
                preconditions = ['exact job and worktree identity',
                                 'registration API guarantees no process start or restart',
                                 'change only that registration and read it back as observe-only',
                                 'one repair per incident']
        else:
            chosen = gave_up
            action = WAITING_BOARD
            fix = ('The inspector gave up after four restarts. This does not prove a folder mismatch. '
                   'Job coordinator reconciles folder and identity and stops the recovery loop from launching a second worker.')
            preconditions = list(BOARD_PRECONDITIONS)
        hit = _build(incidents, WRONG_FOLDER_RESTART, subject, chosen, fix, 'job-coordinator', action, preconditions)
        if hit:
            hits.append(hit)
    return hits


def _stall_block(events):
    reasons = []
    facts_rows = [_facts(event) for event in events]
    if any(facts.get('hold') is True or facts.get('task_hold') is True for facts in facts_rows):
        reasons.append('held')
    if any(facts.get('active_train') is True for facts in facts_rows):
        reasons.append('active train')
    if any(facts.get('pins_changed') is True for facts in facts_rows):
        reasons.append('reviewed pins changed')
    if any(facts.get('identity_changed') is True or facts.get('revision_changed') is True for facts in facts_rows):
        reasons.append('identity changed')
    for facts in facts_rows:
        if 'pending_entries' not in facts:
            continue
        count = _as_int(facts.get('pending_entries'))
        if count != 1:
            reasons.append('pending entry count is not one')
            break
    return reasons


def _detect_stall(rows, incidents, now_ts):
    hits = []
    groups = _group(rows, _train_subject)
    for subject, indexed in groups.items():
        ordered = _ordered(indexed)
        last_success = -1
        for position, (_, event) in enumerate(ordered):
            if _retry_succeeded(event):
                last_success = position
        post = [event for position, (_, event) in enumerate(ordered) if position > last_success]
        # A retry that is still scheduled, or a budget that is already spent, is not a new stall.
        if any(_future_retry(event, now_ts) or _retry_capped(event) for event in post):
            continue
        streak = [event for event in post if _transient(event)]
        if not streak:
            continue
        events = streak
        times = [stamp for stamp in (_event_time(event) for event in events) if stamp is not None]
        if now_ts is None or not times or now_ts - min(times) < TWO_TICKS_S:
            continue
        blocked = _stall_block(events)
        if blocked:
            action = WAITING_BOARD
            preconditions = blocked + ['do not bypass the failed check', 'do not launch a train']
        else:
            action = REQUEUE_TRANSIENT
            preconditions = ['use the landing runner queue lock',
                             'transient classifier on the full failure output',
                             'same task and unchanged reviewed pins',
                             'no hold, no active train, no pending retry',
                             'retry budget not exhausted',
                             'exactly one pending entry',
                             'do not launch a train']
        fix = ('Landing owner restores the same eligible queue entry once through the existing runner. '
               'Retain the retry count and the reviewed pin.')
        hit = _build(incidents, LANDING_TRANSIENT_STALL, subject, events, fix, 'landing-owner', action, preconditions)
        if hit:
            hits.append(hit)
    return hits


def _detect_pin(rows, incidents):
    hits = []
    for subject, indexed in _group(rows, _train_subject).items():
        ordered = _ordered(indexed)
        states = [(event, _pin_state(event)) for _, event in ordered]
        known = [(event, state) for event, state in states if state is not None]
        if not known or known[-1][1] == 'match':
            continue
        mismatch = [event for event, state in known if state == 'mismatch']
        fix = ('Landing owner asks the package owner for review of the new head and an updated queue entry. '
               'No automatic repinning or stale worktree deletion.')
        hit = _build(incidents, LANDING_STALE_PIN, subject, mismatch, fix, 'landing-owner', WAITING_BOARD,
                     BOARD_PRECONDITIONS)
        if hit:
            hits.append(hit)
    return hits


def _detect_review(rows, incidents):
    hits = []
    groups = _group(rows, lambda event: _review_subject(event) if _is_rejection(event) else None)
    for subject, indexed in groups.items():
        unique = []
        seen = set()
        issue_known = False
        for _, event in indexed:
            if _issue_id(event):
                issue_known = True
            attempt = _attempt_id(event)
            if attempt is None or attempt in seen:
                continue
            seen.add(attempt)
            unique.append(event)
        if len(unique) < REVIEW_MIN:
            continue
        fix = ('Feature lead consolidates remaining defects, assigns one reproducer and adjusts scope '
               'or reviewer instructions.')
        if not issue_known:
            fix += ' Issue attribution unknown.'
        fix += ' Board decides any pause or reassignment.'
        if len(unique) >= REVIEW_URGENT:
            fix = 'URGENT: ' + fix
        hit = _build(incidents, REVIEW_CHURN, subject, unique, fix, 'feature-lead', WAITING_BOARD, BOARD_PRECONDITIONS)
        if hit:
            hits.append(hit)
    return hits


def _detect_main(rows, incidents):
    ordered = _ordered(rows)
    states = [(event, _ff_state(event)) for _, event in ordered]
    known = [(event, state) for event, state in states if state is not None]
    if not known or known[-1][1] == 'ok':
        return []
    failed = [event for event, state in known if state == 'failed']
    fix = ('Main-checkout owner inspects dirty paths, branch divergence and runtime revision, then proposes '
           'a non-destructive update. Do not reset, stash, merge or restart a service.')
    hit = _build(incidents, MAIN_CHECKOUT_STALE, 'checkout:main', failed, fix, 'main-checkout-owner',
                 WAITING_BOARD, BOARD_PRECONDITIONS)
    return [hit] if hit else []


def _recovery_subject(event, now_ts):
    if _retry_capped(event):
        return _train_subject(event)
    if _inspector_exhausted(event, now_ts):
        return _job_subject(event)
    if _worker_escalated(event):
        if event.get('feature') and event.get('node'):
            return f"worker:{event['feature']}/{event['node']}"
        task = _first(_facts(event), ('task', 'job', 'job_id'))
        if task:
            return f'worker:{task}'
    return None


def _recovery_fix(events):
    attempts = None
    last_action = None
    for event in events:
        facts = _facts(event)
        if attempts is None:
            for key in ('attempts', 'retry_count', 'escalation_count'):
                attempts = _as_int(facts.get(key))
                if attempts is not None:
                    break
        if last_action is None and isinstance(facts.get('last_action'), str) and facts.get('last_action').strip():
            last_action = facts['last_action'].strip()
    attempt_text = str(attempts) if attempts is not None else 'unrecorded'
    action_text = last_action or 'unrecorded'
    return ('Owning coordinator diagnoses the failed recovery. '
            f'Attempts: {attempt_text}. Last action: {action_text}. {RECOVERY_SENTENCE}')


def _merge_recovery(hit, events, note):
    seen = {(ref['path'], ref['line'], ref.get('field'), ref.get('record_hash')) for ref in hit['evidence_refs']}
    for event in events:
        ref = _evidence(event)
        if ref is None:
            continue
        key = (ref['path'], ref['line'], ref.get('field'), ref.get('record_hash'))
        if key in seen:
            continue
        seen.add(key)
        hit['evidence_refs'].append(ref)
    if RECOVERY_SENTENCE not in hit['suggested_fix']:
        hit['suggested_fix'] = hit['suggested_fix'].rstrip() + ' ' + note
    if hit['action_kind'] == REQUEUE_TRANSIENT:
        hit['action_kind'] = WAITING_BOARD
        hit['preconditions'] = ['retry budget exhausted', 'do not reset the retry budget', 'do not launch a train']


def _detect_recovery(rows, incidents, hits, now_ts):
    groups = {}
    for _, event in rows:
        if not (_retry_capped(event) or _worker_escalated(event) or _inspector_exhausted(event, now_ts)):
            continue
        subject = _recovery_subject(event, now_ts)
        if subject is None:
            continue
        groups.setdefault(subject, []).append(event)
    produced = []
    for subject, events in groups.items():
        note = _recovery_fix(events)
        same = [hit for hit in hits if hit['subject'] == subject]
        target = None
        for pattern in (LANDING_TRANSIENT_STALL, WRONG_FOLDER_RESTART, LANDING_STALE_PIN):
            target = next((hit for hit in same if hit['pattern'] == pattern), None)
            if target:
                break
        if target is None and same:
            target = same[0]
        if target is not None:
            _merge_recovery(target, events, note)
            continue
        open_others = [incident for incident in _iter_incidents(incidents)
                       if str(incident.get('subject')) == subject and not _is_closed(incident)
                       and incident.get('pattern') != RECOVERY_EXHAUSTED]
        if open_others:
            incident = max(open_others, key=lambda item: _episode_num(item.get('episode')))
            folded = _build(
                [{'pattern': incident.get('pattern'), 'subject': subject, 'episode': incident.get('episode')}],
                incident.get('pattern'), subject, events,
                note, incident.get('owner') or 'owning-coordinator', WAITING_BOARD,
                ['retry budget exhausted', 'do not reset the retry budget', 'do not launch a train'])
            if folded:
                produced.append(folded)
            continue
        hit = _build(incidents, RECOVERY_EXHAUSTED, subject, events, note, 'owning-coordinator', WAITING_BOARD,
                     ['do not reset the retry budget', 'do not launch a replacement worker', 'board owns the diagnosis'])
        if hit:
            produced.append(hit)
    return produced


def _detect_unconfirmed(rows, incidents, now_ts):
    hits = []
    proofs = set()
    for _, event in rows:
        if _proof(event):
            subject = _dispatch_subject(event)
            if subject:
                proofs.add(subject)
    groups = _group(rows, lambda event: _dispatch_subject(event) if _is_unconfirmed(event) else None)
    for subject, indexed in groups.items():
        if subject in proofs:
            continue
        events = [event for _, event in indexed if not _proof(event)]
        if not events:
            continue
        times = [stamp for stamp in (_event_time(event) for event in events) if stamp is not None]
        if now_ts is None or not times or now_ts - min(times) < TWO_TICKS_S:
            continue
        fix = ('Dispatch owner reconciles the original receipt, worker and hold. '
               'No duplicate send based on absence alone.')
        hit = _build(incidents, START_UNCONFIRMED, subject, events, fix, 'dispatch-owner', WAITING_BOARD,
                     BOARD_PRECONDITIONS)
        if hit:
            hits.append(hit)
    return hits


def _detect_source(rows, incidents, now_ts):
    hits = []
    groups = _group(rows, lambda event: f"source:{_source_name(event)}" if _source_signal(event) and _source_name(event) else None)
    for subject, indexed in groups.items():
        ordered = _ordered(indexed)
        last_ok = -1
        for position, (_, event) in enumerate(ordered):
            if _source_ok(event):
                last_ok = position
        streak = [event for position, (_, event) in enumerate(ordered)
                  if position > last_ok and _source_problem(event)]
        if not streak:
            continue
        times = [stamp for stamp in (_event_time(event) for event in streak) if stamp is not None]
        if now_ts is None or not times or now_ts - min(times) < TWO_TICKS_S:
            continue
        teams = []
        for event in streak:
            raw = _facts(event).get('teams_unobserved', _facts(event).get('teams'))
            values = raw if isinstance(raw, (list, tuple)) else [raw]
            for value in values:
                if value not in (None, '') and str(value) not in teams:
                    teams.append(str(value))
        shown = ', '.join(teams) if teams else 'unknown'
        fix = (f'QC operator repairs the source mapping or reader. Teams unobserved: {shown}. '
               'No success claim or action from incomplete evidence.')
        hit = _build(incidents, QC_SOURCE_UNAVAILABLE, subject, streak, fix, 'qc-operator', WAITING_BOARD,
                     BOARD_PRECONDITIONS)
        if hit:
            hits.append(hit)
    return hits


def _detect_unmatched(rows, incidents, classified):
    hits = []
    groups = {}
    for index, event in rows:
        if index in classified or not _is_failure(event):
            continue
        facts = _facts(event)
        category = str(facts.get('error_category') or 'unknown').strip().lower() or 'unknown'
        task = _first(facts, ('task', 'task_id'))
        if task is None and event.get('feature') and event.get('node'):
            task = f"{event['feature']}/{event['node']}"
        if task is None:
            continue
        attempt = _attempt_id(event)
        stamp = _event_time(event)
        if attempt is None or stamp is None:
            continue
        groups.setdefault((category, task), []).append((stamp, attempt, event))
    for (category, task), items in groups.items():
        earliest = {}
        for stamp, attempt, event in items:
            current = earliest.get(attempt)
            if current is None or stamp < current[0]:
                earliest[attempt] = (stamp, event)
        ordered = sorted(earliest.values(), key=lambda item: item[0])
        best = None
        for start in range(len(ordered)):
            cluster = [ordered[start]]
            for follow in ordered[start + 1:]:
                if follow[0] - ordered[start][0] <= HOUR_S:
                    cluster.append(follow)
                else:
                    break
            if len(cluster) >= 2 and (best is None or len(cluster) > len(best)):
                best = cluster
        if not best:
            continue
        events = [event for _, event in best]
        owners = []
        features = []
        for event in events:
            owner = _facts(event).get('owner')
            if isinstance(owner, str) and owner.strip():
                owners.append(owner.strip())
            if event.get('feature'):
                features.append(event['feature'])
        if owners:
            owner = owners[0]
        elif features:
            owner = 'feature-lead'
        else:
            owner = 'board'
        fix = ('Two distinct failed attempts within an hour share one unmatched failure cluster. '
               'Board owns the follow-up. No automatic action and no model summary.')
        hit = _build(incidents, UNMATCHED_FAILURE, f'unmatched:{category}:{task}', events, fix, owner,
                     WAITING_BOARD, ['no model call', 'no automatic action', *BOARD_PRECONDITIONS])
        if hit:
            hits.append(hit)
    return hits
