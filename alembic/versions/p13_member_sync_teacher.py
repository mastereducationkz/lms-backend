"""Re-apply the member trigger so member.upserted also carries the group's teacher/curator

A name-keyed consumer (IELTS) creates a group lazily on the first student join. Previously the
member.upserted payload had no teacher, so the freshly-created group was teacher-less until a later
group edit re-emitted group.upserted — meaning a group whose teacher was set at creation and never
edited stayed unlinked on IELTS. Embedding the group's teacher/curator (same resolution as p11's
group trigger) links the teacher the moment a student joins. Additive fields; SAT ignores them
(it creates groups on group.upserted). CREATE OR REPLACE of the same function; idempotent. The
DDL lives in src/services/student_sync.py.

Revision ID: p13_member_sync_teacher
Revises: p12_group_sync_old_name
Create Date: 2026-07-12
"""
from alembic import op

from src.services.student_sync import (
    MEMBER_SYNC_TRIGGER_UP_SQL,
)


revision = 'p13_member_sync_teacher'
down_revision = 'p12_group_sync_old_name'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(MEMBER_SYNC_TRIGGER_UP_SQL)


def downgrade():
    # Strict superset of p8/p11 (extra payload fields only); leaving it in place on downgrade is
    # harmless (consumers ignore unknown fields), so this is a no-op like p11/p12.
    pass
