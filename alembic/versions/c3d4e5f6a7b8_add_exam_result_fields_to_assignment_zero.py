"""add exam result collection fields to assignment zero

Revision ID: c3d4e5f6a7b8
Revises: 8b2eb45d8fe9
Create Date: 2026-03-15 13:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "8b2eb45d8fe9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assignment_zero_submissions", sa.Column("sat_planned_test_date", sa.Date(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("sat_result_score", sa.String(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("sat_result_test_date", sa.Date(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("ielts_planned_test_date", sa.Date(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("ielts_result_score", sa.String(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("ielts_result_test_date", sa.Date(), nullable=True))
    op.add_column("assignment_zero_submissions", sa.Column("ielts_last_date_prompted_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("assignment_zero_submissions", "ielts_last_date_prompted_at")
    op.drop_column("assignment_zero_submissions", "ielts_result_test_date")
    op.drop_column("assignment_zero_submissions", "ielts_result_score")
    op.drop_column("assignment_zero_submissions", "ielts_planned_test_date")
    op.drop_column("assignment_zero_submissions", "sat_result_test_date")
    op.drop_column("assignment_zero_submissions", "sat_result_score")
    op.drop_column("assignment_zero_submissions", "sat_planned_test_date")
