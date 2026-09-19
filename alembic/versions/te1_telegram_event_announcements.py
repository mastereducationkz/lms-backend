"""public Telegram announcements for LMS webinar events

Revision ID: te1_telegram_event_announcements
Revises: pr1_parent_reports
"""
from alembic import op
import sqlalchemy as sa

revision = "te1_telegram_event_announcements"
down_revision = "pr1_parent_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_event_announcements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("event_id", "support_group_id", name="uq_telegram_event_announcement_target"),
    )
    op.create_index("ix_telegram_event_announcements_event_id", "telegram_event_announcements", ["event_id"])
    op.create_index("ix_telegram_event_announcements_support_group_id", "telegram_event_announcements", ["support_group_id"])


def downgrade():
    op.drop_index("ix_telegram_event_announcements_support_group_id", table_name="telegram_event_announcements")
    op.drop_index("ix_telegram_event_announcements_event_id", table_name="telegram_event_announcements")
    op.drop_table("telegram_event_announcements")
