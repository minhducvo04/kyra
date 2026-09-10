"""Table definitions for the relational stores (SQLAlchemy Core).

These mirror the DDL the v1 stores created by hand with sqlite3, column
for column, so an existing data/*.db keeps working with no migration.
Timestamps stay ISO-8601 strings (as v1 wrote them) rather than
TIMESTAMP columns - changing that is a data migration, tracked for a
later slice. Alembic (migrations/) owns schema changes from here on.
"""
from sqlalchemy import CheckConstraint, Column, Float, Integer, MetaData, String, Table, Text

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

# Request receipts live beside learning_items, so both inserts commit or neither does.
learning_requests = Table(
    "learning_requests", metadata,
    Column("request_id", String(36), primary_key=True),
    Column("result", Text, nullable=False),
)

# Retain committed revisions to replay a lost response even after another edit.
checkpoint_revisions = Table(
    "checkpoint_revisions", metadata,
    Column("id", String(36), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("task", Text, nullable=False),
    Column("last_result", Text, nullable=False),
    Column("next_action", Text, nullable=False),
    Column("references", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
    CheckConstraint("revision > 0", name="positive_checkpoint_revision"),
)

job_applications = Table(
    "job_applications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("company", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("link", Text),
    Column("status", String(32), nullable=False, server_default="applied"),
    Column("notes", Text),
    # The company-tailored resume autofill should attach for this application.
    # Duc wants one resume per company (its environment, product area, specialisation),
    # so the file lives with the application, not only in the global profile.
    Column("resume_path", Text),
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


# Focus blocks (plan: docs/plans/2026-09-08-attention-environment.md). One row per
# block, carrying the assigned audio condition and the two reaction-time probes, so
# scripts/focus_report.py can ask which condition Duc actually performs better under.
# No biometric column by design: health data stays on the Apple devices (health plan),
# and everything here is either chosen by the planner or typed by Duc.
focus_sessions = Table(
    "focus_sessions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("condition", String(32), nullable=False),
    Column("task", Text, nullable=False, server_default=""),
    Column("planned_minutes", Integer, nullable=False),
    Column("started_at", String(64), nullable=False),
    Column("ended_at", String(64)),
    # A block Duc walked away from: reaped on the next read, never counted in the
    # experiment, so a closed tab cannot silently become a data point.
    Column("abandoned", Integer, nullable=False, server_default="0"),
    Column("probe_start_ms", Float),
    Column("probe_start_lapses", Integer),
    Column("probe_end_ms", Float),
    Column("probe_end_lapses", Integer),
    Column("rating", Integer),
    Column("note", Text),
)
