"""Local-lead configuration reads only approved immutable source bytes."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion import team_lead_config as config


def test_source_reader_rejects_changed_bytes_and_links(tmp_path):
    source = tmp_path / 'note.md'
    source.write_text('approved')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    reader = config.PinnedSourceReader({'note': source})
    assert reader('note', digest) == 'approved'
    source.write_text('changed')
    with pytest.raises(ValueError):
        reader('note', digest)
    source.unlink()
    other = tmp_path / 'other.md'
    other.write_text('approved')
    source.symlink_to(other)
    with pytest.raises(ValueError):
        reader('note', digest)


def test_source_reader_refuses_parent_link_unknown_reference_and_large_file(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    note = real / 'note'
    note.write_bytes(b'x' * 32769)
    linked = tmp_path / 'linked'
    linked.symlink_to(real, target_is_directory=True)
    reader = config.PinnedSourceReader({'big': note, 'link': linked / 'note'})
    digest = hashlib.sha256(note.read_bytes()).hexdigest()
    for ref in ('missing', 'big', 'link'):
        with pytest.raises(ValueError):
            reader(ref, digest)


def test_missing_config_never_imports_model_or_creates_files(tmp_path, monkeypatch):
    created = {}
    class Service:
        def __init__(self, store, backend, **kwargs):
            created.update(backend=backend, **kwargs)
    monkeypatch.setattr(config, '_service_class', lambda: Service)
    config.build_lead(SimpleNamespace(root=tmp_path), SimpleNamespace(team_lead_config=None))
    assert created['backend'].availability() == {'available': False, 'reason': 'cache_unavailable'}
    assert list(tmp_path.iterdir()) == []


def test_config_requires_unique_refs_and_pinned_source_digest(tmp_path):
    value = dict(version=1, project_id='demo', runtime='/usr/bin/python3',
                 model={'id': 'demo'}, scratch_root='/private/tmp/demo',
                 sources=[
                     dict(id='s', kind='note', ref='note', path=str(tmp_path/'note'), sha256='bad')])
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        config.read_config(path)


def test_config_digest_changes_when_selection_changes(tmp_path):
    src = tmp_path/'note.md'
    src.write_text('note')
    value = dict(version=1, project_id='demo', runtime='/usr/bin/python3',
                 model={'id': 'demo'}, scratch_root='/private/tmp/demo',
                 sources=[
                     dict(id='s', kind='note', ref='note', path=str(src),
                          sha256=hashlib.sha256(src.read_bytes()).hexdigest())])
    path = tmp_path/'config.json'
    path.write_text(json.dumps(value))
    first = config.read_config(path)
    value['sources'] = []
    path.write_text(json.dumps(value))
    second = config.read_config(path)
    assert first['project']['project_sha256'] != second['project']['project_sha256']
    assert 'path' not in first['project']['sources'][0]


def _configuration(tmp_path, **changes):
    value = dict(version=1, project_id='demo', runtime='/usr/bin/python3',
                 model={'id': 'demo'}, scratch_root=str(tmp_path/'scratch'), sources=[])
    value.update(changes)
    path = tmp_path/'config.json'
    path.write_text(json.dumps(value))
    return path


def test_changed_config_cannot_acquire_another_flight_for_same_store(tmp_path, monkeypatch):
    from companion.local_lead_inference import LeadBusyError
    from companion.team_chat import TeamStore
    from companion.workflow_control import WorkflowStore
    monkeypatch.setattr(config, '_FLIGHT_ROOT', tmp_path/'flights')
    store = TeamStore(tmp_path/'team', controller=WorkflowStore(tmp_path/'control.json'))
    path = _configuration(tmp_path)
    settings = SimpleNamespace(team_lead_config=path, data_dir=tmp_path/'data')
    first = config.build_lead(store, settings)
    with first.backend.flight():
        value = json.loads(path.read_text())
        value.update(project_id='another-project', model={'id': 'another-model'},
                     scratch_root=str(tmp_path/'another-scratch'))
        other = tmp_path/'another-config.json'
        other.write_text(json.dumps(value))
        settings.team_lead_config = other
        second = config.build_lead(store, settings)
        with pytest.raises(LeadBusyError):
            with second.backend.flight():
                pytest.fail('same Team store acquired a second flight')


@pytest.mark.parametrize('project_id,sources', [
    (' ', []),
    ('demo', [dict(id=str(i), kind='note', ref='😀'*255+str(i),
                  sha256='a'*64, path='/private/tmp/note') for i in range(8)]),
])
def test_invalid_binding_preserves_status_and_stop_dependency(tmp_path, project_id, sources):
    from companion.team_chat import TeamStore
    from companion.workflow_control import WorkflowStore
    store = TeamStore(tmp_path/'team', controller=WorkflowStore(tmp_path/'control.json'))
    path = _configuration(tmp_path, project_id=project_id, sources=sources)
    service = config.build_lead(store, SimpleNamespace(team_lead_config=path, data_dir=tmp_path/'data'))
    assert service.status() == dict(available=False, active=0, queued=0, reason='cache_unavailable')
    assert not store.path.exists()
    room = store.create_room('Synthetic stop', local_only=True)['id']
    binding = dict(project_id='previous', project_sha256='b'*64, model_id='previous', sources=[])
    store.queue_answer(room, 'Synthetic question', 'stop-existing', binding=binding)
    service.stop(room, 'stop-existing')
    assert store.get_answer(room, 'stop-existing')['status'] == 'stopped'


def test_config_cannot_supply_a_different_lock_path(tmp_path):
    path = _configuration(tmp_path, lock_path=str(tmp_path/'alternate.lock'))
    with pytest.raises(ValueError):
        config.read_config(path)
