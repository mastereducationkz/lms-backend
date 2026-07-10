"""Install the groups->student_sync_outbox enqueue trigger — SSO_SYNC_DESIGN.md §9

Enqueue for cross-platform group sync is done in the DB (not the app) so that the CRM's
cross-DB writes to the LMS `groups` table are captured too, not just LMS-native writes.
The trigger SQL lives in src/services/student_sync.py so the payload it produces stays in
lockstep with the drainer that publishes it (and can be unit-tested against Postgres).

Revision ID: p7_student_sync_group_trigger
Revises: p6_student_sync_outbox
Create Date: 2026-07-10
"""
from alembic import op

from src.services.student_sync import (
    GROUP_SYNC_TRIGGER_UP_SQL,
    GROUP_SYNC_TRIGGER_DOWN_SQL,
)


revision = 'p7_student_sync_group_trigger'
down_revision = 'p6_student_sync_outbox'
branch_labels = None
depends_on = None


def upgrade():
    # Postgres-only (PL/pgSQL trigger). SQLite dev/test DBs simply skip it — enqueue there is
    # exercised by inserting outbox rows directly in the drainer tests.
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(GROUP_SYNC_TRIGGER_UP_SQL)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(GROUP_SYNC_TRIGGER_DOWN_SQL)
