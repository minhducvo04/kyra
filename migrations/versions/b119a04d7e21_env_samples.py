"""Five-minute environment samples with explicit quality codes.

Revision ID: b119a04d7e21
Revises: a319c72d5e80
"""
import sqlalchemy as sa
from alembic import op

revision = "b119a04d7e21"
down_revision = "a319c72d5e80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "env_samples",
        sa.Column("slot", sa.String(64), primary_key=True),
        sa.Column("entity", sa.String(128), primary_key=True),
        sa.Column("metric", sa.String(64), primary_key=True),
        sa.Column("value", sa.Float()),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("quality", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.String(64), nullable=False),
        sa.CheckConstraint("quality IN ('ok', 'stale', 'unavailable', 'not_configured')", name="env_sample_quality"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("env_samples", if_exists=True)
