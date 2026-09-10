from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient

from companion.initiative_store import DbInitiativeStore
from companion.initiatives import Evidence, Initiative
from companion.reminders import RemindersStore


def fixture(tmp_path):
    reminders = RemindersStore(tmp_path / 'reminders.db')
    store = DbInitiativeStore(tmp_path / 'initiatives.db', reminders=reminders)
    item = Initiative('Review trace', 'A trace is ready', 'Read the trace', 10, ['e'])
    evidence = [Evidence('e', 'notes', 'A trace is ready', '2026-09-10')]
    store.save([item], evidence, date(2026, 9, 10))
    return store, reminders, store.list()[0]['id'], item, evidence


def test_accept_is_idempotent_across_concurrent_requests(tmp_path):
    store, reminders, id, _, _ = fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.accept(id), range(4)))
    assert len(reminders.list()) == 1
    assert len({r['reminder_id'] for r in results}) == 1
    assert store.list() == []
    assert store.get(id)['status'] == 'accepted'


def test_accept_resumes_after_reminder_commit_and_lost_response(tmp_path, monkeypatch):
    store, reminders, id, _, _ = fixture(tmp_path)
    original = reminders.add_once
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('lost response')
    monkeypatch.setattr(reminders, 'add_once', lost)
    with pytest.raises(OSError):
        store.accept(id)
    assert len(reminders.list()) == 1
    with pytest.raises(ValueError):
        store.dismiss(id, 'changed my mind')
    monkeypatch.setattr(reminders, 'add_once', original)
    assert store.accept(id)['reminder_id'] == reminders.list()[0].id
    assert len(reminders.list()) == 1


def test_dismiss_reason_survives_daily_reuse_and_prevents_accept(tmp_path):
    store, reminders, id, item, evidence = fixture(tmp_path)
    store.dismiss(id, 'Not useful now')
    store.save([item], evidence, date(2026, 9, 11))
    assert store.list() == []
    assert store.get(id)['reason'] == 'Not useful now'
    with pytest.raises(ValueError):
        store.accept(id)
    assert reminders.list() == []


def test_new_bundle_retires_proposals_but_preserves_accepted(tmp_path):
    store, reminders, id, _, _ = fixture(tmp_path)
    store.save([], [], date(2026, 9, 11))
    assert store.list() == []
    with pytest.raises(ValueError):
        store.accept(id)
    assert reminders.list() == []


def test_endpoints_return_receipt_conflict_and_not_found(tmp_path, monkeypatch):
    from companion import webapp
    store, reminders, id, _, _ = fixture(tmp_path)
    monkeypatch.setattr(webapp, 'initiative_store', lambda: store)
    monkeypatch.setattr('companion.initiative_digest.existing_store', lambda: store)
    with TestClient(webapp.app) as client:
        assert client.get('/api/initiatives').json()['initiatives'][0]['evidence'][0]['quote'] == 'A trace is ready'
        first = client.post(f'/api/initiatives/{id}/accept')
        assert first.status_code == 200
        assert client.post(f'/api/initiatives/{id}/accept').json() == first.json()
        assert client.post(f'/api/initiatives/{id}/dismiss', json={'reason': 'no'}).status_code == 409
        assert client.post('/api/initiatives/missing/accept').status_code == 404
    assert len(reminders.list()) == 1


def test_digest_round_trip_and_html_escape(tmp_path):
    from datetime import datetime

    from companion.digest import DigestData, from_json, render_html, render_markdown, to_json
    store, _, _, _, _ = fixture(tmp_path)
    data = DigestData(generated_at=datetime.now(), initiatives=store.list())
    data.initiatives[0]['title'] = '<script>bad</script>'
    restored = from_json(to_json(data))
    assert restored.initiatives == data.initiatives
    assert 'Kyra suggests' in render_markdown(restored)
    assert '&lt;script&gt;bad&lt;/script&gt;' in render_html(restored)
    assert 'no LLM call' not in render_html(restored)
    assert data.action_count == 0


def test_digest_preview_never_creates_state(tmp_path, monkeypatch):
    from datetime import datetime

    from companion import initiative_digest
    from companion.digest import DigestData
    monkeypatch.setattr(initiative_digest, 'DB_PATH', tmp_path / 'absent.db')
    data = DigestData(generated_at=datetime.now())
    initiative_digest.populate_initiatives(data, write=False, cache_dir=tmp_path / 'absent')
    assert data.initiatives == []
    assert list(tmp_path.iterdir()) == []


def test_historical_digest_cannot_replace_current_proposals(tmp_path):
    store, _, _, item, evidence = fixture(tmp_path)
    store.save([], [], date(2026, 9, 12))
    old = Initiative('Old proposal', 'Old note', 'Read old note', 5, ['e'])
    store.save([old], evidence, date(2026, 9, 11))
    assert store.list() == []


def test_digest_omits_acceptance_in_progress(tmp_path, monkeypatch):
    from datetime import datetime

    from companion.digest import DigestData
    from companion.initiative_digest import populate_initiatives
    store, reminders, id, _, _ = fixture(tmp_path)
    monkeypatch.setattr(reminders, 'add_once', lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.accept(id)
    data = DigestData(generated_at=datetime.now())
    populate_initiatives(data, write=False, cache_dir=tmp_path / 'cache', store=store)
    assert data.initiatives == []


@pytest.mark.parametrize('created_first', [False, True])
def test_migration_handles_startup_table_creation(tmp_path, monkeypatch, created_first):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from companion.schema import metadata
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('ALEMBIC_URL', f'sqlite:///{tmp_path}/migrate.db')
    config = Config()  # Keep Alembic's fileConfig from replacing pytest's log capture.
    config.set_main_option('script_location', str(root / 'migrations'))
    command.upgrade(config, '95948f84f255')
    engine = create_engine(f'sqlite:///{tmp_path}/migrate.db')
    if created_first:
        metadata.create_all(engine)
    command.upgrade(config, 'head')
    assert inspect(engine).has_table('initiatives')
    with engine.connect() as conn:
        assert conn.execute(text('select version_num from alembic_version')).scalar_one() == 'c910a21d8f04'
    engine.dispose()
