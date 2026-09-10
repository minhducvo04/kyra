"""Task checkpoint revisions and learning save receipts.

Revision ID: b721d430a9ef
Revises: 95948f84f255
"""
import sqlalchemy as sa
from alembic import context, op

revision = "b721d430a9ef"
down_revision = "95948f84f255"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Store startup may have created these additive tables before Alembic runs.
    # Never recreate them or discard their saved revisions/receipts.
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("checkpoint_revisions"):
        op.create_table(
            "checkpoint_revisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("revision", sa.Integer(), primary_key=True),
            sa.Column("task", sa.Text(), nullable=False),
            sa.Column("last_result", sa.Text(), nullable=False),
            sa.Column("next_action", sa.Text(), nullable=False),
            sa.Column("references", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.String(64), nullable=False),
            sa.CheckConstraint("revision > 0", name="positive_checkpoint_revision"),
        )
    if context.is_offline_mode() or not sa.inspect(op.get_bind()).has_table("learning_requests"):
        op.create_table(
            "learning_requests",
            sa.Column("request_id", sa.String(36), primary_key=True),
            sa.Column("result", sa.Text(), nullable=False),
        )



def downgrade() -> None:
    op.drop_table("learning_requests")
    op.drop_table("checkpoint_revisions")
