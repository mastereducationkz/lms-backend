"""Re-apply the group trigger with teacher/curator identity + fire on teacher_id/curator_id change

The p7 group trigger only carried name/program/is_active and never fired on a teacher/curator
re-assignment — so synced SAT/IELTS groups had no teacher and teachers couldn't see their students.
This CREATE OR REPLACEs the trigger function (same name as p7) to also enqueue on teacher_id/
curator_id changes and to embed the teacher/curator email+name in the group.upserted payload.
Idempotent; the DDL lives in src/services/student_sync.py.

Revision ID: p11_group_sync_teacher
Revises: p10_student_sync_user_created
Create Date: 2026-07-11
"""
from alembic import op

from src.services.student_sync import (
    GROUP_SYNC_TRIGGER_UP_SQL,
)


revision = 'p11_group_sync_teacher'
down_revision = 'p10_student_sync_user_created'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(GROUP_SYNC_TRIGGER_UP_SQL)


def downgrade():
    # Non-reversible in practice (would need the p7 body verbatim); the newer trigger is a strict
    # superset (extra fields + extra fire conditions), so leaving it in place on downgrade is safe.
    pass
