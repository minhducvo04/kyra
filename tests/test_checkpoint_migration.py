"""The additive migration preserves existing learning records."""
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, insert, select

from companion.schema import learning_items, metadata


def test_upgrade_preserves_learning_and_matches_schema(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    monkeypatch.setenv("ALEMBIC_URL", url)
    config = Config()  # Do not let Alembic replace pytest's logging configuration.
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(config, "95948f84f255")
    engine = create_engine(url)
    existing = dict(id=1, topic="Queues", summary="Summary", key_takeaway="Takeaway",
                    created_at="2026-01-01", next_review_at="2026-01-02", review_count=2)
    with engine.begin() as conn:
        conn.execute(insert(learning_items).values(**existing))
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert dict(conn.execute(select(learning_items)).one()._mapping) == existing
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []
    engine.dispose()


def test_upgrade_after_store_startup_preserves_new_records(tmp_path, monkeypatch):
    from uuid import uuid4

    from companion.checkpoints import CheckpointDraft, DbCheckpointStore
    from companion.learning import LearningStore

    url = f"sqlite:///{tmp_path / 'started.db'}"
    monkeypatch.setenv("ALEMBIC_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(config, "95948f84f255")
    # engine_for_store runs create_all before operators advance Alembic.
    store = DbCheckpointStore(url)
    checkpoint = store.save(uuid4(), CheckpointDraft(revision=0, task="Queues",
        last_result="Baseline", next_action="Disable a node", references=""))
    learning = LearningStore(url)
    request = uuid4()
    item = learning.add("Queues", "Summary", "Takeaway", request_id=request)
    command.upgrade(config, "head")
    assert store.list() == [checkpoint]
    assert learning.add("Queues", "Summary", "Takeaway", request_id=request) == item
    with store._engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []
