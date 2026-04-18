"""add group_type to groups and course_type to courses

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2026-04-18 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s3t4u5v6w7x8"
down_revision: Union[str, Sequence[str], None] = "r2s3t4u5v6w7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("group_type", sa.String(length=32), nullable=False, server_default="group"),
    )
    op.alter_column("groups", "group_type", server_default=None)
    op.add_column(
        "courses",
        sa.Column("course_type", sa.String(length=32), nullable=False, server_default="general_english"),
    )
    op.alter_column("courses", "course_type", server_default=None)


def downgrade() -> None:
    op.drop_column("courses", "course_type")
    op.drop_column("groups", "group_type")
