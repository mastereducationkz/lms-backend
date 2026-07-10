"""Install the users->student_sync_outbox identity trigger — SSO_SYNC_DESIGN.md §identity

Enqueues user.upserted whenever a user's identity fields (name, email, role, is_active) change,
so SAT/IELTS accounts stop diverging when an LMS admin — or the CRM, whose edits land in this
same users table via its cross-DB writes — renames a student, fixes an email, or deactivates an
account. UPDATE-only (creation flows through CRM provisioning); password changes deliberately
emit nothing (Zitadel owns cross-platform passwords). The trigger SQL lives in
src/services/student_sync.py next to the drainer that publishes it.

Revision ID: p9_student_sync_user_trigger
Revises: p8_student_sync_member_trigger
Create Date: 2026-07-11
"""
from alembic import op

from src.services.student_sync import (
    USER_SYNC_TRIGGER_UP_SQL,
    USER_SYNC_TRIGGER_DOWN_SQL,
)


revision = 'p9_student_sync_user_trigger'
down_revision = 'p8_student_sync_member_trigger'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(USER_SYNC_TRIGGER_UP_SQL)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(USER_SYNC_TRIGGER_DOWN_SQL)
