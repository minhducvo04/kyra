"""Bind assignment plans, reviews and build worktrees.

Revision ID: f916a61b8d97
Revises: ecb1b431ebce
"""
from alembic import op
from sqlalchemy import Column, Integer, Text, inspect

revision = "f916a61b8d97"
down_revision = "ecb1b431ebce"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in inspect(op.get_bind()).get_columns("loop_assignments")}
    for name, kind in (("plan_run_id", Integer), ("review_run_id", Integer), ("worktree", Text)):
        if name not in existing:
            op.add_column("loop_assignments", Column(name, kind, nullable=True))


def downgrade():
    existing = {c["name"] for c in inspect(op.get_bind()).get_columns("loop_assignments")}
    with op.batch_alter_table("loop_assignments") as batch:
        for name in ("worktree", "review_run_id", "plan_run_id"):
            if name in existing:
                batch.drop_column(name)
