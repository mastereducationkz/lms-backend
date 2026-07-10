"""Install the users INSERT -> student_sync_outbox trigger (user.created) — Zitadel auto-provision

Companion to p9 (UPDATE-only): new accounts — created by LMS admin endpoints, the CRM's direct
cross-DB inserts, or the amoCRM webhook — enqueue a user.created event so the drainer provisions
them into Zitadel and "Continue with Master Education" works from day one. No-op delivery when
ZITADEL_PAT is unset (the bulk importer reconciles later). The trigger SQL lives in
src/services/student_sync.py.

Revision ID: p10_student_sync_user_created
Revises: p9_student_sync_user_trigger
Create Date: 2026-07-11
"""
from alembic import op

from src.services.student_sync import (
    USER_CREATED_TRIGGER_UP_SQL,
    USER_CREATED_TRIGGER_DOWN_SQL,
)


revision = 'p10_student_sync_user_created'
down_revision = 'p9_student_sync_user_trigger'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(USER_CREATED_TRIGGER_UP_SQL)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(USER_CREATED_TRIGGER_DOWN_SQL)
