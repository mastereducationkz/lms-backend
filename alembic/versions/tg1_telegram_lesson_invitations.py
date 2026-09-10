"""telegram lesson invitations: group ↔ chat links and the per-lesson send log

Revision ID: tg1_telegram_lesson_invitations
Revises: rec2_recording_poster
Create Date: 2026-09-10

The LMS posts each LMS Meet lesson's invitation into its group's Telegram chat 5 minutes before
it starts (via the Support platform's bot). Two new tables, nothing else touched: which chat
is which group's, and which invitations went out.
"""
from alembic import op
import sqlalchemy as sa


revision = "tg1_telegram_lesson_invitations"
down_revision = "rec2_recording_poster"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_group_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("chat_title", sa.String(), nullable=True),
        sa.Column("linked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_telegram_group_links_support_group_id", "telegram_group_links", ["support_group_id"])

    op.create_table(
        "telegram_lesson_invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("event_id", "lms_group_id", name="uq_lesson_invitation_event_group"),
    )
    op.create_index("ix_telegram_lesson_invitations_event_id", "telegram_lesson_invitations", ["event_id"])


def downgrade():
    op.drop_index("ix_telegram_lesson_invitations_event_id", table_name="telegram_lesson_invitations")
    op.drop_table("telegram_lesson_invitations")
    op.drop_index("ix_telegram_group_links_support_group_id", table_name="telegram_group_links")
    op.drop_table("telegram_group_links")
