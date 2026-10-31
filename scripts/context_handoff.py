#!/usr/bin/env python3
"""Self-clear for the long-lived chats (home chat, Board): loop-building node C0, design item 10.

check     read the chat's context percent; one warning line at WARN_PERCENT, the handoff order at CLEAR_PERCENT
handoff   fill a draft (JSON) into the one fixed handoff shape, validate it, write
          data/team/loop/handoffs/<role>-<UTC stamp>.md, emit one F0 context event, print the resume line
validate  refuse a handoff with a missing, empty or misshapen section
clear     refuse unless this session has a fresh valid handoff; then print the clear order and the resume line
resume    print the resume line and the newest handoff for a role (what the fresh session reads first)
register  mark a session id as a long-lived chat so the hook checks it (workers are never checked)
hook      Claude Code hook entry (stdin JSON): UserPromptSubmit runs check, SessionStart after /clear resumes

The context percent comes from read_context(): the desktop session tool's get_usage output
(context.percentUsed, passed with --usage-json) when available, else the last main-thread assistant turn's
usage in the transcript (input + cache read + cache creation over the window), else transcript bytes / 7.
The clear itself is the chat's own action (/clear or the session tool's clear_session); this script never
clears, sends or deletes anything. Data root: --data, else KYRA_DATA_DIR, else the main checkout's data/.
"""
from __future__ import annotations

import argparse
import calendar

try:
    import fcntl
except ImportError:  # Windows: no POSIX locks; callers fail at call time, not import time
    fcntl = None
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from scripts import context_hygiene as hygiene
except ImportError:  # run as a script: scripts/ is sys.path[0]
    import context_hygiene as hygiene

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parents[2] if ROOT.parent.name == 'worktrees' else ROOT
BYTES_PER_TOKEN = hygiene.BYTES_PER_TOKEN
FRESH_SECONDS = 30 * 60  # a handoff older than this does not license a clear or a resume claim

# The one fixed shape: (key, heading, item fields). Order is part of the shape. The first field of an item
# is its title; the rest are optional unless RULES says otherwise. Every *path* field holds an absolute path.
SECTIONS = (
    ('goal', 'Goal', None),
    ('running', 'Running work', ('what', 'path', 'run_id', 'branch', 'worktree', 'sha', 'log', 'pid',
                                 'done_marker', 'on_refusal')),
    ('done', 'Done', ('what', 'proof', 'branch', 'worktree', 'sha', 'contained_in')),
    ('decisions', 'Decisions', None),
    ('open_questions', 'Open questions', ('question', 'guess')),
    ('traps', 'Traps', ('trap', 'check')),
    ('next_steps', 'Next steps', ('step', 'command', 'path', 'run_id')),
    ('logs', 'Logs', None),
)
# Per section: fields every item needs, and a group of which at least one must be present.
RULES = {
    'running': (('done_marker',), ('path', 'run_id', 'worktree', 'log')),
    'done': (('proof',), ()),
    'open_questions': (('guess',), ()),
    'traps': (('check',), ()),
    'next_steps': ((), ('command', 'path', 'run_id')),
}
PATH_FIELDS = frozenset({'path', 'proof', 'worktree', 'log'})
REQUIRED_NONEMPTY = ('goal', 'next_steps', 'logs')
HEADER = ('role', 'session', 'time', 'checkout', 'context_percent', 'context_source')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')


def data_dir(value=None):
    return Path(value or os.environ.get('KYRA_DATA_DIR') or MAIN / 'data')


def loop_dir(data):
    return Path(data) / 'team/loop'


# ---- context reader (the one seam tests fake) ----------------------------------------------------------------

def read_context(*, usage=None, transcript=None, window=hygiene.CONTEXT_WINDOW):
    """{percent, tokens, window, source}: get_usage output first, then transcript usage, then transcript size."""
    context = (usage or {}).get('context', usage or {})
    if isinstance(context.get('percentUsed'), (int, float)) and context.get('status', 'ok') == 'ok':
        window = context.get('contextWindow') or window
        tokens = context.get('tokensUsed') or context['percentUsed'] * window / 100
        return dict(percent=float(context['percentUsed']), tokens=int(tokens), window=int(window), source='usage')
    if transcript is None or not Path(transcript).is_file():
        raise ValueError('no context source: pass --usage-json or a transcript that exists')
    tokens = _last_turn_tokens(Path(transcript))
    source = 'transcript'
    if tokens is None:
        tokens, source = Path(transcript).stat().st_size // BYTES_PER_TOKEN, 'size'
    return dict(percent=100 * tokens / window, tokens=tokens, window=window, source=source)


def _last_turn_tokens(path):
    """Prompt tokens of the last main-thread assistant turn; only the usage counts are kept from each row."""
    tokens = None
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = row.get('message')
            if row.get('type') != 'assistant' or row.get('isSidechain') or not isinstance(message, dict):
                continue
            usage = message.get('usage') or {}
            total = sum(usage.get(k) or 0 for k in
                        ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
            if total:
                tokens = total
    return tokens


def transcript_path(session):
    return Path.home() / '.claude/projects' / str(MAIN).replace('/', '-') / f'{session}.jsonl'


# ---- the handoff shape -----------------------------------------------------------------------------------------

def _line(field, value):
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ValueError(f'{field}: must be a nonempty single printable line')
    return value.strip()


def _absolute(field, value):
    if not _line(field, value).startswith('/'):
        raise ValueError(f'{field}: must be an absolute path')
    return value


def validate(doc):
    """Return the handoff dict when every header field and section is present and well shaped, else raise."""
    if not isinstance(doc, dict):
        raise ValueError('handoff must be an object')
    for key in HEADER:
        if key not in doc:
            raise ValueError(f'{key}: missing header field')
    for key, _, _ in SECTIONS:
        if key not in doc:
            raise ValueError(f'{key}: missing section')
    extra = sorted(set(doc) - set(HEADER) - {key for key, _, _ in SECTIONS})
    if extra:
        raise ValueError(f'{extra[0]}: unknown section, the handoff shape is fixed')
    for key in ('role', 'session'):
        if not isinstance(doc[key], str) or not _ID.fullmatch(doc[key]):
            raise ValueError(f'{key}: must be an id')
    if not isinstance(doc['time'], str) or not _TIME.fullmatch(doc['time']):
        raise ValueError('time: must be UTC YYYY-MM-DDTHH:MM:SSZ')
    _absolute('checkout', doc['checkout'])
    if isinstance(doc['context_percent'], bool) or not isinstance(doc['context_percent'], (int, float)):
        raise ValueError('context_percent: must be a number')
    if doc['context_source'] not in ('usage', 'transcript', 'size'):
        raise ValueError('context_source: must be usage, transcript or size')
    for key, _, fields in SECTIONS:
        items = doc[key]
        if not isinstance(items, list):
            raise ValueError(f'{key}: must be a list')
        if key in REQUIRED_NONEMPTY and not items:
            raise ValueError(f'{key}: must not be empty')
        for i, item in enumerate(items):
            name = f'{key}[{i}]'
            if fields is None:
                (_absolute if key == 'logs' else _line)(name, item)
                continue
            if not isinstance(item, dict) or set(item) - set(fields):
                raise ValueError(f'{name}: must be an object with only {", ".join(fields)}')
            required, one_of = RULES.get(key, ((), ()))
            for field in (fields[0], *required):
                if not item.get(field):
                    raise ValueError(f'{name}.{field}: missing')
            if one_of and not any(item.get(field) for field in one_of):
                raise ValueError(f'{name}: needs one of {", ".join(one_of)}')
            for field, value in item.items():
                (_absolute if field in PATH_FIELDS else _line)(f'{name}.{field}', value)
    if doc['checkout'] not in doc['logs']:
        raise ValueError('logs: must name the checkout root')
    return doc


def render(doc):
    validate(doc)
    out = [f"# Handoff: {doc['role']}", '']
    out += [f'{key}: {doc[key]}' for key in HEADER] + ['']
    for key, heading, fields in SECTIONS:
        out.append(f'## {heading}')
        if not doc[key]:
            out.append('- none')
        for item in doc[key]:
            if fields is None:
                out.append(f'- {item}')
                continue
            out.append(f'- {item[fields[0]]}')
            out += [f'  - {field}: {item[field]}' for field in fields[1:] if item.get(field)]
        out.append('')
    return '\n'.join(out)


def parse(text):
    """The dict back from a rendered handoff; a missing heading stays missing so validate refuses it."""
    doc, section = {}, None
    by_heading = {heading: (key, fields) for key, heading, fields in SECTIONS}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith('## '):
            if line[3:] not in by_heading:
                raise ValueError(f'{line[3:]}: unknown section heading')
            section = by_heading[line[3:]]
            doc[section[0]] = []
        elif section is None and ': ' in line and not line.startswith('#'):
            key, value = line.split(': ', 1)
            if key in HEADER:
                doc[key] = float(value) if key == 'context_percent' else value
        elif section is not None and line.startswith('  - ') and doc[section[0]]:
            name, _, value = line[4:].partition(': ')
            if not isinstance(doc[section[0]][-1], dict):
                raise ValueError(f'{section[0]}: plain items take no fields')
            doc[section[0]][-1][name] = value
        elif section is not None and line.startswith('- '):
            key, fields = section
            if line[2:] != 'none':
                doc[key].append(line[2:] if fields is None else {fields[0]: line[2:]})
    return doc


def validate_file(path):
    doc = validate(parse(Path(path).read_text(encoding='utf-8')))
    return doc, (f"valid: {path} ({len(SECTIONS)} sections, {len(doc['running'])} running, "
                 f"{len(doc['open_questions'])} open questions, {len(doc['next_steps'])} next steps)")


def resume_line(path):
    return (f'Resume from {path}: read it first, start its Next steps item 1 now and ask no questions; '
            'each Open question keeps its guess until the owner answers.')


# ---- git facts (read-only) --------------------------------------------------------------------------------------

def _git(repo, *args):
    run = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None


def worktrees(repo):
    """[{worktree, sha, branch}] from `git worktree list --porcelain`."""
    rows, row = [], {}
    for line in (_git(repo, 'worktree', 'list', '--porcelain') or '').splitlines() + ['']:
        if not line:
            if row:
                rows.append(row)
            row = {}
        elif line.startswith('worktree '):
            row['worktree'] = line[9:]
        elif line.startswith('HEAD '):
            row['sha'] = line[5:]
        elif line.startswith('branch '):
            row['branch'] = line[7:].removeprefix('refs/heads/')
    return rows


def enrich(doc, repo):
    """Fill branch, worktree and sha on Running and Done rows from git, and mark a Done row whose sha is inside
    another listed Done tip (contained_in). Never overwrites what the draft says."""
    trees = worktrees(repo)
    by_path = {row['worktree']: row for row in trees}
    rows = [row for key in ('running', 'done') for row in doc[key] if isinstance(row, dict)]
    for row in rows:
        tree = by_path.get(row.get('worktree')) or by_path.get(row.get('path'))
        if tree:
            row.setdefault('worktree', tree['worktree'])
            row.setdefault('sha', tree['sha'][:8])
            if tree.get('branch'):
                row.setdefault('branch', tree['branch'])
        if row.get('sha') and not row.get('branch'):
            names = (_git(repo, 'branch', '--format=%(refname:short)', '--points-at', row['sha']) or '').split()
            names = names or (_git(repo, 'branch', '--format=%(refname:short)', '--contains', row['sha']) or '').split()
            if names:
                row['branch'] = ', '.join(names[:3])
        if row.get('sha') and not row.get('worktree'):
            full = _git(repo, 'rev-parse', row['sha'])
            match = next((t for t in trees if t['sha'] == full), None)
            if match:
                row['worktree'] = match['worktree']
    # A Done row inside another Done tip (an integration base) says so, naming the lowest such tip.
    tips = {}
    for row in doc['done']:
        if isinstance(row, dict) and row.get('sha'):
            tips.setdefault(row['sha'], row)

    def inside(sha, tip):
        return _git(repo, 'merge-base', '--is-ancestor', sha, tip) is not None

    for row in doc['done']:
        if not isinstance(row, dict) or row.get('contained_in') or not row.get('sha'):
            continue
        holders = [tip for tip in tips if tip != row['sha'] and inside(row['sha'], tip)]
        lowest = [tip for tip in holders if not any(other != tip and inside(other, tip) for other in holders)]
        if lowest:
            other = tips[lowest[0]]
            where = (other.get('branch') or '').split(', ')[0] or other.get('worktree') or 'listed tip'
            row['contained_in'] = f'{where} {lowest[0]}'
    return doc


# ---- state, registry, runs in flight ---------------------------------------------------------------------------

def _update_json(path, change):
    """Read-modify-write one small JSON object under an exclusive lock; returns change()'s value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        state = json.loads(stream.read() or '{}')
        result = change(state)
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(state, sort_keys=True))
        return result


def runs_in_flight(data):
    """Live runs from the R0 run records (read-only): one running-work item each."""
    sys.path.insert(0, str(ROOT / 'src'))
    from companion.feature_team.runs import LIVE, Runs
    store = Runs(loop_dir(data), emit=lambda row: None)
    return [{'what': f"{rec['feature']}:{rec['node']} ({rec['state']})", 'run_id': run_id,
             'worktree': rec['worktree'], 'done_marker': f"state verify_pass or landed in {store.path(run_id)}",
             'on_refusal': 'verify_fail: route the fixback to the owning worker; timeout: one re-dispatch'}
            for run_id, rec in sorted(store.load_all().items()) if rec['state'] in LIVE]


def newest_handoff(data, role):
    paths = sorted((loop_dir(data) / 'handoffs').glob(f'{role}-*.md'))
    return paths[-1] if paths else None


# ---- commands --------------------------------------------------------------------------------------------------

def check(data, role, session, context, *, now=None, warn=hygiene.WARN_PERCENT, clear=hygiene.CLEAR_PERCENT):
    """Return the lines to show the chat. Warns once per session; repeats the handoff order every check at
    the clear threshold; emits a context event on each level change and each 5-point rise of the peak."""
    now = time.time() if now is None else now
    percent = context['percent']
    current = hygiene.level(percent, warn, clear)

    def change(state):
        row = state.setdefault(session, {'role': role, 'level': 'ok', 'peak': 0.0, 'warned': None})
        emit = current != row['level'] or percent >= row['peak'] + 5
        warn_now = current != 'ok' and row['warned'] is None
        if warn_now:
            row['warned'] = now
        row.update(level=current, peak=max(row['peak'], percent), seen=now)
        return emit, warn_now

    emit, warn_now = _update_json(loop_dir(data) / 'context-state.json', change)
    if emit:
        hygiene.emit_context(data, role, percent=percent, tokens=context['tokens'], window=context['window'],
                             source=context['source'], status=current, now=now)
    lines = []
    if warn_now and current == 'warn':
        lines.append(f"context {percent:.0f}% ({context['source']}): past the {warn}% warning; at {clear}% "
                     'write the handoff and clear')
    if current == 'handoff':
        lines.append(f"context {percent:.0f}% ({context['source']}): write the handoff now "
                     f'(scripts/context_handoff.py handoff --role {role} --session {session} --draft <json>), '
                     'then run /clear')
    return lines


def write_handoff(data, role, session, draft, context, *, now=None, repo=MAIN):
    """Fill the header, add the runs in flight and the git facts, validate, write; returns (path, line)."""
    now = time.time() if now is None else now
    doc = {key: list(draft.get(key) or []) for key, _, _ in SECTIONS}
    unknown = sorted(set(draft) - set(doc))
    if unknown:
        raise ValueError(f'{unknown[0]}: unknown section, the handoff shape is fixed')
    known = {item.get('run_id') for item in doc['running'] if isinstance(item, dict)}
    doc['running'] += [item for item in runs_in_flight(data) if item['run_id'] not in known]
    enrich(doc, repo)
    checkout = str(Path(repo).resolve())
    if checkout not in doc['logs']:
        doc['logs'].insert(0, checkout)
    doc.update(role=role, session=session, time=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
               checkout=checkout, context_percent=round(context['percent'], 1), context_source=context['source'])
    text = render(doc)
    folder = loop_dir(data) / 'handoffs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{role}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}.md"
    tmp = path.with_suffix('.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)
    _, line = validate_file(path)
    # The fixed name a cleared lane reads first: every package message starts "read HANDOFF-<lane>.md first".
    latest = folder / f'HANDOFF-{role}.md'
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, latest)
    _update_json(loop_dir(data) / 'handoffs/pending.json',
                 lambda state: state.update({role: {'session': session, 'path': str(path), 't': now}}))
    hygiene.emit_context(data, role, percent=context['percent'], tokens=context['tokens'], window=context['window'],
                         source=context['source'], status='handoff', path=path, now=now)
    return path, line


def clear_step(data, role, session, *, now=None):
    """Refuse unless the newest handoff for role is valid, from this session and fresh; returns its path."""
    now = time.time() if now is None else now
    path = newest_handoff(data, role)
    if path is None:
        raise ValueError(f'no handoff for {role}: write one first (handoff --draft)')
    doc, _ = validate_file(path)
    if doc['session'] != session:
        raise ValueError(f'newest handoff {path} is from session {doc["session"]}, not {session}')
    written = calendar.timegm(time.strptime(doc['time'], '%Y-%m-%dT%H:%M:%SZ'))
    if now - written > FRESH_SECONDS:
        raise ValueError(f'newest handoff {path} is older than {FRESH_SECONDS // 60} minutes: write a fresh one')
    hygiene.emit_context(data, role, percent=doc['context_percent'], tokens=0, window=hygiene.CONTEXT_WINDOW,
                         source=doc['context_source'], status='clear', path=path, now=now)
    return path


def register(data, role, session):
    _update_json(loop_dir(data) / 'long-lived.json', lambda state: state.update({session: role}))


def claim_resume(data, session, *, now=None):
    """SessionStart after /clear: the fresh pending handoff (one per role) becomes this session's; returns
    (role, path) or None. The new session is registered so its prompts are checked too."""
    now = time.time() if now is None else now

    def change(state):
        fresh = [(row['t'], role, row) for role, row in state.items() if now - row['t'] <= FRESH_SECONDS]
        if not fresh:
            return None
        _, role, row = max(fresh)
        del state[role]
        return role, Path(row['path'])

    claimed = _update_json(loop_dir(data) / 'handoffs/pending.json', change)
    if claimed:
        register(data, claimed[0], session)
    return claimed


def hook(data, event, *, window=hygiene.CONTEXT_WINDOW, now=None):
    """Lines for Claude Code to add as context; nothing for sessions that are not registered long-lived chats."""
    name = event.get('hook_event_name')
    session = event.get('session_id') or ''
    if name == 'SessionStart' and event.get('source') == 'clear':
        claimed = claim_resume(data, session, now=now)
        if not claimed:
            return []
        role, path = claimed
        validate_file(path)
        return [resume_line(path), '', path.read_text(encoding='utf-8')]
    if name == 'UserPromptSubmit':
        registry = loop_dir(data) / 'long-lived.json'
        role = json.loads(registry.read_text() or '{}').get(session) if registry.exists() else None
        if not role or not event.get('transcript_path'):
            return []
        context = read_context(transcript=event['transcript_path'], window=window)
        return check(data, role, session, context, now=now)
    return []


def _context(args):
    usage = json.loads(Path(args.usage_json).read_text()) if args.usage_json else None
    transcript = args.transcript or (transcript_path(args.session) if getattr(args, 'session', None) else None)
    return read_context(usage=usage, transcript=transcript, window=args.window)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=Path, help='data root (default KYRA_DATA_DIR, else the main checkout data/)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'handoff', 'clear', 'resume', 'register'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--role', required=True)
        if name != 'resume':
            cmd.add_argument('--session', required=True)
        if name in ('check', 'handoff'):
            cmd.add_argument('--transcript', type=Path)
            cmd.add_argument('--usage-json', type=Path, help='saved get_usage output (context.percentUsed)')
            cmd.add_argument('--window', type=int, default=hygiene.CONTEXT_WINDOW)
    sub.choices['handoff'].add_argument('--draft', type=Path, required=True)
    sub.choices['handoff'].add_argument('--repo', type=Path, default=MAIN, help='checkout for the git facts')
    sub.add_parser('validate').add_argument('path', type=Path)
    sub.add_parser('hook').add_argument('--window', type=int, default=hygiene.CONTEXT_WINDOW)
    args = parser.parse_args(argv)
    data = data_dir(args.data)
    try:
        if args.command == 'check':
            lines = check(data, args.role, args.session, _context(args))
        elif args.command == 'handoff':
            path, valid = write_handoff(data, args.role, args.session,
                                        json.loads(args.draft.read_text(encoding='utf-8')), _context(args),
                                        repo=args.repo)
            lines = [str(path), valid, resume_line(path),
                     f'Next: clear --role {args.role} --session {args.session}, then /clear '
                     '(or the session tool clear_session).']
        elif args.command == 'validate':
            lines = [validate_file(args.path)[1]]
        elif args.command == 'clear':
            path = clear_step(data, args.role, args.session)
            lines = [f'handoff ok: {path}. Run /clear now; the fresh session reads first:', resume_line(path)]
        elif args.command == 'resume':
            path = newest_handoff(data, args.role)
            if path is None:
                raise ValueError(f'no handoff for {args.role}')
            validate_file(path)
            lines = [resume_line(path), '', path.read_text(encoding='utf-8')]
        elif args.command == 'register':
            register(data, args.role, args.session)
            lines = [f'registered {args.session} as long-lived chat {args.role}']
        else:
            lines = hook(data, json.load(sys.stdin), window=args.window)
    except (ValueError, OSError) as error:
        print(f'refused: {error}', file=sys.stderr)
        # A hook must never block the owner's prompt or the session start: report and pass.
        return 0 if args.command == 'hook' else 2
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
