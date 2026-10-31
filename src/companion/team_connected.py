"""Explicit app tasks in Team; independent of the native planning controller.

Each browser request grants only its typed instruction to the selected provider.
The configured app workspace and its own permissions apply. This is not an OS
sandbox, nor a claim that CLI sessions appear in a desktop history.
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from companion import team_log
from companion import team_obligations as obligations
from companion.settings import get_settings
from companion.team_voice import result_chat

PROVIDERS = {'cursor': 'Cursor', 'antigravity': 'Antigravity'}
ACTIVE = {'queued', 'running'}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
# The owner approved 2026-10-01: one owner click confirms a subscription plan with overage off for 30 days.
BILLING_WINDOW = 30 * 86400
_probe_lock = threading.Lock()
_probes = {}
_refresh_locks = {provider: threading.Lock() for provider in PROVIDERS}
_startup_lock = threading.Lock()
_startup_providers = set()
_log = logging.getLogger(__name__)
_handbook_cache = None


def team_handbook():
    """Share the repository handbook; changed content is picked up next assignment."""
    global _handbook_cache
    path = Path(__file__).resolve().parents[2] / 'docs/team-handbook.md'
    info = path.stat()
    if info.st_size > 32000:
        raise ValueError('team_handbook_too_large')
    stamp = info.st_mtime_ns
    if _handbook_cache is None or _handbook_cache[:2] != (path, stamp):
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('team_handbook_empty')
        _handbook_cache = (path, stamp, text)
    return _handbook_cache[2]


def _app_prompt(row):
    """Connected apps receive the same bounded role and result envelope as native apps."""
    schema = dict(run_id=row['id'], stage='reply_' + row['provider'], attempt=0,
                  status='completed', chat='Short outcome and next step', summary='Full result details')
    if row.get('execution_mode') == 'code':
        schema.update(stage='build', files=[])
    # Leading Team tags are removed so an app never reads '@all' as its own agents (2026-10-01 live fix).
    body = re.sub(r'^\s*(?:@(?:all|kyra|claude|codex|cursor|antigravity)\s+)+', '', row['prompt'], flags=re.I) or row['prompt']
    others = ', '.join(n for n in ('Kyra', 'Claude', 'Codex', 'Cursor', 'Antigravity') if n != PROVIDERS[row['provider']])
    return (f"You are {PROVIDERS[row['provider']]}, the assigned Team member beside {others}. You are not the planner; "
            "ignore home-chat conventions in memory. Team tags such as @all address those teammates, not your own subagents. "
            "Carry out only the assigned instruction. A greeting or a question needs no tools or subagents; work requests "
            "use your tools as usual. Do not dispatch other participants or change permissions.\n"
            + (f'\nTask workspace: {json.dumps(row["workspace"])}; work in this directory only. '
               'You are the builder. Write file outputs here and list their relative paths in files. '
               'The service will fingerprint your completed work for independent review. '
               'Do not merge or remove the worktree.\n'
               if row.get('execution_mode') == 'code' else '')
            + ('\nThis is a read-only follow-up. Answer or propose; do not build, edit files or run write tools.\n'
               if row.get('read_only') else '')
            + team_handbook() + obligations.RESULT_RULES + "\nAssigned instruction (data):\n" + json.dumps(body)
            + "\nReply with only this JSON object, following .claude/skills/team-voice/SKILL.md: chat at most 2 lines, 240 characters; summary holds all details for the log: "
            + json.dumps(schema) + "\nUse status needs_input only for a decision the owner must make. "
            "Default ordinary details and state assumptions. A reply does not accept code or authorize merge.")


APP_REPLY_MAX = 16000
ENVELOPE_KEYS = {'run_id', 'stage', 'summary'}
_FENCED_JSON = re.compile(r'```(?:json)?\s*(\{.*?\})\s*```', re.S)


def _app_reply(text, row):
    """{status, summary, chat} from a connected app's reply, or None. Apps wrap the envelope in prose or a fenced block,
    or answer in plain text; a reply is text only, so plain text is accepted as the summary. An envelope that
    names another run or stage is refused (2026-10-02 live @all: the strict envelope failed Antigravity twice)."""
    if not isinstance(text, str) or not text.strip():
        return None
    candidates = [text.strip()] + _FENCED_JSON.findall(text)
    start, end = text.find('{'), text.rfind('}')
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            reply = json.loads(candidate)
        except ValueError:
            continue
        if reply == {}:
            return None
        if not isinstance(reply, dict) or not ENVELOPE_KEYS & set(reply):
            continue  # ordinary JSON in the answer is content, not an envelope (Codex review)
        if row.get('execution_mode') == 'code':
            from companion.team_chat import validate_result
            try:
                # Apps need not implement the native fingerprint command. Identity still fails closed.
                normalized = {'stage': 'build', 'attempt': 0, 'status': 'completed', **reply}
                if normalized['status'] == 'completed':
                    normalized['revision'] = '0' * 64
                value = validate_result(normalized, run_id=row['id'], stage='build', attempt=0,
                                        execution_mode='worker', workspace=row.get('workspace'))
                value.pop('revision', None)
                return value
            except (ValueError, OSError):
                return None
        if reply.get('run_id', row['id']) != row['id'] or reply.get('stage', 'reply_' + row['provider']) != 'reply_' + row['provider']:
            return None
        summary, status = reply.get('summary'), reply.get('status', 'completed')
        if not isinstance(summary, str) or not summary.strip() or status not in {'completed', 'needs_input'}:
            return None
        chat = reply.get('chat') if isinstance(reply.get('chat'), str) else None
        try:
            meta = obligations.metadata(reply)
            flag = reply.get('requires_owner_decision', False)
            if type(flag) is not bool:
                return None
        except ValueError:
            return None
        return {'status': status, 'summary': summary.strip()[:APP_REPLY_MAX], 'chat': chat,
                **({'requires_owner_decision': flag} if 'requires_owner_decision' in reply else {}), **meta}
    if row.get('execution_mode') == 'code':
        return None
    return {'status': 'completed', 'summary': text.strip()[:APP_REPLY_MAX], 'chat': None}


class ConnectedStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'app-runs.sqlite3'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, room_id TEXT NOT NULL, provider TEXT NOT NULL,
                request_id TEXT NOT NULL, prompt TEXT NOT NULL, config_hash TEXT NOT NULL,
                state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                session_id TEXT, surface TEXT NOT NULL DEFAULT '', stop_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(room_id, request_id))''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(runs)')}
            if 'chat' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN chat TEXT')
            if 'message_author' not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN message_author TEXT NOT NULL DEFAULT 'user'")
            if 'acknowledged_at' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN acknowledged_at REAL')
            for name, definition in {
                'execution_mode': "TEXT NOT NULL DEFAULT 'reply'",
                'reviewer': 'TEXT', 'workspace': 'TEXT', 'revision': 'TEXT',
                'files': "TEXT NOT NULL DEFAULT '[]'",
                'retry_count': 'INTEGER NOT NULL DEFAULT 0',
                'parent_request_id': 'TEXT', 'requested_session_id': 'TEXT',
                'controller_claim': 'TEXT',
                'read_only': 'INTEGER NOT NULL DEFAULT 0',
                'read_only_enforced': 'INTEGER NOT NULL DEFAULT 0',
                'completion': "TEXT NOT NULL DEFAULT '{}'",
                'requires_owner_decision': 'INTEGER NOT NULL DEFAULT 0',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {name} {definition}')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, *, room_id, provider, request_id, text, config_hash, message_author="user", execution_mode="reply", reviewer=None, parent_request_id=None, session_id=None, read_only=False):
        if (type(read_only) is not bool or (execution_mode == 'code' and read_only)
                or any(value is not None and (not isinstance(value, str) or not _ID.fullmatch(value))
                       for value in (parent_request_id, session_id))):
            raise ValueError('invalid_app_context')
        if execution_mode not in {'reply', 'code'} or (execution_mode == 'code' and reviewer not in {'claude', 'codex'}):
            raise ValueError('invalid_code_review_assignment')
        if execution_mode == 'reply' and reviewer is not None:
            raise ValueError('reply_cannot_assign_code_reviewer')
        if (message_author not in {'user', 'system'} or provider not in PROVIDERS or not _ID.fullmatch(request_id)
                or not re.fullmatch(r'[a-f0-9]{32}', room_id)
                or not isinstance(text, str) or not text.strip() or '\0' in text
                or len(text.encode('utf-8')) > 16000):
            raise ValueError('invalid_app_request')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._expire(db)
            prior = db.execute('SELECT * FROM runs WHERE room_id=? AND request_id=?', (room_id, request_id)).fetchone()
            if prior:
                if (prior['provider'] != provider or prior['prompt'] != text or prior['message_author'] != message_author
                        or prior['execution_mode'] != execution_mode or prior['reviewer'] != reviewer
                        or prior['parent_request_id'] != parent_request_id or prior['requested_session_id'] != session_id
                        or bool(prior['read_only']) != read_only):
                    raise ValueError('request_changed')
                return self._row(prior), False
            if db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] >= 2000:
                raise ValueError('app_history_full')
            if db.execute("SELECT 1 FROM runs WHERE provider=? AND state IN ('queued','running')", (provider,)).fetchone():
                raise ValueError('app_busy')
            now, ident = time.time(), uuid.uuid4().hex
            db.execute('INSERT INTO runs (id,room_id,provider,request_id,prompt,config_hash,state,created_at,updated_at,message_author) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (ident, room_id, provider, request_id, text, config_hash, 'queued', now, now, message_author))
            db.execute('UPDATE runs SET execution_mode=?,reviewer=?,parent_request_id=?,requested_session_id=?,session_id=?,read_only=? WHERE id=?',
                       (execution_mode, reviewer, parent_request_id, session_id, session_id, read_only, ident))
            if message_author == 'system':
                team_log.append(self.root, room_id, message_id=ident + '-request', author=message_author,
                                kind='assignment', details=text, created_at=now)
            return self._row(db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()), True

    @staticmethod
    def _row(row):
        value = dict(row)
        value['files'] = json.loads(value['files'])
        value['completion'] = json.loads(value['completion'])
        value['requires_owner_decision'] = bool(value['requires_owner_decision'])
        value['read_only'] = bool(value['read_only'])
        value['read_only_enforced'] = bool(value['read_only_enforced'])
        return value

    def build_output(self, ident, *, workspace, revision=None, files=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET workspace=?,revision=?,files=? WHERE id=? AND state='running' AND stop_requested=0",
                       (str(workspace), revision, json.dumps(files or []), ident))

    def retry(self, ident):
        with self.connect() as db:
            return db.execute("UPDATE runs SET retry_count=1 WHERE id=? AND retry_count=0 AND state='running' AND stop_requested=0",
                              (ident,)).rowcount == 1

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise LookupError('unknown_app_run')
            return self._row(row)

    def begin(self, ident):
        with self.connect() as db:
            return db.execute("UPDATE runs SET state='running',updated_at=? WHERE id=? AND state='queued' AND stop_requested=0", (time.time(), ident)).rowcount == 1

    def for_room(self, room_id):
        with self.connect() as db:
            self._expire(db)
            return [self._row(row) for row in db.execute('SELECT * FROM runs WHERE room_id=? ORDER BY created_at', (room_id,))]

    @staticmethod
    def _expire(db):
        # Never infer completion or retry a lost process. A later result is kept.
        now = time.time()
        db.execute("UPDATE runs SET state='uncertain',reason='process_unconfirmed',updated_at=? WHERE state IN ('queued','running') AND updated_at<?", (now, now-330))

    def heartbeat(self, ident):
        with self.connect() as db:
            db.execute("UPDATE runs SET updated_at=? WHERE id=? AND state='running'", (time.time(), ident))

    def stop(self, room_id, ident):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=? AND room_id=?', (ident, room_id)).fetchone()
            if row is None:
                raise LookupError('unknown_app_run')
            if row['state'] in ACTIVE:
                state = 'stopped' if row['state'] == 'queued' else row['state']
                db.execute('UPDATE runs SET stop_requested=1,state=?,reason=? WHERE id=?', (state, 'stop_requested', ident))
        return self.get(ident)

    def event(self, ident, event):
        with self.connect() as db:
            db.execute("UPDATE runs SET updated_at=? WHERE id=? AND state='running'", (time.time(), ident))
            if event['type'] == 'session':
                db.execute("UPDATE runs SET session_id=COALESCE(?,session_id),surface=COALESCE(NULLIF(?,''),surface) WHERE id=? AND state='running'",
                           (event['session_id'], event['surface'], ident))
            elif event['type'] == 'text':
                db.execute("UPDATE runs SET text=substr(text || ?,1,16000) WHERE id=? AND state='running'",
                           (event['text'], ident))

    def finish(self, ident, *, state, text, reason, session_id, surface, chat=None, completion=None):
        meta = obligations.metadata(completion or {})
        decision = (completion or {}).get('requires_owner_decision') is True and bool(text.strip())
        if state == 'needs_input' and completion is not None:
            reason = text[:1500] or reason
            if not decision:
                state = 'blocked'
        if state not in {'completed', 'blocked', 'needs_input', 'failed', 'stopped', 'uncertain', 'cancelled'}:
            state = 'uncertain'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
            if row['state'] in {'running', 'uncertain'}:
                team_log.append(self.root, row['room_id'], message_id=ident, author=row['provider'],
                                kind='app_result', details=text or row['text'], created_at=time.time())
                db.execute('UPDATE runs SET chat=? WHERE id=?', (result_chat(text, chat) if text else None, ident))
            if row['state'] in {'running', 'uncertain'} and state in {'completed', 'needs_input'} and text:
                db.execute('UPDATE runs SET acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=?',
                           (time.time(), ident))
            if row['state'] == 'uncertain' and row['reason'] == 'process_unconfirmed':
                db.execute("UPDATE runs SET text=?,reason='late_result',session_id=?,surface=?,updated_at=? WHERE id=?",
                           (text[:16000] or row['text'], session_id or row['session_id'], surface or row['surface'], time.time(), ident))
                return
            if row['state'] == 'running':
                db.execute('UPDATE runs SET completion=?,requires_owner_decision=? WHERE id=?',
                           (json.dumps(meta), int(decision), ident))
            if row['stop_requested'] and state == 'completed':
                state, reason = 'uncertain', 'result_after_stop'
            db.execute("UPDATE runs SET state=?,text=?,reason=?,session_id=?,surface=?,updated_at=? WHERE id=? AND state='running'",
                       (state, text[:16000], reason or '', session_id or row['session_id'], surface or row['surface'], time.time(), ident))


def _config(provider):
    path = get_settings().team_apps_config
    if path is None:
        return None, ''
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 32000 or path.stat().st_mode & 0o022:
        raise ValueError('invalid_app_configuration')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('providers'), dict):
        raise ValueError('invalid_app_configuration')
    entry = data.get('providers', {}).get(provider)
    if not entry:
        return None, ''
    if (not isinstance(entry, dict) or data.get('version') != 1 or not isinstance(data.get('workspace'), str)
            or not Path(data['workspace']).is_absolute() or not Path(data['workspace']).is_dir()
            or not isinstance(entry.get('command'), list) or len(entry['command']) != 1
            or not isinstance(entry['command'][0], str) or not Path(entry['command'][0]).is_absolute()
            or not isinstance(entry.get('model'), str) or not _ID.fullmatch(entry['model'])):
        raise ValueError('invalid_app_configuration')
    from companion.paths import DATA_DIR
    workspace = Path(data['workspace']).resolve()
    private = Path(DATA_DIR).resolve()
    project = Path(__file__).resolve().parents[2]
    if (workspace == Path.home().resolve() or project.is_relative_to(workspace)
            or workspace.is_relative_to(project)
            or private.is_relative_to(workspace) or workspace.is_relative_to(private)
            or os.path.lexists(workspace / '.env')):
        raise ValueError('unsafe_app_workspace')
    value = {**entry, 'workspace': data['workspace']}
    return value, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _adapter(provider, cfg):
    if provider == 'cursor':
        from companion.team_cursor import CursorAdapter
        return CursorAdapter(cfg['command'])
    from companion.team_antigravity import AntigravityAdapter
    return AntigravityAdapter(cfg['command'], workspace=cfg['workspace'])


def _evidence(provider, cfg):
    from companion.team_cursor import SubscriptionEvidence
    raw = cfg.get('subscription')
    return SubscriptionEvidence(**raw) if isinstance(raw, dict) else None


def _readiness(provider, cfg, pin, *, refresh=False):
    from companion.team_cursor import entitlement_reason
    with _probe_lock:
        cached = _probes.get(provider)
    if not refresh and (cached is None or cached[0] != pin):
        return None, 'not_checked'
    if refresh:
        with _refresh_locks[provider]:
            with _probe_lock:
                latest = _probes.get(provider)
            if latest is not cached and latest is not None and latest[0] == pin:
                # A concurrent startup/manual/dispatch check already observed
                # this exact configuration while we waited. Share that result.
                observed = latest[2]
            else:
                try:
                    observed = _adapter(provider, cfg).probe_entitlement(cfg['model'])
                except Exception:
                    # Never keep an older success after a failed fresh check.
                    with _probe_lock:
                        _probes[provider] = (pin, time.time(), {
                            'authenticated': False, 'reason': 'account_probe_unavailable'})
                    raise
                with _probe_lock:
                    _probes[provider] = (pin, time.time(), observed)
    else:
        observed = cached[2]
    # Auth is observed, not supplied by the saved billing observation. A probe
    # with full current evidence wins over an older configured observation.
    reason = None if observed.get('authenticated') is True else (observed.get('reason') or 'authentication_required')
    evidence = observed.get('evidence') or _evidence(provider, cfg)
    if evidence is not None and not observed.get('evidence'):
        if not getattr(evidence, 'account', ''):
            reason = reason or 'account_unbound'
        elif evidence.account != observed.get('account'):
            reason = reason or 'account_mismatch'
        elif evidence.expires_at - evidence.checked_at > BILLING_WINDOW:
            reason = reason or 'subscription_evidence_stale_or_mismatched'
        if provider == 'cursor' and cfg.get('own_api_keys') is not False:
            reason = reason or 'own_api_keys_unverified'
    reason = reason or (entitlement_reason(evidence, provider, cfg['model']) if evidence is not None
                        else observed.get('reason') or 'subscription_evidence_required')
    return evidence, reason


def start_readiness_refresh():
    """One account-only check per provider/process; startup never waits for I/O.

    Providers run independently under their existing supervised probe bounds.
    This never opens run storage, retries old work, or renews billing evidence.
    """
    settings = get_settings()
    if not (settings.owner_machine and settings.tenant == 'personal'
            and settings.inline_worker and settings.team_apps_config is not None):
        return

    def check(provider):
        try:
            cfg, pin = _config(provider)
            if cfg is not None:
                _readiness(provider, cfg, pin, refresh=True)
        except Exception:
            _log.warning('Connected app startup check unavailable: %s', provider)

    with _startup_lock:
        for provider in PROVIDERS:
            if provider in _startup_providers:
                continue
            _startup_providers.add(provider)
            try:
                threading.Thread(target=check, args=(provider,),
                                 name=f'kyra-app-readiness-{provider}', daemon=True).start()
            except RuntimeError:
                _log.warning('Connected app startup worker unavailable: %s', provider)


def confirm_billing(provider, *, now=time.time):
    """Record the owner's click: the owner checked this app's plan is a subscription with overage off.

    The observation binds to the account a fresh probe sees now, never to a
    browser-supplied value, and expires after BILLING_WINDOW.
    """
    cfg, _ = _config(provider)
    if cfg is None:
        raise ValueError('app_not_configured')
    observed = _adapter(provider, cfg).probe_entitlement(cfg['model'])
    account = observed.get('account')
    if (observed.get('authenticated') is not True or not isinstance(account, str)
            or not re.fullmatch(r'[a-f0-9]{64}', account)):
        raise ValueError('authentication_required')
    checked = now()
    path = Path(get_settings().team_apps_config)
    data = json.loads(path.read_text())
    data['providers'][provider]['subscription'] = dict(
        provider=provider, model=cfg['model'], checked_at=checked, expires_at=checked + BILLING_WINDOW,
        authenticated=True, quota_available=None, overage_disabled=True, terms_accepted=True,
        evidence_id=f'owner-confirmed-{uuid.uuid4().hex}', account=account)
    # Created 0600 before the rename, so the file is never readable by others.
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def app_status(*, refresh=False):
    rows = []
    for provider, name in PROVIDERS.items():
        base = dict(id=provider, name=name, available=False, reason='not_configured', workspace='',
                    surface='cursor_cli_acp' if provider == 'cursor' else 'antigravity_cli', native_visibility='unverified')
        try:
            cfg, pin = _config(provider)
            if cfg is not None:
                base['workspace'] = cfg['workspace']
                policy = cfg.get('subscription')
                if isinstance(policy, dict):
                    base['policy_observed_at'] = policy.get('checked_at')
                    base['policy_recheck_by'] = policy.get('expires_at')
                _, reason = _readiness(provider, cfg, pin, refresh=refresh)
                with _probe_lock:
                    cached = _probes.get(provider)
                base.update(available=reason is None, reason=reason or '',
                            checked_at=cached[1] if cached and cached[0] == pin else None)
        except Exception:
            # Optional provider failures must not disable local Kyra answering.
            # Never put provider stderr, account identifiers or configuration in logs.
            _log.warning('Connected app status unavailable: %s', provider)
            base['reason'] = 'configuration_unavailable'
        rows.append(base)
    return {'apps': rows}


def project_room(view, root, *, index=None):
    from companion.team_log import append_details, details_index

    store = ConnectedStore(root)
    runs = store.for_room(view['id'])
    if index is None:
        index = details_index(root, view['id'])
    messages = list(view['messages'])
    for run in runs:
        if run['message_author'] != 'system':
            messages.append(dict(id=run['id']+'-request', author=run['message_author'], text=run['prompt'], created_at=run['created_at'], kind='app_request'))
        if run['text'] or run['state'] not in ACTIVE:
            text = run['text'] or f"{PROVIDERS[run['provider']]}: {run['state']} ({run['reason']})."
            if run['state'] not in ACTIVE:
                append_details(root, view['id'], dict(id=run['id'], author=run['provider'],
                               kind='app_result', created_at=run['updated_at']), text, index=index)
            messages.append(dict(id=run['id'], author=run['provider'], text=result_chat(text, run.get('chat')), details_id=run['id'], created_at=run['updated_at'], kind='app_result'))
    public = [{key: run[key] for key in ('id','provider','state','reason','session_id','surface','stop_requested','created_at','acknowledged_at','execution_mode','read_only','read_only_enforced')} for run in runs]
    return {**view, 'messages': sorted(messages, key=lambda row: row['created_at']), 'app_runs': public}


def submit(root, room, *, provider, text, request_id, message_author="user", execution_mode="reply", reviewer=None, parent_request_id=None, session_id=None, read_only=False):
    if room['local_only']:
        raise ValueError('local_only_room')
    cfg, pin = _config(provider)
    if cfg is None:
        raise ValueError('app_not_configured')
    store = ConnectedStore(root)
    row, created = store.claim(room_id=room['id'], provider=provider, request_id=request_id, text=text, config_hash=pin, message_author=message_author, execution_mode=execution_mode, reviewer=reviewer,
                               parent_request_id=parent_request_id, session_id=session_id, read_only=read_only)
    if created:
        threading.Thread(target=_work, args=(store,row), daemon=True).start()
    return store.get(row['id'])


class _GateRefused(PermissionError):
    pass


def _run_claimed(store, row, cfg, evidence, request, stop, authorized):
    # Text replies use the app workspace and entitlement, without a code-build claim.
    if row['execution_mode'] == 'reply':
        try:  # replies read current master, not the frozen Sep 29 copy (2026-10-04)
            from companion.team_workspace import refresh_snapshot
            refresh_snapshot(cfg['workspace'])
        except Exception:  # a stale snapshot still answers; never block the reply on it
            logging.getLogger(__name__).warning('app workspace refresh failed', exc_info=True)
        result = _adapter(row['provider'], cfg).run(
            request, authorize=authorized, entitlement=evidence, stop_event=stop,
            on_event=lambda event: store.event(row['id'], event))
        store.event(row['id'], {'type': 'session', 'session_id': result.session_id, 'surface': result.surface})
        with store.connect() as db:
            db.execute('UPDATE runs SET read_only_enforced=? WHERE id=?', (int(result.read_only_enforced), row['id']))
        return result
    from companion.team_transport import read_config
    from companion.workflow_control import WorkflowStore

    try:
        controller = WorkflowStore()
        binding = read_config(store.root)
        if not binding or not binding.get('controller'):
            raise ValueError('submission_binding_changed_or_missing')
        grant = binding['controller']
        participants = {row['provider']: {'id': evidence.evidence_id, 'cwd': request.workspace}}
        stage = 'build'
        with controller.reserve_dispatch(task_id=grant['task_id'], version=grant['version'],
                repo=request.workspace, owner='team', run_id=row['id'], stage=stage,
                attempt=store.get(row['id'])['retry_count'], participants=participants) as reservation:
            claim = reservation.commit()
    except (PermissionError, ValueError, OSError, AttributeError) as exc:
        raise _GateRefused(str(exc)) from exc
    with store.connect() as db:
        db.execute('UPDATE runs SET controller_claim=? WHERE id=?', (json.dumps(claim), row['id']))
    def check(req):
        if not authorized(req) or read_config(store.root) != binding:
            return False
        try:
            controller._dispatch_authority(task_id=grant['task_id'], version=grant['version'],
                repo=request.workspace, participants=participants, actions=['use_' + row['provider']])
            return claim in controller.claims()
        except (PermissionError, ValueError, OSError):
            return False
    result = _adapter(row['provider'], cfg).run(request, authorize=check, entitlement=evidence,
                stop_event=stop, on_event=lambda event: store.event(row['id'], event))
    store.event(row['id'], {'type': 'session', 'session_id': result.session_id, 'surface': result.surface})
    # An uncertain or interrupted send retains its durable claim for reconciliation.
    if result.state != 'uncertain':
        outcome = result.state if result.state in {'completed', 'stopped'} else 'needs_input'
        controller.release_claim(claim, outcome=outcome)
        with store.connect() as db:
            db.execute('UPDATE runs SET controller_claim=NULL WHERE id=?', (row['id'],))
    with store.connect() as db:
        db.execute('UPDATE runs SET read_only_enforced=? WHERE id=?', (int(result.read_only_enforced), row['id']))
    return result


def _work(store, row):
    from companion.team_cursor import AdapterRequest
    if not store.begin(row['id']):
        return
    class StopFlag:
        last_beat = 0.0
        def is_set(self):
            if time.monotonic() - self.last_beat > 2:
                store.heartbeat(row['id'])
                self.last_beat = time.monotonic()
            current = store.get(row['id'])
            return bool(current['stop_requested']) or current['state'] != 'running'
    stop = StopFlag()
    def authorized(_request):
        _, pin = _config(row['provider'])
        return pin == row['config_hash'] and not stop.is_set()
    try:
        cfg, pin = _config(row['provider'])
        if cfg is None or pin != row['config_hash']:
            store.finish(row['id'], state='blocked', text='', reason='configuration_changed', session_id=None, surface='')
            return
        retry_reason = {'antigravity': 'permission_required',
                        'cursor': 'subscription_evidence_stale_or_mismatched'}[row['provider']]
        def retryable(reason):
            return (reason or '').partition(':')[0] == retry_reason
        def blocked_text(reason):
            if (reason or '').partition(':')[0] == 'permission_required':
                if row['provider'] == 'antigravity':
                    return ('Antigravity asked to run a command and Kyra does not approve commands for you. '
                            'One-time, in the Antigravity CLI: run /config, set Tool Permission to '
                            'proceed-in-sandbox and keep Terminal Sandbox on.')
                return ('Cursor asked to run a tool and Kyra does not approve tools for you. '
                        'One-time, review the Cursor CLI permissions in <user-home>/.cursor/cli-config.json, '
                        'allow only the required tool, keep sandbox enabled, then retry.')
            if row['provider'] == 'cursor' and reason == retry_reason:
                return 'Cursor is blocked. Open Cursor and sign in.'
            return ''
        evidence, reason = _readiness(row['provider'], cfg, pin, refresh=True)
        if retryable(reason) and store.retry(row['id']):
            evidence, reason = _readiness(row['provider'], cfg, pin, refresh=True)
        if reason:
            store.finish(row['id'], state='blocked', text=blocked_text(reason),
                         reason=reason, session_id=None, surface='')
            return
        if row.get('execution_mode') == 'code':
            from companion.team_workspace import task_worktree
            # Configuration is still pinned to the account; code always gets a fresh task worktree.
            try:
                from companion.team_transport import read_config
                binding = read_config(store.root)
                if not binding:
                    raise PermissionError('submission_binding_changed_or_missing')
                workspace = task_worktree(row['id'], base='origin/master', root=binding['workspace'])
            except Exception:
                # No adapter has started: workspace creation failure is definitive.
                store.finish(row['id'], state='blocked', text='', reason='task_workspace_unavailable', session_id=None, surface='')
                return
            store.build_output(row['id'], workspace=workspace)
            row = store.get(row['id'])
        try:
            prompt = _app_prompt(row)
        except (OSError, ValueError):
            store.finish(row['id'], state='blocked', text='', reason='team_handbook_unavailable', session_id=None, surface='')
            return
        req = AdapterRequest(row['id'], prompt, row.get('workspace') or cfg['workspace'], cfg['model'], session_id=row.get('session_id'), timeout_seconds=300, read_only=bool(row.get('read_only')), execution_mode=row['execution_mode'])
        result = _run_claimed(store, row, cfg, evidence, req, stop, authorized)
        value = asdict(result)
        store.event(row['id'], {'type': 'session', 'session_id': value['session_id'], 'surface': value['surface']})
        if value['state'] == 'blocked' and retryable(value['reason']) and store.retry(row['id']):
            # A blocked turn is the only retryable case. No successful/uncertain turn is resent.
            evidence, reason = _readiness(row['provider'], cfg, pin, refresh=True)
            if reason:
                value.update(state='blocked', reason=reason, text=blocked_text(reason))
            elif not stop.is_set():
                from dataclasses import replace
                req = replace(req, session_id=value['session_id'] or store.get(row['id'])['session_id'])
                value = asdict(_run_claimed(store, row, cfg, evidence, req, stop, authorized))
        store.event(row['id'], {'type': 'session', 'session_id': value['session_id'], 'surface': value['surface']})
        if value['state'] == 'blocked':
            value['text'] = blocked_text(value['reason']) or value['text']
        reply = None
        if value['state'] == 'completed':
            reply = _app_reply(value['text'], row)
            if reply is None:
                store.finish(row['id'], state='failed', text='', reason='invalid_app_result',
                             session_id=value['session_id'], surface=value['surface'])
                return
            if row.get('execution_mode') == 'code' and reply['status'] == 'completed':
                from companion.team_transport import workspace_revision
                revision = workspace_revision(row['workspace'])
                store.build_output(row['id'], workspace=row['workspace'], revision=revision, files=reply.get('files', []))
            value.update(state=reply['status'], text=reply['summary'], chat=reply.get('chat'))
        store.finish(row['id'], chat=value.get('chat'), completion=reply, **{key: value[key] for key in ('state','text','reason','session_id','surface')})

    except _GateRefused as exc:
        store.finish(row['id'], state='blocked', text='', reason='gate_refused:' + str(exc), session_id=None, surface='')
    except Exception:
        # Never print prompts, account details or raw adapter errors; never retry uncertain turns.
        store.finish(row['id'], state='uncertain', text='', reason='process_unconfirmed', session_id=None, surface='')
