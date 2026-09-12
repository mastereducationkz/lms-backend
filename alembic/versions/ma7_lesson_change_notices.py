"""telegram lesson change notices: reschedule/cancel/substitution notices to a group's chat

Revision ID: ma7_lesson_change_notices
Revises: ma6_telegram_group_questions
Create Date: 2026-09-12

One table. Queued inside the transaction that applies an approved lesson request, sent by the
existing minute-ticker alongside the invitation job.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma7_lesson_change_notices"
down_revision = "ma6_telegram_group_questions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_lesson_change_notices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lesson_request_id", sa.Integer(), sa.ForeignKey("lesson_requests.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("old_start_datetime", sa.DateTime(), nullable=True),
        sa.Column("new_start_datetime", sa.DateTime(), nullable=True),
        sa.Column("old_teacher_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("new_teacher_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("lesson_request_id", "lms_group_id", name="uq_lesson_change_notice_request_group"),
    )
    op.create_index("ix_telegram_lesson_change_notices_event_id", "telegram_lesson_change_notices", ["event_id"])


def downgrade():
    op.drop_index("ix_telegram_lesson_change_notices_event_id", table_name="telegram_lesson_change_notices")
    op.drop_table("telegram_lesson_change_notices")
