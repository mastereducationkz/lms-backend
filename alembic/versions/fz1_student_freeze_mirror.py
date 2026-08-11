"""student freeze mirror: read-only copy of the CRM's freeze lifecycle

The CRM owns whether a student is frozen, since when and until when. The LMS needs the same
facts on rosters, attendance views, dashboards and the curator leaderboard, where a live
lookup per page would put the CRM on the critical path of the LMS's busiest screens.

One row per student, not per period: history stays in the CRM and the LMS only ever asks
"is this student frozen right now". ``revision`` makes at-least-once delivery safe — a
delayed older message is dropped rather than overwriting newer state.

Additive and reversible; nothing else changes. Shipping it before the CRM sends anything
leaves the table empty, which reads as "nobody is frozen" — the correct answer until the CRM
says otherwise.

Revision ID: fz1_student_freeze_mirror
Revises: co2_onboarding_lifecycle
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "fz1_student_freeze_mirror"
down_revision: Union[str, None] = "co2_onboarding_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_freeze_state",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("freeze_start", sa.Date(), nullable=True),
        sa.Column("planned_resume_date", sa.Date(), nullable=True),
        sa.Column("actual_resume_date", sa.Date(), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("responsible_curator_id", sa.Integer(), nullable=True),
        sa.Column("crm_freeze_period_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_frozen", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("ix_student_freeze_state_user_id", "student_freeze_state", ["user_id"])
    op.create_index("ix_student_freeze_state_is_frozen", "student_freeze_state", ["is_frozen"])
    op.create_index(
        "ix_student_freeze_state_curator", "student_freeze_state", ["responsible_curator_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_student_freeze_state_curator", table_name="student_freeze_state")
    op.drop_index("ix_student_freeze_state_is_frozen", table_name="student_freeze_state")
    op.drop_index("ix_student_freeze_state_user_id", table_name="student_freeze_state")
    op.drop_table("student_freeze_state")
