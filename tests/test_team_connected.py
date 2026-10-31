import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from companion.team_connected import ConnectedStore


def payload():
    return dict(room_id="a" * 32, provider="cursor", request_id="demo-1", text="Explain this code.", config_hash="pin")


def test_same_request_is_claimed_once_across_instances(tmp_path):
    a, b = ConnectedStore(tmp_path), ConnectedStore(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda s: s.claim(**payload()), (a, b)))
    assert sum(created for _, created in results) == 1
    assert results[0][0]["id"] == results[1][0]["id"]
    with pytest.raises(ValueError, match="request_changed"):
        a.claim(**{**payload(), "text": "Different work"})


def test_stop_wins_over_late_output(tmp_path):
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**payload())
    assert store.begin(row["id"])
    store.stop(row["room_id"], row["id"])
    store.finish(row["id"], state="completed", text="late", reason="", session_id="s", surface="cursor_cli_acp")
    result = store.get(row["id"])
    assert result["state"] == "uncertain"
    assert result["text"] == "late"
    assert result["stop_requested"]


def test_stale_running_is_uncertain_never_replayed(tmp_path):
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**payload())
    assert store.begin(row["id"])
    with store.connect() as db:
        db.execute("UPDATE runs SET updated_at=? WHERE id=?", (time.time() - 400, row["id"]))
    assert store.for_room(row["room_id"])[0]["state"] == "uncertain"
    _, created = store.claim(**payload())
    assert not created
    assert not store.begin(row["id"])


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_readiness_and_dispatch_use_same_live_evidence(tmp_path, monkeypatch):
    import json

    from companion import team_connected as core
    from companion.team_cursor import AdapterResult
    from tests.test_team_cursor import evidence
    monkeypatch.setattr(core, 'team_handbook', lambda: 'Fixture handbook')
    fresh = evidence()
    cfg = {'workspace': str(tmp_path), 'model': 'fixture-model'}
    seen = []

    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': True, 'evidence': fresh, 'reason': None}

        def run(self, request, *, authorize, entitlement, **kwargs):
            assert authorize(request)
            seen.append(entitlement)
            return AdapterResult('cursor', request.request_id, 'completed', json.dumps(dict(run_id=request.request_id, stage='reply_cursor', attempt=0, status='completed', summary='Done.')), 'fixture-session', 'cursor_cli_acp')

    monkeypatch.setattr(core, '_config', lambda provider: (cfg, 'pin'))
    monkeypatch.setattr(core, '_adapter', lambda provider, config: Fake())
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**payload())
    core._work(store, row)
    assert seen == [fresh]
    assert store.get(row['id'])['state'] == 'completed'
    view = core.project_room({'id': row['room_id'], 'messages': [], 'local_only': False}, tmp_path)
    assert view['messages'][-1]['author'] == 'cursor'
    assert view['messages'][-1]['text'] == 'Done.'


def test_new_config_or_stop_prevents_model_call(tmp_path, monkeypatch):
    from companion import team_connected as core
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**payload())
    monkeypatch.setattr(core, '_config', lambda provider: ({}, 'different-pin'))
    monkeypatch.setattr(core, '_adapter', lambda *args: pytest.fail('adapter must not run'))
    core._work(store, row)
    assert store.get(row['id'])['state'] == 'blocked'
    assert store.get(row['id'])['reason'] == 'configuration_changed'


def test_malformed_optional_app_config_does_not_break_local_status(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from companion import team_connected as core
    path = tmp_path / 'apps.json'
    path.write_text('[]')
    monkeypatch.setattr(core, 'get_settings', lambda: SimpleNamespace(team_apps_config=path))
    rows = core.app_status()['apps']
    assert len(rows) == 2 and all(not row['available'] for row in rows)


def test_probe_failure_does_not_break_other_apps(tmp_path, monkeypatch):
    from companion import team_connected as core
    monkeypatch.setattr(core, '_config', lambda provider: ({'model': 'auto', 'workspace': str(tmp_path)}, 'pin'))
    def failure(*args, **kwargs):
        raise RuntimeError('unavailable provider')
    monkeypatch.setattr(core, '_readiness', failure)
    assert all(not row['available'] for row in core.app_status()['apps'])


def test_status_cache_miss_never_starts_a_vendor_probe(monkeypatch):
    from companion import team_connected as core
    core._probes.clear()
    monkeypatch.setattr(core, '_adapter', lambda *a: pytest.fail('read-only cache lookup started a CLI'))
    evidence, reason = core._readiness('cursor', {'model': 'auto'}, 'fresh-pin')
    assert evidence is None and reason == 'not_checked'


def test_swept_late_result_is_preserved_but_never_completed(tmp_path):
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**payload())
    assert store.begin(row['id'])
    with store.connect() as db:
        db.execute('UPDATE runs SET updated_at=? WHERE id=?', (time.time()-400, row['id']))
    store.for_room(row['room_id'])
    store.finish(row['id'], state='completed', text='late result', reason='', session_id='late-session', surface='cursor_cli_acp')
    result = store.get(row['id'])
    assert result['state'] == 'uncertain'
    assert result['text'] == 'late result' and result['reason'] == 'late_result'


@pytest.mark.parametrize('kind', ['root', 'home', 'project', 'project_child', 'private', 'private_child', 'env'])
def test_configuration_refuses_broad_or_private_workspace(tmp_path, monkeypatch, kind):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from companion import paths
    from companion import team_connected as core
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'child').mkdir()
    ordinary = tmp_path / 'work'
    ordinary.mkdir()
    (ordinary / '.env').write_text('synthetic only')
    selected = {'root': Path('/'), 'home': Path.home(),
                'project': Path(core.__file__).resolve().parents[2],
                'project_child': Path(core.__file__).resolve().parents[2] / 'src', 'private': private,
                'private_child': private / 'child', 'env': ordinary}[kind]
    cfg = tmp_path / 'apps.json'
    cfg.write_text(json.dumps({'version': 1, 'workspace': str(selected),
                               'providers': {'cursor': {'command': ['/bin/false'], 'model': 'auto'}}}))
    monkeypatch.setattr(core, 'get_settings', lambda: SimpleNamespace(team_apps_config=cfg))
    monkeypatch.setattr(paths, 'DATA_DIR', private)
    with pytest.raises(ValueError, match='unsafe_app_workspace'):
        core._config('cursor')


@pytest.mark.parametrize('current,own_keys,reason', [('b'*64, False, 'account_mismatch'), ('a'*64, None, 'own_api_keys_unverified'), ('a'*64, True, 'own_api_keys_unverified'), ('a'*64, False, None)])
def test_saved_policy_binds_account_and_excludes_personal_key_billing(monkeypatch, current, own_keys, reason):
    from dataclasses import asdict, replace

    from companion import team_connected as core
    from tests.test_team_cursor import evidence
    policy = replace(evidence(), account='a'*64)
    cfg = {'model': 'fixture-model', 'subscription': asdict(policy), 'own_api_keys': own_keys}
    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': True, 'account': current, 'evidence': None, 'reason': 'quota_and_overage_read_unavailable'}
    monkeypatch.setattr(core, '_adapter', lambda *args: Fake())
    _, actual = core._readiness('cursor', cfg, 'bound-policy', refresh=True)
    assert actual == reason


@pytest.mark.parametrize('days,reason', [(30, None), (31, 'subscription_evidence_stale_or_mismatched')])
def test_owner_billing_confirmation_holds_thirty_days(monkeypatch, days, reason):
    from dataclasses import asdict, replace

    from companion import team_connected as core
    from tests.test_team_cursor import evidence
    now = time.time()
    policy = replace(evidence(), account='a'*64, checked_at=now - 60, expires_at=now - 60 + days * 86400)
    cfg = {'model': 'fixture-model', 'subscription': asdict(policy), 'own_api_keys': False}
    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': True, 'account': 'a'*64, 'evidence': None}
    monkeypatch.setattr(core, '_adapter', lambda *args: Fake())
    assert core._readiness('cursor', cfg, f'window-{days}', refresh=True)[1] == reason


def _apps_config(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from companion import team_connected as core
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    agy = tmp_path / 'agy'
    agy.write_text('')
    path = tmp_path / 'apps-config.json'
    path.write_text(json.dumps({'version': 1, 'workspace': str(workspace), 'providers': {
        'cursor': {'command': [str(agy)], 'model': 'composer-2.5', 'own_api_keys': False},
        'antigravity': {'command': [str(agy)], 'model': 'gemini-3.1-pro-low', 'subscription': {'kept': True}}}}))
    path.chmod(0o600)
    monkeypatch.setattr(core, 'get_settings', lambda: SimpleNamespace(team_apps_config=path))
    return path


def test_confirm_billing_binds_a_fresh_probe_and_keeps_the_other_app(tmp_path, monkeypatch):
    import json

    from companion import team_connected as core
    path = _apps_config(tmp_path, monkeypatch)
    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': True, 'account': 'c'*64, 'evidence': None}
    monkeypatch.setattr(core, '_adapter', lambda *args: Fake())

    core.confirm_billing('cursor', now=lambda: 1000.0)

    saved = json.loads(path.read_text())
    policy = saved['providers']['cursor']['subscription']
    assert (policy['account'], policy['checked_at'], policy['expires_at']) == ('c'*64, 1000.0, 1000.0 + 30 * 86400)
    assert policy['overage_disabled'] is True and policy['model'] == 'composer-2.5'
    assert saved['providers']['antigravity']['subscription'] == {'kept': True}
    assert path.stat().st_mode & 0o777 == 0o600


def test_confirm_billing_refuses_a_signed_out_app(tmp_path, monkeypatch):
    from companion import team_connected as core
    path = _apps_config(tmp_path, monkeypatch)
    before = path.read_text()
    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': False, 'account': '', 'reason': 'account_identity_missing'}
    monkeypatch.setattr(core, '_adapter', lambda *args: Fake())

    with pytest.raises(ValueError, match='authentication_required'):
        core.confirm_billing('cursor')
    assert path.read_text() == before


@pytest.mark.usefixtures("isolated_app_dispatch")
def test_team_tags_reach_the_app_as_owner_words_not_as_its_own_agent_names(tmp_path, monkeypatch):
    """2026-10-01: '@all say hi' made Antigravity start its own subagents and Cursor search the repo for '@all'."""
    from companion import team_connected as core
    from companion.team_cursor import AdapterResult
    from tests.test_team_cursor import evidence
    cfg = {'workspace': str(tmp_path), 'model': 'fixture-model'}
    prompts = []

    class Fake:
        def probe_entitlement(self, model):
            return {'authenticated': True, 'evidence': evidence(), 'reason': None}

        def run(self, request, *, authorize, entitlement, **kwargs):
            prompts.append(request.prompt)
            return AdapterResult('cursor', request.request_id, 'completed', 'Hi.', 'fixture-session', 'cursor_cli_acp')

    monkeypatch.setattr(core, '_config', lambda provider: (cfg, 'pin'))
    monkeypatch.setattr(core, '_adapter', lambda provider, config: Fake())
    store = ConnectedStore(tmp_path)
    row, _ = store.claim(**{**payload(), 'text': '@all  say hi'})
    core._work(store, row)
    assert prompts[0].split('Assigned instruction (data):\n', 1)[1].startswith('"say hi"')
    assert 'You are Cursor' in prompts[0] and 'not your own subagents' in prompts[0]
    assert store.get(row['id'])['prompt'] == '@all  say hi'  # the chat still shows exactly what the owner typed


def test_app_prompt_keeps_a_cursor_file_mention():
    from companion.team_connected import _app_prompt
    def prompt(text):
        return _app_prompt({'id': 'r1', 'provider': 'cursor', 'prompt': text})
    assert prompt('@cursor @src/foo.py fix this').split('Assigned instruction (data):\n', 1)[1].startswith('"@src/foo.py fix this"')
    assert 'work requests use your tools' in prompt('fix the failing test')


def test_app_reply_accepts_what_real_apps_return():
    """2026-10-02 live @all: Antigravity failed twice on the strict envelope. Text replies stay text-only."""
    from companion.team_connected import _app_reply
    row = {'id': 'r1', 'provider': 'antigravity'}
    env = {'run_id': 'r1', 'stage': 'reply_antigravity', 'attempt': 0, 'status': 'completed', 'summary': 'Hi, ready.'}
    assert _app_reply(json.dumps(env), row) == {'status': 'completed', 'summary': 'Hi, ready.', 'chat': None}
    assert _app_reply('Sure:\n```json\n' + json.dumps(env) + '\n```', row) == {'status': 'completed', 'summary': 'Hi, ready.', 'chat': None}
    assert _app_reply(json.dumps({**env, 'extra': 1}), row) == {'status': 'completed', 'summary': 'Hi, ready.', 'chat': None}
    assert _app_reply(json.dumps({**env, 'status': 'needs_input', 'summary': 'Which repo?'}), row) == {'status': 'needs_input', 'summary': 'Which repo?', 'chat': None}
    assert _app_reply('Hi! Antigravity here, ready.', row) == {'status': 'completed', 'summary': 'Hi! Antigravity here, ready.', 'chat': None}


def test_app_reply_refuses_another_run_or_nothing():
    from companion.team_connected import _app_reply
    row = {'id': 'r1', 'provider': 'antigravity'}
    other = {'run_id': 'r2', 'stage': 'reply_antigravity', 'attempt': 0, 'status': 'completed', 'summary': 'x'}
    assert _app_reply(json.dumps(other), row) is None
    assert _app_reply(json.dumps({'run_id': 'r1', 'stage': 'reply_cursor', 'status': 'completed', 'summary': 'x'}), row) is None
    for empty in ('', '   ', None, '{}', json.dumps({'run_id': 'r1', 'summary': ''})):
        assert _app_reply(empty, row) is None, empty
    assert _app_reply('x' * 20000, row)['summary'] == 'x' * 16000


def test_app_reply_keeps_ordinary_json_as_answer_content():
    """Codex review 2026-10-02: JSON in a normal answer is content, not a broken envelope."""
    from companion.team_connected import _app_reply
    row = {'id': 'r1', 'provider': 'cursor'}
    for text in ('{"enabled": true}', 'Example:\n```json\n{"enabled": true}\n```', 'Set {"timeout": 30} and retry.'):
        assert _app_reply(text, row) == {'status': 'completed', 'summary': text.strip(), 'chat': None}, text
    env = {'run_id': 'r1', 'stage': 'reply_cursor', 'status': 'completed', 'summary': 'Done.'}
    text = 'Config:\n```json\n{"timeout": 30}\n```\nResult:\n```json\n' + json.dumps(env) + '\n```'
    assert _app_reply(text, row) == {'status': 'completed', 'summary': 'Done.', 'chat': None}
