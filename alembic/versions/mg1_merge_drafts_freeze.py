"""Merge the assignment-drafts and scoped-enrollment-freeze migration branches.

PR #52 (hw-drafts-unit-gating) branched its migration chain off ``ca1_crm_audit_outbox``
while main had moved on to ``fz2_scoped_enrollment_freeze`` — two heads, and the container's
boot-time ``alembic upgrade head`` refuses to pick one, which held lms-backend in a crash
loop on deploy (2026-08-18). The branches touch disjoint tables, so this marker simply joins
them; it changes no schema.

Revision ID: mg1_merge_drafts_freeze
Revises: fz2_scoped_enrollment_freeze, 24171660c18a
"""
from typing import Sequence, Union

revision: str = "mg1_merge_drafts_freeze"
down_revision: Union[str, Sequence[str], None] = (
    "fz2_scoped_enrollment_freeze",
    "24171660c18a",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
