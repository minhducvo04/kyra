"""Append owner decisions and reconciliation declarations.
Revision ID: f915b18d3e42
Revises: e915b07c2d31
"""
from alembic import op
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text

revision = "f915b18d3e42"
down_revision = "e915b07c2d31"
branch_labels = None
depends_on = None
metadata = MetaData()
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


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    loop_reconciliations.drop(op.get_bind())
    loop_review_decisions.drop(op.get_bind())
