"""Reminder + planner storage, and the tools that expose it to Kyra.

Local-only by design (see docs/agentic-roadmap.md, job #2) - pure CRUD,
no reasoning needed, so this is exactly the kind of turn a TurnRouter
should send to the local backend rather than spending a Claude call on it.
"""
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from companion.paths import DATA_DIR
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
    def __init__(self, path: Path | str = DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                due_at TEXT,
                created_at TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self._conn.commit()

    def add(self, text: str, due_at: str | None = None) -> Reminder:
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "INSERT INTO reminders (text, due_at, created_at, done) VALUES (?, ?, ?, 0)",
            (text, due_at, now),
        )
        self._conn.commit()
        return Reminder(id=cur.lastrowid, text=text, due_at=due_at, created_at=now, done=False)

    def list(self, include_done: bool = False) -> list[Reminder]:
        q = "SELECT id, text, due_at, created_at, done FROM reminders"
        if not include_done:
            q += " WHERE done = 0"
        q += " ORDER BY (due_at IS NULL), due_at, created_at"
        rows = self._conn.execute(q).fetchall()
        return [Reminder(id=r[0], text=r[1], due_at=r[2], created_at=r[3], done=bool(r[4])) for r in rows]

    def complete(self, reminder_id: int) -> bool:
        cur = self._conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def snooze(self, reminder_id: int, new_due_at: str) -> bool:
        cur = self._conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (new_due_at, reminder_id))
        self._conn.commit()
        return cur.rowcount > 0

    def due_now(self) -> "list[Reminder]":
        """Not-done reminders due at or before now - for a proactive nudge
        when a session starts, rather than a real push notification (that
        needs a background scheduler - see docs/agentic-roadmap.md)."""
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            "SELECT id, text, due_at, created_at, done FROM reminders "
            "WHERE done = 0 AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at",
            (now,),
        ).fetchall()
        return [Reminder(id=r[0], text=r[1], due_at=r[2], created_at=r[3], done=bool(r[4])) for r in rows]


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
