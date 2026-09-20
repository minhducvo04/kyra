"""Shared tool run audit.

Revision ID: e310b72f4a60
Revises: d210a93e7b61
"""
import sqlalchemy as sa
from alembic import op

revision = "e310b72f4a60"
down_revision = "d210a93e7b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tool", sa.String(128), nullable=False),
        sa.Column("args", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("tool_runs", if_exists=True)
