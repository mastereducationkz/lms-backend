"""Re-apply the group trigger so group.upserted also carries old_name (rename support)

IELTS keys its groups by NAME (no lms_group_id column) and its group consumer is update-only,
so a rename in LMS previously couldn't be found on IELTS (it looked up the NEW name) — leaving a
stale-named group that later SPLIT when new members joined under the new name. Carrying old_name
lets the IELTS consumer find the group by its previous name and rename it in place. SAT is keyed
by lms_group_id and is unaffected (it ignores old_name). CREATE OR REPLACE of the same function;
idempotent. The DDL lives in src/services/student_sync.py.

Revision ID: p12_group_sync_old_name
Revises: p11_group_sync_teacher
Create Date: 2026-07-12
"""
from alembic import op

from src.services.student_sync import (
    GROUP_SYNC_TRIGGER_UP_SQL,
)


revision = 'p12_group_sync_old_name'
down_revision = 'p11_group_sync_teacher'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute(GROUP_SYNC_TRIGGER_UP_SQL)


def downgrade():
    # The trigger is a strict superset of p11 (one extra payload field); leaving it in place on
    # downgrade is harmless (consumers ignore unknown fields), so this is a no-op like p11.
    pass
