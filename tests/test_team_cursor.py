"""Real stdio peers exercise dispatch, identity, denial and bounded cleanup."""
import json
import sys
import threading
import time
from dataclasses import replace

import pytest

from companion.team_cursor import AdapterRequest, CursorAdapter, SubscriptionEvidence


@pytest.fixture(autouse=True)
def protocol_without_auth_probe(monkeypatch):
    # These peers test ACP, not provider authentication. The real status-command
    # preflight and no-task-on-failure contract live in test_worker_auth.py.
    monkeypatch.setattr(CursorAdapter, '_preflight', lambda self, request: None)


def evidence(provider='cursor'):
    return SubscriptionEvidence(provider=provider, model='fixture-model', checked_at=time.time(),
        expires_at=time.time()+120, authenticated=True, quota_available=True,
        overage_disabled=True, terms_accepted=True, evidence_id='fixture-observation')


def request(tmp_path):
    return AdapterRequest('fixture-request', 'Reply with fixture answer', str(tmp_path), 'fixture-model')


def peer(tmp_path, body):
    path = tmp_path / 'peer.py'
    path.write_text(body)
    return [sys.executable, '-u', str(path)]


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(0)
    return False


ACP = r'''import sys, json
if sys.argv[1:] == ['status', '--format', 'json']:
    print(json.dumps({'isAuthenticated': True}))
    sys.exit(0)
session = 'fixture-session'
for line in sys.stdin:
    m = json.loads(line)
    method = m['method']
    if method == 'initialize':
        result = {'protocolVersion':1, 'agentCapabilities':{'loadSession':True}}
    elif method == 'session/new':
        result = {'sessionId':session}
    elif method == 'session/load':
        session = m['params']['sessionId']; result = {}
    elif method == 'session/prompt':
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{
            'sessionId':session,'update':{'sessionUpdate':'agent_message_chunk',
            'content':{'type':'text','text':'fixture answer'}}}}), flush=True)
        result = {'stopReason':'end_turn'}
    else:
        result = {}
    if 'id' in m:
        print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}), flush=True)
'''


def test_cursor_real_stdio_turn_and_resume(tmp_path):
    adapter = CursorAdapter(peer(tmp_path, ACP))
    events = []
    result = adapter.run(request(tmp_path), authorize=lambda r: True,
                         entitlement=evidence(), on_event=events.append)
    assert (result.state, result.text, result.session_id) == ('completed', 'fixture answer', 'fixture-session')
    assert result.surface == 'cursor_cli_acp'
    assert result.native_visibility == 'unverified'
    assert any(e['type'] == 'session' for e in events)
    result = adapter.run(replace(request(tmp_path), request_id='second', session_id='exact-resume'),
                         authorize=lambda r: True, entitlement=evidence())
    assert result.session_id == 'exact-resume'


@pytest.mark.parametrize('field,value', [('authenticated',False), ('quota_available',False),
    ('overage_disabled',False), ('terms_accepted',False), ('expires_at',0), ('provider','antigravity'),
    ('model','wrong-model'), ('checked_at',float('nan'))])
def test_cursor_no_spawn_without_current_subscription_proof(tmp_path, field, value):
    result = CursorAdapter(['/does/not/exist']).run(request(tmp_path), authorize=lambda r: True,
        entitlement=replace(evidence(), **{field:value}))
    assert result.state == 'blocked'
    assert result.reason != 'cli_unavailable'


def test_authority_denial_prevents_spawn(tmp_path):
    result = CursorAdapter(['/does/not/exist']).run(request(tmp_path), authorize=lambda r: False,
                                                 entitlement=evidence())
    assert (result.state,result.reason) == ('blocked','authority_denied')


def test_cursor_wrong_session_is_uncertain(tmp_path):
    body = ACP.replace("'sessionId':session,'update'", "'sessionId':'wrong-session','update'")
    result = CursorAdapter(peer(tmp_path, body)).run(request(tmp_path), authorize=lambda r: True,
                                                  entitlement=evidence())
    assert (result.state, result.reason) == ('uncertain','session_mismatch')
    assert not result.text


def test_cursor_permission_request_is_rejected_and_never_success(tmp_path):
    body = ACP.replace("result = {'stopReason':'end_turn'}", """print(json.dumps({'jsonrpc':'2.0','id':99,'method':'session/request_permission','params':{'sessionId':session}}),flush=True)
        reply=json.loads(sys.stdin.readline())
        assert reply['result']['outcome']['outcome']=='cancelled'
        result = {'stopReason':'end_turn'}""")
    result = CursorAdapter(peer(tmp_path, body)).run(request(tmp_path), authorize=lambda r: True,
                                                  entitlement=evidence())
    assert (result.state,result.reason) == ('blocked','permission_required')


def test_cursor_closed_stream_never_claims_completion(tmp_path):
    body = ACP.replace("result = {'stopReason':'end_turn'}", "sys.exit(0)")
    result = CursorAdapter(peer(tmp_path, body)).run(request(tmp_path), authorize=lambda r: True,
                                                  entitlement=evidence())
    assert result.state == 'uncertain'
    assert result.reason == 'connection_closed'


def test_cursor_timeout_is_bounded_and_uncertain(tmp_path, monkeypatch):
    # The peer blocks past the real timeout. The parent clock stays put until that
    # prompt is visible, then jumps past the deadline and the cancel grace, so a
    # slow spawn cannot spend the budget early or blow a tight wall-clock cap.
    marker = tmp_path / 'prompted'
    temporary = str(marker) + '.tmp'
    body = ACP.replace("result = {'stopReason':'end_turn'}",
        "_h=open(" + repr(temporary) + ",'w');"
        "_h.write(str(__import__('time').monotonic()));_h.close();"
        "__import__('os').replace(" + repr(temporary) + "," + repr(str(marker)) + ");"
        "__import__('threading').Event().wait()")
    real_monotonic = time.monotonic
    origin = real_monotonic()
    jumps = {'n': 0}

    def clock():
        if not marker.exists():
            return origin
        jumps['n'] += 1
        return origin + jumps['n'] * 100

    monkeypatch.setattr(time, 'monotonic', clock)
    result = CursorAdapter(peer(tmp_path, body)).run(replace(request(tmp_path), timeout_seconds=60),
                       authorize=lambda r: True, entitlement=evidence())
    assert result.state == 'uncertain'
    assert marker.exists()
    assert jumps['n'] >= 1
    assert real_monotonic() - float(marker.read_text()) < 30


def test_cursor_pre_cancel_never_spawns(tmp_path):
    stop = threading.Event()
    stop.set()
    result = CursorAdapter(['/does/not/exist']).run(request(tmp_path), authorize=lambda r: True,
        entitlement=evidence(),stop_event=stop)
    assert result.state == 'cancelled'


def test_environment_has_no_provider_keys_or_endpoint_overrides(monkeypatch):
    from companion.team_cursor import child_environment
    monkeypatch.setenv('CURSOR_API_KEY','must-not-forward')
    monkeypatch.setenv('ANTHROPIC_API_KEY','must-not-forward')
    monkeypatch.setenv('CURSOR_API_ENDPOINT','https://invalid.example')
    child = child_environment()
    assert not any(k in child for k in ('CURSOR_API_KEY','ANTHROPIC_API_KEY','CURSOR_API_ENDPOINT'))


def test_auth_status_sanitizes_account_and_tokens(tmp_path):
    cmd = peer(tmp_path, "import json; print(json.dumps({'isAuthenticated':True,'accessToken':'private','email':'private'}))")
    status = CursorAdapter(cmd).status()
    assert status == {'authenticated':True,'reason':None,'entitlement':'unverified','account':''}
    assert 'private' not in json.dumps(status)


def test_cursor_readiness_does_not_forge_quota_from_login(tmp_path):
    cmd = peer(tmp_path, "import json; print(json.dumps({'isAuthenticated':True},indent=2))")
    result = CursorAdapter(cmd).probe_entitlement('fixture-model')
    assert result['authenticated'] is True
    assert result['evidence'] is None
    assert result['reason'] == 'quota_and_overage_read_unavailable'


def test_callback_failure_after_session_never_sends_prompt(tmp_path):
    marker = tmp_path / 'sent'
    body = ACP.replace("elif method == 'session/prompt':", "elif method == 'session/prompt':\n        open("+repr(str(marker))+",'w').write('sent')")
    def fail(event):
        raise RuntimeError('private callback details')
    result = CursorAdapter(peer(tmp_path,body)).run(request(tmp_path), authorize=lambda r:True,
        entitlement=evidence(),on_event=fail)
    assert result.state == 'uncertain'
    assert not marker.exists()
    assert 'private callback details' not in repr(result)


def test_response_budget_stops_unbounded_peer(tmp_path):
    body = ACP.replace("'text':'fixture answer'", "'text':'x'*1048577")
    result = CursorAdapter(peer(tmp_path,body)).run(request(tmp_path),authorize=lambda r:True,
        entitlement=evidence())
    assert (result.state,result.reason) == ('uncertain','response_too_large')


def test_unknown_quota_is_allowed_only_with_confirmed_no_overage(tmp_path):
    from companion.team_cursor import entitlement_reason
    value = replace(evidence(),quota_available=None)
    assert entitlement_reason(value,'cursor','fixture-model') is None
    assert entitlement_reason(replace(value,overage_disabled=False),'cursor','fixture-model') == 'overage_not_disabled'
    assert entitlement_reason(replace(value,quota_available=False),'cursor','fixture-model') == 'quota_exhausted'


def test_evidence_expiry_comes_from_observer_not_arbitrary_five_minutes():
    from companion.team_cursor import entitlement_reason
    now = time.time()
    value = replace(evidence(),checked_at=now-600,expires_at=now+600)
    assert entitlement_reason(value,'cursor','fixture-model') is None
    assert entitlement_reason(replace(value,expires_at=now-1),'cursor','fixture-model') == 'subscription_evidence_stale_or_mismatched'


def test_subscription_evidence_account_defaults_empty():
    assert evidence().account == ''


def test_login_switch_changes_opaque_account_and_probe_preserves_it(monkeypatch):
    import hashlib

    from companion import team_cursor
    response = {'isAuthenticated':True, 'userInfo':{'userId':123456789, 'email':'private'}}
    monkeypatch.setattr(team_cursor, 'read_json_command', lambda command, **kw: response)
    adapter = CursorAdapter(['/unused-fixture'])
    first = adapter.status()
    expected = hashlib.sha256(b'123456789').hexdigest()
    assert first['account'] == expected
    assert adapter.probe_entitlement('fixture-model')['account'] == expected
    assert '123456789' not in json.dumps(first) and 'private' not in json.dumps(first)
    response['userInfo'] = {'userId':987654321, 'email':'other-private'}
    second = adapter.status()
    assert second['account'] == hashlib.sha256(b'987654321').hexdigest()
    assert second['account'] != first['account']
    assert adapter.probe_entitlement('fixture-model')['account'] == second['account']


@pytest.mark.parametrize('authenticated,user_info', [
    (False, {'userId':123456789}), (1, {'userId':123456789}),
    (True, {}), (True, None), (True, []), (True, 'invalid'),
    (True, {'userId':True}), (True, {'userId':0}), (True, {'userId':-1}),
    (True, {'userId':'123456789'}), (True, {'userId':1.0}),
])
def test_status_has_no_account_without_authenticated_positive_integer_id(monkeypatch, authenticated, user_info):
    from companion import team_cursor
    response = {'isAuthenticated':authenticated, 'userInfo':user_info}
    monkeypatch.setattr(team_cursor, 'read_json_command', lambda command, **kw: response)
    status = CursorAdapter(['/unused-fixture']).status()
    assert status['account'] == ''


def test_status_error_has_empty_account_without_raw_error(monkeypatch):
    from companion import team_cursor
    def unavailable(command, **kw):
        raise team_cursor.ProtocolError('status_unavailable')
    monkeypatch.setattr(team_cursor, 'read_json_command', unavailable)
    status = CursorAdapter(['/unused-fixture']).status()
    assert status == {'authenticated':False, 'reason':'status_unavailable',
                      'entitlement':'unverified', 'account':''}


def test_real_fake_peer_stops_when_owning_process_is_sigkilled(tmp_path):
    """Real process proof: owner dies; its CLI ends; an unrelated group survives."""
    import os
    import signal
    import subprocess
    from pathlib import Path

    from companion import team_cursor

    marker = tmp_path / 'peer-identity.json'
    body = ACP.replace("result = {'stopReason':'end_turn'}", """import os, threading
        with open(""" + repr(str(marker)) + """, 'w') as handle:
            json.dump({'pid':os.getpid(), 'pgid':os.getpgrp()},handle)
        threading.Event().wait()""")
    command = peer(tmp_path, body)
    runner = tmp_path / 'owner.py'
    runner.write_text(
        'import sys,time\n'
        f'sys.path.insert(0,{str(Path(team_cursor.__file__).parents[1])!r})\n'
        'from companion.team_cursor import CursorAdapter, AdapterRequest, SubscriptionEvidence\n'
        f'request=AdapterRequest("death-probe","fixture",{str(tmp_path)!r},"fixture-model",timeout_seconds=120)\n'
        'now=time.time()\n'
        'proof=SubscriptionEvidence("cursor","fixture-model",now,now+120,True,True,True,True,"fixture")\n'
        f'CursorAdapter({command!r}).run(request,authorize=lambda r:True,entitlement=proof)\n'
    )
    other = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()'],
                             start_new_session=True)
    owner = subprocess.Popen([sys.executable, str(runner)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identity = None
    try:
        assert wait_until(marker.exists, 60), 'fake peer must receive the actual prompt before owner death'
        identity = json.loads(marker.read_text())
        assert identity['pgid'] == identity['pid']
        assert identity['pgid'] not in (os.getpgrp(), owner.pid, other.pid)
        owner.kill()
        owner.wait(timeout=30)

        def peer_exited():
            try:
                os.kill(identity['pid'], 0)
            except ProcessLookupError:
                return True
            return False

        if not wait_until(peer_exited, 30):
            pytest.fail('owned CLI survived its owning process SIGKILL')
        assert other.poll() is None, 'supervision must not stop unrelated process groups'
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=30)
        if identity:
            try:
                os.killpg(identity['pgid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        other.kill()
        other.wait(timeout=30)
