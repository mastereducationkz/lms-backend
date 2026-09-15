"""group_google_calendars + calendar_feed_tokens — calendar subscriptions

Revision ID: gc1_group_calendars_feed_tokens
Revises: gb2_group_bot_v3_tables
"""
import sqlalchemy as sa
from alembic import op

revision = "gc1_group_calendars_feed_tokens"
down_revision = "gb2_group_bot_v3_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_google_calendars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("calendar_id", sa.String(), nullable=False, unique=True),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("synced_hash", sa.String(length=64), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "calendar_feed_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_calendar_feed_tokens_token", "calendar_feed_tokens", ["token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_calendar_feed_tokens_token", table_name="calendar_feed_tokens")
    op.drop_table("calendar_feed_tokens")
    op.drop_table("group_google_calendars")
