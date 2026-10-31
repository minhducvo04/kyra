"""Account-only cold startup; no real vendor, model, native grant or replay."""
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import team_connected as core
from companion import team_http
from companion.settings import get_settings
from tests.test_team_cursor import evidence


def _readiness_threads():
    current = threading.current_thread()
    return [thread for thread in threading.enumerate()
            if thread is not current and thread.name.startswith('kyra-app-readiness-') and thread.is_alive()]


def until(predicate):
    """Poll predicate until it holds, the readiness workers exit, or 3 seconds pass.

    Those workers publish the status the caller is waiting on. Joining them
    returns as soon as they finish. A fixed sleep would keep waiting after they
    had already exited.
    """
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return True
        pending = _readiness_threads()
        if not pending:
            return predicate()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pending[0].join(remaining)
    return predicate()


@pytest.fixture
def boot(tmp_path, monkeypatch):
    from companion import team_lead

    workspace = tmp_path / 'engineering'
    workspace.mkdir()
    config = tmp_path / 'apps.json'
    account = hashlib.sha256(b'1').hexdigest()
    providers = {name: {'command': ['/bin/false'], 'model': 'fixture-model',
                        'own_api_keys': False,
                        'subscription': asdict(replace(evidence(name), account=account))}
                 for name in core.PROVIDERS}
    config.write_text(json.dumps({'version': 1, 'workspace': str(workspace), 'providers': providers}))
    config.chmod(0o600)
    for name, value in {'KYRA_OWNER_MACHINE': 'true', 'KYRA_TENANT': 'personal',
                        'KYRA_INLINE_WORKER': 'true', 'KYRA_TEAM_APPS_CONFIG': str(config)}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    monkeypatch.setattr(core, '_probes', {})
    # Isolate per-process once state between synthetic service instances.
    monkeypatch.setattr(core, '_startup_providers', set(), raising=False)
    monkeypatch.setattr(team_http, 'get_store', lambda: SimpleNamespace(root=tmp_path / 'team', active_runs=lambda: []))
    monkeypatch.setattr(team_lead, 'has_pending_answers', lambda store: False)
    def forbidden(*args, **kwargs):
        pytest.fail('readiness started inference, native work or app replay')
    for module, names in ((core, ('submit', '_work')), (team_http, ('wake_service', 'wake_lead'))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    releases, calls = [], []
    before = set(threading.enumerate())
    class Adapter:
        def probe_entitlement(self, model):
            calls.append(model)
            return {'authenticated': True, 'account': account, 'evidence': None}
        run = forbidden
    monkeypatch.setattr(core, '_adapter', lambda *args: Adapter())
    def client():
        app = FastAPI()
        team_http.install(app)
        return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 4321))
    yield SimpleNamespace(config=config, providers=providers, account=account, client=client,
                          releases=releases, calls=calls, forbidden=forbidden)
    for event in releases:
        event.set()
    for thread in set(threading.enumerate()) - before:
        if thread.name.startswith('kyra-app-readiness-'):
            thread.join(3)
            assert not thread.is_alive(), 'startup probe did not exit'
    get_settings.cache_clear()


def test_registered_startup_refreshes_once_in_parallel_without_blocking_get(boot, monkeypatch):
    release = threading.Event()
    boot.releases.append(release)
    entered = {provider: threading.Event() for provider in core.PROVIDERS}
    calls = []
    class Adapter:
        def __init__(self, provider):
            self.provider = provider
        def probe_entitlement(self, model):
            calls.append(self.provider)
            entered[self.provider].set()
            assert release.wait(3)
            return {'authenticated': True, 'account': boot.account, 'evidence': None}
        run = boot.forbidden
    monkeypatch.setattr(core, '_adapter', lambda provider, cfg: Adapter(provider))
    with boot.client() as client:
        assert all(event.wait(1) for event in entered.values()), 'startup did not probe both apps'
        # Both blocked adapters are still live, but startup and read-only GET completed.
        rows = client.get('/api/team/apps').json()['apps']
        assert {row['reason'] for row in rows} == {'not_checked'}
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: core.start_readiness_refresh(), range(8)))
        assert sorted(calls) == sorted(core.PROVIDERS)
        release.set()
        assert until(lambda: all(row['available'] for row in core.app_status()['apps']))
    # Repeated startup in the same service process cannot probe again after completion.
    with boot.client() as client:
        assert all(row['available'] for row in client.get('/api/team/apps').json()['apps'])
    assert sorted(calls) == sorted(core.PROVIDERS)


@pytest.mark.parametrize('field,value', [('owner_machine', False), ('tenant', 'other'),
                                        ('inline_worker', False), ('team_apps_config', None)])
def test_unconfigured_or_gated_startup_does_no_work(boot, monkeypatch, field, value):
    monkeypatch.setattr(get_settings(), field, value)
    monkeypatch.setattr(core, '_config', boot.forbidden)
    core.start_readiness_refresh()
    assert core._startup_providers == set() and boot.calls == []


def test_account_refresh_preserves_expired_billing_and_saved_runs(boot, tmp_path, monkeypatch):
    data = json.loads(boot.config.read_text())
    for entry in data['providers'].values():
        entry['subscription'].update(checked_at=time.time()-120, expires_at=time.time()-60)
    boot.config.write_text(json.dumps(data))
    before_config = boot.config.read_bytes()
    store = core.ConnectedStore(tmp_path / 'runs')
    row, _ = store.claim(room_id='a'*32, provider='cursor', request_id='old',
                         text='Old saved request', config_hash='old-pin')
    store.begin(row['id'])
    before_runs = store.path.read_bytes()
    monkeypatch.setattr(core, 'ConnectedStore', boot.forbidden)
    with boot.client():
        assert until(lambda: all(row['checked_at'] is not None for row in core.app_status()['apps']))
    assert {row['reason'] for row in core.app_status()['apps']} == {'subscription_evidence_stale_or_mismatched'}
    assert boot.config.read_bytes() == before_config and store.path.read_bytes() == before_runs
    assert len(boot.calls) == 2


def test_probe_exception_does_not_block_other_provider_or_leak_output(boot, monkeypatch, caplog):
    class Adapter:
        def __init__(self, provider):
            self.provider = provider
        def probe_entitlement(self, model):
            boot.calls.append(self.provider)
            if self.provider == 'cursor':
                raise RuntimeError('PRIVATE-PROBE-OUTPUT')
            return {'authenticated': True, 'account': boot.account, 'evidence': None}
    monkeypatch.setattr(core, '_adapter', lambda provider, cfg: Adapter(provider))
    with boot.client():
        assert until(lambda: core.app_status()['apps'][1]['available'])
    assert not core.app_status()['apps'][0]['available']
    assert sorted(boot.calls) == sorted(core.PROVIDERS)
    assert 'PRIVATE-PROBE-OUTPUT' not in caplog.text


@pytest.mark.parametrize('kind', ['missing_workspace', 'malformed_config'])
def test_bad_config_never_starts_adapter_and_startup_survives(boot, monkeypatch, kind):
    if kind == 'missing_workspace':
        data = json.loads(boot.config.read_text())
        data['workspace'] += '/absent'
        boot.config.write_text(json.dumps(data))
    else:
        boot.config.write_text('{invalid')
    monkeypatch.setattr(core, '_adapter', boot.forbidden)
    with boot.client() as client:
        rows = client.get('/api/team/apps').json()['apps']
        assert {row['reason'] for row in rows} == {'configuration_unavailable'}


def test_startup_uses_real_bounded_cursor_status_protocol_only(boot, tmp_path, monkeypatch):
    from companion.team_cursor import CursorAdapter
    receipt = tmp_path / 'args.json'
    executable = tmp_path / 'fixture-cursor'
    executable.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n'
                          f'Path({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n'
                          'print(json.dumps({"isAuthenticated":True,"userInfo":{"userId":1}}))\n')
    executable.chmod(0o700)
    data = json.loads(boot.config.read_text())
    data['providers'] = {'cursor': data['providers']['cursor']}
    data['providers']['cursor']['command'] = [str(executable)]
    boot.config.write_text(json.dumps(data))
    monkeypatch.setattr(core, '_adapter', lambda provider, cfg: CursorAdapter(cfg['command']))
    monkeypatch.setattr(CursorAdapter, 'run', boot.forbidden)
    before = set(threading.enumerate())
    with boot.client() as client:
        # Join the workers that publish this probe. A 3s poll is shorter than the
        # real status protocol bound (10s) and fails under load before the CLI exits.
        for thread in set(threading.enumerate()) - before:
            if thread.name.startswith('kyra-app-readiness-') and thread.is_alive():
                thread.join()
        assert core.app_status()['apps'][0]['available']
        rows = client.get('/api/team/apps').json()['apps']
        assert rows[1]['reason'] == 'not_configured'
    assert json.loads(receipt.read_text()) == ['status', '--format', 'json']


def test_failed_fresh_probe_invalidates_previous_success(boot, monkeypatch):
    cfg, pin = core._config('cursor')
    core._readiness('cursor', cfg, pin, refresh=True)
    assert core.app_status()['apps'][0]['available']
    class Broken:
        def probe_entitlement(self, model):
            raise RuntimeError('private output')
    monkeypatch.setattr(core, '_adapter', lambda *args: Broken())
    with pytest.raises(RuntimeError):
        core._readiness('cursor', cfg, pin, refresh=True)
    assert not core.app_status()['apps'][0]['available']


def test_overlapping_manual_and_startup_probe_share_one_fresh_observation(boot, monkeypatch):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    boot.releases.append(release)
    lock = threading.Lock()
    class ObservedLock:
        def __enter__(self):
            if lock.locked():
                waiting.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()
    monkeypatch.setattr(core, '_refresh_locks', {'cursor': ObservedLock()}, raising=False)
    class Adapter:
        def probe_entitlement(self, model):
            boot.calls.append(model)
            entered.set()
            assert release.wait(3)
            return {'authenticated': True, 'account': boot.account, 'evidence': None}
    monkeypatch.setattr(core, '_adapter', lambda *args: Adapter())
    cfg, pin = core._config('cursor')
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(core._readiness, 'cursor', cfg, pin, refresh=True)
        assert entered.wait(1)
        second = pool.submit(core._readiness, 'cursor', cfg, pin, refresh=True)
        try:
            assert waiting.wait(1), 'parallel refresh bypassed singleflight'
        finally:
            release.set()
        assert first.result()[1] is None and second.result()[1] is None
    assert len(boot.calls) == 1
