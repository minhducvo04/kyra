"""Team threads: job threads derived from the room, reply threads and the bell in one local SQLite file.

Job threads (`job:<parent_request_id>`, `run:<run_id>`) are built on read from room messages; nothing is copied.
Reply and honesty threads, their messages and notifications live in `threads.sqlite3` under the Team root.
A reply thread hangs off any member's message, Slack style. Each reply names one responder: the first @mention of a
member, else the thread's default (the parent's author, or the planner for the owner own messages). Only members with a
reply mode in `team_chat` can answer; a thread never starts a build.

Agent threads (`agent:<hash>`) hang off one delegation child row (planner, interviewer, each task, native or app).
The id is fixed by (room, request, child key), so the page knows it before the first reply. A follow-up goes to that
same member; team_replies sends it with the child's task, logged result and the thread (see `delegation_row`).
"""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

AGENTS = ('claude', 'codex')
RESPONDERS = AGENTS  # members with a reply_<member> execution mode in team_chat
MEMBERS = ('user', 'kyra', 'claude', 'codex', 'cursor', 'antigravity')
NAMES = {'user': 'the owner', 'kyra': 'Kyra', 'claude': 'Claude', 'codex': 'Codex', 'cursor': 'Cursor',
         'antigravity': 'Antigravity'}
ROLES = {'user': 'CEO', 'kyra': 'local assistant', 'claude': 'planner', 'codex': 'builder', 'cursor': 'builder',
         'antigravity': 'builder'}
PLANNER = 'claude'
AGENT_MEMBERS = ('claude', 'codex', 'cursor', 'antigravity')  # members an agent thread can follow up with
APP_MEMBERS = ('cursor', 'antigravity')  # reached through team_connected, resumed by session id when possible
_MENTION = re.compile(r'(?<![\w@])@([A-Za-z]+)\b')
STATES = ('waiting', 'sent', 'answered', 'failed', 'stopped')
REASONS = ('reply', 'needs_owner', 'honesty')
_REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, room_id TEXT NOT NULL, kind TEXT NOT NULL, '
    'agent TEXT, anchor_message_id TEXT, created_at REAL NOT NULL, closed_at REAL)',
    'CREATE UNIQUE INDEX IF NOT EXISTS threads_anchor ON threads(room_id, anchor_message_id) '
    "WHERE kind = 'reply'",
    'CREATE UNIQUE INDEX IF NOT EXISTS threads_agent ON threads(room_id, anchor_message_id) '
    "WHERE kind = 'agent'",
    'CREATE TABLE IF NOT EXISTS thread_messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, author TEXT NOT NULL, '
    'text TEXT NOT NULL, created_at REAL NOT NULL, state TEXT, run_id TEXT, request_id TEXT UNIQUE, '
    'label_tier TEXT, label_classes TEXT, responder TEXT)',
    'CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, message_id TEXT, '
    'reason TEXT NOT NULL, created_at REAL NOT NULL, read_at REAL)',
    'CREATE TABLE IF NOT EXISTS honesty_checks (id TEXT PRIMARY KEY, trigger TEXT NOT NULL, window_from REAL, '
    'window_to REAL, model TEXT, status TEXT NOT NULL, messages_read INTEGER, messages_skipped INTEGER, '
    'started_at REAL NOT NULL, finished_at REAL)',
    'CREATE TABLE IF NOT EXISTS honesty_findings (id TEXT PRIMARY KEY, check_id TEXT NOT NULL, room_id TEXT NOT NULL, '
    'message_id TEXT NOT NULL, quote TEXT NOT NULL, problem TEXT NOT NULL, severity TEXT NOT NULL, evidence TEXT, '
    'training_candidate INTEGER NOT NULL DEFAULT 1, label_tier TEXT, label_classes TEXT, created_at REAL NOT NULL)',
    'CREATE TABLE IF NOT EXISTS honesty_verdicts (finding_id TEXT NOT NULL, label TEXT NOT NULL, note TEXT, '
    'decided_at REAL NOT NULL)',
)


def responder(text, default):
    """The member a thread reply goes to: the first @mention of a member, else `default`."""
    for name in _MENTION.findall(text):
        name = name.lower()
        if name in RESPONDERS:
            return name
        if name in MEMBERS and name != 'user':
            raise ValueError(f'{NAMES[name]} cannot answer in threads yet; mention @claude or @codex')
    return default


def thread_run_ids(room):
    """Run ids of thread replies in a room value: their prompt and result belong in the thread, not the channel."""
    return {m['run_id'] for m in room['messages'] if str(m.get('request_id') or '').startswith('reply.')
            and m.get('run_id')}


def agent_thread_id(room_id, request_id, key):
    """The fixed thread id of one delegation child row."""
    raw = json.dumps([room_id, request_id, key], separators=(',', ':')).encode()
    return 'agent:' + hashlib.sha256(raw).hexdigest()[:32]


def delegation_row(root, room_id, request_id):
    """The saved delegation row (team_delegation's body JSON), read-only; None when absent."""
    path = Path(root) / 'delegations.sqlite3'
    if not path.is_file() or path.is_symlink():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    try:
        found = db.execute('SELECT body FROM delegations WHERE room_id = ? AND request_id = ?',
                           (room_id, request_id)).fetchone()
    finally:
        db.close()
    return json.loads(found[0]) if found else None


def child_member(row, key):
    """The member behind one child: its assignee, else the plan's provider, else its role."""
    child = row.get('children', {}).get(key) or {}
    if child.get('assignee') in AGENT_MEMBERS:
        return child['assignee']
    plan = row.get('plan') or []
    keys = [task.get('key') for task in plan] if all('key' in task for task in plan) else \
        row.get('task_targets') or [task.get('provider') for task in plan]
    provider = next((task.get('provider') for k, task in zip(keys, plan, strict=False) if k == key), None)
    if provider in AGENT_MEMBERS:
        return provider
    role = row.get('roles', {}).get(key) or ('interviewer' if key.startswith('interviewer:') else key)
    if role in AGENT_MEMBERS:
        return role
    return {'planner': 'claude', 'interviewer': 'claude', 'reviewer': 'claude', 'worker': 'codex'}.get(role)


def _line(message):
    keys = ('id', 'author', 'kind', 'step', 'text', 'created_at', 'run_id', 'details_id')
    return {k: message[k] for k in keys if k in message}


def job_threads(room):
    """Derived threads of one room value (as TeamStore.get_room returns it): {thread_id: [lines]}, ordered."""
    if room['room'].get('local_only'):
        return {}
    run_job = {run['id']: run['parent_request_id'] for run in room['runs'] if run.get('parent_request_id')}
    for message in room['messages']:
        if message.get('kind') in {'step', 'report'} and message.get('parent_request_id') and message.get('run_id'):
            run_job.setdefault(message['run_id'], message['parent_request_id'])
    threads = {}
    for message in room['messages']:
        if message.get('kind') in {'step', 'report'} and message.get('parent_request_id'):
            key = 'job:' + message['parent_request_id']
        elif message.get('author') in AGENTS and message.get('run_id') and message.get('kind') != 'answer':
            job = run_job.get(message['run_id'])
            key = 'job:' + job if job else 'run:' + message['run_id']
        elif message.get('kind') in {'step', 'report'} and message.get('run_id'):
            key = 'run:' + message['run_id']
        else:
            continue
        threads.setdefault(key, []).append(_line(message))
    for lines in threads.values():
        lines.sort(key=lambda line: line['created_at'])
    return threads


def _upgrade(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(thread_messages)')}
    if columns and 'responder' not in columns:
        db.execute('ALTER TABLE thread_messages ADD COLUMN responder TEXT')


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'threads.sqlite3'
        if self.root.is_symlink() or self.path.is_symlink():
            raise ValueError('Invalid thread store')

    @contextmanager
    def _connect(self, write=True):
        if not write and not self.path.exists():
            yield None
            return
        if write and not self.path.exists():
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            if not write:  # an older store gains new columns before any read depends on them
                with db:
                    _upgrade(db)
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                    for statement in _SCHEMA:
                        db.execute(statement)
                    _upgrade(db)
                yield db
        finally:
            db.close()
        if write:
            self.path.chmod(0o600)

    def transaction(self):
        """One immediate write transaction for callers that settle reply rows (team_replies)."""
        return self._connect()

    def replies(self, state):
        """the owner reply rows in one state, oldest first, with their thread's room, agent and anchor."""
        with self._connect(write=False) as db:
            if db is None:
                return []
            return [dict(r) for r in db.execute(
                "SELECT m.*, t.room_id, t.kind, COALESCE(m.responder, t.agent) AS agent, t.anchor_message_id "
                "FROM thread_messages m JOIN threads t ON t.id = m.thread_id "
                "WHERE t.kind IN ('reply', 'agent') AND m.author = 'user' AND m.state = ? "
                'ORDER BY m.created_at, m.rowid', (state,))]

    # Threads

    def list_threads(self, room):
        """Job threads (derived) and stored threads for one room value, newest first."""
        if room['room'].get('local_only'):
            return []
        result = [dict(id=key, kind=key.split(':', 1)[0], room_id=room['room']['id'], agent=None,
                       title=lines[0]['text'].split('\n', 1)[0][:160], count=len(lines),
                       updated_at=lines[-1]['created_at'])
                  for key, lines in job_threads(room).items()]
        anchors = {m['id']: m for m in room['messages']}
        with self._connect(write=False) as db:
            rows = [] if db is None else db.execute(
                'SELECT t.*, COUNT(m.id) AS count, MAX(m.created_at) AS last FROM threads t '
                'LEFT JOIN thread_messages m ON m.thread_id = t.id WHERE t.room_id = ? GROUP BY t.id',
                (room['room']['id'],)).fetchall()
        for row in rows:
            anchor = anchors.get(row['anchor_message_id'], {})
            result.append(dict(id=row['id'], kind=row['kind'], room_id=row['room_id'], agent=row['agent'],
                               title=anchor.get('text', row['kind'].title()).split('\n', 1)[0][:160],
                               anchor_message_id=row['anchor_message_id'],
                               count=row['count'], updated_at=row['last'] or row['created_at']))
        return sorted(result, key=lambda t: t['updated_at'], reverse=True)

    def thread(self, thread_id, rooms):
        """One thread's lines. `rooms` is an iterable of room values to search for derived threads."""
        if thread_id.startswith(('job:', 'run:')):
            rooms = list(rooms)
            for room in rooms:
                lines = job_threads(room).get(thread_id)
                if lines:
                    return dict(thread=dict(id=thread_id, kind=thread_id.split(':', 1)[0],
                                            room_id=room['room']['id'], agent=None), anchor=None, lines=lines)
            # The page names an old run by its run id, but the server files the run under its job once the run
            # record carries the job id; follow the run to the job thread that holds its lines.
            if thread_id.startswith('run:'):
                run_id = thread_id[4:]
                for room in rooms:
                    for key, lines in job_threads(room).items():
                        if any(line.get('run_id') == run_id for line in lines):
                            return dict(thread=dict(id=key, kind=key.split(':', 1)[0],
                                                    room_id=room['room']['id'], agent=None), anchor=None, lines=lines)
            raise LookupError('Thread not found')
        with self._connect(write=False) as db:
            row = db and db.execute('SELECT * FROM threads WHERE id = ?', (thread_id,)).fetchone()
            if not row:
                raise LookupError('Thread not found')
            lines = [dict(m) for m in db.execute(
                'SELECT id, author, text, created_at, state, run_id, request_id, responder FROM thread_messages '
                'WHERE thread_id = ? ORDER BY created_at, rowid', (thread_id,))]
        for line in lines:
            line['role'] = ROLES.get(line['author'], '')
            if line['author'] != 'user':  # team_replies logs a member reply's full text under the line's own id
                line['details_id'] = line['id']
        anchor = None
        for room in rooms:
            if room['room']['id'] == row['room_id']:
                found = next((m for m in room['messages'] if m['id'] == row['anchor_message_id']), None)
                anchor = found and _line(found)
        return dict(thread=dict(row), anchor=anchor, lines=lines)

    def reply(self, room, message_id, text, request_id):
        """Open or continue the reply thread anchored on an agent message; store the reply as waiting, once."""
        if room['room'].get('local_only'):
            raise PermissionError('Local-only chats have no reply threads')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Invalid reply text')
        if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
            raise ValueError('Invalid request id')
        anchor = next((m for m in room['messages'] if m['id'] == message_id), None)
        if anchor is None:
            raise LookupError('Message not found')
        if anchor.get('author') not in MEMBERS or anchor.get('kind') in {'step', 'report'}:
            raise ValueError('Only member messages take thread replies')
        if anchor.get('run_id') in thread_run_ids(room):
            raise ValueError('Reply in the thread this message belongs to')
        default = anchor['author'] if anchor['author'] in RESPONDERS else PLANNER
        to = responder(text, default)
        room_id, now = room['room']['id'], time.time()
        with self._connect() as db:
            prior = db.execute('SELECT * FROM thread_messages WHERE request_id = ?', (request_id,)).fetchone()
            if prior is not None:
                thread = db.execute('SELECT * FROM threads WHERE id = ?', (prior['thread_id'],)).fetchone()
                if (thread['room_id'], thread['anchor_message_id'], prior['text']) != (room_id, message_id, text):
                    raise ValueError('Request id reused for a different reply')
                return dict(thread_id=thread['id'], message=dict(prior))
            thread = db.execute("SELECT * FROM threads WHERE room_id = ? AND anchor_message_id = ? AND kind = 'reply'",
                                (room_id, message_id)).fetchone()
            thread_id = thread['id'] if thread else 'reply:' + uuid.uuid4().hex
            if thread is None:
                db.execute("INSERT INTO threads (id, room_id, kind, agent, anchor_message_id, created_at) "
                           "VALUES (?, ?, 'reply', ?, ?, ?)", (thread_id, room_id, default, message_id, now))
            message = dict(id=uuid.uuid4().hex, thread_id=thread_id, author='user', text=text, created_at=now,
                           state='waiting', run_id=None, request_id=request_id, label_tier=None, label_classes=None,
                           responder=to)
            db.execute('INSERT INTO thread_messages (id, thread_id, author, text, created_at, state, run_id, '
                       'request_id, label_tier, label_classes, responder) VALUES (:id, :thread_id, :author, :text, '
                       ':created_at, :state, :run_id, :request_id, :label_tier, :label_classes, :responder)', message)
        return dict(thread_id=thread_id, message=message)

    def agent_reply(self, room, request_id, key, member, text, message_request):
        """Store the owner follow-up in one child's agent thread, as waiting, once per request id."""
        if room['room'].get('local_only'):
            raise PermissionError('Local-only chats have no agent threads')
        if member not in AGENT_MEMBERS:
            raise ValueError('Kyra and unknown members take no follow-ups here')
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Invalid reply text')
        for value in (request_id, key, message_request):
            if not isinstance(value, str) or not _REQUEST.fullmatch(value):
                raise ValueError('Invalid request id')
        room_id, now = room['room']['id'], time.time()
        thread_id, anchor = agent_thread_id(room_id, request_id, key), f'{request_id}/{key}'
        with self._connect() as db:
            prior = db.execute('SELECT * FROM thread_messages WHERE request_id = ?', (message_request,)).fetchone()
            if prior is not None:
                if (prior['thread_id'], prior['text']) != (thread_id, text):
                    raise ValueError('Request id reused for a different reply')
                return dict(thread_id=thread_id, message=dict(prior))
            found = db.execute('SELECT agent FROM threads WHERE id = ?', (thread_id,)).fetchone()
            if found is None:
                db.execute("INSERT INTO threads (id, room_id, kind, agent, anchor_message_id, created_at) "
                           "VALUES (?, ?, 'agent', ?, ?, ?)", (thread_id, room_id, member, anchor, now))
            elif found['agent'] != member:
                raise ValueError('This thread belongs to another member')
            message = dict(id=uuid.uuid4().hex, thread_id=thread_id, author='user', text=text, created_at=now,
                           state='waiting', run_id=None, request_id=message_request, label_tier=None,
                           label_classes=None, responder=member)
            db.execute('INSERT INTO thread_messages (id, thread_id, author, text, created_at, state, run_id, '
                       'request_id, label_tier, label_classes, responder) VALUES (:id, :thread_id, :author, :text, '
                       ':created_at, :state, :run_id, :request_id, :label_tier, :label_classes, :responder)', message)
        return dict(thread_id=thread_id, message=message)

    def agent_lines(self, thread_id):
        """An agent thread's lines, oldest first; [] before its first reply."""
        try:
            return self.thread(thread_id, [])['lines']
        except LookupError:
            return []

    def agent_threads(self, room_id):
        """{'<request>/<key>': {thread_id, count, unread}} for every agent thread in a room, for the agent rows."""
        with self._connect(write=False) as db:
            if db is None:
                return {}
            rows = db.execute(
                "SELECT t.id, t.anchor_message_id, "
                "(SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id) AS count, "
                "(SELECT COUNT(*) FROM notifications n WHERE n.thread_id = t.id AND n.read_at IS NULL) AS unread "
                "FROM threads t WHERE t.room_id = ? AND t.kind = 'agent'", (room_id,)).fetchall()
        return {row['anchor_message_id']: dict(thread_id=row['id'], count=row['count'], unread=row['unread'])
                for row in rows}

    def stop(self, thread_id):
        """Stop every waiting reply in a thread; returns the rows still `sent` for the caller's stop path."""
        with self._connect() as db:
            if db.execute('SELECT 1 FROM threads WHERE id = ?', (thread_id,)).fetchone() is None:
                raise LookupError('Thread not found')
            db.execute("UPDATE thread_messages SET state = 'stopped' WHERE thread_id = ? AND state = 'waiting'",
                       (thread_id,))
            return [dict(r) for r in db.execute(
                "SELECT * FROM thread_messages WHERE thread_id = ? AND state = 'sent'", (thread_id,))]

    # Bell

    def notify(self, thread_id, reason, message_id=None):
        if reason not in REASONS:
            raise ValueError('Invalid notification reason')
        ident = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('INSERT INTO notifications (id, thread_id, message_id, reason, created_at) VALUES (?, ?, ?, ?, ?)',
                       (ident, thread_id, message_id, reason, time.time()))
        return ident

    def notifications(self, limit=50):
        with self._connect(write=False) as db:
            if db is None:
                return dict(unread=0, items=[])
            unread = db.execute('SELECT COUNT(*) FROM notifications WHERE read_at IS NULL').fetchone()[0]
            items = [dict(r) for r in db.execute(
                'SELECT * FROM notifications ORDER BY created_at DESC, rowid DESC LIMIT ?', (limit,))]
        return dict(unread=unread, items=items)

    def mark_read(self, ids=None, everything=False):
        with self._connect() as db:
            if everything:
                db.execute('UPDATE notifications SET read_at = ? WHERE read_at IS NULL', (time.time(),))
            else:
                db.executemany('UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL',
                               [(time.time(), ident) for ident in ids or ()])
        return self.notifications()
