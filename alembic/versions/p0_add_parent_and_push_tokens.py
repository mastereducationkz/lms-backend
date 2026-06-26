"""Add parent_students and user_push_tokens tables

Adds:
  - parent_students: links a parent user to a student user (parent role / mobile parent portal)
  - user_push_tokens: per-device Expo push tokens (multi-device support)

Backfills user_push_tokens from the legacy users.push_token column. The legacy
columns (users.push_token / users.device_type) are intentionally KEPT so the
existing web app keeps working during the transition.

Revision ID: p0_parent_push
Revises: v1w2x3y4z5a6
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'p0_parent_push'
down_revision = 'v1w2x3y4z5a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'parent_students',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('relationship', sa.String(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['parent_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('parent_id', 'student_id', name='uq_parent_students_parent_student'),
    )
    op.create_index('ix_parent_students_id', 'parent_students', ['id'])
    op.create_index('ix_parent_students_parent_id', 'parent_students', ['parent_id'])
    op.create_index('ix_parent_students_student_id', 'parent_students', ['student_id'])

    op.create_table(
        'user_push_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=True),
        sa.Column('device_name', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token', name='uq_user_push_tokens_token'),
    )
    op.create_index('ix_user_push_tokens_id', 'user_push_tokens', ['id'])
    op.create_index('ix_user_push_tokens_user_id', 'user_push_tokens', ['user_id'])
    op.create_index('ix_user_push_tokens_token', 'user_push_tokens', ['token'])

    # Backfill from legacy single-token column. Keep legacy columns intact.
    op.execute(
        """
        INSERT INTO user_push_tokens (user_id, token, platform, is_active, created_at, updated_at)
        SELECT id, push_token, COALESCE(NULLIF(device_type, ''), 'expo'), true, now(), now()
        FROM users
        WHERE push_token IS NOT NULL AND push_token <> ''
        ON CONFLICT (token) DO NOTHING
        """
    )


def downgrade():
    op.drop_index('ix_user_push_tokens_token', table_name='user_push_tokens')
    op.drop_index('ix_user_push_tokens_user_id', table_name='user_push_tokens')
    op.drop_index('ix_user_push_tokens_id', table_name='user_push_tokens')
    op.drop_table('user_push_tokens')
    op.drop_index('ix_parent_students_student_id', table_name='parent_students')
    op.drop_index('ix_parent_students_parent_id', table_name='parent_students')
    op.drop_index('ix_parent_students_id', table_name='parent_students')
    op.drop_table('parent_students')
