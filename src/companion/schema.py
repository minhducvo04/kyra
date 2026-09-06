"""Table definitions for the relational stores (SQLAlchemy Core).

These mirror the DDL the v1 stores created by hand with sqlite3, column
for column, so an existing data/*.db keeps working with no migration.
Timestamps stay ISO-8601 strings (as v1 wrote them) rather than
TIMESTAMP columns - changing that is a data migration, tracked for a
later slice. Alembic (migrations/) owns schema changes from here on.
"""
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text

metadata = MetaData()

reminders = Table(
    "reminders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("text", Text, nullable=False),
    Column("due_at", String(64)),
    Column("created_at", String(64), nullable=False),
    Column("done", Integer, nullable=False, server_default="0"),
)

learning_items = Table(
    "learning_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("topic", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("key_takeaway", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("next_review_at", String(64), nullable=False),
    Column("review_count", Integer, nullable=False, server_default="0"),
)

learning_streak = Table(
    "learning_streak", metadata,
    Column("id", Integer, primary_key=True),
    Column("streak_days", Integer, nullable=False, server_default="0"),
    Column("last_review_date", String(32)),
    CheckConstraint("id = 0", name="single_row"),
)

job_applications = Table(
    "job_applications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("link", Text),
    Column("status", String(32), nullable=False, server_default="applied"),
    Column("notes", Text),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
