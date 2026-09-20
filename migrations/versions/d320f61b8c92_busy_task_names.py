"""Normalize legacy task table names to busy mode.

Revision ID: d320f61b8c92
Revises: c319e82f6a91
"""
import sqlalchemy as sa
from alembic import op

revision = "d320f61b8c92"
down_revision = "c319e82f6a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "father_tasks" not in tables:
        return
    if "busy_tasks" in tables:
        raise RuntimeError("Both legacy and busy task tables exist; reconcile them before upgrading")
    # Batch recreation on SQLite preserves rows while replacing its named CHECK.
    with op.batch_alter_table("father_tasks") as batch:
        batch.drop_constraint("father_task_status", type_="check")
        batch.create_check_constraint("busy_task_status", "status IN ('review', 'approved', 'changes_requested')")
    op.rename_table("father_tasks", "busy_tasks")


def downgrade() -> None:
    # The predecessor now creates busy_tasks too. Keep that schema and its rows.
    pass
