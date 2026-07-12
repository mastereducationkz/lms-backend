"""Re-apply the member trigger so it also fires on UPDATE (touch => re-emit member.upserted)

A student whose group memberships predate their SAT/IELTS account never gets those groups on the
platform: the original member.upserted 404-dead-lettered ("student not found") and account creation
emits nothing. The CRM now performs a no-op touch UPDATE on the student's group_students rows right
after provisioning succeeds; adding UPDATE to the trigger's event set turns that touch into a fresh
canonical member.upserted (same function, NEW values). LMS itself mutates membership as
delete+insert, so UPDATE never fires organically. CREATE OR REPLACE + DROP/CREATE TRIGGER of the
same objects; idempotent. The DDL lives in src/services/student_sync.py.

Revision ID: p14_member_sync_touch
Revises: p13_member_sync_teacher
Create Date: 2026-07-12
"""
from alembic import op

from src.services.student_sync import (
    MEMBER_SYNC_TRIGGER_UP_SQL,
)


revision = 'p14_member_sync_touch'
down_revision = 'p13_member_sync_teacher'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(MEMBER_SYNC_TRIGGER_UP_SQL)


def downgrade():
    # Strict superset of p8/p13 (an extra firing condition nothing organic exercises); leaving it
    # in place on downgrade is harmless, so this is a no-op like p11/p12/p13.
    pass
