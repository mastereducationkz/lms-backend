"""add is_over flag to groups

Revision ID: q7w8e9r0t1y2
Revises: p1q2r3s4t5u6
Create Date: 2026-03-25 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "q7w8e9r0t1y2"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("is_over", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("groups", "is_over", server_default=None)


def downgrade() -> None:
    op.drop_column("groups", "is_over")
