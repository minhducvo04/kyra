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

# v2 slice 3: the job queue. One row per long-running task (a resume fit loop
# runs 1-2 minutes with several model calls and must not sit inside an HTTP
# request). payload/progress/result are JSON text so the table is portable
# across SQLite and Postgres; a worker claims rows by flipping status.
jobs = Table(
    "jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(64), nullable=False),
    Column("status", String(16), nullable=False, server_default="queued"),  # queued|running|done|failed
    Column("payload", Text, nullable=False),
    Column("progress", Text, nullable=False, server_default="[]"),
    Column("result", Text),
    Column("error", Text),
    Column("created_at", String(64), nullable=False),
    Column("started_at", String(64)),
    Column("finished_at", String(64)),
)

outreach_contacts = Table(
    "outreach_contacts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("company", Text, nullable=False),
    Column("role", Text),
    Column("profile_url", Text),
    Column("relation", Text),
    Column("application_id", Integer),  # job_applications.id, no FK: stores may live in separate SQLite files
    Column("note", Text),
    Column("follow_up", Text),
    Column("status", String(32), nullable=False, server_default="drafted"),
    Column("sent_at", String(64)),
    Column("follow_up_at", String(64)),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)
