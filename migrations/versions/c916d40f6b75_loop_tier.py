"""Store the declared tier on each run.
Revision ID: c916d40f6b75
Revises: b916d30f5a64
"""
from alembic import op
from sqlalchemy import Column, String, inspect

revision = "c916d40f6b75"
down_revision = "b916d30f5a64"
branch_labels = None
depends_on = None


def upgrade():
    if "tier" not in {c["name"] for c in inspect(op.get_bind()).get_columns("loop_runs")}:
        op.add_column("loop_runs", Column("tier", String(16), nullable=False, server_default="work"))


def downgrade():
    with op.batch_alter_table("loop_runs") as batch:
        batch.drop_column("tier")
