"""allow a planned exam date without a complete Assignment Zero

Revision ID: ex3_planned_date_stub
Revises: ex2_testimonials_reports
Create Date: 2026-08-09 00:00:00.000000

"Have you completed Assignment Zero?" had two different answers in the system:

  * ``users.assignment_zero_completed`` - the flag the UI reads, which is what makes
    the Assignment Zero page say "Already Completed!"
  * the existence of an ``assignment_zero_submissions`` row - what
    PATCH /assignment-zero/planned-date requires

They can disagree, and when they do a student who has genuinely finished Assignment
Zero is told to "complete Assignment Zero first" when they try to set their exam date -
with no way forward, because the page they are sent to says they are already done.

The planned-date endpoints now create a minimal submission row when one is missing.
Every other required column is a String that the codebase already stores as "" for
unknown values; ``birthday_date`` was the sole exception, and fabricating a birthday to
satisfy a NOT NULL is not acceptable. Relaxing it to nullable is safe (widening only)
and lets the stub row be honest about what it does not know.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ex3_planned_date_stub"
down_revision: Union[str, Sequence[str], None] = "ex2_testimonials_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "assignment_zero_submissions", "birthday_date",
        existing_type=sa.Date(), nullable=True,
    )


def downgrade() -> None:
    # Re-tightening would fail on any stub row created since. Backfill a sentinel first
    # so the downgrade is actually runnable; these rows never had a real birthday.
    op.execute(
        "UPDATE assignment_zero_submissions SET birthday_date = DATE '1900-01-01' "
        "WHERE birthday_date IS NULL"
    )
    op.alter_column(
        "assignment_zero_submissions", "birthday_date",
        existing_type=sa.Date(), nullable=False,
    )
