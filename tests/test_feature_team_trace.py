import json
import subprocess

from companion.feature_team.trace import build_trace


def test_trace_orders_rows_and_writes_once(tmp_path):
    folder = tmp_path / 'team/features/demo'
    folder.mkdir(parents=True)
    (folder / 'metrics.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'t': 3, 'node': 'E1', 'event': 'gate'},
        {'t': 2, 'node': 'E1', 'event': 'fix_back'},
        {'t': 0, 'node': 'E10', 'event': 'verify'},
    ]))
    (folder / 'dispatch.jsonl').write_text(json.dumps({'t': 1, 'node': 'E1'}))
    (tmp_path / 'team/board-inbox.jsonl').write_text(json.dumps(
        {'t': 4, 'text': 'E1 needs a fix'}))
    path, rows, report = build_trace('demo', 'E1', tmp_path, tmp_path, now=10)
    assert [r['t'] for r in rows] == [1, 2, 3, 4]
    assert 'no rule' in report
    assert path.read_text() == report
    again, _, _ = build_trace('demo', 'E1', tmp_path, tmp_path, now=10)
    assert again == path
    assert len(list(path.parent.iterdir())) == 1


def test_missing_rows_git_failure_and_rule(tmp_path):
    folder = tmp_path / 'team/features/demo'
    folder.mkdir(parents=True)
    (folder / 'completion.json').write_text(json.dumps({'nodes': {
        'E1': {'status': 'failed', 'sha': 'abc1234', 't': 1}}}))
    dag = tmp_path / 'private_docs/board/feature-teams/demo.dag.json'
    dag.parent.mkdir(parents=True)
    dag.write_text(json.dumps({'nodes': [], 'failure_handling': {'failed': 'retry'}}))
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('git', 10)
    _, rows, report = build_trace('demo', 'E1', tmp_path, tmp_path, runner=fail, now=10)
    assert 'rule: failed' in report
    assert 'git unavailable' in report
    assert len(rows) == 1
