"""Route recorded failures and requests once, without contacting the owner."""

import hashlib
import json
import re


def rows(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A partial append is retried on the next tick.
        if isinstance(row, dict):
            result.append(row)
    return result


def refused(row):
    return (row.get('event', row.get('kind')) in {'verify', 'gate'}
            and str(row.get('verdict', row.get('status', ''))).lower()
            in {'refused', 'fail', 'failed', 'fix-first', 'changes_requested'})


def events(data, features):
    found = {}
    sources = [(None, data / 'team/board-inbox.jsonl')]
    sources += [(f['name'], data / 'team/features' / f['name'] / 'metrics.jsonl') for f in features]
    for feature, path in sources:
        for row in rows(path):
            if row.get('from') == 'dispatcher-fixback':
                continue
            if row.get('from') == 'harvest' and row.get('run_id'):
                continue  # a loop run's failed verify: the loop tick sends its one fix-back as a run on the same channel
            if not (refused(row) or row.get('kind') in {'request', 'report'}):
                continue
            matches = [f for f in features if row.get('node') in f['dag']._nodes
                       and (row.get('feature', feature) in (None, f['name']))]
            name = matches[0]['name'] if len(matches) == 1 else None
            key = hashlib.sha256(json.dumps([name, row], sort_keys=True).encode()).hexdigest()
            found[key] = (name, row)
    return found


def message(row):
    if refused(row):
        return (f"Fix-back {row['node']}: test {row.get('test', row.get('test_name', 'see evidence'))}\n"
                f"Error: {row.get('error', row.get('error_lines', row.get('evidence', 'not recorded')))}\n"
                f"Rule: {row.get('rule', 'not recorded')}\n"
                'Reproduce first; make the smallest fix and a failing-before regression. '
                'Stay within assigned files. Reply with sha, root cause, regression and pytest summary. Never push.')
    return json.dumps(row, sort_keys=True)


def author_of(state_row):
    """The builder of a node: `author` once verify has written by=verify:<run_id> (H0), else `by`."""
    by = state_row.get('by')
    return state_row.get('author') or (None if isinstance(by, str) and by.startswith('verify:') else by)


def destination(feature, row, failures):
    if row.get('kind') == 'report':
        return 'learning', ''
    if row.get('reason') == 'permission':
        return 'permission', ''
    if feature is None:
        return 'board', 'unknown or ambiguous node'
    node = feature['dag'].node(row['node'])
    author = author_of(feature['state'].get(row['node'], {}))
    if refused(row):
        if failures:
            return 'board', 'second failure'
    elif row.get('reason') in {'missing_input', 'broken_input'}:
        inputs = node['inputs']
        named = [n for n in inputs if re.search(r'(?<![\w-])' + re.escape(n) + r'(?![\w-])', row.get('needs', ''))]
        inputs = named or inputs
        if len(inputs) != 1:
            return 'board', 'input author is ambiguous'
        author = author_of(feature['state'].get(inputs[0], {}))
    elif row.get('reason') == 'unclear_spec':
        author = node.get('planner')
        if not author:
            planners = [w['name'] for w in feature['team'] if w.get('role', '').lower() == 'planner']
            author = planners[0] if len(planners) == 1 else None
    else:
        return 'board', row.get('reason', 'unknown request')
    worker = next((w for w in feature['team'] if author in (w['name'], w['channel'])), None)
    return (worker['channel'], '') if worker else ('board', 'author channel missing')


def append_once(path, key, row):
    if path.name == 'board-inbox.jsonl':
        from companion import board_inbox
        return board_inbox.append({**row, 'routing_id': key}, path=path, once=True)
    if any(r.get('routing_id') == key for r in rows(path)):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({**row, 'routing_id': key}) + '\n')


def normalized(value):
    return ' '.join(str(value or '').casefold().split())


def note_backlog(path, feature, row, *, now, event_id=None):
    """Count each event once per open (node, cause); retain closed and historical rows."""
    from companion import lessons_log
    from companion.feature_team.dispatch import locked

    raw_cause = next((row[field] for field in
                      ('cause', 'error', 'error_lines', 'reason', 'why', 'evidence', 'text')
                      if row.get(field)), 'not recorded')
    cause = normalized(raw_cause)
    key = hashlib.sha256(json.dumps([normalized(row.get('node')), cause]).encode()).hexdigest()
    node = None
    try:
        node = lessons_log._clean('node', row.get('node') or 'unknown')
        cause = normalized(lessons_log._clean('cause', raw_cause))
        text = lessons_log._clean('text', f'{node}: {cause}')
    except ValueError:
        # Keep rejected/long detail in the source event, not in searchable learning text.
        cause = f'details omitted ({key})'
        text = f'Fix-back: {cause}; review source event'
    # Share learning_backlog's lock; route also holds the dispatcher's global lock.
    with locked(path.with_suffix('.lock')):
        lines = path.read_bytes().splitlines(keepends=True) if path.exists() else []
        latest = None
        for index, line in enumerate(lines):
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            # Only rows created by this counter are mutable; never migrate legacy duplicates.
            if isinstance(existing, dict) and existing.get('backlog_key') == key:
                if event_id is not None and event_id in existing.get('event_ids', []):
                    return  # Also covers old events replayed after a completed learning session.
                latest = index, existing
        if latest is not None and latest[1].get('status') != 'done':
            index, existing = latest
            existing.update(count=existing['count'] + 1, last_seen=now)
            if event_id is not None:
                existing.setdefault('event_ids', []).append(event_id)
            lines[index] = (json.dumps(existing) + '\n').encode()
        else:
            if lines and not lines[-1].endswith(b'\n'):
                lines[-1] += b'\n'
            item = dict(t=now, feature=feature or 'engine', kind='workflow', status='open',
                        text=text, node=node, cause=cause,
                        backlog_key=key, count=1, last_seen=now)
            if event_id is not None:
                item['event_ids'] = [event_id]
            item['from'] = 'dispatcher-fixback'
            lines.append((json.dumps(item) + '\n').encode())
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(b''.join(lines))
        temporary.replace(path)


def route(data, features, table, sender, clock, dry_run=False):
    # Called under the dispatcher's global lock. Reserve before transport; uncertain sends never retry.
    from companion.feature_team.dispatch import allowed, budget_pauses, read, save

    path = data / 'team/dispatcher-fixbacks.json'
    journal = read(path, {})
    attempts = 0
    channels = set()
    for key, (name, row) in events(data, features).items():
        old = journal.get(key, {})
        if old.get('status') == 'done':
            continue
        feature = next((f for f in features if f['name'] == name), None)
        failures = sum(r.get('refusal', False) and r.get('feature') == name
                       and r.get('node') == row.get('node') for k, r in journal.items() if k != key)
        target, reason = destination(feature, row, failures)
        if old.get('status') == 'sending':
            target, reason = 'board', 'uncertain delivery; reconcile before retry'
        local = target in {'board', 'permission', 'learning'}
        if not local:
            modes = {}
            pauses = budget_pauses(data / 'team/model-budget.json', clock(), modes)
            member = next((w for w in feature['team'] if w['channel'] == target), {})
            entry = next((w for w in table if w['channel'] == target), member)
            family = entry.get('family', target.partition(':')[0])
            provider = family.split('-', 1)[0]
            if (target in pauses or family in pauses or provider in pauses  # target: the inspector's channel pause
                    or modes.get(provider, modes.get(family)) == 'eco'):
                continue
            if not allowed(target) or target.startswith('claude:'):
                target, reason, local = 'board', 'unsupported author channel', True
            elif attempts >= 3:
                continue
        if dry_run:
            continue
        from companion.feature_team import jev_hooks
        jev_hooks.recovery(data, name, row, key, target, failures)
        record = dict(feature=name, node=row.get('node'), refusal=refused(row),
                      reason=row.get('reason'), status='sending')
        if refused(row):
            note_backlog(data / 'team/learning-backlog.jsonl', name, row, now=clock(), event_id=key)
        elif row.get('kind') == 'request' and any(
                r.get('node') == row.get('node') and r.get('feature') == name
                and r.get('reason') == row.get('reason') for k, r in journal.items() if k != key):
            note_backlog(data / 'team/learning-backlog.jsonl', name, {
                **row, 'text': 'Repeated request: review request-and-fix skill'}, now=clock(), event_id=key)
        if not local:
            journal[key] = record
            save(path, journal)
            attempts += 1
            channels.add(target)
            try:
                reply = sender(dict(feature=name, node=row['node'], channel=target, message=message(row)))
            except Exception:
                reply = 'UNCERTAIN'
            if re.search(r'^(?:(?:codex|cursor|grok) )?STARTED(?:$|[ :])', reply, re.I | re.M):
                journal[key] = {**record, 'status': 'done'}
                save(path, journal)
                continue
            target, reason = 'board', 'NOT STARTED' if 'NOT STARTED' in reply.upper() else 'uncertain delivery'
        output = dict(t=clock(), **{'from': 'dispatcher-fixback'}, kind='routed',
                      feature=name, node=row.get('node'), text=f'{reason}\n{message(row)}')
        if target == 'learning':
            note_backlog(data / 'team/learning-backlog.jsonl', name,
                         {**row, 'text': output['text']}, now=output['t'], event_id=key)
        else:
            filename = 'owner-list.jsonl' if target == 'permission' else 'board-inbox.jsonl'
            append_once(data / 'team' / filename, key, output)
        journal[key] = {**record, 'status': 'done', 'reason': row.get('reason')}
        save(path, journal)
    return attempts, channels
