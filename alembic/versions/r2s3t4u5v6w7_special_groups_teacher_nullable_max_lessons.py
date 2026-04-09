"""special groups: nullable teacher_id, max_open_lessons on course_group_access

Revision ID: r2s3t4u5v6w7
Revises: q7w8e9r0t1y2
Create Date: 2026-04-09 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r2s3t4u5v6w7"
down_revision: Union[str, Sequence[str], None] = "q7w8e9r0t1y2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "groups",
        "teacher_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "course_group_access",
        sa.Column("max_open_lessons", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("course_group_access", "max_open_lessons")
    op.alter_column(
        "groups",
        "teacher_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
