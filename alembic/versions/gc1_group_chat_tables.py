"""group chat tables

Revision ID: gc1_group_chat_tables
Revises: w7x8y9z1a2b3
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'gc1_group_chat_tables'
down_revision = 'w7x8y9z1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'group_conversations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('groups.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('group_id', 'kind', name='uq_group_conversation_group_kind'),
    )
    op.create_index('ix_group_conversations_group_id', 'group_conversations', ['group_id'])

    op.create_table(
        'group_conversation_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('group_conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.UniqueConstraint('conversation_id', 'user_id', name='uq_group_conv_member'),
    )
    op.create_index('ix_group_conversation_members_conversation_id', 'group_conversation_members', ['conversation_id'])
    op.create_index('ix_group_conversation_members_user_id', 'group_conversation_members', ['user_id'])

    op.create_table(
        'group_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.Integer(), sa.ForeignKey('group_conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('file_url', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )
    op.create_index('idx_group_messages_conv_created', 'group_messages', ['conversation_id', 'created_at'])


def downgrade():
    op.drop_table('group_messages')
    op.drop_table('group_conversation_members')
    op.drop_table('group_conversations')
