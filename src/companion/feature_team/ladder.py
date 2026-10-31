"""Escalation ladder with timeboxes, stored on team case files."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from companion.feature_team import diagnose

_LADDER_HEADING = '## Ladder'
_SELF_SECONDS = 15 * 60
_PEER_SECONDS = 20 * 60
_OWNER_REASONS = frozenset({'permission', 'product'})

_BASE_RUNGS = (
    {'id': 'self', 'attempt': 1, 'seconds': _SELF_SECONDS},
    {'id': 'self', 'attempt': 2, 'seconds': _SELF_SECONDS},
    {'id': 'input_author', 'seconds': None},
    {'id': 'peer', 'seconds': _PEER_SECONDS, 'readonly': True},
    {'id': 'board', 'seconds': None},
)


def _rungs(reason: str):
    rows = list(_BASE_RUNGS)
    if reason in _OWNER_REASONS:
        rows.append({'id': 'owner', 'seconds': None})
    return tuple(rows)


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=UTC).replace(microsecond=0).isoformat()


def _parse_iso(text: str) -> float:
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def parse_case_id(stem: str) -> tuple[str | None, str]:
    if '-' not in stem:
        return None, stem
    feature, node = stem.split('-', 1)
    return feature, node


def parse_ladder(body: str) -> dict | None:
    if _LADDER_HEADING not in body:
        return None
    _, tail = body.split(_LADDER_HEADING, 1)
    block = tail.split('\n## ', 1)[0]
    state = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith('- '):
            continue
        key, _, value = line[2:].partition(':')
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key == 'attempt':
            state[key] = int(value)
        elif key == 'deadline':
            state[key] = value
        elif key == 'skipped':
            state.setdefault('skipped', []).append(value)
        elif key == 'delivery':
            state[key] = json.loads(value)
        elif key in {'rung', 'reason', 'routed'}:
            state[key] = value if key != 'routed' else value.lower() == 'true'
    if 'rung' not in state:
        return None
    state.setdefault('attempt', 1)
    state.setdefault('routed', False)
    state.setdefault('reason', '')
    return state


def format_ladder(state: dict) -> str:
    lines = [_LADDER_HEADING, '']
    lines.append(f"- rung: {state['rung']}")
    lines.append(f"- attempt: {state.get('attempt', 1)}")
    if state.get('reason'):
        lines.append(f"- reason: {state['reason']}")
    deadline = state.get('deadline')
    if deadline:
        lines.append(f"- deadline: {deadline}")
    lines.append(f"- routed: {'true' if state.get('routed') else 'false'}")
    if state.get('delivery'):
        lines.append(f"- delivery: {json.dumps(state['delivery'])}")
    for item in state.get('skipped') or []:
        lines.append(f'- skipped: {item}')
    return '\n'.join(lines) + '\n'


def _write_case(path: Path, body: str, *, now: float) -> None:
    ident = path.stem
    temporary = path.with_name(f'.{ident}-{int(now)}.md.tmp')
    temporary.write_text(body, encoding='utf-8')
    temporary.replace(path)


def upsert_ladder(path: Path, state: dict, *, now: float) -> None:
    body = path.read_text(encoding='utf-8')
    if _LADDER_HEADING in body:
        head, _, rest = body.partition(_LADDER_HEADING)
        _, _, tail = rest.partition('\n## ')
        new_body = head.rstrip() + '\n\n' + format_ladder(state)
        if tail.strip():
            new_body += '\n## ' + tail
    else:
        new_body = body.rstrip() + '\n\n' + format_ladder(state)
    if not new_body.endswith('\n'):
        new_body += '\n'
    _write_case(path, new_body, now=now)


def _rung_index(state: dict, reason: str) -> int:
    rungs = _rungs(reason)
    for index, row in enumerate(rungs):
        if row['id'] != state['rung']:
            continue
        if row['id'] == 'self' and row.get('attempt') != state.get('attempt'):
            continue
        return index
    return -1


def _initial_state(reason: str, now: float) -> dict:
    first = _rungs(reason)[0]
    return {
        'rung': first['id'],
        'attempt': first.get('attempt', 1),
        'reason': reason,
        'deadline': _iso(now + first['seconds']),
        'routed': False,
    }


def _advance_state(state: dict, now: float) -> dict:
    reason = state.get('reason', '')
    rungs = _rungs(reason)
    index = _rung_index(state, reason)
    if index < 0 or index + 1 >= len(rungs):
        return state
    nxt = rungs[index + 1]
    out = {
        'rung': nxt['id'],
        'attempt': nxt.get('attempt', 1),
        'reason': reason,
        'routed': False,
    }
    seconds = nxt.get('seconds')
    if seconds is not None:
        out['deadline'] = _iso(now + seconds)
    skipped = state.get('skipped')
    if skipped:
        out['skipped'] = list(skipped)
    return out


def _pause_reason(channel: str, pauses: dict, modes: dict, table: list[dict]) -> str:
    family = _family(channel, table)
    provider = family.split('-', 1)[0]
    key = channel if channel in pauses else provider if provider in pauses else family  # a channel-level pause too
    if key in pauses:
        return pauses[key]
    if modes.get(provider, modes.get(family)) == 'eco':
        return 'eco mode reserved for review or Board work'
    return 'lane unavailable'


def _rung_skip_label(state: dict) -> str:
    if state['rung'] == 'self':
        return f"{state['rung']}/{state.get('attempt', 1)}"
    return state['rung']


def _climb_after_expiry(state: dict, now: float, bundle, node: str, table: list[dict], case_file: Path,
                        data: Path, pauses: dict, modes: dict) -> tuple[dict, int]:
    """Advance from an expired timebox, skipping rungs whose assignee lane is paused."""
    if not _due(state, now):
        return state, 0
    skipped = list(state.get('skipped') or [])
    advances = 0
    current = state
    target, _, _ = _route_target(current, bundle, node, table, case_file, data)
    if target not in {'board', 'owner'} and not _may_send(target, pauses, modes, table):
        skipped.append(f"{_rung_skip_label(current)}: {_pause_reason(target, pauses, modes, table)}")
    while True:
        nxt = _advance_state(current, now)
        if nxt == current:
            break
        current = nxt
        advances += 1
        target, _, _ = _route_target(current, bundle, node, table, case_file, data)
        if target in {'board', 'owner'}:
            break
        if _may_send(target, pauses, modes, table):
            break
        skipped.append(f"{_rung_skip_label(current)}: {_pause_reason(target, pauses, modes, table)}")
    if skipped:
        current = {**current, 'skipped': skipped}
    return current, advances


def _feature_bundle(features, feature: str | None):
    if not feature:
        return None
    return next((f for f in features if f['name'] == feature), None)


def _resolved(bundle, node: str) -> bool:
    if not bundle:
        return False
    return bundle['state'].get(node, {}).get('status') == 'done'


def _assignee_channel(bundle, node: str) -> str | None:
    by = bundle['state'].get(node, {}).get('by')
    if not isinstance(by, str):
        return None
    for worker in bundle['team']:
        if worker['name'] == by:
            return worker['channel']
    return by if isinstance(by, str) and ':' in by else None


def _family(channel: str, table: list[dict]) -> str:
    entry = next((row for row in table if row.get('channel') == channel), None)
    if entry and entry.get('family'):
        return str(entry['family'])
    return channel.partition(':')[0]


def _input_author_channel(bundle, node: str) -> str | None:
    dag = bundle['dag']
    spec = dag.node(node)
    inputs = spec.get('inputs') or []
    if len(inputs) != 1:
        return None
    dep = inputs[0]
    by = bundle['state'].get(dep, {}).get('by')
    if not isinstance(by, str):
        return None
    worker = next((w for w in bundle['team'] if w['name'] == by), None)
    return worker['channel'] if worker else None


def _peer_channel(bundle, node: str, table: list[dict]) -> str | None:
    owner = _assignee_channel(bundle, node)
    if not owner:
        return None
    owner_family = _family(owner, table)
    for worker in bundle['team']:
        if worker.get('role', '').lower() != 'engineer':
            continue
        channel = worker['channel']
        if channel == owner:
            continue
        if _family(channel, table) == owner_family:
            continue
        return channel
    return None


def _case_reason(data: Path, feature: str | None, node: str) -> str:
    inbox = data / 'team/board-inbox.jsonl'
    if not inbox.is_file():
        return ''
    reason = ''
    for line in inbox.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('kind') != 'request':
            continue
        if row.get('node') != node:
            continue
        if feature and row.get('feature') not in (None, feature):
            continue
        if isinstance(row.get('reason'), str):
            reason = row['reason']
    return reason


def _route_target(state: dict, bundle, node: str, table: list[dict], case_file: Path, data: Path):
    if bundle and case_file.is_file() and diagnose.board_demands_diagnose(data, bundle['name'], node):
        channel = _peer_channel(bundle, node, table)
        if not channel:
            return 'board', 'diagnose engineer unavailable', None
        return channel, '', diagnose.consult_message(case_file, node, diagnose.diagnose_id(node))
    rung = state['rung']
    if rung == 'self':
        channel = _assignee_channel(bundle, node)
        if not channel:
            return 'board', 'assignee channel missing', None
        return channel, '', None
    if rung == 'input_author':
        channel = _input_author_channel(bundle, node)
        if not channel:
            return 'board', 'input author missing or ambiguous', None
        return channel, '', None
    if rung == 'peer':
        channel = _peer_channel(bundle, node, table)
        if not channel:
            return 'board', 'peer engineer unavailable', None
        body = case_file.read_text(encoding='utf-8')
        reason = state.get('reason', '')
        if diagnose.should_investigate(state, body, data, bundle['name'], node, rungs=_rungs(reason)):
            diag = diagnose.diagnose_id(node)
            return channel, '', diagnose.consult_message(case_file, node, diag)
        rel = case_file.relative_to(data)
        message = (f'Read-only consult: review case file {rel} for node {node}. '
                   'Do not edit the repository; reply with advice only.')
        return channel, '', message
    if rung == 'board':
        return 'board', '', None
    if rung == 'owner':
        return 'owner', '', None
    return 'board', f'unknown rung {rung}', None


def _may_send(channel: str, pauses: dict, modes: dict, table: list[dict]) -> bool:
    if channel in {'board', 'owner'}:
        return True
    from companion.feature_team.dispatch import allowed

    if not allowed(channel):
        return False
    family = _family(channel, table)
    provider = family.split('-', 1)[0]
    key = channel if channel in pauses else provider if provider in pauses else family  # a channel-level pause too
    if key in pauses:
        return False
    if modes.get(provider, modes.get(family)) == 'eco':
        return False
    return True


def _append_row(path: Path, row: dict) -> None:
    if path.name == 'board-inbox.jsonl':
        from companion import board_inbox
        board_inbox.append(row, path=path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row) + '\n')


def _prepare_diagnose(state: dict, bundle, node: str, case_file: Path, data: Path, clock, dry_run: bool) -> None:
    if not bundle or not case_file.is_file():
        return
    body = case_file.read_text(encoding='utf-8')
    reason = state.get('reason', '')
    if not diagnose.should_investigate(state, body, data, bundle['name'], node, rungs=_rungs(reason)):
        return
    if dry_run:
        return
    diagnose.ensure_pending(case_file, data, bundle['name'], node, now=clock())


def _dispatch(state: dict, bundle, node: str, table: list[dict], case_file: Path, data: Path,
              sender, clock, pauses, modes, dry_run: bool) -> bool:
    if _delivery_held(state):
        return False
    _prepare_diagnose(state, bundle, node, case_file, data, clock, dry_run)
    target, note, message = _route_target(state, bundle, node, table, case_file, data)
    if target in {'board', 'owner'}:
        if dry_run:
            return True
        stamp = datetime.fromtimestamp(clock(), UTC).isoformat()
        row = {'t': stamp, 'from': 'dispatcher-ladder', 'kind': 'routed',
               'feature': bundle['name'], 'node': node, 'text': note or f"ladder rung {state['rung']}"}
        filename = 'owner-list.jsonl' if target == 'owner' else 'board-inbox.jsonl'
        _append_row(data / 'team' / filename, row)
        return True
    if not _may_send(target, pauses, modes, table):
        return False
    if dry_run:
        return True
    payload = dict(feature=bundle['name'], node=node, channel=target)
    if message:
        payload['message'] = message
    # Reserve before calling the courier: even a process exit after delivery must
    # leave a durable hold, including when this rung's deadline later expires.
    delivery = {'status': 'pending', 'channel': target}
    state['delivery'] = delivery
    upsert_ladder(case_file, state, now=clock())
    try:
        reply = str(sender(payload))
    except Exception:
        reply = 'UNCERTAIN'
    started = re.search(r'^(?:(?:codex|cursor|grok) )?STARTED(?:$|[ :])[^\n]*', reply, re.I | re.M)
    ledger = re.search(r'\bledger_id=([\w-]+)', reply)
    delivery['receipt'] = started.group(0) if started else reply.strip()
    if ledger:
        delivery['ledger_id'] = ledger.group(1)
    if started:
        delivery['status'] = 'started'
        state['routed'] = True
    elif 'NOT STARTED' in reply.upper() and 'QUEUED' not in reply.upper():
        state.pop('delivery')
    else:
        delivery['status'] = 'uncertain'
    upsert_ladder(case_file, state, now=clock())
    return bool(started)


def _delivery_held(state: dict) -> bool:
    return state.get('delivery', {}).get('status') in {'pending', 'uncertain'}


def _due(state: dict, now: float) -> bool:
    deadline = state.get('deadline')
    if not deadline:
        if state['rung'] == 'input_author' and state.get('routed'):
            return True
        if (state['rung'] == 'board' and state.get('routed')
                and state.get('reason') in _OWNER_REASONS):
            return True
        return False
    return _parse_iso(deadline) <= now


def tick(data, features, table, sender, clock=time.time, dry_run=False):
    """Advance open cases; return counts for the dispatcher tick."""
    from companion.feature_team.dispatch import budget_pauses

    data = Path(data)
    cases_dir = data / 'team' / 'cases'
    if not cases_dir.is_dir():
        return {'advanced': 0, 'routed': 0, 'open': 0}
    modes: dict = {}
    pauses = budget_pauses(data / 'team/model-budget.json', clock(), modes)
    advanced = routed = open_cases = 0
    for path in sorted(cases_dir.glob('*.md')):
        if path.name.startswith('.'):
            continue
        feature, node = parse_case_id(path.stem)
        bundle = _feature_bundle(features, feature)
        if bundle and _resolved(bundle, node):
            continue
        open_cases += 1
        body = path.read_text(encoding='utf-8')
        state = parse_ladder(body)
        reason = _case_reason(data, feature, node)
        if state is None:
            state = _initial_state(reason, clock())
            if not dry_run:
                upsert_ladder(path, state, now=clock())
        elif reason and not state.get('reason'):
            state['reason'] = reason
        if _delivery_held(state):
            continue
        if _due(state, clock()):
            new_state, steps = _climb_after_expiry(state, clock(), bundle, node, table, path, data,
                                                  pauses, modes)
            if steps == 0:
                continue
            advanced += steps
            if dry_run:
                continue
            upsert_ladder(path, new_state, now=clock())
            if bundle and _dispatch(new_state, bundle, node, table, path, data, sender, clock,
                                    pauses, modes, dry_run):
                new_state['routed'] = True
                routed += 1
                upsert_ladder(path, new_state, now=clock())
            continue
        if not state.get('routed'):
            if bundle and _dispatch(state, bundle, node, table, path, data, sender, clock,
                                    pauses, modes, dry_run):
                state['routed'] = True
                routed += 1
                if not dry_run:
                    upsert_ladder(path, state, now=clock())
    return {'advanced': advanced, 'routed': routed, 'open': open_cases}
