"""Both shipped schema histories converge without losing records or receipts."""
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text

from companion.schema import metadata


@pytest.mark.parametrize("revision", ["base", "95948f84f255", "b721d430a9ef", "c910a21d8f04"])
@pytest.mark.parametrize("startup_first", [False, True])
def test_upgrade_preserves_both_histories(tmp_path, monkeypatch, revision, startup_first):
    url = f"sqlite:///{tmp_path / 'integrated.db'}"
    monkeypatch.setenv("ALEMBIC_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    # An unversioned database is migrated before application startup. Existing
    # installations may have created additive tables before the upgrade runs.
    command.upgrade(config, revision)
    engine = create_engine(url)
    if startup_first and revision != "base":
        metadata.create_all(engine)
    fixtures = {
        "reminders": dict(id=1, text="Review queue trace", due_at=None, created_at="2026-09-10", done=0),
        "learning_items": dict(id=1, topic="Queues", summary="Baseline", key_takeaway="Capacity matters",
                               created_at="2026-09-10", next_review_at="2026-09-11", review_count=0),
        "checkpoint_revisions": dict(id="00000000-0000-0000-0000-000000000001", revision=1,
                                     task="Queues", last_result="Baseline", next_action="Read trace",
                                     references="", updated_at="2026-09-10"),
        "learning_requests": dict(request_id="00000000-0000-0000-0000-000000000002", result='{"id":1}'),
        "initiatives": dict(id="fixture", payload='{"title":"Review trace"}', status="accepted",
                            reason=None, reminder_id=1, last_seen="2026-09-10"),
        "initiative_reminder_receipts": dict(initiative_id="fixture", reminder_id=1),
        "initiative_snapshot": dict(id=0, day="2026-09-10"),
    }
    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for name, row in fixtures.items():
            if name in existing:
                conn.execute(metadata.tables[name].insert().values(**row))
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            ScriptDirectory.from_config(config).get_current_head()
        )
        for name, row in fixtures.items():
            if name in existing:
                assert dict(conn.execute(select(metadata.tables[name])).one()._mapping) == row
    engine.dispose()
