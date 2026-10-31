"""Bounded subscription reads, no model dispatch, and honest cached failures."""
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from companion import team_usage_reader as reader

NOW = datetime(2030, 1, 1, tzinfo=UTC)
RAW = {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 25}}}


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch):
    monkeypatch.setattr(reader, '_cached', None)
    monkeypatch.setattr(reader, '_last_error', None)
    monkeypatch.setattr(reader, '_last_cleanup_error', None)
    monkeypatch.setattr(reader, '_checked_at', None)
    monkeypatch.setattr(reader, '_now', lambda: NOW)
    monkeypatch.setattr(reader, '_local', None)
    monkeypatch.setattr(reader, 'local_rows', lambda now: [])  # the real home stays out


def test_get_is_cache_only_and_refresh_observations_keep_real_time(monkeypatch):
    calls = []
    monkeypatch.setattr(reader, '_read_codex_usage', lambda: calls.append(1) or RAW)
    assert reader.get_snapshot()['rows'][0]['known'] is False
    assert not calls
    fresh = reader.get_snapshot(refresh=True)
    assert fresh['rows'][0]['remaining'] == 75
    assert fresh['reader']['status'] == 'fresh'
    fresh['rows'][0]['remaining'] = 99
    cached = reader.get_snapshot()
    assert cached['rows'][0]['remaining'] == 75
    assert cached['reader']['status'] == 'cached'
    monkeypatch.setattr(reader, '_now', lambda: NOW + timedelta(seconds=61))
    stale = reader.get_snapshot()
    assert stale['rows'][0]['stale'] and stale['rows'][0]['status'] == 'stale'
    assert stale['rows'][0]['observed_at'] == '2030-01-01T00:00:00Z'
    assert calls == [1]


def test_refresh_denial_retains_evidence_but_marks_it_stale(monkeypatch):
    monkeypatch.setattr(reader, '_read_codex_usage', lambda: RAW)
    reader.get_snapshot(refresh=True)

    def denied():
        raise reader.UsageReadError('permission_denied')

    monkeypatch.setattr(reader, '_read_codex_usage', denied)
    value = reader.get_snapshot(refresh=True)
    assert value['reader']['error'] == 'permission_denied'
    assert value['rows'][0]['stale'] and value['rows'][0]['remaining'] == 75
    assert reader.get_snapshot()['reader']['error'] == 'permission_denied'


def test_failure_without_cache_is_unknown_not_zero(monkeypatch):
    def unavailable():
        raise reader.UsageReadError('cli_not_found')
    monkeypatch.setattr(reader, '_read_codex_usage', unavailable)
    value = reader.get_snapshot(refresh=True)
    assert value['reader']['status'] == 'unavailable'
    assert value['rows'][0]['reason'] == 'cli_not_found'
    assert value['rows'][0]['remaining'] is None


def cli_fixture(tmp_path, monkeypatch, body):
    script = tmp_path / 'codex'
    script.write_text(f'#!{sys.executable}\n' + body)
    script.chmod(0o700)
    monkeypatch.setattr(reader.shutil, 'which', lambda name: str(script))


def test_real_pipe_handshake_only_sends_initialize_and_quota_read(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, '''import json, sys
assert sys.argv[1:3] == ['app-server', '--stdio']
assert 'plugins' in sys.argv and 'apps' in sys.argv
first = json.loads(input())
assert first['method'] == 'initialize'
print(json.dumps({'id': first['id'], 'result': {}}), flush=True)
assert json.loads(input())['method'] == 'initialized'
account = json.loads(input())
assert account['method'] == 'account/read' and account['params'] == {'refreshToken': False}
print(json.dumps({'id': account['id'], 'result': {'account': {'type': 'chatgpt'}}}), flush=True)
usage = json.loads(input())
assert usage['method'] == 'account/rateLimits/read'
assert usage['params'] == {'excludeResetCreditDetails': True, 'supportsLunaReserve': False}
print(json.dumps({'method': 'unrelated/notification', 'params': {}}), flush=True)
print(json.dumps({'id': usage['id'], 'result': {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 25}}, 'accountId': 'PRIVATE'}}), flush=True)
sys.stdin.read()
''')
    value = reader.get_snapshot(refresh=True)
    assert value['rows'][0]['remaining'] == 75
    assert 'PRIVATE' not in json.dumps(value)


def test_actual_pipe_timeout_is_bounded(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, 'import threading\nthreading.Event().wait()\n')
    origin = time.monotonic()
    ticks = {'n': 0}

    def monotonic():
        ticks['n'] += 1
        if ticks['n'] == 1:
            return origin
        return origin + 1

    monkeypatch.setattr(reader.time, 'monotonic', monotonic)
    with pytest.raises(reader.UsageReadError, match='timeout'):
        reader._read_codex_usage(timeout_seconds=0.1)


def test_permission_denial_is_sanitized_not_retried(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, "import sys\nsys.stderr.write('Permission denied PRIVATE-CREDENTIAL')\n")
    value = reader.get_snapshot(refresh=True)
    assert value['reader']['error'] == 'permission_denied'
    assert 'PRIVATE' not in json.dumps(value)


def test_unexpected_server_requests_are_not_approved(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, '''import json, sys
input()
print(json.dumps({'id': 77, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}}), flush=True)
sys.stdin.read()
''')
    value = reader.get_snapshot(refresh=True)
    assert value['reader']['error'] == 'authentication_required'


def test_output_flood_is_bounded(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, "import sys\nsys.stdout.write('x' * 300000)\nsys.stdout.flush()\n")
    with pytest.raises(reader.UsageReadError, match='response_too_large'):
        reader._read_codex_usage()


def test_child_environment_excludes_credentials_endpoints_proxies_and_injection(monkeypatch):
    unsafe = ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN',
              'OPENAI_BASE_URL', 'CHATGPT_BASE_URL', 'CODEX_MODEL_PROVIDER', 'HTTP_PROXY',
              'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy',
              'NODE_OPTIONS', 'PYTHONPATH', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES')
    for key in unsafe:
        monkeypatch.setenv(key, 'must-not-inherit')
    env = reader._child_environment()
    assert not set(unsafe).intersection(env)
    assert env['HOME'] == os.environ['HOME']
    assert os.environ['OPENAI_API_KEY'] == 'must-not-inherit'


def test_non_subscription_auth_stops_before_quota_request(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, '''import json, sys
first = json.loads(input())
print(json.dumps({'id': first['id'], 'result': {}}), flush=True)
input()
account = json.loads(input())
assert account['method'] == 'account/read'
print(json.dumps({'id': account['id'], 'result': {'account': {'type': 'apiKey'}}}), flush=True)
sys.stdin.read()
''')
    assert reader.get_snapshot(refresh=True)['reader']['error'] == 'subscription_auth_required'


def test_app_scoped_mcp_startup_stops_quota_read(tmp_path, monkeypatch):
    cli_fixture(tmp_path, monkeypatch, '''import json, sys
input()
print(json.dumps({'method': 'mcpServer/startupStatus/updated', 'params': {'threadId': None}}), flush=True)
sys.stdin.read()
''')
    assert reader.get_snapshot(refresh=True)['reader']['error'] == 'unexpected_mcp_startup'


def test_shutdown_kills_child_that_ignores_termination(tmp_path, monkeypatch):
    marker = tmp_path / 'child.pid'
    child = ('import os, signal, threading; from pathlib import Path; '
             'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             f'Path({str(marker)!r}).write_text(str(os.getpid())); threading.Event().wait()')
    cli_fixture(tmp_path, monkeypatch, f'''import subprocess, sys, threading
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {child!r}])
while not Path({str(marker)!r}).exists():
    pass
threading.Event().wait()
''')
    real_monotonic = time.monotonic
    origin = real_monotonic()
    bound = 2.0

    def monotonic():
        if marker.exists() or real_monotonic() >= origin + bound:
            return origin + 100
        return origin

    class ShortPollSelector(reader.selectors.DefaultSelector):
        def select(self, timeout=None):
            if timeout is None:
                return super().select(0.01)
            return super().select(min(timeout, 0.01) if timeout > 0 else timeout)

    monkeypatch.setattr(reader.time, 'monotonic', monotonic)
    monkeypatch.setattr(reader.selectors, 'DefaultSelector', ShortPollSelector)
    pid = None
    try:
        with pytest.raises(reader.UsageReadError, match='timeout'):
            # 2 s bound, not 0.3 s: under a loaded full suite two interpreter start-ups can outlast 0.3 s, so the
            # reader timed out before the stubborn child existed (seen 2026-10-04: child.pid missing).
            # The fake clock expires as soon as child.pid exists, so the happy path does not wait the bound.
            reader._read_codex_usage(timeout_seconds=bound)
        pid = int(marker.read_text())
        deadline = real_monotonic() + 1.0
        while real_monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        else:
            pytest.fail('quota reader left a live child process')
    finally:
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
