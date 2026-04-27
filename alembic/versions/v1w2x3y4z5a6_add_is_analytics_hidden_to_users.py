"""add is_analytics_hidden to users

Revision ID: v1w2x3y4z5a6
Revises: u5v6w7x8y9z1
Create Date: 2026-04-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "v1w2x3y4z5a6"
down_revision: Union[str, Sequence[str], None] = "u5v6w7x8y9z1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_analytics_hidden", sa.Boolean(), nullable=True))
    op.execute("UPDATE users SET is_analytics_hidden = FALSE WHERE is_analytics_hidden IS NULL")
    op.alter_column("users", "is_analytics_hidden", nullable=False, server_default=sa.text("false"))


def downgrade() -> None:
    op.drop_column("users", "is_analytics_hidden")
