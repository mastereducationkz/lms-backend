"""Add file_url to messages (chat attachments)

Revision ID: p1_message_file_url
Revises: p0_parent_push
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa


revision = 'p1_message_file_url'
down_revision = 'p0_parent_push'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('messages', sa.Column('file_url', sa.String(), nullable=True))


def downgrade():
    op.drop_column('messages', 'file_url')
