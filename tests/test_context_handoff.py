"""Self-clear for the long-lived chats (loop-building C0): warn, fixed-shape handoff, guarded clear, resume."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import context_handoff as ch

from companion.feature_team.events import EventStore
from companion.feature_team.runs import Runs

NOW = 1_800_000_000.0
DRAFT = {
    'goal': ['Land the synthetic widget feature'],
    'running': [{'what': 'widget build on Codex', 'path': '/work/widget', 'run_id': 'widget-W1-1',
                 'log': '/work/logs/widget.log', 'pid': '4242', 'done_marker': 'line "landed " in the log, exit 0',
                 'on_refusal': 'route FAILED lines to the owner, then resume'}],
    'done': [{'what': 'contract tests pass', 'proof': '/work/logs/widget-tests.log'}],
    'decisions': ['Widgets stay local'],
    'open_questions': [{'question': 'Ship the blue variant too?', 'guess': 'no, later'}],
    'traps': [{'trap': 'The gate lock is shared', 'check': "pgrep -f '[g]ate_lock'"}],
    'next_steps': [{'step': 'Verify widget-W1-1 when its result lands', 'command': 'python verify.py widget-W1-1',
                    'path': '/work/results/widget-W1-1.json'}],
    'logs': ['/work/data/team/loop/events.jsonl'],
}


def ctx(percent):
    """The fake context reader result."""
    return {'percent': percent, 'tokens': int(percent * 10_000), 'window': 1_000_000, 'source': 'usage'}


def context_events(data):
    store = EventStore(data)
    return [row for row in store._read() if row['kind'] == 'context']


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(ch, 'MAIN', tmp_path)  # not a git repo: no git facts unless a test makes one
    return tmp_path / 'data'


def write(data, draft=DRAFT, now=NOW, **kwargs):
    return ch.write_handoff(data, 'board', 's1', draft, ctx(61), now=now, repo=data.parent, **kwargs)


def test_warn_fires_once_at_the_warn_threshold(data):
    assert ch.check(data, 'board', 's1', ctx(49.9), now=NOW) == []
    first = ch.check(data, 'board', 's1', ctx(50.0), now=NOW + 1)
    assert len(first) == 1 and 'past the 50% warning' in first[0]
    assert ch.check(data, 'board', 's1', ctx(52.0), now=NOW + 2) == []
    order = ch.check(data, 'board', 's1', ctx(60.5), now=NOW + 3)
    assert len(order) == 1 and 'write the handoff now' in order[0] and '/clear' in order[0]
    statuses = [json.loads(row['detail'])['status'] for row in context_events(data)]
    assert statuses == ['ok', 'warn', 'handoff']  # first peak, then each level change
    assert all(row['worker'] == 'board' and row['run_id'] is None for row in context_events(data))


def test_handoff_validates_and_refuses_a_missing_section(data):
    path, line = write(data)
    assert path.parent == data / 'team/loop/handoffs' and path.name == 'board-20270115T080000Z.md'
    assert line.startswith(f'valid: {path} (8 sections, 1 running, 1 open questions')
    text = path.read_text()
    assert ch.parse(text)['running'] == DRAFT['running']
    for heading in ('## Traps', '## Logs'):
        broken = path.with_name('broken.md')
        broken.write_text(text.replace(heading, '## Unused').split('## Unused')[0])
        with pytest.raises(ValueError, match='missing section'):
            ch.validate_file(broken)
    for change, message in [
            ({'next_steps': []}, 'next_steps: must not be empty'),
            ({'open_questions': [{'question': 'Why?'}]}, 'guess: missing'),
            ({'mood': ['tired']}, 'unknown section'),
            ({'next_steps': [{'step': 'start train35'}]}, 'needs one of command, path, run_id'),
            ({'running': [{**DRAFT['running'][0], 'done_marker': ''}]}, 'done_marker: missing'),
            ({'traps': [{'trap': 'quiet Mac'}]}, 'check: missing'),
            ({'traps': ['quiet Mac']}, 'must be an object'),
            ({'done': [{'what': 'x', 'proof': 'logs/x.log'}]}, 'proof: must be an absolute path'),
            ({'logs': ['data/team/board-inbox.jsonl']}, r'logs\[1\]: must be an absolute path')]:
        with pytest.raises(ValueError, match=message):
            write(data, {**DRAFT, **change})


def test_handoff_writes_one_context_event(data):
    path, _ = ch.write_handoff(data, 'board', 's1', DRAFT, ctx(61.24), now=NOW, repo=data.parent)
    [event] = context_events(data)
    detail = json.loads(event['detail'])
    assert event['path'] == str(path) and detail['status'] == 'handoff' and detail['percent'] == 61.2


def test_lane_handoff_has_a_fixed_name_and_clear_still_uses_the_newest(data):
    """A lane self-clears after each package; its next package starts with "read HANDOFF-<lane>.md first"."""
    first, _ = ch.write_handoff(data, 'lane-0', 's1', DRAFT, ctx(20), now=NOW, repo=data.parent)
    second, _ = ch.write_handoff(data, 'lane-0', 's1', DRAFT, ctx(30), now=NOW + 60, repo=data.parent)
    fixed = first.parent / 'HANDOFF-lane-0.md'
    assert fixed.read_text() == second.read_text() != first.read_text()
    assert ch.newest_handoff(data, 'lane-0') == second
    assert ch.clear_step(data, 'lane-0', 's1', now=NOW + 120) == second
    assert not list(first.parent.glob('*.tmp'))


def test_clear_refuses_without_a_validated_handoff(data):
    with pytest.raises(ValueError, match='no handoff'):
        ch.clear_step(data, 'board', 's1', now=NOW)
    path, _ = write(data)
    with pytest.raises(ValueError, match='not s2'):
        ch.clear_step(data, 'board', 's2', now=NOW)
    with pytest.raises(ValueError, match='older than'):
        ch.clear_step(data, 'board', 's1', now=NOW + ch.FRESH_SECONDS + 1)
    path.write_text(path.read_text().replace('## Next steps', '## Logs'))  # corrupt: duplicate, no next steps
    with pytest.raises(ValueError):
        ch.clear_step(data, 'board', 's1', now=NOW)
    header = {'role': 'board', 'session': 's1', 'time': '2027-01-15T08:00:00Z', 'checkout': '/work',
              'context_percent': 61.0, 'context_source': 'usage'}
    path.write_text(ch.render(ch.parse(ch.render({**DRAFT, **header, 'logs': ['/work', *DRAFT['logs']]}))))
    assert ch.clear_step(data, 'board', 's1', now=NOW + 60) == path
    assert json.loads(context_events(data)[-1]['detail'])['status'] == 'clear'


def test_resume_reconstructs_running_work_including_live_runs(data):
    store = Runs(data / 'team/loop', emit=lambda row: None, clock=lambda: NOW)
    brief = {'run_id': 'widget-W2-1', 'feature': 'widget', 'node': 'W2', 'title': 'second widget',
             'channel': 'codex:lane-0', 'model': 'm', 'effort': 'low', 'base': 'abc1234', 'worktree': '/tmp/wt/w2',
             'files': ['a.py'], 'inputs': [], 'acceptance': ['pytest'], 'deny': [], 'deadline': NOW + 3600,
             'result': 'results/w2.json', 'rules': 'AGENTS.md'}
    store.create(brief)
    store.transition('widget-W2-1', 'dispatched')
    write(data)
    ch.register(data, 'board', 's1')
    lines = ch.hook(data, {'hook_event_name': 'SessionStart', 'source': 'clear', 'session_id': 's2'}, now=NOW + 5)
    assert lines[0] == ch.resume_line(ch.newest_handoff(data, 'board'))
    resumed = ch.validate(ch.parse(lines[2]))
    assert resumed['running'] == DRAFT['running'] + [
        {'what': 'widget:W2 (dispatched)', 'run_id': 'widget-W2-1', 'worktree': '/tmp/wt/w2',
         'done_marker': f"state verify_pass or landed in {data / 'team/loop/runs/widget-W2-1.json'}",
         'on_refusal': 'verify_fail: route the fixback to the owning worker; timeout: one re-dispatch'}]
    assert resumed['next_steps'] == DRAFT['next_steps']
    registry = json.loads((data / 'team/loop/long-lived.json').read_text())
    assert registry == {'s1': 'board', 's2': 'board'}
    # The pending handoff is claimed once; a later clear with nothing pending injects nothing.
    assert ch.hook(data, {'hook_event_name': 'SessionStart', 'source': 'clear', 'session_id': 's3'}, now=NOW) == []


def test_hook_checks_only_registered_sessions(data, tmp_path):
    transcript = tmp_path / 't.jsonl'
    usage = {'input_tokens': 10, 'cache_read_input_tokens': 549_990, 'cache_creation_input_tokens': 0}
    transcript.write_text('\n'.join([
        json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'usage': {'input_tokens': 900_000}}}),
        json.dumps({'type': 'assistant', 'message': {'usage': usage}}),
        json.dumps({'type': 'user', 'message': {'content': 'hi'}})]) + '\n')
    event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'worker', 'transcript_path': str(transcript)}
    assert ch.hook(data, event, now=NOW) == []
    ch.register(data, 'home', 'worker')
    [line] = ch.hook(data, event, now=NOW)
    assert line.startswith('context 55% (transcript): past the 50% warning')


def test_read_context_sources():
    usage = {'context': {'status': 'ok', 'tokensUsed': 223_659, 'contextWindow': 1_000_000, 'percentUsed': 22}}
    assert ch.read_context(usage=usage) == {'percent': 22.0, 'tokens': 223_659, 'window': 1_000_000,
                                            'source': 'usage'}
    with pytest.raises(ValueError, match='no context source'):
        ch.read_context(usage={'context': {'status': 'unavailable'}})


def test_read_context_falls_back_to_size(tmp_path):
    transcript = tmp_path / 't.jsonl'
    transcript.write_bytes(b'x' * 70_000)
    assert ch.read_context(transcript=transcript, window=100_000) == {
        'percent': 10.0, 'tokens': 10_000, 'window': 100_000, 'source': 'size'}


def test_cli_handoff_validate_and_hook_never_blocks(data, tmp_path):
    script = Path(ch.__file__)
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps(DRAFT))
    usage = tmp_path / 'usage.json'
    usage.write_text(json.dumps({'context': {'status': 'ok', 'percentUsed': 61, 'contextWindow': 1_000_000}}))
    run = subprocess.run([sys.executable, str(script), '--data', str(data), 'handoff', '--role', 'board',
                          '--session', 's1', '--draft', str(draft), '--usage-json', str(usage),
                          '--repo', str(tmp_path)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    path, valid, resume = run.stdout.splitlines()[:3]
    assert valid.startswith(f'valid: {path}') and resume == ch.resume_line(path)
    run = subprocess.run([sys.executable, str(script), '--data', str(data), 'clear', '--role', 'board',
                          '--session', 'other'], capture_output=True, text=True)
    assert run.returncode == 2 and 'refused' in run.stderr
    bad = {'hook_event_name': 'UserPromptSubmit', 'session_id': 's1', 'transcript_path': '/nonexistent'}
    ch.register(data, 'board', 's1')
    run = subprocess.run([sys.executable, str(script), '--data', str(data), 'hook'], input=json.dumps(bad),
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == '' and 'refused' in run.stderr


def test_hygiene_alert_is_also_a_context_event(tmp_path, monkeypatch):
    from scripts import context_hygiene as hygiene
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    root = tmp_path / 'checkout'
    (root / 'data/team').mkdir(parents=True)
    (root / 'data/team/config.json').write_text(json.dumps({'lanes': [{'participants': {'claude': {'id': 'f'}}}]}))
    transcript = home / '.claude/projects' / str(root).replace('/', '-') / 'f.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {
        'input_tokens': 50_000, 'cache_read_input_tokens': 500_000}}}) + '\n')  # 55 percent of the window
    assert len(hygiene.tick(root, now=NOW, desktop=tmp_path / 'none', logs=tmp_path / 'none')) == 1
    [event] = context_events(root / 'data')
    assert event['worker'] == 'claude-0'
    assert json.loads(event['detail']) == {'percent': 55.0, 'tokens': 550_000, 'window': 1_000_000,
                                           'status': 'warn', 'source': 'transcript'}
    assert hygiene.tick(root, dry_run=True, now=NOW + 1) == [] and len(context_events(root / 'data')) == 1


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_git_facts_fill_branch_worktree_sha_and_containment(data, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'a')
    git(repo, 'branch', 'feat-r0')
    first = git(repo, 'rev-parse', 'HEAD')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'b')
    git(repo, 'branch', 'int')
    tip = git(repo, 'rev-parse', 'HEAD')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'c')
    tree = tmp_path / 'wt-int'
    git(repo, 'worktree', 'add', '-q', str(tree), 'int')
    draft = {**DRAFT, 'done': [{'what': 'integration base', 'proof': '/work/int.log', 'sha': tip[:8]},
                               {'what': 'R0 record', 'proof': '/work/r0.log', 'sha': first[:8]}],
             'running': [{**DRAFT['running'][0], 'worktree': str(tree.resolve())}]}
    path, _ = ch.write_handoff(data, 'board', 's1', draft, ctx(61), now=NOW, repo=repo)
    doc = ch.validate_file(path)[0]
    base, r0 = doc['done']
    assert base['branch'] == 'int' and base['worktree'] == str(tree.resolve())
    assert r0['branch'] == 'feat-r0' and r0['contained_in'] == f'int {tip[:8]}'
    assert doc['running'][0]['sha'] == tip[:8] and doc['running'][0]['branch'] == 'int'
    assert doc['checkout'] == str(repo.resolve()) and doc['logs'][0] == doc['checkout']
