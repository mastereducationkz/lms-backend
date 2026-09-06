"""student_access_blocks: windows of «no platform access», mirrored from the CRM.

The CRM turns a non-renewing student's LMS login off and on. This table records *when* it
was off so the curator leaderboard can keep those lessons out of the attendance
denominator, the way it already does for freeze days. Written by the CRM in the same
transaction as the ``users.is_active`` flip; read by ``src/curator/access_blocks.py``.

Revision ID: nr1_student_access_blocks
Revises: p23_platform_test_events
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "nr1_student_access_blocks"
down_revision: Union[str, Sequence[str], None] = "p23_platform_test_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_access_blocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("blocked_from", sa.Date(), nullable=False),
        sa.Column("blocked_until", sa.Date(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_student_access_blocks_user_id", "student_access_blocks", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_student_access_blocks_user_id", table_name="student_access_blocks")
    op.drop_table("student_access_blocks")
