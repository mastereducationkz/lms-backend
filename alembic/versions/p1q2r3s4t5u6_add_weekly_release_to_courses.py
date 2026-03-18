"""add release_schedule to courses and week_number to modules

Revision ID: p1q2r3s4t5u6
Revises: c3d4e5f6a7b8
Create Date: 2026-03-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("release_schedule", sa.String(20), nullable=False, server_default="all"))
    op.add_column("modules", sa.Column("week_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("modules", "week_number")
    op.drop_column("courses", "release_schedule")
