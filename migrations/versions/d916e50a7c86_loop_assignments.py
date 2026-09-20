"""Record bounded assignments.
Revision ID: d916e50a7c86
Revises: c916d40f6b75
"""
from alembic import op
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text, UniqueConstraint

revision = "d916e50a7c86"
down_revision = "c916d40f6b75"
branch_labels = None
depends_on = None
metadata = MetaData()
loop_assignments = Table(
    "loop_assignments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(160), nullable=False),
    Column("code", String(160), nullable=False),
    Column("title", Text, nullable=False),
    Column("goal", Text, nullable=False),
    Column("allowed_files", Text, nullable=False),
    Column("acceptance", Text, nullable=False),
    Column("tier", String(16), nullable=False, server_default="work"),
    Column("status", String(16), nullable=False, server_default="assigned"),
    Column("builder_run_id", Integer),
    Column("result_sha256", String(64)),
    Column("commit_hash", String(40)),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    UniqueConstraint("owner", "code", name="loop_assignment_owner_code"),
    CheckConstraint("status IN ('assigned','built','reviewed','committed')", name="loop_assignment_status"),
    CheckConstraint("tier IN ('casual','work','life_changing')", name="loop_assignment_tier"),
)


def upgrade():
    loop_assignments.create(op.get_bind(), checkfirst=True)


def downgrade():
    loop_assignments.drop(op.get_bind(), checkfirst=True)
