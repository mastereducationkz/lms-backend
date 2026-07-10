"""Add student_sync_outbox (cross-platform sync publisher) — SSO_SYNC_DESIGN.md

Revision ID: p6_student_sync_outbox
Revises: p5_central_auth_user_id
Create Date: 2026-07-10
"""
from alembic import op
import sqlalchemy as sa


revision = 'p6_student_sync_outbox'
down_revision = 'p5_central_auth_user_id'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'student_sync_outbox',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_id', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_student_sync_outbox_event_id', 'student_sync_outbox', ['event_id'], unique=True)
    op.create_index('ix_student_sync_outbox_next_attempt_at', 'student_sync_outbox', ['next_attempt_at'])
    op.create_index('ix_student_sync_outbox_status_next', 'student_sync_outbox', ['status', 'next_attempt_at'])


def downgrade():
    op.drop_index('ix_student_sync_outbox_status_next', table_name='student_sync_outbox')
    op.drop_index('ix_student_sync_outbox_next_attempt_at', table_name='student_sync_outbox')
    op.drop_index('ix_student_sync_outbox_event_id', table_name='student_sync_outbox')
    op.drop_table('student_sync_outbox')
