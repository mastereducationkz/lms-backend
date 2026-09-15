"""group bot v3: greetings, pinned timetables, schedule watch, digest sends

Revision ID: gb2_group_bot_v3_tables
Revises: gb1_group_bot_question_intent
"""
import sqlalchemy as sa
from alembic import op

revision = "gb2_group_bot_v3_tables"
down_revision = "gb1_group_bot_question_intent"
branch_labels = None
depends_on = None


def _group_fk():
    return sa.ForeignKey("groups.id", ondelete="CASCADE")


def upgrade() -> None:
    op.create_table(
        "telegram_group_greetings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), _group_fk(), nullable=False, unique=True),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("variant", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "telegram_pinned_timetables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), _group_fk(), nullable=False, unique=True),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "telegram_schedule_watch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), _group_fk(), nullable=False, unique=True),
        sa.Column("pattern_json", sa.Text(), nullable=False),
        sa.Column("pending_json", sa.Text(), nullable=True),
        sa.Column("pending_since", sa.DateTime(), nullable=True),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "telegram_digest_sends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lms_group_id", sa.Integer(), _group_fk(), nullable=False),
        sa.Column("support_group_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("lms_group_id", "kind", "key", name="uq_digest_send_group_kind_key"),
    )


def downgrade() -> None:
    op.drop_table("telegram_digest_sends")
    op.drop_table("telegram_schedule_watch")
    op.drop_table("telegram_pinned_timetables")
    op.drop_table("telegram_group_greetings")
