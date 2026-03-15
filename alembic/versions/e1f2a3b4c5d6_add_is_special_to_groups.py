"""add is_special flag to groups

Revision ID: e1f2a3b4c5d6
Revises: 2518438537dd
Create Date: 2026-03-15 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "2518438537dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("is_special", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("groups", "is_special", server_default=None)


def downgrade() -> None:
    op.drop_column("groups", "is_special")
