"""Private, lazy audit store shared by chat, voice and the console."""
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property

from sqlalchemy import Engine, insert, select

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.schema import tool_runs


@dataclass
class ToolRun:
    id: int
    tool: str
    args: dict
    ok: bool
    summary: str
    error: str | None
    started_at: str
    duration_ms: float


class ToolRunStore:
    def __init__(self, engine: Engine | None = None):
        self._engine = engine

    @cached_property
    def engine(self) -> Engine:
        if self._engine is not None:
            tool_runs.create(self._engine, checkfirst=True)
            return self._engine
        return engine_for_store(DATA_DIR / "tool_runs.db")

    def record(self, tool: str, args: dict, *, ok: bool, summary: str, error: str | None,
               duration_ms: float, started_at: str | None = None) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(insert(tool_runs).values(
                tool=tool, args=json.dumps(args), ok=ok, summary=summary[:300], error=error,
                started_at=started_at or datetime.now(UTC).isoformat(), duration_ms=duration_ms,
            ))
            return result.inserted_primary_key[0]

    def list(self, limit: int = 50) -> list[ToolRun]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tool_runs).order_by(tool_runs.c.id.desc()).limit(max(0, limit)))
            return [ToolRun(**{**row, "args": json.loads(row["args"])}) for row in rows.mappings()]
