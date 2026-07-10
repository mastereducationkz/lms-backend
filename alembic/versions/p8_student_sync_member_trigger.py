"""Install the group_students->student_sync_outbox membership trigger — SSO_SYNC_DESIGN.md §10

Enqueues member.upserted / member.removed when a student is added to / removed from a group,
so membership + derived exam access can sync to SAT/NUET (and later IELTS). Same DB-trigger
approach as the group trigger (p7) so CRM cross-DB membership writes are captured too. The
trigger SQL lives in src/services/student_sync.py.

Revision ID: p8_student_sync_member_trigger
Revises: p7_student_sync_group_trigger
Create Date: 2026-07-10
"""
from alembic import op

from src.services.student_sync import (
    MEMBER_SYNC_TRIGGER_UP_SQL,
    MEMBER_SYNC_TRIGGER_DOWN_SQL,
)


revision = 'p8_student_sync_member_trigger'
down_revision = 'p7_student_sync_group_trigger'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(MEMBER_SYNC_TRIGGER_UP_SQL)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(MEMBER_SYNC_TRIGGER_DOWN_SQL)
