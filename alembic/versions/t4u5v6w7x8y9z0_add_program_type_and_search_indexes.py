"""program_type on groups + indexes for course_type and program_type

Revision ID: t4u5v6w7x8y9z0
Revises: s3t4u5v6w7x8
Create Date: 2026-04-18 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t4u5v6w7x8y9z0"
down_revision: Union[str, Sequence[str], None] = "s3t4u5v6w7x8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_courses_course_type", "courses", ["course_type"], unique=False)
    op.add_column(
        "groups",
        sa.Column("program_type", sa.String(length=32), nullable=False, server_default="general_english"),
    )
    op.alter_column("groups", "program_type", server_default=None)
    op.create_index("ix_groups_program_type", "groups", ["program_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_groups_program_type", table_name="groups")
    op.drop_column("groups", "program_type")
    op.drop_index("ix_courses_course_type", table_name="courses")
