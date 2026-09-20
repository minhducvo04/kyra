"""Stage A5 of docs/plans/2026-09-18-whole-system-phase-1-final.md: some stores may exist only on the owner's own
machine, and saying so in a docstring is not a control.

`engine_for_store` prefers `DATABASE_URL`, which is the shared-deployment path, so a health or ledger table built on
it would follow the configuration into a cloud database (Codex critique C05). The capability here is checked on the
EFFECTIVE destination: a default path, an explicit URL and an injected engine all go through the same validator
(sign-off C05). The same policy covers a sink that already stores tool text: tool-run receipts (sign-off S03).

No store uses the capability yet; health ingestion (stage C) is its first caller. These tests pin it first.
"""
import hashlib
import json

import pytest
from sqlalchemy import create_engine

from companion.db import LocalOnlyError, is_local_personal, local_personal_engine
from companion.settings import Settings
from companion.tool_runs import ToolRunStore
from companion.tools import Tool, ToolRegistry

REMOTE = "postgresql+psycopg://user:pw@db.example.com/kyra"


def _settings(monkeypatch, tmp_path, **env) -> Settings:
    base = {"KYRA_DATA_DIR": str(tmp_path), "KYRA_OWNER_MACHINE": "true", "KYRA_TENANT": "personal", "DATABASE_URL": ""}
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_owner_machine_is_an_explicit_setting_and_defaults_to_false(monkeypatch, tmp_path):
    monkeypatch.delenv("KYRA_OWNER_MACHINE", raising=False)
    monkeypatch.setenv("KYRA_DATA_DIR", str(tmp_path))
    assert Settings().owner_machine is False  # a container never qualifies by accident


def test_the_default_path_opens_on_the_owner_machine(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    engine = local_personal_engine(tmp_path / "health.db", settings=settings)
    assert engine.url.get_backend_name() == "sqlite" and str(tmp_path) in str(engine.url.database)


@pytest.mark.parametrize("env", [
    {"KYRA_OWNER_MACHINE": "false"},                       # a host nobody declared
    {"DATABASE_URL": REMOTE},                              # the shared-deployment path
    {"KYRA_TENANT": "busy"},                             # another person's tenant
])
def test_it_refuses_when_the_environment_is_not_the_owner_s_own(monkeypatch, tmp_path, env):
    if env.get("KYRA_TENANT") == "busy":
        env = {**env, "KYRA_DATA_DIR": str(tmp_path / "elsewhere")}
    settings = _settings(monkeypatch, tmp_path, **env)
    with pytest.raises(LocalOnlyError):
        local_personal_engine(settings.data_dir / "health.db", settings=settings)


def test_a_local_sqlite_database_url_is_allowed_but_only_under_the_data_dir(monkeypatch, tmp_path):
    inside = _settings(monkeypatch, tmp_path, DATABASE_URL=f"sqlite:///{tmp_path}/shared.db")
    assert local_personal_engine(tmp_path / "health.db", settings=inside).url.get_backend_name() == "sqlite"
    outside = _settings(monkeypatch, tmp_path, DATABASE_URL="sqlite:////tmp/kyra-somewhere-else.db")
    with pytest.raises(LocalOnlyError):
        local_personal_engine(tmp_path / "health.db", settings=outside)


def test_an_explicit_url_and_an_injected_engine_are_validated_too(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    with pytest.raises(LocalOnlyError):
        local_personal_engine(tmp_path / "health.db", explicit=REMOTE, settings=settings)
    with pytest.raises(LocalOnlyError):
        local_personal_engine(tmp_path / "health.db", explicit="/tmp/kyra-outside-the-data-dir.db", settings=settings)
    remote_engine = create_engine(REMOTE)  # never connects; only its URL is read
    with pytest.raises(LocalOnlyError):
        local_personal_engine(tmp_path / "health.db", engine=remote_engine, settings=settings)
    local_engine = create_engine(f"sqlite:///{tmp_path}/injected.db")
    assert local_personal_engine(tmp_path / "health.db", engine=local_engine, settings=settings) is local_engine
    assert is_local_personal(local_engine, settings) and not is_local_personal(remote_engine, settings)


def test_the_error_names_the_reason_and_never_the_credentials_in_a_url(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path, DATABASE_URL=REMOTE)
    with pytest.raises(LocalOnlyError) as excinfo:
        local_personal_engine(tmp_path / "health.db", settings=settings)
    assert "pw" not in str(excinfo.value) and "user" not in str(excinfo.value)


# --- S03: receipts already store tool text --------------------------------------------------------------


class _Intake(Tool):
    sensitive = True
    name = "log_intake"
    description = "A tool whose arguments and result are T2."
    input_schema = {"type": "object", "properties": {"note": {"type": "string"}}}

    def run(self, note: str = "") -> dict:
        if note == "boom":
            raise RuntimeError(f"failed while handling {note} with private detail 4471")
        return {"saved": note}


class _Plain(_Intake):
    sensitive = False
    name = "plain_tool"


class _Sink:
    def __init__(self, local_personal: bool):
        self.local_personal = local_personal
        self.rows: list[dict] = []

    def record(self, tool, args, **fields):
        self.rows.append({"tool": tool, "args": args, **fields})
        return len(self.rows)


def test_a_sensitive_tool_s_content_never_reaches_a_sink_that_is_not_local_personal():
    sink = _Sink(local_personal=False)
    registry = ToolRegistry([_Intake(), _Plain()], audit=sink)
    registry.run("log_intake", note="two glasses of wine at nine")
    with pytest.raises(RuntimeError):
        registry.run("log_intake", note="boom")
    registry.run("plain_tool", note="ordinary")
    text = json.dumps(sink.rows)
    assert "wine" not in text and "4471" not in text and "boom" not in text
    first = sink.rows[0]
    expected = hashlib.sha256(json.dumps({"note": "two glasses of wine at nine"}, sort_keys=True).encode()).hexdigest()
    assert first["args"] == {"redacted": True, "sha256": expected} and first["ok"] is True
    assert sink.rows[1]["ok"] is False and sink.rows[1]["error"] == "RuntimeError"
    assert sink.rows[2]["args"] == {"note": "ordinary"}  # an ordinary tool is recorded as before


def test_a_local_personal_sink_keeps_the_content(monkeypatch, tmp_path):
    sink = _Sink(local_personal=True)
    ToolRegistry([_Intake()], audit=sink).run("log_intake", note="two coffees")
    assert sink.rows[0]["args"] == {"note": "two coffees"}


def test_the_real_tool_run_store_reports_whether_it_is_local_personal(monkeypatch, tmp_path):
    settings = _settings(monkeypatch, tmp_path)
    local = ToolRunStore(engine=create_engine(f"sqlite:///{tmp_path}/tool_runs.db"))
    assert local.is_local_personal(settings) is True
    assert ToolRunStore(engine=create_engine(REMOTE)).is_local_personal(settings) is False
    assert local.is_local_personal(_settings(monkeypatch, tmp_path, KYRA_OWNER_MACHINE="false")) is False
