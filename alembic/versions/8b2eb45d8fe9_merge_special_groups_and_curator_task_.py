"""merge special-groups and curator-task heads

Revision ID: 8b2eb45d8fe9
Revises: e1f2a3b4c5d6, j2k3l4m5n6o7
Create Date: 2026-03-15 12:14:30.904167

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b2eb45d8fe9'
down_revision: Union[str, Sequence[str], None] = ('e1f2a3b4c5d6', 'j2k3l4m5n6o7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
