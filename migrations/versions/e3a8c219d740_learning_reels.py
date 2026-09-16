"""Learning reel sources, frozen moments and learner recall.

Revision ID: e3a8c219d740
Revises: d210a93e7b61
"""
import sqlalchemy as sa
from alembic import context, op

revision = "e3a8c219d740"
down_revision = "d210a93e7b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("reel_sources"):
        op.create_table(
            "reel_sources",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("url", sa.Text, nullable=False),
            sa.Column("external_id", sa.Text, nullable=False),
            sa.Column("title", sa.Text, nullable=False),
            sa.Column("author", sa.Text, nullable=False),
            sa.Column("rights_state", sa.String(32), nullable=False),
            sa.Column("release_state", sa.String(32), nullable=False),
            sa.Column("transcript_origin", sa.String(16), nullable=False),
            sa.Column("created_at", sa.String(64), nullable=False),
            sa.Column("thumbnail_url", sa.Text),
            sa.Column("transcript", sa.Text, nullable=False),
            sa.Column("transcript_sha256", sa.String(64), nullable=False),
        )
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("reel_moments"):
        op.create_table(
            "reel_moments",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("source_id", sa.Integer, nullable=False),
            sa.Column("start_s", sa.Float),
            sa.Column("end_s", sa.Float),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("body", sa.Text, nullable=False),
            sa.CheckConstraint("status IN ('proposed', 'approved', 'rejected')", name="reel_moment_status"),
        )
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("reel_attempts"):
        op.create_table(
            "reel_attempts",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(128), nullable=False),
            sa.Column("moment_id", sa.Integer, nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("chosen", sa.Text),
            sa.Column("correct", sa.Integer, nullable=False),
            sa.Column("hinted", sa.Integer, nullable=False),
            sa.Column("revealed", sa.Integer, nullable=False),
            sa.Column("review_first", sa.Integer, nullable=False),
            sa.Column("review_completed", sa.Integer, nullable=False),
            sa.Column("correction_bonus", sa.Integer, nullable=False),
            sa.Column("xp", sa.Integer, nullable=False),
            sa.Column("at", sa.String(64), nullable=False),
        )
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("reel_learner_concepts"):
        op.create_table(
            "reel_learner_concepts",
            sa.Column("user_id", sa.String(128), primary_key=True),
            sa.Column("moment_id", sa.Integer, primary_key=True),
            sa.Column("body", sa.Text, nullable=False),
        )


def downgrade() -> None:
    op.drop_table("reel_learner_concepts")
    op.drop_table("reel_attempts")
    op.drop_table("reel_moments")
    op.drop_table("reel_sources")
