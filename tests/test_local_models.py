import json

import pytest

from companion import local_models as lm


class Loader:
    def __init__(self):
        self.loads, self.swaps, self.closed = [], [], []

    def load(self, row):
        self.loads.append(row['base_model'])
        return row['base_model']

    def swap(self, handle, row):
        self.swaps.append(row['adapter'])

    def answer(self, handle, row, request):
        return {'text': 'ok'}

    def unload(self, handle):
        self.closed.append(handle)

    def ram_gb(self):
        return 0.1


def row(name, ram=4.5, mode='resident', adapter=None):
    return dict(id=name, **{'class': name}, base_model=name, adapter=adapter,
                max_ram_gb=ram, mode=mode, bench={})


def manager(rows, **kwargs):
    return lm.Manager(rows, Loader(), browser=lambda: None, external_big=lambda: [], **kwargs)


def test_resident_cap_before_load():
    m = manager([row('a'), row('b')])
    m.request('a', {})
    with pytest.raises(lm.Refused, match='8 GB'):
        m.request('b', {})
    assert m.loader.loads == ['a']


def test_adapter_swap_shares_one_base():
    a, b = row('a', adapter='first'), row('b', adapter='second')
    b['base_model'] = 'a'
    m = manager([a, b])
    m.request('a', {})
    m.request('b', {})
    m.request('a', {})
    assert m.loader.loads == ['a']
    assert m.loader.swaps == ['first', 'second', 'first']
    assert len(m.status()['loaded']) == 1


def test_big_refused_under_browser_lock():
    m = manager([row('big', 20, 'on_demand')])
    m.browser = lambda: {'train': 'test', 'pid': 123}
    with pytest.raises(lm.Refused, match='browser phase.*123'):
        m.request('big', {})
    assert m.loader.loads == []


def test_other_big_model_refused_and_big_runs_alone():
    m = manager([row('a'), row('big', 20, 'on_demand')])
    m.external_big = lambda: [123]
    with pytest.raises(lm.Refused, match='another big model'):
        m.request('big', {})
    m.external_big = lambda: []
    m.request('a', {})
    m.request('big', {})
    assert m.loader.closed == ['a', 'big']
    assert m.status()['loaded'] == []


def test_roster_initialization_never_overwrites(tmp_path):
    path = tmp_path / 'roster.json'
    lm.init_roster(path)
    original = json.loads(path.read_text())
    assert len({r['base_model'] for r in original if r['mode'] == 'resident'}) == 3
    path.write_text('[]')
    lm.init_roster(path)
    assert path.read_text() == '[]'


def test_measured_memory_cap_unloads():
    m = manager([row('a')])
    m.loader.ram_gb = lambda: 9
    with pytest.raises(lm.Refused, match='measured RAM'):
        m.request('a', {})
    assert m.status()['loaded'] == []


def test_unix_status_and_run(tmp_path):
    import threading
    m = manager([row('a')])
    import tempfile
    from pathlib import Path
    folder = tempfile.TemporaryDirectory(dir="/tmp")
    path = Path(folder.name) / "models.sock"
    with lm.serve(path, m) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert lm.client(path, {'op': 'run', 'agent': 'a'}) == {'text': 'ok'}
            status = lm.client(path, {'op': 'status'})
            assert status['loaded'][0]['base_model'] == 'a'
            assert status['ram_gb'] == .1
            assert path.stat().st_mode & 0o777 == 0o600
        finally:
            server.shutdown()
            thread.join()
    folder.cleanup()


def test_single_owner(tmp_path):
    with lm.ownership(tmp_path), pytest.raises(lm.Refused, match='already running'):
        with lm.ownership(tmp_path):
            pytest.fail('second owner acquired')


def test_big_block_does_not_evict_residents():
    m = manager([row('a'), row('big', 20, 'on_demand')])
    m.request('a', {})
    m.browser = lambda: {'train': 'gate', 'pid': 123}
    with pytest.raises(lm.Refused):
        m.request('big', {})
    assert len(m.status()['loaded']) == 1


def test_bench_losers_are_not_warmed_or_served():
    loser = {**row('loser'), 'selected': False}
    m = manager([loser, row('winner')])
    m.warm()
    assert m.loader.loads == ['winner']
    with pytest.raises(lm.Refused, match='not selected'):
        m.request('loser', {})


@pytest.mark.parametrize('owner', ['foreign', None, 'current'])
def test_denied_process_inspection_checks_ownership(monkeypatch, owner):
    import getpass

    import psutil

    class Process:
        pid = -1

        def cmdline(self):
            raise psutil.AccessDenied(self.pid)

        def username(self):
            if owner is None:
                raise psutil.AccessDenied(self.pid)
            return owner

    monkeypatch.setattr(getpass, 'getuser', lambda: 'current')
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: [Process()])
    if owner == 'current':
        with pytest.raises(lm.Refused, match='cannot inspect'):
            lm.external_big_models()
    else:
        assert lm.external_big_models() == []


def test_ram_sampling_during_torch_import_uses_rss(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    import psutil

    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace())
    monkeypatch.delitem(sys.modules, 'mlx.core', raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=2 * 1024**3)))
    assert lm.OfflineLoader(tmp_path).ram_gb() == 2.0


def test_ram_sampling_during_mlx_import_uses_rss(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    import psutil

    monkeypatch.setitem(sys.modules, 'mlx.core', SimpleNamespace())
    monkeypatch.delitem(sys.modules, 'torch', raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=2 * 1024**3)))
    assert lm.OfflineLoader(tmp_path).ram_gb() == 2.0


@pytest.mark.parametrize('revision', [None, 'complete-revision'])
def test_offline_snapshot_does_not_require_repository_metadata(monkeypatch, tmp_path, revision):
    import sys
    from types import SimpleNamespace

    def snapshot(model, **kwargs):
        assert kwargs['local_files_only'] is True
        assert kwargs['revision'] == revision
        assert kwargs['ignore_patterns'] == ['README.md', '.gitattributes']
        raise RuntimeError('snapshot checked without loading a model')

    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(snapshot_download=snapshot))
    with pytest.raises(RuntimeError, match='snapshot checked'):
        lm.OfflineLoader(tmp_path).load(dict(row('cached'), revision=revision))


@pytest.mark.parametrize('api', ['direct', 'metal', 'missing', 'raises'])
def test_mlx_memory_api_fallback(monkeypatch, tmp_path, api):
    import sys
    from types import SimpleNamespace

    import psutil

    def broken():
        raise RuntimeError('initializing')

    metal = SimpleNamespace(get_active_memory=lambda: 3 * 1024**3)
    mx = SimpleNamespace()
    if api == 'direct':
        mx.get_active_memory = lambda: 3 * 1024**3
    if api in ('metal', 'raises'):
        mx.metal = metal
    if api == 'raises':
        mx.get_active_memory = broken
    monkeypatch.setitem(sys.modules, 'mlx.core', mx)
    monkeypatch.delitem(sys.modules, 'torch', raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=2 * 1024**3)))
    assert lm.OfflineLoader(tmp_path).ram_gb() == (2 if api == 'missing' else 3)


def test_external_big_models_ignores_own_ancestors_and_bare_names(monkeypatch):
    """L0 fix-back (2026-10-15): the profiler refused itself ("another big model is loaded") because its own
    gate_lock.py wrapper and launching shells carry "--model mlx-community/Qwen3.8-27B-4bit" in argv. The ancestor
    chain is skipped, a name alone needs 2 GiB resident, and a foreign loaded model is still found."""
    import os
    from types import SimpleNamespace

    import psutil

    def proc(pid, cmd, rss_gb):
        return SimpleNamespace(pid=pid, cmdline=lambda: cmd.split(), memory_info=lambda: SimpleNamespace(rss=int(rss_gb * 1024**3)))

    table = [
        proc(100, 'zsh -c gate_lock.py run -- agent_profile.py --model mlx-community/Qwen3.8-27B-4bit', 0.05),  # ancestor
        proc(101, 'python scripts/gate_lock.py run -- agent_profile.py --model mlx-community/Qwen3.8-27B-4bit', 0.1),  # ancestor
        proc(200, 'python -m mlx_lm.server --model mlx-community/Qwen3.8-27B-4bit', 0.3),  # a launcher, nothing loaded yet
        proc(300, 'python -m mlx_lm.server --model mlx-community/Qwen3.8-27B-4bit', 9.0),  # a foreign loaded 27B
        proc(400, 'ollama runner', 8.5),  # unnamed but resident
        proc(500, 'python decision_worker.py --family home_router', 1.5),  # small head, no size token
    ]
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: table)
    monkeypatch.setattr(psutil, 'Process', lambda *a, **k: SimpleNamespace(parents=lambda: [SimpleNamespace(pid=100), SimpleNamespace(pid=101)]))
    monkeypatch.setattr(os, 'getpid', lambda: 99)
    assert lm.external_big_models() == [300, 400]
