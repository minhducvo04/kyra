"""initiative proposals and reminder receipts

Revision ID: c910a21d8f04
Revises: 95948f84f255
Create Date: 2026-09-10 01:58:36.558945
"""
import sqlalchemy as sa
from alembic import op


revision = 'c910a21d8f04'
down_revision = '95948f84f255'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('initiative_reminder_receipts',
    sa.Column('initiative_id', sa.String(length=64), nullable=False),
    sa.Column('reminder_id', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('initiative_id'), if_not_exists=True
    )
    op.create_table('initiatives',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('reminder_id', sa.Integer(), nullable=True),
    sa.Column('last_seen', sa.String(length=10), nullable=False),
    sa.CheckConstraint("status IN ('proposed', 'accepting', 'accepted', 'dismissed', 'expired')", name='initiative_status'),
    sa.PrimaryKeyConstraint('id'), if_not_exists=True
    )

    op.create_table('initiative_snapshot',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('day', sa.String(length=10), nullable=False), if_not_exists=True)

def downgrade() -> None:
    op.drop_table('initiative_snapshot')
    op.drop_table('initiatives')
    op.drop_table('initiative_reminder_receipts')
