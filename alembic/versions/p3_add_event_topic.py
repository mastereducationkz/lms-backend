"""Add editable topic column to events

Revision ID: p3_event_topic
Revises: p2_video_hls
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa


revision = 'p3_event_topic'
down_revision = 'p2_video_hls'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('events', sa.Column('topic', sa.String(), nullable=True))


def downgrade():
    op.drop_column('events', 'topic')
