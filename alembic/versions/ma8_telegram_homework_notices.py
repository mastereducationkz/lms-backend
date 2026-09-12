"""telegram homework notices: a group's chat is told when new homework is published

Revision ID: ma8_telegram_homework_notices
Revises: ma7_lesson_change_notices
Create Date: 2026-09-12

One table. Queued right after an assignment's own transaction commits, sent by the existing
minute-ticker alongside the invitation and lesson-change-notice jobs.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma8_telegram_homework_notices"
down_revision = "ma7_lesson_change_notices"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_homework_notices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("assignment_id", "lms_group_id", name="uq_homework_notice_assignment_group"),
    )


def downgrade():
    op.drop_table("telegram_homework_notices")
