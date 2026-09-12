"""group bot: every question asked in a group chat and what the bot answered

Revision ID: ma6_telegram_group_questions
Revises: ma5_transcript_words
Create Date: 2026-09-12

One table. It is both the audit trail — what a bot has been telling students — and the record
behind a question handed to a curator.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma6_telegram_group_questions"
down_revision = "ma5_transcript_words"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_group_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("chat_title", sa.String(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("asker_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("asker_username", sa.String(), nullable=True),
        sa.Column("asker_name", sa.String(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("private_hint", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("handed_to_curator", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_telegram_group_questions_lms_group_id", "telegram_group_questions", ["lms_group_id"])


def downgrade():
    op.drop_index("ix_telegram_group_questions_lms_group_id", table_name="telegram_group_questions")
    op.drop_table("telegram_group_questions")
