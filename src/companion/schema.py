"""Table definitions for the relational stores (SQLAlchemy Core).

These mirror the DDL the v1 stores created by hand with sqlite3, column
for column, so an existing data/*.db keeps working with no migration.
Timestamps stay ISO-8601 strings (as v1 wrote them) rather than
TIMESTAMP columns - changing that is a data migration, tracked for a
later slice. Alembic (migrations/) owns schema changes from here on.
"""
from sqlalchemy import Boolean, CheckConstraint, CheckConstraint, Column, Float, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()

father_tasks = Table(
    "father_tasks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slug", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("facts_json", Text, nullable=False),
    Column("document_path", Text, nullable=False),
    Column("report_json", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("decided_at", String(64)),
    CheckConstraint("status IN ('review', 'approved', 'changes_requested')", name="father_task_status"),
)

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

# A receipt and its reminder commit together, even when initiatives use another database.
initiative_reminder_receipts = Table(
    "initiative_reminder_receipts", metadata,
    Column("initiative_id", String(64), primary_key=True),
    Column("reminder_id", Integer, nullable=False),
)

initiatives = Table(
    "initiatives", metadata,
    Column("id", String(64), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("reason", Text),
    Column("reminder_id", Integer),
    Column("last_seen", String(10), nullable=False),
    CheckConstraint("status IN ('proposed', 'accepting', 'accepted', 'dismissed', 'expired')", name="initiative_status"),
)

# One row serializes daily publication, including an empty day.
initiative_snapshot = Table(
    "initiative_snapshot", metadata,
    Column("id", Integer, primary_key=True),
    Column("day", String(10), nullable=False),
)

# Content-free execution receipts. Prompts and replies stay in private artifact files.
loop_runs = Table(
    "loop_runs", metadata,
    Column("tier", String(16), nullable=False, server_default="work"),
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("project", String(160), nullable=False),
    Column("topic", String(160), nullable=False),
    Column("choice_key", String(64), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("developer", String(64), nullable=False),
    Column("host", String(64), nullable=False),
    Column("method", String(16), nullable=False),
    Column("requested_model", String(160), nullable=False),
    Column("served_model", String(160)),
    Column("effort", String(32)),
    Column("status", String(32), nullable=False),
    Column("provider_session_id", String(160)),
    Column("provider_request_id", String(160)),
    Column("input_sha256", String(64), nullable=False),
    Column("output_sha256", String(64)),
    Column("artifact_dir", Text, nullable=False),
    Column("usage", Text),
    Column("model_usage", Text),
    Column("error", String(160)),
    Column("policy_version", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("started_at", String(64)),
    Column("finished_at", String(64)),
    Column("review_subject_id", Integer),
    Column("review_subject_sha256", String(64)),
    Column("continued_from_run_id", Integer),
    Column("requested_session_id", String(160)),
    Column("review_context", Text),
    UniqueConstraint("continued_from_run_id", name="loop_one_child_per_parent"),
    CheckConstraint("status IN ('queued','dispatching','done','failed','unreconciled','mismatch')", name="loop_run_status"),
)

loop_reviews = Table(
    "loop_reviews", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("subject_run_id", Integer, nullable=False),
    Column("reviewer_run_id", Integer, nullable=False),
    Column("verdict", String(32), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    CheckConstraint("verdict IN ('approve','reject','comment')", name="loop_review_verdict"),
)

# Owner decisions are separate from model comments; neither overwrites execution evidence.
loop_review_decisions = Table(
    "loop_review_decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("review_id", Integer, nullable=False),
    Column("subject_run_id", Integer, nullable=False),
    Column("decision", String(16), nullable=False),
    Column("artifact_sha256", String(64), nullable=False),
    Column("reviewer_output_sha256", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    CheckConstraint("decision IN ('approve','reject')", name="loop_owner_decision"),
)

loop_reconciliations = Table(
    "loop_reconciliations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("run_id", Integer, nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("note_sha256", String(64), nullable=False),
    Column("note_path", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    CheckConstraint("outcome IN ('nothing_happened','provider_processed')", name="loop_declared_outcome"),
)


loop_assignments = Table(
    "loop_assignments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("code", String(160), nullable=False),
    Column("title", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("allowed_files", Text, nullable=False),
    Column("acceptance", Text, nullable=False),
    Column("tier", String(16), nullable=False, server_default="work"),
    Column("status", String(16), nullable=False, server_default="assigned"),
    Column("builder_run_id", Integer),
    Column("result_sha256", String(64)),
    Column("commit_hash", String(40)),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    UniqueConstraint("owner", "code", name="loop_assignment_owner_code"),
    CheckConstraint("status IN ('assigned','built','reviewed','committed')", name="loop_assignment_status"),
    CheckConstraint("tier IN ('casual','work','life_changing')", name="loop_assignment_tier"),


# Arguments and summaries are private runtime state, shared by all front doors.
tool_runs = Table(
    "tool_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tool", String(128), nullable=False),
    Column("args", Text, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("summary", String(300), nullable=False),
    Column("error", Text),
    Column("started_at", String(64), nullable=False),
    Column("duration_ms", Float, nullable=False),
)
