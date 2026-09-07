"""lesson_requests: how an approved cancel was resolved, and the replacement lesson.

Approving a ``cancel`` request now records a decision: ``cancel_only`` (the lesson is gone
as if it never happened) or ``add_replacement`` (a new lesson is appended after the group's
last scheduled one). ``replacement_event_id`` points at that appended lesson so the teacher
can see when it landed.

Revision ID: lr1_cancel_resolution
Revises: nr1_student_access_blocks
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "lr1_cancel_resolution"
down_revision: Union[str, Sequence[str], None] = "nr1_student_access_blocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "fk_lesson_requests_replacement_event_id_events"


def upgrade() -> None:
    op.add_column(
        "lesson_requests",
        sa.Column("cancel_resolution", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "lesson_requests",
        sa.Column("replacement_event_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        _FK_NAME,
        "lesson_requests",
        "events",
        ["replacement_event_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, "lesson_requests", type_="foreignkey")
    op.drop_column("lesson_requests", "replacement_event_id")
    op.drop_column("lesson_requests", "cancel_resolution")
