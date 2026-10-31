"""Authenticated Team surface and a worker that exits when no work is active."""
import inspect
import json
import logging
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from companion import webauth
from companion.errors import ApiError
from companion.settings import get_settings
from companion.team_chat import ACTIVE, MAX_TEXT, TeamCapacityError, TeamService, TeamStateError, TeamStore
from companion.team_log import append_details, details_index, read_details
from companion.team_transport import VisibleTeamTransport, open_codex_chat
from companion.team_voice import LIMITS, chat_line

router = APIRouter()
_log = logging.getLogger(__name__)
_workers = {}
_lead_workers = {}
_delegation_workers = {}
_worker_lock = threading.Lock()
_LOOPBACK = {'127.0.0.1', '::1', 'localhost'}


def get_store():
    settings = get_settings()
    if not settings.owner_machine or settings.tenant != 'personal':
        raise ApiError(403, 'owner_only', 'Team runs on the owner’s Mac.')
    return TeamStore()


def get_service(store=Depends(get_store)):
    return TeamService(store, VisibleTeamTransport(store.root, controller=store.controller, show_codex=open_codex_chat))


def get_lead(store=Depends(get_store)):
    # Home owns configuration and absent-configuration behavior; no discovery here.
    from companion.team_lead_config import build_lead
    lead = build_lead(store, get_settings())
    lead.context_provider = lambda request="": lead_context(store, request)
    return lead


def _submit_with_builder_check(submit, *args, **kwargs):
    if kwargs.get('execution_mode') in {'worker', 'code', 'code_review'}:
        try:
            inspect.signature(submit).bind(*args, **kwargs)
        except TypeError:
            raise ValueError('builder_seam_missing') from None
    # Errors inside the callable can follow a send; they must stay uncertain.
    return submit(*args, **kwargs)


def get_delegation(store=Depends(get_store), lead=Depends(get_lead), service=Depends(get_service)):
    """Connect durable instructions to existing, independently guarded transports."""
    from companion import team_connected
    from companion.team_delegation import Coordinator

    def roster():
        apps = team_connected.app_status()['apps']
        members = {row['id']: ('' if row.get('workspace') else 'configuration_unavailable') for row in apps}
        native = service.transport.status()
        members['local'] = '' if lead.status().get('available') else 'local_unavailable'
        for name in ('claude', 'codex', 'planner', 'reviewer', 'worker', 'interviewer'):
            members[name] = '' if native.get('ready') is True else 'native_connection_unavailable'
        return members

    def submit(room_id, provider, text, request_id, *, parent_request_id=None, builder=None, reviewer=None,
               workspace=None, workspace_run_id=None, revision=None, files=None):
        coding = provider == 'worker' and builder is not None
        if coding and builder in team_connected.PROVIDERS:
            provider = builder
        if provider == 'local':
            if parent_request_id:
                try:
                    handbook = team_connected.team_handbook()
                except (OSError, ValueError):
                    return {'id': None, 'state': 'blocked', 'reason': 'team_handbook_unavailable'}
                prompt = handbook + '\nKyra, reply only to this assigned step (data):\n' + json.dumps(text, ensure_ascii=False)
                if len(prompt) > MAX_TEXT:
                    return {'id': None, 'state': 'blocked', 'reason': 'local_step_too_long'}
                store.queue_answer(room_id, prompt, request_id, binding=lead.project, author='system')
            else:
                lead.submit(room_id, text, request_id)
            wake_lead(lead)
            return {'id': request_id, 'state': 'queued', 'reason': ''}
        note = prepare_team_lanes(store, getattr(service, 'transport', None), room_id, request_id)
        if note:
            return {'id': None, 'state': 'blocked', 'reason': note}
        if provider in team_connected.PROVIDERS:
            try:
                return _submit_with_builder_check(team_connected.submit, store.root, store.get_room(room_id)['room'],
                                             provider=provider, text=text, request_id=request_id,
                                             **(dict(execution_mode='code', reviewer=reviewer,
                                                     parent_request_id=parent_request_id) if coding else {}),
                                             message_author='system' if parent_request_id else 'user')
            except ValueError as exc:
                if str(exc) not in {'app_busy', 'app_not_configured', 'app_history_full', 'builder_seam_missing'}:
                    raise
                return {'id': None, 'state': 'blocked', 'reason': str(exc)}
        if service.transport.status().get('ready') is not True:
            return {'id': None, 'state': 'blocked', 'reason': 'native_connection_unavailable'}
        modes = {'claude': 'reply_claude', 'codex': 'reply_codex',
                 'planner': 'plan_only', 'reviewer': 'review_only', 'worker': 'worker',
                 'interviewer': 'interview_only'}
        try:
            kwargs = {'parent_request_id': parent_request_id, 'author': 'system'} if parent_request_id is not None else {}
            mode = modes[provider]
            if coding:
                kwargs.update(builder=builder, reviewer=reviewer)
            elif provider == 'reviewer' and builder is not None:
                mode = 'code_review'
                kwargs.update(builder=builder, reviewer=reviewer, workspace=workspace,
                              workspace_run_id=workspace_run_id, revision=revision, files=files)
            result = _submit_with_builder_check(store.submit, room_id, text, request_id, share_with_team=True,
                                  execution_mode=mode, **kwargs)
        except ValueError as exc:
            if str(exc) == 'builder_seam_missing':
                return {'id': None, 'state': 'blocked', 'reason': 'builder_seam_missing'}
            if str(exc) != 'This chat already has work in progress or awaiting reconciliation':
                raise
            return {'id': None, 'state': 'blocked', 'reason': 'native_room_busy'}
        wake_service(service)
        return {'id': result['run_id'], 'state': 'queued', 'reason': ''}

    def full_text(room_id, message):
        details = read_details(store.root, room_id, message.get('details_id', message['id']))
        return details['details'] if details else message.get('text', '')

    def child_status(room_id, provider, ident):
        if provider == 'local':
            answer = store.get_answer(room_id, ident)
            state = 'completed' if answer['status'] == 'answered' else answer['status']
            return {'id': ident, 'state': state, 'reason': answer.get('notice', ''), 'text': full_text(room_id, answer)}
        if provider in team_connected.PROVIDERS:
            rows = team_connected.ConnectedStore(store.root).for_room(room_id)
            row = next(row for row in rows if row['id'] == ident)
            return {**row, **row.get('completion', {}), 'text': full_text(room_id, row),
                    'requires_owner_decision': row.get('requires_owner_decision', False)}
        run = next(row for row in store.get_room(room_id)['runs'] if row['id'] == ident)
        state = run['status']
        if state in {'planning', 'building', 'reviewing', 'replying', 'stopping'}:
            state = 'running'
        elif state == 'needs_input' and not run.get('parent_request_id'):
            state = 'blocked'
        value = {'id': ident, 'state': state, 'reason': run.get('reason', ''), 'stage': run.get('stage')}
        value.update(run.get('completion', {}))
        value.update({k: run[k] for k in ('session_id', 'surface') if k in run})
        if run.get('requires_owner_decision') is True:
            value['requires_owner_decision'] = True
        replies = [m for m in store.get_room(room_id)['messages']
                   if m.get('run_id') == ident and m.get('kind') == 'result']
        value['text'] = full_text(room_id, replies[-1]) if replies else ''
        if state == 'completed' and provider == 'planner' and run.get('execution_mode') == 'plan_only':
            value['plan'] = [dict(provider=t['assignee'], title=t['title'], instruction=t['instruction'],
                                  **{k: t[k] for k in ('order', 'mode', 'acceptance') if k in t}) for t in run['tasks']]
            if 'planner_kind' in run:
                value['planner_kind'] = run['planner_kind']
        if provider == 'reviewer':
            if 'verdict' in run:
                value['verdict'] = run['verdict']
            elif state == 'completed':
                # Legacy TeamStore omits verdict; its completed review contract guarantees pass.
                value['verdict'] = 'pass'
        if state == 'completed' and provider == 'interviewer':
            value['ready'] = run.get('interview_ready') is True
            if run.get('planner_kind') == 'answer':
                value.update(planner_kind='answer', plan=[])
        return value

    def child_stop(room_id, provider, ident):
        if provider == 'local':
            return lead.stop(room_id, ident)
        if provider in team_connected.PROVIDERS:
            return team_connected.ConnectedStore(store.root).stop(room_id, ident)
        return service.stop(room_id, run_id=ident)

    coordinator = Coordinator(store, lead, roster=roster, submit=submit,
                              child_status=child_status, child_stop=child_stop)
    coordinator.prepare = getattr(getattr(service, 'transport', None), 'ensure_claude_lanes', lambda: None)
    return coordinator


def wake_delegation(coordinator):
    """The browser may close; persisted consent and transport claims own the work."""
    settings = get_settings()
    if not (settings.owner_machine and settings.tenant == 'personal' and settings.inline_worker):
        return
    key = str(coordinator.store.root.resolve())
    with _worker_lock:
        if key in _delegation_workers:
            _delegation_workers[key].set()
            return
        wake = threading.Event()
        _delegation_workers[key] = wake

    def run():
        try:
            while True:
                wake.clear()
                pending = coordinator.pending()
                for item in pending:
                    coordinator.advance(item['room_id'], item['request_id'])
                wake_lead(coordinator.lead)
                with _worker_lock:
                    if not coordinator.pending() and not wake.is_set():
                        _delegation_workers.pop(key, None)
                        return
                wake.wait(2)
        except Exception:
            _log.error('Team delegation stopped; saved instructions remain inspectable')
        finally:
            with _worker_lock:
                if _delegation_workers.get(key) is wake:
                    _delegation_workers.pop(key, None)
    threading.Thread(target=run, name='kyra-delegation', daemon=True).start()


def wake_lead(service):
    """Resume saved local obligations; wait only while a busy flight holds them."""
    settings = get_settings()
    if not (settings.owner_machine and settings.tenant == 'personal' and settings.inline_worker):
        return
    from companion.team_lead import has_pending_answers
    if not has_pending_answers(service.store):
        return
    key = str(service.store.root.resolve())
    with _worker_lock:
        if key in _lead_workers:
            _lead_workers[key].set()
            return
        wake = threading.Event()
        _lead_workers[key] = wake

    def run():
        try:
            while True:
                wake.clear()
                if service.run_once():
                    continue
                pending = has_pending_answers(service.store)
                with _worker_lock:
                    if not pending and not wake.is_set():
                        _lead_workers.pop(key, None)
                        return
                wake.wait(2)
        except Exception:
            # Thread boundary: never expose raw diagnostics or retry unknown failures.
            _log.error('Local answer worker stopped; inspect saved status before retrying')
        finally:
            with _worker_lock:
                if _lead_workers.get(key) is wake:
                    _lead_workers.pop(key, None)
    try:
        threading.Thread(target=run, name='kyra-local-lead', daemon=True).start()
    except RuntimeError:
        with _worker_lock:
            if _lead_workers.get(key) is wake:
                _lead_workers.pop(key, None)
        raise


def wake_service(service):
    """Short advances only; no timer or thread survives an idle/blocked team."""
    key = str(service.store.root.resolve())
    with _worker_lock:
        if key in _workers:
            _workers[key].set()
            return
        wake = threading.Event()
        _workers[key] = wake
    from companion import team_replies
    from companion.team_threads import Store
    threads = Store(service.store.root)

    def pump():
        try:
            return team_replies.pump(service, threads)
        except Exception:
            # A reply failure never stops native runs; the waiting rows stay for the next wake.
            _log.error('Team replies could not be delivered this pass')
            return {}

    def run():
        try:
            while True:
                wake.clear()
                pump()  # waiting replies become runs once their agent is idle
                active = service.store.active_runs()
                for run_id in active:
                    try:
                        service.step(run_id)
                    except Exception:
                        service.fail_run(run_id)
                if not service.store.active_runs() and pump().get('sent'):
                    continue  # collect finished replies and send the next waiting one before going idle
                with _worker_lock:
                    if not service.store.active_runs() and not wake.is_set():
                        _workers.pop(key, None)
                        return
                wake.wait(2)
        except Exception:
            # Preserve leases after unknown failures; never retry an uncertain send.
            _log.error('Team worker stopped; inspect local state before resuming')
            with _worker_lock:
                _workers.pop(key, None)
    threading.Thread(target=run, name='kyra-team', daemon=True).start()


def visible_messages(messages, runs, root, room_id, *, local_only=False, index=None):
    if local_only:
        return [dict(m) for m in messages if m.get('hidden') is not True and m.get('author') != 'system']
    if root is not None and index is None:
        index = details_index(root, room_id)
    interviews = {r['id'] for r in runs if r.get('execution_mode') == 'interview_only'}
    visible = []
    for original in messages:
        message = dict(original)
        pending = message.get('kind') == 'answer' and message.get('status') in {'queued', 'running', 'stopping'}
        if message.get('author') != 'user' and root is not None and not pending:
            if not message.get('details_id'):
                append_details(root, room_id, message, message.get('text', ''), index=index)
            message['details_id'] = message.get('details_id', message['id'])
        if message.get('hidden') is True or message.get('author') == 'system':
            continue
        if message.get('author') != 'user':
            category = 'report' if message.get('kind') == 'report' else (
                'interview' if message.get('run_id') in interviews else 'reply')
            limit, lines = LIMITS[category]
            message['text'] = chat_line(message.get('text', ''), limit=limit, lines=lines)
        visible.append(message)
    return visible


def room_view(value, root=None, *, index=None):
    run = value['runs'][-1] if value['runs'] else None
    # Internal paths and participant binding details never enter the browser.
    public_run = {k: run[k] for k in ('id', 'status', 'stage', 'attempt', 'reason', 'learning') if k in run} if run else None
    if public_run and 'reason' in public_run:
        public_run['reason'] = chat_line(run['reason'])
        if public_run['reason'] != run['reason']:
            public_run['reason_full'] = run['reason']  # legacy reasons remain available in the more panel
    messages = []
    planned = {r['id'] for r in value['runs']
               if r.get('execution_mode') == 'plan_only' and r.get('planner_kind') in {'task', 'plan'}}
    for message in visible_messages(value['messages'], value['runs'], root, value['room']['id'],
                                    local_only=value['room'].get('local_only', False), index=index):
        if message.get('kind') == 'result' and message.get('run_id') in planned:
            # Planning is progress; each assigned member still has only one reply.
            messages.append({**message, 'kind': 'step', 'step': 'returned'})
        elif message.get('kind') == 'answer':
            keys = ('id', 'author', 'kind', 'text', 'created_at', 'request_id', 'reply_to',
                    'shared', 'status', 'stop_requested', 'sources', 'route', 'model',
                    'notice', 'omitted_counts', 'missing', 'details_id')
            public = {k: message[k] for k in keys if k in message}
            if message.get('status') == 'running' and message.get('stop_requested'):
                public['status'] = 'stopping'
            messages.append(public)
        else:
            messages.append(message)
    files = run_files(root, messages) if root is not None else {}
    for message in messages:
        if message.get('kind') == 'result' and message.get('stage') == 'build' and files.get(message.get('run_id')):
            message['files'] = files[message['run_id']]
    if public_run and root is not None and run['status'] in ACTIVE:
        public_run['progress_tail'] = read_progress(root, run['id'], 3)
    return {**value['room'], 'messages': messages, 'status': run['status'] if run else 'idle',
            'run': public_run, 'tasks': run['tasks'] if run else [], 'codex_chats': codex_chats(value['runs'])}


_RUN_ID = re.compile(r'[a-f0-9]{32}')
PROGRESS_KEEP = 200


def _progress_path(root, run_id):
    return Path(root) / 'runs' / run_id / 'progress.jsonl' if _RUN_ID.fullmatch(str(run_id or '')) else None


def read_progress(root, run_id, count=20):
    """The last `count` progress lines a worker posted for this run, oldest first."""
    path = _progress_path(root, run_id)
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-count:] if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('text'), str):
            rows.append({k: row.get(k) for k in ('at', 'text', 'file')})
    return rows


def append_progress(store, run_id, text, file=None):
    """Append one line while the run is active; keep the newest PROGRESS_KEEP lines, mode 600."""
    import os
    import time
    path = _progress_path(store.root, run_id)
    status = next((r['status'] for room in store._read()['rooms'].values() for r in room['runs']
                   if r['id'] == run_id), None) if path else None
    if status is None:
        raise ApiError(404, 'not_found', 'Unknown run.')
    if status not in ACTIVE:
        raise ApiError(409, 'run_inactive', 'That run is no longer active.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {'at': time.time(), 'text': text, 'file': file}
    with _worker_lock:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            lines = []
        lines = [*lines, json.dumps(row, ensure_ascii=False)][-PROGRESS_KEEP:]
        temporary = path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines) + '\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    return row


REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKTREE = re.compile(r'^.*?/\.claude/worktrees/[^/]+/')


def repo_relative(path):
    """A repo-relative path for display, or None. Worktree and checkout absolute paths fold to the same name."""
    text = str(path or '').strip()
    if text.startswith('/'):
        folded = _WORKTREE.sub('', text, count=1)
        if folded == text:
            prefix = str(REPO_ROOT) + '/'
            if not text.startswith(prefix):
                return None
            folded = text[len(prefix):]
        text = folded
    return text if text and '..' not in text.split('/') else None


def run_files(root, messages):
    """Run id -> up to 32 repo-relative paths the build steps of that run reported changing."""
    found = {}
    for run_id in {m.get('run_id') for m in messages if m.get('kind') == 'result' and m.get('stage') == 'build'}:
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
            continue
        names = []
        for build in sorted((Path(root) / 'runs' / run_id).glob('build-*.json')):
            try:
                listed = json.loads(build.read_text(encoding='utf-8')).get('files') or []
            except (OSError, ValueError, AttributeError):
                continue
            for item in listed if isinstance(listed, list) else []:
                name = repo_relative(item)
                if name and name not in names:
                    names.append(name)
        if names:
            found[run_id] = names[:32]
    return found


_THREAD_ID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def codex_chats(runs):
    """Run id -> the Codex chat id it was sent to, so the page can link straight to that chat. Only the id."""
    from companion.team_chat import _run_binding
    chats = {}
    for run in runs:
        try:
            ident = (_run_binding(run) or {})['participants']['codex']['id']
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(ident, str) and _THREAD_ID.fullmatch(ident):
            chats[run['id']] = ident
    return chats


class RoomInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=100)
    local_only: bool = False


class MessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    share_with_team: bool = False
    execution_mode: Literal["plan", "worker"] = "plan"


class AppTaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provider: Literal['cursor', 'antigravity']
    text: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class DelegationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    mode: Literal['auto', 'cursor', 'antigravity', 'claude', 'codex', 'local', 'all'] = 'auto'
    consent: bool = False
    plan_consent: bool = False
    interview: bool = True  # a planned request interviews the owner before any build (AGENTS.md section 3)


class ProgressInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=160, pattern=r'^[^\r\n]+$')
    file: str | None = Field(default=None, max_length=300, pattern=r'^[^\r\n]+$')


class AnswerStopInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)


class NoteInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    message_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class NoteConfirmInput(NoteInput):
    note_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class BillingConfirmInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    confirm: Literal['subscription_overage_off']


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


def action(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError:
        raise ApiError(404, 'not_found', 'Chat not found.') from None
    except TeamStateError:
        raise
    except TeamCapacityError:
        raise ApiError(409, 'team_capacity',
                       'New work cannot start safely in this Team store. Check storage and setup on the Mac.') from None
    except RuntimeError as exc:
        from companion.local_lead_inference import LeadBusyError, LeadInferenceError
        if isinstance(exc, LeadBusyError):
            raise ApiError(409, 'lead_busy', 'Local answers are busy. Wait for one to finish or stop an answer.') from None
        if isinstance(exc, LeadInferenceError):
            raise ApiError(503, exc.code, 'Local answering is unavailable. Check its setup on the Mac.') from None
        raise
    except ValueError:
        raise ApiError(409, 'conflict', 'Check the message and chat status before continuing.') from None


@router.get('/team')
def page():
    root = Path(__file__).resolve().parents[2] / 'web'
    html = (root / 'team.html').read_text(encoding='utf-8')
    for asset in ('team.js', 'team.css', 'team-voice.js', 'team-history.js', 'team-history.css', 'team-rail.js', 'team-ui.css', 'team-needs.js', 'team-threads.js'):
        path = root / asset
        if path.is_file():
            html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={path.stat().st_mtime_ns}"')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@router.post('/api/team/transcribe')
def transcribe(audio: UploadFile, store=Depends(get_store)):
    """Local speech recognition only; caller chooses the ordinary Team message path."""
    def _transcribe_team_upload(audio):
        # Public build stand-in: the speech model is owned by the private web app.
        raise ApiError(501, 'not_included', 'Speech recognition is not included in this build.')
    audio.file.seek(0, 2)
    size = audio.file.tell()
    audio.file.seek(0)
    if size > 5_000_000:
        raise ApiError(413, 'audio_too_large', 'Keep the recording under 5 MB.')
    if size == 0:
        raise ApiError(422, 'audio_empty', 'No recording was received.')
    try:
        transcript = _transcribe_team_upload(audio)
    except ValueError as exc:
        if str(exc) == 'audio_too_long':
            raise ApiError(413, 'audio_too_long', 'Keep each recording under 90 seconds.') from None
        raise ApiError(422, 'audio_invalid', 'The recording could not be read. Try again or type your request.') from None
    if len(transcript) > 16000:
        raise ApiError(413, 'transcript_too_large', 'Try a shorter recording.')
    return {'transcript': transcript}


def capability_snapshot(store):
    from companion.team_capabilities import read_catalog
    from companion.team_connected import app_status
    _registry = None  # public build stand-in: the tool registry lives in the private web app
    catalog = read_catalog(registry=_registry, route_status=VisibleTeamTransport(store.root).status())
    apps = {row['id']: row for row in app_status()['apps']}
    for row in catalog['capabilities']:
        provider = row['id'].removeprefix('app:')
        if row['kind'] == 'app_route' and provider in apps:
            app = apps[provider]
            row.update(availability='available' if app['available'] else 'unavailable',
                       reason=app['reason'] or 'explicit_team_request',
                       description=f"Ask {row['name']} directly from Team. Its CLI workspace and permissions apply; native desktop history is unverified.")
    if get_settings().team_code_root is not None:
        catalog['capabilities'].append(dict(id='tool:read_project_code', kind='tool', name='read_project_code',
            description='Read bounded engineering excerpts from the configured project to explain its code.',
            availability='available', reason='configured_read_only', checked_at=catalog['checked_at'],
            boundaries=['Read-only excerpts; no private data folders, links, shell or code execution.']))
    return catalog


def usage_snapshot():
    from companion.team_usage_reader import get_snapshot
    return get_snapshot()


def lead_context(store, request=""):
    """Metadata from the server, never a browser-supplied capability assertion."""
    catalog = capability_snapshot(store)
    routes = [{key: row[key] for key in ('id', 'description', 'availability', 'reason')}
              for row in catalog['capabilities'] if row['kind'] == 'app_route']
    context = {'routes': routes, 'checked_at': catalog['checked_at'],
               'tools': [row['name'] for row in catalog['capabilities'] if row['kind'] == 'tool'],
               'skills': [row['name'] for row in catalog['capabilities'] if row['kind'] == 'skill'],
               'notices': catalog['notices'], 'truncated': catalog['truncated'],
               'authority': 'Capability does not grant access. Native dispatch rechecks current workspace authority. Private room history is never forwarded.'}
    from companion.team_code_context import lookup
    context['code_context'] = lookup(get_settings().team_code_root, request)
    encoded = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    while len(encoded.encode()) > 8192 and (context['skills'] or context['tools']):
        (context['skills'] if context['skills'] else context['tools']).pop()
        context['truncated'] = True
        encoded = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
    usage = usage_snapshot()
    selected = {'observed_at': usage['observed_at'], 'remaining_unit': 'percent', 'rows': []}
    for row in usage['rows']:
        item = {key: row.get(key) for key in ('app', 'window', 'status', 'remaining', 'observed_at', 'reset_at',
                                              'model_scope')}
        proposed = {**selected, 'rows': [*selected['rows'], item]}
        if len(json.dumps(proposed).encode()) > 1900:
            selected['truncated'] = True
            break
        selected = proposed
    return {'capability_context': encoded, 'usage_context': json.dumps(selected, separators=(',', ':'))}


@router.get('/api/team/capabilities')
def capabilities(store=Depends(get_store)):
    return capability_snapshot(store)


@router.get('/api/team/usage')
def usage(store=Depends(get_store)):
    return usage_snapshot()


@router.post('/api/team/usage/refresh')
def refresh_usage(body: EmptyInput, store=Depends(get_store)):
    from companion.team_usage_reader import get_snapshot
    return get_snapshot(refresh=True)


@router.get('/api/team/status')
def status(service=Depends(get_service), lead=Depends(get_lead)):
    snapshot, local = service.transport.status(), lead.status()
    for member in snapshot.get('participants', []):
        if member.get('id') == 'local':
            member.update(display_name='Kyra', initials='Ky', role='Local spokesperson and project context',
                          model=lead.project.get('model_id', '') if hasattr(lead, 'project') else '',
                          available=local.get('available') is True)
    from companion.team_connected import app_status
    apps = {row['id']: row for row in app_status()['apps']}
    for member in snapshot.get('participants', []):
        if member['id'] in apps:
            app = apps[member['id']]
            member.update(available=app['available'], role='Direct task in Kyra',
                          model='CLI session; native desktop history unverified', reason=app['reason'])
    return {**snapshot, 'local': local}


@router.get('/api/team/history')
def agent_history(agent: Literal['kyra', 'claude', 'codex', 'cursor', 'antigravity'], store=Depends(get_store)):
    from companion.team_history import history
    return {'agent': agent, 'entries': history(store._read(), store.root, agent)}


@router.post('/api/team/runs/{run_id}/progress', status_code=201)
def post_progress(run_id: str, body: ProgressInput, store=Depends(get_store)):
    return {'progress': append_progress(store, run_id, body.text.strip(), body.file)}


@router.get('/api/team/runs/{run_id}/progress')
def get_progress(run_id: str, store=Depends(get_store)):
    if _progress_path(store.root, run_id) is None:
        raise ApiError(404, 'not_found', 'Unknown run.')
    return {'progress': read_progress(store.root, run_id, 20)}


def _needs_you_reason(item, raw, runs):
    """Short plain words for why a chat waits on the owner, or None when it does not."""
    if any(r.get('state') == 'interviewing' for r in runs):
        return 'planner question waiting'
    shown = [m for m in raw.get('messages', []) if m.get('hidden') is not True and m.get('author') != 'system']
    if (item.get('status') == 'needs_input' or any(r.get('state') == 'needs_input' for r in runs)
            or any(m.get('kind') == 'answer' and m.get('status') == 'needs_input' for m in shown)):
        return 'input needed'
    for run in runs:
        for step in run.get('plan') or []:
            who = (step.get('blocked') or {}).get('who_must_act')
            if step.get('state') == 'blocked' and who == 'the owner':
                return f'step blocked: {who}'
    return None


@router.get('/api/team/rooms')
def rooms(store=Depends(get_store)):
    listing, data = store.list_rooms(), store._read()['rooms']
    coordinator = (get_delegation(store, get_lead(store), get_service(store))
                   if (store.root / 'delegations.sqlite3').exists() else None)
    for item in listing:
        raw = data.get(item['id'], {})
        runs = coordinator.for_room(item['id']) if coordinator is not None else []
        # Same visibility as the open chat: no hidden or system rows, and a delegation shows as its composer post.
        shown = [m for m in raw.get('messages', []) if m.get('hidden') is not True and m.get('author') != 'system'
                 and not str(m.get('request_id') or '').startswith('delegation-')
                 and isinstance(m.get('text'), str) and m['text'].strip()]
        shown += [{'text': r['text'], 'created_at': r['created_at']} for r in runs
                  if isinstance(r.get('text'), str) and r['text'].strip() and 'created_at' in r]
        shown.sort(key=lambda m: m.get('created_at') or 0)
        line = ' '.join(shown[-1]['text'].split()) if shown else ''
        item['recent_at'] = [m['created_at'] for m in shown[-50:] if m.get('created_at')]
        item['last_line'] = line if len(line) <= 90 else line[:89].rstrip() + '…'
        item['needs_you_reason'] = _needs_you_reason(item, raw, runs)
        item['needs_you'] = item['needs_you_reason'] is not None
    return {'rooms': listing}


@router.post('/api/team/rooms', status_code=201)
def create(body: RoomInput, store=Depends(get_store)):
    return {'room': action(store.create_room, body.title, local_only=body.local_only)}


@router.get('/api/team/rooms/{room_id}')
def room(room_id: str, store=Depends(get_store)):
    return {'room': connected_room(store, room_id)}


@router.get('/api/team/rooms/{room_id}/log/{message_id}')
def message_log(room_id: str, message_id: str, store=Depends(get_store)):
    if (not re.fullmatch(r'[a-f0-9]{32}', room_id)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', message_id)):
        raise ApiError(422, 'invalid', 'Invalid chat or message id.')
    action(store.get_room, room_id)
    details = read_details(store.root, room_id, message_id)
    if details is None:
        raise ApiError(404, 'not_found', 'Message details not found.')
    return {key: details[key] for key in ('author', 'kind', 'details', 'created_at')}


FILE_LIMIT = 256 * 1024
_CODE_ROOTS = ('src', 'web', 'docs', 'tests', 'scripts')


@router.get('/api/team/file')
def team_file(path: str = '', store=Depends(get_store)):
    """Read one text file for the file viewer: repo code roots plus two data folders, read-only."""
    name = repo_relative(path)
    parts = name.split('/') if name else []
    if not parts or '' in parts or any(part.startswith('.env') for part in parts):
        raise ApiError(422, 'invalid', 'That path cannot be shown.')
    data = Path(store.root).parent
    if parts[0] in _CODE_ROOTS:
        base = REPO_ROOT / parts[0]
    elif parts[:2] == ['data', 'private_docs']:
        base = data / 'private_docs'
    elif parts[:3] == ['data', 'team', 'runs']:
        base = Path(store.root) / 'runs'
    else:
        raise ApiError(422, 'invalid', 'That path cannot be shown.')
    skip = 1 if parts[0] in _CODE_ROOTS else 2 if parts[1] == 'private_docs' else 3
    size, raw = _read_inside(base, parts[skip:])
    try:
        if b'\0' in raw:
            raise UnicodeDecodeError('utf-8', raw, 0, 1, 'nul')
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return {'path': name, 'size': size, 'binary': True}
    return {'path': name, 'size': size, 'text': text}



def _read_inside(base, parts):
    """Bytes of base/parts, or a 404 or 413. No symlink anywhere below base, and the bytes come from the
    same open handle that was checked (a swap after the check cannot redirect the read or grow past the cap)."""
    import fcntl
    import os
    try:
        allowed = base.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ApiError(404, 'not_found', 'File not found.') from None
    walk = allowed
    for part in parts:  # a link in any component (`src/alias -> src/.env`) is refused, not followed
        walk = walk / part
        if not walk.exists() or walk.is_symlink():
            raise ApiError(404, 'not_found', 'File not found.')
    try:
        fd = os.open(walk, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
    except OSError:
        raise ApiError(404, 'not_found', 'File not found.') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ApiError(404, 'not_found', 'File not found.')
        if hasattr(fcntl, 'F_GETPATH'):  # macOS: the handle's real path must still sit inside the root
            real = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0])
            if not Path(real).is_relative_to(allowed):
                raise ApiError(404, 'not_found', 'File not found.')
        if info.st_size > FILE_LIMIT:
            raise ApiError(413, 'too_large', 'That file is larger than 256 KB.')
        raw = os.read(fd, FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise ApiError(413, 'too_large', 'That file is larger than 256 KB.')
    finally:
        os.close(fd)
    return len(raw), raw


def _file_name(path):
    """The repo-relative name /api/team/file would accept, or a 422."""
    name = repo_relative(path)
    parts = name.split('/') if name else []
    if (not parts or '' in parts or any(part.startswith('.env') for part in parts)
            or not (parts[0] in _CODE_ROOTS or parts[:2] == ['data', 'private_docs'] or parts[:3] == ['data', 'team', 'runs'])):
        raise ApiError(422, 'invalid', 'That path cannot be shown.')
    return name


def _git(cwd, *args):
    import subprocess
    out = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, timeout=20, check=False)
    if out.returncode != 0:
        raise ApiError(409, 'git_failed', 'Git could not compute that diff.')
    return out.stdout


def find_run(store, run_id):
    return next((r for room in store._read()['rooms'].values() for r in room['runs'] if r.get('id') == run_id), None)


@router.get('/api/team/diff')
def team_diff(run_id: str = '', path: str = '', store=Depends(get_store)):
    """Read-only unified diff of one file between a run's base and its task worktree head."""
    from companion.workflow_control import _task_worktree_of
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ApiError(422, 'invalid', 'Unknown run.')
    name = _file_name(path)
    run = find_run(store, run_id)
    if run is None:
        raise ApiError(404, 'not_found', 'Run not found.')
    repo = Path(_git(REPO_ROOT, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()).parent  # the main checkout, also from a worktree
    work = Path(run.get('workspace') or repo / '.claude' / 'worktrees' / f'task-{run_id}')
    if not work.is_dir():
        return JSONResponse({'run_id': run_id, 'path': name, 'missing_worktree': True}, status_code=404)
    if not _task_worktree_of(str(work), str(repo)):
        raise ApiError(422, 'invalid', 'That run has no task worktree.')
    head = _git(work, 'rev-parse', 'HEAD').decode().strip()
    base = run.get('base') if isinstance(run.get('base'), str) and re.fullmatch(r'[a-f0-9]{40}', run['base']) else None
    base = base or _git(work, 'merge-base', 'origin/master', 'HEAD').decode().strip()
    dirty = bool(_git(work, 'status', '--porcelain', '--', name).strip())
    span = [base] if dirty else [f'{base}..HEAD']
    raw = _git(work, 'diff', '--no-color', '--unified=3', *span, '--', name)
    body = {'run_id': run_id, 'path': name, 'base': base, 'head': head,
            'source': 'working_tree' if dirty else 'committed', 'truncated': len(raw) > FILE_LIMIT}
    body['diff'] = raw[:FILE_LIMIT].decode('utf-8', errors='replace')
    return body

def connected_room(store, room_id):
    from companion.team_connected import project_room
    raw = action(store.get_room, room_id)
    local_only = raw['room'].get('local_only', False)
    index = {} if local_only else details_index(store.root, room_id)
    view = project_room(room_view(raw, store.root, index=index), store.root, index=index)
    view['messages'] = visible_messages(view['messages'], raw['runs'], store.root, room_id,
                                        local_only=local_only, index=index)
    # Reads expose saved instructions; they never wake or dispatch a model.
    if (store.root / 'delegations.sqlite3').exists():
        coordinator = get_delegation(store, get_lead(store), get_service(store))
        view['delegations'] = coordinator.for_room(room_id)
        for parent in view['delegations']:
            for child in (parent.get('children') or {}).values():
                tail = read_progress(store.root, child.get('id'), 3) if isinstance(child, dict) else []
                if tail:
                    child['progress_tail'] = tail
    else:
        view['delegations'] = []
    collapse_delegated_requests(view, raw['runs'])
    return view


def collapse_delegated_requests(view, native_runs=()):
    """One instruction and final reply/skip per child; retry diagnostics stay in the audit store."""
    from companion.team_delegation import _identity
    hidden = set()
    quiet_results = set()
    # Provider state can be newer than the coordinator's last poll. Reads must
    # not briefly show the failure that the next advance will retry.
    outcomes = {r['id']: (r['status'], r.get('requires_owner_decision', False)) for r in native_runs}
    outcomes.update({r['id']: (r['state'], r['state'] == 'needs_input') for r in view.get('app_runs', [])})
    outcomes.update({m['request_id']: (m.get('status'), False) for m in view['messages']
                     if m.get('kind') == 'answer' and m.get('request_id')})
    originals = []
    for parent in view['delegations']:
        previous = list(parent.get('previous_children', {}).values())
        children = [*parent['children'].values(), *previous]
        if parent.get('retry_policy') == 1:
            for child in children:
                state, decision = outcomes.get(child.get('id'), (child['state'], child.get('requires_owner_decision', False)))
                if (child.get('id') and state in {'failed', 'blocked', 'needs_input'} and not decision
                        and (child in previous or child.get('skipped') or (
                            not parent.get('stop_requested') and parent['state'] not in {'uncertain', 'stopped', 'stopping'}))):
                    quiet_results.add(child['id'])
        requests = {child['id'] + '-request' for child in children if child.get('id')}
        identities = {child['request_id'] for child in children if child.get('request_id')}
        if parent.get('lead_request_id'):
            identities.add(parent['lead_request_id'])
        linked = [m for m in view['messages'] if m.get('author') == 'user'
                  and (m['id'] in requests or m.get('request_id') in identities)]
        if 'text' in parent and 'created_at' in parent:
            # The delegation is the durable composer post; children are derived work.
            hidden.update(m['id'] for m in linked)
            originals.append(dict(id=_identity(parent['room_id'], parent['request_id'], 'message'),
                                  author='user', text=parent['text'], created_at=parent['created_at'],
                                  request_id=parent['request_id'], shared=True))
        else:
            hidden.update(m['id'] for m in linked[1:])  # Older rows have no composer timestamp.
    hidden.update(m['id'] for m in view['messages'] if (
        (m.get('kind') == 'result' and m.get('run_id') in quiet_results)
        or (m.get('kind') == 'app_result' and m['id'] in quiet_results)
        or (m.get('kind') == 'answer' and m.get('request_id') in quiet_results)))
    view['app_runs'] = [r for r in view.get('app_runs', []) if r['id'] not in quiet_results]
    view['messages'] = sorted([m for m in view['messages'] if m['id'] not in hidden] + originals,
                              key=lambda m: m.get('created_at', 0))


def prepare_team_lanes(store, transport, room_id, request_id):
    note = getattr(transport, 'ensure_claude_lanes', lambda: None)()
    if note:
        store.post_step(room_id, 'stopped', note, event_key=f'lane-closed:{request_id}')
    return note


def start_delegation(room_id, body, coordinator):
    """Shared phone/web consent boundary; preparation never grants send authority."""
    room = action(coordinator.store.get_room, room_id)['room']
    if room['local_only'] or body.consent is not True:
        raise ApiError(403, 'consent_required', 'This request cannot be shared with Team.')
    note = getattr(coordinator, 'prepare', lambda: None)()
    if note:
        coordinator.store.post_step(room_id, 'stopped', note, event_key=f'lane-closed:{body.request_id}')
        return {'state': 'blocked', 'reason': note}
    result = action(coordinator.create, room_id, **body.model_dump())
    wake_delegation(coordinator)
    return result


@router.post('/api/team/rooms/{room_id}/delegations')
def delegate(room_id: str, body: DelegationInput, coordinator=Depends(get_delegation)):
    result = start_delegation(room_id, body, coordinator)
    return {'delegation': result, 'room': connected_room(coordinator.store, room_id)}


class InterviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    text: str = Field(min_length=1, max_length=8000)
    approve: bool = False


@router.post('/api/team/rooms/{room_id}/delegations/interview')
def answer_interview(room_id: str, body: InterviewInput, coordinator=Depends(get_delegation)):
    result = action(coordinator.answer_interview, room_id, body.request_id, body.text, body.approve)
    wake_delegation(coordinator)
    return {'delegation': result, 'room': connected_room(coordinator.store, room_id)}


@router.post('/api/team/rooms/{room_id}/delegations/{request_id}/resume')
def resume_delegation(room_id: str, request_id: str, body: EmptyInput, coordinator=Depends(get_delegation)):
    result = action(coordinator.resume, room_id, request_id)
    wake_delegation(coordinator)
    return {'delegation': result, 'room': connected_room(coordinator.store, room_id)}


@router.post('/api/team/rooms/{room_id}/delegations/stop')
def stop_delegation(room_id: str, body: AnswerStopInput, coordinator=Depends(get_delegation)):
    result = action(coordinator.stop, room_id, body.request_id)
    return {'delegation': result, 'room': connected_room(coordinator.store, room_id)}


@router.get('/api/team/apps')
def apps():
    from companion.team_connected import app_status
    return app_status()


@router.post('/api/team/apps/refresh')
def refresh_apps(body: EmptyInput):
    from companion.team_connected import app_status
    return app_status(refresh=True)


@router.post('/api/team/apps/{provider}/confirm-billing')
def confirm_app_billing(provider: Literal['cursor', 'antigravity'], body: BillingConfirmInput, request: Request,
                        store=Depends(get_store)):
    from urllib.parse import urlsplit

    from companion.team_connected import app_status, confirm_billing
    # Only the owner's click on this page: a browser POST always carries its Origin.
    origin = urlsplit(request.headers.get('origin') or '')
    if (origin.scheme, origin.netloc) != (request.url.scheme, request.url.netloc):
        raise ApiError(403, 'bad_origin', 'Confirm billing from the Team page on this Mac.')
    try:
        confirm_billing(provider)
    except ValueError:
        raise ApiError(409, 'app_signed_out', 'The app is not signed in on this Mac. Sign in, then confirm again.') from None
    return app_status()


@router.post('/api/team/rooms/{room_id}/apps')
def app_task(room_id: str, body: AppTaskInput, store=Depends(get_store)):
    from companion.team_connected import submit
    value = action(store.get_room, room_id)['room']
    if value['local_only']:
        raise ApiError(403, 'local_only_room', 'This chat stays local. Choose a shared chat for app work.')
    result = action(submit, store.root, value, **body.model_dump())
    return {'room': connected_room(store, room_id), 'app_run': {key: result[key] for key in ('id','state','reason')}}


@router.post('/api/team/rooms/{room_id}/apps/{run_id}/stop')
def stop_app_task(room_id: str, run_id: str, body: EmptyInput, store=Depends(get_store)):
    from companion.team_connected import ConnectedStore
    action(store.get_room, room_id)
    action(ConnectedStore(store.root).stop, room_id, run_id)
    return {'room': connected_room(store, room_id)}


@router.post('/api/team/rooms/{room_id}/obsidian/preview')
def note_preview(room_id: str, body: NoteInput, store=Depends(get_store)):
    return action(store.note_preview, room_id, body.message_id)


@router.post('/api/team/rooms/{room_id}/obsidian/confirm')
def note_confirm(room_id: str, body: NoteConfirmInput, store=Depends(get_store)):
    result = action(store.note_confirm, room_id, body.message_id, expected_note_sha256=body.note_sha256)
    return JSONResponse(result, status_code=201 if result['created'] else 200)


@router.post('/api/team/rooms/{room_id}/messages')
def message(room_id: str, body: MessageInput, store=Depends(get_store), service=Depends(get_service),
            lead=Depends(get_lead), coordinator=Depends(get_delegation)):
    if body.share_with_team:
        if body.execution_mode == 'plan':
            return delegate(room_id, DelegationInput(text=body.text, request_id=body.request_id, consent=True),
                            coordinator)
        room = action(store.get_room, room_id)['room']
        if room['local_only']:
            raise ApiError(403, 'local_only_room', 'This chat stays local.')
        note = prepare_team_lanes(store, service.transport, room_id, body.request_id)
        if note:
            raise ApiError(409, 'lane_closed', note)
        result = action(store.submit, room_id, **body.model_dump())
        if result['run_id']:
            wake_service(service)
    else:
        if body.execution_mode != 'plan':
            raise ApiError(422, 'invalid', 'Direct work requires an explicit shared instruction.')
        action(lead.submit, room_id, body.text, body.request_id)
        wake_lead(lead)
    return {'room': room_view(store.get_room(room_id), store.root)}


@router.post('/api/team/rooms/{room_id}/answers/stop')
def stop_answer(room_id: str, body: AnswerStopInput, store=Depends(get_store), lead=Depends(get_lead)):
    action(lead.stop, room_id, body.request_id)
    return {'room': room_view(store.get_room(room_id), store.root)}


@router.post('/api/team/rooms/{room_id}/stop')
def stop(room_id: str, body: EmptyInput, service=Depends(get_service), coordinator=Depends(get_delegation)):
    for parent in action(coordinator.for_room, room_id):
        action(coordinator.stop, room_id, parent['request_id'])
    action(service.stop, room_id)
    return {'room': connected_room(service.store, room_id)}


class ReplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class ReadInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ids: list[str] = Field(default_factory=list, max_length=50)
    all: bool = False


def get_threads(store=Depends(get_store)):
    from companion.team_threads import Store
    return Store(store.root)


@router.get('/api/team/rooms/{room_id}/threads')
def room_threads(room_id: str, store=Depends(get_store), threads=Depends(get_threads)):
    return {'threads': threads.list_threads(action(store.get_room, room_id))}


@router.get('/api/team/threads/{thread_id}')
def thread(thread_id: str, store=Depends(get_store), threads=Depends(get_threads)):
    rooms = (store.get_room(room['id']) for room in store.list_rooms())
    try:
        return threads.thread(thread_id, rooms)
    except LookupError:
        raise ApiError(404, 'thread_not_found',
                       'This thread is gone, or its chat was archived. Close this panel to go back.') from None


@router.post('/api/team/rooms/{room_id}/messages/{message_id}/reply')
def reply(room_id: str, message_id: str, body: ReplyInput, store=Depends(get_store), threads=Depends(get_threads),
          service=Depends(get_service)):
    try:
        result = action(threads.reply, action(store.get_room, room_id), message_id, body.text, body.request_id)
    except PermissionError:
        raise ApiError(403, 'local_only_room', 'This chat stays local. Replies go only to shared chats.') from None
    if get_settings().inline_worker:
        wake_service(service)
    return result


@router.post('/api/team/threads/{thread_id}/stop')
def stop_thread(thread_id: str, body: EmptyInput, threads=Depends(get_threads), service=Depends(get_service)):
    from companion import team_replies
    return {'sent': action(team_replies.stop, service, threads, thread_id)}


@router.get('/api/team/notifications')
def notifications(threads=Depends(get_threads)):
    return threads.notifications()


@router.post('/api/team/notifications/read')
def read_notifications(body: ReadInput, threads=Depends(get_threads)):
    return threads.mark_read(body.ids, everything=body.all)


class VerdictInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    label: Literal['real_problem', 'fine', 'not_critical', 'misunderstanding', 'checker_wrong']
    note: str = Field(default='', max_length=500)


@router.get('/api/team/honesty/checks')
def honesty_checks(threads=Depends(get_threads)):
    from companion import team_honesty
    return {'checks': team_honesty.checks(threads)}


@router.get('/api/team/honesty/checks/{check_id}')
def honesty_check(check_id: str, threads=Depends(get_threads)):
    from companion import team_honesty
    return action(team_honesty.check, threads, check_id)


@router.post('/api/team/honesty/findings/{finding_id}/verdict')
def honesty_verdict(finding_id: str, body: VerdictInput, threads=Depends(get_threads)):
    from companion import team_honesty
    return action(team_honesty.verdict, threads, finding_id, body.label, body.note)


class HelperControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(max_length=64)
    label: str = Field(default='', max_length=200)


class HelperTurn(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    role: Literal['user', 'assistant']
    text: str = Field(min_length=1, max_length=2000)


class HelperInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    question: str = Field(min_length=1, max_length=2000)
    controls: list[HelperControl] = Field(default_factory=list, max_length=120)
    history: list[HelperTurn] = Field(default_factory=list, max_length=12)


@router.post('/api/team/helper')
def helper(body: HelperInput, lead=Depends(get_lead)):
    """Answered by the local lead model only; nothing is saved or sent to a cloud model."""
    from companion import team_helper
    return action(team_helper.ask, lead, get_settings().team_code_root, body.question.strip(),
                  [item.model_dump() for item in body.controls], [turn.model_dump() for turn in body.history])


def install(app):
    install_boundary(app)

    @app.on_event('startup')
    def refresh_connected_apps():
        from companion.team_connected import start_readiness_refresh
        start_readiness_refresh()

    @app.on_event('startup')
    def resume():
        settings = get_settings()
        if settings.owner_machine and settings.tenant == 'personal' and settings.inline_worker:
            try:
                store = get_store()
            except (ValueError, OSError, RuntimeError):
                _log.error('Team state could not be resumed')
                return
            try:
                from companion.team_threads import Store
                if store.active_runs() or Store(store.root).replies('waiting'):
                    wake_service(TeamService(store, VisibleTeamTransport(store.root, controller=store.controller, show_codex=open_codex_chat)))
            except (ValueError, OSError, RuntimeError):
                _log.error('Native Team work could not be resumed')
            try:
                from companion.team_lead import has_pending_answers
                if has_pending_answers(store):
                    wake_lead(get_lead(store))
            except (ValueError, OSError, RuntimeError):
                _log.error('Local answer work could not be resumed')
            try:
                if (store.root / 'delegations.sqlite3').exists():
                    wake_delegation(get_delegation(store, get_lead(store), get_service(store)))
            except (ValueError, OSError, RuntimeError):
                _log.error('Saved team instructions could not be inspected')
    app.include_router(router)


def install_boundary(app):
    """The Team error shape and boundary, without the routes or startup work (the tunnel listener reuses it)."""
    previous_validation_handler = app.exception_handlers[RequestValidationError]

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if request.url.path.startswith('/api/team/'):
            return JSONResponse(status_code=422, content={'error': {
                'code': 'invalid', 'message': 'Check the message and chat fields before continuing.'}})
        return await previous_validation_handler(request, exc)

    @app.middleware('http')
    async def boundary(request, call_next):
        path = request.url.path
        primary = path == '/' and get_settings().team_primary
        if not primary and path.rstrip('/') != '/team' and not path.startswith('/api/team/'):
            return await call_next(request)
        def refuse(code, message):
            return JSONResponse(status_code=code, content={'error': {'code': 'team_boundary', 'message': message}},
                                headers={'Cache-Control': 'no-store'})
        settings = get_settings()
        local = request.client and request.client.host in _LOOPBACK
        # Set only by the Access JWT check (webapp, tunnel_app) after a valid Cloudflare Access JWT; never a header.
        # Verified tunnel traffic arrives from loopback but is treated as remote: it needs the Kyra token or session.
        tunnel = bool(local and getattr(request.state, 'access_verified', False))
        scheme = request.url.scheme
        if tunnel and request.headers.get('x-forwarded-proto', '').lower() == 'https':
            scheme = 'https'  # cloudflared terminates TLS; the header is honoured for verified tunnel traffic only
        if local and not tunnel and request.url.hostname not in _LOOPBACK:
            return refuse(403, 'Use the local address for a local connection.')
        if tunnel or not (local and settings.trust_loopback):
            token = settings.api_token
            header = request.headers.get('authorization', '')
            valid = bool(token) and ((header.startswith('Bearer ') and secrets.compare_digest(header[7:], token))
                    or webauth.session_valid(request.cookies.get(webauth.COOKIE_NAME, ''), token,
                                             settings.session_ttl_days * 86400))
            if scheme != 'https':
                return refuse(403, 'Remote Team access requires HTTPS.')
            if not valid:
                return RedirectResponse('/login', status_code=303, headers={'Cache-Control': 'no-store'}) if path == '/team' or primary else refuse(401, 'Sign in before opening Team.')
        if request.method not in {'GET', 'HEAD'}:
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                if (parsed.scheme, parsed.netloc) != (scheme, request.url.netloc):
                    return refuse(403, 'Use this app’s own controls.')
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return refuse(403, 'Use this app’s own controls.')
            # Bound the actual body, including chunked requests without Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                limit = 5_010_000 if path == '/api/team/transcribe' else 70000
                if len(body) > limit:
                    return refuse(413, 'The message is too large.')
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except (ValueError, OSError, RuntimeError):
            return refuse(503, 'Team storage or setup is unavailable. Check it on the Mac.')
        response.headers['Cache-Control'] = 'no-store'
        return response
