"""telegram_pinned_timetables: keep the timetable on top of the chat's pins

Revision ID: gb3_pinned_timetable_on_top
Revises: gc1_group_calendars_feed_tokens
"""
import sqlalchemy as sa
from alembic import op

revision = "gb3_pinned_timetable_on_top"
down_revision = "gc1_group_calendars_feed_tokens"
branch_labels = None
depends_on = None

TABLE = "telegram_pinned_timetables"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("check_due_at", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("reported_pin_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("pin_checked_at", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("unpinned_seen_at", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("raising_from_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("raises", sa.Integer(), nullable=False, server_default="0"))
    op.add_column(TABLE, sa.Column("raise_attempts", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    for column in ("raise_attempts", "raises", "raising_from_id", "unpinned_seen_at", "pin_checked_at",
                   "reported_pin_id", "check_due_at"):
        op.drop_column(TABLE, column)
