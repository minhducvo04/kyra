"""Record explicit native continuation and reserve one child per parent.
Revision ID: a916c29e4f53
Revises: f915b18d3e42
"""
from alembic import op
from sqlalchemy import Column, Integer, String, inspect

revision = "a916c29e4f53"
down_revision = "f915b18d3e42"
branch_labels = None
depends_on = None


def upgrade():
    inspector = inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("loop_runs")}
    constraints = {c["name"] for c in inspector.get_unique_constraints("loop_runs")}
    # Startup can have created the current table before Alembic reaches this revision.
    with op.batch_alter_table("loop_runs") as batch:
        if "continued_from_run_id" not in columns:
            batch.add_column(Column("continued_from_run_id", Integer))
        if "requested_session_id" not in columns:
            batch.add_column(Column("requested_session_id", String(160)))
        if "loop_one_child_per_parent" not in constraints:
            batch.create_unique_constraint("loop_one_child_per_parent", ["continued_from_run_id"])


def downgrade():
    with op.batch_alter_table("loop_runs") as batch:
        batch.drop_constraint("loop_one_child_per_parent", type_="unique")
        batch.drop_column("requested_session_id")
        batch.drop_column("continued_from_run_id")
