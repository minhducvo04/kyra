"""Learning reels / book summaries + spaced repetition, per
docs/agentic-roadmap.md job #7.

Kyra doesn't need a tool to *write* a summary - that's just her answering
in text, the same as any other question (routes to Claude, per the
benchmark's reasoning-quality gap). What needs a tool is the bookkeeping:
saving a topic + the key takeaway for later, and scheduling when to
resurface it. When the router sends a "remember/save this for review"
turn to the tool path, Claude composes the summary itself and passes it
as a tool argument - LearningStore only persists and schedules, it never
generates content, same division of labor as the reminders tools.

Spaced repetition, not a fixed reminder: each item gets reviewed at 1,
then 3, then 7 days after the last successful review (docs/agentic-
roadmap.md's spec, the same shape Anki uses) - a wrong/forgotten review
resets back to the 1-day step rather than advancing. Reward system starts
as a plain streak count Kyra can mention conversationally, deliberately
not gamified further yet (see the roadmap doc for why).
"""
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from companion.paths import DATA_DIR
from companion.tools import Tool

DB_PATH = DATA_DIR / "learning.db"

# Day offsets from the last review - index by review_count, capped at the
# last entry once you're past it (i.e. every successful review after the
# third just repeats the 7-day cadence).
REVIEW_INTERVALS_DAYS = [1, 3, 7]


@dataclass
class LearningItem:
    id: int
    topic: str
    summary: str
    key_takeaway: str
    created_at: str
    next_review_at: str
    review_count: int


class LearningStore:
    def __init__(self, path: Path | str = DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS learning_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                key_takeaway TEXT NOT NULL,
                created_at TEXT NOT NULL,
                next_review_at TEXT NOT NULL,
                review_count INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS learning_streak (
                id INTEGER PRIMARY KEY CHECK (id = 0),
                streak_days INTEGER NOT NULL DEFAULT 0,
                last_review_date TEXT
            )"""
        )
        self._conn.execute("INSERT OR IGNORE INTO learning_streak (id, streak_days) VALUES (0, 0)")
        self._conn.commit()

    def add(self, topic: str, summary: str, key_takeaway: str) -> LearningItem:
        now = datetime.now(UTC)
        next_review = (now + timedelta(days=REVIEW_INTERVALS_DAYS[0])).isoformat()
        cur = self._conn.execute(
            "INSERT INTO learning_items (topic, summary, key_takeaway, created_at, next_review_at, review_count) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (topic, summary, key_takeaway, now.isoformat(), next_review),
        )
        self._conn.commit()
        return LearningItem(
            id=cur.lastrowid, topic=topic, summary=summary, key_takeaway=key_takeaway,
            created_at=now.isoformat(), next_review_at=next_review, review_count=0,
        )

    def due(self) -> list[LearningItem]:
        now = datetime.now(UTC).isoformat()
        rows = self._conn.execute(
            "SELECT id, topic, summary, key_takeaway, created_at, next_review_at, review_count "
            "FROM learning_items WHERE next_review_at <= ? ORDER BY next_review_at",
            (now,),
        ).fetchall()
        return [LearningItem(*r) for r in rows]

    def mark_reviewed(self, item_id: int, remembered: bool) -> dict | None:
        row = self._conn.execute(
            "SELECT review_count FROM learning_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return None
        review_count = row[0]
        new_count = review_count + 1 if remembered else 0
        idx = min(new_count, len(REVIEW_INTERVALS_DAYS) - 1)
        next_review = (datetime.now(UTC) + timedelta(days=REVIEW_INTERVALS_DAYS[idx])).isoformat()
        self._conn.execute(
            "UPDATE learning_items SET review_count = ?, next_review_at = ? WHERE id = ?",
            (new_count, next_review, item_id),
        )
        self._conn.commit()
        streak = self._bump_streak() if remembered else self._current_streak()
        return {"review_count": new_count, "next_review_at": next_review, "streak_days": streak}

    def _current_streak(self) -> int:
        row = self._conn.execute("SELECT streak_days FROM learning_streak WHERE id = 0").fetchone()
        return row[0] if row else 0

    def _bump_streak(self) -> int:
        """One review counted per calendar day - reviewing 5 things today
        doesn't inflate the streak 5x, and missing a day resets it."""
        today = date.today().isoformat()
        row = self._conn.execute(
            "SELECT streak_days, last_review_date FROM learning_streak WHERE id = 0"
        ).fetchone()
        streak_days, last_date = row
        if last_date == today:
            new_streak = streak_days  # already counted today
        elif last_date == (date.today() - timedelta(days=1)).isoformat():
            new_streak = streak_days + 1  # consecutive day
        else:
            new_streak = 1  # gap, or first ever review - restart
        self._conn.execute(
            "UPDATE learning_streak SET streak_days = ?, last_review_date = ? WHERE id = 0",
            (new_streak, today),
        )
        self._conn.commit()
        return new_streak


class SaveLearningItemTool(Tool):
    name = "save_learning_item"
    description = (
        "Save a topic/book summary Duc just learned, with its key takeaway, so it gets "
        "resurfaced for spaced-repetition review later (1 day, then 3, then 7 days out). "
        "Write the summary and key_takeaway yourself based on what you actually explained - "
        "use this only when Duc explicitly asks to remember/save/track something for review, "
        "not after every explanation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "What this is about, a few words"},
            "summary": {"type": "string", "description": "The structured summary you gave, key ideas"},
            "key_takeaway": {"type": "string", "description": "The single most important, actionable point"},
        },
        "required": ["topic", "summary", "key_takeaway"],
    }

    def __init__(self, store: LearningStore):
        self._store = store

    def run(self, topic: str, summary: str, key_takeaway: str) -> dict:
        return asdict(self._store.add(topic, summary, key_takeaway))


class DueReviewsTool(Tool):
    name = "due_learning_reviews"
    description = "List learning items due for review right now. Use when Duc asks what he should review today."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: LearningStore):
        self._store = store

    def run(self) -> dict:
        return {"due": [asdict(i) for i in self._store.due()]}


class MarkReviewedTool(Tool):
    name = "mark_learning_reviewed"
    description = (
        "Mark a learning item as reviewed, by its id (from due_learning_reviews). "
        "Set remembered=true if Duc actually recalled it, false if he didn't - "
        "forgetting resets it back to a 1-day review instead of advancing."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "remembered": {"type": "boolean"},
        },
        "required": ["id", "remembered"],
    }

    def __init__(self, store: LearningStore):
        self._store = store

    def run(self, id: int, remembered: bool) -> dict:
        result = self._store.mark_reviewed(id, remembered)
        return result if result is not None else {"error": f"no learning item with id {id}"}


def learning_tools(store: LearningStore | None = None) -> list[Tool]:
    store = store or LearningStore()
    return [SaveLearningItemTool(store), DueReviewsTool(store), MarkReviewedTool(store)]
