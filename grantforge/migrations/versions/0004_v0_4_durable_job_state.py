"""v0.4 durable job state: job checkpoints and attempts, draft progress

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.add_column(sa.Column('checkpoint', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
    with op.batch_alter_table('matches') as batch_op:
        batch_op.add_column(sa.Column('draft_progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('matches') as batch_op:
        batch_op.drop_column('draft_progress')
    with op.batch_alter_table('jobs') as batch_op:
        batch_op.drop_column('attempts')
        batch_op.drop_column('checkpoint')
