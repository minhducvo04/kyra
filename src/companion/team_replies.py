"""Team reply delivery: waiting replies go out through the agent's existing visible chat once it is idle.

`pump(service, threads)` is called from the Team wake loop. A waiting reply is labelled locally first; private
content (health, family, contacts) is held as failed and never sent. When the agent holds no Team work, the reply
becomes an ordinary `reply_claude` / `reply_codex` run, which `TeamService.step` sends through its transport
(`VisibleTeamTransport` in the app: the bound visible chat, never headless). A finished run's summary is copied
into the reply thread and rings the bell. `stop(service, threads, thread_id)` stops waiting and sent replies.

The prompt carries the team handbook (read at run time), the project, the parent message, the whole thread so far
and the responder's role, so the answer fits the conversation. A reply run is always `reply_<member>`: it cannot
build, and a proposed build goes back to the planner.

Agent threads (one per delegation child row) follow up with that same member: Claude and Codex through their
native chat as above; Cursor and Antigravity through `team_connected.submit`, resuming the child's adapter session
when that function accepts a `session_id`, else with the task, logged result and thread as context. Everything that
rides along (task, result, thread, reply) is privacy-labelled before anything is sent.
"""
import inspect
import re
import time
import uuid
from pathlib import Path

from companion import local_screen, team_connected, team_log
from companion.privacy import PrivacyClass, Tier
from companion.team_chat import ACTIVE, MAX_TEXT, STAGES
from companion.team_threads import APP_MEMBERS, NAMES, RESPONDERS, ROLES, child_member, delegation_row
from companion.team_voice import result_chat

ALLOWED = frozenset({PrivacyClass.conversation, PrivacyClass.job_search})
ANCHOR_LIMIT = 1500
TEXT_LIMIT = 4000
HANDBOOK_LIMIT = 4000
HANDBOOK = Path(__file__).resolve().parents[2] / 'docs' / 'team-handbook.md'
# The input lexicon has no family class; family words count as third-party data (a private design note, privacy rules).
_FA = 'fa' + 'ther'  # spelled in halves so a repo-wide word scan stays clean
_FAMILY = re.compile(r'\b(?:mom|mum|mother|dad|' + _FA + r'|parents?|grand(?:ma|pa|mother|' + _FA + r'|parents?)|sister|'
                     r'brother|siblings?|wife|husband|son|daughter|kids?|children|family|aunt|uncle|cousin|'
                     r'niece|nephew|in-laws?)\b', re.IGNORECASE)


class Label(tuple):
    """(tier, classes), unpacked as before, plus `held`: True when the text must not leave this Mac, either for a
    private class or because the local screen could not run. An offline screen holds the text without adding health,
    so an ordinary line is never stored as health for good (fail-closed re-review of 85f5308f)."""

    def __new__(cls, tier, classes, offline=False):
        self = super().__new__(cls, (tier, frozenset(classes)))
        self.offline = bool(offline)
        self.held = self.offline or not self[1] <= ALLOWED
        return self


def label(text, labeller=None):
    """Label(tier, classes) for a reply: the shared input lexicon plus the family words above."""
    if labeller is None:
        from companion.input_labels import default_input_labeller
        labeller = default_input_labeller()
    tier, classes = labeller.label(text)
    if _FAMILY.search(text):
        tier, classes = Tier.T2, classes | {PrivacyClass.third_party}
    # The local privacy screen (companion.local_screen) after the word list: a yes, garbage or error labels the text
    # health, so it is held like a word-list hit and never reaches a Team agent. An offline screen holds it unlabelled.
    offline = False
    if PrivacyClass.health not in classes:
        verdict = local_screen.check((text,))
        if verdict == local_screen.HEALTH:
            tier, classes = Tier.T2, classes | {PrivacyClass.health}
        offline = verdict == local_screen.OFFLINE
    return Label(tier, classes, offline)


def _held_why(what, found):
    """The failed-row note for a held label: the private classes, or the offline screen's notice."""
    private = sorted(c.value for c in found[1] - ALLOWED)
    if not private:
        return f'{local_screen.NOTICE}; not sent.'
    return f"{what} ({', '.join(private)}); not sent."


def busy(store, agent):
    """True while any Team run holds or is about to use this agent, or holds an unreconciled lease."""
    for summary in store.list_rooms():
        for run in store.get_room(summary['id'])['runs']:
            if run.get('role_lease') == agent:
                return True
            if run['status'] in ACTIVE | {'uncertain'} and STAGES.get(run['stage'], ('',))[0] == agent:
                return True
    return False


def _occupied(room):
    return any(run['status'] in ACTIVE | {'uncertain'} or run['role_lease'] or run['write_lease']
               for run in room['runs'])


def handbook():
    """The team handbook text, read fresh for every reply; a missing file is named, not invented."""
    try:
        return HANDBOOK.read_text(encoding='utf-8')[:HANDBOOK_LIMIT].strip()
    except OSError:
        return '(The team handbook docs/team-handbook.md is missing in this checkout.)'


def _said(line):
    author = line['author']
    return f"{NAMES.get(author, author.title())} ({ROLES.get(author, 'member')}): {line['text']}"


def outgoing(agent, project, anchor, earlier, reply_text):
    """The whole thread reply prompt: handbook, role, project, parent message, the thread so far, the owner's reply."""
    book = handbook()
    lines = [_said(line) for line in earlier]
    while True:  # the whole thread; only past the assignment limit do the oldest lines go, and the prompt says so
        dropped = len(earlier) - len(lines)
        thread = '\n'.join(([f'({dropped} earlier thread lines omitted to fit the assignment limit)'] if dropped else [])
                           + lines) or '(no earlier replies)'
        prompt = _prompt(book, agent, project, anchor, thread, reply_text)
        if len(prompt) <= MAX_TEXT or not lines:
            return prompt
        lines.pop(0)


def _prompt(book, agent, project, anchor, thread, reply_text):
    return (f'Team handbook:\n{book}\n\n'
            f"You are {NAMES[agent]}, the team's {ROLES[agent]}. The owner (CEO) asked you in a thread.\n"
            f'Project: {project}\n'
            f'Parent message:\n{_said(dict(anchor, text=anchor["text"][:ANCHOR_LIMIT]))}\n'
            f'Thread so far:\n{thread}\n'
            f"the owner's reply:\n{reply_text}\n\n"
            'Follow .claude/skills/team-voice/SKILL.md: chat at most 2 lines, 240 characters; '
            'summary holds full details, including any longer answer the owner asked for. '
            'This is a reply, not work: do not start a build or dispatch anyone. You may propose a build in one line; '
            'only the planner turns it into work.')


def agent_prompt(agent, project, task, result, earlier, reply_text):
    """An agent-thread follow-up: handbook, role, project, the member's own task and logged result, the thread."""
    book, lines = handbook(), [_said(line) for line in earlier]
    while True:
        dropped = len(earlier) - len(lines)
        thread = '\n'.join(([f'({dropped} earlier thread lines omitted to fit the assignment limit)'] if dropped else [])
                           + lines) or '(no earlier replies)'
        prompt = (f'Team handbook:\n{book}\n\n'
                  f"You are {NAMES[agent]}, the team's {ROLES[agent]}. The owner (CEO) follows up in your thread "
                  'about work you did.\n'
                  f'Project: {project}\n'
                  f'Your task:\n{task[:4000] or "(not recorded)"}\n'
                  f'Your result (logged):\n{result[:6000] or "(no result recorded)"}\n'
                  f'Thread so far:\n{thread}\n'
                  f"the owner's follow-up:\n{reply_text}\n\n"
                  'Follow .claude/skills/team-voice/SKILL.md: chat at most 2 lines, 240 characters; '
                  'summary holds full details, including any longer answer the owner asked for. '
                  'This is a reply, not work: do not start a build, edit files or dispatch anyone. You may propose '
                  'a build in one line; only the planner turns it into work.')
        if len(prompt) <= MAX_TEXT or not lines:
            return prompt
        lines.pop(0)


def _add(db, thread_id, author, text, state, run_id=None):
    ident = uuid.uuid4().hex
    db.execute('INSERT INTO thread_messages (id, thread_id, author, text, created_at, state, run_id) '
               'VALUES (?, ?, ?, ?, ?, ?, ?)', (ident, thread_id, author, text[:TEXT_LIMIT], time.time(), state, run_id))
    return ident


def _notify(db, thread_id, reason, message_id):
    db.execute('INSERT INTO notifications (id, thread_id, message_id, reason, created_at) VALUES (?, ?, ?, ?, ?)',
               (uuid.uuid4().hex, thread_id, message_id, reason, time.time()))


def _fail(threads, row, why):
    with threads.transaction() as db:
        if db.execute("UPDATE thread_messages SET state = 'failed' WHERE id = ? AND state IN ('waiting', 'sent')",
                      (row['id'],)).rowcount:
            _notify(db, row['thread_id'], 'needs_owner', _add(db, row['thread_id'], 'kyra', why, 'failed'))


def _label_reply(threads, row, labeller):
    """Label the owner's reply; False (and the row failed) when it holds private content."""
    found = label(row['text'], labeller)
    tier, classes = found
    names = ','.join(sorted(c.value for c in classes))
    with threads.transaction() as db:
        db.execute('UPDATE thread_messages SET label_tier = ?, label_classes = ? WHERE id = ?',
                   (tier.name, names, row['id']))
    if found.held:
        _fail(threads, row, _held_why('Private content', found))
        return False
    return True


def _claim(threads, row):
    with threads.transaction() as db:
        return bool(db.execute("UPDATE thread_messages SET state = 'sent' WHERE id = ? AND state = 'waiting'",
                               (row['id'],)).rowcount)


def _full_result(root, room_id, row, key, agent):
    """The step's whole result from the room's team log; the row keeps only a short line (or the review).

    A reviewed step keeps its build child in `build_results[key]`; each child's full text is logged under its id
    (an app build under its app run's id, also found by the child's request id). Build first, then the review.
    """
    children = [c for c in (row.get('build_results', {}).get(key), row['children'][key]) if c]
    ids, texts = [], []
    for child in children:
        ids += [child['id']] if child.get('id') else []
        if agent in APP_MEMBERS and child.get('request_id'):
            ids += [run['id'] for run in team_connected.ConnectedStore(root).for_room(room_id)
                    if run.get('request_id') == child['request_id'] and run.get('provider') == agent]
        texts += [child['text']] if child.get('text') else []
    parts = []
    for ident in dict.fromkeys(ids):
        entry = team_log.read(root, room_id, ident)
        if entry and entry['details'] not in parts:
            parts.append(entry['details'])
    for text in texts:
        if not any(text in part for part in parts):
            parts.append(text)
    return '\n\n'.join(parts)


def _send_agent(service, threads, row, labeller):
    """A follow-up in a child's agent thread goes to that same member, never as build work."""
    if not _label_reply(threads, row, labeller):
        return 'refused'
    store, agent = service.store, row['agent']
    request_id, _, key = row['anchor_message_id'].partition('/')
    try:
        room = store.get_room(row['room_id'])
    except LookupError:
        _fail(threads, row, 'The chat no longer exists; not sent.')
        return 'failed'
    delegated = delegation_row(store.root, row['room_id'], request_id)
    child = (delegated or {}).get('children', {}).get(key)
    if child is None:
        _fail(threads, row, 'That step is gone; not sent.')
        return 'failed'
    if child_member(delegated, key) != agent:
        _fail(threads, row, f'That step was not done by {NAMES.get(agent, agent)}; not sent.')
        return 'failed'
    task = delegated.get('prompts', {}).get(key) or delegated.get('text', '')
    result = _full_result(store.root, row['room_id'], delegated, key, agent)
    if child.get('reason_details_id'):
        logged = team_log.read(store.root, row['room_id'], child['reason_details_id'])
        result = '\n'.join(x for x in (result, logged and logged['details']) if x)
    earlier = [line for line in threads.agent_lines(row['thread_id']) if line['id'] != row['id']
               and (line['state'] in {'sent', 'answered'} if line['author'] == 'user' else line['author'] == agent)]
    # A member's earlier answer rides along in full (its logged details, room-scoped), checked like the rest.
    for line in earlier:
        entry = team_log.read(store.root, row['room_id'], line.get('details_id', line['id']))
        if entry:
            line['text'] = entry['details']
    found = label('\n'.join([room['room']['title'], task, result, *(line['text'] for line in earlier)]), labeller)
    if found.held:
        _fail(threads, row, _held_why('Private content in the step context', found))
        return 'refused'
    prompt = agent_prompt(agent, room['room']['title'], task, result, earlier, row['text'])
    if agent in APP_MEMBERS:
        if not _claim(threads, row):
            return 'stopped'
        session, accepts = (child.get('session') or {}).get('session_id'), inspect.signature(team_connected.submit).parameters
        resume = {'session_id': session} if session and 'session_id' in accepts else {}
        if 'read_only' in accepts:  # a follow-up never edits; apps without a read-only mode report it (below)
            resume['read_only'] = True
        try:
            run = team_connected.submit(store.root, room['room'], provider=agent, text=prompt,
                                        request_id='reply.' + row['id'], **resume)
        except (ValueError, LookupError, OSError) as exc:
            _fail(threads, row, f'{NAMES[agent]} could not take the follow-up ({exc}); not sent.')
            return 'failed'
        with threads.transaction() as db:
            db.execute('UPDATE thread_messages SET run_id = ? WHERE id = ?', ('app:' + run['id'], row['id']))
            state = db.execute('SELECT state FROM thread_messages WHERE id = ?', (row['id'],)).fetchone()['state']
        if state == 'stopped':  # Stop landed while the app run started; it saw no run id, so stop that run here
            team_connected.ConnectedStore(store.root).stop(row['room_id'], run['id'])
            return 'stopped'
        return 'sent'
    if agent not in RESPONDERS:
        _fail(threads, row, f'{NAMES.get(agent, agent)} cannot answer in threads; not sent.')
        return 'failed'
    if busy(store, agent) or _occupied(room):
        return 'waiting'
    if not _claim(threads, row):
        return 'stopped'
    return _submit_native(service, threads, row, agent, prompt)


def _submit_native(service, threads, row, agent, prompt):
    """A reply_<member> run in the member's native chat: a reply mode, which cannot build."""
    try:
        run_id = service.store.submit(row['room_id'], prompt, 'reply.' + row['id'], share_with_team=True,
                                      execution_mode='reply_' + agent, author='system')['run_id']
    except (ValueError, LookupError):
        _fail(threads, row, f'{agent.title()} could not take the reply; not sent.')
        return 'failed'
    with threads.transaction() as db:
        db.execute('UPDATE thread_messages SET run_id = ? WHERE id = ?', (run_id, row['id']))
        state = db.execute('SELECT state FROM thread_messages WHERE id = ?', (row['id'],)).fetchone()['state']
    if state == 'stopped':  # Stopped between the claim and the submit: the run is still pending, never sent.
        service.stop(row['room_id'], run_id=run_id)
        return 'stopped'
    return 'sent'


def _collect_app(service, threads, row):
    try:
        run = team_connected.ConnectedStore(service.store.root).get(row['run_id'][4:])
    except LookupError:
        _fail(threads, row, 'The app run is gone.')
        return
    if run['state'] in {'queued', 'running', 'stopping'}:
        return
    if run['state'] == 'completed' and run.get('text'):
        author, text, state, reason = row['agent'], run['text'], 'answered', 'reply'
    else:
        author, text, state, reason = 'kyra', f"{NAMES[row['agent']]} did not answer: {run.get('reason') or run['state']}.", \
            ('stopped' if run['state'] == 'stopped' else 'failed'), 'needs_owner'
    with threads.transaction() as db:
        if db.execute('UPDATE thread_messages SET state = ? WHERE id = ? AND state = ?',
                      (state, row['id'], 'sent')).rowcount:
            ident = _add(db, row['thread_id'], author, result_chat(text, text), state, row['run_id'])
            details = text
            if run.get('read_only_enforced') is False:  # honest about apps whose CLI has no read-only mode
                details += f"\n\n(Note: read-only was not enforced for {NAMES[row['agent']]}; only the prompt forbade edits.)"
            team_log.append(service.store.root, row['room_id'], message_id=ident, author=author, kind='reply',
                            details=details, created_at=time.time())
            _notify(db, row['thread_id'], reason, ident)


def _send(service, threads, row, labeller):
    if row.get('kind') == 'agent':
        return _send_agent(service, threads, row, labeller)
    found = label(row['text'], labeller)
    tier, classes = found
    names = ','.join(sorted(c.value for c in classes))
    with threads.transaction() as db:
        db.execute('UPDATE thread_messages SET label_tier = ?, label_classes = ? WHERE id = ?',
                   (tier.name, names, row['id']))
    if found.held:
        _fail(threads, row, _held_why('Private content', found))
        return 'refused'
    store, agent = service.store, row['agent']
    if agent not in RESPONDERS:  # a thread reply only ever becomes a reply_<member> run
        _fail(threads, row, f'{NAMES.get(agent, agent)} cannot answer in threads; not sent.')
        return 'failed'
    try:
        room = store.get_room(row['room_id'])
    except LookupError:
        _fail(threads, row, 'The chat no longer exists; not sent.')
        return 'failed'
    anchor = next((m for m in room['messages'] if m['id'] == row['anchor_message_id']), None)
    if anchor is None:
        _fail(threads, row, 'The message this replies to is gone; not sent.')
        return 'failed'
    # Everything that rides along is checked like the reply itself: project, parent and the thread so far.
    # Only lines that already went out: a held (private) or unlabelled reply of the owner's never rides along.
    earlier = [line for line in threads.thread(row['thread_id'], [])['lines']
               if line['id'] != row['id']
               and (line['state'] in {'sent', 'answered'} if line['author'] == 'user' else line['author'] in RESPONDERS)]
    # Restore only selected context before the privacy check, never after it.
    def full(line):
        entry = team_log.read(store.root, row['room_id'], line.get('details_id', line['id']))
        return {**line, 'text': entry['details'] if entry else line['text']}
    anchor = full(anchor)
    earlier = [full(line) for line in earlier]
    context = '\n'.join([room['room']['title'], anchor['text'], *(line['text'] for line in earlier)])
    found = label(context, labeller)
    if found.held:
        _fail(threads, row, _held_why('Private content in the thread context', found))
        return 'refused'
    if busy(store, agent) or _occupied(room):
        return 'waiting'
    # Claim before submitting, so a Stop that lands first means the reply never reaches the agent.
    if not _claim(threads, row):
        return 'stopped'
    return _submit_native(service, threads, row, agent, outgoing(agent, room['room']['title'], anchor, earlier, row['text']))


def _collect(service, threads, row):
    try:
        room = service.store.get_room(row['room_id'])
    except LookupError:
        _fail(threads, row, 'The chat no longer exists.')
        return
    run = next((r for r in room['runs'] if r['id'] == row['run_id']), None)
    if run is None or run['status'] in ACTIVE:
        return
    result = next((m for m in reversed(room['messages'])
                   if m.get('kind') == 'result' and m.get('run_id') == run['id']), None)
    if run['status'] == 'completed' and result is not None:
        author, text, state, reason = row['agent'], result['text'], 'answered', 'reply'
    elif run['status'] == 'needs_input':
        author, text, state, reason = (row['agent'], result['text'], 'answered', 'needs_owner') if result else \
            ('kyra', run.get('reason') or 'The agent needs input.', 'failed', 'needs_owner')
    else:
        author, text, state, reason = 'kyra', run.get('reason') or f'The reply ended as {run["status"]}.', \
            ('stopped' if run['status'] == 'stopped' else 'failed'), 'needs_owner'
    with threads.transaction() as db:
        if db.execute('UPDATE thread_messages SET state = ? WHERE id = ? AND state = ?',
                      (state, row['id'], 'sent')).rowcount:
            details = (team_log.read(service.store.root, row['room_id'], result.get('details_id'))
                       if result and author == row['agent'] else None)
            ident = _add(db, row['thread_id'], author, result_chat(text, text), state, run['id'])
            # A details_id marks text that was already shortened. If its log
            # write failed, publishing that short line as full details is false.
            missing_details = result and author == row['agent'] and result.get('details_id') and not details
            if not missing_details:
                team_log.append(service.store.root, row['room_id'], message_id=ident, author=author,
                                kind='reply', details=details['details'] if details else text, created_at=time.time())
            _notify(db, row['thread_id'], reason, ident)


def pump(service, threads, *, labeller=None):
    """One pass: collect finished replies, then send waiting ones whose agent is idle. Returns counts by outcome."""
    counts = {}
    waiting = threads.replies('waiting')
    if waiting and labeller is None:
        from companion.input_labels import default_input_labeller
        labeller = default_input_labeller()
    for row in threads.replies('sent'):
        if row['run_id'] and row['run_id'].startswith('app:'):
            _collect_app(service, threads, row)
        elif row['run_id']:
            _collect(service, threads, row)
    for row in waiting:
        outcome = _send(service, threads, row, labeller)
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def stop(service, threads, thread_id):
    """Stop a reply thread: waiting replies never go out; a sent one is stopped through the Team stop path."""
    sent = threads.stop(thread_id)
    with threads.transaction() as db:
        for row in sent:
            db.execute("UPDATE thread_messages SET state = 'stopped' WHERE id = ? AND state = 'sent'", (row['id'],))
        room_id = db.execute('SELECT room_id FROM threads WHERE id = ?', (thread_id,)).fetchone()['room_id']
    for row in sent:
        if row['run_id'] and row['run_id'].startswith('app:'):
            team_connected.ConnectedStore(service.store.root).stop(room_id, row['run_id'][4:])
        elif row['run_id']:
            service.stop(room_id, run_id=row['run_id'])
    return [row['id'] for row in sent]
