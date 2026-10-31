"""Read-only investigate nodes when a case root cause is still unknown."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from companion.feature_team.dag import live_path, load

RUNG_PEER = 4  # 1-based ladder index (self, self, input_author, peer).

_ROOT_CAUSE = '## Root cause'
_PROPOSED_FIX = '## Proposed fix'
_DIAGNOSE = '## Diagnose'
_UNKNOWN = frozenset({'', '(none recorded)', '(unknown)', 'unknown'})


def diagnose_id(case_node: str) -> str:
    return f'{case_node}-diag'


def fix_id(case_node: str) -> str:
    return f'{case_node}-fix'


def _section(body: str, heading: str) -> str:
    if heading not in body:
        return ''
    _, tail = body.split(heading, 1)
    return tail.split('\n## ', 1)[0]


def root_cause_known(body: str) -> bool:
    block = _section(body, _ROOT_CAUSE).strip()
    if not block:
        return False
    for line in block.splitlines():
        text = line.strip().lstrip('- ').strip()
        if text and text.lower() not in _UNKNOWN:
            return True
    return False


def board_demands_diagnose(data: Path, feature: str | None, node: str) -> bool:
    inbox = data / 'team/board-inbox.jsonl'
    if not inbox.is_file():
        return False
    for line in inbox.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('kind') != 'diagnose':
            continue
        if row.get('node') != node:
            continue
        if feature and row.get('feature') not in (None, feature):
            continue
        return True
    return False


def _peer_rung(state: dict, reason: str, rungs) -> bool:
    rung = state.get('rung')
    attempt = state.get('attempt', 1)
    for index, row in enumerate(rungs, start=1):
        if row['id'] != rung:
            continue
        if row['id'] == 'self' and row.get('attempt') != attempt:
            continue
        return index == RUNG_PEER
    return False


def should_investigate(state: dict, body: str, data: Path, feature: str | None, node: str,
                       *, rungs) -> bool:
    if board_demands_diagnose(data, feature, node):
        return True
    if not _peer_rung(state, state.get('reason', ''), rungs):
        return False
    return not root_cause_known(body)


def _dag_path(data: Path, feature: str) -> Path:
    return live_path(data / 'team/feature-teams', feature)


def _completion_path(data: Path, feature: str) -> Path:
    return data / 'team/features' / feature / 'completion.json'


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _write_case(path: Path, body: str, *, now: float) -> None:
    ident = path.stem
    temporary = path.with_name(f'.{ident}-{int(now)}.md.tmp')
    temporary.write_text(body, encoding='utf-8')
    temporary.replace(path)


def _upsert_heading(path: Path, heading: str, lines: list[str], *, now: float) -> None:
    body = path.read_text(encoding='utf-8')
    block = '\n'.join(lines).rstrip() + '\n'
    if heading in body:
        head, _, rest = body.partition(heading)
        _, _, tail = rest.partition('\n## ')
        new_body = head.rstrip() + '\n\n' + heading + '\n\n' + block
        if tail.strip():
            new_body += '\n## ' + tail
    else:
        new_body = body.rstrip() + '\n\n' + heading + '\n\n' + block
    if not new_body.endswith('\n'):
        new_body += '\n'
    _write_case(path, new_body, now=now)


def _append_dag_node(data: Path, feature: str, node: dict) -> None:
    path = _dag_path(data, feature)
    document = _read_json(path)
    nodes = document.setdefault('nodes', [])
    if any(item.get('id') == node['id'] for item in nodes):
        return
    nodes.append(deepcopy(node))
    payload = {'nodes': nodes}
    temporary = path.with_suffix('.validate.tmp')
    temporary.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    load(temporary)
    temporary.unlink(missing_ok=True)
    _write_json(path, payload)


def _mark_completion(data: Path, feature: str, node: str, status: str, **fields) -> None:
    path = _completion_path(data, feature)
    document = _read_json(path) if path.is_file() else {'nodes': {}}
    rows = document.setdefault('nodes', {})
    row = {**rows.get(node, {}), 'status': status, **fields}
    rows[node] = row
    _write_json(path, document)


def ensure_pending(case_file: Path, data: Path, feature: str, case_node: str, *, now: float) -> str:
    """Append a read-only diagnose node to the DAG and case file; never enqueue it."""
    data = Path(data)
    diag = diagnose_id(case_node)
    _append_dag_node(data, feature, {
        'id': diag,
        'inputs': [case_node],
        'kind': 'diagnose',
        'readonly': True,
        'intern_friendly': False,
        'tier': 'T1',
        'acceptance': 'Read the case file; reply with root cause and a proposed fix node; do not edit the repo.',
    })
    _mark_completion(data, feature, diag, 'queued')
    _upsert_heading(case_file, _DIAGNOSE, [
        f'- node: {diag}',
        '- status: pending',
        '- readonly: true',
    ], now=now)
    return diag


def consult_message(case_file: Path, case_node: str, diag_node: str) -> str:
    rel = case_file.name
    return (f'Read-only diagnose for node {case_node} (investigate node {diag_node}). '
            f'Review case file {rel}; do not edit the repository. '
            'Reply with a one-line root cause and a proposed fix node spec (id, acceptance, files). '
            'The dispatcher records the fix as pending; it is never auto-run.')


def record(data: Path, feature: str, case_node: str, case_file: Path, *,
           root_cause: str, fix: dict, now: float, dry_run: bool = False) -> dict:
    """Write root cause and a proposed fix node into the case file and DAG (queued, not dispatched)."""
    data = Path(data)
    root_cause = root_cause.strip()
    if not root_cause:
        raise ValueError('root_cause must be non-empty')
    fix_node = dict(fix)
    fix_node.setdefault('id', fix_id(case_node))
    fix_node.setdefault('inputs', [case_node])
    fix_node.setdefault('kind', 'code')
    fix_node.setdefault('intern_friendly', False)
    fix_node.setdefault('tier', 'T1')
    acceptance = fix_node.get('acceptance')
    if not isinstance(acceptance, str) or not acceptance.strip():
        raise ValueError('fix acceptance must be a non-empty string')
    if dry_run:
        return {'diagnose': diagnose_id(case_node), 'fix': fix_node['id']}
    _upsert_heading(case_file, _ROOT_CAUSE, [root_cause], now=now)
    files = fix_node.get('files') or []
    proposed = [
        f"- node: {fix_node['id']}",
        f'- acceptance: {acceptance.strip()}',
    ]
    if files:
        proposed.append('- files: ' + ', '.join(str(item) for item in files))
    _upsert_heading(case_file, _PROPOSED_FIX, proposed, now=now)
    _append_dag_node(data, feature, fix_node)
    _mark_completion(data, feature, fix_node['id'], 'queued')
    _mark_completion(data, feature, diagnose_id(case_node), 'done')
    _upsert_heading(case_file, _DIAGNOSE, [
        f'- node: {diagnose_id(case_node)}',
        '- status: done',
        f'- proposed_fix: {fix_node["id"]}',
    ], now=now)
    return {'diagnose': diagnose_id(case_node), 'fix': fix_node['id']}
