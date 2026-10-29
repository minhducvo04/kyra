"""Read existing node evidence into a private chronological case file."""

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from companion.feature_team.dag import live_path


def _json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _stamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0


def _mentions(row, node):
    if row.get('node') == node or node in row.get('nodes', []):
        return True
    return bool(re.search(r'(?<![\w-])' + re.escape(node) + r'(?![\w-])',
                          str(row.get('text', ''))))


def build_trace(feature, node, data, repo, *, now=None, runner=subprocess.run):
    """Return (path, ordered evidence, markdown); never mutate source rows.

    Unknown failure-handling schemas conservatively produce 'no rule'. Git
    lookup failure is evidence absence, not a reason to lose the case file.
    """
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in (feature, node)):
        raise ValueError('feature and node must be plain names')
    data, repo = Path(data), Path(repo)
    folder = data / 'team/features' / feature
    completion = _json(folder / 'completion.json', {'nodes': {}})['nodes'].get(node, {})
    dag = _json(live_path(data / 'private_docs/board/feature-teams', feature), {})
    spec = next((r for r in dag.get('nodes', []) if r.get('id') == node), {})
    rows = []
    for source in sorted(folder.glob('*.jsonl')) + [data / 'team/board-inbox.jsonl']:
        try:
            lines = source.read_text().splitlines()
        except FileNotFoundError:
            continue
        selected = []
        for index, line in enumerate(lines, 1):
            try:
                row = json.loads(line)
            except ValueError:
                continue  # A partial append must not prevent tracing earlier evidence.
            if isinstance(row, dict) and _mentions(row, node):
                selected.append({**row, '_source': f'{source.name}:{index}'})
        rows.extend(selected[-20:] if source.name == 'board-inbox.jsonl' else selected)
    if completion:
        rows.append({**completion, 'event': 'completion snapshot', '_source': 'completion.json'})
    rules = spec.get('failure_handling', dag.get('failure_handling', {}))
    kinds = {r.get(key) for r in rows for key in ('event', 'kind', 'status')
             if isinstance(r.get(key), str)}
    matched = [key for key in rules if key in kinds] if isinstance(rules, dict) else []
    rule = 'rule: ' + ', '.join(matched) if matched else 'no rule'
    git_note = 'git unavailable: no branch or commit recorded'
    ref = spec.get('branch') or completion.get('branch')
    if not ref:
        ref = next((r['branch'] for r in reversed(rows) if r.get('branch')), None)
    ref = ref or completion.get('sha')
    if isinstance(ref, str) and ref and not ref.startswith('-'):
        try:
            result = runner(['git', '-C', str(repo), 'log', '-1', '--format=%H%n%cI',
                             '--name-only', ref, '--'], capture_output=True, text=True,
                            timeout=10, check=True)
            lines = result.stdout.splitlines()
            rows.append({'t': lines[1], 'event': 'git last commit', 'sha': lines[0],
                         'files': [line for line in lines[2:] if line], '_source': 'git'})
            git_note = 'git: branch tip' if spec.get('branch') else 'git: recorded ref'
        except (OSError, subprocess.SubprocessError, IndexError):
            git_note = 'git unavailable: lookup failed'
    rows.sort(key=lambda r: _stamp(r.get('t')))
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = [f'# {feature} / {node}', '', rule, git_note, '',
             '| Time | Source | Evidence |', '| --- | --- | --- |']
    for row in rows:
        evidence = {k: v for k, v in row.items() if k not in ('t', '_source')}
        lines.append(f"| {cell(row.get('t', 'unknown'))} | {cell(row['_source'])} | "
                     f'{cell(json.dumps(evidence, ensure_ascii=False))} |')
    report = '\n'.join(lines) + '\n'
    target = folder / 'traces' / f'{node}-{int(now if now is not None else time.time())}.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open('x') as output:
            output.write(report)
    except FileExistsError:
        if target.read_text() != report:
            raise ValueError('trace timestamp already contains different evidence') from None
    return target, rows, report
