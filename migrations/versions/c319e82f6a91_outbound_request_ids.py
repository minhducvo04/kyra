"""Replace outbound content digests with random request identifiers.

Revision ID: c319e82f6a91
Revises: b119a04d7e21
"""
import secrets

import sqlalchemy as sa
from alembic import op

revision = "c319e82f6a91"
down_revision = "b119a04d7e21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = {column["name"] for column in sa.inspect(conn).get_columns("outbound_audit")}
    # Stores may have run create_all with the current schema before Alembic.
    if "request_id" not in columns:
        op.add_column("outbound_audit", sa.Column("request_id", sa.String(64), nullable=True))
    table = sa.table("outbound_audit", sa.column("id", sa.Integer), sa.column("request_id", sa.String(64)))
    last_id = 0
    while True:
        ids = conn.execute(sa.select(table.c.id).where(
            table.c.id > last_id, table.c.request_id.is_(None),
        ).order_by(table.c.id).limit(500)).scalars().all()
        if not ids:
            break
        for row_id in ids:
            conn.execute(table.update().where(table.c.id == row_id).values(request_id=secrets.token_urlsafe(16)))
        last_id = ids[-1]
    with op.batch_alter_table("outbound_audit") as batch:
        if "sha256" in columns:
            batch.drop_column("sha256")
        batch.alter_column("request_id", existing_type=sa.String(64), nullable=False)
        if "ix_outbound_audit_ts" not in {index["name"] for index in sa.inspect(conn).get_indexes("outbound_audit")}:
            batch.create_index("ix_outbound_audit_ts", ["ts"])


def downgrade() -> None:
    # Removed digests cannot be recovered; an empty legacy field conveys no content.
    with op.batch_alter_table("outbound_audit") as batch:
        batch.drop_index("ix_outbound_audit_ts")
        batch.add_column(sa.Column("sha256", sa.String(64), nullable=False, server_default=""))
        batch.drop_column("request_id")
