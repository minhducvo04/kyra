"""Reminder + planner storage, and the tools that expose it to Kyra.

Local-only by design (see docs/agentic-roadmap.md, job #2) - pure CRUD,
no reasoning needed, so this is exactly the kind of turn a TurnRouter
should send to the local backend rather than spending a Claude call on it.
"""
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.schema import initiative_reminder_receipts as RECEIPTS
from companion.schema import reminders as T
from companion.tools import Tool

DB_PATH = DATA_DIR / "reminders.db"


@dataclass
class Reminder:
    id: int
    text: str
    due_at: str | None
    created_at: str
    done: bool


class RemindersStore:
    def __init__(self, path: Path | str | None = None, *, engine: Engine | None = None):
        self._engine = engine or engine_for_store(DB_PATH, path)

    def add(self, text: str, due_at: str | None = None) -> Reminder:
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            res = conn.execute(insert(T).values(text=text, due_at=due_at, created_at=now, done=0))
        return Reminder(id=res.inserted_primary_key[0], text=text, due_at=due_at, created_at=now, done=False)

    def add_once(self, initiative_id: str, text: str) -> Reminder:
        """The receipt uniqueness constraint rolls back a duplicate reminder insert."""
        now = datetime.now(UTC).isoformat()
        try:
            with self._engine.begin() as conn:
                previous = conn.execute(select(RECEIPTS.c.reminder_id).where(RECEIPTS.c.initiative_id == initiative_id)).scalar_one_or_none()
                if previous is None:
                    result = conn.execute(insert(T).values(text=text, created_at=now, done=0))
                    previous = result.inserted_primary_key[0]
                    conn.execute(insert(RECEIPTS).values(initiative_id=initiative_id, reminder_id=previous))
        except IntegrityError:
            with self._engine.connect() as conn:
                previous = conn.execute(select(RECEIPTS.c.reminder_id).where(RECEIPTS.c.initiative_id == initiative_id)).scalar_one()
        with self._engine.connect() as conn:
            row = conn.execute(select(T).where(T.c.id == previous)).one()
        return Reminder(id=row.id, text=row.text, due_at=row.due_at, created_at=row.created_at, done=bool(row.done))

    def list(self, include_done: bool = False) -> list[Reminder]:
        q = select(T)
        if not include_done:
            q = q.where(T.c.done == 0)
        q = q.order_by(T.c.due_at.is_(None), T.c.due_at, T.c.created_at)
        with self._engine.connect() as conn:
            rows = conn.execute(q).all()
        return [Reminder(id=r.id, text=r.text, due_at=r.due_at, created_at=r.created_at, done=bool(r.done)) for r in rows]

    def complete(self, reminder_id: int) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(update(T).where(T.c.id == reminder_id).values(done=1)).rowcount > 0

    def snooze(self, reminder_id: int, new_due_at: str) -> bool:
        with self._engine.begin() as conn:
            return conn.execute(update(T).where(T.c.id == reminder_id).values(due_at=new_due_at)).rowcount > 0

    def due_now(self) -> "list[Reminder]":
        """Not-done reminders due at or before now - for a proactive nudge
        when a session starts (the 05:00 digest uses this)."""
        now = datetime.now(UTC).isoformat()
        q = select(T).where(T.c.done == 0, T.c.due_at.is_not(None), T.c.due_at <= now).order_by(T.c.due_at)
        with self._engine.connect() as conn:
            rows = conn.execute(q).all()
        return [Reminder(id=r.id, text=r.text, due_at=r.due_at, created_at=r.created_at, done=bool(r.done)) for r in rows]


class AddReminderTool(Tool):
    name = "add_reminder"
    description = (
        "Add a new reminder or task for Duc, optionally with a due date/time. "
        "Use this whenever he asks you to remember to do something or not forget something."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remember, in a few words"},
            "due_at": {
                "type": "string",
                "description": "ISO 8601 datetime (e.g. 2026-09-02T09:00:00+00:00). Omit if there's no specific time.",
            },
        },
        "required": ["text"],
    }

    def __init__(self, store: RemindersStore):
        self._store = store

    def run(self, text: str, due_at: str | None = None) -> dict:
        r = self._store.add(text, due_at)
        return asdict(r)


class ListRemindersTool(Tool):
    name = "list_reminders"
    description = "List Duc's reminders/tasks. By default only shows what's not done yet."
    input_schema = {
        "type": "object",
        "properties": {"include_done": {"type": "boolean", "description": "Include already-completed items too"}},
        "required": [],
    }

    def __init__(self, store: RemindersStore):
        self._store = store

    def run(self, include_done: bool = False) -> dict:
        return {"reminders": [asdict(r) for r in self._store.list(include_done=include_done)]}


class CompleteReminderTool(Tool):
    name = "complete_reminder"
    description = "Mark a reminder as done, by its id (from list_reminders)."
    input_schema = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}

    def __init__(self, store: RemindersStore):
        self._store = store

    def run(self, id: int) -> dict:
        return {"ok": self._store.complete(id)}


class SnoozeReminderTool(Tool):
    name = "snooze_reminder"
    description = "Change a reminder's due date/time, by its id (from list_reminders)."
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "due_at": {"type": "string", "description": "New ISO 8601 datetime"},
        },
        "required": ["id", "due_at"],
    }

    def __init__(self, store: RemindersStore):
        self._store = store

    def run(self, id: int, due_at: str) -> dict:
        return {"ok": self._store.snooze(id, due_at)}


def reminder_tools(store: RemindersStore | None = None) -> list[Tool]:
    """All four reminder tools, sharing one store - the usual way to wire
    this into a ToolRegistry: `registry = ToolRegistry(reminder_tools())`."""
    store = store or RemindersStore()
    return [AddReminderTool(store), ListRemindersTool(store), CompleteReminderTool(store), SnoozeReminderTool(store)]
