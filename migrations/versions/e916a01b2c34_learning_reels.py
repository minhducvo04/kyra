"""Learning reels, immutable moments and learner attempts.

Revision ID: e916a01b2c34
Revises: d210a93e7b61
"""
import sqlalchemy as sa
from alembic import op

revision = "e916a01b2c34"
down_revision = "d210a93e7b61"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("reel_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("transcript_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False), if_not_exists=True)
    op.create_table("reel_moments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("start_s", sa.Float()), sa.Column("end_s", sa.Float()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('proposed', 'approved', 'rejected')", name="reel_status"), if_not_exists=True)
    op.create_table("reel_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("moment_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("at", sa.String(64), nullable=False), sa.Column("chosen", sa.Text()),
        sa.Column("correct", sa.Integer()),
        sa.Column("revealed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xp", sa.Integer(), nullable=False), sa.Column("review_key", sa.String(64)), if_not_exists=True)
    op.create_table("reel_learner_concepts",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("moment_id", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False), if_not_exists=True)


def downgrade():
    for name in ("reel_learner_concepts", "reel_attempts", "reel_moments", "reel_sources"):
        op.drop_table(name)
