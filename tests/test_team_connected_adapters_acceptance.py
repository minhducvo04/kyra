"""Claude's independent acceptance for the Cursor and Antigravity connected adapters (2026-09-29).

Contract: a private design note. Fake stdio peers written into
pytest's tmp_path log what they actually received, so "no process started", "the prompt was never sent" and "sent
exactly once" are observed, not inferred. Nothing here logs in, accepts terms, spends, or starts a real model.

Covered at the adapter seam: no spawn without current entitlement and trusted authority (including an authority that
raises); authority revoked after the session never sends the prompt; the child sees only the workspace (cwd) and an
allowlisted environment, never provider keys, the Kyra token or data directory; the prompt never travels on argv; no
invented session identity; another session's text never reaches a result; stop mid-turn is cancelled or uncertain and
the prompt is sent once; a crash is uncertain with one spawn; provenance never claims a desktop or native thread;
requests are frozen once built. Home's integration (durable claims, dedup, 409, room projection, Stop flag, status
cache, per-task edit sets) is named for a later review in a private design note, not faked here.
"""
import dataclasses
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from companion.team_antigravity import AntigravityAdapter
from companion.team_cursor import AdapterRequest, CursorAdapter, SubscriptionEvidence

SECRETS = ('KYRA_API_TOKEN', 'KYRA_DATA_DIR', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY',
           'GOOGLE_API_KEY', 'CURSOR_API_KEY')
PROMPT = 'Fixture task: reply with one short sentence'

CURSOR_PEER = r'''
import json, os, sys, time
LOG, MODE = {log!r}, {mode!r}
def log(entry):
    with open(LOG, 'a') as handle:
        handle.write(entry + '\n')
def send(message):
    print(json.dumps(message), flush=True)
def chunk(session_id, text):
    send({{'jsonrpc': '2.0', 'method': 'session/update', 'params': {{'sessionId': session_id, 'update': {{
        'sessionUpdate': 'agent_message_chunk', 'content': {{'type': 'text', 'text': text}}}}}}}})
log('spawn')
log('env:' + json.dumps(sorted(os.environ)))
log('cwd:' + os.getcwd())
log('argv:' + json.dumps(sys.argv))
session = 'fixture-session'
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    log('method:' + str(method))
    if method == 'initialize':
        result = {{'protocolVersion': 1, 'agentCapabilities': {{'loadSession': True}}}}
    elif method == 'session/new':
        result = {{}} if MODE == 'no_session_id' else {{'sessionId': session}}
    elif method == 'session/prompt':
        if MODE == 'crash':
            sys.exit(1)
        chunk(session, 'fixture answer')
        if MODE == 'mixed':
            chunk('other-session', 'OTHER SESSION TEXT')
        if MODE in ('honor_cancel', 'ignore_cancel'):
            for extra in sys.stdin:
                cancel = json.loads(extra)
                log('method:' + str(cancel.get('method')))
                if cancel.get('method') == 'session/cancel':
                    if MODE == 'honor_cancel':
                        send({{'jsonrpc': '2.0', 'id': message['id'], 'result': {{'stopReason': 'cancelled'}}}})
                    else:
                        time.sleep(5)
                    break
            continue
        result = {{'stopReason': 'end_turn'}}
    else:
        result = {{}}
    if 'id' in message:
        send({{'jsonrpc': '2.0', 'id': message['id'], 'result': result}})
'''

ANTIGRAVITY_PEER = r'''
import json, os, sys
LOG, MODE = {log!r}, {mode!r}
def log(entry):
    with open(LOG, 'a') as handle:
        handle.write(entry + '\n')
def send(message):
    print(json.dumps(message), flush=True)
log('spawn')
log('env:' + json.dumps(sorted(os.environ)))
log('cwd:' + os.getcwd())
log('argv:' + json.dumps(sys.argv))
line = sys.stdin.readline()
if not line:
    sys.exit(0)
log('event:' + str(json.loads(line).get('event')))
if MODE == 'crash':
    sys.exit(1)
if MODE != 'no_init':
    send({{'event': 'init', 'conversation_id': 'ag-session'}})
send({{'event': 'step_update', 'step_update': {{'conversation_id': 'ag-session', 'step_type': 'agent_response',
                                              'text_delta': 'fixture answer'}}}})
send({{'event': 'result', 'result': {{'conversation_id': 'ag-session', 'status': 'SUCCESS',
                                     'response': 'fixture answer'}}}})
'''

ADAPTERS = {'cursor': (CursorAdapter, CURSOR_PEER), 'antigravity': (AntigravityAdapter, ANTIGRAVITY_PEER)}


@pytest.fixture(autouse=True)
def protocol_without_auth_probe(monkeypatch):
    # Fake protocol peers do not implement sign-in or TUI status probes.
    # Real preflight blocking is covered independently in test_worker_auth.py.
    for adapter_class, _ in ADAPTERS.values():
        monkeypatch.setattr(adapter_class, '_preflight', lambda self, request: None)


def evidence(provider, **changes):
    now = time.time()
    value = SubscriptionEvidence(provider=provider, model='fixture-model', checked_at=now, expires_at=now + 120,
                                 authenticated=True, quota_available=True, overage_disabled=True,
                                 terms_accepted=True, evidence_id='fixture-evidence')
    return dataclasses.replace(value, **changes)


def build(tmp_path, provider, mode='normal', **request_changes):
    """A fake peer for this provider, its log path and a request whose workspace exists in tmp_path."""
    adapter_class, template = ADAPTERS[provider]
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    log = tmp_path / 'peer.log'
    script = tmp_path / 'peer.py'
    script.write_text(template.format(log=str(log), mode=mode))
    adapter = adapter_class([sys.executable, '-u', str(script)])
    request = AdapterRequest('acceptance-request', PROMPT, str(workspace), 'fixture-model')
    return adapter, dataclasses.replace(request, **request_changes), log


def entries(log, prefix):
    lines = log.read_text().splitlines() if log.exists() else []
    return [line[len(prefix):] for line in lines if line.startswith(prefix)]


def raising(_request):
    raise RuntimeError('authority store unavailable')


@pytest.mark.parametrize('provider', sorted(ADAPTERS))
@pytest.mark.parametrize('case', ['no_evidence', 'other_provider', 'expired', 'authority_false', 'authority_raises'])
def test_no_process_starts_without_current_entitlement_and_trusted_authority(tmp_path, provider, case):
    adapter, request, log = build(tmp_path, provider)
    other = 'cursor' if provider == 'antigravity' else 'antigravity'
    entitlement = {'no_evidence': None, 'other_provider': evidence(other),
                   'expired': evidence(provider, expires_at=time.time() - 1)}.get(case, evidence(provider))
    authorize = {'authority_false': lambda r: False, 'authority_raises': raising}.get(case, lambda r: True)
    result = adapter.run(request, authorize=authorize, entitlement=entitlement)
    assert result.state == 'blocked', result
    assert not log.exists(), 'no process may start before entitlement and authority both pass'


@pytest.mark.parametrize('provider', sorted(ADAPTERS))
def test_authority_revoked_after_the_session_never_sends_the_prompt(tmp_path, provider):
    adapter, request, log = build(tmp_path, provider)
    answers = iter([True, False])
    result = adapter.run(request, authorize=lambda r: next(answers, False), entitlement=evidence(provider))
    assert result.state != 'completed', result
    assert len(entries(log, 'spawn')) <= 1, 'one process at most; it may be stopped before it logs'
    assert 'session/prompt' not in entries(log, 'method:') and 'user' not in entries(log, 'event:')


@pytest.mark.parametrize('provider', sorted(ADAPTERS))
def test_the_child_sees_only_its_workspace_and_no_secrets(tmp_path, provider, monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, 'fixture-secret-value')
    adapter, request, log = build(tmp_path, provider)
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence(provider))
    assert result.state == 'completed', result
    environment = json.loads(entries(log, 'env:')[0])
    assert [name for name in SECRETS if name in environment] == []
    assert Path(entries(log, 'cwd:')[0]).resolve() == Path(request.workspace).resolve()
    assert not any(PROMPT in argument for argument in json.loads(entries(log, 'argv:')[0])), 'prompt only on stdin'


@pytest.mark.parametrize('provider, mode', [('cursor', 'no_session_id'), ('antigravity', 'no_init')])
def test_no_session_identity_is_invented(tmp_path, provider, mode):
    adapter, request, log = build(tmp_path, provider, mode)
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence(provider))
    assert result.state == 'uncertain', result
    assert result.session_id is None and result.text == ''


def test_another_sessions_text_never_reaches_the_result(tmp_path):
    adapter, request, log = build(tmp_path, 'cursor', 'mixed')
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence('cursor'))
    assert result.state == 'uncertain' and result.reason == 'session_mismatch'
    assert 'OTHER SESSION TEXT' not in result.text


@pytest.mark.parametrize('mode, expected', [('honor_cancel', 'cancelled'), ('ignore_cancel', 'uncertain')])
def test_stop_mid_turn_is_cancelled_or_uncertain_and_the_prompt_is_sent_once(tmp_path, mode, expected):
    adapter, request, log = build(tmp_path, 'cursor', mode)
    stop = threading.Event()
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence('cursor'), stop_event=stop,
                         on_event=lambda event: stop.set() if event['type'] == 'text' else None)
    assert result.state == expected, result
    assert entries(log, 'spawn') == [''] and entries(log, 'method:').count('session/prompt') == 1
    assert 'session/cancel' in entries(log, 'method:')


def test_antigravity_stop_before_start_never_spawns(tmp_path):
    adapter, request, log = build(tmp_path, 'antigravity')
    stop = threading.Event()
    stop.set()
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence('antigravity'), stop_event=stop)
    assert (result.state, result.reason) == ('cancelled', 'not_started')
    assert not log.exists()


def test_antigravity_stop_mid_turn_is_uncertain_never_cancelled_or_completed(tmp_path):
    adapter, request, log = build(tmp_path, 'antigravity')
    stop = threading.Event()
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence('antigravity'), stop_event=stop,
                         on_event=lambda event: stop.set() if event['type'] == 'text' else None)
    assert result.state == 'uncertain', result
    assert entries(log, 'spawn') == [''] and entries(log, 'event:') == ['user']


@pytest.mark.parametrize('provider', sorted(ADAPTERS))
def test_a_crash_mid_turn_is_uncertain_with_one_spawn_and_no_resend(tmp_path, provider):
    adapter, request, log = build(tmp_path, provider, 'crash')
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence(provider))
    assert result.state == 'uncertain', result
    assert entries(log, 'spawn') == ['']
    assert entries(log, 'method:').count('session/prompt') <= 1 and entries(log, 'event:').count('user') <= 1


@pytest.mark.parametrize('provider', sorted(ADAPTERS))
def test_provenance_never_claims_a_desktop_or_native_thread(tmp_path, provider):
    adapter, request, log = build(tmp_path, provider)
    events = []
    result = adapter.run(request, authorize=lambda r: True, entitlement=evidence(provider), on_event=events.append)
    assert result.state == 'completed' and result.native_visibility == 'unverified'
    assert result.surface == {'cursor': 'cursor_cli_acp', 'antigravity': 'antigravity_cli'}[provider]
    assert events and all(event['native_visibility'] == 'unverified' for event in events)
    assert not any(word in repr(result).lower() for word in ('desktop', 'native_thread'))
    assert result.served_model is None, 'the selected model is not an observed served model'


def test_requests_are_frozen_once_built(tmp_path):
    _, request, _ = build(tmp_path, 'cursor')
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.workspace = str(tmp_path)


@pytest.mark.parametrize('change', [{'workspace': 'relative/workspace'}, {'workspace': '/nonexistent-kyra-fixture'},
                                    {'prompt': 'x' * 16385}, {'timeout_seconds': 301}])
def test_an_invalid_request_is_refused_before_any_process(tmp_path, change):
    adapter, request, log = build(tmp_path, 'cursor')
    with pytest.raises(ValueError):
        adapter.run(dataclasses.replace(request, **change), authorize=lambda r: True, entitlement=evidence('cursor'))
    assert not log.exists()
