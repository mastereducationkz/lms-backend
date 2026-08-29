"""Per-device refresh-token sessions.

Replaces the single users.refresh_token slot (one valid refresh token per user
TOTAL — every device logged every other device out) with independent per-device
chains that rotate in place and tolerate a short replay grace window.

Revision ID: rs1_refresh_sessions
Revises: ev1_events_timestamp_server_default
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "rs1_refresh_sessions"
down_revision: Union[str, Sequence[str], None] = "ev1_events_timestamp_server_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("previous_token", sa.String(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index("ix_refresh_sessions_token", "refresh_sessions", ["token"], unique=True)
    op.create_index("ix_refresh_sessions_previous_token", "refresh_sessions", ["previous_token"])


def downgrade() -> None:
    op.drop_index("ix_refresh_sessions_previous_token", table_name="refresh_sessions")
    op.drop_index("ix_refresh_sessions_token", table_name="refresh_sessions")
    op.drop_index("ix_refresh_sessions_user_id", table_name="refresh_sessions")
    op.drop_table("refresh_sessions")
