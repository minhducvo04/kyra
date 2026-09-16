"""Father document review tasks.

Revision ID: c83f029a174e
Revises: d210a93e7b61
"""
import sqlalchemy as sa
from alembic import context, op

revision = "c83f029a174e"
down_revision = "d210a93e7b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("father_tasks"):
        op.create_table(
            "father_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("slug", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("facts_json", sa.Text(), nullable=False),
            sa.Column("document_path", sa.Text(), nullable=False),
            sa.Column("report_json", sa.Text(), nullable=False),
            sa.Column("note", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(64), nullable=False),
            sa.Column("decided_at", sa.String(64)),
            sa.CheckConstraint("status IN ('review', 'approved', 'changes_requested')", name="father_task_status"),
        )


def downgrade() -> None:
    op.drop_table("father_tasks")
