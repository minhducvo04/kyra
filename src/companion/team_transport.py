"""Team binding and visible transport. Configuration is local, never a browser grant."""
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from companion import app_dispatch, app_tasks, delivery_ledger
from companion.paths import write_json

# Observational metadata only; these limits do not grant dispatch authority.
_METADATA_BYTES = 256 * 1024
_METADATA_LINE_BYTES = 16 * 1024


def _chat_label(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value).split())[:160]


def _codex_home(value):
    from companion.settings import get_settings
    return Path(value) if value is not None else get_settings().codex_home


def codex_chat_name(binding, *, codex_home=None):
    """Read only the bounded name index, never derive a name from chat messages."""
    name = _chat_label(binding.get('display_name'))
    if name:
        return name
    ident = binding['id']
    index = _codex_home(codex_home) / 'session_index.jsonl'
    try:
        if index.is_symlink():
            return ident[:12]
        with index.open('rb') as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - _METADATA_BYTES))
            if size > _METADATA_BYTES:
                stream.readline(_METADATA_LINE_BYTES)
            raw = stream.read(_METADATA_BYTES)
        for line in reversed(raw.splitlines()):
            if len(line) > _METADATA_LINE_BYTES:
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(row, dict) and row.get('id') == ident:
                name = _chat_label(row.get('thread_name'))
                if name:
                    return name
    except OSError:
        pass
    return _chat_label(ident[:12])


def codex_queue_reason(binding, *, codex_home=None, minutes=0):
    return f'waiting on Codex chat {codex_chat_name(binding, codex_home=codex_home)} · {max(0, minutes)} min'


def _codex_started(delivery, *, codex_home):
    """Inspect only session/start event metadata in the receipt's exact rollout.

    Unrelated lines are discarded without decoding their message/tool payloads.
    Missing, rotated, oversized or partial evidence never means a worker started.
    """
    ident, source = delivery.get('task_id'), delivery.get('rollout')
    offset, sent_at = delivery.get('rollout_offset'), delivery.get('sent_at')
    if (not isinstance(ident, str) or not isinstance(source, str)
            or type(offset) is not int or offset < 0
            or type(sent_at) not in (int, float) or not math.isfinite(sent_at)):
        return False
    path = Path(source)
    sessions = _codex_home(codex_home) / 'sessions'
    if (path.is_symlink() or not path.resolve().is_relative_to(sessions.resolve())
            or not path.name.endswith(f'-{ident}.jsonl')):
        return False
    try:
        with path.open('rb') as stream:
            header = stream.readline(_METADATA_LINE_BYTES + 1)
            if len(header) > _METADATA_LINE_BYTES:
                return False
            meta = json.loads(header)
            if (not isinstance(meta, dict) or meta.get('type') != 'session_meta'
                    or not isinstance(meta.get('payload'), dict) or meta['payload'].get('id') != ident):
                return False
            size = stream.seek(0, 2)
            if not len(header) <= offset <= size or size - offset > _METADATA_BYTES:
                return False
            stream.seek(offset)
            raw = stream.read(_METADATA_BYTES + 1)
        if len(raw) > _METADATA_BYTES:
            return False
        for line in raw.splitlines(keepends=True):
            if (len(line) > _METADATA_LINE_BYTES or not line.endswith(b'\n')
                    or not re.match(rb'\s*\{\s*"timestamp"\s*:\s*"[^"\n]+"\s*,\s*"type"\s*:\s*"event_msg"\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"task_started"', line)):
                continue
            event = json.loads(line)
            stamp = datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is not None and sent_at <= stamp.timestamp() <= time.time():
                return True
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        pass
    return False


class TeamTransport(ABC):
    @abstractmethod
    def send(self, *, role, run_id, stage, attempt, assignment_path, result_path, binding): ...


def read_config(root):
    path = Path(root) / 'config.json'
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError('Invalid team configuration')
    value = json.loads(path.read_text())
    return validate_config(value)


def validate_config(value):
    if isinstance(value, dict) and 'receptionist_threads' in value:
        threads = value['receptionist_threads']
        if (not isinstance(threads, list)
                or any(not isinstance(t, str) or not re.fullmatch(r'[a-zA-Z0-9][\w-]{0,127}', t) for t in threads)):
            raise ValueError('Invalid receptionist_threads')
    if isinstance(value, dict) and value.get('version') == 3:
        if (type(value['version']) is not int
                or set(value) - {'receptionist_threads'} != {'version', 'workspace', 'scope', 'controller', 'lanes'}
                or not isinstance(value['lanes'], list) or not 1 <= len(value['lanes']) <= 16):
            raise ValueError('Invalid team lanes')
        if len(json.dumps(value).encode()) > 16384:
            raise ValueError('Team lanes exceed the configuration budget')
        pins = set()
        for lane in value['lanes']:
            if not isinstance(lane, dict) or set(lane) != {'workspace', 'participants'}:
                raise ValueError('Invalid team lane')
            validate_config(lane_config(value, lane))
            for role, pin in lane['participants'].items():
                key = (role, pin['id'])
                if key in pins:
                    raise ValueError('A pinned chat must belong to only one lane')
                pins.add(key)
        if not isinstance(value['workspace'], str) or not Path(value['workspace']).is_absolute():
            raise ValueError('Invalid bound repository')
        if len({config_fingerprint(lane) for lane in value['lanes']}) != len(value['lanes']):
            raise ValueError('Duplicate lane')
        return value
    fields = {'version', 'workspace', 'scope', 'participants'}
    if isinstance(value, dict) and value.get('version') == 2:
        fields = fields | {'controller'}
    if (not isinstance(value, dict) or set(value) - {'receptionist_threads'} != fields
            or type(value['version']) is not int or value['version'] not in (1, 2) or not isinstance(value['scope'], str)
            or not 1 <= len(value['scope']) <= 4000
            or not isinstance(value['workspace'], str) or not Path(value['workspace']).is_absolute()
            or not isinstance(value['participants'], dict) or set(value['participants']) != {'claude', 'codex'}):
        raise ValueError('Invalid team configuration')
    for binding in value['participants'].values():
        if (not isinstance(binding, dict) or set(binding) != {'id', 'cwd'}
                or not isinstance(binding['id'], str) or not 1 <= len(binding['id']) <= 128
                or not isinstance(binding['cwd'], str) or not Path(binding['cwd']).is_absolute()):
            raise ValueError('Invalid team binding')
    if value['version'] == 2:
        binding = value['controller']
        if (not isinstance(binding, dict) or set(binding) != {'task_id', 'version'}
                or not isinstance(binding['task_id'], str) or not binding['task_id'].strip()
                or len(binding['task_id']) > 512 or type(binding['version']) is not int or binding['version'] < 1):
            raise ValueError('Invalid controller binding')
    return value


def config_lanes(config):
    return config['lanes'] if config['version'] == 3 else [
        dict(workspace=config['workspace'], participants=config['participants'])]


def lane_config(config, lane):
    """A lane has the existing single-pair transport shape, never fresh authority."""
    return dict(version=2, workspace=lane['workspace'], participants=lane['participants'],
                scope=config['scope'], controller=config['controller'])


def validate_lane_repositories(config):
    from companion.workflow_control import repository_identity
    common = repository_identity(config['workspace'], require_root=config['version'] == 3)
    for lane in config_lanes(config):
        if (repository_identity(lane['workspace'], require_root=config['version'] == 3) != common
                or any(repository_identity(pin['cwd'], require_root=False) != common
                       for pin in lane['participants'].values())):
            raise ValueError('Participants and workspace must belong to the same repository')


def config_fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def discover():
    return (app_tasks.discover_claude(Path.home() / '.claude/sessions')
            + app_tasks.discover_codex(Path.home() / '.codex/sessions'))


def _bound(config, role, tasks):
    binding = config['participants'][role]
    task = app_tasks.choose([task for task in tasks if task.app == role],
                           repo=binding['cwd'], pinned_id=binding['id'])
    if role == 'claude' and sum(t.app == role and t.name == task.name for t in tasks) != 1:
        raise app_tasks.AmbiguousDestination('The Claude display name is not unique')
    return task


def workspace_revision(workspace):
    """Fingerprint tracked changes plus bounded non-ignored untracked files, not just HEAD."""
    root = Path(workspace).resolve()
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=15, check=False)
        if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
            raise ValueError('Workspace revision is unavailable or exceeds the review budget')
        return result.stdout
    digest = hashlib.sha256(git('rev-parse', 'HEAD'))
    digest.update(git('diff', '--binary', 'HEAD', '--'))
    paths = git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    total = 0
    for name in sorted(path for path in paths if path):
        path = root / name.decode('utf-8')
        if path.is_symlink():
            raise ValueError('Untracked symlink requires explicit review')
        if not path.resolve().is_relative_to(root):
            raise ValueError('Workspace path escaped')
        total += path.stat().st_size
        if total > 16 * 1024 * 1024:
            raise ValueError('Untracked work exceeds the review budget')
        digest.update(name + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def configure(root, *, workspace, scope, controller, participants=None, lanes=None, authority=None,
              receptionist_threads=None):
    from companion.team_chat import ACTIVE, TeamStore
    from companion.workflow_control import WorkflowStore

    authority = WorkflowStore() if authority is None else authority
    root = Path(root)
    store = TeamStore(root, controller=authority)
    store.check_namespace()
    workspace = str(Path(workspace).resolve())
    tasks = discover()
    if lanes is not None and participants is not None:
        raise ValueError('Choose lanes or the legacy participant pair')
    if receptionist_threads is None:
        receptionist_threads = (read_config(root) or {}).get('receptionist_threads', [])
    value = validate_config(dict(version=3, workspace=workspace, scope=scope, lanes=lanes, controller=controller)
                            if lanes is not None else dict(version=2, workspace=workspace, scope=scope,
                                                           participants=participants, controller=controller))
    value = validate_config(dict(value, receptionist_threads=receptionist_threads))
    validate_lane_repositories(value)
    for lane in config_lanes(value):
        for role in ('claude', 'codex'):
            _bound(lane_config(value, lane), role, tasks)
    with authority._locked():
        for lane in config_lanes(value):
            authority._dispatch_authority(task_id=controller['task_id'], version=controller['version'],
                                          repo=lane['workspace'], participants=lane['participants'],
                                          actions=['use_claude', 'use_codex'])
        with store.edit() as data:
            store.check_namespace(data)
            if any(run['status'] in ACTIVE or run['role_lease'] or run['write_lease']
                   for room in data['rooms'].values() for run in room['runs']):
                raise ValueError('Existing work must finish or be reconciled before changing bindings')
            if (root / 'config.json').is_symlink():
                raise ValueError('Configuration cannot be a symlink')
            write_json(root / 'config.json', value)
            (root / 'config.json').chmod(0o600)


_CODEX_APPS = ('/ChatGPT.app', '/Codex.app')


def _front_app():
    """Bundle path of the frontmost app, or None; read with lsappinfo, which needs no OS permission."""
    try:
        asn = subprocess.run(['lsappinfo', 'front'], capture_output=True, text=True, timeout=2).stdout.strip()
        info = subprocess.run(['lsappinfo', 'info', '-only', 'bundlepath', asn],
                              capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'="(.+)"', info)
    return found.group(1) if found else None


def open_codex_chat(thread_id, *, run=subprocess.run, front=_front_app, sleep=time.sleep):
    """Show a queued Codex chat so its task starts, then hand focus back (owner, 2026-10-01).

    A queued Codex task starts only while its chat is open in the app, and the app (inside ChatGPT) brings
    itself to the front on a thread link even with `open -g`. Focus goes back to the app the owner was using once
    Codex has taken it; if the owner moved elsewhere meanwhile, nothing is touched.
    """
    if not isinstance(thread_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', thread_id):
        return
    before = front()
    run(['open', '-g', f'codex://threads/{thread_id}'], timeout=10, check=False)
    if not before or before.endswith(_CODEX_APPS):
        return
    for _ in range(15):  # up to 3 s for Codex to take focus
        if (front() or '').endswith(_CODEX_APPS):
            break
        sleep(0.2)
    else:
        return
    sleep(1.0)  # Codex finishes activating after its first frame; switching sooner is undone
    if (front() or '').endswith(_CODEX_APPS):
        run(['open', '-a', before], timeout=10, check=False)


CLAUDE_DESKTOP_SESSIONS = Path.home() / 'Library/Application Support/Claude/claude-code-sessions'


LANE_RETRY_SECONDS = 300


def claude_desktop_id(cli_id, base=None):
    """The desktop session ("local_...") whose record names this chat id, or None.

    Lane config pins the chat (CLI) id, but the desktop link opens by the desktop id; the app keeps the pair in
    <base>/<org>/<account>/local_<id>.json as {"sessionId", "cliSessionId"}. Read-only, bounded, first exact match.
    """
    base = Path(CLAUDE_DESKTOP_SESSIONS if base is None else base)
    for path in sorted(base.glob('*/*/local_*.json'))[:2000]:
        try:
            if path.stat().st_size > 2_000_000:
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict):
            continue
        desktop = record.get('sessionId')
        if (record.get('cliSessionId') == cli_id and isinstance(desktop, str)
                and re.fullmatch(r'local_[A-Za-z0-9-]{1,120}', desktop) and record.get('isArchived') is not True):
            return desktop
    return None


def open_claude_lane(session_id, base=None, run=subprocess.run):
    """Open a lane's desktop link by its pinned chat id; never a CLI or a permission-dialog action.

    Verified 2026-10-04: `open claude://claude.ai/epitaxy/<desktop id>` starts a closed lane's chat process without a
    click. The chat id itself is not a desktop link, so it is mapped first; an unknown id raises instead of guessing.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
        raise ValueError('Invalid Claude session id')
    desktop = session_id if session_id.startswith('local_') else claude_desktop_id(session_id, base)
    if desktop is None:
        raise ValueError('No desktop session records this Claude chat')
    run(['open', f'claude://claude.ai/epitaxy/{desktop}'], timeout=5, check=True, capture_output=True)


class VisibleTeamTransport(TeamTransport):
    def __init__(self, root, *, controller=None, codex_home=None, show_codex=None,
                 claude_opener=None, clock=time.monotonic, sleep=time.sleep):
        from companion.workflow_control import WorkflowStore
        self.root = Path(root)
        self.deliveries = delivery_ledger.path_for(self.root.parent)
        self.claude_opener = claude_opener or open_claude_lane
        self.clock, self.sleep = clock, sleep
        self._lane_opened = {}
        self.show_codex = show_codex
        self.codex_home = _codex_home(codex_home)
        self.controller = WorkflowStore() if controller is None else controller

    def ensure_claude_lanes(self):
        config = read_config(self.root)
        if config is None:
            return None  # Existing missing-configuration gate remains responsible.
        claude_lanes = [(i, lane) for i, lane in enumerate(config_lanes(config), 1)
                        if isinstance((lane.get('participants') or {}).get('claude'), dict)
                        and isinstance(lane['participants']['claude'].get('id'), str)]
        closed = []
        for item in claude_lanes:
            if self.participant_live(config, item[1], 'claude'):
                self._lane_opened.pop(item[1]['participants']['claude']['id'], None)  # live now: no cool-down left
            else:
                closed.append(item)
        now = self.clock()
        # One open and one bounded wait per lane per LANE_RETRY_SECONDS: later checks (each step of a long request,
        # repeated phone sends) report a lane that stayed closed at once instead of reopening and waiting again.
        fresh = [(i, lane) for i, lane in closed
                 if now - self._lane_opened.get(lane['participants']['claude']['id'], -LANE_RETRY_SECONDS)
                 >= LANE_RETRY_SECONDS]
        deadline = now + (30 if fresh else 0)
        for _, lane in fresh:
            ident = lane['participants']['claude']['id']
            self._lane_opened[ident] = now
            try:
                self.claude_opener(ident)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
        while closed:
            still = [(i, lane) for i, lane in closed if not self.participant_live(config, lane, 'claude')]
            for item in closed:
                if item not in still:  # an open that worked never holds back a later reopen
                    self._lane_opened.pop(item[1]['participants']['claude']['id'], None)
            closed = still
            if not closed:
                return None
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(0.5, remaining))
        if closed:
            names = ', '.join(f'Claude lane {i}' for i, _ in closed)
            return f'{names} is still closed. Open it on the Mac and retry.'
        return None

    def binding(self):
        config = read_config(self.root)
        return config_fingerprint(config)

    def context(self):
        config = read_config(self.root)
        return self.context_for(config)

    def context_for(self, config):
        if config is None:
            raise ValueError('Connect the team on the Mac first')
        script = Path(__file__).resolve().parents[2] / 'scripts/team_chat.py'
        command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' revision --workspace ' + shlex.quote(config['workspace'])
        return (f'Assigned workspace: {json.dumps(config["workspace"])}.\n'
                f'Owner-approved engineering scope: {json.dumps(config["scope"])}.\n'
                'Do not switch branches or change app/model settings. The code checkout may be different from your native chat cwd; use the assigned workspace explicitly.\n'
                f'Before writing build/review results, get the exact workspace fingerprint with: {command}\n'
                'The fingerprint covers non-ignored code. Read and verify any separately approved ignored artifacts explicitly; it does not bind their contents.\n')

    def revision(self):
        config = read_config(self.root)
        return self.revision_for(config)

    def revision_for(self, config):
        if config is None:
            raise ValueError('Team configuration is unavailable')
        return workspace_revision(config['workspace'])

    def participant_live(self, config, lane, role):
        """Whether this lane's chat for the role is open now; lane choice only, never send authority."""
        try:
            _bound(lane_config(config, lane) if config['version'] == 3 else config, role, discover())
        except (ValueError, OSError):
            return False
        return True

    def send(self, *, role, run_id, stage, attempt, assignment_path, result_path, binding):
        config = read_config(self.root)
        if config is None or config['version'] not in (2, 3):
            return {'state': 'blocked'}
        if config['version'] == 3:
            if ({k: v for k, v in binding.items() if k != 'lane'} != config
                    or binding.get('lane') not in config['lanes']):
                return {'state': 'blocked'}
            binding = lane_config(config, binding['lane'])
        elif binding != config:
            return {'state': 'blocked'}
        try:
            task = _bound(binding, role, discover())
        except (ValueError, OSError):
            return {'state': 'blocked'}
        receipt = app_dispatch.send(task, str(assignment_path), str(result_path),
                                    kind='review' if stage == 'review' else 'bounded_build',
                                    store_path=self.deliveries, legacy_store_path=self.root / 'deliveries.json',
                                    preserve_model=True)
        if role == 'codex' and receipt.get('state') == 'queued' and self.show_codex is not None:
            def show(thread_id=task.id):
                try:
                    self.show_codex(thread_id)
                except (OSError, subprocess.SubprocessError):
                    pass  # Showing the chat is a convenience; the task stays queued and the step line links it.
            threading.Thread(target=show, daemon=True, name='kyra-show-codex').start()
        return {**receipt, 'binding_assurance': 'unique_name' if role == 'claude' else 'native_thread'}

    def delivery_status(self, run, *, codex_home=None):
        """Observe a matching Codex receipt; no sends, authority changes or retries."""
        if run.get('stage') not in {'build', 'reply_codex'} or not run.get('delivery_id'):
            return None
        try:
            rows = delivery_ledger.read(self.deliveries, legacy_store_path=self.root / 'deliveries.json',
                                     max_bytes=_METADATA_BYTES)
            matches = [row for row in rows if isinstance(row, dict) and row.get('id') == run['delivery_id']]
            if len(matches) != 1:
                return None
            receipt = matches[0]
            binding = run.get('lane') or run.get('submitted_config', {})
            pin = binding.get('participants', {}).get('codex', {})
            if (receipt.get('app') != 'codex' or receipt.get('task_id') != pin.get('id')
                    or receipt.get('assignment') != run.get('assignment_path')
                    or receipt.get('result') != run.get('result_path')
                    or receipt.get('state') not in {'queued', 'acknowledged', 'running'}):
                return None
            return 'running' if _codex_started(receipt, codex_home=self.codex_home if codex_home is None else codex_home) else None
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def status(self):
        participants = []
        try:
            config = read_config(self.root)
        except (ValueError, OSError, TypeError):
            config = None
        tasks = discover() if config else []
        for role, name, initials, description in [('claude', 'Claude', 'Cl', 'Plans and independent checks'),
                                                  ('codex', 'Codex', 'Cx', 'Builds and coordinates code')]:
            available = False
            if config:
                for lane in config_lanes(config):
                    try:
                        _bound(config if config['version'] == 1 else lane_config(config, lane), role, tasks)
                        available = True
                    except ValueError:
                        pass
            participants.append(dict(id=role, display_name=name, initials=initials, role=description,
                                     model='Selected in the native app; served model not reported', available=available))
        for role, name in [('local', 'Local models'), ('cursor', 'Cursor'), ('antigravity', 'Antigravity')]:
            participants.append(dict(id=role, display_name=name, initials=name[:2], role='Not connected to Team yet', model='', available=False))
        ready = bool(config and config['version'] in (2, 3) and all(p['available'] for p in participants[:2]))
        if ready:
            try:
                grant = config['controller']
                with self.controller._locked():
                    for lane in config_lanes(config):
                        self.controller._dispatch_authority(
                            task_id=grant['task_id'], version=grant['version'], repo=lane['workspace'],
                            participants=lane['participants'], actions=['use_claude', 'use_codex'])
            except (ValueError, OSError, KeyError):
                ready = False
        if not ready:
            for member in participants[:2]:
                member['available'] = False
        return dict(participants=participants, ready=ready,
                    reason='Chats connected. Dispatch also requires current controller authority. Claude uses a unique name; native chat history is shared across rooms.' if ready
                    else 'Bind the visible chats to current controller authority on this Mac before starting shared work.')
