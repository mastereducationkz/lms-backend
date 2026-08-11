"""teacher_hourly_rates

Mirror of the teacher pay rates the CRM decides («Ступенчатая оплата»): career level,
lesson kind, and — for group lessons — how many groups the teacher runs. Written only by
the CRM through its LMS-write session, the same way `users.official_full_name` already is.

The teacher dashboard's salary breakdown reads it so the rate stops being a number the
teacher types by hand. A teacher with no row keeps the previous behaviour (the rate passed
by the caller), so this is additive.

Revision ID: tlr1_teacher_hourly_rates
Revises: co1_curator_onboarding
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "tlr1_teacher_hourly_rates"
down_revision: Union[str, Sequence[str], None] = "co1_curator_onboarding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teacher_hourly_rates",
        sa.Column("teacher_id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(length=32), nullable=False, server_default="Level 1"),
        sa.Column("individual_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pair_rate", sa.Integer(), nullable=True),
        sa.Column("group_band", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["teacher_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("teacher_hourly_rates")
