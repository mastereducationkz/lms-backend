"""add official_full_name to users (CRM ФИО sync)

Revision ID: of1_official_name
Revises: gc1_group_chat_tables
Create Date: 2026-07-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "of1_official_name"
down_revision: Union[str, Sequence[str], None] = "gc1_group_chat_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("official_full_name", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "official_full_name")
