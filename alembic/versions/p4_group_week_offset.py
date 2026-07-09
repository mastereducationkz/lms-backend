"""Add weekly_set_week_offset to groups

Revision ID: p4_group_week_offset
Revises: p3_event_topic
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa


revision = 'p4_group_week_offset'
down_revision = 'p3_event_topic'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('groups', sa.Column('weekly_set_week_offset', sa.Integer(),
                                      nullable=False, server_default='0'))


def downgrade():
    op.drop_column('groups', 'weekly_set_week_offset')
