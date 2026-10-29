"""Fresh activity is distinct from delivery completion and process presence."""
import json

from companion.app_tasks import AppTask
from companion.workflow_activity import observe_activity


def task(tmp_path, app, content, uid='chat'):
    path = tmp_path / (app + '.jsonl')
    path.write_text(content)
    return AppTask(app, uid, 'Task', str(tmp_path), None, 100, str(path))


def event(kind):
    return json.dumps({'type': 'event_msg', 'payload': {'type': kind}}) + '\n'


def observe(tmp_path, t, now=105):
    return observe_activity([{'app': t.app, 'id': t.id}], str(tmp_path), now=now,
                            discover=lambda: [t], stale_after=60)[0]


def test_codex_completion_stops_activity_without_a_result_file(tmp_path):
    t = task(tmp_path, 'codex', event('task_started') + event('task_complete'))
    assert observe(tmp_path, t)['state'] == 'completed'


def test_codex_new_turn_after_completion_is_active(tmp_path):
    t = task(tmp_path, 'codex', event('task_complete') + event('task_started'))
    assert observe(tmp_path, t)['state'] == 'working'


def test_stale_active_is_unknown_not_idle(tmp_path):
    t = task(tmp_path, 'codex', event('task_started'))
    assert observe(tmp_path, t, now=1000)['state'] == 'unknown'


def test_claude_permission_wait_is_not_busy(tmp_path):
    t = task(tmp_path, 'claude', json.dumps({'status': 'waiting_for_permission'}))
    assert observe(tmp_path, t)['state'] == 'needs_input'


def test_missing_wrong_workspace_and_unsupported_are_unknown(tmp_path):
    t = task(tmp_path, 'codex', event('task_complete'))
    for chats, repo, tasks in [([{'app':'codex','id':'other'}], str(tmp_path), [t]),
                               ([{'app':'codex','id':'chat'}], str(tmp_path/'other'), [t]),
                               ([{'app':'cursor','id':'chat'}], str(tmp_path), [])]:
        rows = observe_activity(chats, repo, now=105, discover=lambda tasks=tasks: tasks)
        assert rows[0]['state'] == 'unknown'


def test_codex_awaiting_user_input_then_response(tmp_path):
    call = json.dumps({'type':'response_item','payload': {'type':'function_call',
                       'name':'request_user_input','call_id':'q1'}}) + '\n'
    t = task(tmp_path, 'codex', event('task_started') + call)
    assert observe(tmp_path, t)['state'] == 'needs_input'
    with open(t.source, 'a') as stream:
        stream.write(json.dumps({'type':'response_item','payload': {
            'type':'function_call_output','call_id':'q1','output':'redacted'}})+'\n')
    assert observe(tmp_path, t)['state'] == 'working'


def test_explicit_chat_workspace_can_differ_from_execution_worktree(tmp_path):
    t = task(tmp_path, 'codex', event('task_complete'))
    rows = observe_activity([{'app':'codex','id':'chat','cwd':str(tmp_path)}],
                            str(tmp_path/'worktree'), now=105, discover=lambda:[t])
    assert rows[0]['state'] == 'completed'


def test_current_desktop_originator_is_discovered_but_cli_is_not(tmp_path):
    from companion.app_tasks import discover_codex
    p = tmp_path / 'rollout-current.jsonl'
    meta = {'type':'session_meta','payload': {'originator':'codex_work_desktop',
             'source':'vscode','id':'current','cwd':str(tmp_path)}}
    p.write_text(json.dumps(meta)+'\n')
    assert [t.id for t in discover_codex(tmp_path)] == ['current']
    meta['payload']['source'] = 'exec'
    p.write_text(json.dumps(meta)+'\n')
    assert discover_codex(tmp_path) == []


def test_long_codex_turn_stays_active_when_start_is_outside_bounded_tail(tmp_path):
    # A real long desktop turn had only recent tool/reasoning records in its last MiB.
    from pathlib import Path
    recent = json.dumps({'type': 'response_item', 'payload': {
        'type': 'custom_tool_call', 'name': 'functions.exec', 'call_id': 'c1'}}) + '\n'
    padding = json.dumps({'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user', 'content': 'x' * 1_048_576}}) + '\n'
    t = task(tmp_path, 'codex', event('task_started') + padding + recent)
    assert observe(tmp_path, t)['state'] == 'working'
    assert observe(tmp_path, t, now=1000)['state'] == 'unknown'
    for terminal in ('task_complete', 'turn_aborted'):
        Path(t.source).write_text(event(terminal) + recent)
        assert observe(tmp_path, t)['state'] in {'completed', 'blocked'}
