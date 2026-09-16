"""Bind independent reviews to bounded earlier-turn evidence.
Revision ID: b916d30f5a64
Revises: a916c29e4f53
"""
from alembic import op
from sqlalchemy import Column, Text, inspect

revision = "b916d30f5a64"
down_revision = "a916c29e4f53"
branch_labels = None
depends_on = None


def upgrade():
    if "review_context" not in {c["name"] for c in inspect(op.get_bind()).get_columns("loop_runs")}:
        op.add_column("loop_runs", Column("review_context", Text))


def downgrade():
    with op.batch_alter_table("loop_runs") as batch:
        batch.drop_column("review_context")
