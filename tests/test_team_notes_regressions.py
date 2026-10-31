"""Builder regressions for selected note bytes and filesystem publication boundaries."""
import json
import os

import pytest

from companion import team_chat
from companion.errors import ApiError
from companion.settings import get_settings
from companion.workflow_control import WorkflowStore


@pytest.fixture
def selected(tmp_path, monkeypatch):
    monkeypatch.setattr(team_chat, 'DATA_DIR', tmp_path)
    monkeypatch.setenv('KYRA_OBSIDIAN_VAULT', str(tmp_path / 'vault-root' / 'obsidian' / 'Kyra'))
    get_settings.cache_clear()
    store = team_chat.TeamStore(tmp_path / 'team', controller=WorkflowStore(tmp_path / 'control.json'))
    room, run, message = 'a' * 32, 'b' * 32, 'c' * 32
    body = {'version': 2, 'controller_path': str(store.controller.path.resolve()), 'rooms': {
        room: {'room': {'id': room, 'title': '  Bản ghi 日本語  '}, 'runs': [
            {'id': run, 'status': 'completed', 'stage': 'review', 'phase': 'done', 'attempt': 0,
             'role_lease': None, 'write_lease': False, 'expires_at': 0, 'stop_requested': False}],
            'messages': [{'id': message, 'author': 'claude', 'stage': 'review', 'kind': 'result',
                          'run_id': run, 'text': '  result\n\n', 'created_at': 1700000000}]}}}
    store.root.mkdir()
    store.path.write_text(json.dumps(body))
    vault = tmp_path / 'vault-root' / 'obsidian' / 'Kyra'
    vault.mkdir(parents=True)
    yield store, room, message, body, vault
    get_settings.cache_clear()


def test_verbatim_selection_and_conflict_do_not_mutate_source(selected):
    store, room, message, _, vault = selected
    original = store.path.read_bytes()
    preview = store.note_preview(room, message)
    assert '\n  result\n\n\n' in preview['note']
    assert '\n  Bản ghi 日本語  \n' in preview['note']
    assert not (vault / 'Generated Team').exists()
    saved = store.note_confirm(room, message, expected_note_sha256=preview['note_sha256'])
    assert saved['created'] is True
    target, = (vault / 'Generated Team').glob('*.md')
    assert target.read_text() == preview['note']
    target.write_text('owner changes')
    with pytest.raises(ApiError) as caught:
        store.note_confirm(room, message, expected_note_sha256=preview['note_sha256'])
    assert caught.value.code == 'note_conflict'
    assert target.read_text() == 'owner changes'
    assert store.path.read_bytes() == original


@pytest.mark.parametrize(('key', 'value'), [('author', '<iframe>'), ('stage', '```'), ('created_at', float('inf'))])
def test_invalid_metadata_is_not_written(selected, key, value):
    store, room, message, body, vault = selected
    body['rooms'][room]['messages'][0][key] = value
    store.path.write_text(json.dumps(body))
    with pytest.raises(ApiError) as caught:
        store.note_preview(room, message)
    assert caught.value.code == 'invalid_text'
    assert not (vault / 'Generated Team').exists()


def test_parent_swap_cannot_publish_to_attacker(selected, monkeypatch, tmp_path):
    store, room, message, _, vault = selected
    preview = store.note_preview(room, message)
    outside = tmp_path / 'canary'
    outside.mkdir()
    link = os.link
    def swap(src, dst, **kwargs):
        generated = vault / 'Generated Team'
        generated.rename(vault / 'Moved')
        generated.symlink_to(outside, target_is_directory=True)
        return link(src, dst, **kwargs)
    monkeypatch.setattr(os, 'link', swap)
    # A held directory handle may publish to the original directory after a same-user rename;
    # it must never follow the substituted link or escape into the canary.
    try:
        store.note_confirm(room, message, expected_note_sha256=preview['note_sha256'])
    except ApiError:
        pass
    assert not list(outside.iterdir())


def test_fifo_target_refused_without_reading(selected):
    store, room, message, _, vault = selected
    preview = store.note_preview(room, message)
    generated = vault / 'Generated Team'
    generated.mkdir()
    target = generated / (('b' * 32) + '-' + message + '.md')
    os.mkfifo(target)
    with pytest.raises(ApiError) as caught:
        store.note_confirm(room, message, expected_note_sha256=preview['note_sha256'])
    assert caught.value.code == 'unsafe_destination'


def test_confirm_revalidates_completed_status(selected):
    store, room, message, body, vault = selected
    preview = store.note_preview(room, message)
    body['rooms'][room]['runs'][0]['status'] = 'stopped'
    store.path.write_text(json.dumps(body))
    with pytest.raises(ApiError) as caught:
        store.note_confirm(room, message, expected_note_sha256=preview['note_sha256'])
    assert caught.value.code == 'note_stale'
    assert not (vault / 'Generated Team').exists()
