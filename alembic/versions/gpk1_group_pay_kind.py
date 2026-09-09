"""group_pay_kinds

Mirror of the CRM's verdict on whether a group is one student's course or a class.

The question decides money on both sides — billing charges an individual lesson at a
different rate, and payroll pays one at a different rate — so the two systems giving
different answers is not a display bug. They did: the CRM reads a group's *starting roster*
(one student at its first marked lesson, and never a second since), while the LMS payslip
could only read `groups.group_type` and guess from the name. On production that disagreed on
«Indi Inayat & Tomiris SAT 2026» — two named students the name calls an indi — and on two
groups whose registers say nothing at all, where the name is the only evidence there is.

Neither system can be made exact alone: the LMS lacks the roster rule, and re-implementing
~150 lines of it here would drift. So the CRM pushes its verdict, the same way it already
pushes `teacher_hourly_rates` and `users.official_full_name`. A group with no row keeps the
LMS's previous behaviour, so this is additive.

Revision ID: gpk1_group_pay_kind
Revises: onb3_onboarding_pause
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "gpk1_group_pay_kind"
down_revision: Union[str, Sequence[str], None] = "onb3_onboarding_pause"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_pay_kinds",
        sa.Column("group_id", sa.Integer(), primary_key=True),
        # 'individual' or 'group' — the CRM's word, stored verbatim rather than as a boolean
        # so a third kind (the pay grid already has «Парное») needs no migration here.
        sa.Column("pay_kind", sa.String(length=16), nullable=False),
        # How the CRM knew: 'group_type', 'roster' or 'name'. Display and diagnosis only —
        # nothing branches on it, but «why is this an indi» is the first question asked.
        sa.Column("basis", sa.String(length=16), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("group_pay_kinds")
