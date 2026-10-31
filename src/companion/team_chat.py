"""Durable personal team conversations and bounded visible-agent handoffs.

Result envelopes correlate a reply with an assignment; they do not authenticate
its author against another process running as the same operating-system user.
"""
import hashlib
import json
import logging
import math
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from companion import team_log
from companion import team_obligations as obligations
from companion.file_lock import exclusive
from companion.paths import DATA_DIR, write_json
from companion.settings import get_settings
from companion.team_voice import LIMITS, chat_line, result_chat

log = logging.getLogger(__name__)

ACTIVE = {'queued', 'planning', 'building', 'reviewing', 'replying', 'stopping'}
TERMINAL = {'completed', 'needs_input', 'stopped', 'uncertain'}
STATES = ACTIVE | TERMINAL
STAGES = {'plan': ('claude', 'planning'), 'build': ('codex', 'building'), 'review': ('claude', 'reviewing'),
          'reply_claude': ('claude', 'replying'), 'reply_codex': ('codex', 'replying')}
SINGLE_STAGES = {'reply_claude': 'reply_claude', 'reply_codex': 'reply_codex',
                 'plan_only': 'plan', 'review_only': 'review', 'interview_only': 'plan'}
EXECUTION_MODES = {'plan', 'worker', 'code_review', *SINGLE_STAGES}
PLAN_PROVIDERS = {'kyra', 'claude', 'codex', 'cursor', 'antigravity'}
MAX_TEXT = 16000
MAX_RESULT = 65536
MAX_STORE_BYTES = 16 * 1024 * 1024
COMPLETION_RESERVE_BYTES = 8 * 1024 * 1024
ADMISSION_LIMIT_BYTES = MAX_STORE_BYTES - COMPLETION_RESERVE_BYTES
CAPACITY_REASON = 'Team storage is near its limit; preserve history and archive closed chats on the Mac.'
STAGE_TIMEOUT_SECONDS = 2 * 60 * 60
LOCAL_COMPLETION_BYTES = 128 * 1024
ANSWER_ACTIVE = {'queued', 'running'}
ANSWER_STATES = ANSWER_ACTIVE | {'answered', 'failed', 'stopped'}
ANSWER_ROUTES = {'answer', 'clarify', 'team_plan', 'worker_task', 'app_cursor', 'app_antigravity', 'app_claude', 'app_codex', 'app_all'}
ANSWER_NOTICES = {'', 'cancelled', 'timeout', 'cache_unavailable', 'model_mismatch',
                  'input_too_large', 'output_invalid', 'worker_failed', 'sandbox_unavailable',
                  'source_changed'}


STATUS_NOTICES = {
    'team_unavailable': 'Saved Team status is unavailable.',
    'team_legacy': 'This Team store predates the storage reserve; new work needs a fresh store.',
    'team_capacity': 'Team storage is near its limit; archive closed chats on the Mac.',
    'controller_unavailable': 'Saved controller status is unavailable.',
    'controller_namespace': 'Team controller status is unknown for this store.',
    'claims_unavailable': 'Saved shared claims are unconfirmed; inspect them on the Mac.',
}
CONTROLLER_LABELS = {
    'running': 'Controller saved as running',
    'stopped_idle': 'Controller saved as stopped: idle',
    'stopped_blocked': 'Controller saved as stopped: blocked',
    'suspended_unknown': 'Controller saved as suspended: unknown activity',
    'exhausted': 'Controller saved as exhausted',
    'expired': 'Controller saved as expired',
    'revoked': 'Controller saved as revoked',
}
REASON_LABELS = {
    CAPACITY_REASON: 'Storage limit needs attention',
    'The time limit ended. Inspect the native chat before continuing.': 'Time limit ended',
    'The time limit ended before dispatch.': 'Time limit ended before dispatch',
    'The worker finished but its reply was invalid. Inspect it on the Mac before a fresh instruction.': 'Invalid worker reply',
    'A shared claim needs inspection in the native chat before continuing.': 'Shared claim needs inspection',
    'A reserved worker needs reconciliation before a fresh instruction.': 'Shared claim needs inspection',
    'The sending process stopped. Check the native chat before continuing.': 'Sending process stopped',
    'Delivery receipt was invalid. Inspect the native chat before continuing.': 'Invalid delivery receipt',
}


def empty_status_snapshot(*, now=None):
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now):
        raise ValueError('Invalid snapshot time')
    return dict(read_at=now, team_saved_at=None, controller_saved_at=None,
                claims_saved_at=None, notices=[], runs=[])


def _saved_at(path):
    value = path.stat().st_mtime
    return value if math.isfinite(value) else None


def _saved_controller_label(state, config, stage, now, role=None):
    """Compare saved fields only; this is not dispatch authority or live health."""
    if state is None or not config or config.get('version') not in (2, 3):
        return 'Controller status unknown', False
    from companion.team_transport import config_lanes, validate_config
    validate_config(config)
    binding, envelope = config['controller'], state['envelope']
    if (binding['version'] != state['version'] or binding['task_id'] not in envelope['task_ids']
            or any(lane['workspace'] not in [envelope['repo'], *envelope.get('workspaces', [])]
                   or any({'app': app, **pin} not in envelope['chats'] for app, pin in lane['participants'].items())
                   for lane in config_lanes(config))
            or f'use_{role or STAGES[stage][0]}' not in envelope['actions']):
        return 'Controller binding changed or unsupported', False
    if now >= envelope['expires_at']:
        return 'Controller authorization expired', False
    if state['checks'] >= envelope['max_checks']:
        return 'Controller check limit reached', False
    return CONTROLLER_LABELS[state['status']], state['status'] == 'running'


def _saved_claim_matches(claim, run):
    config = run.get('submitted_config')
    if not config or config.get('version') not in (2, 3):
        return False
    lane = run.get('lane') or config
    pins = lane.get('participants', {})
    role = _stage_role(run, claim['stage'])
    return (claim['owner'] == 'team' and claim['run_id'] == run['id']
            and claim['task_id'] == config['controller']['task_id']
            and claim['version'] == config['controller']['version']
            and claim['repo'] == run.get('workspace', lane['workspace'])
            and (claim['participants'] == {role: pins.get(role)} or claim['participants'] == pins))


def _stage_role(run, stage=None):
    stage = stage or run['stage']
    if stage == 'build':
        return run.get('builder', 'codex')
    if stage == 'review' and run.get('execution_mode') != 'review_only':
        return run.get('reviewer', 'claude')
    return STAGES[stage][0]


def _run_binding(run):
    from companion.team_transport import lane_config
    config = run.get('submitted_config')
    binding = lane_config(config, run['lane']) if config and run.get('lane') else config
    return {**binding, 'workspace': run['workspace']} if binding and run.get('workspace') else binding


def _text(value, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\0' in value:
        raise ValueError('Invalid or oversized text')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Invalid Unicode text') from None
    return value.strip()


_HANDBOOK_PATH = Path(__file__).resolve().parents[2] / 'docs' / 'team-handbook.md'
_HANDBOOK_CACHE = {}


def team_handbook():
    """Load the repo-owned handbook; invalidate the one-entry cache on edits."""
    from companion import team_connected
    if hasattr(team_connected, 'team_handbook'):
        return team_connected.team_handbook()
    path = _HANDBOOK_PATH
    stamp = path.stat().st_mtime_ns
    key = (path, stamp)
    if key not in _HANDBOOK_CACHE:
        content = _text(path.read_text(encoding='utf-8'), 32000)
        _HANDBOOK_CACHE.clear()
        _HANDBOOK_CACHE[key] = content
    return _HANDBOOK_CACHE[key]


def _task_workspace(run_id, binding):
    """Create/reuse the task branch; dispatch must separately authorize this path."""
    from companion.team_workspace import task_worktree
    try:
        return Path(task_worktree(run_id, base='origin/master', root=binding['workspace']))
    except RuntimeError as exc:
        raise ValueError('task_workspace_unavailable') from exc


def _role_line(run):
    role = 'planner' if run['stage'] == 'plan' else 'reviewer' if run['stage'] == 'review' else 'builder'
    try:
        from companion.team_workspace import role_line
    except ModuleNotFoundError as exc:
        if exc.name != 'companion.team_workspace':
            raise
        return f'You are the {role} for this Team assignment. Follow the assigned role and Team handbook.'
    return role_line((run.get('lane') or {}).get('id', 'assigned'), role)


def _planner_tasks(value):
    tasks, kind = value['tasks'], value.get('kind', 'plan')
    if not isinstance(kind, str) or kind not in {'answer', 'task', 'plan'} or not isinstance(tasks, list):
        raise ValueError('Invalid planner decision')
    modern = 'kind' in value
    if ((kind == 'answer' and tasks) or (kind == 'task' and len(tasks) != 1)
            or (kind == 'plan' and not 1 <= len(tasks) <= (8 if modern else 3))):
        raise ValueError('Invalid planner task count')
    providers = set()
    for task in tasks:
        keys = {'provider', 'title', 'instruction'} | ({'order'} if modern else set())
        if modern and isinstance(task, dict) and 'mode' in task:
            keys.add('mode')
        if isinstance(task, dict) and 'acceptance' in task:
            obligations.items(task['acceptance'])
            keys.add('acceptance')
        if (not isinstance(task, dict) or set(task) != keys
                or not isinstance(task['provider'], str) or task['provider'] not in PLAN_PROVIDERS
                or (not modern and task['provider'] != 'codex' and task['provider'] in providers)):
            raise ValueError('Invalid or repeated plan provider')
        providers.add(task['provider'])
        _text(task['title'], 160)
        _text(task['instruction'], 8000)
        if modern and (type(task['order']) is not int or not 1 <= task['order'] <= 8
                       or task.get('mode', 'reply') not in ('reply', 'code')):
            raise ValueError('Invalid task order or mode')
    return tasks


def validate_result(value, *, run_id, stage, attempt, revision=None, execution_mode='plan', workspace=None):
    """Only a bounded, explicitly terminal envelope can advance a stage."""
    base = {'run_id', 'stage', 'attempt', 'status', 'summary'}
    if (not isinstance(value, dict) or not base <= value.keys()
            or value['run_id'] != run_id or value['stage'] != stage
            or type(value['attempt']) is not int or value['attempt'] != attempt
            or value['status'] not in ('completed', 'needs_input')):
        raise ValueError('Result identity or terminal status does not match')
    _text(value['summary'])
    base.update(obligations.metadata(value))
    if 'files' in value:
        from companion.team_workspace import validate_files
        if stage not in {'build', 'review'} or execution_mode in SINGLE_STAGES:
            raise ValueError('File outputs require a code task')
        validate_files(value['files'], workspace)
        base.add('files')
    if 'chat' in value:
        base.add('chat')
    if 'requires_owner_decision' in value:
        flag = value['requires_owner_decision']
        if type(flag) is not bool or (flag and value['status'] != 'needs_input'
                                      and value.get('verdict') != 'needs_input'):
            raise ValueError('Invalid owner decision flag')
        base.add('requires_owner_decision')
    if (execution_mode not in EXECUTION_MODES or stage not in STAGES
            or (execution_mode in SINGLE_STAGES and stage != SINGLE_STAGES[execution_mode])
            or (execution_mode not in SINGLE_STAGES and stage.startswith('reply_'))
            or (execution_mode == 'code_review' and stage != 'review')):
        raise ValueError('Result stage does not match the execution mode')
    if execution_mode in SINGLE_STAGES:
        expected = base
        if value['status'] == 'completed':
            if execution_mode == 'plan_only':
                expected = base | {'tasks'} | ({'kind'} if 'kind' in value else set())
            elif execution_mode == 'review_only':
                expected = base | {'verdict'}
            elif execution_mode == 'interview_only':
                expected = base | {'ready'} | ({'kind', 'tasks'} if 'kind' in value else set())
        if set(value) != expected:
            raise ValueError('Result fields do not match the execution mode')
        if value['status'] == 'completed' and execution_mode == 'plan_only':
            _planner_tasks(value)
        if (value['status'] == 'completed' and execution_mode == 'review_only'
                and value['verdict'] not in ('pass', 'changes_requested', 'needs_input')):
            raise ValueError('Invalid report review verdict')
        if value['status'] == 'completed' and execution_mode == 'interview_only' and type(value['ready']) is not bool:
            raise ValueError('Invalid interview flag')
        if value['status'] == 'completed' and execution_mode == 'interview_only' and 'kind' in value:
            if value['kind'] != 'answer':
                raise ValueError('An interview cannot propose work')
            if value['ready'] is not False:
                raise ValueError('An interview answer cannot request approval')
            _planner_tasks(value)
        return deepcopy(value)
    expected = base if value['status'] == 'needs_input' else base | {
        'plan': {'tasks'}, 'build': {'revision'}, 'review': {'revision', 'verdict'}
    }[stage]
    if set(value) != expected:
        raise ValueError('Result fields do not match the stage')
    if value['status'] == 'needs_input':
        return deepcopy(value)
    if stage == 'plan':
        tasks = value['tasks']
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 4:
            raise ValueError('A plan needs one to four bounded tasks')
        for task in tasks:
            if not isinstance(task, dict) or set(task) - {'acceptance'} != {'title', 'instruction'}:
                raise ValueError('Invalid plan task')
            _text(task['title'], 160)
            _text(task['instruction'], 8000)
            if 'acceptance' in task:
                obligations.items(task['acceptance'])
    else:
        if (not isinstance(value['revision'], str)
                or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value['revision'])):
            raise ValueError('Invalid revision')
        if revision is not None and value['revision'] != revision:
            raise ValueError('The reviewed revision changed')
        if stage == 'review' and value['verdict'] not in ('pass', 'changes_requested', 'needs_input'):
            raise ValueError('Invalid review verdict')
    return deepcopy(value)


def _message(room, author, text, **metadata):
    room['messages'].append(dict(id=uuid.uuid4().hex, author=author, text=text,
                                 created_at=time.time(), **metadata))
    room['room']['updated_at'] = time.time()


def _finish(run, status, reason, *, release=True):
    run.update(status=status, reason=reason, phase='terminal')
    if release:
        run.update(role_lease=None, write_lease=False, release_outcome=status)


def _pending_claim_uncertain(run, claims):
    """Classify a pending row against a controller-locked claim snapshot, without I/O."""
    if run['status'] not in ACTIVE or run['phase'] != 'pending':
        return False
    matches = [c for c in claims if c['owner'] == 'team' and c['run_id'] == run['id']]
    return len(matches) > 1 or bool(matches and matches[0] != run.get('claim'))


class TeamStateError(ValueError):
    """Saved state needs operator inspection; never a normal request conflict."""


class TeamCapacityError(ValueError):
    """New admission cannot consume the fixed completion reserve."""


def serialized_size(data):
    return len(json.dumps(data, indent=2).encode('utf-8'))


def _hex(value, length=32):
    return isinstance(value, str) and re.fullmatch(f'[a-f0-9]{{{length}}}', value) is not None


def _receipt_id(value):
    return value is None or _hex(value)


def validate_lead_binding(binding):
    """Only trusted, bounded metadata; this does not authorize a native worker."""
    if (not isinstance(binding, dict)
            or set(binding) != {'project_id', 'project_sha256', 'model_id', 'sources'}
            or not _hex(binding['project_sha256'], 64)):
        raise ValueError('Invalid local project binding')
    _text(binding['project_id'], 128)
    _text(binding['model_id'], 128)
    _validate_answer_sources(binding['sources'])
    if serialized_size(binding) > 16 * 1024:
        raise ValueError('Local project binding is too large')


def _validate_answer_sources(sources):
    if not isinstance(sources, list) or len(sources) > 8:
        raise ValueError('Invalid local sources')
    ids = set()
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'id', 'kind', 'ref', 'sha256'}
                or source['kind'] not in {'preference', 'project', 'note'}
                or not _hex(source['sha256'], 64)):
            raise ValueError('Invalid local source')
        _text(source['id'], 128)
        _text(source['ref'], 256)
        if source['id'] in ids:
            raise ValueError('Duplicate local source')
        ids.add(source['id'])


def validate_answer(message):
    keys = {'id', 'author', 'kind', 'created_at', 'text', 'request_id', 'reply_to',
            'run_id', 'shared', 'status', 'stop_requested', 'attempt_id', 'binding',
            'sources', 'route', 'model', 'notice', 'started_at', 'finished_at',
            'omitted_counts', 'missing'}
    if (set(message) != keys or not _hex(message['id']) or not _hex(message['reply_to'])
            or message['author'] != 'kyra' or message['kind'] != 'answer'
            or message['run_id'] is not None or message['shared'] is not False
            or message['status'] not in ANSWER_STATES or type(message['stop_requested']) is not bool
            or message['notice'] not in ANSWER_NOTICES
            or (message['attempt_id'] is not None and not _hex(message['attempt_id']))):
        raise ValueError('Invalid local answer')
    _text(message['request_id'], 128)
    validate_lead_binding(message['binding'])
    if message['model'] != message['binding']['model_id']:
        raise ValueError('Invalid local model')
    _validate_answer_sources(message['sources'])
    allowed = message['binding']['sources']
    if any(source not in allowed for source in message['sources']):
        raise ValueError('Invalid local citations')
    counts = message['omitted_counts']
    if (not isinstance(counts, dict) or set(counts) != {'sources', 'history'}
            or type(counts['sources']) is not int or not 0 <= counts['sources'] <= len(allowed)
            or type(counts['history']) is not int or not 0 <= counts['history'] <= 500):
        raise ValueError('Invalid local omissions')
    for field in ('created_at', 'started_at', 'finished_at'):
        value = message[field]
        if value is None and field != 'created_at':
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError('Invalid local timestamp')
    if message['status'] == 'answered':
        _text(message['text'], 4000)
        if type(message['missing']) is not bool or message['route'] not in ANSWER_ROUTES:
            raise ValueError('Invalid local answer route')
    elif message['text'] != '' or message['sources'] or message['route'] is not None or message['missing'] is not None:
        raise ValueError('Unsettled local output')
    if serialized_size(message) > LOCAL_COMPLETION_BYTES:
        raise ValueError('Local answer exceeds reserve')


STEP_KINDS = {'assigned', 'dispatched', 'asked', 'returned', 'reviewed', 'ready', 'stopped'}


def validate_step(message):
    required = {'id', 'author', 'kind', 'text', 'step', 'created_at'}
    if (not isinstance(message, dict) or not required <= message.keys()
            or not set(message) <= required | {'run_id', 'parent_request_id', 'details_id', 'hidden'}
            or not _hex(message['id']) or message['author'] != 'kyra' or message['kind'] != 'step'
            or not isinstance(message['step'], str) or message['step'] not in STEP_KINDS):
        raise ValueError('Invalid step message')
    text = message['text']
    if (not isinstance(text, str) or not text.strip() or len(text) > 320
            or not text.isprintable()):
        raise ValueError('Step text must be one short printable line')
    stamp = message['created_at']
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= 1e12:
        raise ValueError('Invalid step timestamp')
    if 'run_id' in message and not _hex(message['run_id']):
        raise ValueError('Invalid step run')
    if 'parent_request_id' in message and (
            not isinstance(message['parent_request_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', message['parent_request_id'])):
        raise ValueError('Invalid step parent')


def validate_report(message):
    required = {'id', 'author', 'kind', 'text', 'created_at'}
    if (not isinstance(message, dict) or not required <= message.keys()
            or not set(message) <= required | {'run_id', 'parent_request_id', 'details_id', 'hidden'}
            or message['author'] != 'claude' or message['kind'] != 'report'):
        raise ValueError('Invalid report message')
    text = message['text']
    if (not isinstance(text, str) or not text.strip() or len(text) > 4000
            or text.count('\n') >= 30 or any(c != '\n' and not c.isprintable() for c in text)):
        raise ValueError('Report text must be at most 4000 characters and 30 printable lines')
    # Reuse the fixed identity/timestamp validation without relaxing step text.
    validate_step({**message, 'kind': 'step', 'author': 'kyra', 'step': 'ready', 'text': 'Report'})


def local_answers(data):
    return [(room_id, message) for room_id, room in data['rooms'].items()
            for message in room['messages'] if message.get('kind') == 'answer']


def _local_credit(data):
    return sum(m.get('status') in ANSWER_ACTIVE for _, m in local_answers(data)) * LOCAL_COMPLETION_BYTES


def _fixed_schema(data):
    """Validate the bounded writer schema, not arbitrary hand-edited state."""
    from companion.team_transport import validate_config
    from companion.workflow_control import CLAIM_KEYS

    if set(data) != {'version', 'controller_path', 'rooms'} or data['version'] != 2:
        raise TeamCapacityError('Legacy Team state has no reserve; new work requires a fresh v2 root.')
    if not isinstance(data['controller_path'], str) or not Path(data['controller_path']).is_absolute():
        raise ValueError('Invalid namespace')
    run_keys = {'id', 'status', 'stage', 'phase', 'attempt', 'reason', 'created_at', 'expires_at',
                'stop_requested', 'role_lease', 'write_lease', 'tasks', 'revision', 'submitted_config',
                'binding', 'claim', 'dispatch_pid', 'assignment_path', 'result_path', 'delivery_id',
                'release_outcome', 'reconciled_at', 'learning', 'input_message_id', 'execution_mode', 'lane', 'parent_request_id', 'planner_kind', 'acknowledged_at', 'interview_ready', 'requires_owner_decision', 'builder', 'reviewer', 'workspace', 'workspace_run_id', 'files', 'verdict', 'completion', 'open_items', 'wait_started_at', 'claim_released', 'precondition_wait'}
    message_keys = {'id', 'author', 'text', 'created_at', 'request_id', 'run_id', 'shared', 'kind', 'stage', 'assurance', 'hidden', 'details_id'}
    ids = set()
    def number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
            raise ValueError('Unbounded scalar')
    def text(value, limit):
        _text(value, limit)
    for key, room in data['rooms'].items():
        meta = room['room']
        if (not _hex(key) or set(room) != {'room', 'messages', 'runs'}
                or set(meta) != {'id', 'title', 'local_only', 'created_at', 'updated_at'}
                or meta['id'] != key or type(meta['local_only']) is not bool):
            raise ValueError('Unknown room metadata')
        text(meta['title'], 100)
        number(meta['created_at'])
        number(meta['updated_at'])
        for message in room['messages']:
            if 'hidden' in message and type(message['hidden']) is not bool:
                raise ValueError('Invalid hidden flag')
            if 'details_id' in message and not _hex(message['details_id']):
                raise ValueError('Invalid details id')
            if message.get('kind') == 'report':
                validate_report(message)
                continue
            if message.get('kind') == 'step':
                validate_step(message)
                continue
            if message.get('kind') == 'answer':
                validate_answer(message)
                parents = [m for m in room['messages'] if m['id'] == message['reply_to']]
                if (len(parents) != 1 or parents[0]['author'] not in {'user', 'system'}
                        or parents[0].get('shared') is not False
                        or parents[0].get('request_id') != message['request_id']):
                    raise ValueError('Invalid local answer parent')
                continue
            if (not set(message) <= message_keys or not _hex(message['id'])
                    or message['author'] not in {'user', 'system', *PLAN_PROVIDERS}):
                raise ValueError('Unknown message metadata')
            text(message['text'], MAX_TEXT)
            number(message['created_at'])
            if message.get('run_id') is not None and not _hex(message['run_id']):
                raise ValueError('Invalid message run')
            if 'request_id' in message:
                text(message['request_id'], 128)
            if 'shared' in message and type(message['shared']) is not bool:
                raise ValueError('Invalid sharing')
            if 'stage' in message and message['stage'] not in STAGES:
                raise ValueError('Invalid stage')
            if 'kind' in message and message['kind'] != 'result':
                raise ValueError('Invalid message kind')
            if 'assurance' in message:
                text(message['assurance'], 200)
        for run in room['runs']:
            if (not set(run) <= run_keys or not _hex(run['id']) or run['id'] in ids
                    or run['status'] not in STATES or run['stage'] not in STAGES
                    or run['phase'] not in {'pending', 'dispatching', 'waiting', 'terminal'}
                    or type(run['attempt']) is not int or not 0 <= run['attempt'] <= 2
                    or type(run['stop_requested']) is not bool or type(run['write_lease']) is not bool
                    or run['role_lease'] not in (None, 'claude', 'codex')):
                raise ValueError('Unknown run metadata')
            if 'claim_released' in run and (run['claim_released'] is not True or not run.get('claim')):
                raise ValueError('Invalid released stage claim')
            if 'open_items' in run:
                entries = run['open_items']
                if not isinstance(entries, list) or len(entries) > 128:
                    raise ValueError('Invalid open work queue')
                for item in entries:
                    if (not isinstance(item, dict) or set(item) - {'text', 'owner', 'state', 'due_at', 'nudged', 'kind', 'expires_at'} or not {'text', 'owner', 'state', 'due_at', 'nudged', 'kind'} <= set(item)
                            or not isinstance(item['owner'], str) or not 1 <= len(item['owner']) <= 120
                            or not item['owner'].isprintable() or (item['kind'] != 'handoff' and item['owner'] not in PLAN_PROVIDERS)
                            or item['state'] not in {'queued', 'completed', 'expired'}
                            or item['kind'] not in {'acceptance', 'blocker', 'handoff'} or type(item['nudged']) is not bool
                            or not isinstance(item['due_at'], (int, float)) or not math.isfinite(item['due_at'])):
                        raise ValueError('Invalid open work item')
                    text(item['text'], 1500)
                    if 'expires_at' in item:
                        number(item['expires_at'])
            if 'precondition_wait' in run and run['precondition_wait'] is not True:
                raise ValueError('Invalid precondition wait')
            if 'completion' in run:
                if not isinstance(run['completion'], dict) or set(run['completion']) - set(obligations.FIELDS):
                    raise ValueError('Invalid saved completion metadata')
                obligations.metadata(run['completion'])
            if 'verdict' in run and run['verdict'] not in {'pass', 'changes_requested', 'needs_input'}:
                raise ValueError('Invalid saved review verdict')
            if run.get('execution_mode', 'plan') not in EXECUTION_MODES:
                raise ValueError('Invalid execution mode')
            if (run.get('builder', 'codex') not in {'claude', 'codex', 'cursor', 'antigravity'}
                    or run.get('reviewer', 'claude') not in {'claude', 'codex'}
                    or run.get('builder', 'codex') == run.get('reviewer', 'claude')):
                raise ValueError('Invalid independent reviewer')
            if 'workspace' in run and (not isinstance(run['workspace'], str) or len(run['workspace']) > 4096
                                       or not Path(run['workspace']).is_absolute()):
                raise ValueError('Invalid task workspace')
            if 'workspace_run_id' in run and not _hex(run['workspace_run_id']):
                raise ValueError('Invalid workspace run')
            if 'files' in run and (not isinstance(run['files'], list) or len(run['files']) > 32
                                  or any(not isinstance(f, str) or len(f) > 256 for f in run['files'])):
                raise ValueError('Invalid saved files')
            if 'parent_request_id' in run:
                if (run.get('execution_mode') not in {'worker', 'code_review', *SINGLE_STAGES} or not isinstance(run['parent_request_id'], str)
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', run['parent_request_id'])):
                    raise ValueError('Invalid worker parent')
            ids.add(run['id'])
            if 'input_message_id' in run and not _hex(run['input_message_id']):
                raise ValueError('Invalid selected instruction')
            text(run['reason'], MAX_TEXT)
            for field in ('created_at', 'expires_at', 'reconciled_at', 'acknowledged_at', 'wait_started_at'):
                if field in run:
                    number(run[field])
            if 'dispatch_pid' in run and (type(run['dispatch_pid']) is not int or not 0 < run['dispatch_pid'] < 2**31):
                raise ValueError('Invalid sender')
            if not _receipt_id(run.get('delivery_id')) or not _hex(run['binding'], 64):
                raise ValueError('Invalid delivery metadata')
            if run.get('revision') is not None and not (_hex(run['revision'], 40) or _hex(run['revision'], 64)):
                raise ValueError('Invalid revision')
            if 'release_outcome' in run and run['release_outcome'] not in {'completed', 'needs_input', 'stopped', 'operator_reconciled'}:
                raise ValueError('Invalid release')
            if run.get('submitted_config') is not None:
                validate_config(run['submitted_config'])
            if 'lane' in run:
                from companion.team_transport import config_lanes
                if not run.get('submitted_config') or run['lane'] not in config_lanes(run['submitted_config']):
                    raise ValueError('Lane outside submitted configuration')
            if 'requires_owner_decision' in run and type(run['requires_owner_decision']) is not bool:
                raise ValueError('Invalid owner decision flag')
            if 'interview_ready' in run and type(run['interview_ready']) is not bool:
                raise ValueError('Invalid interview flag')
            if 'claim' in run and (not isinstance(run['claim'], dict) or set(run['claim']) != CLAIM_KEYS):
                raise ValueError('Invalid recorded claim')
            for field in ('assignment_path', 'result_path'):
                if field in run:
                    text(run[field], 4096)
            if 'learning' in run:
                if set(run['learning']) != {'status', 'reason'} or run['learning']['status'] != 'none':
                    raise ValueError('Unknown learning metadata')
                text(run['learning']['reason'], 200)
            if not isinstance(run['tasks'], list) or len(run['tasks']) > (8 if run.get('execution_mode') == 'plan_only' else 4):
                raise ValueError('Invalid task count')
            plan_only = run.get('execution_mode') == 'plan_only'
            if 'planner_kind' in run and (not isinstance(run['planner_kind'], str) or run['planner_kind'] not in {'answer', 'task', 'plan'}):
                raise ValueError('Invalid planner kind')
            if (run.get('execution_mode') == 'interview_only' and 'planner_kind' in run
                    and (run['planner_kind'] != 'answer' or run['tasks'])):
                raise ValueError('An interview answer cannot contain work')
            for index, task in enumerate(run['tasks']):
                optional = ({'acceptance'} if 'acceptance' in task else set()) | ({'order'} if 'order' in task else set()) | ({'mode'} if 'mode' in task else set())
                if (set(task) != {'id', 'assignee', 'status', 'title', 'instruction'} | optional
                        or task['id'] != f'{run["id"]}-{index}'
                        or task['assignee'] not in (PLAN_PROVIDERS if plan_only else {'codex', 'claude'})
                        or task['status'] not in {'queued', 'building', 'reviewing', 'completed'}):
                    raise ValueError('Unknown task metadata')
                text(task['title'], 160)
                text(task['instruction'], 8000)
                if 'acceptance' in task:
                    obligations.items(task['acceptance'])
            if plan_only and run['tasks']:
                decision = {'tasks': [{('provider' if k == 'assignee' else k): v for k, v in t.items()
                                       if k not in {'id', 'status'}} for t in run['tasks']]}
                if 'planner_kind' in run:
                    decision['kind'] = run['planner_kind']
                _planner_tasks(decision)



def require_admission(before, after, *, claims, candidate_claim=None):
    """Check both crash and normal baselines against the fixed completion envelope."""
    current = list(claims)
    if candidate_claim is not None:
        current = [c for c in current if c['id'] != candidate_claim['id']] + [candidate_claim]
    try:
        if before.get('controller_path') != after.get('controller_path'):
            raise TeamCapacityError('Team storage location changed; restore its controller binding on the Mac before retrying.')
        for data in (before, after):
            _fixed_schema(data)
            baseline = serialized_size(data)
            answers = [m for _, m in local_answers(data)]
            if sum(m['status'] == 'queued' for m in answers) > 2:
                raise TeamCapacityError('More than 2 local answers are queued; try again when one finishes.')
            if sum(m['status'] == 'running' for m in answers) > 1:
                raise TeamCapacityError('More than 1 local answer is running; wait for it to finish or reconcile the worker on the Mac.')
            if baseline + _local_credit(data) > ADMISSION_LIMIT_BYTES:
                raise TeamCapacityError(CAPACITY_REASON)
            controls = slots = 0
            for room in data['rooms'].values():
                for run in room['runs']:
                    matches = [c for c in current if c['owner'] == 'team' and c['run_id'] == run['id']]
                    if len(matches) > 1:
                        raise TeamCapacityError('A Team run has multiple recovery claims; reconcile its workers on the Mac before retrying.')
                    held = bool(matches)
                    controls += bool(run['status'] in ACTIVE | {'uncertain'} or run['role_lease'] or run['write_lease'] or held)
                    slots += bool(run['status'] == 'uncertain' or run['phase'] in {'waiting', 'dispatching'}
                                  or (held and matches[0] != run.get('claim')))
                    if held and matches[0] != run.get('claim'):
                        copy = deepcopy(data)
                        _, copied_run = TeamStore.find_run(copy, run['id'])
                        copied_run['claim'] = matches[0]
                        if serialized_size(copy) - baseline > 65536:
                            raise TeamCapacityError('A Team recovery claim is too large; inspect its controller binding on the Mac before retrying.')
            if controls > 100:
                raise TeamCapacityError('More than 100 Team runs are unsettled; finish or reconcile existing runs before starting more.')
            if slots > 2:
                raise TeamCapacityError('More than 2 Team runs are waiting on replies or recovery; try again when one finishes, or reconcile uncertain runs on the Mac.')
    except TeamCapacityError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise TeamCapacityError('Team state is invalid; inspect and repair its metadata on the Mac before retrying.') from None


class TeamStore:
    """One locked local JSON record; explicit roots also support hermetic tests."""
    def __init__(self, root=None, *, controller=None, jobs=None):
        from companion.workflow_control import WorkflowStore
        self.root = Path(root) if root is not None else DATA_DIR / 'team'
        self.jobs = jobs  # job queue for the end-of-session honesty check; None means the app's default queue
        self.path = self.root / 'state.json'
        self.controller = WorkflowStore() if controller is None else controller

    def note_preview(self, room_id, message_id):
        from companion.team_notes import preview
        return preview(self, get_settings().obsidian_vault, DATA_DIR, room_id, message_id)

    def note_confirm(self, room_id, message_id, *, expected_note_sha256):
        from companion.team_notes import confirm
        return confirm(self, get_settings().obsidian_vault, DATA_DIR, room_id, message_id, expected_note_sha256)

    def check_namespace(self, data=None, *, controller=None):
        """Check provenance before any registry read, including failure/recovery paths."""
        data = self._read() if data is None else data
        controller = self.controller if controller is None else controller
        if data['version'] == 2 and Path(data['controller_path']).resolve() != controller.path.resolve():
            raise TeamStateError('Team controller namespace does not match')

    @contextmanager
    def admission(self):
        with self.controller._locked():
            with self.edit() as data:
                self.check_namespace(data)
                claims = self.controller._claims()['claims']
                before = deepcopy(data)
                yield data
                if data != before:
                    require_admission(before, data, claims=claims)

    def _read(self):
        if not self.path.exists():
            return {'version': 2, 'controller_path': str(self.controller.path.resolve()), 'rooms': {}}
        if self.path.is_symlink() or self.path.stat().st_size > MAX_STORE_BYTES:
            raise TeamStateError('Invalid team store')
        try:
            value = json.loads(self.path.read_text())
        except (ValueError, UnicodeError):
            raise TeamStateError('Invalid team store') from None
        try:
            keys = {'version', 'rooms'} | ({'controller_path'} if value.get('version') == 2 else set())
            if (set(value) != keys or type(value['version']) is not int or value['version'] not in (1, 2)
                    or not isinstance(value['rooms'], dict)):
                raise ValueError
            for key, room in value['rooms'].items():
                if set(room) != {'room', 'messages', 'runs'} or room['room']['id'] != key:
                    raise ValueError
                if not isinstance(room['messages'], list) or not isinstance(room['runs'], list):
                    raise ValueError
                for run in room['runs']:
                    if run['status'] not in STATES or run['stage'] not in STAGES:
                        raise ValueError
                    for required in ('id', 'phase', 'attempt', 'role_lease', 'write_lease', 'expires_at', 'stop_requested'):
                        if required not in run:
                            raise ValueError
        except (KeyError, TypeError, ValueError):
            raise TeamStateError('Invalid team store') from None
        return value

    def status_snapshot(self, *, now=None):
        """Project saved owner status without locks, writes, discovery or an authority grant.

        Save times are independent observations, not a transactional snapshot or
        worker heartbeat. A historical run.claim alone is never a held claim.
        """
        result = empty_status_snapshot(now=now)

        def notice(code):
            if not any(item['code'] == code for item in result['notices']):
                result['notices'].append(dict(code=code, text=STATUS_NOTICES[code]))

        try:
            if not self.path.exists() and not self.path.is_symlink():
                return result
            data = self._read()
            result['team_saved_at'] = _saved_at(self.path)
            rows, ids = [], set()
            for room_id, room in data['rooms'].items():
                if not _hex(room_id):
                    raise ValueError('Invalid room id')
                title = _text(room['room']['title'], 100)
                for run in room['runs']:
                    if not _hex(run['id']) or run['id'] in ids:
                        raise ValueError('Invalid run id')
                    if run.get('execution_mode', 'plan') not in EXECUTION_MODES:
                        raise ValueError('Invalid execution mode')
                    ids.add(run['id'])
                    rows.append((room_id, title, run))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            notice('team_unavailable')
            return result

        if serialized_size(data) + _local_credit(data) > ADMISSION_LIMIT_BYTES:
            notice('team_capacity')
        joined = data['version'] == 2 and Path(data['controller_path']).resolve() == self.controller.path.resolve()
        if data['version'] == 1:
            notice('team_legacy')
        elif not joined:
            notice('controller_namespace')
        state, claims = None, None
        if joined:
            try:
                if self.controller.path.is_symlink():
                    raise ValueError('Invalid controller')
                state = self.controller._read()
                if state['status'] == 'unconfigured':
                    state = None
                    notice('controller_unavailable')
                else:
                    result['controller_saved_at'] = _saved_at(self.controller.path)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                state = None
                notice('controller_unavailable')
            try:
                registry = self.controller.read_existing_claims()
                if registry is not None:
                    claims = registry['claims']
                    result['claims_saved_at'] = _saved_at(self.controller.path.parent / 'claims.json')
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
                notice('claims_unavailable')
            if claims is None and any(run.get('claim') or run['phase'] in {'waiting', 'dispatching'}
                                      for _, _, run in rows):
                notice('claims_unavailable')

        for room_id, title, run in rows:
            try:
                label, supported = _saved_controller_label(state, run.get('submitted_config'),
                                                           run['stage'], result['read_at'], _stage_role(run))
                held = claims is not None and any(_saved_claim_matches(c, run) for c in claims)
            except (ValueError, TypeError, KeyError, AttributeError):
                label, supported, held = 'Controller status unknown', False, False
                notice('team_unavailable')
            lease = bool(run['role_lease'] or run['write_lease'])
            terminal = run['status'] in {'completed', 'stopped'}
            if terminal and not (held or lease):
                continue
            claim_label = 'Saved shared claim retained' if held else 'Saved local lease retained' if lease else ''
            if claims is None and (run.get('claim') or run['phase'] in {'waiting', 'dispatching'}):
                claim_label = 'Shared claim unconfirmed' + ('; saved local lease retained' if lease else '')
            saved_reason = run.get('reason')
            if not isinstance(saved_reason, str):
                saved_reason = None
            reason = ('A finished run still holds a saved claim or lease; reconcile it on the Mac.' if terminal
                      else REASON_LABELS.get(saved_reason, 'Saved Team status; open the chat for details.'))
            result['runs'].append(dict(key=f"team:{run['id']}", room_id=room_id, run_id=run['id'],
                title=title, status=run['status'], stage=run['stage'], reason_label=reason,
                controller_label=label, claim_label=claim_label,
                attention=terminal or run['status'] in {'needs_input', 'uncertain'} or not supported))
        return result

    @contextmanager
    def edit(self):
        if self.root.is_symlink():
            raise ValueError('Team store cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.lock').open('a') as lock, exclusive(lock):
            value = self._read()
            before = deepcopy(value)
            yield value
            if value != before:
                if value['version'] != before['version'] or value.get('controller_path') != before.get('controller_path'):
                    raise TeamStateError('Team state provenance is immutable')
                if serialized_size(value) > MAX_STORE_BYTES:
                    raise ValueError('Team storage limit reached; preserve and archive old chats on the Mac')
                write_json(self.path, value)
                self.path.chmod(0o600)

    @staticmethod
    def find_run(value, run_id):
        for room in value['rooms'].values():
            for run in room['runs']:
                if run['id'] == run_id:
                    return room, run
        raise LookupError('Run not found')

    def create_room(self, title, local_only=False):
        title = _text(title, 100)
        if type(local_only) is not bool:
            raise ValueError('Invalid room privacy')
        with self.admission() as data:
            if len(data['rooms']) >= 100:
                raise ValueError('Room limit reached')
            room = dict(id=uuid.uuid4().hex, title=title, local_only=local_only,
                        created_at=time.time(), updated_at=time.time())
            data['rooms'][room['id']] = {'room': room, 'messages': [], 'runs': []}
        return room

    def post_step(self, room_id, step, text, *, event_key, parent_request_id=None, run_id=None, refresh=False):
        """Publish once, or refresh the same waiting notice at most every 15 minutes."""
        event_key = _text(event_key, 256)
        ident = hashlib.sha256(json.dumps([room_id, event_key], separators=(',', ':')).encode()).hexdigest()[:32]
        message = dict(id=ident, kind='step', author='kyra', text=text, step=step, created_at=time.time())
        if parent_request_id is not None:
            message['parent_request_id'] = parent_request_id
        if run_id is not None:
            message['run_id'] = run_id
        validate_step(message)
        with self.admission() as data:
            room = data['rooms'].get(room_id)
            if room is None:
                raise LookupError('Room not found')
            if room['room']['local_only']:
                raise ValueError('Local-only chats cannot receive Team steps')
            prior = next((m for m in room['messages'] if m['id'] == ident), None)
            if prior is not None:
                if refresh and message['created_at'] - prior['created_at'] >= 900:
                    prior.update(text=text, created_at=message['created_at'])
                    room['room']['updated_at'] = message['created_at']
                return deepcopy(prior)
            # Like agent results, progress for accepted work may cross the room's
            # new-input limit; admission still protects the byte reserve.
            room['messages'].append(message)
            room['room']['updated_at'] = message['created_at']
        return deepcopy(message)

    def post_report(self, room_id, text, *, event_key, parent_request_id=None, run_id=None):
        """Publish one planner report; no execution or merge authority is attached."""
        event_key = _text(event_key, 256)
        ident = hashlib.sha256(json.dumps(['report', room_id, event_key], separators=(',', ':')).encode()).hexdigest()[:32]
        message = dict(id=ident, kind='report', author='claude', text=text, created_at=time.time())
        if parent_request_id is not None:
            message['parent_request_id'] = parent_request_id
        if run_id is not None:
            message['run_id'] = run_id
        validate_report(message)
        with self.admission() as data:
            room = data['rooms'].get(room_id)
            if room is None:
                raise LookupError('Room not found')
            if room['room']['local_only']:
                raise ValueError('Local-only chats cannot receive Team reports')
            prior = next((m for m in room['messages'] if m['id'] == ident), None)
            if prior is not None:
                return deepcopy(prior)
            # Completion output keeps the byte/reserve checks, not the input cap.
            room['messages'].append(message)
            room['room']['updated_at'] = message['created_at']
        from companion import team_honesty
        team_honesty.after_report(self.root, self.jobs)  # a posted report ends a session; never raises
        return deepcopy(message)

    def get_room(self, room_id):
        value = self._read()['rooms'].get(room_id)
        if value is None:
            raise LookupError('Room not found')
        return deepcopy(value)

    def list_rooms(self):
        return sorted([{**room['room'], 'status': room['runs'][-1]['status'] if room['runs'] else 'idle'}
                       for room in self._read()['rooms'].values()], key=lambda room: room['updated_at'], reverse=True)

    def submit(self, room_id, text, request_id, share_with_team=True, execution_mode="plan", *, parent_request_id=None, author="user", input_message_id=None, builder="codex", reviewer="claude", workspace=None, workspace_run_id=None, revision=None, files=None):
        if (builder not in {'codex', 'claude', 'cursor', 'antigravity'} or reviewer not in {'claude', 'codex'}
                or builder == reviewer or (execution_mode != 'code_review' and builder not in {'codex', 'claude'})):
            raise ValueError('Invalid builder or independent reviewer')
        if execution_mode == 'code_review':
            from companion.team_workspace import validate_files
            if (not workspace or not Path(workspace).is_absolute() or not _hex(workspace_run_id)
                    or not (_hex(revision, 40) or _hex(revision, 64))):
                raise ValueError('Code review needs its task workspace and revision')
            workspace = str(Path(workspace).resolve(strict=True))
            files = validate_files(files or [], workspace)
        elif any(v is not None for v in (workspace, workspace_run_id, revision, files)):
            raise ValueError('Only code review may import a build')
        text, request_id = _text(text), _text(request_id, 128)
        if not isinstance(author, str) or author not in {'user', 'system'} or (author == 'system' and not share_with_team):
            raise ValueError('Invalid instruction author')
        if type(share_with_team) is not bool:
            raise ValueError('Invalid sharing choice')
        if (execution_mode not in EXECUTION_MODES or (execution_mode != 'plan' and not share_with_team)
                or (execution_mode == 'worker' and len(text) > 8000)):
            raise ValueError('Invalid direct task; use planning for longer instructions')
        if parent_request_id is not None and (
                execution_mode not in {'worker', 'code_review', *SINGLE_STAGES} or not share_with_team or not isinstance(parent_request_id, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', parent_request_id)):
            raise ValueError('Invalid worker parent')
        with self.admission() as data:
            room = data['rooms'].get(room_id)
            if room is None:
                raise LookupError('Room not found')
            if input_message_id is not None and not any(m['id'] == input_message_id and m.get('shared') is True
                                                        and m.get('kind') != 'answer' for m in room['messages']):
                raise ValueError('Selected instruction must be shared')
            prior = next((m for m in room['messages'] if m.get('request_id') == request_id), None)
            if prior:
                if prior['text'] != text or prior.get('shared') != share_with_team or prior['author'] != author:
                    raise ValueError('That request id was already used for a different message')
                if prior['run_id']:
                    old_run = next(r for r in room['runs'] if r['id'] == prior['run_id'])
                    if (old_run.get('execution_mode', 'plan') != execution_mode
                            or old_run.get('parent_request_id') != parent_request_id
                            or old_run.get('builder', 'codex') != builder or old_run.get('reviewer', 'claude') != reviewer
                            or (execution_mode == 'code_review' and any(old_run.get(k) != v for k, v in
                                {'workspace': workspace, 'workspace_run_id': workspace_run_id, 'revision': revision, 'files': files}.items()))
                            or (input_message_id is not None and old_run.get('input_message_id') != input_message_id)):
                        raise ValueError('That request already has another execution mode')
                return {'run_id': prior['run_id']}
            if len(room['messages']) >= 500:
                raise ValueError('This chat is full; start another chat')
            if share_with_team and room['room']['local_only']:
                raise ValueError('Local-only chats cannot send to hosted participants')
            occupants = [r for r in room['runs'] if r['status'] in ACTIVE | {'uncertain'}
                         or r['role_lease'] or r['write_lease']]
            siblings = (parent_request_id is not None and len(occupants) < 3 and all(
                r.get('parent_request_id') == parent_request_id and r.get('execution_mode') in {'worker', 'code_review', *SINGLE_STAGES}
                and r['status'] in ACTIVE for r in occupants))
            if share_with_team and occupants and not siblings:
                raise ValueError('This chat already has work in progress or awaiting reconciliation')
            run_id = uuid.uuid4().hex if share_with_team else None
            config = None
            if run_id:
                from companion.team_transport import config_fingerprint, read_config
                config = read_config(self.root)
            _message(room, author, text, request_id=request_id, run_id=run_id, shared=share_with_team)
            if author == 'system':
                message = room['messages'][-1]
                message.update(hidden=True, details_id=message['id'])
                team_log.append(self.root, room_id, message_id=message['id'], author=author,
                                kind='assignment', details=text, created_at=message['created_at'])
            if run_id:
                stage = SINGLE_STAGES.get(execution_mode, 'review' if execution_mode == 'code_review' else 'build' if execution_mode == 'worker' else 'plan')
                reason = {'plan': 'Waiting for the planner.', 'build': 'Waiting for the worker.',
                          'review': 'Waiting for report review.'}.get(stage, 'Waiting for the native reply.')
                room['runs'].append(dict(id=run_id, status='queued', stage=stage, phase='pending', attempt=0,
                                         execution_mode=execution_mode, builder=builder, reviewer=reviewer,
                                         reason=reason, created_at=time.time(),
                                         expires_at=time.time() + 1800, stop_requested=False,
                                         role_lease=None, write_lease=False,
                                         tasks=[dict(id=f'{run_id}-0', assignee=builder, status='queued', title='Direct task', instruction=text)] if execution_mode == 'worker' else [], revision=revision,
                                         submitted_config=deepcopy(config) if config and config['version'] in (2, 3) else None,
                                         binding=config_fingerprint(config),
                                         input_message_id=input_message_id or room['messages'][-1]['id']))
                if execution_mode == 'code_review':
                    room['runs'][-1].update(workspace=workspace, workspace_run_id=workspace_run_id, files=files)
                if parent_request_id is not None:
                    room['runs'][-1]['parent_request_id'] = parent_request_id
            return {'run_id': run_id}

    def queue_answer(self, room_id, text, request_id, *, binding, author="user"):
        """Atomically admit the private question and its one bounded answer slot."""
        from companion.local_lead_inference import LeadBusyError
        text, request_id = _text(text), _text(request_id, 128)
        validate_lead_binding(binding)
        if not isinstance(author, str) or author not in {'user', 'system'}:
            raise ValueError('Invalid instruction author')
        binding = deepcopy(binding)
        with self.admission() as data:
            room = data['rooms'].get(room_id)
            if room is None:
                raise ValueError('Room not found')
            prior = next((m for m in room['messages'] if m.get('request_id') == request_id
                          and m.get('kind') != 'answer'), None)
            if prior is not None:
                answer = next((m for m in room['messages'] if m.get('kind') == 'answer'
                               and m.get('reply_to') == prior['id']), None)
                if prior['text'] != text or prior['author'] != author or prior.get('shared') is not False or answer is None:
                    raise ValueError('That request id was already used for a different message')
                validate_answer(answer)
                return {k: answer[k] for k in ('reply_to', 'request_id', 'status')} | {'answer_id': answer['id']}
            answers = [m for _, m in local_answers(data)]
            if sum(m['status'] == 'queued' for m in answers) >= 2 or sum(m['status'] in ANSWER_ACTIVE for m in answers) >= 3:
                raise LeadBusyError()
            if len(room['messages']) > 498:
                raise ValueError('This chat is full; start another chat')
            _message(room, author, text, request_id=request_id, run_id=None, shared=False)
            if author == 'system':
                message = room['messages'][-1]
                message.update(hidden=True, details_id=message['id'])
                team_log.append(self.root, room_id, message_id=message['id'], author=author,
                                kind='assignment', details=text, created_at=message['created_at'])
            reply_to = room['messages'][-1]['id']
            _message(room, 'kyra', '', kind='answer', request_id=request_id, reply_to=reply_to,
                     run_id=None, shared=False, status='queued', stop_requested=False,
                     attempt_id=None, binding=binding, sources=[], route=None, model=binding['model_id'],
                     notice='', started_at=None, finished_at=None, omitted_counts={'sources': 0, 'history': 0},
                     missing=None)
            answer = room['messages'][-1]
            return {k: answer[k] for k in ('reply_to', 'request_id', 'status')} | {'answer_id': answer['id']}

    def get_answer(self, room_id, request_id):
        room = self.get_room(room_id)
        answer = next((m for m in room['messages'] if m.get('kind') == 'answer'
                       and m.get('request_id') == request_id), None)
        if answer is None:
            raise LookupError('Answer not found')
        validate_answer(answer)
        return deepcopy(answer)

    def request_answer_stop(self, room_id, request_id):
        with self.edit() as data:
            self.check_namespace(data)
            room = data['rooms'].get(room_id)
            if room is None:
                raise LookupError('Room not found')
            answer = next((m for m in room['messages'] if m.get('kind') == 'answer'
                           and m.get('request_id') == request_id), None)
            if answer is None:
                raise LookupError('Answer not found')
            validate_answer(answer)
            if answer['status'] in ANSWER_ACTIVE:
                answer['stop_requested'] = True
                if answer['status'] == 'queued':
                    answer.update(status='stopped', finished_at=time.time(), notice='cancelled')
                room['room']['updated_at'] = time.time()
            validate_answer(answer)
            return deepcopy(answer)

    def stop(self, room_id, *, run_id=None):
        """Persist Stop only; callers use TeamService.stop to release shared claims."""
        with self.edit() as data:
            room = data['rooms'].get(room_id)
            if room is None:
                raise LookupError('Room not found')
            if run_id is not None and not any(r['id'] == run_id for r in room['runs']):
                raise LookupError('Run not found in this room')
            for run in room['runs']:
                if run_id is not None and run['id'] != run_id:
                    continue
                if run.get('precondition_wait') or any(i['state'] == 'queued' for i in run.get('open_items', [])):
                    run['stop_requested'] = True
                if run['status'] in ACTIVE:
                    run['stop_requested'] = True
                    if run['phase'] in {'waiting', 'dispatching'}:
                        run.update(status='stopping', reason='No later stage will start. Waiting for the current worker to finish.')
                    else:
                        _finish(run, 'stopped', 'Stopped before another stage was sent.')
            return deepcopy(room)

    def active_runs(self):
        return [run['id'] for room in self._read()['rooms'].values() for run in room['runs'] if run['status'] in ACTIVE
                or (not run['stop_requested'] and (run.get('precondition_wait') or any(i['kind'] == 'handoff' and i['state'] == 'queued' for i in run.get('open_items', []))))
                or (not run['stop_requested'] and any(i['state'] == 'queued' and not i['nudged'] for i in run.get('open_items', [])))]



def memory_pack_lines(run, history):
    """KYRA_MEMORY_V2=1 only: one memory pack per dispatched task. Team members are cloud agents, so the pack is
    always built with the outbound release policy (memory_v2.wiring). Empty when the flag is off."""
    return ''  # public build stand-in: memory packs (memory_v2.wiring) are not included


def progress_lines(run_id):
    """The two lines every builder gets: post a one-line progress update when starting a file or a long check."""
    return ('\nProgress line: when you start a file or a long check, post one short line (160 characters at most) so the owner sees what you are doing:\n'
            f"curl -s -X POST http://127.0.0.1:8420/api/team/runs/{run_id}/progress -H 'Content-Type: application/json' "
            '-d \'{"text":"<what you are doing>","file":"<path>"}\'\n')

class TeamService:
    """Advance bounded stages; the first-stage queue expires after 30 minutes."""
    def __init__(self, store, transport, *, controller=None):
        self.store, self.transport = store, transport
        self.controller = store.controller if controller is None else controller
        if self.controller.path.resolve() != store.controller.path.resolve():
            raise TeamStateError('Store and service controllers must match')

    def capacity_status(self):
        with self.controller._locked():
            data = self.store._read()
            self.store.check_namespace(data)
            claims = self.controller._claims()['claims'] if (self.controller.path.parent / 'claims.json').exists() else []
            reason = None
            try:
                require_admission(data, data, claims=claims)
            except TeamCapacityError as exc:
                reason = str(exc)
            return dict(eligible=reason is None, canonical_bytes=serialized_size(data),
                        hard_limit_bytes=MAX_STORE_BYTES, reserve_bytes=COMPLETION_RESERVE_BYTES,
                        admission_limit_bytes=ADMISSION_LIMIT_BYTES, admission_allowed=reason is None,
                        reason=reason)

    @staticmethod
    def _room_digest(room):
        return hashlib.sha256(json.dumps(room, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=True).encode('utf-8')).hexdigest()

    @staticmethod
    def _archive_reason(room, claims):
        if any(m.get('kind') == 'answer' and m.get('status') in ANSWER_ACTIVE for m in room['messages']):
            return 'Local answers must finish before archive.'
        ids = {run['id'] for run in room['runs']}
        if any(run['status'] in ACTIVE | {'uncertain'} or run['phase'] in {'waiting', 'dispatching'}
               or run['role_lease'] or run['write_lease'] for run in room['runs']):
            return 'Work must finish and be reconciled before archive.'
        if any(c['owner'] == 'team' and c['run_id'] in ids for c in claims):
            return 'A shared claim still belongs to this room.'
        return None

    def archive_preview(self, room_id):
        if not _hex(room_id):
            raise ValueError('Invalid room id')
        with self.controller._locked():
            with self.store.edit() as data:
                self.store.check_namespace(data)
                if data['version'] != 2:
                    raise TeamCapacityError('Legacy Team state has no archive provenance; preserve the original root.')
                claims = self.controller._claims()['claims'] if (self.controller.path.parent / 'claims.json').exists() else []
                room = data['rooms'].get(room_id)
                if room is None:
                    raise LookupError('Room not found')
                reason = self._archive_reason(room, claims)
                return dict(room_id=room_id, room_sha256=self._room_digest(room), eligible=reason is None,
                            message_count=len(room['messages']), run_count=len(room['runs']),
                            reason=reason, process_crash_only=True)

    def archive_room(self, room_id, *, expected_room_sha256):
        """Explicit archive-before-removal; process-crash safe, not power-loss durable."""
        if not _hex(room_id) or not _hex(expected_room_sha256, 64):
            raise ValueError('Exact room id and SHA-256 required')
        with self.controller._locked():
            with self.store.edit() as data:
                self.store.check_namespace(data)
                if data['version'] != 2:
                    raise TeamCapacityError('Legacy Team state has no archive provenance; preserve the original root.')
                claims = self.controller._claims()['claims'] if (self.controller.path.parent / 'claims.json').exists() else []
                directory = self.store.root / 'archives'
                if directory.is_symlink():
                    raise ValueError('Unsafe archive directory')
                directory.mkdir(mode=0o700, exist_ok=True)
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError('Unsafe archive directory')
                path = directory / f'{room_id}-{expected_room_sha256}.json'
                origin = self.store.root.stat()
                identity = dict(path=str(self.store.root.resolve()), device=origin.st_dev, inode=origin.st_ino)
                existing = None
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    pass
                else:
                    with os.fdopen(fd, 'rb') as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                            raise ValueError('Unsafe archive file')
                        existing = stream.read(MAX_STORE_BYTES + 8193)
                    if len(existing) > MAX_STORE_BYTES + 8192:
                        raise ValueError('Archive exceeds limit')
                room = data['rooms'].get(room_id)
                removed = room is not None
                if room is None:
                    if existing is None:
                        raise LookupError('Room and archive not found')
                    archived = json.loads(existing)
                    room = archived.get('room')
                if (not isinstance(room, dict) or room.get('room', {}).get('id') != room_id
                        or self._room_digest(room) != expected_room_sha256):
                    raise ValueError('Room changed; preview again')
                reason = self._archive_reason(room, claims)
                if reason:
                    raise ValueError(reason)
                payload = dict(version=1, root=identity, room_sha256=expected_room_sha256, room=room)
                encoded = json.dumps(payload, indent=2, sort_keys=True).encode('utf-8')
                if existing is not None and existing != encoded:
                    raise ValueError('Existing archive does not match')
                if removed:
                    remaining = deepcopy(data)
                    del remaining['rooms'][room_id]
                    if serialized_size(remaining) > MAX_STORE_BYTES:
                        raise TeamCapacityError('Remaining Team state exceeds hard limit')
                if existing is None:
                    fd, temporary = tempfile.mkstemp(prefix='.archive-', dir=directory)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(encoded)
                            stream.flush()
                        # Exclusive publication cannot overwrite a different archive.
                        os.link(temporary, path)
                    finally:
                        os.unlink(temporary)
                if removed:
                    del data['rooms'][room_id]
        return dict(room_id=room_id, archived=True, removed=removed, archive_path=str(path),
                    room_sha256=expected_room_sha256)

    def _assignment(self, room, run, result_path):
        # Thread replies, local answers and private inputs stay out of room context.
        from companion import team_threads
        if hasattr(team_threads, 'thread_run_ids'):
            thread_runs = team_threads.thread_run_ids(room)
        else:
            thread_runs = {m['run_id'] for m in room['messages']
                           if str(m.get('request_id', '')).startswith('reply.') and m.get('run_id')}
        shared = [m for m in room['messages'] if (m.get('shared') is True
                  or (m.get('kind') == 'result' and m.get('shared') is not False))
                  and m.get('kind') != 'answer' and m.get('run_id') not in thread_runs
                  and not str(m.get('request_id', '')).startswith('reply.')][-12:]
        selected_ids = {m['id'] for m in shared}
        history = [{'author': m['author'], 'text': (team_log.read(self.store.root, room['room']['id'],
                    m.get('details_id')) or {}).get('details', m['text'])} for m in room['messages']
                   if m['id'] in selected_ids or (run['id'] in thread_runs and m['id'] == run.get('input_message_id'))
                   or (m.get('kind') == 'result' and m.get('run_id') == run['id'])]
        mode = run.get('execution_mode', 'plan')
        if mode in SINGLE_STAGES:
            binding = _run_binding(run) or {}
            context = (f'Assigned workspace: {json.dumps(binding.get("workspace"))}.\n'
                       f'Owner-approved engineering scope: {json.dumps(binding.get("scope"))}.\n'
                       'Use only the supplied assignment and final result file. No source inspection, branch switch, '
                       'app/model setting change, or other file write is requested.\n')
        else:
            context = (self.transport.context_for(_run_binding(run)) if hasattr(self.transport, 'context_for')
                       else getattr(self.transport, 'context', lambda: 'Use only the assigned project and its existing permissions.')())
        schema = dict(run_id=run['id'], stage=run['stage'], attempt=run['attempt'], status='completed', chat='Short outcome and next step', summary='Full result details')
        if mode in {'reply_claude', 'reply_codex'}:
            role = _stage_role(run).title()
            goal = (f'{role}: reply directly to the selected instruction in summary. This is a reply, not a coding plan or implementation. '
                    'Do not edit source, invent coding tasks, or dispatch other participants. '
                    'Use only this assignment and the final result file; no other source reads are requested. '
                    'A reply does not accept code changes or replace independent code review. '
                    'Put all supporting details and any Mermaid diagram in summary, not chat.')
        elif mode == 'plan_only':
            schema.update(kind='plan', tasks=[{'provider': 'codex', 'title': 'Bounded task',
                          'instruction': 'Specific work and its acceptance check', 'acceptance': ['Concrete required outcome'], 'order': 1, 'mode': 'code'}])
            goal = ('Claude: decide whether to answer directly (kind=answer, tasks=[]), delegate one task '
                    '(kind=task, exactly one task), or plan up to eight steps (kind=plan). '
                    'Targets may be kyra, claude, codex, cursor or antigravity. Roles: codex and claude build and review code; '
                    'antigravity does web research and current facts (mode=reply, cite source URLs); cursor answers read-only '
                    'questions about the code (mode=reply); give antigravity or cursor a step whenever the request has such a part, '
                    'and never mode=code. Each task has provider, title, '
                    'instruction, acceptance (a list of concrete required outcomes), integer order (1..8), and mode=reply for text or mode=code for code changes. '
                    'Lower orders complete before higher ones; equal orders may run in parallel. '
                    'Decide with stated defaults; ask one question at a time, each with your guess, '
                    'routed through the home chat with choices. '
                    'Put a direct answer in summary. The service dispatches the returned tasks; do not send them yourself.')
        elif mode == 'interview_only':
            schema['ready'] = False
            goal = ('Claude: interview only, before any plan. The selected instruction holds the request, the interview '
                    'so far and the interview rules. Put exactly one question with your guess in summary and set ready '
                    'false, or, when the intent is clear, a short restatement ending with "Start?" and set ready true. '
                    'If the request only needs a direct answer and no work, put the complete answer in summary, '
                    'keep ready=false, and add kind=answer and tasks=[] to the result shape. '
                    'Omit kind and tasks for questions or work restatements. '
                    'Do not inspect source, plan, implement or dispatch.')
        elif mode == 'review_only':
            schema['verdict'] = 'pass'
            goal = ('Claude: review reports only. The selected instruction contains bounded request context and worker summaries. '
                    'Assess those supplied reports; do not inspect source, implement repairs, or dispatch workers. '
                    'Return pass, changes_requested or needs_input. This is not an independent code revision review '
                    'and does not accept code or authorize merge. No revision or workspace fingerprint is requested for this report review.')
        elif run['stage'] == 'plan':
            schema['tasks'] = [{'title': 'Bounded task', 'instruction': 'Specific work and its acceptance check',
                                'acceptance': ['Concrete required outcome']}]
            goal = 'Claude: plan only. Divide this instruction into one to four bounded coding tasks. Do not implement them.'
        elif run['stage'] == 'build':
            schema.update(revision='REPLACE_WITH_WORKSPACE_REVISION', files=[])
            goal = (f'{_stage_role(run).title()}: carry out the exact user-shared instruction below. No planner has approved this direct task. Re-read the workspace, perform the smallest sufficient checks and report the actual workspace revision.'
                    if run.get('execution_mode') == 'worker' else
                    f'{_stage_role(run).title()}: re-read the current workspace because an earlier parallel plan may be stale. Build the planned tasks, run the smallest sufficient checks and report the actual workspace revision.')
        else:
            schema.update(revision=run['revision'], verdict='pass')
            goal = f'{_stage_role(run).title()}: independently check the build and tests at the exact given revision. Do not edit implementation. Return pass, changes_requested or needs_input.'
        if run.get('workspace'):
            context += (f'\nTask workspace: {json.dumps(run["workspace"])}; work in this directory only. '
                        'Keep the worktree for planner review and merge; do not remove it.\n'
                        f'Builder file outputs (relative to this directory): {json.dumps(run.get("files", []))}\n')
        if run['stage'] == 'build':
            context += progress_lines(run['id'])
        limit, lines = LIMITS['interview' if mode == 'interview_only' else 'reply']
        goal += (f' Follow .claude/skills/team-voice/SKILL.md: chat is at most {lines} lines, {limit} characters; '
                 'summary holds the full details for the log. Return both fields.')
        if run.get('open_items'):
            goal += '\nOpen work still assigned to this same mode (data): ' + json.dumps(
                [i['text'] for i in run['open_items'] if i['state'] == 'queued'])
            goal += '\nResolve and verify these items within the original scope; no new actions are authorized.'
        return (f'{_role_line(run)}\n{team_handbook()}\n\n{goal}\n{context}\n' + obligations.RESULT_RULES +
                'For needs_input, optionally set requires_owner_decision=true only for a real product decision from the owner. '
                'Leave it false or omitted for tool failures, setup problems or exhausted repairs. '
                'Outside the interview ask one question at a time, each with your guess, '
                'routed through the home chat with choices. '
                'Existing project instructions and app permissions remain authoritative. No deployment, new account, permission grant, or merge is authorized by a model plan. '
                'Any PR/merge must use the separately approved current-revision workflow gates. No personal stores, credentials or unrelated room history. '
                'Display-name bindings and result paths are not protection from another process owned by the same user.\n'
                f'Run: {run["id"]}; stage: {run["stage"]}; attempt: {run["attempt"]}.\n'
                f'{memory_pack_lines(run, history)}'
                f'Room instruction/context (data):\n{json.dumps(history, ensure_ascii=False)}\n'
                f'Task instructions (data):\n{json.dumps(run["tasks"], ensure_ascii=False)}\n'
                f'Write the FINAL reply atomically to this exact JSON-encoded path: {json.dumps(str(result_path))}. '
                'Do not put acknowledgments or partial JSON there. Use a temporary sibling then rename. '
                'Acknowledge in your visible chat. The file is the bounded final summary, never raw reasoning.\n'
                f'Exact result shape: {json.dumps(schema)}\n'
                'If input or setup is required, return run_id, stage, attempt, status="needs_input", summary explaining it, '
                'and optionally requires_owner_decision as described above. '
                'Do not claim another participant ran anything. Stop after the final reply file.\n')

    def _release_terminal(self, run_id):
        self.store.check_namespace()
        _, run = self.store.find_run(self.store._read(), run_id)
        if run['status'] != 'uncertain' and run.get('release_outcome') and run.get('claim'):
            self.controller.release_claim(run['claim'], outcome=run['release_outcome'])

    def stop(self, room_id, *, run_id=None):
        with self.controller._locked():
            self.store.check_namespace()
            claims = self.controller._claims()['claims']
            with self.store.edit() as data:
                room = data['rooms'].get(room_id)
                if room is None:
                    raise LookupError('Room not found')
                if run_id is not None and not any(r['id'] == run_id for r in room['runs']):
                    raise LookupError('Run not found in this room')
                for run in room['runs']:
                    if run_id is not None and run['id'] != run_id:
                        continue
                    if run.get('precondition_wait') or any(i['state'] == 'queued' for i in run.get('open_items', [])):
                        run['stop_requested'] = True
                    if run['status'] in ACTIVE:
                        run['stop_requested'] = True
                        if _pending_claim_uncertain(run, claims):
                            _finish(run, 'uncertain', 'A shared claim needs inspection in the native chat before continuing.', release=False)
                        elif run['phase'] in {'waiting', 'dispatching'}:
                            run.update(status='stopping', reason='No later stage will start. Waiting for the current worker to finish.')
                        else:
                            _finish(run, 'stopped', 'Stopped before another stage was sent.')
                room = deepcopy(room)
        for run in room['runs']:
            if run_id is None or run['id'] == run_id:
                self._release_terminal(run['id'])
        return room

    def fail_run(self, run_id):
        # Claim-only crashes must not masquerade as a known unsent request.
        with self.controller._locked():
            self.store.check_namespace()
            claims = self.controller._claims()['claims']
            with self.store.edit() as data:
                _, run = self.store.find_run(data, run_id)
                if run['status'] in TERMINAL:
                    return
                held = any(c['owner'] == 'team' and c['run_id'] == run_id for c in claims)
                inflight = held or run['phase'] in {'waiting', 'dispatching'}
                _finish(run, 'uncertain' if inflight else 'needs_input',
                        'Local setup or state needs inspection on the Mac.', release=not inflight)

    def reconcile(self, run_id, *, worker_ended, expected_claim=None):
        """End inspected work, selecting a replacement claim only by exact snapshot."""
        from companion.workflow_control import CLAIM_KEYS

        if worker_ended is not True:
            raise ValueError('Exact worker completion must be asserted')
        with self.controller._locked():
            self.store.check_namespace()
            claims = self.controller._claims()['claims']
            with self.store.edit() as data:
                room, run = self.store.find_run(data, run_id)
                retry = (run['status'] == 'stopped' and run.get('reconciled_at') is not None
                         and run.get('release_outcome') == 'operator_reconciled'
                         and run.get('claim') in claims)
                if run['status'] != 'uncertain' and not retry:
                    raise ValueError('Only uncertain runs can be reconciled')
                matches = [c for c in claims if c['owner'] == 'team' and c['run_id'] == run_id]
                claim = run.get('claim')
                if expected_claim is not None and (len(matches) != 1 or matches[0] != expected_claim):
                    raise PermissionError('Selected claim changed; inspect exact worker')
                if len(matches) > 1:
                    raise PermissionError('Claim changed; inspect exact worker')
                if matches and claim is not None and matches[0] != claim:
                    current = matches[0]
                    immutable = CLAIM_KEYS - {'stage', 'attempt', 'participants'}
                    if run.get('claim_released'):
                        immutable -= {'id'}
                    if (expected_claim is None or run['status'] != 'uncertain'
                            or not isinstance(claim, dict) or set(claim) != CLAIM_KEYS
                            or any(claim[k] != current[k] for k in immutable)
                            or type(claim['attempt']) is not int or type(run['attempt']) is not int
                            or (current['stage'], current['attempt']) != (run['stage'], run['attempt'])):
                        raise PermissionError('Claim changed; select the exact inspected next-stage claim')
                    stage, attempt = claim['stage'], claim['attempt']
                    successor = (
                        (stage == 'plan' and attempt == 0 and (current['stage'], current['attempt']) == ('build', 0))
                        or (stage == 'build' and 0 <= attempt <= 2
                            and (current['stage'], current['attempt']) == ('review', attempt))
                        or (stage == 'review' and 0 <= attempt < 2
                            and (current['stage'], current['attempt']) == ('build', attempt + 1)))
                    if not successor:
                        raise PermissionError('Claim is not the persisted next transition')
                    pins = (_run_binding(run) or {}).get('participants', {})
                    role = _stage_role(run, current['stage'])
                    if current['participants'] != {role: pins.get(role)} and current['participants'] != pins:
                        raise PermissionError('Claim participant is not the pinned successor')
                    claim = current
                if matches and matches[0]['task_id'] != (run.get('submitted_config') or {}).get('controller', {}).get('task_id'):
                    raise PermissionError('Claim task does not match submission')
                if claim is None and matches:
                    claim = matches[0]
                if claim is not None:
                    run['claim'] = claim
                if not retry:
                    _finish(run, 'stopped', 'Operator declared the native worker ended. No retry was sent.')
                    run.update(stop_requested=True, reconciled_at=time.time(), release_outcome='operator_reconciled')
                    room['room']['updated_at'] = time.time()
        self._release_terminal(run_id)
        return {'run_id': run_id, 'status': 'stopped', 'retry_sent': False}

    def _unmet(self, run):
        # Caller holds the controller lock; do not acquire it again via claims().
        return obligations.unmet_preconditions(
            run.get('completion', {}).get('preconditions', []), run.get('workspace'),
            run=run, claims=self.controller._claims()['claims'],
            delivery_status=getattr(self.transport, 'delivery_status', None))

    def _consume(self, room, run, result, revision, revision_error):
        run_id = run['id']
        run['completion'] = obligations.completion(self.store.root, run, result, result['summary'])
        if not run.get('precondition_wait'):
            run.pop('wait_started_at', None)
        run['acknowledged_at'] = time.time()
        run['requires_owner_decision'] = result.get('requires_owner_decision', False)
        role = _stage_role(run)
        mode = run.get('execution_mode', 'plan')
        if mode == 'code_review':
            run['verdict'] = result.get('verdict', 'needs_input')
        limit, lines = LIMITS['interview' if mode == 'interview_only' else 'reply']
        text = result_chat(result['summary'], result.get('chat'), limit=limit, lines=lines)
        _message(room, role, text, run_id=run_id, kind='result', stage=run['stage'], shared=True,
                 assurance='Assignment-correlated reply; not authenticated against same-user processes.')
        message = room['messages'][-1]
        message['details_id'] = message['id']
        team_log.append(self.store.root, room['room']['id'], message_id=message['id'], author=role,
                        kind='result', details=result['summary'], created_at=message['created_at'])
        run['role_lease'] = None
        if run['stop_requested']:
            _finish(run, 'stopped', 'Current reply received. No later stage was sent.')
            return
        handoffs = run['completion'].get('handoffs', [])
        if handoffs:
            run['completion']['handoffs'] = handoffs
        queue = run.setdefault('open_items', [])
        for handoff in handoffs:
            item = next((i for i in queue if i['kind'] == 'handoff' and i['owner'] == handoff['owner']
                         and i['text'] == handoff['acceptance']), None)
            if item is None:
                if len(queue) >= 128:
                    _finish(run, 'needs_input', 'Open work queue is full; inspect the handoff.')
                    return
                item = dict(text=handoff['acceptance'], owner=handoff['owner'], kind='handoff',
                            state='queued', due_at=handoff.get('due_at', time.time() + 1800), nudged=False,
                            expires_at=time.time() + obligations.HANDOFF_SECONDS)
                queue.append(item)
            if handoff.get('evidence') or handoff.get('close_reason'):
                item['state'] = 'completed'
        if any(i['kind'] == 'handoff' and i['state'] != 'completed' for i in queue):
            _finish(run, 'needs_input', 'Waiting on the named external owner; no second writer will start.')
            return
        if (result['status'] == 'needs_input' or result.get('verdict') == 'needs_input') and not run['requires_owner_decision']:
            pending = self._unmet(run)
            if pending:
                run['precondition_wait'] = True
                since = run.setdefault('wait_started_at', time.time())
                _finish(run, 'needs_input', obligations.wait_reason(pending, since, time.time()))
                return
        run.pop('precondition_wait', None)
        run.pop('wait_started_at', None)
        required = [item for task in run['tasks'] for item in task.get('acceptance', [])]
        required += [i['text'] for i in run.get('open_items', []) if i['state'] == 'queued' and i['kind'] == 'acceptance']
        missing = obligations.missing(required, result, result['summary'])
        decision = run['requires_owner_decision'] and bool(result['summary'].strip())
        ordinary_blocker = (result['status'] == 'needs_input' or result.get('verdict') == 'needs_input') and not decision
        if not run.get('parent_request_id') and run['stage'] != 'plan' and mode not in {'plan_only', 'interview_only'}:
            queue = run.setdefault('open_items', [])
            needs = missing + ([result['summary'][:1500]] if ordinary_blocker else [])
            if result['status'] == 'completed' and not ordinary_blocker:
                for item in queue:
                    if item['kind'] != 'handoff' and item['text'] not in needs:
                        item['state'] = 'completed'
            for item_text in dict.fromkeys(needs):
                existing = next((i for i in queue if i['text'] == item_text), None)
                if existing:
                    existing['state'] = 'queued'
                elif len(queue) < 128:
                    queue.append(dict(text=item_text, owner=run.get('builder', 'codex') if mode in {'worker', 'plan'} and run['stage'] != 'plan' else role,
                                      state='queued', due_at=time.time() + 1800, nudged=False,
                                      kind='acceptance' if item_text in missing else 'blocker'))
            if needs and not decision and mode not in {'review_only', 'code_review'} and run['attempt'] < 2:
                stage = 'build' if mode in {'worker', 'plan'} and run['stage'] == 'review' else run['stage']
                run.update(attempt=run['attempt'] + 1, stage=stage, phase='pending', reason='Repair queued for the assigned owner.')
                return
        if missing and not decision and run['stage'] != 'plan' and mode not in {'plan_only', 'interview_only'}:
            run['completion']['unmet_acceptance'] = missing[:obligations.LIMIT]
            _finish(run, 'needs_input', 'Unmet acceptance or follow-up: ' + '; '.join(missing)[:1400])
            run['requires_owner_decision'] = False
            return
        if result['status'] == 'needs_input' or result.get('verdict') == 'needs_input':
            _finish(run, 'needs_input', chat_line(result['summary']))
            return
        if mode in {'reply_claude', 'reply_codex'}:
            _finish(run, 'completed', 'Native reply received; no code acceptance or worker dispatch.')
            return
        if mode == 'plan_only':
            if 'kind' in result:
                run['planner_kind'] = result['kind']
            run['tasks'] = [dict(id=f'{run_id}-{i}', assignee=task['provider'], status='queued',
                                 **{k: v for k, v in task.items() if k != 'provider'})
                            for i, task in enumerate(result['tasks'])]
            _finish(run, 'completed', 'Planner answer received.' if result.get('kind') == 'answer'
                    else 'Plan received; proposed worker tasks have not been dispatched.')
            return
        if mode == 'interview_only':
            run['interview_ready'] = result['ready']
            if 'kind' in result:
                run['planner_kind'] = result['kind']
            _finish(run, 'completed', 'Interview turn received; nothing was planned or dispatched.')
            return
        if mode == 'review_only':
            _finish(run, 'completed' if result['verdict'] == 'pass' else 'needs_input',
                    'Report review passed; this is not code revision acceptance.' if result['verdict'] == 'pass'
                    else chat_line(result['summary']))
            return
        if run['stage'] in {'build', 'review'}:
            if revision_error:
                _finish(run, 'needs_input', 'The worker finished, but the workspace revision could not be checked. Inspect it on the Mac.')
                return
            if revision is not None and result['revision'] != revision:
                _finish(run, 'needs_input', 'The workspace changed. A fresh build and independent review are required.')
                return
        if run['stage'] == 'plan':
            run['tasks'] = [{'id': f'{run_id}-{i}', 'assignee': run.get('builder', 'codex'), 'status': 'queued', **task}
                            for i, task in enumerate(result['tasks'])]
            run.update(stage='build', phase='pending')
        elif run['stage'] == 'build':
            run.update(revision=result['revision'], files=result.get('files', []), stage='review', phase='pending')
            for task in run['tasks']:
                task['status'] = 'reviewing'
        elif result['verdict'] == 'pass':
            _finish(run, 'completed', 'Build and independent review finished for the same workspace revision.')
            for task in run['tasks']:
                task['status'] = 'completed'
            run['learning'] = {'status': 'none', 'reason': 'No curated, non-private training example was supplied; transcripts are not automatically training data.'}
            return
        elif mode == 'code_review':
            _finish(run, 'needs_input', chat_line(result['summary']))
            run['reason'] = 'Code review requested changes. The planner must route repair to the original builder.'
            return
        elif run['attempt'] >= 2:
            _finish(run, 'needs_input', 'Two repair attempts were used. Review the remaining issue before continuing.')
            return
        else:
            run.update(attempt=run['attempt'] + 1, stage='build', phase='pending')

    def _reply(self, run):
        """Read a bounded reply and inspect Git without holding either store lock."""
        path = Path(run['result_path'])
        if not path.exists() or path.is_symlink():
            return None, None, False, False
        decoded = None
        try:
            with path.open('rb') as stream:
                raw = stream.read(MAX_RESULT + 1)
            if len(raw) > MAX_RESULT:
                raise ValueError('Result too large')
            decoded = json.loads(raw)
            result = validate_result(decoded, run_id=run['id'], stage=run['stage'], attempt=run['attempt'],
                                     revision=run['revision'] if run['stage'] == 'review' else None,
                                     execution_mode=run.get('execution_mode', 'plan'), workspace=run.get('workspace'))
        except (ValueError, TypeError, RecursionError, UnicodeError, OSError):
            terminal = (isinstance(decoded, dict) and decoded.get('run_id') == run['id']
                        and decoded.get('stage') == run['stage']
                        and type(decoded.get('attempt')) is int and decoded['attempt'] == run['attempt']
                        and decoded.get('status') in ('completed', 'needs_input'))
            return None, None, False, terminal
        revision, error = None, False
        if (result['status'] == 'completed' and run.get('execution_mode') != 'review_only'
                and run['stage'] in {'build', 'review'}):
            try:
                revision = self._revision(run)
                error = revision is None
            except (ValueError, OSError, subprocess.SubprocessError):
                error = True
        return result, revision, error, True

    def _revision(self, run):
        if hasattr(self.transport, 'revision_for'):
            return self.transport.revision_for(_run_binding(run))
        return getattr(self.transport, 'revision', lambda: None)()

    def _select_lane(self, config, run):
        from companion.team_transport import config_lanes, validate_lane_repositories
        from companion.workflow_control import _directory_id
        if config['version'] == 3:
            validate_lane_repositories(config)
        if run.get('lane'):
            if run['lane'] not in config_lanes(config):
                raise PermissionError('lane_binding_changed')
            return run['lane']
        role = _stage_role(run)
        claims = self.controller.claims()
        free = [lane for lane in config_lanes(config)
                if not any(c['run_id'] != run['id'] and (c['workspace_id'] == _directory_id(lane['workspace'])
                           or c['participants'].get(role, {}).get('id') == lane['participants'][role]['id'])
                           for c in claims)]
        if not free:
            raise ValueError('workspace_or_participant_claimed')
        # Prefer a lane whose chat is open now; with none, the first free lane reports the closed chat.
        live = getattr(self.transport, 'participant_live', None)
        return next((lane for lane in free if live is None or live(config, lane, role)), free[0])

    def _busy_requires_recovery(self, config, run, claims):
        blockers = self._seat_blockers(run, claims)
        active = {r['id'] for room in self.store._read()['rooms'].values() for r in room['runs']
                  if r['status'] in ACTIVE}
        return bool(blockers) and not any(c['owner'] == 'team' and c['run_id'] in active for c in blockers)

    def step(self, run_id):
        self.store.check_namespace()
        _, snapshot = self.store.find_run(self.store._read(), run_id)
        external = any(i['kind'] == 'handoff' and i['state'] == 'queued' for i in snapshot.get('open_items', []))
        held = snapshot.get('precondition_wait') or external
        reply = self._reply(snapshot) if snapshot['phase'] == 'waiting' or held else None
        started = False
        codex_waiting = snapshot['phase'] == 'waiting' and _stage_role(snapshot) == 'codex'
        if codex_waiting and snapshot['status'] not in TERMINAL:
            try:
                started = getattr(self.transport, 'delivery_status', lambda run: None)(snapshot) == 'running'
            except (OSError, ValueError, TypeError):
                pass  # An unavailable observation is not evidence of a start.
        sender_missing = False
        if snapshot['status'] not in TERMINAL and snapshot['phase'] == 'dispatching':
            try:
                os.kill(snapshot['dispatch_pid'], 0)
            except (ProcessLookupError, KeyError):
                sender_missing = True
        with self.controller._locked():
            self.store.check_namespace()
            claims = self.controller._claims()['claims']
            with self.store.edit() as data:
                room, run = self.store.find_run(data, run_id)
                expired = False
                for item in run.get('open_items', []):
                    if (item['kind'] == 'handoff' and item['state'] == 'queued'
                            and time.time() >= item.get('expires_at', item['due_at'] + 1800)):
                        item['state'] = 'expired'
                        expired = True
                        if not run.get('parent_request_id') and not run['stop_requested']:
                            _message(room, 'system', f"{item['owner']}: handoff expired; owned work remains open for reconciliation.",
                                     run_id=run_id, kind='step', shared=True)
                if expired:
                    run['reason'] = 'External handoff expired; reconcile the owned open item before continuing.'
                    external = any(i['kind'] == 'handoff' and i['state'] == 'queued' for i in run.get('open_items', []))
                    held = external or run.get('precondition_wait')
                    reply = None
                if run.get('precondition_wait') and time.time() >= run['expires_at']:
                    run.pop('precondition_wait', None)
                    run['reason'] = 'The task authorization window ended; retry needs a fresh request.'
                    held = external
                    reply = None
                if held and not run['stop_requested'] and all(run[k] == snapshot[k] for k in ('phase', 'stage', 'attempt', 'status')):
                    if run.get('precondition_wait'):
                        pending = self._unmet(run)
                        run['reason'] = obligations.wait_reason(pending, run.get('wait_started_at', time.time()), time.time()) if pending else run['reason']
                    else:
                        pending = []
                    if reply and reply[0]:
                        observed = obligations.completion(self.store.root, run, reply[0], reply[0]['summary'])
                        reply = ({**reply[0], **observed}, *reply[1:])
                    if reply and reply[0] and not pending and (not external or any(
                            (h.get('evidence') or h.get('close_reason')) and i['kind'] == 'handoff'
                            and i['state'] == 'queued' and i['owner'] == h['owner'] and i['text'] == h['acceptance']
                            for h in reply[0].get('handoffs', []) for i in run.get('open_items', []))):
                        if time.time() >= run['expires_at']:
                            run.pop('precondition_wait', None)
                            run['reason'] = 'The task authorization window ended; retry needs a fresh request.'
                        else:
                            run.update(status=STAGES[run['stage']][1], phase='waiting')
                            self._consume(room, run, reply[0], reply[1], reply[2])
                    reply = None
                if run['status'] not in TERMINAL:
                    if reply and all(run[k] == snapshot[k] for k in ('phase', 'stage', 'attempt')):
                        result, revision, error, terminal = reply
                        if result is not None:
                            self._consume(room, run, result, revision, error)
                        elif terminal:
                            _finish(run, 'needs_input', 'The worker finished but its reply was invalid. Inspect it on the Mac before a fresh instruction.')
                        elif codex_waiting:
                            from companion.team_transport import codex_chat_name, codex_queue_reason
                            pin = _run_binding(run)['participants']['codex']
                            home = getattr(self.transport, 'codex_home', None)
                            if started:
                                run.pop('wait_started_at', None)
                                run['reason'] = f'Codex is working in chat “{codex_chat_name(pin, codex_home=home)}”.'
                            elif not run['reason'].startswith('Codex is working in chat '):
                                since = run.setdefault('wait_started_at', time.time())
                                run['reason'] = codex_queue_reason(pin, codex_home=home, minutes=int((time.time() - since) // 60))
                        else:
                            run['reason'] = 'Waiting for a complete reply matching this task and stage.'
                    if run['status'] not in TERMINAL and time.time() >= run['expires_at']:
                        uncertain = run['phase'] in {'waiting', 'dispatching'} or _pending_claim_uncertain(run, claims)
                        _finish(run, 'uncertain' if uncertain else 'needs_input',
                                'The time limit ended. Inspect the native chat before continuing.' if uncertain
                                else 'The time limit ended before dispatch.', release=not uncertain)
                    if (run['status'] not in TERMINAL and sender_missing
                            and all(run.get(k) == snapshot.get(k) for k in ('phase', 'stage', 'attempt', 'dispatch_pid'))):
                        _finish(run, 'uncertain', 'The sending process stopped. Check the native chat before continuing.', release=False)
                    if run['status'] not in TERMINAL and run['phase'] == 'pending' and run['stop_requested']:
                        uncertain = _pending_claim_uncertain(run, claims)
                        _finish(run, 'uncertain' if uncertain else 'stopped',
                                'A shared claim needs inspection in the native chat before continuing.' if uncertain
                                else 'Stopped before another stage was sent.', release=not uncertain)
        self._release_terminal(run_id)
        with self.store.edit() as data:
            room, run = self.store.find_run(data, run_id)
            due = [i for i in run.get('open_items', []) if i['state'] == 'queued' and not i['nudged'] and i['due_at'] <= time.time()]
            if due and not run['stop_requested'] and not run.get('parent_request_id'):
                for owner in dict.fromkeys(i['owner'] for i in due):
                    _message(room, 'system', f'{owner}: please report the status of handed-off work.' if any(i['kind'] == 'handoff' and i['owner'] == owner for i in due) else f'{owner.title()}: assigned open work is overdue. Resolve it or report the blocker.',
                             run_id=run_id, kind='step', shared=True)
                for item in due:
                    item['nudged'] = True
        self._post_codex_progress(run_id)
        self._dispatch(run_id)
        self._post_wait_notice(run_id)

    def _post_codex_progress(self, run_id):
        room, run = self.store.find_run(self.store._read(), run_id)
        if (run['status'] in TERMINAL or run['phase'] != 'waiting'
                or _stage_role(run) != 'codex'):
            return
        state = 'working' if run['reason'].startswith('Codex is working in chat ') else 'queued'
        if state == 'queued':
            return
        self.store.post_step(room['room']['id'], 'dispatched', run['reason'],
                             event_key=f'codex:{run_id}:{run["stage"]}:{run["attempt"]}:{state}',
                             run_id=run_id, parent_request_id=run.get('parent_request_id'))

    def _release_finished_stage(self, run_id, *, force=False):
        # Pending plus a different stage/attempt is persisted only after a
        # correlated reply. Never release dispatching, waiting or orphan claims.
        with self.controller._locked():
            claims = self.controller._claims()
            with self.store.edit() as data:
                self.store.check_namespace(data)
                _, run = self.store.find_run(data, run_id)
                old = run.get('claim')
                if (run['status'] not in ACTIVE or run['phase'] != 'pending' or not old
                        or (old['stage'], old['attempt']) == (run['stage'], run['attempt'])
                        or _pending_claim_uncertain(run, claims['claims'])
                        or (not force and old in claims['claims']
                            and not self._seat_blockers(run, claims['claims']))):
                    return
                if old in claims['claims']:
                    claims['claims'].remove(old)
                    self.controller._save_claims(claims)
                # Also repairs a crash after claim release, before this save.
                # Retain the old identity for exact successor reconciliation
                # if the next reservation persists but the Team save fails.
                run.update(claim_released=True, write_lease=False, role_lease=None)

    def _seat_blockers(self, run, claims):
        from companion.team_transport import config_lanes
        from companion.workflow_control import _directory_id
        role = _stage_role(run)
        lanes = [run['lane']] if run.get('lane') else config_lanes(run['submitted_config'])
        return [c for c in claims if c['run_id'] != run['id'] and any(
            c['workspace_id'] == _directory_id(run.get('workspace', lane['workspace']))
            or c['participants'].get(role, {}).get('id') == lane['participants'][role]['id']
            for lane in lanes)]

    def _record_seat_wait(self, run_id):
        victim_id = None
        with self.controller._locked():
            claims = self.controller._claims()['claims']
            with self.store.edit() as data:
                self.store.check_namespace(data)
                _, run = self.store.find_run(data, run_id)
                if run['status'] not in ACTIVE or run['phase'] != 'pending':
                    return
                holders = self._seat_blockers(run, claims)
                if not holders:
                    return  # A legacy/unknown lease is not an identified holder.
                rooms = {r['id']: room for room in data['rooms'].values() for r in room['runs']}
                runs = {r['id']: r for room in data['rooms'].values() for r in room['runs']}
                def label(holder):
                    room = rooms.get(holder, {}).get('room', {}).get('title', 'unknown room')
                    room = ' '.join(''.join(c if c.isprintable() else ' ' for c in room).split())
                    return f'seat held by run {holder} in {room}'
                since = run.setdefault('wait_started_at', time.time())
                run['reason'] = f"waiting on {label(holders[0]['run_id'])} · {max(0, int((time.time() - since) // 60))} min"
                for claim in holders:
                    other = runs.get(claim['run_id']) if claim['owner'] == 'team' else None
                    if (not other or other['status'] not in ACTIVE or other['phase'] != 'pending'
                            or _pending_claim_uncertain(run, claims) or _pending_claim_uncertain(other, claims)
                            or any(not r.get('claim') or (r['claim']['stage'], r['claim']['attempt'])
                                   == (r['stage'], r['attempt']) for r in (run, other))):
                        continue
                    if not any(c['owner'] == 'team' and c['run_id'] == run_id
                               for c in self._seat_blockers(other, claims)):
                        continue
                    victim = max((run, other), key=lambda r: (r['created_at'], r['id']))
                    holder = other if victim is run else run
                    victim_id = victim['id']
                    _finish(victim, 'needs_input',
                            f"Seat wait cycle: {label(holder['id'])}. Planner must retry this younger run after the holder finishes.")
                    break
        if victim_id:
            self._release_terminal(victim_id)
            self._post_wait_notice(victim_id)

    def _post_wait_notice(self, run_id):
        room, run = self.store.find_run(self.store._read(), run_id)
        if run['status'] == 'needs_input' and run['reason'].startswith('Seat wait cycle:'):
            self.store.post_step(room['room']['id'], 'asked', run['reason'],
                                 event_key=f'seat-cycle:{run_id}', run_id=run_id,
                                 parent_request_id=run.get('parent_request_id'))
            return
        if (run['status'] in TERMINAL or not run['reason'].startswith('waiting on ')
                or time.time() - run.get('wait_started_at', time.time()) <= 300):
            return
        self.store.post_step(room['room']['id'], 'dispatched', run['reason'],
                             event_key=f'wait:{run_id}:{run["stage"]}:{run["attempt"]}',
                             run_id=run_id, parent_request_id=run.get('parent_request_id'), refresh=True)

    def _dispatch(self, run_id):
        from companion.team_transport import config_fingerprint, lane_config, read_config
        self.store.check_namespace()
        room, snapshot = self.store.find_run(self.store._read(), run_id)
        if snapshot['status'] in TERMINAL or snapshot['phase'] != 'pending':
            return
        self._release_finished_stage(run_id)
        room, snapshot = self.store.find_run(self.store._read(), run_id)
        if snapshot['status'] in TERMINAL or snapshot['phase'] != 'pending':
            return
        binding = snapshot.get('submitted_config')
        try:
            if (not binding or binding.get('version') not in (2, 3) or binding != read_config(self.store.root)
                    or snapshot.get('binding') != config_fingerprint(binding)):
                raise PermissionError('submission_binding_changed_or_missing')
            lane = self._select_lane(binding, snapshot)
            selected = {**snapshot, 'lane': lane}
            if snapshot['stage'] in {'build', 'review'} and snapshot.get('execution_mode') not in SINGLE_STAGES:
                task_path = _task_workspace(snapshot.get('workspace_run_id', run_id), lane)
                if snapshot.get('workspace') and Path(snapshot['workspace']).resolve() != task_path.resolve():
                    raise PermissionError('task_workspace_changed')
                selected['workspace'] = str(task_path)
                if snapshot['stage'] == 'review':
                    from companion.team_workspace import validate_files
                    validate_files(snapshot.get('files', []), task_path)
                    if self._revision(selected) != snapshot['revision']:
                        raise ValueError('workspace_revision_changed')
            work_path = selected.get('workspace', lane['workspace'])
            previous = snapshot.get('claim')
            if previous and previous['repo'] != work_path and not snapshot.get('claim_released'):
                self._release_finished_stage(run_id, force=True)
                room, snapshot = self.store.find_run(self.store._read(), run_id)
                if snapshot['status'] in TERMINAL or snapshot['phase'] != 'pending':
                    return
                selected = {**snapshot, 'lane': lane, 'workspace': work_path}
            delivery_binding = {**binding, 'lane': deepcopy(lane)} if binding['version'] == 3 else lane_config(binding, lane)
            run_dir = self.store.root / 'runs' / run_id
            name = f'{snapshot["stage"]}-{snapshot["attempt"]}'
            assignment, result = run_dir / f'{name}.md', run_dir / f'{name}.json'
            content = self._assignment(room, selected, result)
            grant = binding['controller']
            with self.controller.reserve_dispatch(task_id=grant['task_id'], version=grant['version'],
                    repo=work_path, owner='team', run_id=run_id, stage=snapshot['stage'],
                    attempt=snapshot['attempt'], participants={_stage_role(snapshot): lane['participants'][_stage_role(snapshot)]},
                    previous_claim=None if snapshot.get('claim_released') else snapshot.get('claim'),
                    **({'builder': snapshot['builder']} if snapshot.get('builder', 'codex') != 'codex' else {})) as reservation:
                self.controller._dispatch_authority(
                    task_id=grant['task_id'], version=grant['version'], repo=work_path,
                    participants=lane['participants'], actions=[f'use_{_stage_role(snapshot)}'])
                with self.store.edit() as data:
                    self.store.check_namespace(data)
                    _, run = self.store.find_run(data, run_id)
                    if run != snapshot:
                        return
                    if read_config(self.store.root) != binding:
                        raise PermissionError('submission_binding_changed')
                    if result.exists() or result.is_symlink():
                        raise ValueError('reply_exists_for_unsent_stage')
                    # Legacy local leases remain a conservative migration fence.
                    others = [r for item in data['rooms'].values() for r in item['runs'] if r['id'] != run_id]
                    current_claims = self.controller._claims()['claims']
                    blockers = [r for r in others if (r['role_lease'] or r['write_lease'])
                                and not any(_saved_claim_matches(c, r) for c in current_claims)]
                    if blockers:
                        raise ValueError('uncertain_worker' if any(r['status'] in TERMINAL for r in blockers)
                                         else 'workspace_or_participant_claimed')
                    role, status = _stage_role(run), STAGES[run['stage']][1]
                    proposed = deepcopy(data)
                    _, proposed_run = self.store.find_run(proposed, run_id)
                    proposed_run.update(status=status, phase='dispatching', role_lease=role,
                               lane=deepcopy(lane),
                               write_lease=True, dispatch_pid=os.getpid(),
                               expires_at=time.time() + STAGE_TIMEOUT_SECONDS,
                               assignment_path=str(assignment), result_path=str(result),
                               reason=f'{role.title()} handoff is being sent; acknowledgment is not yet confirmed.')
                    if selected.get('workspace'):
                        proposed_run['workspace'] = selected['workspace']
                    if run['stage'] == 'build':
                        for task in proposed_run['tasks']:
                            task['status'] = 'building'
                    def admission(candidate):
                        proposed_run['claim'] = candidate
                        require_admission(data, proposed, claims=current_claims, candidate_claim=candidate)
                    claim = reservation.commit(before_persist=admission)
                    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    assignment.write_text(content)
                    assignment.chmod(0o600)
                    run.update(proposed_run)
                    run.pop('claim_released', None)
                    run.pop('wait_started_at', None)
                    send = dict(role=role, run_id=run_id, stage=run['stage'], attempt=run['attempt'],
                                assignment_path=assignment, result_path=result, binding=deepcopy(delivery_binding))
        except (PermissionError, ValueError, OSError, subprocess.SubprocessError) as exc:
            # If commit succeeded but the Team write failed, only fail_run may
            # classify it; retaining the claim is essential after that crash gap.
            self.store.check_namespace()
            claims = self.controller.claims()
            orphan = any(c['owner'] == 'team' and c['run_id'] == run_id and c != snapshot.get('claim')
                         for c in claims)
            if 'claim' in locals() or orphan:
                self.fail_run(run_id)
                raise
            with self.store.edit() as data:
                _, run = self.store.find_run(data, run_id)
                if run != snapshot:
                    return
                if isinstance(exc, TeamCapacityError):
                    _finish(run, 'needs_input', str(exc))
                elif str(exc) == 'workspace_or_participant_claimed':
                    if self._busy_requires_recovery(binding, snapshot, claims):
                        _finish(run, 'needs_input', 'A reserved worker needs reconciliation before a fresh instruction.')
                    else:
                        run.update(status='queued', reason='Waiting for the reserved participants or workspace.')
                else:
                    log.warning('Team run %s refused before dispatch: %s', run_id, type(exc).__name__ + ': ' + str(exc)[:200])
                    _finish(run, 'needs_input', 'The submitted setup or authority no longer permits this stage. Inspect it on the Mac.')
            if str(exc) == 'workspace_or_participant_claimed':
                self._record_seat_wait(run_id)
            self._release_terminal(run_id)
            return
        try:
            receipt = self.transport.send(**send)
        except Exception:
            receipt = {'state': 'ambiguous'}
        with self.store.edit() as data:
            self.store.check_namespace(data)
            _, run = self.store.find_run(data, run_id)
            if run.get('claim') != claim or run['phase'] != 'dispatching':
                return
            if isinstance(receipt, dict) and not _receipt_id(receipt.get('id')):
                _finish(run, 'uncertain', 'Delivery receipt was invalid. Inspect the native chat before continuing.', release=False)
            elif isinstance(receipt, dict) and receipt.get('state') == 'queued':
                from companion.team_transport import codex_queue_reason
                reason = (codex_queue_reason(_run_binding(run)['participants']['codex'],
                                             codex_home=getattr(self.transport, 'codex_home', None)) if role == 'codex'
                          else 'Delivered to the visible chat; waiting for its complete task reply.')
                run.update(phase='waiting', reason=reason, delivery_id=receipt.get('id'))
                if role == 'codex':
                    run['wait_started_at'] = time.time()
            elif isinstance(receipt, dict) and receipt.get('state') == 'blocked':
                _finish(run, 'needs_input', 'The selected participant is unavailable. Connect it on the Mac.')
            else:
                _finish(run, 'uncertain', 'Delivery was not confirmed. Inspect the native chat; this request will not be resent.', release=False)
        self._release_terminal(run_id)
        self._post_codex_progress(run_id)
