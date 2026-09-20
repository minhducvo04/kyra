"""Add personal working-loop receipts and bound reviews.

Revision ID: e915b07c2d31
Revises: d210a93e7b61
"""
from alembic import op
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text

revision = "e915b07c2d31"
down_revision = "d210a93e7b61"
branch_labels = None
depends_on = None

metadata = MetaData()
loop_runs = Table(
    "loop_runs", metadata,
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


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    loop_reviews.drop(op.get_bind())
    loop_runs.drop(op.get_bind())
