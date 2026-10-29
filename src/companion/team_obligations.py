"""Bounded completion evidence and open work, shared by Team transports.

Evidence is a model's cited proof, not execution of that proof. Independent review
still decides its truth. Retry checks use only bounded paths and fixed Git commands.
"""
import math
import re
import subprocess
from pathlib import Path

from companion import delivery_ledger

FIELDS = ('acceptance', 'follow_ups', 'unmet_acceptance', 'handoffs', 'preconditions')
LIMIT = 16
TEXT_LIMIT = 500
RESULT_RULES = (
    '\nCompletion contract: include acceptance as an object mapping each assigned acceptance item '
    'verbatim to concrete evidence (empty evidence means unmet). Include follow_ups and unmet_acceptance '
    'as lists of short item strings, or empty lists. Do not hide open work in a completed summary. '
    'needs_input is an owner decision only when requires_owner_decision=true and summary states the '
    'decision; ordinary blockers go back to the assigned owner for bounded repair. '
    'Report external ownership as handoffs: [{owner, acceptance, due_at}], with evidence or '
    'close_reason only when closing that exact item. Describe prose handoffs as Handed off to OWNER: ACCEPTANCE, where OWNER names the destination (Cursor app task NAME, Codex queue NAME, lane N, or NAME session); a bare app name is not recorded. '
    'For checkable blockers include preconditions: [{kind: file_exists, commit_on_master, seat_free or chat_open, value: '
    'workspace-relative file, hex commit or bound participant}]. These hold retries until verified locally. '
    'These fields do not expand scope or authorize tools, private data access, sending, or merge.\n'
)


def items(value):
    if (not isinstance(value, list) or len(value) > LIMIT
            or any(not isinstance(s, str) or not s.strip() or len(s) > TEXT_LIMIT for s in value)
            or len(set(value)) != len(value)):
        raise ValueError('Invalid bounded work items')
    return value


def metadata(value):
    result = {}
    for name in FIELDS:
        if name not in value:
            continue
        data = value[name]
        if name == 'acceptance':
            if not isinstance(data, dict):
                raise ValueError('Invalid acceptance evidence')
            items(list(data))
            if any(not isinstance(v, str) or len(v) > 1000 for v in data.values()):
                raise ValueError('Invalid acceptance evidence')
        elif name == 'handoffs':
            handoffs(data)
        elif name == 'preconditions':
            preconditions(data)
        else:
            items(data)
        result[name] = data
    return result


def listed(summary):
    """Recognize explicit open-work lists, not arbitrary prose or imperative text."""
    result, section = [], False
    for line in summary.splitlines():
        heading = line.strip().strip('#* ').rstrip(':').lower().replace('-', '_').replace(' ', '_')
        if heading in {'follow_ups', 'followups', 'unmet_acceptance', 'missing_acceptance'}:
            section = True
            continue
        match = re.match(r'^\s*[-*]\s+(?:\[ \]\s*)?(.+)', line)
        if section and match:
            text = match[1].strip()
            if text.lower().strip('.') not in {'none', 'no follow-ups', 'no open items'}:
                result.append(text[:TEXT_LIMIT])
        elif line.strip():
            section = False
    if len(result) > LIMIT:
        raise ValueError('Too many open work items')
    return list(dict.fromkeys(result))


def missing(required, value, summary=''):
    evidence = value.get('acceptance', {})
    return list(dict.fromkeys(
        [item for item in required if not evidence.get(item, '').strip()]
        + value.get('unmet_acceptance', []) + value.get('follow_ups', []) + listed(summary)))


def retry_prompt(instruction, blocker):
    # Retain the whole assigned instruction. Retry context is bounded separately.
    return (instruction + '\nRepair context (data): ' + blocker[:1500]
            + '\nResolve and verify this blocker before continuing. Stay within the original scope '
              'and permissions. If unresolved, report the blocker; do not claim completion.')


def handoffs(value):
    if not isinstance(value, list) or len(value) > LIMIT:
        raise ValueError('Invalid handoffs')
    for item in value:
        if (not isinstance(item, dict) or set(item) - {'owner', 'acceptance', 'due_at', 'evidence', 'close_reason'}
                or not {'owner', 'acceptance'} <= item.keys()):
            raise ValueError('Invalid handoff')
        for key in ('owner', 'acceptance', 'evidence', 'close_reason'):
            if key in item and (not isinstance(item[key], str) or not item[key].strip()
                                or len(item[key]) > (120 if key == 'owner' else TEXT_LIMIT)
                                or not item[key].isprintable()):
                raise ValueError('Invalid handoff text')
        if 'due_at' in item and (type(item['due_at']) not in (int, float)
                                or not math.isfinite(item['due_at']) or item['due_at'] < 0):
            raise ValueError('Invalid handoff due time')
    return value


def reported_handoffs(value, summary=''):
    """Structured records win; recognize explicit single-line handoff declarations too."""
    result = {(i['owner'], i['acceptance']): i for i in handoffs(value.get('handoffs', []))}
    for line in summary.splitlines():
        match = re.fullmatch(r'(?i)(?:work )?(?:handed off|handoff) to (.{1,120}?): (.{1,500})', line.strip())
        if match and external_owner(match[1]):
            owner, acceptance = match.groups()
            item = dict(owner=owner, acceptance=acceptance)
            handoffs([item])
            result.setdefault((owner, acceptance), item)
    if len(result) > LIMIT:
        raise ValueError('Too many handoffs')
    return list(result.values())


def preconditions(value):
    if not isinstance(value, list) or len(value) > LIMIT:
        raise ValueError('Invalid preconditions')
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'kind', 'value'}
                or item['kind'] not in {'file_exists', 'commit_on_master', 'seat_free', 'chat_open'}
                or not isinstance(item['value'], str) or not 1 <= len(item['value']) <= 500
                or not item['value'].isprintable()):
            raise ValueError('Invalid precondition')
        path = Path(item['value'])
        if item['kind'] == 'file_exists' and (path.is_absolute() or '..' in path.parts or not path.parts):
            raise ValueError('Precondition path must stay in the workspace')
        if item['kind'] == 'commit_on_master' and not re.fullmatch(r'[0-9a-f]{7,40}', item['value']):
            raise ValueError('Precondition commit must be a hex identity')
    return value


def unmet_preconditions(value, workspace, *, run=None, claims=None, delivery_status=None):
    """No shell, fetch, content reads, or checks outside the bound workspace."""
    pending = []
    root = Path(workspace).resolve() if workspace else None
    for item in preconditions(value):
        ok = False
        try:
            if item['kind'] == 'seat_free':
                binding = (run or {}).get('lane') or (run or {}).get('submitted_config') or {}
                pins = binding.get('participants', {})
                pin = pins.get(item['value'], {}).get('id', item['value'])
                ok = claims is not None and pin in {p.get('id') for p in pins.values()} and not any(
                    c.get('run_id') != (run or {}).get('id') and any(
                        p.get('id') == pin for p in c.get('participants', {}).values()) for c in claims)
            elif item['kind'] == 'chat_open':
                binding = (run or {}).get('lane') or (run or {}).get('submitted_config') or {}
                pin = binding.get('participants', {}).get('codex', {}).get('id')
                ok = (item['value'] in {'codex', pin} and delivery_status is not None
                      and delivery_status(run) in {'running', 'idle'})
            elif root and root.is_dir():
                if item['kind'] == 'file_exists':
                    path = root / item['value']
                    ok = path.resolve().is_relative_to(root) and path.is_file()
                else:
                    # Local master is the checkout's observed master, never fetch here.
                    check = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor',
                                            item['value'], 'refs/heads/master'],
                                           capture_output=True, timeout=3, check=False)
                    ok = check.returncode == 0
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
            pass
        if not ok:
            text = item['kind'] + ' ' + item['value']
            if item['kind'] == 'seat_free' and not _bound(run, item['value']):
                text += ' (not in this lane; stop or reconcile the run)'
            pending.append(text)
    return pending


def _bound(run, value):
    binding = (run or {}).get('lane') or (run or {}).get('submitted_config') or {}
    pins = binding.get('participants', {}) if isinstance(binding, dict) else {}
    return value in pins or value in {p.get('id') for p in pins.values() if isinstance(p, dict)}


def wait_reason(pending, since, now):
    return 'waiting on ' + '; '.join(pending)[:180] + f' · {max(0, int((now - since) // 60))} min'


HANDOFF_SECONDS = 3600


def external_owner(owner):
    """Only an explicit named destination, never a promise about the current session."""
    return bool(re.fullmatch(
        r'(?i)(?:(?:cursor|antigravity) (?:app task|task|chat) .+|codex (?:queue|chat) .+|'
        r'(?:named )?lane .+|(?!(?:this|current|the|my|our) )(?:.+) (?:session|chat))', owner))


def blocked_preconditions(value, reason):
    """Recognize bounded blocker facts; no arbitrary command or path execution."""
    found = {(i['kind'], i['value']): i for i in preconditions(value.get('preconditions', []))}
    patterns = {
        'file_exists': r'(?i)\bfile [`\"]?([\w./-]+)[`\"]? (?:is )?missing\b',
        'commit_on_master': r'(?i)\bcommit ([0-9a-f]{7,40}) (?:is )?not on master\b',
        'seat_free': r'(?i)\b(codex|claude|cursor|antigravity) (?:seat|participant) (?:is )?(?:busy|claimed|pinned)\b',
        'chat_open': r'(?i)\b(codex) chat (?:is )?(?:closed|unavailable|not open)\b',
    }
    for kind, pattern in patterns.items():
        for match in re.finditer(pattern, reason[:16000]):
            item = dict(kind=kind, value=match[1] if kind == 'file_exists' else match[1].lower())
            try:
                preconditions([item])
            except ValueError:
                continue
            found.setdefault((kind, item['value']), item)
    return list(found.values())[:LIMIT]


def dispatch_handoffs(root, run):
    """Own-run external receipts only. The reply's own dispatch is not a handoff.

    A record may bind explicitly by run_id or by an assignment under runs/<id>.
    Receipt closure must come from the store, never the model's assertion.
    """
    try:
        rows = delivery_ledger.read(delivery_ledger.path_for(Path(root).parent), max_bytes=4_000_000)
    except (OSError, ValueError) as exc:  # fail closed as an owned hold, never by stopping the worker
        return [_store_hold('deliveries.json unreadable (' + str(exc)[:60] + ')')]
    result = []
    run_dir = Path(root) / 'runs' / str(run.get('id', ''))
    for row in rows:
        if not isinstance(row, dict):
            continue  # The ledger validates rows; retain the defensive boundary here.
        assignment = Path(row.get('assignment', ''))
        if not run.get('id') or not (row.get('run_id') == run['id'] or assignment.is_relative_to(run_dir)):
            continue
        # Current/previous native stages use this exact service-owned filename shape.
        if (str(assignment) == run.get('assignment_path') or row.get('id') == run.get('delivery_id')
                or (assignment.parent == run_dir and re.fullmatch(
                    r'(?:plan|build|review|reply_codex|reply_claude|interview)-[0-9]+\.md', assignment.name))):
            continue
        if not all(isinstance(row.get(k), str) and row[k] for k in ('id', 'app', 'task_id', 'state')):
            result.append(_store_hold('a delivery row for this run is incomplete'))
            continue
        owner = (row['app'] + ' task ' + row['task_id'])[:120]
        item = dict(owner=owner, acceptance=('Resolve delivery ' + row['id'])[:500])
        if row['state'] in {'completed', 'failed'}:
            item['close_reason'] = 'Delivery store records ' + row['state']
        elif row['state'] not in {'queued', 'acknowledged', 'running', 'ambiguous'}:
            result.append(_store_hold('delivery ' + row['id'][:40] + ' has an unknown state'))
            continue
        sent = row.get('sent_at')
        if type(sent) in (int, float) and math.isfinite(sent) and sent >= 0:
            item['due_at'] = sent + 1800
        result.append(item)
    return handoffs(result)


def _store_hold(problem):
    return dict(owner='Kyra delivery store', acceptance=('Inspect and repair: ' + problem)[:500])


def completion(root, run, value, summary):
    result = metadata(value)
    reported = reported_handoffs(value, summary)
    derived = dispatch_handoffs(root, run)
    # Recorded deliveries override any claimed closure of the same item.
    merged = {(i['owner'], i['acceptance']): i for i in reported + derived}
    if merged:
        result['handoffs'] = handoffs(list(merged.values()))
    conditions = blocked_preconditions(value, summary)
    if conditions:
        result['preconditions'] = conditions
    return result
