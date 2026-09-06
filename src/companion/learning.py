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
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, insert, select, update

from companion.db import engine_for_store
from companion.paths import DATA_DIR
from companion.schema import learning_items as T
from companion.schema import learning_streak as S
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
    def __init__(self, path: Path | str | None = None, *, engine: Engine | None = None):
        self._engine = engine or engine_for_store(DB_PATH, path)
        with self._engine.begin() as conn:
            if conn.execute(select(S.c.id).where(S.c.id == 0)).first() is None:
                conn.execute(insert(S).values(id=0, streak_days=0))

    def add(self, topic: str, summary: str, key_takeaway: str) -> LearningItem:
        now = datetime.now(UTC)
        next_review = (now + timedelta(days=REVIEW_INTERVALS_DAYS[0])).isoformat()
        with self._engine.begin() as conn:
            res = conn.execute(insert(T).values(
                topic=topic, summary=summary, key_takeaway=key_takeaway,
                created_at=now.isoformat(), next_review_at=next_review, review_count=0,
            ))
        return LearningItem(
            id=res.inserted_primary_key[0], topic=topic, summary=summary, key_takeaway=key_takeaway,
            created_at=now.isoformat(), next_review_at=next_review, review_count=0,
        )

    def due(self) -> list[LearningItem]:
        now = datetime.now(UTC).isoformat()
        with self._engine.connect() as conn:
            rows = conn.execute(select(T).where(T.c.next_review_at <= now).order_by(T.c.next_review_at)).all()
        return [LearningItem(**r._mapping) for r in rows]

    def mark_reviewed(self, item_id: int, remembered: bool) -> dict | None:
        with self._engine.begin() as conn:
            row = conn.execute(select(T.c.review_count).where(T.c.id == item_id)).first()
            if row is None:
                return None
            new_count = row.review_count + 1 if remembered else 0
            idx = min(new_count, len(REVIEW_INTERVALS_DAYS) - 1)
            next_review = (datetime.now(UTC) + timedelta(days=REVIEW_INTERVALS_DAYS[idx])).isoformat()
            conn.execute(update(T).where(T.c.id == item_id).values(review_count=new_count, next_review_at=next_review))
            streak = self._bump_streak(conn) if remembered else self._current_streak(conn)
        return {"review_count": new_count, "next_review_at": next_review, "streak_days": streak}

    def _current_streak(self, conn) -> int:
        row = conn.execute(select(S.c.streak_days).where(S.c.id == 0)).first()
        return row.streak_days if row else 0

    def _bump_streak(self, conn) -> int:
        """One review counted per calendar day - reviewing 5 things today
        doesn't inflate the streak 5x, and missing a day resets it."""
        today = date.today().isoformat()
        row = conn.execute(select(S.c.streak_days, S.c.last_review_date).where(S.c.id == 0)).first()
        streak_days, last_date = row.streak_days, row.last_review_date
        if last_date == today:
            new_streak = streak_days  # already counted today
        elif last_date == (date.today() - timedelta(days=1)).isoformat():
            new_streak = streak_days + 1  # consecutive day
        else:
            new_streak = 1  # gap, or first ever review - restart
        conn.execute(update(S).where(S.c.id == 0).values(streak_days=new_streak, last_review_date=today))
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
