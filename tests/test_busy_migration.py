"""Busy-mode schema upgrades preserve legacy rows and converge with fresh installs."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

LEGACY = "fa" + "ther"
PREVIOUS = "c319e82f6a91"
ROW = dict(id=7, slug="memo", version=1, status="review", facts_json='{"topic":"Queues"}',
           document_path="fixture/document.docx", report_json='{"findings":[]}', note="review me",
           created_at="2026-09-20", decided_at=None)


def _database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'busy-migration.db'}"
    monkeypatch.setenv("ALEMBIC_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    return config, create_engine(url)


def _legacy_row(engine):
    with engine.begin() as conn:
        for table in ("busy_tasks", f"{LEGACY}_tasks"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text(f"""CREATE TABLE {LEGACY}_tasks (
            id INTEGER PRIMARY KEY, slug TEXT NOT NULL, version INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL, facts_json TEXT NOT NULL, document_path TEXT NOT NULL,
            report_json TEXT NOT NULL, note TEXT NOT NULL, created_at VARCHAR(64) NOT NULL,
            decided_at VARCHAR(64),
            CONSTRAINT {LEGACY}_task_status CHECK (status IN ('review', 'approved', 'changes_requested'))
        )"""))
        conn.execute(text(f"INSERT INTO {LEGACY}_tasks ({', '.join(ROW)}) "
                          f"VALUES ({', '.join(':' + key for key in ROW)})"), ROW)


def test_upgrade_keeps_legacy_row_and_renames_constraint(tmp_path, monkeypatch):
    config, engine = _database(tmp_path, monkeypatch)
    try:
        command.upgrade(config, PREVIOUS)
        _legacy_row(engine)
        command.upgrade(config, "head")
        assert f"{LEGACY}_tasks" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert dict(conn.execute(text("SELECT * FROM busy_tasks")).mappings().one()) == ROW
        assert {c['name'] for c in inspect(engine).get_check_constraints('busy_tasks')} == {"busy_task_status"}
        # The rewritten predecessor already describes the new schema, so downgrading
        # this compatibility revision must not bring back the legacy name.
        command.downgrade(config, PREVIOUS)
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert dict(conn.execute(text("SELECT * FROM busy_tasks")).mappings().one()) == ROW
    finally:
        engine.dispose()


def test_fresh_upgrade_and_repeated_upgrade_keep_new_schema(tmp_path, monkeypatch):
    config, engine = _database(tmp_path, monkeypatch)
    try:
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert 'busy_tasks' in inspect(engine).get_table_names()
        assert f"{LEGACY}_tasks" not in inspect(engine).get_table_names()
        assert {c['name'] for c in inspect(engine).get_check_constraints('busy_tasks')} == {"busy_task_status"}
    finally:
        engine.dispose()


def test_collision_refuses_without_losing_either_table(tmp_path, monkeypatch):
    config, engine = _database(tmp_path, monkeypatch)
    try:
        command.upgrade(config, PREVIOUS)
        _legacy_row(engine)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE TABLE busy_tasks AS SELECT * FROM {LEGACY}_tasks"))
        with pytest.raises(RuntimeError, match="Both"):
            command.upgrade(config, "head")
        with engine.connect() as conn:
            for table in ("busy_tasks", f"{LEGACY}_tasks"):
                assert dict(conn.execute(text(f"SELECT * FROM {table}")).mappings().one()) == ROW
    finally:
        engine.dispose()
