"""Content-free outbound release audit.

Revision ID: a319c72d5e80
Revises: d5973dc728ab
"""
import sqlalchemy as sa
from alembic import op

revision = "a319c72d5e80"
down_revision = "d5973dc728ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbound_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.String(64), nullable=False),
        sa.Column("destination", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("would_refuse", sa.Boolean(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("classes", sa.Text(), nullable=False),
        sa.Column("refused", sa.Text(), nullable=False),
        sa.Column("flags", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("outbound_audit", if_exists=True)
