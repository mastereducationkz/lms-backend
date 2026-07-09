"""Add central_auth_user_id to users (stable IdP subject linkage — SSO Phase 2)

Revision ID: p5_central_auth_user_id
Revises: p4_group_week_offset
Create Date: 2026-07-10

Captures the central IdP (Zitadel) subject on each LMS user so OIDC logins can be
matched by a stable id rather than a fragile email string. Nullable + indexed;
backfilled at login. Distinct from the overloaded ``student_id`` column.
"""
from alembic import op
import sqlalchemy as sa


revision = 'p5_central_auth_user_id'
down_revision = 'p4_group_week_offset'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('central_auth_user_id', sa.String(), nullable=True))
    op.create_index('ix_users_central_auth_user_id', 'users', ['central_auth_user_id'], unique=False)


def downgrade():
    op.drop_index('ix_users_central_auth_user_id', table_name='users')
    op.drop_column('users', 'central_auth_user_id')
